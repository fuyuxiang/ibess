"""验证分发边界和本机配置初始化，完全使用临时目录。"""
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest

from init_env import initialize
from package_source import package


class SourceHygieneTests(unittest.TestCase):
    def test_archive_excludes_private_data_but_keeps_templates_and_fixtures(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            root.mkdir()
            keep = [".env.example", "deploy/linux/.env.example", "deploy/integration/a.env.example",
                    "services/api/app/main.py", "docs/contracts/fixtures/sample.json",
                    "web-console/src/components/evidence/JointEvidence.tsx",
                    "services/api/tests/face_fixtures/public.jpg", "web-console/public/stage/placeholder.svg"]
            private = [".env", ".env.production", "deploy/a.env", "config/dev.local",
                       "certs/client.key", "certs/client.pem", "certs/client.p12", "id_ed25519",
                       "data.sqlite3", "backups/data.zip", "private_assets/a.jpg", "evidence/frame.jpg",
                       "tests/视频/a.m4a", "web-console/public/stage/room.jpg",
                       "web-console/tests/auth-result.json", "web-console/tests/auth-preparation.json",
                       "web-console/tests/business-export-confirm.json", "tests/integration/service-result.json",
                       "docs/generated/manual.docx", "node_modules/pkg/index.js", "log/debug.log"]
            for name in keep + private:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("fixture", encoding="utf-8")
            outside = Path(tmp) / "outside-secret"
            outside.write_text("private", encoding="utf-8")
            (root / "public-link.txt").symlink_to(outside)
            output = root / "output" / "source.tar.gz"
            package(root, output)
            with tarfile.open(output) as archive:
                members = {m.name.removeprefix("./"): m for m in archive.getmembers()}
            self.assertTrue(set(keep).issubset(members))
            self.assertTrue(set(private).isdisjoint(members))
            self.assertNotIn("public-link.txt", members)
            self.assertTrue(all(m.uid == 0 and m.gid == 0 and not m.uname and not m.gname for m in members.values()))

    def test_init_creates_distinct_random_secrets_without_overwriting(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env.example").write_text("POSTGRES_PASSWORD=\nMINIO_ROOT_PASSWORD=\nPHASE_CONTROL_TOKEN_EVENT_SERVICE=\n")
            env = initialize(root)
            text = env.read_text()
            values = dict(line.split("=", 1) for line in text.splitlines())
            secrets = [values[k] for k in ("POSTGRES_PASSWORD", "MINIO_ROOT_PASSWORD", "PHASE_CONTROL_TOKEN_EVENT_SERVICE")]
            self.assertEqual(len(set(secrets)), 3)
            self.assertTrue(all(len(v) >= 40 for v in secrets))
            self.assertEqual(env.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                initialize(root)
            self.assertEqual(env.read_text(), text)

    def test_shell_loader_preserves_environment_and_treats_values_as_data(self):
        loader = Path(__file__).with_name("load-env.sh").resolve()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = root / ".env"
            env.write_text('IBESS_TEST_EXISTING=file\nIBESS_TEST_VALUE="value with spaces"\nIBESS_TEST_LITERAL=$(touch injected)\n')
            result = subprocess.run(["bash", "-eu", "-c", '''
source "$1"
load_poc_env "$2"
[ "$IBESS_TEST_EXISTING" = inherited ]
[ "$IBESS_TEST_VALUE" = 'value with spaces' ]
[ "$IBESS_TEST_LITERAL" = '$(touch injected)' ]
''', "test", str(loader), str(env)], cwd=root,
                env={**os.environ, "IBESS_TEST_EXISTING": "inherited"}, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((root / "injected").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
