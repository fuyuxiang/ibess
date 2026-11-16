import copy
import json
from pathlib import Path
import unittest

from ibess_contracts.messages import decode_message, observation_message
from ibess_contracts.validation import ContractValidator

ROOT = Path(__file__).resolve().parents[2]


class ContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.validator = ContractValidator()
        cls.observation = json.loads(
            (ROOT / "docs/contracts/fixtures/S10-suspected.json").read_text()
        )["observations"][0]

    def test_synthetic_examples_match_shipped_schemas(self):
        for path in (ROOT / "docs/contracts/fixtures").glob("*.json"):
            bundle = json.loads(path.read_text())
            for key, kind in (
                ("phase_configs", "phase-config"),
                ("observations", "observation"),
            ):
                for item in bundle[key]:
                    self.assertEqual(
                        self.validator.validate_object(kind, item), [], path.name
                    )

    def test_version_capture_time_scope_and_interval_are_validated(self):
        for patch in (
            {"contract_version": "999"},
            {"occurred_at": "2026-13-45T00:00:00Z"},
            {"start_ms": 16000},
            {"phase_version": True},
            {"session_id": None},
        ):
            with self.subTest(patch=patch):
                self.assertTrue(
                    self.validator.validate_object(
                        "observation", {**self.observation, **patch}
                    )
                )

    def test_unbound_observation_requires_explicit_quality_gap(self):
        value = copy.deepcopy(self.observation)
        value.update(
            binding_status="unbound",
            phase_id=None,
            phase_version=None,
            stage_snapshot_id=None,
        )
        self.assertTrue(self.validator.validate_object("observation", value))
        value["quality_flags"].append("binding_missing")
        self.assertEqual(self.validator.validate_object("observation", value), [])

    def test_old_and_versioned_redis_envelopes_are_compatible(self):
        new = observation_message(
            "behavior_detected", "camera-001", "s", {"frame_key": "original.jpg"}, 123
        )
        self.assertEqual(decode_message(new)["contract_version"], "2.0.0")
        legacy = dict(new)
        legacy.pop("contract_version")
        self.assertEqual(decode_message(legacy)["payload"], new["payload"])
        with self.assertRaises(ValueError):
            decode_message({**new, "contract_version": "999"})
        with self.assertRaises(ValueError):
            observation_message("behavior_detected", "cam1", "s", {}, float("nan"))
