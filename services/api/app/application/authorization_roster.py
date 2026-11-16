"""Build versioned room authorization records from the existing attendance and verification tables."""
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy.orm import joinedload

from app.business_authz_models import BusinessAuthorizationEntry, BusinessAuthorizationSnapshot
from app.business_fact_link_models import BusinessFactVerification
from app.business_facts_models import BusinessAttendanceFact
from app.business_models import BusinessParticipant, BusinessRoom


def present_people(db, room_id):
    facts = db.query(BusinessAttendanceFact).filter_by(room_id=str(room_id)).order_by(
        BusinessAttendanceFact.occurred_at.asc(), BusinessAttendanceFact.fact_id).all()
    present = {}
    for fact in facts:
        if fact.fact_type == 'change' and fact.person_id:
            present.pop((fact.payload or {}).get('replaced_person_id'), None)
        if fact.fact_type in ('check_in', 'change') and fact.person_id:
            present[fact.person_id] = {'seat_id': fact.seat_id, 'fact_id': fact.fact_id}
        elif fact.fact_type == 'check_out' and fact.person_id:
            present.pop(fact.person_id, None)
    return facts, present


def save_authorization(db, *, project_id, room_id, actor, phase_id=None,
                       information_visibility='public', confidentiality_policy_id=None,
                       grants=None, verification_overrides=None, commit=True):
    room = db.query(BusinessRoom).filter_by(room_id=str(room_id)).with_for_update().first()
    if room is None or str(room.project_id) != str(project_id):
        raise HTTPException(422, '项目与评标室不一致或不存在')
    if information_visibility == 'confidential' and not confidentiality_policy_id:
        raise HTTPException(422, '保密信息级别必须提供保密依据编号（confidentiality_policy_id）')
    grants = grants or {}
    for person_id in grants:
        if not db.query(BusinessParticipant).filter_by(project_id=str(project_id), person_id=person_id).first():
            raise HTTPException(422, '授权覆盖的人员必须先加入所属项目')
    _, present = present_people(db, room_id)
    links = {link.fact_id: link for link in db.query(BusinessFactVerification).options(
        joinedload(BusinessFactVerification.result)).filter(
            BusinessFactVerification.fact_id.in_([p['fact_id'] for p in present.values()])).all()}
    db.query(BusinessAuthorizationSnapshot).filter_by(room_id=str(room_id), status='active').update(
        {'status': 'superseded'}, synchronize_session='fetch')
    snapshot = BusinessAuthorizationSnapshot(snapshot_id=str(uuid4()), project_id=str(project_id),
        room_id=str(room_id), phase_id=phase_id, information_visibility=information_visibility,
        confidentiality_policy_id=confidentiality_policy_id, status='active', created_by=actor,
        created_at=datetime.now(timezone.utc))
    db.add(snapshot)
    db.flush()
    for person_id, info in present.items():
        participant = db.query(BusinessParticipant).filter_by(project_id=str(project_id), person_id=person_id).first()
        if not participant:
            continue
        link = links.get(info['fact_id'])
        verified = bool(link and link.result and link.result.status == 'matched')
        result_id = link.result_id if verified else None
        if verification_overrides is not None:
            result_id = verification_overrides.get(person_id)
            verified = result_id is not None
        db.add(BusinessAuthorizationEntry(entry_id=str(uuid4()), snapshot_id=snapshot.snapshot_id,
            person_id=person_id, seat_id=info['seat_id'], role=participant.role, identity_verified=verified,
            verification_result_id=result_id, basis_fact_id=info['fact_id'],
            recipient_authorized=grants.get(person_id, False), grant_fact_id=None))
    db.flush()
    if commit:
        db.commit()
        db.refresh(snapshot)
    return snapshot
