"""评分记录回归（9.3）：临时 PostgreSQL schema、独立应用，不导入 app.main。"""
import os
import unittest
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker


def _hdr():
    return {"X-Requested-With": "XMLHttpRequest"}


class ScoreRecordsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from app.business_models import BusinessProject, BusinessRoom
        from app.db import get_session
        from app.models import Base
        from app.routers.score_records import router
        from app.score_models import BusinessScoreRecord

        cls.Project, cls.Room = BusinessProject, BusinessRoom
        cls.Base, cls.Score = Base, BusinessScoreRecord
        url = os.environ.get("WORKFLOW_TEST_DATABASE_URL")
        if not url:
            raise unittest.SkipTest("请显式配置 WORKFLOW_TEST_DATABASE_URL，使用专用测试数据库")
        cls.admin_engine = create_engine(url)
        cls.schema = "test_scores_" + uuid.uuid4().hex
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
            project = self.Project(code="S" + uuid.uuid4().hex[:8], name="评分测试项目", owner="测试业主", status="running")
            session.add(project)
            session.flush()
            room = self.Room(project_id=project.project_id, code="R" + uuid.uuid4().hex[:6], name="评分测试评标室", location="测试楼", enabled=True)
            session.add(room)
            session.commit()
            self.project_id, self.room_id = project.project_id, room.room_id

    def _body(self, **overrides):
        body = {"project_id": self.project_id, "room_id": self.room_id, "phase_id": "SCORE_CHECK",
                "occurred_at": "2026-10-20T06:00:00Z",
                "scores": [{"subject": "供应商甲", "item": "技术方案", "score": 85, "max_score": 100, "basis": "依据技术响应表"}]}
        body.update(overrides)
        return body

    def test_01_create_and_list(self):
        response = self.client.post("/api/scores", json=self._body(), headers=_hdr())
        self.assertEqual(response.status_code, 201)
        self.assertFalse(response.json()["deduplicated"])
        self.assertEqual(response.json()["actor"], "wyh")
        listing = self.client.get(f"/api/scores?room_id={self.room_id}", headers=_hdr())
        self.assertEqual(listing.json()["total"], 1)

    def test_02_idempotent_same_content(self):
        first = self.client.post("/api/scores", json=self._body(), headers=_hdr())
        second = self.client.post("/api/scores", json=self._body(), headers=_hdr())
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 201)
        self.assertTrue(second.json()["deduplicated"])
        self.assertEqual(second.json()["record_id"], first.json()["record_id"])

    def test_03_score_out_of_range_rejected(self):
        body = self._body(scores=[{"subject": "供应商甲", "item": "技术方案", "score": 120, "max_score": 100}])
        response = self.client.post("/api/scores", json=body, headers=_hdr())
        self.assertEqual(response.status_code, 422)

    def test_04_project_room_mismatch_rejected(self):
        with self.Session() as session:
            other = self.Project(code="O" + uuid.uuid4().hex[:8], name="其他项目", owner="测试", status="running")
            session.add(other)
            session.commit()
            other_id = other.project_id
        response = self.client.post("/api/scores", json=self._body(project_id=other_id), headers=_hdr())
        self.assertEqual(response.status_code, 422)

    def test_05_confidential_requires_nothing_extra_but_verify(self):
        created = self.client.post("/api/scores", json=self._body(), headers=_hdr()).json()
        response = self.client.get(f"/api/scores/{created['record_id']}/verify", headers=_hdr())
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["verified"])
        self.assertEqual(response.json()["expected"], created["content_sha256"])

    def test_06_different_content_different_hash(self):
        self.client.post("/api/scores", json=self._body(), headers=_hdr())
        other = self._body(scores=[{"subject": "供应商乙", "item": "技术方案", "score": 90, "max_score": 100}])
        created = self.client.post("/api/scores", json=other, headers=_hdr())
        self.assertFalse(created.json()["deduplicated"])


if __name__ == "__main__":
    unittest.main()
