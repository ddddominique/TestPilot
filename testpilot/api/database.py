"""Environment-based PostgreSQL configuration and request session cleanup."""

import os
from typing import Iterator, Optional

from fastapi import Request
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine, URL, make_url
from sqlalchemy.orm import Session, sessionmaker


def database_url(value: Optional[str] = None) -> URL:
    configured = value if value is not None else os.getenv("DATABASE_URL")
    if not configured:
        raise ValueError("DATABASE_URL is required; see .env.example for local PostgreSQL setup")
    try:
        url = make_url(configured)
    except Exception as exc:
        raise ValueError("DATABASE_URL must be a valid PostgreSQL URL") from exc
    if url.drivername != "postgresql+psycopg":
        raise ValueError("DATABASE_URL must use the postgresql+psycopg driver")
    return url


def create_database_engine(value: Optional[str] = None) -> Engine:
    return create_engine(
        database_url(value), pool_pre_ping=True, connect_args={"connect_timeout": 5},
    )


def create_session_factory(engine: Engine) -> sessionmaker:
    return sessionmaker(bind=engine, expire_on_commit=False)


def get_session(request: Request) -> Iterator[Session]:
    # Closing also rolls back unfinished read transactions. Write transactions
    # commit inside repository methods, before an HTTP response is returned.
    with request.app.state.session_factory() as session:
        yield session
