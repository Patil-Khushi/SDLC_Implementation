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
from app.services.naming_contract import build_naming_contract_from_package

logger = logging.getLogger(__name__)

#: Cap on how many already-generated paths are listed in a single item's context (keeps the prompt
#: bounded on large plans; the list is a discovery aid, not an exhaustive manifest).
_MANIFEST_CAP = 200

#: Char budget for the bodies of an item's already-produced files that get fed into each subsequent
#: per-file prompt (their paths are always listed; only the bodies are capped) — keeps the prompt
#: bounded when an item's earlier files are large, without dropping the cross-file context entirely.
_SIBLING_CHARS_CAP = 60_000


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
        produced = self._already_generated(state)
        context, sections_used = self._assemble_context(work_item, design_package, produced)
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
        """Produce the item's files. A multi-file item is generated ONE FILE PER CALL so its output
        can't grow with the file count and truncate at the max_tokens cap — the whole-item call's
        failure mode was ``stop=max_tokens`` → half a JSON object → "no JSON object found in reply".
        Items with a single known target — or none, where the model chooses the files — keep the
        one-shot call, which for a single file already can't truncate on *combining* files."""
        targets = [p.strip() for p in work_item.target_files if p and p.strip()]
        if len(targets) >= 2:
            return self._generate_per_file(work_item, context, system, targets)
        return self._generate_whole_item(work_item, context, system)

    def _generate_whole_item(self, work_item: WorkItem, context: str, system: str) -> list[dict[str, str]] | None:
        """One call returning EVERY file for the item; re-ask once on parse failure. Used for the
        single/zero-target items that :meth:`_generate_files` keeps on the one-shot path."""
        prompt = self._build_prompt(work_item, context)
        raw = self.llm.complete(prompt=prompt, system=system)
        parsed, error = self._parse(raw)
        if parsed is None:
            raw = self.llm.complete(prompt=self._retry_instruction(prompt, error), system=system)
            parsed, error = self._parse(raw)
        if parsed is None:
            # Don't discard the evidence: without this, a deterministic parse failure (same item,
            # every run) is undiagnosable — the raw reply is the only artifact that says why.
            logger.error(
                "[code_generator] %s: unparseable reply (%s). First 1500 chars:\n%s\n--- last 500 chars:\n%s",
                work_item.id, error, raw[:1500], raw[-500:],
            )
        return parsed

    def _generate_per_file(
        self, work_item: WorkItem, context: str, system: str, targets: list[str]
    ) -> list[dict[str, str]] | None:
        """Generate a multi-file item ONE FILE PER CALL. Each call emits a single file, so output
        stays well under the max_tokens cap that truncated the whole-item reply. Every file produced
        is fed into the prompts that follow (``siblings``) so a later file (e.g. the controller)
        imports the exact symbols an earlier one (the service/validator) actually exported. A file
        that still won't parse after a retry is skipped and logged rather than sinking the whole
        item — the completeness gate/repair loop then fills the gap. Returns the files produced, or
        ``None`` only if EVERY file failed (so the caller records the item as failed, as before)."""
        produced: list[dict[str, str]] = []
        siblings: dict[str, str] = {}
        for index, path in enumerate(targets, start=1):
            logger.info(
                "[code_generator] %s: [FILE %d/%d] generating %s", work_item.id, index, len(targets), path
            )
            entry = self._generate_one_file(
                work_item, context, system, path, work_item.file_specs.get(path, ""), targets, siblings
            )
            if entry is None:
                logger.warning(
                    "[code_generator] %s: file %r not produced — skipping (gate/repair will catch it)",
                    work_item.id, path,
                )
                continue
            siblings[entry["path"]] = entry["content"]  # visible to the files generated after it
            produced.append(entry)
        if not produced:
            logger.error(
                "[code_generator] %s: per-file generation produced 0 of %d planned files",
                work_item.id, len(targets),
            )
            return None
        return produced

    def _generate_one_file(
        self, work_item: WorkItem, context: str, system: str, path: str, spec: str,
        targets: list[str], siblings: dict[str, str],
    ) -> dict[str, str] | None:
        """Generate EXACTLY one file; re-ask once on parse failure. Returns ``{path, content}`` with
        the path pinned to the REQUESTED one (the model may re-case or rename it), or ``None`` if it
        still won't parse after the retry."""
        prompt = self._build_file_prompt(work_item, context, path, spec, targets, siblings)
        raw = self.llm.complete(prompt=prompt, system=system)
        parsed, error = self._parse(raw)
        if parsed is None:
            raw = self.llm.complete(prompt=self._retry_instruction(prompt, error), system=system)
            parsed, error = self._parse(raw)
        if parsed is None:
            logger.error(
                "[code_generator] %s: file %r unparseable (%s). First 1500 chars:\n%s\n--- last 500 chars:\n%s",
                work_item.id, path, error, raw[:1500], raw[-500:],
            )
            return None
        # Pin the requested path onto the returned content. A normalized path match wins outright.
        norm = _normalize_path(path)
        base = norm.rsplit("/", 1)[-1].lower()
        match = next((f for f in parsed if _normalize_path(f["path"]) == norm), None)
        if match is None:
            # Otherwise tolerate a COSMETIC path difference (dropped directory, re-cased, ./-prefix)
            # by accepting a lone file whose basename still matches. A DIFFERENT basename means the
            # model produced some OTHER file — relabeling it would silently write the wrong content
            # under this path (a bug no error surfaces), so reject it and let the gate/repair supply
            # the real file instead. The target path here is authoritative (it comes from the plan),
            # so a mismatch is a real signal, not noise to paper over.
            base_matches = [
                f for f in parsed if _normalize_path(f["path"]).rsplit("/", 1)[-1].lower() == base
            ]
            if len(base_matches) == 1:
                match = base_matches[0]
        if match is None:
            logger.warning(
                "[code_generator] %s: reply for %r held %d file(s), none matching by path (%s) — skipping",
                work_item.id, path, len(parsed), [f["path"] for f in parsed],
            )
            return None
        return {"path": path, "content": match["content"]}

    @staticmethod
    def _retry_instruction(prompt: str, error: str) -> str:
        """The re-ask appended after an unparseable reply — shared by the whole-item and per-file
        paths so both nudge the model identically (strict JSON, escaped backslashes/quotes)."""
        return (
            f"{prompt}\n\nYour previous reply was not valid JSON matching "
            f'{{"files":[{{"path":...,"content":...}}]}}. Error: {error}. '
            "Reply with STRICT JSON only — no prose, no code fences. Inside string values, "
            "escape every backslash as \\\\ (regex patterns like \\. or \\d are the usual "
            "culprits) and every double quote as \\\"."
        )

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
    def _build_file_prompt(
        work_item: WorkItem, context: str, path: str, spec: str,
        targets: list[str], siblings: dict[str, str],
    ) -> str:
        """Prompt for ONE file of a multi-file item. Same grounding as the whole-item prompt (item
        metadata + the shared design-pack context), plus: the full list of planned siblings so the
        model knows the module's shape, and the CONTENT of siblings already produced this item so a
        later file imports the exact symbols an earlier one exported. Output is a single file, so it
        can't hit the max_tokens cap and truncate — the failure this whole path exists to prevent."""
        planned = "\n".join(f"- {p}" for p in targets)
        spec_block = f"What THIS file must contain (from the design package):\n- {path}: {spec}\n\n" if spec else ""
        svg_hint = ""
        if path.lower().endswith(".svg"):
            svg_hint = (
                "This is an .svg target: `content` must be a COMPLETE standalone SVG document "
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
            f"All files planned for this work item (stay consistent with these siblings):\n{planned}\n\n"
            f"{spec_block}"
            f"{svg_hint}"
            f"{_siblings_view(siblings)}"
            f"Context (only the cited slices):\n{context}\n\n"
            f"Generate EXACTLY ONE file now: {path}\n"
            'Respond with STRICT JSON containing ONLY that one file: '
            '{"files":[{"path":...,"content":...}]}. No other files, no prose, no code fences.'
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

    def _already_generated(self, state: WorkflowState) -> list[str]:
        """Project-relative paths already written this run (scaffold + prior work items).

        ``generated_code`` holds workspace paths prefixed with the project dir; strip that prefix so
        the manifest lists paths as a sibling module would import them.
        """
        project_dir = _project_dir(state)
        prefix = f"{project_dir}/"
        out: list[str] = []
        for path in state.get("generated_code") or []:
            rel = path[len(prefix):] if path.startswith(prefix) else path
            if rel and rel not in out:
                out.append(rel)
        return out

    def _assemble_context(
        self,
        work_item: WorkItem,
        design_package: dict[str, Any],
        produced: list[str] | None = None,
    ) -> tuple[str, list[str]]:
        """Return (joined context text, names of the sections that were actually populated).

        The names feed the ``[plan]`` summary line so a human can see which design-pack slices
        this item's generation was grounded in, before the LLM is even called.
        """
        sections: list[tuple[str, str]] = []

        # Authoritative naming contract FIRST (see app/services/naming_contract.py). Each work item
        # is generated in its own isolated LLM call, so without a single must-match block every file
        # re-derives entity/field/endpoint identifiers independently and the pieces don't line up
        # (a router importing a symbol the schema never exported, a client calling an unmounted
        # path). Prepending it to every item's context binds them all to the same names. Empty when
        # the pack has no parseable schema/API mapping — then behaviour is exactly as before.
        contract = build_naming_contract_from_package(design_package)
        if contract:
            sections.append(("Naming contract", contract))

        # Manifest of files ALREADY generated this run (scaffold + earlier items). Because items are
        # generated in isolation, an entry point / router / service otherwise can't know what its
        # siblings actually produced, so it invents module names/paths that don't resolve (the
        # router-imports-a-file-that-doesn't-exist and phantom-shared-module bugs). Listing the real
        # paths lets each item import what exists instead of guessing.
        if produced:
            listing = "\n".join(f"- {p}" for p in produced[:_MANIFEST_CAP])
            if len(produced) > _MANIFEST_CAP:
                listing += f"\n- ... (+{len(produced) - _MANIFEST_CAP} more)"
            sections.append((
                "Existing files",
                "## Files already generated in this project — import from these EXACT paths; do NOT "
                "invent new module names for something already produced here\n" + listing,
            ))

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


def _normalize_path(path: str) -> str:
    """Normalize a model-echoed path for comparison: strip surrounding space, a leading ``./`` and
    ``/``, so a rename/re-case/prefix mismatch doesn't hide that a returned file IS the one asked
    for (see :meth:`CodeGeneratorAgent._generate_one_file`)."""
    path = path.strip()
    while path.startswith("./"):
        path = path[2:]
    return path.lstrip("/")


def _siblings_view(siblings: dict[str, str]) -> str:
    """Compact view of the files already generated for THIS work item, injected into each subsequent
    per-file prompt so later files reference earlier ones by their real paths/symbols instead of
    re-inventing them. Bounded by :data:`_SIBLING_CHARS_CAP` so a big early file can't blow up the
    prompt for every file that follows — an over-budget body is elided but its path still listed."""
    if not siblings:
        return ""
    parts: list[str] = []
    budget = _SIBLING_CHARS_CAP
    for path, body in siblings.items():
        if len(body) > budget:
            parts.append(f"### {path}\n(content omitted — too large; import it by this exact path)")
            continue
        parts.append(f"### {path}\n```\n{body}\n```")
        budget -= len(body)
    return (
        "Files already generated for THIS work item — import from / stay consistent with these "
        "EXACT paths and the symbols they define:\n" + "\n\n".join(parts) + "\n\n"
    )


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


# Models writing regex-heavy content (ESLint configs, knexfile patterns, README markdown)
# routinely emit "\." or "\d" inside string values; json.loads rejects those even with
# strict=False (that flag only permits raw control characters), so one bad escape sinks an
# otherwise perfect reply. Repair by MATCHING AND CONSUMING each valid escape token (left
# intact) so the regex engine can never re-examine the second byte of a valid pair — a bare
# lookahead ("\\(?!...)") gets this wrong: on a valid \\d it skips the first backslash, then
# matches the second one alone and doubles it, corrupting \\d into \\\d (PR #15 review).
# Any backslash NOT consumed as part of a valid token falls through to the final "\\"
# alternative and is doubled into a literal backslash.
_ESCAPE_TOKEN = re.compile(r'\\u[0-9a-fA-F]{4}|\\[\\"/bfnrt]|\\')


def _repair_invalid_escapes(text: str) -> str:
    """Double every backslash that does not start a valid JSON escape; leave valid ones alone."""
    return _ESCAPE_TOKEN.sub(lambda m: m.group(0) if len(m.group(0)) > 1 else r"\\", text)


# A second malformation seen live on the SAME work item that motivated the escape repair above
# (resources pack, ``backend-root-2``, reproduced on demand): for some file entries the model wrote
#     {"path":".env.example","content"># ---- Application ----\n...
# i.e. a literal ``>`` where ``:"`` belongs, while the value body itself was correctly escaped AND
# correctly closed. It reproduced on the first ask and on the retry, so no amount of re-asking
# recovers it. Restricted to the three schema keys so a ``"...">`` sequence inside a string value
# cannot be rewritten, and only ever applied on the salvage path (after normal parsing failed).
_MISSING_COLON_QUOTE = re.compile(r'"(path|content|notes)">')


def _repair_missing_colon_quote(text: str) -> str:
    """Rewrite ``"content">…`` to ``"content":"…`` (likewise ``path``/``notes``)."""
    return _MISSING_COLON_QUOTE.sub(r'"\1":"', text)


def _extract_json(text: str) -> Any:
    """Best-effort JSON object extraction from a model reply.

    Tolerant of the ways a model wraps a big ``{"files":[...]}`` payload: a code fence anywhere
    (```json … ```), a prose preamble/postamble, **unescaped control characters** inside string
    values (raw newlines/tabs in generated source — ``strict=False`` accepts those), and
    **invalid backslash escapes** (``"\\."`` from a regex in an ESLint config — repaired to
    ``"\\\\."`` as a last resort; see ``_repair_invalid_escapes``), and a **missing ``:"`` after a
    schema key** (``"content">…``; see ``_repair_missing_colon_quote``).
    """
    stripped = text.strip()

    candidates: list[str] = []
    # A reply that IS the JSON object wins outright — never let a ``` inside a string value
    # (a README code fence, say) trick the fence regex into extracting a garbage fragment.
    if stripped.startswith("{"):
        candidates.append(stripped)
    fence = re.search(r"```[a-zA-Z0-9]*\n?(.*?)```", stripped, re.DOTALL)
    if fence:
        candidates.append(fence.group(1).strip())
    if stripped not in candidates:
        candidates.append(stripped)

    for cand in candidates:
        trimmed = None
        start, end = cand.find("{"), cand.rfind("}")  # trim prose around the object
        if start != -1 and end > start:
            trimmed = cand[start : end + 1]
        for attempt in (cand, trimmed):
            if attempt is None:
                continue
            try:
                return json.loads(attempt, strict=False)  # strict=False: allow raw \n/\t in strings
            except (ValueError, TypeError):
                pass
            # Salvage passes, cheapest first. The two repairs compose because both malformations
            # have been observed in the same reply — fixing only one still leaves it unparseable.
            for salvaged in (
                _repair_invalid_escapes(attempt),
                _repair_missing_colon_quote(attempt),
                _repair_invalid_escapes(_repair_missing_colon_quote(attempt)),
            ):
                try:
                    return json.loads(salvaged, strict=False)
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
