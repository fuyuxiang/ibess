"""Single schema owner: transactional, versioned PostgreSQL migrations."""

from pathlib import Path
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from ibess_db.schema import SCHEMA_HEAD, require_schema


def upgrade_schema(engine):
    if engine.dialect.name != "postgresql":
        raise RuntimeError(
            "Production migrations require PostgreSQL; isolated SQLite tests create their own tables"
        )
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    with engine.begin() as connection:
        connection.execute(text("SELECT pg_advisory_xact_lock(81234171)"))
        config.attributes["connection"] = connection
        command.upgrade(config, "head")


def verify_schema(engine):
    from app.schema import load_metadata

    metadata = load_metadata()
    require_schema(engine, metadata.tables)
    inspector = inspect(engine)
    for table in metadata.sorted_tables:
        missing = set(table.columns.keys()) - {
            c["name"] for c in inspector.get_columns(table.name)
        }
        if missing:
            raise RuntimeError(
                f"Schema revision {SCHEMA_HEAD} is incomplete: {table.name} columns {sorted(missing)}"
            )


def ensure_operator_schema(engine):
    """Compatibility entry point for deployment scripts; all DDL is versioned."""
    upgrade_schema(engine)


def main():
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("upgrade", "check"))
    args = parser.parse_args()
    from app.db import engine

    try:
        if args.action == "upgrade":
            upgrade_schema(engine)
        verify_schema(engine)
        print(f"Database schema ready: {SCHEMA_HEAD}")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
