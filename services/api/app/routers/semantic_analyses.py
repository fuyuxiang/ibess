"""Admin-only semantic analysis task API; model execution belongs to 8.3."""

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audio_models import AudioRecording
from app.db import get_session
from app.routers.participant_verification import PrivateResponseRoute
from app.semantic_models import SemanticAnalysis


router = APIRouter(prefix="/audio", route_class=PrivateResponseRoute)

REQUIRED_INPUT_FIELDS = {
    "contract_version", "analysis_id", "recording_id", "project_id", "room_id", "session_id", "stage_snapshot_id",
    "phase_id", "phase_version", "scene_id", "rule_version", "prompt_version", "target_segment_ids", "segments",
    "context", "source_id", "binding_status", "media_run_id", "asset_id", "occurred_at", "asr_model", "asr_version",
    "speaker_model", "speaker_model_version", "model_name", "model_version", "window_start_ms", "window_end_ms",
    "quality_flags",
}


class SemanticAnalysisCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    input: dict = Field(min_length=1)


def _admin(request: Request):
    admin = getattr(request.state, "admin", None)
    if not isinstance(admin, dict) or admin.get("role") != "admin" or not admin.get("username"):
        raise HTTPException(401, "请先登录管理员账号")
    return admin["username"]


def _now():
    return datetime.now(timezone.utc)


def _error(code, message, status):
    raise HTTPException(status, {"code": code, "message": message})


def _validate_input(value, recording_id, duration_ms):
    if not isinstance(value, dict) or not REQUIRED_INPUT_FIELDS.issubset(value) or set(value)-REQUIRED_INPUT_FIELDS-{'time_basis'}:
        _error("INVALID_SEMANTIC_INPUT", "语义输入字段不完整或包含未声明字段", 422)
    if value["contract_version"] != "2.0.0" or value["recording_id"] != str(recording_id):
        _error("INVALID_SEMANTIC_INPUT", "语义输入与录音作用域不一致", 422)
    if value["scene_id"] not in {"S06", "S07"}:
        _error("INVALID_SEMANTIC_INPUT", "只支持S06和S07语义场景", 422)
    for key in ('source_id','session_id'):
        field = value[key]
        if field is not None and (not isinstance(field,str) or not field.strip() or len(field)>64):
            _error('INVALID_SEMANTIC_INPUT','采集源和场次须为不超过64字符的有效标识',422)
    for key in ('model_name','model_version','rule_version'):
        field = value[key]
        if not isinstance(field,str) or not field.strip() or len(field)>256:
            _error('INVALID_SEMANTIC_INPUT','模型和规则版本须为不超过256字符的有效文本',422)
    start, end = value["window_start_ms"], value["window_end_ms"]
    if (isinstance(start, bool) or isinstance(end, bool) or not isinstance(start, int) or not isinstance(end, int)
            or start < 0 or end <= start or end > duration_ms or end - start > 60000):
        _error("INVALID_SEMANTIC_INPUT", "语义窗口时间无效", 422)
    if value["binding_status"] not in {"bound", "unbound"}:
        _error("INVALID_SEMANTIC_INPUT", "绑定状态无效", 422)
    segments = value["segments"]
    target_ids = value["target_segment_ids"]
    if not isinstance(segments, list) or not segments or not isinstance(target_ids, list) or not target_ids:
        _error("INVALID_SEMANTIC_INPUT", "语义窗口必须包含句段和目标句段", 422)
    ids = set()
    for segment in segments:
        if not isinstance(segment, dict) or set(segment) != {"segment_id", "speaker", "start_ms", "end_ms", "text"}:
            _error("INVALID_SEMANTIC_INPUT", "句段字段无效", 422)
        if (not isinstance(segment['segment_id'],str) or not segment['segment_id']
                or not isinstance(segment['text'],str) or not segment['text'].strip()
                or any(isinstance(segment[k],bool) or not isinstance(segment[k],int) for k in ('start_ms','end_ms'))):
            _error('INVALID_SEMANTIC_INPUT','句段标识、原文和时间类型无效',422)
        if segment["segment_id"] in ids:
            _error("INVALID_SEMANTIC_INPUT", "句段标识重复", 422)
        ids.add(segment["segment_id"])
        if (isinstance(segment["start_ms"], bool) or isinstance(segment["end_ms"], bool)
                or segment["start_ms"] < start or segment["end_ms"] > end
                or segment["end_ms"] <= segment["start_ms"]):
            _error("INVALID_SEMANTIC_INPUT", "句段超出语义窗口", 422)
    if any(segment_id not in ids for segment_id in target_ids) or len(set(target_ids)) != len(target_ids):
        _error("INVALID_SEMANTIC_INPUT", "目标句段不存在或重复", 422)
    context = value["context"]
    if not isinstance(context, dict) or not isinstance(context.get("applicable_rules"), list) or not context["applicable_rules"]:
        _error("INVALID_SEMANTIC_INPUT", "业务规则上下文缺失", 422)
    return value


def _task_key(value):
    normalized = deepcopy(value)
    normalized.pop("analysis_id", None)
    raw = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _iso(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if value else None


def _json(row, *, deduplicated=False):
    return {
        "analysis_id": row.analysis_id, "recording_id": row.recording_id, "task_key": row.task_key,
        "scene_id": row.scene_id, "window_start_ms": row.window_start_ms, "window_end_ms": row.window_end_ms,
        "status": row.status, "attempts": row.attempts, "request": row.request_snapshot,
        "result": row.result, "error": row.error, "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at), "completed_at": _iso(row.completed_at), "deduplicated": deduplicated,
    }


def _get(session, analysis_id):
    row = session.get(SemanticAnalysis, str(analysis_id))
    if row is None:
        _error("ANALYSIS_NOT_FOUND", "语义任务不存在", 404)
    return row


def create_analysis_core(session: Session, recording_id: str, value: dict):
    """语义任务创建核心（8.2）：校验→task_key幂等→入队。供实时面板与9.4离线质检共同复用。

    返回 (row, deduplicated)；创建即提交。
    """
    recording = session.scalar(select(AudioRecording).where(AudioRecording.recording_id == str(recording_id)))
    if recording is None:
        _error("RECORDING_NOT_FOUND", "录音不存在", 404)
    if recording.status != "ready":
        _error("RECORDING_NOT_READY", "录音尚未完成转写，不能排队语义分析", 409)
    value = _validate_input(value, str(recording_id), recording.duration_ms)
    from app.supervision_models import RecordingCaptureTime
    capture = session.get(RecordingCaptureTime,str(recording_id))
    value = deepcopy(value)
    value['time_basis'] = 'capture_start' if capture else 'unknown'
    value['occurred_at'] = _iso(capture.started_at if capture else recording.created_at)
    # 场次由已登记录音时间与业务绑定解析，多机位不会使同一场次变为歧义。
    from app.media_binding_models import MediaBinding
    from app.phase_models import PhaseBinding
    from app.core.supervision_scope import resolve_scope
    try:
        UUID(value.get('project_id') or '')
        UUID(value.get('room_id') or '')
        binding = session.query(MediaBinding).filter_by(recording_id=str(recording_id)).first()
    except (ValueError,TypeError,AttributeError):
        binding = None
    valid_business = binding and (value.get('project_id'),value.get('room_id'))==(binding.project_id,binding.room_id)
    if valid_business:
        scopes=[]
        if capture:
            for b in session.query(PhaseBinding).filter_by(project_id=binding.project_id,room_id=binding.room_id):
                scope=resolve_scope(session,b.source_id,capture.started_at,b.session_id)
                if scope:scopes.append(scope)
        unique={scope['session_id']:scope for scope in scopes}
        value['session_id']=next(iter(unique)) if len(unique)==1 else None
    task_key = _task_key(value)
    existing = session.scalar(select(SemanticAnalysis).where(SemanticAnalysis.task_key == task_key))
    if existing is not None:
        return existing, True
    now = _now()
    snapshot = deepcopy(value)
    snapshot["analysis_id"] = str(uuid4())
    row = SemanticAnalysis(analysis_id=snapshot["analysis_id"], recording_id=str(recording_id), task_key=task_key,
                           scene_id=snapshot["scene_id"], window_start_ms=snapshot["window_start_ms"],
                           window_end_ms=snapshot["window_end_ms"], status="queued", request_snapshot=snapshot,
                           attempts=0, created_at=now, updated_at=now)
    session.add(row)
    from app.media_binding_models import MediaBinding
    from app.business_models import BusinessProject, BusinessRoom
    from app.supervision_models import SemanticBusinessSnapshot
    try:
        UUID(value.get('project_id') or '')
        UUID(value.get('room_id') or '')
        binding = session.query(MediaBinding).filter_by(recording_id=str(recording_id)).first()
    except (ValueError,TypeError,AttributeError):
        binding = None
    if binding and (value.get('project_id'),value.get('room_id'))==(binding.project_id,binding.room_id):
        project,room = session.get(BusinessProject,binding.project_id),session.get(BusinessRoom,binding.room_id)
        session.flush()
        session.add(SemanticBusinessSnapshot(analysis_id=row.analysis_id,project_id=binding.project_id,room_id=binding.room_id,
            snapshot={'project_name':project.name,'room_name':room.name}))
    session.commit()
    return row, False


@router.post("/recordings/{recording_id}/semantic-analyses", status_code=202)
def create_analysis(recording_id: UUID, body: SemanticAnalysisCreate, response: Response,
                    actor: str = Depends(_admin), session: Session = Depends(get_session)):
    row, deduplicated = create_analysis_core(session, str(recording_id), deepcopy(body.input))
    response.status_code = 200 if deduplicated else 202
    return _json(row, deduplicated=deduplicated)


@router.get("/recordings/{recording_id}/semantic-analyses")
def list_analyses(recording_id: UUID, response: Response, limit: int = Query(30, ge=1, le=100),
                  actor: str = Depends(_admin), session: Session = Depends(get_session)):
    rows = session.scalars(select(SemanticAnalysis).where(SemanticAnalysis.recording_id == str(recording_id))
                           .order_by(SemanticAnalysis.created_at.desc()).limit(limit)).all()
    response.headers["Cache-Control"] = "no-store"
    from app.supervision_models import SemanticEventLink, SupervisionIntakeFailure
    links = {link.analysis_id: link.event_id for link in session.query(SemanticEventLink).filter(SemanticEventLink.analysis_id.in_([row.analysis_id for row in rows])).all()}
    errors = {r.analysis_id:r.error for r in session.query(SupervisionIntakeFailure).filter(SupervisionIntakeFailure.analysis_id.in_([row.analysis_id for row in rows])).all()}
    return {"items": [_json(row) | {'event_id': links.get(row.analysis_id),'intake_error':errors.get(row.analysis_id)} for row in rows]}


@router.post('/semantic-analyses/{analysis_id}/intake-retry')
def retry_intake(analysis_id:UUID, actor:str=Depends(_admin), session:Session=Depends(get_session)):
    from app.supervision_models import SupervisionIntakeFailure
    row = _get(session,analysis_id)
    if row.status!='ready':
        _error('ANALYSIS_NOT_READY','只有已完成任务可以重试预警接入',409)
    failure = session.get(SupervisionIntakeFailure,str(analysis_id))
    if failure:
        failure.next_retry_at = datetime.utcnow()
    session.commit()
    return {'queued':True}


@router.get("/semantic-analyses/{analysis_id}")
def get_analysis(analysis_id: UUID, response: Response, actor: str = Depends(_admin), session: Session = Depends(get_session)):
    response.headers["Cache-Control"] = "no-store"
    return _json(_get(session, analysis_id))


@router.post("/semantic-analyses/{analysis_id}/retry", status_code=202)
def retry_analysis(analysis_id: UUID, response: Response, actor: str = Depends(_admin), session: Session = Depends(get_session)):
    row = _get(session, analysis_id)
    if row.status != "failed":
        _error("ANALYSIS_NOT_FAILED", "只有失败任务可以重试", 409)
    now = _now()
    row.status = "queued"
    row.error = None
    row.result = None
    row.claim_id = None
    row.claimed_by = None
    row.claim_at = None
    row.completed_at = None
    row.updated_at = now
    session.commit()
    return _json(row)
