"""Version reusable room resources and reduce nonessential registry requirements."""

import json
from pathlib import Path
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0006_preparation"
down_revision = "0005_optional_identity"
branch_labels = depends_on = None


def upgrade():
    for table in json.loads(
        (
            Path(__file__).resolve().parents[1] / "snapshots/0006_preparation.json"
        ).read_text()
    ):
        op.get_bind().exec_driver_sql(table["create_sql"])
    op.execute("""CREATE FUNCTION room_monitoring_presets_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'Room monitoring revisions are immutable'; END; $$""")
    op.execute("""CREATE TRIGGER room_monitoring_presets_immutable BEFORE UPDATE OR DELETE
        ON room_monitoring_presets FOR EACH ROW EXECUTE FUNCTION room_monitoring_presets_immutable()""")
    op.add_column(
        "supervision_session_workflows",
        sa.Column("preparation_snapshot", JSONB(), nullable=True),
    )
    op.alter_column("business_projects", "owner", nullable=True)
    op.drop_constraint("business_project_text", "business_projects", type_="check")
    op.create_check_constraint(
        "business_project_text",
        "business_projects",
        "length(btrim(code)) > 0 AND length(btrim(name)) > 0 AND (owner IS NULL OR length(btrim(owner)) > 0)",
    )
    op.alter_column("business_rooms", "location", nullable=True)
    op.drop_constraint("business_room_text", "business_rooms", type_="check")
    op.create_check_constraint(
        "business_room_text",
        "business_rooms",
        "length(btrim(code)) > 0 AND length(btrim(name)) > 0 AND (location IS NULL OR length(btrim(location)) > 0)",
    )


def downgrade():
    raise RuntimeError(
        "Room resource revisions are audit records; use a forward migration or verified backup"
    )
