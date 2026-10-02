from alembic import context
from sqlalchemy import create_engine
from sqlmodel import SQLModel

from acm_hub import models  # noqa: F401  (register tables)

target_metadata = SQLModel.metadata


def include_object(obj, name, type_, reflected, compare_to):  # noqa: ANN001
    # The FTS5 virtual table and its shadow tables are managed by hand (see migration 0001);
    # autogenerate must never try to drop them.
    return not (type_ == "table" and name.startswith("memory_fts"))


config = context.config


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
            include_object=include_object,
        )
        with context.begin_transaction():
            context.run_migrations()
        return
    engine = create_engine(config.get_main_option("sqlalchemy.url"))
    with engine.connect() as conn:
        context.configure(
            connection=conn,
            target_metadata=target_metadata,
            render_as_batch=True,
            include_object=include_object,
        )
        with context.begin_transaction():
            context.run_migrations()


run_migrations_online()
