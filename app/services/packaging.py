"""Builds the run's final downloadable artifact — a zip of the generated project plus its
documentation and review/security reports.

Deterministic, no LLM — pure Python stdlib (``zipfile``). Reads files back through the injected
Executor (never touches the filesystem directly, per DEVELOPER_GUIDE.md rule 5), so it works the
same way against the real exec-sandbox or a FakeExecutor in tests.
"""

from __future__ import annotations

import re
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from app.config.settings import get_settings
from app.integrations.executor import Executor


def build_project_zip(
    *,
    executor: Executor,
    project_dir: str,
    generated_code: list[str],
    documentation: str = "",
    review_report: str = "",
    security_report: str = "",
) -> str:
    """Zip every generated file (source + boilerplate) plus README/review/security docs.

    ``generated_code`` entries are ``<project_dir>/...``-prefixed workspace paths (as written by
    ``scaffold_node``/``code_generator_node``); the prefix is stripped so the zip's internal layout
    is a clean, self-contained project tree, not one nested under the run's project_id folder.
    Missing/unreadable files are skipped (not fatal) — same graceful-degradation style the other
    report-writing agents use.

    The scaffold already renders its own boilerplate ``README.md`` (``app/services/readme.py``);
    when Documentation produced one too, its version — written from the actual final source,
    after every work item — supersedes the scaffold's (reserved up front, before the generated-code
    loop, so the scaffold's copy is skipped rather than double-written into the zip).
    """
    run_dir = Path(get_settings().reports_dir) / _slug(project_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    zip_path = run_dir / f"{_slug(project_dir)}.zip"

    prefix = f"{project_dir}/"
    written: set[str] = set()
    seen: set[str] = set()
    with ZipFile(zip_path, "w", ZIP_DEFLATED) as zf:
        if documentation.strip():
            zf.writestr("README.md", documentation)
            written.add("README.md")
        if review_report.strip():
            zf.writestr("docs/code-review-report.md", review_report)
            written.add("docs/code-review-report.md")
        if security_report.strip():
            zf.writestr("docs/security-report.md", security_report)
            written.add("docs/security-report.md")

        for path in generated_code:
            if path in seen:
                continue
            seen.add(path)
            arcname = path[len(prefix):] if path.startswith(prefix) else path
            if arcname in written:
                continue
            try:
                content = executor.read_file(path)
            except Exception:  # noqa: BLE001 - a missing file just means it's left out, not fatal
                continue
            zf.writestr(arcname, content)
            written.add(arcname)

    return str(zip_path)


def _slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", value) or "run"
