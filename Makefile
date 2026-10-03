VENV := .venv
PY := $(VENV)/bin/python

.PHONY: install test check build up down logs

$(PY):
	python3 -m venv $(VENV)

install: $(PY)
	$(PY) -m pip install -r requirements-dev.txt

test: $(PY)
	$(PY) -m pytest

check: $(PY)
	$(PY) .cursor/skills/pseudo-software/scripts/check_index.py

build:
	docker compose build

up:
	docker compose up -d

down:
	docker compose down

logs:
	docker compose logs -f
