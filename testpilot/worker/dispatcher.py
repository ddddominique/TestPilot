"""PostgreSQL outbox dispatcher. Publication retries never retry execution."""

import argparse
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import exists, select, update
from sqlalchemy.exc import SQLAlchemyError

from testpilot.api.database import create_database_engine, create_session_factory
from testpilot.api.db_models import RunDispatch, TestRunRecord

from .celery_app import app

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DispatchBackoff:
    interval_seconds: int = 60
    max_seconds: int = 900

    def __post_init__(self):
        if (type(self.interval_seconds) is not int or type(self.max_seconds) is not int
                or not 1 <= self.interval_seconds <= self.max_seconds <= 86_400):
            raise ValueError("Dispatch backoff requires integer seconds: 1 <= interval <= maximum <= 86400")

    @classmethod
    def from_environment(cls):
        values = []
        for name, default in (("TESTPILOT_DISPATCH_INTERVAL_SECONDS", 60),
                              ("TESTPILOT_DISPATCH_MAX_BACKOFF_SECONDS", 900)):
            try:
                values.append(int(os.getenv(name, str(default))))
            except ValueError:
                raise ValueError(f"{name} must be an integer number of seconds") from None
        return cls(*values)

    def delay(self, attempt):
        # Saturate without constructing huge powers for long-lived outbox rows.
        delay = self.interval_seconds
        remaining = attempt - 1
        while remaining > 0 and delay < self.max_seconds:
            delay = min(delay * 2, self.max_seconds)
            remaining -= 1
        return delay


def still_queued(sessions, run_id, task_id, token):
    """Fresh pre-publication check; release the read connection before broker I/O."""
    with sessions() as session:
        return session.scalar(select(exists().where(
            TestRunRecord.id == run_id, TestRunRecord.status == "QUEUED",
            TestRunRecord.task_id == task_id, RunDispatch.run_id == TestRunRecord.id,
            RunDispatch.lease_token == token,
        )))


def publish(run_id, task_id):
    # send_task never uses Celery eager execution, even if a caller configures it.
    app.send_task("testpilot.execute_run", args=[str(run_id)], task_id=str(task_id), retry=False)


def dispatch_once(sessions, publisher=publish, *, now=None, backoff=None):
    """CAS lease, commit, publish without a connection, then record the receipt.

    Re-publish QUEUED jobs after a bounded delay even following a successful
    publish: Redis can lose accepted messages. All copies have the same task ID.
    A crash in either publication window is safe because claims are fenced.
    """
    backoff = backoff if backoff is not None else DispatchBackoff.from_environment()
    now = now or datetime.now(timezone.utc)
    token = uuid4()
    eligible = exists().where(TestRunRecord.id == RunDispatch.run_id, TestRunRecord.status == "QUEUED")
    with sessions() as session, session.begin():
        candidate = session.execute(select(RunDispatch.run_id, RunDispatch.attempts).where(
            RunDispatch.available_at <= now, eligible,
        ).order_by(RunDispatch.available_at, RunDispatch.run_id).limit(1)).first()
        if candidate is None:
            return False
        leased = session.scalar(update(RunDispatch).where(
            RunDispatch.run_id == candidate.run_id, RunDispatch.available_at <= now, eligible,
            RunDispatch.attempts == candidate.attempts,
        ).values(available_at=now + timedelta(seconds=backoff.delay(candidate.attempts + 1)), lease_token=token,
                 attempts=RunDispatch.attempts + 1).returning(RunDispatch.run_id))
        if leased is None:
            return False
        task_id = session.scalar(select(TestRunRecord.task_id).where(TestRunRecord.id == leased))
    if not still_queued(sessions, leased, task_id, token):
        return True  # A competing worker claimed it, or this lease was superseded.
    # Exceptions are deliberately not recorded verbatim: broker URLs contain secrets.
    try:
        publisher(leased, task_id)
    except Exception:
        error = "Broker publication failed or acknowledgement was uncertain; dispatch will retry."
        logger.warning("Run %s: %s", leased, error)
    else:
        error = None
        logger.info("Run %s published; if still unclaimed, redelivery is due after %s.",
                    leased, now + timedelta(seconds=backoff.delay(candidate.attempts + 1)))
    with sessions() as session, session.begin():
        session.execute(update(RunDispatch).where(
            RunDispatch.run_id == leased, RunDispatch.lease_token == token,
        ).values(last_error=error, **({"published_at": now} if error is None else {})))
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Attempt one publication and exit")
    args = parser.parse_args()
    try:
        backoff = DispatchBackoff.from_environment()
    except ValueError as exc:
        parser.error(str(exc))
    logging.basicConfig(level=logging.INFO)
    engine = create_database_engine()
    sessions = create_session_factory(engine)
    try:
        while True:
            try:
                dispatched = dispatch_once(sessions, backoff=backoff)
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
