# Dependencies

Versions were queried from npm and PyPI and resolved on 2026-07-27. JavaScript
dependencies are pinned in `package.json` and `pnpm-lock.yaml`; Python runtime
dependencies are pinned in `packages/api/requirements.lock`.

| Dependency | Version | Purpose | License |
| --- | --- | --- | --- |
| FastAPI | 0.140.0 | Defines and exports the API contract source. | MIT |
| Pydantic | 2.13.4 | Validates strict API DTOs. | MIT |
| Uvicorn | 0.51.0 | Runs the API development server after Task 2. | BSD-3-Clause |
| SQLAlchemy | 2.0.51 | Provides PostgreSQL-capable async persistence models and repositories. | MIT |
| Alembic | 1.18.5 | Applies versioned database migrations. | MIT |
| asyncpg | 0.31.0 | Provides the production PostgreSQL async driver. | Apache-2.0 |
| greenlet | 3.5.4 | Enables SQLAlchemy's async bridge. | MIT |
| cryptography | 49.0.0 | Encrypts normalized email addresses with authenticated AES-GCM. | Apache-2.0 OR BSD-3-Clause |
