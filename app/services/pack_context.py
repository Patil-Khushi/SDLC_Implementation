"""The slices of a design package one generation call needs — deterministic, no LLM, no IO.

Code generation used to assemble each work item's context by artifact FILENAME (``SKILL.md``,
``schema.sql``, ``validation-rules.json``) and by crude text filters: an OpenAPI YAML contributed
only the lines containing a cited path, the mockup only lines naming a screen, validation rules
only groups whose key contained ``login``. On the current pack format that left Fleet's backend
items with no conventions, no table definitions and no request/response schemas — the model
generated an API it had never been shown.

Every function here takes a :class:`~app.services.pack_index.PackIndex` (artifacts found by
CONTENT) and returns ready-to-embed text, or ``""`` when the pack has nothing for it — so a
sparse pack degrades to less context, never to an error. Each slice has its own character budget
and is cut on a line boundary with a visible marker, so one huge artifact can't crowd out the rest.

What is sliced how:

* API — the cited operations, parsed (parameters, body, responses, security), plus EVERY
  component they reference, transitively, so a schema class is generated with its real fields.
* DB — the pack's own DDL for the cited tables (with their indexes, ALTERs, enum types and the
  comments above them), or the normalized definition when the schema is JSON-only.
* Validation rules — the groups bound to the cited operations by operationId, request-schema name
  or ``METHOD /path``; groups bound to no operation at all (``passwordPolicy``, ``errorCodes``)
  are pack-wide and go to every item.
* Per FILE: a page gets its own screen's mockup section, states and endpoints; a frontend
  service/type/hook/component gets the operations of the resource its name is about; a config
  module gets the declared environment variables.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable

import yaml

from app.services.pack_index import PackIndex

#: Character budget per slice. Generous for what a file is built FROM (schemas, DDL, mockup),
#: tighter for background (states, payloads).
BUDGET = {
    "conventions": 20_000, "tokens": 10_000, "rules": 8_000, "validation_shared": 4_000,
    "requirements": 8_000, "api": 24_000, "api_basics": 4_000, "db": 30_000, "validation": 8_000,
    "payloads": 6_000, "routes": 6_000, "states": 5_000, "mockup": 28_000, "style_guide": 16_000,
    "file_api": 12_000, "env": 5_000, "component": 6_000,
}

_HTTP = ("get", "post", "put", "patch", "delete", "head", "options")


def clip(text: str, limit: int) -> str:
    """``text`` cut to ``limit`` chars on a line boundary, with a marker saying how much went."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit("\n", 1)[0] if "\n" in text[:limit] else text[:limit]
    return f"{cut}\n... ({len(text) - len(cut)} more characters not shown)"


def _yaml(obj: Any) -> str:
    try:
        return yaml.safe_dump(obj, sort_keys=False, default_flow_style=False, width=110, allow_unicode=False).strip()
    except yaml.YAMLError:
        return json.dumps(obj, indent=1, sort_keys=False, default=str)


def _singular(word: str) -> str:
    if len(word) > 3 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 4 and word.endswith(("sses", "xes", "ches", "shes")):
        return word[:-2]
    if word.endswith(("ss", "us", "is")):
        return word
    return word[:-1] if len(word) > 1 and word.endswith("s") else word


def _words(text: str) -> list[str]:
    """``VehicleTypesService`` / ``vehicle_types`` / ``vehicle-types`` -> ``["vehicle", "type", "service"]``."""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", str(text))
    return [_singular(w) for w in re.findall(r"[a-z0-9]+", spaced.lower())]


def _norm(text: str) -> str:
    return "".join(_words(text))


# --------------------------------------------------------------------------- operations

def _endpoint_key(method: str, path: str) -> str:
    path = re.sub(r"\{[^}]*\}", "{}", str(path).split("?", 1)[0]).rstrip("/") or "/"
    return f"{method.upper()} {path.lower()}"


def operations_for(index: PackIndex, endpoints: Iterable[str]) -> list[tuple[str, str, dict[str, Any]]]:
    """The spec's operations cited by ``endpoints`` (``"METHOD /path"``, or a bare path for all
    of its methods), in spec order. Path parameter names need not match (``{id}`` ~ ``{vehicleId}``)."""
    keys, paths = set(), set()
    for ep in endpoints or []:
        method, _, path = str(ep).strip().partition(" ")
        if not path:
            paths.add(_endpoint_key("GET", method).split(" ", 1)[1])
        elif method.lower() in _HTTP:
            keys.add(_endpoint_key(method, path))
    return [
        (m, p, op) for m, p, op in index.operations()
        if _endpoint_key(m, p) in keys or _endpoint_key(m, p).split(" ", 1)[1] in paths
    ]


def _resource_of(path: str) -> str:
    literal = [s for s in str(path).strip("/").split("/") if s and not s.startswith("{")]
    return _norm(literal[0]) if literal else ""


#: Words that say what KIND of file something is, not which resource it is about.
_KIND_WORDS = {
    "service", "api", "client", "hook", "use", "context", "provider", "store", "slice", "type", "index",
    "page", "view", "screen", "component", "card", "form", "list", "detail", "row", "item", "table",
    "modal", "field", "badge", "util", "helper", "container", "section", "panel", "my", "query",
    "mutation", "action", "reducer", "selector", "model", "schema", "dto", "interface", "tsx", "ts",
}


def operations_for_file(index: PackIndex, path: str) -> list[tuple[str, str, dict[str, Any]]]:
    """Operations of the resource a frontend file is named after: ``vehicleTypesService.ts`` ->
    the ``/vehicle-types`` operations, ``useMyWorkOrders.ts`` -> ``/work-orders``,
    ``OdometerReadingForm.tsx`` -> ``/readings``. The longest run of the name's words that names
    a resource (a path's first segment, or a tag) wins; a name about no resource gets nothing."""
    stem = str(path).replace("\\", "/").rsplit("/", 1)[-1].split(".", 1)[0]
    words = _words(stem)
    if not words:
        return []
    by_resource: dict[str, list[tuple[str, str, dict[str, Any]]]] = {}
    for m, p, op in index.operations():
        for key in {_resource_of(p), *(_norm(t) for t in op.get("tags") or [])}:
            if key:
                by_resource.setdefault(key, []).append((m, p, op))
    # Every word takes part in a run ("vehicleTypesService" is about vehicle-types, not vehicles),
    # but a lone kind word is never a resource ("LoginForm" is not about a /forms resource).
    for size in range(len(words), 0, -1):
        for start in range(len(words) - size + 1):
            if size == 1 and words[start] in _KIND_WORDS:
                continue
            hits = by_resource.get("".join(words[start:start + size]))
            if hits:
                rest = set(words) - set(words[start:start + size])
                # Operations that also name the rest of the file's words first (useDepotDashboard:
                # the dashboard operation before the depot CRUD).
                return sorted(hits, key=lambda h: -len(rest & set(_words(h[1]))))
    return []


def _refs(node: Any) -> Iterable[str]:
    stack = [node]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            ref = cur.get("$ref")
            if isinstance(ref, str):
                yield ref
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)


def _component(spec: dict[str, Any], ref: str) -> tuple[str, str, Any] | None:
    m = re.fullmatch(r"#/components/(\w+)/(.+)", ref)
    if not m:
        return None
    obj = ((spec.get("components") or {}).get(m.group(1)) or {}).get(m.group(2))
    return (m.group(1), m.group(2), obj) if obj is not None else None


def component_closure(spec: dict[str, Any], roots: Iterable[Any]) -> list[tuple[str, str, Any]]:
    """Every component ``roots`` reference, transitively, in first-reference order."""
    out: list[tuple[str, str, Any]] = []
    seen: set[str] = set()
    queue = [ref for root in roots for ref in _refs(root)]
    while queue:
        ref = queue.pop(0)
        if ref in seen:
            continue
        seen.add(ref)
        comp = _component(spec, ref)
        if comp:
            out.append(comp)
            queue.extend(r for r in _refs(comp[2]) if r not in seen)
    return out


def _schema_label(spec: dict[str, Any], schema: Any) -> str:
    if not isinstance(schema, dict):
        return ""
    if isinstance(schema.get("$ref"), str):
        return schema["$ref"].rsplit("/", 1)[-1]
    if schema.get("type") == "array":
        inner = _schema_label(spec, schema.get("items"))
        return f"{inner}[]" if inner else "array"
    return str(schema.get("type") or "object")


def _content_label(spec: dict[str, Any], node: Any) -> str:
    if not isinstance(node, dict):
        return ""
    if isinstance(node.get("$ref"), str):                     # a components/responses ref
        comp = _component(spec, node["$ref"])
        return _content_label(spec, comp[2]) if comp else node["$ref"].rsplit("/", 1)[-1]
    for media, body in (node.get("content") or {}).items():
        if isinstance(body, dict) and "schema" in body:
            return f"{_schema_label(spec, body['schema'])} ({media})" if "json" not in media else _schema_label(spec, body["schema"])
    return ""


def _render_operation(spec: dict[str, Any], method: str, path: str, op: dict[str, Any]) -> list[str]:
    head = f"### {method} {path}" + (f"  (operationId: {op['operationId']})" if op.get("operationId") else "")
    lines = [head]
    text = " ".join(str(op.get(k) or "").strip() for k in ("summary", "description")).strip()
    if text:
        lines.append(clip(text, 900))
    security = op["security"] if "security" in op else spec.get("security")
    if security == []:
        lines.append("security: none (public)")
    elif security:
        lines.append("security: " + ", ".join(name for req in security if isinstance(req, dict) for name in req))
    item = (spec.get("paths") or {}).get(path) or {}
    params = list(item.get("parameters") or []) + list(op.get("parameters") or [])
    rendered = []
    for param in params:
        if isinstance(param, dict) and "$ref" in param:
            comp = _component(spec, param["$ref"])
            param = comp[2] if comp else param
        if isinstance(param, dict) and param.get("name"):
            schema = param.get("schema") or {}
            bits = [param.get("in", ""), _schema_label(spec, schema)]
            if param.get("required"):
                bits.append("required")
            if isinstance(schema, dict) and schema.get("enum"):
                bits.append("one of " + "|".join(map(str, schema["enum"])))
            if isinstance(schema, dict) and "default" in schema:
                bits.append(f"default {schema['default']}")
            rendered.append(f"{param['name']} ({', '.join(b for b in bits if b)})")
    if rendered:
        lines.append("parameters: " + "; ".join(rendered))
    body = op.get("requestBody")
    if isinstance(body, dict):
        label = _content_label(spec, body)
        if label:
            lines.append(f"request body: {label}" + (" (required)" if body.get("required") else ""))
    responses = []
    for status, response in (op.get("responses") or {}).items():
        label = _content_label(spec, response)
        desc = (response.get("description") if isinstance(response, dict) else "") or ""
        responses.append(f"{status} {label or '(no body)'}" + (f" - {clip(desc, 120)}" if desc and not label else ""))
    if responses:
        lines.append("responses: " + "; ".join(responses))
    return lines


def api_section(index: PackIndex, ops: list[tuple[str, str, dict[str, Any]]], *, budget: int = BUDGET["api"]) -> str:
    """The operations, then every component they reference (transitively) as YAML."""
    spec = index.openapi() or {}
    if not ops:
        return ""
    lines: list[str] = []
    for method, path, op in ops:
        lines.extend(_render_operation(spec, method, path, op))
        lines.append("")
    item_params = [((spec.get("paths") or {}).get(p) or {}).get("parameters") for _m, p, _o in ops]
    closure = component_closure(spec, [op for _m, _p, op in ops] + item_params)
    schemes = {name for _m, _p, op in ops for req in (op.get("security") or spec.get("security") or [])
               if isinstance(req, dict) for name in req}
    security_schemes = ((spec.get("components") or {}).get("securitySchemes") or {})
    if closure or schemes:
        lines.append("### Components (every one the operations above reference, transitively)")
        for kind, name, obj in closure:
            lines.append(_yaml({name: obj}) if kind == "schemas" else _yaml({f"{kind}/{name}": obj}))
        for name in sorted(schemes):
            if name in security_schemes:
                lines.append(_yaml({f"securitySchemes/{name}": security_schemes[name]}))
    return clip("\n".join(lines), budget)


def api_basics(index: PackIndex, error_schema: str | None = None) -> str:
    """What every backend file may need from the API without citing an operation: the server
    URL, the security schemes, and the error body schema."""
    spec = index.openapi() or {}
    if not spec:
        return ""
    components = spec.get("components") or {}
    out: dict[str, Any] = {}
    if spec.get("servers"):
        out["servers"] = spec["servers"]
    if components.get("securitySchemes"):
        out["securitySchemes"] = components["securitySchemes"]
    schemas = components.get("schemas") or {}
    if error_schema and error_schema in schemas:
        out[error_schema] = schemas[error_schema]
        for _k, name, obj in component_closure(spec, [schemas[error_schema]]):
            out.setdefault(name, obj)
    return clip(_yaml(out), BUDGET["api_basics"]) if out else ""


def screen_endpoints(index: PackIndex, screens: Iterable[str]) -> list[str]:
    """``METHOD /path`` strings the UI <-> API mapping assigns to ``screens``."""
    from app.services.design_pack import _table_endpoint_columns

    out: list[str] = []
    for screen in screens or []:
        rows = index.api_ui_rows(screen)
        if not rows:
            continue
        _screen_col, endpoint_col = _table_endpoint_columns(list(rows[0].keys()))
        for row in rows:
            value = str(row.get(endpoint_col, "") if endpoint_col else "").strip()
            if re.match(r"^[A-Za-z]+\s+/", value) and value not in out:
                out.append(value)
    return out


# --------------------------------------------------------------------------- database

_COMMENT_LINES_RE = re.compile(r"(?:^[ \t]*--[^\n]*\n)+\Z", re.MULTILINE)


def _sql_texts(index: PackIndex) -> list[str]:
    return [v for v in (index.package.get(n) for n in index.names("db_schema")) if isinstance(v, str)]


def _statement_from(text: str, start: int) -> str:
    """The SQL statement starting at ``start``: through its balanced parentheses to the ``;``."""
    depth, i, quote = 0, start, ""
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "'\"":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == ";" and depth <= 0:
            return text[start: i + 1]
        i += 1
    return text[start:]


def _ddl_for(text: str, table: str) -> str:
    name = rf"(?:[\w\"`]+\.)?[\"`]?{re.escape(table)}[\"`]?"
    m = re.search(rf"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?{name}\s*\(", text, re.IGNORECASE)
    if not m:
        return ""
    lead = _COMMENT_LINES_RE.search(text[: m.start()])
    parts = [((lead.group(0) if lead else "") + _statement_from(text, m.start())).strip()]
    parts[0] = re.sub(r"\n(?:[ \t]*\n)+", "\n", parts[0])     # packs often double-space DDL
    for stmt_re in (
        rf"CREATE\s+(?:UNIQUE\s+)?INDEX\s+[^;]*?\bON\s+{name}\b",
        rf"ALTER\s+TABLE\s+(?:ONLY\s+)?{name}\s",
        rf"COMMENT\s+ON\s+(?:TABLE|COLUMN)\s+{name}\b",
        rf"CREATE\s+(?:OR\s+REPLACE\s+)?TRIGGER\s+[^;]*?\bON\s+{name}\b",
    ):
        for s in re.finditer(stmt_re, text, re.IGNORECASE):
            parts.append(_statement_from(text, s.start()).strip())
    body = parts[0]
    for t in re.finditer(r"CREATE\s+TYPE\s+[\"`]?(\w+)[\"`]?\s+AS\s+ENUM", text, re.IGNORECASE):
        if re.search(rf"\b{re.escape(t.group(1))}\b", body):
            parts.insert(0, _statement_from(text, t.start()).strip())
    return "\n".join(dict.fromkeys(parts))


def _render_table(table: dict[str, Any]) -> str:
    lines = [f"TABLE {table['name']}"]
    for col in table["columns"]:
        bits = [col["name"], col.get("type") or ""]
        if not col.get("nullable", True):
            bits.append("NOT NULL")
        if col.get("default") is not None:
            bits.append(f"DEFAULT {col['default']}")
        if col["name"] in table.get("enums", {}):
            bits.append("IN (" + ", ".join(f"'{v}'" for v in table["enums"][col["name"]]) + ")")
        lines.append("  " + " ".join(b for b in bits if b))
    if table.get("primary_key"):
        lines.append(f"  PRIMARY KEY ({', '.join(table['primary_key'])})")
    for fk in table.get("foreign_keys") or []:
        lines.append(f"  FOREIGN KEY ({', '.join(fk.get('columns') or [])}) REFERENCES "
                     f"{fk.get('ref_table')}({', '.join(fk.get('ref_columns') or [])})")
    for check in table.get("checks") or []:
        lines.append(f"  CHECK ({check})")
    return "\n".join(lines)


def db_section(index: PackIndex, tables: Iterable[str], *, budget: int = BUDGET["db"]) -> str:
    """The cited tables as the pack defines them: its own DDL when it has SQL, else normalized."""
    wanted = [t for t in dict.fromkeys(tables or []) if t]
    if not wanted:
        return ""
    texts = _sql_texts(index)
    normalized = {t["name"]: t for t in index.tables()}
    blocks = []
    for table in wanted:
        ddl = next((d for d in (_ddl_for(text, table) for text in texts) if d), "")
        if ddl:
            blocks.append(ddl)
        elif table in normalized:
            blocks.append(_render_table(normalized[table]))
    return clip("\n\n".join(blocks), budget)


# --------------------------------------------------------------------------- validation rules

def _validation_groups(index: PackIndex) -> dict[str, Any]:
    rules = index.validation_rules()
    if isinstance(rules, dict):
        return rules
    return {}


def _group_binds(key: str, op_ids: set[str], schema_names: set[str], ep_keys: set[str], paths: set[str]) -> bool:
    k = key.strip()
    method, _, path = k.partition(" ")
    if path and method.lower() in _HTTP:
        return _endpoint_key(method, path) in ep_keys
    if k.startswith("/"):
        return _endpoint_key("GET", k).split(" ", 1)[1] in paths
    return k.lower() in op_ids or k.lower() in schema_names


def _binding_sets(spec: dict[str, Any], ops: list[tuple[str, str, dict[str, Any]]], endpoints: Iterable[str] = ()):
    op_ids = {str(op.get("operationId") or "").lower() for _m, _p, op in ops} - {""}
    schema_names: set[str] = set()
    for _m, _p, op in ops:
        body = op.get("requestBody") or {}
        for media in (body.get("content") or {}).values():
            if isinstance(media, dict):
                schema_names.update(r.rsplit("/", 1)[-1].lower() for r in _refs(media.get("schema")))
    ep_keys = {_endpoint_key(m, p) for m, p, _op in ops}
    for ep in endpoints or []:
        method, _, path = str(ep).strip().partition(" ")
        if path:
            ep_keys.add(_endpoint_key(method, path))
    paths = {k.split(" ", 1)[1] for k in ep_keys}
    return op_ids, schema_names, ep_keys, paths


def validation_section(index: PackIndex, ops: list[tuple[str, str, dict[str, Any]]],
                       endpoints: Iterable[str] = ()) -> str:
    """The rule groups bound to these operations (by operationId, request-schema name, or
    ``METHOD /path``), verbatim."""
    groups = _validation_groups(index)
    if not groups:
        return ""
    sets = _binding_sets(index.openapi() or {}, ops, endpoints)
    picked = {k: v for k, v in groups.items() if _group_binds(k, *sets)}
    return clip(json.dumps(picked, indent=1, ensure_ascii=False), BUDGET["validation"]) if picked else ""


def validation_shared(index: PackIndex) -> str:
    """Rule groups bound to NO operation of the spec (password policy, error codes, messages):
    they apply everywhere. A text-only rules artifact is shared whole."""
    groups = _validation_groups(index)
    if not groups:
        hit = index.first("validation_rules")
        return clip(hit[1], BUDGET["validation_shared"]) if hit and isinstance(hit[1], str) else ""
    spec = index.openapi() or {}
    every_op = index.operations()
    sets = _binding_sets(spec, every_op)
    component_names = {n.lower() for n in ((spec.get("components") or {}).get("schemas") or {})}
    shared = {
        k: v for k, v in groups.items()
        if not _group_binds(k, *sets) and k.lower() not in component_names
    }
    if not every_op:          # no spec to bind against: only clearly non-operation groups are shared
        shared = {k: v for k, v in shared.items() if not re.match(r"^[A-Z]+\s+/", k)}
    return clip(json.dumps(shared, indent=1, ensure_ascii=False), BUDGET["validation_shared"]) if shared else ""


# --------------------------------------------------------------------------- requirements & rules

def _req_text(req: dict[str, Any]) -> str:
    for key in ("text", "description", "statement", "requirement", "title", "name"):
        if req.get(key):
            return str(req[key]).strip()
    return ""


def _req_line(req: dict[str, Any]) -> str:
    line = f"- {req.get('id', '?')}: {_req_text(req)}"
    criteria = req.get("acceptance_criteria") or req.get("acceptance") or []
    if isinstance(criteria, list) and criteria:
        line += "\n" + "\n".join(f"  - acceptance: {c}" for c in criteria if c)
    return line


def requirements_section(index: PackIndex, ids: Iterable[str]) -> str:
    reqs = index.requirement_text(list(dict.fromkeys(ids or [])))
    return clip("\n".join(_req_line(r) for r in reqs if _req_text(r)), BUDGET["requirements"]) if reqs else ""


def rules_section(index: PackIndex) -> str:
    """Business rules, non-functional requirements and constraints: pack-wide by nature."""
    reqs = index.requirements()
    parts = []
    for title, key in (("Business rules", "business_rules"), ("Non-functional requirements", "non_functional")):
        lines = [_req_line(r) for r in reqs.get(key) or [] if isinstance(r, dict) and _req_text(r)]
        if lines:
            parts.append(f"{title}:\n" + "\n".join(lines))
    constraints = [str(c.get("text") if isinstance(c, dict) else c).strip() for c in reqs.get("constraints") or []]
    constraints = [c for c in constraints if c and c != "None"]
    if constraints:
        parts.append("Constraints:\n" + "\n".join(f"- {c}" for c in constraints))
    return clip("\n\n".join(parts), BUDGET["rules"])


def payloads_section(index: PackIndex, ops: list[tuple[str, str, dict[str, Any]]]) -> str:
    """The pack's sample request/response bodies for these operations (invalid example UUIDs
    already replaced, see :func:`app.services.pack_preflight.normalized_sample_payloads`)."""
    if not ops:
        return ""
    from app.services.pack_preflight import normalized_sample_payloads

    payloads = normalized_sample_payloads(index)
    if not isinstance(payloads, dict):
        return ""
    ids = {str(op.get("operationId") or "") for _m, _p, op in ops} - {""}
    keys = {_endpoint_key(m, p) for m, p, _op in ops}
    picked = {
        name: body for name, body in payloads.items()
        if name in ids or (isinstance(body, dict) and body.get("method") and body.get("path")
                           and _endpoint_key(str(body["method"]), str(body["path"])) in keys)
    }
    return clip(json.dumps(picked, indent=1, ensure_ascii=False), BUDGET["payloads"]) if picked else ""


# --------------------------------------------------------------------------- frontend

def tokens_section(index: PackIndex) -> str:
    tokens = index.tokens()
    return clip(json.dumps(tokens, indent=1, ensure_ascii=False), BUDGET["tokens"]) if tokens else ""


def routes_section(index: PackIndex, screens: Iterable[str] | None = None) -> str:
    """The route table, narrowed to ``screens`` when given (the router gets all of it)."""
    routes = index.routes()
    if routes is None:
        return ""
    if isinstance(routes, str):
        return clip(routes, BUDGET["routes"])
    wanted = [s.lower() for s in screens or []]
    entries = routes.get("routes") if isinstance(routes, dict) and isinstance(routes.get("routes"), list) else routes
    if wanted and isinstance(entries, list):
        entries = [r for r in entries if any(w in json.dumps(r).lower() for w in wanted)]
    elif wanted and isinstance(entries, dict):
        entries = {k: v for k, v in entries.items() if any(w in f"{k} {json.dumps(v)}".lower() for w in wanted)}
    return clip(json.dumps(entries, indent=1, ensure_ascii=False), BUDGET["routes"]) if entries else ""


#: What a page DOES, with the words packs use for it. Two names that disagree on it are different
#: pages however many nouns they share ("AdminProductNew" is not "Admin Edit Product").
_ACTION_SYNONYMS = {"new": "create", "add": "create", "create": "create", "edit": "edit", "update": "edit",
                    "modify": "edit", "detail": "detail", "delete": "delete", "remove": "delete"}


def _page_words(name: str) -> tuple[set[str], set[str]]:
    """(words that say what the page is ABOUT, words that say what it DOES). "list" is neither: it
    is the default kind of page, and sharing it says nothing ("AdminProductList" vs "Admin Order
    List")."""
    words = {_ACTION_SYNONYMS.get(w, w) for w in _words(name)} - {"page", "screen", "view", "container", "route", "list"}
    return words, words & set(_ACTION_SYNONYMS.values())


def screen_for_file(path: str, screens: Iterable[str], preferred: Iterable[str] = ()) -> str | None:
    """Which of ``screens`` a page file renders: the best word overlap with its name among screens
    that agree with it on what the page does (``VehicleListPage.tsx`` -> "Vehicle List",
    ``AdminProductNew.jsx`` -> "Admin Create Product", ``AdminProductList.jsx`` -> "Admin
    Catalogue Products" rather than "Admin Edit Product"). A tie goes to a ``preferred`` screen;
    ``None`` when no screen clearly wins."""
    stem = str(path).replace("\\", "/").rsplit("/", 1)[-1].split(".", 1)[0]
    have, have_actions = _page_words(stem)
    if not have:
        return None
    favoured = {s.lower() for s in preferred or []}
    scored: list[tuple[float, bool, str]] = []
    for screen in dict.fromkeys(screens or []):
        want, want_actions = _page_words(screen)
        if not want or want_actions != have_actions:
            continue
        scored.append((len(have & want) / len(have | want), screen.lower() in favoured, screen))
    if not scored:
        return None
    scored.sort(key=lambda s: (s[0], s[1]), reverse=True)
    best = scored[0]
    if best[0] < 0.5 or (len(scored) > 1 and scored[1][:2] == best[:2]):
        return None
    return best[2]


def all_screens(index: PackIndex) -> list[str]:
    """Every screen the pack names: the UI <-> API mapping's screens, then mockup sections the
    mapping does not mention (as titles: ``screen-admin-returns`` -> "Admin Returns")."""
    from app.services.design_pack import _table_endpoint_columns

    names: list[str] = []
    rows = index.api_ui_rows()
    if rows:
        screen_col, _ = _table_endpoint_columns(list(rows[0].keys()))
        if screen_col:
            names = list(dict.fromkeys(str(r.get(screen_col, "")).strip() for r in rows if r.get(screen_col)))
    have = {re.sub(r"[^a-z0-9]+", "-", n.lower()).strip("-") for n in names}
    for sid in re.findall(r"<section\b[^>]*\bid=[\"']screen-([\w-]+)[\"']", index.mockup_html() or "", re.IGNORECASE):
        if sid.lower() not in have and not re.search(r"style|guide|design-system", sid, re.IGNORECASE):
            names.append(" ".join(w.capitalize() for w in sid.split("-")))
            have.add(sid.lower())
    return names


def _compact_html(html: str) -> str:
    """Indentation and blank lines removed: a third of a mockup section is whitespace."""
    return re.sub(r"\n(?:[ \t]*\n)+", "\n", re.sub(r"\n[ \t]+", "\n", html or "")).strip()


def own_screen(index: PackIndex, path: str, html: str | None = None) -> str | None:
    """The screen a page file is named after, when the mockup has a section with EXACTLY that name
    (``AdminProductList.jsx`` -> ``screen-admin-product-list``). An exact name beats a merely
    overlapping one: "Admin Edit Product" shares two of three words with it, and is the wrong page."""
    html = html if html is not None else index.mockup_html()
    stem = str(path).replace("\\", "/").rsplit("/", 1)[-1].split(".", 1)[0]
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", stem)
    words = [w for w in re.findall(r"[a-z0-9]+", spaced.lower()) if w not in ("page", "screen", "view")]
    if not words or not html:
        return None
    slug = "-".join(words)
    if re.search(rf"\bid=[\"']screen-{re.escape(slug)}[\"']", html, re.IGNORECASE):
        return " ".join(w.capitalize() for w in words)
    return None


def mockup_for_screen(index: PackIndex, screen: str, html: str | None = None) -> str:
    return clip(_compact_html(index.screen_section(screen, html)), BUDGET["mockup"])


def style_guide_section(index: PackIndex, html: str | None = None) -> str:
    """The mockup's own design-system section (its buttons, badges, cards, fields), if it has one
    — what a shared UI component is built from."""
    html = html if html is not None else index.mockup_html()
    m = re.search(r"<section\b[^>]*\bid=[\"']screen-([\w-]*(?:style|guide|design-system|component)[\w-]*)[\"']",
                  html or "", re.IGNORECASE)
    return clip(_compact_html(index.screen_section(m.group(1).replace("-", " "), html)), BUDGET["style_guide"]) if m else ""


_VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}


def _element_at(html: str, start: int, tag: str) -> str:
    if tag.lower() in _VOID_TAGS:
        end = html.find(">", start)
        return html[start: end + 1] if end >= 0 else html[start:]
    depth = 0
    for m in re.finditer(rf"<(/?){tag}\b[^>]*>", html[start:], re.IGNORECASE):
        depth += -1 if m.group(1) else 1
        if depth == 0:
            return html[start: start + m.end()]
    return html[start:]


def component_fragment(html: str, path: str) -> str:
    """The first mockup element whose class / id names this component (``VehicleCard.tsx`` ->
    ``class="... vehicle-card ..."``), so a component is styled like its design."""
    if not html:
        return ""
    stem = str(path).replace("\\", "/").rsplit("/", 1)[-1].split(".", 1)[0]
    kebab = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "-", stem).lower().replace("_", "-")
    if len(kebab) < 4:
        return ""
    m = re.search(rf"<(\w+)\b[^>]*\b(?:class|id|data-component)=[\"'][^\"']*\b{re.escape(kebab)}\b[^\"']*[\"']",
                  html, re.IGNORECASE)
    return clip(_compact_html(_element_at(html, m.start(), m.group(1))), BUDGET["component"]) if m else ""


def states_for_screen(index: PackIndex, screen: str) -> str:
    return clip(index.state_transitions(screen), BUDGET["states"])


# --------------------------------------------------------------------------- configuration

_CONFIG_STEMS = {"config", "settings", "configuration", "env", "environment", "database", "db", "session",
                 "knexfile", "constants"}


def is_config_file(path: str) -> bool:
    parts = str(path).replace("\\", "/").lower().split("/")
    base = parts[-1]
    stem = base.split(".", 1)[0]
    return (stem in _CONFIG_STEMS or "config" in base or base.startswith(".env")
            or any(p in ("config", "configs", "settings") for p in parts[:-1]))


def env_section(index: PackIndex) -> str:
    """Every declared environment variable with its example, flags and comment."""
    lines = []
    for var in index.env_vars():
        flags = [f for f, on in (("required", var.get("required")), ("secret", var.get("secret"))) if on]
        example = "" if var.get("secret") else str(var.get("example") or "")
        line = f"- {var['key']}" + (f" ({', '.join(flags)})" if flags else "")
        if example:
            line += f" = {example}"
        if var.get("comment"):
            line += f"  # {var['comment']}"
        lines.append(line)
    return clip("\n".join(lines), BUDGET["env"])


def conventions_section(index: PackIndex) -> str:
    return clip(index.conventions_text(), BUDGET["conventions"])


def pack_conventions(design_package: dict[str, Any] | None) -> str:
    """The pack's coding conventions as text, found by content (``SKILL.md``, ``skills.md``,
    ``coding_guidelines.json`` ...) — for agents that hold only the ``design_package`` dict."""
    from app.services.pack_index import index_for

    return conventions_section(index_for(design_package or {})) if design_package else ""
