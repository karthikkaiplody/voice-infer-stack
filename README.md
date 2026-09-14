# Where Did My 800 Milliseconds Go?

A local voice agent you can run on your own machine and watch, stage by stage,
while it answers you. It is a companion to a talk at the
[IEEE RTC Conference 2026](https://rtc-conference.com) (Real Time
Communications, Illinois Tech, AI in RTC track), and it exists to make one
claim tangible:

> Building a low-latency voice agent is not only a model problem. It is a
> real-time systems problem.

Four stages in a chain, and the dependencies between them:

```
  you speak ─► voice activity ─► endpointing ─► speech to text
                                                     │
                       playback ◄─ text to speech ◄─ language model
```

Everything is local: [Pipecat](https://github.com/pipecat-ai/pipecat) as the
orchestrator, Whisper, Ollama and Kokoro as the stages. No API keys, nothing
leaves the machine.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="diagrams/slides/cascade-pipeline.clean.dark.png">
  <img src="diagrams/slides/cascade-pipeline.clean.light.png" alt="The default stack, stage by stage: microphone frames through Silero VAD and endpointing, Whisper, an Ollama language model, and Kokoro speech synthesis.">
</picture>

*The default stack, stage by stage. Every box is swappable from the
environment — see [Part 1 · Swap and see](docs/1-swap-and-see.md).*

**This is not a benchmark.** There are no leaderboards, no "X is faster than
Y", and no claim about anyone's stack. The numbers you get are yours, from your
hardware, and the point is the *shape* of them.

## Start here

Three commands, in order. Each one teaches something the previous one could not.

```bash
python3 budget.py          # 1. read a recorded turn. No install. No models.
make setup                 # 2. deps and model weights (once, ~4 GB)
make live                  # 3. talk to it, and watch the latency arrive
```

**1 — where the time goes, with nothing installed.** Not "without installing
models": without installing *anything*. The traces are committed, and
`budget.py`, `analysis.py` and `viewer.py` are pure standard library, tested on
the Python 3.9 that ships with macOS.

```
  BUDGET  02-medium  ·  file  ·  median of 3 runs: 1671 ms
  ------------------------------------------------------------------
  stage                    ms    share
  turn_detection         1102      66%   waiting, not computing
  stt                    1042      62%   cannot overlap (segmented)
  llm                     218      13%
  tts                     350      21%

  against the 800 ms human window: 2.1x over
```

The first row is the one to sit with. It burns no FLOPs, it is two config
constants, and it is the largest line in that budget by a distance. The shares
add up to more than 100% because the stages overlap; that is the second thing
to sit with.

Those are one machine's numbers, on one utterance, and they are not yours.

**2 — install.** Needs [uv](https://docs.astral.sh/uv/),
[Ollama](https://ollama.com) and ffmpeg. Everything else downloads on first run.

**3 — watch it happen.** `make live`, then open http://localhost:8080, press
start and ask it something. The page draws the turn as a waterfall while it
runs: when your turn was declared over, when the transcript existed, when the
first token arrived, when the first audio sample existed.

Every bar on that page is an OpenTelemetry span, streamed to the browser as
Pipecat emits it and drawn at its real start and end. They are the same spans
written to `artifacts/live-traces.jsonl`, so when the conversation is over:

```bash
uv run python budget.py --traces artifacts/live-traces.jsonl
```

prints the budget for the turn you just watched. The page and the number cannot
disagree, because there is only one measurement.

**No microphone?** The same pipeline runs from a WAV file, at real 20 ms
cadence: `make trace`. Details in [Part 1](docs/1-swap-and-see.md).

**Wear headphones.** Microphone and speakers on one machine with no acoustic
echo cancellation means the agent hears itself: its own voice trips voice
activity detection and its own words come back through speech-to-text as your
next question. The pipeline mutes the microphone while the bot speaks, which
stops the loop, but headphones remove the problem rather than managing it.

**If nothing happens, watch the input meter**, not the waterfall. It is drawn
with Pipecat's own volume metric, the same number the VAD compares against
`min_volume`, and the red line is where that gate sits. A bar that never moves
is the wrong microphone (`make devices`); a bar that moves but never crosses
the gate is a threshold your room needs tuned.

## The guide, in order

Four short parts. Each stands alone; together they are the talk, written down.

| Part | What it teaches |
|---|---|
| [1 · Swap and see](docs/1-swap-and-see.md) | Every component is one environment variable. Three guided swaps, each moving the budget in a different way. |
| [2 · What to notice](docs/2-what-to-notice.md) | The three insights: the biggest line item does no computing, you cannot just turn it down, and two default timeouts can cost more than any model. |
| [3 · How the measurement works](docs/3-measurement.md) | The rules that keep the numbers honest, and how to point `budget.py` at your own agent. |
| [4 · From here to production](docs/4-to-production.md) | The map from this teaching stack to the other eighty percent: barge-in, networks, fleets, evals. |

## What each file does

| File | |
|---|---|
| `agent.py` | The pipeline. Which stages, in what order, and what decides your turn is over. Used by both ways of running it. |
| `live.py` | The server behind `make live`: the microphone, and the spans on their way to the browser. |
| `ui/` | The page. Plain HTML, CSS and one JS file, no build step. |
| `config.py` | Every model, rate, prompt and threshold, in one frozen object. |
| `factory.py` | Builds the four stages from config. The seam that makes components swappable. |
| `tracing_setup.py` | OpenTelemetry wiring, the JSONL exporter, and the two spans Pipecat does not emit. |
| `budget.py` | Reads a trace file, prints the per-stage budget. Standard library only. |
| `analysis.py` | Span interval maths: overlap, union, median. Standard library only. |
| `viewer.py` | Renders one recorded turn as a standalone HTML waterfall. Standard library only. |
| `wav_transport.py` | Feeds a WAV into Pipecat at real 20 ms cadence, for running without a microphone. |
| `record.py`, `make_fixtures.py` | Record your own utterance, or generate one locally. |
| `test_measurements.py` | Invariants that catch a number that is quietly wrong. |
| `Makefile` | Every entry point: `make live`, `make trace`, `make budget`, `make viewer`. |
| `docs/` | The four-part guide above. |
| `SPANS.md` | The span contract `budget.py` reads. What to emit for your own agent. |
| `fixtures/`, `artifacts/` | Synthetic utterances, and the recorded turns everyone can read. |
| `diagrams/` | The diagrams in this README and the guide, as hand-drawn HTML plus the script that renders them. |
| `warm.py` | Pre-downloads model weights so the first run is not a cold surprise. |

## Setup notes

Apple Silicon is the tested path (MLX Whisper, CoreML, MPS). Elsewhere, set
`VOICE_STT_ENGINE=faster-whisper` and expect different absolute numbers. The
shape should hold. The numbers will not.

## Licence

MIT.
