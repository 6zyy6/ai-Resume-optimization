import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import select, text as sql_text

from app.db.models import BulletFactLink, Fact
from app.modules.facts.service import FactService
from app.modules.resumes.schemas import VersionCreate
from test_resume_versions import _headers, _resume, resume_client


def _snapshot(text: str = "Increased conversion by 12.5% and processed 1,000 orders.") -> dict:
    return {
        "schema_version": "1",
        "title": "Evidence resume",
        "target": None,
        "sections": [
            {
                "id": "experience",
                "type": "experience",
                "title": "Experience",
                "items": [
                    {
                        "id": "bullet_1",
                        "text": text,
                        "fact_refs": [],
                    }
                ],
            }
        ],
    }


def _version_payload(claim_evidence: list[dict] | None = None, text: str = "Increased conversion by 12.5% and processed 1,000 orders.") -> dict:
    payload = {"base_version": 0, "snapshot": _snapshot(text)}
    if claim_evidence is not None:
        payload["claim_evidence"] = claim_evidence
    return payload


def test_version_create_exposes_required_claim_evidence_in_runtime_schema():
    """Without a separate evidence graph, the public version contract cannot validate claims."""
    schema = VersionCreate.model_json_schema()

    assert "claim_evidence" in schema["properties"]
    assert "claim_evidence" in schema["required"]


@pytest.mark.parametrize(
    "claim_evidence, expected_code",
    [
        (None, "VALIDATION_FAILED"),
        ([{"bullet_id": "missing", "start": 0, "end": 1, "fact_refs": []}], "CLAIM_EVIDENCE_UNKNOWN_BULLET"),
        ([{"bullet_id": "bullet_1", "start": -1, "end": 4, "fact_refs": []}], "CLAIM_EVIDENCE_RANGE_INVALID"),
        ([{"bullet_id": "bullet_1", "start": 0, "end": 999, "fact_refs": []}], "CLAIM_EVIDENCE_RANGE_INVALID"),
        ([{"bullet_id": "bullet_1", "start": 0, "end": 8, "fact_refs": []}, {"bullet_id": "bullet_1", "start": 7, "end": 16, "fact_refs": []}], "CLAIM_EVIDENCE_RANGE_OVERLAP"),
        ([{"bullet_id": "bullet_1", "start": 0, "end": 8, "fact_refs": []}, {"bullet_id": "bullet_1", "start": 0, "end": 8, "fact_refs": []}], "CLAIM_EVIDENCE_RANGE_OVERLAP"),
        ([{"bullet_id": "bullet_1", "start": 0, "end": 8, "fact_refs": []}], "CLAIM_EVIDENCE_COVERAGE_REQUIRED"),
    ],
)
def test_claim_evidence_validation_has_stable_domain_errors(
    resume_client, claim_evidence, expected_code
):
    """Range/coverage errors must not collapse into generic text similarity failures."""
    client, _ = resume_client
    resume_id = _resume(client, f"claim-evidence-{expected_code}")

    response = client.post(
        f"/v1/resumes/{resume_id}/versions",
        json=_version_payload(claim_evidence),
        headers=_headers(f"claim-evidence-{expected_code}"),
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == expected_code


def test_version_create_rejects_unknown_claim_evidence_fields(resume_client):
    client, _ = resume_client
    resume_id = _resume(client, "claim-evidence-unknown-field")
    payload = _version_payload([])
    payload["unsupported_claim_field"] = True

    response = client.post(
        f"/v1/resumes/{resume_id}/versions",
        json=payload,
        headers=_headers("claim-evidence-unknown-field"),
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"


def test_openapi_and_generated_types_publish_claim_evidence():
    """The runtime and generated contracts must agree on the evidence parameter."""
    generated = json.loads(
        (Path(__file__).resolve().parents[3] / "packages/shared/generated/openapi.json").read_text()
    )
    schema = generated["components"]["schemas"]["VersionCreate"]

    assert "claim_evidence" in schema["properties"]
    assert "claim_evidence" in schema["required"]


def _evidence(fact_id: str, text: str) -> list[dict]:
    return [{"bullet_id": "bullet_1", "start": 0, "end": len(text), "fact_refs": [fact_id]}]


@pytest.mark.parametrize(
    ("text", "fact_value"),
    [
        ("Increased conversion by 12.5%", "Conversion increased by 12.5%"),
        ("Processed 1,000 orders", "Handled 1,000 customer orders"),
        ("将转化率提升12.5%", "转化率同比提升12.5%"),
    ],
)
def test_explicit_evidence_allows_supported_english_chinese_and_numbers(
    resume_client, text, fact_value
):
    """A valid mapping should survive paraphrase without heuristic matching."""
    client, _ = resume_client
    fact = client.post(
        "/v1/facts",
        json={"kind": "metric", "value": fact_value, "status": "confirmed", "sources": [{"source_type": "user_confirmation", "content": fact_value}]},
        headers={"Idempotency-Key": "supported-fact"},
    )
    resume_id = _resume(client, "supported-resume")
    response = client.post(
        f"/v1/resumes/{resume_id}/versions",
        json=_version_payload(_evidence(fact.json()["id"], text), text),
        headers=_headers("supported-version"),
    )

    assert fact.status_code == 201
    assert response.status_code == 201


@pytest.mark.parametrize(
    ("text", "fact_value"),
    [
        ("Increased conversion by 12.5%", "Reduced refund rate by 12.5%"),
        ("将转化率提升12.5%", "将退款率降低12.5%"),
    ],
)
def test_same_number_with_different_subject_or_result_is_not_evidence(
    resume_client, text, fact_value
):
    client, _ = resume_client
    fact = client.post(
        "/v1/facts",
        json={"kind": "metric", "value": fact_value, "status": "confirmed", "sources": [{"source_type": "user_confirmation", "content": fact_value}]},
        headers={"Idempotency-Key": "adversarial-fact"},
    )
    resume_id = _resume(client, "adversarial-resume")
    response = client.post(
        f"/v1/resumes/{resume_id}/versions",
        json=_version_payload(_evidence(fact.json()["id"], text), text),
        headers=_headers("adversarial-version"),
    )

    assert fact.status_code == 201
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "CLAIM_EVIDENCE_FACT_MISMATCH"


@pytest.mark.parametrize(
    ("kind", "status", "expected_code"),
    [
        ("unconfirmed", "unconfirmed", "CLAIM_EVIDENCE_FACT_NOT_CONFIRMED"),
        ("rejected", "rejected", "CLAIM_EVIDENCE_FACT_NOT_CONFIRMED"),
    ],
)
def test_claim_evidence_rejects_non_confirmed_facts(
    resume_client, kind, status, expected_code
):
    """Fact IDs alone must not authorize a claim."""
    client, _ = resume_client
    text = "Increased conversion by 12.5%"
    fact = client.post(
        "/v1/facts", json={"kind": kind, "value": text, "status": status}, headers={"Idempotency-Key": f"{kind}-fact"}
    )
    resume_id = _resume(client, f"{kind}-resume")
    response = client.post(f"/v1/resumes/{resume_id}/versions", json=_version_payload(_evidence(fact.json()["id"], text), text), headers=_headers(f"{kind}-version"))

    assert fact.status_code == 201
    assert response.status_code == 422
    assert response.json()["error"]["code"] == expected_code


def test_cross_owner_fact_cannot_support_claim(resume_client):
    """A real foreign fact must fail before any link row is written."""
    client, sessions = resume_client
    text = "Increased conversion by 12.5%"
    other = asyncio.run(FactService(sessions).create_fact("usr_b", kind="metric", value=text, status="confirmed", sources=[{"source_type": "user_confirmation", "content": text}]))
    resume_id = _resume(client, "cross-owner-evidence-resume")
    response = client.post(f"/v1/resumes/{resume_id}/versions", json=_version_payload(_evidence(other.id, text), text), headers=_headers("cross-owner-evidence-version"))

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "CLAIM_EVIDENCE_FACT_OWNER_INVALID"


def test_source_less_confirmed_fact_cannot_support_claim(resume_client):
    """A malformed legacy fact is still not permitted to attest to a claim."""
    client, sessions = resume_client
    text = "Increased conversion by 12.5%"

    async def seed_source_less_fact():
        async with sessions.begin() as session:
            await session.execute(
                sql_text("DROP TRIGGER IF EXISTS trg_confirmed_fact_requires_source_insert")
            )
            session.add(
                Fact(
                    id="source_less_confirmed",
                    owner_user_id="usr_a",
                    kind="metric",
                    value_encrypted=text,
                    status="confirmed",
                    confirmed_at=datetime.now(timezone.utc),
                )
            )

    asyncio.run(seed_source_less_fact())
    resume_id = _resume(client, "source-less-evidence-resume")
    response = client.post(
        f"/v1/resumes/{resume_id}/versions",
        json=_version_payload(_evidence("source_less_confirmed", text), text),
        headers=_headers("source-less-evidence-version"),
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "CLAIM_EVIDENCE_FACT_SOURCE_REQUIRED"


def test_save_and_restore_preserve_exact_claim_link_ranges(resume_client):
    """Restore must copy the immutable evidence graph instead of re-parsing prose."""
    client, sessions = resume_client
    text = "Increased conversion by 12.5%"
    fact = client.post(
        "/v1/facts",
        json={"kind": "metric", "value": "Conversion increased by 12.5%", "status": "confirmed", "sources": [{"source_type": "user_confirmation", "content": "Conversion increased by 12.5%"}]},
        headers={"Idempotency-Key": "links-fact"},
    ).json()
    resume_id = _resume(client, "links-resume")
    saved = client.post(f"/v1/resumes/{resume_id}/versions", json=_version_payload(_evidence(fact["id"], text), text), headers=_headers("links-save"))
    assert saved.status_code == 201
    restored = client.post(f"/v1/resumes/{resume_id}/versions/{saved.json()['id']}/restore", json={"base_version": 1}, headers=_headers("links-restore"))

    async def links(version_id: str):
        async with sessions() as session:
            return list((await session.scalars(select(BulletFactLink).where(BulletFactLink.resume_version_id == version_id))).all())

    assert restored.status_code == 201
    source_links = asyncio.run(links(saved.json()["id"]))
    restored_links = asyncio.run(links(restored.json()["id"]))
    assert [(row.fact_id, row.claim_range) for row in source_links] == [(fact["id"], {"start": 0, "end": len(text)})]
    assert [(row.fact_id, row.claim_range) for row in restored_links] == [(fact["id"], {"start": 0, "end": len(text)})]
