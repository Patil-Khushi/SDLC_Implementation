"""Code Generation Agent (IMP-001).

Turns ONE work item of a Design Package into real, on-disk source file(s) in the sandbox
workspace, then records what it produced. Single responsibility (CLAUDE.md): it generates and
writes for ONE work item — no gate/compile logic, no git, no routing, no cross-item retries
(the graph loops over items and runs the fixed gate after each).

Rules honored here:
- ``self.llm`` (the gateway) is the ONLY model access — no provider SDK import.
- All writes go through the injected ``Executor`` — never open files or shell out directly.
- Writes only the fields this agent owns: ``generated_code``, ``generation_summary``, and its
  own ``generation_metrics`` keys (files_produced, seconds_per_item). It never touches
  compile_passes/compile_failures/repairs_used, and echoes run_id/attempt unchanged.
- Before calling the LLM, it logs a deterministic ``[plan]`` line to ``generation_summary``
  (target files + requirement/endpoint/table/screen coverage + which design-pack context
  sections were used) — pure logging, not a gate decision, so it stays "one agent = one job".
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from app.agents.base import BaseAgent
from app.graph.state import WorkflowState
from app.integrations.executor import Executor, get_executor
from app.models import GenerationSummary, WorkItem
from app.services.llm_gateway import LLMGateway

logger = logging.getLogger(__name__)


def _project_dir(state: WorkflowState) -> str:
    """Root dir of the generated project within the workspace. Single source of truth shared by
    the code_generator (initial write) and the repair path (fix write) so BOTH agree on where a
    work item's files live — the completeness gate checks ``<project_dir>/<target>``."""
    return state.get("project_id") or state.get("run_id") or "project"


def _project_path(project_dir: str, path: str) -> str:
    """Map an LLM-proposed, project-relative path to its workspace path under ``project_dir``.
    Idempotent: a path the model already prefixed with ``project_dir/`` is not double-prefixed."""
    rel = path.lstrip("/")
    prefix = f"{project_dir}/"
    if rel.startswith(prefix):
        rel = rel[len(prefix):]
    return f"{project_dir}/{rel}"


class CodeGeneratorAgent(BaseAgent):
    name = "code_generator"

    def __init__(self, executor: Executor | None = None, llm: LLMGateway | None = None) -> None:
        super().__init__()
        if llm is not None:  # allow test/DI override of the gateway singleton
            self.llm = llm
        self._executor = executor

    def _resolve_executor(self) -> Executor:
        return self._executor if self._executor is not None else get_executor()

    def execute(self, state: WorkflowState) -> WorkflowState:
        work_item = state.get("current_work_item")
        if work_item is None:
            # Nothing to generate this step. The graph sets current_work_item per iteration
            # (built in a later prompt); with none, this agent is a clean no-op.
            return state

        design_package = state.get("design_package") or {}
        context, sections_used = self._assemble_context(work_item, design_package)
        self._append_plan(state, work_item, sections_used)

        phase, subject = _phase_of(work_item)
        run_id = state.get("run_id") or "-"
        targets = ", ".join(work_item.target_files) or "(none specified)"
        logger.info("[code_generator] run=%s | [PLANNING] %s -> %s", run_id, work_item.id, targets)
        logger.info(
            "[code_generator] run=%s |   [BOILERPLATE] context: %s",
            run_id,
            ", ".join(sections_used) or "(none)",
        )
        logger.info("[code_generator] run=%s | [GENERATING %s] %s", run_id, phase, subject)

        system = self._load_prompt("code_generation")

        started = time.perf_counter()
        files = self._generate_files(work_item, context, system)
        elapsed = round(time.perf_counter() - started, 3)

        if files is None:
            self._record_failure(state, work_item, elapsed)
            state["codegen_ok"] = False  # signals the router to escalate (no gate/commit)
            return state

        written = self._write_files(self._resolve_executor(), state, work_item, files)
        self._record_success(state, work_item, written, elapsed)
        state["codegen_ok"] = True
        state["workflow_status"] = "code_generated"
        return state

    # -- generation -----------------------------------------------------------

    def _generate_files(self, work_item: WorkItem, context: str, system: str) -> list[dict[str, str]] | None:
        """Ask the model for the {"files":[...]} JSON; re-ask once on parse failure."""
        prompt = self._build_prompt(work_item, context)
        parsed, error = self._parse(self.llm.complete(prompt=prompt, system=system))
        if parsed is None:
            retry = (
                f"{prompt}\n\nYour previous reply was not valid JSON matching "
                f'{{"files":[{{"path":...,"content":...}}]}}. Error: {error}. '
                "Reply with STRICT JSON only — no prose, no code fences."
            )
            parsed, error = self._parse(self.llm.complete(prompt=retry, system=system))
        return parsed

    @staticmethod
    def _build_prompt(work_item: WorkItem, context: str) -> str:
        targets = "\n".join(f"- {p}" for p in work_item.target_files) or "- (none specified)"
        # Per-file spec from the design package's structure tree (e.g. "Express app factory:
        # mounts middleware, routers, error handler"). This is what grounds generation of files
        # that aren't tied to any single endpoint/screen — app entrypoints, config, middleware,
        # stores — which otherwise have no context to build from.
        specs = "\n".join(
            f"- {p}: {work_item.file_specs[p]}"
            for p in work_item.target_files
            if work_item.file_specs.get(p)
        )
        specs_block = f"What each file must contain (from the design package):\n{specs}\n\n" if specs else ""
        svg_hint = ""
        if any(p.lower().endswith(".svg") for p in work_item.target_files):
            svg_hint = (
                "For any .svg target, `content` must be a COMPLETE standalone SVG document "
                '(<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" …>…</svg>). Use '
                "currentColor for strokes/fills so it inherits the design tokens, and keep the same "
                "24×24 stroke style as the mockup. Reuse a provided mockup icon's paths verbatim "
                "when the target filename matches an icon shown there.\n\n"
            )
        return (
            f"Work item: {work_item.id}\n"
            f"Covers requirements: {', '.join(work_item.requirement_ids) or '-'}\n"
            f"Endpoints: {', '.join(work_item.endpoints) or '-'}\n"
            f"Tables: {', '.join(work_item.tables) or '-'}\n"
            f"Screens: {', '.join(work_item.screens) or '-'}\n"
            f"Target files (produce ONLY these):\n{targets}\n\n"
            f"{specs_block}"
            f"{svg_hint}"
            f"Context (only the cited slices):\n{context}\n\n"
            'Respond with STRICT JSON only: {"files":[{"path":...,"content":...}],"notes":...}'
        )

    @staticmethod
    def _parse(raw: str) -> tuple[list[dict[str, str]] | None, str]:
        """Parse the model reply into a list of {path, content}. Returns (files, error)."""
        obj = _extract_json(raw)
        if not isinstance(obj, dict):
            return None, "no JSON object found in reply"
        files = obj.get("files")
        if not isinstance(files, list) or not files:
            return None, "'files' must be a non-empty array"
        clean: list[dict[str, str]] = []
        for entry in files:
            if (
                not isinstance(entry, dict)
                or not isinstance(entry.get("path"), str)
                or not isinstance(entry.get("content"), str)
            ):
                return None, "each file needs string 'path' and 'content'"
            clean.append({"path": entry["path"], "content": entry["content"]})
        return clean, ""

    # -- writing + recording --------------------------------------------------

    def _write_files(
        self, executor: Executor, state: WorkflowState, work_item: WorkItem, files: list[dict[str, str]]
    ) -> list[str]:
        project_dir = _project_dir(state)
        generated = list(state.get("generated_code", []))
        written: list[str] = []
        for entry in files:
            path = _project_path(project_dir, entry["path"])
            executor.write_file(path, entry["content"])
            written.append(path)
            generated.append(path)
        state["generated_code"] = generated
        return written

    def _record_success(
        self, state: WorkflowState, work_item: WorkItem, written: list[str], seconds: float
    ) -> None:
        # Build the per-item summary (compile_passed stays None — the gate fills it later).
        summary = GenerationSummary(work_item_id=work_item.id, files_produced=written)
        line = (
            f"[code_generator] {summary.work_item_id}: {len(summary.files_produced)} file(s) "
            f"[{', '.join(summary.files_produced)}] | "
            f"reqs={','.join(work_item.requirement_ids) or '-'} "
            f"endpoints={','.join(work_item.endpoints) or '-'} "
            f"tables={','.join(work_item.tables) or '-'} "
            f"screens={','.join(work_item.screens) or '-'}"
        )
        self._append_summary(state, line)
        self._bump_metrics(state, work_item.id, files=len(written), seconds=seconds)
        logger.info(
            "[code_generator] run=%s | [DONE] %s - %d file(s) in %.3fs: %s",
            state.get("run_id") or "-",
            work_item.id,
            len(written),
            seconds,
            ", ".join(written) or "(none)",
        )

    def _record_failure(self, state: WorkflowState, work_item: WorkItem, seconds: float) -> None:
        # No files written, no partial state; record the failure and its timing only.
        self._append_summary(
            state, f"[code_generator] {work_item.id}: FAILED — model did not return valid JSON (0 files)"
        )
        self._bump_metrics(state, work_item.id, files=0, seconds=seconds)
        logger.warning(
            "[code_generator] run=%s | [FAILED] %s - model did not return valid JSON (0 files) after %.3fs",
            state.get("run_id") or "-",
            work_item.id,
            seconds,
        )

    @staticmethod
    def _append_summary(state: WorkflowState, line: str) -> None:
        state["generation_summary"] = (state.get("generation_summary") or "") + line + "\n"

    @staticmethod
    def _bump_metrics(state: WorkflowState, work_item_id: str, *, files: int, seconds: float) -> None:
        # Own only files_produced + seconds_per_item. compile_passes/failures/repairs_used are
        # the gate/repair nodes' fields — untouched here.
        metrics: dict[str, Any] = dict(state.get("generation_metrics") or {})
        metrics["files_produced"] = int(metrics.get("files_produced", 0)) + files
        per_item: dict[str, float] = dict(metrics.get("seconds_per_item") or {})
        per_item[work_item_id] = seconds
        metrics["seconds_per_item"] = per_item
        state["generation_metrics"] = metrics

    # -- context assembly (tight slices only, not whole files) ----------------

    def _assemble_context(self, work_item: WorkItem, design_package: dict[str, Any]) -> tuple[str, list[str]]:
        """Return (joined context text, names of the sections that were actually populated).

        The names feed the ``[plan]`` summary line so a human can see which design-pack slices
        this item's generation was grounded in, before the LLM is even called.
        """
        sections: list[tuple[str, str]] = []

        skill = _artifact_text(design_package, "SKILL.md", "style-guide/SKILL.md")
        if skill:
            sections.append(("Conventions", "## Conventions (style-guide)\n" + skill.strip()))

        if work_item.endpoints or work_item.tables:  # backend
            paths = _openapi_slice(_artifact(design_package, "openapi.yaml", "openapi.json"), work_item.endpoints)
            if paths:
                sections.append(("API", "## API — cited OpenAPI paths\n" + paths))
            tables = _schema_slice(_artifact_text(design_package, "schema.sql"), work_item.tables)
            if tables:
                sections.append(("DB", "## DB — cited tables\n" + tables))

        if work_item.screens:  # frontend
            # Accept both the canonical names and the design-narrative variants some packs ship
            # (e.g. tic-tac-toe's design-tokens.json / functional-html-mockup.html / route-list.md).
            routes = _routes_slice(
                _artifact(design_package, "routes.json", "route-list.md", "routes.md"),
                work_item.screens,
            )
            if routes:
                sections.append(("Routes", "## Routes — cited\n" + routes))
            tokens = _artifact(design_package, "tokens.json", "design-tokens.json")
            if tokens is not None:
                sections.append(("Design tokens", "## Design tokens\n" + _as_text(tokens)))
            mockup = _mockup_slice(
                _artifact_text(design_package, "mockup.html", "functional-html-mockup.html"),
                work_item.screens,
            )
            if mockup:
                sections.append(("Mockup", "## Mockup — cited components\n" + mockup))

        is_asset_item = any(p.lower().endswith(".svg") for p in work_item.target_files)
        if is_asset_item:  # asset items have no screens, so the screen-keyed frontend context above is skipped
            tokens = _artifact(design_package, "tokens.json", "design-tokens.json")
            if tokens is not None:
                sections.append(("Design tokens", "## Design tokens\n" + _as_text(tokens)))
            svgs = _all_svgs(_artifact_text(design_package, "mockup.html", "functional-html-mockup.html"))
            if svgs:
                sections.append((
                    "Mockup SVGs",
                    "## SVG icons provided by the mockup (reuse verbatim where an icon matches the target filename)\n" + svgs,
                ))

        if work_item.id.startswith("frontend") and not is_asset_item:
            assets = _available_assets(design_package)
            if assets:
                sections.append((
                    "Assets",
                    "## Available asset files (import these EXACT paths; do NOT invent asset names)\n" + assets,
                ))

        rules = _validation_slice(
            _artifact(design_package, "validation-rules.json", "validation-rules.md"),
            [*work_item.endpoints, *work_item.screens],
        )
        if rules:
            sections.append(("Validation rules", "## Validation rules — COPY MESSAGES VERBATIM\n" + rules))

        text = "\n\n".join(body for _, body in sections) if sections else "(no design-pack context found for this item)"
        return text, [name for name, _ in sections]

    def _append_plan(self, state: WorkflowState, work_item: WorkItem, sections_used: list[str]) -> None:
        """Log what this item is about to produce and why, BEFORE calling the LLM."""
        targets = ", ".join(work_item.target_files) or "(none specified)"
        line = (
            f"[plan] {work_item.id}: will produce {targets} | "
            f"reqs={','.join(work_item.requirement_ids) or '-'} "
            f"endpoints={','.join(work_item.endpoints) or '-'} "
            f"tables={','.join(work_item.tables) or '-'} "
            f"screens={','.join(work_item.screens) or '-'} | "
            f"context={','.join(sections_used) or '-'}"
        )
        self._append_summary(state, line)


# --------------------------------------------------------------------------- helpers


def _phase_of(work_item: WorkItem) -> tuple[str, str]:
    """Classify a work item into a human-readable (phase, subject) for terminal logs.

    Pure logging aid — derived only from the item's own fields, no gate/routing decision.
    e.g. a screen "login" → ("FRONTEND", "Login page"); tables → ("BACKEND · DATABASE", ...).
    """
    if work_item.screens:
        pretty = ", ".join(f"{s.replace('-', ' ').replace('_', ' ').title()} page" for s in work_item.screens)
        return "FRONTEND", pretty or "screen"
    if work_item.tables:
        return "BACKEND/DATABASE", "tables " + ", ".join(work_item.tables)
    if work_item.endpoints:
        return "BACKEND/API", "endpoints " + ", ".join(work_item.endpoints)
    return "CODE", ", ".join(work_item.target_files) or work_item.id


def _extract_json(text: str) -> Any:
    """Best-effort JSON object extraction from a model reply.

    Tolerant of the ways a model wraps a big ``{"files":[...]}`` payload: a code fence anywhere
    (```json … ```), a prose preamble/postamble, and — crucially — **unescaped control characters
    inside string values** (raw newlines/tabs in generated source). ``strict=False`` lets
    ``json.loads`` accept those literal control chars instead of rejecting the whole reply, which
    is the most common reason a code-carrying reply otherwise "has no JSON object".
    """
    stripped = text.strip()

    # A fenced block anywhere wins (```json … ``` or ``` … ```); fall back to the whole reply.
    candidates: list[str] = []
    fence = re.search(r"```[a-zA-Z0-9]*\n?(.*?)```", stripped, re.DOTALL)
    if fence:
        candidates.append(fence.group(1).strip())
    candidates.append(stripped)

    for cand in candidates:
        try:
            return json.loads(cand, strict=False)  # strict=False: allow raw \n/\t in string values
        except (ValueError, TypeError):
            pass
        start, end = cand.find("{"), cand.rfind("}")  # trim prose around the object
        if start != -1 and end > start:
            try:
                return json.loads(cand[start : end + 1], strict=False)
            except (ValueError, TypeError):
                pass
    return None


def _all_svgs(mockup_html: str) -> str:
    """Every distinct inline ``<svg>…</svg>`` block from the mockup, so an asset item can reuse the
    icons the design already provides instead of inventing new ones. Capped to keep the prompt bounded."""
    if not mockup_html:
        return ""
    blocks = re.findall(r"<svg\b[^>]*>.*?</svg>", mockup_html, re.DOTALL | re.IGNORECASE)
    seen: set[str] = set()
    uniq: list[str] = []
    for block in blocks:
        key = re.sub(r"\s+", " ", block).strip()
        if key not in seen:
            seen.add(key)
            uniq.append(block.strip())
    return "\n".join(uniq[:40])


def _available_assets(design_package: dict[str, Any]) -> str:
    """The exact ``@/assets/…`` import paths the planner synthesized, so components import real files.

    Names come from the SAME helper the planner uses (single source of truth), derived from the
    frontend structure tree — whether it's wrapped in a ``tree`` key or is the tree directly.
    """
    from app.services.plan_builder import _asset_leaves

    struct = _artifact(design_package, "frontend-structure.json", "frontend_structure.json", "frontend-structure")
    if not isinstance(struct, dict):
        return ""
    tree = struct.get("tree", struct)
    lines: list[str] = []
    for path, _ in _asset_leaves(tree):
        idx = path.find("src/")
        imp = "@/" + path[idx + len("src/") :] if idx != -1 else path
        lines.append(f"- {imp}")
    return "\n".join(lines)


def _artifact(design_package: dict[str, Any], *names: str) -> Any:
    """Return the first present artifact among ``names`` (case-insensitive)."""
    lowered = {k.lower(): v for k, v in design_package.items()}
    for name in names:
        if name in design_package:
            return design_package[name]
        if name.lower() in lowered:
            return lowered[name.lower()]
    return None


def _artifact_text(design_package: dict[str, Any], *names: str) -> str:
    value = _artifact(design_package, *names)
    return value if isinstance(value, str) else ("" if value is None else _as_text(value))


def _as_text(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, indent=2, sort_keys=True)


def _openapi_slice(openapi: Any, endpoints: list[str]) -> str:
    if not endpoints:
        return ""
    if isinstance(openapi, dict) and isinstance(openapi.get("paths"), dict):
        picked: dict[str, Any] = {}
        for endpoint in endpoints:
            method, _, path = endpoint.partition(" ")
            path = path or method
            item = openapi["paths"].get(path)
            if isinstance(item, dict):
                sub = item.get(method.lower())
                picked.setdefault(path, {})[method.lower()] = sub if sub is not None else item
        if picked:
            return json.dumps(picked, indent=2, sort_keys=True)
    if isinstance(openapi, str):
        wanted = {e.partition(" ")[2] or e for e in endpoints}
        lines = [ln for ln in openapi.splitlines() if any(w and w in ln for w in wanted)]
        return "\n".join(lines)
    return ""


def _schema_slice(schema_sql: str, tables: list[str]) -> str:
    if not schema_sql or not tables:
        return ""
    blocks: list[str] = []
    for table in tables:
        match = re.search(
            rf"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[`\"]?{re.escape(table)}[`\"]?\b.*?;",
            schema_sql,
            re.IGNORECASE | re.DOTALL,
        )
        if match:
            blocks.append(match.group(0).strip())
    return "\n\n".join(blocks)


def _routes_slice(routes: Any, screens: list[str]) -> str:
    if routes is None or not screens:
        return ""
    if isinstance(routes, dict):
        picked = {k: v for k, v in routes.items() if any(s.lower() in str(k).lower() for s in screens)}
        return json.dumps(picked, indent=2, sort_keys=True) if picked else ""
    if isinstance(routes, list):
        picked_list = [r for r in routes if any(s.lower() in json.dumps(r).lower() for s in screens)]
        return json.dumps(picked_list, indent=2, sort_keys=True) if picked_list else ""
    if isinstance(routes, str):  # markdown/plain routes (e.g. route-list.md) — include as-is
        return routes
    return ""


def _mockup_slice(mockup_html: str, screens: list[str]) -> str:
    if not mockup_html or not screens:
        return ""
    lines = mockup_html.splitlines()
    # A small mockup is typically a single-screen app (e.g. tic-tac-toe): slicing it by the literal
    # screen name would drop the very layout the generator needs, so include it whole. Large,
    # multi-screen mockups (e.g. ecommerce) stay sliced to just the cited screens.
    if len(lines) <= 200:
        return mockup_html.strip()
    return "\n".join(ln for ln in lines if any(s.lower() in ln.lower() for s in screens))


def _validation_slice(rules: Any, keys: list[str]) -> str:
    if rules is None or not keys:
        return ""
    if isinstance(rules, dict):
        picked = {k: v for k, v in rules.items() if any(key.lower() in str(k).lower() for key in keys)}
        return json.dumps(picked, indent=2, sort_keys=True) if picked else ""
    if isinstance(rules, str):
        return rules
    return ""
