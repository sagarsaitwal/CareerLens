# CareerLens — development commands.
#
# Every target runs inside WSL2. Nothing here requires Python, Node,
# Docker or any CareerLens dependency to be installed on Windows.
# See docs/SYSTEM-REQUIREMENTS.md "Development and Deployment Environment".

.PHONY: help preflight up down logs build migrate revision \
        test test-backend test-frontend lint typecheck fresh ps

help:
	@echo "CareerLens — run all of these inside WSL2"
	@echo ""
	@echo "  make preflight       verify WSL2, Docker and .env before starting"
	@echo "  make up              start the stack (api, worker, postgres, redis, nginx)"
	@echo "  make down            stop the stack"
	@echo "  make ps              show service status"
	@echo "  make logs            follow logs"
	@echo "  make build           rebuild images"
	@echo "  make migrate         apply Alembic migrations"
	@echo "  make revision m=\"..\" create a migration (review by hand)"
	@echo "  make test            backend + frontend tests"
	@echo "  make lint            ruff"
	@echo "  make typecheck       mypy + tsc"
	@echo "  make fresh           destroy volumes and rebuild from scratch"

# Fails early with an actionable message rather than letting Compose emit
# a confusing interpolation error.
preflight:
	@test -f /proc/version && grep -qi microsoft /proc/version \
		|| (echo "ERROR: not running inside WSL2. CareerLens is WSL2-only." && exit 1)
	@command -v docker >/dev/null \
		|| (echo "ERROR: docker not found in WSL2. Install Docker Engine inside your distro." && exit 1)
	@docker compose version >/dev/null 2>&1 \
		|| (echo "ERROR: 'docker compose' unavailable. Install the compose plugin." && exit 1)
	@docker info >/dev/null 2>&1 \
		|| (echo "ERROR: Docker daemon not reachable. Start it: sudo service docker start" && exit 1)
	@test -f .env \
		|| (echo "ERROR: .env missing. Run: cp .env.example .env" && exit 1)
	@echo "preflight OK"

up: preflight
	docker compose up -d
	@echo ""
	@echo "frontend  http://localhost:8080"
	@echo "api docs  http://localhost:8000/api/docs"
	@echo "health    curl http://localhost:8080/api/health"

down:
	docker compose down

ps:
	docker compose ps

logs:
	docker compose logs -f

build: preflight
	docker compose build

migrate:
	docker compose exec api alembic upgrade head

revision:
	@test -n "$(m)" || (echo 'usage: make revision m="description"' && exit 1)
	docker compose exec api alembic revision --autogenerate -m "$(m)"
	@echo ""
	@echo "Review the generated migration by hand. Autogenerate does not"
	@echo "emit triggers, partial indexes or check constraints."

# Integration tests run against a real PostgreSQL on the test profile.
# SQLite is never substituted: constraint behaviour is part of the spec.
#
# The URL is assembled inside the container, where .env is already
# loaded, so it does not depend on the host shell exporting anything.
test-backend: preflight
	docker compose --profile test up -d postgres_test
	docker compose exec -T api sh -c \
		'TEST_DATABASE_URL="postgresql+psycopg://$$POSTGRES_USER:$$POSTGRES_PASSWORD@postgres_test:5432/$${POSTGRES_DB}_test" pytest -q'

# Frontend tooling runs from the WSL2 shell, per the documented
# environment (Node/npm inside WSL2, never on Windows).
test-frontend:
	cd frontend && npm run test

test: test-backend test-frontend

lint:
	docker compose exec -T api ruff check app tests

typecheck:
	docker compose exec -T api mypy app
	cd frontend && npm run typecheck

fresh: preflight
	docker compose down -v
	docker compose build --no-cache
	docker compose up -d
