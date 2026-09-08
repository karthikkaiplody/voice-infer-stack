# Where Did My 800 Milliseconds Go?
#
#   make setup      install python deps + pull models   (once, needs network)
#   make budget     read the committed reference traces (no models needed)
#   make fixtures   regenerate the synthetic utterances
#   make record     record your own voice
#   make bench      run both builds and write fresh traces
#   make sweep      sweep the VAD silence timeout
#   make clips      write the cold-open audio for both builds
#   make charts     render the waterfall

FIXTURE ?= 02-medium
REPS    ?= 3
TRACES  ?= artifacts/reference-traces.jsonl
SECONDS ?= 6
NAME    ?= my-question

.PHONY: help setup models budget fixtures record bench sweep clips charts test clean

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

bench: ## run both builds: make bench FIXTURE=02-medium REPS=3
	uv run python bench.py --fixture $(FIXTURE) --reps $(REPS) --traces $(TRACES)

sweep: ## sweep the VAD silence timeout and show what it costs
	uv run python sweep.py

clips: ## record the cold-open audio for both builds
	uv run python clips.py --fixture $(FIXTURE)

charts:
	uv run python chart.py --traces $(TRACES)

test:
	uv run pytest -q

clean:
	rm -rf __pycache__ .pytest_cache
