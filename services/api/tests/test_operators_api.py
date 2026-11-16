"""Operator API compatibility on a migrated, authenticated, isolated PostgreSQL schema."""
import unittest
import test_poc_workflow as workflow


class OperatorApiTest(unittest.TestCase):
    setUpClass = classmethod(workflow.POCWorkflowTest.setUpClass.__func__)
    tearDownClass = classmethod(workflow.POCWorkflowTest.tearDownClass.__func__)
    setUp = workflow.POCWorkflowTest.setUp
    tearDown = workflow.POCWorkflowTest.tearDown

    def test_operator_can_be_created_disabled_and_bound(self):
        response = self.client.post(
            "/api/operators",
            json={
                "code": "crowd_gathering",
                "name": "人员聚集",
                "category": "rule",
                "version": "v1",
                "input_types": ["person_detected"],
                "output_event_type": "crowd_gathering",
                "risk_level": "medium",
                "parameters": {"count_threshold": 4, "distance_threshold": 0.18},
            },
        )
        self.assertEqual(response.status_code, 201)
        operator = response.json()
        self.assertFalse(operator["enabled"])

        enabled = self.client.post(f"/api/operators/{operator['operator_id']}/enable")
        self.assertEqual(enabled.status_code, 200)
        self.assertTrue(enabled.json()["enabled"])

        binding = self.client.post(
            f"/api/operators/{operator['operator_id']}/bindings",
            json={"session_id": "session-1", "camera_id": "camera-1"},
        )
        self.assertEqual(binding.status_code, 201)
        self.assertEqual(binding.json()["camera_id"], "camera-1")

    def test_duplicate_operator_code_is_rejected(self):
        payload = {
            "code": "device_health",
            "name": "设备健康检测",
            "category": "rule",
            "version": "v1",
            "input_types": ["video_offline"],
            "output_event_type": "video_offline",
            "risk_level": "high",
            "parameters": {},
        }
        self.assertEqual(self.client.post("/api/operators", json=payload).status_code, 201)
        duplicate = self.client.post("/api/operators", json=payload)
        self.assertEqual(duplicate.status_code, 409)

    def test_invalid_risk_level_is_rejected(self):
        response = self.client.post(
            "/api/operators",
            json={
                "code": "invalid-risk",
                "name": "非法风险等级",
                "category": "rule",
                "version": "v1",
                "input_types": ["person_detected"],
                "output_event_type": "custom_event",
                "risk_level": "urgent",
                "parameters": {},
            },
        )
        self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()
