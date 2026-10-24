"""Local candidate-file credential scan. Never prints matching credential values."""

import argparse
import json
import re
import subprocess
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = {
    "mesh_key": re.compile(r"\brsk_[A-Za-z0-9_-]{16,}"),
    "tavily_key": re.compile(r"\btvly-[A-Za-z0-9_-]{16,}"),
    "provider_key": re.compile(r"\bsk-[A-Za-z0-9_-]{24,}"),
    "github_token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})"),
    "aws_access_key": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "literal_credential": re.compile(
        r"(?i)(?:MESH_API_KEY|TAVILY_API_KEY|SUBMISSION_TOKEN)\s*[=:]\s*"
        r"[\"']([A-Za-z0-9_/-]{16,})[\"']"
    ),
}


def scan_text(text):
    findings = []
    for number, line in enumerate(text.splitlines(), 1):
        for rule, pattern in PATTERNS.items():
            for match in pattern.finditer(line):
                value = match.group(1) if rule == "literal_credential" else match.group()
                if value == "fake-private-key":  # Exact existing serialization-test sentinel.
                    continue
                # Explicit fixture prefixes, not an arbitrary substring exemption.
                if value.casefold().startswith(
                    ("your_", "rsk_offline", "tvly-offline", "sk-offline", "offline_",
                     "offline-", "model_id_")
                ):
                    continue
                findings.append({"line": number, "rule": rule})
    return findings


def scan(root=ROOT):
    candidates = subprocess.check_output(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=root
    ).decode().split("\0")
    tracked = subprocess.check_output(["git", "ls-files", "-z", "--cached"], cwd=root)
    findings, inspected, errors = [], 0, []
    for relative in sorted(set(filter(None, candidates))):
        path = root / relative
        if not path.is_file():
            continue
        try:
            if path.suffix == ".docx":
                with zipfile.ZipFile(path) as archive:
                    content = "\n".join(
                        archive.read(n).decode("utf-8", errors="replace")
                        for n in archive.namelist()
                        if n.endswith(".xml")
                    )
            else:
                content = path.read_bytes().decode("utf-8", errors="replace")
            inspected += 1
            findings.extend({"path": relative, **f} for f in scan_text(content))
        except (OSError, zipfile.BadZipFile) as error:
            errors.append({"path": relative, "error_type": type(error).__name__})
    ignored_probe = subprocess.run(
        ["git", "check-ignore", "--stdin"],
        cwd=root,
        input=b".env\n.env.production\ndata/probe.db\n.venv-p8a/probe\nprivate.key\n",
        capture_output=True,
        check=False,
    )
    return {
        "mode": "LOCAL_PATTERN_SCAN_NOT_A_COMPREHENSIVE_SECURITY_AUDIT",
        "tracked_files": len(list(filter(None, tracked.decode().split("\0")))),
        "candidate_files_inspected": inspected,
        "findings": findings,
        "errors": errors,
        "ignore_probes": ignored_probe.stdout.decode().splitlines(),
        "passed": not findings and not errors and len(ignored_probe.stdout.splitlines()) == 5,
        "limitations": [
            "Pattern-based scanning cannot prove absence of arbitrary or encoded secrets",
            "Ignored environments/databases/private files are outside publication candidates",
            "History is not scanned; rescan staged files before the owner's local checkpoint",
            "Dependency consistency is separate; no vulnerability-database audit is claimed",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = scan()
    with args.output.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(result, indent=2) + "\n")
    print(f"Credential scan: {result['candidate_files_inspected']} files, "
          f"{len(result['findings'])} findings, {len(result['errors'])} scan errors.")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
