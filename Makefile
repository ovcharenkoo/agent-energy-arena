# Feedback loops for the Agent Energy Arena.
#
# `make check` is the canonical "is it ready to commit?" gate. CI and the
# AFK agent loop should both run this. Individual targets exist for fast
# iteration during development.

# Honor an active virtualenv if present; otherwise fall back to the local
# .venv (created by `make venv`); otherwise system python.
PYTHON ?= $(shell \
	if [ -n "$$VIRTUAL_ENV" ]; then echo "$$VIRTUAL_ENV/bin/python"; \
	elif [ -x ".venv/bin/python" ]; then echo ".venv/bin/python"; \
	else echo python3; fi)

# Interpreter used to create .venv. Must satisfy requires-python (>=3.11);
# macOS system python3 is 3.9, so pick the first available 3.11+ binary.
VENV_PYTHON ?= $(shell \
	for py in python3.14 python3.13 python3.12 python3.11 python3; do \
		if command -v $$py >/dev/null 2>&1; then echo $$py; break; fi; \
	done)

.PHONY: help venv install test typecheck lint format format-check check serve play eval score clean

help:
	@echo "Targets:"
	@echo "  install       Install package with dev extras into the active env"
	@echo "  venv          Create .venv if it does not already exist"
	@echo "  test          Run pytest"
	@echo "  typecheck     Run mypy"
	@echo "  lint          Run ruff lint (no fixes)"
	@echo "  format        Apply ruff format in-place"
	@echo "  format-check  Verify ruff format without writing"
	@echo "  check         lint + format-check + typecheck + test (commit gate)"
	@echo "  serve         Run uvicorn locally at :8000 (no docker)"
	@echo "  play          docker compose up — world + UI at :8000"
	@echo "  eval          docker compose --profile eval run agent — score submit/agent.py"
	@echo "  score         Run the scripted agent on seed 42 and print the score line"

venv:
	@test -d .venv || $(VENV_PYTHON) -m venv .venv
	@$(PYTHON) -m pip install --upgrade pip >/dev/null

install: venv
	$(PYTHON) -m pip install -e ".[dev,llm]"

test:
	$(PYTHON) -m pytest

typecheck:
	$(PYTHON) -m mypy

lint:
	$(PYTHON) -m ruff check .

format:
	$(PYTHON) -m ruff format .

format-check:
	$(PYTHON) -m ruff format --check .

check: lint format-check typecheck test

serve:
	$(PYTHON) -m uvicorn world.api:app --reload --host 0.0.0.0 --port 8000

# The three commands every participant must remember (brief §11.2).
play:
	docker compose up

eval:
	docker compose --profile eval run --rm agent

score:
	$(PYTHON) evaluate.py --agent submit.agent --seed 42

clean:
	rm -rf .mypy_cache .ruff_cache .pytest_cache
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
