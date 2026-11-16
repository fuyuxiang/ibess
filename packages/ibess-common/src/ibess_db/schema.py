"""Workers validate the migration contract and never create or alter tables."""

from sqlalchemy import inspect, text

SCHEMA_HEAD = "0003_scope"


def require_schema(engine, tables=()):
    inspector = inspect(engine)
    missing = sorted(set(tables) - set(inspector.get_table_names()))
    if missing:
        raise RuntimeError(
            "Database schema is incomplete; run API migrations first: "
            + ", ".join(missing)
        )
    if engine.dialect.name == "postgresql":
        if not inspector.has_table("alembic_version"):
            raise RuntimeError(
                "Database has not been migrated; run python -m app.migrations upgrade"
            )
        with engine.connect() as connection:
            versions = set(
                connection.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalars()
            )
        if versions != {SCHEMA_HEAD}:
            raise RuntimeError(
                "Database revision is incompatible; run API migrations before starting workers"
            )
