"""Execute required P3 checks and save their actual outputs, including migration checks."""

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
        ["-m", "alembic", "upgrade", "head"],
        ["-m", "alembic", "current"],
        ["-m", "alembic", "check"],
        ["scripts/offline_p3.py"],
    ):
        clock = perf_counter()
        result = subprocess.run(
            [sys.executable, *arguments],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=180,
        )
        checks.append(
            {
                "command": [sys.executable, *arguments],
                "exit_code": result.returncode,
                "elapsed_seconds": round(perf_counter() - clock, 3),
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
        )
        print("python " + " ".join(arguments), flush=True)
        print(result.stdout, end="", flush=True)
        if result.stderr:
            print(result.stderr, end="", file=sys.stderr, flush=True)
    passed = all(check["exit_code"] == 0 for check in checks)
    output = ROOT / "results/p3_checks.json"
    output.parent.mkdir(exist_ok=True)
    output.write_text(
        json.dumps(
            {
                "phase": "P3",
                "started_at_utc": started,
                "python": platform.python_version(),
                "platform": platform.platform(),
                "mode": "offline fixtures",
                "passed": passed,
                "checks": checks,
                "live_mesh_calls": 0,
                "live_tavily_calls": 0,
                "actual_api_cost": 0,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"P3 checks {'PASS' if passed else 'FAIL'}; saved {output}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
