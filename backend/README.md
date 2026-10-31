# Backend: Agentic Dev/Test Copilot

FastAPI service with LangGraph agents, PostgreSQL + pgvector, and Alembic migrations. See `docs/Project Technical Spec.md` for the architecture and `docs/PRD.md` for product scope.

## Current state

- Dependencies are managed with **uv** (`pyproject.toml` + `uv.lock`). There is no `requirements.txt`.
- Working: `GET /health`, GitHub sign-in (`/auth/*`), `/tickets` CRUD, the database models and migrations. Agents, RAG, LLM and the remaining routers are still empty stubs.
- Every `/tickets` route requires a signed-in session. There is no development bypass.
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

## Configure

Copy `.env.example` to `.env` and fill it in. **The API refuses to start if a required value is missing**, so nothing runs with insecure defaults.

| Variable | Purpose |
| --- | --- |
| `DATABASE_URL` | Postgres connection (psycopg v3 driver) |
| `SECRET_KEY` | Signs the OAuth state cookie. At least 32 characters: `openssl rand -hex 32` |
| `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET` | From your GitHub App (below) |
| `GITHUB_REDIRECT_URI` | Must exactly match the app's callback URL |
| `FRONTEND_URL` | Where users land after sign-in; also the only allowed CORS origin |
| `COOKIE_SECURE` | `true` by default (required by the `__Host-` cookie prefix) |
| `ENABLE_DOCS` | `false` by default. Set `true` locally for Swagger UI at `/docs` |

Running only migrations needs just `DATABASE_URL`.

### Register the GitHub App (one-time)

One GitHub App handles sign-in now and repo access later (Week 2), so you register it once.

GitHub > Settings > Developer settings > GitHub Apps > **New GitHub App**, then:

| Field | Value |
| --- | --- |
| GitHub App name | anything unique, e.g. `agentic-copilot-dev` |
| Homepage URL | `http://localhost:5173` |
| Callback URL | `http://localhost:8000/auth/github/callback` (must equal `GITHUB_REDIRECT_URI`) |
| Expire user authorization tokens | leave on |
| Request user authorization (OAuth) during installation | off |
| Webhook > Active | **off** (nothing receives webhooks yet) |
| Account permissions > Email addresses | **Read-only** |
| Repository permissions | none for now |
| Where can this GitHub App be installed? | **Any account** |

"Any account" matters: GitHub only lets members of the owning organisation authorize a *private* app, so with "Only on this account" nobody else could sign in.

Then on the app's page: copy the **Client ID**, click **Generate a new client secret**, and put both in `backend/.env`. Ignore the App ID and private key until Week 2. Never commit any of these. Use a separate GitHub App per deployed environment.

Local HTTPS note: the session cookie uses the `__Host-` prefix, which needs `Secure`. Chrome and Firefox accept it on `http://localhost`; Safari does not, so use Chrome or Firefox for local sign-in.

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

With `ENABLE_DOCS=true`, interactive API docs (Swagger UI) are at http://localhost:8000/docs.

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

The connection URL comes from `DATABASE_URL` (see Configure above).

Other migration commands: `uv run alembic current`, `uv run alembic downgrade -1`, and `uv run alembic revision --autogenerate -m "message"` after changing a model in `app/db/models/`. Always read the generated file before applying it.

## Tests

```bash
cd backend
uv run pytest
```

Tests need the Postgres container running. They use their own `copilot_test` database, which is created and migrated automatically, so your development data is never touched. Each test rolls back its changes. GitHub is faked, so no credentials or network are needed.

```bash
uv run ruff check . && uv run ruff format --check .   # lint and formatting
```

## Code layout

Requests flow in one direction; each layer only knows the one below it.

| Layer | Path | Responsibility |
| --- | --- | --- |
| Routes | `app/api/*.py` | Parse the request, call a service, shape the response. No business logic, no queries. |
| Wiring | `app/api/deps.py` | Builds services and the current user for each request (FastAPI dependencies) |
| Schemas | `app/schemas/` | Request/response models (the HTTP contract) |
| Services | `app/services/` | Use cases and business rules; own the transaction (`commit`). No FastAPI imports. |
| Repositories | `app/repositories/` | Database queries only |
| Integrations | `app/integrations/` | Clients for external systems (GitHub) |
| Models | `app/db/models/` | SQLAlchemy tables, one module per aggregate |
| Core | `app/core/` | Settings, security helpers, domain exceptions |

Services raise domain exceptions (`app/core/exceptions.py`); `app/api/errors.py` turns them into HTTP responses.

## Adding dependencies

```bash
uv add <package>          # runtime dependency
uv add --dev <package>    # dev-only dependency (e.g. test tooling)
```

Commit both `pyproject.toml` and `uv.lock`.

## Planned (from the spec)

| Week | Addition |
| --- | --- |
| 1 | Schema, migrations, `/tickets` CRUD and GitHub sign-in (done); LangGraph orchestrator skeleton |
| 2 | Repo-connect flow (needs its own GitHub permission decision, see learnings 03) |
| 3 | `Dockerfile` for the backend; `infra/docker-compose.yml` wiring Postgres + backend + frontend. Then `docker compose up` replaces the manual steps above |
| 9 | Kubernetes manifests in `infra/k8s/` for local `kind`/`minikube` |

The spec's folder tree lists a `requirements.txt` and a `Dockerfile` under `backend/`. The project uses uv instead of `requirements.txt`. If a Dockerfile is needed for Week 3, export it from uv at that point.
