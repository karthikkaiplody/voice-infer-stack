# Telemetry contract v1

The repository-owned event contract is
[`telemetry_contract/v1.schema.json`](telemetry_contract/v1.schema.json).
Every event carries `schema_version: "1.0.0"`, a unique event ID, a nanosecond
timestamp, stable identities, and controlled metadata. Unknown schema versions
fail closed. `telemetry.py` validates the rules that JSON Schema cannot express,
including event order, span parents, turn identity, and tool retry lifecycles.

## Timing boundaries

The v1 event names are:

```text
user_speech.started                 user_speech.ended
endpointing.started                 endpointing.resolved
stt.started                         stt.first
stt.final                           llm.started
llm.first_token                     tool.requested
tool.running                        tool.progress
tool.completed                      tool.error
tool.cancelled                      tts.started
tts.first_synthesized_sample        output_transport.first_audio
turn.completed                      turn.interrupted
turn.failed
```

`output_transport.first_audio` is the primary observable output boundary. It
means the output transport accepted the first audio frame. It does not prove
that the user heard it. `tts.first_synthesized_sample` is recorded separately.
Neither is audible playback unless a transport supplies a playback
acknowledgment boundary.

The runtime records `tts.first_synthesized_sample` on the first TTS-side frame
push. It records `output_transport.first_audio` only when `BaseOutputTransport`
re-pushes that frame downstream after `_internal_write_audio_frame` succeeds.
The end-to-end window closes at that later transport-acceptance timestamp and
carries `measured_as=output_transport_accepted`.

Contract v1 is fully defined and fixture-validated. The current Pipecat runtime
emits its existing stage spans plus the instrumented synthesis, transport, and
end-to-end boundaries; it does not yet emit every named v1 lifecycle event.
Synthetic fixtures cover the complete lifecycle examples. Expanded runtime
event emission belongs to the future live-ingestion milestone.

User speaking, user silence, endpointing wait, and assistant speaking are
conversation state. They are not compute latency. VAD processing time is a
compute metric only where an implementation has a defensible compute boundary.

## Identity and tools

Session, conversation, turn, trace, span, configuration snapshot, and workload
fixture IDs remain stable for their documented scopes. Child spans reference a
parent span in the same trace. A tool attempt also carries a stable logical
tool-call ID, a distinct attempt ID, and a one-based attempt number. Its states
are `requested -> running -> progress* -> completed/error/cancelled`. A retry is
a new attempt under the same logical tool-call ID.

## Metadata-only privacy boundary

Telemetry is metadata-only by default. The exporter applies an allowlist before
data reaches JSONL or the live browser stream. It excludes raw audio,
transcripts, prompts, generated text, TTS text, tool arguments and results, raw
errors, credentials, provider payloads, hostnames, usernames, absolute paths,
serial numbers, device identifiers, and network identifiers.

Raw-content capture is not implemented. If introduced later, it must be an
explicit local development opt-in, use a Git-ignored session-specific temporary
directory, be removed on graceful shutdown, and be subject to startup TTL
cleanup. It must never be enabled by default.

## Synthetic fixtures and comparisons

The four files in `telemetry_fixtures/` are synthetic metadata-only examples:
a completed turn, a slow blocking-tool turn, an interrupted turn, and a failed
tool turn with a retry. Fixtures must pass `validate_trace` and must never be
copied from customer or developer sessions.

Direct comparisons require matching `workload_fixture_id`. Failed, cancelled,
and interrupted turns remain separate from completed turns. A missing stage is
unavailable, never zero milliseconds. Median may be shown at `n >= 5`; for
`n < 20` it needs a small-sample label. P95 requires `n >= 20`. The MVP does not
show p99, rankings, winners, leaderboards, or combined scores.
Microphone runs receive unique `noncomparable_live_*` workload IDs and
`telemetry.workload_comparable=false`; sharing the word “live” never makes two
uncontrolled microphone turns directly comparable.

## Legacy span reader

`budget.py` does not import the pipeline. It reads a file of OpenTelemetry spans
and reports where the time went. If your voice agent emits the spans below, it
will read your traces too:

```bash
uv run python budget.py --traces /path/to/your-traces.jsonl
```

### File format

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

### Required: the budget window

```
e2e.speech_end_to_first_audio
```

One per turn. It starts when the user stops speaking and, for newly recorded
traces, ends at `output_transport.first_audio`. The explicit
`tts.first_synthesized_sample` span keeps synthesis and transport separate.
Corrected windows carry `measured_as=output_transport_accepted`. A window
without that exact marker is legacy data ending at the first synthesized
sample; `budget.py` and `viewer.py` label it non-comparable.

This span is what makes the numbers meaningful. Everything before it is the user
talking, which costs the user nothing. Stages are clipped to this window, so a
stage that began while the user was still speaking is not charged for the part
that was free. Segmented speech-to-text always does exactly that, and without
clipping it appears to dominate the budget when most of its span was free.

| Attribute | | |
|---|---|---|
| `mode` | required | how the turn was produced, e.g. `live`, `file` |
| `fixture` | optional | which recorded utterance, if it was not a microphone |

### Recognised stages

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
| `wait_ms` | On `turn_detection`, the measured silence wait |

### Two things that are easy to get wrong

**`turn_detection` is a wait, not work.** `budget.py` deliberately does not
count an intersection between `turn_detection` and a compute stage as a
scheduling win. Speech-to-text transcribing buffered audio while the detector
waits is normal, not a saving, and counting it inflates the headline.

**More than one span with the same name inside one window means work was
discarded.** Two `llm` spans in a turn is the signature of a false endpoint: the
agent answered a fragment, threw it away, and answered again. `budget.py`
reports the count. See [`artifacts/turn-detection/`](artifacts/turn-detection/)
for a captured example.
