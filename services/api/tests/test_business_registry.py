"""基础业务配置回归：临时 PostgreSQL schema、独立应用，不导入 app.main。"""
import os
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker


class BusinessRegistryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from app.business_models import (
            BusinessEventContext, BusinessParticipant, BusinessPerson, BusinessProject,
            BusinessRoom, BusinessSeat, RegistryBootstrap,
        )
        from app.core.business_registry import ensure_business_seed
        from app.db import get_session
        from app.models import Base, Event
        from app.routers.business_registry import router

        cls.Project, cls.Room, cls.Person = BusinessProject, BusinessRoom, BusinessPerson
        cls.Seat, cls.Participant = BusinessSeat, BusinessParticipant
        cls.Context, cls.Bootstrap, cls.Event = BusinessEventContext, RegistryBootstrap, Event
        cls.seed = staticmethod(ensure_business_seed)
        cls.Base = Base
        url = os.environ.get("WORKFLOW_TEST_DATABASE_URL")
        if not url:
            raise unittest.SkipTest("请显式配置 WORKFLOW_TEST_DATABASE_URL，使用专用测试数据库")
        cls.admin_engine = create_engine(url)
        cls.schema = "test_registry_" + uuid.uuid4().hex
        with cls.admin_engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{cls.schema}"'))
        try:
            cls.engine = create_engine(url, connect_args={"options": f"-csearch_path={cls.schema}"})
            from app.migrations import upgrade_schema
            upgrade_schema(cls.engine)
            cls.Session = sessionmaker(bind=cls.engine)
            cls.app = FastAPI()
            cls.app.include_router(router, prefix="/api")

            def override_session():
                with cls.Session() as session:
                    yield session

            cls.app.dependency_overrides[get_session] = override_session
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
        with self.engine.begin() as connection:
            names = ",".join(f'"{table.name}"' for table in self.Base.metadata.sorted_tables)
            connection.execute(text("TRUNCATE " + names + " CASCADE"))
        self.client = TestClient(self.app)

    def tearDown(self):
        self.client.close()

    def create(self, entity, body):
        response = self.client.post("/api/registry/" + entity, json=body)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def project(self, code="P1", **changes):
        return self.create("projects", {"code": code, "name": "评标项目", "owner": "项目业主", "status": "running", **changes})

    def room(self, project, code="R1", **changes):
        return self.create("rooms", {"project_id": project["project_id"], "code": code, "name": "评标室", "location": "一层", "mode": "centralized", "enabled": True, **changes})

    def person(self, code="U1", **changes):
        return self.create("persons", {"code": code, "name": "张专家", "unit": "专家单位", "role": "expert", "enabled": True, **changes})

    def member(self, project, person, **changes):
        return self.create("participants", {"project_id": project["project_id"], "person_id": person["person_id"], "role": "expert", **changes})

    def seat(self, room, code="S1", person=None, **changes):
        return self.create("seats", {"room_id": room["room_id"], "code": code, "person_id": person["person_id"] if person else None, "enabled": True, **changes})

    def update(self, entity, key, record, **changes):
        return self.client.put(f"/api/registry/{entity}/{record[key]}", json={**record, **changes})

    def test_seed_once_is_editable_has_six_rooms_and_no_people(self):
        self.seed(self.engine)
        first = self.client.get("/api/registry").json()
        self.assertEqual(set(first), {"projects", "rooms", "persons", "seats", "participants"})
        self.assertEqual(len(first["projects"]), 1)
        self.assertEqual(first["projects"][0]["name"], "业务演示项目")
        self.assertEqual({r["name"] for r in first["rooms"]}, {"评标室 A01", "评标室 A02", "异地专家组 B01", "评标室 A03", "评标室 A04", "评标室 A05"})
        self.assertEqual(first["persons"], [])
        self.assertEqual(self.update("projects", "project_id", first["projects"][0], name="已改名").status_code, 200)
        self.seed(self.engine)
        self.assertEqual(self.client.get("/api/registry").json()["projects"][0]["name"], "已改名")
        for room in first["rooms"]:
            self.assertEqual(self.client.delete(f"/api/registry/rooms/{room['room_id']}").json(), {"ok": True})
        self.assertEqual(self.client.delete(f"/api/registry/projects/{first['projects'][0]['project_id']}").status_code, 200)
        self.seed(self.engine)
        self.assertEqual(self.client.get("/api/registry").json()["projects"], [])

    def test_concurrent_seed_runs_only_once(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda _: self.seed(self.engine), range(2)))
        result = self.client.get("/api/registry").json()
        self.assertEqual(len(result["projects"]), 1)
        self.assertEqual(len(result["rooms"]), 6)

    def test_crud_persists_disabled_config_and_seat_project(self):
        project = self.project()
        room = self.room(project, enabled=False, mode="remote")
        person = self.person(enabled=False)
        membership = self.member(project, person, role="supervisor")
        seat = self.seat(room, enabled=False)
        self.assertEqual(seat["project_id"], project["project_id"])
        self.assertEqual(self.update("rooms", "room_id", room, name="异地评标室").status_code, 200)
        self.assertEqual(self.update("persons", "person_id", person, unit="新单位").status_code, 200)
        self.assertEqual(self.update("participants", "membership_id", membership, role="staff").status_code, 200)
        self.assertEqual(self.update("seats", "seat_id", seat, code="S2").status_code, 200)
        snapshot = self.client.get("/api/registry").json()
        self.assertEqual(snapshot["rooms"][0]["name"], "异地评标室")
        self.assertFalse(snapshot["rooms"][0]["enabled"])
        self.assertFalse(snapshot["persons"][0]["enabled"])
        for entity, key, record in (("seats", "seat_id", seat), ("participants", "membership_id", membership), ("persons", "person_id", person), ("rooms", "room_id", room), ("projects", "project_id", project)):
            self.assertEqual(self.client.delete(f"/api/registry/{entity}/{record[key]}").json(), {"ok": True})
        self.assertTrue(all(not rows for rows in self.client.get("/api/registry").json().values()))

    def test_codes_and_memberships_are_unique_on_create_and_edit(self):
        project, other_project = self.project(), self.project("P2")
        person, other_person = self.person(), self.person("U2")
        room, other_room = self.room(project), self.room(project, "R2")
        self.room(other_project)  # room code is unique only within its project
        seat, other_seat = self.seat(room), self.seat(room, "S2")
        self.seat(other_room)  # seat code is unique only within its room
        membership = self.member(project, person)
        other_member = self.member(project, other_person)
        for entity, key, record, changes in (("projects", "project_id", other_project, {"code": project["code"]}), ("persons", "person_id", other_person, {"code": person["code"]}), ("rooms", "room_id", other_room, {"code": room["code"]}), ("seats", "seat_id", other_seat, {"code": seat["code"]}), ("participants", "membership_id", other_member, {"person_id": person["person_id"]})):
            with self.subTest(entity=entity):
                response = self.client.post("/api/registry/" + entity, json={**record, **changes})
                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(self.update(entity, key, record, **changes).status_code, 409)

    def test_assignment_requires_membership_active_person_and_one_seat_per_project(self):
        project, other = self.project(), self.project("P2")
        room, room2, foreign_room = self.room(project), self.room(project, "R2"), self.room(other)
        person = self.person()
        body = {"room_id": room["room_id"], "code": "S1", "person_id": person["person_id"], "enabled": True}
        self.assertEqual(self.client.post("/api/registry/seats", json=body).status_code, 422)
        self.member(other, person)
        self.assertEqual(self.client.post("/api/registry/seats", json=body).status_code, 422)
        self.member(project, person)
        self.assertEqual(self.update("persons", "person_id", person, enabled=False).status_code, 200)
        self.assertEqual(self.client.post("/api/registry/seats", json=body).status_code, 422)
        self.assertEqual(self.update("persons", "person_id", person, enabled=True).status_code, 200)
        assigned = self.seat(room, person=person)
        empty = self.seat(room2)
        self.assertEqual(self.update("seats", "seat_id", empty, person_id=person["person_id"]).status_code, 409)
        self.seat(foreign_room, person=person)  # same person may sit in a different project
        self.assertEqual(self.update("seats", "seat_id", assigned, person_id=None).status_code, 200)
        self.assertEqual(self.update("seats", "seat_id", empty, person_id=person["person_id"]).status_code, 200)

    def test_concurrent_assignment_has_one_winner(self):
        project = self.project()
        room = self.room(project)
        person = self.person()
        self.member(project, person)
        def assign(code):
            with TestClient(self.app) as client:
                return client.post("/api/registry/seats", json={"room_id": room["room_id"], "code": code, "person_id": person["person_id"], "enabled": True}).status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(assign, ["S1", "S2"]))
        self.assertEqual(sorted(results), [201, 409])
        self.assertEqual(len(self.client.get("/api/registry").json()["seats"]), 1)

    def test_parent_and_membership_changes_cannot_bypass_assignments(self):
        project, other = self.project(), self.project("P2")
        room, foreign_room = self.room(project), self.room(other)
        person, person2 = self.person(), self.person("U2")
        membership = self.member(project, person)
        seat = self.seat(room, person=person)
        self.assertEqual(self.update("rooms", "room_id", room, project_id=other["project_id"]).status_code, 409)
        self.assertEqual(self.update("seats", "seat_id", seat, room_id=foreign_room["room_id"]).status_code, 422)
        self.assertEqual(self.update("participants", "membership_id", membership, person_id=person2["person_id"]).status_code, 409)
        self.assertEqual(self.client.delete(f"/api/registry/participants/{membership['membership_id']}").status_code, 409)
        self.assertEqual(self.client.delete(f"/api/registry/persons/{person['person_id']}").status_code, 409)
        self.assertEqual(self.client.delete(f"/api/registry/rooms/{room['room_id']}").status_code, 409)
        self.assertEqual(self.client.delete(f"/api/registry/projects/{project['project_id']}").status_code, 409)

    def test_unassigned_room_and_seat_can_move_and_derive_new_project(self):
        project, other = self.project(), self.project("P2")
        room, foreign_room = self.room(project), self.room(other)
        response = self.update("rooms", "room_id", room, project_id=other["project_id"], code="R2")
        self.assertEqual(response.status_code, 200, response.text)
        seat = self.seat(room)
        response = self.update("seats", "seat_id", seat, room_id=foreign_room["room_id"])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["project_id"], other["project_id"])

    def test_invalid_associations_and_missing_targets_have_distinct_errors(self):
        missing = str(uuid.uuid4())
        project = self.project()
        room = self.room(project)
        person = self.person()
        for entity, body in (("rooms", {**room, "project_id": missing}), ("seats", {"room_id": missing, "code": "S1", "enabled": True}), ("seats", {"room_id": room["room_id"], "person_id": missing, "code": "S1", "enabled": True}), ("participants", {"project_id": missing, "person_id": person["person_id"], "role": "expert"})):
            self.assertEqual(self.client.post("/api/registry/" + entity, json=body).status_code, 422)
        self.assertEqual(self.update("rooms", "room_id", room, project_id=missing).status_code, 422)
        for entity in ("projects", "rooms", "persons", "seats", "participants"):
            self.assertEqual(self.client.delete(f"/api/registry/{entity}/{missing}").status_code, 404)
        self.assertEqual(self.client.put(f"/api/registry/projects/{missing}", json=project).status_code, 404)

    def test_whitespace_lengths_dates_and_enums_are_validated(self):
        project = self.project(code="  P1  ", name="  项目  ")
        self.assertEqual(project["code"], "P1")
        self.assertEqual(project["name"], "项目")
        invalid = [{"name": " \t "}, {"code": "x" * 65}, {"status": "unknown"}, {"start_at": "2026-10-09T00:00:00Z", "end_at": "2026-10-08T00:00:00Z"}]
        for changes in invalid:
            self.assertEqual(self.update("projects", "project_id", project, **changes).status_code, 422)
        response = self.update("projects", "project_id", project, start_at="2026-10-08T08:00:00+08:00", end_at="2026-10-08T01:00:00Z")
        self.assertEqual(response.status_code, 200, response.text)
        room, person = self.room(project), self.person()
        self.assertEqual(self.update("rooms", "room_id", room, mode="invalid").status_code, 422)
        self.assertEqual(self.update("rooms", "room_id", room, location=" ").status_code, 422)
        self.assertEqual(self.update("persons", "person_id", person, role="invalid").status_code, 422)
        self.assertEqual(self.update("persons", "person_id", person, unit=" ").status_code, 422)
        self.assertEqual(self.client.post("/api/registry/participants", json={"project_id": project["project_id"], "person_id": person["person_id"], "role": "invalid"}).status_code, 422)
        self.assertEqual(self.client.post("/api/registry/seats", json={"room_id": "not-uuid", "code": "S1"}).status_code, 422)

    def test_historical_context_protects_entities_and_snapshot_survives_rename(self):
        project = self.project()
        room = self.room(project)
        person = self.person()
        membership = self.member(project, person)
        seat = self.seat(room, person=person)
        event_id = str(uuid.uuid4())
        snapshot = {"project_name": project["name"], "room_name": room["name"], "seat_code": seat["code"], "person_name": person["name"]}
        with self.Session() as db:
            db.add(self.Event(event_id=event_id, event_type="sensitive_word", status="PENDING"))
            db.flush()
            db.add(self.Context(event_id=event_id, project_id=project["project_id"], room_id=room["room_id"], seat_id=seat["seat_id"], person_id=person["person_id"], snapshot=snapshot))
            db.commit()
        for entity, key, record, changes in (("projects", "project_id", project, {"name": "新项目"}), ("rooms", "room_id", room, {"name": "新房间"}), ("persons", "person_id", person, {"name": "新姓名"}), ("seats", "seat_id", seat, {"code": "S2", "person_id": None})):
            self.assertEqual(self.update(entity, key, record, **changes).status_code, 200)
            self.assertEqual(self.client.delete(f"/api/registry/{entity}/{record[key]}").status_code, 409)
        self.assertEqual(self.client.delete(f"/api/registry/participants/{membership['membership_id']}").status_code, 409)
        with self.Session() as db:
            self.assertEqual(db.get(self.Context, event_id).snapshot, snapshot)
        self.assertEqual(self.update("rooms", "room_id", room, project_id=self.project("P2")["project_id"]).status_code, 409)

    def test_database_constraints_enforce_foreign_keys_and_assignment_uniqueness(self):
        project = self.project()
        room = self.room(project)
        person = self.person()
        self.member(project, person)
        self.seat(room, person=person)
        invalid_rows = [
            self.Room(room_id=str(uuid.uuid4()), project_id=str(uuid.uuid4()), code="BAD", name="房间", location="一层", mode="centralized", enabled=True),
            self.Seat(seat_id=str(uuid.uuid4()), room_id=room["room_id"], project_id=project["project_id"], code="S2", person_id=person["person_id"], enabled=True),
            self.Project(project_id=str(uuid.uuid4()), code=project["code"], name="重复", owner="业主", status="draft"),
            self.Person(person_id=str(uuid.uuid4()), code="BAD", name="人员", unit="单位", role="invalid", enabled=True),
            self.Context(event_id=str(uuid.uuid4()), project_id=project["project_id"], room_id=room["room_id"], snapshot={}),
        ]
        for row in invalid_rows:
            with self.subTest(model=type(row).__name__), self.Session() as db:
                db.add(row)
                with self.assertRaises(IntegrityError):
                    db.commit()
                db.rollback()

    def test_historical_seat_and_room_ids_cannot_be_moved(self):
        project, other = self.project(), self.project("P2")
        room, sibling, foreign_room = self.room(project), self.room(project, "R2"), self.room(other)
        seat = self.seat(room)
        event_id = str(uuid.uuid4())
        with self.Session() as db:
            db.add(self.Event(event_id=event_id, status="PENDING"))
            db.flush()
            db.add(self.Context(event_id=event_id, project_id=project["project_id"], room_id=room["room_id"], seat_id=seat["seat_id"], snapshot={"room_name": room["name"]}))
            db.commit()
        for target in (sibling, foreign_room):
            self.assertEqual(self.update("seats", "seat_id", seat, room_id=target["room_id"]).status_code, 409)
        self.assertEqual(self.update("rooms", "room_id", room, project_id=other["project_id"]).status_code, 409)
        with self.Session() as db:
            context = db.get(self.Context, event_id)
            self.assertEqual((context.project_id, context.room_id, context.seat_id), (project["project_id"], room["room_id"], seat["seat_id"]))
            self.assertEqual(db.get(self.Seat, seat["seat_id"]).room_id, room["room_id"])

    def test_event_cleanup_cascades_context_but_preserves_configuration(self):
        project = self.project()
        room = self.room(project)
        event_id = str(uuid.uuid4())
        with self.Session() as db:
            db.add(self.Event(event_id=event_id, status="PENDING"))
            db.flush()
            db.add(self.Context(event_id=event_id, project_id=project["project_id"], room_id=room["room_id"], snapshot={"room_name": room["name"]}))
            db.commit()
        self.assertEqual(self.client.delete(f"/api/registry/rooms/{room['room_id']}").status_code, 409)
        with self.Session() as db:
            db.query(self.Event).filter(self.Event.event_id == event_id).delete(synchronize_session=False)
            db.commit()
            self.assertIsNone(db.get(self.Context, event_id))
            self.assertIsNotNone(db.get(self.Room, room["room_id"]))
            self.assertIsNotNone(db.get(self.Project, project["project_id"]))
        self.assertEqual(self.client.delete(f"/api/registry/rooms/{room['room_id']}").status_code, 200)


if __name__ == "__main__":
    unittest.main()
