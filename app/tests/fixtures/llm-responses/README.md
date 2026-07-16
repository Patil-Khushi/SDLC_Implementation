# llm-responses/ — recorded model output (record once, replay forever)

The `fake_gateway` fixture (see `../../conftest.py`) replays files from this directory instead
of calling a real model, so tests are deterministic and cost no tokens.

- **Key:** the work-item id, inferred from the prompt (`Work item: <id>`), or pinned via
  `gateway.use("<key>")`. Variant payloads use a suffix, e.g. `backend-loginUser.broken1`,
  `backend-loginUser.fixed`.
- **File:** `<key>.txt` containing the raw model response (the strict-JSON `{"files":[...]}`).
- **Record:** run the suite with `RECORD=1` (and a real `ANTHROPIC_API_KEY`) once — the gateway
  calls the real model per key and writes `<key>.txt` here. Commit the results; later runs replay
  them. A missing key fails loudly telling you to run `RECORD=1`.

Hand-authored `*.broken1.txt` / `*.fixed.txt` recordings drive the repair/cap tests against the
REAL compiler (broken1 = code that will not compile; fixed = code that compiles).

**Path convention (repair recordings):** code generation prefixes each file path with the run's
`project_id`; the repair step writes the paths it was *shown* verbatim (already prefixed). So a
codegen recording (e.g. `*.broken1`) uses an UNPREFIXED path, while a repair recording (`*.fixed`)
uses the PREFIXED path so it overwrites the same file. `test_imp001` runs with `project_id="p1"`,
hence `p1/…` in `backend-loginUser.fixed.txt`.
