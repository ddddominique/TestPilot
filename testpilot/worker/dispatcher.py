"""PostgreSQL outbox dispatcher. Publication retries never retry execution."""

import argparse
import logging
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import exists, select, update
from sqlalchemy.exc import SQLAlchemyError

from testpilot.api.database import create_database_engine, create_session_factory
from testpilot.api.db_models import RunDispatch, TestRunRecord

from .celery_app import app

logger = logging.getLogger(__name__)
DISPATCH_INTERVAL = 60  # Also the publication lease and queued redelivery delay.


def publish(run_id, task_id):
    # send_task never uses Celery eager execution, even if a caller configures it.
    app.send_task("testpilot.execute_run", args=[str(run_id)], task_id=str(task_id), retry=False)


def dispatch_once(sessions, publisher=publish, *, now=None):
    """CAS lease, commit, publish without a connection, then record the receipt.

    Re-publish QUEUED jobs after a bounded delay even following a successful
    publish: Redis can lose accepted messages. All copies have the same task ID.
    A crash in either publication window is safe because claims are fenced.
    """
    now = now or datetime.now(timezone.utc)
    token = uuid4()
    eligible = exists().where(TestRunRecord.id == RunDispatch.run_id, TestRunRecord.status == "QUEUED")
    with sessions() as session, session.begin():
        candidate = session.scalar(select(RunDispatch.run_id).where(
            RunDispatch.available_at <= now, eligible,
        ).order_by(RunDispatch.available_at, RunDispatch.run_id).limit(1))
        if candidate is None:
            return False
        leased = session.scalar(update(RunDispatch).where(
            RunDispatch.run_id == candidate, RunDispatch.available_at <= now, eligible,
        ).values(available_at=now + timedelta(seconds=DISPATCH_INTERVAL), lease_token=token,
                 attempts=RunDispatch.attempts + 1).returning(RunDispatch.run_id))
        if leased is None:
            return False
        task_id = session.scalar(select(TestRunRecord.task_id).where(TestRunRecord.id == leased))
    # Exceptions are deliberately not recorded verbatim: broker URLs contain secrets.
    try:
        publisher(leased, task_id)
    except Exception:
        error = "Broker publication failed or acknowledgement was uncertain; dispatch will retry."
        logger.warning("Run %s: %s", leased, error)
    else:
        error = None
    with sessions() as session, session.begin():
        session.execute(update(RunDispatch).where(
            RunDispatch.run_id == leased, RunDispatch.lease_token == token,
        ).values(last_error=error, **({"published_at": now} if error is None else {})))
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Attempt one publication and exit")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    engine = create_database_engine()
    sessions = create_session_factory(engine)
    try:
        while True:
            try:
                dispatched = dispatch_once(sessions)
            except SQLAlchemyError:
                logger.error("Dispatcher database operation failed; intent remains durable.")
                dispatched = False
            if args.once:
                return
            if not dispatched:
                time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
