"""Frozen baseline; create missing tables without replacing legacy data."""
import json
from pathlib import Path
from alembic import op
from sqlalchemy import inspect

revision = "0001_baseline"
down_revision = None
branch_labels = depends_on = None


def upgrade():
    connection = op.get_bind()
    snapshot = Path(__file__).resolve().parents[1] / "snapshots/0001_schema.json"
    for table in json.loads(snapshot.read_text()):
        if not inspect(connection).has_table(table["name"]):
            connection.exec_driver_sql(table["create_sql"])
            # SQLAlchemy implements unique=True,index=True as a unique index;
            # referenced keys must exist before dependent tables are created.
            for index in table["indexes"]:
                connection.exec_driver_sql(index["sql"])


def downgrade():
    raise RuntimeError("Baseline downgrade would remove business data; restore a verified backup instead")
