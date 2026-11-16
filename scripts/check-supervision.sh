#!/usr/bin/env bash
# All database tests create random schemas; never loads .env or writes deployment events.
set -euo pipefail
cd "$(dirname "$0")/.."
: "${SUPERVISION_TEST_DATABASE_URL:?Set an explicit PostgreSQL URL for an isolated test database}"
CHECK_PYTHON=${CHECK_PYTHON:-.venv/bin/python}
if [[ ! -x "$CHECK_PYTHON" ]]; then CHECK_PYTHON=python3; fi
(cd web-console && ./node_modules/.bin/tsc --noEmit --incremental false && ./node_modules/.bin/vite build)
(cd web-console && node --test tests/*.test.mjs src/components/workflow/workflow-model.test.mjs)
WORKFLOW_TEST_DATABASE_URL="$SUPERVISION_TEST_DATABASE_URL" PYTHONPATH=services/api:services/api/tests "$CHECK_PYTHON" -m unittest services/api/tests/test_supervision.py -q
WORKFLOW_TEST_DATABASE_URL="$SUPERVISION_TEST_DATABASE_URL" PYTHONPATH=services/api:services/api/tests "$CHECK_PYTHON" -m unittest services/api/tests/test_supervision_repairs.py -q
WORKFLOW_TEST_DATABASE_URL="$SUPERVISION_TEST_DATABASE_URL" PYTHONPATH=services/api:services/api/tests "$CHECK_PYTHON" -m unittest services/api/tests/test_supervision_workspace.py -q
WORKFLOW_TEST_DATABASE_URL="$SUPERVISION_TEST_DATABASE_URL" PYTHONPATH=services/api:services/api/tests "$CHECK_PYTHON" -m unittest services/api/tests/test_poc_workflow.py -q
SEMANTIC_TEST_DATABASE_URL="$SUPERVISION_TEST_DATABASE_URL" PYTHONPATH=services/api "$CHECK_PYTHON" -m unittest services/api/tests/test_semantic_analyses_v2.py -q
EVENT_TEST_DATABASE_URL="$SUPERVISION_TEST_DATABASE_URL" PYTHONPATH=services/event-service "$CHECK_PYTHON" -m unittest discover -s services/event-service/tests -q
PYTHONPATH=services/media-service "$CHECK_PYTHON" -m unittest discover -s services/media-service/tests -q
PYTHONPATH=services/vision-worker "$CHECK_PYTHON" -m unittest discover -s services/vision-worker/tests -q
for module in test_registry_events test_audio_recordings test_admin_auth; do
  WORKFLOW_TEST_DATABASE_URL="$SUPERVISION_TEST_DATABASE_URL" PYTHONPATH=services/api:services/api/tests "$CHECK_PYTHON" -m unittest "services/api/tests/$module.py" -q
done
