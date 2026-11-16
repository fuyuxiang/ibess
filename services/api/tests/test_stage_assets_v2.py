"""执行卡 4.1：素材清单及登记 API。

同一份测试双后端：设 STAGE_ASSETS_TEST_DATABASE_URL 时用真实 PostgreSQL 随机 schema（另一台机器），
否则用 SQLite 临时文件（本机桩验证）。素材根是临时目录，探测器用假对象；不碰正式库、不碰共享入口。
运行（services/api 下）：PYTHONPATH=. python -m unittest discover -s tests -p test_stage_assets_v2.py -v
"""
import hashlib
import json
import os
import secrets
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import CheckConstraint, MetaData, create_engine, event, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.models import Base, EvalSession
from app.business_models import BusinessParticipant, BusinessPerson, BusinessProject, BusinessRoom, BusinessSeat
from app.auth_models import AdminAccount, AdminAudit, AdminSession
from app.phase_models import PhaseBinding
from app.auth import AdminAuthMiddleware
from app.db import get_session
from app.asset_models import StageAsset
from app.routers.stage_assets import (
    DURATION_TOLERANCE_MS,
    FfprobeProber,
    ProbeResult,
    get_assets_root,
    get_prober,
    router,
)

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "tests" / "contracts"))
from contract_validator import ContractValidator  # noqa: E402  只读加载 1.2 的校验器

CONTRACT_FIELDS = ("contract_version", "asset_id", "asset_type", "relative_path", "sha256",
                   "duration_ms", "authorization_source", "visibility_notes")


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def probed(duration_ms: int) -> ProbeResult:
    return ProbeResult(status="probed", tool="ffprobe", duration_ms=duration_ms, format_name="mov,mp4,m4a,3gp,3g2,mj2",
                       streams=[{"codec_type": "video", "codec_name": "h264", "width": 1280, "height": 720}])


class FakeProber:
    def __init__(self, result: ProbeResult):
        self.result = result
        self.calls: list[Path] = []

    def probe(self, path) -> ProbeResult:
        self.calls.append(Path(path))
        return self.result


class StageAssetsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tables = [BusinessProject.__table__, BusinessRoom.__table__, BusinessPerson.__table__, BusinessParticipant.__table__,
                      BusinessSeat.__table__, EvalSession.__table__, PhaseBinding.__table__, AdminAccount.__table__,
                      AdminAudit.__table__, AdminSession.__table__, StageAsset.__table__]
        url = os.environ.get("STAGE_ASSETS_TEST_DATABASE_URL")
        cls.tmpdb = None
        cls.engine = None
        try:
            if url:
                cls.backend = "postgresql"
                cls.admin = create_engine(url)
                if cls.admin.dialect.name != "postgresql":
                    raise RuntimeError("STAGE_ASSETS_TEST_DATABASE_URL 必须指向 PostgreSQL")
                cls.schema = "test_assets_v2_" + uuid4().hex
                with cls.admin.begin() as connection:
                    connection.execute(text(f'CREATE SCHEMA "{cls.schema}"'))
                cls.engine = create_engine(url, connect_args={"options": f"-csearch_path={cls.schema} -cstatement_timeout=15000"})
                Base.metadata.create_all(cls.engine, tables=cls.tables)
            else:
                cls.backend = "sqlite"
                cls.tmpdb = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
                cls.engine = create_engine(f"sqlite:///{Path(cls.tmpdb.name) / 'stage_assets.db'}")

                @event.listens_for(cls.engine, "connect")
                def sqlite_compat(dbapi_connection, _record):
                    dbapi_connection.create_function("btrim", 1, lambda value: value.strip() if value is not None else None)
                    dbapi_connection.execute("PRAGMA foreign_keys=ON")
                # B / 0.4 的表带 PostgreSQL 专用的正则 CHECK（username ~ '...'），SQLite 建不出来；
                # 隔离库里只去掉这类约束建同名表，本卡自己的表约束全部保留。
                safe = MetaData()
                for table in cls.tables:
                    table.to_metadata(safe)
                for table in safe.tables.values():
                    for constraint in list(table.constraints):
                        if isinstance(constraint, CheckConstraint) and "~" in str(constraint.sqltext):
                            table.constraints.discard(constraint)
                safe.create_all(cls.engine)
                # SQLite 读回的 DateTime 没有时区，而 app.auth._now() 带时区；隔离测试里让 _now 返回 naive UTC，
                # 只影响登录过期比较，不改 B / 0.4 的代码。
                import app.auth as auth_module
                cls._now_patch = mock.patch.object(auth_module, "_now", lambda: datetime.now(timezone.utc).replace(tzinfo=None))
                cls._now_patch.start()
            cls.Session = sessionmaker(bind=cls.engine)
            with cls.Session.begin() as db:
                db.add(AdminAccount(account_id=1, username="asset_admin", display_name="测试管理员", password_hash="not-a-real-password"))
            cls.app = FastAPI()
            cls.app.add_middleware(AdminAuthMiddleware, session_factory=cls.Session)
            cls.app.include_router(router, prefix="/api")

            def override_db():
                with cls.Session() as db:
                    yield db
            cls.app.dependency_overrides[get_session] = override_db
        except Exception:
            cls.tearDownClass()
            raise

    @classmethod
    def tearDownClass(cls):
        patch = getattr(cls, "_now_patch", None)
        if patch is not None:
            patch.stop()
            cls._now_patch = None
        if cls.engine is not None:
            cls.engine.dispose()
        if getattr(cls, "backend", None) == "postgresql" and hasattr(cls, "schema"):
            with cls.admin.begin() as connection:
                connection.execute(text(f'DROP SCHEMA "{cls.schema}" CASCADE'))
            cls.admin.dispose()
        elif cls.tmpdb is not None:
            cls.tmpdb.cleanup()

    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.root_path = Path(self.root.name).resolve()
        (self.root_path / "authorized" / "demo").mkdir(parents=True)
        self.app.dependency_overrides[get_assets_root] = lambda: self.root_path
        self.prober = FakeProber(probed(60000))
        self.app.dependency_overrides[get_prober] = lambda: self.prober
        self.session_id, self.project, self.room = (str(uuid4()) for _ in range(3))
        self.source = "camera-assets-test"
        token, self.csrf = secrets.token_urlsafe(32), secrets.token_hex(32)
        with self.Session.begin() as db:
            db.add(BusinessProject(project_id=self.project, code=self.project, name="测试项目", owner="测试", status="running"))
            db.flush()
            db.add(BusinessRoom(room_id=self.room, project_id=self.project, code=self.room, name="测试评标室", location="测试", enabled=True))
            db.add(EvalSession(session_id=self.session_id, name="素材测试场次", status="running", current_phase="review"))
            db.flush()
            db.add(PhaseBinding(session_id=self.session_id, project_id=self.project, room_id=self.room, source_id=self.source, active=True))
            db.add(AdminSession(token_hash=hashlib.sha256(token.encode("ascii")).hexdigest(), account_id=1,
                                expires_at=datetime.now(timezone.utc) + timedelta(hours=1), csrf_token=self.csrf))
        self.client = TestClient(self.app, headers={"X-Requested-With": "XMLHttpRequest", "X-CSRF-Token": self.csrf})
        self.client.cookies.set("eval_admin_session", token)
        self.url = f"/api/sessions/{self.session_id}/assets"
        self.validator = ContractValidator()

    def tearDown(self):
        self.client.close()
        self.root.cleanup()

    # ---- helpers ----
    def write(self, relative: str, data: bytes) -> str:
        path = self.root_path.joinpath(*relative.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return sha256_of(data)

    def manifest(self, **overrides) -> dict:
        body = {"contract_version": "2.0.0", "asset_id": "clip-01", "asset_type": "video",
                "relative_path": "authorized/demo/clip.mp4", "sha256": "0" * 64, "duration_ms": 60000,
                "authorization_source": "auth-record-01", "visibility_notes": "固定机位，桌面可见"}
        body.update(overrides)
        return body

    def register(self, body: dict):
        return self.client.post(self.url + "/register", json=body)

    def code(self, response) -> str:
        detail = response.json()["detail"]
        return detail["code"] if isinstance(detail, dict) else "VALIDATION"

    def count(self) -> int:
        with self.Session() as db:
            return len(db.scalars(select(StageAsset).where(StageAsset.session_id == self.session_id)).all())

    # ---- AC-1 ----
    def test_ac1_register_video_then_list(self):
        digest = self.write("authorized/demo/clip.mp4", b"fake-mp4-bytes" * 1000)
        response = self.register(self.manifest(sha256=digest))
        self.assertEqual(201, response.status_code, response.text)
        value = response.json()
        self.assertEqual(self.session_id, value["session_id"])
        self.assertEqual(digest, value["sha256"])
        self.assertEqual(14000, value["size_bytes"])
        self.assertEqual("probed", value["probe"]["status"])
        self.assertEqual((60000, "mov,mp4,m4a,3gp,3g2,mj2"), (value["probe"]["duration_ms"], value["probe"]["format_name"]))
        self.assertEqual("h264", value["probe"]["streams"][0]["codec_name"])
        self.assertFalse(value["probe_pending"])
        self.assertEqual("asset_admin", value["registered_by"])
        self.assertTrue(value["registered_at"].endswith("Z"))
        self.assertEqual([self.root_path / "authorized" / "demo" / "clip.mp4"], self.prober.calls)
        self.assertEqual([], self.validator.validate_object("asset-manifest", {k: value[k] for k in CONTRACT_FIELDS}))
        listing = self.client.get(self.url)
        self.assertEqual(200, listing.status_code, listing.text)
        body = listing.json()
        self.assertEqual({"project_id": self.project, "room_id": self.room, "source_id": self.source}, body["binding"])
        self.assertEqual(["clip-01"], [a["asset_id"] for a in body["assets"]])
        self.assertNotIn(str(self.root_path), response.text + listing.text)   # 不回显根目录

    def test_ac1_audio_and_listing_order(self):
        d1 = self.write("authorized/demo/a.wav", b"a" * 10)
        d2 = self.write("authorized/demo/b.wav", b"b" * 10)
        self.assertEqual(201, self.register(self.manifest(asset_id="audio-b", asset_type="audio", relative_path="authorized/demo/b.wav", sha256=d2, duration_ms=60000)).status_code)
        time.sleep(0.005)    # 登记时间要可区分，列表按登记时间排序
        self.assertEqual(201, self.register(self.manifest(asset_id="audio-a", asset_type="audio", relative_path="authorized/demo/a.wav", sha256=d1, duration_ms=60000)).status_code)
        ids = [a["asset_id"] for a in self.client.get(self.url).json()["assets"]]
        self.assertEqual(["audio-b", "audio-a"], ids)      # 登记顺序

    # ---- AC-2 ----
    def test_ac2_path_rules_rejected_with_code(self):
        self.write("authorized/demo/clip.mp4", b"x")
        for bad in ("../clip.mp4", "/authorized/demo/clip.mp4", "C:/authorized/demo/clip.mp4",
                    "authorized\\demo\\clip.mp4", "authorized/../authorized/demo/clip.mp4", "authorized/demo/../../etc/passwd",
                    "authorized/./clip.mp4", "authorized//clip.mp4"):
            with self.subTest(path=bad):
                response = self.register(self.manifest(relative_path=bad))
                self.assertEqual(422, response.status_code, response.text)
                self.assertEqual("PATH_INVALID", self.code(response))
        self.assertEqual(0, self.count())

    def test_ac2_symlink_escaping_root_rejected(self):
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        secret = Path(outside.name) / "secret.mp4"
        secret.write_bytes(b"outside")
        link = self.root_path / "authorized" / "demo" / "link.mp4"
        relative = "authorized/demo/link.mp4"
        try:
            os.symlink(secret, link)
        except (OSError, NotImplementedError):
            try:                                             # Windows 没有符号链接权限时退回目录联接（junction）
                import _winapi
                _winapi.CreateJunction(outside.name, str(self.root_path / "authorized" / "escape"))
                relative = "authorized/escape/secret.mp4"
            except (ImportError, OSError, NotImplementedError) as exc:
                self.skipTest(f"本机建不出符号链接或目录联接：{type(exc).__name__}")
        response = self.register(self.manifest(relative_path=relative, sha256=sha256_of(b"outside")))
        self.assertEqual(422, response.status_code, response.text)
        self.assertEqual("PATH_OUTSIDE_ROOT", self.code(response))
        missing = self.register(self.manifest(relative_path=relative.replace("secret", "absent"), sha256=sha256_of(b"outside")))
        self.assertEqual("PATH_OUTSIDE_ROOT", self.code(missing))   # 根外文件存不存在都一个答案，不做存在性探测
        self.assertEqual(0, self.count())

    # ---- AC-3 ----
    def test_ac3_missing_file_404_and_directory_422(self):
        response = self.register(self.manifest(relative_path="authorized/demo/nope.mp4"))
        self.assertEqual(404, response.status_code, response.text)
        self.assertEqual("FILE_NOT_FOUND", self.code(response))
        response = self.register(self.manifest(relative_path="authorized/demo"))
        self.assertEqual(422, response.status_code, response.text)
        self.assertEqual("NOT_A_FILE", self.code(response))
        self.assertEqual(0, self.count())

    # ---- AC-4 ----
    def test_ac4_sha_mismatch_409_nothing_persisted(self):
        self.write("authorized/demo/clip.mp4", b"real content")
        response = self.register(self.manifest(sha256="f" * 64))
        self.assertEqual(409, response.status_code, response.text)
        self.assertEqual("SHA256_MISMATCH", self.code(response))
        self.assertEqual(0, self.count())
        self.assertEqual([], self.prober.calls)             # 摘要不对就不探测

    # ---- AC-5 ----
    def test_ac5_scoring_json_object_registers_without_probe(self):
        data = json.dumps({"bidders": [{"id": "b1", "total": 88.5}]}).encode("utf-8")
        digest = self.write("authorized/demo/scoring.json", data)
        response = self.register(self.manifest(asset_id="score-01", asset_type="scoring", relative_path="authorized/demo/scoring.json",
                                               sha256=digest, duration_ms=None))
        self.assertEqual(201, response.status_code, response.text)
        value = response.json()
        self.assertEqual("not_applicable", value["probe"]["status"])
        self.assertIsNone(value["duration_ms"])
        self.assertFalse(value["probe_pending"])
        self.assertEqual([], self.prober.calls)
        self.assertEqual([], self.validator.validate_object("asset-manifest", {k: value[k] for k in CONTRACT_FIELDS}))

    def test_ac5_scoring_invalid_json_and_duration_rejected(self):
        bad = self.write("authorized/demo/bad.json", b"{not json")
        response = self.register(self.manifest(asset_id="s1", asset_type="scoring", relative_path="authorized/demo/bad.json", sha256=bad, duration_ms=None))
        self.assertEqual((422, "SCORING_INVALID_JSON"), (response.status_code, self.code(response)))
        arr = self.write("authorized/demo/arr.json", b"[1, 2, 3]")
        response = self.register(self.manifest(asset_id="s2", asset_type="scoring", relative_path="authorized/demo/arr.json", sha256=arr, duration_ms=None))
        self.assertEqual((422, "SCORING_INVALID_JSON"), (response.status_code, self.code(response)))
        ok = self.write("authorized/demo/ok.json", b"{}")
        response = self.register(self.manifest(asset_id="s3", asset_type="scoring", relative_path="authorized/demo/ok.json", sha256=ok, duration_ms=5000))
        self.assertEqual((422, "SCORING_DURATION"), (response.status_code, self.code(response)))
        self.assertEqual(0, self.count())

    # ---- AC-6 ----
    def test_ac6_probe_duration_mismatch_409(self):
        digest = self.write("authorized/demo/clip.mp4", b"v")
        self.prober.result = probed(60000 + DURATION_TOLERANCE_MS + 1)
        response = self.register(self.manifest(sha256=digest))
        self.assertEqual((409, "DURATION_MISMATCH"), (response.status_code, self.code(response)))
        self.prober.result = probed(60000 + DURATION_TOLERANCE_MS)
        self.assertEqual(201, self.register(self.manifest(sha256=digest)).status_code)

    def test_ac6_probe_failed_422_and_unavailable_registers_pending(self):
        digest = self.write("authorized/demo/clip.mp4", b"v")
        self.prober.result = ProbeResult(status="failed", tool="ffprobe", error_code="FFPROBE_EXIT_1")
        response = self.register(self.manifest(sha256=digest))
        self.assertEqual((422, "MEDIA_UNREADABLE"), (response.status_code, self.code(response)))
        self.assertEqual(0, self.count())
        self.prober.result = ProbeResult(status="unavailable", tool="ffprobe", error_code="FFPROBE_NOT_FOUND")
        response = self.register(self.manifest(sha256=digest))
        self.assertEqual(201, response.status_code, response.text)
        self.assertEqual("unavailable", response.json()["probe"]["status"])
        self.assertTrue(response.json()["probe_pending"])
        self.assertEqual(60000, response.json()["duration_ms"])    # 时长来自清单，不冒充探测

    def test_ac6_video_without_duration_rejected(self):
        digest = self.write("authorized/demo/clip.mp4", b"v")
        response = self.register(self.manifest(sha256=digest, duration_ms=None))
        self.assertEqual((422, "DURATION_INVALID"), (response.status_code, self.code(response)))
        response = self.register(self.manifest(sha256=digest, duration_ms=0))
        self.assertEqual((422, "DURATION_INVALID"), (response.status_code, self.code(response)))

    # ---- AC-7 ----
    def test_ac7_idempotent_same_manifest_200_and_conflict_409(self):
        digest = self.write("authorized/demo/clip.mp4", b"v")
        first = self.register(self.manifest(sha256=digest))
        self.assertEqual(201, first.status_code)
        again = self.register(self.manifest(sha256=digest))
        self.assertEqual(200, again.status_code, again.text)
        self.assertEqual(first.json(), again.json())
        self.assertEqual(1, self.count())
        other = self.write("authorized/demo/other.mp4", b"w")
        conflict = self.register(self.manifest(relative_path="authorized/demo/other.mp4", sha256=other))
        self.assertEqual((409, "ASSET_CONFLICT"), (conflict.status_code, self.code(conflict)))
        self.assertEqual(1, self.count())

    # ---- AC-8 ----
    def test_ac8_unknown_closed_and_unauthenticated(self):
        digest = self.write("authorized/demo/clip.mp4", b"v")
        response = self.client.post(f"/api/sessions/{uuid4()}/assets/register", json=self.manifest(sha256=digest))
        self.assertEqual((404, "SESSION_NOT_FOUND"), (response.status_code, self.code(response)))
        self.assertEqual(404, self.client.get(f"/api/sessions/{uuid4()}/assets").status_code)
        with self.Session.begin() as db:
            db.scalar(select(EvalSession).where(EvalSession.session_id == self.session_id)).status = "closed"
        response = self.register(self.manifest(sha256=digest))
        self.assertEqual((409, "SESSION_CLOSED"), (response.status_code, self.code(response)))
        anonymous = TestClient(self.app, headers={"X-Requested-With": "XMLHttpRequest"})
        self.assertEqual(401, anonymous.post(self.url + "/register", json=self.manifest(sha256=digest)).status_code)
        self.assertEqual(401, anonymous.get(self.url).status_code)
        anonymous.close()

    # ---- AC-9 ----
    def test_ac9_root_unconfigured_503(self):
        digest = self.write("authorized/demo/clip.mp4", b"v")
        del self.app.dependency_overrides[get_assets_root]
        for value in (None, "relative/dir", str(self.root_path / "does-not-exist")):
            with self.subTest(root=value):
                if value is None:
                    os.environ.pop("STAGE_ASSETS_ROOT", None)
                else:
                    os.environ["STAGE_ASSETS_ROOT"] = value
                response = self.register(self.manifest(sha256=digest))
                self.assertEqual((503, "ASSET_ROOT_UNCONFIGURED"), (response.status_code, self.code(response)))
        os.environ["STAGE_ASSETS_ROOT"] = str(self.root_path)
        self.assertEqual(201, self.register(self.manifest(sha256=digest)).status_code)
        os.environ.pop("STAGE_ASSETS_ROOT", None)

    # ---- AC-10 ----
    def test_ac10_example_manifest_passes_contract(self):
        example = json.loads((REPO / "stage_assets" / "manifest.example.json").read_text(encoding="utf-8"))
        self.assertTrue(example["synthetic"])
        self.assertEqual(3, len(example["assets"]))
        for item in example["assets"]:
            with self.subTest(asset=item["asset_id"]):
                self.assertEqual([], self.validator.validate_object("asset-manifest", item))
                self.assertTrue(item["asset_id"].startswith("synthetic-"))
        self.assertEqual({"video", "audio", "scoring"}, {item["asset_type"] for item in example["assets"]})

    # ---- 独立复核发现（2026-10-19，变更包 1.02 验证第 7 节）----
    def test_r1_probed_without_duration_is_rejected(self):
        digest = self.write("authorized/demo/clip.mp4", b"v")
        self.prober.result = ProbeResult(status="probed", tool="ffprobe", duration_ms=None, format_name="h264", streams=[])
        response = self.register(self.manifest(sha256=digest))
        self.assertEqual((422, "MEDIA_UNREADABLE"), (response.status_code, self.code(response)))
        self.assertEqual(0, self.count())

    def test_r2_reregister_refreshes_probe_after_ffprobe_becomes_available(self):
        digest = self.write("authorized/demo/clip.mp4", b"v")
        self.prober.result = ProbeResult(status="unavailable", tool="ffprobe", error_code="FFPROBE_NOT_FOUND")
        first = self.register(self.manifest(sha256=digest))
        self.assertTrue(first.json()["probe_pending"])
        self.prober.result = probed(60000)                 # 10.2 装好 ffprobe 之后
        again = self.register(self.manifest(sha256=digest))
        self.assertEqual(200, again.status_code, again.text)
        self.assertEqual(("probed", 60000, False), (again.json()["probe"]["status"], again.json()["probe"]["duration_ms"], again.json()["probe_pending"]))
        self.assertEqual(1, self.count())
        self.prober.result = ProbeResult(status="unavailable", tool="ffprobe", error_code="FFPROBE_NOT_FOUND")
        third = self.register(self.manifest(sha256=digest))   # 探测器又不可用：不把已探测的记录降级
        self.assertEqual("probed", third.json()["probe"]["status"])

    def test_r3_unreadable_file_is_422_not_500(self):
        digest = self.write("authorized/demo/clip.mp4", b"v")
        import app.routers.stage_assets as module
        with mock.patch.object(module, "_sha256_and_size", side_effect=OSError("locked")):
            response = self.register(self.manifest(sha256=digest))
        self.assertEqual((422, "FILE_UNREADABLE"), (response.status_code, self.code(response)))
        self.assertEqual(0, self.count())

    def test_r4_drive_letter_segment_rejected_before_lookup(self):
        response = self.register(self.manifest(relative_path="authorized/D:/app/main.py"))
        self.assertEqual((422, "PATH_INVALID"), (response.status_code, self.code(response)))

    def test_r5_duration_must_be_strict_int_within_int4(self):
        digest = self.write("authorized/demo/clip.mp4", b"v")
        for bad in ("60000", True, 2 ** 40, 1.5):
            with self.subTest(duration=bad):
                response = self.register(self.manifest(sha256=digest, duration_ms=bad))
                self.assertEqual(422, response.status_code, response.text)
        self.assertEqual(0, self.count())

    def test_r6_non_canonical_alias_rejected(self):
        digest = self.write("authorized/demo/clip.mp4", b"v")
        response = self.register(self.manifest(relative_path="AUTHORIZED/DEMO/CLIP.MP4", sha256=digest))
        self.assertIn(self.code(response), ("PATH_NOT_CANONICAL", "FILE_NOT_FOUND"))   # 大小写不敏感文件系统 → 别名拒；敏感 → 不存在
        self.assertEqual(0, self.count())
        self.assertEqual(201, self.register(self.manifest(sha256=digest)).status_code)

    def test_r8_scoring_too_large_and_bom(self):
        import app.routers.stage_assets as module
        big = self.write("authorized/demo/big.json", b"{}" + b" " * 64)
        with mock.patch.object(module, "SCORING_MAX_BYTES", 10):
            response = self.register(self.manifest(asset_id="big", asset_type="scoring", relative_path="authorized/demo/big.json", sha256=big, duration_ms=None))
        self.assertEqual((422, "SCORING_INVALID_JSON"), (response.status_code, self.code(response)))
        bom = self.write("authorized/demo/bom.json", b"\xef\xbb\xbf" + json.dumps({"ok": True}).encode("utf-8"))
        response = self.register(self.manifest(asset_id="bom", asset_type="scoring", relative_path="authorized/demo/bom.json", sha256=bom, duration_ms=None))
        self.assertEqual(201, response.status_code, response.text)

    # ---- 模型约束 ----
    def test_model_unique_and_check_constraints(self):
        def row(**kw):
            base = dict(session_id=self.session_id, asset_id="m-1", asset_type="video", relative_path="authorized/demo/m.mp4",
                        sha256="a" * 64, duration_ms=1000, authorization_source="auth", visibility_notes="n",
                        size_bytes=1, probe={"status": "unavailable"}, registered_by="asset_admin")
            base.update(kw)
            return StageAsset(**base)
        with self.Session.begin() as db:
            db.add(row())
        with self.assertRaises(IntegrityError):
            with self.Session.begin() as db:
                db.add(row())                                   # (session_id, asset_id) 唯一
        with self.assertRaises(IntegrityError):
            with self.Session.begin() as db:
                db.add(row(asset_id="m-2", asset_type="image"))  # 类型 CHECK
        with self.assertRaises(IntegrityError):
            with self.Session.begin() as db:
                db.add(row(asset_id="m-3", asset_type="scoring", duration_ms=5))   # scoring 不能有时长
        with self.assertRaises(IntegrityError):
            with self.Session.begin() as db:
                db.add(row(asset_id="m-4", session_id=str(uuid4())))              # 场次外键

    # ---- 探测器 ----
    def test_ffprobe_prober_unavailable_failed_and_probed(self):
        target = self.root_path / "authorized" / "demo" / "clip.mp4"
        target.write_bytes(b"v")
        self.assertEqual("unavailable", FfprobeProber(command=[]).probe(target).status)
        ok_script = self.root_path / "fake_ffprobe_ok.py"
        ok_script.write_text("import json,sys\nprint(json.dumps({'format': {'format_name': 'mov,mp4', 'duration': '60.500'},"
                             " 'streams': [{'codec_type': 'video', 'codec_name': 'h264', 'width': 1280, 'height': 720, 'extra': 'x'}]}))\n",
                             encoding="utf-8")
        result = FfprobeProber(command=[sys.executable, str(ok_script)]).probe(target)
        self.assertEqual(("probed", 60500, "mov,mp4"), (result.status, result.duration_ms, result.format_name))
        self.assertEqual([{"codec_type": "video", "codec_name": "h264", "width": 1280, "height": 720}], result.streams)
        bad_script = self.root_path / "fake_ffprobe_bad.py"
        bad_script.write_text("import sys\nsys.exit(1)\n", encoding="utf-8")
        failed = FfprobeProber(command=[sys.executable, str(bad_script)]).probe(target)
        self.assertEqual(("failed", "FFPROBE_EXIT_1"), (failed.status, failed.error_code))
        garbage = self.root_path / "fake_ffprobe_garbage.py"
        garbage.write_text("print('not json')\n", encoding="utf-8")
        self.assertEqual("failed", FfprobeProber(command=[sys.executable, str(garbage)]).probe(target).status)


if __name__ == "__main__":
    unittest.main()
