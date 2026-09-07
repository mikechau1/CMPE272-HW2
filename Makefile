.DEFAULT_GOAL := help
SHELL := /bin/bash

VENV    := .venv
PY      := $(VENV)/bin/python
PIP     := $(VENV)/bin/pip
PYTEST  := $(VENV)/bin/pytest
RUFF    := $(VENV)/bin/ruff
HTTP    := $(VENV)/bin/http
PORT    ?= 8000
IMAGE   ?= issues-gateway:local

.PHONY: help install run dev test test-unit test-integration test-tunnel cov lint fmt \
        spec examples ui-doc tunnel docker-build docker-run compose-up compose-tunnel clean env

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

$(VENV)/bin/activate: requirements-dev.txt requirements.txt
	python3 -m venv $(VENV)
	$(PIP) install --quiet --upgrade pip
	$(PIP) install --quiet -r requirements-dev.txt
	@touch $(VENV)/bin/activate

install: $(VENV)/bin/activate ## Create the virtualenv and install dependencies

env: ## Create .env from the template if it does not exist
	@test -f .env && echo ".env already exists, leaving it alone" || { \
		cp .env.example .env; \
		echo "Wrote .env -- now set GITHUB_TOKEN, GITHUB_OWNER, GITHUB_REPO."; \
		echo "Suggested WEBHOOK_SECRET: $$(openssl rand -hex 32)"; \
	}

run: install ## Run the service (reads .env)
	$(VENV)/bin/uvicorn app.main:app --host 0.0.0.0 --port $(PORT)

dev: install ## Run with auto-reload for development
	$(VENV)/bin/uvicorn app.main:app --host 127.0.0.1 --port $(PORT) --reload

test: install ## Run everything that needs no credentials (unit + mocked integration)
	$(PYTEST) -m "not integration and not tunnel"

test-unit: install ## Run the unit tests only
	$(PYTEST) tests/unit

test-integration: install ## Run tests against the real GitHub API (needs .env)
	$(PYTEST) -m integration

test-tunnel: install ## Run the end-to-end webhook tests (needs ngrok; see README)
	RUN_TUNNEL_TESTS=1 $(PYTEST) -m tunnel

cov: install ## Run the credential-free suite with a coverage report
	$(PYTEST) -m "not integration and not tunnel" --cov=app --cov-report=term-missing --cov-report=html
	@echo "HTML report: htmlcov/index.html"

lint: install ## Lint
	$(RUFF) check app tests docs
	$(RUFF) format --check app tests docs

fmt: install ## Auto-format and auto-fix
	$(RUFF) format app tests docs
	$(RUFF) check --fix app tests docs

spec: install ## Validate openapi.yaml as OpenAPI 3.1
	$(PY) -c "from openapi_spec_validator import validate; \
from openapi_spec_validator.readers import read_from_filename; \
spec, _ = read_from_filename('openapi.yaml'); validate(spec); \
print('openapi.yaml is valid OpenAPI', spec['openapi'])"

examples: install ## Exercise every route with HTTPie against a running service
	./scripts/httpie_examples.sh

ui-doc: install ## Rebuild docs/UI-Walkthrough.docx (service must be running)
	$(PIP) install --quiet -r docs/requirements.txt
	$(PY) docs/capture_ui.py
	$(PY) docs/build_walkthrough.py

tunnel: ## Expose the local service to GitHub with ngrok
	@command -v ngrok >/dev/null 2>&1 || { \
		echo "ngrok is not installed. brew install ngrok  (or https://ngrok.com/download)"; \
		exit 1; \
	}
	ngrok http $(PORT)

docker-build: ## Build the container image
	docker build -t $(IMAGE) .

docker-run: docker-build ## Run the container image with .env
	docker run --rm -it --name issues-gateway \
		--env-file .env -e PORT=8000 -e EVENT_STORE_PATH=/data/events.db \
		-p $(PORT):8000 -v issues-gateway-data:/data $(IMAGE)

compose-up: ## Start the gateway with docker compose
	docker compose up --build

compose-tunnel: ## Start the gateway plus ngrok (needs NGROK_AUTHTOKEN in .env)
	docker compose --profile tunnel up --build

clean: ## Remove build, test and cache artefacts
	rm -rf .pytest_cache .ruff_cache htmlcov .coverage coverage.xml data
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
