VENV := .venv
PY := $(VENV)/bin/python

.PHONY: install test run build up down logs

$(PY):
	python3 -m venv $(VENV)

install: $(PY)
	$(PY) -m pip install -r requirements-dev.txt

test: $(PY)
	$(PY) -m pytest

run: $(PY)
	set -a; [ -f .env ] && . ./.env; set +a; BAIDU_EASY_ADDR=$${BAIDU_EASY_ADDR:-:28080} $(PY) -m app.main

build:
	docker compose build

up:
	mkdir -p downloads
	docker compose up -d

down:
	docker compose down

logs:
	docker compose logs -f
