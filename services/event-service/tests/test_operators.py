import unittest

from service.operators import OperatorConfig, evaluate_operator


class OperatorTest(unittest.TestCase):
    def config(self, code, parameters=None, enabled=True):
        return OperatorConfig(
            code=code,
            version="v1",
            enabled=enabled,
            input_types=("person_detected",),
            output_event_type=code,
            risk_level="medium",
            parameters=parameters or {},
        )

    def test_crowd_operator_emits_only_above_threshold(self):
        config = self.config("crowd_gathering", {"count_threshold": 4})
        self.assertIsNone(evaluate_operator(config, {"type": "person_detected"}, {"count": 3}))
        boxes = [[.2+i*.02,.2,.3+i*.02,.6] for i in range(4)]
        result = evaluate_operator(config, {"type": "person_detected"}, {"count": 4, "max_conf": 0.91, 'person_boxes':boxes})
        self.assertEqual(result.event_type, "crowd_gathering")
        self.assertEqual(result.detail["count"], 4)

    def test_disabled_or_unmatched_scope_does_not_emit(self):
        config = self.config("crowd_gathering", enabled=False)
        self.assertIsNone(evaluate_operator(config, {"type": "person_detected"}, {"count": 10}))
        config = self.config("crowd_gathering")
        scoped = OperatorConfig(**{**config.__dict__, "camera_id": "camera-2"})
        self.assertIsNone(evaluate_operator(scoped, {"type": "person_detected", "source_id": "camera-1"}, {"count": 10}))


if __name__ == "__main__":
    unittest.main()
