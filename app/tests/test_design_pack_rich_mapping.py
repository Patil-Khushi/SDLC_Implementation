"""``design_pack.has_rich_api_mapping`` must agree with ``detect_roles`` on EVERY input, since
``app.services.boilerplate`` uses the former (it only ever has an in-memory artifact dict) to
answer the exact same question ``build_plan()`` answers with the latter (it has ``pack_dir`` on
disk) — see ``has_rich_api_mapping``'s docstring. A disagreement between the two silently
reintroduces the scaffold/plan-builder root mismatch PR #24 fixed.

Regression coverage for a review-flagged gap: the first version of ``has_rich_api_mapping`` checked
only the CSV's header row, so a header-only CSV (columns present, zero data rows) registered as a
"rich mapping" — while ``detect_roles`` (via ``_read_table``) requires at least one real data row
and would see no table at all for the same file. Fixed by routing both through the shared
``_rows_from_delimited_text``.
"""

from __future__ import annotations

from pathlib import Path

from app.services.design_pack import detect_roles, has_rich_api_mapping

_RICH_HEADER = "operation_id,endpoint_path,req_ids"
_RICH_ROW = "loginUser,/api/login,REQ-1"


def test_header_only_csv_is_not_a_rich_mapping(tmp_path: Path) -> None:
    # The exact gap flagged in review: headers alone, with zero data rows underneath.
    header_only_text = _RICH_HEADER + "\n"
    assert has_rich_api_mapping({"api-to-ui-mapping.csv": header_only_text}) is False

    # detect_roles must agree from the same bytes on disk — this is the invariant that matters.
    p = tmp_path / "api-to-ui-mapping.csv"
    p.write_text(header_only_text, encoding="utf-8")
    assert detect_roles(tmp_path).get("rich_api_mapping") in (None, [])


def test_csv_with_a_real_data_row_is_a_rich_mapping(tmp_path: Path) -> None:
    text_with_data = f"{_RICH_HEADER}\n{_RICH_ROW}\n"
    assert has_rich_api_mapping({"api-to-ui-mapping.csv": text_with_data}) is True

    p = tmp_path / "api-to-ui-mapping.csv"
    p.write_text(text_with_data, encoding="utf-8")
    assert detect_roles(tmp_path).get("rich_api_mapping")


def test_csv_with_only_blank_data_rows_is_not_a_rich_mapping() -> None:
    # A row of bare commas parses as a "row" of all-empty values — _rows_from_delimited_text's
    # final filter must still reject it as having no real data, same as _read_table does.
    text = f"{_RICH_HEADER}\n,,\n"
    assert has_rich_api_mapping({"api-to-ui-mapping.csv": text}) is False


def test_pre_parsed_row_list_form_is_also_respected() -> None:
    # Some callers may hand this function already-parsed rows (a list of dicts) rather than raw
    # text; a header-only equivalent there is an empty list, and a real data row is a non-empty one.
    assert has_rich_api_mapping({"api-to-ui-mapping.csv": []}) is False
    assert has_rich_api_mapping(
        {"api-to-ui-mapping.csv": [{"operation_id": "loginUser", "endpoint_path": "/api/login", "req_ids": "REQ-1"}]}
    ) is True


def test_non_rich_ui_mapping_csv_is_not_flagged() -> None:
    # A plain UI<->API table (no operation_id/endpoint_path/req* columns) must not be mistaken for
    # the legacy rich mapping — it's the adaptive path's own input shape, not the legacy one.
    text = "screen,endpoint\nLogin,/api/login\n"
    assert has_rich_api_mapping({"api-to-ui-mapping.csv": text}) is False


def test_non_csv_artifacts_are_ignored() -> None:
    assert has_rich_api_mapping({"backend-structure.json": {"tree": {"src/": {}}}, "skills.md": "notes"}) is False


def test_empty_design_package_is_not_a_rich_mapping() -> None:
    assert has_rich_api_mapping({}) is False
