"""Pack preflight — find and settle a design pack's contradictions BEFORE code is generated.

Deterministic, no LLM, no I/O, and it never edits the pack. Every work item is generated in its
own isolated call, so when the pack says the same thing two ways, different files pick different
answers and the pieces stop fitting: in a real run roles were checked as ``"Administrator"`` while
the database stored ``"administrator"`` (every guarded route answered 403), the example payloads
sent ``vehicle_class: "Heavy"`` into a column constrained to ``heavy_truck``, and half the
example ids were not valid UUIDs, so any test built from them failed validation.

Policy (decided with the user): **normalize + warn**. For each contradiction one source of truth
is chosen — the database schema wins for stored values (enums, roles), the OpenAPI spec wins for
the wire format (error envelope), the pack's env files win for variable names — and recorded as a
CANONICAL FACT that later stages put in every generation prompt. Each one is also a FINDING in a
report shown at plan approval, so the Design team can fix the pack itself. Gaps nothing can settle
(an endpoint the UI needs that the API lacks; a login with no way to create the first user) are
reported as such and generation continues.

Severity:
* ``normalized`` — the pack contradicts itself; the pipeline will use ``canonical``.
* ``gap``        — the pack is missing something the app needs; generation continues without it
                   (or a later stage compensates, e.g. a first-admin bootstrap).
* ``info``       — worth a human look; changes nothing.
"""

from __future__ import annotations

import base64
import binascii
import copy
import json
import re
import uuid
from collections import OrderedDict, defaultdict
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import urlparse

from app.services import design_pack
from app.services.pack_index import PackIndex, _fingerprint, index_for

NORMALIZED = "normalized"
GAP = "gap"
INFO = "info"
_SEVERITY_ORDER = {GAP: 0, NORMALIZED: 1, INFO: 2}

_HTTP_METHODS = ("get", "post", "put", "patch", "delete")
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_JWT_RE = re.compile(r"^eyJ[\w-]+\.eyJ[\w-]+\.[\w-]*$")
_ROLE_TEXT_RE = re.compile(r"(?:allowed|permitted|required)\s+roles?\s*:\s*([^\n.]+)", re.IGNORECASE)
_ENV_NAME_RE = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")
_SNAKE_RE = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")
_SYMBOL_VERBS = (
    "get_", "require_", "create_", "build_", "make_", "verify_", "hash_", "check_", "validate_",
    "load_", "issue_", "decode_", "encode_", "authenticate_", "authorize_", "ensure_", "init_",
    "register_", "parse_", "compute_", "calculate_", "fetch_", "send_", "resolve_",
)
_SYMBOL_NOUNS_RE = r"(?:dependency|dependencies|factory|function|helper|decorator|callable|class)"
_FRONTEND_ENV_PREFIXES = ("VITE_", "REACT_APP_", "NEXT_PUBLIC_", "EXPO_PUBLIC_", "VUE_APP_", "NG_APP_")
#: Wrapper directories a structure tree may nest everything under (mirrors plan_builder).
_GENERIC_WRAPPERS = {"root", "project", "workspace", "repo"}
_PASSWORD_COL_RE = re.compile(r"password|passwd|pwd", re.IGNORECASE)
_LOGIN_PATH_RE = re.compile(r"/(?:auth/)?(?:login|signin|sign-in|token)\b", re.IGNORECASE)
_SIGNUP_PATH_RE = re.compile(r"register|signup|sign-up|invite", re.IGNORECASE)


@dataclass
class Finding:
    check: str
    severity: str
    title: str
    detail: str
    canonical: Any = None
    sources: list[str] = field(default_factory=list)


@dataclass
class PreflightResult:
    findings: list[Finding] = field(default_factory=list)
    facts: dict[str, Any] = field(default_factory=dict)

    def counts(self) -> dict[str, int]:
        out = {GAP: 0, NORMALIZED: 0, INFO: 0}
        for f in self.findings:
            out[f.severity] = out.get(f.severity, 0) + 1
        return out

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable form for ``WorkflowState`` (``pack_findings`` / ``pack_facts``)."""
        return {"findings": [asdict(f) for f in self.findings], "facts": self.facts, "counts": self.counts()}

    def summary_line(self) -> str:
        c = self.counts()
        return (f"{len(self.findings)} finding(s): {c[GAP]} gap(s), {c[NORMALIZED]} normalized "
                f"contradiction(s), {c[INFO]} note(s)")

    def to_markdown(self, project: str = "") -> str:
        lines = [
            "# Pack Preflight Report" + (f" — {project}" if project else ""),
            "",
            "Contradictions and gaps found in the design pack before any code was generated. "
            "Policy: **normalize + warn** — where the pack says one thing two ways, the value below "
            "is what every generated file uses (database schema wins for stored values, OpenAPI for "
            "the wire format, the pack's env files for variable names). Gaps are things the pack "
            "does not provide at all; generation continues without them. Fix these in the pack to "
            "remove them from the next run.",
            "",
            f"**{self.summary_line()}**",
        ]
        titles = {GAP: "Gaps — the pack is missing something the app needs",
                  NORMALIZED: "Normalized — the pack contradicts itself; the pipeline uses the canonical value",
                  INFO: "Notes"}
        for severity in (GAP, NORMALIZED, INFO):
            group = [f for f in self.findings if f.severity == severity]
            if not group:
                continue
            lines += ["", f"## {titles[severity]}", ""]
            for f in group:
                lines.append(f"### {f.title}")
                lines.append("")
                lines.append(f.detail)
                if f.canonical not in (None, "", [], {}):
                    lines.append("")
                    lines.append(f"**Pipeline uses:** `{_short(f.canonical)}`")
                if f.sources:
                    lines.append("")
                    lines.append("Sources: " + ", ".join(f"`{s}`" for s in f.sources))
                lines.append("")
        lines += ["", "## Canonical facts given to code generation", "", "```json",
                  json.dumps(self.facts, indent=2, sort_keys=True), "```", ""]
        return "\n".join(lines)


def _short(value: Any, limit: int = 300) -> str:
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
    return text if len(text) <= limit else text[: limit - 3] + "..."


# --------------------------------------------------------------------------- helpers

def _operations(spec: dict[str, Any] | None) -> list[tuple[str, str, dict[str, Any]]]:
    out = []
    for path, item in ((spec or {}).get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        for method, op in item.items():
            if method.lower() in _HTTP_METHODS and isinstance(op, dict):
                out.append((method.upper(), str(path), op))
    return out


def _op_roles(op: dict[str, Any]) -> list[str]:
    """Roles an operation admits: an ``x-roles``-style list, else "Allowed roles: A, B" prose."""
    for key in ("x-roles", "x-allowed-roles", "x-permissions"):
        if isinstance(op.get(key), list):
            return [str(r).strip() for r in op[key] if str(r).strip()]
    text = " ".join(str(op.get(k) or "") for k in ("description", "summary"))
    m = _ROLE_TEXT_RE.search(text)
    if not m:
        return []
    return [r.strip(" `'\"") for r in re.split(r",|/|\band\b|\bor\b", m.group(1)) if r.strip(" `'\"")]


def _table_for_path(path: str, tables: set[str]) -> str | None:
    """The table a REST path is about: its LAST literal segment that names a table, trying
    singular/plural forms. ``/vehicles/{id}/work-orders`` is about ``work_orders`` (a nested
    collection), ``/work-orders/{id}/close`` about ``work_orders`` (``close`` is an action), and
    ``/depots/{id}/dashboard`` about ``depots``."""
    literals = [s for s in path.strip("/").split("/") if s and not s.startswith("{")]
    for seg in reversed(literals):
        base = seg.replace("-", "_").lower()
        for cand in (base, f"{base}s", f"{base}es", base[:-1] if base.endswith("s") else None):
            if cand and cand in tables:
                return cand
    return None


def _examples(spec: dict[str, Any] | None) -> list[tuple[str, str, Any]]:
    """``(location, path, example)`` for every request/response/schema example in the spec."""
    out: list[tuple[str, str, Any]] = []

    def _content_examples(content: Any, where: str, path: str) -> None:
        if not isinstance(content, dict):
            return
        for media in content.values():
            if not isinstance(media, dict):
                continue
            if "example" in media:
                out.append((where, path, media["example"]))
            for ex in (media.get("examples") or {}).values():
                if isinstance(ex, dict) and "value" in ex:
                    out.append((where, path, ex["value"]))

    for method, path, op in _operations(spec):
        name = op.get("operationId") or f"{method} {path}"
        body = op.get("requestBody") or {}
        _content_examples(body.get("content") if isinstance(body, dict) else None, f"{name} request", path)
        for status, resp in (op.get("responses") or {}).items():
            if isinstance(resp, dict):
                _content_examples(resp.get("content"), f"{name} {status} response", path)
    for schema_name, schema in (((spec or {}).get("components") or {}).get("schemas") or {}).items():
        if isinstance(schema, dict) and "example" in schema:
            out.append((f"schema {schema_name}", "", schema["example"]))
    return out


def _payload_examples(payloads: dict[str, Any] | None) -> list[tuple[str, str, Any]]:
    out: list[tuple[str, str, Any]] = []
    for op_name, entry in (payloads or {}).items():
        if not isinstance(entry, dict):
            continue
        path = str(entry.get("path") or "")
        if "request" in entry:
            out.append((f"sample {op_name} request", path, entry["request"]))
        for status, body in (entry.get("responses") or {}).items():
            out.append((f"sample {op_name} {status} response", path, body))
    return out


def _walk_pairs(obj: Any):
    """Every ``(key, scalar value)`` pair anywhere in a nested JSON value."""
    stack = [obj]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for k, v in node.items():
                if isinstance(v, (dict, list)):
                    stack.append(v)
                else:
                    yield str(k), v
        elif isinstance(node, list):
            stack.extend(node)


def _all_strings(obj: Any):
    stack = [obj]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
        elif isinstance(node, str):
            yield node


def _structure_leaves(index: PackIndex) -> list[tuple[str, str]]:
    """``(path, description)`` for every file leaf of the backend and frontend structure trees."""
    leaves: list[tuple[str, str]] = []
    for role in ("backend_structure", "frontend_structure"):
        hit = index.first(role)
        if not hit:
            continue
        stack = [("", design_pack._tree_root(hit[1]))]
        while stack:
            prefix, node = stack.pop()
            if not isinstance(node, dict):
                continue
            for key, value in node.items():
                path = f"{prefix}{key}"
                if isinstance(value, dict):
                    stack.append((path if path.endswith("/") else path + "/", value))
                elif isinstance(value, str) and not path.endswith("/"):
                    parts = path.split("/")
                    while len(parts) > 1 and parts[0].lower() in _GENERIC_WRAPPERS:
                        parts = parts[1:]   # "root/backend/app/x.py" -> "backend/app/x.py"
                    leaves.append(("/".join(parts), value))
    return sorted(leaves)


def _closest(name: str, candidates: list[str]) -> str | None:
    """The declared env var a stray name most plausibly means (``SECRET_KEY`` -> ``JWT_SECRET_KEY``)."""
    tokens = set(name.split("_"))
    best, best_score = None, 0.0
    for cand in candidates:
        if cand.endswith("_" + name) or name.endswith("_" + cand):
            score = 0.9
        else:
            ct = set(cand.split("_"))
            score = len(tokens & ct) / len(tokens | ct)
        if score > best_score:
            best, best_score = cand, score
    return best if best_score >= 0.5 else None


# --------------------------------------------------------------------------- the checks

def _check_enums(index: PackIndex, spec: dict | None, payloads: dict | None, result: PreflightResult) -> None:
    tables = index.tables()
    by_table = {t["name"]: t["enums"] for t in tables if t["enums"]}
    if not by_table:
        return
    by_column: dict[str, set[str]] = defaultdict(set)
    for enums in by_table.values():
        for col, values in enums.items():
            by_column[col].update(values)
    result.facts["enums"] = {f"{t}.{c}": v for t, enums in sorted(by_table.items()) for c, v in sorted(enums.items())}
    table_names = {t["name"] for t in tables}

    offenders: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))   # col -> value -> where

    def _allowed(col: str, path: str) -> set[str] | None:
        table = _table_for_path(path, table_names) if path else None
        if table and col in by_table.get(table, {}):
            return set(by_table[table][col])
        return by_column.get(col)

    for where, path, example in _examples(spec) + _payload_examples(payloads):
        for key, value in _walk_pairs(example):
            if isinstance(value, str) and key in by_column:
                allowed = _allowed(key, path)
                if allowed and value not in allowed:
                    offenders[key][value].add(where)

    # Enum lists the spec declares for request/response schemas.
    for schema_name, schema in (((spec or {}).get("components") or {}).get("schemas") or {}).items():
        for prop, definition in ((schema or {}).get("properties") or {}).items() if isinstance(schema, dict) else []:
            if isinstance(definition, dict) and isinstance(definition.get("enum"), list) and prop in by_column:
                for value in definition["enum"]:
                    if str(value) not in by_column[prop]:
                        offenders[prop][str(value)].add(f"schema {schema_name}")

    # Choices the mockup offers in <select name="col"> / id="...-col".
    html = index.mockup_html()
    for m in re.finditer(r"<select\b([^>]*)>(.*?)</select>", html, re.IGNORECASE | re.DOTALL):
        attrs = m.group(1)
        named = re.search(r"\bname=[\"']([\w-]+)[\"']", attrs) or re.search(r"\bid=[\"'](?:[\w-]*?-)?([\w]+)[\"']", attrs)
        col = named.group(1) if named else ""
        if col not in by_column:
            continue
        for value in re.findall(r"<option\b[^>]*\bvalue=[\"']([^\"']+)[\"']", m.group(2), re.IGNORECASE):
            if value not in by_column[col]:
                offenders[col][value].add("mockup <select>")

    # Validation-rule enums.
    for group, fields in (index.validation_rules() or {}).items():
        for col, rule in (fields or {}).items() if isinstance(fields, dict) else []:
            if isinstance(rule, dict) and isinstance(rule.get("enum"), list) and col in by_column:
                for value in rule["enum"]:
                    if str(value) not in by_column[col]:
                        offenders[col][str(value)].add(f"validation-rules {group}")

    for col, values in sorted(offenders.items()):
        per_table = {t: enums[col] for t, enums in sorted(by_table.items()) if col in enums}
        allowed_text = "; ".join(f"`{t}.{col}`: {', '.join(f'`{v}`' for v in vals)}" for t, vals in per_table.items())
        listed = "; ".join(f"`{v}` ({', '.join(sorted(w)[:3])}{'…' if len(w) > 3 else ''})" for v, w in sorted(values.items()))
        suggestions = {v: s for v in values if (s := _suggest_enum(v, sorted(by_column[col]))) and s != v}
        result.findings.append(Finding(
            check="enum_drift", severity=NORMALIZED,
            title=f"`{col}` values outside the database's allowed set",
            detail=(f"The database only accepts {allowed_text}, but the pack also uses: {listed}. Writing any "
                    "of these would be rejected by the database constraint."
                    + (" Likely meant: " + ", ".join(f"`{k}` → `{v}`" for k, v in sorted(suggestions.items())) + "."
                       if suggestions else "")),
            canonical=per_table,
            sources=sorted({w.split(" ")[0] for ws in values.values() for w in ws}),
        ))


def _suggest_enum(value: str, canonical: list[str]) -> str | None:
    slug = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    for c in canonical:
        if c == slug or c.startswith(slug + "_") or slug.startswith(c):
            return c
    for c in canonical:   # a truncated spelling: "percent" -> "percentage"
        if len(slug) >= 4 and c.startswith(slug):
            return c
    return None


def _check_roles(index: PackIndex, spec: dict | None, payloads: dict | None, result: PreflightResult) -> None:
    role_tables = [t for t in index.tables() if any(re.fullmatch(r"(?:\w+_)?roles?", c) for c in t["enums"])]
    role_tables.sort(key=lambda t: (t["name"] not in {"users", "user", "accounts", "members", "staff"}, t["name"]))
    canonical: list[str] = []
    source = ""
    if role_tables:
        table = role_tables[0]
        col = next(c for c in table["enums"] if re.fullmatch(r"(?:\w+_)?roles?", c))
        canonical, source = list(table["enums"][col]), f"{table['name']}.{col} ({table['source']})"

    mentions: dict[str, set[str]] = defaultdict(set)          # spelling -> where
    op_roles: dict[str, list[str]] = {}
    for method, path, op in _operations(spec):
        roles = _op_roles(op)
        if roles:
            op_roles[f"{method} {path}"] = roles
            for r in roles:
                mentions[r].add("openapi")
    for text in _all_strings([e for _w, _p, e in _examples(spec) + _payload_examples(payloads)]):
        if _JWT_RE.match(text.strip()):
            claims = _jwt_claims(text.strip())
            for key in ("role", "roles"):
                value = claims.get(key)
                for r in (value if isinstance(value, list) else [value] if value else []):
                    mentions[str(r)].add("sample JWT")
    for r in re.findall(r"require_roles?\(\s*['\"]([\w-]+)['\"]", index.conventions_text()):
        mentions[r].add("conventions")

    if not canonical and not mentions:
        return
    if not canonical:
        canonical = sorted({m for m in mentions if m.lower() != "internal"}, key=str.lower)
        source = "OpenAPI role mentions (no role column in the schema)"

    def _map(mention: str) -> str | None:
        low = mention.lower()
        for c in canonical:
            if c.lower() == low:
                return c
        for c in canonical:
            if len(low) >= 3 and (c.lower().startswith(low) or low.startswith(c.lower())):
                return c
        return None

    display = {c: c for c in canonical}
    for mention in mentions:
        mapped = _map(mention)
        if mapped and mention != mapped and "openapi" in mentions[mention]:
            display[mapped] = mention
    result.facts["roles"] = {"values": canonical, "display": display, "source": source}
    if op_roles:
        result.facts["operation_roles"] = {
            op: [m for m in (_map(r) for r in roles) if m] for op, roles in sorted(op_roles.items())
        }

    differing = {m: w for m, w in mentions.items() if _map(m) and m != _map(m)}
    if differing:
        spellings = "; ".join(f"`{m}` ({', '.join(sorted(w))})" for m, w in sorted(differing.items()))
        result.findings.append(Finding(
            check="role_vocabulary", severity=NORMALIZED,
            title="Role names are spelled differently across the pack",
            detail=(f"Stored role values are {', '.join(f'`{c}`' for c in canonical)} ({source}), but the pack "
                    f"also writes them as {spellings}. Code that compares role strings exactly would deny "
                    "every request. Generated code must store and compare the canonical values; the other "
                    "spellings are display names only."),
            canonical=canonical, sources=sorted({w for ws in differing.values() for w in ws}),
        ))
    unknown = {m: w for m, w in mentions.items() if not _map(m)}
    for mention, where in sorted(unknown.items()):
        ops = [op for op, roles in op_roles.items() if mention in roles]
        result.findings.append(Finding(
            check="unknown_role", severity=GAP,
            title=f"Role `{mention}` is not one of the stored roles",
            detail=(f"`{mention}` is required by {', '.join(ops) or ', '.join(sorted(where))}, but no user can "
                    f"have it (stored roles: {', '.join(canonical)}). As written, nobody can call "
                    + ("these endpoints." if ops else "what it guards.")
                    + " Decide whether it is a service-to-service credential or one of the existing roles."),
            sources=sorted(where),
        ))


def _jwt_claims(token: str) -> dict[str, Any]:
    try:
        part = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)).decode("utf-8"))
    except (IndexError, ValueError, binascii.Error, UnicodeDecodeError):
        return {}


def _uuid_fields(index: PackIndex, spec: dict | None) -> set[str]:
    fields: set[str] = set()
    for t in index.tables():
        fields.update(c["name"] for c in t["columns"] if c["type"].startswith("uuid"))

    def _scan(schema: Any) -> None:
        if isinstance(schema, dict):
            for prop, definition in (schema.get("properties") or {}).items():
                if isinstance(definition, dict):
                    if definition.get("format") == "uuid":
                        fields.add(prop)
                    _scan(definition)
            _scan(schema.get("items"))

    for schema in (((spec or {}).get("components") or {}).get("schemas") or {}).values():
        _scan(schema)
    return fields


def _check_uuid_examples(index: PackIndex, spec: dict | None, payloads: dict | None,
                         result: PreflightResult) -> dict[str, str]:
    fields = _uuid_fields(index, spec)
    bad: dict[str, set[str]] = defaultdict(set)
    for where, _path, example in _examples(spec) + _payload_examples(payloads):
        for key, value in _walk_pairs(example):
            if key in fields and isinstance(value, str) and value and not _UUID_RE.match(value):
                bad[value].add(where.split(" ")[0] if where.startswith("sample") else "openapi")
    replacements = {v: str(uuid.uuid5(uuid.NAMESPACE_URL, f"sdlc-pack-example:{v}")) for v in sorted(bad)}
    if bad:
        sample = ", ".join(f"`{v}`" for v in sorted(bad)[:5]) + (" …" if len(bad) > 5 else "")
        result.findings.append(Finding(
            check="malformed_example", severity=NORMALIZED,
            title=f"{len(bad)} example id(s) are not valid UUIDs",
            detail=(f"Fields typed `uuid` carry example values that are not UUIDs ({sample}). A test or seed "
                    "built from them fails validation before it tests anything. Generation is given a "
                    "normalized copy of the sample payloads where each one is replaced by a deterministic "
                    "valid UUID (the same invalid value always maps to the same UUID, so references between "
                    "payloads still line up)."),
            canonical={k: replacements[k] for k in sorted(replacements)[:5]},
            sources=sorted({w for ws in bad.values() for w in ws}),
        ))
    return replacements


def _check_coverage(index: PackIndex, spec: dict | None, result: PreflightResult) -> None:
    ops = _operations(spec)
    spec_keys = {design_pack.endpoint_key(m, p) for m, p, _ in ops}
    rows = index.api_ui_rows()
    if rows and spec_keys:
        screen_col, endpoint_col = design_pack._table_endpoint_columns(list(rows[0].keys()))
        missing: dict[str, set[str]] = defaultdict(set)
        for row in rows:
            parsed = design_pack._parse_endpoint_cell(row.get(endpoint_col or "", "")) if endpoint_col else None
            if parsed and design_pack.endpoint_key(*parsed) not in spec_keys:
                missing[f"{parsed[0]} {parsed[1]}"].add(str(row.get(screen_col or "", "")))
        for endpoint, screens in sorted(missing.items()):
            result.findings.append(Finding(
                check="missing_endpoint", severity=GAP,
                title=f"`{endpoint}` is used by the UI but not defined in the API",
                detail=f"Screen(s) {', '.join(sorted(s for s in screens if s))} call `{endpoint}`, which the OpenAPI spec does not define.",
                sources=["api-to-ui mapping", "openapi"],
            ))

    # Foreign keys a screen SENDS need a way to list the choices.
    tables = index.tables()
    table_names = {t["name"] for t in tables}
    fk_target = {c: fk["ref_table"] for t in tables for fk in t["foreign_keys"] for c in fk["columns"] if fk.get("ref_table")}
    list_tables = {
        _table_for_path(p, table_names) for m, p, _ in ops
        if m == "GET" and not re.search(r"\{[^}]+\}\s*$", p)
    } - {None}
    if rows:
        headers = {h.lower(): h for h in rows[0]}
        dir_col = headers.get("direction")
        field_col = next((headers[h] for h in headers if "field" in h and "api" in h), None)
        label_col = next((headers[h] for h in headers if "field" in h and "ui" in h), None)
        screen_col, _ = design_pack._table_endpoint_columns(list(rows[0].keys()))
        # Sending a reference is not the same as CHOOSING it: a checkout sends the current cart's
        # id from context. Only a reference the UI lets the user pick needs a list endpoint — the
        # mockup renders a <select> for it, or (no mockup) its label says assign/select/choose.
        html = index.mockup_html()
        select_fields = {
            m.group(1).split("-")[-1] if "-" in m.group(1) else m.group(1)
            for attrs in re.findall(r"<select\b([^>]*)>", html, re.IGNORECASE)
            for m in [re.search(r"\bname=[\"']([\w-]+)[\"']", attrs) or re.search(r"\bid=[\"']([\w-]+)[\"']", attrs)] if m
        }
        needed: dict[tuple[str, str], set[str]] = defaultdict(set)
        for row in rows:
            if dir_col and str(row.get(dir_col, "")).strip().lower() not in {"send", "input", "write"}:
                continue
            column = str(row.get(field_col or "", "")).split(".")[-1].strip()
            target = fk_target.get(column)
            if not target or target in list_tables:
                continue
            label = str(row.get(label_col or "", ""))
            is_choice = column in select_fields if html else bool(
                re.search(r"\b(?:assign|reassign|select|choose|pick)\b", label, re.IGNORECASE))
            if is_choice:
                needed[(column, target)].add(str(row.get(screen_col or "", "")))
        for (column, target), screens in sorted(needed.items()):
            result.findings.append(Finding(
                check="missing_list_endpoint", severity=GAP,
                title=f"No endpoint lists `{target}`, but screens choose one",
                detail=(f"{', '.join(sorted(s for s in screens if s))} send(s) `{column}` (a reference to `{target}`), "
                        f"but the API has no endpoint that lists `{target}`, so the UI cannot offer the choices. "
                        f"Add a list endpoint for `{target}` (or say which existing one supplies it)."),
                sources=["api-to-ui mapping", "openapi", "db schema"],
            ))

    # A login with no way to create the first user.
    login = next((f"{m} {p}" for m, p, _ in ops if m == "POST" and _LOGIN_PATH_RE.search(p)), None)
    user_table = next((t for t in tables if any(_PASSWORD_COL_RE.search(c["name"]) for c in t["columns"])), None)
    if login and user_table:
        creates = [
            f"{m} {p}" for m, p, _ in ops
            if m == "POST" and (_table_for_path(p, table_names) == user_table["name"] or _SIGNUP_PATH_RE.search(p))
        ]
        result.facts["auth"] = {"login": login, "user_table": user_table["name"],
                                "user_creation": creates, "bootstrap_needed": not creates}
        if not creates:
            result.findings.append(Finding(
                check="bootstrap_needed", severity=GAP,
                title="There is a login but no way to create the first user",
                detail=(f"`{login}` authenticates against `{user_table['name']}`, but no endpoint creates a "
                        "user, and the pack has no seed data — a freshly deployed app cannot be logged into. "
                        "The pipeline will add a first-administrator seed command (credentials from env "
                        "variables); the Design team should still decide how users are managed."),
                canonical={"seed_admin": True, "user_table": user_table["name"]},
                sources=["openapi", "db schema"],
            ))


def _check_env_names(index: PackIndex, leaves: list[tuple[str, str]], result: PreflightResult) -> None:
    declared = [v["key"] for v in index.env_vars()]
    if not declared:
        return
    result.facts["env_vars"] = declared
    declares_frontend = any(k.startswith(_FRONTEND_ENV_PREFIXES) for k in declared)
    stray: dict[str, set[str]] = defaultdict(set)
    for path, desc in leaves:
        # Only where the structure is talking about CONFIGURATION: a config/settings/env file, or a
        # description that mentions the environment. UPPER_SNAKE elsewhere is usually something
        # else entirely — error codes (INVALID_CREDENTIALS), constants, enum members.
        name_part = path.rsplit("/", 1)[-1].lower()
        if not (re.search(r"config|settings|\.env|env\.|docker-compose|compose\.ya?ml", name_part)
                or re.search(r"\benv(?:ironment)?\b|environment variable", desc, re.IGNORECASE)):
            continue
        for name in _ENV_NAME_RE.findall(desc):
            if name in declared:
                continue
            if name.startswith(_FRONTEND_ENV_PREFIXES) and not declares_frontend:
                continue  # a bundler-exposed frontend variable; these env files describe the backend
            stray[name].add(path)
    for name, paths in sorted(stray.items()):
        guess = _closest(name, declared)
        result.findings.append(Finding(
            check="env_name_conflict", severity=NORMALIZED,
            title=f"Environment variable `{name}` is not declared by the pack's env files",
            detail=(f"{', '.join(sorted(paths))} mention(s) `{name}`, which `.env.example` / `env-vars.json` do not "
                    "declare. Generated config must read the declared names"
                    + (f"; this one is most likely `{guess}`." if guess else ".")),
            canonical=guess, sources=sorted(paths),
        ))
    m = re.search(r"prefix(?:ed)?\s+(?:all\s+)?(?:the\s+)?(?:environment variables|env(?:ironment)?\s+var\w*)[^.\n]*?"
                  r"\bwith\s+[`'\"]?([A-Z][A-Z0-9]*_)", index.conventions_text(), re.IGNORECASE)
    if m:
        prefix = m.group(1)
        without = [k for k in declared if not k.startswith(prefix)]
        if len(without) > len(declared) / 2:
            result.findings.append(Finding(
                check="env_prefix_rule", severity=NORMALIZED,
                title=f"Conventions require a `{prefix}` prefix the pack's own env files don't use",
                detail=(f"The conventions say every backend variable starts with `{prefix}`, but {len(without)} of "
                        f"{len(declared)} declared variables don't (e.g. {', '.join(without[:4])}). The declared "
                        "names win; the prefix rule is not applied."),
                canonical=declared[:8], sources=["conventions", "env files"],
            ))


def _check_symbol_owners(index: PackIndex, leaves: list[tuple[str, str]], result: PreflightResult) -> None:
    columns = {c["name"] for t in index.tables() for c in t["columns"]} | {t["name"] for t in index.tables()}
    owners_of: dict[str, list[tuple[str, int, int]]] = defaultdict(list)   # symbol -> (path, n_symbols, pos)
    for path, desc in leaves:
        if re.search(r"package initiali[sz]er|re-exports?", desc, re.IGNORECASE) or "/tests/" in f"/{path}":
            continue
        symbols = []
        for m in _SNAKE_RE.finditer(desc):
            ident = m.group(0)
            if ident in columns or any(col.startswith(ident) for col in columns):
                continue
            tail = desc[m.end(): m.end() + 40]
            if ident.startswith(_SYMBOL_VERBS) or tail.startswith("(") or re.match(rf"\s+{_SYMBOL_NOUNS_RE}\b", tail):
                symbols.append((ident, m.start()))
        first_pos: dict[str, int] = {}
        for ident, pos in symbols:
            first_pos.setdefault(ident, pos)
        # (path, how many symbols this file's description lists, where this one appears): the
        # owner is the file most DEDICATED to a symbol — "require_role dependency factory ..."
        # beats a list of three helpers that merely includes it.
        for ident, pos in first_pos.items():
            owners_of[ident].append((path, len(first_pos), pos))
    owned: dict[str, str] = {}
    for symbol, places in sorted(owners_of.items()):
        files = {p for p, _n, _pos in places}
        if len(files) < 2:
            continue
        owner = min(places, key=lambda x: (x[1], x[2], x[0]))[0]
        others = sorted(files - {owner})
        owned[symbol] = owner
        result.findings.append(Finding(
            check="duplicate_owner", severity=NORMALIZED,
            title=f"`{symbol}` is described in {len(files)} files",
            detail=(f"The structure describes `{symbol}` in {', '.join(sorted(files))}. Generated separately, each "
                    "file would define its own version. It is defined ONCE, in the file dedicated to it; the "
                    f"others import it from there: {', '.join(others)}."),
            canonical={"symbol": symbol, "owner": owner, "importers": others},
            sources=sorted(files),
        ))
    if owned:
        result.facts["symbol_owners"] = owned


def _check_error_envelope(index: PackIndex, spec: dict | None, result: PreflightResult) -> None:
    schemas = ((spec or {}).get("components") or {}).get("schemas") or {}
    name = next((n for n in schemas if re.fullmatch(r"(?:api)?(?:error|errorresponse|errorbody|problem|problemdetails)", n.lower())), None)
    if not name or not isinstance(schemas[name], dict):
        return
    fields = list((schemas[name].get("properties") or {}).keys())
    result.facts["error_envelope"] = {"schema": name, "fields": fields, "required": list(schemas[name].get("required") or [])}
    conventions = index.conventions_text()
    for m in re.finditer(r"\{([^{}]{0,200})\}", conventions):
        window = conventions[max(0, m.start() - 160): m.start()].lower()
        keys = re.findall(r"[\"'](\w+)[\"']\s*:", m.group(1))
        if keys and "error" in window and set(keys) != set(fields):
            result.findings.append(Finding(
                check="error_envelope", severity=NORMALIZED,
                title="Two different error response shapes",
                detail=(f"The conventions describe error responses as `{{{', '.join(keys)}}}`, but the OpenAPI "
                        f"`{name}` schema is `{{{', '.join(fields)}}}`. The API contract wins: every error the "
                        "backend returns, and every error the frontend parses, uses the OpenAPI shape."),
                canonical=fields, sources=["conventions", "openapi"],
            ))
            break


def _check_base_path(spec: dict | None, result: PreflightResult) -> None:
    if not spec:
        return
    servers = spec.get("servers") or []
    url = servers[0].get("url") if servers and isinstance(servers[0], dict) else (servers[0] if servers else "")
    base = urlparse(str(url or "")).path.rstrip("/") if url else ""
    result.facts["api_base_path"] = base
    if base:
        result.findings.append(Finding(
            check="api_base_path", severity=INFO,
            title=f"API paths live under `{base}`",
            detail=(f"The OpenAPI server URL ends in `{base}`, so the routers' mount prefix, the frontend API "
                    "client's base URL and any dev-server proxy must all agree on it."),
            canonical=base, sources=["openapi servers"],
        ))


def _check_role_screens(index: PackIndex, result: PreflightResult) -> None:
    roles = result.facts.get("roles") or {}
    op_roles: dict[str, list[str]] = result.facts.get("operation_roles") or {}
    if not roles or not op_roles:
        return
    rows = index.api_ui_rows()
    if not rows:
        return
    screen_col, endpoint_col = design_pack._table_endpoint_columns(list(rows[0].keys()))
    screen_endpoints: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        parsed = design_pack._parse_endpoint_cell(row.get(endpoint_col or "", "")) if endpoint_col else None
        if parsed:
            screen_endpoints[str(row.get(screen_col or "", "")).strip()].add(f"{parsed[0]} {parsed[1]}")
    canonical = {r.lower(): r for r in roles.get("values", [])}
    conflicts: dict[tuple[str, str], set[str]] = defaultdict(set)
    for name in index.names("diagrams"):
        stem = name.rsplit("/", 1)[-1].rsplit(".", 1)[0].lower()
        role = canonical.get(stem)
        text = index.text(name)
        if not role or text.lstrip().startswith("erDiagram"):
            continue
        for screen, endpoints in screen_endpoints.items():
            if screen and re.search(rf"\b{re.escape(screen)}\b", text, re.IGNORECASE):
                for endpoint in endpoints:
                    allowed = op_roles.get(endpoint)
                    if allowed and role not in allowed:
                        conflicts[(role, endpoint)].add(screen)
    for (role, endpoint), screens in sorted(conflicts.items()):
        result.findings.append(Finding(
            check="role_screen_conflict", severity=INFO,
            title=f"{roles['display'].get(role, role)}'s use-cases need `{endpoint}`, which doesn't allow them",
            detail=(f"The {role} use-case diagram includes {', '.join(sorted(screens))}, which call(s) `{endpoint}`, "
                    f"but that endpoint allows only {', '.join(op_roles[endpoint])}. Either the screen or the "
                    "permission is wrong; the API's permission is what gets generated."),
            sources=["use-case diagrams", "openapi"],
        ))


def _check_open_questions(index: PackIndex, result: PreflightResult) -> None:
    for name, obj in index._roles.get("decisions", []):
        if not isinstance(obj, dict):
            continue
        for concern in obj.get("srs_concerns") or obj.get("open_questions") or []:
            if isinstance(concern, dict) and concern.get("question"):
                result.findings.append(Finding(
                    check="open_question", severity=INFO,
                    title=f"Unresolved requirements question ({concern.get('kind', 'question')})",
                    detail=str(concern["question"]) + (f"\n\nEvidence: {concern['evidence']}" if concern.get("evidence") else ""),
                    sources=[name],
                ))


def _check_manifest(index: PackIndex, result: PreflightResult) -> None:
    hit = index.first("manifest_index")
    if not hit or not isinstance(hit[1], str):
        return
    present = {k.lower() for k in index.package}
    missing = []
    for ref in sorted(set(re.findall(r"[\w./\\-]+\.(?:json|ya?ml|md|csv|sql|html|example)\b", hit[1]))):
        ref_norm = ref.replace("\\", "/").lower()
        while ref_norm.startswith("./"):
            ref_norm = ref_norm[2:]
        ref_norm = ref_norm.lstrip("/")      # NOT lstrip("./"): that also eats the dot of ".env.example"
        if design_pack.is_pipeline_state_file(ref_norm):
            continue
        if not any(p == ref_norm or p.endswith("/" + ref_norm) for p in present):
            missing.append(ref)
    if missing:
        result.findings.append(Finding(
            check="manifest_missing", severity=INFO,
            title=f"{len(missing)} file(s) listed in the pack index are not in the pack",
            detail="Listed but not found: " + ", ".join(f"`{m}`" for m in missing),
            sources=[hit[0]],
        ))


# --------------------------------------------------------------------------- entry points

def run_preflight(index: PackIndex) -> PreflightResult:
    """Every check over one indexed design package."""
    result = PreflightResult()
    spec = index.openapi()
    payloads = index.sample_payloads()
    leaves = _structure_leaves(index)
    _check_enums(index, spec, payloads, result)
    _check_roles(index, spec, payloads, result)
    _check_uuid_examples(index, spec, payloads, result)
    _check_coverage(index, spec, result)
    _check_env_names(index, leaves, result)
    _check_symbol_owners(index, leaves, result)
    _check_error_envelope(index, spec, result)
    _check_base_path(spec, result)
    _check_role_screens(index, result)
    _check_open_questions(index, result)
    _check_manifest(index, result)
    result.findings.sort(key=lambda f: (_SEVERITY_ORDER.get(f.severity, 9), f.check, f.title))
    return result


def normalized_sample_payloads(index: PackIndex) -> dict[str, Any] | None:
    """The pack's sample payloads with every invalid example UUID replaced by a deterministic valid
    one (consistently: the same bad value always becomes the same UUID). A copy — the pack itself
    is never modified."""
    payloads = index.sample_payloads()
    if payloads is None:
        return None
    probe = PreflightResult()
    replacements = _check_uuid_examples(index, index.openapi(), payloads, probe)
    if not replacements:
        return payloads

    def _swap(node: Any) -> Any:
        if isinstance(node, dict):
            return {k: _swap(v) for k, v in node.items()}
        if isinstance(node, list):
            return [_swap(v) for v in node]
        if isinstance(node, str):
            return replacements.get(node, node)
        return node

    return _swap(copy.deepcopy(payloads))


_RESULT_CACHE: OrderedDict[str, PreflightResult] = OrderedDict()
_RESULT_CACHE_MAX = 8


def preflight_for(design_package: dict[str, Any] | None) -> PreflightResult:
    """:func:`run_preflight` for this package CONTENT, computed once and reused (later stages ask for
    the canonical facts once per generated file)."""
    package = design_package or {}
    key = _fingerprint(package)
    hit = _RESULT_CACHE.get(key)
    if hit is not None:
        _RESULT_CACHE.move_to_end(key)
        return hit
    result = run_preflight(index_for(package))
    _RESULT_CACHE[key] = result
    if len(_RESULT_CACHE) > _RESULT_CACHE_MAX:
        _RESULT_CACHE.popitem(last=False)
    return result
