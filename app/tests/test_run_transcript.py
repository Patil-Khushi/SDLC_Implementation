"""Record a run's terminal output, replay it in minutes — the demo path for a 3-6 hour pipeline.

A real run cannot be performed in front of an audience, so it is recorded and replayed. These tests
pin the properties that make the replay trustworthy and safe to run in a room: nothing is lost from
the terminal, credentials never reach the transcript file, the replay lasts as long as it is asked
to, and a killed run still leaves something replayable.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from app.services.run_transcript import (
    MANIFEST_NAME,
    TRANSCRIPT_NAME,
    Event,
    RunTranscript,
    artifacts_from_state,
    coalesce_lines,
    filter_highlights,
    format_artifacts,
    format_banner,
    load_events,
    load_manifest,
    pace,
    replay,
    scrub,
)


def test_recording_tees_output_and_leaves_the_terminal_untouched(tmp_path: Path, capsys) -> None:
    # The run must look EXACTLY as it does without recording — a demo aid may not alter the thing
    # it observes.
    with RunTranscript(tmp_path, project="proj-1") as tr:
        print("hello terminal")
        print("to stderr", file=sys.stderr)
        tr.finish({"workflow_status": "completed"})

    captured = capsys.readouterr()
    assert "hello terminal" in captured.out          # still printed live
    assert "to stderr" in captured.err

    events = load_events(tr.dir)
    assert [e.text for e in events if e.stream == "out"] == ["hello terminal\n"]
    assert [e.text for e in events if e.stream == "err"] == ["to stderr\n"]


def test_streams_are_restored_after_recording(tmp_path: Path) -> None:
    before = (sys.stdout, sys.stderr)
    with RunTranscript(tmp_path, project="p") as tr:
        assert sys.stdout is not before[0]  # tee installed
        tr.finish()
    assert (sys.stdout, sys.stderr) == before  # ...and removed again


def test_tokens_never_reach_the_transcript_file(tmp_path: Path) -> None:
    # The transcript gets copied to a demo machine and pasted into chats. The state dump redacts
    # secret-looking FIELDS; this redacts token-shaped TEXT wherever it appears.
    with RunTranscript(tmp_path, project="p") as tr:
        print("pushing with ghp_0123456789abcdefghijklmnopqrstuvwx now")
        print("url https://x-access-token:ghs_abcdefghijklmnopqrstuvwxyz012345@github.com/o/r")
        tr.finish()

    raw = (tr.dir / TRANSCRIPT_NAME).read_text(encoding="utf-8")
    assert "ghp_0123456789" not in raw
    assert "ghs_abcdefghij" not in raw
    assert "<redacted>" in raw


def test_scrub_leaves_ordinary_text_alone() -> None:
    assert scrub("no secrets here, just github.com/owner/repo") == (
        "no secrets here, just github.com/owner/repo"
    )


def test_print_chunks_are_regrouped_into_whole_lines() -> None:
    # print() writes the text and the "\n" separately. Without regrouping, --highlights drops the
    # bare newline belonging to a kept line and everything collapses onto one row.
    raw = [
        Event(t=0.0, stream="out", text="=" * 10),
        Event(t=0.0, stream="out", text="\n"),
        Event(t=1.0, stream="out", text="BUILD PLAN"),
        Event(t=1.0, stream="out", text="\n"),
    ]
    lines = coalesce_lines(raw)
    assert [e.text for e in lines] == ["=" * 10 + "\n", "BUILD PLAN\n"]


def test_partial_final_line_from_a_killed_run_is_kept() -> None:
    lines = coalesce_lines([Event(t=0.5, stream="out", text="half a line, no newline")])
    assert [e.text for e in lines] == ["half a line, no newline"]


def test_truncated_transcript_still_loads(tmp_path: Path) -> None:
    # A run killed mid-write leaves a partial JSON line; the demo must not die on it.
    path = tmp_path / TRANSCRIPT_NAME
    path.write_text(
        json.dumps({"t": 0.0, "s": "out", "x": "good line\n"}) + "\n" + '{"t": 1.0, "s": "out"',
        encoding="utf-8",
    )
    assert [e.text for e in load_events(path)] == ["good line\n"]


def test_replay_lasts_as_long_as_requested_and_emits_everything() -> None:
    events = [Event(t=float(i), stream="out", text=f"line {i}\n") for i in range(20)]
    slept: list[float] = []
    written: list[str] = []

    count = replay(events, duration=30.0, write=lambda _s, t: written.append(t),
                   sleep=slept.append)

    assert count == 20
    assert len(written) == 20                       # nothing dropped
    assert abs(sum(slept) - 30.0) < 0.01            # ...and it took the duration asked for


def test_one_huge_gap_cannot_freeze_the_screen() -> None:
    # A single 40-minute stage (one big LLM call, an npm install) scaled proportionally would eat
    # the whole replay. Gaps are capped, then re-normalized so the total is still correct.
    events = [
        Event(t=0.0, stream="out", text="a\n"),
        Event(t=1.0, stream="out", text="b\n"),
        Event(t=2400.0, stream="out", text="c\n"),   # 40 min later
    ]
    delays = pace(events, duration=60.0)
    assert abs(sum(delays) - 60.0) < 0.01
    assert max(delays) <= 60.0                      # no single pause dominates the replay
    assert delays[1] > 0                            # the short gap is still visible


def test_same_instant_events_are_spread_evenly() -> None:
    events = [Event(t=5.0, stream="out", text=f"{i}\n") for i in range(4)]
    delays = pace(events, duration=8.0)
    assert abs(sum(delays) - 8.0) < 0.01


def test_pace_of_empty_input_is_empty() -> None:
    assert pace([], duration=10.0) == []


def test_highlights_keep_stage_banners_and_drop_noise() -> None:
    events = [
        Event(t=0.0, stream="err", text="INFO ================ AGENT: Code Generator ===========\n"),
        Event(t=1.0, stream="err", text="INFO    -> generating source files for work item X\n"),
        Event(t=2.0, stream="err", text="INFO HTTP Request: POST https://api... 200 OK\n"),
        Event(t=3.0, stream="err", text="INFO [finalize] PR ready: https://github.com/o/r/pull/2\n"),
    ]
    kept = [e.text for e in filter_highlights(events)]
    assert any("AGENT: Code Generator" in t for t in kept)
    assert any("[finalize] PR ready" in t for t in kept)
    assert not any("HTTP Request" in t for t in kept)


def test_banner_always_says_it_is_a_replay_of_a_recording() -> None:
    # Non-negotiable: the audience must never be shown a recording framed as a live build.
    manifest = {"project": "resources-app", "started_at": "2026-07-24 01:00:00 UTC",
                "ended_at": "2026-07-24 04:30:00 UTC", "duration_seconds": 12600}
    banner = format_banner(manifest, event_count=900, replay_seconds=150, highlights=False)
    assert "REPLAY of a RECORDED pipeline run - not a live build" in banner
    assert "resources-app" in banner
    assert "3h 30m" in banner        # the real duration is stated, not hidden
    assert banner.isascii()          # Windows consoles mangle non-ASCII banners


def test_artifacts_panel_lists_the_real_outputs() -> None:
    panel = format_artifacts({"artifacts": {"repo_url": "https://github.com/o/r",
                                            "pull_request": "https://github.com/o/r/pull/2",
                                            "package_zip": "reports/r/r.zip"}})
    assert "https://github.com/o/r/pull/2" in panel
    assert "reports/r/r.zip" in panel


def test_manifest_records_artifacts_and_duration(tmp_path: Path) -> None:
    with RunTranscript(tmp_path, project="proj-2") as tr:
        print("x")
        tr.finish(artifacts_from_state({
            "workflow_status": "completed",
            "generated_code": ["a", "b", "c"],
            "unit_tests": ["t1"],
            "repo_url": "https://github.com/o/r",
            "pr_url": "https://github.com/o/r/pull/2",
            "git_token": "ghp_should_never_be_copied_into_a_manifest",
        }))

    manifest = load_manifest(tr.dir)
    assert manifest["project"] == "proj-2"
    assert manifest["event_count"] >= 1
    assert manifest["duration_seconds"] >= 0
    artifacts = manifest["artifacts"]
    assert artifacts["files_generated"] == 3 and artifacts["unit_tests_written"] == 1
    assert artifacts["pull_request"] == "https://github.com/o/r/pull/2"
    # artifacts_from_state copies only counts/paths — never a credential-bearing field
    assert not any("token" in k for k in artifacts)
    assert "ghp_should_never" not in json.dumps(manifest)


def test_artifacts_from_state_omits_empty_fields() -> None:
    artifacts = artifacts_from_state({"workflow_status": "completed", "repo_url": "", "pr_url": None})
    assert artifacts == {"workflow_status": "completed"}


def test_manifest_is_written_even_when_the_run_crashed(tmp_path: Path) -> None:
    # run_fixture calls finish() in a finally: a crashed 5-hour run must still be replayable.
    try:
        with RunTranscript(tmp_path, project="boom") as tr:
            print("started work")
            raise RuntimeError("pipeline exploded")
    except RuntimeError:
        pass
    finally:
        tr.finish({"workflow_status": "crashed"})

    assert (tr.dir / MANIFEST_NAME).is_file()
    assert [e.text for e in load_events(tr.dir)] == ["started work\n"]
    assert load_manifest(tr.dir)["artifacts"]["workflow_status"] == "crashed"
