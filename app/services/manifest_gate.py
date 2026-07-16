"""Deterministic manifest gate (no LLM).

Reads a design pack's ``index.md`` manifest — the mandatory Handoff-ID → file table — and
verifies each listed file EXISTS ON DISK. The manifest's own "Present" column (and any decoy
like ``index.false-claim.md``) is NOT trusted: presence is decided by the filesystem.

Returns ``{"ok": bool, "missing": [handoff_ids]}`` with the missing IDs sorted for determinism.
"""

from __future__ import annotations

from pathlib import Path


def check_manifest(pack_dir: str | Path) -> dict:
    """Verify every file listed in ``<pack_dir>/index.md`` exists on disk.

    Args:
        pack_dir: the design-pack directory containing ``index.md``.

    Returns:
        ``{"ok": True, "missing": []}`` when all listed files exist, else
        ``{"ok": False, "missing": [<handoff ids>]}`` (sorted).
    """
    pack = Path(pack_dir)
    index = pack / "index.md"
    if not index.exists():
        # No manifest at all — treat as a hard gate failure.
        return {"ok": False, "missing": ["index.md"]}

    missing: set[str] = set()
    for handoff_ids, rel_file in _iter_manifest_rows(index.read_text(encoding="utf-8")):
        if not (pack / rel_file).exists():
            missing.update(handoff_ids)

    ordered = sorted(missing)
    return {"ok": not ordered, "missing": ordered}


def _iter_manifest_rows(index_md: str):
    """Yield ``(handoff_ids, rel_file)`` for each data row of the manifest table.

    Table shape: ``| # | Handoff ID(s) | File | Owner | Present |``. Header/separator rows and
    any row whose first cell isn't a number are skipped. The File cell may carry a parenthetical
    (e.g. ``assets/ (logo.svg, ...)`` or ``index.md (this file)``) — only the leading path token
    is used.
    """
    for line in index_md.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if len(cells) < 3 or not cells[0].isdigit():
            continue  # header, separator, or non-data row
        handoff_ids = [i.strip() for i in cells[1].split(",") if i.strip()]
        file_token = cells[2].split()[0] if cells[2].split() else ""
        if handoff_ids and file_token:
            yield handoff_ids, file_token
