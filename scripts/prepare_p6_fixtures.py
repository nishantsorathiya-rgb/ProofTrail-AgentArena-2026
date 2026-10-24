"""Create a NEW synthetic draft bundle; refuses to overwrite owner review or an existing version."""

import argparse
import csv
from pathlib import Path

from app.evaluation.dataset import EvaluationCase, EvaluationDataset

DOMAINS = (
    ("Harbor Museum", "acquisitions", "galleries"),
    ("Juniper Observatory", "observations", "telescopes"),
    ("Cedar Transit", "journeys", "routes"),
    ("Maple Library", "loans", "branches"),
    ("Orchid Clinic", "appointments", "wards"),
    ("Willow Farm", "harvests", "fields"),
    ("Quartz Laboratory", "samples", "instruments"),
    ("Silver Theatre", "performances", "stages"),
    ("Meadow Utility", "connections", "stations"),
    ("Coral Archive", "records", "collections"),
)


def drafts():
    cases = []
    for index, (entity, first, second) in enumerate(DOMAINS):
        split = "development" if index < 6 else "held_out"
        for variant in range(6):
            case_id = f"PT6-{index * 6 + variant + 1:03}"
            year, count = 2008 + index, 31 + index * 7 + variant
            measure = first if variant % 2 == 0 else second
            original = f"In {year}, {entity} recorded {count} {measure}."
            label = ("SUPPORTED", "CONTRADICTED", "INSUFFICIENT_EVIDENCE")[variant // 2]
            claim = original
            reason = "The complete assertion exactly matches the synthetic ledger."
            if label == "CONTRADICTED":
                claim = f"In {year}, {entity} recorded {count + 9} {measure}."
                reason = (
                    "The ledger gives a different count for the same entity, period and measure."
                )
            elif label == "INSUFFICIENT_EVIDENCE":
                claim = f"In {year + 1}, {entity} recorded {count} {measure}."
                reason = "The ledger covers a different year; no assertion about this year follows."
            cases.append(
                EvaluationCase(
                    case_id=case_id,
                    claim_text=claim,
                    evidence_text="EXPLICIT SYNTHETIC LEDGER; no real-world provenance. "
                    + original,
                    evidence_source=f"fixture:prooftrail-p6-v1/{case_id}",
                    evidence_provenance="Authored synthetic controlled record; "
                    "no web page retrieved.",
                    expected_verdict=label,
                    explanation=reason,
                    review_status="UNREVIEWED",
                    dataset_split=split,
                    evidence_kind="synthetic",
                    family_id=f"domain-{index + 1}",
                )
            )
    dataset = EvaluationDataset(
        dataset_version="prooftrail-synthetic-v1",
        construction="Generated draft labels, UNREVIEWED. Domain families split before evaluation. "
        "Controlled numeric records test plumbing; "
        "they cannot measure real-world semantic accuracy.",
        cases=tuple(cases),
    )
    dataset.validate_target()
    return dataset


def write_new(directory: Path):
    directory.mkdir(parents=True, exist_ok=True)
    dataset = drafts()
    # Exclusive creation protects owner edits and versioned snapshots.
    with (directory / "claims.v1.json").open("x", encoding="utf-8") as stream:
        stream.write(dataset.model_dump_json(indent=2) + "\n")
    with (directory / "owner_review.v1.csv").open("x", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(EvaluationCase.model_fields))
        writer.writeheader()
        writer.writerows(c.model_dump(mode="json") for c in dataset.cases)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    write_new(parser.parse_args().directory)
