
# ─────────────────────────────────────────────────────────────────────────────
#  BugForge — dev harness
# ─────────────────────────────────────────────────────────────────────────────

COMPOSE ?= docker compose
DC      := $(COMPOSE) -f docker-compose.yml

# Anything after the target name on the command line (used by `make logs api`)
ARGS := $(filter-out $@,$(MAKECMDGOALS))

.DEFAULT_GOAL := help
.PHONY: help up down build reset seed ghost logs psql ps schemas migrate restart clean

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

up: ## Build if needed and start every service, waiting for health
	$(DC) up -d --build --wait
	@echo ""
	@echo "  web         http://localhost:3000"
	@echo "  api         http://localhost:8000"
	@echo "  collector   http://localhost:8001"
	@echo "  supportdesk http://localhost:3001"
	@echo "  gitea       http://localhost:3002"
	@echo "  db          postgres://bugforge:bugforge@localhost:5432/bugforge"

down: ## Stop and remove containers (volumes are kept)
	$(DC) down --remove-orphans

build: ## Rebuild all images from scratch
	$(DC) build --no-cache

ps: ## Show container status
	$(DC) ps

restart: ## Restart a service, e.g. make restart api
	$(DC) restart $(ARGS)

logs: ## Tail logs, e.g. make logs api
	$(DC) logs -f --tail=200 $(ARGS)

psql: ## Open a psql shell on the bugforge database
	$(DC) exec db psql -U bugforge -d bugforge

schemas: ## Drop and recreate the shop + telemetry schemas and all tables
	$(DC) exec -T api python -c "from app.db import reset_database; reset_database()"
	$(DC) exec -T collector python -c "from app.models import reset_database; reset_database()"

migrate: ## Create any missing schemas/tables without dropping data
	$(DC) exec -T api python -c "from app.db import create_all; create_all()"
	$(DC) exec -T collector python -c "from app.models import create_all; create_all()"

seed: ## Load the deterministic base seed (users, products, coupons, orders, tickets)
	$(DC) exec -T api python /srv/scripts/seed.py

ghost: ## Re-run every ghost run, producing authentic historical telemetry
	$(DC) exec -T api python /srv/scripts/ghosts/run_all.py

reset: ## Full deterministic reset: schemas -> seed -> ghost runs
	@echo "==> ensuring the stack is up"
	$(DC) up -d --wait
	@echo "==> dropping + recreating schemas shop and telemetry"
	$(MAKE) schemas
	@echo "==> seeding base data"
	$(MAKE) seed
	@echo "==> running ghost runs"
	$(MAKE) ghost
	@echo "==> reset complete"

clean: ## Stop everything and delete volumes (destroys the database)
	$(DC) down -v --remove-orphans

# Swallow extra goals so `make logs api` does not try to build a target named api
%:
	@:

test:  ## Run the api test suite
	docker compose exec -T api python -m pytest /srv/tests -q
