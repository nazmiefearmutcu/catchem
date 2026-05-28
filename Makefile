SHELL := /bin/bash
VENV := .venv
PY := $(VENV)/bin/python
PIP := $(VENV)/bin/uv pip
PYTEST := $(VENV)/bin/pytest
CATCHEM := $(VENV)/bin/catchem

.PHONY: help bootstrap install test test-fast test-guards test-smoke coverage run-replay run-api lint clean nuke

help:
	@echo "Targets:"
	@echo "  bootstrap     – one-command setup + run smoke (calls scripts/catchem_bootstrap_and_run.sh)"
	@echo "  install       – create venv + install editable"
	@echo "  test          – run full pytest suite"
	@echo "  test-fast     – unit-only (skip ml, smoke, integration)"
	@echo "  test-guards   – guard tests only (must always pass)"
	@echo "  test-smoke    – smoke tests"
	@echo "  coverage      – run full suite with line+branch coverage (term-missing)"
	@echo "  run-replay    – run replay mode against awareness JSONL"
	@echo "  run-api       – start catchem HTTP API"
	@echo "  lint          – ruff check"
	@echo "  clean         – remove caches and data/logs"
	@echo "  nuke          – clean + drop venv (dangerous)"

bootstrap:
	bash scripts/catchem_bootstrap_and_run.sh

install:
	@if [ ! -d "$(VENV)" ]; then uv venv --python 3.13 --seed; fi
	$(PIP) install -e ".[dev]"

test:
	$(PYTEST) -ra

test-fast:
	$(PYTEST) -ra -m "not ml and not smoke and not integration"

test-guards:
	$(PYTEST) -ra -m "guard"

test-smoke:
	$(PYTEST) -ra -m "smoke"

coverage:
	$(PYTEST) --cov=src/catchem --cov-report=term-missing

run-replay:
	$(CATCHEM) run --mode replay_existing

run-api:
	$(CATCHEM) serve

lint:
	$(VENV)/bin/ruff check src tests scripts || true

clean:
	rm -rf .pytest_cache .ruff_cache build dist *.egg-info
	rm -rf data/logs/*.log data/cache/*

nuke: clean
	rm -rf $(VENV)
