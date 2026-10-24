import subprocess
import sys
from pathlib import Path


def test_live_smoke_requires_both_owner_gates_before_any_work():
    root = Path(__file__).parents[2]
    for arguments in ([], ["--approved-live"], ["--confirmed-free-credits"]):
        completed = subprocess.run(
            [sys.executable, "scripts/tavily_smoke.py", "--query", "Offline gate test", *arguments],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        assert completed.returncode == 2
        assert "no requests sent" in completed.stderr
