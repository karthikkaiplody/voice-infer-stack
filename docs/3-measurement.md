# Part 3 · How the measurement works

*Part 3 of the companion guide · [← Part 2 · What to notice](2-what-to-notice.md) · [Part 4 · From here to production →](4-to-production.md)*

Voice latency is easy to measure wrongly. This page is what the repo does
about that — and it is the part to read if you want to point the tooling at
your own agent.

## The rules

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
- **The primary output boundary is transport acceptance.**
  `output_transport.first_audio` means the output transport accepted the first
  frame: the output transport successfully wrote it, then re-pushed it
  downstream. `tts.first_synthesized_sample` is a separate earlier boundary.
  Neither says that the user heard playback.
- **Conversation state is not compute.** Speaking duration, silence, endpointing
  wait, and assistant-speaking state are reported separately from STT, LLM,
  tool, TTS, transport, and defensible VAD compute latency.

`uv run pytest -q` checks these hold, against the committed traces, so it needs
no models. The tests are about the numbers rather than the plumbing.

## The other trace file

`artifacts/scheduling-comparison.jsonl` is a sanitized recording from earlier
in this repo's life: the same synthetic workload run twice, once with the stages
strictly sequential and once overlapped. Nothing produces it any more and it is not a
claim about anything. It is kept because it is what `test_measurements.py` runs
against — one of the two runs overlaps its stages and the other does not, so
the interval maths has something it could get wrong.

The two committed trace files that contain end-to-end budget windows mark them
with `measured_as=tts_first_synthesized_sample_legacy`. The false-endpoint trace
file contains no end-to-end budget window. For the two legacy data sets, the
readers display: “Legacy measurement: ends at first synthesized sample; not
comparable to output_transport.first_audio.” They must not be compared with
corrected v1 transport-acceptance measurements.

All committed telemetry fixtures are synthetic or sanitized and metadata-only.
Raw-content capture is absent and disabled by default. Any future capture must
be an explicit local-development opt-in in a Git-ignored, session-specific
temporary directory, with graceful-shutdown deletion and startup TTL cleanup.

Comparisons require the same `workload_fixture_id`. Failed, cancelled, and
interrupted turns are separate populations. Missing stages are unavailable,
never zero. Median requires at least five samples and carries a small-sample
label below 20; p95 requires at least 20. The MVP has no p99, ranking, winner,
leaderboard, or combined score.

```bash
python3 budget.py --traces artifacts/scheduling-comparison.jsonl
```

## Point it at your own agent

`budget.py` reads a JSONL file of OpenTelemetry spans and does not import the
pipeline. If your agent emits the span names in [`SPANS.md`](../SPANS.md) — which
a Pipecat agent very nearly does already — it will read your traces too:

```bash
python3 budget.py --traces /path/to/your-traces.jsonl
```

---

*Next: [Part 4 · From here to production →](4-to-production.md)*
