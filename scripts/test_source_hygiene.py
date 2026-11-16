"""验证分发边界和本机配置初始化，完全使用临时目录。"""
import os
import json
import hashlib
from configure_phase_consumer import configure, scopes_for_session
from unittest.mock import patch
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
                    "web-console/src/components/evidence/EvidencePlayer.tsx",
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
            local=root/'config/phase-consumers.local'
            mapping=json.loads(local.read_text())['event-service']
            self.assertEqual(mapping['token_sha256'],hashlib.sha256(values['PHASE_CONTROL_TOKEN_EVENT_SERVICE'].encode()).hexdigest())
            self.assertEqual(mapping['scopes'],[])
            self.assertNotIn(values['PHASE_CONTROL_TOKEN_EVENT_SERVICE'],local.read_text())
            self.assertEqual(local.stat().st_mode & 0o777,0o600)
            with self.assertRaises(FileExistsError):
                initialize(root)
            self.assertEqual(env.read_text(), text)

    def test_consumer_configuration_keeps_exact_scopes_and_does_not_evaluate_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            from uuid import uuid4
            root=Path(tmp);token='A'*43
            (root/'.env').write_text('PHASE_CONTROL_TOKEN_EVENT_SERVICE='+token+'\nIGNORE=$(touch injected)\n')
            scope={key:str(uuid4()) for key in ('project_id','room_id','session_id')}
            scope['source_id']='camera-001'
            path=configure(root,scope);configure(root,scope)
            self.assertEqual(json.loads(path.read_text())['event-service']['scopes'],[scope])
            before=path.read_text()
            with self.assertRaises(ValueError):configure(root,{**scope,'source_id':'*'})
            self.assertEqual(path.read_text(),before)
            self.assertFalse((root/'injected').exists())

    def test_consumer_deployment_reads_all_channels_without_shell_or_wildcards(self):
        from uuid import uuid4
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);sid=str(uuid4())
            (root/'.env').write_text('PHASE_CONTROL_TOKEN_EVENT_SERVICE='+'A'*43)
            scopes=[dict(project_id=str(uuid4()),room_id=str(uuid4()),session_id=sid,source_id=source) for source in ('camera-001','audio-001')]
            with patch('configure_phase_consumer.subprocess.run',return_value=SimpleNamespace(stdout=json.dumps(scopes))) as run:
                result=scopes_for_session(root,sid)
            self.assertEqual(run.call_args.args[0][-1],sid)
            self.assertNotIn('shell',run.call_args.kwargs)
            configure(root,result)
            path=root/'config/phase-consumers.local';before=path.read_text()
            self.assertEqual(json.loads(before)['event-service']['scopes'],scopes)
            with self.assertRaises(ValueError):configure(root,[scopes[0],{**scopes[1],'source_id':'*'}])
            self.assertEqual(path.read_text(),before)
            with patch('configure_phase_consumer.subprocess.run',return_value=SimpleNamespace(stdout=json.dumps([{**scopes[0],'session_id':str(uuid4())}]))):
                with self.assertRaises(ValueError):scopes_for_session(root,sid)

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
