from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.integrations.ai_client import (
    AiCancellation,
    AiClient,
    InternalAiClient,
)
from app.integrations.storage import StoragePort, build_storage
from app.modules.exports.service import ExportService
from app.modules.imports.service import ImportService
from app.modules.intake.service import IntakeService
from app.modules.jobs.service import JobService
from app.modules.matching.service import MatchingService
from app.modules.privacy.worker import PrivacyWorker
from app.modules.tasks.service import TaskClaim, TaskService
from app.workers.execution import TerminalFailure, register_operation


@dataclass(frozen=True)
class TaskAiCancellation(AiCancellation):
    task_service: TaskService
    claim: TaskClaim

    async def register_run(self, ai_run_id: str) -> bool:
        return await self.task_service.register_ai_run(
            self.claim.owner_user_id,
            self.claim.task_id,
            self.claim.token,
            ai_run_id,
        )

    async def is_cancel_requested(self) -> bool:
        return await self.task_service.is_cancel_requested(
            self.claim.owner_user_id,
            self.claim.task_id,
        )

    async def acknowledge_cancel(self, ai_run_id: str) -> None:
        await self.task_service.settle_ai_run(
            self.claim.owner_user_id,
            self.claim.task_id,
            ai_run_id,
        )


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
    intake = IntakeService(sessions, ai_client)
    jobs = JobService(sessions, ai_client)
    matching = MatchingService(sessions, ai_client)
    exports = ExportService(sessions, storage)
    privacy = PrivacyWorker(sessions, storage)

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
            claim_token=claim.token,
            task_service=task_service,
            cancellation=TaskAiCancellation(task_service, claim),
        )

    async def fail_parse_job(
        claim: TaskClaim,
        failure: TerminalFailure,
    ) -> None:
        task = await _task(task_service, claim)
        await jobs.fail_parse(
            claim.owner_user_id,
            _resource(task, "job_description"),
            task.id,
            claim.token,
            failure.error_code,
            task_service=task_service,
        )

    async def match_resume_to_job(claim: TaskClaim) -> str:
        task = await _task(task_service, claim)
        return await matching.process_match(
            claim.owner_user_id,
            _resource(task, "match_analysis"),
            trace_id=task.trace_id,
            task_id=task.id,
            claim_token=claim.token,
            task_service=task_service,
            cancellation=TaskAiCancellation(task_service, claim),
        )

    async def fail_match_resume_to_job(
        claim: TaskClaim,
        failure: TerminalFailure,
    ) -> None:
        task = await _task(task_service, claim)
        await matching.fail_match(
            claim.owner_user_id,
            _resource(task, "match_analysis"),
            task.id,
            claim.token,
            failure.error_code,
            task_service=task_service,
        )

    async def render_resume_export(claim: TaskClaim) -> str:
        task = await _task(task_service, claim)
        return await exports.process_export(
            claim.owner_user_id, _resource(task, "export")
        )

    async def generate_intake_draft(claim: TaskClaim) -> str:
        task = await _task(task_service, claim)
        return await intake.process_draft(
            claim.owner_user_id,
            _resource(task, "intake_session"),
            task_id=task.id,
            claim_token=claim.token,
            task_service=task_service,
            cancellation=TaskAiCancellation(task_service, claim),
        )

    async def fail_intake_draft(
        claim: TaskClaim,
        failure: TerminalFailure,
    ) -> None:
        task = await _task(task_service, claim)
        await intake.fail_draft(
            claim.owner_user_id,
            _resource(task, "intake_session"),
            task.id,
            claim.token,
            failure.error_code,
            task_service=task_service,
        )

    async def analyze_intake_answer(claim: TaskClaim) -> str:
        task = await _task(task_service, claim)
        answer_id = _resource(task, "intake_answer")
        return await intake.process_answer_analysis(
            claim.owner_user_id,
            answer_id,
            task_id=task.id,
            claim_token=claim.token,
            task_service=task_service,
            cancellation=TaskAiCancellation(task_service, claim),
        )

    async def fail_intake_answer_analysis(
        claim: TaskClaim,
        failure: TerminalFailure,
    ) -> None:
        task = await _task(task_service, claim)
        await intake.fail_answer_analysis(
            claim.owner_user_id,
            _resource(task, "intake_answer"),
            task.id,
            claim.token,
            failure.error_code,
            permanent=not failure.retryable,
            task_service=task_service,
        )

    async def export_private_data(claim: TaskClaim) -> str:
        return await privacy.export_data(claim.owner_user_id, claim.task_id)

    async def delete_private_data(claim: TaskClaim) -> str:
        return await privacy.delete_account(claim.owner_user_id, claim.task_id)

    register_operation("parse_resume_import", parse_resume_import)
    register_operation(
        "parse_job",
        parse_job,
        terminal_failure_handler=fail_parse_job,
    )
    register_operation(
        "match_resume_to_job",
        match_resume_to_job,
        terminal_failure_handler=fail_match_resume_to_job,
    )
    register_operation("render_resume_export", render_resume_export)
    register_operation(
        "analyze_intake_answer",
        analyze_intake_answer,
        terminal_failure_handler=fail_intake_answer_analysis,
    )
    register_operation(
        "generate_intake_draft",
        generate_intake_draft,
        terminal_failure_handler=fail_intake_draft,
    )
    register_operation("data_export", export_private_data)
    register_operation("account_deletion", delete_private_data)


async def _task(service: TaskService, claim: TaskClaim):
    task = await service.get_task(claim.owner_user_id, claim.task_id)
    if task is None:
        raise RuntimeError("Claimed pipeline task is missing")
    return task


def _resource(task, expected_type: str) -> str:
    if task.resource_type != expected_type or not task.resource_id:
        raise RuntimeError("Pipeline task resource is invalid")
    return task.resource_id
