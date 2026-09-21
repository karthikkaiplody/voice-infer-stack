# Changelog

All notable changes to this project are recorded here. It follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
[Semantic Versioning](https://semver.org/). The event contract is versioned
separately (see [SPANS.md](SPANS.md)); this release ships contract `1.1.0`.

## [1.0.0] - 2026-09-20

The first release: the companion to the IEEE RTC Conference 2026 talk
"Where Did My 800 Milliseconds Go?".

### Added

- **A local observability page** (React and TypeScript) that draws one voice-agent
  turn as it happens: the current state, the primary metric (user speech ended to
  output transport accepted first audio), a waterfall with an honest state for
  every stage, the outcome, an event log, a safe configuration summary, a tuning
  panel for six latency settings, and a switch between recorded turns and the
  live agent.
- **Fixture replay** with five synthetic scenarios (a normal turn, a slow tool, an
  interrupted turn, a failed tool, and a grounded answer). It needs no
  microphone, model or API key, and the page labels every number in it as
  synthetic.
- **Live mode**: the real Pipecat agent on your machine, streaming the events it
  actually produces. It greets you, and its library agent looks up local notes
  before answering. The page warns when the microphone delivers only silence.
- **Agents**: a folder holds a prompt, a greeting and markdown notes, and a local
  BM25 search puts the best notes in front of the model. See
  [agents/README.md](agents/README.md).
- **Telemetry contract `1.1.0`**: metadata-only events with strict validation.
  `1.1.0` adds the knowledge lookup stage and nothing else; `1.0.0` traces stay
  valid.
- **Tools that need nothing installed** (Python 3 standard library only): a
  per-stage latency budget and a standalone HTML waterfall for a recorded turn.
- **Docs**: a four-part guide, the contract, a guide to writing agents, a repo map,
  `CONTRIBUTING.md`, `SECURITY.md`, and CI.

### Privacy and security

- The server listens on `127.0.0.1` only and rejects any request whose Host or
  Origin is not local, on every route.
- Traces, the browser stream and the console (at its default level) carry
  metadata only: no audio, transcripts, prompts, generated text, tool data, raw
  errors, credentials, device names, hostnames or paths.
- The repository history was reviewed for secrets, personal data and raw content
  before release, and raw benchmark traces from early development were removed
  from it.

### Known limitations

- **macOS on Apple Silicon only.** Speech-to-text uses MLX Whisper. `portaudio`
  must be installed with Homebrew before `make setup-demo`.
- **The live agent registers no tools**, so the Tool calls row reads *Not
  instrumented*. The microphone is muted while it speaks, so an interrupted turn
  appears only in the recorded scenarios.
- **`npm audit` reports two moderate advisories in `vitest`'s test tooling** (a
  mock-redirect feature this project does not use). They are development-only:
  the built page ships React alone, which has none. The fix is a major upgrade to
  vitest 5, planned for a later release.

### Not in this release

Cloud deployment, a database or saved trace history, comparing configurations,
auth, a hosted service, package publishing, a provider-switching UI,
Grafana/Tempo/ClickHouse/Kafka/Kubernetes, raw content viewing, and support for
other platforms.

[1.0.0]: https://github.com/karthikkaiplody/voice-infer-stack/releases/tag/v1.0.0
