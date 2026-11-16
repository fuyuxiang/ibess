"""执行卡 10.2：A 侧部署叠加的静态校验 + 迁移脚本的 SQLite 演练。本机没有 Docker，真实 compose config / up 在另一台机器。
运行（项目根）：python -m pytest -q deploy/integration/test_integration_deploy.py
"""
import importlib.util
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
OVERLAY = HERE / "docker-compose.a.yml"
NEW_SERVICES = ("stage-media-controller", "offline-worker")
A_TABLES_API = ("stage_assets", "stage_media_bindings", "offline_qa_jobs", "source_zone_configs")
A_TABLES_EVENT = ("risk_clues", "clue_processing", "observation_receipts")


def load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def split_volume(volume: str) -> tuple[str, str, str]:
    """(来源, 容器路径, 选项)；来源可以是 ${VAR:-默认} 插值，默认值本身带冒号前缀，不能直接按冒号切。"""
    match = re.match(r"^(\$\{[^}]*\}|[^:]+):([^:]+)(?::(.+))?$", volume)
    assert match, volume
    source = match.group(1)
    if source.startswith("${"):
        source = source.split(":-", 1)[1][:-1] if ":-" in source else source
    return source, match.group(2), match.group(3) or ""


class OverlayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = load(ROOT / "docker-compose.yml")
        cls.overlay = load(OVERLAY)

    def test_ac1_only_adds_services_and_api_mount(self):
        services = self.overlay["services"]
        self.assertEqual(set(services) - set(self.base["services"]), set(NEW_SERVICES))
        touched = set(services) & set(self.base["services"])
        self.assertEqual(touched, {"api"}, "B 的服务只碰 api")
        api = services["api"]
        self.assertEqual(set(api), {"environment", "volumes"}, "5.01：api 仍用 B 的镜像，只追加素材根与只读挂载")
        self.assertEqual(set(api["environment"]), {"STAGE_ASSETS_ROOT"})
        self.assertTrue(all(v.endswith(":ro") for v in api["volumes"]), api["volumes"])

    def test_501_controller_publishes_to_own_path(self):
        url = self.overlay["services"]["stage-media-controller"]["environment"]["RTSP_PUBLISH_URL"]
        self.assertNotRegex(url, r"/cam[1-9]\}?$", "不能与 B 的 poc-pub-cam1～9 同推一个路径")
        self.assertIn("/stage", url)

    def test_ac2_new_services_internal_healthy_readonly(self):
        text = OVERLAY.read_text(encoding="utf-8")
        self.assertNotRegex(text, r"localhost|127\.0\.0\.1", "跨机部署不写 localhost")
        for name in NEW_SERVICES:
            service = self.overlay["services"][name]
            self.assertNotIn("ports", service, f"{name} 不对外暴露端口")
            self.assertEqual(service["depends_on"]["postgres"]["condition"], "service_healthy")
            self.assertIn("profiles", service, f"{name} 按需启动")
            targets = set()
            for volume in service.get("volumes", []):
                _source, target, options = split_volume(volume)
                targets.add(target)
                if target in ("/stage-assets", "/assets"):
                    self.assertEqual(options, "ro", f"{name} 的 {target} 必须只读")
            self.assertIn("/stage-assets", targets)
            env = service["environment"]
            for key, value in env.items():
                if re.search(r"TOKEN|KEY|PASSWORD", key):
                    self.assertRegex(str(value), r"^\$\{", f"{name}.{key} 只能由环境项注入，不写死")
        controller = self.overlay["services"]["stage-media-controller"]["environment"]
        self.assertEqual(controller["MEDIA_SESSION_ID"], "${MEDIA_SESSION_ID:-}", "场次由部署填写，不给默认场次")
        offline = self.overlay["services"]["offline-worker"]
        self.assertEqual(offline["build"], {"context": ".", "dockerfile": "services/offline-worker/Dockerfile"})

    def test_mount_sources_exist_or_are_declared(self):
        declared = set(self.base.get("volumes", {})) | set(self.overlay.get("volumes", {}))
        for name, service in self.overlay["services"].items():
            for volume in service.get("volumes", []):
                source, _target, _options = split_volume(volume)
                if source.startswith("."):
                    self.assertTrue((ROOT / source).exists() or source.startswith("../"), f"{name} 挂载源 {source} 不存在")
                else:
                    self.assertIn(source, declared, f"{name} 用了未声明的卷 {source}")
        for name, service in self.overlay["services"].items():
            build = service.get("build")
            if build:
                context = ROOT / build["context"]
                self.assertTrue((context / build["dockerfile"]).resolve().is_file(), f"{name} 的 Dockerfile 不存在")

    def test_ac6_images_only_add_confirmed_dependencies(self):
        controller = (HERE / "media-controller.Dockerfile").read_text(encoding="utf-8")
        self.assertIn("ffmpeg", controller)
        installs = set(re.findall(r"pip install[^\n]*", controller))
        self.assertEqual(installs, {"pip install --no-cache-dir redis sqlalchemy \"psycopg[binary]\""})
        api = (HERE / "api-a.Dockerfile").read_text(encoding="utf-8")
        self.assertIn("FROM ${BASE_IMAGE}", api)
        self.assertNotIn("pip install", api, "api 派生镜像只加 ffmpeg")


class ScriptTest(unittest.TestCase):
    def test_ac4_start_script_guards(self):
        script = (HERE / "start_a.ps1").read_text(encoding="utf-8")
        self.assertIn("start-demo.sh", script, "5.01：B 的启动方式照旧，本脚本只在其后按需执行")
        self.assertIn("poc-publisher", script)
        self.assertRegex(script, r"poc-publisher[\s\S]*throw", "旧推流在跑时拒绝启动控制器")
        self.assertIn("/cam[1-9]$", script, "推流路径与 B 的 cam1～9 相同时拒绝")
        self.assertIn("未就绪", script, "模型 / 素材缺失时显式报")
        for forbidden in ("down -v", "volume rm", "--force-recreate", "Remove-Item"):
            self.assertNotIn(forbidden, script)
        self.assertIn("docker-compose.a.yml", script)

    def test_ac5_backup_and_restore_are_non_destructive(self):
        backup = (ROOT / "scripts" / "backup_poc.ps1").read_text(encoding="utf-8")
        self.assertIn("pg_dump", backup)
        self.assertNotRegex(backup, r"\bpsql\b|pg_restore|down -v|volume rm|Remove-Item", "备份只读，不回写")
        self.assertIn(":ro", backup, "打包卷时只读挂载")
        restore = (HERE / "restore_poc.ps1").read_text(encoding="utf-8")
        self.assertIn("_restore_", restore, "默认恢复到新库名")
        self.assertIn("OverwriteCurrentDatabase", restore, "覆盖现库必须显式开关")
        self.assertNotIn("down -v", restore)

    def test_review_ps7_bom_volume_check_absolute_path_and_event_source(self):
        for path in (HERE / "start_a.ps1", HERE / "restore_poc.ps1", ROOT / "scripts" / "backup_poc.ps1"):
            raw = path.read_bytes()
            self.assertTrue(raw.startswith(b"\xef\xbb\xbf"), f"{path.name} 要带 BOM，Windows PowerShell 才不按 ANSI 码页误读")
            self.assertTrue(raw[3:].startswith(b"#Requires -Version 7"), f"{path.name} 要声明 PowerShell 7")
            self.assertNotIn("powershell -File", path.read_text(encoding="utf-8-sig"))
        backup = (ROOT / "scripts" / "backup_poc.ps1").read_text(encoding="utf-8-sig")
        self.assertNotIn("volume inspect", backup, "卷不存在时 5.1 下会抛终止错误；用 volume ls 过滤")
        self.assertIn("GetFullPath", backup, "docker -v 需要绝对路径")
        self.assertNotIn("powershell -File", (HERE / "README.md").read_text(encoding="utf-8"))

    def test_501_recreate_script_is_dry_run_by_default(self):
        script = (HERE / "recreate_vision_workers.sh").read_text(encoding="utf-8")
        self.assertIn("APPLY=0", script)
        self.assertIn('[ "$APPLY" -eq 1 ] || continue', script, "改名、删除与重建只在 --apply 时执行")
        self.assertIn("MSYS_NO_PATHCONV=1", script, "Git Bash 下不能让 docker 参数被改写成 Windows 路径")
        code = "\n".join(line for line in script.splitlines() if not line.lstrip().startswith("#"))
        self.assertNotRegex(code, r"\beval\b", "参数用数组传，不经 eval（注释里提到不算）")
        self.assertRegex(script, r'docker rename "\$name" "\$name-old"[\s\S]*docker rename "\$name-old" "\$name"', "先改名备份，失败改回")
        self.assertLess(script.index('docker rename "$name" "$name-old"'), script.index('docker rm "$name-old"'), "新容器起来后才删旧的")
        self.assertIn("docker image inspect", script, "镜像不存在时不动任何容器")
        for forbidden in ("volume rm", "down -v", "system prune"):
            self.assertNotIn(forbidden, script)
        checked = subprocess.run(["bash", "-n", str(HERE / "recreate_vision_workers.sh")], capture_output=True, text=True)
        self.assertEqual(checked.returncode, 0, checked.stderr)

    def test_shared_release_build_inputs_exist(self):
        # The current release explicitly authorizes unified build contexts and migrations.
        for service in ("api", "event-service", "media-service", "semantic-worker", "vision-worker", "audio-worker"):
            dockerfile = (ROOT / "services" / service / "Dockerfile").read_text()
            self.assertIn("packages/ibess-common", dockerfile)
            if service not in ("vision-worker", "audio-worker"):
                self.assertTrue((ROOT / "services" / service / "requirements.lock").is_file())
        self.assertTrue((ROOT / "services/api/alembic.ini").is_file())


class MigrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("migrate_a_tables", HERE / "migrate_a_tables.py")
        cls.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.mod)

    def _run(self, target, url, *extra):
        services = ROOT / "services" / ("api" if target == "api" else "event-service")
        return subprocess.run([sys.executable, "-X", "utf8", str(HERE / "migrate_a_tables.py"), "--target", target, "--database-url", url, *extra],
                              cwd=services, capture_output=True, text=True, env={**__import__("os").environ, "PYTHONPATH": str(services)})

    def test_dry_run_and_sqlite_apply_never_mutate_legacy_rows(self):
        from sqlalchemy import create_engine, inspect, text
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            url = f"sqlite:///{Path(tmp) / 'a.db'}"
            engine = create_engine(url)
            with engine.begin() as connection:
                connection.execute(text("CREATE TABLE eval_sessions (session_id VARCHAR(36) PRIMARY KEY, name VARCHAR(64))"))
                connection.execute(text("INSERT INTO eval_sessions VALUES ('s-1', 'old')"))
            dry = self._run("api", url)
            self.assertEqual(dry.returncode, 0, dry.stderr)
            self.assertIn("dry-run", dry.stdout)
            refused = self._run("api", url, "--apply")
            self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
            self.assertIn("PostgreSQL", refused.stdout)
            self.assertEqual(inspect(engine).get_table_names(), ["eval_sessions"])
            with engine.connect() as connection:
                self.assertEqual(connection.scalar(text("SELECT name FROM eval_sessions")), "old")
            engine.dispose()

    def test_table_lists_match_models(self):
        self.assertEqual(tuple(self.mod.TARGETS["api"]["tables"]), A_TABLES_API)
        self.assertEqual(tuple(self.mod.TARGETS["event"]["tables"]), A_TABLES_EVENT)


if __name__ == "__main__":
    unittest.main()
