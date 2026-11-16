"""S04事实与人脸核验结果关联 API（6.2）。

关联约束：一条事实至多关联一条核验结果（唯一）；核验结果必须属于事实登记的同一人员。
全部端点走管理员认证/CSRF中间件。路径统一挂在 /api/fact-links 下，避免与 /api/facts/{fact_id} 冲突。
"""
from datetime import datetime, timezone
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session, joinedload

from app.business_fact_link_models import BusinessFactVerification
from app.business_facts_models import BusinessAttendanceFact
from app.db import get_session
from app.face_models import PersonVerificationResult

router = APIRouter()


class VerificationLinkIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    result_id: UUID


def _actor(request: Request):
    admin = getattr(request.state, "admin", None)
    if not isinstance(admin, dict) or not admin.get("username"):
        raise HTTPException(403, "管理员身份缺失，无法操作人脸核验关联")
    return admin["username"]


def _fact(session, fact_id, message="业务事实不存在"):
    fact = session.get(BusinessAttendanceFact, str(fact_id))
    if fact is None:
        raise HTTPException(404, message)
    return fact


def _serialize(link: BusinessFactVerification) -> dict:
    result = link.result
    return {
        "link_id": link.link_id, "fact_id": link.fact_id, "result_id": link.result_id,
        "actor": link.actor,
        "created_at": link.created_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "status": result.status if result else None,
        "matched": result.matched if result else None,
        "similarity": result.similarity if result else None,
        "checked_at": (result.checked_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if result and result.checked_at else None),
        "person_name": result.person_name if result else None,
        "model": result.model if result else None,
    }


@router.get("/fact-links")
def list_links(project_id: UUID, session: Session = Depends(get_session)):
    """按项目返回已关联映射：{fact_id: 关联信息}，供事实列表合并展示。"""
    rows = (session.query(BusinessFactVerification)
            .options(joinedload(BusinessFactVerification.result))
            .join(BusinessAttendanceFact, BusinessAttendanceFact.fact_id == BusinessFactVerification.fact_id)
            .filter(BusinessAttendanceFact.project_id == str(project_id)).all())
    return {"links": {row.fact_id: _serialize(row) for row in rows}}


@router.get("/fact-links/{fact_id}/candidates")
def candidates(fact_id: UUID, limit: int = 5, session: Session = Depends(get_session)):
    """返回该事实登记人员的最近核验结果，供人工选择关联。"""
    fact = _fact(session, fact_id)
    if fact.person_id is None:
        raise HTTPException(422, "该事实未登记人员，无法关联人脸核验结果")
    rows = (session.query(PersonVerificationResult)
            .filter(PersonVerificationResult.person_id == fact.person_id)
            .order_by(PersonVerificationResult.checked_at.desc()).limit(min(max(limit, 1), 20)).all())
    return {"items": [{"result_id": row.result_id, "status": row.status, "matched": row.matched,
                       "similarity": row.similarity, "checked_at": row.checked_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                       "person_name": row.person_name, "model": row.model} for row in rows]}


@router.post("/fact-links/auto-link")
def auto_link(request: Request, project_id: UUID, session: Session = Depends(get_session)):
    """为项目内未关联且已登记人员的 check_in/change 事实，自动关联该人最近一次 matched 核验。"""
    actor = _actor(request)
    facts = (session.query(BusinessAttendanceFact)
             .filter(BusinessAttendanceFact.project_id == str(project_id),
                     BusinessAttendanceFact.fact_type.in_(("check_in", "change")),
                     BusinessAttendanceFact.person_id.isnot(None)).all())
    linked_ids = {row.fact_id for row in session.query(BusinessFactVerification).all()}
    linked = 0
    skipped_no_result = 0
    for fact in facts:
        if fact.fact_id in linked_ids:
            continue
        result = (session.query(PersonVerificationResult)
                  .filter(PersonVerificationResult.person_id == fact.person_id,
                          PersonVerificationResult.status == "matched")
                  .order_by(PersonVerificationResult.checked_at.desc()).first())
        if result is None:
            skipped_no_result += 1
            continue
        session.add(BusinessFactVerification(link_id=str(uuid4()), fact_id=fact.fact_id,
                                             result_id=result.result_id, actor=actor,
                                             created_at=datetime.now(timezone.utc)))
        linked += 1
    session.commit()
    return {"linked": linked, "skipped_no_result": skipped_no_result}


@router.post("/fact-links/{fact_id}", status_code=201)
def create_link(fact_id: UUID, body: VerificationLinkIn, request: Request, session: Session = Depends(get_session)):
    fact = _fact(session, fact_id)
    if fact.person_id is None:
        raise HTTPException(422, "该事实未登记人员，无法关联人脸核验结果")
    result = session.get(PersonVerificationResult, str(body.result_id))
    if result is None:
        raise HTTPException(422, "人脸核验结果不存在")
    if result.person_id != fact.person_id:
        raise HTTPException(422, "核验结果与事实登记人员不一致，禁止关联")
    existing = session.query(BusinessFactVerification).filter_by(fact_id=str(fact.fact_id)).first()
    if existing is not None:
        raise HTTPException(409, "该事实已关联人脸核验结果，请先撤销原关联")
    link = BusinessFactVerification(link_id=str(uuid4()), fact_id=str(fact.fact_id),
                                    result_id=str(body.result_id), actor=_actor(request),
                                    created_at=datetime.now(timezone.utc))
    session.add(link)
    session.commit()
    session.refresh(link)
    return _serialize(link)


@router.get("/fact-links/{fact_id}")
def get_link(fact_id: UUID, session: Session = Depends(get_session)):
    link = session.query(BusinessFactVerification).options(joinedload(BusinessFactVerification.result)).filter_by(fact_id=str(fact_id)).first()
    if link is None:
        raise HTTPException(404, "该事实尚未关联人脸核验结果")
    return _serialize(link)


@router.delete("/fact-links/{fact_id}")
def delete_link(fact_id: UUID, session: Session = Depends(get_session)):
    link = session.query(BusinessFactVerification).filter_by(fact_id=str(fact_id)).first()
    if link is None:
        raise HTTPException(404, "该事实尚未关联人脸核验结果")
    session.delete(link)
    session.commit()
    return {"ok": True}
