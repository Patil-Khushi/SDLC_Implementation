"""Python / FastAPI half of the deterministic wiring pass (no LLM) — see ``app.services.wiring``.

The reconcile pass was JavaScript-only, so a generated FastAPI backend reached the debugging loop
with every cross-file defect still in it. The Fleet product is the evidence: ``main.py`` (generated
first, before any router existed) did ``from app.routers import auth, vehicles, ...`` when the
routers are ``app.modules.<name>.router``, so the app could not even be imported; and it mounted
``include_router(depots.router, prefix="/depots")`` on routers that already declare
``APIRouter(prefix="/depots")``, so every route was served at ``/depots/depots``.

Same contract as the JS side: pure functions over ``{path: content}``, fixers return ONLY changed
files and act only when exactly one repair is possible, analysis returns findings and changes
nothing. Regex rather than ``ast`` on purpose — a syntax error in one generated file must not
blind the pass to every other file.

Import resolution follows the project's real import roots: the parent of each top-level package
(a directory with ``__init__.py`` whose parent has none), so ``backend/app/modules/auth/router.py``
is ``app.modules.auth.router``. Directories without ``__init__.py`` are namespace packages, as
Python treats them. Only imports whose first segment is one of the project's own top-level names
are judged; everything else is a package question for :func:`reconcile_python_requirements`.
"""

from __future__ import annotations

import posixpath
import re
import sys
from dataclasses import dataclass, field

_STDLIB = frozenset(getattr(sys, "stdlib_module_names", ())) | {"__future__"}

#: Import name -> requirement to declare. Curated on purpose: a guessed PyPI name that does not
#: exist fails ``pip install -r`` for the WHOLE project, which is worse than one missing import.
PIP_PACKAGES = {
    "fastapi": "fastapi", "uvicorn": "uvicorn[standard]", "starlette": "starlette", "sqlalchemy": "SQLAlchemy",
    "sqlmodel": "sqlmodel",
    "alembic": "alembic", "pydantic": "pydantic", "pydantic_settings": "pydantic-settings",
    "jose": "python-jose[cryptography]", "jwt": "PyJWT", "passlib": "passlib[bcrypt]", "bcrypt": "bcrypt",
    "argon2": "argon2-cffi", "dotenv": "python-dotenv", "httpx": "httpx", "requests": "requests",
    "psycopg2": "psycopg2-binary", "psycopg": "psycopg[binary]", "asyncpg": "asyncpg", "aiosqlite": "aiosqlite",
    "pymongo": "pymongo", "motor": "motor", "redis": "redis", "celery": "celery",
    "email_validator": "email-validator", "multipart": "python-multipart", "yaml": "PyYAML",
    "dateutil": "python-dateutil", "pytz": "pytz", "pytest": "pytest", "pytest_asyncio": "pytest-asyncio",
    "faker": "Faker", "factory": "factory-boy", "freezegun": "freezegun", "respx": "respx",
    "slowapi": "slowapi", "limits": "limits", "structlog": "structlog", "loguru": "loguru", "PIL": "Pillow",
    "boto3": "boto3", "sentry_sdk": "sentry-sdk", "aiofiles": "aiofiles", "orjson": "orjson",
    "gunicorn": "gunicorn", "tenacity": "tenacity", "apscheduler": "APScheduler", "jinja2": "Jinja2",
    "itsdangerous": "itsdangerous", "cryptography": "cryptography", "cachetools": "cachetools",
    "prometheus_client": "prometheus-client", "flask": "Flask", "django": "Django", "marshmallow": "marshmallow",
}
#: Installed by the packages above; importing them is fine and declaring them is unnecessary.
_IMPLICIT = {"typing_extensions", "anyio", "pydantic_core", "annotated_types", "sniffio", "h11", "idna",
             "certifi", "click", "greenlet", "mako", "_pytest", "pkg_resources", "setuptools", "pluggy"}
#: A declared requirement brings these with it (its own install requirements).
_PROVIDED_BY = {
    "fastapi": {"starlette", "pydantic"}, "pydantic-settings": {"pydantic"}, "sqlmodel": {"sqlalchemy", "pydantic"},
    "uvicorn": {"click", "h11"}, "passlib": set(), "python-jose": {"cryptography"}, "alembic": {"sqlalchemy"},
}

_ROUTER_FILE_WORDS = {"router", "routers", "routes", "route", "api", "endpoints", "views", "controller",
                      "controllers", "handlers"}


def _norm(path: str) -> str:
    p = path.replace("\\", "/").strip()
    return posixpath.normpath(p) if p else p


def _is_test(path: str) -> bool:
    base = path.rsplit("/", 1)[-1].lower()
    return base.startswith("test_") or base.endswith("_test.py") or base == "conftest.py" or "/tests/" in f"/{path.lower()}"


def _code(text: str) -> str:
    """Comments and docstring bodies blanked, line structure kept (shared with the import graph)."""
    from app.services.wiring import _strip_py_comments

    return _strip_py_comments(text)


# --------------------------------------------------------------------------- import statements

@dataclass
class ImportStmt:
    """One ``from X import a, b as c`` / ``import x.y`` statement, by LINE span (inclusive)."""

    start: int
    end: int
    indent: str
    module: str                                     # "app.routers", ".schemas"; "" for `import x`
    names: list[tuple[str, str]] = field(default_factory=list)   # (name, alias or "")
    plain: list[tuple[str, str]] = field(default_factory=list)   # `import a.b as c` -> ("a.b", "c")

    def bound(self, name: str, alias: str) -> str:
        return alias or name


_FROM_HEAD_RE = re.compile(r"^(\s*)from\s+(\.*[\w.]*)\s+import\s+(.*)$")
_IMPORT_HEAD_RE = re.compile(r"^(\s*)import\s+([\w.]+(?:\s+as\s+\w+)?(?:\s*,\s*[\w.]+(?:\s+as\s+\w+)?)*)\s*$")


def _split_names(raw: str) -> list[tuple[str, str]]:
    out = []
    for part in raw.replace("(", " ").replace(")", " ").replace("\\", " ").split(","):
        bits = part.split()
        if not bits:
            continue
        if len(bits) >= 3 and bits[1] == "as":
            out.append((bits[0], bits[2]))
        elif re.fullmatch(r"\w+|\*", bits[0]):
            out.append((bits[0], ""))
    return out


def import_statements(text: str) -> list[ImportStmt]:
    lines = _code(text).split("\n")
    out: list[ImportStmt] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        m = _FROM_HEAD_RE.match(line)
        if m:
            indent, module, rest = m.group(1), m.group(2), m.group(3)
            end = i
            if "(" in rest and ")" not in rest:
                while end + 1 < len(lines) and ")" not in lines[end]:
                    end += 1
                    rest += " " + lines[end]
            while rest.rstrip().endswith("\\") and end + 1 < len(lines):
                end += 1
                rest = rest.rstrip()[:-1] + " " + lines[end]
            out.append(ImportStmt(i, end, indent, module, names=_split_names(rest)))
            i = end + 1
            continue
        m = _IMPORT_HEAD_RE.match(line)
        if m:
            plain = []
            for part in m.group(2).split(","):
                bits = part.split()
                plain.append((bits[0], bits[2] if len(bits) >= 3 else ""))
            out.append(ImportStmt(i, i, m.group(1), "", plain=plain))
        i += 1
    return out


# --------------------------------------------------------------------------- the project's modules

_DEF_RE = re.compile(r"^[ \t]*(?:async[ \t]+def|def|class)[ \t]+(\w+)", re.MULTILINE)
_ASSIGN_RE = re.compile(r"^[ \t]*([A-Za-z_][\w \t,]*?)[ \t]*(?::[^=\n]*)?=(?!=)", re.MULTILINE)
_ALL_RE = re.compile(r"__all__\s*(?:\+)?=\s*[\[(]([^\])]*)[\])]", re.DOTALL)


def defined_names(text: str, *, include_imports: bool = True) -> set[str] | None:
    """Every name a module binds, at any depth (permissive on purpose: a missing-name finding must
    never be raised for a name the module does define, if only inside ``try:``). ``None`` when the
    module's namespace is not knowable from its text — a star import or a module ``__getattr__``.

    ``include_imports=False`` keeps only what the module itself DEFINES — for pointing at where a
    name really lives, rather than at every file that imports it."""
    code = _code(text)
    if re.search(r"^[ \t]*from[ \t]+\S+[ \t]+import[ \t]+\*", code, re.MULTILINE) or re.search(
            r"^def[ \t]+__getattr__\b", code, re.MULTILINE):
        return None
    names = set(_DEF_RE.findall(code))
    for target in _ASSIGN_RE.findall(code):
        names.update(t.strip() for t in target.split(",") if re.fullmatch(r"\w+", t.strip()))
    if not include_imports:
        return names
    for stmt in import_statements(text):
        names.update(alias or name for name, alias in stmt.names)
        for mod, alias in stmt.plain:
            names.add(alias or mod.split(".")[0])
    for body in _ALL_RE.findall(code):
        names.update(re.findall(r"['\"](\w+)['\"]", body))
    return names


class PyProject:
    """The generated Python modules, keyed the way they are imported."""

    def __init__(self, files: dict[str, str]) -> None:
        self.files = {_norm(p): c for p, c in files.items() if _norm(p).endswith(".py")}
        inits = {p.rsplit("/", 1)[0] if "/" in p else "" for p in self.files if p.endswith("__init__.py")}
        # A top-level package has NO package anywhere above it. `app/modules/auth/` under an
        # `app/` package is a subpackage even though `app/modules/` lacks an __init__.py (Python
        # reaches it as a namespace package) — treating it as a root of its own would rename
        # `app.modules.auth` to `auth`.
        tops = {d for d in inits if not any(d.startswith(o + "/") for o in inits if o != d)}
        self.roots = sorted({(d.rsplit("/", 1)[0] + "/") if "/" in d else "" for d in tops} | {
            (p.split("/", 1)[0] + "/") for p in self.files if "/" in p
        }, key=len, reverse=True)
        self.modules: dict[str, dict[str, str]] = {r: {} for r in self.roots}      # root -> dotted -> file
        self.namespaces: dict[str, set[str]] = {r: set() for r in self.roots}
        for path in self.files:
            for root in self.roots:
                if not path.startswith(root):
                    continue
                parts = path[len(root):-3].split("/")
                if parts[-1] == "__init__":
                    parts = parts[:-1]
                if parts:
                    self.modules[root].setdefault(".".join(parts), path)
                for k in range(1, len(parts)):
                    self.namespaces[root].add(".".join(parts[:k]))

    def roots_for(self, path: str) -> list[str]:
        """Import roots that apply to a file: every root of its top-level folder, deepest first."""
        side = path.split("/", 1)[0] + "/" if "/" in path else ""
        return [r for r in self.roots if r.startswith(side) or r == ""] if side else [r for r in self.roots if r == ""]

    def top_levels(self, path: str) -> set[str]:
        out: set[str] = set()
        for root in self.roots_for(path):
            out.update(d.split(".")[0] for d in self.modules[root])
        return out

    def lookup(self, dotted: str, importer: str) -> tuple[str, str] | None:
        """``("module", file)`` / ``("namespace", "")`` for an absolute dotted name, or ``None``."""
        for root in self.roots_for(importer):
            if dotted in self.modules[root]:
                return "module", self.modules[root][dotted]
            if dotted in self.namespaces[root]:
                return "namespace", ""
        return None

    def resolve(self, module: str, importer: str) -> tuple[str, str] | None:
        """Resolve an import's module part — absolute or relative — from ``importer``."""
        if not module.startswith("."):
            return self.lookup(module, importer)
        dots = len(module) - len(module.lstrip("."))
        base = posixpath.dirname(importer)
        for _ in range(dots - 1):
            base = posixpath.dirname(base)
        rest = module.lstrip(".")
        target = _norm(posixpath.join(base, rest.replace(".", "/"))) if rest else base
        if f"{target}.py" in self.files:
            return "module", f"{target}.py"
        if f"{target}/__init__.py" in self.files:
            return "module", f"{target}/__init__.py"
        if any(p.startswith(target + "/") for p in self.files):
            return "namespace", ""
        return None

    def is_local(self, module: str, importer: str) -> bool:
        return module.startswith(".") or module.split(".")[0] in self.top_levels(importer)

    def strip_root_prefix(self, module: str, importer: str) -> str | None:
        """``backend.app.x`` -> ``app.x``: a module written by PATH from a folder above the import
        root, when what follows the prefix starts with one of the project's own packages. ``None``
        when the module has no such prefix."""
        if module.startswith(".") or self.is_local(module, importer):
            return None
        tops = self.top_levels(importer)
        for root in self.roots_for(importer):
            segs = [s for s in root.strip("/").split("/") if s]
            for k in range(len(segs)):
                prefix = ".".join(segs[k:]) + "."
                rest = module[len(prefix):] if module.startswith(prefix) else ""
                if rest and rest.split(".")[0] in tops:
                    return rest
        return None

    def submodule(self, resolved: tuple[str, str], module: str, name: str, importer: str) -> bool:
        """Is ``name`` a submodule of the (package) ``module``?"""
        kind, path = resolved
        if kind == "module" and not path.endswith("__init__.py"):
            return False
        if module.startswith("."):
            return self.resolve(f"{module}.{name}" if not module.endswith(".") else f"{module}{name}", importer) is not None
        return self.lookup(f"{module}.{name}", importer) is not None

    def dotted_of(self, path: str) -> str | None:
        for root in self.roots_for(path):
            for dotted, file in self.modules[root].items():
                if file == path:
                    return dotted
        return None


# --------------------------------------------------------------------------- unresolved imports

def find_unresolved_python_imports(files: dict[str, str]) -> list:
    """Every import of the project's OWN code that names a module or a name that does not exist
    in the generated file set, as :class:`app.services.wiring.UnresolvedImport` findings (a missing
    NAME carries ``name``; its ``target`` is the module file that lacks it)."""
    from app.services.wiring import UnresolvedImport

    project = PyProject(files)
    defs_cache: dict[str, set[str] | None] = {}

    def defs(path: str) -> set[str] | None:
        if path not in defs_cache:
            defs_cache[path] = defined_names(project.files.get(path, ""))
        return defs_cache[path]

    definers: dict[str, list[str]] = {}
    for path in project.files:
        for name in (defined_names(project.files[path], include_imports=False) or ()) if not _is_test(path) else ():
            definers.setdefault(name, []).append(path)

    found: dict[tuple[str, str, str], UnresolvedImport] = {}
    for importer in sorted(project.files):
        for stmt in import_statements(project.files[importer]):
            specs = [(stmt.module, stmt.names)] if stmt.module else [(mod, []) for mod, _alias in stmt.plain]
            for module, names in specs:
                # `from backend.app.x import y`: judged as `app.x` — the reconcile pass already
                # rewrote every such import whose module exists, so what is left here names a
                # module nobody generated, and saying only "backend is not a package" would hide it.
                stripped = project.strip_root_prefix(module, importer) if module else None
                if stripped and project.resolve(stripped, importer) is None:
                    roots = project.roots_for(importer)
                    found[(importer, module, "")] = UnresolvedImport(
                        importer=importer, specifier=module,
                        target=_norm(posixpath.join(roots[0] if roots else "", stripped.replace(".", "/"))),
                        candidates=tuple(sorted(p for p in project.files
                                                if p.rsplit("/", 1)[-1] == stripped.rsplit(".", 1)[-1] + ".py")[:5]))
                    continue
                if not module or not project.is_local(module, importer):
                    continue
                resolved = project.resolve(module, importer)
                if resolved is None:
                    roots = project.roots_for(importer)
                    target = _norm(posixpath.join(roots[0] if roots else "", module.replace(".", "/"))) \
                        if not module.startswith(".") else module
                    hits = sorted({p for n, _a in names for p in project.files
                                   if f"/{n}/" in f"/{p}" or p.rsplit("/", 1)[-1] == f"{n}.py"})
                    found[(importer, module, "")] = UnresolvedImport(
                        importer=importer, specifier=module, target=target, candidates=tuple(hits[:5]))
                    continue
                kind, path = resolved
                known = defs(path) if kind == "module" else set()
                for name, _alias in names:
                    if name == "*" or project.submodule(resolved, module, name, importer):
                        continue
                    if known is None or name in known:
                        continue
                    others = tuple(sorted(p for p in definers.get(name, []) if p not in (path, importer))[:5])
                    found[(importer, module, name)] = UnresolvedImport(
                        importer=importer, specifier=module, target=path or module, candidates=others, name=name)
    return [found[k] for k in sorted(found)]


# --------------------------------------------------------------------------- import roots

def reconcile_python_import_roots(files: dict[str, str]) -> dict[str, str]:
    """``from backend.app.dependencies import get_db`` -> ``from app.dependencies import get_db``.

    A generation call that sees paths like ``backend/app/dependencies.py`` routinely imports them
    by PATH, prefixing the folder the code runs from. That only resolves when the process starts
    one level up — never in the project's own Dockerfile or test runner. Rewritten only when the
    prefixed module resolves to nothing AND the stripped one resolves to a generated module, so
    there is exactly one reading of what was meant. ``from`` imports only (a plain ``import a.b``
    is referenced by its full dotted name downstream, which a rename here would break)."""
    project = PyProject(files)
    originals = {_norm(p): p for p in files}
    changed: dict[str, str] = {}
    for path, text in sorted(project.files.items()):
        lines = text.split("\n")
        edited = False
        for stmt in import_statements(text):
            module = stmt.module
            if not module or module.startswith(".") or project.resolve(module, path) is not None:
                continue
            for root in project.roots_for(path):
                segs = [s for s in root.strip("/").split("/") if s]
                hit = None
                for k in range(len(segs)):
                    prefix = ".".join(segs[k:]) + "."
                    if module.startswith(prefix) and project.lookup(module[len(prefix):], path):
                        hit = module[len(prefix):]
                        break
                if hit:
                    new_line = re.sub(rf"(\bfrom\s+){re.escape(module)}(\s+import\b)", rf"\g<1>{hit}\g<2>",
                                      lines[stmt.start], count=1)
                    if new_line != lines[stmt.start]:
                        lines[stmt.start] = new_line
                        edited = True
                    break
        if edited:
            changed[originals.get(path, path)] = "\n".join(lines)
    return changed


# --------------------------------------------------------------------------- FastAPI routers

_ROUTER_ASSIGN_RE = re.compile(r"^(\w+)[ \t]*(?::[ \t]*APIRouter[ \t]*)?=[ \t]*APIRouter[ \t]*\(", re.MULTILINE)
_FASTAPI_ASSIGN_RE = re.compile(r"^([ \t]*)(\w+)[ \t]*(?::[ \t]*FastAPI[ \t]*)?=[ \t]*FastAPI[ \t]*\(", re.MULTILINE)
_PREFIX_RE = re.compile(r"\bprefix\s*=\s*(['\"])(.*?)\1")


def _call_args(text: str, open_paren: int) -> tuple[str, int]:
    """The text inside the parentheses opening at ``open_paren``, and the index just past them."""
    depth, i = 0, open_paren
    while i < len(text):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return text[open_paren + 1: i], i + 1
        i += 1
    return text[open_paren + 1:], len(text)


def _clean_prefix(prefix: str) -> str:
    return "/" + prefix.strip().strip("/") if prefix and prefix.strip("/") else ""


@dataclass
class RouterDef:
    path: str
    module: str          # dotted
    var: str
    prefix: str          # normalized own prefix, "" when none

    @property
    def resource(self) -> str:
        parts = self.module.split(".")
        return parts[-1] if parts[-1] not in _ROUTER_FILE_WORDS or len(parts) < 2 else parts[-2]


def router_defs(project: PyProject) -> list[RouterDef]:
    out = []
    for path, text in sorted(project.files.items()):
        if _is_test(path):
            continue
        code = _code(text)
        for m in _ROUTER_ASSIGN_RE.finditer(code):
            args, _end = _call_args(code, m.end() - 1)
            prefix = _PREFIX_RE.search(args)
            dotted = project.dotted_of(path)
            if dotted:
                out.append(RouterDef(path, dotted, m.group(1), _clean_prefix(prefix.group(2)) if prefix else ""))
    return out


def _bare(name: str) -> str:
    return re.sub(r"_?(?:router|routes|api)$", "", name.lower()) or name.lower()


def _usage(code: str, bound: str) -> str | None:
    """How the entry uses an imported name in ``include_router``: ``"attr:<x>"`` (``auth.router``),
    ``"object"`` (``auth_router``), or ``None`` (not a router import)."""
    m = re.search(rf"include_router\(\s*{re.escape(bound)}\.(\w+)", code)
    if m:
        return f"attr:{m.group(1)}"
    if re.search(rf"include_router\(\s*{re.escape(bound)}\s*[,)]", code):
        return "object"
    return None


def _fix_router_imports(entry: str, text: str, project: PyProject, routers: list[RouterDef]) -> str:
    """Rewrite imports of routers from modules that do not exist (or lack the name) to the module
    that DOES define the router — when exactly one does. Usage downstream is left untouched."""
    lines = text.split("\n")
    code = _code(text)
    replacements: list[tuple[int, int, list[str]]] = []
    for stmt in import_statements(text):
        if not stmt.module or not project.is_local(stmt.module, entry):
            continue
        resolved = project.resolve(stmt.module, entry)
        known = defined_names(project.files.get(resolved[1], "")) if resolved and resolved[0] == "module" else set()
        keep, new_lines, ok = [], [], True
        for name, alias in stmt.names:
            fine = resolved is not None and (
                project.submodule(resolved, stmt.module, name, entry) or known is None or name in (known or set()))
            if fine:
                keep.append((name, alias))
                continue
            bound = alias or name
            usage = _usage(code, bound)
            if usage is None:
                ok = False
                break
            want_var = usage.split(":", 1)[1] if usage.startswith("attr:") else None
            hits = [r for r in routers if r.resource.lower() == _bare(name) and (want_var is None or r.var == want_var)]
            if usage == "object":
                hits = [r for r in hits if r.var == "router"] or hits
            if len({(r.module, r.var) for r in hits}) != 1:
                ok = False
                break
            r = hits[0]
            if want_var:   # `bound.router` -> bind `bound` to the router MODULE
                parent, _, last = r.module.rpartition(".")
                new_lines.append(f"from {parent} import {last}" + (f" as {bound}" if last != bound else "")
                                 if parent else f"import {r.module} as {bound}")
            else:          # `bound` IS the APIRouter object
                new_lines.append(f"from {r.module} import {r.var}" + (f" as {bound}" if r.var != bound else ""))
        if not ok or not new_lines:
            continue
        rebuilt = []
        if keep:
            rebuilt.append(f"from {stmt.module} import " + ", ".join(f"{n} as {a}" if a else n for n, a in keep))
        rebuilt.extend(new_lines)
        replacements.append((stmt.start, stmt.end, [stmt.indent + ln for ln in rebuilt]))
    for start, end, new in sorted(replacements, reverse=True):
        lines[start:end + 1] = new
    return "\n".join(lines)


def _bindings(entry: str, text: str, project: PyProject, routers: list[RouterDef]) -> dict[str, RouterDef]:
    """Names in the entry bound to a router (module or object) — after the import fixes."""
    by_module = {r.module: r for r in routers}
    by_obj = {(r.module, r.var): r for r in routers}
    out: dict[str, RouterDef] = {}
    for stmt in import_statements(text):
        if not stmt.module:
            continue
        resolved = project.resolve(stmt.module, entry)
        if stmt.module.startswith("."):
            base = project.dotted_of(resolved[1]) if resolved and resolved[1] else None
        else:
            base = stmt.module
        for name, alias in stmt.names:
            bound = alias or name
            if base and f"{base}.{name}" in by_module:
                out[bound] = by_module[f"{base}.{name}"]
            elif base and (base, name) in by_obj:
                out[bound] = by_obj[(base, name)]
    return out


def _include_calls(text: str) -> list[tuple[int, int, str]]:
    """``(start, end, args)`` character spans of every ``include_router(...)`` call."""
    out = []
    for m in re.finditer(r"\binclude_router\s*\(", text):
        args, end = _call_args(text, m.end() - 1)
        out.append((m.start(), end, args))
    return out


def _fix_double_prefix(text: str, bindings: dict[str, RouterDef]) -> str:
    """``include_router(depots.router, prefix="/depots")`` on a router that already declares
    ``prefix="/depots"`` serves ``/depots/depots``: drop the include's copy."""
    for start, end, args in sorted(_include_calls(text), reverse=True):
        first = re.match(r"\s*([\w.]+)", args)
        prefix = _PREFIX_RE.search(args)
        if not first or not prefix:
            continue
        bound = first.group(1).split(".")[0]
        router = bindings.get(bound)
        if router is None or not router.prefix or _clean_prefix(prefix.group(2)) != router.prefix:
            continue
        call = text[start:end]
        new_call = re.sub(r",\s*prefix\s*=\s*(['\"])" + re.escape(prefix.group(2)) + r"\1", "", call, count=1)
        if new_call == call:
            new_call = re.sub(r"prefix\s*=\s*(['\"])" + re.escape(prefix.group(2)) + r"\1\s*,\s*", "", call, count=1)
        text = text[:start] + new_call + text[end:]
    return text


def _referenced(router: RouterDef, project: PyProject) -> bool:
    """Does any other file import this router (its module, or its object)?"""
    parent, _, last = router.module.rpartition(".")
    for path, text in project.files.items():
        if path == router.path or _is_test(path):
            continue
        for stmt in import_statements(text):
            for mod, _alias in stmt.plain:
                if mod == router.module:
                    return True
            if not stmt.module:
                continue
            resolved = project.resolve(stmt.module, path)
            target = resolved[1] if resolved else ""
            names = {n for n, _a in stmt.names}
            if target == router.path or (stmt.module == router.module):
                return True
            if (stmt.module == parent or (target and target.endswith("__init__.py")
                                          and project.dotted_of(target) == parent)) and last in names:
                return True
    return False


def _mount_unreferenced(entry: str, text: str, app_var: str, routers: list[RouterDef],
                        project: PyProject) -> tuple[str, list[RouterDef]]:
    """Import + ``include_router`` every router no file references, when it declares its own
    prefix (so where it is served is not a guess). Returns (text, routers left unmounted)."""
    lines = text.split("\n")
    missing = [r for r in routers if r.path != entry and not _referenced(r, project)]
    mountable = [r for r in missing if r.prefix]
    if not mountable:
        return text, missing
    code_lines = _code(text).split("\n")
    calls = [i for i, ln in enumerate(code_lines) if re.search(rf"\b{re.escape(app_var)}\.include_router\s*\(", ln)]
    if calls:
        last = calls[-1]
        depth = 0
        for j in range(last, len(code_lines)):
            depth += code_lines[j].count("(") - code_lines[j].count(")")
            if depth <= 0:
                last = j
                break
        indent = re.match(r"\s*", lines[calls[-1]]).group(0)
        insert_at = last + 1
    else:
        m = _FASTAPI_ASSIGN_RE.search("\n".join(code_lines))
        if not m:
            return text, missing
        start = "\n".join(code_lines)[: m.start()].count("\n")
        depth, insert_at = 0, start + 1
        for j in range(start, len(code_lines)):
            depth += code_lines[j].count("(") - code_lines[j].count(")")
            if depth <= 0:
                insert_at = j + 1
                break
        indent = m.group(1)
    taken = set(re.findall(r"\b\w+\b", text))
    imports, includes = [], []
    for r in mountable:
        alias = f"{r.resource}_{r.var}"
        n = 2
        while alias in taken:
            alias, n = f"{r.resource}_{r.var}_{n}", n + 1
        taken.add(alias)
        imports.append(f"from {r.module} import {r.var} as {alias}")
        includes.append(f"{indent}{app_var}.include_router({alias})")
    lines[insert_at:insert_at] = includes
    top_imports = [s.end for s in import_statements(text) if not s.indent]
    at = (max(top_imports) + 1) if top_imports else 0
    lines[at:at] = imports
    return "\n".join(lines), [r for r in missing if not r.prefix]


def reconcile_fastapi_routers(files: dict[str, str]) -> dict[str, str]:
    """Wire a FastAPI app to the routers that were actually generated. ``{entry: new_content}``
    when something changed, else ``{}``. Idempotent."""
    project = PyProject(files)
    entries = [p for p, t in sorted(project.files.items()) if not _is_test(p) and _FASTAPI_ASSIGN_RE.search(_code(t))]
    if not entries:
        return {}
    routers = router_defs(project)
    if not routers:
        return {}
    original = {_norm(p): p for p in files}
    changed: dict[str, str] = {}
    for entry in entries:
        text = project.files[entry]
        new = _fix_router_imports(entry, text, project, routers)
        new = _fix_double_prefix(new, _bindings(entry, new, project, routers))
        if entry == entries[0]:
            m = _FASTAPI_ASSIGN_RE.search(_code(new))
            project.files[entry] = new          # mounting checks references against the fixed entry
            new, _left = _mount_unreferenced(entry, new, m.group(2), routers, project)
        if new != text:
            changed[original.get(entry, entry)] = new
            project.files[entry] = new
    return changed


def fastapi_mounts(files: dict[str, str]) -> tuple[list[str], list[str], str]:
    """``(effective route prefixes, routers nothing includes, FastAPI root_path)`` — for the
    base-path agreement report."""
    project = PyProject(files)
    routers = router_defs(project)
    prefixes: list[str] = []
    root_path = ""
    for path, text in project.files.items():
        if _is_test(path):
            continue
        code = _code(text)
        m = _FASTAPI_ASSIGN_RE.search(code)
        if m:
            args, _end = _call_args(code, m.end() - 1)
            rp = re.search(r"\broot_path\s*=\s*(['\"])(.*?)\1", args)
            root_path = _clean_prefix(rp.group(2)) if rp else root_path
        bindings = _bindings(path, text, project, routers)
        for _s, _e, args in _include_calls(code):
            first = re.match(r"\s*([\w.]+)", args)
            prefix = _PREFIX_RE.search(args)
            router = bindings.get(first.group(1).split(".")[0]) if first else None
            prefixes.append((_clean_prefix(prefix.group(2)) if prefix else "") + (router.prefix if router else ""))
    unmounted = [r.path for r in routers if not _referenced(r, project)]
    return prefixes, unmounted, root_path


# --------------------------------------------------------------------------- requirements

_REQ_NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
_TRIGGERS = (
    (re.compile(r"\bEmailStr\b"), "email-validator"),
    (re.compile(r"\b(?:OAuth2PasswordRequestForm|UploadFile)\b|\b(?:File|Form)\s*\("), "python-multipart"),
    (re.compile(r"\bfastapi\.testclient\b|\bstarlette\.testclient\b"), "httpx"),
)
_DRIVER_TRIGGERS = (
    (re.compile(r"postgresql\+asyncpg|\bcreate_async_engine\b.*postgres", re.S), "asyncpg"),
    (re.compile(r"postgresql\+psycopg2|postgres(?:ql)?://"), "psycopg2-binary"),
    (re.compile(r"postgresql\+psycopg(?!2)"), "psycopg[binary]"),
    (re.compile(r"sqlite\+aiosqlite"), "aiosqlite"),
)


def _canonical(requirement: str) -> str:
    m = _REQ_NAME_RE.match(requirement)
    return re.sub(r"[-_.]+", "-", m.group(1)).lower() if m else ""


def _declared_requirements(text: str) -> set[str]:
    out = set()
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if line and not line.startswith("-"):
            name = _canonical(line)
            if name:
                out.add(name)
    return out


def _owning_requirements(path: str, req_files: list[str]) -> str | None:
    """The nearest ``requirements*.txt`` directory above a source file."""
    best, best_len = None, -1
    for req in req_files:
        directory = req.rsplit("/", 1)[0] if "/" in req else ""
        if directory and not path.startswith(directory + "/"):
            continue
        if len(directory) > best_len:
            best, best_len = directory, len(directory)
    return best


def reconcile_python_requirements(files: dict[str, str]) -> tuple[dict[str, str], list[str]]:
    """Declare in ``requirements.txt`` the packages the generated code imports but nothing lists
    (the Python twin of ``reconcile_package_dependencies``). Returns ``(changed files, imports it
    could not map to a package)``. Test-only needs go to a ``requirements-dev.txt`` beside it when
    one exists. Unpinned additions: always installable, never a wrong pin."""
    project = PyProject(files)
    originals = {_norm(p): p for p in files}
    req_files = [_norm(p) for p in files if re.fullmatch(r"requirements(?:[-_.]\w+)*\.txt", _norm(p).rsplit("/", 1)[-1])]
    if not req_files:
        return {}, []
    by_dir: dict[str, dict[str, set[str]]] = {}
    unknown: set[str] = set()
    env_text = {d: "" for d in {(r.rsplit("/", 1)[0] if "/" in r else "") for r in req_files}}
    for path, content in files.items():
        p = _norm(path)
        base = p.rsplit("/", 1)[-1]
        if base.startswith(".env") or base.startswith("docker-compose") or base.endswith((".ini", ".toml", ".cfg")):
            d = _owning_requirements(p, req_files)
            if d is not None:
                env_text[d] = env_text.get(d, "") + "\n" + content
    for path, text in sorted(project.files.items()):
        directory = _owning_requirements(path, req_files)
        if directory is None:
            continue
        bucket = by_dir.setdefault(directory, {"prod": set(), "dev": set()})["dev" if _is_test(path) else "prod"]
        tops = project.top_levels(path)
        for stmt in import_statements(text):
            mods = [stmt.module] if stmt.module else [m for m, _a in stmt.plain]
            for mod in mods:
                top = mod.split(".")[0]
                if not top or mod.startswith(".") or top in tops or top in _STDLIB or top in _IMPLICIT:
                    continue
                if top in PIP_PACKAGES:
                    bucket.add(PIP_PACKAGES[top])
                else:
                    unknown.add(top)
        code = _code(text)
        for pattern, req in _TRIGGERS:
            if pattern.search(code) and ("fastapi" in code or req != "python-multipart"):
                bucket.add(req)
        if re.search(r"^\s*(?:from|import)\s+fastapi\b", code, re.MULTILINE):
            bucket.add("uvicorn[standard]")
        env_text[directory] = env_text.get(directory, "") + "\n" + text
    changed: dict[str, str] = {}
    for directory, needs in sorted(by_dir.items()):
        joined = env_text.get(directory, "")
        if "sqlalchemy" in joined.lower():
            for pattern, req in _DRIVER_TRIGGERS:
                if pattern.search(joined):
                    needs["prod"].add(req)
                    break
        prefix = f"{directory}/" if directory else ""
        here = [r for r in req_files if r.startswith(prefix) and "/" not in r[len(prefix):]]
        main = f"{prefix}requirements.txt" if f"{prefix}requirements.txt" in here else sorted(here)[0]
        dev = next((r for r in here if re.search(r"requirements[-_.]dev", r)), main)
        declared = set().union(*(_declared_requirements(files[originals[r]]) for r in here))
        declared |= {dep for name in list(declared) for dep in _PROVIDED_BY.get(name, ())}
        additions: dict[str, set[str]] = {}
        for target, wanted in ((main, needs["prod"]), (dev, needs["dev"] - needs["prod"])):
            missing = {w for w in wanted if _canonical(w) not in declared}
            declared |= {_canonical(w) for w in missing}
            if missing:
                additions.setdefault(target, set()).update(missing)
        for target, missing in additions.items():
            current = files[originals[target]]
            sep = "" if current.endswith("\n") or not current else "\n"
            changed[originals[target]] = (current + sep + "# Added by reconciliation: imported by the generated "
                                          "code but not declared\n" + "\n".join(sorted(missing, key=str.lower)) + "\n")
    return changed, sorted(unknown)
