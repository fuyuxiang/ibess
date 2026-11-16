"""Read-only deployment helper. Outputs exact scopes, never credentials."""

import json
import sys
from uuid import UUID
from app.db import SessionLocal
from app.schema import load_metadata


def session_scopes(db, session_id):
    from app.phase_models import PhaseBinding
    from app.core.supervision_evidence import session_channels

    binding = (
        db.query(PhaseBinding).filter_by(session_id=session_id, active=True).first()
    )
    if not binding:
        raise ValueError("场次没有有效监测范围")
    return [
        dict(
            project_id=binding.project_id,
            room_id=binding.room_id,
            session_id=session_id,
            source_id=c["source_id"],
        )
        for c in session_channels(db, session_id)
        if c["active"]
    ]


if __name__ == "__main__":
    try:
        session_id = str(UUID(sys.argv[1]))
        load_metadata()
        with SessionLocal() as db:
            scopes = session_scopes(db, session_id)
        if not scopes:
            raise ValueError("empty")
        print(json.dumps(scopes))
    except Exception:
        sys.exit("读取失败，请检查场次、数据库与当前版本；未输出部署凭据。")
