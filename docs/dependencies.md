# Dependencies

Versions were queried from npm and PyPI and resolved on 2026-07-27 and
2026-07-28. JavaScript dependencies are pinned in `package.json` and
`pnpm-lock.yaml`; Python runtime dependencies are pinned in
`packages/api/requirements.lock`.

| Dependency | Version | Purpose | License |
| --- | --- | --- | --- |
| FastAPI | 0.140.0 | Defines and exports the API contract source. | MIT |
| Pydantic | 2.13.4 | Validates strict API DTOs. | MIT |
| Uvicorn | 0.51.0 | Runs the API development server after Task 2. | BSD-3-Clause |
| SQLAlchemy | 2.0.51 | Provides PostgreSQL-capable async persistence models and repositories. | MIT |
| Alembic | 1.18.5 | Applies versioned database migrations. | MIT |
| asyncpg | 0.31.0 | Provides the production PostgreSQL async driver. | Apache-2.0 |
| Redis | 8.0.1 | Provides shared TTL-backed OTP challenges and atomic authentication rate limits. | MIT |
| Celery | 5.6.3 | Dispatches durable Outbox tasks to named Redis-backed worker queues. | BSD-3-Clause |
| greenlet | 3.5.4 | Enables SQLAlchemy's async bridge. | MIT |
| cryptography | 49.0.0 | Encrypts normalized email addresses with authenticated AES-GCM. | Apache-2.0 OR BSD-3-Clause |
| @earendil-works/pi-ai | 0.82.1 | Provides provider-owned model streaming, structured calls, usage and cost data. | MIT |
| @earendil-works/pi-agent-core | 0.82.1 | Runs the bounded `next_question` tool loop. | MIT |
| Fastify | 5.10.0 | Serves the loopback/internal Pi run API. | MIT |
| TypeBox | 1.3.8 | Validates workflow, tool and model-output schemas. | MIT |

## Pi service environment

The checked-in `.env.example` contains placeholders only. The service reads
the process environment directly and does not load a repository `.env` file.

| Variable | Requirement |
| --- | --- |
| `AI_RUNTIME_MODE` | Defaults to `production`; `fixture` enables deterministic, network-free tests. |
| `AI_SERVICE_TOKEN` | Required in production; inject a short-lived random Bearer service token. |
| `AI_MODEL_ROUTES_JSON` | Required in production; configures all eight workflows with approved primary/fallback provider and model, `max_tokens`, `thinking`, `timeout_ms`, `retry_count` and `max_cost_usd`. |
| `AI_HOST` | Optional; defaults to loopback `127.0.0.1`. |
| `AI_PORT` | Optional; defaults to `3101`. |
| `OPENAI_API_KEY` | Required only when an approved route selects provider `openai`. |
| `ANTHROPIC_API_KEY` or `ANTHROPIC_OAUTH_TOKEN` | Required only when an approved route selects provider `anthropic`. |
| `GEMINI_API_KEY` | Required only when an approved route selects provider `google`. |

Production readiness requires a service token, complete approved routes for
all eight workflows, and environment credentials for both selected providers
on each route. Provider credentials are never accepted in route JSON, request
bodies, trace metadata or error responses.
