# The span contract

`budget.py` does not import the pipeline. It reads a file of OpenTelemetry spans
and reports where the time went. If your voice agent emits the spans below, it
will read your traces too:

```bash
uv run python budget.py --traces /path/to/your-traces.jsonl
```

## File format

One JSON object per line. This is what `tracing_setup.JsonlSpanExporter` writes,
and any OTel exporter can be adapted to it in a few lines.

```json
{
  "name": "llm",
  "trace_id": "3f1c...",
  "span_id": "9a2b...",
  "parent_span_id": "1d4e...",
  "start_time_ns": 1788662359000968000,
  "end_time_ns": 1788662359551000000,
  "duration_ms": 550.0,
  "attributes": {"gen_ai.request.model": "llama3.2:3b", "metrics.ttfb": 0.09}
}
```

`start_time_ns` and `end_time_ns` are **wall-clock** nanoseconds and both are
required. Duration alone is not enough: overlap between stages is computed by
intersecting time intervals, and a duration has no position on a timeline.

## Required: the budget window

```
e2e.speech_end_to_first_audio
```

One per turn. It defines what counts as latency: it starts the instant the user
**stops speaking** and ends when the **first synthesized audio sample exists**.

This span is what makes the numbers meaningful. Everything before it is the user
talking, which costs the user nothing. Stages are clipped to this window, so a
stage that began while the user was still speaking is not charged for the part
that was free. Segmented speech-to-text always does exactly that, and without
clipping it appears to dominate the budget when most of its span was free.

| Attribute | | |
|---|---|---|
| `mode` | required | how this run was scheduled, e.g. `naive`, `streaming` |
| `fixture` | optional | which utterance |

## Recognised stages

Any subset. Missing stages are simply not reported.

| Span name | What it should cover |
|---|---|
| `turn_detection` | From end of speech until the agent decides the turn is over. Usually a silence timeout, so mostly waiting rather than computing. |
| `stt` | Speech to text. |
| `llm` | Generation. |
| `tts` | Speech synthesis. |

These names and the `gen_ai.*` attributes below match what
[Pipecat](https://github.com/pipecat-ai/pipecat) emits with `enable_tracing=True`,
so a Pipecat agent needs no extra instrumentation beyond the window span.

### Attributes read, all optional

| Attribute | Used for |
|---|---|
| `metrics.ttfb` | Time to first byte or token, in **seconds** |
| `gen_ai.request.model` | Labelling |
| `gen_ai.provider.name` | Labelling |
| `gen_ai.usage.input_tokens` / `output_tokens` | Labelling |
| `transcript`, `output` | Checking both runs produced the same text |
| `wait_ms` | On `turn_detection`, the measured silence wait |

## Two things that are easy to get wrong

**`turn_detection` is a wait, not work.** `budget.py` deliberately does not
count an intersection between `turn_detection` and a compute stage as a
scheduling win. Speech-to-text transcribing buffered audio while the detector
waits is normal, not a saving, and counting it inflates the headline.

**More than one span with the same name inside one window means work was
discarded.** Two `llm` spans in a turn is the signature of a false endpoint: the
agent answered a fragment, threw it away, and answered again. `budget.py`
reports the count. See [`artifacts/turn-detection/`](artifacts/turn-detection/)
for a captured example.
