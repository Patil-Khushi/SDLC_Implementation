"""Record a pipeline run's terminal output, then replay it in minutes instead of hours.

Why this exists: a real end-to-end run takes 3-6 hours (59 work items, ~130 LLM calls, Docker
sandbox review, npm installs, real test runs). That cannot be performed live in front of an
audience. So the run happens for real ahead of time and is RECORDED here; the replay player then
re-emits exactly what that run printed, compressed into a couple of minutes.

What is recorded is the run's own stdout/stderr — not a re-simulation. Recording at the STREAM
level rather than the logging level is deliberate: the visible run is a mix of ``logging`` output
(agent progress, on stderr) and bare ``print()`` (the build-plan table, the shared-state dump, the
publish/review footer, on stdout). A logging handler would silently miss half of it, and would also
bake in a dependency on today's log format. Teeing both streams captures the terminal verbatim.

The replay is labelled as a replay by :func:`format_banner` — always. This is a recording of real
work (the repo, PR, reports and zip it references are the real artifacts of the recorded run), and
presenting it as a live build would misrepresent it.

Layout written per run (``<transcript-dir>/<project>/``)::

    transcript.jsonl   one JSON event per line: {"t": seconds-since-start, "s": "out"|"err", "x": text}
    manifest.json      project, timestamps, original duration, event count, artifact pointers
"""

from __future__ import annotations

import io
import json
import re
import sys
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

TRANSCRIPT_NAME = "transcript.jsonl"
MANIFEST_NAME = "manifest.json"

#: Default wall-clock length of a replay. Short enough to hold a room, long enough that the stage
#: banners are readable as they pass.
DEFAULT_REPLAY_SECONDS = 150.0

#: No single pause may exceed this, however long the real gap was. One work item can take minutes
#: (a large LLM call) and an npm install longer; scaled proportionally those would still dominate
#: and leave the screen frozen. Capping keeps the replay moving, and :func:`pace` then re-normalizes
#: so the total still lands on the requested duration.
MAX_GAP_SECONDS = 1.2

#: Credential shapes scrubbed from the transcript before it touches disk. The state dump already
#: redacts secret-looking FIELDS, but a transcript is a file that gets copied onto a demo machine
#: and pasted into chats, so anything token-shaped is removed wherever it appears.
_SECRET_RE = re.compile(
    r"gh[pousr]_[A-Za-z0-9]{16,}"           # gh CLI / classic PATs (ghp_, gho_, ghu_, ghs_, ghr_)
    r"|github_pat_[A-Za-z0-9_]{20,}"        # fine-grained PATs
    r"|x-access-token:[^@\s]+"              # token embedded in a clone/push URL
)

#: Lines worth keeping in ``--highlights``: the stage banners and each agent's headline outcome.
_HIGHLIGHT_MARKERS = (
    "AGENT:", "   -> ", "[plan]", "[code_generator]", "[publish]", "[gate]", "[commit]",
    "[code_review]", "[refactoring]", "[debug", "[unit_test", "[documentation]", "[security]",
    "[finalize]", "[package]", "BUILD PLAN", "SHARED STATE", "workflow_status", "===",
)

#: ...minus the per-item CHATTER that shares those prefixes. Without this the include-list alone is
#: no filter at all: measured on a run-shaped transcript it kept 927 of 927 lines, because every
#: "[FILE 3/22] generating ..." line also carries "[code_generator]". A 2-minute narrative wants one
#: line per work item, not one per file.
_HIGHLIGHT_NOISE = (
    "[FILE ",            # per-file progress within one work item
    "[BOILERPLATE]",     # which context slices were assembled
    "HTTP Request",      # httpx per-call logging
    "Ignoring path",     # semgrep/eslint walker noise
    "none matching by path",
    "not produced",
)


def is_highlight(text: str) -> bool:
    """Whether a line belongs in the condensed (``--highlights``) narrative."""
    if any(noise in text for noise in _HIGHLIGHT_NOISE):
        return False
    return any(marker in text for marker in _HIGHLIGHT_MARKERS)


@dataclass(frozen=True)
class Event:
    """One chunk of output: ``t`` seconds after the run started, on stdout (``out``) or stderr."""

    t: float
    stream: str
    text: str


def scrub(text: str) -> str:
    """Replace anything token-shaped with a placeholder (see :data:`_SECRET_RE`)."""
    return _SECRET_RE.sub("<redacted>", text)


class _Tee(io.TextIOBase):
    """Write-through wrapper: everything still reaches the real terminal, and is also recorded.

    Only ``write``/``flush``/``isatty`` are overridden; the run must look and behave exactly as it
    does without recording (progress bars and colour depend on ``isatty`` being honest).
    """

    def __init__(self, original: TextIO, sink: Callable[[str], None]) -> None:
        self._original = original
        self._sink = sink

    def write(self, text: str) -> int:
        written = self._original.write(text)
        if text:
            self._sink(text)
        return written

    def flush(self) -> None:
        self._original.flush()

    def isatty(self) -> bool:
        return bool(getattr(self._original, "isatty", lambda: False)())

    @property
    def encoding(self) -> str:  # some libraries inspect this before writing
        return getattr(self._original, "encoding", "utf-8")

    def writable(self) -> bool:
        return True


class RunTranscript:
    """Context manager that tees stdout+stderr into ``<out_dir>/<project>/transcript.jsonl``.

    Enter it BEFORE ``logging.basicConfig``: a ``StreamHandler`` captures ``sys.stderr`` by value at
    configure time, so configuring logging first would leave the agents' progress lines untee'd.

    Never raises into the run: recording is a demo convenience, and a full disk or a locked file
    must not take down a 5-hour build (:attr:`error` records why it stopped, if it did).
    """

    def __init__(self, out_dir: Path | str, *, project: str) -> None:
        self.dir = Path(out_dir) / _slug(project)
        self.project = project
        self.error: str = ""
        self.event_count = 0
        self._fh: TextIO | None = None
        self._start = 0.0
        self._started_at = ""
        self._saved: tuple[TextIO, TextIO] | None = None

    # -- recording ------------------------------------------------------------

    def __enter__(self) -> "RunTranscript":
        self.dir.mkdir(parents=True, exist_ok=True)
        self._fh = (self.dir / TRANSCRIPT_NAME).open("w", encoding="utf-8", newline="")
        self._start = time.monotonic()
        self._started_at = _now_iso()
        self._saved = (sys.stdout, sys.stderr)
        sys.stdout = _Tee(sys.stdout, lambda text: self._record("out", text))  # type: ignore[assignment]
        sys.stderr = _Tee(sys.stderr, lambda text: self._record("err", text))  # type: ignore[assignment]
        return self

    def __exit__(self, *_exc: object) -> None:
        if self._saved is not None:
            sys.stdout, sys.stderr = self._saved
            self._saved = None
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    def _record(self, stream: str, text: str) -> None:
        if self._fh is None:
            return
        try:
            line = json.dumps(
                {"t": round(time.monotonic() - self._start, 3), "s": stream, "x": scrub(text)}
            )
            self._fh.write(line + "\n")
            self._fh.flush()  # a run that dies mid-way must still leave a replayable transcript
            self.event_count += 1
        except Exception as exc:  # noqa: BLE001 - recording must never break the run it observes
            self.error = f"{type(exc).__name__}: {exc}"
            self._fh = None

    def finish(self, artifacts: dict[str, Any] | None = None) -> Path:
        """Write ``manifest.json`` (duration + artifact pointers) and return its path."""
        payload = {
            "project": self.project,
            "started_at": self._started_at,
            "ended_at": _now_iso(),
            "duration_seconds": round(time.monotonic() - self._start, 1),
            "event_count": self.event_count,
            "recording_error": self.error,
            "artifacts": artifacts or {},
        }
        path = self.dir / MANIFEST_NAME
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return path


# -- replay -------------------------------------------------------------------


def resolve_transcript(target: Path | str) -> Path:
    """Accept either a run directory or the ``transcript.jsonl`` itself."""
    path = Path(target)
    return path / TRANSCRIPT_NAME if path.is_dir() else path


def coalesce_lines(events: Iterable[Event]) -> list[Event]:
    """Regroup raw write chunks into ONE event per output line.

    ``print("x")`` reaches the tee as two writes — the text, then ``"\\n"`` — and a logging record
    likewise. Left as chunks, ``--highlights`` drops the bare newline that belonged to a kept line
    and the output runs together on one row, while pacing spends its budget on newlines. Each line
    is timestamped when it COMPLETED, which is when a viewer actually saw it.
    """
    out: list[Event] = []
    buffer = {"out": "", "err": ""}
    for event in events:
        stream = event.stream if event.stream in buffer else "out"
        buffer[stream] += event.text
        while "\n" in buffer[stream]:
            line, _, rest = buffer[stream].partition("\n")
            out.append(Event(t=event.t, stream=stream, text=line + "\n"))
            buffer[stream] = rest
    for stream, remainder in buffer.items():
        if remainder:  # a run killed before its last newline still shows that partial line
            out.append(Event(t=out[-1].t if out else 0.0, stream=stream, text=remainder))
    out.sort(key=lambda e: e.t)  # stable: interleaves stdout/stderr as they appeared
    return out


def load_events(target: Path | str, *, as_lines: bool = True) -> list[Event]:
    """Read a transcript, skipping any truncated/garbled trailing line from a killed run."""
    events: list[Event] = []
    with resolve_transcript(target).open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                events.append(Event(t=float(obj["t"]), stream=str(obj["s"]), text=str(obj["x"])))
            except (ValueError, KeyError, TypeError):
                continue  # a partial last line (run killed mid-write) is not a reason to fail
    return coalesce_lines(events) if as_lines else events


def load_manifest(target: Path | str) -> dict[str, Any]:
    path = resolve_transcript(target).parent / MANIFEST_NAME
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {}


def filter_highlights(events: Sequence[Event]) -> list[Event]:
    """Keep only stage banners and headline outcomes (see :func:`is_highlight`)."""
    return [e for e in events if is_highlight(e.text)]


def pace(events: Sequence[Event], *, duration: float = DEFAULT_REPLAY_SECONDS,
         max_gap: float = MAX_GAP_SECONDS) -> list[float]:
    """Delays to sleep BEFORE each event so the replay lasts ``duration`` seconds.

    Two competing goals: the replay must last as long as asked, and no single pause may exceed
    ``max_gap`` (one work item's LLM call can be minutes; scaled proportionally it would freeze the
    screen mid-demo). So the real gaps are scaled — keeping the shape of the run, generation stays
    visibly the long stretch — then clamped, and the leftover budget is redistributed into the gaps
    that still have room UNDER the cap. Scaling the clamped values by a single factor instead (the
    obvious approach) pushes them back over the cap: measured 2.18s against a 1.2s cap.

    Both properties hold on exit: ``sum(...) == duration`` and ``max(...) <= max_gap`` — as long as
    ``duration <= max_gap * (len(events) - 1)``, i.e. there are enough gaps to hold the time. When
    there are not, the cap wins and the replay simply runs shorter than requested.
    """
    if not events:
        return []
    # The first event is emitted immediately, so the run's length is carried by the n-1 gaps
    # BETWEEN events — dividing by n instead would make every replay finish early.
    def _even() -> list[float]:
        if len(events) == 1:
            return [0.0]
        return [0.0] + [min(duration / (len(events) - 1), max_gap)] * (len(events) - 1)

    span = events[-1].t - events[0].t
    gaps = [0.0] + [max(0.0, events[i].t - events[i - 1].t) for i in range(1, len(events))]
    if span <= 0:  # everything landed in the same instant — spread it evenly instead
        return _even()
    delays = [min(g * (duration / span), max_gap) for g in gaps]
    total = sum(delays)
    if total <= 0:
        return _even()
    if total > duration:  # clamping cannot cause this, but scaling DOWN is always safe
        return [d * (duration / total) for d in delays]

    # Fill the deficit proportionally to each gap's remaining headroom, so the cap is respected by
    # construction and the relative pacing of the run is preserved.
    room = [max_gap - d for d in delays[1:]]  # index 0 is the immediate first event
    available = sum(room)
    deficit = duration - total
    if deficit <= 0 or available <= 0:
        return delays
    share = min(1.0, deficit / available)
    return [delays[0]] + [d + r * share for d, r in zip(delays[1:], room)]


def format_banner(manifest: dict[str, Any], *, event_count: int, replay_seconds: float,
                  highlights: bool) -> str:
    """The always-on header that says this is a replay of a recorded run, and of which one."""
    original = float(manifest.get("duration_seconds") or 0.0)
    bar = "=" * 78
    lines = [
        bar,
        "  REPLAY of a RECORDED pipeline run - not a live build",
        bar,
        f"  project        : {manifest.get('project', '(unknown)')}",
        f"  recorded       : {manifest.get('started_at', '(unknown)')} -> {manifest.get('ended_at', '(unknown)')}",
        f"  real duration  : {_human(original)}",
        f"  replaying      : {event_count} output event(s) in ~{_human(replay_seconds)}"
        + ("  [highlights only]" if highlights else ""),
        "  artifacts below are the REAL outputs of that run (repo, PR, reports, zip).",
        bar,
        "",
    ]
    return "\n".join(lines)


def format_artifacts(manifest: dict[str, Any]) -> str:
    """Closing panel: where the recorded run's real outputs live."""
    artifacts = manifest.get("artifacts") or {}
    bar = "=" * 78
    lines = ["", bar, "  ARTIFACTS OF THE RECORDED RUN", bar]
    if not artifacts:
        lines.append("  (none recorded)")
    for key, value in artifacts.items():
        if value in (None, "", [], {}):
            continue
        lines.append(f"  {key:22} : {value}")
    lines += [bar, ""]
    return "\n".join(lines)


def replay(
    events: Sequence[Event],
    *,
    duration: float = DEFAULT_REPLAY_SECONDS,
    write: Callable[[str, str], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Re-emit ``events`` paced over ``duration``. Returns how many were emitted.

    ``write``/``sleep`` are injected by tests (and by the CLI, to reach the real streams rather
    than whatever tee may be installed).
    """
    emit = write or _default_write
    for event, delay in zip(events, pace(events, duration=duration)):
        if delay > 0:
            sleep(delay)
        emit(event.stream, event.text)
    return len(events)


def safe_write(target: TextIO, text: str) -> None:
    """Write ``text``, degrading unencodable characters instead of raising.

    A recorded run's output contains em dashes and arrows; a Windows console on cp1252 cannot encode
    them. The live run gets away with it (Python replaces them on the way out), but a replay that
    raised UnicodeEncodeError would abort the demo mid-sentence — so encode defensively here.
    """
    try:
        target.write(text)
    except UnicodeEncodeError:
        encoding = getattr(target, "encoding", "ascii") or "ascii"
        target.write(text.encode(encoding, errors="replace").decode(encoding, errors="replace"))
    target.flush()


def _default_write(stream: str, text: str) -> None:
    safe_write(sys.stderr if stream == "err" else sys.stdout, text)


# -- helpers ------------------------------------------------------------------


def _slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", value) or "run"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _human(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    if seconds < 90:
        return f"{seconds:.0f}s"
    minutes, secs = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m {secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def artifacts_from_state(state: dict[str, Any], *, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Pull the run's real outputs off the final ``WorkflowState`` for the manifest.

    Deliberately counts/paths only — never state values that could carry a credential.
    """
    out: dict[str, Any] = {
        "workflow_status": state.get("workflow_status"),
        "files_generated": len(state.get("generated_code", []) or []),
        "unit_tests_written": len(state.get("unit_tests", []) or []),
        "repo_url": state.get("repo_url") or "",
        "pull_request": state.get("pr_url") or "",
        "finalize_status": state.get("finalize_status") or "",
        "package_zip": state.get("package_path") or "",
        "review_report": state.get("review_report_path") or "",
        "refactoring_report": state.get("refactoring_report_path") or "",
        "security_report": state.get("security_report_path") or "",
    }
    out.update(extra or {})
    return {k: v for k, v in out.items() if v not in (None, "", 0, [], {})}
