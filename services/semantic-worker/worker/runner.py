"""Semantic task runner; database adapters are injected by 10.1/10.2."""

from datetime import datetime, timedelta, timezone
import logging
import os
import time
from uuid import uuid4

from sqlalchemy import JSON, Column, DateTime, Integer, MetaData, String, Table, create_engine, or_, select, update

from worker.llm_client import ModelClientError
from worker.result_validator import SemanticResultError
from worker.llm_client import OpenAICompatibleClient, SemanticPipeline


log = logging.getLogger("semantic-worker")


def _now():
    return datetime.now(timezone.utc)


def task_table():
    metadata = MetaData()
    return Table(
        "semantic_analyses", metadata,
        Column("analysis_id", String(36), primary_key=True),
        Column("recording_id", String(36)), Column("task_key", String(64)), Column("scene_id", String(8)),
        Column("window_start_ms", Integer), Column("window_end_ms", Integer), Column("status", String(16)),
        Column("request_snapshot", JSON), Column("result", JSON), Column("error", JSON),
        Column("claim_id", String(36)), Column("claimed_by", String(128)), Column("claim_at", DateTime(timezone=True)),
        Column("attempts", Integer), Column("created_at", DateTime(timezone=True)),
        Column("updated_at", DateTime(timezone=True)), Column("completed_at", DateTime(timezone=True)),
    )


class SqlAlchemyRepository:
    """PostgreSQL adapter for the independent 8.2 semantic task table."""

    def __init__(self, engine, *, lease_seconds=120):
        self.engine = engine
        self.tasks = task_table()
        self.lease_seconds = lease_seconds

    def claim_next(self, worker_id, lease_seconds=None):
        now = _now()
        stale = now - timedelta(seconds=lease_seconds or self.lease_seconds)
        with self.engine.begin() as connection:
            statement = (select(self.tasks).where(or_(self.tasks.c.status == "queued",
                (self.tasks.c.status == "processing") & (self.tasks.c.claim_at <= stale)))
                .order_by(self.tasks.c.created_at).with_for_update(skip_locked=True).limit(1))
            row = connection.execute(statement).mappings().first()
            if row is None:
                return None
            claim_id = str(uuid4())
            connection.execute(update(self.tasks).where(self.tasks.c.analysis_id == row["analysis_id"]).values(
                status="processing", claim_id=claim_id, claimed_by=worker_id, claim_at=now,
                attempts=self.tasks.c.attempts + 1, updated_at=now))
            result = dict(row)
            result.update({"claim_id": claim_id, "status": "processing", "request": row["request_snapshot"]})
            return result

    def complete(self, analysis_id, claim_id, result):
        now = _now()
        with self.engine.begin() as connection:
            updated = connection.execute(update(self.tasks).where(
                self.tasks.c.analysis_id == str(analysis_id), self.tasks.c.status == "processing",
                self.tasks.c.claim_id == str(claim_id)).values(status="ready", result=result, error=None,
                    claim_id=None, claimed_by=None, claim_at=None, completed_at=now, updated_at=now))
            return updated.rowcount == 1

    def fail(self, analysis_id, claim_id, error, retryable):
        now = _now()
        with self.engine.begin() as connection:
            updated = connection.execute(update(self.tasks).where(
                self.tasks.c.analysis_id == str(analysis_id), self.tasks.c.status == "processing",
                self.tasks.c.claim_id == str(claim_id)).values(status="failed", error={"code": error, "retryable": bool(retryable)},
                    claim_id=None, claimed_by=None, claim_at=None, updated_at=now))
            return updated.rowcount == 1

    def heartbeat(self, analysis_id, claim_id):
        with self.engine.begin() as connection:
            updated = connection.execute(update(self.tasks).where(
                self.tasks.c.analysis_id == str(analysis_id), self.tasks.c.status == "processing",
                self.tasks.c.claim_id == str(claim_id)).values(claim_at=_now(), updated_at=_now()))
            return updated.rowcount == 1


class SemanticRunner:
    def __init__(self, repository, pipeline, *, worker_id, max_retries=2, lease_seconds=120, sleep=time.sleep):
        if not worker_id or not isinstance(worker_id, str):
            raise ValueError("worker_id is required")
        if isinstance(max_retries, bool) or not isinstance(max_retries, int) or max_retries < 0:
            raise ValueError("max_retries must be a non-negative integer")
        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int) or lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        self.repository = repository
        self.pipeline = pipeline
        self.worker_id = worker_id
        self.max_retries = max_retries
        self.lease_seconds = lease_seconds
        self.sleep = sleep

    def run_once(self):
        task = self.repository.claim_next(self.worker_id, self.lease_seconds)
        if task is None:
            return False
        analysis_id = task["analysis_id"]
        claim_id = task["claim_id"]
        request = task.get("request") or task.get("request_snapshot")
        if not isinstance(request, dict):
            self.repository.fail(analysis_id, claim_id, "REQUEST_SNAPSHOT_INVALID", False)
            return True

        result = None
        for attempt in range(self.max_retries + 1):
            try:
                result = self.pipeline.analyze(request)
                break
            except ModelClientError:
                if attempt >= self.max_retries:
                    self.repository.fail(analysis_id, claim_id, "MODEL_RESPONSE_INVALID", True)
                    return True
                self.sleep(min(2 ** attempt, 8))
            except SemanticResultError:
                self.repository.fail(analysis_id, claim_id, "SEMANTIC_RESULT_INVALID", False)
                return True
            except Exception:
                # 不把模型响应、请求正文或供应商异常写进数据库/日志。
                self.repository.fail(analysis_id, claim_id, "SEMANTIC_WORKER_ERROR", True)
                log.exception("语义任务处理失败；analysis_id=%s error_type=unexpected", analysis_id)
                return True

        self.complete_claim(task, result)
        return True

    def complete_claim(self, task, result):
        """Repository must reject a stale claim instead of overwriting a newer result."""
        return self.repository.complete(task["analysis_id"], task["claim_id"], result)

    def heartbeat(self, task):
        heartbeat = getattr(self.repository, "heartbeat", None)
        if heartbeat is None:
            return False
        return bool(heartbeat(task["analysis_id"], task["claim_id"]))


def build_runner_from_environment():
    """Build the single-concurrency cloud MiniMax runner without exposing credentials."""
    database_url = os.environ.get("DATABASE_URL")
    base_url = os.environ.get("SEMANTIC_MODEL_BASE_URL")
    model = os.environ.get("SEMANTIC_MODEL_NAME", "MiniMax-M3.1-Flash-Preview")
    api_key = os.environ.get("SEMANTIC_MODEL_API_KEY")
    worker_id = os.environ.get("SEMANTIC_WORKER_ID", "semantic-worker-1")
    if not database_url or not base_url or not api_key:
        raise RuntimeError("语义worker配置缺失；需要DATABASE_URL、SEMANTIC_MODEL_BASE_URL和SEMANTIC_MODEL_API_KEY")
    engine = create_engine(database_url, pool_pre_ping=True)
    repository = SqlAlchemyRepository(engine)
    client = OpenAICompatibleClient(base_url, model, api_key, timeout=60)
    return SemanticRunner(repository, SemanticPipeline(client), worker_id=worker_id, max_retries=2), engine


def main():
    runner, engine = build_runner_from_environment()
    try:
        while True:
            if not runner.run_once():
                time.sleep(2)
    except KeyboardInterrupt:
        pass
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
