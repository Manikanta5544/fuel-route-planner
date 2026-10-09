PYTHON ?= python3.13
VENV := .venv
PY := $(VENV)/bin/python
WEB_CONCURRENCY ?= 1

.PHONY: setup etl run test lint bench

setup:
	$(PYTHON) -m venv $(VENV)
	$(PY) -m pip install -q -e ".[dev]"

etl:
	$(PY) manage.py build_stations --download

run:
	WEB_CONCURRENCY=$(WEB_CONCURRENCY) $(PY) scripts/serve.py

test:
	$(PY) -m ruff check .
	$(PY) -m pytest

bench:
	$(PY) scripts/bench.py
