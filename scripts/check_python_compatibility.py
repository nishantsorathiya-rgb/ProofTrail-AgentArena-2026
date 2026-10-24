"""Run the existing offline suite and schema checks in the selected 3.11/3.12 environment."""

import argparse
import ast
import importlib.metadata
import json
import os
import platform
import re
import subprocess
import sys
import tomllib
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet

ROOT = Path(__file__).resolve().parents[1]
ORGANIZER_SHA256 = "f34c4aba59caa82bd992a3fb77b51c0c94d3d2c3551acb55f72ddaf4e6e91436"


def dependency_metadata():
    manifest = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    requirements = [
        line for line in (ROOT / "requirements.lock").read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    ] + manifest["build-system"]["requires"]
    records = []
    for value in requirements:
        requirement = Requirement(value)
        try:
            package = importlib.metadata.distribution(requirement.name)
            constraint = package.metadata.get("Requires-Python")
            records.append({
                "name": requirement.name,
                "pinned_requirement": value,
                "installed_version": package.version,
                "requires_python": constraint,
                "matches_pin": requirement.specifier.contains(package.version),
                "declares_python311": SpecifierSet(constraint or "").contains("3.11"),
                "declares_python312": SpecifierSet(constraint or "").contains("3.12"),
            })
        except importlib.metadata.PackageNotFoundError:
            records.append({"name": requirement.name, "error": "not_installed"})
    return records


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if sys.version_info[:2] not in {(3, 11), (3, 12)}:
        parser.exit(2, "Use a clean Python 3.11 or 3.12 environment.\n")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:8]
    directory = ROOT / "results/python_compatibility_runs" / stamp
    directory.mkdir(parents=True, exist_ok=False)
    environment = os.environ.copy()
    for name in ("MESH_API_KEY", "TAVILY_API_KEY", "SUBMISSION_TOKEN"):
        environment.pop(name, None)
    environment["ENABLE_LIVE_REQUESTS"] = "false"
    environment["DATABASE_URL"] = f"sqlite:///{(directory / 'schema.db').as_posix()}"
    environment["PYTHONIOENCODING"] = "utf-8"
    checks = []

    def run(*arguments):
        print("python " + " ".join(map(str, arguments)), flush=True)
        start = perf_counter()
        try:
            process = subprocess.run(
                [sys.executable, *map(str, arguments)], cwd=ROOT, env=environment,
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=1800, check=False,
            )
        except subprocess.TimeoutExpired as error:
            def decoded(value):
                return (
                    value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value
                )
            process = subprocess.CompletedProcess(
                arguments, 124, decoded(error.stdout) or "",
                (decoded(error.stderr) or "") + "\nValidation command timed out.\n",
            )
        entry = {
            "command": [sys.executable, *map(str, arguments)],
            "exit_code": process.returncode,
            "elapsed_seconds": round(perf_counter() - start, 3),
            "stdout": process.stdout, "stderr": process.stderr,
        }
        checks.append(entry)
        (directory / f"check-{len(checks):02}.json").write_text(
            json.dumps(entry, indent=2) + "\n", encoding="utf-8"
        )
        print(process.stdout[-3000:], end="", flush=True)
        if process.returncode:
            print(process.stderr[-3000:], file=sys.stderr, flush=True)

    paths = sorted(p for folder in ("app", "scripts", "tests")
                   for p in (ROOT / folder).rglob("*.py"))
    syntax_failures = []
    for path in paths:
        try:
            ast.parse(path.read_text(encoding="utf-8"), feature_version=(3, 11))
        except SyntaxError as error:
            syntax_failures.append({
                "path": str(path.relative_to(ROOT)), "line": error.lineno, "message": error.msg,
            })
    for arguments in (
        ("-m", "pytest", "--collect-only", "-q"), ("-m", "pytest", "-q"),
        ("-m", "compileall", "app"), ("-m", "compileall", "scripts", "tests"),
        ("-m", "ruff", "check", "."), ("-m", "pip", "check"),
        ("-m", "alembic", "upgrade", "head"), ("-m", "alembic", "heads"),
        ("-m", "alembic", "current"), ("-m", "alembic", "check"),
    ):
        run(*arguments)
    collection = sorted(line for line in checks[0]["stdout"].splitlines() if "::" in line)
    baseline = ROOT / "results/p8a_sources/python_compatibility_baseline_collection.txt"
    baseline_ids = sorted(line for line in baseline.read_text(encoding="utf-8-sig").splitlines()
                          if "::" in line) if baseline.exists() else None
    pins = dependency_metadata()
    count = re.search(r"(\d+) passed", checks[1]["stdout"])
    tests_passed = int(count[1]) if count else None
    metadata_passed = all(
        r.get("matches_pin") and r.get("declares_python311") and r.get("declares_python312")
        for r in pins
    )
    workflow = ROOT / ".github/workflows/agent-arena-2026-checks.yml"
    workflow_hash = sha256(workflow.read_bytes()).hexdigest()
    passed = (
        all(c["exit_code"] == 0 for c in checks) and not syntax_failures
        and metadata_passed and tests_passed == 317 and len(collection) == 317
        and (baseline_ids is None or collection == baseline_ids)
        and workflow_hash == ORGANIZER_SHA256
    )
    result = {
        "phase": "P8A.1", "mode": "OFFLINE_PROVIDER_TESTS",
        "python": platform.python_version(), "python_executable": sys.executable,
        "platform": platform.platform(), "raw_run_directory": str(directory.relative_to(ROOT)),
        "checks": checks, "passed": passed, "tests_passed": tests_passed,
        "collected_tests": len(collection),
        "original_test_node_ids_unchanged": collection == baseline_ids if baseline_ids else None,
        "python311_syntax_scan": {"files": len(paths), "failures": syntax_failures},
        "dependency_metadata": pins, "dependency_compatibility_passed": metadata_passed,
        "organizer_workflow_sha256": workflow_hash,
        "organizer_workflow_unchanged": workflow_hash == ORGANIZER_SHA256,
        "isolated_migration_database": str(directory / "schema.db"),
        "live_mesh_requests": 0, "live_tavily_requests": 0, "actual_api_cost_usd": 0,
        "github_actions_verified": False, "release_ready": False,
    }
    output = args.output if args.output.is_absolute() else ROOT / args.output
    if output.exists():
        archive = output.parent / "python_compatibility_history" / (
            output.stem + "-" + sha256(output.read_bytes()).hexdigest() + ".json"
        )
        archive.parent.mkdir(parents=True, exist_ok=True)
        if not archive.exists():
            archive.write_bytes(output.read_bytes())
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(f"Python {platform.python_version()}: {'PASS' if passed else 'FAIL'}; "
          f"{tests_passed} tests passed; live provider calls 0.")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
