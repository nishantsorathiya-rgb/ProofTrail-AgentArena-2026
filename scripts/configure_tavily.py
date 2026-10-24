"""Private Tavily key entry and local checks only; this module makes no API requests."""

import argparse
import getpass
import json
import re
import sys
import warnings
from pathlib import Path

from dotenv import dotenv_values, set_key

from scripts.configure_mesh import require_private_env

ROOT = Path(__file__).resolve().parents[1]


def check(root: Path = ROOT) -> dict:
    target = require_private_env(root)
    values = dotenv_values(target) if target.exists() else {}
    key = values.get("TAVILY_API_KEY") or ""
    return {
        "mode": "LOCAL_ONLY_NO_API_REQUESTS",
        "key_configured_with_plausible_format": bool(
            re.fullmatch(r"tvly-[A-Za-z0-9_-]{16,512}", key)
        ),
        "application_live_enabled": str(values.get("ENABLE_LIVE_REQUESTS", "false")).lower()
        in {"true", "1", "yes", "on"},
        "credentials_authenticated": False,
        "account_credits_checked": False,
        "api_requests": 0,
    }


def enter_key(root: Path = ROOT) -> None:
    target = require_private_env(root)
    if not sys.stdin.isatty():
        raise ValueError("Use a private interactive terminal.")
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        key = getpass.getpass("Tavily API key (hidden; never paste into chat): ")
    if not re.fullmatch(r"tvly-[A-Za-z0-9_-]{16,512}", key):
        raise ValueError("Invalid key format; no changes made.")
    set_key(target, "ENABLE_LIVE_REQUESTS", "false", quote_mode="always")
    set_key(target, "TAVILY_API_KEY", key, quote_mode="always")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--enter-key", action="store_true")
    action.add_argument("--check", action="store_true")
    args = parser.parse_args()
    try:
        if args.check:
            print(json.dumps(check(), indent=2))
        else:
            enter_key()
            print("Tavily key saved privately. No API requests made; live mode remains off.")
    except (ValueError, OSError, getpass.GetPassWarning, EOFError):
        print("Private configuration failed; no credentials displayed. See the P8 setup plan.")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
