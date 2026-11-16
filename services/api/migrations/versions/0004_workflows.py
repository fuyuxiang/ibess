"""Add preparation and append-only session operations without modifying existing event data."""

import json
from pathlib import Path
from alembic import op

revision = "0004_workflows"
down_revision = "0003_scope"
branch_labels = depends_on = None


def upgrade():
    path = Path(__file__).resolve().parents[1] / "snapshots/0004_workflows.json"
    for table in json.loads(path.read_text()):
        op.get_bind().exec_driver_sql(table["create_sql"])
        for index in table["indexes"]:
            op.get_bind().exec_driver_sql(index)


def downgrade():
    raise RuntimeError(
        "Workflow journals are audit records; use a forward migration or verified backup"
    )
