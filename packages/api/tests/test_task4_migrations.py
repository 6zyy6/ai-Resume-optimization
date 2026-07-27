import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import ResumeVersion
from app.modules.resumes.service import ResumeService


def _config(database_path: Path, monkeypatch) -> Config:
    monkeypatch.setenv(
        "ALEMBIC_DATABASE_URL", f"sqlite+aiosqlite:///{database_path}"
    )
    api_root = Path(__file__).resolve().parents[1]
    config = Config(api_root / "alembic.ini")
    config.set_main_option("script_location", str(api_root / "migrations"))
    return config


def test_seeded_0001_upgrade_backfills_head_and_keeps_next_parent(
    tmp_path, monkeypatch
):
    """Counting no existing history would make the next save start from version zero."""
    database_path = tmp_path / "seeded-upgrade.db"
    config = _config(database_path, monkeypatch)
    command.upgrade(config, "0001")
    engine = create_engine(f"sqlite:///{database_path}")
    now = datetime.now(timezone.utc).isoformat()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users "
                "(id, status, locale, created_at) "
                "VALUES ('usr_1', 'active', 'zh-CN', :now)"
            ),
            {"now": now},
        )
        connection.execute(
            text(
                "INSERT INTO resumes "
                "(id, owner_user_id, kind, title, created_at) "
                "VALUES ('resume_1', 'usr_1', 'base', 'Base', :now)"
            ),
            {"now": now},
        )
        connection.execute(
            text(
                "INSERT INTO job_descriptions "
                "(id, owner_user_id, title, raw_encrypted, status, created_at) "
                "VALUES ('job_1', 'usr_1', 'Role', 'JD', 'ready', :now)"
            ),
            {"now": now},
        )
        connection.execute(
            text(
                "INSERT INTO facts "
                "(id, owner_user_id, kind, value_encrypted, status, created_at) "
                "VALUES ('fact_1', 'usr_1', 'metric', '42', 'unconfirmed', :now)"
            ),
            {"now": now},
        )
        connection.execute(
            text(
                "INSERT INTO resumes "
                "(id, owner_user_id, kind, title, base_resume_id, "
                " job_description_id, created_at) "
                "VALUES ('resume_targeted', 'usr_1', 'job_targeted', "
                " 'Targeted', 'resume_1', 'job_1', :now)"
            ),
            {"now": now},
        )
        for index in (1, 2):
            connection.execute(
                text(
                    "INSERT INTO resume_versions "
                    "(id, owner_user_id, resume_id, parent_version_id, "
                    " snapshot_json, snapshot_hash, created_by, created_at) "
                    "VALUES (:id, 'usr_1', 'resume_1', :parent, :snapshot, "
                    " :hash, 'usr_1', :created_at)"
                ),
                {
                    "id": f"version_{index}",
                    "parent": None if index == 1 else "version_1",
                    "snapshot": f'{{"title":"{index}"}}',
                    "hash": f"hash_{index}",
                    "created_at": f"2026-07-28T00:00:0{index}+00:00",
                },
            )
        connection.execute(
            text(
                "INSERT INTO resume_versions "
                "(id, owner_user_id, resume_id, parent_version_id, "
                " snapshot_json, snapshot_hash, created_by, created_at) "
                "VALUES ('version_targeted', 'usr_1', 'resume_targeted', NULL, "
                " '{}', 'hash_targeted', 'usr_1', :now)"
            ),
            {"now": now},
        )
        connection.execute(
            text(
                "INSERT INTO bullet_fact_links "
                    "(resume_version_id, bullet_id, fact_id, claim_range, "
                    " owner_user_id) "
                    "VALUES ('version_targeted', 'bullet_1', 'fact_1', "
                    " :claim_range, 'usr_1')"
                ),
                {"claim_range": '{"start":0,"end":2}'},
            )
    engine.dispose()

    command.upgrade(config, "0002")
    engine = create_engine(f"sqlite:///{database_path}")
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT head_version, head_version_id FROM resumes "
                "WHERE id = 'resume_1'"
            )
        ).one()
        targeted = connection.execute(
            text(
                "SELECT base_resume_owner_user_id, "
                "job_description_owner_user_id "
                "FROM resumes WHERE id = 'resume_targeted'"
            )
        ).one()
        fact_owner = connection.execute(
            text(
                "SELECT fact_owner_user_id FROM bullet_fact_links "
                "WHERE resume_version_id = 'version_targeted'"
            )
        ).scalar_one()
        resume_checks = {
            check["name"]
            for check in inspect(connection).get_check_constraints("resumes")
        }
    engine.dispose()

    assert row == (2, "version_2")
    assert targeted == ("usr_1", "usr_1")
    assert fact_owner == "usr_1"
    assert {
        "ck_base_resume_has_no_references",
        "ck_resume_base_reference_paired",
        "ck_resume_job_reference_paired",
    } <= resume_checks

    async def save_next():
        async_engine = create_async_engine(
            f"sqlite+aiosqlite:///{database_path}"
        )
        sessions = async_sessionmaker(async_engine, expire_on_commit=False)
        try:
            saved = await ResumeService(sessions).save_resume_version(
                "usr_1",
                "resume_1",
                2,
                {
                    "schema_version": "1",
                    "title": "next",
                    "target": None,
                    "sections": [],
                },
                "next-save",
            )
            async with sessions() as session:
                persisted = await session.scalar(
                    select(ResumeVersion).where(
                        ResumeVersion.id == saved.row.id
                    )
                )
                return persisted.parent_version_id
        finally:
            await async_engine.dispose()

    assert asyncio.run(save_next()) == "version_2"


def test_duplicate_restore_downgrade_refuses_before_schema_mutation(
    tmp_path, monkeypatch
):
    """Running any downgrade DDL before duplicate preflight leaves a partial schema."""
    database_path = tmp_path / "duplicate-downgrade.db"
    config = _config(database_path, monkeypatch)
    command.upgrade(config, "0002")
    engine = create_engine(f"sqlite:///{database_path}")
    now = datetime.now(timezone.utc).isoformat()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users "
                "(id, status, locale, created_at) "
                "VALUES ('usr_1', 'active', 'zh-CN', :now)"
            ),
            {"now": now},
        )
        connection.execute(
            text(
                "INSERT INTO resumes "
                "(id, owner_user_id, kind, title, head_version, created_at) "
                "VALUES ('resume_1', 'usr_1', 'base', 'Base', 0, :now)"
            ),
            {"now": now},
        )
        for index in (1, 2):
            connection.execute(
                text(
                    "INSERT INTO resume_versions "
                    "(id, owner_user_id, resume_id, parent_version_id, "
                    " snapshot_json, snapshot_hash, created_by, created_at) "
                    "VALUES (:id, 'usr_1', 'resume_1', :parent, '{}', "
                    " 'same-hash', 'usr_1', :created_at)"
                ),
                {
                    "id": f"version_{index}",
                    "parent": None if index == 1 else "version_1",
                    "created_at": f"2026-07-28T00:00:0{index}+00:00",
                },
            )
    before_columns = [column["name"] for column in inspect(engine).get_columns("resumes")]
    before_checks = inspect(engine).get_check_constraints("facts")
    engine.dispose()

    with pytest.raises(RuntimeError):
        command.downgrade(config, "0001")

    engine = create_engine(f"sqlite:///{database_path}")
    try:
        assert [column["name"] for column in inspect(engine).get_columns("resumes")] == before_columns
        assert inspect(engine).get_check_constraints("facts") == before_checks
        with engine.connect() as connection:
            version = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
        assert version == "0002"
    finally:
        engine.dispose()


def test_nullable_reference_downgrade_refuses_before_schema_mutation(
    tmp_path, monkeypatch
):
    """SQL NULL must not make an incompatible physical-owner pair evade preflight."""
    database_path = tmp_path / "nullable-reference-downgrade.db"
    config = _config(database_path, monkeypatch)
    command.upgrade(config, "0002")
    engine = create_engine(f"sqlite:///{database_path}")
    now = datetime.now(timezone.utc).isoformat()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users "
                "(id, status, locale, created_at) "
                "VALUES ('usr_1', 'active', 'zh-CN', :now)"
            ),
            {"now": now},
        )
        connection.execute(text("PRAGMA ignore_check_constraints=ON"))
        connection.execute(
            text(
                "INSERT INTO resumes "
                "(id, owner_user_id, kind, title, base_resume_id, "
                "base_resume_owner_user_id, head_version, created_at) "
                "VALUES ('resume_bad', 'usr_1', 'base', 'Bad', "
                "'missing_resume', NULL, 0, :now)"
            ),
            {"now": now},
        )
        connection.execute(text("PRAGMA ignore_check_constraints=OFF"))
    before_columns = [
        column["name"] for column in inspect(engine).get_columns("resumes")
    ]
    engine.dispose()

    with pytest.raises(RuntimeError, match="alias-owned references"):
        command.downgrade(config, "0001")

    engine = create_engine(f"sqlite:///{database_path}")
    try:
        assert [
            column["name"] for column in inspect(engine).get_columns("resumes")
        ] == before_columns
        with engine.connect() as connection:
            assert (
                connection.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar_one()
                == "0002"
            )
    finally:
        engine.dispose()
