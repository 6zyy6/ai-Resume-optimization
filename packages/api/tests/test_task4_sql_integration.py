import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import (
    Base,
    BulletFactLink,
    Fact,
    FactSource,
    JobDescription,
    Resume,
    ResumeVersion,
    SourceRecord,
    User,
    UserAlias,
)
from app.modules.resumes.quality import check_exportable, claim_ranges
from app.modules.resumes.service import ResumeError, ResumeService


def _snapshot(title: str) -> dict:
    return {
        "schema_version": "1",
        "title": title,
        "target": None,
        "sections": [],
    }


@pytest.fixture
async def task4_sessions(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'task4-integration.db'}",
        connect_args={"timeout": 30},
    )

    @event.listens_for(engine.sync_engine, "connect")
    def enable_foreign_keys(dbapi_connection, _):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.mark.anyio
async def test_local_twenty_concurrent_version_admissions_have_one_winner(
    task4_sessions,
):
    """Ignoring the SQLite write lock can admit two writes from the same base."""
    async with task4_sessions.begin() as session:
        session.add(User(id="usr_owner"))
        await session.flush()
        session.add(
            Resume(
                id="resume_1",
                owner_user_id="usr_owner",
                kind="base",
                title="Base",
            )
        )
    service = ResumeService(task4_sessions)

    results = await asyncio.gather(
        *[
            service.save_resume_version(
                "usr_owner",
                "resume_1",
                0,
                _snapshot(f"candidate-{index}"),
                f"candidate-{index}",
            )
            for index in range(20)
        ],
        return_exceptions=True,
    )

    saved = [result for result in results if not isinstance(result, BaseException)]
    rejected = [result for result in results if isinstance(result, ResumeError)]
    assert len(saved) == 1
    assert len(rejected) == 19, results
    assert {error.code for error in rejected} == {"RESUME_VERSION_CONFLICT"}
    async with task4_sessions() as session:
        resume = await session.get(Resume, "resume_1")
        assert resume.head_version == 1
        assert resume.head_version_id == saved[0].row.id
        assert await session.scalar(
            select(func.count()).select_from(saved[0].row.__class__)
        ) == 1


@pytest.mark.anyio
async def test_concurrent_same_version_key_replays_and_changed_body_conflicts(
    task4_sessions,
):
    """Claiming after the resource write leaks duplicate-key or stale-head errors."""
    async with task4_sessions.begin() as session:
        session.add(User(id="usr_idem"))
        await session.flush()
        session.add(
            Resume(
                id="resume_idem",
                owner_user_id="usr_idem",
                kind="base",
                title="Base",
            )
        )
    service = ResumeService(task4_sessions)
    same = await asyncio.gather(
        *[
            service.save_resume_version(
                "usr_idem",
                "resume_idem",
                0,
                _snapshot("same"),
                "same-version-key",
            )
            for _ in range(10)
        ]
    )
    assert len({result.row.id for result in same}) == 1
    assert {result.status_code for result in same} == {201}

    async with task4_sessions.begin() as session:
        session.add(
            Resume(
                id="resume_changed",
                owner_user_id="usr_idem",
                kind="base",
                title="Changed",
            )
        )
    changed = await asyncio.gather(
        service.save_resume_version(
            "usr_idem",
            "resume_changed",
            0,
            _snapshot("one"),
            "changed-version-key",
        ),
        service.save_resume_version(
            "usr_idem",
            "resume_changed",
            0,
            _snapshot("two"),
            "changed-version-key",
        ),
        return_exceptions=True,
    )
    assert len([result for result in changed if not isinstance(result, BaseException)]) == 1
    conflicts = [result for result in changed if isinstance(result, ResumeError)]
    assert len(conflicts) == 1
    assert conflicts[0].code == "IDEMPOTENCY_KEY_REUSED"


@pytest.mark.anyio
async def test_sqlite_head_invariant_rejects_wrong_resume_on_insert_and_update(
    task4_sessions,
):
    """Dropping either trigger permits a head owned by a different resume."""
    async with task4_sessions.begin() as session:
        session.add(User(id="usr_head"))
        await session.flush()
        session.add_all(
            [
                Resume(
                    id="resume_head_a",
                    owner_user_id="usr_head",
                    kind="base",
                    title="A",
                ),
                Resume(
                    id="resume_head_b",
                    owner_user_id="usr_head",
                    kind="base",
                    title="B",
                ),
            ]
        )
        await session.flush()
        session.add(
            ResumeVersion(
                id="version_head_b",
                owner_user_id="usr_head",
                resume_id="resume_head_b",
                snapshot_json=_snapshot("B"),
                snapshot_hash="hash-b",
                created_by="usr_head",
            )
        )

    async with task4_sessions() as session:
        resume = await session.get(Resume, "resume_head_a")
        resume.head_version = 1
        resume.head_version_id = "version_head_b"
        with pytest.raises(IntegrityError):
            await session.commit()

    async with task4_sessions() as session:
        session.add(
            Resume(
                id="resume_head_insert",
                owner_user_id="usr_head",
                kind="base",
                title="Insert",
                head_version=1,
                head_version_id="version_head_b",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


def test_number_tokens_keep_decimal_and_thousands_separators():
    """Splitting decimal/thousands punctuation corrupts claim-to-fact ranges."""
    text = "Increased conversion 12.5%; processed 1,000 orders"
    assert claim_ranges(text) == [(0, 26), (28, 50)]


def test_number_bearing_claim_still_requires_textual_evidence():
    """Matching only the number lets unrelated evidence support a claim."""
    facts = [
        SimpleNamespace(
            id="fact_1",
            status="confirmed",
            value_encrypted="Reduced churn by 12.5%",
        )
    ]
    snapshot = {
        "sections": [
            {
                "items": [
                    {
                        "id": "bullet_1",
                        "text": "Increased conversion by 12.5%",
                        "fact_refs": ["fact_1"],
                    }
                ]
            }
        ]
    }

    assert [issue.code for issue in check_exportable(snapshot, facts)] == [
        "BULLET_CLAIM_NOT_COVERED"
    ]


@pytest.mark.parametrize(
    ("claim", "evidence"),
    [
        ("Increased revenue with 20%", "Resolved tickets with 20%"),
        ("Increased revenue and retention", "Resolved tickets and incidents"),
        ("Increased the conversion rate", "Reduced the response time"),
        ("Improved revenue by 20%", "Improved response time by 20%"),
        ("通过增长策略提升转化率20%", "通过客服流程解决工单20%"),
        ("提升营收20%", "提升工单处理量20%"),
    ],
)
def test_common_words_do_not_count_as_textual_evidence(claim, evidence):
    facts = [
        SimpleNamespace(
            id="fact_common",
            status="confirmed",
            value_encrypted=evidence,
        )
    ]
    snapshot = {
        "sections": [
            {
                "items": [
                    {
                        "id": "bullet_common",
                        "text": claim,
                        "fact_refs": ["fact_common"],
                    }
                ]
            }
        ]
    }

    assert [issue.code for issue in check_exportable(snapshot, facts)] == [
        "BULLET_CLAIM_NOT_COVERED"
    ]


@pytest.mark.parametrize(
    ("claim", "evidence"),
    [
        ("Increased conversion by 12.5%", "Conversion increased by 12.5%"),
        ("Processed 1,000 orders", "Handled 1000 customer orders"),
        ("将转化率提升20%", "转化率同比提升20%"),
    ],
)
def test_meaningful_english_and_chinese_terms_cover_valid_claims(claim, evidence):
    facts = [
        SimpleNamespace(
            id="fact_meaningful",
            status="confirmed",
            value_encrypted=evidence,
        )
    ]
    snapshot = {
        "sections": [
            {
                "items": [
                    {
                        "id": "bullet_meaningful",
                        "text": claim,
                        "fact_refs": ["fact_meaningful"],
                    }
                ]
            }
        ]
    }

    assert check_exportable(snapshot, facts) == []


def test_claim_fact_mapping_requires_exact_cardinality():
    """Accepting surplus refs maps them onto the final claim range."""
    facts = [
        SimpleNamespace(
            id=f"fact_{index}",
            status="confirmed",
            value_encrypted="Increased conversion",
        )
        for index in (1, 2)
    ]
    snapshot = {
        "sections": [
            {
                "items": [
                    {
                        "id": "bullet_1",
                        "text": "Increased conversion",
                        "fact_refs": ["fact_1", "fact_2"],
                    }
                ]
            }
        ]
    }

    assert [issue.code for issue in check_exportable(snapshot, facts)] == [
        "BULLET_FACT_CARDINALITY_MISMATCH"
    ]


@pytest.mark.parametrize(
    ("text", "refs", "expected"),
    [
        (
            "Increased conversion by 13.5%",
            ["fact_metric"],
            ["BULLET_NEW_NUMBER"],
        ),
        (
            "Reduced churn by 12.5%",
            ["fact_metric"],
            ["BULLET_CLAIM_NOT_COVERED"],
        ),
        (
            "Increased conversion by 12.5%",
            ["fact_missing"],
            ["BULLET_FACT_NOT_CONFIRMED"],
        ),
        (
            "Increased conversion by 12.5%",
            [],
            ["BULLET_FACT_REFERENCE_REQUIRED"],
        ),
        (
            "Increased conversion; processed orders",
            ["fact_metric", "fact_metric"],
            ["BULLET_FACT_CARDINALITY_MISMATCH"],
        ),
        (
            "Increased conversion by 12.5%",
            ["fact_metric"],
            [],
        ),
    ],
)
def test_quality_negative_matrix(text, refs, expected):
    facts = [
        SimpleNamespace(
            id="fact_metric",
            status="confirmed",
            value_encrypted="Increased conversion by 12.5% and processed orders",
        )
    ]
    snapshot = {
        "sections": [
            {
                "items": [
                    {
                        "id": "bullet_1",
                        "text": text,
                        "fact_refs": refs,
                    }
                ]
            }
        ]
    }
    assert [issue.code for issue in check_exportable(snapshot, facts)] == expected


@pytest.mark.anyio
async def test_claim_links_persist_exact_rows_and_ranges(task4_sessions):
    """Collapsing surplus refs onto one range loses the claim relationship."""
    async with task4_sessions.begin() as session:
        session.add(User(id="usr_claims"))
        await session.flush()
        for index, evidence in (
            (1, "Increased conversion by 12.5%"),
            (2, "Processed 1,000 orders"),
        ):
            source = SourceRecord(
                id=f"source_claim_{index}",
                owner_user_id="usr_claims",
                source_type="user_confirmation",
                content_encrypted=evidence,
            )
            fact = Fact(
                id=f"fact_claim_{index}",
                owner_user_id="usr_claims",
                kind="metric",
                value_encrypted=evidence,
                status="unconfirmed",
            )
            session.add_all([source, fact])
            await session.flush()
            session.add(
                FactSource(
                    fact_id=fact.id,
                    source_record_id=source.id,
                    owner_user_id="usr_claims",
                    source_hash=f"hash-{index}",
                )
            )
            await session.flush()
            fact.status = "confirmed"
            fact.confirmed_at = datetime.now(timezone.utc)

    service = ResumeService(task4_sessions)
    resume = await service.create_resume(
        "usr_claims",
        {
            "kind": "base",
            "title": "Claims",
            "base_resume_id": None,
            "job_description_id": None,
        },
        "claims-resume",
    )
    saved = await service.save_resume_version(
        "usr_claims",
        resume.id,
        0,
        {
            "schema_version": "1",
            "title": "Claims",
            "target": None,
            "sections": [
                {
                    "id": "section_claims",
                    "type": "experience",
                    "title": "Experience",
                    "items": [
                        {
                            "id": "bullet_claims",
                            "text": "Increased conversion by 12.5%; processed 1,000 orders",
                            "fact_refs": ["fact_claim_1", "fact_claim_2"],
                        }
                    ],
                }
            ],
        },
        "claims-save",
    )
    async with task4_sessions() as session:
        links = list(
            (
                await session.scalars(
                    select(BulletFactLink)
                    .where(BulletFactLink.resume_version_id == saved.row.id)
                    .order_by(BulletFactLink.fact_id)
                )
            ).all()
        )
    assert [link.fact_id for link in links] == ["fact_claim_1", "fact_claim_2"]
    assert [link.claim_range for link in links] == [
        {"start": 0, "end": 29},
        {"start": 31, "end": 53},
    ]


@pytest.mark.anyio
async def test_chained_aliases_support_mixed_target_references_and_fact_links(
    task4_sessions,
):
    """Using one owner column for authorization and physical FKs breaks merged data."""
    async with task4_sessions.begin() as session:
        session.add_all(
            [User(id=user_id) for user_id in ("usr_root", "usr_old_a", "usr_old_b")]
        )
        await session.flush()
        session.add_all(
            [
                UserAlias(alias_user_id="usr_old_a", canonical_user_id="usr_root"),
                UserAlias(alias_user_id="usr_old_b", canonical_user_id="usr_old_a"),
            ]
        )
        session.add(
            Resume(
                id="resume_base",
                owner_user_id="usr_old_a",
                kind="base",
                title="Historical base",
            )
        )
        session.add(
            JobDescription(
                id="job_historical",
                owner_user_id="usr_old_b",
                title="Role",
                raw_encrypted="JD",
                status="ready",
            )
        )
        source = SourceRecord(
            id="source_historical",
            owner_user_id="usr_old_b",
            source_type="user_confirmation",
            content_encrypted="Increased conversion by 12.5%",
        )
        fact = Fact(
            id="fact_historical",
            owner_user_id="usr_old_b",
            kind="metric",
            value_encrypted="Increased conversion by 12.5%",
            status="unconfirmed",
        )
        session.add_all([source, fact])
        await session.flush()
        session.add(
            FactSource(
                fact_id=fact.id,
                source_record_id=source.id,
                owner_user_id=fact.owner_user_id,
                source_hash="source-hash",
            )
        )
        await session.flush()
        fact.status = "confirmed"
        fact.confirmed_at = datetime.now(timezone.utc)

    service = ResumeService(task4_sessions)
    targeted = await service.create_resume(
        "usr_root",
        {
            "kind": "job_targeted",
            "title": "Targeted",
            "base_resume_id": "resume_base",
            "job_description_id": "job_historical",
        },
        "target-create",
    )
    saved = await service.save_resume_version(
        "usr_old_b",
        targeted.id,
        0,
        {
            "schema_version": "1",
            "title": "Targeted",
            "target": None,
            "sections": [
                {
                    "id": "section_1",
                    "type": "experience",
                    "title": "Experience",
                    "items": [
                        {
                            "id": "bullet_1",
                            "text": "Increased conversion by 12.5%",
                            "fact_refs": ["fact_historical"],
                        }
                    ],
                }
            ],
        },
        "target-save",
    )

    async with task4_sessions() as session:
        persisted = await session.get(Resume, targeted.id)
        link = await session.scalar(
            select(BulletFactLink).where(
                BulletFactLink.resume_version_id == saved.row.id
            )
        )
        assert persisted.owner_user_id == "usr_root"
        assert persisted.base_resume_owner_user_id == "usr_old_a"
        assert persisted.job_description_owner_user_id == "usr_old_b"
        assert link.owner_user_id == "usr_root"
        assert link.fact_owner_user_id == "usr_old_b"
        assert link.claim_range == {"start": 0, "end": 29}


@pytest.mark.anyio
async def test_alias_reference_trigger_rejects_an_unrelated_physical_owner(
    task4_sessions,
):
    """Removing the canonical-group trigger would permit arbitrary cross-user links."""
    async with task4_sessions.begin() as session:
        session.add_all([User(id="usr_a"), User(id="usr_b")])
        await session.flush()
        session.add(
            Resume(
                id="resume_a",
                owner_user_id="usr_a",
                kind="base",
                title="A",
            )
        )
        session.add(
            JobDescription(
                id="job_b",
                owner_user_id="usr_b",
                title="B",
                raw_encrypted="B",
                status="ready",
            )
        )
        await session.flush()
        session.add(
            Resume(
                id="target_bad",
                owner_user_id="usr_a",
                kind="job_targeted",
                title="Bad",
                base_resume_id="resume_a",
                base_resume_owner_user_id="usr_a",
                job_description_id="job_b",
                job_description_owner_user_id="usr_b",
            )
        )
        with pytest.raises(IntegrityError):
            await session.flush()


@pytest.mark.anyio
async def test_base_resume_cannot_bypass_reference_owner_with_null(task4_sessions):
    """MATCH SIMPLE accepts an arbitrary id when its physical-owner column is NULL."""
    async with task4_sessions.begin() as session:
        session.add(User(id="usr_nullable"))

    async with task4_sessions.begin() as session:
        session.add(
            Resume(
                id="resume_nullable_bypass",
                owner_user_id="usr_nullable",
                kind="base",
                title="Invalid base",
                base_resume_id="arbitrary_missing_resume",
                base_resume_owner_user_id=None,
            )
        )
        with pytest.raises(IntegrityError):
            await session.flush()


@pytest.mark.anyio
async def test_every_non_null_resume_reference_enforces_canonical_owner(
    task4_sessions,
):
    """Canonical-owner enforcement must not depend on the public kind enum."""
    async with task4_sessions.begin() as session:
        session.add_all([User(id="usr_reference_a"), User(id="usr_reference_b")])
        await session.flush()
        session.add(
            Resume(
                id="resume_reference_b",
                owner_user_id="usr_reference_b",
                kind="base",
                title="B",
            )
        )
        await session.flush()
        session.add(
            Resume(
                id="resume_reference_bypass",
                owner_user_id="usr_reference_a",
                kind="legacy",
                title="Invalid legacy",
                base_resume_id="resume_reference_b",
                base_resume_owner_user_id="usr_reference_b",
            )
        )
        with pytest.raises(IntegrityError):
            await session.flush()


@pytest.mark.anyio
async def test_bullet_fact_trigger_rejects_an_unrelated_physical_owner(
    task4_sessions,
):
    """Physical fact FKs alone do not prevent unrelated canonical groups."""
    async with task4_sessions.begin() as session:
        session.add_all([User(id="usr_link_a"), User(id="usr_link_b")])
        await session.flush()
        session.add(
            Resume(
                id="resume_link_a",
                owner_user_id="usr_link_a",
                kind="base",
                title="A",
            )
        )
        session.add(
            Fact(
                id="fact_link_b",
                owner_user_id="usr_link_b",
                kind="metric",
                value_encrypted="B",
                status="unconfirmed",
            )
        )
        await session.flush()
        session.add(
            ResumeVersion(
                id="version_link_a",
                owner_user_id="usr_link_a",
                resume_id="resume_link_a",
                snapshot_json=_snapshot("A"),
                snapshot_hash="hash-a",
                created_by="usr_link_a",
            )
        )
        await session.flush()
        session.add(
            BulletFactLink(
                resume_version_id="version_link_a",
                bullet_id="bullet_bad",
                fact_id="fact_link_b",
                fact_owner_user_id="usr_link_b",
                owner_user_id="usr_link_a",
                claim_range={"start": 0, "end": 1},
            )
        )
        with pytest.raises(IntegrityError):
            await session.flush()
