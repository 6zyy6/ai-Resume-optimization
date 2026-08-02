import argparse
import asyncio
import json

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.modules.tasks.service import TaskService
from app.workers.execution import TaskExecutor, resolve_operation
from app.workers.pipeline import configure_pipeline_operations


async def run(owner_user_id: str, task_id: str) -> dict[str, object]:
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    task_service = TaskService(sessions)
    configure_pipeline_operations(sessions, settings, task_service)
    try:
        return await TaskExecutor(
            task_service,
            sleep=lambda _delay: None,
            jitter=lambda: 0,
        ).execute(owner_user_id, task_id, resolve_operation)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("owner_user_id")
    parser.add_argument("task_id")
    arguments = parser.parse_args()
    print(json.dumps(asyncio.run(run(arguments.owner_user_id, arguments.task_id))))
