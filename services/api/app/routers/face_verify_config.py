"""人脸核验规则配置（管理侧只配置；比对在工作台或自动链路执行）。"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from app.db import get_session

router = APIRouter(prefix='/face-verify', tags=['face-verify'])


def _admin(request: Request) -> str:
    admin = getattr(request.state, 'admin', None)
    if not isinstance(admin, dict) or admin.get('role') != 'admin' or not admin.get('username'):
        raise HTTPException(401, '请先登录管理员账号')
    return admin['username']


class ConfigIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    threshold_same: float = Field(ge=0.1, le=1.0, description='判定本人阈值（余弦相似度，模型校准值 0.363）')
    threshold_suspect: float = Field(ge=0.0, le=1.0, description='疑似阈值（余弦相似度）')
    auto_enabled: bool
    interval_minutes: int = Field(ge=1, le=60, description='自动比对周期(分钟)')


class CaptureIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    camera: str = Field(default='cam10', pattern=r'^cam\d{1,2}$', description='摄像头标识，如 cam10')


def _row(mapping) -> dict:
    # 对外用分钟：库里的 interval_seconds 是秒，AI 那边的自动比对循环直接按秒 sleep，
    # 换单位只在这一层换算，不动表结构也不动 face_auto。
    return {
        'threshold_same': float(mapping['threshold_same']),
        'threshold_suspect': float(mapping['threshold_suspect']),
        'auto_enabled': bool(mapping['auto_enabled']),
        'interval_minutes': max(1, round(int(mapping['interval_seconds']) / 60)),
        'updated_by': mapping['updated_by'],
        'updated_at': mapping['updated_at'].astimezone(timezone.utc).isoformat().replace('+00:00', 'Z') if mapping['updated_at'] else None,
    }


@router.get('/config')
def get_config(db=Depends(get_session), _admin: str = Depends(_admin)):
    row = db.execute(text("select threshold_same, threshold_suspect, auto_enabled, interval_seconds, updated_by, updated_at from face_verify_config where id = 1")).mappings().first()
    if row is None:
        raise HTTPException(500, '核验配置缺失')
    return _row(row)


@router.put('/config')
def put_config(body: ConfigIn, db=Depends(get_session), _admin: str = Depends(_admin)):
    if body.threshold_same <= body.threshold_suspect:
        raise HTTPException(422, '本人阈值必须大于疑似阈值')
    db.execute(text("""update face_verify_config set threshold_same = :same, threshold_suspect = :suspect,
        auto_enabled = :auto, interval_seconds = :interval, updated_by = :actor, updated_at = now() where id = 1"""),
        {'same': body.threshold_same, 'suspect': body.threshold_suspect, 'auto': body.auto_enabled,
         'interval': body.interval_minutes * 60, 'actor': _admin})
    db.commit()
    return get_config(db=db, _admin=_admin)


@router.get('/cameras')
def cameras(_admin: str = Depends(_admin)):
    """可抓拍的摄像头清单（演示环境 12 路虚拟摄像头）。"""
    from app import face_auto
    return {'items': [{'key': key, 'label': label} for key, label in face_auto.CAMERA_LABELS.items()],
            'default': 'cam10'}


@router.post('/capture')
def capture(body: CaptureIn, _admin: str = Depends(_admin)):
    """立即抓拍一帧并比对：返回抓拍图 + 最高相似人员 + 结论（命中则写核验记录）。"""
    from app import face_auto
    try:
        return face_auto.capture_and_match(body.camera, record=True)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f'抓拍失败：{type(exc).__name__}') from None
