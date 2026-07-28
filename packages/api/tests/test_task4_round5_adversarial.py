import asyncio
import importlib
import importlib.util

from sqlalchemy import select

from app.db.models import BulletFactLink, ResumeVersion
from test_resume_versions import _headers, _resume, resume_client


def _snapshot(text: str, bullet_id: str = "bullet_1") -> dict:
    return {
        "schema_version": "1",
        "title": "Round five evidence",
        "target": None,
        "sections": [
            {
                "id": "experience",
                "type": "experience",
                "title": "Experience",
                "items": [{"id": bullet_id, "text": text, "fact_refs": []}],
            }
        ],
    }


def _payload(snapshot: dict, claim_evidence: list[dict]) -> dict:
    return {
        "base_version": 0,
        "snapshot": snapshot,
        "claim_evidence": claim_evidence,
    }


def _confirmed_fact(client, value: str, key: str) -> str:
    response = client.post(
        "/v1/facts",
        json={
            "kind": "responsibility",
            "value": value,
            "status": "confirmed",
            "sources": [
                {"source_type": "user_confirmation", "content": value}
            ],
        },
        headers=_headers(key),
    )
    assert response.status_code == 201
    return response.json()["id"]


def test_one_fact_can_support_two_adjacent_ranges_in_one_bullet(resume_client):
    """The persisted graph must distinguish ranges even when fact and bullet match."""
    client, sessions = resume_client
    first = "Managed operations "
    text = f"{first}Managed operations"
    fact_id = _confirmed_fact(client, "Managed operations", "round5-multi-range-fact")
    resume_id = _resume(client, "round5-multi-range-resume")
    response = client.post(
        f"/v1/resumes/{resume_id}/versions",
        json=_payload(
            _snapshot(text),
            [
                {"bullet_id": "bullet_1", "start": 0, "end": len(first), "fact_refs": [fact_id]},
                {"bullet_id": "bullet_1", "start": len(first), "end": len(text), "fact_refs": [fact_id]},
            ],
        ),
        headers=_headers("round5-multi-range-save"),
    )

    assert response.status_code == 201

    async def links():
        async with sessions() as session:
            return list(
                (
                    await session.scalars(
                        select(BulletFactLink).where(
                            BulletFactLink.resume_version_id == response.json()["id"]
                        )
                    )
                ).all()
            )

    assert sorted(
        ((link.fact_id, link.claim_range) for link in asyncio.run(links())),
        key=lambda item: (item[0], item[1]["start"], item[1]["end"]),
    ) == [
        (fact_id, {"start": 0, "end": len(first)}),
        (fact_id, {"start": len(first), "end": len(text)}),
    ]


def test_explicit_ordinary_responsibility_mapping_does_not_require_lexical_overlap(
    resume_client,
):
    """A no-number synonym rewrite is valid because the graph is explicit."""
    client, _ = resume_client
    text = "Managed client onboarding"
    fact_id = _confirmed_fact(
        client,
        "Guided customer activation",
        "round5-synonym-fact",
    )
    resume_id = _resume(client, "round5-synonym-resume")
    response = client.post(
        f"/v1/resumes/{resume_id}/versions",
        json=_payload(
            _snapshot(text),
            [{"bullet_id": "bullet_1", "start": 0, "end": len(text), "fact_refs": [fact_id]}],
        ),
        headers=_headers("round5-synonym-save"),
    )

    assert response.status_code == 201


def test_saved_version_quality_is_reproducible_after_linked_fact_changes(resume_client):
    """A mutable Fact cannot retrospectively invalidate an immutable version."""
    client, _ = resume_client
    text = "Managed operations"
    fact_id = _confirmed_fact(client, text, "round5-immutable-fact")
    resume_id = _resume(client, "round5-immutable-resume")
    saved = client.post(
        f"/v1/resumes/{resume_id}/versions",
        json=_payload(
            _snapshot(text),
            [{"bullet_id": "bullet_1", "start": 0, "end": len(text), "fact_refs": [fact_id]}],
        ),
        headers=_headers("round5-immutable-save"),
    )
    assert saved.status_code == 201
    before = client.post(f"/v1/resumes/{resume_id}/quality-checks")
    changed = client.patch(
        f"/v1/facts/{fact_id}",
        json={"value": "Unrelated work"},
        headers=_headers("round5-immutable-fact-change"),
    )
    after = client.post(f"/v1/resumes/{resume_id}/quality-checks")

    assert before.status_code == 200
    assert changed.status_code == 200
    assert changed.json()["status"] == "unconfirmed"
    assert after.status_code == 200
    assert after.json() == before.json() == {"issues": []}


def test_duplicate_bullet_id_is_rejected_without_writing_version_or_links(resume_client):
    """Range identity is ambiguous until bullet IDs are globally unique in a snapshot."""
    client, sessions = resume_client
    text = "Managed operations"
    fact_id = _confirmed_fact(client, text, "round5-duplicate-bullet-fact")
    resume_id = _resume(client, "round5-duplicate-bullet-resume")
    snapshot = _snapshot(text, "bullet_duplicate")
    snapshot["sections"].append(
        {
            "id": "education",
            "type": "education",
            "title": "Education",
            "items": [
                {
                    "id": "bullet_duplicate",
                    "text": text,
                    "fact_refs": [],
                }
            ],
        }
    )
    response = client.post(
        f"/v1/resumes/{resume_id}/versions",
        json=_payload(
            snapshot,
            [{"bullet_id": "bullet_duplicate", "start": 0, "end": len(text), "fact_refs": [fact_id]}],
        ),
        headers=_headers("round5-duplicate-bullet-save"),
    )

    async def persisted_counts():
        async with sessions() as session:
            versions = list(
                (
                    await session.scalars(
                        select(ResumeVersion).where(ResumeVersion.resume_id == resume_id)
                    )
                ).all()
            )
            links = list((await session.scalars(select(BulletFactLink))).all())
            return len(versions), len(links)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "CLAIM_EVIDENCE_DUPLICATE_BULLET_ID"
    assert asyncio.run(persisted_counts()) == (0, 0)


def test_postgresql_audit_url_uses_a_read_only_connection(monkeypatch):
    """The production audit contract must work against PostgreSQL without writes."""
    audit = importlib.import_module("scripts.audit_legacy_resume_references")
    statements: list[str] = []

    class Result:
        def fetchall(self):
            return [("legacy_resume",)]

    class Connection:
        def execute(self, statement):
            statements.append(str(statement))
            return Result()

        def close(self):
            statements.append("CLOSE")

    monkeypatch.setattr(audit, "_connect_read_only", lambda url: Connection(), raising=False)

    assert audit.audit("postgresql+psycopg://user:secret@db/resumes") == (
        1,
        ["legacy_resume"],
    )
    assert statements[0] == "BEGIN READ ONLY"
    assert statements[1].lstrip().upper().startswith("SELECT ID FROM RESUMES")
    assert statements[-1] == "CLOSE"


def test_version_evidence_projection_consumer_contract_is_importable():
    """Export and Pi need one shared persisted-graph projection before either consumes it."""
    module_name = "app.modules.resumes.evidence_projection"
    assert importlib.util.find_spec(module_name) is not None

    projection = importlib.import_module(module_name)
    assert hasattr(projection, "VersionEvidenceProjection")
    assert callable(projection.load_version_evidence)
