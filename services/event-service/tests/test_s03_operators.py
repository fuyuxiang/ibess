"""5.1/5.2 离岗与越界算子支撑回归：absence状态注入（纯逻辑）+ 冷却扩展。"""
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "service"))

import main  # noqa: E402


class AbsenceTest(unittest.TestCase):
    def setUp(self):
        self.seen = {}

    def test_01_first_observation_zero(self):
        # 首次观察无历史基线：不判离席（返回0），但记录基线
        self.assertEqual(main.compute_absence_seconds(self.seen, "camera-005", 0, 1000.0), 0.0)

    def test_02_counts_journey(self):
        self.seen[("seen", "camera-005")] = 1000.0  # 基线：1000秒时有人
        self.assertEqual(main.compute_absence_seconds(self.seen, "camera-005", 0, 1020.0), 20.0)
        self.assertEqual(main.compute_absence_seconds(self.seen, "camera-005", 0, 1045.0), 45.0)
        # 有人回来：清零并刷新基线
        self.assertEqual(main.compute_absence_seconds(self.seen, "camera-005", 2, 1050.0), 0.0)
        self.assertEqual(main.compute_absence_seconds(self.seen, "camera-005", 0, 1051.0), 1.0)

    def test_03_per_camera_independent(self):
        self.seen[("seen", "camera-005")] = 1000.0
        self.assertEqual(main.compute_absence_seconds(self.seen, "camera-006", 0, 1100.0), 0.0)
        self.assertEqual(main.compute_absence_seconds(self.seen, "camera-005", 0, 1010.0), 10.0)

    def test_04_negative_guard(self):
        self.seen[("seen", "camera-005")] = 2000.0
        self.assertEqual(main.compute_absence_seconds(self.seen, "camera-005", 0, 1999.0), 0.0)


class OperatorConfigTest(unittest.TestCase):
    """seat_absence / zone_intrusion handler 判定逻辑（operators.py 原实现，验参数口径）。"""

    def test_05_seat_absence_threshold(self):
        from operators import OperatorConfig, evaluate_operator
        config = OperatorConfig(code="seat_absence", version="v1", enabled=True,
                                input_types=("person_detected",), output_event_type="seat_absence",
                                risk_level="medium", parameters={"absence_seconds": 25})
        hit = evaluate_operator(config, {"type": "person_detected", "source_id": "camera-005"},
                                {"absence_seconds": 30, "confidence": 0.9})
        self.assertIsNotNone(hit)
        self.assertEqual(hit.event_type, "seat_absence")
        self.assertIn("30 秒", hit.reason)
        miss = evaluate_operator(config, {"type": "person_detected", "source_id": "camera-005"},
                                 {"absence_seconds": 10})
        self.assertIsNone(miss)

    def test_06_zone_intrusion_requires_inside(self):
        from operators import OperatorConfig, evaluate_operator
        config = OperatorConfig(code="zone_intrusion", version="v1", enabled=True,
                                input_types=("person_detected",), output_event_type="zone_intrusion",
                                risk_level="high", parameters={})
        self.assertIsNone(evaluate_operator(config, {"type": "person_detected"}, {}))
        hit = evaluate_operator(config, {"type": "person_detected", "zone_id": "z1"},
                                {"inside": True, "zone_name": "限制区域"})
        self.assertIsNotNone(hit)
        self.assertEqual(hit.event_type, "zone_intrusion")

    def test_07_cooldown_covers_new_types(self):
        self.assertEqual(main.COOLDOWN_BY_TYPE["seat_absence"], 180.0)
        self.assertEqual(main.COOLDOWN_BY_TYPE["zone_intrusion"], 120.0)

    @unittest.skipUnless(os.environ.get('DEPLOYMENT_OPERATORS_TEST_DATABASE_URL'), '部署注册数据验收须显式指定DEPLOYMENT_OPERATORS_TEST_DATABASE_URL')
    def test_08_registered_operator_configs(self):
        # operators 表应有 seat_absence 与 zone_intrusion 配置（由部署脚本/SQL注册）
        import sqlalchemy
        url = os.environ['DEPLOYMENT_OPERATORS_TEST_DATABASE_URL']
        engine = sqlalchemy.create_engine(url)
        try:
            with engine.begin() as connection:
                rows = connection.execute(sqlalchemy.text(
                    "select code, enabled, output_event_type from operators where code in ('seat_absence','zone_intrusion')")).fetchall()
            codes = {row[0] for row in rows}
            self.assertIn("seat_absence", codes)
            self.assertIn("zone_intrusion", codes)
        finally:
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
