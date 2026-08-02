from __future__ import annotations

import hashlib
import json

import sqlalchemy as sa
from alembic import op


revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def _input_hash(title: str, raw: str) -> str:
    snapshot = {
        "workflow_type": "parse_jd",
        "workflow_version": "2",
        "prompt_template_version": "jd-parse@2",
        "locale": "zh-CN",
        "input_version": 1,
        "payload": {
            "jd_text": raw,
            "job_title": title,
            "allowed_categories": [
                "responsibility",
                "must_have",
                "nice_to_have",
                "implicit_capability",
            ],
        },
    }
    encoded = json.dumps(
        snapshot,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _backfill_legacy_requirements() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT r.id, r.owner_user_id, r.job_id, r.text_encrypted, "
            "j.title, j.raw_encrypted "
            "FROM jd_requirements r JOIN job_descriptions j "
            "ON j.id = r.job_id AND j.owner_user_id = r.owner_user_id "
            "ORDER BY r.owner_user_id, r.job_id, r.priority, r.id"
        )
    ).mappings()
    consumed: dict[tuple[str, str], list[tuple[int, int]]] = {}
    for row in rows:
        raw = row["raw_encrypted"]
        value = row["text_encrypted"]
        if not isinstance(raw, str) or not raw:
            raise RuntimeError(
                "cannot backfill JD requirement provenance without source text"
            )
        key = (row["owner_user_id"], row["job_id"])
        start = -1
        search_from = 0
        while True:
            candidate = raw.find(value, search_from)
            if candidate < 0:
                break
            candidate_end = candidate + len(value)
            if all(
                candidate_end <= used_start or candidate >= used_end
                for used_start, used_end in consumed.get(key, ())
            ):
                start = candidate
                break
            search_from = candidate + 1
        if start < 0:
            start = 0
            end = len(raw)
            explicitness = "implicit"
            confidence_band = "low"
        else:
            end = start + len(value)
            consumed.setdefault(key, []).append((start, end))
            explicitness = "explicit"
            confidence_band = "high"
        source = raw[start:end]
        bind.execute(
            sa.text(
                "UPDATE jd_requirements SET source_start = :source_start, "
                "source_end = :source_end, source_hash = :source_hash, "
                "explicitness = :explicitness, confidence_band = :confidence_band, "
                "generation_mode = 'rule_fallback', "
                "workflow_version = 'legacy-rule-fallback@1', ai_run_id = NULL, "
                "input_hash = :input_hash WHERE id = :id AND owner_user_id = :owner"
            ),
            {
                "source_start": start,
                "source_end": end,
                "source_hash": hashlib.sha256(source.encode()).hexdigest(),
                "explicitness": explicitness,
                "confidence_band": confidence_band,
                "input_hash": _input_hash(row["title"], raw),
                "id": row["id"],
                "owner": row["owner_user_id"],
            },
        )


def upgrade() -> None:
    with op.batch_alter_table("jd_requirements") as batch:
        batch.add_column(sa.Column("source_start", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("source_end", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("source_hash", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("explicitness", sa.String(length=16), nullable=True))
        batch.add_column(sa.Column("confidence_band", sa.String(length=16), nullable=True))
        batch.add_column(sa.Column("generation_mode", sa.String(length=32), nullable=True))
        batch.add_column(sa.Column("workflow_version", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("ai_run_id", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("input_hash", sa.String(length=64), nullable=True))

    _backfill_legacy_requirements()

    with op.batch_alter_table("jd_requirements") as batch:
        for name, column_type in (
            ("source_start", sa.Integer()),
            ("source_end", sa.Integer()),
            ("source_hash", sa.String(length=64)),
            ("explicitness", sa.String(length=16)),
            ("confidence_band", sa.String(length=16)),
            ("generation_mode", sa.String(length=32)),
            ("workflow_version", sa.String(length=64)),
            ("input_hash", sa.String(length=64)),
        ):
            batch.alter_column(name, existing_type=column_type, nullable=False)
        batch.create_check_constraint(
            "ck_jd_requirement_source_range",
            "source_start >= 0 AND source_end > source_start",
        )
        batch.create_check_constraint(
            "ck_jd_requirement_explicitness",
            "explicitness IN ('explicit', 'implicit')",
        )
        batch.create_check_constraint(
            "ck_jd_requirement_confidence_band",
            "confidence_band IN ('high', 'medium', 'low')",
        )
        batch.create_check_constraint(
            "ck_jd_requirement_generation_mode",
            "generation_mode IN ('model', 'rule_fallback')",
        )
        batch.create_check_constraint(
            "ck_jd_requirement_generation_provenance",
            "(generation_mode = 'model' AND ai_run_id IS NOT NULL) OR "
            "(generation_mode = 'rule_fallback' AND ai_run_id IS NULL)",
        )
        batch.create_foreign_key(
            "fk_jd_requirement_ai_run_owner",
            "ai_runs",
            ["ai_run_id", "owner_user_id"],
            ["id", "owner_user_id"],
        )
        batch.create_index("ix_jd_requirements_ai_run_id", ["ai_run_id"])


def downgrade() -> None:
    generated = op.get_bind().execute(
        sa.text(
            "SELECT id FROM jd_requirements "
            "WHERE workflow_version != 'legacy-rule-fallback@1' "
            "OR generation_mode != 'rule_fallback' OR ai_run_id IS NOT NULL LIMIT 1"
        )
    ).first()
    if generated is not None:
        raise RuntimeError(
            "cannot downgrade JD provenance while newly parsed requirements exist"
        )

    with op.batch_alter_table("jd_requirements") as batch:
        batch.drop_index("ix_jd_requirements_ai_run_id")
        batch.drop_constraint("fk_jd_requirement_ai_run_owner", type_="foreignkey")
        batch.drop_constraint(
            "ck_jd_requirement_generation_provenance", type_="check"
        )
        batch.drop_constraint("ck_jd_requirement_generation_mode", type_="check")
        batch.drop_constraint("ck_jd_requirement_confidence_band", type_="check")
        batch.drop_constraint("ck_jd_requirement_explicitness", type_="check")
        batch.drop_constraint("ck_jd_requirement_source_range", type_="check")
        batch.drop_column("input_hash")
        batch.drop_column("ai_run_id")
        batch.drop_column("workflow_version")
        batch.drop_column("generation_mode")
        batch.drop_column("confidence_band")
        batch.drop_column("explicitness")
        batch.drop_column("source_hash")
        batch.drop_column("source_end")
        batch.drop_column("source_start")
