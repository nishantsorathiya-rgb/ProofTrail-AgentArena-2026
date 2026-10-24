"""Prepare ignored local configuration; accept a key only in a private, masked terminal."""

import argparse
import getpass
import re
import subprocess
import sys
import warnings
from pathlib import Path

from dotenv import set_key

ROOT = Path(__file__).resolve().parents[1]
LOCAL_SETTINGS = {
    "MESH_BASE_URL": "https://api.meshapi.ai/v1",
    "MESH_MODEL_ID": "openai/gpt-4.1-mini",
    "ENABLE_LIVE_REQUESTS": "false",
    "MAX_LLM_CALLS_PER_RUN": "8",
    "MAX_SEARCHES_PER_RUN": "2",
    "MAX_FETCHES_PER_RUN": "2",
    "MAX_CLAIMS_PER_RUN": "1",
    "MAX_RESEARCH_SUBQUESTIONS": "1",
    "MAX_RESEARCH_TASKS_PER_RUN": "2",
    "MAX_RETRIES_PER_SUBQUESTION": "1",
    "MAX_PARALLEL_FETCHES": "1",
    "MAX_PARALLEL_RESEARCH_TASKS": "1",
    "MAX_PARALLEL_VERIFICATIONS": "1",
    "MAX_ACTIVE_RUNS": "1",
    "MAX_PENDING_RUNS": "0",
    "HTTP_TIMEOUT_SECONDS": "12",
    "TAVILY_MAX_ATTEMPTS": "1",
}


def require_private_env(root: Path) -> Path:
    target = root / ".env"
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise ValueError("Unsafe .env path; no changes made.")
    ignored = subprocess.run(
        ["git", "check-ignore", "-q", ".env"], cwd=root, capture_output=True, check=False,
    )
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", ".env"], cwd=root,
        capture_output=True, check=False,
    )
    if ignored.returncode != 0 or tracked.returncode != 1:
        raise ValueError(".env must be Git-ignored and untracked before private configuration.")
    return target


def prepare(root: Path = ROOT) -> None:
    target = require_private_env(root)
    if not target.exists():
        target.write_text(
            "# Private local ProofTrail configuration. Never commit.\n", encoding="utf-8",
        )
    for name, value in LOCAL_SETTINGS.items():
        set_key(target, name, value, quote_mode="always")


def enter_key(root: Path = ROOT) -> None:
    target = require_private_env(root)
    if not sys.stdin.isatty():
        raise ValueError("Run key entry yourself in an interactive local terminal.")
    with warnings.catch_warnings():
        # Fail instead of falling back to visible input on an unsupported terminal.
        warnings.simplefilter("error", getpass.GetPassWarning)
        key = getpass.getpass("Mesh inference key (hidden; never paste into chat): ")
    if not re.fullmatch(r"rsk_[A-Za-z0-9_-]{16,512}", key):
        raise ValueError("Expected a Mesh inference key; no key was saved.")
    set_key(target, "ENABLE_LIVE_REQUESTS", "false", quote_mode="always")
    set_key(target, "MESH_API_KEY", key, quote_mode="always")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--prepare", action="store_true")
    action.add_argument("--enter-key", action="store_true")
    args = parser.parse_args()
    try:
        if args.prepare:
            prepare()
        else:
            enter_key()
    except (ValueError, OSError, getpass.GetPassWarning, EOFError):
        print("Private configuration failed; no credentials are displayed. See the setup guide.")
        return 2
    print("Private configuration saved locally; no API requests made. Live mode remains opt-in.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
