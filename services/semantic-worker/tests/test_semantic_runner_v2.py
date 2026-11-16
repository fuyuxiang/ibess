import copy
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from worker.llm_client import ModelClientError
from worker.runner import SemanticRunner
from worker.result_validator import SemanticResultError


RESULT = {
    "scene_id": "S06", "conclusion": "normal", "risk_level": "none", "reason": "正常讨论。",
    "evidence": [], "missing_facts": [], "recommendation": "no_action",
}


class MemoryRepository:
    def __init__(self, task):
        self.task = copy.deepcopy(task)
        self.completed = []
        self.failed = []
        self.claims = 0

    def claim_next(self, worker_id, lease_seconds):
        if self.task is None or self.task.get("claimed"):
            return None
        self.claims += 1
        self.task["claimed"] = True
        self.task["claim_id"] = f"claim-{self.claims}"
        return copy.deepcopy(self.task)

    def complete(self, analysis_id, claim_id, result):
        if claim_id != self.task.get("claim_id"):
            return False
        self.completed.append((analysis_id, claim_id, result))
        return True

    def fail(self, analysis_id, claim_id, error, retryable):
        if claim_id != self.task.get("claim_id"):
            return False
        self.failed.append((analysis_id, claim_id, error, retryable))
        return True


class FlakyPipeline:
    def __init__(self, failures=0):
        self.failures = failures
        self.calls = 0

    def analyze(self, request):
        self.calls += 1
        if self.calls <= self.failures:
            raise ModelClientError("MODEL_RESPONSE_INVALID")
        return copy.deepcopy(RESULT)


class InvalidPipeline:
    def analyze(self, request):
        raise SemanticResultError("QUOTE_MISMATCH", "invalid quote")


def task():
    return {"analysis_id": "analysis-1", "claim_id": None, "request": {"scene_id": "S06"}, "claimed": False}


class SemanticRunnerTests(unittest.TestCase):
    def test_retries_cloud_model_failure_twice_then_completes(self):
        repo = MemoryRepository(task())
        pipeline = FlakyPipeline(failures=2)
        runner = SemanticRunner(repo, pipeline, worker_id="semantic-a", max_retries=2)

        processed = runner.run_once()

        self.assertTrue(processed)
        self.assertEqual(3, pipeline.calls)
        self.assertEqual(1, len(repo.completed))
        self.assertEqual([], repo.failed)

    def test_model_failure_after_retry_budget_is_retryable_failed_task(self):
        repo = MemoryRepository(task())
        pipeline = FlakyPipeline(failures=3)
        runner = SemanticRunner(repo, pipeline, worker_id="semantic-a", max_retries=2)

        self.assertTrue(runner.run_once())
        self.assertEqual(3, pipeline.calls)
        self.assertEqual([], repo.completed)
        self.assertEqual(("MODEL_RESPONSE_INVALID", True), (repo.failed[0][2], repo.failed[0][3]))

    def test_invalid_model_result_is_not_retried(self):
        repo = MemoryRepository(task())
        runner = SemanticRunner(repo, InvalidPipeline(), worker_id="semantic-a", max_retries=2)

        self.assertTrue(runner.run_once())
        self.assertEqual(1, len(repo.failed))
        self.assertEqual(("SEMANTIC_RESULT_INVALID", False), (repo.failed[0][2], repo.failed[0][3]))

    def test_stale_claim_cannot_mark_new_claim_complete(self):
        repo = MemoryRepository(task())
        runner = SemanticRunner(repo, FlakyPipeline(), worker_id="semantic-a")
        claimed = repo.claim_next("semantic-a", 120)
        repo.task["claim_id"] = "claim-new"

        self.assertFalse(runner.complete_claim(claimed, RESULT))
        self.assertEqual([], repo.completed)

    def test_no_task_is_a_noop(self):
        repo = MemoryRepository(None)
        runner = SemanticRunner(repo, FlakyPipeline(), worker_id="semantic-a")

        self.assertFalse(runner.run_once())


if __name__ == "__main__":
    unittest.main(verbosity=2)
