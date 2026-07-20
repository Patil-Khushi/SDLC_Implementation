"""design_pack must tolerate ordinary Windows-authored packs: a UTF-8 BOM and non-UTF-8 dumps.

Both inputs are routine on Windows (PowerShell's ``Out-File`` writes UTF-8 *with BOM*; SQL
Server / MySQL exports are often cp1252 or UTF-16). Before the fix, a BOM made a valid
``openapi.json`` fail JSON detection, and a non-UTF-8 ``.sql`` raised an uncaught
``UnicodeDecodeError`` (a ``ValueError``, not ``OSError``) that crashed ``resolve()``.
"""

from __future__ import annotations

from pathlib import Path

from app.services.design_pack import _load_structured, _schema_entities


def test_bom_prefixed_json_still_parses(tmp_path: Path) -> None:
    p = tmp_path / "openapi.json"
    p.write_text('{"openapi": "3.0.0", "paths": {}}', encoding="utf-8-sig")  # BOM-prefixed
    obj = _load_structured(p)
    assert isinstance(obj, dict) and obj.get("openapi") == "3.0.0"


def test_non_utf8_sql_degrades_instead_of_crashing(tmp_path: Path) -> None:
    p = tmp_path / "schema.sql"
    # cp1252 'é' (0xE9) is not valid UTF-8 here — reading must degrade to [], not raise.
    p.write_bytes("-- café users\nCREATE TABLE users (id INT);".encode("cp1252"))
    assert _schema_entities(p, None) == []
