"""Alembic setup, using the same PostgreSQL configuration as the API."""

from logging.config import fileConfig

from alembic import context

from testpilot.api.database import create_database_engine, database_url
from testpilot.api.db_models import Base


config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)
target_metadata = Base.metadata


if context.is_offline_mode():
    context.configure(
        url=database_url(), target_metadata=target_metadata,
        literal_binds=True, dialect_opts={"paramstyle": "named"}, compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    # An explicitly injected connection lets integration tests migrate an
    # isolated PostgreSQL schema using the very same revision files.
    supplied_connection = config.attributes.get("connection")

    def migrate(connection):
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()

    if supplied_connection is not None:
        migrate(supplied_connection)
    else:
        engine = create_database_engine()
        try:
            with engine.connect() as connection:
                migrate(connection)
        finally:
            engine.dispose()
