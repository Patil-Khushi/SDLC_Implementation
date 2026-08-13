"""Replay a RECORDED pipeline run in ~2 minutes — the demo front end for a 3-6 hour build.

The real run cannot be performed live (59 work items, ~130 LLM calls, Docker review sandbox, npm
installs, real test runs). So run it for real beforehand — ``scripts/run_fixture.py`` records every
line it prints — and then run THIS script in the room. It re-emits that run's own output, paced to
the length you ask for, and ends with a panel pointing at the real artifacts (GitHub repo, pull
request, reports, zip).

It is labelled a replay throughout: the header states the recorded run's identity, when it ran, and
its real duration. The work is genuine; the three minutes are not the work.

Usage (from services/implementation/):

    # newest recorded run, ~2.5 minutes
    python scripts/demo_replay.py

    # a specific run, tighter, banners+outcomes only (calmest narrative for a room)
    python scripts/demo_replay.py --project resources-app-0724 --duration 90 --highlights

    # list what has been recorded
    python scripts/demo_replay.py --list
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

_IMPL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_IMPL_DIR))

from app.services.run_transcript import (  # noqa: E402
    DEFAULT_REPLAY_SECONDS,
    MANIFEST_NAME,
    TRANSCRIPT_NAME,
    filter_highlights,
    format_artifacts,
    format_banner,
    load_events,
    load_manifest,
    replay,
    safe_write,
)

DEFAULT_TRANSCRIPT_DIR = _IMPL_DIR / "run-transcripts"

# The real streams, captured at import: the replay must reach the terminal even if something in
# the process has swapped sys.stdout (e.g. a recorder tee) since.
_STDOUT, _STDERR = sys.stdout, sys.stderr


def _recorded_runs(root: Path) -> list[Path]:
    """Recorded run directories, newest transcript first."""
    if not root.is_dir():
        return []
    runs = [d for d in root.iterdir() if d.is_dir() and (d / TRANSCRIPT_NAME).is_file()]
    return sorted(runs, key=lambda d: (d / TRANSCRIPT_NAME).stat().st_mtime, reverse=True)


def _write(stream: str, text: str) -> None:
    safe_write(_STDERR if stream == "err" else _STDOUT, text)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--project", default=None,
                        help="recorded run to replay (default: the most recently recorded one)")
    parser.add_argument("--transcript-dir", type=Path, default=DEFAULT_TRANSCRIPT_DIR,
                        help=f"where run_fixture.py wrote its recordings (default: {DEFAULT_TRANSCRIPT_DIR})")
    parser.add_argument("--duration", type=float, default=DEFAULT_REPLAY_SECONDS,
                        help=f"wall-clock seconds the replay should take (default: {DEFAULT_REPLAY_SECONDS:.0f})")
    parser.add_argument("--highlights", action="store_true",
                        help="show only stage banners + each agent's headline outcome (calmer for a room)")
    parser.add_argument("--list", action="store_true", dest="list_runs",
                        help="list the recorded runs available to replay, then exit")
    args = parser.parse_args()

    runs = _recorded_runs(args.transcript_dir)

    if args.list_runs:
        if not runs:
            print(f"No recorded runs under {args.transcript_dir}.")
            return 1
        print(f"Recorded runs under {args.transcript_dir} (newest first):\n")
        for run in runs:
            manifest = load_manifest(run)
            status = (manifest.get("artifacts") or {}).get("workflow_status", "?")
            print(f"  {run.name:38} {manifest.get('started_at', '?'):24} "
                  f"status={status} events={manifest.get('event_count', '?')}")
        return 0

    if args.project:
        target = args.transcript_dir / args.project
        if not (target / TRANSCRIPT_NAME).is_file():
            print(f"No recording for --project {args.project!r} (looked for "
                  f"{target / TRANSCRIPT_NAME}).", file=sys.stderr)
            print("Available: " + (", ".join(r.name for r in runs) or "(none)"), file=sys.stderr)
            return 1
    elif runs:
        target = runs[0]
    else:
        print(f"No recorded runs under {args.transcript_dir} — nothing to replay.", file=sys.stderr)
        print("Record one first:  python scripts/run_fixture.py <pack> --project <name> -y",
              file=sys.stderr)
        return 1

    events = load_events(target)
    if not events:
        print(f"{target / TRANSCRIPT_NAME} has no usable events.", file=sys.stderr)
        return 1
    if args.highlights:
        events = filter_highlights(events) or events  # never leave the room with a blank screen

    manifest = load_manifest(target)
    if not manifest:
        # A transcript without its manifest still replays; only the header/artifacts get thinner.
        manifest = {"project": target.name}
        print(f"(note: {MANIFEST_NAME} missing for {target.name} — replaying without run metadata)",
              file=sys.stderr)

    _write("out", format_banner(manifest, event_count=len(events),
                                replay_seconds=args.duration, highlights=args.highlights))
    started = time.monotonic()
    try:
        replay(events, duration=args.duration, write=_write)
    except KeyboardInterrupt:
        _write("out", "\n\n(replay stopped)\n")
    _write("out", format_artifacts(manifest))
    _write("out", f"(replayed {len(events)} event(s) in {time.monotonic() - started:.0f}s; "
                  f"the recorded run itself took {manifest.get('duration_seconds', '?')}s)\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
