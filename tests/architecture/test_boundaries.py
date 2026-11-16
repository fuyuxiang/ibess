"""Enforce ownership and delivery boundaries, rather than directory naming alone."""

import ast
import json
from pathlib import Path
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]


class ArchitectureTest(unittest.TestCase):
    def test_workers_never_create_or_alter_the_schema(self):
        for service in (
            "event-service",
            "media-service",
            "vision-worker",
            "audio-worker",
            "semantic-worker",
            "offline-worker",
        ):
            for path in (ROOT / "services" / service).rglob("*.py"):
                if "tests" in path.parts:
                    continue
                for node in ast.walk(ast.parse(path.read_text())):
                    if isinstance(node, ast.Call) and isinstance(
                        node.func, ast.Attribute
                    ):
                        self.assertNotIn(
                            node.func.attr, ("create_all", "drop_all"), str(path)
                        )

    def test_contract_package_has_no_service_or_database_imports(self):
        for path in (ROOT / "packages/ibess-common/src/ibess_contracts").rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                imports = (
                    [n.name for n in node.names]
                    if isinstance(node, ast.Import)
                    else [node.module or ""]
                    if isinstance(node, ast.ImportFrom)
                    else []
                )
                for name in imports:
                    self.assertNotIn(
                        name.split(".")[0],
                        ("app", "service", "worker", "sqlalchemy", "ibess_db"),
                        str(path),
                    )

    def test_parameter_shims_have_one_canonical_definition(self):
        from ibess_contracts.rule_parameters import PARAMETERS

        self.assertEqual(
            len(
                set(PARAMETERS)
                & {
                    "whispering",
                    "workstation_visit",
                    "document_transfer",
                    "frequent_absence",
                    "joint_absence",
                    "unauthorized_entry",
                }
            ),
            6,
        )
        for relative in (
            "services/api/app/core/rule_parameters.py",
            "services/event-service/service/rule_parameters.py",
        ):
            tree = ast.parse((ROOT / relative).read_text())
            self.assertTrue(
                any(
                    isinstance(n, ast.ImportFrom)
                    and n.module == "ibess_contracts.rule_parameters"
                    for n in tree.body
                )
            )
            self.assertFalse(
                any(
                    isinstance(n, (ast.Assign, ast.AnnAssign))
                    for n in tree.body
                    if not (
                        isinstance(n, ast.Assign)
                        and any(
                            isinstance(t, ast.Name) and t.id == "__all__"
                            for t in n.targets
                        )
                    )
                )
            )

    def test_every_frozen_table_has_a_schema_owner(self):
        directory = ROOT / "services/api/migrations"
        baseline = json.loads((directory / "snapshots/0001_schema.json").read_text())
        baseline += json.loads((directory / "snapshots/0004_workflows.json").read_text())
        baseline += json.loads((directory / "snapshots/0006_preparation.json").read_text())
        ownership = json.loads((directory / "ownership.json").read_text())
        self.assertEqual(
            {table["name"] for table in baseline}, set(ownership["tables"])
        )
        self.assertTrue(
            all(item["schema_owner"] == "api" for item in ownership["tables"].values())
        )

    def test_compose_python_builds_include_shared_package_and_migration_assets(self):
        compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
        for name, service in compose["services"].items():
            build = service.get("build")
            if not isinstance(build, dict) or not build.get(
                "dockerfile", ""
            ).startswith("services/"):
                continue
            self.assertEqual(build["context"], ".", name)
            dockerfile = ROOT / build["dockerfile"]
            content = dockerfile.read_text()
            self.assertIn("packages/ibess-common", content, name)
            for line in content.splitlines():
                if line.startswith("COPY ") and not line.startswith("COPY --"):
                    source = line.split()[1]
                    self.assertTrue(
                        (ROOT / source).exists(), f"Missing build input {source}"
                    )
        api = (ROOT / "services/api/Dockerfile").read_text()
        self.assertIn("COPY services/api/migrations", api)
        self.assertIn("COPY services/api/alembic.ini", api)
