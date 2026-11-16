"""执行卡 4.3：素材适配脚本（假 HTTP 传输）。
运行（项目根）：PYTHONPATH=scripts python -m unittest discover -s scripts -p test_stage_asset_adapter_v2.py -v
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from stage_asset_adapter import StageAssetAdapter, build_from_env, encode_multipart, request_id_for  # noqa: E402

SESSION = "session-a"
STARTED = "2026-10-19T02:00:00Z"


class FakeTransport:
    def __init__(self, *, assets, bindings=(), upload_status=202, bind_status=201):
        self.assets = assets
        self.bindings = list(bindings)
        self.upload_status = upload_status
        self.bind_status = bind_status
        self.calls = []
        self.uploads = 0

    def request(self, method, path, *, headers, body, content_type):
        self.calls.append((method, path, dict(headers), body, content_type))
        if method == "GET" and path.endswith("/assets"):
            return 200, {"session_id": SESSION, "assets": self.assets}
        if method == "GET" and path.endswith("/media-bindings"):
            return 200, {"session_id": SESSION, "bindings": self.bindings}
        if method == "POST" and path == "/api/audio/recordings":
            self.uploads += 1
            if self.upload_status != 202:
                return self.upload_status, {"detail": "坏文件"}
            return 202, {"recording_id": f"rec-{self.uploads}", "status": "queued"}
        if method == "POST" and path.endswith("/media-bindings"):
            payload = json.loads(body.decode("utf-8"))
            if self.bind_status != 201:
                return self.bind_status, {"detail": {"code": "BINDING_REQUIRED", "message": "x"}}
            return 201, {"binding_id": "b-" + payload["request_id"][:6], "timeline": [{}, {}], **payload}
        return 404, None


def asset(asset_id, asset_type, relative_path, duration_ms=None):
    return {"asset_id": asset_id, "asset_type": asset_type, "relative_path": relative_path, "duration_ms": duration_ms}


class AdapterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "authorized").mkdir()
        (self.root / "authorized" / "talk.wav").write_bytes(b"RIFF" + b"\x00" * 40)
        (self.root / "authorized" / "score.json").write_text("{}", encoding="utf-8")
        self.assets = [asset("talk-01", "audio", "authorized/talk.wav", 30000), asset("score-01", "scoring", "authorized/score.json"),
                       asset("clip-01", "video", "authorized/clip.mp4", 60000)]

    def tearDown(self):
        self.tmp.cleanup()

    def _adapter(self, transport):
        return StageAssetAdapter(transport, cookie="cookie-value", csrf="csrf-value", assets_root=self.root)

    def test_ac8_audio_uploads_then_binds_scoring_binds_video_rejected(self):
        transport = FakeTransport(assets=self.assets)
        reports = self._adapter(transport).process(SESSION, STARTED)
        by_id = {r["asset_id"]: r for r in reports}
        self.assertEqual(by_id["talk-01"]["action"], "uploaded_and_bound")
        self.assertEqual(by_id["talk-01"]["recording_id"], "rec-1")
        self.assertEqual(by_id["score-01"]["action"], "bound")
        self.assertEqual(by_id["score-01"]["next_step"], "scoring_check_pending")
        self.assertEqual(by_id["clip-01"]["action"], "rejected")
        upload = next(c for c in transport.calls if c[1] == "/api/audio/recordings")
        self.assertTrue(upload[4].startswith("multipart/form-data; boundary="))
        self.assertIn(b'name="file"; filename="talk.wav"', upload[3])
        self.assertIn(b"RIFF", upload[3])
        self.assertEqual(upload[2]["X-CSRF-Token"], "csrf-value")
        self.assertEqual(upload[2]["X-Requested-With"], "XMLHttpRequest")
        self.assertIn("eval_admin_session=cookie-value", upload[2]["Cookie"])
        binds = [json.loads(c[3].decode("utf-8")) for c in transport.calls if c[1].endswith("/media-bindings") and c[0] == "POST"]
        self.assertEqual([(b["kind"], b["subject_id"], b["asset_id"], b["duration_ms"]) for b in binds],
                         [("recording", "rec-1", "talk-01", 30000), ("scoring", "score-01", "score-01", None)])
        self.assertEqual(binds[0]["request_id"], request_id_for(SESSION, "talk-01", STARTED))
        self.assertEqual(binds[0]["started_at"], STARTED)
        reads = [c for c in transport.calls if c[0] == "GET"]
        self.assertNotIn("X-CSRF-Token", reads[0][2])

    def test_ac8_existing_binding_skips_upload(self):
        transport = FakeTransport(assets=self.assets, bindings=[{"binding_id": "b-old", "asset_id": "talk-01", "kind": "recording", "subject_id": "rec-old"}])
        reports = self._adapter(transport).process(SESSION, STARTED, asset_ids=["talk-01"])
        self.assertEqual(reports, [{"asset_id": "talk-01", "asset_type": "audio", "action": "skipped", "binding_id": "b-old", "recording_id": "rec-old"}])
        self.assertEqual(transport.uploads, 0)

    def test_ac9_errors_reported_without_credentials_and_others_continue(self):
        transport = FakeTransport(assets=self.assets, upload_status=422)
        reports = self._adapter(transport).process(SESSION, STARTED)
        by_id = {r["asset_id"]: r for r in reports}
        self.assertEqual(by_id["talk-01"]["action"], "failed")
        self.assertEqual(by_id["talk-01"]["error"], "UPLOAD_HTTP_422")
        self.assertEqual(by_id["score-01"]["action"], "bound", "一条失败不影响其他")
        text = json.dumps(reports)
        self.assertNotIn("cookie-value", text)
        self.assertNotIn("csrf-value", text)
        self.assertNotIn(self.tmp.name, text)
        denied = FakeTransport(assets=self.assets, bind_status=409)
        reports = self._adapter(denied).process(SESSION, STARTED, asset_ids=["score-01"])
        self.assertEqual(reports[0]["error"], "BIND_HTTP_409_BINDING_REQUIRED")
        missing_file = FakeTransport(assets=[asset("gone", "audio", "authorized/none.wav")])
        reports = self._adapter(missing_file).process(SESSION, STARTED)
        self.assertEqual((reports[0]["action"], reports[0]["error"]), ("failed", "ASSET_FILE_MISSING"))
        self.assertNotIn(self.tmp.name, json.dumps(reports), "缺文件的报告不带绝对路径")
        with self.assertRaises(ValueError):
            StageAssetAdapter(transport, cookie="", csrf="x", assets_root=self.root)

    def test_build_from_env_requires_absolute_root(self):
        with self.assertRaises(ValueError):
            build_from_env({"STAGE_ASSETS_ROOT": "relative", "POC_ADMIN_COOKIE": "c", "POC_ADMIN_CSRF": "s"})
        adapter = build_from_env({"STAGE_ASSETS_ROOT": str(self.root), "POC_ADMIN_COOKIE": "c", "POC_ADMIN_CSRF": "s", "POC_API_BASE_URL": "http://api:8000/"})
        self.assertEqual(adapter.transport.base_url, "http://api:8000")

    def test_multipart_encoding_escapes_filename(self):
        body, ctype = encode_multipart("file", 'a"b\r\n.wav', b"xyz", "audio/wav")
        self.assertIn(b'filename="a_b__.wav"', body)
        self.assertTrue(ctype.startswith("multipart/form-data; boundary=----poc"))
        self.assertTrue(body.endswith(b"--\r\n"))


if __name__ == "__main__":
    unittest.main()
