# Dependencies

Versions were queried from npm and PyPI and resolved on 2026-07-27. JavaScript
dependencies are pinned in `package.json` and `pnpm-lock.yaml`; Python runtime
dependencies are pinned in `packages/api/requirements.lock`.

| Dependency | Version | Purpose | License |
| --- | --- | --- | --- |
| FastAPI | 0.140.0 | Defines and exports the API contract source. | MIT |
| Pydantic | 2.13.4 | Validates strict API DTOs. | MIT |
| Uvicorn | 0.51.0 | Runs the API development server after Task 2. | BSD-3-Clause |
