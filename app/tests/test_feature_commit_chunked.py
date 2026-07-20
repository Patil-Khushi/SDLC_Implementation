"""Chunked per-file generation (manifest -> one file per call -> fallback) in
``scripts/feature_commit.py``. No prior test covered this logic (confirmed absent in the PR #2
review); these pin the manifest/per-file/fallback cascade and, specifically, the path-matching
regression the review found.

Regression: ``_generate_file`` used to fall back to ``files[0]`` whenever no returned entry
matched the requested path by exact string — so a reply with MULTIPLE files and no exact match
silently paired an unrelated file's content with the requested path (e.g. main.py could end up
holding utils.py's content, with nothing to signal the mismatch). It now only accepts an
unmatched entry when there is exactly ONE file in the reply (unambiguous), and skips (returns
None) otherwise.
"""

from __future__ import annotations

from collections import deque

import scripts.feature_commit as fc


class _ScriptedGateway:
    """Serves canned ``.complete()`` replies in order; records prompts for inspection."""

    def __init__(self, responses: list[str]) -> None:
        self._queue: deque[str] = deque(responses)
        self.calls: list[str] = []

    def complete(self, prompt: str, *, system: str | None = None, max_tokens: int | None = None) -> str:
        self.calls.append(prompt)
        if not self._queue:
            raise AssertionError("ScriptedGateway ran out of responses")
        return self._queue.popleft()


# --------------------------------------------------------------------------- _layer_manifest

def test_layer_manifest_parses_dedupes_and_strips_leading_slash() -> None:
    gw = _ScriptedGateway([
        '{"files":['
        '{"path":"/backend/app/main.py","purpose":"entrypoint"},'
        '{"path":"backend/app/main.py","purpose":"dup, dropped"},'
        '{"path":"backend/app/db.py","purpose":"db setup"}'
        "]}"
    ])
    manifest = fc._layer_manifest(gw, "ctx", [], "US-01", "Title", "body", "BACKEND", "instr")
    assert [m["path"] for m in manifest] == ["backend/app/main.py", "backend/app/db.py"]
    assert manifest[0]["purpose"] == "entrypoint"


def test_layer_manifest_returns_empty_on_unparseable_reply() -> None:
    gw = _ScriptedGateway(["not json at all"])
    assert fc._layer_manifest(gw, "ctx", [], "US-01", "Title", "body", "BACKEND", "instr") == []


# --------------------------------------------------------------------------- _generate_file

def test_generate_file_exact_path_match() -> None:
    gw = _ScriptedGateway(['{"files":[{"path":"backend/app/main.py","content":"MAIN"}]}'])
    f = fc._generate_file(
        gw, "ctx", "US-01", "Title", "body", "BACKEND",
        "backend/app/main.py", "entrypoint", ["backend/app/main.py"], {},
    )
    assert f == {"path": "backend/app/main.py", "content": "MAIN"}


def test_generate_file_normalizes_leading_dot_slash() -> None:
    gw = _ScriptedGateway(['{"files":[{"path":"./backend/app/main.py","content":"MAIN"}]}'])
    f = fc._generate_file(
        gw, "ctx", "US-01", "Title", "body", "BACKEND",
        "backend/app/main.py", "entrypoint", ["backend/app/main.py"], {},
    )
    assert f == {"path": "backend/app/main.py", "content": "MAIN"}


def test_generate_file_accepts_a_single_unambiguous_reply_even_if_the_path_differs() -> None:
    # Exactly one file back: no ambiguity about which file it is, even though the model
    # echoed a different-looking path (e.g. re-cased).
    gw = _ScriptedGateway(['{"files":[{"path":"backend/app/MAIN.py","content":"MAIN"}]}'])
    f = fc._generate_file(
        gw, "ctx", "US-01", "Title", "body", "BACKEND",
        "backend/app/main.py", "entrypoint", ["backend/app/main.py"], {},
    )
    assert f == {"path": "backend/app/main.py", "content": "MAIN"}


def test_generate_file_skips_when_multiple_files_none_match() -> None:
    # Regression for the silent-mismatch bug: previously this returned
    # {"path": "backend/app/main.py", "content": "UTILS"} (files[0], wrong content).
    gw = _ScriptedGateway([
        '{"files":['
        '{"path":"backend/app/utils.py","content":"UTILS"},'
        '{"path":"backend/app/other.py","content":"OTHER"}'
        "]}"
    ])
    f = fc._generate_file(
        gw, "ctx", "US-01", "Title", "body", "BACKEND",
        "backend/app/main.py", "entrypoint", ["backend/app/main.py"], {},
    )
    assert f is None


def test_generate_file_returns_none_when_still_unparseable_after_retry() -> None:
    gw = _ScriptedGateway(["not json", "still not json"])
    f = fc._generate_file(
        gw, "ctx", "US-01", "Title", "body", "BACKEND",
        "backend/app/main.py", "entrypoint", ["backend/app/main.py"], {},
    )
    assert f is None


# --------------------------------------------------------------------------- _generate_layer_chunked

def test_generate_layer_chunked_end_to_end() -> None:
    gw = _ScriptedGateway([
        '{"files":[{"path":"backend/app/main.py","purpose":"entrypoint"},'
        '{"path":"backend/app/db.py","purpose":"db setup"}]}',
        '{"files":[{"path":"backend/app/main.py","content":"MAIN"}]}',
        '{"files":[{"path":"backend/app/db.py","content":"DB"}]}',
    ])
    current: dict[str, str] = {}
    files = fc._generate_layer_chunked(gw, "ctx", current, "US-01", "Title", "body", "BACKEND", "instr")
    assert files == [
        {"path": "backend/app/main.py", "content": "MAIN"},
        {"path": "backend/app/db.py", "content": "DB"},
    ]
    # current is updated as each file lands, so a later file (or layer) in the same feature sees it.
    assert current == {"backend/app/main.py": "MAIN", "backend/app/db.py": "DB"}


def test_generate_layer_chunked_falls_back_when_manifest_empty() -> None:
    gw = _ScriptedGateway([
        '{"files":[]}',  # manifest: no files -> _layer_manifest returns []
        '{"files":[{"path":"backend/app/main.py","content":"WHOLE"}]}',  # whole-layer fallback
    ])
    files = fc._generate_layer_chunked(gw, "ctx", {}, "US-01", "Title", "body", "BACKEND", "instr")
    assert files == [{"path": "backend/app/main.py", "content": "WHOLE"}]


def test_generate_layer_chunked_falls_back_when_every_file_fails_to_parse() -> None:
    gw = _ScriptedGateway([
        '{"files":[{"path":"backend/app/main.py","purpose":"x"}]}',  # manifest: 1 file
        "not json",       # first attempt for that file
        "still not json",  # retry attempt -> _generate_file gives up, returns None
        '{"files":[{"path":"backend/app/main.py","content":"WHOLE"}]}',  # whole-layer fallback
    ])
    files = fc._generate_layer_chunked(gw, "ctx", {}, "US-01", "Title", "body", "BACKEND", "instr")
    assert files == [{"path": "backend/app/main.py", "content": "WHOLE"}]


def test_generate_layer_chunked_caps_an_oversized_manifest() -> None:
    n = fc._MAX_FILES_PER_LAYER + 10
    listed = ",".join(f'{{"path":"f{i}.py","purpose":"p"}}' for i in range(n))
    responses = [f'{{"files":[{listed}]}}']
    responses += [f'{{"files":[{{"path":"f{i}.py","content":"C{i}"}}]}}' for i in range(fc._MAX_FILES_PER_LAYER)]
    gw = _ScriptedGateway(responses)
    files = fc._generate_layer_chunked(gw, "ctx", {}, "US-01", "Title", "body", "BACKEND", "instr")
    assert len(files) == fc._MAX_FILES_PER_LAYER


# --------------------------------------------------------------------------- _write path guard

def test_write_skips_a_path_that_escapes_the_project_dir(tmp_path) -> None:
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    current: dict[str, str] = {}
    written = fc._write(
        project_dir,
        [
            {"path": "../outside.py", "content": "evil"},
            {"path": "backend/app/main.py", "content": "MAIN"},
        ],
        current,
    )
    assert written == ["backend/app/main.py"]
    assert not (tmp_path / "outside.py").exists()
    assert (project_dir / "backend/app/main.py").read_text(encoding="utf-8") == "MAIN"
