import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
API_ROOT = ROOT / "packages" / "api"
sys.path.insert(0, str(API_ROOT))

from fastapi import FastAPI

from app.contracts import (
    ApiErrorEnvelope,
    FactRecord,
    MatchCategory,
    ResumeSnapshot,
    SuggestionStatus,
    TaskRecord,
)
from app.modules.auth.router import router as auth_router
from app.modules.facts.router import router as facts_router
from app.modules.exports.router import router as exports_router
from app.modules.imports.router import router as imports_router
from app.modules.jobs.router import router as jobs_router
from app.modules.matching.router import router as matching_router
from app.modules.privacy.router import router as privacy_router
from app.modules.resumes.router import router as resumes_router
from app.modules.suggestions.router import router as suggestions_router
from app.modules.tasks.router import router as tasks_router
from app.modules.usage.router import router as usage_router


def build_application() -> FastAPI:
    app = FastAPI(title="AI Resume API", version="1")
    app.include_router(auth_router)
    app.include_router(usage_router)
    app.include_router(privacy_router)
    app.include_router(facts_router)
    app.include_router(resumes_router)
    app.include_router(imports_router)
    app.include_router(jobs_router)
    app.include_router(matching_router)
    app.include_router(suggestions_router)
    app.include_router(exports_router)
    app.include_router(tasks_router)

    @app.get("/contracts/fact", response_model=FactRecord)
    def fact_contract() -> FactRecord:
        raise NotImplementedError

    @app.get("/contracts/resume-snapshot", response_model=ResumeSnapshot)
    def resume_snapshot_contract() -> ResumeSnapshot:
        raise NotImplementedError

    @app.get(
        "/contracts/task",
        response_model=TaskRecord,
        responses={400: {"model": ApiErrorEnvelope}},
    )
    def task_contract() -> TaskRecord:
        raise NotImplementedError

    @app.get("/contracts/match-category", response_model=MatchCategory)
    def match_category_contract() -> MatchCategory:
        raise NotImplementedError

    @app.get("/contracts/suggestion-status", response_model=SuggestionStatus)
    def suggestion_status_contract() -> SuggestionStatus:
        raise NotImplementedError

    return app


def main() -> None:
    generated = ROOT / "packages" / "shared" / "generated"
    generated.mkdir(parents=True, exist_ok=True)
    openapi_path = generated / "openapi.json"
    schema_path = generated / "schema.ts"
    openapi_path.write_text(
        json.dumps(build_application().openapi(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    subprocess.run(
        ["pnpm", "exec", "openapi-typescript", str(openapi_path), "-o", str(schema_path)],
        check=True,
        cwd=ROOT,
    )


if __name__ == "__main__":
    main()
