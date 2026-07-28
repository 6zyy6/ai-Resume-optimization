import os

from celery import Celery

from app.workers.execution import QUEUE_NAMES, execute_task


def create_celery_app(broker_url: str | None = None) -> Celery:
    application = Celery(
        "ai_resume",
        broker=broker_url or os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/0"),
    )
    application.conf.update(
        task_default_queue="ai.interactive",
        task_create_missing_queues=False,
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
