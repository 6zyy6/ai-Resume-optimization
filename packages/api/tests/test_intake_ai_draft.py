from pathlib import Path

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from app.db.models import ResumeVersion
from app.modules.resumes.fact_policy import (
    ConfirmedFactProjection,
    DraftClaim,
    fact_policy_check,
)
from app.modules.resumes import quality as resume_quality


def test_resume_version_model_declares_generation_provenance_and_owner_fk():
    table = ResumeVersion.__table__

    assert {
        "generation_mode",
        "workflow_version",
        "ai_run_id",
        "input_hash",
    } <= set(table.c.keys())
    check_sql = " ".join(
        str(constraint.sqltext)
        for constraint in table.constraints
        if hasattr(constraint, "sqltext")
    )
    assert "manual" in check_sql
    assert "model" in check_sql
    assert "rule_fallback" in check_sql
    assert any(
        {element.parent.name for element in constraint.elements}
        == {"ai_run_id", "owner_user_id"}
        and {element.target_fullname for element in constraint.elements}
        == {"ai_runs.id", "ai_runs.owner_user_id"}
        for constraint in table.foreign_key_constraints
    )


def test_migration_0014_backfills_manual_versions_and_round_trips(
    tmp_path,
    monkeypatch,
):
    database_path = tmp_path / "resume-generation-provenance.db"
    config = _alembic_config(database_path)
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{database_path}")
    command.upgrade(config, "0013")
    engine = create_engine(f"sqlite:///{database_path}")
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (id, status, locale, created_at) VALUES "
                "('usr_draft', 'active', 'zh-CN', '2026-08-02 08:00:00')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO resumes "
                "(id, kind, title, head_version, created_at, owner_user_id) VALUES "
                "('resume_draft', 'base', '旧简历', 1, "
                "'2026-08-02 08:00:00', 'usr_draft')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO resume_versions "
                "(id, resume_id, parent_version_id, snapshot_json, snapshot_hash, "
                "created_by, created_at, owner_user_id) VALUES "
                "('rver_manual', 'resume_draft', NULL, '{}', 'hash_manual', "
                "'usr_draft', '2026-08-02 08:00:00', 'usr_draft')"
            )
        )

    command.upgrade(config, "0014")
    with engine.begin() as connection:
        row = connection.execute(
            text(
                "SELECT generation_mode, workflow_version, ai_run_id, input_hash "
                "FROM resume_versions WHERE id = 'rver_manual'"
            )
        ).one()
        assert row == ("manual", None, None, None)
        with pytest.raises(IntegrityError):
            connection.execute(
                text(
                    "UPDATE resume_versions SET generation_mode = 'silent_fallback' "
                    "WHERE id = 'rver_manual'"
                )
            )

    command.downgrade(config, "0013")
    assert "generation_mode" not in {
        column["name"] for column in inspect(engine).get_columns("resume_versions")
    }


def test_manual_resume_version_response_exposes_generation_provenance(
    pipeline_client,
):
    client, _, _ = pipeline_client
    created = client.post(
        "/v1/resumes",
        json={"kind": "base", "title": "手工简历"},
        headers={"Idempotency-Key": "draft-provenance-resume"},
    )
    assert created.status_code == 201
    saved = client.post(
        f"/v1/resumes/{created.json()['id']}/versions",
        json={
            "base_version": 0,
            "snapshot": {
                "schema_version": "1",
                "title": "手工简历",
                "target": None,
                "sections": [],
            },
            "claim_evidence": [],
        },
        headers={"Idempotency-Key": "draft-provenance-version"},
    )

    assert saved.status_code == 201
    assert saved.json()["generation_mode"] == "manual"
    assert saved.json()["workflow_version"] is None
    assert saved.json()["ai_run_id"] is None
    assert saved.json()["input_hash"] is None


def test_fact_policy_accepts_exact_sourced_claim_and_returns_range():
    text = "负责用户调研，覆盖 20 名学生"
    result = fact_policy_check(
        text,
        [DraftClaim(text=text, fact_refs=("fact_research",), claim_order=0)],
        [
            ConfirmedFactProjection(
                id="fact_research",
                value="负责用户调研，覆盖 20 名学生",
                status="confirmed",
                source_hashes=("source_hash",),
            )
        ],
    )

    assert [(claim.text, claim.start, claim.end) for claim in result.supported_claims] == [
        (text, 0, len(text))
    ]
    assert result.issues == ()


@pytest.mark.parametrize(
    ("claim", "fact"),
    [
        ("负责用户调研", "参与用户调研"),
        ("推动用户调研", "参与用户调研"),
        ("managed customer research", "supported customer research"),
    ],
)
def test_fact_policy_rejects_responsibility_strength_inflation(claim, fact):
    result = fact_policy_check(
        claim,
        [DraftClaim(text=claim, fact_refs=("fact_research",), claim_order=0)],
        [
            ConfirmedFactProjection(
                id="fact_research",
                value=fact,
                status="confirmed",
                source_hashes=("source_hash",),
            )
        ],
    )

    assert result.supported_claims == ()
    assert [issue.code for issue in result.issues] == [
        "CLAIM_RESPONSIBILITY_STRENGTH_UNSUPPORTED"
    ]


def test_fact_policy_allows_responsibility_strength_downgrade():
    result = fact_policy_check(
        "用户调研",
        [DraftClaim(text="用户调研", fact_refs=("fact_research",), claim_order=0)],
        [
            ConfirmedFactProjection(
                id="fact_research",
                value="负责用户调研",
                status="confirmed",
                source_hashes=("source_hash",),
            )
        ],
    )

    assert [claim.text for claim in result.supported_claims] == ["用户调研"]
    assert result.issues == ()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("参与用户调研", 1),
        ("推动用户调研", 2),
        ("负责用户调研", 2),
        ("supported customer research", 1),
        ("managed customer research", 2),
        ("用户调研", 0),
    ],
)
def test_responsibility_strength_has_explicit_chinese_and_english_levels(
    text,
    expected,
):
    assert resume_quality.responsibility_strength(text) == expected


@pytest.mark.parametrize(
    ("claim", "evidence", "expected"),
    [
        ("负责用户调研", "参与用户调研", False),
        ("managed customer research", "supported customer research", False),
        ("用户调研", "负责用户调研", True),
        ("负责用户调研", "负责市场分析", True),
    ],
)
def test_responsibility_helper_checks_strength_only_for_equivalent_subjects(
    claim,
    evidence,
    expected,
):
    assert resume_quality.responsibility_claim_supported(claim, [evidence]) is expected


@pytest.mark.parametrize(
    ("claim", "fact", "expected_code"),
    [
        (
            DraftClaim(text="负责用户调研", fact_refs=(), claim_order=0),
            ConfirmedFactProjection(
                id="fact_research",
                value="负责用户调研",
                status="confirmed",
                source_hashes=("source_hash",),
            ),
            "CLAIM_FACT_REFERENCE_REQUIRED",
        ),
        (
            DraftClaim(
                text="负责用户调研",
                fact_refs=("fact_research",),
                claim_order=0,
            ),
            ConfirmedFactProjection(
                id="fact_research",
                value="负责用户调研",
                status="unconfirmed",
                source_hashes=("source_hash",),
            ),
            "CLAIM_FACT_NOT_CONFIRMED",
        ),
        (
            DraftClaim(
                text="负责用户调研",
                fact_refs=("fact_research",),
                claim_order=0,
            ),
            ConfirmedFactProjection(
                id="fact_research",
                value="负责用户调研",
                status="confirmed",
                source_hashes=(),
            ),
            "CLAIM_FACT_SOURCE_REQUIRED",
        ),
        (
            DraftClaim(
                text="覆盖 30 名学生",
                fact_refs=("fact_research",),
                claim_order=0,
            ),
            ConfirmedFactProjection(
                id="fact_research",
                value="覆盖 20 名学生",
                status="confirmed",
                source_hashes=("source_hash",),
            ),
            "CLAIM_FACT_MISMATCH",
        ),
        (
            DraftClaim(
                text="获得国家级一等奖",
                fact_refs=("fact_research",),
                claim_order=0,
            ),
            ConfirmedFactProjection(
                id="fact_research",
                value="参加校内比赛",
                status="confirmed",
                source_hashes=("source_hash",),
            ),
            "CLAIM_FACT_MISMATCH",
        ),
    ],
)
def test_fact_policy_rejects_unsupported_claims(claim, fact, expected_code):
    result = fact_policy_check(claim.text, [claim], [fact])

    assert result.supported_claims == ()
    assert [issue.code for issue in result.issues] == [expected_code]


def test_fact_policy_uses_exact_non_overlapping_model_ranges():
    result = fact_policy_check(
        "负责调研；完成原型",
        [
            DraftClaim(
                text="完成原型",
                fact_refs=("fact_prototype",),
                claim_order=1,
            ),
            DraftClaim(
                text="负责调研",
                fact_refs=("fact_research",),
                claim_order=0,
            ),
        ],
        [
            ConfirmedFactProjection(
                id="fact_research",
                value="负责调研",
                status="confirmed",
                source_hashes=("research_hash",),
            ),
            ConfirmedFactProjection(
                id="fact_prototype",
                value="完成原型",
                status="confirmed",
                source_hashes=("prototype_hash",),
            ),
        ],
    )

    assert [claim.claim_order for claim in result.supported_claims] == [0, 1]
    assert [(claim.start, claim.end) for claim in result.supported_claims] == [
        (0, 4),
        (5, 9),
    ]


def _alembic_config(database_path: Path) -> Config:
    api_root = Path(__file__).resolve().parents[1]
    config = Config(api_root / "alembic.ini")
    config.set_main_option("script_location", str(api_root / "migrations"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")
    return config
