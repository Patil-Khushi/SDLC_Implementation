# Demoing the pipeline in 2 minutes (record at night, replay in the room)

A real end-to-end run takes **3–6 hours** — 59 work items, ~130 LLM calls, a Docker review sandbox,
npm installs, real test runs, a GitHub push per feature. That cannot be performed live in front of
an audience.

So the run happens for real ahead of time and is **recorded**; in the room you replay it in about
two minutes. The work is genuine — the repo, pull request, reports and zip the replay points at are
the real outputs of that run. Only the waiting is removed.

The replay is **always labelled as a replay**, with the recorded run's identity, when it ran, and its
real duration. Do not present it as a live build; the honest framing is also the more impressive
one — "this ran for three and a half hours last night, here is what it did."

---

## 1. The night before — run it for real

Recording is **on by default**. Nothing extra to remember:

```powershell
./.venv/Scripts/python.exe scripts/run_fixture.py fixtures/resources --project vp-demo -y
```

When it finishes it prints where the recording landed:

```
[record] transcript: ...\SDLC_Implementation\run-transcripts\vp-demo
[record] replay it in ~150s with:  python scripts/demo_replay.py --project vp-demo
```

Notes:

- Each run writes `run-transcripts/<project>/transcript.jsonl` (one line per output line) and
  `manifest.json` (real duration + artifact pointers: repo URL, PR URL, zip, report paths).
- The manifest is written in a `finally`, so **even a crashed or Ctrl-C'd run is replayable** — it
  simply ends where the run ended.
- Tokens are scrubbed from the transcript before it touches disk, so the file is safe to copy to a
  demo laptop.
- `--no-record` opts out.

**Check the recording before you go to bed** — five seconds now beats a surprise in the room:

```powershell
./.venv/Scripts/python.exe scripts/demo_replay.py --list
./.venv/Scripts/python.exe scripts/demo_replay.py --project vp-demo --duration 20 --highlights
```

## 2. In the room — one command

```powershell
./.venv/Scripts/python.exe scripts/demo_replay.py --project vp-demo
```

Useful variations:

| Command | Effect |
| --- | --- |
| `--duration 90` | fit a tighter slot (default 150s) |
| `--highlights` | only stage banners + each agent's headline outcome — calmest narrative for a room |
| `--list` | show what has been recorded |
| `--project <name>` | pick a specific run (default: the most recent recording) |

Pacing keeps the *shape* of the real run — code generation is visibly the long stretch — but no
single pause exceeds ~1.2s, so the screen never appears frozen. `Ctrl-C` stops it cleanly and still
prints the artifacts panel.

## 3. Suggested 3-minute narrative

1. **Open with the artifacts, not the logs.** Have the GitHub repo, its `dev → main` pull request,
   and the zip open in tabs first: "this is what the pipeline produced unattended last night."
2. **Then run the replay** (`--highlights`, `--duration 120`) and narrate the stages as they scroll:
   plan → code generation per work item → live push per feature → Code Review in a sandbox →
   Refactoring applying the findings → Debugging → Unit Tests → Documentation → Security →
   the pull request → the zip.
3. **Close on the closing panel** — it lists the real repo, PR, reports and zip.
4. If asked "is this live?", answer plainly: it is a replay of last night's real run, because the
   real thing takes hours. Offer to start a live run in the background and show the first work item
   being generated and pushed — a live run reaches its first pushed feature within a few minutes.

## Files

| Path | Role |
| --- | --- |
| `app/services/run_transcript.py` | recorder (stdout/stderr tee, scrubbing, manifest) + replay engine (pacing, highlights, banner) |
| `scripts/demo_replay.py` | the CLI you run in the room |
| `scripts/run_fixture.py` | records automatically (`--no-record`, `--transcript-dir` to change) |
| `run-transcripts/<project>/` | one recording per run |

## Why not cache the LLM calls instead?

Replaying at the LLM layer (recording prompts/responses and serving them from a cassette) would
still execute everything else — file writes, git pushes, the Docker review sandbox, `npm install`,
the real test suite. Those dominate the wall clock, so a "cached" run would still take a long while
and could still fail in the room on Docker or the network. Recording the run's output makes the
demo both fast and deterministic. A cassette layer is worth building for cheap *testing*, but it is
the wrong tool for a live presentation.
