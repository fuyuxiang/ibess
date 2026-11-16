"""S04业务事实回归：临时 PostgreSQL schema、独立应用，不导入 app.main。"""
import os
import unittest
import uuid
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker


def _request(client, method, path, body=None):
    headers = {"X-Requested-With": "XMLHttpRequest"}
    return client.request(method, path, json=body, headers=headers)


class BusinessFactsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from app.business_facts_models import BusinessAttendanceFact
        from app.business_models import BusinessParticipant, BusinessPerson, BusinessProject, BusinessRoom, BusinessSeat
        from app.db import get_session
        from app.models import Base
        from app.routers.business_facts import router

        cls.Fact = BusinessAttendanceFact
        cls.Project, cls.Room, cls.Person = BusinessProject, BusinessRoom, BusinessPerson
        cls.Seat, cls.Participant = BusinessSeat, BusinessParticipant
        cls.Base = Base
        url = os.environ.get("WORKFLOW_TEST_DATABASE_URL")
        if not url:
            raise unittest.SkipTest("请显式配置 WORKFLOW_TEST_DATABASE_URL，使用专用测试数据库")
        cls.admin_engine = create_engine(url)
        cls.schema = "test_facts_" + uuid.uuid4().hex
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
                # 生产环境由 AdminAuthMiddleware 注入；测试模拟同一契约。
                request.state.admin = {"username": "wyh", "role": "admin", "display_name": "测试管理员"}
                return await call_next(request)

            def override_session():
                with cls.Session() as session:
                    yield session

            cls.app.dependency_overrides[get_session] = override_session
            cls.client = TestClient(cls.app)
            # 管理员身份由中间件注入；测试直接模拟 request.state.admin。
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
            project = self.Project(code="P" + uuid.uuid4().hex[:8], name="事实测试项目", owner="测试业主", status="running")
            session.add(project)
            session.flush()
            room = self.Room(project_id=project.project_id, code="R" + uuid.uuid4().hex[:6], name="事实测试评标室", location="测试楼", enabled=True)
            session.add(room)
            session.flush()
            persons = {}
            for code, name, role in (("P1", "专家甲", "expert"), ("P2", "专家乙", "expert")):
                person = self.Person(code=code + uuid.uuid4().hex[:6], name=name, unit="测试单位", role=role, enabled=True)
                session.add(person)
                session.flush()
                session.add(self.Participant(project_id=project.project_id, person_id=person.person_id, role=role))
                persons[code] = person
            seat = self.Seat(room_id=room.room_id, project_id=project.project_id, code="S1", person_id=persons["P1"].person_id, enabled=True)
            session.add(seat)
            session.commit()
            self.project_id, self.room_id = project.project_id, room.room_id
            self.p1_id, self.p2_id = persons["P1"].person_id, persons["P2"].person_id
            self.seat_id = seat.seat_id

    def test_01_create_check_in(self):
        status, body = self._create({"fact_type": "check_in", "project_id": self.project_id, "room_id": self.room_id,
                                     "person_id": self.p1_id, "seat_id": self.seat_id, "phase_id": "DISCIPLINE", "note": "到场签到"})
        self.assertEqual(status, 201)
        self.assertEqual(body["person_name"], "专家甲")
        self.assertEqual(body["actor"], "wyh")

    def test_02_invalid_fact_type_rejected(self):
        status, body = self._create({"fact_type": "illegal", "project_id": self.project_id, "room_id": self.room_id})
        self.assertEqual(status, 422)

    def test_03_project_room_mismatch_rejected(self):
        with self.Session() as session:
            other = self.Project(code="Q" + uuid.uuid4().hex[:8], name="其他项目", owner="测试", status="running")
            session.add(other)
            session.commit()
            other_id = other.project_id
        status, body = self._create({"fact_type": "check_in", "project_id": other_id, "room_id": self.room_id})
        self.assertEqual(status, 422)

    def test_04_person_must_join_project(self):
        with self.Session() as session:
            stranger = self.Person(code="S" + uuid.uuid4().hex[:8], name="外部人员", unit="其他单位", role="staff", enabled=True)
            session.add(stranger)
            session.commit()
            stranger_id = stranger.person_id
        status, body = self._create({"fact_type": "check_in", "project_id": self.project_id, "room_id": self.room_id, "person_id": stranger_id})
        self.assertEqual(status, 422)

    def test_05_change_requires_replaced_person(self):
        status, body = self._create({"fact_type": "change", "project_id": self.project_id, "room_id": self.room_id, "person_id": self.p2_id})
        self.assertEqual(status, 422)
        status, body = self._create({"fact_type": "change", "project_id": self.project_id, "room_id": self.room_id,
                                     "person_id": self.p2_id, "seat_id": self.seat_id,
                                     "payload": {"replaced_person_id": self.p1_id}, "note": "专家甲回避，专家乙递补"})
        self.assertEqual(status, 201)
        self.assertEqual(body["replaced_person_name"], "专家甲")

    def test_06_change_same_person_rejected(self):
        status, body = self._create({"fact_type": "change", "project_id": self.project_id, "room_id": self.room_id,
                                     "person_id": self.p1_id, "payload": {"replaced_person_id": self.p1_id}})
        self.assertEqual(status, 422)

    def test_07_leave_permit_requires_note(self):
        status, body = self._create({"fact_type": "leave_permit", "project_id": self.project_id, "room_id": self.room_id, "person_id": self.p1_id})
        self.assertEqual(status, 422)
        status, body = self._create({"fact_type": "leave_permit", "project_id": self.project_id, "room_id": self.room_id,
                                     "person_id": self.p1_id, "phase_id": "DETAIL", "note": "临时离岗10分钟，已许可"})
        self.assertEqual(status, 201)

    def test_08_filter_and_delete(self):
        self._create({"fact_type": "check_in", "project_id": self.project_id, "room_id": self.room_id, "person_id": self.p1_id})
        self._create({"fact_type": "clarification", "project_id": self.project_id, "room_id": self.room_id,
                      "person_id": self.p1_id, "payload": {"question": "澄清交付周期"}, "note": "已答复"})
        status, body = self._list(fact_type="check_in")
        self.assertEqual(status, 200)
        self.assertEqual(body["total"], 1)
        fact_id = body["items"][0]["fact_id"]
        response = self.client.delete(f"/api/facts/{fact_id}", headers={"X-Requested-With": "XMLHttpRequest"})
        self.assertEqual(response.status_code, 200)
        status, _ = self._list(fact_type="check_in")
        self.assertEqual(status, 200)
        self.assertEqual(self._list(fact_type="check_in")[1]["total"], 0)

    def _create(self, body):
        response = _request(self.client, "POST", "/api/facts", body)
        return response.status_code, _json(response)

    def _list(self, **params):
        from urllib.parse import urlencode
        query = {"project_id": self.project_id, **params}
        response = self.client.get("/api/facts?" + urlencode(query), headers={"X-Requested-With": "XMLHttpRequest"})
        return response.status_code, _json(response)


def _json(response):
    try:
        return response.json()
    except Exception:
        return {}


if __name__ == "__main__":
    unittest.main()
