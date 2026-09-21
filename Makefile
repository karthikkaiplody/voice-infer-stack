# Where Did My 800 Milliseconds Go?
#
#   make setup-demo replay safe fixtures in the browser: Python + Node deps only
#   make demo       open the page on synthetic fixtures at 127.0.0.1:8080, no mic or model
#   make budget     read a recorded turn, no install needed
#   make demo-live  talk to the agent and watch measured stages live (127.0.0.1:8080)
#   make live       open the page on the live agent (127.0.0.1:8080)
#                   both start the same server; the page has a Fixture / Live switch
#   make viewer     open one recorded turn as an HTML waterfall
#
#   make setup      setup-demo, then pull models        (once, needs network)
#   make devices    list microphones so you can pick the right one
#   make trace      run one WAV utterance through the pipeline, no microphone
#   make fixtures   regenerate the synthetic utterances
#   make record     record your own voice
#   make test       the Python tests (tests/)
#   make ui-test    type-check and test the browser UI (ui/tests/)
#   make test-all   Python tests, then the UI type-check and tests
#   make ui-build   build the browser UI into ui/dist (git-ignored)

FIXTURE ?= 02-medium
TRACES  ?= artifacts/reference-traces.jsonl
SECONDS ?= 6
NAME    ?= my-question
PORT    ?= 8080

# What a live conversation runs with. `make demo` starts with the same settings, so
# switching to Live in the page gives the agent `make live` would have.
#
# A live conversation wants responsiveness, not the benchmark's accuracy.
# whisper-tiny returns in ~60 ms against ~1 s for large-v3-turbo-q4, and the
# endpointing timers are set for a local stack rather than a network. The agent is
# the library one: it greets you, and looks up its notes before each answer.
# llama3.2:3b because the 1b model misreads the notes (it says the library is
# closed on Sundays). Override any of these to feel what changes:
#   VOICE_STT_MODEL=... make live     VOICE_AGENT= make live  (no agent, no lookup)
LIVE_ENV = VOICE_AGENT=$${VOICE_AGENT-library} \
	VOICE_STT_MODEL=$${VOICE_STT_MODEL:-mlx-community/whisper-tiny} \
	VOICE_LLM_MODEL=$${VOICE_LLM_MODEL:-llama3.2:3b} \
	VOICE_USER_SPEECH_TIMEOUT=$${VOICE_USER_SPEECH_TIMEOUT:-0.2} \
	VOICE_VAD_STOP_SECS=$${VOICE_VAD_STOP_SECS:-0.4}

.PHONY: help setup setup-demo models budget fixtures record trace devices demo demo-live live viewer test test-all ui-test ui-build clean

help:
	@grep -E '^#   ' Makefile | sed 's/^#   //'

setup-demo: ## install what the fixture replay needs: Python deps + UI deps (Node >= 20.19 or 22.12)
	@# PyAudio has no prebuilt macOS wheel: it compiles against Homebrew's portaudio.
	@# Without it `uv sync` fails deep inside a C compiler; say what to do instead.
	@[ -e "$$(brew --prefix portaudio 2>/dev/null)/include/portaudio.h" ] || \
	  { echo "portaudio is missing. Install it first:  brew install portaudio"; exit 1; }
	uv sync
	npm --prefix ui ci

setup: setup-demo ## install deps and pull every model
	$(MAKE) models

demo: ui-build ## open the page on the synthetic fixtures (no mic or model); the page can switch to live
	$(LIVE_ENV) uv run python -m voice_agent.server.live --mode fixture --port $(PORT)

models:
	@command -v ollama >/dev/null || { echo "install ollama: https://ollama.com"; exit 1; }
	ollama pull $$(uv run python -c "from voice_agent.config import CONFIG; print(CONFIG.llm_model)")
	uv run python -m voice_agent.pipeline.warm

budget: ## read a trace file and print where the time went
	uv run python -m voice_agent.analysis.budget --traces $(TRACES)

fixtures: ## regenerate synthetic utterances with Kokoro
	uv run python -m voice_agent.tools.make_fixtures

record: ## record your own utterance: make record NAME=my-question SECONDS=6
	uv run python -m voice_agent.tools.record --name $(NAME) --seconds $(SECONDS)

trace: ## run one fixture through the pipeline without a microphone
	uv run python -m voice_agent.pipeline.agent fixtures/audio/$(FIXTURE).wav
	uv run python -m voice_agent.analysis.budget --traces artifacts/my-traces.jsonl

devices: ## list microphones, so you can pick the right one
	@uv run python -c "from voice_agent.pipeline import factory; [print(('  * ' if d['default'] else '    ')+f\"[{d['index']}] {d['name']}\") for d in factory.list_input_devices()]"
	@echo "  * = system default.  Choose another: VOICE_AUDIO_DEVICE=5 make live"

demo-live: ui-build ## talk to the agent and watch contract-v1 events, live
	$(MAKE) live

live: ## open the page on the live agent; the page can switch to the fixtures
	$(LIVE_ENV) uv run python -m voice_agent.server.live --mode live --port $(PORT)

ui-test: ## type-check and test the browser UI
	npm --prefix ui run typecheck
	npm --prefix ui test

ui-build: ## build the browser UI into ui/dist
	npm --prefix ui run build

viewer: ## render one turn as a standalone HTML page
	python3 -m voice_agent.analysis.viewer --traces $(TRACES) --open

test: ## run the Python tests
	uv run pytest -q

test-all: test ui-test ## run the Python tests, then the UI checks

clean:
	find . -name __pycache__ -not -path "./.venv/*" -not -path "./ui/node_modules/*" -prune -exec rm -rf {} +
	rm -rf .pytest_cache
