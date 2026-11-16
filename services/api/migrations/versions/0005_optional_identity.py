"""Make identity verification an explicit preparation option without deleting evidence."""

from alembic import op
import sqlalchemy as sa

revision = "0005_optional_identity"
down_revision = "0004_workflows"
branch_labels = depends_on = None


def upgrade():
    op.add_column("supervision_session_workflows", sa.Column(
        "identity_verification_enabled", sa.Boolean(), nullable=False, server_default=sa.false()))
    # Existing active/finished sessions used the mandatory identity workflow.
    # Preserve their execution/history; drafts and newly created sessions opt in.
    op.execute("""UPDATE supervision_session_workflows w
        SET identity_verification_enabled = true FROM eval_sessions e
        WHERE e.session_id = w.session_id AND e.status IN ('running', 'closed')""")


def downgrade():
    raise RuntimeError("Identity settings are audit context; use a forward migration or verified backup")
