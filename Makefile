PYTHON ?= .venv/bin/python
IMAGE ?= ghcr.io/arkane-systems/db-backup
TAG ?= dev

.PHONY: venv lint format test integration restore-test-check stack-up stack-down image

venv:
	python3 -m venv .venv
	.venv/bin/pip install --upgrade pip
	.venv/bin/pip install -e '.[test]' ruff

lint:
	ruff check src tests
	ruff format --check src tests

format:
	ruff check --fix src tests
	ruff format src tests

# Unit tests: no servers needed.
test:
	$(PYTHON) -m pytest

# Integration tests: back up and restore real servers in the compose.yaml stack.
integration: stack-up
	docker compose --profile tool run --rm --build tests

# Phase-2 restore testing, end to end: back up the compose.yaml servers, then run
# scripts/restore-test.sh on the result (scratch servers, restore, inventory check).
restore-test-check: stack-up
	rm -rf .backups && mkdir -p .backups
	env UID=$$(id -u) GID=$$(id -g) docker compose --profile tool run --rm --build dbbackup backup pg maria mongo mongo-filtered
	scripts/restore-test.sh --build .backups

stack-up:
	mkdir -p .backups
	docker compose up -d --wait

stack-down:
	docker compose down -v

image:
	docker build --target runtime -t $(IMAGE):$(TAG) .
