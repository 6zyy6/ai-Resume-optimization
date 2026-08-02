from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, func, inspect, select, text

from app.db.models import (
    AiRun,
    BulletFactLink,
    Fact,
    FactSource,
    JdRequirement,
    JobDescription,
    MatchAnalysis,
    MatchItem,
    Outbox,
    Resume,
    ResumeVersion,
    SourceRecord,
    Suggestion,
    SuggestionFactLink,
    Task,
    UsageLedger,
    User,
)
from app.integrations.ai_client import (
    AiExecutionReceipt,
    GenerateSuggestionsBatchRequest,
    MatchResumeToJdRequest,
    derive_ai_run_id,
)
from app.modules.matching.service import MatchingService
from app.modules.resumes.service import canonical_snapshot
from app.modules.suggestions.service import SuggestionService
from app.modules.tasks.service import TaskService


pytestmark = pytest.mark.anyio


def _alembic_config(database_path: Path) -> Config:
    api_root = Path(__file__).resolve().parents[1]
    config = Config(api_root / "alembic.ini")
    config.set_main_option("script_location", str(api_root / "migrations"))
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{database_path}")
    return config


def test_migration_0016_backfills_existing_business_rows_as_rule_fallback(
    tmp_path,
    monkeypatch,
):
    database_path = tmp_path / "match-provenance.db"
    config = _alembic_config(database_path)
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{database_path}")
    command.upgrade(config, "0015")
    engine = create_engine(f"sqlite:///{database_path}")
    now = "2026-08-02 08:00:00"
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (id, status, locale, created_at) "
                "VALUES ('usr_legacy_match', 'active', 'zh-CN', :now)"
            ),
            {"now": now},
        )
        connection.execute(
            text(
                "INSERT INTO resumes (id, kind, title, head_version, created_at, owner_user_id) "
                "VALUES ('resume_legacy_match', 'base', 'Legacy', 1, :now, "
                "'usr_legacy_match')"
            ),
            {"now": now},
        )
        connection.execute(
            text(
                "INSERT INTO resume_versions (id, resume_id, snapshot_json, snapshot_hash, "
                "generation_mode, created_by, created_at, owner_user_id) VALUES "
                "('version_legacy_match', 'resume_legacy_match', :snapshot, :hash, "
                "'manual', 'usr_legacy_match', :now, 'usr_legacy_match')"
            ),
            {"snapshot": '{"sections":[]}', "hash": "a" * 64, "now": now},
        )
        connection.execute(
            text(
                "INSERT INTO job_descriptions "
                "(id, title, raw_encrypted, status, created_at, owner_user_id) VALUES "
                "('job_legacy_match', 'Legacy', 'Python', 'parsed', :now, "
                "'usr_legacy_match')"
            ),
            {"now": now},
        )
        connection.execute(
            text(
                "INSERT INTO jd_requirements (id, job_id, type, priority, text_encrypted, "
                "confirmed, source_start, source_end, source_hash, explicitness, "
                "confidence_band, generation_mode, workflow_version, input_hash, "
                "owner_user_id) VALUES ('req_legacy_match', 'job_legacy_match', "
                "'must_have', 1, 'Python', 1, 0, 6, :hash, 'explicit', 'high', "
                "'rule_fallback', 'legacy-rule-fallback@1', :hash, 'usr_legacy_match')"
            ),
            {"hash": "b" * 64},
        )
        connection.execute(
            text(
                "INSERT INTO match_analyses (id, resume_version_id, job_id, "
                "job_owner_user_id, status, workflow_version, created_at, owner_user_id) "
                "VALUES ('match_legacy', 'version_legacy_match', 'job_legacy_match', "
                "'usr_legacy_match', 'succeeded', 'legacy@1', :now, 'usr_legacy_match')"
            ),
            {"now": now},
        )
        connection.execute(
            text(
                "INSERT INTO match_items (id, analysis_id, requirement_id, "
                "requirement_owner_user_id, category, evidence_refs, owner_user_id) "
                "VALUES ('item_legacy', 'match_legacy', 'req_legacy_match', "
                "'usr_legacy_match', 'proved', '[]', 'usr_legacy_match')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO suggestions (id, analysis_id, target_path, original_hash, "
                "original_text_encrypted, suggested_encrypted, requirement_id, reason, "
                "risk_flags, status, created_at, owner_user_id) VALUES "
                "('suggestion_legacy', 'match_legacy', '/sections/0/items/0/text', :hash, "
                "'Python', 'Python', 'req_legacy_match', 'legacy', '[]', 'pending', "
                ":now, 'usr_legacy_match')"
            ),
            {"hash": "c" * 64, "now": now},
        )

    command.upgrade(config, "0016")
    with engine.connect() as connection:
        analysis = connection.execute(
            text(
                "SELECT generation_mode, workflow_version, ai_run_id, input_hash, "
                "updated_at FROM match_analyses WHERE id = 'match_legacy'"
            )
        ).one()
        item = connection.execute(
            text(
                "SELECT resume_target_paths, reason_code, generation_mode, ai_run_id "
                "FROM match_items WHERE id = 'item_legacy'"
            )
        ).one()
        suggestion = connection.execute(
            text(
                "SELECT generation_mode, workflow_version, ai_run_id, input_hash, "
                "updated_at FROM suggestions WHERE id = 'suggestion_legacy'"
            )
        ).one()
    assert analysis.generation_mode == "rule_fallback"
    assert analysis.workflow_version == "legacy-rule-fallback@1"
    assert analysis.ai_run_id is None and len(analysis.input_hash) == 64
    assert item.resume_target_paths == "[]"
    assert item.reason_code == "legacy_rule_fallback"
    assert item.generation_mode == "rule_fallback" and item.ai_run_id is None
    assert suggestion.generation_mode == "rule_fallback"
    assert suggestion.workflow_version == "legacy-rule-fallback@1"
    assert suggestion.ai_run_id is None and len(suggestion.input_hash) == 64
    assert analysis.updated_at == suggestion.updated_at == now
    assert inspect(engine).get_pk_constraint("suggestion_fact_links")[
        "constrained_columns"
    ] == ["suggestion_id", "fact_id", "claim_start", "claim_end"]

    command.downgrade(config, "0015")
    assert "generation_mode" not in {
        column["name"] for column in inspect(engine).get_columns("match_analyses")
    }
    engine.dispose()


class TwoStageReceiptClient:
    def __init__(
        self,
        *,
        suggestion_status: str = "succeeded",
        suggestion_error: str | None = None,
        match_status: str = "succeeded",
        match_error: str | None = None,
        match_items: tuple[dict, ...] | None = None,
        suggestions: tuple[dict, ...] | None = None,
    ) -> None:
        self.suggestion_status = suggestion_status
        self.suggestion_error = suggestion_error
        self.match_status = match_status
        self.match_error = match_error
        self.match_items = match_items
        self.suggestions = suggestions
        self.requests: list[object] = []

    async def run(self, request, cancellation=None):
        self.requests.append(request)
        stage = "match" if isinstance(request, MatchResumeToJdRequest) else "suggestions"
        if cancellation is not None:
            ai_run_id = derive_ai_run_id(request.task_id, stage, request.input_hash)
            assert await cancellation.register_run(ai_run_id) is True
        if isinstance(request, MatchResumeToJdRequest):
            items = self.match_items if self.match_items is not None else (
                {
                    "requirement_ref": request.payload.confirmed_requirements[0].id,
                    "category": "transferable",
                    "fact_refs": (request.payload.confirmed_facts[0].id,),
                    "resume_target_paths": ("/sections/0/items/0/text",),
                    "reason_code": "relevant_fact_underexpressed",
                },
            )
            return _receipt(
                request,
                status=self.match_status,
                error_code=self.match_error,
                result={"matches": items} if self.match_status == "succeeded" else None,
            )
        assert isinstance(request, GenerateSuggestionsBatchRequest)
        suggestions = self.suggestions if self.suggestions is not None else (
            {
                "target_path": request.payload.matches[0].target_path,
                "original_hash": request.payload.matches[0].original_hash,
                "suggested_text": "Python",
                "atomic_claims": (
                    {
                        "text": "Python",
                        "fact_refs": request.payload.matches[0].fact_refs,
                        "claim_order": 0,
                    },
                ),
                "requirement_ref": request.payload.matches[0].requirement_ref,
                "reason": "突出已确认的 Python 经验",
                "risk_flags": (),
                "proposed_status": "pending",
            },
        )
        return _receipt(
            request,
            status=self.suggestion_status,
            error_code=self.suggestion_error,
            result=(
                {"suggestions": suggestions}
                if self.suggestion_status == "succeeded"
                else None
            ),
        )


class FailFirstSuggestionTransport(TwoStageReceiptClient):
    def __init__(self) -> None:
        super().__init__()
        self.suggestion_attempts = 0

    async def run(self, request, cancellation=None):
        if isinstance(request, GenerateSuggestionsBatchRequest):
            self.suggestion_attempts += 1
            if self.suggestion_attempts == 1:
                self.requests.append(request)
                assert cancellation is not None
                ai_run_id = derive_ai_run_id(
                    request.task_id, "suggestions", request.input_hash
                )
                assert await cancellation.register_run(ai_run_id) is True
                raise TimeoutError("transport failed after stage registration")
        return await super().run(request, cancellation)


class DriftDuringSuggestionClient(TwoStageReceiptClient):
    def __init__(self, sessions, suffix: str, drift: str) -> None:
        super().__init__()
        self.sessions = sessions
        self.suffix = suffix
        self.drift = drift
        self.mutated = False

    async def run(self, request, cancellation=None):
        if isinstance(request, GenerateSuggestionsBatchRequest) and not self.mutated:
            async with self.sessions.begin() as session:
                if self.drift == "fact_status":
                    fact = await session.scalar(
                        select(Fact).where(Fact.id == f"fact_{self.suffix}")
                    )
                    assert fact is not None
                    fact.status = "rejected"
                elif self.drift == "fact_source":
                    source = SourceRecord(
                        id=f"src_{self.suffix}_added",
                        owner_user_id=f"usr_{self.suffix}",
                        source_type="user_confirmation",
                        content_encrypted="Additional evidence",
                    )
                    session.add(source)
                    await session.flush()
                    session.add(
                        FactSource(
                            fact_id=f"fact_{self.suffix}",
                            source_record_id=source.id,
                            owner_user_id=f"usr_{self.suffix}",
                            source_hash=hashlib.sha256(
                                source.content_encrypted.encode()
                            ).hexdigest(),
                        )
                    )
                else:
                    requirement = await session.scalar(
                        select(JdRequirement).where(
                            JdRequirement.id == f"req_{self.suffix}"
                        )
                    )
                    assert requirement is not None
                    if self.drift == "requirement_confirmation":
                        requirement.confirmed = False
                    else:
                        requirement.text_encrypted = "Go"
            self.mutated = True
        return await super().run(request, cancellation)


def _receipt(request, *, status="succeeded", error_code=None, result=None):
    stage = "match" if isinstance(request, MatchResumeToJdRequest) else "suggestions"
    ai_run_id = derive_ai_run_id(request.task_id, stage, request.input_hash)
    return AiExecutionReceipt.model_validate(
        {
            "run": {
                "ai_run_id": ai_run_id,
                "trace_id": request.trace_id,
                "task_id": request.task_id,
                "workflow_type": request.workflow_type,
                "workflow_version": "2",
                "prompt_template_version": request.prompt_template_version,
                "status": status,
                "error_code": error_code,
                "provider": "deepseek",
                "requested_model": "deepseek-chat",
                "response_model": "deepseek-chat",
                "started_at": "2026-08-02T08:00:00Z",
                "first_token_at": "2026-08-02T08:00:01Z",
                "finished_at": "2026-08-02T08:00:02Z",
                "usage": {
                    "input": 12,
                    "output": 8,
                    "cache_read": 0,
                    "cache_write": 0,
                    "reasoning": 0,
                    "total_tokens": 20,
                    "cost_usd": Decimal("0.01"),
                },
                "events": (
                    {
                        "ai_run_id": ai_run_id,
                        "trace_id": request.trace_id,
                        "task_id": request.task_id,
                        "event_seq": 1,
                        "event_type": f"run_{status}",
                        "occurred_at": "2026-08-02T08:00:02Z",
                        "details": {"error_code": error_code} if error_code else None,
                    },
                ),
                "turn_count": 1,
                "tool_call_count": 0,
                "retry_count": 0,
                "fallback_count": 0,
                "schema_valid": status == "succeeded",
                "facts_valid": status == "succeeded",
                "input_hash": request.input_hash,
                "exportable": False,
                "risk_flags": (),
            },
            "result": result,
        }
    )


async def test_succeeded_match_then_failed_suggestions_audits_two_runs_without_publication(
    sql_session_factory,
):
    ai = TwoStageReceiptClient(
        suggestion_status="failed",
        suggestion_error="provider_unavailable",
    )
    owner, service, tasks, analysis, claim = await _queued_match(
        sql_session_factory, ai, "stage2_failure"
    )

    await service.process_match(
        owner,
        analysis.id,
        trace_id="ignored-caller-trace",
        task_id=analysis.task_id,
        claim_token=claim.token,
        task_service=tasks,
    )

    async with sql_session_factory() as session:
        runs = list(
            (
                await session.scalars(
                    select(AiRun)
                    .where(AiRun.task_id == analysis.task_id)
                    .order_by(AiRun.workflow_stage)
                )
            ).all()
        )
        counts = [
            int(await session.scalar(select(func.count()).select_from(model)) or 0)
            for model in (MatchItem, Suggestion, SuggestionFactLink)
        ]
        task = await session.scalar(select(Task).where(Task.id == analysis.task_id))
        current = await session.scalar(
            select(MatchAnalysis).where(MatchAnalysis.id == analysis.id)
        )

    assert [run.workflow_stage for run in runs] == ["match", "suggestions"]
    assert counts == [0, 0, 0]
    assert task is not None and task.status == "failed"
    assert current is not None and current.status == "failed"


async def test_success_publishes_exact_requirement_coverage_with_shared_trace_and_distinct_runs(
    sql_session_factory,
):
    ai = TwoStageReceiptClient()
    owner, service, tasks, analysis, claim = await _queued_match(
        sql_session_factory, ai, "success"
    )

    await service.process_match(
        owner,
        analysis.id,
        trace_id="ignored-caller-trace",
        task_id=analysis.task_id,
        claim_token=claim.token,
        task_service=tasks,
    )

    async with sql_session_factory() as session:
        runs = list(
            (
                await session.scalars(
                    select(AiRun)
                    .where(AiRun.task_id == analysis.task_id)
                    .order_by(AiRun.workflow_stage)
                )
            ).all()
        )
        items = list(
            (
                await session.scalars(
                    select(MatchItem).where(MatchItem.analysis_id == analysis.id)
                )
            ).all()
        )
        suggestions = list(
            (
                await session.scalars(
                    select(Suggestion).where(Suggestion.analysis_id == analysis.id)
                )
            ).all()
        )
        task = await session.scalar(select(Task).where(Task.id == analysis.task_id))
        outbox = await session.scalar(
            select(Outbox).where(Outbox.task_id == analysis.task_id)
        )

    assert len(runs) == 2
    assert {run.trace_id for run in runs} == {f"trace_success"}
    assert len({run.id for run in runs}) == 2
    assert all(
        run.id == derive_ai_run_id(run.task_id, run.workflow_stage, run.input_hash)
        for run in runs
    )
    assert [(item.requirement_id, item.category) for item in items] == [
        ("req_success", "underexpressed")
    ]
    assert len(suggestions) == 1
    assert task is not None and task.status == "succeeded"
    assert await _count(sql_session_factory, UsageLedger) == 1
    assert outbox is not None
    assert set(outbox.payload) == {
        "analysis_id",
        "generation_mode",
        "match_input_hash",
        "task_id",
    }


async def test_unconfirmed_requirements_are_excluded_from_both_model_stages(
    sql_session_factory,
):
    ai = TwoStageReceiptClient()
    owner, service, tasks, analysis, claim = await _queued_match(
        sql_session_factory,
        ai,
        "mixed_confirmation",
        add_unconfirmed_requirement=True,
    )

    await service.process_match(
        owner,
        analysis.id,
        trace_id="ignored",
        task_id=analysis.task_id,
        claim_token=claim.token,
        task_service=tasks,
    )

    assert len(ai.requests) == 2
    assert [
        item.id for item in ai.requests[0].payload.confirmed_requirements
    ] == ["req_mixed_confirmation"]
    assert [
        item.id for item in ai.requests[1].payload.confirmed_requirements
    ] == ["req_mixed_confirmation"]
    async with sql_session_factory() as session:
        items = list(
            (
                await session.scalars(
                    select(MatchItem).where(MatchItem.analysis_id == analysis.id)
                )
            ).all()
        )
    assert [item.requirement_id for item in items] == ["req_mixed_confirmation"]


async def test_failed_match_receipt_stops_before_suggestions_and_publishes_nothing(
    sql_session_factory,
):
    ai = TwoStageReceiptClient(
        match_status="failed",
        match_error="provider_unavailable",
    )
    owner, service, tasks, analysis, claim = await _queued_match(
        sql_session_factory, ai, "stage1_failure"
    )

    await service.process_match(
        owner,
        analysis.id,
        trace_id="ignored",
        task_id=analysis.task_id,
        claim_token=claim.token,
        task_service=tasks,
    )

    assert len(ai.requests) == 1
    assert await _count(sql_session_factory, AiRun) == 1
    assert await _count(sql_session_factory, MatchItem) == 0
    assert await _count(sql_session_factory, Suggestion) == 0
    task = await tasks.get_task(owner, analysis.task_id)
    assert task is not None and task.status == "failed"


@pytest.mark.parametrize("mode", ["unknown", "duplicate", "missing"])
async def test_invalid_requirement_coverage_fails_closed(
    sql_session_factory,
    mode,
):
    suffix = f"invalid_{mode}"
    requirement_id = f"req_{suffix}"
    valid = {
        "requirement_ref": requirement_id,
        "category": "transferable",
        "fact_refs": (f"fact_{suffix}",),
        "resume_target_paths": ("/sections/0/items/0/text",),
        "reason_code": "underexpressed",
    }
    items = {
        "unknown": ({**valid, "requirement_ref": "req_unknown"},),
        "duplicate": (valid, valid),
        "missing": (),
    }[mode]
    ai = TwoStageReceiptClient(match_items=items)
    owner, service, tasks, analysis, claim = await _queued_match(
        sql_session_factory, ai, suffix
    )

    await service.process_match(
        owner,
        analysis.id,
        trace_id="ignored",
        task_id=analysis.task_id,
        claim_token=claim.token,
        task_service=tasks,
    )

    assert len(ai.requests) == 1
    assert await _count(sql_session_factory, MatchItem) == 0
    assert await _count(sql_session_factory, Suggestion) == 0
    task = await tasks.get_task(owner, analysis.task_id)
    assert task is not None and task.status == "failed"


async def test_unsupported_suggestion_claim_fails_task_without_public_rows(
    sql_session_factory,
):
    suffix = "unsupported_claim"
    ai = TwoStageReceiptClient(
        suggestions=(
            {
                "target_path": "/sections/0/items/0/text",
                "original_hash": hashlib.sha256(b"Python").hexdigest(),
                "suggested_text": "Kubernetes",
                "atomic_claims": (
                    {
                        "text": "Kubernetes",
                        "fact_refs": (f"fact_{suffix}",),
                        "claim_order": 0,
                    },
                ),
                "requirement_ref": f"req_{suffix}",
                "reason": "突出容器经验",
                "risk_flags": (),
                "proposed_status": "pending",
            },
        )
    )
    owner, service, tasks, analysis, claim = await _queued_match(
        sql_session_factory, ai, suffix
    )

    await service.process_match(
        owner,
        analysis.id,
        trace_id="ignored",
        task_id=analysis.task_id,
        claim_token=claim.token,
        task_service=tasks,
    )

    assert await _count(sql_session_factory, AiRun) == 2
    assert await _count(sql_session_factory, MatchItem) == 0
    assert await _count(sql_session_factory, SuggestionFactLink) == 0
    task = await tasks.get_task(owner, analysis.task_id)
    assert task is not None and task.status == "failed"


@pytest.mark.parametrize(
    "drift",
    ["fact_status", "fact_source", "requirement_confirmation", "requirement_value"],
)
async def test_final_publication_revalidates_current_policy_state(
    sql_session_factory,
    drift,
):
    suffix = f"final_drift_{drift}"
    ai = DriftDuringSuggestionClient(sql_session_factory, suffix, drift)
    owner, service, tasks, analysis, claim = await _queued_match(
        sql_session_factory, ai, suffix
    )

    await service.process_match(
        owner,
        analysis.id,
        trace_id="ignored",
        task_id=analysis.task_id,
        claim_token=claim.token,
        task_service=tasks,
    )

    async with sql_session_factory() as session:
        task = await session.scalar(select(Task).where(Task.id == analysis.task_id))
        current = await session.scalar(
            select(MatchAnalysis).where(MatchAnalysis.id == analysis.id)
        )
    assert await _count(sql_session_factory, AiRun) == 2
    assert await _count(sql_session_factory, UsageLedger) == 1
    assert await _count(sql_session_factory, MatchItem) == 0
    assert await _count(sql_session_factory, Suggestion) == 0
    assert await _count(sql_session_factory, SuggestionFactLink) == 0
    assert task is not None and task.status == "failed"
    assert task.error_code == "MATCH_PUBLICATION_STATE_CHANGED"
    assert current is not None and current.status == "failed"


async def test_match_without_editable_candidates_still_settles_empty_suggestion_stage(
    sql_session_factory,
):
    suffix = "empty_batch"
    ai = TwoStageReceiptClient(
        match_items=(
            {
                "requirement_ref": f"req_{suffix}",
                "category": "direct",
                "fact_refs": (f"fact_{suffix}",),
                "resume_target_paths": (),
                "reason_code": "direct_evidence",
            },
        ),
        suggestions=(),
    )
    owner, service, tasks, analysis, claim = await _queued_match(
        sql_session_factory, ai, suffix
    )

    await service.process_match(
        owner,
        analysis.id,
        trace_id="ignored",
        task_id=analysis.task_id,
        claim_token=claim.token,
        task_service=tasks,
    )

    assert len(ai.requests) == 2
    assert isinstance(ai.requests[1], GenerateSuggestionsBatchRequest)
    assert ai.requests[1].payload.matches == ()
    assert await _count(sql_session_factory, AiRun) == 2
    assert await _count(sql_session_factory, MatchItem) == 1
    assert await _count(sql_session_factory, Suggestion) == 0


async def test_repeated_fact_claims_keep_exact_ranges_through_acceptance(
    sql_session_factory,
):
    suffix = "repeated_fact_ranges"
    ai = TwoStageReceiptClient(
        suggestions=(
            {
                "target_path": "/sections/0/items/0/text",
                "original_hash": hashlib.sha256(b"Python").hexdigest(),
                "suggested_text": "Python, Python",
                "atomic_claims": (
                    {
                        "text": "Python",
                        "fact_refs": (f"fact_{suffix}",),
                        "claim_order": 0,
                    },
                    {
                        "text": "Python",
                        "fact_refs": (f"fact_{suffix}",),
                        "claim_order": 1,
                    },
                ),
                "requirement_ref": f"req_{suffix}",
                "reason": "保留两个独立原子声明",
                "risk_flags": (),
                "proposed_status": "pending",
            },
        )
    )
    owner, service, tasks, analysis, claim = await _queued_match(
        sql_session_factory, ai, suffix
    )

    await service.process_match(
        owner,
        analysis.id,
        trace_id="ignored",
        task_id=analysis.task_id,
        claim_token=claim.token,
        task_service=tasks,
    )

    async with sql_session_factory() as session:
        suggestion = await session.scalar(
            select(Suggestion).where(Suggestion.analysis_id == analysis.id)
        )
        assert suggestion is not None
        links = list(
            (
                await session.scalars(
                    select(SuggestionFactLink)
                    .where(SuggestionFactLink.suggestion_id == suggestion.id)
                )
            ).all()
        )
        links.sort(key=lambda link: link.claim_range["start"])
    assert [link.claim_range for link in links] == [
        {"start": 0, "end": 6},
        {"start": 8, "end": 14},
    ]
    published = await service.get(owner, analysis.id)
    assert published is not None
    assert getattr(published, "suggestion_fact_links", {}) == {
        suggestion.id: [
            {
                "fact_id": f"fact_{suffix}",
                "claim_range": {"start": 0, "end": 6},
            },
            {
                "fact_id": f"fact_{suffix}",
                "claim_range": {"start": 8, "end": 14},
            },
        ]
    }

    saved = await SuggestionService(sql_session_factory).decide(
        owner,
        suggestion.id,
        "accept",
        edited_text=None,
        idempotency_key="accept_repeated_fact_ranges",
    )
    async with sql_session_factory() as session:
        copied = list(
            (
                await session.scalars(
                    select(BulletFactLink)
                    .where(
                        BulletFactLink.resume_version_id == saved.version.id,
                        BulletFactLink.bullet_id == "bullet_python",
                        BulletFactLink.fact_id == f"fact_{suffix}",
                    )
                    .order_by(BulletFactLink.claim_start)
                )
            ).all()
        )
    assert [link.claim_range for link in copied] == [
        {"start": 0, "end": 6},
        {"start": 8, "end": 14},
    ]


async def test_retry_resumes_the_stable_registered_suggestion_run(
    sql_session_factory,
):
    from app.workers.pipeline import TaskAiCancellation

    ai = FailFirstSuggestionTransport()
    owner, service, tasks, analysis, claim = await _queued_match(
        sql_session_factory, ai, "stable_retry"
    )
    cancellation = TaskAiCancellation(tasks, claim)

    with pytest.raises(TimeoutError, match="stage registration"):
        await service.process_match(
            owner,
            analysis.id,
            trace_id="ignored",
            task_id=analysis.task_id,
            claim_token=claim.token,
            task_service=tasks,
            cancellation=cancellation,
        )
    await service.process_match(
        owner,
        analysis.id,
        trace_id="ignored",
        task_id=analysis.task_id,
        claim_token=claim.token,
        task_service=tasks,
        cancellation=cancellation,
    )

    assert ai.suggestion_attempts == 2
    assert await _count(sql_session_factory, AiRun) == 2
    assert await _count(sql_session_factory, MatchItem) == 1
    task = await tasks.get_task(owner, analysis.task_id)
    assert task is not None and task.status == "succeeded"


async def _count(sessions, model) -> int:
    async with sessions() as session:
        return int(await session.scalar(select(func.count()).select_from(model)) or 0)


async def _queued_match(
    sessions,
    ai_client,
    suffix: str,
    *,
    add_unconfirmed_requirement: bool = False,
):
    owner = f"usr_{suffix}"
    source_hash = hashlib.sha256(b"Python").hexdigest()
    snapshot, snapshot_hash = canonical_snapshot(
        {
            "schema_version": "1",
            "title": "Candidate",
            "target": "Backend",
            "sections": [
                {
                    "id": "experience",
                    "type": "experience",
                    "title": "Experience",
                    "items": [
                        {
                            "id": "bullet_python",
                            "text": "Python",
                            "fact_refs": [f"fact_{suffix}"],
                        }
                    ],
                }
            ],
        }
    )
    async with sessions.begin() as session:
        user = User(id=owner)
        session.add(user)
        await session.flush()
        source = SourceRecord(
            id=f"src_{suffix}",
            owner_user_id=owner,
            source_type="user_confirmation",
            content_encrypted="Python",
        )
        fact = Fact(
            id=f"fact_{suffix}",
            owner_user_id=owner,
            kind="skill",
            value_encrypted="Python",
            status="unconfirmed",
            confirmed_at=None,
        )
        resume = Resume(
            id=f"resume_{suffix}",
            owner_user_id=owner,
            kind="base",
            title="Candidate",
            head_version=0,
        )
        job = JobDescription(
            id=f"job_{suffix}",
            owner_user_id=owner,
            title="Backend",
            raw_encrypted="Python",
            status="parsed",
        )
        session.add_all([source, fact, resume, job])
        await session.flush()
        session.add(
            FactSource(
                fact_id=fact.id,
                source_record_id=source.id,
                owner_user_id=owner,
                source_hash=source_hash,
            )
        )
        await session.flush()
        fact.status = "confirmed"
        fact.confirmed_at = datetime(2026, 8, 2, 8, tzinfo=timezone.utc)
        version = ResumeVersion(
            id=f"version_{suffix}",
            owner_user_id=owner,
            resume_id=resume.id,
            snapshot_json=snapshot,
            snapshot_hash=snapshot_hash,
            created_by=owner,
        )
        requirement = JdRequirement(
            id=f"req_{suffix}",
            owner_user_id=owner,
            job_id=job.id,
            type="must_have",
            priority=1,
            text_encrypted="Python",
            confirmed=True,
            source_start=0,
            source_end=6,
            source_hash=source_hash,
            explicitness="explicit",
            confidence_band="high",
            generation_mode="rule_fallback",
            workflow_version="2",
            ai_run_id=None,
            input_hash="a" * 64,
        )
        session.add_all([version, requirement])
        if add_unconfirmed_requirement:
            session.add(
                JdRequirement(
                    id=f"req_{suffix}_unconfirmed",
                    owner_user_id=owner,
                    job_id=job.id,
                    type="preferred",
                    priority=2,
                    text_encrypted="Kubernetes",
                    confirmed=False,
                    source_start=0,
                    source_end=6,
                    source_hash=source_hash,
                    explicitness="explicit",
                    confidence_band="high",
                    generation_mode="rule_fallback",
                    workflow_version="2",
                    ai_run_id=None,
                    input_hash="b" * 64,
                )
            )
        await session.flush()
        resume.head_version = 1
        resume.head_version_id = version.id
        session.add(
            BulletFactLink(
                resume_version_id=version.id,
                bullet_id="bullet_python",
                fact_id=fact.id,
                owner_user_id=owner,
                fact_owner_user_id=owner,
                claim_range={"start": 0, "end": 6},
                fact_value_encrypted_at_link="Python",
                fact_status_at_link="confirmed",
                fact_source_hashes_at_link=[source_hash],
            )
        )

    tasks = TaskService(sessions)
    service = MatchingService(sessions, ai_client)
    result = await service.create(
        owner,
        resume_version_id=f"version_{suffix}",
        job_id=f"job_{suffix}",
        idempotency_key=f"match_{suffix}",
        trace_id=f"trace_{suffix}",
        task_service=tasks,
    )
    claim = await tasks.claim_task(owner, result.analysis.task_id)
    assert claim is not None
    return owner, service, tasks, result.analysis, claim
