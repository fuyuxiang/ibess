"""业务演示闭环：在独立 PostgreSQL schema 内测试，不修改演示业务数据。"""
import os
import unittest
import uuid
from datetime import datetime

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text


class POCWorkflowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        url = os.environ.get("WORKFLOW_TEST_DATABASE_URL")
        if not url:
            raise unittest.SkipTest("请显式配置 WORKFLOW_TEST_DATABASE_URL，使用专用测试数据库")
        cls.admin_engine = create_engine(url)
        cls.schema = "test_workflow_" + uuid.uuid4().hex
        with cls.admin_engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{cls.schema}"'))
        # Main module performs its additive startup migrations in the isolated schema.
        os.environ["DATABASE_URL"] = url + ("&" if "?" in url else "?") + f"options=-csearch_path%3D{cls.schema}"
        os.environ["REDIS_URL"] = "redis://127.0.0.1:1/0"  # 不向演示环境发布测试消息
        from app.main import app
        from app.db import SessionLocal, engine
        from app.models import Base
        cls.app, cls.Session, cls.engine, cls.Base = app, SessionLocal, engine, Base

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()
        with cls.admin_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{cls.schema}" CASCADE'))
        cls.admin_engine.dispose()

    def setUp(self):
        with self.engine.begin() as connection:
            names = ",".join(f'"{table.name}"' for table in reversed(self.Base.metadata.sorted_tables))
            connection.execute(text("TRUNCATE " + names + " RESTART IDENTITY CASCADE"))
        self.client = TestClient(self.app)
        self.client.headers.update({"X-Requested-With": "XMLHttpRequest"})
        initialized = self.client.post('/api/auth/setup', json={
            'username': 'test_admin', 'display_name': '回归测试管理员', 'password': 'IntegrationTest12345',
        })
        self.assertEqual(initialized.status_code, 201, initialized.text)
        self.client.headers.update({'X-CSRF-Token': initialized.json()['csrf_token']})

    def tearDown(self):
        self.client.close()

    def create(self, event_type="crowd_gathering", **kwargs):
        response = self.client.post("/api/demo/events", json={"event_type": event_type, "room": "评标室 A01", **kwargs})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def decide(self, event, action="confirm", **kwargs):
        return self.client.post(f"/api/events/{event['event_id']}/decisions", json={
            "action": action, "note": "演示人工复核", "reviewer_id": "demo-reviewer",
            "expected_status": event["status"], "request_id": str(uuid.uuid4()), **kwargs,
        })

    def test_simulated_event_is_persisted_and_has_no_fabricated_media(self):
        event = self.create(note="验证人工复核流程")
        self.assertTrue(event["is_demo"])
        self.assertIsNone(event["confidence"])
        self.assertTrue(event["occurred_at"].endswith("Z"))
        detail = self.client.get(f"/api/events/{event['event_id']}").json()
        self.assertEqual(detail["demo_input"]["note"], "验证人工复核流程")
        self.assertEqual(detail["evidences"], [])
        self.assertEqual(len(detail["audit"]), 1)

    def test_generate_retry_is_idempotent_and_mismatched_payload_conflicts(self):
        key = str(uuid.uuid4())
        a = self.create(request_id=key)
        b = self.create(request_id=key)
        self.assertEqual(a["event_id"], b["event_id"])
        response = self.client.post("/api/demo/events", json={"request_id": key, "event_type": "seat_absence", "room": "评标室 A01"})
        self.assertEqual(response.status_code, 409)

    def test_confirmation_is_persistent_idempotent_and_has_unconfigured_reminder(self):
        event = self.create()
        key = str(uuid.uuid4())
        first = self.decide(event, request_id=key)
        self.assertEqual(first.status_code, 200, first.text)
        again = self.decide(event, request_id=key)
        self.assertEqual(again.status_code, 200, again.text)
        self.assertEqual(first.json()["decision_id"], again.json()["decision_id"])
        detail = self.client.get(f"/api/events/{event['event_id']}").json()
        self.assertEqual(detail["status"], "CONFIRMED")
        self.assertEqual(len(detail["decisions"]), 1)
        self.assertEqual(detail["reminders"][0]["status"], "NOT_CONFIGURED")
        self.assertIsNone(detail["reminders"][0]["asset_url"])

    def test_stale_operation_does_not_overwrite_a_decision(self):
        event = self.create()
        self.assertEqual(self.decide(event).status_code, 200)
        conflict = self.decide(event, "false_positive")
        self.assertEqual(conflict.status_code, 409)
        reused = self.decide(event, "false_positive", request_id="same-request")
        self.assertEqual(reused.status_code, 409)

    def test_false_positive_requires_reason_and_does_not_create_reminder(self):
        event = self.create()
        self.assertEqual(self.decide(event, "false_positive", note="").status_code, 422)
        self.assertEqual(self.decide(event, "false_positive", note="正常讨论的演示样例").status_code, 200)
        detail = self.client.get(f"/api/events/{event['event_id']}").json()
        self.assertEqual(detail["status"], "FALSE_POSITIVE")
        self.assertEqual(detail["reminders"], [])
        self.assertEqual(self.decide({**event, "status": "FALSE_POSITIVE"}).status_code, 409)

    def test_search_paginates_filters_and_counts_only_demo_events(self):
        self.create("crowd_gathering", note="专用检索词")
        self.create("seat_absence")
        page = self.client.get("/api/events/query", params={"demo_only": "true", "page_size": 1}).json()
        self.assertEqual(page["total"], 2)
        self.assertEqual(len(page["items"]), 1)
        self.assertEqual(page["stats"]["pending"], 2)
        filtered = self.client.get("/api/events/query", params={"keyword": "专用检索词", "demo_only": "true"}).json()
        self.assertEqual(filtered["total"], 1)
        self.assertEqual(self.client.get("/api/events/query", params={"page": 0}).status_code, 422)

    def test_room_filter_is_exact_and_does_not_match_other_room_notes(self):
        target_room = "评标室 A01"
        target = self.create(room=target_room)
        self.create(room="异地专家组 B01", note=f"备注提及{target_room}")
        self.create(room=target_room + "分室")
        response = self.client.get("/api/events/query", params={"room": target_room})
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual([item["event_id"] for item in result["items"]], [target["event_id"]])
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["stats"]["rooms"], [{"name": target_room, "value": 1}])

    def test_room_filter_precedes_pagination_stats_and_combines_with_pending(self):
        target_room = "评标室 A01"
        first = self.create("crowd_gathering", room=target_room)
        confirmed = self.create("camera_blacked", room=target_room)
        self.assertEqual(self.decide(confirmed).status_code, 200)
        latest = self.create("sensitive_word", room=target_room)
        # Newer events in another room must not occupy the target room's pages.
        self.create(room="异地专家组 B01", note=target_room)
        self.create("video_offline", room="异地专家组 B01")
        expected_ids = [latest["event_id"], confirmed["event_id"], first["event_id"]]
        for page, expected_id in enumerate(expected_ids, start=1):
            with self.subTest(page=page):
                response = self.client.get("/api/events/query", params={"room": target_room, "page": page, "page_size": 1})
                self.assertEqual(response.status_code, 200, response.text)
                result = response.json()
                self.assertEqual([item["event_id"] for item in result["items"]], [expected_id])
                self.assertEqual(result["total"], 3)
                self.assertEqual(result["stats"]["total"], 3)
                self.assertEqual(result["stats"]["pending"], 2)
                self.assertEqual(result["stats"]["confirmed"], 1)
                self.assertEqual(result["stats"]["high"], 2)
                self.assertEqual(result["stats"]["medium"], 1)
                self.assertEqual(result["stats"]["rooms"], [{"name": target_room, "value": 3}])
        for page, expected_id in enumerate((latest["event_id"], first["event_id"]), start=1):
            with self.subTest(pending_page=page):
                response = self.client.get("/api/events/query", params={"room": target_room, "status": "PENDING", "page": page, "page_size": 1})
                self.assertEqual(response.status_code, 200, response.text)
                result = response.json()
                self.assertEqual([item["event_id"] for item in result["items"]], [expected_id])
                self.assertEqual(result["total"], 2)
                self.assertEqual(result["stats"]["total"], 2)
                self.assertEqual(result["stats"]["pending"], 2)
                self.assertEqual(result["stats"]["confirmed"], 0)
                self.assertEqual(result["stats"]["rooms"], [{"name": target_room, "value": 2}])

    def test_room_filter_validates_maximum_length(self):
        room = "R" * 100
        target = self.create(room=room)
        response = self.client.get("/api/events/query", params={"room": room})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual([item["event_id"] for item in response.json()["items"]], [target["event_id"]])
        self.assertEqual(self.client.get("/api/events/query", params={"room": room + "R"}).status_code, 422)

    def test_date_filter_uses_beijing_calendar_day(self):
        event = self.create()
        from app.models import Event
        with self.Session() as db:
            record = db.query(Event).filter(Event.event_id == event["event_id"]).one()
            record.occurred_at = datetime(2026, 10, 7, 16, 30)
            db.commit()
        self.assertEqual(self.client.get("/api/events/query", params={"date_from": "2026-10-08", "date_to": "2026-10-08"}).json()["total"], 1)
        self.assertEqual(self.client.get("/api/events/query", params={"date_to": "2026-10-07"}).json()["total"], 0)
        self.assertEqual(self.client.get("/api/events/query", params={"date_from": "2026-10-09", "date_to": "2026-10-08"}).status_code, 422)

    def test_export_contains_actual_records_and_explicit_simulation_label(self):
        event = self.create()
        self.decide(event, "false_positive", note="模拟条件不能认定违规")
        response = self.client.get(f"/api/events/{event['event_id']}/export")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("attachment", response.headers["content-disposition"])
        result = response.json()
        self.assertTrue(result["event"]["is_demo"])
        self.assertEqual(result["event"]["decisions"][0]["note"], "模拟条件不能认定违规")
        self.assertEqual(result["event"]["evidences"], [])
        self.assertIn("模拟", result["description"])

    def test_unconfigured_reminder_cannot_be_marked_played(self):
        event = self.create()
        self.decide(event)
        reminder = self.client.get(f"/api/events/{event['event_id']}").json()["reminders"][0]
        response = self.client.post(f"/api/reminders/{reminder['execution_id']}/status", json={"status": "COMPLETED", "attempt_id": reminder.get("attempt_id")})
        self.assertEqual(response.status_code, 409)

    def test_configured_reminder_requires_real_playback_transitions_and_logs_retry(self):
        config = self.client.put("/api/demo/reminder", json={"url": "/reminder-assets/supervision.mp4", "version": "v1"})
        self.assertEqual(config.status_code, 200, config.text)
        self.assertEqual(self.client.put("/api/demo/reminder", json={"url": "https://example.com/fake.mp4", "version": "v1"}).status_code, 422)
        self.assertEqual(self.client.put("/api/demo/reminder", json={"url": "/reminder-assets/../private.mp4", "version": "v1"}).status_code, 422)
        event = self.create()
        self.decide(event)
        reminder = self.client.get(f"/api/events/{event['event_id']}").json()["reminders"][0]
        route = f"/api/reminders/{reminder['execution_id']}/status"
        self.assertEqual(reminder["status"], "QUEUED")
        first_attempt = reminder.get("attempt_id")
        self.assertEqual(self.client.post(route, json={"status": "COMPLETED", "attempt_id": first_attempt}).status_code, 409)
        self.assertEqual(self.client.post(route, json={"status": "FAILED", "error": "素材不存在", "attempt_id": first_attempt}).status_code, 200)
        retry = self.client.post(f"/api/reminders/{reminder['execution_id']}/retry")
        self.assertEqual(retry.status_code, 200)
        second_attempt = retry.json().get("attempt_id")
        self.assertNotEqual(first_attempt, second_attempt, "每轮重试必须有独立回执编号")
        self.assertEqual(self.client.post(route, json={"status": "PLAYING", "attempt_id": second_attempt}).status_code, 200)
        self.assertEqual(self.client.post(route, json={"status": "COMPLETED", "attempt_id": first_attempt}).status_code, 409)
        self.assertEqual(self.client.post(route, json={"status": "COMPLETED", "attempt_id": second_attempt}).status_code, 200)
        final = self.client.get(f"/api/events/{event['event_id']}").json()
        self.assertEqual(final["reminders"][0]["status"], "COMPLETED")
        self.assertGreaterEqual(len(final["audit"]), 6)
        self.assertEqual([a["status"] for a in final["reminders"][0]["attempts"]], ["FAILED", "COMPLETED"])


if __name__ == "__main__":
    unittest.main()
