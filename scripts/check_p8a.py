"""P8A offline release validation; never authenticates or calls Mesh/Tavily."""

import ast
import json
import platform
import re
import subprocess
import sys
from collections import Counter
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from time import perf_counter
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


def artifact_summary(directory):
    paired = json.loads((directory / "paired/comparison.json").read_text(encoding="utf-8"))
    performance = json.loads((directory / "performance.json").read_text(encoding="utf-8"))
    cases = json.loads(
        (ROOT / "tests/evaluation/datasets/claims.v1.json").read_text(encoding="utf-8")
    )["cases"]
    arms = {}
    for arm in ("baseline", "prooftrail"):
        arms[arm] = {
            **paired["aggregate_provisional_metrics"][arm],
            "missing_expected_subtopic_instances": sum(
                len(p[arm]["quality"]["provisional_synthetic_metrics"]["expected_subtopics"])
                - len(p[arm]["quality"]["provisional_synthetic_metrics"]["covered_subtopics"])
                for p in paired["pairs"]
            ),
            "verification_failures": sum(
                p[arm]["state"]["verification_failures"] for p in paired["pairs"]
            ),
            "stages": [
                {
                    "case_id": p["case_id"],
                    **{
                        k: p[arm]["state"][k]
                        for k in (
                            "research_duration_seconds", "verification_duration_seconds",
                            "synthesis_duration_seconds", "retry_duration_seconds",
                            "total_duration_seconds",
                        )
                    },
                }
                for p in paired["pairs"]
            ],
        }
    return {
        "review_status": dict(Counter(c["review_status"] for c in cases)),
        "class_balance": dict(Counter(c["expected_verdict"] for c in cases)),
        "splits": dict(Counter(c["dataset_split"] for c in cases)),
        "human_reviewed_accuracy": None,
        "live_model_accuracy": None,
        "same_initial_evidence_all_pairs": all(
            p["identical_initial_evidence"] for p in paired["pairs"]
        ),
        "benchmark_mode": paired["mode"],
        "benchmark": arms,
        "database_read_profile": {
            k: performance["read_profile_after"][k]
            for k in ("median_queries", "median_seconds_excluding_warmup", "runs")
        },
        "cache_hits": performance["retrieval_cache_profile"]["state"]["cache_hits"],
        "concurrency_samples": [
            {k: row[k] for k in (
                "configured_research_concurrency", "observed_peak_http_requests", "wall_seconds"
            )}
            for row in performance["existing_concurrency_profiles"]
        ],
    }


def main():
    if sys.version_info[:2] not in {(3, 11), (3, 12)}:
        raise SystemExit("Python 3.11 or 3.12 is required.")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:8]
    directory = ROOT / "results/p8a_runs" / stamp
    directory.mkdir(parents=True, exist_ok=False)
    checks = []

    def run(*args):
        print("python " + " ".join(map(str, args)), flush=True)
        started = perf_counter()
        result = subprocess.run(
            [sys.executable, *map(str, args)], cwd=ROOT,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            check=False, timeout=1800,
        )
        entry = {
            "command": [sys.executable, *map(str, args)], "exit_code": result.returncode,
            "elapsed_seconds": round(perf_counter() - started, 3),
            "stdout": result.stdout, "stderr": result.stderr,
        }
        checks.append(entry)
        (directory / f"check-{len(checks):02}.json").write_text(
            json.dumps(entry, indent=2) + "\n", encoding="utf-8"
        )
        print(result.stdout, end="", flush=True)
        if result.returncode:
            print(result.stderr[-4000:], file=sys.stderr, flush=True)
        return result.returncode == 0

    for args in (
        ("-m", "pytest", "-q"), ("-m", "compileall", "app"),
        ("-m", "ruff", "check", "."), ("-m", "pip", "check"),
        ("-m", "alembic", "upgrade", "head"), ("-m", "alembic", "heads"),
        ("-m", "alembic", "current"), ("-m", "alembic", "check"),
    ):
        run(*args)
    core_passed = all(c["exit_code"] == 0 for c in checks)
    semantic_passed = run(
        "-m", "scripts.p7_semantic", "--require-safe", "--output", directory / "semantic.json"
    ) if core_passed else False
    summary = None
    if semantic_passed:
        paired = run("-m", "scripts.p7_benchmark", "--run-directory", directory / "paired")
        reads = run("-m", "scripts.p7_profile_reads", "--label", "P8A-current", "--output",
                    directory / "reads.json")
        if paired and reads and run(
            "-m", "scripts.p7_performance", "--output", directory / "performance.json",
            "--before", ROOT / "results/p7_reads_before.json",
            "--after", directory / "reads.json",
            "--comparison", directory / "paired/comparison.json",
        ):
            summary = artifact_summary(directory)
    run("-m", "scripts.p8a_live_preflight", "--output", directory / "live_preflight.json")
    security_passed = run("-m", "scripts.p8a_security", "--output", directory / "security.json")
    compatibility = []
    for path in sorted((ROOT / "app").rglob("*.py")):
        try:
            ast.parse(path.read_text(encoding="utf-8"), feature_version=(3, 11))
        except SyntaxError:
            compatibility.append(str(path.relative_to(ROOT)))
    passed = all(c["exit_code"] == 0 for c in checks) and semantic_passed and summary is not None
    count = re.search(r"(\d+) passed", checks[0]["stdout"])
    gates = {
        "G1": "PASSED" if core_passed else "FAILED",
        "G2": "PASSED" if semantic_passed else "FAILED",
        "G3": "PENDING", "G4": "PENDING", "G5": "PENDING", "G6": "PENDING",
        "G7": "PASSED" if summary and summary["same_initial_evidence_all_pairs"] else "FAILED",
        "G8": "PASSED" if security_passed else "FAILED",
        "G9": "PENDING", "G10": "PENDING",
    }
    result = {
        "phase": "P8A", "mode": "OFFLINE_RELEASE_VALIDATION",
        "python": platform.python_version(), "python_executable": sys.executable,
        "platform": platform.platform(), "raw_run_directory": str(directory.relative_to(ROOT)),
        "checks": checks, "all_offline_checks_passed": passed,
        "tests_passed": int(count[1]) if count else None,
        "semantic_probes_passed": semantic_passed, "measurements": summary,
        "organizer_workflow": {
            "sha256": sha256(
                (ROOT / ".github/workflows/agent-arena-2026-checks.yml").read_bytes()
            ).hexdigest(),
            "runtime_python": "3.11", "supported_project_pythons": ["3.11", "3.12"],
            "python311_ast_probe_failed_files": compatibility,
            "compatibility_status": (
                "FAILED_LOCAL_SYNTAX_PROBE" if compatibility else "PASSED_LOCAL_SYNTAX_PROBE"
            ),
            "github_actions_run_verified": False, "executed": False,
            "side_effect": "On push, downloads organizer checks and reports to organizer",
        },
        "release_gates": gates,
        "gate_scopes": {
            "G2": "Known offline semantic bypass regressions pass; not general accuracy",
            "G8": "No detected credential patterns in candidate files; scan limitations apply",
        },
        "ready_for_submission": False, "live_mesh_requests": 0, "live_tavily_requests": 0,
        "actual_api_cost_usd": 0, "authorized_api_spend_usd": 0,
        "clean_install": {
            "environment": Path(sys.prefix).name,
            "installation_command": "python -m pip install -c requirements.lock -e '.[dev]'",
            "installation_log": "results/p8a_sources/clean_install_output.json"
            if (ROOT / "results/p8a_sources/clean_install_output.json").exists() else None,
            "note": "Local fresh venv, not a GitHub-hosted runner measurement",
        },
    }
    target = ROOT / "results/p8a_checks.json"
    if target.exists():
        archive = ROOT / "results/p8a_history" / (sha256(target.read_bytes()).hexdigest() + ".json")
        archive.parent.mkdir(parents=True, exist_ok=True)
        if not archive.exists():
            archive.write_bytes(target.read_bytes())
    target.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(f"P8A offline checks {'PASS' if passed else 'FAIL'}; submission readiness PENDING.")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
