.DEFAULT_GOAL := help
SHELL := /usr/bin/env bash
.SHELLFLAGS := -euo pipefail -c

# Pull project secrets from .env (sourced into the shell at recipe time)
ifneq (,$(wildcard .env))
  include .env
  export
endif

PROJECT ?= eshop

.PHONY: help bootstrap build warmup run-build run-test run-all ps logs destroy clean nuke \
        run-task tasks-ls tasks-show tasks-memory \
        install-gvisor verify-gvisor

help:  ## Show this help
	@awk 'BEGIN{FS=":.*##"; printf "\nUsage: make \033[36m<target>\033[0m\n\nTargets:\n"} \
	      /^[a-zA-Z_-]+:.*?##/ {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

# ── one-time bootstrap ──

bootstrap:  ## Generate .env (DB passwords)
	@mkdir -p out
	@[ -f .env ] || { \
	    MSSQL_PW="$$(openssl rand -base64 16 | tr -d '/+=' | head -c 24)A1!"; \
	    PG_PW="$$(openssl rand -hex 16)"; \
	    printf 'MSSQL_SA_PASSWORD=%s\nPOSTGRES_PASSWORD=%s\n' "$$MSSQL_PW" "$$PG_PW" > .env && \
	    echo "✓ generated .env"; \
	}

# ── build sandbox images ──

build:  ## Build a project's sandbox image: `make build PROJECT=eshop`
	python3 harness/sandbox.py build $(PROJECT)

# ── warm baseline (one-time per image; subsequent runs restore from it) ──

warmup:  ## Cold-start once, run prep, `docker commit` warm baselines
	python3 harness/sandbox.py warmup $(PROJECT)

# ── run a stage end-to-end ──

run-build:  ## Run the build stage end-to-end: `make run-build PROJECT=eshop`
	python3 harness/sandbox.py run $(PROJECT) build

run-test:   ## Run the test stage end-to-end (uses :warm if present)
	python3 harness/sandbox.py run $(PROJECT) test

run-all:    ## Run build + test in one stage (no caching between)
	python3 harness/sandbox.py run $(PROJECT) all

# ── agent loop: task-scoped iterations with persistent memory ──
# Usage:
#   make run-task PROJECT=eshop TASK=fix-health SOURCE=~/work/eshop
# Or just the task without source:
#   make run-task PROJECT=eshop TASK=fix-health

run-task:   ## Run with persistent task memory: `make run-task TASK=fix-health [SOURCE=~/work/eshop]`
	@: $${TASK:?set TASK=<task-id>}
	python3 harness/sandbox.py run $(PROJECT) $${STAGE:-test} \
	  --task "$$TASK" \
	  $${SOURCE:+--source "$$SOURCE"}

tasks-ls:   ## List all tasks with iteration history
	python3 harness/sandbox.py tasks ls

tasks-show: ## Show one task's iteration history: `make tasks-show TASK=fix-health`
	@: $${TASK:?set TASK=<task-id>}
	python3 harness/sandbox.py tasks show $(PROJECT) "$$TASK"

tasks-memory: ## Print the host path to a task's memory dir
	@: $${TASK:?set TASK=<task-id>}
	python3 harness/sandbox.py tasks memory $(PROJECT) "$$TASK"

# ── inspect / debug ──

ps:         ## docker compose ps for the project
	python3 harness/sandbox.py ps $(PROJECT)

logs:       ## docker compose logs for the project
	python3 harness/sandbox.py logs $(PROJECT)

destroy:    ## Tear down the stack
	python3 harness/sandbox.py destroy $(PROJECT)

# ── cleanup ──

clean:      ## Remove generated artifacts (out/)
	rm -rf out

nuke: clean ## Also remove warm baselines + sandbox images
	@for tag in latest warm; do \
	  docker rmi ai-harness/$(PROJECT):$$tag 2>/dev/null || true; \
	done
	@for svc in sqlserver postgres redis; do \
	  docker rmi ai-harness/$(PROJECT)-$$svc:warm 2>/dev/null || true; \
	done

# ── optional: gVisor kernel isolation (host setup) ──

install-gvisor:  ## Install runsc (gVisor) and register with Docker (sudo)
	@bash scripts/install-gvisor.sh

verify-gvisor:   ## Verify runsc is intercepting
	@bash scripts/verify-gvisor.sh
