"""离线质检运行回归（9.4）：临时 PostgreSQL schema、独立应用，不导入 app.main。"""
import os
import unittest
import uuid
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker


def _hdr():
    return {"X-Requested-With": "XMLHttpRequest"}


class OfflineQaTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from app.audio_models import AudioRecording, AudioSegment
        from app.business_models import BusinessProject, BusinessRoom
        from app.db import get_session
        from app.media_binding_models import MediaBinding
        from app.models import Base
        from app.offline_qa_models import OfflineQaRun
        from app.routers.offline_qa import router

        cls.Base, cls.Project, cls.Room = Base, BusinessProject, BusinessRoom
        cls.Recording, cls.Segment, cls.Binding = AudioRecording, AudioSegment, MediaBinding
        url = os.environ.get("WORKFLOW_TEST_DATABASE_URL")
        if not url:
            raise unittest.SkipTest("请显式配置 WORKFLOW_TEST_DATABASE_URL，使用专用测试数据库")
        cls.admin_engine = create_engine(url)
        cls.schema = "test_offlineqa_" + uuid.uuid4().hex
        with cls.admin_engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{cls.schema}"'))
        try:
            cls.engine = create_engine(url, connect_args={"options": f"-csearch_path={cls.schema}"})
            Base.metadata.create_all(cls.engine)
            cls.Session = sessionmaker(bind=cls.engine)
            cls.app = FastAPI()
            cls.app.include_router(router, prefix="/api")

            @cls.app.middleware("http")
            async def fake_admin(request, call_next):
                request.state.admin = {"username": "wyh", "role": "admin", "display_name": "测试管理员"}
                return await call_next(request)

            def override_session():
                with cls.Session() as session:
                    yield session

            cls.app.dependency_overrides[get_session] = override_session
            cls.client = TestClient(cls.app)
        except Exception:
            cls.engine.dispose()
            with cls.admin_engine.begin() as connection:
                connection.execute(text(f'DROP SCHEMA "{cls.schema}" CASCADE'))
            cls.admin_engine.dispose()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()
        with cls.admin_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{cls.schema}" CASCADE'))
        cls.admin_engine.dispose()

    def setUp(self):
        with self.Session() as session:
            project = self.Project(code="Q" + uuid.uuid4().hex[:8], name="离线质检项目", owner="测试业主", status="running")
            session.add(project)
            session.flush()
            room = self.Room(project_id=project.project_id, code="Q" + uuid.uuid4().hex[:6], name="质检评标室", location="测试楼", enabled=True)
            session.add(room)
            session.flush()
            rec = self.Recording(recording_id=str(uuid.uuid4()), title="质检录音", source_id="audio-upload",
                                 source_type="upload", status="ready", sha256="c" * 64, duration_ms=30000,
                                 original_path="q.original.wav",
                                 created_at=datetime.now(timezone.utc))
            session.add(rec)
            session.flush()
            seg = self.Segment(segment_id=str(uuid.uuid4()), recording_id=rec.recording_id, ordinal=1, speaker="A",
                               start_ms=0, end_ms=5000, text="我们现在开始核对评分。")
            session.add(seg)
            session.commit()
            self.project_id, self.room_id = project.project_id, room.room_id
            self.recording_id = rec.recording_id

    def _bind(self):
        with self.Session() as session:
            session.add(self.Binding(binding_id=str(uuid.uuid4()), recording_id=self.recording_id,
                                     project_id=self.project_id, room_id=self.room_id,
                                     actor="wyh", created_at=datetime.now(timezone.utc)))
            session.commit()

    def test_01_unbound_mode_creates(self):
        # 未绑定录音：unbound 占位上下文质检（重放研判，不依赖快照）
        response = self.client.post("/api/offline-qa/runs", json={"recording_id": self.recording_id}, headers=_hdr())
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["binding_status"], "unbound")

    def test_02_create_run_two_tasks(self):
        self._bind()
        response = self.client.post("/api/offline-qa/runs", json={"recording_id": self.recording_id}, headers=_hdr())
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(len(body["task_keys"]), 2)
        self.assertEqual(body["scenes"], ["S06", "S07"])
        report = self.client.get(f"/api/offline-qa/runs/{body['run_id']}", headers=_hdr())
        self.assertEqual(report.status_code, 200)
        self.assertEqual(report.json()["summary"]["total"], 2)
        self.assertIn(report.json()["status"], ("queued", "completed"))

    def test_03_rerun_produces_new_tasks(self):
        self._bind()
        first = self.client.post("/api/offline-qa/runs", json={"recording_id": self.recording_id}, headers=_hdr()).json()
        second = self.client.post("/api/offline-qa/runs", json={"recording_id": self.recording_id}, headers=_hdr()).json()
        self.assertNotEqual(set(first["task_keys"]), set(second["task_keys"]))
        listing = self.client.get("/api/offline-qa/runs", headers=_hdr())
        mine = [item for item in listing.json()["items"] if item["recording_id"] == self.recording_id]
        self.assertEqual(len(mine), 2)

    def test_04_invalid_scene_rejected(self):
        self._bind()
        response = self.client.post("/api/offline-qa/runs", json={
            "recording_id": self.recording_id, "scenes": ["S09"]}, headers=_hdr())
        self.assertEqual(response.status_code, 422)

    def test_05_missing_recording_404(self):
        self._bind()
        response = self.client.post("/api/offline-qa/runs", json={"recording_id": str(uuid.uuid4())}, headers=_hdr())
        self.assertEqual(response.status_code, 404)

    def test_06_missing_run_404(self):
        response = self.client.get(f"/api/offline-qa/runs/{uuid.uuid4()}", headers=_hdr())
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
