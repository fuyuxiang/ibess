"""S04人脸核验关联回归：临时 PostgreSQL schema、独立应用，不导入 app.main。"""
import os
import unittest
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker


def _hdr():
    return {"X-Requested-With": "XMLHttpRequest"}


class BusinessFactLinksTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from app.business_fact_link_models import BusinessFactVerification
        from app.business_facts_models import BusinessAttendanceFact
        from app.business_models import BusinessParticipant, BusinessPerson, BusinessProject, BusinessRoom
        from app.db import get_session
        from app.face_models import PersonVerificationResult
        from app.models import Base
        from app.routers.business_fact_links import router
        from app.routers.business_facts import router as facts_router

        cls.Link, cls.Fact, cls.Result = BusinessFactVerification, BusinessAttendanceFact, PersonVerificationResult
        cls.Project, cls.Room, cls.Person, cls.Participant = BusinessProject, BusinessRoom, BusinessPerson, BusinessParticipant
        cls.Base = Base
        url = os.environ.get("WORKFLOW_TEST_DATABASE_URL")
        if not url:
            raise unittest.SkipTest("请显式配置 WORKFLOW_TEST_DATABASE_URL，使用专用测试数据库")
        cls.admin_engine = create_engine(url)
        cls.schema = "test_factlinks_" + uuid.uuid4().hex
        with cls.admin_engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{cls.schema}"'))
        try:
            cls.engine = create_engine(url, connect_args={"options": f"-csearch_path={cls.schema}"})
            Base.metadata.create_all(cls.engine)
            cls.Session = sessionmaker(bind=cls.engine)
            cls.app = FastAPI()
            cls.app.include_router(facts_router, prefix="/api")
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
            project = self.Project(code="F" + uuid.uuid4().hex[:8], name="关联测试项目", owner="测试业主", status="running")
            session.add(project)
            session.flush()
            room = self.Room(project_id=project.project_id, code="R" + uuid.uuid4().hex[:6], name="关联测试评标室", location="测试楼", enabled=True)
            session.add(room)
            session.flush()
            person = self.Person(code="L" + uuid.uuid4().hex[:8], name="核验专家", unit="测试单位", role="expert", enabled=True)
            session.add(person)
            session.flush()
            session.add(self.Participant(project_id=project.project_id, person_id=person.person_id, role="expert"))
            session.commit()
            self.project_id, self.room_id, self.person_id = project.project_id, room.room_id, person.person_id

    def _fact(self, fact_type="check_in", person=True):
        response = self.client.post("/api/facts", json={
            "fact_type": fact_type, "project_id": self.project_id, "room_id": self.room_id,
            "person_id": self.person_id if person else None, "note": "关联测试"}, headers=_hdr())
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["fact_id"]

    def _result(self, status="matched", similarity=0.93, minutes_ago=30):
        result_id = str(uuid.uuid4())
        with self.Session() as session:
            session.add(self.Result(result_id=result_id, person_id=self.person_id, status=status,
                                    matched=status == "matched", similarity=similarity, threshold=0.8,
                                    checked_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
                                    person_name="核验专家", reference_version="v1"))
            session.commit()
        return result_id

    def test_01_link_requires_fact_person(self):
        fact_id = self._fact(person=False)
        result_id = self._result()
        response = self.client.post(f"/api/fact-links/{fact_id}", json={"result_id": result_id}, headers=_hdr())
        self.assertEqual(response.status_code, 422)

    def test_02_link_person_mismatch_rejected(self):
        fact_id = self._fact()
        with self.Session() as session:
            stranger = self.Person(code="X" + uuid.uuid4().hex[:8], name="别人", unit="测试单位", role="staff", enabled=True)
            session.add(stranger)
            session.commit()
            result_id = str(uuid.uuid4())
            session.add(self.Result(result_id=result_id, person_id=stranger.person_id, status="matched",
                                    matched=True, similarity=0.9, checked_at=datetime.now(timezone.utc),
                                    person_name="别人", reference_version="v1"))
            session.commit()
        response = self.client.post(f"/api/fact-links/{fact_id}", json={"result_id": result_id}, headers=_hdr())
        self.assertEqual(response.status_code, 422)

    def test_03_link_and_get(self):
        fact_id = self._fact()
        result_id = self._result()
        response = self.client.post(f"/api/fact-links/{fact_id}", json={"result_id": result_id}, headers=_hdr())
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["status"], "matched")
        self.assertEqual(response.json()["actor"], "wyh")
        response = self.client.get(f"/api/fact-links/{fact_id}", headers=_hdr())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["similarity"], 0.93)

    def test_04_duplicate_link_rejected(self):
        fact_id = self._fact()
        first = self._result()
        second = self._result(minutes_ago=10)
        self.assertEqual(self.client.post(f"/api/fact-links/{fact_id}", json={"result_id": first}, headers=_hdr()).status_code, 201)
        response = self.client.post(f"/api/fact-links/{fact_id}", json={"result_id": second}, headers=_hdr())
        self.assertEqual(response.status_code, 409)

    def test_05_candidates(self):
        fact_id = self._fact()
        self._result(minutes_ago=60)
        self._result(minutes_ago=5)
        response = self.client.get(f"/api/fact-links/{fact_id}/candidates", headers=_hdr())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["items"]), 2)
        self.assertEqual(response.json()["items"][0]["status"], "matched")

    def test_06_auto_link_only_unlinked(self):
        fact_a = self._fact()
        self._result(minutes_ago=20)
        fact_b = self._fact()
        result_new = self._result(minutes_ago=1)
        response = self.client.post(f"/api/fact-links/auto-link?project_id={self.project_id}", headers=_hdr())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["linked"], 2)
        self.assertEqual(self.client.post(f"/api/fact-links/auto-link?project_id={self.project_id}", headers=_hdr()).json()["linked"], 0)
        for fact_id in (fact_a, fact_b):
            response = self.client.get(f"/api/fact-links/{fact_id}", headers=_hdr())
            self.assertEqual(response.json()["result_id"], result_new)

    def test_07_delete_link(self):
        fact_id = self._fact()
        result_id = self._result()
        self.client.post(f"/api/fact-links/{fact_id}", json={"result_id": result_id}, headers=_hdr())
        self.assertEqual(self.client.delete(f"/api/fact-links/{fact_id}", headers=_hdr()).status_code, 200)
        self.assertEqual(self.client.get(f"/api/fact-links/{fact_id}", headers=_hdr()).status_code, 404)

    def test_08_links_map_by_project(self):
        fact_id = self._fact()
        result_id = self._result()
        self.client.post(f"/api/fact-links/{fact_id}", json={"result_id": result_id}, headers=_hdr())
        response = self.client.get(f"/api/fact-links?project_id={self.project_id}", headers=_hdr())
        self.assertEqual(response.status_code, 200)
        self.assertIn(fact_id, response.json()["links"])


def _json(response):
    try:
        return response.json()
    except Exception:
        return {}


if __name__ == "__main__":
    unittest.main()
