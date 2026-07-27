from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("resumes") as batch:
        batch.add_column(sa.Column("head_version", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("head_version_id", sa.String(64), nullable=True))
        batch.add_column(sa.Column("base_resume_owner_user_id", sa.String(64), nullable=True))
        batch.add_column(sa.Column("job_description_owner_user_id", sa.String(64), nullable=True))
    op.execute(
        """
        UPDATE resumes
        SET base_resume_owner_user_id = CASE
              WHEN base_resume_id IS NOT NULL THEN owner_user_id
              ELSE NULL
            END,
            job_description_owner_user_id = CASE
              WHEN job_description_id IS NOT NULL THEN owner_user_id
              ELSE NULL
            END,
            head_version = (
              SELECT COUNT(*) FROM resume_versions rv
              WHERE rv.resume_id = resumes.id
                AND rv.owner_user_id = resumes.owner_user_id
            ),
            head_version_id = (
              SELECT rv.id FROM resume_versions rv
              WHERE rv.resume_id = resumes.id
                AND rv.owner_user_id = resumes.owner_user_id
              ORDER BY rv.created_at DESC, rv.id DESC LIMIT 1
            )
        """
    )
    with op.batch_alter_table("resumes") as batch:
        batch.drop_constraint("fk_resume_base_owner", type_="foreignkey")
        batch.drop_constraint("fk_resume_job_owner", type_="foreignkey")
        batch.drop_constraint("ck_targeted_resume_has_base_and_job", type_="check")
        batch.create_foreign_key(
            "fk_resume_base_owner",
            "resumes",
            ["base_resume_id", "base_resume_owner_user_id"],
            ["id", "owner_user_id"],
        )
        batch.create_foreign_key(
            "fk_resume_job_owner",
            "job_descriptions",
            ["job_description_id", "job_description_owner_user_id"],
            ["id", "owner_user_id"],
        )
        batch.create_check_constraint(
            "ck_targeted_resume_has_base_and_job",
            "kind <> 'job_targeted' OR "
            "(base_resume_id IS NOT NULL AND base_resume_owner_user_id IS NOT NULL "
            "AND job_description_id IS NOT NULL AND job_description_owner_user_id IS NOT NULL)",
        )

    with op.batch_alter_table("resume_versions") as batch:
        batch.drop_constraint("uq_resume_snapshot_hash", type_="unique")

    with op.batch_alter_table("bullet_fact_links") as batch:
        batch.add_column(sa.Column("fact_owner_user_id", sa.String(64), nullable=True))
    op.execute(
        "UPDATE bullet_fact_links SET fact_owner_user_id = owner_user_id"
    )
    with op.batch_alter_table("bullet_fact_links") as batch:
        batch.drop_constraint("fk_bullet_fact_fact_owner", type_="foreignkey")
        batch.create_foreign_key(
            "fk_bullet_fact_fact_owner",
            "facts",
            ["fact_id", "fact_owner_user_id"],
            ["id", "owner_user_id"],
        )
        batch.alter_column(
            "fact_owner_user_id",
            existing_type=sa.String(64),
            nullable=False,
        )

    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        _install_sqlite_resume_triggers()
    elif dialect == "postgresql":
        _install_postgresql_resume_triggers()

    sqlite = dialect == "sqlite"
    if sqlite:
        for trigger in (
            "trg_confirmed_fact_requires_source_insert",
            "trg_confirmed_fact_requires_source_update",
            "trg_confirmed_fact_keeps_source",
            "trg_confirmed_fact_keeps_source_update",
        ):
            op.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    with op.batch_alter_table("facts") as batch:
        batch.create_check_constraint(
            "ck_fact_status",
            "status IN ('unconfirmed', 'confirmed', 'rejected')",
        )
    if sqlite:
        _install_sqlite_fact_triggers()


def downgrade() -> None:
    bind = op.get_bind()
    duplicate = bind.execute(
        sa.text(
            """
            SELECT 1 FROM resume_versions
            GROUP BY resume_id, snapshot_hash HAVING COUNT(*) > 1 LIMIT 1
            """
        )
    ).scalar()
    incompatible_reference = bind.execute(
        sa.text(
            """
            SELECT 1 FROM resumes
            WHERE (base_resume_id IS NOT NULL
                   AND base_resume_owner_user_id <> owner_user_id)
               OR (job_description_id IS NOT NULL
                   AND job_description_owner_user_id <> owner_user_id)
            UNION ALL
            SELECT 1 FROM bullet_fact_links
            WHERE fact_owner_user_id <> owner_user_id
            LIMIT 1
            """
        )
    ).scalar()
    if duplicate:
        raise RuntimeError("cannot downgrade 0002 while restored duplicate snapshots exist")
    if incompatible_reference:
        raise RuntimeError("cannot downgrade 0002 while alias-owned references exist")

    dialect = bind.dialect.name
    if dialect == "sqlite":
        for trigger in (
            "trg_resume_head_matches_version",
            "trg_resume_head_insert_matches_version",
            "trg_resume_references_same_canonical_owner_insert",
            "trg_resume_references_same_canonical_owner_update",
            "trg_bullet_fact_same_canonical_owner_insert",
            "trg_bullet_fact_same_canonical_owner_update",
            "trg_confirmed_fact_requires_source_insert",
            "trg_confirmed_fact_requires_source_update",
            "trg_confirmed_fact_keeps_source",
            "trg_confirmed_fact_keeps_source_update",
        ):
            op.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    elif dialect == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS trg_resume_head_integrity ON resumes")
        op.execute("DROP TRIGGER IF EXISTS trg_resume_reference_owner ON resumes")
        op.execute("DROP TRIGGER IF EXISTS trg_bullet_fact_reference_owner ON bullet_fact_links")
        op.execute("DROP FUNCTION IF EXISTS enforce_resume_head_integrity()")
        op.execute("DROP FUNCTION IF EXISTS enforce_canonical_reference_owner()")
        op.execute("DROP FUNCTION IF EXISTS canonical_owner(varchar)")

    with op.batch_alter_table("facts") as batch:
        batch.drop_constraint("ck_fact_status", type_="check")
    if dialect == "sqlite":
        _install_sqlite_fact_triggers()

    with op.batch_alter_table("bullet_fact_links") as batch:
        batch.drop_constraint("fk_bullet_fact_fact_owner", type_="foreignkey")
        batch.create_foreign_key(
            "fk_bullet_fact_fact_owner",
            "facts",
            ["fact_id", "owner_user_id"],
            ["id", "owner_user_id"],
        )
        batch.drop_column("fact_owner_user_id")

    with op.batch_alter_table("resume_versions") as batch:
        batch.create_unique_constraint(
            "uq_resume_snapshot_hash",
            ["resume_id", "snapshot_hash"],
        )
    if dialect == "sqlite":
        op.execute(
            """
            CREATE TRIGGER trg_resume_versions_no_update
            BEFORE UPDATE ON resume_versions
            BEGIN SELECT RAISE(ABORT, 'resume versions are append-only'); END
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_resume_versions_no_delete
            BEFORE DELETE ON resume_versions
            BEGIN SELECT RAISE(ABORT, 'resume versions are append-only'); END
            """
        )

    with op.batch_alter_table("resumes") as batch:
        batch.drop_constraint("fk_resume_base_owner", type_="foreignkey")
        batch.drop_constraint("fk_resume_job_owner", type_="foreignkey")
        batch.drop_constraint("ck_targeted_resume_has_base_and_job", type_="check")
        batch.create_foreign_key(
            "fk_resume_base_owner",
            "resumes",
            ["base_resume_id", "owner_user_id"],
            ["id", "owner_user_id"],
        )
        batch.create_foreign_key(
            "fk_resume_job_owner",
            "job_descriptions",
            ["job_description_id", "owner_user_id"],
            ["id", "owner_user_id"],
        )
        batch.create_check_constraint(
            "ck_targeted_resume_has_base_and_job",
            "kind <> 'job_targeted' OR "
            "(base_resume_id IS NOT NULL AND job_description_id IS NOT NULL)",
        )
        batch.drop_column("job_description_owner_user_id")
        batch.drop_column("base_resume_owner_user_id")
        batch.drop_column("head_version_id")
        batch.drop_column("head_version")


def _install_sqlite_resume_triggers() -> None:
    op.execute(
        """
        CREATE TRIGGER trg_resume_head_matches_version
        BEFORE UPDATE OF head_version, head_version_id ON resumes
        WHEN NEW.head_version_id IS NOT NULL AND NOT EXISTS (
          SELECT 1 FROM resume_versions
          WHERE id = NEW.head_version_id
            AND resume_id = NEW.id
            AND owner_user_id = NEW.owner_user_id
        ) BEGIN SELECT RAISE(ABORT, 'resume head must reference its own version'); END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_resume_head_insert_matches_version
        BEFORE INSERT ON resumes
        WHEN NEW.head_version_id IS NOT NULL AND NOT EXISTS (
          SELECT 1 FROM resume_versions
          WHERE id = NEW.head_version_id
            AND resume_id = NEW.id
            AND owner_user_id = NEW.owner_user_id
        ) BEGIN SELECT RAISE(ABORT, 'resume head must reference its own version'); END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_resume_versions_no_update
        BEFORE UPDATE ON resume_versions
        BEGIN SELECT RAISE(ABORT, 'resume versions are append-only'); END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_resume_versions_no_delete
        BEFORE DELETE ON resume_versions
        BEGIN SELECT RAISE(ABORT, 'resume versions are append-only'); END
        """
    )
    op.execute(_sqlite_resume_reference_trigger("insert", "BEFORE INSERT"))
    op.execute(
        _sqlite_resume_reference_trigger(
            "update",
            "BEFORE UPDATE OF owner_user_id, base_resume_id, "
            "base_resume_owner_user_id, job_description_id, "
            "job_description_owner_user_id",
        )
    )
    op.execute(_sqlite_bullet_reference_trigger("insert", "BEFORE INSERT"))
    op.execute(
        _sqlite_bullet_reference_trigger(
            "update",
            "BEFORE UPDATE OF owner_user_id, fact_id, fact_owner_user_id",
        )
    )


def _sqlite_resume_reference_trigger(suffix: str, event: str) -> str:
    return f"""
        CREATE TRIGGER trg_resume_references_same_canonical_owner_{suffix}
        {event} ON resumes
        WHEN NEW.kind = 'job_targeted' AND (
          NOT EXISTS (
            WITH RECURSIVE resource_owner(id) AS (
              SELECT NEW.owner_user_id
              UNION
              SELECT ua.canonical_user_id
              FROM user_aliases ua JOIN resource_owner ro ON ua.alias_user_id = ro.id
            ), reference_owner(id) AS (
              SELECT NEW.base_resume_owner_user_id
              UNION
              SELECT ua.canonical_user_id
              FROM user_aliases ua JOIN reference_owner ro ON ua.alias_user_id = ro.id
            )
            SELECT 1 FROM resource_owner JOIN reference_owner USING (id)
          )
          OR NOT EXISTS (
            WITH RECURSIVE resource_owner(id) AS (
              SELECT NEW.owner_user_id
              UNION
              SELECT ua.canonical_user_id
              FROM user_aliases ua JOIN resource_owner ro ON ua.alias_user_id = ro.id
            ), reference_owner(id) AS (
              SELECT NEW.job_description_owner_user_id
              UNION
              SELECT ua.canonical_user_id
              FROM user_aliases ua JOIN reference_owner ro ON ua.alias_user_id = ro.id
            )
            SELECT 1 FROM resource_owner JOIN reference_owner USING (id)
          )
        )
        BEGIN SELECT RAISE(ABORT, 'resume references must share a canonical owner'); END
    """


def _sqlite_bullet_reference_trigger(suffix: str, event: str) -> str:
    return f"""
        CREATE TRIGGER trg_bullet_fact_same_canonical_owner_{suffix}
        {event} ON bullet_fact_links
        WHEN NOT EXISTS (
          WITH RECURSIVE resource_owner(id) AS (
            SELECT NEW.owner_user_id
            UNION
            SELECT ua.canonical_user_id
            FROM user_aliases ua JOIN resource_owner ro ON ua.alias_user_id = ro.id
          ), reference_owner(id) AS (
            SELECT NEW.fact_owner_user_id
            UNION
            SELECT ua.canonical_user_id
            FROM user_aliases ua JOIN reference_owner ro ON ua.alias_user_id = ro.id
          )
          SELECT 1 FROM resource_owner JOIN reference_owner USING (id)
        )
        BEGIN SELECT RAISE(ABORT, 'bullet fact must share a canonical owner'); END
    """


def _install_postgresql_resume_triggers() -> None:
    op.execute(
        """
        CREATE FUNCTION enforce_resume_head_integrity()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.head_version_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM resume_versions rv
            WHERE rv.id = NEW.head_version_id
              AND rv.resume_id = NEW.id
              AND rv.owner_user_id = NEW.owner_user_id
          ) THEN
            RAISE EXCEPTION 'resume head must reference its own version'
              USING ERRCODE = '23503';
          END IF;
          RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER trg_resume_head_integrity
        AFTER INSERT OR UPDATE ON resumes
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION enforce_resume_head_integrity()
        """
    )
    op.execute(
        """
        CREATE FUNCTION canonical_owner(user_key varchar)
        RETURNS varchar LANGUAGE sql STABLE AS $$
          WITH RECURSIVE owner_chain(id) AS (
            SELECT user_key
            UNION
            SELECT ua.canonical_user_id
            FROM user_aliases ua JOIN owner_chain oc ON ua.alias_user_id = oc.id
          )
          SELECT oc.id FROM owner_chain oc
          WHERE NOT EXISTS (
            SELECT 1 FROM user_aliases ua WHERE ua.alias_user_id = oc.id
          )
          LIMIT 1
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION enforce_canonical_reference_owner()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_TABLE_NAME = 'resumes'
             AND NEW.kind = 'job_targeted'
             AND (
               canonical_owner(NEW.owner_user_id)
                 IS DISTINCT FROM canonical_owner(NEW.base_resume_owner_user_id)
               OR canonical_owner(NEW.owner_user_id)
                 IS DISTINCT FROM canonical_owner(NEW.job_description_owner_user_id)
             ) THEN
            RAISE EXCEPTION 'resume references must share a canonical owner'
              USING ERRCODE = '23503';
          END IF;
          IF TG_TABLE_NAME = 'bullet_fact_links'
             AND canonical_owner(NEW.owner_user_id)
                 IS DISTINCT FROM canonical_owner(NEW.fact_owner_user_id) THEN
            RAISE EXCEPTION 'bullet fact must share a canonical owner'
              USING ERRCODE = '23503';
          END IF;
          RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER trg_resume_reference_owner
        AFTER INSERT OR UPDATE ON resumes
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION enforce_canonical_reference_owner()
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER trg_bullet_fact_reference_owner
        AFTER INSERT OR UPDATE ON bullet_fact_links
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION enforce_canonical_reference_owner()
        """
    )


def _install_sqlite_fact_triggers() -> None:
    op.execute(
        """
        CREATE TRIGGER trg_confirmed_fact_requires_source_update
        BEFORE UPDATE OF status, confirmed_at ON facts
        WHEN NEW.status = 'confirmed' AND NOT EXISTS (
          SELECT 1 FROM fact_sources
          WHERE fact_id = NEW.id AND owner_user_id = NEW.owner_user_id
        ) BEGIN SELECT RAISE(ABORT, 'confirmed fact requires a source'); END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_confirmed_fact_keeps_source
        BEFORE DELETE ON fact_sources
        WHEN EXISTS (
          SELECT 1 FROM facts
          WHERE id = OLD.fact_id
            AND owner_user_id = OLD.owner_user_id
            AND status = 'confirmed'
        ) AND NOT EXISTS (
          SELECT 1 FROM fact_sources
          WHERE fact_id = OLD.fact_id
            AND owner_user_id = OLD.owner_user_id
            AND NOT (
              source_record_id = OLD.source_record_id
              AND source_hash = OLD.source_hash
            )
        )
        BEGIN SELECT RAISE(ABORT, 'confirmed fact requires a source'); END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_confirmed_fact_keeps_source_update
        BEFORE UPDATE OF fact_id, owner_user_id, source_record_id, source_hash
        ON fact_sources
        WHEN EXISTS (
          SELECT 1 FROM facts
          WHERE id = OLD.fact_id
            AND owner_user_id = OLD.owner_user_id
            AND status = 'confirmed'
        ) AND NOT EXISTS (
          SELECT 1 FROM fact_sources
          WHERE fact_id = OLD.fact_id
            AND owner_user_id = OLD.owner_user_id
            AND NOT (
              source_record_id = OLD.source_record_id
              AND source_hash = OLD.source_hash
            )
        )
        BEGIN SELECT RAISE(ABORT, 'confirmed fact requires a source'); END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_confirmed_fact_requires_source_insert
        BEFORE INSERT ON facts
        WHEN NEW.status = 'confirmed' AND NOT EXISTS (
          SELECT 1 FROM fact_sources
          WHERE fact_id = NEW.id AND owner_user_id = NEW.owner_user_id
        ) BEGIN SELECT RAISE(ABORT, 'confirmed fact requires a source'); END
        """
    )
