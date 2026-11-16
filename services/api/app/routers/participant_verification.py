"""绑定人员的限时核验授权；人员端只可提交候选，底照由管理员维护。"""
import hashlib
import re
import secrets
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response, UploadFile
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.business_models import BusinessParticipant, BusinessPerson, BusinessProject, BusinessRoom
from app.core.face_match import FaceMatchError, get_face_engine
from app.db import get_session
from app.face_models import FaceProfile, PersonVerificationLink, PersonVerificationResult
from app.routers.faces import iso, person, profile, sample


class PrivateResponseRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()
        async def run(request):
            try:
                response = await handler(request)
                response.headers['Cache-Control'] = 'no-store'
                return response
            except HTTPException as exc:
                exc.headers = {**(exc.headers or {}), 'Cache-Control': 'no-store'}
                raise
        return run


router = APIRouter(route_class=PrivateResponseRoute)


class NewLink(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expires_in_minutes: int = Field(default=60, ge=5, le=1440)
    project_id: UUID | None = None
    room_id: UUID | None = None


def now():
    return datetime.now(timezone.utc)


def binding(session, person_id, project_id, room_id, *, public=False):
    code = 410 if public else 422
    if room_id and not project_id:
        raise HTTPException(code, '选择评标室前请先指定所属项目')
    project = room = None
    if project_id:
        project = session.get(BusinessProject, str(project_id), populate_existing=True)
        member = session.scalar(select(BusinessParticipant).where(
            BusinessParticipant.project_id == str(project_id), BusinessParticipant.person_id == str(person_id)))
        if project is None or project.status == 'closed' or member is None:
            raise HTTPException(code, '人员与项目的关联无效，请联系管理员检查配置')
    if room_id:
        room = session.get(BusinessRoom, str(room_id), populate_existing=True)
        if room is None or not room.enabled or room.project_id != str(project_id):
            raise HTTPException(code, '评标室配置无效，请联系管理员检查配置')
    return project, room


def link_status(session, link):
    if link.revoked_at:
        return 'revoked'
    if link.expires_at <= now():
        return 'expired'
    identity = session.get(BusinessPerson, link.person_id, populate_existing=True)
    reference = profile(session, link.person_id)
    if identity is None or not identity.enabled or reference is None or reference.version != link.reference_version:
        return 'invalidated'
    try:
        binding(session, link.person_id, link.project_id, link.room_id, public=True)
    except HTTPException:
        return 'invalidated'
    if link.completed_at:
        return 'completed'
    return 'exhausted' if link.attempts >= link.max_attempts else 'active'


def link_json(session, link):
    return {'link_id': link.link_id, 'person_id': link.person_id, 'created_at': iso(link.created_at),
            'expires_at': iso(link.expires_at), 'status': link_status(session, link),
            'attempts': link.attempts, 'max_attempts': link.max_attempts,
            'project_id': link.project_id, 'project_name': link.project_name,
            'room_id': link.room_id, 'room_name': link.room_name}


def result_json(row):
    return {'result_id': row.result_id, 'link_id': row.link_id, 'status': row.status,
            'matched': row.matched, 'similarity': row.similarity, 'threshold': row.threshold,
            'checked_at': iso(row.checked_at), 'person_name': row.person_name,
            'project_name': row.project_name, 'room_name': row.room_name,
            'model': row.model, 'reference_version': row.reference_version,
            'error': row.error, 'liveness_checked': False}


@router.post('/faces/persons/{person_id}/verification-links')
def create_link(person_id: UUID, body: NewLink, session: Session = Depends(get_session)):
    person(session, person_id, active=True, lock=True)
    reference = profile(session, person_id)
    if reference is None:
        raise HTTPException(409, '请先由管理员登记人脸底照')
    project, room = binding(session, person_id, body.project_id, body.room_id)
    created = now()
    # 每名人员保留一个未完成入口，重新生成使旧入口失效。
    old = session.scalars(select(PersonVerificationLink).where(
        PersonVerificationLink.person_id == str(person_id),
        PersonVerificationLink.revoked_at.is_(None), PersonVerificationLink.completed_at.is_(None)).with_for_update()).all()
    for row in old:
        row.revoked_at = created
    token = secrets.token_urlsafe(32)
    row = PersonVerificationLink(link_id=str(uuid4()), person_id=str(person_id),
        token_hash=hashlib.sha256(token.encode('ascii')).hexdigest(), reference_version=reference.version,
        project_id=str(body.project_id) if body.project_id else None,
        room_id=str(body.room_id) if body.room_id else None,
        project_name=project.name if project else None, room_name=room.name if room else None,
        created_at=created, expires_at=created + timedelta(minutes=body.expires_in_minutes), attempts=0, max_attempts=5)
    session.add(row)
    result = {**link_json(session, row), 'verification_url': '/verify#' + token}
    session.commit()
    return result


@router.get('/faces/persons/{person_id}/verification-links')
def list_links(person_id: UUID, session: Session = Depends(get_session)):
    person(session, person_id)
    rows = session.scalars(select(PersonVerificationLink).where(PersonVerificationLink.person_id == str(person_id))
                           .order_by(PersonVerificationLink.created_at.desc()).limit(20)).all()
    return {'items': [link_json(session, row) for row in rows]}


@router.delete('/faces/verification-links/{link_id}')
def revoke_link(link_id: UUID, session: Session = Depends(get_session)):
    row = session.get(PersonVerificationLink, str(link_id))
    if row is None:
        raise HTTPException(404, '核验链接不存在')
    person(session, row.person_id, lock=True)
    row = session.scalar(select(PersonVerificationLink).where(PersonVerificationLink.link_id == str(link_id))
                         .with_for_update().execution_options(populate_existing=True))
    if row is None:
        raise HTTPException(404, '核验链接不存在')
    row.revoked_at = now()
    session.commit()
    return {'ok': True}


@router.get('/faces/persons/{person_id}/verification-results')
def list_results(person_id: UUID, session: Session = Depends(get_session)):
    person(session, person_id)
    rows = session.scalars(select(PersonVerificationResult).where(PersonVerificationResult.person_id == str(person_id))
                           .order_by(PersonVerificationResult.checked_at.desc()).limit(30)).all()
    return {'items': [result_json(row) for row in rows]}


def authorized_link(request, session, *, lock=False):
    header = request.headers.get('authorization', '')
    match = re.fullmatch(r'Bearer ([A-Za-z0-9_-]{43})', header, flags=re.IGNORECASE)
    if not match:
        raise HTTPException(404, '核验入口无效，请使用管理员提供的完整链接')
    digest = hashlib.sha256(match.group(1).encode('ascii')).hexdigest()
    row = session.scalar(select(PersonVerificationLink).where(PersonVerificationLink.token_hash == digest))
    if row is None:
        raise HTTPException(404, '核验入口无效，请联系管理员重新生成')
    identity = person(session, row.person_id, lock=lock)
    if lock:
        row = session.scalar(select(PersonVerificationLink).where(PersonVerificationLink.token_hash == digest)
                             .with_for_update().execution_options(populate_existing=True))
        if row is None:
            raise HTTPException(404, '核验入口已失效')
    if row.revoked_at or row.expires_at <= now() or not identity.enabled:
        raise HTTPException(410, '核验链接已过期、撤销或人员已停用，请联系管理员')
    reference = profile(session, row.person_id)
    if reference is None or reference.version != row.reference_version:
        raise HTTPException(409, '登记底照已变化，请联系管理员重新生成核验链接')
    binding(session, row.person_id, row.project_id, row.room_id, public=True)
    return row, identity, reference


@router.get('/participant-verification/context')
def public_context(request: Request, session: Session = Depends(get_session)):
    link, identity, reference = authorized_link(request, session)
    state = 'completed' if link.completed_at else 'exhausted' if link.attempts >= link.max_attempts else 'active'
    last = session.scalar(select(PersonVerificationResult).where(PersonVerificationResult.link_id == link.link_id)
                          .order_by(PersonVerificationResult.checked_at.desc()).limit(1))
    ready, message = True, '请拍摄单人清晰照片进行核验'
    try:
        engine = get_face_engine()
        if reference.model != engine.model:
            ready, message = False, '人脸模型已更新，请联系管理员重新登记底照并生成入口'
    except FaceMatchError as exc:
        ready, message = False, exc.message
    if state == 'completed':
        message = '本次核验已完成，结果已提交后台'
    elif state == 'exhausted':
        message = '核验次数已用完，请联系管理员重新生成入口'
    return {'person': {'name': identity.name, 'code': identity.code, 'unit': identity.unit},
            'project_name': link.project_name, 'room_name': link.room_name,
            'expires_at': iso(link.expires_at), 'status': state,
            'remaining_attempts': max(0, link.max_attempts - link.attempts),
            'model_ready': ready, 'message': message, 'can_verify': ready and state == 'active',
            'last_result': result_json(last) if last else None}


@router.post('/participant-verification/submit')
def public_submit(request: Request, file: UploadFile, session: Session = Depends(get_session)):
    link, identity, reference = authorized_link(request, session, lock=True)
    if link.completed_at:
        raise HTTPException(409, '本次核验已完成，请勿重复提交')
    if link.attempts >= link.max_attempts:
        raise HTTPException(429, '核验次数已用完，请联系管理员')
    try:
        engine = get_face_engine()
    except FaceMatchError as exc:
        raise HTTPException(exc.status_code, exc.message) from None
    if reference.model != engine.model:
        raise HTTPException(409, '人脸模型已更新，请联系管理员重新登记底照并生成入口')
    link.attempts += 1
    record = PersonVerificationResult(result_id=str(uuid4()), link_id=link.link_id, person_id=link.person_id,
        status='invalid', matched=None, similarity=None, threshold=engine.threshold, checked_at=now(),
        person_name=identity.name, project_name=link.project_name, room_name=link.room_name,
        model=engine.model, reference_version=reference.version, error=None)
    try:
        extracted = sample(engine, file)
        score = engine.compare(reference.embedding, extracted.embedding)
    except (HTTPException, FaceMatchError) as exc:
        if exc.status_code not in (413, 422):
            raise HTTPException(exc.status_code, exc.detail if isinstance(exc, HTTPException) else exc.message) from None
        detail = (exc.detail if isinstance(exc, HTTPException) else exc.message)[:512]
        record.error = detail
        session.add(record)
        session.commit()
        raise HTTPException(exc.status_code, detail) from None
    if link.expires_at <= now():
        raise HTTPException(410, '核验链接已过期，请联系管理员重新生成')
    binding(session, link.person_id, link.project_id, link.room_id, public=True)
    record.checked_at = now()
    record.matched, record.similarity = score >= engine.threshold, round(score, 6)
    record.status = 'matched' if record.matched else 'unmatched'
    if record.matched:
        link.completed_at = record.checked_at
    session.add(record)
    result = result_json(record)
    session.commit()
    return result
