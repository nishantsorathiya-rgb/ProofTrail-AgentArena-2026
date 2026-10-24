"""Score explicit owner citation reviews. No provider requests or automatic review labels."""

import argparse
import json
from pathlib import Path

from app.evaluation.citation_review import score_owner_citations


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evaluation", type=Path, required=True)
    parser.add_argument("--worksheet", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = json.loads(args.evaluation.read_text(encoding="utf-8"))
    predictions = result["predictions"] + result.get("adversarial_predictions", [])
    scored = score_owner_citations(predictions, args.worksheet)
    with args.output.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(scored, indent=2) + "\n")


if __name__ == "__main__":
    main()
