# Backend: Agentic Dev/Test Copilot

FastAPI service with LangGraph agents, PostgreSQL + pgvector, and Alembic migrations. See `docs/Project Technical Spec.md` for the architecture and `docs/PRD.md` for product scope.

## Current state

- Dependencies are managed with **uv** (`pyproject.toml` + `uv.lock`). There is no `requirements.txt`.
- Only `GET /health` exists so far. The database models and the first migration are done; the API, agents, RAG and LLM modules are still empty stubs.
- **Docker Compose is not set up yet.** The spec schedules `infra/docker-compose.yml` and the backend/frontend Dockerfiles for Week 3. Until then, run the backend natively with uv and run Postgres in a single container (see below).

## Prerequisites

| Tool | Why | Install (macOS) |
| --- | --- | --- |
| uv | Python version + dependency management (Python 3.11 is pinned in `.python-version`; uv fetches it if missing) | `brew install uv` |
| Docker | Runs PostgreSQL 16 + pgvector locally | Docker Desktop |

You do not need to install Python 3.11 yourself. uv will download the pinned version.

## First-time setup

```bash
cd backend
uv sync          # creates .venv/ and installs runtime + dev dependencies from uv.lock
```

## Run the API

```bash
cd backend
uv run uvicorn app.main:app --reload --port 8000
```

Check it:

```bash
curl http://localhost:8000/health
# {"status":"ok"}
```

Interactive API docs (Swagger UI) are at http://localhost:8000/docs.

## Run the database (interim, until Docker Compose exists)

Run PostgreSQL 16 with the pgvector extension in one container:

```bash
docker run -d --name copilot-db \
  -e POSTGRES_USER=copilot \
  -e POSTGRES_PASSWORD=copilot \
  -e POSTGRES_DB=copilot \
  -p 5432:5432 \
  -v copilot-pgdata:/var/lib/postgresql/data \
  pgvector/pgvector:pg16
```

Stop and restart it later with `docker stop copilot-db` and `docker start copilot-db`. Remove it with `docker rm -f copilot-db`. The named volume keeps data across restarts. Delete it with `docker volume rm copilot-pgdata` if you want a clean database.

The credentials above are for local development only. Do not reuse them anywhere else.

Apply the schema (creates the `pgvector` extension and all tables):

```bash
cd backend
uv run alembic upgrade head
```

The connection URL defaults to the container above. Override it with `DATABASE_URL` in the environment or in `backend/.env` (see `.env.example`).

Other migration commands: `uv run alembic current`, `uv run alembic downgrade -1`, and `uv run alembic revision --autogenerate -m "message"` after changing `app/db/models.py`. Always read the generated file before applying it.

## Tests

```bash
cd backend
uv run pytest
```

The model tests use the real local Postgres, so start the container and run `alembic upgrade head` first. Each test rolls back its changes.

## Adding dependencies

```bash
uv add <package>          # runtime dependency
uv add --dev <package>    # dev-only dependency (e.g. test tooling)
```

Commit both `pyproject.toml` and `uv.lock`.

## Planned (from the spec)

| Week | Addition |
| --- | --- |
| 1 | Schema and migration (done); `/tickets` CRUD; LangGraph orchestrator skeleton; GitHub OAuth callback |
| 3 | `Dockerfile` for the backend; `infra/docker-compose.yml` wiring Postgres + backend + frontend. Then `docker compose up` replaces the manual steps above |
| 9 | Kubernetes manifests in `infra/k8s/` for local `kind`/`minikube` |

The spec's folder tree lists a `requirements.txt` and a `Dockerfile` under `backend/`. The project uses uv instead of `requirements.txt`. If a Dockerfile is needed for Week 3, export it from uv at that point.
