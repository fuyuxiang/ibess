"""offline_qa_jobs 的直连仓库（照 8.2 semantic-worker 的写法：Core 表定义 + skip_locked 认领 + claim_id 防旧认领覆盖）。

列名与 api 的 app.offline_visual_models.OfflineQaJob 一致；本包不导入 api。
过期租约（worker 崩溃、机器重启）可被重新认领，从 0 重跑；尝试次数用完的过期任务转 failed ATTEMPTS_EXHAUSTED。
"""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import uuid4

from sqlalchemy import JSON, Boolean, Column, DateTime, Integer, MetaData, String, Table, or_, select, update


def _now() -> datetime:
    return datetime.now(timezone.utc)


def job_table() -> Table:
    metadata = MetaData()
    return Table(
        "offline_qa_jobs", metadata,
        Column("job_id", String(36), primary_key=True), Column("request_id", String(64)), Column("request_digest", String(64)),
        Column("session_id", String(36)), Column("project_id", String(36)), Column("room_id", String(36)), Column("source_id", String(256)),
        Column("binding_id", String(36)), Column("asset_id", String(256)), Column("status", String(16)), Column("cancel_requested", Boolean),
        Column("frozen", JSON), Column("progress_ms", Integer), Column("attempts", Integer), Column("max_attempts", Integer),
        Column("claim_id", String(36)), Column("claimed_by", String(128)), Column("claim_at", DateTime(timezone=True)),
        Column("error", JSON), Column("result", JSON), Column("created_by", String(128)), Column("created_at", DateTime(timezone=True)),
        Column("updated_at", DateTime(timezone=True)), Column("finished_at", DateTime(timezone=True)), Column("contract_version", String(16)),
    )


class SqlAlchemyJobRepository:
    def __init__(self, engine, *, lease_seconds: int = 120) -> None:
        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int) or lease_seconds <= 0:
            raise ValueError("lease_seconds 必须是正整数")
        self.engine = engine
        self.jobs = job_table()
        self.lease_seconds = lease_seconds

    def _active(self, job_id: str, claim_id: str):
        c = self.jobs.c
        return (c.job_id == str(job_id)) & (c.status == "running") & (c.claim_id == str(claim_id))

    def claim_next(self, worker_id: str) -> Optional[dict]:
        """认领一条 queued 或租约过期的 running；过期且次数用完 / 已请求取消的，先收成终态再找下一条。"""
        c = self.jobs.c
        while True:
            now = _now()
            stale = now - timedelta(seconds=self.lease_seconds)
            with self.engine.begin() as connection:
                statement = (select(self.jobs).where(or_(c.status == "queued", (c.status == "running") & (c.claim_at <= stale)))
                             .order_by(c.created_at, c.job_id).with_for_update(skip_locked=True).limit(1))
                row = connection.execute(statement).mappings().first()
                if row is None:
                    return None
                job_id = row["job_id"]
                if row["status"] == "running" and row["cancel_requested"]:
                    connection.execute(update(self.jobs).where(c.job_id == job_id).values(
                        status="cancelled", claim_id=None, claimed_by=None, claim_at=None, finished_at=now, updated_at=now))
                    continue
                if row["status"] == "running" and row["attempts"] >= row["max_attempts"]:
                    connection.execute(update(self.jobs).where(c.job_id == job_id).values(
                        status="failed", error={"code": "ATTEMPTS_EXHAUSTED", "retryable": True,
                                                "message": f"已认领 {row['attempts']} 次都没跑完（租约过期）"},
                        claim_id=None, claimed_by=None, claim_at=None, finished_at=now, updated_at=now))
                    continue
                claim_id = str(uuid4())
                connection.execute(update(self.jobs).where(c.job_id == job_id).values(
                    status="running", claim_id=claim_id, claimed_by=worker_id, claim_at=now, attempts=c.attempts + 1,
                    progress_ms=0, updated_at=now))
                job = dict(row)
                job.update(status="running", claim_id=claim_id, claimed_by=worker_id, attempts=row["attempts"] + 1, progress_ms=0)
                job["frozen"] = copy.deepcopy(row["frozen"])
                return job

    def heartbeat(self, job_id: str, claim_id: str, progress_ms: int) -> str:
        """续租并写进度。返回 ok / cancel（已请求取消）/ lost（认领已失效，别再写）。"""
        now = _now()
        with self.engine.begin() as connection:
            updated = connection.execute(update(self.jobs).where(self._active(job_id, claim_id)).values(
                claim_at=now, progress_ms=max(0, int(progress_ms)), updated_at=now))
            if updated.rowcount != 1:
                return "lost"
            cancel = connection.execute(select(self.jobs.c.cancel_requested).where(self.jobs.c.job_id == str(job_id))).scalar()
            return "cancel" if cancel else "ok"

    def pin_components(self, job_id: str, claim_id: str, components: dict) -> bool:
        """第一次运行写入代码版本清单；只在当前认领有效且尚未写过时写。"""
        with self.engine.begin() as connection:
            frozen = connection.execute(select(self.jobs.c.frozen).where(self._active(job_id, claim_id))).scalar()
            if frozen is None or frozen.get("components") is not None:
                return False
            frozen = {**frozen, "components": components}
            updated = connection.execute(update(self.jobs).where(self._active(job_id, claim_id)).values(frozen=frozen, updated_at=_now()))
            return updated.rowcount == 1

    def _finish(self, job_id: str, claim_id: str, **values) -> bool:
        now = _now()
        with self.engine.begin() as connection:
            updated = connection.execute(update(self.jobs).where(self._active(job_id, claim_id)).values(
                claim_id=None, claimed_by=None, claim_at=None, finished_at=now, updated_at=now, **values))
            return updated.rowcount == 1

    def complete(self, job_id: str, claim_id: str, result: dict, progress_ms: int) -> bool:
        """已请求取消的任务不写成功（返回 False，由调用方改走 cancel）。"""
        now = _now()
        with self.engine.begin() as connection:
            updated = connection.execute(update(self.jobs).where(self._active(job_id, claim_id) & (self.jobs.c.cancel_requested.is_(False))).values(
                status="succeeded", result=result, error=None, progress_ms=max(0, int(progress_ms)),
                claim_id=None, claimed_by=None, claim_at=None, finished_at=now, updated_at=now))
            return updated.rowcount == 1

    def fail(self, job_id: str, claim_id: str, code: str, *, retryable: bool, message: str = "", result: Optional[dict] = None) -> bool:
        return self._finish(job_id, claim_id, status="failed", error={"code": code, "retryable": bool(retryable), "message": message[:500]},
                            result=result)

    def cancel(self, job_id: str, claim_id: str) -> bool:
        return self._finish(job_id, claim_id, status="cancelled", error=None)
