"""One explicit administrator confirmation for attendance, selected identity evidence and room authorization."""
from datetime import datetime, timezone
import hashlib
import json
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.application.authorization_roster import present_people, save_authorization
from app.business_authz_models import BusinessAuthorizationSnapshot
from app.business_fact_link_models import BusinessFactVerification
from app.business_models import BusinessParticipant, BusinessPerson, BusinessRoom, BusinessSeat
from app.db import get_session
from app.face_models import FaceProfile, PersonVerificationLink, PersonVerificationResult
from app.models import EvalSession
from app.phase_models import PhaseBinding
from app.session_workflow_models import SessionWorkflow

router = APIRouter(prefix='/rosters', tags=['room-roster'])


def actor(request):
    account = getattr(request.state, 'admin', {})
    if account.get('role') != 'admin':
        raise HTTPException(403, '本场名单由管理员确认')
    return account['username']


def iso(value):
    return value.astimezone(timezone.utc).isoformat() if value else None


def eligible(result, profile, room, db):
    if not profile or result.person_id != profile.person_id or result.status != 'matched' or result.matched is not True:
        return False
    if result.reference_version != profile.version or result.model != profile.model:
        return False
    if result.link_id:
        link = db.get(PersonVerificationLink, result.link_id)
        if not link or (link.project_id and link.project_id != room.project_id) or (link.room_id and link.room_id != room.room_id):
            return False
    return True


def session_scope(db, room, session_id=None, *, writing=False):
    if session_id is None:
        return None
    session = db.query(EvalSession).filter_by(session_id=str(session_id)).first()
    binding = db.query(PhaseBinding).filter_by(session_id=str(session_id), active=True).first()
    workflow = db.get(SessionWorkflow, str(session_id))
    if not session or not binding or not workflow or (binding.project_id, binding.room_id) != (room.project_id, room.room_id):
        raise HTTPException(422, '场次与名单所属评标室不一致或尚未纳入监管流程')
    if writing and session.status == 'closed':
        raise HTTPException(409, '已结束场次的名单只供查阅，原身份依据和授权历史继续保留')
    if writing and db.query(PhaseBinding).join(EvalSession, EvalSession.session_id == PhaseBinding.session_id).filter(
            PhaseBinding.room_id == room.room_id, PhaseBinding.active.is_(True),
            EvalSession.status == 'running', EvalSession.session_id != str(session_id)).first():
        raise HTTPException(409, '本室另有进行中场次，不能从准备中的场次修改当前人员名单')
    return workflow


def roster(db, room, workflow=None):
    require_identity = workflow is None or workflow.identity_verification_enabled
    facts, present = present_people(db, room.room_id)
    current = db.query(BusinessAuthorizationSnapshot).filter_by(room_id=room.room_id, status='active').first()
    entries = {e.person_id: e for e in current.entries} if current else {}
    items = []
    for person_id, info in sorted(present.items()):
        person, profile = db.get(BusinessPerson, person_id), db.get(FaceProfile, person_id)
        member = db.query(BusinessParticipant).filter_by(project_id=room.project_id, person_id=person_id).first()
        seat = db.get(BusinessSeat, info['seat_id']) if info['seat_id'] else None
        link = db.query(BusinessFactVerification).filter_by(fact_id=info['fact_id']).first()
        results = db.query(PersonVerificationResult).filter_by(person_id=person_id).order_by(
            PersonVerificationResult.checked_at.desc(), PersonVerificationResult.result_id).limit(20).all()
        choices = [r for r in results if eligible(r, profile, room, db)]
        linked = db.get(PersonVerificationResult, link.result_id) if link else None
        if linked and eligible(linked, profile, room, db) and linked.result_id not in {r.result_id for r in choices}:
            choices.append(linked)
        valid = bool(linked and eligible(linked, profile, room, db))
        allowed = bool(person and person.enabled and member and (not seat or (seat.enabled and seat.room_id == room.room_id)))
        items.append({'person_id': person_id, 'name': person.name if person else '人员已移除',
            'role': member.role if member else '', 'seat_id': info['seat_id'], 'seat_code': seat.code if seat else None,
            'fact_id': info['fact_id'], 'registered': bool(profile), 'enabled': allowed,
            'verified': valid, 'verification_result_id': link.result_id if valid else None,
            'recipient_authorized': bool(entries.get(person_id) and entries[person_id].recipient_authorized),
            'verifications': [{'result_id': r.result_id, 'checked_at': iso(r.checked_at),
                'source': r.room_name or '现场手动核验', 'similarity': r.similarity} for r in choices],
            '_profile': profile.version if profile else None, '_link': link.result_id if link else None})
    version_data = {'room': [room.room_id, room.project_id, room.enabled],
        'facts': [[f.fact_id, f.fact_type, f.person_id, f.seat_id, iso(f.occurred_at), f.payload] for f in facts],
        'snapshot': current.snapshot_id if current else None, 'items': items,
        'session': [workflow.session_id, workflow.version, require_identity] if workflow else None}
    version = hashlib.sha256(json.dumps(version_data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    for item in items:
        item.pop('_profile'); item.pop('_link')
    return {'room_id': room.room_id, 'project_id': room.project_id, 'version': version, 'items': items,
        'identity_verification_enabled': bool(require_identity),
        'confirmed_at': iso(current.created_at) if current else None, 'confirmed_by': current.created_by if current else None,
        'information_visibility': current.information_visibility if current else 'public',
        'confidentiality_policy_id': current.confidentiality_policy_id if current else None,
        'history': [{'at': iso(s.created_at), 'actor': s.created_by, 'count': len(s.entries), 'current': s.status == 'active'}
            for s in db.query(BusinessAuthorizationSnapshot).filter_by(room_id=room.room_id).order_by(
                BusinessAuthorizationSnapshot.created_at.desc()).limit(10)],
        'needs_confirmation': not current or {(e.person_id, e.basis_fact_id) for e in current.entries} !=
            {(i['person_id'], i['fact_id']) for i in items} or any(
                (require_identity and not i['verified']) or not i['enabled'] for i in items)}


def find_room(db, room_id, lock=False):
    query = db.query(BusinessRoom).filter_by(room_id=str(room_id))
    room = (query.with_for_update() if lock else query).first()
    if not room:
        raise HTTPException(404, '评标室不存在')
    return room


@router.get('/{room_id}')
def read_roster(room_id: UUID, request: Request, session_id: UUID | None = None, db: Session = Depends(get_session)):
    actor(request)
    room = find_room(db, room_id)
    return roster(db, room, session_scope(db, room, session_id))


class Selection(BaseModel):
    model_config = ConfigDict(extra='forbid')
    person_id: UUID
    verification_result_id: UUID


class Confirmation(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_version: str = Field(pattern=r'^[a-f0-9]{64}$')
    session_id: UUID | None = None
    selections: list[Selection] = Field(default_factory=list, max_length=1000)
    recipients: list[UUID] = Field(default_factory=list, max_length=1000)
    information_visibility: Literal['public', 'confidential']
    confidentiality_policy_id: str | None = Field(default=None, min_length=1, max_length=64)


@router.post('/{room_id}/confirm', status_code=201)
def confirm_roster(room_id: UUID, body: Confirmation, request: Request, db: Session = Depends(get_session)):
    who = actor(request)
    # Use the same lock order as workflow settings before locking the room.
    if body.session_id:
        db.query(EvalSession).filter_by(session_id=str(body.session_id)).with_for_update().first()
    room = find_room(db, room_id, lock=True)
    if not room.enabled:
        raise HTTPException(422, '评标室已停用')
    # Face registration/replacement also locks the person row. Keep the selected
    # reference stable until both verification links and the roster version commit.
    _, present = present_people(db, room.room_id)
    db.query(BusinessPerson).filter(BusinessPerson.person_id.in_(present)).order_by(
        BusinessPerson.person_id).with_for_update().all()
    workflow = session_scope(db, room, body.session_id, writing=True)
    state = roster(db, room, workflow)
    if state['version'] != body.expected_version:
        raise HTTPException(409, '名单或核验记录已变化，请刷新核对后重新确认')
    selected = {str(s.person_id): str(s.verification_result_id) for s in body.selections}
    present_ids = {i['person_id'] for i in state['items']}
    recipients = {str(p) for p in body.recipients}
    if len(selected) != len(body.selections) or not set(selected).issubset(present_ids) or not recipients.issubset(present_ids):
        raise HTTPException(422, '名单包含重复或不在本室到场名单中的人员')
    plan = []
    for item in state['items']:
        result_id = selected.get(item['person_id']) or item['verification_result_id']
        if not item['enabled'] or ((result_id or state['identity_verification_enabled']) and
                result_id not in {r['result_id'] for r in item['verifications']}):
            raise HTTPException(422, f"请先核验并选择 {item['name']} 的有效核验记录")
        if result_id:
            plan.append((item['fact_id'], result_id))
    # Validate every selection before writing; any failure rolls back links and the new authorization version together.
    for fact_id, result_id in plan:
        link = db.query(BusinessFactVerification).filter_by(fact_id=fact_id).first()
        if link and link.result_id == result_id:
            continue
        if not link:
            link = BusinessFactVerification(link_id=str(uuid4()), fact_id=fact_id)
            db.add(link)
        link.result_id, link.actor, link.created_at = result_id, who, datetime.now(timezone.utc)
    db.flush()
    snapshot = save_authorization(db, project_id=room.project_id, room_id=room.room_id, actor=who,
        information_visibility=body.information_visibility,
        confidentiality_policy_id=(body.confidentiality_policy_id or '').strip() or None,
        grants={p: p in recipients for p in present_ids},
        verification_overrides={item['person_id']: selected.get(item['person_id']) or item['verification_result_id']
                                for item in state['items']}, commit=False)
    if workflow:
        from app.application import session_workflow as flow
        flow.journal(db, db.query(EvalSession).filter_by(session_id=workflow.session_id).one(), workflow, 'preparation',
                     '保存本场人员名单，保留核验依据和授权历史', who,
                     {'authorization_snapshot_id': snapshot.snapshot_id,
                      'identity_verification_enabled': workflow.identity_verification_enabled})
    db.commit()
    return roster(db, room, workflow)
