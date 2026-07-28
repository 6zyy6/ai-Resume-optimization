import os

from celery import Celery
from celery.signals import worker_process_init

from app.workers.execution import (
    QUEUE_NAMES,
    configure_worker,
    execute_task,
    resolve_operation,
)


def create_celery_app(broker_url: str | None = None) -> Celery:
    application = Celery(
        "ai_resume",
        broker=broker_url or os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/0"),
    )
    application.conf.update(
        task_default_queue="ai.interactive",
        task_create_missing_queues=False,
        broker_connection_timeout=5,
        task_publish_retry=False,
        task_queues={name: {} for name in QUEUE_NAMES},
        task_routes={
            "app.workers.execution.execute_task": {
                "queue": "ai.interactive",
            }
        },
    )
    application.task(name="app.workers.execution.execute_task")(execute_task)
    return application


celery_app = create_celery_app()


@worker_process_init.connect
def initialize_worker_runtime(**_kwargs) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.core.config import get_settings
    from app.modules.tasks.service import TaskService
    from app.workers.pipeline import configure_pipeline_operations

    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    task_service = TaskService(sessions)
    configure_pipeline_operations(sessions, settings, task_service)
    configure_worker(task_service, resolve_operation)
