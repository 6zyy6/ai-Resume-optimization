import asyncio
import hashlib
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from urllib.parse import quote

import httpx
import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    ArrayObject,
    DictionaryObject,
    FloatObject,
    NameObject,
    TextStringObject,
)
from reportlab.pdfgen import canvas
from sqlalchemy import create_engine, func, inspect, select
from sqlalchemy.dialects import postgresql

from app.db.models import (
    BulletFactLink,
    Export,
    File,
    JdRequirement,
    JobDescription,
    MatchAnalysis,
    Outbox,
    Resume,
    ResumeVersion,
    Suggestion,
    SuggestionFactLink,
    Task,
    UserAlias,
    VersionOperation,
)
from app.integrations.ai_client import (
    InternalAiClient,
    LegacyAiClientAdapter,
)
from app.integrations.storage import CosStorage
from app.modules.imports.parsers import FileParseError, parse_resume_file
from app.modules.matching.service import MatchingService
from app.modules.tasks.service import TaskServiceError
from app.workers.execution import HttpServiceError, should_retry


@pytest.mark.parametrize(
    "claim",
    ["获得诺贝尔奖", "创建 Linux 内核", "负责管理"],
)
def test_export_rejects_claim_not_supported_by_immutable_version_evidence(
    pipeline_client,
    claim,
):
    client, sessions, _ = pipeline_client
    fact = client.post(
        "/v1/facts",
        json={
            "kind": "skill",
            "value": "Python",
            "status": "confirmed",
            "sources": [
                {"source_type": "user_confirmation", "content": "Python"}
            ],
        },
        headers={"Idempotency-Key": "review-export-python-fact"},
    )
    assert fact.status_code == 201
    version_id = asyncio.run(
        _seed_forged_version(
            sessions,
            fact.json()["id"],
            claim,
        )
    )

    created = client.post(
        "/v1/exports",
        json={
            "resume_version_id": version_id,
            "template_version": "clear-standard",
        },
        headers={"Idempotency-Key": "review-export-forged-claim"},
    )

    assert created.status_code == 202
    with pytest.raises(
        Exception,
        match="EXPORT_BLOCKED_BY_FACTS",
    ):
        asyncio.run(
            client.app.state.export_service.process_export(
                "usr_a", created.json()["id"]
            )
        )


def test_export_allows_exact_supported_claim_made_only_of_stopwords(
    pipeline_client,
):
    client, sessions, _ = pipeline_client
    fact = client.post(
        "/v1/facts",
        json={
            "kind": "responsibility",
            "value": "负责管理",
            "status": "confirmed",
            "sources": [
                {
                    "source_type": "user_confirmation",
                    "content": "负责管理",
                }
            ],
        },
        headers={"Idempotency-Key": "review-stopword-fact"},
    )
    version_id = asyncio.run(
        _seed_forged_version(
            sessions,
            fact.json()["id"],
            "负责管理",
            fact_value="负责管理",
        )
    )
    created = client.post(
        "/v1/exports",
        json={
            "resume_version_id": version_id,
            "template_version": "clear-standard",
        },
        headers={"Idempotency-Key": "review-stopword-export"},
    )
    result_id = asyncio.run(
        client.app.state.export_service.process_export(
            "usr_a",
            created.json()["id"],
        )
    )

    assert created.status_code == 202
    assert result_id == created.json()["id"]


def test_matching_base_resume_creates_targeted_resume_and_decision_keeps_base_head(
    pipeline_client,
):
    client, sessions, _ = pipeline_client
    base_resume_id, base_version_id, base_head, _ = _create_base_version(client)
    job_id, _ = _create_parsed_job(
        client, sessions, "Python SQL", "review-target-job", confirm=True
    )

    match = client.post(
        "/v1/match-analyses",
        json={"resume_version_id": base_version_id, "job_id": job_id},
        headers={"Idempotency-Key": "review-target-match"},
    )
    assert match.status_code == 202, match.text
    targeted_version_id = match.json()["resume_version_id"]
    targeted = asyncio.run(_version_and_resume(sessions, targeted_version_id))

    assert targeted[0].resume_id != base_resume_id
    assert targeted[1].kind == "job_targeted"
    assert targeted[1].base_resume_id == base_resume_id
    assert targeted[1].job_description_id == job_id

    asyncio.run(
        client.app.state.matching_service.process_match(
            "usr_a",
            match.json()["id"],
            trace_id="trace_review_target",
            task_id=match.json()["task_id"],
        )
    )
    suggestions = client.get(
        f"/v1/match-analyses/{match.json()['id']}/suggestions"
    )
    assert suggestions.status_code == 200
    accepted = client.post(
        f"/v1/suggestions/{suggestions.json()['items'][0]['id']}/accept",
        headers={"Idempotency-Key": "review-target-accept"},
    )
    assert accepted.status_code == 201, accepted.text

    base_after, targeted_after = asyncio.run(
        _resume_heads(sessions, base_resume_id, targeted[1].id)
    )
    assert base_after == base_head
    assert targeted_after == accepted.json()["version_id"]


def test_targeted_resume_creation_locks_the_source_resume_row():
    statements = []

    class RecordingSession:
        async def scalar(self, statement):
            statements.append(statement)
            return None

    source = ResumeVersion(
        id="review_lock_version",
        owner_user_id="usr_a",
        resume_id="review_lock_resume",
        snapshot_json={},
        snapshot_hash="0" * 64,
        created_by="usr_a",
    )
    job = JobDescription(
        id="review_lock_job",
        owner_user_id="usr_a",
        title="Target",
        raw_encrypted="Python",
        status="parsed",
    )

    with pytest.raises(RuntimeError, match="Match source resume is missing"):
        asyncio.run(
            MatchingService._target_version(
                RecordingSession(),
                "usr_a",
                source,
                job,
            )
        )

    sql = str(
        statements[0].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert sql.rstrip().endswith("FOR UPDATE")


def test_targeted_key_supports_alias_owned_base_and_job(pipeline_client):
    client, sessions, _ = pipeline_client
    version_id, job_id = asyncio.run(
        _seed_alias_owned_match_input(sessions)
    )
    client.app.state.matching_service = MatchingService(sessions)

    response = client.post(
        "/v1/match-analyses",
        json={"resume_version_id": version_id, "job_id": job_id},
        headers={"Idempotency-Key": "review-alias-targeted"},
    )
    targeted = asyncio.run(
        _version_and_resume(sessions, response.json()["resume_version_id"])
    )

    assert response.status_code == 202
    assert targeted[1].owner_user_id == "usr_a"
    assert targeted[1].base_resume_owner_user_id == "usr_b"
    assert targeted[1].job_description_owner_user_id == "usr_b"
    asyncio.run(
        client.app.state.matching_service.process_match(
            "usr_a",
            response.json()["id"],
            trace_id="trace-review-alias-targeted",
            task_id=response.json()["task_id"],
        )
    )
    suggestions = client.get(
        f"/v1/match-analyses/{response.json()['id']}/suggestions"
    )
    assert suggestions.status_code == 200
    assert suggestions.json()["items"] == []


def test_pi_final_match_creates_suggestion_links_used_by_public_response(
    pipeline_client,
):
    client, sessions, _ = pipeline_client
    _, base_version_id, _, fact_id = _create_base_version(client)
    job_id, requirement_id = _create_parsed_job(
        client, sessions, "Python SQL", "review-pi-job", confirm=True
    )
    from test_match_ai_orchestration import TwoStageReceiptClient

    client.app.state.matching_service = MatchingService(
        sessions, TwoStageReceiptClient()
    )
    match = client.post(
        "/v1/match-analyses",
        json={"resume_version_id": base_version_id, "job_id": job_id},
        headers={"Idempotency-Key": "review-pi-match"},
    )
    assert match.status_code == 202, match.text
    queued_suggestions = client.get(
        f"/v1/match-analyses/{match.json()['id']}/suggestions"
    )
    assert queued_suggestions.status_code == 409
    assert (
        queued_suggestions.json()["error"]["code"]
        == "MATCH_ANALYSIS_NOT_READY"
    )
    queued_id = asyncio.run(
        _first_suggestion_id(sessions, match.json()["id"])
    )
    assert queued_id is None
    claim = asyncio.run(
        client.app.state.task_service.claim_task(
            "usr_a", match.json()["task_id"]
        )
    )
    assert claim is not None
    asyncio.run(
        client.app.state.matching_service.process_match(
            "usr_a",
            match.json()["id"],
            trace_id="trace_review_pi",
            task_id=match.json()["task_id"],
            claim_token=claim.token,
            task_service=client.app.state.task_service,
        )
    )

    response = client.get(
        f"/v1/match-analyses/{match.json()['id']}/suggestions"
    )
    persisted = asyncio.run(_suggestion_audit(sessions, match.json()["id"]))

    assert response.status_code == 200
    assert len(response.json()["items"]) == 1
    assert response.json()["items"][0]["fact_refs"] == [fact_id]
    assert response.json()["items"][0]["requirement_text"] == "Python SQL"
    assert persisted == [
        (
            response.json()["items"][0]["id"],
            fact_id,
        )
    ]


def test_pdf_with_text_layer_and_annotation_javascript_is_rejected():
    source = BytesIO()
    document = canvas.Canvas(source)
    document.drawString(72, 720, "Readable resume text")
    document.save()

    reader = PdfReader(BytesIO(source.getvalue()))
    writer = PdfWriter()
    writer.append_pages_from_reader(reader)
    annotation = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Annot"),
            NameObject("/Subtype"): NameObject("/Link"),
            NameObject("/Rect"): ArrayObject(
                [
                    FloatObject(0),
                    FloatObject(0),
                    FloatObject(100),
                    FloatObject(100),
                ]
            ),
            NameObject("/A"): DictionaryObject(
                {
                    NameObject("/S"): NameObject("/JavaScript"),
                    NameObject("/JS"): TextStringObject("app.alert('x')"),
                }
            ),
        }
    )
    writer.pages[0][NameObject("/Annots")] = ArrayObject(
        [writer._add_object(annotation)]
    )
    malicious = BytesIO()
    writer.write(malicious)

    with pytest.raises(FileParseError, match="FILE_TYPE_UNSUPPORTED"):
        parse_resume_file(
            "resume.pdf",
            "application/pdf",
            malicious.getvalue(),
        )


@pytest.mark.parametrize(
    ("failure", "expected_status", "retryable"),
    [
        (httpx.Response(429), 429, True),
        (httpx.Response(503), 503, True),
        (httpx.Response(400), 400, False),
    ],
)
def test_internal_ai_http_statuses_map_to_worker_retry_policy(
    failure,
    expected_status,
    retryable,
):
    def handler(_request: httpx.Request) -> httpx.Response:
        return failure

    client = InternalAiClient(
        "http://pi.internal",
        "service-token",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(HttpServiceError) as raised:
        asyncio.run(_run_ai(client))
    assert raised.value.status_code == expected_status
    assert should_retry(raised.value) is retryable


def test_internal_ai_transport_timeout_is_retryable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("provider timeout", request=request)

    client = InternalAiClient(
        "http://pi.internal",
        "service-token",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(TimeoutError) as raised:
        asyncio.run(_run_ai(client))
    assert should_retry(raised.value) is True


@pytest.mark.parametrize(
    ("error_code", "expected_exception", "expected_retryable"),
    [
        ("provider_429", HttpServiceError, True),
        ("provider_unavailable", HttpServiceError, True),
        ("provider_timeout", TimeoutError, True),
        ("invalid_json", RuntimeError, False),
    ],
)
def test_internal_ai_terminal_failure_maps_to_worker_retry_policy(
    error_code,
    expected_exception,
    expected_retryable,
):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                202,
                json={"ai_run_id": "run_terminal", "status": "queued"},
            )
        return httpx.Response(
            200,
            json={
                "run": {
                    "ai_run_id": "run_terminal",
                    "status": "failed",
                    "error_code": error_code,
                }
            },
        )

    client = InternalAiClient(
        "http://pi.internal",
        "service-token",
        poll_interval_seconds=0,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(expected_exception) as raised:
        asyncio.run(_run_ai(client))
    assert should_retry(raised.value) is expected_retryable


def test_export_file_retention_is_seven_days(pipeline_client):
    client, sessions, _ = pipeline_client
    _, version_id, _, _ = _create_base_version(client)
    before = datetime.now(timezone.utc)
    created = client.post(
        "/v1/exports",
        json={
            "resume_version_id": version_id,
            "template_version": "clear-standard",
        },
        headers={"Idempotency-Key": "review-export-retention"},
    )
    assert created.status_code == 202, created.text
    expires_at = asyncio.run(_export_expiry(sessions, created.json()["id"]))
    assert before + timedelta(days=7) <= expires_at
    assert expires_at <= datetime.now(timezone.utc) + timedelta(days=7, seconds=2)


def test_completed_job_is_readable_with_real_requirements(pipeline_client):
    client, sessions, _ = pipeline_client
    job_id, requirement_id = _create_parsed_job(
        client, sessions, "必须熟练 Python", "review-job-get"
    )

    response = client.get(f"/v1/jobs/{job_id}")

    assert response.status_code == 200
    assert response.json()["status"] == "parsed"
    assert len(response.json()["requirements"]) == 1
    requirement = response.json()["requirements"][0]
    assert {
        "id": requirement_id,
        "type": "must_have",
        "priority": 1,
        "text": "必须熟练 Python",
        "confirmed": False,
    }.items() <= requirement.items()
    assert requirement["source_range"] == {"start": 0, "end": 11}
    assert requirement["source_hash"] == hashlib.sha256(
        "必须熟练 Python".encode()
    ).hexdigest()
    assert requirement["generation_mode"] == "rule_fallback"


def test_cos_download_uses_rfc5987_utf8_filename():
    class FakeClient:
        def get_presigned_url(self, **kwargs):
            return kwargs

    storage = object.__new__(CosStorage)
    storage.bucket = "resume-bucket"
    storage.client = FakeClient()

    request = storage.download_url("exports/object", "张三_简历.pdf", 600)
    disposition = request["Params"]["response-content-disposition"]

    assert (
        f"filename*=UTF-8''{quote('张三_简历.pdf')}"
        in disposition
    )


def test_0006_adds_job_task_correlation_and_targeted_resume_uniqueness(
    tmp_path,
    monkeypatch,
):
    database_path = tmp_path / "task7-review-migration.db"
    monkeypatch.setenv(
        "ALEMBIC_DATABASE_URL",
        f"sqlite+aiosqlite:///{database_path}",
    )
    api_root = Path(__file__).resolve().parents[1]
    config = Config(api_root / "alembic.ini")
    config.set_main_option("script_location", str(api_root / "migrations"))

    command.upgrade(config, "head")
    engine = create_engine(f"sqlite:///{database_path}")
    try:
        schema = inspect(engine)
        assert "task_id" in {
            column["name"]
            for column in schema.get_columns("job_descriptions")
        }
        assert "ix_job_descriptions_task_id" in {
            index["name"]
            for index in schema.get_indexes("job_descriptions")
        }
        assert "targeted_resume_keys" in schema.get_table_names()
        assert {
            "owner_user_id",
            "base_resume_id",
            "base_resume_owner_user_id",
            "job_description_id",
            "job_description_owner_user_id",
        } == set(
            schema.get_pk_constraint("targeted_resume_keys")[
                "constrained_columns"
            ]
        )
    finally:
        engine.dispose()
    command.downgrade(config, "0005")
    engine = create_engine(f"sqlite:///{database_path}")
    try:
        schema = inspect(engine)
        assert "task_id" not in {
            column["name"]
            for column in schema.get_columns("job_descriptions")
        }
        assert "targeted_resume_keys" not in schema.get_table_names()
    finally:
        engine.dispose()


def test_0006_preserves_duplicate_history_and_selects_canonical_resume(
    tmp_path,
    monkeypatch,
):
    database_path = tmp_path / "task7-duplicate-targeted.db"
    monkeypatch.setenv(
        "ALEMBIC_DATABASE_URL",
        f"sqlite+aiosqlite:///{database_path}",
    )
    api_root = Path(__file__).resolve().parents[1]
    config = Config(api_root / "alembic.ini")
    config.set_main_option("script_location", str(api_root / "migrations"))
    command.upgrade(config, "0005")
    engine = create_engine(f"sqlite:///{database_path}")
    try:
        with engine.begin() as connection:
            for resume_id, head_version in (
                ("resume_target_old", 1),
                ("resume_target_new", 2),
            ):
                connection.execute(
                    sa.text(
                        """
                        INSERT INTO resumes (
                            id, owner_user_id, kind, title, head_version,
                            base_resume_id, base_resume_owner_user_id,
                            job_description_id, job_description_owner_user_id,
                            created_at
                        ) VALUES (
                            :id, 'usr_history', 'job_targeted', :id,
                            :head_version, 'resume_base', 'usr_history',
                            'job_history', 'usr_history', CURRENT_TIMESTAMP
                        )
                        """
                    ),
                    {"id": resume_id, "head_version": head_version},
                )
                connection.execute(
                    sa.text(
                        """
                        INSERT INTO resume_versions (
                            id, owner_user_id, resume_id, parent_version_id,
                            snapshot_json, snapshot_hash, created_by, created_at
                        ) VALUES (
                            :version_id, 'usr_history', :resume_id, NULL,
                            '{}', :snapshot_hash, 'usr_history',
                            CURRENT_TIMESTAMP
                        )
                        """
                    ),
                    {
                        "version_id": f"version_{resume_id}",
                        "resume_id": resume_id,
                        "snapshot_hash": "0" * 64,
                    },
                )
        command.upgrade(config, "0006")
        with engine.connect() as connection:
            canonical = connection.execute(
                sa.text(
                    """
                    SELECT resume_id
                    FROM targeted_resume_keys
                    WHERE owner_user_id = 'usr_history'
                      AND base_resume_id = 'resume_base'
                      AND base_resume_owner_user_id = 'usr_history'
                      AND job_description_id = 'job_history'
                      AND job_description_owner_user_id = 'usr_history'
                    """
                )
            ).scalars().all()
            versions = connection.execute(
                sa.text(
                    """
                    SELECT resume_id
                    FROM resume_versions
                    WHERE owner_user_id = 'usr_history'
                    ORDER BY resume_id
                    """
                )
            ).scalars().all()
        assert canonical == ["resume_target_new"]
        assert versions == ["resume_target_new", "resume_target_old"]
    finally:
        engine.dispose()


def test_job_parse_task_admission_failure_rolls_back_business_resource(
    pipeline_client,
):
    client, sessions, _ = pipeline_client
    job = client.post(
        "/v1/jobs",
        json={"title": "后端实习生", "raw": "Python"},
        headers={"Idempotency-Key": "review-atomic-job"},
    )
    assert job.status_code == 201

    client.app.state.task_service = RejectingTaskService()
    response = client.post(
        f"/v1/jobs/{job.json()['id']}/parse",
        headers={"Idempotency-Key": "review-atomic-parse"},
    )
    state = asyncio.run(_job_and_durable_counts(sessions, job.json()["id"]))

    assert response.status_code == 503
    assert state == ("draft", 0, 0)


def test_import_task_admission_failure_leaves_no_orphan_import(pipeline_client):
    client, sessions, _ = pipeline_client
    file_id = _confirmed_upload(client, b"Python")
    client.app.state.task_service = RejectingTaskService()

    response = client.post(
        "/v1/imports",
        json={"file_id": file_id},
        headers={"Idempotency-Key": "review-atomic-import"},
    )

    assert response.status_code == 503
    assert asyncio.run(_owned_count(sessions, "import")) == 0
    assert asyncio.run(_owned_count(sessions, "task")) == 0
    assert asyncio.run(_owned_count(sessions, "outbox")) == 0


def test_match_task_admission_failure_leaves_no_orphan_analysis(
    pipeline_client,
):
    client, sessions, _ = pipeline_client
    _, version_id, _, _ = _create_base_version(client)
    job_id, _ = _create_parsed_job(
        client, sessions, "Python SQL", "review-atomic-match-job", confirm=True
    )
    client.app.state.task_service = RejectingTaskService()

    response = client.post(
        "/v1/match-analyses",
        json={"resume_version_id": version_id, "job_id": job_id},
        headers={"Idempotency-Key": "review-atomic-match"},
    )

    assert response.status_code == 503
    assert asyncio.run(_owned_count(sessions, "match")) == 0


def test_export_task_admission_failure_leaves_no_orphan_export(
    pipeline_client,
):
    client, sessions, _ = pipeline_client
    _, version_id, _, _ = _create_base_version(client)
    client.app.state.task_service = RejectingTaskService()

    response = client.post(
        "/v1/exports",
        json={
            "resume_version_id": version_id,
            "template_version": "clear-standard",
        },
        headers={"Idempotency-Key": "review-atomic-export"},
    )

    assert response.status_code == 503
    assert asyncio.run(_owned_count(sessions, "export")) == 0


class RejectingTaskService:
    async def create_task(self, *_args, **_kwargs):
        raise TaskServiceError("TASK_QUEUE_BUSY", "queue unavailable", 503)

    async def create_task_in_session(self, *_args, **_kwargs):
        raise TaskServiceError("TASK_QUEUE_BUSY", "queue unavailable", 503)


async def _run_ai(client):
    return await LegacyAiClientAdapter(client).run(
        workflow_type="match_resume_to_jd",
        workflow_version="1",
        trace_id="trace_review",
        task_id="task_review",
        facts=[],
        input_data={},
    )


async def _seed_forged_version(
    sessions,
    fact_id: str,
    claim: str,
    *,
    fact_value: str = "Python",
) -> str:
    async with sessions.begin() as session:
        resume = Resume(
            id="review_forged_resume",
            owner_user_id="usr_a",
            kind="base",
            title="Forged",
            head_version=0,
        )
        version = ResumeVersion(
            id="review_forged_version",
            owner_user_id="usr_a",
            resume_id=resume.id,
            snapshot_json={
                "schema_version": "1",
                "title": "Forged",
                "target": None,
                "sections": [
                    {
                        "id": "achievements",
                        "type": "experience",
                        "title": "荣誉",
                        "items": [
                            {
                                "id": "forged_bullet",
                                "text": claim,
                                "fact_refs": [fact_id],
                                "risk_flags": [],
                            }
                        ],
                    }
                ],
            },
            snapshot_hash=hashlib.sha256(claim.encode()).hexdigest(),
            created_by="usr_a",
        )
        session.add_all([resume, version])
        await session.flush()
        session.add_all(
            [
                VersionOperation(
                    id="review_forged_operation",
                    owner_user_id="usr_a",
                    version_id=version.id,
                    operation_type="save",
                    actor="usr_a",
                    metadata_json={},
                ),
                BulletFactLink(
                    resume_version_id=version.id,
                    bullet_id="forged_bullet",
                    fact_id=fact_id,
                    claim_start=0,
                    claim_end=len(claim),
                    owner_user_id="usr_a",
                    fact_owner_user_id="usr_a",
                    claim_range={"start": 0, "end": len(claim)},
                    fact_value_encrypted_at_link=fact_value,
                    fact_status_at_link="confirmed",
                    fact_source_hashes_at_link=[
                        hashlib.sha256(fact_value.encode()).hexdigest()
                    ],
                ),
            ]
        )
        resume.head_version = 1
        resume.head_version_id = version.id
    return version.id


def _create_base_version(client):
    fact = client.post(
        "/v1/facts",
        json={
            "kind": "skill",
            "value": "Python",
            "status": "confirmed",
            "sources": [
                {"source_type": "user_confirmation", "content": "Python"}
            ],
        },
        headers={"Idempotency-Key": "review-fact-python"},
    )
    assert fact.status_code == 201, fact.text
    resume = client.post(
        "/v1/resumes",
        json={"kind": "base", "title": "张三"},
        headers={"Idempotency-Key": "review-base-resume"},
    )
    assert resume.status_code == 201, resume.text
    text = "使用 Python 开发服务"
    version = client.post(
        f"/v1/resumes/{resume.json()['id']}/versions",
        json={
            "base_version": 0,
            "snapshot": {
                "schema_version": "1",
                "title": "张三",
                "target": "后端工程师",
                "sections": [
                    {
                        "id": "experience",
                        "type": "experience",
                        "title": "项目经历",
                        "items": [
                            {
                                "id": "bullet_1",
                                "text": text,
                                "fact_refs": [fact.json()["id"]],
                            }
                        ],
                    }
                ],
            },
            "claim_evidence": [
                {
                    "bullet_id": "bullet_1",
                    "start": 0,
                    "end": len(text),
                    "fact_refs": [fact.json()["id"]],
                }
            ],
        },
        headers={"Idempotency-Key": "review-base-version"},
    )
    assert version.status_code == 201, version.text
    return (
        resume.json()["id"],
        version.json()["id"],
        version.json()["id"],
        fact.json()["id"],
    )


def _create_parsed_job(
    client,
    sessions,
    raw: str,
    key: str,
    *,
    confirm: bool = False,
):
    job = client.post(
        "/v1/jobs",
        json={"title": "目标岗位", "raw": raw},
        headers={"Idempotency-Key": key},
    )
    assert job.status_code == 201, job.text
    parsed = client.post(
        f"/v1/jobs/{job.json()['id']}/parse",
        headers={"Idempotency-Key": f"{key}-parse"},
    )
    assert parsed.status_code == 202, parsed.text
    claim = asyncio.run(
        client.app.state.task_service.claim_task("usr_a", parsed.json()["task_id"])
    )
    assert claim is not None
    asyncio.run(
        client.app.state.job_service.process_parse(
            "usr_a",
            job.json()["id"],
            trace_id=f"trace-{key}",
            task_id=parsed.json()["task_id"],
            claim_token=claim.token,
            task_service=client.app.state.task_service,
        )
    )
    requirement_id = asyncio.run(
        _first_requirement_id(sessions, job.json()["id"])
    )
    if confirm:
        parsed_job = client.get(f"/v1/jobs/{job.json()['id']}").json()
        for index, requirement in enumerate(parsed_job["requirements"]):
            response = client.patch(
                f"/v1/jobs/{job.json()['id']}/requirements/{requirement['id']}",
                json={"confirmed": True},
                headers={"Idempotency-Key": f"{key}-confirm-{index}"},
            )
            assert response.status_code == 200, response.text
    return job.json()["id"], requirement_id


async def _first_requirement_id(sessions, job_id: str):
    async with sessions() as session:
        from app.db.models import JdRequirement

        return await session.scalar(
            select(JdRequirement.id)
            .where(
                JdRequirement.job_id == job_id,
                JdRequirement.owner_user_id == "usr_a",
            )
            .order_by(JdRequirement.priority, JdRequirement.id)
        )


async def _first_suggestion_id(sessions, analysis_id: str):
    async with sessions() as session:
        return await session.scalar(
            select(Suggestion.id)
            .where(
                Suggestion.analysis_id == analysis_id,
                Suggestion.owner_user_id == "usr_a",
            )
            .order_by(Suggestion.id)
        )


async def _seed_alias_owned_match_input(sessions):
    async with sessions.begin() as session:
        session.add(
            UserAlias(alias_user_id="usr_b", canonical_user_id="usr_a")
        )
        base = Resume(
            id="review_alias_base",
            owner_user_id="usr_b",
            kind="base",
            title="Alias Base",
            head_version=1,
        )
        version = ResumeVersion(
            id="review_alias_version",
            owner_user_id="usr_b",
            resume_id=base.id,
            snapshot_json={
                "schema_version": "1",
                "title": "Alias Base",
                "target": None,
                "sections": [],
            },
            snapshot_hash="a" * 64,
            created_by="usr_b",
        )
        job = JobDescription(
            id="review_alias_job",
            owner_user_id="usr_b",
            title="Alias Job",
            raw_encrypted="Python",
            status="parsed",
        )
        session.add_all([base, version, job])
        await session.flush()
        base.head_version_id = version.id
        session.add(
            JdRequirement(
                id="review_alias_requirement",
                owner_user_id="usr_b",
                job_id=job.id,
                type="must_have",
                priority=1,
                text_encrypted="Python",
                confirmed=True,
                source_start=0,
                source_end=6,
                source_hash=hashlib.sha256(b"Python").hexdigest(),
                explicitness="explicit",
                confidence_band="high",
                generation_mode="rule_fallback",
                workflow_version="legacy-rule-fallback@1",
                ai_run_id=None,
                input_hash="a" * 64,
            )
        )
    return version.id, job.id


async def _version_and_resume(sessions, version_id: str):
    async with sessions() as session:
        version = await session.scalar(
            select(ResumeVersion).where(
                ResumeVersion.id == version_id,
                ResumeVersion.owner_user_id == "usr_a",
            )
        )
        resume = await session.scalar(
            select(Resume).where(
                Resume.id == version.resume_id,
                Resume.owner_user_id == "usr_a",
            )
        )
        return version, resume


async def _resume_heads(sessions, base_id: str, targeted_id: str):
    async with sessions() as session:
        base = await session.scalar(
            select(Resume).where(
                Resume.id == base_id,
                Resume.owner_user_id == "usr_a",
            )
        )
        targeted = await session.scalar(
            select(Resume).where(
                Resume.id == targeted_id,
                Resume.owner_user_id == "usr_a",
            )
        )
        return base.head_version_id, targeted.head_version_id


async def _suggestion_audit(sessions, analysis_id: str):
    async with sessions() as session:
        return list(
            (
                await session.execute(
                    select(Suggestion.id, SuggestionFactLink.fact_id)
                    .join(
                        SuggestionFactLink,
                        (SuggestionFactLink.suggestion_id == Suggestion.id)
                        & (
                            SuggestionFactLink.owner_user_id
                            == Suggestion.owner_user_id
                        ),
                    )
                    .where(
                        Suggestion.analysis_id == analysis_id,
                        Suggestion.owner_user_id == "usr_a",
                        SuggestionFactLink.owner_user_id == "usr_a",
                    )
                )
            ).all()
        )


async def _export_expiry(sessions, export_id: str):
    async with sessions() as session:
        row = await session.scalar(
            select(File)
            .join(
                Export,
                (Export.file_id == File.id)
                & (Export.owner_user_id == File.owner_user_id),
            )
            .where(
                Export.id == export_id,
                Export.owner_user_id == "usr_a",
                File.owner_user_id == "usr_a",
            )
        )
        value = row.expires_at
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


async def _job_and_durable_counts(sessions, job_id: str):
    async with sessions() as session:
        status = await session.scalar(
            select(JobDescription.status).where(
                JobDescription.id == job_id,
                JobDescription.owner_user_id == "usr_a",
            )
        )
        tasks = await session.scalar(
            select(func.count())
            .select_from(Task)
            .where(
                Task.resource_type == "job_description",
                Task.resource_id == job_id,
                Task.owner_user_id == "usr_a",
            )
        )
        outbox = await session.scalar(
            select(func.count())
            .select_from(Outbox)
            .join(
                Task,
                (Task.id == Outbox.task_id)
                & (Task.owner_user_id == Outbox.owner_user_id),
            )
            .where(
                Task.resource_type == "job_description",
                Task.resource_id == job_id,
                Task.owner_user_id == "usr_a",
                Outbox.owner_user_id == "usr_a",
            )
        )
        return status, tasks, outbox


def _confirmed_upload(client, content: bytes) -> str:
    created = client.post(
        "/v1/files/upload-tokens",
        json={
            "display_name": "resume.txt",
            "mime": "text/plain",
            "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
            "purpose": "resume_import",
        },
        headers={"Idempotency-Key": "review-upload-token"},
    )
    assert created.status_code == 201
    uploaded = client.put(
        created.json()["upload_url"],
        content=content,
        headers={"Content-Type": "text/plain"},
    )
    assert uploaded.status_code == 204
    confirmed = client.post(
        f"/v1/files/{created.json()['file_id']}/confirm-upload",
        headers={"Idempotency-Key": "review-atomic-file-confirm"},
    )
    assert confirmed.status_code == 200
    return created.json()["file_id"]


async def _owned_count(sessions, kind: str) -> int:
    model = {
        "import": __import__(
            "app.db.models", fromlist=["ResumeImport"]
        ).ResumeImport,
        "task": Task,
        "outbox": Outbox,
        "match": MatchAnalysis,
        "export": Export,
    }[kind]
    async with sessions() as session:
        return int(
            await session.scalar(
                select(func.count())
                .select_from(model)
                .where(model.owner_user_id == "usr_a")
            )
            or 0
        )
