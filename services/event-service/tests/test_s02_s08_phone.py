"""S02/S08：明确动作复核加人物归属，几何位置单独不能认定通话/拍摄。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "service"))

from operators import OperatorConfig, evaluate_operator  # noqa: E402


def _config(code):
    return OperatorConfig(code=code, version="v1", enabled=True,
                          input_types=("person_detected",), output_event_type=code,
                          risk_level="high" if code == "phone_capture" else "medium",
                          parameters={})


def _payload(phones, persons, posture=''):
    return {"count": len(persons), "max_conf": 0.8, "phone_count": len(phones),
            "phone_conf_max": 0.7, "phone_boxes": phones, "person_boxes": persons, 'phone_postures':[posture]*len(phones)}


class PhoneOperatorTest(unittest.TestCase):
    def test_01_call_detected_near_head(self):
        # 人物框覆盖 y 0.2-0.9；手机中心 (0.5, 0.3) 落在人物上部45%
        result = evaluate_operator(_config("phone_call"),
                                   {"type": "person_detected", "source_id": "camera-010"},
                                   _payload([[0.46, 0.27, 0.54, 0.33]], [[0.3, 0.2, 0.7, 0.9]], 'ear'))
        self.assertIsNotNone(result)
        self.assertEqual(result.event_type, "phone_call")
        self.assertIn("贴耳", result.reason)

    def test_02_call_not_detected_phone_low(self):
        # 手机在画面下部（桌面）——不判通话
        self.assertIsNone(evaluate_operator(_config("phone_call"),
                                            {"type": "person_detected"},
                                            _payload([[0.46, 0.75, 0.54, 0.8]], [[0.3, 0.2, 0.7, 0.9]])))

    def test_03_call_no_phone(self):
        self.assertIsNone(evaluate_operator(_config("phone_call"),
                                            {"type": "person_detected"}, _payload([], [[0.3, 0.2, 0.7, 0.9]])))

    def test_04_capture_detected_desk_area(self):
        result = evaluate_operator(_config("phone_capture"),
                                   {"type": "person_detected", "source_id": "camera-011"},
                                   _payload([[0.5, 0.72, 0.6, 0.85]], [[0.2, 0.1, 0.9, 0.95]], 'desk'))
        self.assertIsNotNone(result)
        self.assertEqual(result.event_type, "phone_capture")
        self.assertIn("拍摄", result.reason)

    def test_05_capture_not_detected_high(self):
        # 手机在头部区域——不判拍摄
        self.assertIsNone(evaluate_operator(_config("phone_capture"),
                                            {"type": "person_detected"},
                                            _payload([[0.5, 0.2, 0.56, 0.26]], [[0.2, 0.1, 0.9, 0.95]])))

    def test_06_scope_and_type_guard(self):
        config = _config("phone_call")
        # 非 person_detected 输入不触发
        self.assertIsNone(evaluate_operator(config, {"type": "transcript_text"},
                                            _payload([[0.46, 0.27, 0.54, 0.33]], [[0.3, 0.2, 0.7, 0.9]])))

    def test_07_unstored_detected(self):
        result = evaluate_operator(_config("phone_unstored"),
                                   {"type": "person_detected", "source_id": "camera-012"},
                                   _payload([[0.4, 0.5, 0.5, 0.62]], [[0.2, 0.1, 0.8, 0.95]]))
        self.assertIsNotNone(result)
        self.assertEqual(result.event_type, "phone_unstored")
        self.assertIn("未寄存", result.reason)
        self.assertIsNone(evaluate_operator(_config("phone_unstored"),
                                            {"type": "person_detected"},
                                            _payload([], [[0.2, 0.1, 0.8, 0.95]])))

    def test_08_cooldown_registered(self):
        import main
        self.assertEqual(main.COOLDOWN_BY_TYPE["phone_call"], 120.0)
        self.assertEqual(main.COOLDOWN_BY_TYPE["phone_capture"], 180.0)
        self.assertEqual(main.COOLDOWN_BY_TYPE["phone_unstored"], 180.0)


if __name__ == "__main__":
    unittest.main()
