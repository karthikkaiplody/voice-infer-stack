# Changelog

All notable changes to this project are recorded here. It follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
[Semantic Versioning](https://semver.org/). The event contract is versioned
separately (see [SPANS.md](SPANS.md)); releases 1.0.0 and 1.0.1 ship contract `1.1.0`.

## [Unreleased]

### Added

- The library agent can suggest books. A catalog of nineteen real books in seven
  genres, with where to find them and what the information line cannot check, and a
  short summary of each book, all in `agents/library/knowledge/`. Its prompt now
  allows suggestions, but only from the notes, and sends a title it does not know
  to the front desk. Asked for a suggestion with no genre, it offers the staff picks.
- A Node check up front. `make setup-demo`, `make ui-build`, `make ui-test` and the
  targets that build the page stop in the first second when Node is missing or
  older than 20.19 / 22.12, name the version and path they found, and say how to
  install a newer one. Before, an old Node failed after the long Python install,
  or as an error from the build tools. A test keeps the check in step with
  `engines` in `ui/package.json`.
- `make ui-build` and `make ui-test` say to run `make setup-demo` when the page's
  dependencies are not installed.

- An *Answered too early* replay scenario: a one-second pause is read as the end of
  the turn, the agent answers fast, and the turn ends interrupted. It is the
  false-endpoint lesson in the page, using contract `1.0.0` events.
- The page now replays a recorded turn as soon as it opens on recorded turns, so
  `make demo` no longer lands on an empty waterfall.

### Changed

- The recorded turns now run at realistic speeds. A normal turn showed 320 ms from
  speech end to first audio, with every stage 70 to 130 ms, which contradicted what
  this repo measures (about 1.3 to 1.8 s on a local stack). The normal, grounded,
  interrupted and answered-too-early turns now use the fixture's own settings (a
  600 ms endpointing wait) and stage times taken from this repo's runs, so a normal
  turn is 1,250 ms and the README screenshot shows it. The page opens on that turn.
- The silent-microphone warning on the page is now a short list of steps that starts
  with the Mac's own input setting (System Settings → Sound → Input) and keeps
  `make devices` and `VOICE_AUDIO_DEVICE` as the fallback, instead of sending
  everyone straight to the terminal. It still does not name the device.
- The guide's three swaps in [Part 1](docs/1-swap-and-see.md) now describe what
  they measure. Two of them did not reproduce as written: the safety-net timer had
  no effect, and the smaller model made the turn slower, not the same. The false
  endpoint needs `VOICE_USER_SPEECH_TIMEOUT=0.0` as well as a short silence window.
- The diagram PNGs moved from `diagrams/slides/` to `diagrams/images/` and lost
  the `.clean.` part of their names. `render-slides.mjs` is now
  `render-images.mjs` and renders only the images the docs use.
- `make help` no longer prints a stray line from the Makefile's comments.

### Fixed

- `make live` on a fresh clone served "The UI is not built yet" (HTTP 503) unless
  `make demo` had been run first, so the README's live path did not work as
  written. `make live` now builds the page first, as `make demo` does.
- `make trace` failed with `no 'e2e.speech_end_to_first_audio' spans` whenever the
  turn closed before the last transcript arrived, because that transcript started
  a new logical turn and discarded the open budget window. The window now survives
  it, and the budget prints `llm 2x - work was discarded`.

### Removed

- Six presentation-size slide images (`*.dark.png` and `*.light.png` without
  `clean`). Nothing used them.
- Three helpers in `voice_agent/analysis/measurement.py` that nothing called:
  `load_traces`, `overlap_report` and `stage_table`.

## [1.0.1] - 2026-09-20

### Changed

- The README is now a short front page: the pitch, a screenshot, the quick start,
  and a table of links, with the details in collapsible sections. The sequence
  diagram, where the numbers come from, privacy, and the live-mode notes moved,
  unchanged, to [docs/how-it-works.md](docs/how-it-works.md) and
  [docs/live-mode.md](docs/live-mode.md).

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

[1.0.1]: https://github.com/karthikkaiplody/voice-infer-stack/tree/v1.0.1
[1.0.0]: https://github.com/karthikkaiplody/voice-infer-stack/tree/v1.0.0
