PYTHON ?= python3.13
VENV := .venv
PY := $(VENV)/bin/python
WEB_CONCURRENCY ?= 2

.PHONY: setup etl run test lint bench

setup:
	$(PYTHON) -m venv $(VENV)
	$(PY) -m pip install -q -e ".[dev]"

etl:
	$(PY) manage.py build_stations --download

run:
	$(PY) -m uvicorn config.asgi:application --host 0.0.0.0 --port 8000 --workers $(WEB_CONCURRENCY) --no-access-log

test:
	$(PY) -m ruff check .
	$(PY) -m pytest

bench:
	$(PY) scripts/bench.py
