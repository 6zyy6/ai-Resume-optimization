import argparse
import asyncio
import json

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import AiRun, AiTraceEvent, Outbox, Task, TaskEvent


async def inspect(database_url: str, owner_user_id: str, task_id: str) -> dict[str, object]:
    engine = create_async_engine(database_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions() as session:
            task = await session.scalar(
                select(Task).where(
                    Task.id == task_id,
                    Task.owner_user_id == owner_user_id,
                )
            )
            runs = list(
                (
                    await session.scalars(
                        select(AiRun)
                        .where(
                            AiRun.task_id == task_id,
                            AiRun.owner_user_id == owner_user_id,
                        )
                        .order_by(AiRun.started_at, AiRun.id)
                    )
                ).all()
            )
            run_ids = [run.id for run in runs]
            traces = (
                list(
                    (
                        await session.scalars(
                            select(AiTraceEvent)
                            .where(
                                AiTraceEvent.owner_user_id == owner_user_id,
                                AiTraceEvent.ai_run_id.in_(run_ids),
                            )
                            .order_by(
                                AiTraceEvent.ai_run_id,
                                AiTraceEvent.event_seq,
                            )
                        )
                    ).all()
                )
                if run_ids
                else []
            )
            task_events = list(
                (
                    await session.scalars(
                        select(TaskEvent)
                        .where(
                            TaskEvent.task_id == task_id,
                            TaskEvent.owner_user_id == owner_user_id,
                        )
                        .order_by(TaskEvent.seq)
                    )
                ).all()
            )
            outbox = await session.scalar(
                select(Outbox).where(
                    Outbox.task_id == task_id,
                    Outbox.owner_user_id == owner_user_id,
                )
            )
            orphan_trace_count = await session.scalar(
                select(func.count())
                .select_from(AiTraceEvent)
                .outerjoin(
                    AiRun,
                    and_(
                        AiRun.id == AiTraceEvent.ai_run_id,
                        AiRun.owner_user_id == AiTraceEvent.owner_user_id,
                    ),
                )
                .where(AiRun.id.is_(None))
            )
            trace_sequences = {
                run_id: [
                    event.event_seq
                    for event in traces
                    if event.ai_run_id == run_id
                ]
                for run_id in run_ids
            }
            trace_types = {
                run_id: [
                    event.event_type
                    for event in traces
                    if event.ai_run_id == run_id
                ]
                for run_id in run_ids
            }
            return {
                "task_status": task.status if task else None,
                "task_trace_id": task.trace_id if task else None,
                "runs": [
                    {
                        "id": run.id,
                        "owner_user_id": run.owner_user_id,
                        "task_id": run.task_id,
                        "trace_id": run.trace_id,
                        "workflow_type": run.workflow_type,
                        "workflow_version": run.workflow_version,
                        "prompt_template_version": run.prompt_template_version,
                        "status": run.status,
                        "input_hash": run.input_hash,
                        "receipt_hash": run.receipt_hash,
                        "result_ref": run.result_ref,
                        "trace_sequence": trace_sequences[run.id],
                        "trace_types": trace_types[run.id],
                    }
                    for run in runs
                ],
                "trace_count": len(traces),
                "task_event_sequences": [event.seq for event in task_events],
                "task_event_stages": [event.stage for event in task_events],
                "outbox_exists": outbox is not None,
                "outbox_dispatched": bool(outbox and outbox.dispatched_at),
                "outbox_owner_matches": bool(
                    outbox
                    and outbox.owner_user_id == owner_user_id
                    and outbox.task_id == task_id
                ),
                "orphan_trace_count": orphan_trace_count,
            }
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("database_url")
    parser.add_argument("owner_user_id")
    parser.add_argument("task_id")
    arguments = parser.parse_args()
    print(
        json.dumps(
            asyncio.run(
                inspect(
                    arguments.database_url,
                    arguments.owner_user_id,
                    arguments.task_id,
                )
            )
        )
    )
