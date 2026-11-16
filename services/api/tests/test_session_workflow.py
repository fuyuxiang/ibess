"""Managed sessions use the production authentication, migrations and PostgreSQL API."""

from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from test_supervision import SupervisionTest


class SessionWorkflowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        SupervisionTest.setUpClass()
        cls.app, cls.Session, cls.engine = (
            SupervisionTest.app,
            SupervisionTest.Session,
            SupervisionTest.engine,
        )

    @classmethod
    def tearDownClass(cls):
        SupervisionTest.tearDownClass()

    def setUp(self):
        self.fixture = SupervisionTest()
        self.fixture.setUp()
        self.client = self.fixture.client
        self.app.state.phase_control_required_consumers = ["event-service"]

    def tearDown(self):
        self.app.state.phase_control_required_consumers = None
        self.fixture.tearDown()

    def read(self, sid):
        r = self.client.get("/api/supervision/workflows/" + sid)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def write(self, sid, action, *, method="POST", expected=None, **body):
        return self.client.request(
            method,
            f"/api/supervision/workflows/{sid}/{action}",
            json={
                "expected_version": self.read(sid)["version"]
                if expected is None
                else expected,
                **body,
            },
        )

    def create(self):
        project, room = self.fixture.business()
        r = self.client.post(
            "/api/supervision/workflows",
            json={
                "name": "独立监管测试场次",
                "project_id": project["project_id"],
                "room_id": room["room_id"],
                "source_id": "camera-001",
                "expected_people": 1,
            },
        )
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()["session_id"], project, room

    def prepare(self):
        from app.business_authz_models import (
            BusinessAuthorizationEntry,
            BusinessAuthorizationSnapshot,
        )
        from app.business_models import BusinessPerson, BusinessParticipant
        from app.zone_config_models import SourceZoneConfig

        sid, project, room = self.create()
        with self.Session.begin() as db:
            person = BusinessPerson(
                code=uuid4().hex,
                name="测试人员",
                role="expert",
                unit="测试",
                enabled=True,
            )
            db.add(person)
            db.flush()
            db.add(
                BusinessParticipant(
                    project_id=project["project_id"],
                    person_id=person.person_id,
                    role="expert",
                )
            )
            roster = BusinessAuthorizationSnapshot(
                project_id=project["project_id"],
                room_id=room["room_id"],
                created_by="test_admin",
                created_at=datetime.now(timezone.utc),
            )
            db.add(roster)
            db.flush()
            db.add(
                BusinessAuthorizationEntry(
                    snapshot_id=roster.snapshot_id,
                    person_id=person.person_id,
                    role="expert",
                    identity_verified=True,
                )
            )
            db.add(
                SourceZoneConfig(
                    config_id=str(uuid4()),
                    session_id=sid,
                    project_id=project["project_id"],
                    room_id=room["room_id"],
                    source_id="camera-001",
                    config_version=1,
                    zones=[
                        {
                            "kind": "seat",
                            "zone_id": "seat-01",
                            "polygon": [[0, 0], [0.5, 0], [0.5, 0.5]],
                        }
                    ],
                    entry_lines=[
                        {"line_id": "entry-01", "points": [[0, 0.1], [1, 0.1]]}
                    ],
                )
            )
        r = self.write(
            sid,
            "checks",
            method="PUT",
            checks={
                "room_inspected": True,
                "identity_checked": True,
                "devices_stored": True,
            },
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["readiness"]["ready"])
        return sid, project, room

    def running(self):
        sid, project, room = self.prepare()
        r = self.write(sid, "start")
        self.assertEqual(r.status_code, 200, r.text)
        return sid, project, room

    def prepare_without_people(self):
        from app.zone_config_models import SourceZoneConfig
        sid, project, room = self.create()
        with self.Session.begin() as db:
            db.add(SourceZoneConfig(config_id=str(uuid4()),session_id=sid,
                project_id=project['project_id'],room_id=room['room_id'],source_id='camera-001',config_version=1,
                zones=[{'kind':'seat','zone_id':'seat-1','polygon':[[0,0],[1,0],[1,1]]}],
                entry_lines=[{'line_id':'entry-1','points':[[0,.1],[1,.1]]}]))
        response = self.write(sid, 'checks', method='PUT', checks={'room_inspected':True,'devices_stored':True})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()['readiness']['ready'])
        return sid, project, room

    def test_basic_monitoring_starts_and_archives_without_roster_or_faces(self):
        from app.models import EvalSession
        from app.business_authz_models import BusinessAuthorizationSnapshot
        from app.face_models import FaceProfile
        from app import face_auto
        sid, _, _ = self.prepare_without_people()
        before = self.read(sid)
        self.assertFalse(before['identity_verification_enabled'])
        self.assertFalse(before['checks'].get('identity_checked'))
        self.assertTrue(all(not c['required'] for c in before['readiness']['items'] if c['key'] in ('roster','identity')))
        with self.Session() as db:
            self.assertEqual(db.query(BusinessAuthorizationSnapshot).count(), 0)
            self.assertEqual(db.query(FaceProfile).count(), 0)
        self.assertEqual(self.write(sid,'start').status_code, 200)
        with patch.object(face_auto,'SessionLocal',self.Session), patch.object(face_auto,'capture_and_match') as matching:
            self.assertEqual(face_auto.run_once('cam1'), 0)
            matching.assert_not_called()
        self.assertEqual(self.write(sid,'finish',note='合成测试：基础行为监管结束，继续收尾核对').status_code,200)
        with self.Session.begin() as db:
            db.query(EvalSession).filter_by(session_id=sid).one().end_at = datetime.utcnow()-timedelta(seconds=100)
        self.assertEqual(self.write(sid,'checks',method='PUT',checks={'materials_reconciled':True,'room_cleared':True}).status_code,200)
        archived = self.write(sid,'archive',note='合成测试：无人员名单的基础监管完成归档',evidence_gap_note='测试环境没有接入现场采样，保留覆盖待核验事实。')
        self.assertEqual(archived.status_code,201,archived.text)
        report = self.client.get('/api/supervision/reports/'+archived.json()['report_id'])
        self.assertEqual(report.status_code,200)
        self.assertIs(report.json()['snapshot']['identity_verification_enabled'],False)

    def test_identity_opt_in_is_strict_versioned_and_enforced_only_when_enabled(self):
        sid, _, _ = self.prepare_without_people()
        version = self.read(sid)['version']
        self.assertEqual(self.write(sid,'identity-setting',method='PUT',enabled='false').status_code,422)
        enabled = self.write(sid,'identity-setting',method='PUT',enabled=True)
        self.assertEqual(enabled.status_code,200,enabled.text)
        self.assertTrue(enabled.json()['identity_verification_enabled'])
        self.assertFalse(enabled.json()['readiness']['ready'])
        self.assertEqual(self.write(sid,'start').status_code,409)
        self.assertEqual(self.write(sid,'identity-setting',method='PUT',expected=version,enabled=False).status_code,409)
        disabled = self.write(sid,'identity-setting',method='PUT',enabled=False)
        self.assertTrue(disabled.json()['readiness']['ready'])
        self.assertEqual(self.write(sid,'start').status_code,200)
        self.assertEqual(self.write(sid,'identity-setting',method='PUT',enabled=True).status_code,409)

    def test_enabled_identity_preserves_existing_roster_and_automatic_matching(self):
        from app import face_auto
        from app.business_authz_models import BusinessAuthorizationSnapshot
        sid, _, room = self.prepare()
        with self.Session() as db:
            previous = [s.snapshot_id for s in db.query(BusinessAuthorizationSnapshot).filter_by(room_id=room['room_id'])]
        self.assertEqual(self.write(sid,'identity-setting',method='PUT',enabled=True).status_code,200)
        self.assertTrue(self.read(sid)['readiness']['ready'])
        self.assertEqual(self.write(sid,'start').status_code,200)
        with patch.object(face_auto,'SessionLocal',self.Session), patch.object(face_auto,'capture_and_match',return_value={'recorded':True}) as matching:
            self.assertEqual(face_auto.run_once('cam1'),1)
            matching.assert_called_once()
        with self.Session() as db:
            self.assertEqual([s.snapshot_id for s in db.query(BusinessAuthorizationSnapshot).filter_by(room_id=room['room_id'])],previous)

    def closed(self):
        from app.models import EvalSession

        sid, project, room = self.running()
        r = self.write(sid, "finish", note="现场人员已离场，后续继续核对资料")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["lifecycle"], "closing")
        self.assertEqual(self.write(sid, "archive", note="完成核验").status_code, 409)
        with self.Session.begin() as db:
            db.query(EvalSession).filter_by(session_id=sid).one().end_at = (
                datetime.utcnow() - timedelta(seconds=100)
            )
        r = self.write(
            sid,
            "checks",
            method="PUT",
            checks={"materials_reconciled": True, "room_cleared": True},
        )
        self.assertEqual(r.status_code, 200, r.text)
        return sid, project, room

    def test_preparation_guard_version_actor_and_legacy_bypass(self):
        sid, _, _ = self.create()
        r = self.write(sid, "start")
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(r.json()["detail"]["code"], "PREPARATION_INCOMPLETE")
        self.assertEqual(
            self.client.post(f"/api/sessions/{sid}/start").status_code, 409
        )
        version = self.read(sid)["version"]
        r = self.write(sid, "checks", method="PUT", checks={"room_inspected": True})
        self.assertEqual(r.json()["checks"]["room_inspected"]["actor"], "test_admin")
        self.assertEqual(
            self.write(
                sid,
                "checks",
                method="PUT",
                expected=version,
                checks={"room_inspected": False},
            ).status_code,
            409,
        )
        self.assertEqual(
            self.write(
                sid, "checks", method="PUT", checks={"room_inspected": "false"}
            ).status_code,
            422,
        )
        self.assertFalse(self.read(sid)["readiness"]["ready"])

    def test_saved_phase_requires_real_ack_and_plans_must_be_reapplied(self):
        sid, _, _ = self.prepare()
        data = self.read(sid)
        self.assertEqual(data["control"]["sync_status"], "pending")
        plans = {p["id"]: p["scenes"] for p in data["phases"]}
        plans["SIGNIN"] = ["S01", "S10"]
        self.assertEqual(
            self.write(sid, "plan", method="PUT", phase_plans=plans).status_code, 200
        )
        self.assertFalse(self.read(sid)["readiness"]["ready"])
        self.assertEqual(
            self.write(
                sid, "phase", phase="SIGNIN", note="应用核对后的初始方案"
            ).status_code,
            200,
        )
        self.assertTrue(self.read(sid)["readiness"]["ready"])
        self.assertEqual(self.write(sid, "start").status_code, 200)
        self.assertEqual(
            self.write(sid, "phase", phase="DETAIL", note="试图跳过阶段").status_code,
            409,
        )
        result = self.write(
            sid, "phase", phase="DISCIPLINE", note="已入场，宣读评标纪律"
        )
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["phase_id"], "DISCIPLINE")
        self.assertEqual(result.json()["control"]["sync_status"], "pending")
        self.assertEqual(self.client.post(f"/api/sessions/{sid}/stop").status_code, 409)

    def test_camera_alias_conflict_blocks_another_prepared_session(self):
        from app.models import EvalSession
        from app.phase_models import PhaseBinding
        from app.session_workflow_models import SessionWorkflow
        from app.zone_config_models import SourceZoneConfig
        from copy import deepcopy

        sid, project, room = self.prepare()
        other = str(uuid4())
        with self.Session.begin() as db:
            db.add(
                EvalSession(
                    session_id=other,
                    name="同机位后续场次",
                    status="draft",
                    current_phase="SIGNIN",
                    expected_people=1,
                )
            )
            db.flush()
            db.add(
                PhaseBinding(
                    session_id=other,
                    project_id=project["project_id"],
                    room_id=room["room_id"],
                    source_id="cam1",
                )
            )
            db.flush()
            original = db.get(SessionWorkflow, sid)
            db.add(
                SessionWorkflow(
                    session_id=other,
                    version=0,
                    checks=deepcopy(original.checks),
                    phase_plans=deepcopy(original.phase_plans),
                )
            )
            zone = db.query(SourceZoneConfig).filter_by(session_id=sid).one()
            db.add(
                SourceZoneConfig(
                    config_id=str(uuid4()),
                    session_id=other,
                    project_id=project["project_id"],
                    room_id=room["room_id"],
                    source_id="cam1",
                    config_version=1,
                    zones=zone.zones,
                    entry_lines=zone.entry_lines,
                )
            )
        self.assertEqual(
            self.write(
                other, "phase", phase="SIGNIN", note="同机位后续场次准备"
            ).status_code,
            200,
        )
        self.assertTrue(self.read(other)["readiness"]["ready"])
        self.assertEqual(self.write(sid, "start").status_code, 200)
        self.assertFalse(self.read(other)["readiness"]["ready"])
        self.assertEqual(self.write(other, "start").status_code, 409)
        self.assertEqual(
            self.write(sid, "finish", note="前一场次现场结束").status_code, 200
        )
        self.assertTrue(self.read(other)["readiness"]["ready"])
        self.assertEqual(self.write(other, "start").status_code, 200)

    def test_workflow_permissions_are_enforced_by_server(self):
        sid, _, _ = self.prepare()
        for role in ("supervisor", "operator", "viewer"):
            username = "user_" + role
            created = self.client.post(
                "/api/auth/team",
                json={
                    "username": username,
                    "display_name": role,
                    "role": role,
                    "password": "IntegrationTeam12345",
                },
            )
            self.assertEqual(created.status_code, 201, created.text)
            with TestClient(
                self.app, headers={"X-Requested-With": "XMLHttpRequest"}
            ) as client:
                login = client.post(
                    "/api/auth/login",
                    json={"username": username, "password": "IntegrationTeam12345"},
                )
                self.assertEqual(login.status_code, 200, login.text)
                client.headers["X-CSRF-Token"] = login.json()["csrf_token"]
                self.assertEqual(
                    client.get(f"/api/supervision/workflows/{sid}").status_code, 200
                )
                self.assertEqual(
                    client.post("/api/supervision/workflows", json={}).status_code, 403
                )
                self.assertEqual(
                    client.put(
                        f"/api/supervision/workflows/{sid}/plan", json={}
                    ).status_code,
                    403,
                )
                self.assertEqual(client.put(f'/api/supervision/workflows/{sid}/identity-setting',json={
                    'expected_version':self.read(sid)['version'],'enabled':True}).status_code,403)
                result = client.post(
                    f"/api/supervision/workflows/{sid}/checks", json={}
                )
                self.assertNotEqual(result.status_code, 200)
                result = client.put(
                    f"/api/supervision/workflows/{sid}/checks",
                    json={
                        "expected_version": self.read(sid)["version"],
                        "checks": {"room_inspected": True},
                    },
                )
                self.assertEqual(
                    result.status_code,
                    200 if role == "supervisor" else 403,
                    result.text,
                )

    def test_patrol_handover_and_permit_are_atomic_and_expiring(self):
        from app.business_facts_models import BusinessAttendanceFact
        from app.phase_models import PhaseSnapshot

        sid, _, _ = self.running()
        for kind in ("patrol", "handover"):
            r = self.write(sid, "journal", kind=kind, note="现场核验与接续安排已登记")
            self.assertEqual(r.status_code, 201, r.text)
        until = (datetime.now(timezone.utc) + timedelta(minutes=20)).isoformat()
        result = self.write(
            sid,
            "permit",
            authorization_kind="contact_allowed",
            valid_until=until,
            note="经监管人员确认，进行本项目澄清讨论",
        )
        self.assertEqual(result.status_code, 201, result.text)
        import sys
        from pathlib import Path

        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "vision-worker"))
        from worker.a_context import DbContextSource

        context = DbContextSource("", engine_factory=lambda: self.engine).load(
            "camera-001"
        )
        self.assertTrue(
            any(a["kind"] == "contact_allowed" for a in context.authorizations)
        )
        record = result.json()["journal"][0]
        self.assertEqual((record["kind"], record["actor"]), ("permit", "test_admin"))
        with self.Session() as db:
            fact = db.get(BusinessAttendanceFact, record["detail"]["fact_id"])
            self.assertEqual(fact.payload["authorization_kind"], "contact_allowed")
            snapshot = (
                db.query(PhaseSnapshot)
                .filter_by(session_id=sid)
                .order_by(PhaseSnapshot.phase_version.desc())
                .first()
            )
            self.assertEqual(
                snapshot.exceptions, [{"fact_id": fact.fact_id, "fact_version": 1}]
            )
            count = db.query(BusinessAttendanceFact).count()
        for patch in (
            {"valid_until": "2020-01-01T00:00:00Z"},
            {"valid_until": "2030-01-01T00:00:00"},
            {"note": "字" * 501},
            {"seat_id": str(uuid4())},
        ):
            r = self.write(
                sid,
                "permit",
                **{
                    "authorization_kind": "break",
                    "valid_until": until,
                    "note": "测试拒绝无效许可",
                    **patch,
                },
            )
            self.assertEqual(r.status_code, 422, r.text)
        with self.Session() as db:
            self.assertEqual(db.query(BusinessAttendanceFact).count(), count)
        self.assertEqual(
            len([j for j in self.read(sid)["journal"] if j["kind"] == "permit"]), 1
        )

    def test_finish_does_not_hide_pending_events_and_archive_is_versioned(self):
        from app.models import Event
        from app.supervision_models import SupervisionSessionReport

        sid, _, _ = self.closed()
        eid = str(uuid4())
        with self.Session.begin() as db:
            db.add(
                Event(
                    event_id=eid,
                    session_id=sid,
                    camera_id="camera-001",
                    risk_level="high",
                    reason="晚到现场线索",
                    event_type="phone_call",
                    status="PENDING",
                    occurred_at=datetime.utcnow() - timedelta(minutes=2),
                )
            )
        self.assertEqual(self.read(sid)["pending_events"], 1)
        self.assertEqual(
            self.write(sid, "archive", note="尚有待办不能归档").status_code, 409
        )
        dismissed = self.fixture.operation(
            {"event_id": eid},
            "dismiss",
            resolution="normal",
            note="复核原始画面确认无违规现象",
            evidence_note="测试环境无真实录像，已核查并保留资料缺口",
            acknowledge_missing_evidence=True,
        )
        self.assertEqual(dismissed.status_code, 200, dismissed.text)
        self.assertEqual(
            self.write(sid, "archive", note="已复核但需说明证据缺口").json()["detail"][
                "code"
            ],
            "EVIDENCE_EXPLANATION_REQUIRED",
        )
        first = self.write(
            sid,
            "archive",
            note="本场次收尾核查完成",
            evidence_gap_note="测试环境未接真实录像，资料缺口已如实登记",
        )
        self.assertEqual(first.status_code, 201, first.text)
        self.assertEqual(self.read(sid)["lifecycle"], "archived")
        report_id = first.json()["report_id"]
        original = self.client.get("/api/supervision/reports/" + report_id).json()
        self.assertEqual(original["snapshot"]["report_type"], "归档监管记录")
        self.assertEqual(
            self.write(
                sid, "journal", kind="supplement", note="补充交接签收与核验情况"
            ).status_code,
            201,
        )
        self.assertEqual(self.read(sid)["lifecycle"], "supplement_required")
        second = self.write(
            sid,
            "archive",
            note="补充交接签收后的归档",
            evidence_gap_note="录像来源未配置，仍如实保留资料缺口说明",
        )
        self.assertEqual(second.status_code, 201, second.text)
        self.assertEqual(self.read(sid)["lifecycle"], "archived")
        self.assertNotEqual(second.json()["report_id"], report_id)
        self.assertEqual(
            self.client.get("/api/supervision/reports/" + report_id).json(), original
        )
        with self.Session() as db:
            self.assertEqual(db.query(SupervisionSessionReport).count(), 2)
        with self.Session.begin() as db:
            db.query(Event).filter_by(
                event_id=eid
            ).one().reason = "新增补充证据后的线索描述"
        self.assertEqual(self.read(sid)["lifecycle"], "supplement_required")

    def test_unmanaged_records_are_readable_and_adoption_preserves_data(self):
        from app.models import EvalSession

        unbound = str(uuid4())
        with self.Session.begin() as db:
            db.add(
                EvalSession(session_id=unbound, name="无范围历史场次", status="closed")
            )
        self.assertFalse(self.read(unbound)["managed"])
        self.assertEqual(self.write(unbound, "adopt").status_code, 409)

        event, _, _, sid = self.fixture.bound_event()
        self.assertFalse(self.read(sid)["managed"])
        result = self.write(sid, "adopt")
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["total_events"], 1)
        self.assertEqual(result.json()["status"], "running")
        self.assertEqual(self.fixture.detail(event)["reason"], "回归实际视频线索")
        self.assertEqual(
            self.client.get(
                "/api/supervision/workflows", params={"keyword": "%"}
            ).json()["total"],
            0,
        )
