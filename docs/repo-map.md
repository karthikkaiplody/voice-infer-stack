# Repo map

*[← README](../README.md)*

Where everything lives, and what each file is for. If you only want to run the
project, you do not need this page.

## The folders

```
voice_agent/     the Python code, split by what it is for (start here)
  config.py        every model, rate, prompt and threshold, in one frozen object
  paths.py         every folder and file the code reads or writes
  pipeline/        the voice agent: audio in, spoken answer out
  knowledge/       what the agent knows, and how it looks things up
  telemetry/       how a turn is measured, and what may be reported
  server/          the local web server behind the browser UI
  analysis/        read recorded turns (standard library only)
  tools/           one-off scripts: record your voice, regenerate test audio
tests/           Python tests, in folders that match voice_agent/
ui/              the browser page (React + TypeScript)
  src/             the page itself
  tests/           the page's tests, in folders that match src/
agents/          one folder per agent: its prompt and its notes (edit these)
fixtures/        audio/ (WAV utterances)  telemetry/ (recorded contract events)
telemetry_contract/   the event contract, as a JSON Schema
artifacts/       recorded turns everyone can read
docs/            the four-part guide, how it works, live mode, this page, and the README's images
diagrams/        the diagrams as HTML, their PNGs in images/, and the script that renders them
```

Every folder under `voice_agent/` opens with a docstring that lists its files, so
`voice_agent/pipeline/__init__.py` is the quickest way to see what is in it.

## The files

| File | What it does |
|---|---|
| `voice_agent/pipeline/agent.py` | The pipeline: which stages, in what order, and what decides your turn is over. Used by both ways of running it. |
| `voice_agent/pipeline/factory.py` | Builds each stage from config. The seam that makes components swappable. |
| `voice_agent/pipeline/wav_transport.py` | Feeds a WAV into Pipecat at real 20 ms cadence, for running without a microphone. |
| `voice_agent/pipeline/warm.py` | Pre-downloads model weights so the first run is not a cold surprise. |
| `voice_agent/knowledge/agents.py` | Loads an agent from `agents/<name>/`: prompt, greeting and notes. |
| `voice_agent/knowledge/search.py` | Splits notes into passages and ranks them against a question. |
| `voice_agent/knowledge/retrieval.py` | The stage that puts the best notes in front of the model. |
| `voice_agent/telemetry/contract.py` | Contract validation, stable identity, immutable safe configuration snapshots, and privacy filtering. |
| `voice_agent/telemetry/tracing.py` | OpenTelemetry wiring, metadata filtering, and the timing boundaries Pipecat does not emit. |
| `voice_agent/telemetry/runtime_events.py` | Turns what Pipecat does into contract events while the agent runs. |
| `voice_agent/server/live.py` | The server behind `make live` and `make demo`: serves the page and streams events to it. |
| `voice_agent/server/tuning.py` | The few settings the page may change, with limits and warnings. |
| `voice_agent/server/input_watch.py` | Notices a microphone that delivers only silence, and tells the page. |
| `voice_agent/analysis/budget.py` | Reads a trace file, prints the per-stage budget. |
| `voice_agent/analysis/measurement.py` | Span interval maths: overlap, union, median. |
| `voice_agent/analysis/viewer.py` | Renders one recorded turn as a standalone HTML waterfall. |
| `voice_agent/tools/record.py`, `make_fixtures.py` | Record your own utterance, or generate one locally. |
| `tests/analysis/test_measurements.py` | Invariants that catch a number that is quietly wrong. |
| `Makefile` | Every entry point: `make live`, `make demo`, `make trace`, `make budget`, `make test-all`. |
| `SPANS.md` | The event and span contract. What to emit for your own agent. |
