"""Reusable room resources with immutable revisions; writes remain administrator-only."""

from copy import deepcopy
from typing import Literal
from uuid import UUID, uuid4
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from typing import Annotated
from sqlalchemy.orm import Session
from app.db import get_session
from app.business_models import BusinessRoom, BusinessSeat
from app.room_monitoring_models import RoomMonitoringPreset
from app.core.supervision_scope import normalize_source
from app.routers.zone_config import ZoneIn, EntryLineIn

router = APIRouter(prefix="/supervision/room-presets", tags=["room-monitoring"])
Text = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)
]
Source = Annotated[
    str, StringConstraints(strip_whitespace=True, pattern=r"^[A-Za-z0-9_-]{1,64}$")
]


class ChannelIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: Source
    name: Text
    kind: Literal["video", "audio"] = "video"
    zones: list[ZoneIn] = Field(default_factory=list, max_length=128)
    entry_lines: list[EntryLineIn] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def unique_geometry(self):
        ids = [z.zone_id for z in self.zones] + [
            line.line_id for line in self.entry_lines
        ]
        seats = [z.seat_id for z in self.zones if z.seat_id]
        if (
            len(ids) != len(set(ids))
            or len(seats) != len(set(seats))
            or (self.kind == "audio" and ids)
        ):
            raise ValueError("区域、门线或工位不能重复，拾音通道不能配置画面区域")
        return self


class PresetIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=0, strict=True)
    scheme: Literal["basic", "standard"] = "basic"
    primary_source_id: Source
    channels: list[ChannelIn] = Field(min_length=1, max_length=50)
    note: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=2, max_length=500)
    ]

    @model_validator(mode="after")
    def valid_sources(self):
        ids = [normalize_source(c.source_id) for c in self.channels]
        if len(ids) != len(set(ids)) or not any(
            c.source_id == self.primary_source_id and c.kind == "video"
            for c in self.channels
        ):
            raise ValueError("通道及其别名不能重复，主通道必须是已配置的视频机位")
        return self


def serialize(row):
    return {
        "preset_id": row.preset_id,
        "room_id": row.room_id,
        "version": row.version,
        "scheme": row.scheme,
        "primary_source_id": row.primary_source_id,
        "channels": deepcopy(row.channels),
        "note": row.note,
        "created_by": row.created_by,
        "created_at": row.created_at.isoformat(),
    }


def validate_seats(db, room, channels):
    ids = {
        z["seat_id"] for c in channels for z in c.get("zones", []) if z.get("seat_id")
    }
    if (
        ids
        and {
            s.seat_id
            for s in db.query(BusinessSeat).filter(
                BusinessSeat.seat_id.in_(ids),
                BusinessSeat.room_id == room.room_id,
                BusinessSeat.project_id == room.project_id,
                BusinessSeat.enabled.is_(True),
            )
        }
        != ids
    ):
        raise HTTPException(422, "资源包含其他评标室或已停用的工位，请重新标定")


def find_preset(db, preset_id, room, *, lock=False):
    query = db.query(RoomMonitoringPreset).filter_by(
        preset_id=str(preset_id), room_id=room.room_id
    )
    row = (query.with_for_update() if lock else query).first()
    if not row:
        raise HTTPException(422, "所选资源版本不存在或不属于本评标室")
    validate_seats(db, room, row.channels)
    return row


@router.get("/{room_id}")
def versions(room_id: UUID, db: Session = Depends(get_session)):
    if not db.get(BusinessRoom, str(room_id)):
        raise HTTPException(404, "评标室不存在")
    return {
        "items": [
            serialize(row)
            for row in db.query(RoomMonitoringPreset)
            .filter_by(room_id=str(room_id))
            .order_by(RoomMonitoringPreset.version.desc())
            .all()
        ]
    }


@router.post("/{room_id}", status_code=201)
def save(
    room_id: UUID, body: PresetIn, request: Request, db: Session = Depends(get_session)
):
    account = getattr(request.state, "admin", {})
    if account.get("role") != "admin":
        raise HTTPException(403, "现场资源仅由管理员维护")
    room = (
        db.query(BusinessRoom).filter_by(room_id=str(room_id)).with_for_update().first()
    )
    if not room or not room.enabled:
        raise HTTPException(422, "评标室不存在或已停用")
    latest = (
        db.query(RoomMonitoringPreset)
        .filter_by(room_id=room.room_id)
        .order_by(RoomMonitoringPreset.version.desc())
        .first()
    )
    if body.expected_version != (latest.version if latest else 0):
        raise HTTPException(409, "现场资源版本已更新，请刷新核对后保存")
    channels = [c.model_dump(mode="json") for c in body.channels]
    validate_seats(db, room, channels)
    if body.scheme == "standard" and (
        not any(z["kind"] == "seat" for c in channels for z in c["zones"])
        or not any(c["entry_lines"] for c in channels)
    ):
        raise HTTPException(
            422,
            "完整现场监管需配置工位观察区和出入口线；未标定时可先保存基础行为监管资源",
        )
    row = RoomMonitoringPreset(
        preset_id=str(uuid4()),
        room_id=room.room_id,
        version=body.expected_version + 1,
        scheme=body.scheme,
        primary_source_id=body.primary_source_id,
        channels=channels,
        note=body.note,
        created_by=account["username"],
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return serialize(row)
