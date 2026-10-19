"""Fail stale RUNNING attempts without re-executing external operations."""

import argparse
from datetime import datetime, timedelta, timezone

from sqlalchemy import update

from testpilot.api.database import create_database_engine, create_session_factory
from testpilot.api.db_models import TestRunRecord

from .celery_app import RECONCILE_MIN_AGE


def reconcile(sessions, age_seconds=3_600, *, now=None):
    if age_seconds < RECONCILE_MIN_AGE:
        raise ValueError(f"Stale age must be at least {RECONCILE_MIN_AGE} seconds (above the task hard limit).")
    now = now or datetime.now(timezone.utc)
    with sessions() as session, session.begin():
        result = session.execute(update(TestRunRecord).where(
            TestRunRecord.status == "RUNNING", TestRunRecord.started_at < now - timedelta(seconds=age_seconds),
        ).values(status="FAILED", completed_at=now, updated_at=now, execution_outcome="INFRASTRUCTURE_ERROR",
                 error_details="Execution outcome unknown: worker interrupted or result persistence failed. "
                 "No automatic execution retry. Inspect worker logs and Docker containers before creating a new run."))
        return result.rowcount


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--running-age-seconds", type=int, default=3_600)
    args = parser.parse_args()
    if args.running_age_seconds < RECONCILE_MIN_AGE:
        parser.error(f"--running-age-seconds must be at least {RECONCILE_MIN_AGE}")
    engine = create_database_engine()
    try:
        print(f"Marked {reconcile(create_session_factory(engine), args.running_age_seconds)} stale attempts FAILED.")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
