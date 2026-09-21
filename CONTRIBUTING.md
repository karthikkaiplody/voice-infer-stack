# Contributing

Thanks for taking a look. This is a teaching companion for a talk about where a
voice agent's latency goes, so the bar for a change is: does it make the numbers
more honest, or the project easier to learn from?

## Scope

In scope: the local page, the local pipeline, the event contract, the fixtures,
the docs, and the tests around them. This release supports macOS on Apple Silicon.

Out of scope for now, so please do not send these: cloud deployment, a database or
saved trace history, comparing configurations, auth, a hosted service, package
publishing, a provider-switching UI, Grafana/Tempo/ClickHouse/Kafka/Kubernetes, and
support for other platforms. If you think one belongs, open an issue first.

## Set up

```bash
git clone https://github.com/karthikkaiplody/voice-infer-stack.git
cd voice-infer-stack
make setup-demo        # Python and Node dependencies. No models needed.
make test-all          # Python tests, then the page's type-check, tests and build
```

You need `brew install portaudio`, [uv](https://docs.astral.sh/uv/) and Node.js 20.19+ or 22.12+. Nothing
in the test suite needs a microphone, a model or the network. `make demo` opens
the page on recorded turns.

## The rule that matters: no raw content, ever

Telemetry here is metadata-only, and so is everything committed. Never commit, and
never paste into an issue or pull request:

- audio of a person, or transcripts, prompts or replies from a real conversation;
- trace files from your own sessions (`artifacts/live-traces.jsonl` and
  `artifacts/my-traces.jsonl` are git-ignored for this reason);
- API keys, tokens, `.env` files, or local configuration;
- absolute paths, usernames, hostnames, or the names of your audio devices.

Fixtures must be synthetic and must pass `validate_trace`. If your change makes
code emit, store or show a new field, it has to go through the allowlist in
`voice_agent/telemetry/contract.py`, with a test that a canary value in that
field does not come out the other side.

## Changing the event contract

The contract is [SPANS.md](SPANS.md) plus `telemetry_contract/v1.schema.json`.
Adding an event or attribute is a minor version (`1.x.0`) and must stay backward
compatible: old traces stay valid, and a reader that only knows the old version
must reject the new events rather than misread them. Update the schema, the
Python validator, the UI parser (`ui/src/contract/events.ts`), and the parity test
in `ui/tests/contract/`. Removing or changing the meaning of anything is a new
major version and needs an issue first.

## Making a change

1. Keep it small and focused. One idea per pull request.
2. Match the code around it: naming, comment density, idiom. Comments say *why*.
3. Add or update tests. A bug fix starts with a test that fails.
4. Run `make test-all` and make sure it passes.
5. If you change a number the project reports, or a claim in the docs, say how you
   checked it. This project's whole point is that its numbers are not quietly wrong.

Commit messages follow [Conventional Commits](https://www.conventionalcommits.org):
`feat:`, `fix:`, `docs:`, `test:`, `ci:`, `chore:`, with a short imperative summary.

## Reporting a bug

Open an issue with what you ran, what you expected, what happened, your macOS
version and chip, and the Node and uv versions. Leave out anything from the list
above. For anything involving privacy or security, use [SECURITY.md](SECURITY.md)
instead of a public issue.
