"""Run real P1 checks and save their literal output; no external API calls."""

import json
import platform
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    if sys.version_info[:2] not in {(3, 11), (3, 12)}:
        raise SystemExit("Use a project Python 3.11 or 3.12 virtual environment.")
    started = datetime.now(UTC).isoformat()
    checks = []
    for arguments in (
        ["-m", "compileall", "app"],
        ["-m", "pytest", "-q"],
        ["-m", "ruff", "check", "."],
        ["-m", "pip", "check"],
    ):
        clock = perf_counter()
        completed = subprocess.run(
            [sys.executable, *arguments],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=120,
        )
        record = {
            "command": [sys.executable, *arguments],
            "exit_code": completed.returncode,
            "elapsed_seconds": round(perf_counter() - clock, 3),
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }
        checks.append(record)
        print("python " + " ".join(arguments))
        print(completed.stdout, end="")
        if completed.stderr:
            print(completed.stderr, end="", file=sys.stderr)
    passed = all(check["exit_code"] == 0 for check in checks)
    result = {
        "phase": "P1",
        "started_at_utc": started,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "mode": "offline",
        "live_mesh_calls": 0,
        "live_tavily_calls": 0,
        "api_cost": 0,
        "live_model_tokens": None,
        "passed": passed,
        "checks": checks,
    }
    destination = ROOT / "results"
    destination.mkdir(exist_ok=True)
    (destination / "p1_checks.json").write_text(
        json.dumps(result, indent=2) + "\n",
        encoding="utf-8",
    )
    snapshot = subprocess.run(
        [sys.executable, "-m", "pip", "freeze", "--exclude-editable"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    (destination / "p1_environment.txt").write_text(snapshot.stdout, encoding="utf-8")
    print(f"P1 checks: {'PASS' if passed else 'FAIL'}; saved results/p1_checks.json")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
