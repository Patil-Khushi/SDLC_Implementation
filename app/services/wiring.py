"""Deterministic post-generation wiring reconciliation (no LLM).

Each work item is generated in its OWN isolated LLM call, so cross-file WIRING is routinely left
undone even when the individual files are correct: an Express app factory ships with its module
routers commented out (or never mounted), so no endpoint is reachable — the audit's issue 2a. This
module repairs that deterministically AFTER every item is generated, as pure logic over the
``{path: content}`` file set, returning ONLY the files it changed (empty dict → nothing to do).

Conservative by design: it acts only when the gap is unambiguous, edits CommonJS Express entries
only (the shape these packs generate), and is idempotent — re-running over its own output changes
nothing. Anything it is not sure about is left untouched (and surfaced by the caller as a note),
never rewritten on a guess.

Same family as ``plan_builder`` / ``boilerplate``: deterministic, side-effect free, unit-testable
without an executor or a model.
"""

from __future__ import annotations

import json
import posixpath
import re

_ROUTER_RE = re.compile(r"\.(routes|router)\.(js|ts|mjs|cjs)$", re.IGNORECASE)
_SOURCE_EXT_RE = re.compile(r"\.(js|ts|mjs|cjs)$", re.IGNORECASE)
_APP_ENTRY_BASENAMES = ("app.js", "app.ts", "app.mjs", "app.cjs")
_SERVER_ENTRY_BASENAMES = ("server.js", "server.ts", "server.mjs", "server.cjs")
#: The exact commented placeholder the isolated app-factory item tends to emit, e.g.
#: ``// app.use('/api/module', require('./modules/module/module.router'));`` — removed on wiring.
_PLACEHOLDER_RE = re.compile(r"^\s*//\s*\w+\.use\(\s*['\"]/api/\w+['\"].*require\(", re.IGNORECASE)


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def _is_test(path: str) -> bool:
    base = _basename(path).lower()
    if ".test." in base or ".spec." in base:
        return True
    segs = path.lower().split("/")
    return "__tests__" in segs or "tests" in segs


def _is_router_module(path: str) -> bool:
    """A per-feature Express router file (``orders.routes.js``), excluding tests and the aggregator
    ``routes/index.js`` (which the entry mounts as a whole, not per-module)."""
    if _is_test(path) or not _ROUTER_RE.search(_basename(path)):
        return False
    stem = _ROUTER_RE.sub("", _basename(path)).lower()
    return stem not in ("index",)


def _mount_name(path: str) -> str:
    """The ``/api/<name>`` segment for a router: its file stem, or its parent module dir name."""
    segs = [s for s in path.split("/") if s]
    stem = _ROUTER_RE.sub("", segs[-1])
    parent = segs[-2] if len(segs) >= 2 else ""
    if stem and stem.lower() not in ("index", "routes", "router"):
        return stem
    if parent and parent.lower() not in ("routes", "router", "src", "app"):
        return parent
    return stem or parent or "api"


def _rel_require(from_file: str, to_file: str) -> str:
    """A ``require``-style relative specifier from ``from_file`` to ``to_file`` (extension dropped)."""
    from_dir = from_file.rsplit("/", 1)[0] if "/" in from_file else ""
    rel = posixpath.relpath(to_file, from_dir) if from_dir else to_file
    rel = _SOURCE_EXT_RE.sub("", rel.replace("\\", "/"))
    return rel if rel.startswith(".") else "./" + rel


def _find_app_entry(files: dict[str, str]) -> str | None:
    """The CommonJS Express app-factory file to wire routers into, or ``None`` if there isn't a
    single clear one. Prefers ``app.*`` over ``server.*``; requires ``express()`` + CommonJS."""
    def _candidates(basenames: tuple[str, ...]) -> list[str]:
        out = [
            p for p, c in files.items()
            if _basename(p).lower() in basenames
            and "express(" in c
            and ("require(" in c or "module.exports" in c)  # CommonJS only (v1)
            and re.search(r"(\w+)\s*=\s*express\(", c)
        ]
        return sorted(out, key=lambda p: (p.count("/"), len(p)))

    return next(iter(_candidates(_APP_ENTRY_BASENAMES) or _candidates(_SERVER_ENTRY_BASENAMES)), None)


def _app_var(content: str) -> str:
    m = re.search(r"(\w+)\s*=\s*express\(", content)
    return m.group(1) if m else "app"


def reconcile_express_routers(files: dict[str, str]) -> dict[str, str]:
    """Mount every generated feature router in the Express app factory that doesn't already mount it.

    Returns ``{path: new_content}`` for the entry file IF it changed, else ``{}``. Idempotent: a
    router already referenced (by relative specifier or basename) is skipped, so a second pass is a
    no-op. Only CommonJS Express entries are touched; anything else returns ``{}`` (reported, not
    rewritten).
    """
    entry = _find_app_entry(files)
    if entry is None:
        return {}
    content = files[entry]
    routers = sorted(p for p in files if _is_router_module(p))
    if not routers:
        return {}

    app = _app_var(content)
    additions: list[str] = []
    for router in routers:
        spec = _rel_require(entry, router)
        stem = _SOURCE_EXT_RE.sub("", _basename(router))
        # Already wired? Match the relative specifier or the router's basename-stem in a require().
        if spec in content or re.search(rf"require\([^)]*{re.escape(stem)}[^)]*\)", content):
            continue
        additions.append(f"{app}.use('/api/{_mount_name(router)}', require('{spec}'));")
    if not additions:
        return {}

    lines = content.split("\n")
    indent = "  "
    # Drop the commented placeholder line(s) the isolated factory emitted.
    lines = [ln for ln in lines if not _PLACEHOLDER_RE.match(ln)]
    block = [f"{indent}{a}" for a in additions]

    anchor = next((i for i, ln in enumerate(lines) if "module routers" in ln.lower()), None)
    if anchor is not None:
        insert_at = anchor + 1
    else:
        # Before the 404/error catch-all, else before the module export / `return app`, else append.
        insert_at = next(
            (i for i, ln in enumerate(lines)
             if re.search(rf"{re.escape(app)}\.use\(\s*\(", ln)          # app.use((req,res,next)=>...)
             or "module.exports" in ln or re.search(r"\breturn\s+" + re.escape(app) + r"\b", ln)),
            len(lines),
        )
        block = [f"{indent}// Module routers (wired by reconciliation)", *block]
    new_content = "\n".join(lines[:insert_at] + block + lines[insert_at:])
    if new_content == content:
        return {}
    return {entry: new_content}


def _semver_major(spec: str) -> int | None:
    """Leading major version of an npm range (``^9.1.0`` → 9, ``>=8`` → 8); ``None`` if not numeric
    (a tag/url/``*`` — deliberately not reasoned about)."""
    m = re.search(r"(\d+)", str(spec).lstrip("^~>=<v "))
    return int(m.group(1)) if m and str(spec).lstrip("^~>=<v ")[:1].isdigit() else None


def reconcile_package_peerdeps(files: dict[str, str]) -> dict[str, str]:
    """Fix the one peer-dep conflict the audit found: ESLint >= 9 with
    ``eslint-plugin-react-hooks`` < 5 (its peer range caps at ESLint 8), which makes a clean
    ``npm install`` fail with ERESOLVE unless forced (issue 6a). Bumps the plugin to a v5 range so
    the manifest installs cleanly. Returns changed ``package.json`` files only; idempotent.
    """
    changed: dict[str, str] = {}
    for path, content in files.items():
        if _basename(path).lower() != "package.json" or "node_modules/" in path:
            continue
        try:
            pkg = json.loads(content)
        except (ValueError, TypeError):
            continue
        if not isinstance(pkg, dict):
            continue
        edited = False
        for section in ("devDependencies", "dependencies"):
            deps = pkg.get(section)
            if not isinstance(deps, dict):
                continue
            eslint_major = _semver_major(deps.get("eslint", "")) if "eslint" in deps else None
            hooks_major = _semver_major(deps.get("eslint-plugin-react-hooks", "")) if "eslint-plugin-react-hooks" in deps else None
            if eslint_major is not None and eslint_major >= 9 and hooks_major is not None and hooks_major < 5:
                deps["eslint-plugin-react-hooks"] = "^5.1.0"
                edited = True
        if edited:
            changed[path] = json.dumps(pkg, indent=2, ensure_ascii=False) + "\n"
    return changed


def reconcile_wiring(files: dict[str, str]) -> dict[str, str]:
    """Run all deterministic wiring fixers over ``files``; return the union of changed files.

    Fixers: Express router registration (issue 2a) and package.json peer-dep repair (issue 6a).
    Kept as the single entry point so more fixers (frontend entry mounting the real router, import
    normalization) can be added here later without changing the caller.
    """
    changed: dict[str, str] = {}
    changed.update(reconcile_express_routers({**files, **changed}))
    changed.update(reconcile_package_peerdeps({**files, **changed}))
    return changed
