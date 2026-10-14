"""What the files generated so far actually EXPORT — deterministic, no LLM.

Each work item is generated in its own call and used to see earlier files only as PATHS. A page
generated after ``types/common.ts`` therefore imported ``ApiError`` from it because that is what an
API types module "should" export, when the file defines ``Error``; ``dependencies.py`` imported
``AsyncSessionLocal`` from a ``database.py`` that defines ``SessionLocal``. In the real Fleet run
that drift took the debugging agent eight rounds (225 -> 1 errors) to clean up.

:func:`extract_exports` lists a file's public names with their signatures; :func:`render_exports`
turns the files of one side into a bounded block for the prompt, so a file imports what exists —
and a TypeScript file can tell a NAMED export from a DEFAULT one, the other classic mismatch.
"""

from __future__ import annotations

import re

_PY_DEF_RE = re.compile(r"^(?P<kw>async\s+def|def|class)\s+(?P<name>[A-Za-z_]\w*)\s*(?P<rest>[^\n]*)", re.MULTILINE)
_PY_ASSIGN_RE = re.compile(r"^(?P<name>[A-Za-z_]\w*)\s*(?::\s*[^=\n]+)?=\s*(?P<value>[^\n]{0,60})", re.MULTILINE)
_TS_EXPORT_RE = re.compile(
    r"^export\s+(?P<default>default\s+)?(?:declare\s+)?(?P<kind>async\s+function\*?|function\*?|class|abstract\s+class|"
    r"const|let|var|type|interface|enum)\s+(?P<name>[A-Za-z_$][\w$]*)(?P<rest>[^\n]*)", re.MULTILINE)
_TS_LIST_RE = re.compile(r"^export\s*(?P<type>type\s*)?\{(?P<names>[^}]*)\}(?:\s*from\s*['\"](?P<src>[^'\"]+)['\"])?",
                         re.MULTILINE | re.DOTALL)
_TS_DEFAULT_RE = re.compile(r"^export\s+default\s+(?!(?:async\s+)?(?:function|class|abstract)\b)(?P<expr>[^\n;]{1,60})", re.MULTILINE)
_CJS_RE = re.compile(r"module\.exports\s*=\s*\{(?P<names>[^}]*)\}|module\.exports\s*=\s*(?P<single>[A-Za-z_$][\w$]*)")
_MAX_SIG = 110
_MAX_SYMBOLS = 14

_PY_EXT = (".py",)
_JS_EXT = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")


def _clip(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= _MAX_SIG else text[: _MAX_SIG - 3] + "..."


def _py_exports(text: str) -> list[str]:
    out: list[str] = []
    for m in _PY_DEF_RE.finditer(text):
        name = m.group("name")
        if name.startswith("_") and not name.startswith("__all"):
            continue
        kw = m.group("kw")
        rest = m.group("rest").rstrip(":").strip()
        out.append(_clip(f"{'async def' if 'async' in kw else kw} {name}{rest}"))
    for m in _PY_ASSIGN_RE.finditer(text):
        name = m.group("name")
        if name.startswith("_") or name in ("self",) or any(name == o.split()[1].split("(")[0] for o in out if " " in o):
            continue
        out.append(_clip(f"{name} = {m.group('value')}"))
    return out


def _js_exports(text: str) -> list[str]:
    out: list[str] = []
    for m in _TS_EXPORT_RE.finditer(text):
        kind = re.sub(r"\s+", " ", m.group("kind"))
        label = "default " if m.group("default") else "named "
        out.append(_clip(f"{label}{kind} {m.group('name')}{m.group('rest')}"))
    for m in _TS_LIST_RE.finditer(text):
        names = [re.sub(r"\s+", " ", n).strip() for n in m.group("names").split(",") if n.strip()]
        prefix = "named type" if m.group("type") else "named"
        for n in names:
            out.append(f"{prefix} {n}" + (f" (re-exported from {m.group('src')})" if m.group("src") else ""))
    if _TS_DEFAULT_RE.search(text):
        m = _TS_DEFAULT_RE.search(text)
        out.append(_clip(f"default {m.group('expr')}"))
    for m in _CJS_RE.finditer(text):
        names = [n.split(":")[0].strip() for n in (m.group("names") or m.group("single") or "").split(",") if n.strip()]
        out.extend(f"commonjs {n}" for n in names)
    return out


def extract_exports(path: str, text: str) -> list[str]:
    """Public names of one source file with their signature lines; ``[]`` for anything else."""
    low = path.lower()
    if low.endswith(_PY_EXT):
        return _py_exports(text)
    if low.endswith(_JS_EXT) and not low.endswith(".d.ts"):
        return _js_exports(text)
    return []


def render_exports(files: dict[str, list[str]], *, side: str = "", cap: int = 24_000) -> str:
    """The exports block for the prompt: files of ``side`` (``"backend"`` / ``"frontend"``) with at
    least one export, in the order given (generation order), within ``cap`` characters. Files whose
    block does not fit are named without their symbols, so the model still knows they exist."""
    blocks: list[str] = []
    bare: list[str] = []
    used = 0
    for path, symbols in files.items():
        if side and not path.startswith(side + "/"):
            continue
        if not symbols:
            continue
        shown = symbols[:_MAX_SYMBOLS]
        block = f"### {path}\n" + "\n".join(f"- {s}" for s in shown) + (
            f"\n- ... (+{len(symbols) - _MAX_SYMBOLS} more)" if len(symbols) > _MAX_SYMBOLS else "")
        if used + len(block) > cap:
            bare.append(path)
            continue
        blocks.append(block)
        used += len(block) + 1
    if not blocks and not bare:
        return ""
    text = (
        "## What the files already generated EXPORT — import ONLY these names, exactly as written "
        "(a `named` export is imported with braces, a `default` one without; Python names are the "
        "`def`/`class`/assignment names shown). If you need something that is not listed, the "
        "file that should hold it is generated by another work item: do not assume it exists.\n\n"
        + "\n".join(blocks)
    )
    if bare:
        text += "\n\n(Also generated, symbols not shown: " + ", ".join(bare[:60]) + ")"
    return text


# --------------------------------------------------------------------------- import checking

_JS_IMPORT_RE = re.compile(
    r"^\s*import\s+(?:type\s+)?(?P<clause>[^'\";]*?)\s*from\s*['\"](?P<src>[^'\"]+)['\"]", re.MULTILINE)
_STAR_RE = re.compile(r"^\s*export\s*\*\s*(?:as\s+\w+\s*)?from\b", re.MULTILINE)


def js_exported_names(text: str) -> tuple[set[str], bool, bool]:
    """``(named exports, has a default export, namespace unknown)`` of a JS/TS file. The namespace
    is unknown when it re-exports with ``export *`` or is CommonJS: nothing can be claimed missing."""
    named: set[str] = set()
    default = False
    for m in _TS_EXPORT_RE.finditer(text):
        if m.group("default"):
            default = True
        else:
            named.add(m.group("name"))
    for m in _TS_LIST_RE.finditer(text):
        for part in m.group("names").split(","):
            bits = re.sub(r"^type\s+", "", part.strip()).split(" as ")
            name = bits[-1].strip()
            if name == "default":
                default = True
            elif name:
                named.add(name)
    if _TS_DEFAULT_RE.search(text) or re.search(r"^export\s+default\b", text, re.MULTILINE):
        default = True
    unknown = bool(_STAR_RE.search(text)) or bool(_CJS_RE.search(text))
    return named, default, unknown


def js_imports(text: str) -> list[tuple[str, list[str], str]]:
    """``(specifier, imported names, default-name or "")`` for each ``import ... from '...'``
    (``import * as X`` and side-effect imports name nothing and are skipped)."""
    out: list[tuple[str, list[str], str]] = []
    for m in _JS_IMPORT_RE.finditer(text):
        clause = m.group("clause").strip()
        if not clause or clause.startswith("*"):
            continue
        default = ""
        names: list[str] = []
        brace = re.search(r"\{([^}]*)\}", clause)
        head = clause[: brace.start()] if brace else clause
        head = head.strip().rstrip(",").strip()
        if head and not head.startswith("*"):
            default = head
        if brace:
            for part in brace.group(1).split(","):
                name = re.sub(r"^type\s+", "", part.strip()).split(" as ")[0].strip()
                if name:
                    names.append(name)
        out.append((m.group("src"), names, default))
    return out
