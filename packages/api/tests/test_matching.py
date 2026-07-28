import asyncio
import hashlib
import json
from datetime import datetime, timedelta, timezone

from app.db.models import (
    Fact,
    FactSource,
    File as FileModel,
    Export,
    JdRequirement,
    JobDescription,
    MatchAnalysis,
    Resume,
    ResumeImport,
    ResumeVersion,
    SourceRecord,
    Suggestion,
    VersionOperation,
)
from app.modules.resumes.service import canonical_snapshot
from app.integrations.ai_client import InternalAiClient
from app.modules.matching.service import MATCH_CATEGORIES, classify_requirements
import httpx


def test_matching_emits_exactly_the_four_contract_categories():
    items = classify_requirements(
        [
            {"id": "r1", "text": "Python"},
            {"id": "r2", "text": "SQL"},
            {"id": "r3", "text": "Kubernetes"},
            {"id": "r4", "text": "沟通能力"},
        ],
        facts=("熟练 Python", "做过数据库项目"),
    )

    assert MATCH_CATEGORIES == {
        "proved",
        "underexpressed",
        "needs_confirmation",
        "real_gap",
    }
    assert {item.category for item in items} <= MATCH_CATEGORIES
    assert len(items) == 4


def test_internal_ai_client_sends_exact_pi_contract_and_polls_to_terminal():
    calls = {"get": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer service-token"
        if request.method == "POST":
            body = json.loads(request.content)
            assert set(body) == {
                "workflow_type",
                "workflow_version",
                "trace_id",
                "task_id",
                "locale",
                "target",
                "confirmed_facts",
                "jd_requirements",
                "current_object",
            }
            assert body["confirmed_facts"][0]["status"] == "confirmed"
            return httpx.Response(
                202, json={"ai_run_id": "run_1", "status": "queued"}
            )
        calls["get"] += 1
        if calls["get"] == 1:
            return httpx.Response(
                200, json={"run": {"ai_run_id": "run_1", "status": "running"}}
            )
        return httpx.Response(
            200,
            json={
                "run": {
                    "ai_run_id": "run_1",
                    "status": "succeeded",
                    "output": {"matches": []},
                }
            },
        )

    client = InternalAiClient(
        "http://pi.internal",
        "service-token",
        poll_interval_seconds=0,
        transport=httpx.MockTransport(handler),
    )
    result = asyncio.run(
        client.run(
            workflow_type="match_resume_to_jd",
            workflow_version="1",
            trace_id="trace_1",
            task_id="task_1",
            facts=[
                {
                    "id": "fact_1",
                    "kind": "skill",
                    "value": "Python",
                    "status": "confirmed",
                }
            ],
            input_data={
                "target": "后端工程师",
                "jd_requirements": [
                    {"id": "req_1", "type": "must_have", "text": "Python"}
                ],
                "resume_snapshot": {"title": "张三"},
            },
        )
    )
    assert result["result"] == {"matches": []}
    assert calls["get"] == 2


def test_job_parse_and_match_api_returns_evidence_and_complete_suggestion(
    pipeline_client,
):
    client, sessions, _ = pipeline_client
    version_id = asyncio.run(_seed_resume(sessions))
    job = client.post(
        "/v1/jobs",
        json={"title": "后端实习生", "company": "示例公司", "raw": "Python SQL"},
        headers={"Idempotency-Key": "job-create"},
    )
    parsed = client.post(
        f"/v1/jobs/{job.json()['id']}/parse",
        headers={"Idempotency-Key": "job-parse"},
    )
    asyncio.run(
        client.app.state.job_service.process_parse(
            "usr_a",
            job.json()["id"],
            trace_id="trace_job_parse",
            task_id=parsed.json()["task_id"],
        )
    )
    match = client.post(
        "/v1/match-analyses",
        json={"resume_version_id": version_id, "job_id": job.json()["id"]},
        headers={"Idempotency-Key": "match-create"},
    )
    asyncio.run(
        client.app.state.matching_service.process_match(
            "usr_a",
            match.json()["id"],
            trace_id="trace_match",
            task_id=match.json()["task_id"],
        )
    )
    suggestions = client.get(
        f"/v1/match-analyses/{match.json()['id']}/suggestions"
    )
    completed = client.get(f"/v1/match-analyses/{match.json()['id']}")

    assert job.status_code == 201
    assert parsed.status_code == 202
    assert parsed.json()["status"] == "queued"
    assert parsed.json()["task_id"]
    assert match.status_code == 202
    assert match.json()["status"] == "queued"
    assert completed.json()["status"] == "succeeded"
    assert match.json()["items"][0]["category"] == "underexpressed"
    assert len(match.json()["items"][0]["evidence_refs"]) == 1
    item = suggestions.json()["items"][0]
    assert suggestions.status_code == 200
    assert item["status"] == "pending"
    assert item["requirement_id"] == match.json()["items"][0]["requirement_id"]
    assert item["original_text"] == "使用 Python 开发服务"
    assert item["suggested_text"]
    assert item["reason"]
    assert item["fact_refs"]
    assert item["risk_flags"] == []


def test_pipeline_resources_are_not_visible_across_owners(pipeline_client):
    client, sessions, _ = pipeline_client
    asyncio.run(_seed_other_owner_pipeline(sessions))

    assert client.delete(
        "/v1/files/file_b",
        headers={"Idempotency-Key": "delete-other-file"},
    ).status_code == 404
    assert client.get("/v1/imports/import_b").status_code == 404
    assert client.post(
        "/v1/jobs/job_b/parse",
        headers={"Idempotency-Key": "parse-other-job"},
    ).status_code == 404
    assert client.get("/v1/match-analyses/match_b").status_code == 404
    assert client.post(
        "/v1/suggestions/suggestion_b/accept",
        headers={"Idempotency-Key": "accept-other-suggestion"},
    ).status_code == 404
    assert client.get("/v1/exports/export_b").status_code == 404


async def _seed_resume(sessions) -> str:
    canonical, digest = canonical_snapshot(
        {
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
                            "text": "使用 Python 开发服务",
                            "fact_refs": ["fact_python"],
                        }
                    ],
                }
            ],
        }
    )
    async with sessions.begin() as session:
        source = SourceRecord(
            id="src_python",
            owner_user_id="usr_a",
            source_type="user_confirmation",
            content_encrypted="Python",
        )
        fact = Fact(
            id="fact_python",
            owner_user_id="usr_a",
            kind="skill",
            value_encrypted="Python",
            status="unconfirmed",
        )
        resume = Resume(
            id="resume_match",
            owner_user_id="usr_a",
            kind="base",
            title="张三",
            head_version=0,
        )
        session.add_all([source, fact, resume])
        await session.flush()
        session.add(
            FactSource(
                fact_id=fact.id,
                source_record_id=source.id,
                owner_user_id="usr_a",
                source_hash=hashlib.sha256(b"Python").hexdigest(),
            )
        )
        await session.flush()
        fact.status = "confirmed"
        fact.confirmed_at = datetime.now(timezone.utc)
        version = ResumeVersion(
            id="version_match",
            owner_user_id="usr_a",
            resume_id=resume.id,
            snapshot_json=canonical,
            snapshot_hash=digest,
            created_by="usr_a",
        )
        session.add(version)
        await session.flush()
        resume.head_version = 1
        resume.head_version_id = version.id
        session.add(
            VersionOperation(
                id="vop_match",
                owner_user_id="usr_a",
                version_id=version.id,
                operation_type="save",
                actor="usr_a",
                metadata_json={},
            )
        )
    return version.id


async def _seed_other_owner_pipeline(sessions) -> None:
    canonical, digest = canonical_snapshot(
        {
            "schema_version": "1",
            "title": "Private",
            "target": None,
            "sections": [],
        }
    )
    async with sessions.begin() as session:
        file_row = FileModel(
            id="file_b",
            owner_user_id="usr_b",
            purpose="resume_import",
            display_name="private.txt",
            object_key="uploads/private-b",
            sha256="0" * 64,
            size=1,
            mime="text/plain",
            status="confirmed",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        export_file = FileModel(
            id="export_file_b",
            owner_user_id="usr_b",
            purpose="resume_export",
            display_name="private.pdf",
            object_key="exports/private-b",
            sha256="1" * 64,
            size=1,
            mime="application/pdf",
            status="confirmed",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        job = JobDescription(
            id="job_b",
            owner_user_id="usr_b",
            title="Private job",
            raw_encrypted="Python",
            status="parsed",
        )
        resume = Resume(
            id="resume_b",
            owner_user_id="usr_b",
            kind="base",
            title="Private",
            head_version=0,
        )
        session.add_all([file_row, export_file, job, resume])
        await session.flush()
        version = ResumeVersion(
            id="version_b",
            owner_user_id="usr_b",
            resume_id=resume.id,
            snapshot_json=canonical,
            snapshot_hash=digest,
            created_by="usr_b",
        )
        requirement = JdRequirement(
            id="requirement_b",
            owner_user_id="usr_b",
            job_id=job.id,
            type="must_have",
            priority=1,
            text_encrypted="Python",
            confirmed=True,
        )
        imported = ResumeImport(
            id="import_b",
            owner_user_id="usr_b",
            file_id=file_row.id,
            status="parsed",
            draft_facts=[],
        )
        session.add_all([version, requirement, imported])
        await session.flush()
        resume.head_version = 1
        resume.head_version_id = version.id
        analysis = MatchAnalysis(
            id="match_b",
            owner_user_id="usr_b",
            resume_version_id=version.id,
            job_id=job.id,
            status="succeeded",
            workflow_version="1",
        )
        export = Export(
            id="export_b",
            owner_user_id="usr_b",
            resume_version_id=version.id,
            template_version="clear-standard",
            file_id=export_file.id,
            content_hash=digest,
            status="succeeded",
            download_name="private.pdf",
        )
        session.add_all([analysis, export])
        await session.flush()
        session.add(
            Suggestion(
                id="suggestion_b",
                owner_user_id="usr_b",
                analysis_id=analysis.id,
                target_path="/title",
                original_hash=hashlib.sha256(b"Private").hexdigest(),
                original_text_encrypted="Private",
                suggested_encrypted="Private changed",
                reason="private",
                risk_flags=[],
                status="pending",
            )
        )
