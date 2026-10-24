"""Independent semantic review of *specific predicted quotes*, separate from case labels."""

import csv
import json
from pathlib import Path


def worksheet_rows(predictions):
    rows = []
    for row in predictions:
        for citation in row["proposed_citations"]:
            rows.append(
                {
                    "case_id": row["case_id"],
                    "attempt_id": row["prediction"]["attempt_id"],
                    "evidence_id": citation["evidence_id"],
                    "claim_text": row["claim_text"],
                    "predicted_verdict": row["prediction"]["verdict"],
                    "exact_proposed_quotes": json.dumps(
                        citation["proposed_quotes"], ensure_ascii=False
                    ),
                    "full_evidence_context": row["evidence_text"],
                    "structurally_valid": str(citation["structurally_valid"]),
                    "semantic_judgment": "UNREVIEWED",
                    "review_status": "UNREVIEWED",
                    "reviewer": "",
                    "reviewer_notes": "",
                    "exclusion_reason": "",
                }
            )
    return rows


def export_worksheet(predictions, path: Path):
    ensure_replaceable(path)
    rows = worksheet_rows(predictions)
    if not rows:
        return
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def ensure_replaceable(path: Path):
    """Regenerate untouched output templates, but preserve every owner annotation."""
    if not path.exists():
        return
    with path.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    if any(
        row.get("review_status") != "UNREVIEWED"
        or row.get("semantic_judgment") != "UNREVIEWED"
        or any(row.get(key) for key in ("reviewer", "reviewer_notes", "exclusion_reason"))
        for row in rows
    ):
        raise ValueError("Citation worksheet has owner edits; choose a new output worksheet path.")


def score_owner_citations(predictions, path: Path):
    baseline = worksheet_rows(predictions)

    def identity(row):
        return row["case_id"], row["attempt_id"], row["evidence_id"]

    known = {identity(r): r for r in baseline}
    editable = {
        "semantic_judgment",
        "review_status",
        "reviewer",
        "reviewer_notes",
        "exclusion_reason",
    }
    with path.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    if len({identity(r) for r in rows}) != len(rows) or {identity(r) for r in rows} != set(known):
        raise ValueError("Citation worksheet must match this exact prediction session.")
    valid, invalid = 0, 0
    for row in rows:
        original = known[identity(row)]
        if set(row) != set(original) or any(
            row[k] != original[k] for k in original if k not in editable
        ):
            raise ValueError("Prediction/context fields cannot be changed during citation review.")
        if row["review_status"] not in {"UNREVIEWED", "HUMAN_REVIEWED", "AMBIGUOUS", "EXCLUDED"}:
            raise ValueError("Unknown citation review status.")
        if row["review_status"] == "EXCLUDED" and not row["exclusion_reason"].strip():
            raise ValueError("Excluded citations require reasons.")
        if row["review_status"] != "HUMAN_REVIEWED":
            continue
        if not row["reviewer"].strip() or row["semantic_judgment"] not in {
            "SUPPORTS_CLAIM",
            "CONTRADICTS_CLAIM",
            "DOES_NOT_ESTABLISH",
        }:
            raise ValueError("Human citation review requires a reviewer and a semantic judgment.")
        if row["predicted_verdict"] == "SUPPORTED":
            if row["semantic_judgment"] == "SUPPORTS_CLAIM":
                valid += 1
            else:
                invalid += 1
    return {
        "reviewed_supported_citations": valid + invalid,
        "semantically_supporting": valid,
        "semantically_non_supporting": invalid,
        "semantic_support_precision": valid / (valid + invalid) if valid + invalid else None,
        "denominator": "Owner-reviewed individual citations attached to SUPPORTED predictions; "
        "not inferred from URLs, quote matching or case classification labels.",
    }
