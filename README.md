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

## Without a microphone

Same pipeline, same stages, same spans, fed from a WAV file at real 20 ms
cadence:

```bash
make trace                 # or: make trace FIXTURE=03-trailing-pause
```

It runs the utterance twice, says which run was cold, and prints the budget.
Record your own instead:

```bash
make record NAME=my-question SECONDS=7
make trace FIXTURE=my-question
```

Speak, then **stop talking and let the recording run on** for a second. That
trailing silence is not waste; it is what the turn detector needs in order to
decide you have finished. The script measures where your speech actually ends,
so the silence never inflates the result, and it will tell you something
specific about how you talk:

```
  longest pause       260 ms
  needs stop_secs >=  0.26 s   (below this the agent will cut you off)
```

## Turn a knob, watch the shape change

Every component is chosen in `factory.py` and set from the environment, so a
swap is one variable, not an edit:

| Knob | Values | What it changes |
|---|---|---|
| `VOICE_STT_ENGINE` | `mlx`, `faster-whisper` | MLX is Apple Silicon only; faster-whisper runs anywhere |
| `VOICE_STT_MODEL` | any MLX Whisper id | `whisper-tiny` is ~15x faster than `large-v3-turbo-q4` here |
| `VOICE_TTS_ENGINE` | `kokoro`, `piper` | both local, both download on first use |
| `VOICE_LLM_MODEL` | any Ollama model | `llama3.2:1b`, `qwen2.5:0.5b`, … |
| `VOICE_VAD_STOP_SECS` | seconds | how long to wait in silence before deciding you are done |
| `VOICE_VAD_MIN_VOLUME` | 0–1 | how loud counts as speech. Machine-specific; see the meter |
| `VOICE_USER_SPEECH_TIMEOUT` | seconds | a second timer stacked on the first. See below |

```bash
VOICE_TTS_ENGINE=piper VOICE_LLM_MODEL=qwen2.5:0.5b make live
```

Every trace records the stack it ran on, so a result can never be separated
from the configuration that produced it:

```
stack: stt=mlx:whisper-tiny  llm=ollama:llama3.2:1b  tts=kokoro:af_heart
       vad=silero  endpoint=vad_timeout@0.4s
```

**What is not swappable, and why.** Silero is the only local VAD Pipecat ships,
so there is no second option to offer. Both Whisper engines are *segmented*:
neither emits a partial transcript while you are still speaking. That is a
property of the model, not the engine, and no swap in `factory.py` changes it.
It is also a large part of what hosted streaming speech-to-text actually sells
you.

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

## Three things worth noticing

### 1. The biggest line item does no computing

`stop_secs` is how long the agent waits in silence before deciding you have
finished speaking. It burns no FLOPs, it is a config constant, and most people
never open it. On the live page it is the first bar, and it usually starts
before anything else is allowed to run.

### 2. You cannot just turn it down

Below the length of a speaker's natural pause, the detector fires mid-sentence,
the agent answers a fragment, and the work is thrown away. Turning the timeout
down can make the turn *slower*, because a discarded generation costs more than
the wait it saved. The live page shows this directly: a stage that ran twice in
one turn is marked, because the first one was wasted. A captured example is in
[`artifacts/turn-detection/`](artifacts/turn-detection/) — read it with
`python3 viewer.py --traces artifacts/turn-detection/false-endpoint-traces.jsonl --open`.

### 3. Two default timeouts can cost more than any model

Pipecat closes a turn when **two** timers, started together, have both
finished:

| Timer | Default | What it is |
|---|---|---|
| `ttfs_p99_latency` | **1.0 s** | safety net for how long speech-to-text takes to return a final transcript. A conservative catch-all for local models, whose speed depends entirely on your hardware — hosted services ship measured values instead. Unset by default, so the fallback applies, and it logs a warning most people never read. |
| `user_speech_timeout` | **0.6 s** | policy window in which the user may resume speaking. A pause policy, not a network allowance. |

The safety net is short-circuited the moment speech-to-text flags a transcript
as final, so the full second is the worst case, not the design. But Whisper
tiny returns in about 60 ms locally, and the worst case is what an unflagged
transcript gets you: the pipeline waiting up to a second for something that
had already arrived. `make live` sets both honestly for a local stack; set
them back and watch the first row grow. (Pipecat will log that the STT wait
"collapsed to 0s". That is the point, not a bug.)

## Measurement notes

Voice latency is easy to measure wrongly. What this repo does:

- **One source of truth.** Everything that shows what happened and when reads
  OpenTelemetry spans. The live page, `budget.py` and `viewer.py` are three
  readers of one measurement, not three implementations of it.
- **t0 is the end of speech, not the end of the file**, and not the moment the
  detector noticed. Pipecat's `VADUserStoppedSpeakingFrame` carries both the
  moment it decided and the silence it had to hear first, so the end of speech
  is the difference between them.
- **Overlap is interval intersection, never a sum of durations.** Summing
  cannot tell concurrency apart from a stage that kept running after first
  audio.
- **Stages are clipped to the window.** Speech-to-text always begins while you
  are still talking; that part was free and is not charged to your wait. On the
  live page it is the hatched part of the bar, left of zero.
- **Real 20 ms cadence** when feeding a file. Pushing a whole WAV as one frame
  would make voice activity detection see something no microphone produces, and
  every turn-detection number would be fiction.
- **Warmup is named, not hidden.** The first inference pays model load and
  graph compilation and is several times slower.

`uv run pytest -q` checks these hold, against the committed traces, so it needs
no models. The tests are about the numbers rather than the plumbing.

## The other trace file

`artifacts/scheduling-comparison.jsonl` is a recording from earlier in this
repo's life: the same utterance run twice, once with the stages strictly
sequential and once overlapped. Nothing produces it any more and it is not a
claim about anything. It is kept because it is what `test_measurements.py` runs
against — one of the two runs overlaps its stages and the other does not, so
the interval maths has something it could get wrong.

```bash
python3 budget.py --traces artifacts/scheduling-comparison.jsonl
```

## Point it at your own agent

`budget.py` reads a JSONL file of OpenTelemetry spans and does not import the
pipeline. If your agent emits the span names in [`SPANS.md`](SPANS.md) — which
a Pipecat agent very nearly does already — it will read your traces too:

```bash
python3 budget.py --traces /path/to/your-traces.jsonl
```

## From this repo to production

Everything this repo leaves out is a decision, and each one has a production
counterpart. If you are here from the talk, this is the map from the four
boxes to the other eighty percent.

| Here (teaching scale) | Production (the other 80%) |
|---|---|
| The microphone is muted while the bot speaks, so the agent cannot be interrupted | Barge-in is a policy, not a boolean: when to stop playback, when to keep listening, and how to preserve the context that was interrupted. Track false-barge-in and missed-interruption rates — aggregate latency stays green while callers are being cut off |
| No network: microphone and speaker on one machine | Two network legs per turn, each with jitter, packet loss, a codec and a playout buffer. Budget them on both sides of the four stages |
| One turn at a time, one person, one laptop | Fleet dashboards: P95 turn latency per stage, WER drift, audio quality, cost per conversation |
| `uv run pytest -q` checks the measurement is honest | Eval suites: golden conversations re-run on every prompt, model or tool change |
| Spans read from a local JSONL file | The same spans over OTLP into Langfuse, Jaeger or Honeycomb. The span contract in [`SPANS.md`](SPANS.md) is the stable part; the OTel `gen_ai.*` attribute names are still Development-stability, so the contract here is deliberately the repo's own |

Descriptions of Pipecat behaviour in this repo were verified against Pipecat
1.8.1, September 2026. If a newer Pipecat disagrees, believe the newer one and
re-check the sections above.

## Setup notes

Apple Silicon is the tested path (MLX Whisper, CoreML, MPS). Elsewhere, set
`VOICE_STT_ENGINE=faster-whisper` and expect different absolute numbers. The
shape should hold. The numbers will not.

## Licence

MIT.
