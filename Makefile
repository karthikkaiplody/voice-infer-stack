# Where Did My 800 Milliseconds Go?
#
#   make budget     read a recorded turn, no install needed
#   make live       talk to the agent and watch the latency (127.0.0.1:8080)
#   make viewer     open one recorded turn as an HTML waterfall
#
#   make setup      install python deps + pull models   (once, needs network)
#   make devices    list microphones so you can pick the right one
#   make trace      run one WAV utterance through the pipeline, no microphone
#   make fixtures   regenerate the synthetic utterances
#   make record     record your own voice
#   make test       the measurement invariants

FIXTURE ?= 02-medium
TRACES  ?= artifacts/reference-traces.jsonl
SECONDS ?= 6
NAME    ?= my-question

.PHONY: help setup models budget fixtures record trace devices live viewer test clean

help:
	@grep -E '^#   ' Makefile | sed 's/^#   //'

setup: ## install deps and pull every model
	uv sync
	$(MAKE) models

models:
	@command -v ollama >/dev/null || { echo "install ollama: https://ollama.com"; exit 1; }
	ollama pull $$(uv run python -c "from config import CONFIG; print(CONFIG.llm_model)")
	uv run python warm.py

budget: ## read a trace file and print where the time went
	uv run python budget.py --traces $(TRACES)

fixtures: ## regenerate synthetic utterances with Kokoro
	uv run python make_fixtures.py

record: ## record your own utterance: make record NAME=my-question SECONDS=6
	uv run python record.py --name $(NAME) --seconds $(SECONDS)

trace: ## run one fixture through the pipeline without a microphone
	uv run python agent.py fixtures/$(FIXTURE).wav
	uv run python budget.py --traces artifacts/my-traces.jsonl

devices: ## list microphones, so you can pick the right one
	@uv run python -c "import factory; [print(('  * ' if d['default'] else '    ')+f\"[{d['index']}] {d['name']}\") for d in factory.list_input_devices()]"
	@echo "  * = system default.  Choose another: VOICE_AUDIO_DEVICE=5 make live"

live: ## talk to the agent and watch the stages light up
	@# A live conversation wants responsiveness, not the benchmark's accuracy.
	@# whisper-tiny returns in ~60 ms against ~1 s for large-v3-turbo-q4, and
	@# the endpointing timers are set for a local stack rather than a network.
	@# Override any of these to feel what changes: VOICE_STT_MODEL=... make live
	VOICE_STT_MODEL=$${VOICE_STT_MODEL:-mlx-community/whisper-tiny} \
	VOICE_LLM_MODEL=$${VOICE_LLM_MODEL:-llama3.2:1b} \
	VOICE_USER_SPEECH_TIMEOUT=$${VOICE_USER_SPEECH_TIMEOUT:-0.2} \
	VOICE_VAD_STOP_SECS=$${VOICE_VAD_STOP_SECS:-0.4} \
	uv run python live.py

viewer: ## render one turn as a standalone HTML page
	python3 viewer.py --traces $(TRACES) --open

test:
	uv run pytest -q

clean:
	rm -rf __pycache__ .pytest_cache
