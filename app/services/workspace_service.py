"""Workspace file management for generated project artifacts.

Small helper for writing generated files into the run workspace and returning the
workspace-relative path recorded in ``WorkflowState["generated_code"]``.

NOTE (IMP-001): once the exec-sandbox executor exists, code generation writes files INTO the
sandbox via ``integrations/executor.write_file`` (CLAUDE.md rule 5). This local helper is the
pre-sandbox stand-in and keeps writes off the deterministic-gate path.
"""

from __future__ import annotations

from pathlib import Path


def write_file(workspace_dir: str, rel_path: str, content: str) -> str:
    """Write ``content`` to ``workspace_dir/rel_path`` (creating parents); return ``rel_path``."""
    target = Path(workspace_dir) / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return rel_path
