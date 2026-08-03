"""Deterministic post-generation wiring reconciliation (no LLM).

Each work item is generated in its OWN isolated LLM call, so cross-file WIRING is routinely left
undone even when the individual files are correct: an Express app factory ships with its module
routers commented out (or never mounted), so no endpoint is reachable — the audit's issue 2a. This
module repairs that deterministically AFTER every item is generated, as pure logic over the
``{path: content}`` file set, returning ONLY the files it changed (empty dict → nothing to do).

The same isolation breaks the MANIFEST the same way: every item writes its own view of
``package.json``, so the surviving one declares only that item's packages while the other items'
code imports ``pg`` / ``bcrypt`` / ``jsonwebtoken`` / ``react-redux`` / … — a run that needed ten
manual ``npm install`` rescues, each discovered by an expensive LLM debugging pass reading one test
stack trace at a time. :func:`reconcile_package_dependencies` closes that gap by pure logic: read
what the generated code actually imports, diff it against what the manifest declares, add the
difference. And :func:`find_unresolved_imports` reports the sibling failure — files importing each
other under names nobody generated (``src/utils/AppError``) or under two conventions for one module
(``src/db`` vs ``src/config/db``) — as findings, WITHOUT inventing the missing file, because
choosing between "add a shim" and "rename the importer" is a judgement call that belongs to the
debugging agent, not to a deterministic pass.

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
from dataclasses import dataclass
from typing import Any

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


# --- import scanning (shared by the dependency fixer and the unresolved-import report) ----------

_JS_SOURCE_RE = re.compile(r"\.(js|jsx|ts|tsx|mjs|cjs|mts|cts)$", re.IGNORECASE)
#: Extensions a bare specifier may resolve to, in Node/bundler resolution order.
_RESOLVE_EXTS = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".mts", ".cts", ".json")
#: TypeScript's NodeNext convention: source imports ``./x.js`` but the file on disk is ``./x.ts``.
_TS_REWRITES = {".js": (".ts", ".tsx", ".jsx"), ".mjs": (".mts",), ".cjs": (".cts",)}

#: Node builtins are importable without ever being declared in package.json, so they must never be
#: mistaken for a missing dependency. Matched on the FIRST path segment after the optional ``node:``
#: prefix, which covers ``fs/promises``, ``stream/web``, ``timers/promises``, ``node:crypto``, ….
_NODE_BUILTINS = frozenset({
    "assert", "async_hooks", "buffer", "child_process", "cluster", "console", "constants", "crypto",
    "dgram", "diagnostics_channel", "dns", "domain", "events", "fs", "http", "http2", "https",
    "inspector", "module", "net", "os", "path", "perf_hooks", "process", "punycode", "querystring",
    "readline", "repl", "stream", "string_decoder", "sys", "test", "timers", "tls", "trace_events",
    "tty", "url", "util", "v8", "vm", "wasi", "worker_threads", "zlib",
})
#: Specifier schemes that are not npm packages at all.
_NON_PACKAGE_SCHEMES = ("http:", "https:", "data:", "file:", "blob:")
#: npm name grammar, lowercased by policy since npm rejects uppercase for new packages. Enforcing it
#: is a SAFETY net, not pedantry: a generated file that does ``require('AppError')`` (a local class
#: name, not a package) must not turn into an uninstallable dependency that breaks ``npm install``
#: for the whole project — that specifier is a code bug for the debugging agent, not a manifest gap.
_NPM_NAME_RE = re.compile(r"^(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*$")

#: Curated ``name -> version range`` map. WHY it exists: this pass is pure/offline (CLAUDE.md rule
#: 5 — nothing here may shell out or reach the registry), so a correct "latest" range cannot be
#: looked up. Guessing a pin would be worse than useless (a wrong pin fails ``npm install`` exactly
#: like the missing dependency did), so known packages of this stack get the SAME ranges the
#: scaffold already pins in ``app.services.boilerplate`` — keeping generated manifests consistent
#: with the scaffolded ones — and anything unknown falls back to ``"*"``, which is always
#: installable and invents nothing. Adding a package is a data edit here, not a code change.
_NPM_VERSIONS: dict[str, str] = {
    # express / node backend
    "express": "^4.19.2", "cors": "^2.8.5", "helmet": "^7.1.0", "morgan": "^1.10.0",
    "compression": "^1.7.4", "cookie-parser": "^1.4.6", "dotenv": "^16.4.5",
    "express-validator": "^7.1.0", "express-rate-limit": "^7.4.0", "express-session": "^1.18.0",
    "multer": "^1.4.5-lts.1", "nodemailer": "^6.9.14", "swagger-ui-express": "^5.0.1",
    "socket.io": "^4.7.5", "socket.io-client": "^4.7.5", "winston": "^3.14.2",
    # validation / utilities
    "joi": "^17.13.3", "zod": "^3.23.8", "uuid": "^10.0.0", "axios": "^1.7.7",
    "lodash": "^4.17.21", "dayjs": "^1.11.13", "date-fns": "^3.6.0", "slugify": "^1.6.6",
    # auth / crypto
    "jsonwebtoken": "^9.0.2", "bcryptjs": "^2.4.3", "bcrypt": "^5.1.1", "passport": "^0.7.0",
    "passport-jwt": "^4.0.1",
    # data stores
    "pg": "^8.12.0", "pg-hstore": "^2.3.4", "mysql2": "^3.11.0", "knex": "^3.1.0",
    "sequelize": "^6.37.3", "mongoose": "^8.5.1", "redis": "^4.7.0", "ioredis": "^5.4.1",
    "@prisma/client": "^5.19.0", "@elastic/elasticsearch": "^8.15.0",
    # react frontend
    "react": "^18.3.0", "react-dom": "^18.3.0", "react-router-dom": "^6.26.0",
    "react-redux": "^9.1.2", "redux": "^5.0.1", "@reduxjs/toolkit": "^2.2.7",
    "zustand": "^4.5.5", "recharts": "^2.12.7", "chart.js": "^4.4.4",
    "react-chartjs-2": "^5.2.0", "react-hook-form": "^7.53.0", "formik": "^2.4.6",
    "yup": "^1.4.0", "clsx": "^2.1.1", "classnames": "^2.5.1", "react-icons": "^5.3.0",
    "styled-components": "^6.1.13", "framer-motion": "^11.5.4", "react-toastify": "^10.0.5",
    "@tanstack/react-query": "^5.55.0", "@mui/material": "^5.16.7",
    "@mui/icons-material": "^5.16.7", "@emotion/react": "^11.13.3",
    "@emotion/styled": "^11.13.0", "@headlessui/react": "^2.1.8", "bootstrap": "^5.3.3",
    "react-bootstrap": "^2.10.4",
    # tooling / test (usually devDependencies)
    "jest": "^29.7.0", "supertest": "^7.0.0", "vitest": "^2.0.5", "nodemon": "^3.1.4",
    "cross-env": "^7.0.3", "concurrently": "^9.0.1", "jsdom": "^25.0.0", "ts-node": "^10.9.2",
    "typescript": "^5.5.4", "vite": "^5.4.3", "eslint": "^9.9.0", "prettier": "^3.3.3",
    "tailwindcss": "^3.4.10", "autoprefixer": "^10.4.20", "postcss": "^8.4.45",
    "mongodb-memory-server": "^10.0.0", "@faker-js/faker": "^9.0.1",
    "@vitejs/plugin-react": "^4.3.1", "@testing-library/react": "^16.0.1",
    "@testing-library/jest-dom": "^6.5.0", "@testing-library/user-event": "^14.5.2",
}
_UNKNOWN_VERSION = "*"      # always installable; never a wrong pin

#: Every import form the generated packs emit. ``require``/dynamic ``import()`` are matched anywhere
#: (they are expressions); the ``from``-clause and bare ``import 'x'`` forms are anchored to a
#: statement start AND to end-of-line so JSX attributes (``<Route from="/old" to=…>``) and
#: ``Array.from('abc')`` cannot masquerade as module specifiers. ``[^;]{0,400}?`` lets the clause
#: span the newlines of a multi-line ``import { a, b } from 'x';`` while a ``;`` (statement end)
#: stops it running into the next statement.
_IMPORT_RE = re.compile(
    r"""
      \brequire(?:\.resolve)?\s*\(\s*(?P<req>['"][^'"\n]+['"])\s*\)
    | \bimport\s*\(\s*(?P<dyn>['"][^'"\n]+['"])\s*\)
    | ^[^\S\n]*(?:import|export)\b[^;]{0,400}?\bfrom\s*(?P<frm>['"][^'"\n]+['"])[^\S\n]*;?[^\S\n]*$
    | ^[^\S\n]*import\s+(?P<bare>['"][^'"\n]+['"])[^\S\n]*;?[^\S\n]*$
    """,
    re.VERBOSE | re.MULTILINE,
)


def _strip_comments(text: str) -> str:
    """Blank out ``//`` and ``/* */`` comments, string-literal aware, preserving line structure.

    Necessary because a COMMENTED-OUT import must not count: the isolated app-factory item ships
    ``// app.use('/api/x', require('./modules/x/x.router'));``, which would otherwise be reported as
    an unresolvable import against a module that was never meant to exist. String awareness keeps
    ``'https://…'`` from eating the rest of the line.

    Deliberately not a JS parser: an apostrophe in JSX text or a quote inside a regex literal can
    desynchronize the scanner. That only ever HIDES imports further down the file (never invents
    one), and imports sit at the top of these files, above any JSX or regex.
    """
    out: list[str] = []
    i, n = 0, len(text)
    quote: str | None = None
    while i < n:
        ch = text[i]
        if quote is not None:
            out.append(ch)
            if ch == "\\" and i + 1 < n:            # escaped char: consume both, verbatim
                out.append(text[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        nxt = text[i + 1] if i + 1 < n else ""
        if ch == "/" and nxt == "/":
            while i < n and text[i] != "\n":        # drop to EOL; the newline itself is kept
                i += 1
            continue
        if ch == "/" and nxt == "*":
            i += 2
            while i < n and not (text[i] == "*" and i + 1 < n and text[i + 1] == "/"):
                if text[i] == "\n":                 # keep newlines so ^/$ anchors stay aligned
                    out.append("\n")
                i += 1
            i += 2
            continue
        if ch in "'\"`":
            quote = ch
        out.append(ch)
        i += 1
    return "".join(out)


def _imported_specifiers(content: str) -> list[str]:
    """Every module specifier ``content`` imports, in first-seen order, deduplicated."""
    seen: set[str] = set()
    out: list[str] = []
    for m in _IMPORT_RE.finditer(_strip_comments(content)):
        raw = m.group("req") or m.group("dyn") or m.group("frm") or m.group("bare")
        if not raw:
            continue
        spec = raw[1:-1].strip()
        if spec and spec not in seen:
            seen.add(spec)
            out.append(spec)
    return out


def _is_js_source(path: str) -> bool:
    return bool(_JS_SOURCE_RE.search(_basename(path))) and "node_modules/" not in path


def _norm(path: str) -> str:
    """Project-relative POSIX form (``./src/a.js`` and ``src\\a.js`` → ``src/a.js``)."""
    p = path.replace("\\", "/").strip()
    return posixpath.normpath(p) if p else p


def _resolve_module(base: str, paths: set[str]) -> str | None:
    """Node/bundler resolution of a normalized, extension-less-or-not path against a file SET:
    exact hit, then ``base.<ext>``, then ``base/index.<ext>``, then TypeScript's ``.js`` → ``.ts``
    rewrite. Returns the file it resolves to, or ``None``."""
    if base in paths:
        return base
    for ext in _RESOLVE_EXTS:
        if base + ext in paths:
            return base + ext
    for ext in _RESOLVE_EXTS:
        if f"{base}/index{ext}" in paths:
            return f"{base}/index{ext}"
    stem, dot, ext = base.rpartition(".")
    if dot:
        for alt in _TS_REWRITES.get("." + ext.lower(), ()):
            if stem + alt in paths:
                return stem + alt
    return None


# --- dependency reconciliation (missing npm packages) --------------------------------------------

def _loads_jsonc(text: str) -> dict | None:
    """``json.loads`` for config files that are JSON-with-comments in practice (tsconfig.json).
    Returns ``None`` (never raises) when it still doesn't parse — an unreadable config just means
    "no aliases known", which the caller treats conservatively."""
    for candidate in (text, re.sub(r",(\s*[}\]])", r"\1", _strip_comments(text))):
        try:
            data = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict):
            return data
    return None


def _alias_segments(files: dict[str, str], pkg_dir: str, pkg: dict) -> frozenset[str]:
    """First path segments the PROJECT maps to itself, which must never be read as package names.

    ``@/components/Button`` is a tsconfig/bundler alias, not the ``@`` scope of an npm package;
    ``#db`` is a package.json subpath import. ``@`` and ``~`` are always aliases (neither is a legal
    npm name), the rest come from ``compilerOptions.paths`` in the nearest tsconfig/jsconfig.
    """
    out = {"@", "~"}
    for cfg in ("tsconfig.json", "jsconfig.json"):
        for candidate in {posixpath.join(pkg_dir, cfg) if pkg_dir else cfg, cfg}:
            if candidate not in files:
                continue
            data = _loads_jsonc(files[candidate]) or {}
            options = data.get("compilerOptions")
            mapped = options.get("paths") if isinstance(options, dict) else None
            for key in mapped if isinstance(mapped, dict) else ():
                seg = str(key).split("/", 1)[0].strip()
                if seg and seg != "*":
                    out.add(seg)
    imports = pkg.get("imports")
    if isinstance(imports, dict):
        out.update(str(k).split("/", 1)[0] for k in imports)
    return frozenset(out)


def _package_name(spec: str, aliases: frozenset[str]) -> str | None:
    """The npm package a specifier names, or ``None`` when it isn't an external package.

    Filters relative/absolute paths, URLs, ``#`` subpath imports, project aliases and node builtins;
    reduces subpaths (``lodash/debounce`` → ``lodash``, ``@scope/pkg/sub`` → ``@scope/pkg``).
    """
    spec = spec.strip()
    if not spec or spec[0] in "./#" or spec.startswith(_NON_PACKAGE_SCHEMES):
        return None
    bare = spec[5:] if spec.lower().startswith("node:") else spec
    segments = [s for s in bare.split("/") if s != ""]
    if not segments:
        return None
    if segments[0] in aliases or bare.split("/", 1)[0] in aliases:
        return None
    if segments[0].startswith("@"):
        if len(segments) < 2:
            return None                                   # ``@`` alone / ``@/x`` — never a package
        name = f"{segments[0]}/{segments[1]}"
    else:
        name = segments[0]
    if name in _NODE_BUILTINS or not _NPM_NAME_RE.match(name):
        return None
    return name


def _manifests(files: dict[str, str]) -> dict[str, tuple[str, dict]]:
    """``{package.json path: (its directory, parsed object)}`` for every manifest in the file set
    (``""`` = repo root). Unparseable or non-object manifests are skipped, never rewritten."""
    out: dict[str, tuple[str, dict]] = {}
    for path, content in files.items():
        if _basename(path).lower() != "package.json" or "node_modules/" in path:
            continue
        try:
            pkg = json.loads(content)
        except (ValueError, TypeError):
            continue
        if isinstance(pkg, dict):
            norm = _norm(path)
            out[path] = (norm.rsplit("/", 1)[0] if "/" in norm else "", pkg)
    return out


def _owning_manifest(path: str, manifests: dict[str, tuple[str, dict]]) -> str | None:
    """The manifest a source file's dependencies belong to: the NEAREST package.json above it, so a
    monorepo (``backend/`` + ``frontend/``) sends each import to the right side. ``None`` when the
    file sits above every manifest — where the answer would be a guess."""
    norm = _norm(path)
    best: str | None = None
    best_len = -1
    for manifest, (pkg_dir, _) in manifests.items():
        if pkg_dir and not norm.startswith(pkg_dir + "/"):
            continue
        if len(pkg_dir) > best_len:
            best, best_len = manifest, len(pkg_dir)
    return best


def _declared(pkg: dict) -> set[str]:
    """Every package name the manifest already knows about, in any dependency section — a package
    declared as a peer/optional dep must not be re-added to ``dependencies``."""
    names: set[str] = set()
    for section in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        deps = pkg.get(section)
        if isinstance(deps, dict):
            names.update(str(k) for k in deps)
    bundled = pkg.get("bundledDependencies") or pkg.get("bundleDependencies")
    if isinstance(bundled, list):
        names.update(str(k) for k in bundled)
    return names


def _json_style(text: str) -> tuple[str, bool]:
    """The manifest's own ``(indent, ends-with-newline)`` so a rewrite keeps its formatting instead
    of reflowing every untouched line into a diff."""
    m = re.search(r"^([ \t]+)\"", text, re.MULTILINE)
    return (m.group(1) if m else "  "), text.endswith("\n")


def _merge_dep_section(pkg: dict, section: str, additions: dict[str, str]) -> None:
    """Add ``additions`` to ``pkg[section]``, preserving the section's existing style: an
    already-alphabetical section (what npm writes) stays alphabetical; a hand-ordered one keeps its
    order and takes the new names, sorted, at the end. Deterministic either way."""
    existing = pkg.get(section) if isinstance(pkg.get(section), dict) else {}
    merged = {**existing, **{n: v for n, v in sorted(additions.items()) if n not in existing}}
    keys = list(existing)
    if keys == sorted(keys):
        merged = {k: merged[k] for k in sorted(merged)}
    pkg[section] = merged


def _dep_version(name: str) -> str | None:
    """The range to declare a missing package with, or ``None`` to leave it alone.

    An UNKNOWN scoped name is far more likely a project alias whose config we couldn't read than a
    real package, and a bogus ``@x/y`` fails ``npm install`` for the WHOLE project — strictly worse
    than the single missing import. So unknown scoped names are skipped (left to the debugging
    agent) while unknown bare names get ``"*"``.
    """
    if name in _NPM_VERSIONS:
        return _NPM_VERSIONS[name]
    return None if name.startswith("@") else _UNKNOWN_VERSION


def reconcile_package_dependencies(files: dict[str, str]) -> dict[str, str]:
    """Declare the packages the generated code actually imports but the manifest never listed.

    The real failure this fixes: each work item is generated in its own isolated LLM call and writes
    its own ``package.json`` view, so the surviving manifest declared only one item's packages and
    the run needed ten manual ``npm install`` rescues (``pg``, ``bcrypt``, ``jsonwebtoken``,
    ``express-validator``, ``react-redux``, ``recharts``, ``helmet``, ``cors``, ``compression``,
    ``supertest``) — each one found by an LLM debugging pass reading a single stack trace. Every one
    of those is derivable by pure logic from the imports already sitting in the generated files.

    Scans every JS/TS source for its external imports, attributes each to the NEAREST package.json
    (so a monorepo's backend and frontend stay separate), and adds what is missing: imports seen
    only in tests go to ``devDependencies``, everything else to ``dependencies``. Versions come from
    the curated ``_NPM_VERSIONS`` map, ``"*"`` otherwise — never a guessed pin.

    Returns changed ``package.json`` files only (``{}`` when every import is already declared), so a
    second pass over its own output is a no-op.
    """
    manifests = _manifests(files)
    if not manifests:
        return {}
    all_paths = {_norm(p) for p in files}
    aliases = {m: _alias_segments(files, pkg_dir, pkg) for m, (pkg_dir, pkg) in manifests.items()}
    prod: dict[str, set[str]] = {m: set() for m in manifests}
    dev: dict[str, set[str]] = {m: set() for m in manifests}

    for path, content in sorted(files.items()):
        if not _is_js_source(path):
            continue
        manifest = _owning_manifest(path, manifests)
        if manifest is None:
            continue                                    # above every manifest — not ours to decide
        pkg_dir = manifests[manifest][0]
        bucket = dev[manifest] if _is_test(path) else prod[manifest]
        for spec in _imported_specifiers(content):
            name = _package_name(spec, aliases[manifest])
            if name is None:
                continue
            # A bare specifier that resolves to a GENERATED file is the project importing itself
            # through a bundler/jest root (``moduleDirectories: ['src']``), not a package to install.
            if _resolve_module(_norm(posixpath.join(pkg_dir, spec)), all_paths) or _resolve_module(
                _norm(posixpath.join(pkg_dir, "src", spec)), all_paths
            ):
                continue
            bucket.add(name)

    changed: dict[str, str] = {}
    for manifest, (_pkg_dir, pkg) in manifests.items():
        declared = _declared(pkg) | {str(pkg.get("name") or "")}    # never self-depend
        missing_prod = sorted(prod[manifest] - declared)
        missing_dev = sorted(dev[manifest] - prod[manifest] - declared)
        adds_prod = {n: v for n in missing_prod if (v := _dep_version(n)) is not None}
        adds_dev = {n: v for n in missing_dev if (v := _dep_version(n)) is not None}
        if not adds_prod and not adds_dev:
            continue
        if adds_prod:
            _merge_dep_section(pkg, "dependencies", adds_prod)
        if adds_dev:
            _merge_dep_section(pkg, "devDependencies", adds_dev)
        indent, trailing_nl = _json_style(files[manifest])
        text = json.dumps(pkg, indent=indent, ensure_ascii=False) + ("\n" if trailing_nl else "")
        if text != files[manifest]:
            changed[manifest] = text
    return changed


# --- unresolvable local imports (report-only) ----------------------------------------------------

@dataclass(frozen=True)
class UnresolvedImport:
    """A relative import that points at nothing in the generated file set.

    Report-only on purpose: the two real cases — a module nobody generated
    (``src/utils/AppError``) and one generated under a different convention (``src/db.js`` vs
    ``src/config/db.js``) — are fixed either by creating the file or by renaming the importer, and
    choosing between them is a judgement call for the LLM debugging agent. ``candidates`` carries
    the same-basename files that already exist, which is the evidence for that call.
    """

    importer: str                       #: the file containing the import (as keyed in ``files``)
    specifier: str                      #: the specifier exactly as written (``'../config/db'``)
    target: str                         #: normalized project-relative path it points at
    candidates: tuple[str, ...] = ()    #: existing files whose basename matches — likely intent

    def as_note(self) -> str:
        """One-line, human-readable form for a ``generation_summary`` note or a log line."""
        hint = f" (did you mean {', '.join(self.candidates)}?)" if self.candidates else ""
        return f"{self.importer} imports '{self.specifier}' -> no such module {self.target}{hint}"

    def to_dict(self) -> dict[str, Any]:
        """Plain-dict form for storage in ``WorkflowState.unresolved_imports`` — a TypedDict that
        flows through the LangGraph checkpointer, which is not obliged to round-trip an arbitrary
        dataclass. ``candidates`` becomes a list (not a tuple) for the same reason."""
        return {
            "importer": self.importer,
            "specifier": self.specifier,
            "target": self.target,
            "candidates": list(self.candidates),
        }


def _basename_matches(target: str, files_by_stem: dict[str, list[str]]) -> tuple[str, ...]:
    """Existing files whose basename-stem equals the missing module's last segment (case-insensitive
    — a case-only mismatch is itself a real failure on the Linux sandbox)."""
    stem = _basename(target).lower()
    stem = stem.rsplit(".", 1)[0] if "." in stem else stem
    return tuple(sorted(files_by_stem.get(stem, []))[:5])


def find_unresolved_imports(files: dict[str, str]) -> list[UnresolvedImport]:
    """Every RELATIVE import in the generated code that resolves to no generated file.

    Deliberately NOT a fixer: it returns findings for the caller to surface (a
    ``generation_summary`` note / debugging-agent input) and changes nothing, which is also why it
    is a separate entry point — :func:`reconcile_wiring` keeps returning exactly the changed-files
    dict its caller consumes today.

    Conservative: only ``./`` and ``../`` specifiers are judged (a bare specifier is a package
    question, answered by :func:`reconcile_package_dependencies`), and a path that escapes the file
    set's root is skipped, since nothing outside the set is visible here. Deterministic order.
    """
    all_paths = {_norm(p) for p in files}
    files_by_stem: dict[str, list[str]] = {}
    for path in files:
        base = _basename(_norm(path))
        stem = (base.rsplit(".", 1)[0] if "." in base else base).lower()
        files_by_stem.setdefault(stem, []).append(_norm(path))

    found: dict[tuple[str, str], UnresolvedImport] = {}
    for path, content in sorted(files.items()):
        if not _is_js_source(path):
            continue
        from_dir = posixpath.dirname(_norm(path))
        for spec in _imported_specifiers(content):
            if not (spec.startswith("./") or spec.startswith("../")):
                continue
            target = _norm(posixpath.join(from_dir, spec))
            if target.startswith("..") or target in (".", ""):
                continue                                # outside the known tree — can't judge it
            if _resolve_module(target, all_paths):
                continue
            found[(path, spec)] = UnresolvedImport(
                importer=path,
                specifier=spec,
                target=target,
                candidates=_basename_matches(target, files_by_stem),
            )
    return [found[k] for k in sorted(found)]


def target_resolves(target: str, paths: "set[str] | list[str]") -> bool:
    """True if ``target`` (an :class:`UnresolvedImport`'s ``target`` field) now resolves against
    ``paths`` — a plain path collection, normalized here exactly like :func:`find_unresolved_imports`
    normalizes its own file set.

    Lets a caller that only has a growing list of paths (not full file contents — e.g. the
    Debugging agent's ``generated_code``) cheaply re-check a single finding after a round writes a
    new file, without re-scanning every generated file's imports (see
    ``app.agents.debugging._prune_unresolved``).
    """
    normalized = {_norm(p) for p in paths}
    return _resolve_module(_norm(target), normalized) is not None


def reconcile_wiring(files: dict[str, str]) -> dict[str, str]:
    """Run all deterministic wiring fixers over ``files``; return the union of changed files.

    Fixers: Express router registration (issue 2a), missing npm dependency declaration, and
    package.json peer-dep repair (issue 6a) — each fixer sees the previous fixers' output, so the
    dependency pass reads the routers the first fixer just wired in and the peer-dep pass reads the
    manifest the second one just extended.

    Kept as the single entry point so more fixers can be added here without changing the caller;
    the return type stays ``{path: new_content}``. Report-only analysis that changes no file lives
    in its own entry point (:func:`find_unresolved_imports`) rather than here.
    """
    changed: dict[str, str] = {}
    changed.update(reconcile_express_routers({**files, **changed}))
    changed.update(reconcile_package_dependencies({**files, **changed}))
    changed.update(reconcile_package_peerdeps({**files, **changed}))
    return changed
