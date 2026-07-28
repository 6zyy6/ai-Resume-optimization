import pytest
from pydantic import ValidationError

from app.contracts import FactStatus, MatchCategory, SuggestionStatus, TaskRecord, TaskStatus
from scripts.export_openapi import build_application


def test_contract_enums_are_authoritative():
    assert [value.value for value in FactStatus] == ["unconfirmed", "confirmed", "rejected"]
    assert [value.value for value in TaskStatus] == [
        "queued", "running", "succeeded", "failed", "cancelled", "waiting_for_user"
    ]
    assert [value.value for value in MatchCategory] == [
        "proved", "underexpressed", "needs_confirmation", "real_gap"
    ]
    assert [value.value for value in SuggestionStatus] == [
        "pending", "accepted", "edited", "ignored", "reverted", "blocked"
    ]


def test_task_record_rejects_coerced_progress():
    with pytest.raises(ValidationError):
        TaskRecord(
            id="task_1",
            type="resume_analysis",
            status=TaskStatus.QUEUED,
            progress="1",
            stage="queued",
            trace_id="tr_1",
            result_ref=None,
            error_code=None,
        )


def test_openapi_includes_all_enum_contracts():
    schemas = build_application().openapi()["components"]["schemas"]

    assert schemas["FactStatus"]["enum"] == ["unconfirmed", "confirmed", "rejected"]
    assert schemas["TaskStatus"]["enum"] == [
        "queued", "running", "succeeded", "failed", "cancelled", "waiting_for_user"
    ]
    assert schemas["MatchCategory"]["enum"] == [
        "proved", "underexpressed", "needs_confirmation", "real_gap"
    ]
    assert schemas["SuggestionStatus"]["enum"] == [
        "pending", "accepted", "edited", "ignored", "reverted", "blocked"
    ]


def test_openapi_includes_auth_usage_and_privacy_routes():
    paths = build_application().openapi()["paths"]

    assert "/v1/auth/email/start" in paths
    assert "/v1/auth/email/verify" in paths
    assert "/v1/auth/wechat/login" in paths
    assert "/v1/auth/identities/bind-email" in paths
    assert "/v1/auth/refresh" in paths
    assert "/v1/auth/logout" in paths
    assert "/v1/me/usage" in paths
    assert "/v1/me/data-exports" in paths
    assert "/v1/me/deletion-requests" in paths
    assert "/v1/files/upload-tokens" in paths
    assert "/v1/imports" in paths
    assert "/v1/jobs" in paths
    assert "/v1/match-analyses" in paths
    assert "/v1/suggestions/{suggestion_id}/accept" in paths
    assert "/v1/exports" in paths


def test_openapi_declares_the_runtime_error_envelope():
    paths = build_application().openapi()["paths"]

    auth_validation = paths["/v1/auth/email/start"]["post"]["responses"]["422"]
    privacy_limit = paths["/v1/me/data-exports"]["post"]["responses"]["429"]
    usage_auth = paths["/v1/me/usage"]["get"]["responses"]["401"]
    assert auth_validation["content"]["application/json"]["schema"]["$ref"].endswith(
        "/ApiErrorEnvelope"
    )
    assert privacy_limit["content"]["application/json"]["schema"]["$ref"].endswith(
        "/ApiErrorEnvelope"
    )
    assert usage_auth["content"]["application/json"]["schema"]["$ref"].endswith(
        "/ApiErrorEnvelope"
    )
