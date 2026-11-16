"""Real PostgreSQL migration tests: fresh install, legacy adoption, atomicity and concurrency."""
import json
import os
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import unittest
from uuid import uuid4

from sqlalchemy import create_engine, inspect, text


class SchemaMigrationTest(unittest.TestCase):
    def setUp(self):
        url = os.environ.get("WORKFLOW_TEST_DATABASE_URL")
        if not url:
            self.skipTest("Explicit dedicated WORKFLOW_TEST_DATABASE_URL required")
        self.admin = create_engine(url)
        self.schema = "test_migration_" + uuid4().hex
        with self.admin.begin() as connection:
            connection.exec_driver_sql(f'CREATE SCHEMA "{self.schema}"')
        self.engine = create_engine(url, connect_args={"options": f"-csearch_path={self.schema}"})
        snapshot = Path(__file__).resolve().parents[1] / "migrations/snapshots/0001_schema.json"
        self.baseline = json.loads(snapshot.read_text())

    def tearDown(self):
        if hasattr(self, "engine"):
            self.engine.dispose()
            with self.admin.begin() as connection:
                connection.exec_driver_sql(f'DROP SCHEMA "{self.schema}" CASCADE')
            self.admin.dispose()

    def test_empty_database_and_repeat_upgrade_match_head(self):
        from app.migrations import upgrade_schema, verify_schema
        upgrade_schema(self.engine)
        verify_schema(self.engine)
        upgrade_schema(self.engine)
        self.assertEqual(set(inspect(self.engine).get_table_names()), {t["name"] for t in self.baseline} | {"alembic_version"})

    def legacy(self, omit=()):
        omitted = {"description", "created_at", "updated_at", "phase_id", *omit}
        with self.engine.begin() as connection:
            for table in self.baseline:
                if table["name"] not in ("events", "evidences", "operators", "operator_bindings"):
                    continue
                lines = [line for line in table["create_sql"].splitlines()
                         if not line.strip() or line.strip().split()[0] not in omitted]
                sql = "\n".join(lines).replace(", \n)", "\n)").replace(",\n)", "\n)")
                connection.exec_driver_sql(sql)
                for index in table["indexes"]:
                    if not any(word in index["sql"] for word in omitted):
                        connection.exec_driver_sql(index["sql"])
            connection.execute(text("INSERT INTO events(event_id,reason,status) VALUES ('preserved-event','original reason','CONFIRMED')"))
            connection.execute(text("INSERT INTO evidences(event_id,object_key) VALUES ('preserved-event','original.jpg')"))
            if "input_types" not in omitted:
                connection.execute(text("""INSERT INTO operators(operator_id,code,enabled,input_types,parameters)
                    VALUES ('preserved-rule','whispering',false,'["behavior_detected"]',CAST(:parameters AS jsonb))"""),
                    {"parameters": json.dumps({"duration_seconds": 8})})

    def test_legacy_rows_and_custom_rule_configuration_are_preserved(self):
        from app.migrations import upgrade_schema, verify_schema
        self.legacy()
        upgrade_schema(self.engine)
        verify_schema(self.engine)
        with self.engine.connect() as connection:
            self.assertEqual(connection.execute(text("SELECT reason,status FROM events WHERE event_id='preserved-event'")).one(), ("original reason", "CONFIRMED"))
            self.assertEqual(connection.scalar(text("SELECT object_key FROM evidences WHERE event_id='preserved-event'")), "original.jpg")
            enabled, parameters = connection.execute(text("SELECT enabled,parameters FROM operators WHERE code='whispering'")).one()
            self.assertFalse(enabled)
            self.assertEqual(parameters, {"duration_seconds": 8})

    def test_concurrent_upgrade_is_serialized(self):
        from app.migrations import upgrade_schema, verify_schema
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda _: upgrade_schema(self.engine), range(2)))
        verify_schema(self.engine)

    def test_incompatible_legacy_schema_rolls_back_without_stamping(self):
        from app.migrations import upgrade_schema
        self.legacy(omit=("input_types",))
        before = set(inspect(self.engine).get_table_names())
        with self.assertRaisesRegex(RuntimeError, "Explicit data migration"):
            upgrade_schema(self.engine)
        self.assertEqual(set(inspect(self.engine).get_table_names()), before)
        with self.engine.connect() as connection:
            self.assertEqual(connection.scalar(text("SELECT count(*) FROM events")), 1)

    def test_worker_readiness_never_creates_tables(self):
        from ibess_db.schema import require_schema
        with self.assertRaises(RuntimeError):
            require_schema(self.engine, ("events", "evidences"))
        self.assertEqual(inspect(self.engine).get_table_names(), [])
