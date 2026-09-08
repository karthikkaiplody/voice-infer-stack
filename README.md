# Where Did My 800 Milliseconds Go?

Two builds of the same local voice agent. Same models, same audio, same config.
The only difference is **when** each stage is allowed to start.

Companion repo for a talk at the [IEEE RTC Conference 2026](https://rtc-conference.com)
(Real Time Communications, Illinois Tech, AI in RTC track).

```
naive      turn_detection 562   stt 1020   llm 567   tts 1369            3562 ms
streaming  turn_detection [*]   stt 1125   llm 551   tts  436   -286     1813 ms
                                                      ▲          ▲
                                       full synthesis  │          │  llm/tts overlap
                                       vs first chunk  │          │
                                              -933 ms  ┘          └  -286 ms
```

Measured on an Apple M4 Pro, fully local, no API keys. **These are not your
numbers.** They are one machine, one utterance, one voice. The point of this
repo is the method, so you can get yours.

## See the numbers without installing anything

Not "without installing models" — without installing **anything**. The traces
are committed, and `budget.py` and `analysis.py` are pure standard library:

```bash
git clone https://github.com/karthikkaiplody/voice-infer-stack
cd voice-infer-stack
python3 budget.py
```

No virtualenv, no dependencies, no GPU, no Ollama, no model weights. Tested on
the Python 3.9 that ships with macOS. It prints where every millisecond of one
real recorded turn went.

That is deliberate. The pipeline is how the traces were made; the traces and the
analysis are the part worth reading.

## Run it yourself

```bash
make setup                          # deps + pull models (needs network, ~4 GB)
make bench                          # run both builds, write fresh traces
make budget                         # where did the time go
make clips                          # write the audio each build produces
```

Record your own voice and measure that instead:

```bash
uv run python record.py --list-devices
make record NAME=my-question SECONDS=7
make bench FIXTURE=my-question
make budget
```

Speak, then **stop talking and let the recording run on** for a second. That
trailing silence is not waste. It is what the turn detector needs in order to
decide you have finished, and the script measures where your speech actually
ends so the silence never inflates the result.

It will also tell you something specific about how you talk:

```
  longest pause       260 ms
  needs stop_secs >=  0.26 s   (below this the agent will cut you off)
```

## What each file does

| File | |
|---|---|
| `naive.py` | Build 1. Sequential, no framework. The version most people write first. |
| `streaming.py` | Build 2. The same models on a Pipecat pipeline that overlaps work. |
| `config.py` | Models, prompt, sample rates, `stop_secs`. Imported by **both** builds, so they cannot drift apart. |
| `wav_transport.py` | Feeds a WAV into Pipecat at real 20 ms cadence. See below. |
| `analysis.py` | Span interval maths. Overlap, union, median. |
| `budget.py` | Reads a trace file, prints the per-stage budget. |
| `bench.py` | Runs both builds with warmup and writes traces. |
| `clips.py` | Writes what each build sounds like, real silences included. |
| `record.py` | Record your own utterance and measure its endpoints. |
| `sweep.py` | Sweeps the VAD silence timeout. See "the knob" below. |
| `make_fixtures.py` | Regenerates the synthetic utterances with Kokoro. No API keys. |

## Three things this repo exists to show

### 1. The biggest line item does no computing

`stop_secs` is how long the agent waits in silence before deciding you have
finished speaking. On the sequential build it is 562 ms of a 3562 ms budget and
it burns no FLOPs. It is a config constant, and most people never open it.

### 2. You cannot just turn it down

Below the length of a speaker's natural pause, the detector fires mid-sentence,
the agent answers a fragment, and the work is thrown away. `make sweep`:

| fixture | pause | stop_secs | end-to-end | LLM calls | |
|---|---|---|---|---|---|
| 02-medium | 260 ms | 0.2 | 2233 ms | **2.00** | false endpoint |
| 02-medium | 260 ms | 0.3 | 1354 ms | 1.00 | clean |
| 02-medium | 260 ms | 0.8 | 1983 ms | 1.00 | clean |
| 03-trailing-pause | 420 ms | 0.3 | 2387 ms | **2.00** | false endpoint |
| 03-trailing-pause | 420 ms | 0.5 | 1730 ms | 1.00 | clean |

Turning the timeout down made it **slower**, because a discarded generation
costs more than the wait saved. Above that floor, latency tracks the timeout
roughly one to one. A captured trace of the failure is in
[`artifacts/turn-detection/`](artifacts/turn-detection/).

### 3. Streaming's win is mostly not overlap

Overlapping the LLM and TTS recovered 286 ms. Emitting TTS's first chunk instead
of synthesizing the whole reply recovered about 930 ms. Streaming helps mostly
by **not finishing work you do not need yet**.

Also: local Whisper is a *segmented* model. It emits no partial transcript while
you are still speaking, so speech-to-text cannot overlap your speech at all, no
matter how you schedule it. That is a large part of what hosted streaming STT
actually sells you.

## Measurement notes

Voice latency is easy to measure wrongly. What this repo does:

- **One clock.** OpenTelemetry stamps spans in wall clock; timing capture uses a
  monotonic clock. Both are recorded together so they can be compared at all.
- **t0 is the end of speech, not the end of the file.** Fixtures carry trailing
  silence. Using file duration would understate every number.
- **Overlap is interval intersection, never a sum of durations.** Summing cannot
  tell concurrency apart from a stage that kept running after first audio.
- **Real 20 ms cadence.** Pipecat has no file-based audio transport. Pushing a
  whole WAV as one frame would make VAD and endpointing see something no
  microphone produces, and every turn-detection number would be fiction.
- **Warmup is discarded.** The first inference pays model load and graph
  compilation. Cold runs are 2-4x slower and are not representative.
- **Medians with the sample count reported**, and failed runs counted, not
  silently dropped.

## Point it at your own agent

`budget.py` reads a JSONL file of OpenTelemetry spans. It does not import the
pipeline. If your agent emits the span names in [`SPANS.md`](SPANS.md):

```bash
uv run python budget.py --traces /path/to/your-traces.jsonl
```

## What this is not

Not a benchmark, not a product, not a research contribution, and not a claim
about anyone's stack but this one. One machine, one voice, small sample sizes.
It is a teaching artifact: the smallest thing that shows where a voice agent's
time goes and lets you measure your own.

## Setup

Needs [uv](https://docs.astral.sh/uv/), [Ollama](https://ollama.com), and
ffmpeg (`brew install ffmpeg`). Everything else downloads on first run.

Apple Silicon is the tested path (MLX Whisper, CoreML, MPS). On other hardware,
swap `WhisperSTTServiceMLX` for `WhisperSTTService` (faster-whisper) and expect
different absolute numbers. The shape should hold. The numbers will not.

## Licence

MIT.
