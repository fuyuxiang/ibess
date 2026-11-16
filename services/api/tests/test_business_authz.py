"""S04授权快照回归：临时 PostgreSQL schema、独立应用，不导入 app.main。"""
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


class BusinessAuthzTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from app.business_authz_models import BusinessAuthorizationEntry, BusinessAuthorizationSnapshot
        from app.business_fact_link_models import BusinessFactVerification
        from app.business_facts_models import BusinessAttendanceFact
        from app.business_models import BusinessParticipant, BusinessPerson, BusinessProject, BusinessRoom
        from app.db import get_session
        from app.face_models import PersonVerificationResult
        from app.models import Base
        from app.routers.business_authz import router
        from app.routers.business_facts import router as facts_router

        cls.Snapshot, cls.Entry, cls.Link = BusinessAuthorizationSnapshot, BusinessAuthorizationEntry, BusinessFactVerification
        cls.Fact, cls.Result = BusinessAttendanceFact, PersonVerificationResult
        cls.Project, cls.Room, cls.Person, cls.Participant = BusinessProject, BusinessRoom, BusinessPerson, BusinessParticipant
        cls.Base = Base
        url = os.environ.get("WORKFLOW_TEST_DATABASE_URL")
        if not url:
            raise unittest.SkipTest("请显式配置 WORKFLOW_TEST_DATABASE_URL，使用专用测试数据库")
        cls.admin_engine = create_engine(url)
        cls.schema = "test_authz_" + uuid.uuid4().hex
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
            project = self.Project(code="A" + uuid.uuid4().hex[:8], name="授权快照测试项目", owner="测试业主", status="running")
            session.add(project)
            session.flush()
            room = self.Room(project_id=project.project_id, code="R" + uuid.uuid4().hex[:6], name="授权测试评标室", location="测试楼", enabled=True)
            session.add(room)
            session.flush()
            persons = {}
            for code, name in (("E1", "专家甲"), ("E2", "专家乙")):
                person = self.Person(code=code + uuid.uuid4().hex[:6], name=name, unit="测试单位", role="expert", enabled=True)
                session.add(person)
                session.flush()
                session.add(self.Participant(project_id=project.project_id, person_id=person.person_id, role="expert"))
                persons[code] = person
            session.commit()
            self.project_id, self.room_id = project.project_id, room.room_id
            self.p1_id, self.p2_id = persons["E1"].person_id, persons["E2"].person_id

    def _fact(self, fact_type, person_id=None, payload=None, note="快照测试", minutes_ago=0):
        response = self.client.post("/api/facts", json={
            "fact_type": fact_type, "project_id": self.project_id, "room_id": self.room_id,
            "person_id": person_id, "payload": payload, "note": note,
            "occurred_at": (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()}, headers=_hdr())
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["fact_id"]

    def _verify(self, fact_id, status="matched", similarity=0.92):
        with self.Session() as session:
            result_id = str(uuid.uuid4())
            fact = session.get(self.Fact, fact_id)
            session.add(self.Result(result_id=result_id, person_id=fact.person_id, status=status,
                                    matched=status == "matched", similarity=similarity, threshold=0.8,
                                    checked_at=datetime.now(timezone.utc), person_name="核验专家", reference_version="v1"))
            session.flush()
            session.add(self.Link(fact_id=fact_id, result_id=result_id, actor="wyh", created_at=datetime.now(timezone.utc)))
            session.commit()
        return result_id

    def _generate(self, **overrides):
        body = {"project_id": self.project_id, "room_id": self.room_id, "phase_id": "DISCIPLINE", **overrides}
        response = self.client.post("/api/authz/snapshots", json=body, headers=_hdr())
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def test_01_snapshot_assembles_presence_and_identity(self):
        fact_id = self._fact("check_in", self.p1_id)
        self._verify(fact_id)
        self._fact("check_in", self.p2_id)  # 未核验
        snapshot = self._generate(information_visibility="public")
        self.assertEqual(len(snapshot["entries"]), 2)
        by_person = {entry["person_id"]: entry for entry in snapshot["entries"]}
        self.assertTrue(by_person[self.p1_id]["identity_verified"])
        self.assertFalse(by_person[self.p2_id]["identity_verified"])
        self.assertEqual(by_person[self.p1_id]["basis_fact_id"], fact_id)

    def test_02_checkout_removes_person(self):
        self._fact("check_in", self.p1_id)
        self._fact("check_out", self.p1_id)
        snapshot = self._generate()
        self.assertEqual([entry["person_id"] for entry in snapshot["entries"]], [])

    def test_03_change_swaps_person(self):
        self._fact("check_in", self.p1_id)
        self._fact("change", self.p2_id, payload={"replaced_person_id": self.p1_id}, note="回避换人")
        snapshot = self._generate()
        self.assertEqual([entry["person_id"] for entry in snapshot["entries"]], [self.p2_id])

    def test_04_confidential_requires_policy(self):
        response = self.client.post("/api/authz/snapshots", json={
            "project_id": self.project_id, "room_id": self.room_id, "information_visibility": "confidential"}, headers=_hdr())
        self.assertEqual(response.status_code, 422)

    def test_05_grants_apply_and_supersede(self):
        self._fact("check_in", self.p1_id)
        first = self._generate(information_visibility="confidential", confidentiality_policy_id="policy-test-1",
                               grants=[{"person_id": self.p1_id, "recipient_authorized": True}])
        self.assertEqual(first["status"], "active")
        second = self._generate(information_visibility="public")
        response = self.client.get(f"/api/authz/snapshots/{first['snapshot_id']}", headers=_hdr())
        self.assertEqual(response.json()["status"], "superseded")
        self.assertTrue(second["entries"][0]["recipient_authorized"] is False)
        self.assertTrue(first["entries"][0]["recipient_authorized"] is True)

    def test_06_latest_shape_for_semantic(self):
        self._fact("check_in", self.p1_id)
        self._verify(self.client.get(f"/api/facts?project_id={self.project_id}", headers=_hdr()).json()["items"][0]["fact_id"])
        self._generate(information_visibility="confidential", confidentiality_policy_id="policy-test-2",
                       grants=[{"person_id": self.p1_id, "recipient_authorized": True}])
        response = self.client.get(f"/api/authz/snapshots/latest?room_id={self.room_id}", headers=_hdr())
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["binding_status"], "bound")
        self.assertEqual(payload["information_visibility"], "confidential")
        self.assertTrue(payload["recipient_authorized"])
        self.assertTrue(payload["speaker_bindings"][0]["identity_verified"])
        self.assertIsNotNone(payload["confidentiality_policy_id"])

    def test_07_latest_missing_returns_404(self):
        response = self.client.get(f"/api/authz/snapshots/latest?room_id={self.room_id}", headers=_hdr())
        self.assertEqual(response.status_code, 404)

    def test_08_grant_person_must_join_project(self):
        with self.Session() as session:
            stranger = self.Person(code="Z" + uuid.uuid4().hex[:8], name="外人", unit="测试单位", role="staff", enabled=True)
            session.add(stranger)
            session.commit()
            stranger_id = stranger.person_id
        self._fact("check_in", self.p1_id)
        response = self.client.post("/api/authz/snapshots", json={
            "project_id": self.project_id, "room_id": self.room_id,
            "grants": [{"person_id": stranger_id, "recipient_authorized": True}]}, headers=_hdr())
        self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()
