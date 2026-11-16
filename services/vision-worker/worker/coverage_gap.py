"""见证缺口登记（执行卡 5.3）：记每段故障的起止，跨阶段、跨媒体轮次保留同一 gap_id。

只做簿记：开、关、查、导出、恢复。不碰数据库和 Redis；跨进程持久化由接线方用 export() / restore() 做。
gap_id 用确定性拼接（源、媒体轮次、种类、起始毫秒），重复消息和重启不会生成新 id。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional

STATE_VERSION = 1


def make_gap_id(source_id: str, media_run_id: str, kind: str, start_ms: int) -> str:
    return f"gap:{source_id}:{media_run_id}:{kind}:{int(start_ms)}"


@dataclass
class Gap:
    gap_id: str
    kind: str                      # offline | quality | audio_offline
    source_id: str
    gap_media_run_id: str          # 原缺口所属媒体轮次（契约 payload.gap_media_run_id）
    open_start_ms: int             # 相对 gap_media_run_id 的起始毫秒
    opened_at: str                 # UTC ISO，打开时刻
    scope: dict = field(default_factory=dict)
    technical_reason: str = ""
    closed_at: Optional[str] = None
    close_media_run_id: Optional[str] = None
    close_end_ms: Optional[int] = None

    @property
    def is_open(self) -> bool:
        return self.closed_at is None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Gap":
        return cls(**data)


class GapRegistry:
    """同一源、同一种类同时只有一条打开的缺口；关闭是幂等的。"""

    def __init__(self, max_closed: int = 500) -> None:
        self._gaps: dict[str, Gap] = {}
        self.max_closed = max_closed          # 长时间运行只保留最近的已关闭缺口（5.01 复核：常驻进程不能无限增长）

    def open(self, *, source_id: str, kind: str, media_run_id: str, start_ms: int, occurred_at: str,
             scope: Optional[dict] = None, technical_reason: str = "") -> Gap:
        existing = self.open_gap(source_id, kind)
        if existing is not None:
            return existing
        gap_id = make_gap_id(source_id, media_run_id, kind, start_ms)
        suffix = 1
        while gap_id in self._gaps:          # 同一起点已有一条关闭的缺口：加序号，不覆盖历史
            suffix += 1
            gap_id = f"{make_gap_id(source_id, media_run_id, kind, start_ms)}:{suffix}"
        gap = Gap(gap_id=gap_id, kind=kind, source_id=source_id, gap_media_run_id=media_run_id,
                  open_start_ms=int(start_ms), opened_at=occurred_at, scope=dict(scope or {}),
                  technical_reason=technical_reason)
        self._gaps[gap.gap_id] = gap
        closed = [g for g in self._gaps.values() if not g.is_open]
        for old in closed[:max(0, len(closed) - self.max_closed)]:
            del self._gaps[old.gap_id]
        return gap

    def open_gap(self, source_id: str, kind: str) -> Optional[Gap]:
        for gap in self._gaps.values():
            if gap.source_id == source_id and gap.kind == kind and gap.is_open:
                return gap
        return None

    def open_gaps(self, source_id: Optional[str] = None) -> list[Gap]:
        return [g for g in self._gaps.values() if g.is_open and (source_id is None or g.source_id == source_id)]

    def all(self) -> list[Gap]:
        return list(self._gaps.values())

    def find(self, gap_id: str) -> Optional[Gap]:
        return self._gaps.get(gap_id)

    def close(self, gap_id: str, *, media_run_id: str, end_ms: int, occurred_at: str) -> Gap:
        gap = self._gaps[gap_id]              # 未登记的缺口直接 KeyError，不静默新建
        if not gap.is_open:
            return gap
        gap.closed_at = occurred_at
        gap.close_media_run_id = media_run_id
        gap.close_end_ms = int(end_ms)
        return gap

    def export(self) -> dict:
        return {"version": STATE_VERSION, "gaps": [g.to_dict() for g in self._gaps.values()]}

    def restore(self, state: dict) -> None:
        if not isinstance(state, dict) or state.get("version") != STATE_VERSION:
            raise ValueError(f"缺口状态版本不支持：{state.get('version') if isinstance(state, dict) else state!r}")
        self._gaps = {g["gap_id"]: Gap.from_dict(g) for g in state.get("gaps", [])}

    def __len__(self) -> int:
        return len(self._gaps)
