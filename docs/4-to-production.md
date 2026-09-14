# Part 4 · From here to production

*Part 4 of the companion guide · [← Part 3 · How the measurement works](3-measurement.md) · [README](../README.md)*

Everything this repo leaves out is a decision, and each one has a production
counterpart. If you are here from the talk, this is the map from the four
boxes to the other eighty percent.

| Here (teaching scale) | Production (the other 80%) |
|---|---|
| The microphone is muted while the bot speaks, so the agent cannot be interrupted | Barge-in is a policy, not a boolean: when to stop playback, when to keep listening, and how to preserve the context that was interrupted. Track false-barge-in and missed-interruption rates — aggregate latency stays green while callers are being cut off |
| No network: microphone and speaker on one machine | Two network legs per turn, each with jitter, packet loss, a codec and a playout buffer. Budget them on both sides of the four stages |
| One turn at a time, one person, one laptop | Fleet dashboards: P95 turn latency per stage, WER drift, audio quality, cost per conversation |
| `uv run pytest -q` checks the measurement is honest | Eval suites: golden conversations re-run on every prompt, model or tool change |
| Spans read from a local JSONL file | The same spans over OTLP into Langfuse, Jaeger or Honeycomb. The span contract in [`SPANS.md`](../SPANS.md) is the stable part; the OTel `gen_ai.*` attribute names are still Development-stability, so the contract here is deliberately the repo's own |

Descriptions of Pipecat behaviour in this repo were verified against Pipecat
1.8.1, September 2026. If a newer Pipecat disagrees, believe the newer one and
re-check the sections above.

---

*[← Back to the README](../README.md)*
