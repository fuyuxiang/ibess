import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[3]
SERVICE_ROOT = ROOT / "services" / "semantic-worker"
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from worker.runner import build_runner_from_environment


class SemanticDeploymentConfigTests(unittest.TestCase):
    def test_missing_cloud_key_fails_before_worker_starts(self):
        env = {
            "DATABASE_URL": "postgresql+psycopg://demo:demo@postgres:5432/demo",
            "SEMANTIC_MODEL_BASE_URL": "https://api.minimax.cn/v1",
            "SEMANTIC_MODEL_NAME": "MiniMax-M3.1-Flash-Preview",
            "SEMANTIC_MODEL_API_KEY": "",
        }
        with patch.dict(os.environ, env, clear=False):
            with self.assertRaisesRegex(RuntimeError, "SEMANTIC_MODEL_API_KEY"):
                build_runner_from_environment()

    def test_compose_declares_cloud_worker_and_secret_env_names(self):
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn("semantic-worker:", compose)
        self.assertIn("SEMANTIC_MODEL_BASE_URL", compose)
        self.assertIn("SEMANTIC_MODEL_NAME", compose)
        self.assertIn("SEMANTIC_MODEL_API_KEY", compose)
        dockerfile = (ROOT / "services" / "semantic-worker" / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn('CMD ["python", "-m", "worker.runner"]', dockerfile)

    def test_env_example_documents_cloud_model_without_real_secret(self):
        env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
        self.assertIn("SEMANTIC_MODEL_BASE_URL=https://api.minimax.cn/v1", env_example)
        self.assertIn("SEMANTIC_MODEL_NAME=MiniMax-M3.1-Flash-Preview", env_example)
        self.assertIn("SEMANTIC_MODEL_API_KEY=", env_example)
        self.assertNotIn("sk-", env_example)


if __name__ == "__main__":
    unittest.main(verbosity=2)
