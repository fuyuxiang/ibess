"""媒体绑定回归（4.3）：临时 PostgreSQL schema、独立应用，不导入 app.main。"""
import os
import unittest
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker


def _hdr():
    return {"X-Requested-With": "XMLHttpRequest"}


class MediaBindingsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from app.audio_models import AudioRecording
        from app.business_models import BusinessProject, BusinessRoom
        from app.db import get_session
        from app.media_binding_models import MediaBinding
        from app.models import Base
        from app.routers.media_bindings import router

        cls.Project, cls.Room, cls.Recording, cls.Binding = BusinessProject, BusinessRoom, AudioRecording, MediaBinding
        cls.Base = Base
        url = os.environ.get("WORKFLOW_TEST_DATABASE_URL")
        if not url:
            raise unittest.SkipTest("请显式配置 WORKFLOW_TEST_DATABASE_URL，使用专用测试数据库")
        cls.admin_engine = create_engine(url)
        cls.schema = "test_mediabind_" + uuid.uuid4().hex
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
            project = self.Project(code="M" + uuid.uuid4().hex[:8], name="媒体绑定测试项目", owner="测试业主", status="running")
            session.add(project)
            session.flush()
            room_a = self.Room(project_id=project.project_id, code="A" + uuid.uuid4().hex[:6], name="评标室甲", location="测试楼", enabled=True)
            room_b = self.Room(project_id=project.project_id, code="B" + uuid.uuid4().hex[:6], name="评标室乙", location="测试楼", enabled=True)
            session.add_all([room_a, room_b])
            session.flush()
            rec = self.Recording(recording_id=str(uuid.uuid4()), title="绑定测试录音", source_id="audio-upload",
                                 source_type="upload", status="ready", sha256="a" * 64, duration_ms=60000,
                                 original_path="x.original.wav", created_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc))
            session.add(rec)
            session.commit()
            self.project_id, self.room_a, self.room_b = project.project_id, room_a.room_id, room_b.room_id
            self.recording_id = rec.recording_id

    def _bind(self, room_id=None, recording_id=None):
        return self.client.post("/api/media-bindings", json={
            "recording_id": recording_id or self.recording_id,
            "room_id": room_id or self.room_a}, headers=_hdr())

    def test_01_bind_and_query(self):
        response = self._bind()
        self.assertEqual(response.status_code, 201)
        self.assertFalse(response.json()["deduplicated"])
        self.assertEqual(response.json()["actor"], "wyh")
        listing = self.client.get(f"/api/media-bindings?room_id={self.room_a}", headers=_hdr())
        self.assertEqual(listing.json()["total"], 1)
        by_recording = self.client.get(f"/api/media-bindings/by-recording/{self.recording_id}", headers=_hdr())
        self.assertEqual(by_recording.json()["room_id"], self.room_a)

    def test_02_rebind_to_other_room(self):
        self._bind()
        response = self._bind(room_id=self.room_b)
        self.assertEqual(response.status_code, 201)
        self.assertTrue(response.json().get("rebound"))
        listing = self.client.get(f"/api/media-bindings?room_id={self.room_b}", headers=_hdr())
        self.assertEqual(listing.json()["total"], 1)
        listing = self.client.get(f"/api/media-bindings?room_id={self.room_a}", headers=_hdr())
        self.assertEqual(listing.json()["total"], 0)

    def test_03_same_room_idempotent(self):
        self._bind()
        response = self._bind()
        self.assertEqual(response.status_code, 201)
        self.assertTrue(response.json()["deduplicated"])

    def test_04_missing_recording_404(self):
        response = self._bind(recording_id=str(uuid.uuid4()))
        self.assertEqual(response.status_code, 404)

    def test_05_unbound_404(self):
        response = self.client.get(f"/api/media-bindings/by-recording/{uuid.uuid4()}", headers=_hdr())
        self.assertEqual(response.status_code, 404)

    def test_06_batch_bind(self):
        with self.Session() as session:
            ids = []
            for _ in range(2):
                rec = self.Recording(recording_id=str(uuid.uuid4()), title="批量", source_id="audio-upload",
                                     source_type="upload", status="ready", sha256="b" * 64, duration_ms=60000,
                                     original_path="y.original.wav",
                                     created_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc))
                session.add(rec)
                ids.append(rec.recording_id)
            session.commit()
        response = self.client.post("/api/media-bindings/batch", json={
            "recording_ids": ids, "room_id": self.room_a}, headers=_hdr())
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["total"], 2)
        repeat = self.client.post("/api/media-bindings/batch", json={
            "recording_ids": ids, "room_id": self.room_a}, headers=_hdr())
        self.assertTrue(all(item["status"] == "deduplicated" for item in repeat.json()["items"]))

    def test_07_unbind(self):
        self._bind()
        response = self.client.delete(f"/api/media-bindings/{self.recording_id}", headers=_hdr())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get(f"/api/media-bindings/by-recording/{self.recording_id}", headers=_hdr()).status_code, 404)


if __name__ == "__main__":
    unittest.main()
