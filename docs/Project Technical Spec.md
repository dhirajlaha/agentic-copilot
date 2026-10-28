# Project Technical Spec: Agentic Dev/Test Copilot

Exact stack, schema, folder structure and week-by-week build tasks — ready to start coding from. Point ingestion at a real public open-source repo (not a private sandbox) so the demo solves real tickets, not synthetic ones. Positioned as a SaaS: users sign in and connect their own GitHub repo via OAuth, rather than you manually pointing it at one.

## Tech Stack (exact)

| Layer | Choice | Notes |
| --- | --- | --- |
| Backend | Python 3.11, FastAPI | matches your existing FastAPI experience |
| Agent orchestration | LangGraph | state-machine style graph of agents; conditional edges for retry loops |
| LLM (weeks 1-7) | Anthropic Claude API or OpenAI API | whichever you already have a key for; swapped out in week 8 |
| LLM (weeks 8+) | Llama-3-8B-Instruct served via vLLM, AWQ-quantized | vLLM implements PagedAttention natively — this is the inference-optimization bonus |
| Embeddings | `bge-small-en-v1.5` via `sentence-transformers` (local, free) or `text-embedding-3-small` (API) | local keeps the whole RAG pipeline self-hosted and cost-free |
| Frontend | React 18 + Vite + TypeScript + TanStack Query | TanStack Query handles the polling for live agent-run status cleanly |
| Database | PostgreSQL 16 + `pgvector` extension | one DB for both relational state and vector search — simpler than a separate vector DB |
| Migrations | Alembic | standard with SQLAlchemy |
| ORM | SQLAlchemy 2.0 |  |
| Test sandbox | Python `subprocess` with CPU/time/memory limits (`resource` module), or Docker-in-Docker if isolation needs are higher | start with subprocess; upgrade only if you hit real isolation problems |
| Containerization | Docker + Docker Compose (local) |  |
| Orchestration (cloud) | Kubernetes — `kind`/`minikube` locally, AWS EKS in prod |  |
| CI/CD | GitHub Actions | build, test, push to ECR, deploy |
| Cloud | AWS (EC2/EKS, ECR, IAM) | matches the cloud-knowledge gap you flagged |
| Auth | GitHub OAuth App (sign-in + repo scopes) | lets users connect their own repo; request minimal scopes |
| Landing/positioning | Static marketing + pricing page; Stripe test mode optional, no live billing needed | gives the SaaS story without real payment-processing scope creep |

## Repo Folder Structure

```
agentic-dev-copilot/
├── backend/
│   ├── app/
│   │   ├── main.py
│   │   ├── api/              # FastAPI routers: tickets.py, runs.py, rag.py, benchmarks.py
│   │   ├── agents/           # orchestrator.py, planner.py, context.py, coder.py, test_gen.py, test_exec.py, reporter.py
│   │   ├── rag/               # chunker.py, ingest.py, retriever.py
│   │   ├── llm/                # client.py (provider-agnostic interface), api_client.py, vllm_client.py
│   │   ├── db/                  # models.py, session.py, migrations/ (alembic)
│   │   └── core/                # config.py, logging.py
│   ├── tests/
│   ├── Dockerfile
│   └── requirements.txt
├── frontend/
│   ├── src/
│   │   ├── components/        # TicketList, RunDetail, AgentTraceTimeline, BenchmarkChart
│   │   ├── pages/
│   │   ├── api/                 # fetch wrappers / generated client
│   │   └── hooks/               # useRunStatus (polling), useTickets
│   ├── Dockerfile
│   └── package.json
├── infra/
│   ├── docker-compose.yml
│   ├── k8s/
│   │   ├── base/               # deployment.yaml + service.yaml per component
│   │   └── overlays/            # local/, prod/
│   └── eksctl-config.yaml       # or Terraform, week 10
├── docs/
│   └── architecture.md          # the README that does the talking in interviews
└── .github/workflows/ci.yml
```

**SaaS additions**: `backend/app/api/auth.py` (GitHub OAuth callback) and `backend/app/api/repos.py` (connect/list/disconnect repos); `frontend/src/pages/Landing.tsx` (public marketing/pricing page) alongside the dashboard pages. The agents/rag/llm folders are unchanged — they now operate per `repo_connection_id` instead of a single hardcoded repo.

## Database Schema (Postgres + pgvector)

| Table | Key columns | Purpose |
| --- | --- | --- |
| users | id, github\_id, email, created\_at | one row per signed-up user |
| repo\_connections | id, user\_id (FK), repo\_full\_name, installation\_token (encrypted), connected\_at | one row per repo a user has connected via GitHub OAuth/App |
| `tickets` | id, user\_id (FK), title, description, repo\_connection\_id (FK), status, created\_at | a feature request, PR diff, or GitHub issue fed into the system, scoped to one connected repo |
| `agent_runs` | id, ticket\_id (FK), status, started\_at, finished\_at | one orchestrator run per ticket |
| `agent_steps` | id, run\_id (FK), agent\_name, input, output, tokens\_used, latency\_ms, created\_at | every agent step, for the live trace dashboard |
| `code_chunks` | id, repo\_connection\_id (FK), file\_path, content, embedding `vector(384)`, updated\_at | RAG index — `embedding` is the pgvector column (384 dims for bge-small); always filtered by repo\_connection\_id so tenants' code never mixes |
| `test_results` | id, run\_id (FK), test\_name, status, output, created\_at | pass/fail output from the Test Executor |
| `benchmark_runs` | id, model\_name, quantization, avg\_latency\_ms, throughput\_tokens\_per\_sec, cost\_estimate, created\_at | the API-vs-self-hosted comparison from week 8 |

All migrations managed via Alembic; `code_chunks.embedding` needs an IVFFlat or HNSW index once you have more than a few thousand chunks.

## API Contracts

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/tickets` | create a ticket for a connected repo (title, description, repo\_connection\_id) |
| POST | `/tickets/{id}/run` | kick off an orchestrator run for that ticket |
| GET | `/runs/{id}` | run status + full step trace (polled by the dashboard every 1-2s) |
| GET | `/runs/{id}/stream` | optional SSE/WebSocket for live trace instead of polling |
| POST | `/rag/ingest` | index a connected repo (by repo\_connection\_id) into `code_chunks` |
| GET | `/rag/chunks?q=` | debug endpoint: see what the Context Agent would retrieve for a query |
| GET | `/benchmarks` | list benchmark runs for the API-vs-self-hosted comparison view |
| GET | /auth/github/callback | completes GitHub OAuth, creates/looks up the user, issues a session |
| GET | /repos | list the signed-in user's connected repos |
| POST | /repos/connect | start the GitHub App install/OAuth flow for a new repo |
| DELETE | /repos/{id} | disconnect a repo and delete its indexed data |

Start with polling (`GET /runs/{id}`) in week 3 — it's simpler and good enough for a demo; add the stream endpoint later only if you want the extra polish.

## Agent Logic

The orchestrator is a LangGraph state machine with one node per agent below; edges are conditional on the state returned by each node.

1. **Planner Agent** — takes the ticket's title/description, breaks it into sub-tasks, and makes a first guess at which files likely need touching (from repo structure/filenames, not embeddings yet).
2. **Context Agent (RAG)** — takes the planner's sub-tasks, queries `code_chunks` via pgvector for the top-k relevant snippets, and assembles a trimmed context window for the next step. Always scoped to the ticket's repo\_connection\_id so tenants never see each other's code.
3. **Coder step** — given the ticket + retrieved context, generates a diff/patch. On a retry (see below), it also receives the previous attempt's failing test output.
4. **Test Generator** — given the diff, generates pytest test cases covering the new/changed logic.
5. **Test Executor** — runs the generated tests in a sandboxed subprocess (time + resource limits), captures pass/fail and output; run in an isolated, network-disabled container once this is publicly deployed, not a bare host subprocess (see PRD — Risks).
6. **Reporter** — summarizes the run, writes everything to `agent_runs`/`agent_steps`/`test_results`, and formats it for the dashboard.

**Retry condition**: if Test Executor reports failures, the graph routes back to the Coder step with the failure output included in context — capped at 2 retries, after which the run is marked `needs_human_review` rather than looping forever. This retry loop is itself a good interview talking point for "agentic test automation."

## Week-by-Week Build Tasks

| Week | Build task | Files/components touched |
| --- | --- | --- |
| 1 | Repo scaffold; Postgres schema + Alembic migration for `tickets`/`agent_runs`/`agent_steps`; FastAPI skeleton with `/tickets` CRUD; LangGraph orchestrator with a single no-op node; GitHub OAuth login scaffold (/auth/github/callback) | `backend/app/db`, `backend/app/api`, `agents/orchestrator.py` |
| 2 | Planner agent (prompt + parsing); repo-connect flow storing the OAuth installation token; Coder step producing a text diff from the raw ticket (no RAG yet); wired into orchestrator; steps logged to `agent_steps` | `agents/planner.py`, `agents/coder.py` |
| 3 | React dashboard (ticket list, run-detail view polling `GET /runs/{id}`); Dockerfiles for backend + frontend; `docker-compose.yml` wiring Postgres + backend + frontend; a public landing/pricing page (frontend/src/pages/Landing.tsx) | `frontend/`, `infra/docker-compose.yml` |
| 4 | `pgvector` extension + `code_chunks` table; `chunker.py`; `ingest.py` to embed a real public open-source repo; Context Agent querying top-k chunks | `rag/`, `agents/context.py` |
| 5 | Hybrid (keyword + vector) search in `retriever.py`; a small retrieval eval script (precision@k against \~15 queries drawn from that repo's closed issues with a merged fix (your ground truth — see the PRD tab)); Coder's context now sourced from the Context Agent | `rag/retriever.py`, `tests/eval_retrieval.py` |
| 6 | Test Generator agent; sandboxed Test Executor; `test_results` table; orchestrator retry loop on failure (max 2 retries) | `agents/test_gen.py`, `agents/test_exec.py` |
| 7 | No new features — deep-read week on KV cache + PagedAttention (feeds week 8); optionally add token/latency logging to the current API-based LLM calls as a baseline for week 8's benchmark | `llm/api_client.py` |
| 8 | Stand up a vLLM server (Llama-3-8B-Instruct, AWQ-quantized) in its own container; `vllm_client.py` implementing the same interface as `api_client.py`; `benchmark_runs` table + comparison script | `llm/vllm_client.py`, infra (vLLM service) |
| 9 | Kubernetes manifests for every component (backend, frontend, Postgres, vLLM); full stack running on local `kind`/`minikube` | `infra/k8s/base` |
| 10 | AWS EKS cluster (`eksctl`); push images to ECR; deploy manifests to EKS; GitHub Actions CI/CD (build, push, deploy on merge to main) | `infra/k8s/overlays/prod`, `.github/workflows/ci.yml` |
| 11 | Polish only: `docs/architecture.md` with the diagram, benchmark numbers, and before/after results on the real GitHub issues you ran it against; basic health checks/logging; this is also the interview-sprint week in the main plan, so keep project work light; also polish the landing/pricing page copy with the real benchmark numbers |  `docs/architecture.md` |
