from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager
import asyncio
import json
import logging
from pathlib import Path
import re
import redis

from app.db import engine, SessionLocal
from app.migrations import upgrade_schema, verify_schema
from app.schema import load_metadata
from app.models import Base
from app import business_models  # 注册独立业务配置表，保留检测服务原表
from app import business_facts_models  # 6.1 S04业务事实表（签到/换人/许可/澄清）
from app import business_fact_link_models  # 6.2 人脸核验结果与事实关联表
from app import business_authz_models  # 6.3 S04授权快照表
from app import score_models  # 9.3 评分记录表
from app import video_evidence_models  # 9.1 视频证据资产表
from app import media_binding_models  # 4.3 媒体绑定表
from app import offline_qa_models  # 9.4 离线质检运行表
from app.routers import media_clips  # 9.2 事件前后录像
from app import auth_models
from app import face_models
from app import audio_models
from app import phase_models, phase_control_models, semantic_models  # 建表前显式注册阶段、控制和语义任务表
from app import supervision_models
from app import zone_config_models
from app.routers import zone_config
from app.routers import supervision
from app.routers import session_workflow
from app.routers import supervision_workspace
from app.phase_control import PhaseControlPublisher
from app.auth import AdminAuthMiddleware, router as auth_router
from app.core.business_registry import ensure_business_seed
from app.routers import rosters
from app.routers import room_monitoring
from app.routers import business_authz
from app.routers import evidence
from app.routers import media_bindings
from app.routers import video_audits  # 历史视频审计
from app.routers import roi_zones  # ROI 区域标定
from app.routers import monitor_overlay  # 画面叠加层
from app.routers import stage_summary  # 评标阶段总览
from app.routers import offline_qa
from app.routers import acoustic_events
from app.routers import ops, ops_status
from app.routers import score_records
from app.routers import business_fact_links
from app.routers import business_facts
from app.routers import business_registry
from app.routers import faces
from app.routers import participant_verification
from app.routers import audio_recordings
from app.routers import sensitive_lexicon
from app.routers import poc, ontology, sessions, system, semantic_analyses
from app.routers import workflow_events as events
from app.routers import health
from app.routers import phase_control
from app.core.service_recovery import DatabaseRecoveryMiddleware, initialize_database
import os

# 业务日志统一走 root logger：不配这一行时，除 uvicorn 的访问日志外全部丢失，
# 自动比对、建表、种子等动作在日志里一个字都看不到，排查只能靠猜。
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)

def seed_configuration(engine):
    from app.core.face_config import ensure_face_config_seed
    ensure_business_seed(engine)
    ensure_face_config_seed(engine)
    from app.core.rule_catalog import ensure_camera_rules
    ensure_camera_rules(engine)


load_metadata()
initialize_database(engine, upgrade_schema, verify_schema, seed_configuration,
                    timeout=float(os.environ.get('API_STARTUP_TIMEOUT', '120')))

def build_phase_control_publisher():
    client = redis.from_url(os.environ.get('REDIS_URL', 'redis://localhost:6379/0'),
        socket_connect_timeout=1, socket_timeout=1, decode_responses=True)
    return PhaseControlPublisher(SessionLocal, client, poll_seconds=1, batch_size=10, close_client=True)


def load_phase_control_consumers(required_consumers):
    """读取部署方挂载的摘要与精确scope；原始服务token不进入API配置。"""
    configured = os.environ.get('POC_PHASE_CONTROL_CONSUMERS_FILE')
    if configured is None:
        return None  # 未配置时内部路由继续默认拒绝，不隐式授权。

    def unique_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    try:
        path = Path(configured)
        if not path.is_absolute():
            raise ValueError
        with path.open('rb') as file:
            raw = file.read(256 * 1024 + 1)
        if len(raw) > 256 * 1024:
            raise ValueError
        mapping = json.loads(raw.decode('utf-8'), object_pairs_hook=unique_keys)
        if not isinstance(mapping, dict) or set(mapping) != set(required_consumers):
            raise ValueError
        digests = set()
        for name, spec in mapping.items():
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', name):
                raise ValueError
            if not isinstance(spec, dict) or set(spec) != {'token_sha256', 'scopes'}:
                raise ValueError
            digest = spec['token_sha256']
            if not isinstance(digest, str) or not re.fullmatch(r'[a-f0-9]{64}', digest) or digest in digests:
                raise ValueError
            digests.add(digest)
            scopes = spec['scopes']
            if not isinstance(scopes, list):
                raise ValueError
            scope_keys = ('project_id', 'room_id', 'session_id', 'source_id')
            seen = set()
            for scope in scopes:
                if not isinstance(scope, dict) or set(scope) != set(scope_keys):
                    raise ValueError
                for key in scope_keys:
                    value = scope[key]
                    if (not isinstance(value, str) or not value or value != value.strip()
                            or len(value) > (256 if key == 'source_id' else 36)
                            or '*' in value or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value)):
                        raise ValueError
                identity = tuple(scope[key] for key in scope_keys)
                if identity in seen:
                    raise ValueError
                seen.add(identity)
        return mapping
    except (OSError, ValueError, TypeError, RecursionError):
        # 固定异常文案，不回显路径、配置正文、摘要或令牌。
        raise RuntimeError('阶段控制消费者凭据配置无效；code=CONSUMER_CONFIG_INVALID') from None


async def semantic_supervision_loop():
    from app.core.supervision import sync_semantic_events
    from app.core.workflow import publish_event
    def intake():
        with SessionLocal() as db:
            created = sync_semantic_events(db)
            db.commit()
            for event in created:
                publish_event(event, db)
    while True:
        try:
            await asyncio.to_thread(intake)
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.getLogger('supervision').exception('语义线索接入暂不可用，将自动重试')
        await asyncio.sleep(5)


async def media_preservation_loop():
    from app.core.media_preservation import preserve_due_events
    while True:
        try:
            await asyncio.to_thread(preserve_due_events,SessionLocal)
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.getLogger('supervision').exception('录像保全任务暂不可用，将重试')
        await asyncio.sleep(5)


async def coverage_sampling_loop():
    from app.core.supervision_reporting import sample_coverage
    while True:
        try:
            await asyncio.to_thread(sample_coverage,SessionLocal)
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.getLogger('supervision').exception('监测覆盖采样暂不可用，未采样区间保持待核验')
        await asyncio.sleep(30)


@asynccontextmanager
async def phase_control_lifespan(app):
    from app import face_auto
    face_task = asyncio.create_task(face_auto.loop())
    app.state.face_auto_task = face_task
    task = asyncio.create_task(semantic_supervision_loop())
    media_task = asyncio.create_task(media_preservation_loop())
    coverage_task = asyncio.create_task(coverage_sampling_loop())
    try:
        async with control_lifespan(app):
            yield
    finally:
        task.cancel()
        media_task.cancel()
        coverage_task.cancel()
        try:
            await coverage_task
        except asyncio.CancelledError:
            pass
        try:
            await media_task
        except asyncio.CancelledError:
            pass
        if face_task:
            face_task.cancel()
            try:
                await face_task
            except asyncio.CancelledError:
                pass
        try:
            await task
        except asyncio.CancelledError:
            pass


@asynccontextmanager
async def control_lifespan(app):
    # 人脸自动抓拍 worker：按「规则配置」周期抓帧比对（开关在 face_verify_config）
    app.state.phase_control_publisher = None
    app.state.phase_control_required_consumers = None
    app.state.phase_control_consumers = None
    enabled = os.environ.get('POC_PHASE_CONTROL_ENABLED','false').strip().lower()
    if enabled not in ('true','false'):
        raise RuntimeError('POC_PHASE_CONTROL_ENABLED须为true或false')
    if enabled == 'false':
        yield
        return
    # 消费者必需集合由服务端设置；不提供默认减项。
    raw = os.environ.get('POC_PHASE_CONTROL_REQUIRED_CONSUMERS')
    try:
        consumers = json.loads(raw)
        if (not isinstance(consumers,list) or not consumers
                or any(not isinstance(c,str) or not c.strip() or len(c)>128 for c in consumers)
                or len(set(consumers)) != len(consumers)):
            raise ValueError
    except (ValueError, TypeError):
        raise RuntimeError('阶段控制必需消费者配置无效') from None
    credentials = load_phase_control_consumers(consumers)
    worker = None
    try:
        app.state.phase_control_required_consumers = sorted(consumers)
        app.state.phase_control_consumers = credentials
        worker = build_phase_control_publisher()
        app.state.phase_control_publisher = worker
        worker.start()
        yield
    finally:
        # 先撤销写配置和服务授权，再等待线程退出；异常路径同样清理。
        app.state.phase_control_required_consumers = None
        app.state.phase_control_consumers = None
        if worker is not None:
            stopped = await asyncio.to_thread(worker.stop, timeout=30)
            if not stopped:
                logging.getLogger('poc.phase-control').error('控制发布循环停止超时；code=PUBLISHER_STOP_TIMEOUT')


app = FastAPI(title="智能评标监管系统 API", version="0.6.0", docs_url=None, redoc_url=None, openapi_url=None,
    lifespan=phase_control_lifespan)
app.add_middleware(AdminAuthMiddleware)
app.add_middleware(DatabaseRecoveryMiddleware)
app.include_router(health.router)
app.include_router(ops.router, prefix="/api", tags=["ops"])
app.include_router(auth_router, prefix="/api", tags=["administrator"])

app.include_router(system.router, prefix="/api/system", tags=["system"])
app.include_router(events.router, prefix="/api", tags=["events"])
app.include_router(supervision.router, prefix="/api")
app.include_router(supervision_workspace.router, prefix="/api")
app.include_router(session_workflow.router, prefix="/api")
app.include_router(poc.router, prefix="/api", tags=["poc-workflow"])
app.include_router(sessions.router, prefix="/api", tags=["sessions"])
app.include_router(ontology.router, prefix="/api", tags=["ontology"])
app.include_router(business_registry.router, prefix="/api", tags=["business-registry"])
app.include_router(business_facts.router, prefix="/api", tags=["business-facts"])
app.include_router(business_fact_links.router, prefix="/api", tags=["business-fact-links"])
app.include_router(business_authz.router, prefix="/api", tags=["business-authz"])
app.include_router(rosters.router, prefix="/api")
app.include_router(room_monitoring.router, prefix="/api")
app.include_router(evidence.router, prefix="/api", tags=["evidence-9-1"])
app.include_router(media_bindings.router, prefix="/api", tags=["media-bindings-4-3"])
app.include_router(video_audits.router, prefix="/api", tags=["video-audits"])
app.include_router(roi_zones.router, prefix="/api", tags=["roi"])
app.include_router(zone_config.router, prefix="/api", tags=["source-zones"])
app.include_router(monitor_overlay.router, prefix="/api", tags=["monitor-overlay"])
app.include_router(stage_summary.router, prefix="/api", tags=["stages"])
app.include_router(offline_qa.router, prefix="/api", tags=["offline-qa-9-4"])
app.include_router(acoustic_events.router, prefix="/api", tags=["acoustic-7-4"])
app.include_router(ops_status.router, prefix="/api", tags=["ops-status"])
app.include_router(media_clips.router, prefix="/api", tags=["media-clips-9-2"])
app.include_router(score_records.router, prefix="/api", tags=["score-records-9-3"])
app.include_router(faces.router, prefix="/api", tags=["face-match"])
app.include_router(participant_verification.router, prefix="/api", tags=["participant-verification"])
app.include_router(audio_recordings.router, prefix="/api", tags=["audio-recordings"])
app.include_router(semantic_analyses.router, prefix="/api", tags=["semantic-analyses"])
app.include_router(sensitive_lexicon.router, prefix="/api", tags=["sensitive-lexicon"])
app.include_router(phase_control.router, tags=['phase-control'])
from app.routers import face_verify_config as face_verify_config_router
app.include_router(face_verify_config_router.router, prefix='/api', tags=['face-verify'])


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    # 校验错误只返回位置及原因，不回显密码、令牌或请求正文。
    errors = [{"loc": error["loc"], "msg": error["msg"], "type": error["type"]} for error in exc.errors()]
    return JSONResponse({"detail": errors}, status_code=422, headers={"Cache-Control": "no-store"})


@app.get("/")
def root():
    return {"service": "poc-api", "status": "ok"}
