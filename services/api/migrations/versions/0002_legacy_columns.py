"""Adopt pre-Alembic deployments using additive, nullable column changes only."""
import json
from pathlib import Path
from alembic import op
from sqlalchemy import inspect

revision = "0002_legacy_columns"
down_revision = "0001_baseline"
branch_labels = depends_on = None


def upgrade():
    connection = op.get_bind()
    snapshot = Path(__file__).resolve().parents[1] / "snapshots/0001_schema.json"
    for table in json.loads(snapshot.read_text()):
        columns = {c["name"] for c in inspect(connection).get_columns(table["name"])}
        for column in table["columns"]:
            if column["name"] in columns:
                continue
            if not column["safe_add"]:
                raise RuntimeError(f"Explicit data migration required for {table['name']}.{column['name']}")
            name = connection.dialect.identifier_preparer.quote(table["name"])
            connection.exec_driver_sql(f"ALTER TABLE {name} ADD COLUMN {column['sql']}")
        for index in table["indexes"]:
            connection.exec_driver_sql(index["sql"])


def downgrade():
    raise RuntimeError("Compatibility columns are retained; use a forward migration or verified backup")
