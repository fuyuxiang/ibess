"""S04授权快照 API（6.3）。

生成：按 6.1 事实推导在场人员（check_in在册、check_out离场、change换人、
leave_permit不影响在场、clarification仅记录），按 6.2 核验关联推导 identity_verified，
叠加人工授权覆盖（grants），生成房间时点快照；同房间旧 active 快照自动置为 superseded。
消费：GET latest 返回语义研判 trusted context 所需字段（speaker_bindings/接收授权/保密依据）。
"""
from datetime import timezone
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, StringConstraints
from sqlalchemy.orm import Session, joinedload

from app.business_authz_models import BusinessAuthorizationEntry, BusinessAuthorizationSnapshot

from app.db import get_session

router = APIRouter()
PhaseId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
PolicyId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
ROLES = ("expert", "supervisor", "staff", "owner", "agent", "bidder")


class AuthzGrant(BaseModel):
    model_config = ConfigDict(extra="ignore")

    person_id: UUID
    recipient_authorized: bool = False


class SnapshotIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    project_id: UUID
    room_id: UUID
    phase_id: PhaseId | None = None
    information_visibility: Literal["public", "confidential"] = "public"
    confidentiality_policy_id: PolicyId | None = None
    grants: list[AuthzGrant] = []


def _actor(request: Request):
    admin = getattr(request.state, "admin", None)
    if not isinstance(admin, dict) or not admin.get("username"):
        raise HTTPException(403, "管理员身份缺失，无法生成授权快照")
    return admin["username"]


def _serialize(snapshot: BusinessAuthorizationSnapshot) -> dict:
    return {
        "snapshot_id": snapshot.snapshot_id, "project_id": snapshot.project_id, "room_id": snapshot.room_id,
        "phase_id": snapshot.phase_id, "information_visibility": snapshot.information_visibility,
        "confidentiality_policy_id": snapshot.confidentiality_policy_id, "status": snapshot.status,
        "created_by": snapshot.created_by,
        "created_at": snapshot.created_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "entries": [{
            "entry_id": entry.entry_id, "person_id": entry.person_id, "seat_id": entry.seat_id,
            "role": entry.role, "identity_verified": entry.identity_verified,
            "verification_result_id": entry.verification_result_id, "basis_fact_id": entry.basis_fact_id,
            "recipient_authorized": entry.recipient_authorized, "grant_fact_id": entry.grant_fact_id,
            "person_name": entry.person.name if entry.person else None,
        } for entry in snapshot.entries],
    }



@router.post("/authz/snapshots", status_code=201)
def create_snapshot(body: SnapshotIn, request: Request, session: Session = Depends(get_session)):
    from app.application.authorization_roster import save_authorization
    snapshot = save_authorization(session, project_id=str(body.project_id), room_id=str(body.room_id),
        actor=_actor(request), phase_id=body.phase_id, information_visibility=body.information_visibility,
        confidentiality_policy_id=body.confidentiality_policy_id,
        grants={str(grant.person_id): grant.recipient_authorized for grant in body.grants})
    return _serialize(snapshot)


@router.get("/authz/snapshots")
def list_snapshots(room_id: UUID, session: Session = Depends(get_session)):
    rows = (session.query(BusinessAuthorizationSnapshot)
            .options(joinedload(BusinessAuthorizationSnapshot.entries).joinedload(BusinessAuthorizationEntry.person))
            .filter(BusinessAuthorizationSnapshot.room_id == str(room_id))
            .order_by(BusinessAuthorizationSnapshot.created_at.desc()).limit(20).all())
    return {"items": [_serialize(row) for row in rows]}


@router.get("/authz/snapshots/latest")
def latest_snapshot(room_id: UUID, session: Session = Depends(get_session)):
    """返回语义研判 trusted context 形状：speaker_bindings 槽位 + 接收授权 + 保密依据。"""
    snapshot = (session.query(BusinessAuthorizationSnapshot)
                .options(joinedload(BusinessAuthorizationSnapshot.entries).joinedload(BusinessAuthorizationEntry.person))
                .filter(BusinessAuthorizationSnapshot.room_id == str(room_id),
                        BusinessAuthorizationSnapshot.status == "active")
                .order_by(BusinessAuthorizationSnapshot.created_at.desc()).first())
    if snapshot is None:
        raise HTTPException(404, "该评标室尚无有效授权快照")
    entries = sorted(snapshot.entries, key=lambda item: (item.person.name if item.person else ""))
    authorized = [entry for entry in entries if entry.recipient_authorized and entry.identity_verified]
    return {
        "snapshot_id": snapshot.snapshot_id, "project_id": snapshot.project_id, "room_id": snapshot.room_id,
        "phase_id": snapshot.phase_id, "binding_status": "bound",
        "information_visibility": snapshot.information_visibility,
        "confidentiality_policy_id": snapshot.confidentiality_policy_id,
        "speaker_bindings": [{
            "segment_speaker": None,  # 由语义输入组装方按转写说话人映射
            "person_id": entry.person_id, "display_name": entry.person.name if entry.person else None,
            "role": entry.role, "identity_verified": entry.identity_verified,
            "basis_fact_id": entry.basis_fact_id,
        } for entry in entries],
        "recipient_authorized": bool(authorized),
        "recipient_fact_id": (authorized[0].basis_fact_id or snapshot.snapshot_id) if authorized else snapshot.snapshot_id,
        "confidentiality_policy_id": snapshot.confidentiality_policy_id,
        "context_snapshot_id": snapshot.snapshot_id,
        "created_by": snapshot.created_by,
        "created_at": snapshot.created_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


@router.get("/authz/snapshots/{snapshot_id}")
def get_snapshot(snapshot_id: UUID, session: Session = Depends(get_session)):
    snapshot = (session.query(BusinessAuthorizationSnapshot)
                .options(joinedload(BusinessAuthorizationSnapshot.entries).joinedload(BusinessAuthorizationEntry.person))
                .filter(BusinessAuthorizationSnapshot.snapshot_id == str(snapshot_id)).first())
    if snapshot is None:
        raise HTTPException(404, "授权快照不存在")
    return _serialize(snapshot)
