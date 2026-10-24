"""Capture actual required P4 command outputs; all provider work is offline."""

import json
import platform
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]


def main():
    if sys.version_info[:2] not in {(3, 11), (3, 12)}:
        raise SystemExit("Use a project Python 3.11 or 3.12 virtual environment.")
    checks = []
    started = datetime.now(UTC).isoformat()
    for arguments in (
        ["-m", "pytest", "-q"],
        ["-m", "compileall", "app"],
        ["-m", "ruff", "check", "."],
        ["-m", "pip", "check"],
        ["-m", "alembic", "upgrade", "head"],
        ["-m", "alembic", "current"],
        ["-m", "alembic", "check"],
        ["-m", "scripts.offline_p4"],
    ):
        print("python " + " ".join(arguments), flush=True)
        clock = perf_counter()
        try:
            result = subprocess.run(
                [sys.executable, *arguments],
                cwd=ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=240,
            )
        except subprocess.TimeoutExpired as error:

            def decoded(value):
                return (
                    value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value
                )

            result = subprocess.CompletedProcess(
                [sys.executable, *arguments],
                124,
                stdout=decoded(error.stdout) or "",
                stderr=(decoded(error.stderr) or "") + "\nValidation command timed out.\n",
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
        print(result.stdout, end="", flush=True)
        if result.stderr:
            print(result.stderr, end="", file=sys.stderr, flush=True)
    passed = all(c["exit_code"] == 0 for c in checks)
    match = re.search(r"(\d+) passed", checks[0]["stdout"])
    output = ROOT / "results/p4_checks.json"
    output.write_text(
        json.dumps(
            {
                "phase": "P4",
                "started_at_utc": started,
                "python": platform.python_version(),
                "platform": platform.platform(),
                "mode": "offline fixtures",
                "passed": passed,
                "tests_passed": int(match[1]) if match else None,
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
    print(f"P4 checks {'PASS' if passed else 'FAIL'}; saved {output}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
