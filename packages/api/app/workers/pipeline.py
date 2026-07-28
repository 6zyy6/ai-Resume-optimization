from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.integrations.ai_client import InternalAiClient
from app.integrations.ai_client import AiClient
from app.integrations.storage import StoragePort, build_storage
from app.modules.exports.service import ExportService
from app.modules.imports.service import ImportService
from app.modules.jobs.service import JobService
from app.modules.matching.service import MatchingService
from app.modules.tasks.service import TaskClaim, TaskService
from app.workers.execution import register_operation


def configure_pipeline_operations(
    sessions: async_sessionmaker[AsyncSession],
    settings: Settings,
    task_service: TaskService,
    *,
    storage_override: StoragePort | None = None,
    ai_client_override: AiClient | None = None,
) -> None:
    storage = storage_override or build_storage(settings)
    ai_client = ai_client_override or (
        InternalAiClient(settings.ai_internal_url, settings.ai_service_token)
        if settings.ai_service_token
        else None
    )
    imports = ImportService(sessions, storage)
    jobs = JobService(sessions, ai_client)
    matching = MatchingService(sessions, ai_client)
    exports = ExportService(sessions, storage)

    async def parse_resume_import(claim: TaskClaim) -> str:
        task = await _task(task_service, claim)
        return await imports.process_import(
            claim.owner_user_id, _resource(task, "resume_import")
        )

    async def parse_job(claim: TaskClaim) -> str:
        task = await _task(task_service, claim)
        return await jobs.process_parse(
            claim.owner_user_id,
            _resource(task, "job_description"),
            trace_id=task.trace_id,
            task_id=task.id,
        )

    async def match_resume_to_job(claim: TaskClaim) -> str:
        task = await _task(task_service, claim)
        return await matching.process_match(
            claim.owner_user_id,
            _resource(task, "match_analysis"),
            trace_id=task.trace_id,
            task_id=task.id,
        )

    async def render_resume_export(claim: TaskClaim) -> str:
        task = await _task(task_service, claim)
        return await exports.process_export(
            claim.owner_user_id, _resource(task, "export")
        )

    register_operation("parse_resume_import", parse_resume_import)
    register_operation("parse_job", parse_job)
    register_operation("match_resume_to_job", match_resume_to_job)
    register_operation("render_resume_export", render_resume_export)


async def _task(service: TaskService, claim: TaskClaim):
    task = await service.get_task(claim.owner_user_id, claim.task_id)
    if task is None:
        raise RuntimeError("Claimed pipeline task is missing")
    return task


def _resource(task, expected_type: str) -> str:
    if task.resource_type != expected_type or not task.resource_id:
        raise RuntimeError("Pipeline task resource is invalid")
    return task.resource_id
