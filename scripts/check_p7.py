"""Run required checks first, then semantic release probes; benchmark only after all pass."""

import json
import platform
import re
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from time import perf_counter
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


def publish_versioned(target, result):
    """Preserve the previous canonical artifact before publishing another measured session."""
    if target.exists():
        archive = target.parent / "p7_history" / target.stem
        archive.mkdir(parents=True, exist_ok=True)
        snapshot = archive / (sha256(target.read_bytes()).hexdigest() + target.suffix)
        if not snapshot.exists():
            shutil.copy2(target, snapshot)
    target.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")


def main():
    if sys.version_info[:2] not in {(3, 11), (3, 12)}:
        raise SystemExit("Use a project Python 3.11 or 3.12 virtual environment.")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:8]
    directory = ROOT / "results/p7_runs" / stamp
    directory.mkdir(parents=True, exist_ok=False)
    checks = []

    def run(arguments):
        print("python " + " ".join(map(str, arguments)), flush=True)
        clock = perf_counter()
        try:
            result = subprocess.run(
                [sys.executable, *map(str, arguments)],
                cwd=ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=1500,
            )
        except subprocess.TimeoutExpired as error:

            def decoded(value):
                return (
                    value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value
                )

            result = subprocess.CompletedProcess(
                arguments,
                124,
                stdout=decoded(error.stdout) or "",
                stderr=(decoded(error.stderr) or "") + "\nValidation command timed out.\n",
            )
        entry = {
            "command": [sys.executable, *map(str, arguments)],
            "exit_code": result.returncode,
            "elapsed_seconds": round(perf_counter() - clock, 3),
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
        checks.append(entry)
        (directory / f"check-{len(checks):02}.json").write_text(
            json.dumps(entry, indent=2), encoding="utf-8"
        )
        print(result.stdout, end="", flush=True)
        # Full safe provider logs are preserved in the raw command JSON, avoid flooding the UI.
        if result.returncode and result.stderr:
            print(result.stderr[-5000:], end="", file=sys.stderr, flush=True)
        return result.returncode == 0

    for arguments in (
        ["-m", "pytest", "-q"],
        ["-m", "compileall", "app"],
        ["-m", "ruff", "check", "."],
        ["-m", "pip", "check"],
        ["-m", "alembic", "upgrade", "head"],
        ["-m", "alembic", "heads"],
        ["-m", "alembic", "current"],
        ["-m", "alembic", "check"],
    ):
        run(arguments)
    core_passed = all(c["exit_code"] == 0 for c in checks)
    semantic_passed = False
    if core_passed:
        semantic_passed = run(
            ["-m", "scripts.p7_semantic", "--require-safe", "--output", directory / "semantic.json"]
        )
    if semantic_passed:
        # The P0 safety gate always precedes these noncritical measurements.
        paired_passed = run(["-m", "scripts.p7_benchmark", "--run-directory", directory / "paired"])
        reads_passed = run(
            [
                "-m",
                "scripts.p7_profile_reads",
                "--label",
                "post-semantic-fix",
                "--output",
                directory / "reads_after.json",
            ]
        )
        if paired_passed and reads_passed:
            run(
                [
                    "-m",
                    "scripts.p7_performance",
                    "--output",
                    directory / "performance.json",
                    "--before",
                    ROOT / "results/p7_reads_before.json",
                    "--after",
                    directory / "reads_after.json",
                    "--comparison",
                    directory / "paired/comparison.json",
                ]
            )
            comparison = json.loads(
                (directory / "paired/comparison.json").read_text(encoding="utf-8")
            )
            comparison["raw_run_directory"] = str(directory.relative_to(ROOT))
            publish_versioned(ROOT / "results/p7_baseline_comparison.json", comparison)
            if (directory / "performance.json").exists():
                performance = json.loads(
                    (directory / "performance.json").read_text(encoding="utf-8")
                )
                performance["raw_run_directory"] = str(directory.relative_to(ROOT))
                publish_versioned(ROOT / "results/p7_performance.json", performance)
    run(["-m", "scripts.p7_readiness", "--output", directory / "readiness.json"])
    passed = all(c["exit_code"] == 0 for c in checks) and semantic_passed
    match = re.search(r"(\d+) passed", checks[0]["stdout"])
    result = {
        "phase": "P7",
        "python": platform.python_version(),
        "platform": platform.platform(),
        "mode": "OFFLINE_OWNER_REQUEST",
        "owner_live_decision": "Keep P7 offline; no provider requests",
        "raw_run_directory": str(directory.relative_to(ROOT)),
        "checks": checks,
        "passed": passed,
        "tests_passed": int(match[1]) if match else None,
        "known_semantic_release_probes_passed": semantic_passed,
        "production_release_ready": False,
        "remaining_release_requirements": [
            "Independent human-reviewed semantic evaluation",
            "Approved live Mesh/Tavily compatibility and semantic validation",
            "Owner review; correlated semantic errors remain possible with the same model",
        ],
        "live_mesh_requests": 0,
        "live_tavily_requests": 0,
        "actual_api_cost_usd": 0,
    }
    publish_versioned(ROOT / "results/p7_checks.json", result)
    print(
        f"P7 offline checks {'PASS' if passed else 'FAIL'}; release readiness is a separate gate."
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
