"""Code Review Agent - deterministic findings + LLM interpretation -> engineering report.

Runs ONCE at the end of the pipeline. **Static, read-only, advisory** (Testing executes;
Refactoring modifies). Accuracy comes from separating two things:

* **Verified findings** - produced by the deterministic tools (Ruff, ESLint, SonarQube) and merged
  by ``services/finding_aggregator`` in pure Python. Reproducible, no LLM, confidence "Very High".
* **Engineering observations** - the LLM's higher-level judgement (design, risk, prioritization),
  clearly separated and carrying the LLM's own (lower) confidence.

Pipeline:  clone -> Ruff/ESLint (+ sonar-scanner) -> Finding Aggregator -> LLM interprets ->
render an 8-section Markdown report -> save to ``reports/``.

Owns only: ``review_report`` (+ ``review_report_path``) and its ``workflow_status`` stamp.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.agents.base import BaseAgent
from app.config.settings import get_settings
from app.graph.state import WorkflowState
from app.integrations.review_sandbox import ReviewSandbox, get_review_sandbox
from app.integrations.sonarqube import SonarMeasures, SonarQubeClient, SonarResult, get_sonarqube_client
from app.services import finding_aggregator as agg
from app.services.llm_gateway import LLMGateway

logger = logging.getLogger(__name__)

_MAX_FILES = 25
_MAX_FILE_CHARS = 20_000
_MAX_STRUCTURE = 60
_PY_EXTS = (".py",)
_JS_EXTS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")
_SOURCE_EXTS = _PY_EXTS + _JS_EXTS
_VERDICT = ("approve", "changes_requested")


class CodeReviewAgent(BaseAgent):
    name = "code_review"

    def __init__(
        self,
        sandbox_factory: Callable[[], ReviewSandbox] | None = None,
        llm: LLMGateway | None = None,
        sonarqube: SonarQubeClient | None = None,
    ) -> None:
        super().__init__()
        if llm is not None:
            self.llm = llm
        self._sandbox_factory = sandbox_factory
        self._sonarqube = sonarqube

    def _new_sandbox(self) -> ReviewSandbox:
        return self._sandbox_factory() if self._sandbox_factory else get_review_sandbox()

    def _resolve_sonarqube(self) -> SonarQubeClient:
        return self._sonarqube if self._sonarqube is not None else get_sonarqube_client()

    def execute(self, state: WorkflowState) -> WorkflowState:
        project_id = state.get("project_id") or state.get("run_id") or "project"
        run_id = state.get("run_id", "")
        repo_url = (state.get("repo_url") or "").strip()
        branch = (state.get("branch") or get_settings().working_branch or "").strip()
        style_guide = _artifact_text(state.get("design_package") or {}, "SKILL.md", "style-guide/SKILL.md")

        meta = {"project": project_id, "run_id": run_id, "repo_url": repo_url or "(none)",
                "branch": branch or "(default)", "commit": "-",
                "date": _now_iso(), "languages": "-", "files_reviewed": 0, "raw_findings": 0}
        tools = {"Ruff": "not run", "ESLint": "not run", "SonarQube": "not run"}

        if not repo_url:
            review = _empty_review("No repository URL was provided, so there was nothing to review.")
            report = _render(meta, [], review, SonarResult(error="not run"), "not run", tools,
                             SonarMeasures(error="not run"))
            return self._finish(state, project_id, run_id, report, [])

        logger.info("Code review starting for %s (repo: %s)", project_id, repo_url)
        ruff_raw: list[dict[str, Any]] = []
        eslint_raw: list[dict[str, Any]] = []
        sonar = SonarResult(error="not run")
        measures = SonarMeasures(error="not run")
        verified: list[dict[str, Any]] = []
        code_context, structure, scan_note, clone_error = "(source unavailable)", "", "not run", ""
        try:
            logger.info("[1/6] Starting sandbox + cloning branch '%s' ...", branch or "default")
            with self._new_sandbox() as sb:
                clone = sb.clone(repo_url, ref=branch or None)
                if not clone.ok and branch:
                    # Working branch (e.g. 'dev') doesn't exist -> fall back to the repo's default branch.
                    logger.warning("[1/6] Branch '%s' unavailable; falling back to the default branch", branch)
                    clone = sb.clone(repo_url, ref=None)
                if not clone.ok:
                    clone_error = (clone.stderr or clone.stdout or "clone failed").strip()[:400]
                    logger.warning("[1/6] Clone FAILED: %s", clone_error)
                else:
                    sha = _head_sha(sb)                       # pin the exact reviewed commit (audit + repro)
                    actual_branch = _head_branch(sb) or (branch or "default")
                    meta["branch"] = actual_branch
                    meta["commit"] = sha or "-"
                    state["branch"] = actual_branch
                    state["commit_sha"] = sha
                    files = _safe_list(sb)
                    langs = _detect_languages(files)
                    meta["languages"] = _lang_label(langs)
                    meta["files_reviewed"] = len([p for p in files if p.endswith(_SOURCE_EXTS)])
                    logger.info("[1/6] Cloned '%s' @ %s - %s | %d source file(s)",
                                actual_branch, (sha or "-")[:12], meta["languages"], meta["files_reviewed"])
                    ruff_raw, eslint_raw = self._run_linters(sb, langs, tools)
                    scan_note = self._run_sonar_scan(sb, tools)
                    # Fetch SonarQube issues/measures WHILE the sandbox is still open, so we know
                    # every flagged file+line BEFORE reading any source - not just a guessed subset.
                    sonar = self._run_sonarqube(tools)
                    measures = self._run_sonar_measures()
                    verified = agg.aggregate(ruff_raw, eslint_raw, _sonar_raw(sonar))
                    meta["raw_findings"] = len(verified)     # BEFORE classify/rollup, for the dashboard
                    read_cache: dict[str, str | None] = {}
                    self._fill_evidence(sb, files, verified, read_cache)
                    # Deterministic suppression + severity correction (needs evidence for
                    # hardcoded-secret checks), THEN collapse repeated suppressed findings
                    # (e.g. hundreds of pytest asserts) into one row per rule.
                    agg.classify(verified)
                    verified = agg.rollup_suppressed(verified)
                    logger.info("[4/6] Finding Aggregator: %d raw -> %d after classify/rollup",
                                meta["raw_findings"], len(verified))
                    code_context = self._build_llm_context(sb, files, verified, read_cache)
                    structure = _structure(files)
                    logger.info("[6/6] Tearing down sandbox ...")
        except Exception as exc:  # noqa: BLE001
            logger.exception("review sandbox failed for run %s", run_id)
            clone_error = clone_error or f"review sandbox error: {exc}"

        if clone_error:
            review = _empty_review(f"The repository could not be analyzed: {clone_error}",
                                   verdict="changes_requested")
            report = _render(meta, [], review, SonarResult(error="not run"), scan_note, tools,
                             SonarMeasures(error="not run"))
            return self._finish(state, project_id, run_id, report, [])

        logger.info("[5/6] LLM interpreting findings + code ...")
        review = self._review_llm(verified, meta, code_context, structure, style_guide, measures)
        report = _render(meta, verified, review, sonar, scan_note, tools, measures)
        return self._finish(state, project_id, run_id, report, verified)

    # -- tools (deterministic) ------------------------------------------------

    def _run_linters(self, sb: ReviewSandbox, langs: set[str],
                     tools: dict[str, str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        ruff_raw: list[dict[str, Any]] = []
        eslint_raw: list[dict[str, Any]] = []
        if "python" in langs:
            logger.info("[2/6] Running ruff ...")
            # Native Python parser (no plugins). Broad rule set: style, bugs (B), complexity (C90),
            # naming (N), security (S/bandit), pyupgrade (UP), perf (PERF), simplify (SIM).
            res = sb.run([
                "ruff", "check", "--output-format=json",
                "--select", "E,F,W,B,C90,N,S,UP,PERF,SIM",
                "--config", "lint.mccabe.max-complexity=10", ".",
            ])
            ruff_raw = _parse_ruff(res.stdout)
            tools["Ruff"] = f"{len(ruff_raw)} finding(s)"
            logger.info("[2/6] ruff: %d finding(s)", len(ruff_raw))
        if "js" in langs:
            logger.info("[2/6] Running eslint ...")
            # Use the sandbox's bundled JSX/TS-aware config (never the untrusted repo's config).
            res = sb.run([
                "/opt/eslint/node_modules/.bin/eslint", "--no-config-lookup",
                "--config", "/opt/eslint/eslint.config.mjs", "-f", "json", ".",
            ])
            eslint_raw = _parse_eslint(res.stdout)
            tools["ESLint"] = f"{len(eslint_raw)} finding(s)"
            logger.info("[2/6] eslint: %d finding(s)", len(eslint_raw))
        return ruff_raw, eslint_raw

    def _run_sonar_scan(self, sb: ReviewSandbox, tools: dict[str, str]) -> str:
        settings = get_settings()
        if not settings.sonarqube_enabled or not (settings.sonarqube_url and settings.sonarqube_project_key):
            tools["SonarQube"] = "skipped (disabled)"
            logger.info("[3/6] SonarQube scan skipped (disabled/unconfigured)")
            return "SonarQube scan skipped (disabled/unconfigured)."
        logger.info("[3/6] Running sonar-scanner (upload + wait) ...")
        cmd = ["sonar-scanner", f"-Dsonar.projectKey={settings.sonarqube_project_key}", "-Dsonar.sources=.",
               f"-Dsonar.host.url={settings.sonarqube_scanner_url}", "-Dsonar.qualitygate.wait=true"]
        if settings.sonarqube_token:
            cmd.append(f"-Dsonar.token={settings.sonarqube_token}")  # nosec
        res = sb.run(cmd, timeout=settings.review_sandbox_timeout)
        out = f"{res.stdout or ''}\n{res.stderr or ''}"
        if res.ok:
            logger.info("[3/6] SonarQube scan completed - gate passed")
            return "SonarQube scan completed; quality gate PASSED."
        if "QUALITY GATE STATUS" in out or "ANALYSIS SUCCESSFUL" in out:
            logger.info("[3/6] SonarQube scan completed - gate FAILED (issues uploaded)")
            return "SonarQube scan completed; quality gate FAILED (issues uploaded - see dashboard)."
        logger.warning("[3/6] SonarQube scan did not complete cleanly")
        return f"SonarQube scan did not complete cleanly: {(res.stderr or res.stdout or '')[:300]}"

    def _run_sonarqube(self, tools: dict[str, str]) -> SonarResult:
        logger.info("[4/6] Reading SonarQube issues via API ...")
        try:
            result = self._resolve_sonarqube().fetch_issues()
        except Exception as exc:  # noqa: BLE001
            return SonarResult(error=f"sonarqube unavailable: {exc}")
        if result.ok:
            tools["SonarQube"] = f"{len(result.issues)} issue(s)"
            logger.info("[4/6] SonarQube: %d issue(s)", len(result.issues))
        else:
            logger.info("[4/6] SonarQube read skipped: %s", result.error)
        return result

    def _run_sonar_measures(self) -> SonarMeasures:
        """Deterministic metrics (LOC/complexity/coverage/duplication/tech-debt) from SonarQube."""
        try:
            return self._resolve_sonarqube().fetch_measures()
        except Exception as exc:  # noqa: BLE001
            return SonarMeasures(error=f"sonarqube unavailable: {exc}")

    # -- LLM interpretation ---------------------------------------------------

    def _review_llm(self, verified: list[dict[str, Any]], meta: dict[str, Any], code_context: str,
                    structure: str, style_guide: str, measures: SonarMeasures) -> dict[str, Any]:
        actionable, suppressed = _split(verified)
        system = self._load_prompt("code_review")
        sections: list[str] = []
        if style_guide.strip():
            sections.append("## Style guide (SKILL.md)\n" + style_guide.strip())
        sections.append("## Project structure\n" + (structure or "(unavailable)"))
        if measures.ok and measures.values:
            sections.append("## Metrics (measured by SonarQube - reference only, NEVER recompute or estimate)\n"
                            + "```json\n" + json.dumps(measures.values, indent=2) + "\n```")
        sections.append(
            "## Finding counts (deterministic - report these numbers verbatim, do not recompute)\n"
            f"- Raw tool findings: {meta.get('raw_findings', len(verified))}\n"
            f"- Auto-suppressed as known false-positive patterns (e.g. pytest asserts, safe auth "
            f"constants): {sum(f.get('occurrences', 1) for f in suppressed)}\n"
            f"- Actionable findings requiring attention: {len(actionable)}"
        )
        sections.append(
            "## Actionable findings (from Ruff/ESLint/SonarQube, AFTER deterministic false-positive "
            "filtering - facts, do not repeat as your own, do not re-flag the suppressed items above)\n"
            + "```json\n" + json.dumps(actionable, indent=2) + "\n```"
        )
        sections.append("## Source under review\n" + code_context)
        sections.append("Interpret the above and reply with STRICT JSON "
                        "(executive_summary, verdict, engineering_observations[], recommendations[]).")
        prompt = "\n\n".join(sections)

        parsed, err = _parse_review(self.llm.complete(prompt=prompt, system=system))
        if parsed is None:
            retry = f"{prompt}\n\nYour previous reply was not valid JSON ({err}). Reply with STRICT JSON only."
            parsed, err = _parse_review(self.llm.complete(prompt=retry, system=system))
        if parsed is None:
            return _empty_review(f"Automated interpretation could not be parsed ({err}).",
                                 verdict="changes_requested")
        return parsed

    # -- code / evidence gathering (read-only, targeted) -----------------------

    def _fill_evidence(self, sb: ReviewSandbox, files: list[str],
                       verified: list[dict[str, Any]], cache: dict[str, str | None]) -> None:
        """Attach the exact offending line of code to EVERY finding (not just an alphabetical
        subset) by reading, on demand, exactly the files the findings reference - any tool,
        any file type (Dockerfile included), while the sandbox is still open."""
        for f in verified:
            line = f.get("line")
            path = str(f.get("file", ""))
            if not line or not path or f.get("evidence"):
                continue
            content = self._read_one(sb, files, path, cache)
            if content is None:
                continue
            lines = content.splitlines()
            if 1 <= line <= len(lines):
                f["evidence"] = lines[line - 1].strip()[:200]

    def _build_llm_context(self, sb: ReviewSandbox, files: list[str], verified: list[dict[str, Any]],
                           cache: dict[str, str | None]) -> str:
        """Bounded source context for the LLM - files WITH findings are prioritized first, so the
        model always sees the flagged code even when the repo has more than _MAX_FILES sources."""
        flagged = [f["file"] for f in verified if f.get("file")]
        seen: set[str] = set()
        ordered: list[str] = []
        for path in flagged:
            if path not in seen:
                seen.add(path)
                ordered.append(path)
        for path in files:
            if path.endswith(_SOURCE_EXTS) and path not in seen:
                seen.add(path)
                ordered.append(path)

        blocks: list[str] = []
        for path in ordered[:_MAX_FILES]:
            content = self._read_one(sb, files, path, cache)
            if content is None:
                continue
            if len(content) > _MAX_FILE_CHARS:
                content = content[:_MAX_FILE_CHARS] + "\n... (truncated)"
            blocks.append(f"### {path}\n```\n{content}\n```")
        if not blocks:
            return "(no Python/JS source files found to review)"
        note = f"\n\n(+{len(ordered) - len(blocks)} more)" if len(ordered) > len(blocks) else ""
        return "\n\n".join(blocks) + note

    @staticmethod
    def _read_one(sb: ReviewSandbox, files: list[str], path: str, cache: dict[str, str | None]) -> str | None:
        """Read one file's text, resolving a tool-reported path to the real repo-relative path."""
        if path in cache:
            return cache[path]
        resolved = _resolve_path(files, path)
        try:
            content = sb.read_text(resolved)
        except Exception:  # noqa: BLE001 - missing/unreadable file, not fatal
            content = None
        cache[path] = content
        return content

    # -- persistence ----------------------------------------------------------

    def _finish(self, state: WorkflowState, project_id: str, run_id: str, report: str,
                verified: list[dict[str, Any]]) -> WorkflowState:
        # One folder per run: reports/<project>-<run>/ containing report.md + findings.json
        run_dir = Path(get_settings().reports_dir) / f"{_slug(project_id)}-{_slug(run_id or 'run')}"
        run_dir.mkdir(parents=True, exist_ok=True)

        md_path = run_dir / "report.md"
        md_path.write_text(report, encoding="utf-8")
        # The normalized verified-findings JSON - the machine-readable artifact for the Refactoring
        # agent (it should consume this, not parse the Markdown).
        json_path = run_dir / "findings.json"
        json_path.write_text(json.dumps(verified, indent=2), encoding="utf-8")

        state["review_report"] = report
        state["review_report_path"] = str(md_path)
        state["review_findings_path"] = str(json_path)
        state["workflow_status"] = "code_reviewed"
        logger.info("Report saved to folder: %s", run_dir)
        return state


# --------------------------------------------------------------------------- 8-section renderer


def _render(meta: dict[str, Any], verified: list[dict[str, Any]], review: dict[str, Any],
            sonar: SonarResult, scan_note: str, tools: dict[str, str], measures: SonarMeasures) -> str:
    actionable, suppressed = _split(verified)
    sev = agg.severity_counts(actionable)          # severity breakdown = ACTIONABLE only
    cats = agg.category_counts(actionable)
    verdict = _final_verdict(actionable, review.get("verdict"))
    obs = [o for o in review.get("engineering_observations", []) if isinstance(o, dict)]
    recs = [r for r in review.get("recommendations", []) if isinstance(r, dict)]
    raw_count = meta.get("raw_findings", len(verified))
    suppressed_count = sum(f.get("occurrences", 1) for f in suppressed)

    L: list[str] = []
    a = L.append
    a("# Code Review Report\n")

    # 1. Metadata
    a("## Section 1: Metadata\n")
    a("| Field | Value |")
    a("| --- | --- |")
    a(f"| Project | {meta['project']} |")
    a(f"| Repository | {meta['repo_url']} |")
    a(f"| Branch | {meta.get('branch', '-')} |")
    a(f"| Commit | {(meta.get('commit') or '-')[:12]} |")
    a(f"| Reviewed By | Code Review Agent (automated) |")
    a(f"| Run ID | {meta['run_id'] or '-'} |")
    a(f"| Review Date | {meta['date']} |")
    a(f"| Language(s) | {meta['languages']} |")
    a(f"| Files Reviewed | {meta['files_reviewed']} |")
    a(f"| Tools | Ruff: {tools['Ruff']} \\| ESLint: {tools['ESLint']} \\| SonarQube: {tools['SonarQube']} |")
    a(f"| Verdict | {_verdict_label(verdict)} |\n")

    # 2. Executive Summary
    a("## Section 2: Executive Summary\n")
    a((review.get("executive_summary") or "(no summary)").strip() + "\n")

    # 3. Static Analysis Summary (dashboard)
    a("## Section 3: Static Analysis Summary\n")
    a("**Summary dashboard:**\n")
    a("| Metric | Count |")
    a("| --- | --- |")
    a(f"| Files scanned | {meta['files_reviewed']} |")
    a(f"| Lines of code | {_m(measures.values, 'ncloc') if measures.ok else 'n/a'} |")
    a(f"| Raw tool findings | {raw_count} |")
    a(f"| Auto-suppressed (false positives) | {suppressed_count} |")
    a(f"| **Actionable findings** | **{len(actionable)}** |\n")
    a("**Actionable findings, by severity:**\n")
    a("| Critical | High | Medium | Low | Info |")
    a("| --- | --- | --- | --- | --- |")
    a(f"| {sev['Critical']} | {sev['High']} | {sev['Medium']} | {sev['Low']} | {sev['Info']} |\n")
    a("**Actionable findings, by category:**\n")
    if cats:
        a("| Category | Count |")
        a("| --- | --- |")
        for c, n in sorted(cats.items(), key=lambda kv: -kv[1]):
            a(f"| {c} | {n} |")
        a("")
    else:
        a("_No actionable findings._\n")

    # 4. Static Analysis Findings (4.1 actionable, 4.2 suppressed false positives)
    a("## Section 4: Static Analysis Findings\n")
    a("_A tool detecting a pattern (confidence: Very High) is not the same as that pattern being a "
      "real, actionable problem - those are different questions. Section 4.1 lists findings that "
      "survived deterministic false-positive filtering; 4.2 lists what was filtered out, and why._\n")

    a("### 4.1 Actionable Findings\n")
    if actionable:
        a("| ID | Category | Severity | Sources | Rule(s) | Location | Issue | Evidence (code) | Why / Impact / Fix |")
        a("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
        for f in actionable:
            loc = f"{f['file']}:{f['line']}" if f.get("line") else f["file"]
            detail = "; ".join(f["tool_messages"]) or f["message"]
            evidence = f.get("evidence", "")
            ev_cell = f"`{_esc(evidence)}`" if evidence else "-"
            a(f"| {f['id']} | {f['category']} | {f['severity']} | {', '.join(f['sources'])} "
              f"| {', '.join(f['rule_ids']) or '-'} | `{loc}` | {_esc(detail)} "
              f"| {ev_cell} | {_why_impact_fix(f)} |")
        a("")
    else:
        a("_No actionable findings - nothing survived filtering as a real issue._\n")

    a("### 4.2 Suppressed Findings (Auto-Filtered False Positives)\n")
    a("_Collapsed to one row per rule (repeated instances rolled into a count) - these are NOT "
      "shown as individual findings because each was matched against a known, documented "
      "false-positive pattern (the same patterns real tools solve with `per-file-ignores`/`nosec`)._\n")
    if suppressed:
        a("| Rule(s) | Category | Occurrences | Sample Location | Reason Suppressed |")
        a("| --- | --- | --- | --- | --- |")
        for f in suppressed:
            loc = f"{f['file']}:{f['line']}" if f.get("line") else f["file"]
            extra = len(f.get("additional_locations", []))
            loc_cell = f"`{loc}`" + (f" (+{extra} more)" if extra else "")
            a(f"| {', '.join(f['rule_ids']) or '-'} | {f['category']} | {f.get('occurrences', 1)} "
              f"| {loc_cell} | {_esc(f.get('suppressed_reason', ''))} |")
        a("")
    else:
        a("_Nothing was suppressed._\n")

    # 5. Engineering Observations
    a("## Section 5: Engineering Observations\n")
    a("_LLM judgement beyond what tools detect (design, risk, testability). Confidence is the "
      "model's own estimate - treat as advisory._\n")
    if obs:
        a("| Area | Observation | Severity | Confidence |")
        a("| --- | --- | --- | --- |")
        for o in obs:
            a(f"| {_esc(str(o.get('area', '-')))} | {_esc(str(o.get('observation', '')))} "
              f"| {str(o.get('severity', 'low')).lower()} | {str(o.get('confidence', 'medium')).lower()} |")
        a("")
    else:
        a("_No additional engineering observations._\n")

    # 6. Metrics
    a("## Section 6: Metrics\n")
    a("_Engineering metrics below are **measured by SonarQube** (deterministic) - not estimated by "
      "the LLM. Coverage requires a coverage report (produced by the Testing phase)._\n")
    if measures.ok and measures.values:
        m = measures.values
        a("| Metric | Value | Source |")
        a("| --- | --- | --- |")
        a(f"| Lines of code | {_m(m, 'ncloc')} | SonarQube |")
        a(f"| Cyclomatic complexity | {_m(m, 'complexity')} | SonarQube |")
        a(f"| Cognitive complexity | {_m(m, 'cognitive_complexity')} | SonarQube |")
        a(f"| Test coverage | {_m(m, 'coverage', '%')} | SonarQube |")
        a(f"| Duplicated lines | {_m(m, 'duplicated_lines_density', '%')} | SonarQube |")
        a(f"| Technical debt | {_debt(m)} | SonarQube |")
        a(f"| Bugs | {_m(m, 'bugs')} | SonarQube |")
        a(f"| Vulnerabilities | {_m(m, 'vulnerabilities')} | SonarQube |")
        a(f"| Code smells | {_m(m, 'code_smells')} | SonarQube |")
        a(f"| Security hotspots | {_m(m, 'security_hotspots')} | SonarQube |")
        a("")
    else:
        a(f"_SonarQube metrics unavailable: {measures.error or 'not run'}._\n")
    files_affected = len({f["file"] for f in actionable})
    a("**Actionable findings (from Ruff / ESLint / SonarQube, post-filtering):**\n")
    a(f"- **Total actionable findings:** {len(actionable)}")
    a(f"- **High/Critical:** {sev['Critical'] + sev['High']}  |  **Medium:** {sev['Medium']}  |  **Low/Info:** {sev['Low'] + sev['Info']}")
    a(f"- **Files affected:** {files_affected}")
    a(f"- **SonarQube issues (open):** {('%d' % len(sonar.issues)) if sonar.ok else sonar.error}")
    a(f"- **Scan status:** {scan_note}\n")

    # 7. Recommendations
    a("## Section 7: Recommendations\n")
    a("_Prioritized actions for the Refactoring agent._\n")
    if recs:
        a("| Priority | Action |")
        a("| --- | --- |")
        for r in sorted(recs, key=lambda x: {"high": 0, "medium": 1, "low": 2}.get(str(x.get("priority", "low")).lower(), 3)):
            a(f"| {str(r.get('priority', 'low')).lower()} | {_esc(str(r.get('action', '')))} |")
        a("")
    else:
        a("_No recommendations._\n")

    # 8. Final Verdict
    a("## Section 8: Final Verdict\n")
    a(f"- **Verdict:** {_verdict_label(verdict)}")
    a(f"- **Rationale:** {_verdict_rationale(actionable, sev)}")
    a(f"- **Sign-off:** Pending (automated review - no human sign-off recorded)")
    a("")
    return "\n".join(L)


# --------------------------------------------------------------------------- helpers


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", value) or "run"


def _esc(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ").strip()


def _m(values: dict[str, str], key: str, suffix: str = "") -> str:
    v = values.get(key)
    return f"{v}{suffix}" if v not in (None, "") else "n/a"


def _debt(values: dict[str, str]) -> str:
    """SonarQube sqale_index is technical debt in MINUTES; render human-readable."""
    raw = values.get("sqale_index")
    try:
        minutes = int(raw)
    except (TypeError, ValueError):
        return "n/a"
    if minutes < 60:
        return f"{minutes} min"
    hours, rem = divmod(minutes, 60)
    return f"{hours}h {rem}m" if rem else f"{hours}h"


def _why_impact_fix(f: dict[str, Any]) -> str:
    """Deterministic root-cause/impact/fix from the rule knowledge base; falls back to the tool's
    own message (still accurate, just less rich) when no canned entry exists for this rule."""
    kb = agg.rule_explanation(f.get("rule_ids", []))
    if kb:
        return _esc(f"Why: {kb['why']} Impact: {kb['impact']} Fix: {kb['fix']}")
    fallback = "; ".join(f.get("tool_messages", [])) or f.get("message", "")
    return _esc(f"See tool message: {fallback}") if fallback else "-"


def _split(verified: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split into (actionable, suppressed). Actionable = requires attention; Suppressed = a
    deterministic false-positive pattern (see finding_aggregator.classify)."""
    actionable = [f for f in verified if f.get("status") != "Suppressed"]
    suppressed = [f for f in verified if f.get("status") == "Suppressed"]
    return actionable, suppressed


def _empty_review(summary: str, verdict: str = "approve") -> dict[str, Any]:
    return {"executive_summary": summary, "verdict": verdict,
            "engineering_observations": [], "recommendations": []}


def _verdict_label(v: str) -> str:
    return {"approve": "APPROVE", "changes_requested": "CHANGES REQUESTED"}.get(v, v.upper())


def _final_verdict(actionable: list[dict[str, Any]], llm_verdict: Any) -> str:
    """Driven ONLY by actionable findings - a suppressed false positive must never force a
    CHANGES REQUESTED verdict, no matter its raw (pre-classification) severity."""
    if any(f.get("severity") in ("Critical", "High") for f in actionable):
        return "changes_requested"
    v = str(llm_verdict or "approve").lower()
    return v if v in _VERDICT else "approve"


def _verdict_rationale(actionable: list[dict[str, Any]], sev: dict[str, int]) -> str:
    hi = sev["Critical"] + sev["High"]
    if hi:
        return f"{hi} high/critical actionable finding(s) require changes before proceeding."
    if actionable:
        return f"{len(actionable)} lower-severity actionable finding(s); safe to proceed with recommended cleanups."
    return "No actionable findings; code is clean per the static-analysis tools (after false-positive filtering)."


def _lang_label(langs: set[str]) -> str:
    names = []
    if "python" in langs:
        names.append("Python")
    if "js" in langs:
        names.append("JavaScript/TypeScript")
    return ", ".join(names) or "unknown"


def _safe_list(sb: ReviewSandbox) -> list[str]:
    try:
        return sb.list_files()
    except Exception:  # noqa: BLE001
        return []


def _head_sha(sb: ReviewSandbox) -> str:
    """The exact commit that was cloned - recorded so the review is reproducible/auditable."""
    try:
        res = sb.run(["git", "rev-parse", "HEAD"])
        return (res.stdout or "").strip() if res.ok else ""
    except Exception:  # noqa: BLE001
        return ""


def _head_branch(sb: ReviewSandbox) -> str:
    """The actual branch name that was cloned (empty if detached HEAD)."""
    try:
        res = sb.run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
        name = (res.stdout or "").strip() if res.ok else ""
        return "" if name in ("", "HEAD") else name
    except Exception:  # noqa: BLE001
        return ""


def _detect_languages(files: list[str]) -> set[str]:
    langs: set[str] = set()
    if any(p.endswith(_PY_EXTS) for p in files):
        langs.add("python")
    if any(p.endswith(_JS_EXTS) for p in files):
        langs.add("js")
    return langs


def _resolve_path(files: list[str], raw_path: str) -> str:
    """Resolve a tool-reported path (may be absolute / prefixed) to the real repo-relative path
    the sandbox can read. Falls back to the raw path unchanged if no match is found."""
    if raw_path in files:
        return raw_path
    norm = raw_path.replace("\\", "/")
    for prefix in ("/work/repo/", "./"):
        if norm.startswith(prefix):
            norm = norm[len(prefix):]
    if norm in files:
        return norm
    for path in files:                              # last resort: suffix match on the path
        if path.endswith(norm) or norm.endswith(path):
            return path
    return raw_path


def _structure(files: list[str]) -> str:
    shown = files[:_MAX_STRUCTURE]
    lines = "\n".join(f"- {p}" for p in shown)
    if len(files) > _MAX_STRUCTURE:
        lines += f"\n- ... (+{len(files) - _MAX_STRUCTURE} more)"
    return lines


def _parse_ruff(stdout: str) -> list[dict[str, Any]]:
    text = (stdout or "").strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return []
    out: list[dict[str, Any]] = []
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict):
            continue
        loc = item.get("location") or {}
        out.append({"rule_id": item.get("code", ""), "file": item.get("filename", ""),
                    "line": loc.get("row"), "column": loc.get("column"),
                    "message": item.get("message", "")})
    return out


def _parse_eslint(stdout: str) -> list[dict[str, Any]]:
    text = (stdout or "").strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return []
    out: list[dict[str, Any]] = []
    for entry in data if isinstance(data, list) else []:
        if not isinstance(entry, dict):
            continue
        path = entry.get("filePath", "")
        for msg in entry.get("messages", []) or []:
            if isinstance(msg, dict):
                out.append({"rule_id": msg.get("ruleId") or "eslint", "file": path,
                            "line": msg.get("line"), "column": msg.get("column"),
                            "message": msg.get("message", ""), "severity": msg.get("severity", 1)})
    return out


def _sonar_raw(sonar: SonarResult) -> list[dict[str, Any]]:
    if not sonar.ok:
        return []
    return [{"rule_id": i.rule, "file": i.path, "line": i.line, "message": i.message,
             "severity": i.severity, "type": i.type} for i in sonar.issues]


def _parse_review(raw: str) -> tuple[dict[str, Any] | None, str]:
    obj = _extract_json(raw)
    if not isinstance(obj, dict):
        return None, "no JSON object found"
    if not isinstance(obj.get("executive_summary"), str):
        return None, "'executive_summary' must be a string"
    obj.setdefault("engineering_observations", [])
    obj.setdefault("recommendations", [])
    return obj, ""


def _extract_json(text: str) -> Any:
    stripped = (text or "").strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```[a-zA-Z0-9]*", "", stripped).strip()
        if stripped.endswith("```"):
            stripped = stripped[:-3].strip()
    try:
        return json.loads(stripped)
    except (ValueError, TypeError):
        pass
    start, end = stripped.find("{"), stripped.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(stripped[start:end + 1])
        except (ValueError, TypeError):
            return None
    return None


def _artifact_text(design_package: dict[str, Any], *names: str) -> str:
    lowered = {k.lower(): v for k, v in design_package.items()}
    for name in names:
        value = design_package.get(name)
        if value is None:
            value = lowered.get(name.lower())
        if value is not None:
            return value if isinstance(value, str) else json.dumps(value, indent=2, sort_keys=True)
    return ""
