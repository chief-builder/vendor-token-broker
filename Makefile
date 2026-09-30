# Developer entry points. `make test` works from a clean clone (it creates
# .venv on first use); the stack targets need Docker. CI runs these targets.

PYTHON  ?= python3.14
VENV    := .venv
BIN     := $(VENV)/bin
COMPOSE := docker compose -f tests/stack/docker-compose.yml
MARKERS := not external and not multi and not gateway

.DEFAULT_GOAL := help
.PHONY: help install lint format typecheck test docs check build lock \
        stack-up stack-down test-integration test-gateway test-multi test-all

help:  ## List the targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-17s %s\n", $$1, $$2}'

# Rebuilt whenever a lock or pyproject.toml changes.
$(BIN)/.installed: requirements-dev.lock pyproject.toml
	test -x $(BIN)/python || $(PYTHON) -m venv $(VENV)
	$(BIN)/pip install --quiet --require-hashes -r requirements-dev.lock
	$(BIN)/pip install --quiet --no-deps -e .
	touch $@

install: $(BIN)/.installed  ## Create .venv and install the hash-pinned dev lock

lint: install  ## Lint and check formatting
	$(BIN)/ruff check src tests tools
	$(BIN)/ruff format --check src tests tools

format: install  ## Apply the formatter and safe lint fixes
	$(BIN)/ruff format src tests tools
	$(BIN)/ruff check --fix src tests tools

typecheck: install  ## Type-check src with mypy
	$(BIN)/mypy

test: install  ## Unit tests with coverage (offline, no containers)
	$(BIN)/pytest tests/unit -q --cov=token_broker --cov=mcp_gateway \
	  --cov-report=term --cov-report=xml --cov-fail-under=90

docs: install  ## Check that docs/index.html is current and its anchors resolve
	$(BIN)/python tools/build-pages.py --check

check: lint typecheck test docs  ## Everything CI checks without Docker

build: install  ## Build the wheel and both images
	$(BIN)/pip wheel --quiet --no-deps --wheel-dir dist .
	docker build -t vendor-token-broker:local .
	docker build -f Dockerfile.gateway -t vtb-mcp-gateway:local .

lock:  ## Regenerate the three hash-pinned locks from pyproject.toml (needs uv)
	uv pip compile pyproject.toml --extra redis --universal --python-version 3.14 \
	  --generate-hashes --upgrade -o requirements.lock
	uv pip compile pyproject.toml --extra redis --extra dev --extra gateway --universal \
	  --python-version 3.14 --generate-hashes --upgrade -o requirements-dev.lock
	uv pip compile pyproject.toml --extra gateway --universal --python-version 3.14 \
	  --generate-hashes --upgrade -o requirements-gateway.lock

stack-up:  ## Start the test stack with the gateway profile (COORD_BACKEND=memory|redis)
	$(COMPOSE) --profile gateway up -d --build --wait

stack-down:  ## Stop every profile of the test stack (OpenBao state is lost)
	$(COMPOSE) --profile gateway --profile multi down --remove-orphans

test-integration: install  ## Broker integration suite against a running stack
	$(BIN)/pytest tests/integration -q -m "$(MARKERS)"

test-gateway: install  ## Gateway (Keycloak) suite against a running gateway-profile stack
	$(BIN)/pytest tests/integration -q -m "gateway and not external"

test-multi: install  ## Two replicas behind nginx: restarts the stack in the multi profile
	$(MAKE) stack-down
	$(COMPOSE) --profile multi up -d --build --wait
	BROKER_URL=http://localhost:8400 BROKER_CONTAINERS=vtb-broker-a,vtb-broker-b \
	  $(BIN)/pytest tests/integration/test_multi_replica.py -q

test-all: test stack-up test-integration test-gateway  ## Unit + broker + gateway suites (starts the stack)
