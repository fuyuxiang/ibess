PYTHON ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)

.PHONY: bootstrap lint test integration web compose check up migrate
bootstrap:
	python3 -m venv .venv
	.venv/bin/python -m pip install -r requirements/dev.lock
	.venv/bin/python -m pip install --no-deps -e packages/ibess-common

lint:
	$(PYTHON) tools/check.py lint
test:
	$(PYTHON) tools/check.py unit
integration:
	$(PYTHON) tools/check.py integration
web:
	$(PYTHON) tools/check.py web
compose:
	$(PYTHON) tools/check.py compose
check:
	$(PYTHON) tools/check.py all
up:
	bash scripts/start_poc.sh --build --workers
migrate:
	docker compose run --rm --no-deps api python -m app.migrations upgrade
