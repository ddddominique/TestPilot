"""Conservative Celery settings; PostgreSQL, not Celery, owns run results."""

import os

from celery import Celery

HARD_TIME_LIMIT = 1_800
SOFT_TIME_LIMIT = 1_740

app = Celery("testpilot", broker=os.getenv("CELERY_BROKER_URL", "redis://127.0.0.1:6379/0"),
             include=["testpilot.worker.tasks"])
app.conf.update(
    task_serializer="json", accept_content=["json"], task_ignore_result=True,
    task_default_queue="testpilot", task_acks_late=False,
    task_reject_on_worker_lost=False, worker_prefetch_multiplier=1,
    worker_concurrency=1, task_soft_time_limit=SOFT_TIME_LIMIT,
    task_time_limit=HARD_TIME_LIMIT, task_publish_retry=False,
    broker_connection_retry_on_startup=True, broker_connection_retry=True,
    broker_connection_max_retries=10, broker_connection_timeout=3,
    broker_transport_options={"socket_connect_timeout": 3, "socket_timeout": 3,
                              "visibility_timeout": 3_600},
    worker_enable_remote_control=False,
)
