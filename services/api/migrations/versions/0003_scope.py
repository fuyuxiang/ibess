"""Version the source-alias and occurrence-time scope resolver."""
from pathlib import Path
from alembic import op

revision = "0003_scope"
down_revision = "0002_legacy_columns"
branch_labels = depends_on = None


def upgrade():
    sql = Path(__file__).resolve().parents[1] / "snapshots/0003_scope.sql"
    op.get_bind().exec_driver_sql(sql.read_text())


def downgrade():
    raise RuntimeError("Scope functions are retained to protect existing worker contracts")
