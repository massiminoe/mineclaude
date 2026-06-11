.DEFAULT_GOAL := help

COMPOSE      := docker compose
COMPOSE_ARM  := docker compose -f docker-compose.yml -f docker-compose.arm64.yml

.PHONY: help
help: ## Show this help
	@grep -E '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

## --- Docker stack (amd64 / emulated) ---

.PHONY: up
up: ## Build + start the full stack (detached)
	$(COMPOSE) up --build -d

.PHONY: down
down: ## Stop the stack
	$(COMPOSE) down

.PHONY: clean
clean: ## Stop the stack and wipe volumes (regenerates ops)
	$(COMPOSE) down -v

.PHONY: logs
logs: ## Tail mc-client logs
	$(COMPOSE) logs -f mc-client

## --- Docker stack (arm64 native) ---

.PHONY: up-arm
up-arm: ## Build + start the arm64-native stack (detached)
	$(COMPOSE_ARM) up --build -d

.PHONY: down-arm
down-arm: ## Stop the arm64-native stack
	$(COMPOSE_ARM) down

.PHONY: clean-arm
clean-arm: ## Stop the arm64-native stack and wipe volumes
	$(COMPOSE_ARM) down -v

.PHONY: logs-arm
logs-arm: ## Tail mc-client logs (arm64 stack)
	$(COMPOSE_ARM) logs -f mc-client

## --- Python / app ---

.PHONY: run
run: ## Run the MCP launcher
	.venv/bin/mineclaude

.PHONY: run-mock
run-mock: ## Run the MCP launcher with a mock bridge (no MC server)
	MOCK_BRIDGE=1 .venv/bin/mineclaude

.PHONY: test
test: ## Run the test suite
	.venv/bin/pytest

.PHONY: test-e2e
test-e2e: ## Run the opt-in e2e tests
	.venv/bin/pytest --run-e2e

.PHONY: skill-docs
skill-docs: ## Regenerate generated skill docs from code
	.venv/bin/python scripts/gen_skill_docs.py

## --- Frontend ---

.PHONY: frontend
frontend: ## Run the frontend dev server
	cd frontend && npm run dev

.PHONY: frontend-build
frontend-build: ## Production build of the frontend
	cd frontend && npx vite build
