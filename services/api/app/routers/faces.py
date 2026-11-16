from datetime import datetime, timezone
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Response, UploadFile
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.business_models import BusinessPerson
from app.face_models import PersonVerificationResult
from app.core.face_match import FaceMatchError, MAX_FILE_BYTES, MODEL_VERSION, get_face_engine
from app.db import get_session
from app.face_models import FaceProfile

router = APIRouter(prefix='/faces')


def _verdict(session: Session, similarity: float) -> tuple[str, bool, float, float]:
    """按「规则配置」阈值判定：matched / suspect / unmatched（余弦相似度口径）。"""
    row = session.execute(text(
        'select threshold_same, threshold_suspect from face_verify_config where id = 1')).mappings().first()
    if row is None:
        from app.core.face_match import get_face_engine
        same = float(get_face_engine().threshold)
        suspect = max(0.0, same - 0.08)
    else:
        same = float(row['threshold_same'])
        suspect = float(row['threshold_suspect'])
    if similarity >= same:
        return 'matched', True, same, suspect
    if similarity >= suspect:
        return 'suspect', False, same, suspect
    return 'unmatched', False, same, suspect


def iso(value):
    return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def person(session, person_id, *, active=False, lock=False):
    query = select(BusinessPerson).where(BusinessPerson.person_id == str(person_id)).execution_options(populate_existing=True)
    if lock:
        query = query.with_for_update()
    row = session.scalar(query)
    if row is None:
        raise HTTPException(404, '人员不存在')
    if active and not row.enabled:
        raise HTTPException(422, '人员已停用，不能登记或核验人脸')
    return row


def profile(session, person_id):
    return session.scalar(select(FaceProfile).where(FaceProfile.person_id == str(person_id)).execution_options(populate_existing=True))


def metadata(person_id, row):
    return {'person_id': str(person_id), 'registered': row is not None,
            'registered_at': iso(row.registered_at) if row else None,
            'model': row.model if row else None,
            'photo_url': f'/api/faces/persons/{person_id}/photo' if row else None,
            'version': row.version if row else None}


def ready_engine():
    try:
        return get_face_engine()
    except FaceMatchError as exc:
        raise HTTPException(exc.status_code, exc.message) from None


def sample(engine, file):
    try:
        data = file.file.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            raise HTTPException(413, '图片不能超过 5 MB')
        return engine.extract(data)
    except FaceMatchError as exc:
        raise HTTPException(exc.status_code, exc.message) from None
    finally:
        file.file.close()


@router.get('/status')
def status(response: Response):
    response.headers['Cache-Control'] = 'no-store'
    try:
        engine = get_face_engine()
        return {'ready': True, 'model': engine.model, 'threshold': engine.threshold, 'message': '人脸匹配模型已就绪'}
    except FaceMatchError as exc:
        return {'ready': False, 'model': MODEL_VERSION, 'threshold': 0.363, 'message': exc.message}


@router.get('/persons/{person_id}')
def get_profile(person_id: UUID, response: Response, session: Session = Depends(get_session)):
    response.headers['Cache-Control'] = 'no-store'
    person(session, person_id)
    return metadata(person_id, profile(session, person_id))


@router.get('/persons/{person_id}/photo')
def get_photo(person_id: UUID, session: Session = Depends(get_session)):
    person(session, person_id)
    row = profile(session, person_id)
    if row is None:
        raise HTTPException(404, '尚未登记人脸照片')
    return Response(row.photo, media_type='image/jpeg', headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})


@router.put('/persons/{person_id}')
def register(person_id: UUID, file: UploadFile, response: Response, session: Session = Depends(get_session), engine=Depends(ready_engine)):
    person(session, person_id, active=True)
    extracted = sample(engine, file)
    person(session, person_id, active=True, lock=True)
    row = profile(session, person_id)
    if row is None:
        row = FaceProfile(person_id=str(person_id))
        session.add(row)
    row.version, row.model = str(uuid4()), engine.model
    row.registered_at = datetime.now(timezone.utc)
    row.embedding, row.photo = extracted.embedding.tolist(), extracted.photo
    result = metadata(person_id, row)
    session.commit()
    response.headers['Cache-Control'] = 'no-store'
    return result


@router.delete('/persons/{person_id}')
def delete_profile(person_id: UUID, response: Response, session: Session = Depends(get_session)):
    person(session, person_id, lock=True)
    row = profile(session, person_id)
    if row:
        session.delete(row)
    session.commit()
    response.headers['Cache-Control'] = 'no-store'
    return {'ok': True}


@router.post('/persons/{person_id}/verify')
def verify(person_id: UUID, file: UploadFile, response: Response, record: bool = False, session: Session = Depends(get_session), engine=Depends(ready_engine)):
    person(session, person_id, active=True)
    row = profile(session, person_id)
    if row is None:
        raise HTTPException(409, '请先为该人员登记人脸照片')
    if row.model != engine.model:
        raise HTTPException(409, '人脸模型已更新，请重新登记照片')
    version, embedding = row.version, list(row.embedding)
    extracted = sample(engine, file)
    current_person = person(session, person_id, active=True, lock=True)
    current = profile(session, person_id)
    if current is None or current.version != version:
        raise HTTPException(409, '登记照片已发生变化，请重新核验')
    try:
        similarity = engine.compare(embedding, extracted.embedding)
    except FaceMatchError as exc:
        raise HTTPException(exc.status_code, exc.message) from None
    response.headers['Cache-Control'] = 'no-store'
    status, matched, threshold_same, threshold_suspect = _verdict(session, similarity)
    if record:
        session.add(PersonVerificationResult(
            result_id=str(uuid4()), link_id=None, person_id=str(person_id),
            status=status, matched=matched, similarity=round(similarity, 6),
            threshold=threshold_same, checked_at=datetime.now(timezone.utc),
            person_name=current_person.name, project_name=None, room_name=None,
            model=engine.model, reference_version=version, error=None))
        session.commit()
    return {'matched': matched, 'status': status, 'similarity': round(similarity, 6),
            'threshold': threshold_same, 'threshold_suspect': threshold_suspect,
            'person_id': str(person_id), 'person_name': current_person.name,
            'model': engine.model, 'checked_at': iso(datetime.now(timezone.utc)),
            'reference_version': version, 'liveness_checked': False}
