from io import BytesIO
import asyncio

import pytest
from pypdf import PdfReader

from app.modules.exports.templates import (
    FONT_PATH,
    ExportBlocked,
    canonical_resume_text,
    content_hash,
    normalized_pdf_text,
    render_resume_pdf,
    sanitize_download_name,
)
from reportlab.pdfbase.ttfonts import TTFont


SNAPSHOT = {
    "schema_version": "1",
    "title": "张三的简历",
    "target": "后端工程师",
    "sections": [
        {
            "id": "experience",
            "title": "项目经历",
            "items": [
                {
                    "id": "bullet_1",
                    "text": "使用 Python 完成数据分析。",
                    "fact_refs": ["fact_python"],
                    "risk_flags": [],
                }
            ],
        }
    ],
}


def _text(pdf: bytes) -> str:
    return "\n".join(page.extract_text() or "" for page in PdfReader(BytesIO(pdf)).pages)


def test_two_templates_preserve_snapshot_hash_and_extractable_text():
    expected_hash = content_hash(SNAPSHOT)
    standard = render_resume_pdf(SNAPSHOT, "clear-standard")
    modern = render_resume_pdf(SNAPSHOT, "modern-whitespace")

    assert standard.snapshot_hash == expected_hash
    assert modern.snapshot_hash == expected_hash
    assert expected_hash == content_hash(SNAPSHOT)
    for rendered in (standard, modern):
        extracted = _text(rendered.pdf)
        assert "张三的简历" in extracted
        assert "Python" in extracted
        assert normalized_pdf_text(rendered.pdf) == canonical_resume_text(SNAPSHOT)


def test_export_blocks_pending_or_unsupported_claims():
    pending = {
        **SNAPSHOT,
        "sections": [
            {
                **SNAPSHOT["sections"][0],
                "items": [
                    {
                        **SNAPSHOT["sections"][0]["items"][0],
                        "risk_flags": ["needs_confirmation"],
                    }
                ],
            }
        ],
    }
    with pytest.raises(ExportBlocked, match="EXPORT_BLOCKED_BY_FACTS"):
        render_resume_pdf(pending, "clear-standard")


def test_download_name_is_sanitized_and_expiry_is_ten_minutes():
    assert sanitize_download_name("../../张三 简历?.pdf") == "张三_简历.pdf"
    rendered = render_resume_pdf(SNAPSHOT, "clear-standard")
    assert rendered.download_expires_in == 600


def test_bundled_font_covers_3500_common_and_100_rare_name_glyphs_in_pdf():
    common = "".join(chr(codepoint) for codepoint in range(0x4E00, 0x4E00 + 3500))
    rare_names = "".join(chr(codepoint) for codepoint in range(0x3400, 0x3400 + 100))
    cmap = TTFont("NotoCoverage", str(FONT_PATH)).face.charToGlyph
    missing = [
        character
        for character in common + rare_names
        if ord(character) not in cmap
    ]
    assert missing == []

    snapshot = {
        "schema_version": "1",
        "title": "中文字体覆盖",
        "target": None,
        "sections": [
            {
                "id": "glyphs",
                "title": "字符",
                "items": [
                    {
                        "id": "glyph_line",
                        "text": common + rare_names,
                        "fact_refs": ["font_fixture"],
                        "risk_flags": [],
                    }
                ],
            }
        ],
    }
    extracted = "".join(_text(render_resume_pdf(snapshot, "clear-standard").pdf).split())
    assert common in extracted
    assert rare_names in extracted


def test_export_api_persists_pdf_and_returns_ten_minute_signed_url(
    pipeline_client,
):
    client, _, storage = pipeline_client
    version_id = _create_exportable_version(client)
    created = client.post(
        "/v1/exports",
        json={
            "resume_version_id": version_id,
            "template_version": "modern-whitespace",
            "download_name": "../../张三 简历?.pdf",
        },
        headers={"Idempotency-Key": "export-create"},
    )
    assert created.status_code == 202
    assert created.json()["status"] == "queued"
    assert created.json()["task_id"]
    assert created.json()["download_url"] is None
    asyncio.run(
        client.app.state.export_service.process_export(
            "usr_a", created.json()["id"]
        )
    )
    fetched = client.get(f"/v1/exports/{created.json()['id']}")

    assert created.json()["download_name"] == "张三_简历.pdf"
    assert fetched.status_code == 200
    assert fetched.json()["status"] == "succeeded"
    assert fetched.json()["download_expires_in"] == 600
    assert fetched.json()["download_url"].startswith("/v1/storage/download/")
    downloaded = client.get(fetched.json()["download_url"])
    assert downloaded.status_code == 200, downloaded.text
    assert downloaded.content.startswith(b"%PDF-")
    tampered = client.get(
        fetched.json()["download_url"].replace(
            "/exports/", "/exports/tampered-", 1
        )
    )
    object_key = next(
        key for key, value in storage.objects.items() if value.content.startswith(b"%PDF-")
    )
    expired = client.get(
        storage._url(
            "download",
            object_key,
            -1,
            fetched.json()["download_name"],
        )
    )
    assert tampered.status_code == 403
    assert expired.status_code == 403
    assert fetched.json()["content_hash"] == created.json()["content_hash"]
    assert any(
        value.content.startswith(b"%PDF-") for value in storage.objects.values()
    )


def _create_exportable_version(client):
    fact = client.post(
        "/v1/facts",
        json={
            "kind": "skill",
            "value": "Python",
            "status": "confirmed",
            "sources": [{"source_type": "user_confirmation", "content": "Python"}],
        },
        headers={"Idempotency-Key": "export-fact"},
    )
    resume = client.post(
        "/v1/resumes",
        json={"kind": "base", "title": "张三"},
        headers={"Idempotency-Key": "export-resume"},
    )
    text = "使用 Python 完成数据分析"
    version = client.post(
        f"/v1/resumes/{resume.json()['id']}/versions",
        json={
            "base_version": 0,
            "snapshot": {
                "schema_version": "1",
                "title": "张三的简历",
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
        headers={"Idempotency-Key": "export-version"},
    )
    assert fact.status_code == 201
    assert resume.status_code == 201
    assert version.status_code == 201
    return version.json()["id"]
