"""CLI: ``python -m app.models`` regenerates the published JSON Schemas.

Run after editing any contract model so contracts/implementation-to-testing/*.schema.json stay
in sync (test_contracts.py enforces it).
"""

from __future__ import annotations

from app.models import export_json_schemas


def main() -> None:
    for stem, path in export_json_schemas().items():
        print(f"wrote {stem} -> {path}")


if __name__ == "__main__":
    main()
