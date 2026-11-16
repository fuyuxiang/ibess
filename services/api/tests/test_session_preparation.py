"""Reusable resources, required calibration and atomic plan updates on real PostgreSQL."""

from copy import deepcopy
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_session_workflow import SessionWorkflowTest


class SessionPreparationTest(SessionWorkflowTest):
    def resource(self, room, *, version=0, channels=None, scheme="basic"):
        return self.client.post(
            "/api/supervision/room-presets/" + room["room_id"],
            json={
                "expected_version": version,
                "scheme": scheme,
                "primary_source_id": "camera-001",
                "channels": channels
                or [
                    {
                        "source_id": "camera-001",
                        "name": "全景机位",
                        "kind": "video",
                        "zones": [],
                        "entry_lines": [],
                    }
                ],
                "note": "回归测试：维护评标室资源",
            },
        )

    def scene(self, project, room, preset):
        response = self.client.post(
            "/api/supervision/workflows",
            json={
                "name": "复用资源测试场次",
                "project_id": project["project_id"],
                "room_id": room["room_id"],
                "preset_id": preset["preset_id"],
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["session_id"]

    def calibrated(self, project, room):
        seat = self.client.post(
            "/api/registry/seats",
            json={"code": "A01", "room_id": room["room_id"], "enabled": True},
        ).json()
        return [
            {
                "source_id": "camera-001",
                "name": "全景机位",
                "kind": "video",
                "zones": [
                    {
                        "zone_id": "seat-a",
                        "name": "观察工位",
                        "kind": "seat",
                        "seat_id": seat["seat_id"],
                        "polygon": [[0, 0], [0.5, 0], [0.5, 0.5]],
                    }
                ],
                "entry_lines": [
                    {
                        "line_id": "door-a",
                        "name": "入口",
                        "start": [0, 0.2],
                        "end": [1, 0.2],
                        "direction": "both",
                    }
                ],
            },
            {
                "source_id": "camera-011",
                "name": "侧面机位",
                "kind": "video",
                "zones": [],
                "entry_lines": [],
            },
        ]

    def test_basic_starts_without_any_geometry_but_standard_requires_it(self):
        sid, _, _ = self.create()
        basic = self.write(
            sid,
            "checks",
            method="PUT",
            checks={"room_inspected": True, "devices_stored": True},
        ).json()
        self.assertTrue(basic["readiness"]["ready"])
        self.assertTrue(
            all(
                not c["required"] and not c["passed"]
                for c in basic["readiness"]["items"]
                if c["key"] in ("seat_zones", "entry_lines")
            )
        )
        standard = self.write(sid, "preparation", method="PUT", scheme="standard")
        self.assertEqual(standard.status_code, 200, standard.text)
        self.assertFalse(standard.json()["readiness"]["ready"])
        self.assertEqual(self.write(sid, "start").status_code, 409)
        self.assertEqual(
            self.write(sid, "preparation", method="PUT", scheme="basic").status_code,
            200,
        )
        self.assertEqual(self.write(sid, "start").status_code, 200)

    def test_revisions_and_copied_geometry_do_not_mutate_existing_sessions(self):
        from app.zone_config_models import SourceZoneConfig

        project, room = self.fixture.business()
        channels = self.calibrated(project, room)
        first = self.resource(room, channels=channels, scheme="standard").json()
        sid = self.scene(project, room, first)
        changed = deepcopy(channels)
        changed[0]["zones"][0]["polygon"][1] = [0.8, 0]
        second = self.resource(
            room, version=1, channels=changed, scheme="standard"
        ).json()
        other = self.scene(project, room, second)
        self.assertEqual(
            self.read(sid)["preparation_snapshot"]["preset_id"], first["preset_id"]
        )
        self.assertEqual(
            self.read(other)["preparation_snapshot"]["preset_id"], second["preset_id"]
        )
        with self.Session() as db:
            a = (
                db.query(SourceZoneConfig)
                .filter_by(session_id=sid, source_id="camera-001")
                .one()
            )
            b = (
                db.query(SourceZoneConfig)
                .filter_by(session_id=other, source_id="camera-001")
                .one()
            )
            self.assertEqual(a.zones[0]["polygon"][1], [0.5, 0])
            self.assertEqual(b.zones[0]["polygon"][1], [0.8, 0])
        applied = self.write(
            sid,
            "preparation",
            method="PUT",
            scheme="standard",
            preset_id=second["preset_id"],
        )
        self.assertEqual(applied.status_code, 200, applied.text)
        journal = [
            j
            for j in applied.json()["journal"]
            if j["detail"].get("preset_id") == second["preset_id"]
        ][0]
        self.assertEqual(journal["detail"]["previous"]["preset_id"], first["preset_id"])
        self.assertEqual(
            journal["detail"]["previous_zones"][0]["zones"][0]["polygon"][1], [0.5, 0]
        )
        history = self.client.get(
            "/api/supervision/room-presets/" + room["room_id"]
        ).json()["items"]
        self.assertEqual([item["version"] for item in history], [2, 1])
        self.assertEqual(history[1]["channels"], first["channels"])

    def test_resource_conflicts_validation_and_immutable_database_history(self):
        project, room = self.fixture.business()
        first = self.resource(room).json()
        self.assertEqual(self.resource(room).status_code, 409)
        self.assertEqual(
            self.resource(room, version=1, scheme="standard").status_code, 422
        )
        alias = [
            {
                "source_id": s,
                "name": "测试",
                "kind": "video",
                "zones": [],
                "entry_lines": [],
            }
            for s in ("camera-001", "cam1")
        ]
        self.assertEqual(
            self.resource(room, version=1, channels=alias).status_code, 422
        )
        project2, room2 = self.fixture.business()
        wrong = self.calibrated(project2, room2)
        self.assertEqual(
            self.resource(room, version=1, channels=wrong).status_code, 422
        )
        for statement in (
            "UPDATE room_monitoring_presets SET version=9 WHERE preset_id=:id",
            "DELETE FROM room_monitoring_presets WHERE preset_id=:id",
        ):
            with self.assertRaises(DBAPIError), self.engine.begin() as db:
                db.execute(text(statement), {"id": first["preset_id"]})
        self.assertEqual(
            self.client.delete(
                "/api/supervision/room-presets/" + room["room_id"]
            ).status_code,
            405,
        )

    def test_reload_cannot_change_source_or_channel_kind_and_scopes_remain_exact(self):
        from app.maintenance_scope import session_scopes

        project, room = self.fixture.business()
        channels = self.calibrated(project, room)
        first = self.resource(room, channels=channels).json()
        sid = self.scene(project, room, first)
        with self.Session() as db:
            scopes = session_scopes(db, sid)
        self.assertEqual({s["source_id"] for s in scopes}, {"camera-001", "camera-011"})
        self.assertTrue(
            all(
                set(s) == {"project_id", "room_id", "session_id", "source_id"}
                and s["session_id"] == sid
                for s in scopes
            )
        )
        changed = deepcopy(channels)
        changed[1]["kind"] = "audio"
        second = self.resource(room, version=1, channels=changed).json()
        before = self.read(sid)
        response = self.write(
            sid,
            "preparation",
            method="PUT",
            scheme="basic",
            preset_id=second["preset_id"],
        )
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.read(sid)["version"], before["version"])
        self.assertEqual(
            self.read(sid)["preparation_snapshot"], before["preparation_snapshot"]
        )

    def test_plan_and_outbox_are_atomic_and_identity_requires_entrance_only_on_opt_in(
        self,
    ):
        from app import phase_control
        from app.phase_models import PhaseSnapshot

        sid, _, _ = self.create()
        before = self.read(sid)
        with self.Session() as db:
            count = db.query(PhaseSnapshot).filter_by(session_id=sid).count()
        with patch.object(
            phase_control,
            "enqueue_snapshot",
            side_effect=phase_control.PhaseControlError(
                "TEST_FAILURE", "回归注入同步失败"
            ),
        ):
            response = self.write(sid, "preparation", method="PUT", scheme="standard")
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.read(sid)["phases"], before["phases"])
        self.assertEqual(self.read(sid)["version"], before["version"])
        with self.Session() as db:
            self.assertEqual(
                db.query(PhaseSnapshot).filter_by(session_id=sid).count(), count
            )
        enabled = self.write(sid, "identity-setting", method="PUT", enabled=True).json()
        self.assertTrue(
            next(c for c in enabled["readiness"]["items"] if c["key"] == "entry_lines")[
                "required"
            ]
        )
        self.assertTrue(
            all("S04" in scenes for scenes in [p["scenes"] for p in enabled["phases"]])
        )
        disabled = self.write(
            sid, "identity-setting", method="PUT", enabled=False
        ).json()
        self.assertTrue(
            all(
                "S04" not in scenes
                for scenes in [p["scenes"] for p in disabled["phases"]]
            )
        )
        stale = self.write(
            sid,
            "preparation",
            method="PUT",
            expected=before["version"],
            scheme="standard",
        )
        self.assertEqual(stale.status_code, 409)

    def test_reduced_forms_accept_missing_metadata_and_rules_get_automatic_revision(
        self,
    ):
        project = self.client.post(
            "/api/registry/projects", json={"code": uuid4().hex, "name": "精简信息项目"}
        ).json()
        self.assertIsNone(project["owner"])
        room = self.client.post(
            "/api/registry/rooms",
            json={
                "project_id": project["project_id"],
                "code": "01",
                "name": "精简评标室",
            },
        ).json()
        self.assertIsNone(room["location"])
        invalid = self.client.put(
            "/api/registry/rooms/" + room["room_id"],
            json={
                **{
                    k: room[k]
                    for k in ("project_id", "code", "name", "mode", "enabled")
                },
                "location": "  ",
            },
        )
        self.assertEqual(invalid.status_code, 422)
        rule = self.client.post(
            "/api/operators",
            json={
                "code": "whispering",
                "name": "近距交流",
                "output_event_type": "whispering",
                "version": "historic-v1",
            },
        ).json()
        updated = self.client.patch(
            "/api/operators/" + rule["operator_id"],
            json={"name": "交头接耳核查", "expected_updated_at": rule["updated_at"]},
        )
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertNotEqual(updated.json()["version"], "historic-v1")
        audit = self.client.get(
            "/api/operators/" + rule["operator_id"] + "/audit"
        ).json()
        self.assertEqual(len(audit), 1)

    def test_legacy_custom_plans_remain_until_explicit_change_and_are_retained_in_journal(
        self,
    ):
        from app.session_workflow_models import SessionWorkflow

        sid, _, _ = self.create()
        plans = {
            phase["id"]: ["S01", "S09", "S10"] for phase in self.read(sid)["phases"]
        }
        with self.Session.begin() as db:
            book = db.get(SessionWorkflow, sid)
            book.preparation_snapshot = None
            book.phase_plans = deepcopy(plans)
        previous = self.read(sid)
        self.assertEqual(previous["monitoring_scheme"], "custom")
        self.assertEqual({p["id"]: p["scenes"] for p in previous["phases"]}, plans)
        updated = self.write(sid, "preparation", method="PUT", scheme="basic")
        self.assertEqual(updated.status_code, 200, updated.text)
        history = [
            j for j in updated.json()["journal"] if "previous_plans" in j["detail"]
        ]
        self.assertEqual(history[0]["detail"]["previous_plans"], plans)

    def test_archive_and_html_keep_the_selected_resource_revision_after_room_update(
        self,
    ):
        from datetime import datetime, timedelta
        from app.models import EvalSession

        project, room = self.fixture.business()
        first = self.resource(room).json()
        sid = self.scene(project, room, first)
        self.assertEqual(
            self.write(
                sid,
                "checks",
                method="PUT",
                checks={"room_inspected": True, "devices_stored": True},
            ).status_code,
            200,
        )
        self.assertEqual(self.write(sid, "start").status_code, 200)
        self.assertEqual(
            self.write(sid, "finish", note="合成回归：现场结束").status_code, 200
        )
        with self.Session.begin() as db:
            db.query(EvalSession).filter_by(session_id=sid).one().end_at = (
                datetime.utcnow() - timedelta(seconds=100)
            )
        self.assertEqual(
            self.write(
                sid,
                "checks",
                method="PUT",
                checks={"materials_reconciled": True, "room_cleared": True},
            ).status_code,
            200,
        )
        archived = self.write(
            sid,
            "archive",
            note="合成回归：保留本场独立监测依据",
            evidence_gap_note="本环境没有实际现场画面与消费者，覆盖状态继续待核验。",
        )
        self.assertEqual(archived.status_code, 201, archived.text)
        report_id = archived.json()["report_id"]
        second = self.resource(room, version=1).json()
        self.assertNotEqual(first["preset_id"], second["preset_id"])
        report = self.client.get("/api/supervision/reports/" + report_id).json()
        self.assertEqual(
            report["snapshot"]["preparation_snapshot"]["preset_id"], first["preset_id"]
        )
        self.assertEqual(report["snapshot"]["preparation_snapshot"]["version"], 1)
        html = self.client.get("/api/supervision/reports/" + report_id + "/download")
        self.assertEqual(html.status_code, 200, html.text)
        self.assertIn("监测方案：基础行为监管", html.text)
        self.assertIn("现场资源版本：1", html.text)

    def test_non_admin_cannot_write_resources_or_preparation(self):
        project, room = self.fixture.business()
        preset = self.resource(room).json()
        sid = self.scene(project, room, preset)
        for role in ("viewer", "operator", "supervisor"):
            account = "test_" + role
            response = self.client.post(
                "/api/auth/team",
                json={
                    "username": account,
                    "display_name": "隔离回归账号",
                    "role": role,
                    "password": "IntegrationTest12345",
                },
            )
            self.assertEqual(response.status_code, 201, response.text)
            with TestClient(self.app) as client:
                client.headers["X-Requested-With"] = "XMLHttpRequest"
                login = client.post(
                    "/api/auth/login",
                    json={"username": account, "password": "IntegrationTest12345"},
                )
                client.headers["X-CSRF-Token"] = login.json()["csrf_token"]
                self.assertEqual(
                    client.get(
                        "/api/supervision/room-presets/" + room["room_id"]
                    ).status_code,
                    200,
                )
                self.assertEqual(
                    client.post(
                        "/api/supervision/room-presets/" + room["room_id"], json={}
                    ).status_code,
                    403,
                )
                self.assertEqual(
                    client.put(
                        "/api/supervision/workflows/" + sid + "/preparation",
                        json={
                            "expected_version": self.read(sid)["version"],
                            "scheme": "standard",
                        },
                    ).status_code,
                    403,
                )
