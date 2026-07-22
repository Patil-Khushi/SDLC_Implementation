"""Security Agent - Semgrep findings + LLM interpretation -> a compact security report.

Runs LAST, as the true final stage of the run. Mirrors ``code_review.py``'s read-only sandbox
pattern closely (clone -> run a static tool -> LLM interprets -> render + persist a report), just
simpler - one table, one summary, no four-bucket classification.

Deliberately does NOT reuse ``services/finding_aggregator`` (built for Code Review's
bucket/auto_fix/operation machinery): every Semgrep finding is security-category and never
auto-fixed, so that machinery buys nothing here and would only couple this agent to Code Review's
schema. Semgrep is a LOCAL CLI TOOL run inside the sandbox - like Ruff/ESLint, not like SonarQube
(a remote server queried over HTTP) - so its parsing lives here as a private function, the same
way ``_parse_ruff``/``_parse_eslint`` live directly in ``code_review.py`` rather than in a
dedicated ``app/integrations/`` client.

Owns: ``security_report`` (+ ``security_report_path``), the routing signals ``security_verdict``
and ``security_findings_path`` (consumed by ``router.route_after_security`` and by Refactoring),
and its ``workflow_status`` stamp.
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
from app.integrations.review_sandbox import ReviewSandbox, get_review_sandbox, is_allowed_repo_url
from app.services.llm_gateway import LLMGateway

logger = logging.getLogger(__name__)

_SEVERITY_MAP = {"ERROR": "High", "WARNING": "Medium", "INFO": "Low"}
_SEV_RANK = {"High": 3, "Medium": 2, "Low": 1}
_VERDICT = ("approve", "changes_requested")
# Explicit, sandbox-bundled ruleset - never the cloned repo's own .semgrep.yml (same "never trust
# the untrusted repo's own config" principle as ESLint's --no-config-lookup in code_review.py).
_SEMGREP_CONFIG = "/opt/semgrep/rules.yml"


class SecurityAgent(BaseAgent):
    name = "security"

    def __init__(
        self,
        sandbox_factory: Callable[[], ReviewSandbox] | None = None,
        llm: LLMGateway | None = None,
    ) -> None:
        super().__init__()
        if llm is not None:
            self.llm = llm
        self._sandbox_factory = sandbox_factory

    def _new_sandbox(self) -> ReviewSandbox:
        return self._sandbox_factory() if self._sandbox_factory else get_review_sandbox()

    def execute(self, state: WorkflowState) -> WorkflowState:
        project_id = state.get("project_id") or state.get("run_id") or "project"
        run_id = state.get("run_id", "")
        repo_url = (state.get("repo_url") or "").strip()
        branch = (state.get("branch") or get_settings().working_branch or "").strip()

        meta = {"project": project_id, "run_id": run_id, "repo_url": repo_url or "(none)",
                "branch": branch or "(default)", "date": _now_iso()}

        if not repo_url:
            logger.info("[security] run=%s | no repo_url - nothing to scan", run_id or "-")
            review = _empty_review("No repository URL was provided, so there was nothing to scan.")
            report = _render(meta, [], review)
            return self._finish(state, project_id, run_id, report, [], review)

        if not is_allowed_repo_url(repo_url):
            logger.warning("[security] run=%s | refusing to clone disallowed repo_url: %s", run_id or "-", repo_url)
            review = _empty_review(
                f"Repository URL '{repo_url}' is not an allowed GitHub URL (expected "
                "https://github.com/<owner>/<repo>) - refusing to clone.",
                verdict="changes_requested",
            )
            report = _render(meta, [], review)
            return self._finish(state, project_id, run_id, report, [], review)

        findings: list[dict[str, Any]] = []
        clone_error = ""
        try:
            logger.info("[security] run=%s | [1/3] cloning '%s' (branch %s)...", run_id or "-", repo_url, branch or "default")
            with self._new_sandbox() as sb:
                clone = sb.clone(repo_url, ref=branch or None)
                if not clone.ok and branch:
                    clone = sb.clone(repo_url, ref=None)  # working branch missing -> default branch
                if not clone.ok:
                    clone_error = (clone.stderr or clone.stdout or "clone failed").strip()[:400]
                    logger.warning("[security] run=%s | [1/3] clone FAILED: %s", run_id or "-", clone_error)
                else:
                    logger.info("[security] run=%s | [2/3] running semgrep...", run_id or "-")
                    res = sb.run(["semgrep", "--config", _SEMGREP_CONFIG, "--json", "."])
                    findings = _parse_semgrep(res.stdout)
                    logger.info("[security] run=%s | [2/3] semgrep: %d finding(s)", run_id or "-", len(findings))
        except Exception as exc:  # noqa: BLE001
            logger.exception("[security] run=%s | sandbox failed", run_id or "-")
            clone_error = clone_error or f"security sandbox error: {exc}"

        if clone_error:
            review = _empty_review(f"The repository could not be scanned: {clone_error}", verdict="changes_requested")
            report = _render(meta, [], review)
            return self._finish(state, project_id, run_id, report, [], review)

        logger.info("[security] run=%s | [3/3] LLM interpreting findings...", run_id or "-")
        review = self._review_llm(findings, meta)
        report = _render(meta, findings, review)
        return self._finish(state, project_id, run_id, report, findings, review)

    def _review_llm(self, findings: list[dict[str, Any]], meta: dict[str, Any]) -> dict[str, Any]:
        system = self._load_prompt("security")
        prompt = (
            f"## Project\n{meta['project']} (repo: {meta['repo_url']})\n\n"
            "## Semgrep findings (deterministic - report these verbatim, do not recompute)\n"
            + "```json\n" + json.dumps(findings, indent=2) + "\n```\n\n"
            "Interpret the above and reply with STRICT JSON (executive_summary, verdict)."
        )
        parsed, err = _parse_review(self.llm.complete(prompt=prompt, system=system))
        if parsed is None:
            retry = f"{prompt}\n\nYour previous reply was not valid JSON ({err}). Reply with STRICT JSON only."
            parsed, err = _parse_review(self.llm.complete(prompt=retry, system=system))
        if parsed is None:
            return _empty_review(f"Automated interpretation could not be parsed ({err}).", verdict="changes_requested")
        return parsed

    def _finish(self, state: WorkflowState, project_id: str, run_id: str, report: str,
                findings: list[dict[str, Any]], review: dict[str, Any]) -> WorkflowState:
        # One folder per project: reports/<project>/ containing security-report.md +
        # security-findings.json (a later run for the same project overwrites the previous one).
        run_dir = Path(get_settings().reports_dir) / _slug(project_id)
        run_dir.mkdir(parents=True, exist_ok=True)

        md_path = run_dir / "security-report.md"
        md_path.write_text(report, encoding="utf-8")
        json_path = run_dir / "security-findings.json"
        verdict = _final_verdict(findings, review.get("verdict"))
        # A structured object, not a bare list - a bare `[]` reads as "did this even run?" on its
        # own; verdict/summary/findings_count make "clean scan, approved" unambiguous even without
        # opening the Markdown report.
        payload = {
            "verdict": verdict,
            "summary": review.get("executive_summary", ""),
            "findings_count": len(findings),
            "findings": findings,
        }
        json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

        state["security_report"] = report
        state["security_report_path"] = str(md_path)
        # Routing signals for the Security<->Refactoring loop (app/graph/router.py) and the
        # finalize (dev -> main PR) step — findings_path mirrors Code Review's review_findings_path.
        state["security_verdict"] = verdict
        state["security_findings_path"] = str(json_path)
        state["workflow_status"] = "security_reviewed"
        logger.info("[security] run=%s | report saved to folder: %s", run_id or "-", run_dir)
        return state


# --------------------------------------------------------------------------- renderer


def _render(meta: dict[str, Any], findings: list[dict[str, Any]], review: dict[str, Any]) -> str:
    verdict = _final_verdict(findings, review.get("verdict"))
    sev = {k: 0 for k in _SEV_RANK}
    for f in findings:
        sev[f.get("severity", "Low")] = sev.get(f.get("severity", "Low"), 0) + 1

    L: list[str] = []
    a = L.append
    a("# Security Report\n")

    a("## Metadata\n")
    a("| Field | Value |")
    a("| --- | --- |")
    a(f"| Project | {meta['project']} |")
    a(f"| Repository | {meta['repo_url']} |")
    a(f"| Branch | {meta['branch']} |")
    a(f"| Run ID | {meta['run_id'] or '-'} |")
    a(f"| Scan Date | {meta['date']} |")
    a(f"| Verdict | {_verdict_label(verdict)} |\n")

    if not findings and verdict == "approve":
        # Guarded on verdict too, not just empty findings - a clone/scan ERROR also has empty
        # findings but a "changes_requested" verdict (see _empty_review call sites above); this
        # callout must never claim "approved" for that case.
        a("**No security issues found - APPROVED.** Semgrep scanned the repository and flagged nothing.\n")

    a("## Executive Summary\n")
    a((review.get("executive_summary") or "(no summary)").strip() + "\n")

    a("## Findings\n")
    if findings:
        a(f"**By severity:** High: {sev['High']} | Medium: {sev['Medium']} | Low: {sev['Low']}\n")
        a("| Rule | Severity | Location | Message |")
        a("| --- | --- | --- | --- |")
        for f in sorted(findings, key=lambda x: -_SEV_RANK.get(x.get("severity", "Low"), 0)):
            loc = f"{f['file']}:{f['line']}" if f.get("line") else f.get("file", "-")
            a(f"| {f.get('rule', '-')} | {f.get('severity', 'Low')} | `{loc}` | {_esc(f.get('message', ''))} |")
        a("")
    else:
        a("_No findings - nothing for Semgrep to report._\n")

    a("## Verdict\n")
    a(f"- **Verdict:** {_verdict_label(verdict)}")
    a(f"- **Rationale:** {_verdict_rationale(findings, sev)}")
    a("")
    return "\n".join(L)


# --------------------------------------------------------------------------- helpers


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", value) or "run"


def _esc(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ").strip()


def _empty_review(summary: str, verdict: str = "approve") -> dict[str, Any]:
    return {"executive_summary": summary, "verdict": verdict}


def _verdict_label(v: str) -> str:
    return {"approve": "APPROVE", "changes_requested": "CHANGES REQUESTED"}.get(v, v.upper())


def _final_verdict(findings: list[dict[str, Any]], llm_verdict: Any) -> str:
    if any(f.get("severity") == "High" for f in findings):
        return "changes_requested"
    v = str(llm_verdict or "approve").lower()
    return v if v in _VERDICT else "approve"


def _verdict_rationale(findings: list[dict[str, Any]], sev: dict[str, int]) -> str:
    if sev["High"]:
        return f"{sev['High']} high-severity finding(s) require attention before proceeding."
    if findings:
        return f"{len(findings)} lower-severity finding(s); safe to proceed with recommended cleanups."
    return "No findings; Semgrep found nothing to flag."


def _parse_semgrep(stdout: str) -> list[dict[str, Any]]:
    text = (stdout or "").strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return []
    out: list[dict[str, Any]] = []
    for item in (data.get("results") if isinstance(data, dict) else None) or []:
        if not isinstance(item, dict):
            continue
        extra = item.get("extra") or {}
        start = item.get("start") or {}
        severity = _SEVERITY_MAP.get(str(extra.get("severity", "")).upper(), "Low")
        out.append({
            "rule": item.get("check_id", ""), "file": item.get("path", ""),
            "line": start.get("line"), "severity": severity,
            "message": str(extra.get("message", "")),
        })
    return out


def _parse_review(raw: str) -> tuple[dict[str, Any] | None, str]:
    obj = _extract_json(raw)
    if not isinstance(obj, dict):
        return None, "no JSON object found"
    if not isinstance(obj.get("executive_summary"), str):
        return None, "'executive_summary' must be a string"
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
