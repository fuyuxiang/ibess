"""One quality entry point; each service/API test class runs in an isolated interpreter."""

import argparse
import ast
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SERVICES = (
    "event-service",
    "vision-worker",
    "audio-worker",
    "semantic-worker",
    "media-service",
    "offline-worker",
)
DATABASE_KEYS = (
    "WORKFLOW_TEST_DATABASE_URL",
    "ZONE_TEST_DATABASE_URL",
    "PHASE_TEST_DATABASE_URL",
    "STAGE_ASSETS_TEST_DATABASE_URL",
    "MEDIA_BINDINGS_TEST_DATABASE_URL",
    "OFFLINE_QA_TEST_DATABASE_URL",
    "SEMANTIC_TEST_DATABASE_URL",
    "EVENT_TEST_DATABASE_URL",
    "OFFLINE_TEST_DATABASE_URL",
)


def api_suites():
    # Historical tests reuse the application metadata and class lifecycle helpers.
    # Separate processes prevent one class from dropping another class's schema.
    for file in sorted((ROOT / "services/api/tests").glob("test_*.py")):
        for node in ast.parse(file.read_text()).body:
            if not isinstance(node, ast.ClassDef):
                continue
            methods = [
                m.name
                for m in node.body
                if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
                and m.name.startswith("test_")
            ]
            if methods:
                selector = file.relative_to(ROOT).as_posix() + "::" + node.name
                # Base suites run separately; child fixtures can intentionally differ.
                yield selector, [selector + "::" + method for method in methods]


def plan(profile):
    jobs = []
    if profile in ("all", "lint"):
        jobs.append(("lint", [sys.executable, "-m", "ruff", "check", "."], ROOT, {}))
    if profile in ("all", "unit"):
        jobs.append(
            (
                "contracts-and-architecture",
                [sys.executable, "-m", "pytest", "-q", "tests"],
                ROOT,
                {},
            )
        )
        for service in SERVICES:
            jobs.append(
                (
                    service,
                    [sys.executable, "-m", "pytest", "-q", "tests"],
                    ROOT / "services" / service,
                    {},
                )
            )
        jobs.append(
            (
                "scripts",
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "-q",
                    "scripts",
                    "deploy/integration/test_integration_deploy.py",
                ],
                ROOT,
                {"PYTHONPATH": str(ROOT / "scripts")},
            )
        )
        jobs.append(
            (
                "api-runtime-recovery",
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "-q",
                    "tests/test_service_recovery.py",
                ],
                ROOT / "services/api",
                {},
            )
        )
    if profile in ("all", "integration"):
        url = os.environ.get("IBESS_TEST_DATABASE_URL") or os.environ.get(
            "WORKFLOW_TEST_DATABASE_URL"
        )
        if not url:
            raise ValueError(
                "Integration profile requires explicit IBESS_TEST_DATABASE_URL pointing to a dedicated PostgreSQL database"
            )
        redis_url = os.environ.get("IBESS_TEST_REDIS_URL") or os.environ.get(
            "PHASE_TEST_REDIS_URL"
        )
        if not redis_url:
            raise ValueError(
                "Integration profile requires explicit IBESS_TEST_REDIS_URL pointing to a dedicated Redis instance"
            )
        database = {
            **{key: url for key in DATABASE_KEYS},
            "PHASE_TEST_REDIS_URL": redis_url,
        }
        for selector, methods in api_suites():
            jobs.append(
                (
                    selector.split("/")[-1],
                    [sys.executable, "-m", "pytest", "-q", *methods],
                    ROOT,
                    {**database, "PYTHONPATH": str(ROOT / "services/api")},
                )
            )
        for service in ("audio-worker", "event-service", "offline-worker"):
            jobs.append(
                (
                    service + "-postgresql",
                    [sys.executable, "-m", "pytest", "-q", "tests"],
                    ROOT / "services" / service,
                    database,
                )
            )
    if profile in ("all", "web"):
        for action in ("test", "build"):
            jobs.append(
                ("web-" + action, ["npm", "run", action], ROOT / "web-console", {})
            )
    if profile in ("all", "compose"):
        env = {
            key: os.environ.get(key) or "quality-check-only"
            for key in ("POSTGRES_PASSWORD", "MINIO_ROOT_PASSWORD")
        }
        for suffix, overlays in (
            ("base", []),
            ("linux", ["deploy/linux/docker-compose.linux.yml"]),
            ("optional", ["deploy/integration/docker-compose.a.yml"]),
        ):
            command = ["docker", "compose", "-f", "docker-compose.yml"]
            for overlay in overlays:
                command.extend(["-f", overlay])
            jobs.append(
                ("compose-" + suffix, command + ["config", "--quiet"], ROOT, env)
            )
    return jobs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "profile", choices=("lint", "unit", "integration", "web", "compose", "all")
    )
    parser.add_argument(
        "--report", type=Path, default=ROOT / "tmp/reports/quality.json"
    )
    parser.add_argument(
        "--suite",
        action="append",
        default=[],
        help="Run only suites whose names contain this value",
    )
    args = parser.parse_args()
    try:
        jobs = plan(args.profile)
        if args.suite:
            jobs = [job for job in jobs if any(name in job[0] for name in args.suite)]
            if not jobs:
                raise ValueError("No suites matched the requested selection")
    except ValueError as exc:
        parser.error(str(exc))
    results = []
    with tempfile.TemporaryDirectory(prefix="ibess-quality-") as temporary:
        for name, command, cwd, overrides in jobs:
            env = {
                **os.environ,
                **{key: "" for key in DATABASE_KEYS},
                "PYTHONPATH": os.pathsep.join([str(cwd), str(ROOT / "services/api")])
                if cwd.name == "audio-worker"
                else str(cwd),
                "DATABASE_URL": "sqlite:///" + str(Path(temporary) / "isolated.db"),
                "CORE_BEHAVIORS_ENABLED": "0",
                "LATEST_FRAME_CAPTURE": "0",
                "FACE_MODEL_DIR": str(ROOT / "models/face"),
                **overrides,
            }
            print(f"Checking {name} ...", flush=True)
            process = subprocess.run(
                command, cwd=cwd, env=env, text=True, capture_output=True
            )
            output = process.stdout + process.stderr
            passed = re.search(r"(\d+) passed", output) or re.search(
                r"# pass (\d+)", output
            )
            skipped = re.search(r"(\d+) skipped", output)
            row = {
                "suite": name,
                "status": "passed" if process.returncode == 0 else "failed",
                "exit_code": process.returncode,
                "passed": int(passed[1]) if passed else 0,
                "skipped": int(skipped[1]) if skipped else 0,
            }
            results.append(row)
            print(
                f"{name}: {row['status']}"
                + (
                    f" ({row['passed']} tests, {row['skipped']} skipped)"
                    if passed
                    else ""
                ),
                flush=True,
            )
            if process.returncode:
                print(output[-14000:], flush=True)
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "profile": args.profile,
        "selection": args.suite,
        "status": "passed"
        if all(r["status"] == "passed" for r in results)
        else "failed",
        "suites": results,
        "passed": sum(r["passed"] for r in results),
        "skipped": sum(r["skipped"] for r in results),
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(f"Report: {args.report}", flush=True)
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
