"""3.3 source-scoped normalized drawing zones and entry lines."""
from datetime import datetime, timezone
import math
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field, RootModel, StringConstraints, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth import AuthRoute
from app.business_models import BusinessSeat
from app.db import get_session
from app.models import EvalSession
from app.phase_models import PhaseBinding
from app.zone_config_models import SourceZoneConfig

router = APIRouter(route_class=AuthRoute)
Label = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
EPSILON = 1e-10


class Point(RootModel[tuple[float, float]]):
    @field_validator('root', mode='before')
    @classmethod
    def normalized_point(cls, value):
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            raise ValueError('点必须是[x,y]坐标')
        result = []
        for coordinate in value:
            if isinstance(coordinate, bool) or not isinstance(coordinate, (int, float)) or not math.isfinite(coordinate):
                raise ValueError('坐标必须是有限数值')
            if not 0 <= coordinate <= 1:
                raise ValueError('坐标须位于0到1之间')
            result.append(float(coordinate))
        return tuple(result)


class ZoneIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    zone_id: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64, pattern=r'^[A-Za-z0-9_-]+$')]
    name: Label
    kind: Literal['seat', 'restricted']
    seat_id: str | None = None
    polygon: list[Point] = Field(min_length=3, max_length=64)

    @field_validator('seat_id')
    @classmethod
    def valid_seat_id(cls, value):
        if value is not None:
            try:
                return str(UUID(value))
            except (ValueError, TypeError, AttributeError):
                raise ValueError('工位ID无效') from None
        return value

    @model_validator(mode='after')
    def validate_geometry(self):
        if (self.kind == 'seat') != (self.seat_id is not None):
            raise ValueError('工位区域必须且只能关联一个工位')
        points = [point.root for point in self.polygon]
        if len(set(points)) != len(points) or any(_same(points[i], points[(i + 1) % len(points)]) for i in range(len(points))):
            raise ValueError('多边形顶点不能重复或相邻重合')
        if abs(_signed_area(points)) <= EPSILON:
            raise ValueError('多边形面积必须大于零')
        for i in range(len(points)):
            a, b = points[i], points[(i + 1) % len(points)]
            for j in range(i + 1, len(points)):
                if j in (i, (i + 1) % len(points)) or i == (j + 1) % len(points):
                    continue
                c, d = points[j], points[(j + 1) % len(points)]
                if _segments_intersect(a, b, c, d):
                    raise ValueError('多边形边不能自相交或相切')
        return self


class EntryLineIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    line_id: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64, pattern=r'^[A-Za-z0-9_-]+$')]
    name: Label
    start: Point
    end: Point
    direction: Literal['in', 'out', 'both'] = 'both'

    @model_validator(mode='after')
    def validate_line(self):
        if _same(self.start.root, self.end.root):
            raise ValueError('出入口线起点和终点不能重合')
        return self


class ZoneConfigWrite(BaseModel):
    model_config = ConfigDict(extra='forbid')
    config_version: int = Field(strict=True, ge=0)
    zones: list[ZoneIn] = Field(max_length=128)
    entry_lines: list[EntryLineIn] = Field(max_length=32)

    @model_validator(mode='after')
    def unique_ids(self):
        identifiers = [item.zone_id for item in self.zones] + [item.line_id for item in self.entry_lines]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError('区域与出入口线ID不能重复')
        return self


def _same(left, right):
    return abs(left[0] - right[0]) <= EPSILON and abs(left[1] - right[1]) <= EPSILON


def _signed_area(points):
    return sum(points[i][0] * points[(i + 1) % len(points)][1] - points[(i + 1) % len(points)][0] * points[i][1]
        for i in range(len(points))) / 2


def _orientation(a, b, c):
    value = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
    return 0 if abs(value) <= EPSILON else 1 if value > 0 else -1


def _on_segment(a, b, point):
    return (min(a[0], b[0]) - EPSILON <= point[0] <= max(a[0], b[0]) + EPSILON
        and min(a[1], b[1]) - EPSILON <= point[1] <= max(a[1], b[1]) + EPSILON)


def _segments_intersect(a, b, c, d):
    one, two = _orientation(a, b, c), _orientation(a, b, d)
    three, four = _orientation(c, d, a), _orientation(c, d, b)
    if one * two < 0 and three * four < 0:
        return True
    return ((one == 0 and _on_segment(a, b, c)) or (two == 0 and _on_segment(a, b, d))
        or (three == 0 and _on_segment(c, d, a)) or (four == 0 and _on_segment(c, d, b)))


def _http(code, message, status):
    raise HTTPException(status, {'code': code, 'message': message}, headers={'Cache-Control': 'no-store'})


def _find_scope(db: Session, session_id: str, source_id: str, *, lock=False, allow_closed=False):
    query = select(EvalSession).where(EvalSession.session_id == session_id)
    if lock:
        query = query.with_for_update()
    current = db.scalar(query)
    if current is None:
        _http('SESSION_NOT_FOUND', '评标场次不存在', 404)
    if current.status == 'closed' and not allow_closed:
        _http('SESSION_CLOSED', '已关闭场次不能修改区域配置', 409)
    binding = db.scalar(select(PhaseBinding).where(PhaseBinding.session_id == session_id,
        PhaseBinding.source_id == source_id, PhaseBinding.active.is_(True)))
    if binding is None:
        from sqlalchemy import inspect, text
        channel = None
        if inspect(db.connection()).has_table('supervision_channels'):
            channel = db.execute(text("SELECT 1 FROM supervision_channels WHERE session_id=:session AND source_id=:source AND kind='video' AND active=true"),
                                 {'session':session_id, 'source':source_id}).first()
        if channel:
            binding = db.scalar(select(PhaseBinding).where(PhaseBinding.session_id==session_id, PhaseBinding.active.is_(True)))
    if binding is None:
        _http('SOURCE_NOT_BOUND', '视频源未绑定到该场次', 409)
    return current, binding


def _read(db: Session, binding: PhaseBinding, session_id: str, source_id: str):
    row = db.scalar(select(SourceZoneConfig).where(SourceZoneConfig.session_id == session_id,
        SourceZoneConfig.source_id == source_id))
    if row is None:
        return {'project_id': binding.project_id, 'room_id': binding.room_id, 'session_id': session_id,
            'source_id': source_id, 'config_version': 0, 'zones': [], 'entry_lines': []}
    if (row.project_id, row.room_id) != (binding.project_id, binding.room_id):
        _http('ZONE_SCOPE_MISMATCH', '区域配置作用域与当前绑定不一致', 409)
    return {'project_id': row.project_id, 'room_id': row.room_id, 'session_id': row.session_id,
        'source_id': row.source_id, 'config_version': row.config_version, 'zones': row.zones, 'entry_lines': row.entry_lines}


@router.get('/sessions/{session_id}/sources/{source_id}/zones')
def get_zone_config(session_id: UUID, source_id: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=256)],
                    response: Response, db: Session = Depends(get_session)):
    response.headers['Cache-Control'] = 'no-store'
    try:
        _, binding = _find_scope(db, str(session_id), source_id, allow_closed=True)
        return _read(db, binding, str(session_id), source_id)
    except SQLAlchemyError:
        db.rollback()
        _http('ZONE_STORAGE_UNAVAILABLE', '区域配置读取暂不可用', 503)


@router.put('/sessions/{session_id}/sources/{source_id}/zones')
def put_zone_config(session_id: UUID, source_id: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=256)],
                    body: ZoneConfigWrite, response: Response, db: Session = Depends(get_session)):
    response.headers['Cache-Control'] = 'no-store'
    sid = str(session_id)
    try:
        _, binding = _find_scope(db, sid, source_id, lock=True)
        row = db.scalar(select(SourceZoneConfig).where(SourceZoneConfig.session_id == sid,
            SourceZoneConfig.source_id == source_id).with_for_update())
        version = row.config_version if row else 0
        if body.config_version != version:
            _http('VERSION_CONFLICT', '区域配置版本已变化，请刷新后重试', 409)
        seat_ids = {zone.seat_id for zone in body.zones if zone.seat_id}
        if seat_ids:
            seats = db.scalars(select(BusinessSeat).where(BusinessSeat.seat_id.in_(seat_ids),
                BusinessSeat.project_id == binding.project_id, BusinessSeat.room_id == binding.room_id,
                BusinessSeat.enabled.is_(True))).all()
            if {seat.seat_id for seat in seats} != seat_ids:
                _http('SEAT_ROOM_MISMATCH', '所关联工位不存在、已停用或不属于该评标室', 422)
        values = {'zones': [item.model_dump(mode='json') for item in body.zones],
            'entry_lines': [item.model_dump(mode='json') for item in body.entry_lines]}
        if row is None:
            row = SourceZoneConfig(config_id=str(uuid4()), session_id=sid, project_id=binding.project_id,
                room_id=binding.room_id, source_id=source_id, config_version=1, **values)
            db.add(row)
        else:
            row.config_version += 1
            row.zones, row.entry_lines = values['zones'], values['entry_lines']
            row.updated_at = datetime.now(timezone.utc)
        db.commit()
        return _read(db, binding, sid, source_id)
    except HTTPException:
        db.rollback()
        raise
    except IntegrityError:
        db.rollback()
        _http('VERSION_CONFLICT', '区域配置已由另一请求初始化，请刷新后重试', 409)
    except SQLAlchemyError:
        db.rollback()
        _http('ZONE_STORAGE_UNAVAILABLE', '区域配置保存暂不可用', 503)
