"""Run P2 offline checks and preserve literal outputs without overwriting P1 results."""

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
        ["-m", "pytest", "-q"],
        ["-m", "compileall", "app"],
        ["-m", "ruff", "check", "."],
        ["-m", "pip", "check"],
        ["scripts/offline_p2.py"],
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
        checks.append(
            {
                "command": [sys.executable, *arguments],
                "exit_code": completed.returncode,
                "elapsed_seconds": round(perf_counter() - clock, 3),
                "stdout": completed.stdout,
                "stderr": completed.stderr,
            }
        )
        print("python " + " ".join(arguments))
        print(completed.stdout, end="")
        if completed.stderr:
            print(completed.stderr, end="", file=sys.stderr)
    passed = all(check["exit_code"] == 0 for check in checks)
    result = {
        "phase": "P2",
        "started_at_utc": started,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "mode": "offline provider fixtures",
        "passed": passed,
        "checks": checks,
        "live_tavily_calls": 0,
        "live_mesh_calls": 0,
        "actual_api_cost": 0,
        "live_smoke_executed": False,
    }
    output = ROOT / "results/p2_checks.json"
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(f"P2 checks {'PASS' if passed else 'FAIL'}; saved {output}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
