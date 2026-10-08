PYTHON ?= .venv/bin/python
IMAGE ?= ghcr.io/arkane-systems/db-backup
TAG ?= dev

.PHONY: venv lint format test integration stack-up stack-down image

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

stack-up:
	mkdir -p .backups
	docker compose up -d --wait

stack-down:
	docker compose down -v

image:
	docker build --target runtime -t $(IMAGE):$(TAG) .
