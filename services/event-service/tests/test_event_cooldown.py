"""2.3 事件分级冷却逻辑回归：纯逻辑测试（无外部依赖）。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "service"))

import main  # noqa: E402


class CooldownTest(unittest.TestCase):
    def setUp(self):
        self.last_emit = {}

    def _emit(self, source, obs_type, now):
        # 契约：should_emit返回True后由调用方记录时间戳（与consume()一致）。
        if main.should_emit(self.last_emit, source, obs_type, now):
            self.last_emit[(source, obs_type)] = now
            return True
        return False

    def test_01_first_emit_allowed_per_source(self):
        self.assertTrue(self._emit("camera-001", "person_detected", 1000.0))
        self.assertTrue(self._emit("camera-002", "person_detected", 1000.0))

    def test_02_same_source_within_window_blocked(self):
        self.assertTrue(self._emit("camera-001", "person_detected", 1000.0))
        self.assertFalse(self._emit("camera-001", "person_detected", 1040.0))

    def test_03_window_expiry_allowed(self):
        self.assertTrue(self._emit("camera-001", "person_detected", 1000.0))
        # person_detected 冷却90秒
        self.assertFalse(self._emit("camera-001", "person_detected", 1089.0))
        self.assertTrue(self._emit("camera-001", "person_detected", 1090.5))

    def test_04_per_type_windows_independent(self):
        self.assertTrue(self._emit("camera-001", "person_detected", 1000.0))
        self.assertTrue(self._emit("camera-001", "crowd_gathering", 1000.5))
        self.assertFalse(self._emit("camera-001", "crowd_gathering", 1150.0))
        self.assertTrue(self._emit("camera-001", "crowd_gathering", 1180.5))

    def test_05_default_window_for_unknown_type(self):
        # zone_intrusion 已有专属120秒窗口；默认60秒窗用真正未注册的类型验证
        self.assertTrue(self._emit("camera-003", "zone_check", 1000.0))
        self.assertFalse(self._emit("camera-003", "zone_check", 1030.0))
        self.assertTrue(self._emit("camera-003", "zone_check", 1060.5))

    def test_06_cooldown_values_documented(self):
        self.assertEqual(main.COOLDOWN_BY_TYPE["person_detected"], 90.0)
        self.assertEqual(main.COOLDOWN_BY_TYPE["crowd_gathering"], 180.0)
        self.assertEqual(main.cooldown_for("unknown_type"), 60.0)


if __name__ == "__main__":
    unittest.main()
