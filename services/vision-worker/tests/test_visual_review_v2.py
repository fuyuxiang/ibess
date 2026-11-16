"""执行卡 7.1：候选、多模态客户端、复核校验、可运行性检查。

桩验证：假模型客户端 / 假 urlopen、构造帧与检测；没有 GPU、模型、ultralytics。
运行（services/vision-worker 下）：PYTHONPATH=. python -m unittest discover -s tests -p test_visual_review_v2.py -v
"""
import io
import json
import re
import sys
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from worker.object_candidates import Candidate, CandidateConfig, CandidateDeduper, extract_candidates, unsupported_kinds
from worker.stage_health import FrameContext
from worker.visual_client import OpenAICompatibleVisionClient, VisualModelConfig, VisualModelError, encode_jpeg, load_prompt
from worker.visual_review import (
    DEFAULT_PROMPT_DIR, ENUMS, FactValidationError, VisualReviewer, build_messages, build_review_request, reviewer_from_env, validate_facts,
)

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "tests" / "contracts"))
sys.path.insert(0, str(REPO / "deploy" / "visual-model"))
from contract_validator import ContractValidator  # noqa: E402
import check_runnability  # noqa: E402

T0 = datetime(2026, 10, 19, 2, 0, 0, tzinfo=timezone.utc)
PROMPT = load_prompt(DEFAULT_PROMPT_DIR, "visual-review")


def ctx(ms=1000, bound=True):
    fields = dict(source_id="camera-a", occurred_at=(T0 + timedelta(milliseconds=ms)).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                  media_run_id="run-1", start_ms=ms, asset_id="asset-1")
    if bound:
        fields.update(project_id="project-a", room_id="room-a", session_id="session-a", stage_snapshot_id="snap-1", phase_id="DETAILED", phase_version=1)
    return FrameContext(**fields)


def frame(seed=0, size=(360, 640)):
    rng = np.random.default_rng(seed)
    return rng.integers(0, 255, size=(*size, 3), dtype=np.uint8)


def candidate(kind="phone", bbox=(0.40, 0.40, 0.46, 0.50), too_small=False, holder="track:run-1:1", start_ms=1000):
    return Candidate(kind=kind, label="cell phone", bbox=bbox, confidence=0.6, start_ms=start_ms, holder_track_id=holder, too_small=too_small)


def request(cand=None, n_frames=3):
    frames = [(600 + i * 200, frame(i)) for i in range(n_frames)]
    return build_review_request(frames, cand or candidate(), prompt=PROMPT, model="fake-vlm")


def answer(**overrides):
    base = {"object_kind": "phone", "visibility": "clear", "holding": "in_hand", "activity": "operating_device",
            "evidence": [{"frame_index": 1, "region": "roi"}], "notes": "桌边有人手持小型设备"}
    base.update(overrides)
    return json.dumps(base, ensure_ascii=False)


class ScriptedClient:
    def __init__(self, replies):
        self.replies = list(replies)
        self.messages = []
        self.calls = 0

    def complete(self, messages, **kwargs):
        self.calls += 1
        self.messages.append(messages)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


class CandidateTest(unittest.TestCase):
    def test_ac1_mapping_normalization_holder_and_too_small(self):
        raw = [{"class": "cell phone", "conf": 0.5, "bbox": [640, 288, 704, 360]}, {"class": "book", "conf": 0.4, "bbox": [100, 100, 400, 300]},
               {"class": "laptop", "conf": 0.9, "bbox": [1000, 200, 1250, 500]}, {"class": "person", "conf": 0.9, "bbox": [600, 100, 900, 700]},
               {"class": "cell phone", "conf": 0.9, "bbox": [10, 10, 20, 18]}, {"class": "cell phone", "conf": 0.1, "bbox": [10, 10, 200, 180]}]
        persons = [{"track_id": "track:run-1:1", "bbox": [0.45, 0.10, 0.75, 0.95]}]
        cands = extract_candidates(raw, 1280, 720, start_ms=1000, persons=persons)
        kinds = [(c.kind, c.label, c.holder_track_id, c.too_small) for c in cands]
        self.assertEqual(kinds, [("phone", "cell phone", "track:run-1:1", False), ("paper", "book", None, False), ("screen", "laptop", None, False),
                                 ("phone", "cell phone", None, True)])
        self.assertEqual(cands[0].bbox, (0.5, 0.4, 0.55, 0.5))
        self.assertEqual(cands[0].subject, "phone:track:run-1:1")
        self.assertEqual(unsupported_kinds(), {"watch": "no_model"})
        with self.assertRaises(ValueError):
            CandidateConfig(min_side=0)
        with self.assertRaises(ValueError):
            extract_candidates(raw, 0, 720, start_ms=0)

    def test_ac2_dedupe_same_holder_same_kind_within_5s(self):
        dedupe = CandidateDeduper(5000)
        self.assertTrue(dedupe.admit(candidate(start_ms=1000)))
        self.assertFalse(dedupe.admit(candidate(start_ms=4000)))
        self.assertTrue(dedupe.admit(candidate(start_ms=6000)))
        self.assertTrue(dedupe.admit(candidate(kind="paper", start_ms=6100)), "不同种类不互压")
        self.assertTrue(dedupe.admit(candidate(holder="track:run-1:2", start_ms=6200)), "不同持有人不互压")
        dedupe.reset()
        self.assertTrue(dedupe.admit(candidate(start_ms=6300)))
        # 复核第 7 条：过小候选不占去重窗口；无人持有的两处物品不互压
        fresh = CandidateDeduper(5000)
        self.assertTrue(fresh.admit(candidate(too_small=True, start_ms=1000)))
        self.assertTrue(fresh.admit(candidate(start_ms=2000)), "随后清晰的候选仍可送审")
        self.assertTrue(fresh.admit(candidate(holder=None, start_ms=3000)))
        self.assertTrue(fresh.admit(candidate(holder=None, bbox=(0.80, 0.10, 0.86, 0.20), start_ms=3100)), "画面另一处的无人持有物品")


class ClientTest(unittest.TestCase):
    def _opener(self, outcomes):
        calls = []

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def opener(req, timeout):
            calls.append(req)
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return Response(json.dumps({"choices": [{"message": {"content": outcome}}]}).encode("utf-8"))
        opener.calls = calls
        return opener

    def test_ac7_retries_on_timeout_and_5xx_not_on_4xx(self):
        config = VisualModelConfig(base_url="http://vlm:8001/v1", model="m", api_key="secret-key", timeout_seconds=1, max_retries=2)
        sleeps = []
        opener = self._opener([TimeoutError(), urllib.error.HTTPError("u", 503, "x", {}, None), "ok"])
        client = OpenAICompatibleVisionClient(config, opener=opener, sleep=sleeps.append)
        self.assertEqual(client.complete([{"role": "user", "content": "hi"}]), "ok")
        self.assertEqual(len(opener.calls), 3)
        self.assertEqual(sleeps, [1.0, 2.0])
        self.assertEqual(opener.calls[0].get_header("Authorization"), "Bearer secret-key")
        self.assertNotIn("secret-key", repr(client) + repr(config))
        exhausted = OpenAICompatibleVisionClient(config, opener=self._opener([TimeoutError()] * 3), sleep=sleeps.append)
        with self.assertRaises(VisualModelError) as err:
            exhausted.complete([])
        self.assertEqual((err.exception.code, err.exception.retryable), ("TIMEOUT", True))
        bad = OpenAICompatibleVisionClient(config, opener=self._opener([urllib.error.HTTPError("u", 401, "x", {}, None)]), sleep=sleeps.append)
        with self.assertRaises(VisualModelError) as err:
            bad.complete([])
        self.assertEqual((err.exception.code, err.exception.retryable), ("UPSTREAM_401", False))
        shape = OpenAICompatibleVisionClient(config, opener=self._opener(["not-json-shape"]), sleep=sleeps.append)
        shape.opener = lambda req, timeout: io.BytesIO(b"{}")
        with self.assertRaises(VisualModelError):
            OpenAICompatibleVisionClient._extract(b"{}")

    def test_ac9_from_env_requires_all_fields(self):
        self.assertIsNone(VisualModelConfig.from_env({}))
        self.assertIsNone(VisualModelConfig.from_env({"VISUAL_MODEL_BASE_URL": "http://x", "VISUAL_MODEL_NAME": "m"}))
        config = VisualModelConfig.from_env({"VISUAL_MODEL_BASE_URL": "http://x/v1", "VISUAL_MODEL_NAME": "m", "VISUAL_MODEL_API_KEY": "k", "VISUAL_MODEL_TIMEOUT": "30"})
        self.assertEqual((config.model, config.timeout_seconds, config.max_retries), ("m", 30.0, 2))
        with tempfile.TemporaryDirectory() as tmp:
            key_file = Path(tmp) / "key"
            key_file.write_text("from-file\n", encoding="utf-8")
            config = VisualModelConfig.from_env({"VISUAL_MODEL_BASE_URL": "http://x/v1", "VISUAL_MODEL_NAME": "m", "VISUAL_MODEL_API_KEY_FILE": str(key_file)})
            self.assertEqual(config.api_key, "from-file")
        reviewer, prompt = reviewer_from_env({})
        self.assertIsNone(reviewer.client)
        self.assertEqual(prompt.label, "visual-review-v1")

    def test_ac11_prompt_loading_and_rules(self):
        self.assertEqual((PROMPT.name, PROMPT.version), ("visual-review", "v1"))
        first = PROMPT.text.splitlines()[0]
        self.assertIn("能看见什么", first)
        self.assertIn("不要猜", first)
        for key, values in ENUMS.items():
            for value in values:
                self.assertIn(value, PROMPT.text, f"枚举 {key}={value} 必须写进提示词")
        self.assertNotRegex(PROMPT.text, r"frame_index\"?\s*:\s*\d", "不给可照抄的具体帧号示例")
        self.assertIn("因为", PROMPT.text, "禁令要给理由")
        with self.assertRaises(FileNotFoundError):
            load_prompt(DEFAULT_PROMPT_DIR, "nope")
        with self.assertRaises(FileNotFoundError):
            load_prompt(DEFAULT_PROMPT_DIR, "visual-review", version=99)
        jpeg = encode_jpeg(frame(1, size=(2000, 3000)), max_side=640)
        self.assertEqual(jpeg[:2], b"\xff\xd8")
        with self.assertRaises(ValueError):
            encode_jpeg(np.zeros((0, 0, 3), dtype=np.uint8))


class ReviewTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.validator = ContractValidator()

    def test_ac3_request_has_panorama_roi_and_multiframe(self):
        req = request(n_frames=5)
        self.assertEqual(len(req.frames), 3)
        self.assertEqual([f.start_ms for f in req.frames], [800, 1000, 1200], "当前帧居中、前后各一帧")
        self.assertEqual(req.frame_indexes, (0, 1, 2))
        messages = build_messages(req)
        self.assertEqual(messages[0]["role"], "system")
        images = [p for p in messages[1]["content"] if p["type"] == "image_url"]
        self.assertEqual(len(images), 6)
        self.assertTrue(all(p["image_url"]["url"].startswith("data:image/jpeg;base64,") for p in images))
        text = json.dumps(messages, ensure_ascii=False)
        self.assertNotIn("C:\\", text)
        self.assertNotIn("secret", text)
        self.assertIn("phone", messages[1]["content"][0]["text"])
        single = request(n_frames=1)
        self.assertEqual(len(single.frames), 1)
        self.assertTrue(req.request_id.startswith("review:"))

    def test_ac4_valid_answer_becomes_visual_fact_observation(self):
        req = request()
        reviewer = VisualReviewer(ScriptedClient([answer()]), model_name="fake-vlm")
        obs = reviewer.review(req, ctx())
        self.assertEqual(self.validator.validate_object("observation", obs), [])
        self.assertEqual(obs["type"], "visual_fact")
        self.assertEqual(obs["payload"]["status"], "ok")
        self.assertEqual(obs["payload"]["facts"]["object_kind"], "phone")
        self.assertEqual(obs["payload"]["facts"]["evidence"], [{"frame_index": 1, "region": "roi"}])
        self.assertEqual(obs["payload"]["candidate"]["holder_track_id"], "track:run-1:1")
        self.assertEqual((obs["model_name"], obs["model_version"]), ("fake-vlm", "fake-vlm@visual-review-v1"))
        self.assertEqual((obs["start_ms"], obs["end_ms"]), (600, 1000))
        self.assertEqual(obs["evidence_refs"][0]["evidence_type"], "keyframe")
        self.assertEqual(obs["quality_flags"], [])
        self.assertNotIn("conclusion", json.dumps(obs["payload"]))
        unbound = VisualReviewer(ScriptedClient([answer()])).review(req, ctx(bound=False))
        self.assertIn("binding_missing", unbound["quality_flags"])
        self.assertEqual(self.validator.validate_object("observation", unbound), [])

    def test_ac5_invalid_answer_gets_one_correction_then_failed(self):
        req = request()
        client = ScriptedClient(["not json", answer(visibility="fuzzy")])
        obs = VisualReviewer(client).review(req, ctx())
        self.assertEqual(client.calls, 2)
        self.assertEqual(client.messages[1][-1]["role"], "user")
        self.assertIn("不符合要求", client.messages[1][-1]["content"][0]["text"])
        self.assertEqual((obs["payload"]["status"], obs["payload"]["error_code"]), ("failed", "ENUM_VIOLATION"))
        self.assertIn("model_failed", obs["quality_flags"])
        self.assertIsNone(obs["payload"]["facts"])
        self.assertEqual(self.validator.validate_object("observation", obs), [])
        fixed = ScriptedClient(["```json\n" + answer() + "\n```"])
        self.assertEqual(VisualReviewer(fixed).review(req, ctx())["payload"]["status"], "ok", "围栏 JSON 可解析")
        corrected = ScriptedClient([answer(extra_key=1), answer()])
        self.assertEqual(VisualReviewer(corrected).review(req, ctx())["payload"]["status"], "ok", "纠错一次后合法")

    def test_ac6_hallucinated_reference_is_invalid(self):
        req = request()
        for bad in (answer(evidence=[{"frame_index": 7, "region": "roi"}]), answer(evidence=[{"frame_index": 1, "region": "table"}]),
                    answer(evidence=[{"frame_index": True, "region": "roi"}]), answer(evidence=[{"frame_index": 1, "region": "roi", "x": 1}])):
            with self.assertRaises(FactValidationError) as err:
                validate_facts(req, bad)
            self.assertIn(err.exception.code, ("INVALID_REFERENCE", "SCHEMA_MISMATCH"))
        client = ScriptedClient([answer(evidence=[{"frame_index": 9, "region": "roi"}])] * 2)
        obs = VisualReviewer(client).review(req, ctx())
        self.assertEqual(obs["payload"]["error_code"], "INVALID_REFERENCE")

    def test_ac7_model_errors_become_failed_without_normal(self):
        req = request()
        for code in ("TIMEOUT", "UPSTREAM_503", "NETWORK", "UPSTREAM_401"):
            obs = VisualReviewer(ScriptedClient([VisualModelError(code)])).review(req, ctx())
            self.assertEqual((obs["payload"]["status"], obs["payload"]["error_code"]), ("failed", code))
            self.assertNotIn("normal", json.dumps(obs["payload"]))

    def test_ac8_unclear_or_too_small_is_flagged_not_asserted(self):
        req = request()
        obs = VisualReviewer(ScriptedClient([answer(visibility="unclear", holding="unknown", activity="unknown", evidence=[])])).review(req, ctx())
        self.assertIn("visibility_unclear", obs["quality_flags"])
        self.assertEqual(obs["payload"]["facts"]["holding"], "unknown")
        with self.assertRaises(FactValidationError):
            validate_facts(req, answer(visibility="unclear", holding="in_hand", activity="unknown"))
        small = request(candidate(too_small=True))
        obs = VisualReviewer(ScriptedClient([answer()])).review(small, ctx())
        self.assertIn("visibility_unclear", obs["quality_flags"])
        self.assertIn("candidate_too_small", obs["quality_flags"])
        with self.assertRaises(FactValidationError):
            validate_facts(req, answer(evidence=[]))
        # 复核第 13 条：过小候选的事实里不保留模型说的肯定持握 / 动作
        small_ear = VisualReviewer(ScriptedClient([answer(holding="at_ear", activity="phone_call")])).review(small, ctx())
        self.assertEqual({k: small_ear["payload"]["facts"][k] for k in ("visibility", "holding", "activity")},
                         {"visibility": "unclear", "holding": "unknown", "activity": "unknown"})

    def test_review_regressions_float_frame_transport_errors_env(self):
        import http.client
        req = request()
        evidence = [{"frame_index": float(req.frame_indexes[0]), "region": "roi"}]
        with self.assertRaises(FactValidationError):
            validate_facts(req, answer(evidence=evidence))           # 浮点帧号不是请求里的帧
        config = VisualModelConfig(base_url="http://vlm:8001/v1", model="m", api_key="k", timeout_seconds=1, max_retries=0)
        def broken(req_, timeout):
            raise http.client.IncompleteRead(b"")
        client = OpenAICompatibleVisionClient(config, opener=broken, sleep=lambda s: None)
        obs = VisualReviewer(client).review(req, ctx())
        self.assertEqual((obs["payload"]["status"], obs["payload"]["error_code"]), ("failed", "NETWORK"), "传输层异常也要落成 failed 观察")
        raw = json.dumps({"choices": [{"message": {"content": [{"type": "text", "text": None}, {"type": "text", "text": "ok"}]}}]}).encode()
        self.assertEqual(OpenAICompatibleVisionClient._extract(raw), "ok")
        env = {"VISUAL_MODEL_BASE_URL": "http://x", "VISUAL_MODEL_NAME": "m", "VISUAL_MODEL_API_KEY": "k", "VISUAL_MODEL_TIMEOUT": "abc"}
        self.assertIsNone(VisualModelConfig.from_env(env), "配置写错按未配置处理")

    def test_ac9_model_not_configured_is_failed(self):
        obs = VisualReviewer(None).review(request(), ctx())
        self.assertEqual((obs["payload"]["status"], obs["payload"]["error_code"]), ("failed", "MODEL_NOT_CONFIGURED"))
        self.assertIn("model_failed", obs["quality_flags"])

    def test_ac10_negative_examples_pass_through_without_escalation(self):
        req = request()
        for reply, expect in ((answer(object_kind="paper", holding="in_hand", activity="reading"), ("paper", "reading")),
                              (answer(object_kind="cup", holding="in_hand", activity="none"), ("cup", "none")),
                              (answer(object_kind="none", holding="none", activity="none"), ("none", "none"))):
            obs = VisualReviewer(ScriptedClient([reply])).review(req, ctx())
            facts = obs["payload"]["facts"]
            self.assertEqual((facts["object_kind"], facts["activity"]), expect)
            self.assertNotEqual(facts["holding"], "at_ear")
            self.assertEqual(obs["payload"]["candidate"]["kind"], "phone", "候选种类原样保留，事实由模型说，不互相覆盖")
        with self.assertRaises(FactValidationError):
            validate_facts(req, answer(notes="x" * 201))


class RunnabilityTest(unittest.TestCase):
    def test_ac12_restricted_on_this_machine_and_ok_when_all_present(self):
        report = check_runnability.run({}, runner=lambda *a, **k: (_ for _ in ()).throw(OSError("no")))
        self.assertEqual(report["status"], "restricted")
        self.assertEqual(set(report["missing"]) >= {"config", "service"}, True)
        self.assertEqual(report["nature"], "runnability-only; 不评估识别效果")

        class Done:
            returncode = 0
            stdout = "NVIDIA RTX, 8192 MiB\n"

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def opener(req, timeout):
            if req.full_url.endswith("/models"):
                return Response(json.dumps({"data": [{"id": "vlm-x"}]}).encode("utf-8"))
            return Response(b'{"choices":[{"message":{"content":"ok"}}]}')

        env = {"VISUAL_MODEL_BASE_URL": "http://vlm:8001/v1", "VISUAL_MODEL_NAME": "vlm-x", "VISUAL_MODEL_API_KEY": "k"}
        import shutil
        original = shutil.which
        shutil.which = lambda name: "/usr/bin/nvidia-smi" if name == "nvidia-smi" else original(name)
        try:
            report = check_runnability.run(env, probe=True, runner=lambda *a, **k: Done(), opener=opener)
        finally:
            shutil.which = original
        self.assertEqual(report["status"], "ok")
        self.assertIn("latency_ms", report["checks"]["probe"]["detail"])
        wrong_model = check_runnability.run(dict(env, VISUAL_MODEL_NAME="other"), runner=lambda *a, **k: Done(), opener=opener)
        self.assertIn("service", wrong_model["missing"])


if __name__ == "__main__":
    unittest.main()
