"""FastAPI application factory; the CLI imports none of these modules."""

from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from .database import create_database_engine, create_session_factory
from .routes import router


def create_app(database_url: Optional[str] = None, sessions: Optional[sessionmaker] = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI):
        engine = None
        try:
            if sessions is None:
                engine = create_database_engine(database_url)
                application.state.session_factory = create_session_factory(engine)
            else:
                application.state.session_factory = sessions
            yield
        finally:
            if engine is not None:
                engine.dispose()

    application = FastAPI(title="TestPilot API", version="0.1.0", lifespan=lifespan)
    application.include_router(router)

    @application.exception_handler(SQLAlchemyError)
    async def database_error(request: Request, exc: SQLAlchemyError) -> JSONResponse:
        # Do not return SQL statements, connection strings, or credentials.
        return JSONResponse(status_code=503, content={"detail": "Database operation failed. Please retry."})

    return application


app = create_app()
