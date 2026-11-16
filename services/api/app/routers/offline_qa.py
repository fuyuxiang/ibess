"""离线质检运行 API（9.4 最小路径）：单文件单任务——对已绑定录音的整段转写重放 S06/S07 研判。

重跑语义：每次运行 media_run_id=offline-qa-<run_id> 使 task_key 唯一（绕过实时链路幂等），
同输入重检产生新任务。报告按 run 聚合各窗口任务结论。
"""
from datetime import datetime, timezone
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.audio_models import AudioRecording, AudioSegment
from app.db import get_session
from app.media_binding_models import MediaBinding
from app.offline_qa_models import OfflineQaRun
from app.routers.business_authz import latest_snapshot
from app.routers.semantic_analyses import create_analysis_core
from sqlalchemy import text

router = APIRouter()
QA_SCENES = ("S06", "S07")


def _actor(request: Request):
    admin = getattr(request.state, "admin", None)
    if not isinstance(admin, dict) or not admin.get("username"):
        raise HTTPException(403, "管理员身份缺失")
    return admin["username"]


class QaRunIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    recording_id: UUID
    scenes: list[str] = Field(default_factory=lambda: ["S06", "S07"])


def _build_context(session: Session, room_id: str | None, scene: str, segments: list) -> dict:
    """构造质检 context：与实时面板 assemble 完全同构（按场景的规则/例外 + 说话人槽位映射）。

    绑定评标室且有有效快照→身份绑定真实人员；否则 unbound 占位（离线重检不强制依赖快照）。
    """
    speakers = sorted({segment.speaker for segment in segments if segment.speaker})
    authz = None
    if room_id:
        try:
            authz = latest_snapshot(UUID(room_id), session)
        except HTTPException:
            authz = None
    bindings = []
    for index, speaker in enumerate(speakers):
        if authz:
            source = authz["speaker_bindings"][index % len(authz["speaker_bindings"])]
            bindings.append({"segment_speaker": speaker, "person_id": source["person_id"],
                             "display_name": source["display_name"], "role": source["role"],
                             "identity_verified": source["identity_verified"],
                             "basis_fact_id": source["basis_fact_id"]})
        else:
            bindings.append({"segment_speaker": speaker, "person_id": None, "display_name": None,
                             "role": None, "identity_verified": False, "basis_fact_id": None})
    snapshot_id = authz["context_snapshot_id"] if authz else "unbound-demo"
    if scene == "S07":
        return ({"applicable_rules": ["未公开评审信息仅可提供给有权限的对象", "保密目录内信息不得外传"],
                 "exceptions": ["已批准的必要澄清"], "speaker_bindings": bindings,
                 "recipient_authorized": authz["recipient_authorized"] if authz else None,
                 "recipient_fact_id": authz["recipient_fact_id"] if authz else None,
                 "confidentiality_policy_id": authz["confidentiality_policy_id"] if authz else None,
                 "information_visibility": authz["information_visibility"] if authz else "public"},
                snapshot_id, "bound" if authz else "unbound")
    return ({"applicable_rules": ["专家应按评分细则独立打分", "不得诱导或施压他人调整分值"],
             "exceptions": ["正常讨论与纠错", "引用他人话语"], "speaker_bindings": bindings,
             "recipient_authorized": None, "recipient_fact_id": None,
             "confidentiality_policy_id": None, "information_visibility": "public"},
            snapshot_id, "bound" if authz else "unbound")


@router.post("/offline-qa/runs", status_code=201)
def create_qa_run(body: QaRunIn, request: Request, session: Session = Depends(get_session)):
    actor = _actor(request)
    scenes = [scene for scene in dict.fromkeys(body.scenes) if scene in QA_SCENES]
    if not scenes:
        raise HTTPException(422, f"场景仅支持 {'/'.join(QA_SCENES)}")
    recording = session.get(AudioRecording, str(body.recording_id))
    if recording is None:
        raise HTTPException(404, "录音不存在")
    if recording.status != "ready":
        raise HTTPException(409, "录音尚未完成转写")
    binding = session.query(MediaBinding).filter_by(recording_id=recording.recording_id).first()
    segments = (session.query(AudioSegment).filter_by(recording_id=recording.recording_id)
                .order_by(AudioSegment.start_ms).all())
    if not segments:
        raise HTTPException(409, "该录音无转写句段，无法质检")
    run_id = str(uuid4())
    duration_ms = int(recording.duration_ms or 0)
    task_keys = []
    now = datetime.now(timezone.utc)
    room_id = str(binding.room_id) if binding else "unbound-demo"
    project_id = str(binding.project_id) if binding else "unbound-demo"
    for scene in scenes:
        context, stage_snapshot_id, binding_status = _build_context(
            session, str(binding.room_id) if binding else None, scene, segments)
        value = {
            "contract_version": "2.0.0", "analysis_id": str(uuid4()),
            "recording_id": recording.recording_id,
            "project_id": project_id, "room_id": room_id,
            "session_id": "session-001", "stage_snapshot_id": stage_snapshot_id,
            "phase_id": "DETAILED", "phase_version": "phase-v1",
            "scene_id": scene, "rule_version": f"{scene.lower()}-v1",
            "prompt_version": f"{scene.lower()}-prompt-{'v3' if scene == 'S07' else 'v1'}",
            "target_segment_ids": [segment.segment_id for segment in segments],
            "segments": [{"segment_id": segment.segment_id, "speaker": segment.speaker,
                          "start_ms": segment.start_ms, "end_ms": segment.end_ms,
                          "text": segment.text} for segment in segments],
            "context": context, "source_id": recording.source_id,
            "binding_status": binding_status, "media_run_id": f"offline-qa-{run_id}",
            "asset_id": f"asset-{recording.recording_id}", "occurred_at": recording.created_at.isoformat(),
            "asr_model": recording.model or "Paraformer", "asr_version": "funasr-v1",
            "speaker_model": recording.speaker_model or "CAM++", "speaker_model_version": "campplus-v1",
            "model_name": "MiniMax-M3.1-Flash-Preview", "model_version": "m3.1",
            "window_start_ms": 0, "window_end_ms": min(duration_ms, 60000),
            "quality_flags": ["offline_qa", "authz_snapshot"],
        }
        row, _ = create_analysis_core(session, recording.recording_id, value)
        task_keys.append(row.task_key)
    run = OfflineQaRun(run_id=run_id, recording_id=recording.recording_id, room_id=room_id,
                       scenes=scenes, task_keys=task_keys, status="queued",
                       actor=actor, created_at=now)
    session.add(run)
    session.commit()
    return {"run_id": run_id, "recording_id": recording.recording_id, "room_id": room_id,
            "scenes": scenes, "task_keys": task_keys, "status": "queued",
            "binding_status": binding_status}


def _serialize_run(session: Session, run: OfflineQaRun) -> dict:
    rows = []
    for task_key in (run.task_keys or []):
        row = session.execute(text(
            "select analysis_id, scene_id, status, result from semantic_analyses where task_key = :k"),
            {"k": task_key}).mappings().first()
        if row is None:
            rows.append({"task_key": task_key, "status": "missing"})
            continue
        result = row["result"] or {}
        rows.append({"task_key": task_key, "analysis_id": row["analysis_id"], "scene_id": row["scene_id"],
                     "status": row["status"], "conclusion": result.get("conclusion"),
                     "risk_level": result.get("risk_level"), "reason": result.get("reason")})
    ready = [row for row in rows if row.get("status") == "ready"]
    summary = {"total": len(rows),
               "pending": sum(1 for row in rows if row.get("status") in ("queued", "processing", "missing")),
               "suspected": sum(1 for row in ready if row.get("conclusion") == "suspected"),
               "normal": sum(1 for row in ready if row.get("conclusion") == "normal"),
               "insufficient": sum(1 for row in ready if row.get("conclusion") == "insufficient")}
    status = "completed" if summary["pending"] == 0 else "queued"
    return {"run_id": run.run_id, "recording_id": run.recording_id, "room_id": run.room_id,
            "scenes": run.scenes, "actor": run.actor, "status": status,
            "created_at": run.created_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "tasks": rows, "summary": summary}


@router.get("/offline-qa/runs/{run_id}")
def get_qa_run(run_id: UUID, session: Session = Depends(get_session)):
    run = session.get(OfflineQaRun, str(run_id))
    if run is None:
        raise HTTPException(404, "质检运行不存在")
    return _serialize_run(session, run)


@router.get("/offline-qa/runs")
def list_qa_runs(limit: int = 10, session: Session = Depends(get_session)):
    runs = (session.query(OfflineQaRun).order_by(OfflineQaRun.created_at.desc())
            .limit(min(max(limit, 1), 50)).all())
    return {"items": [_serialize_run(session, run) for run in runs], "total": len(runs)}
