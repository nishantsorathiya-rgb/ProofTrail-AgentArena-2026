"""Exact-match locator regressions, including immutable real Session B quotations."""

import json
from hashlib import sha256
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from app.application.verification import parse_and_validate_decision
from app.domain.claims import CandidateClaim, EvidenceInput
from app.domain.workflow import WorkflowError

ROOT = Path(__file__).resolve().parents[2]
TRACE = json.loads((ROOT / "results/p8_session_b_001_trace.json").read_text(encoding="utf-8"))
FAILURE = json.loads(
    (ROOT / "results/p8_session_b_001_failure_analysis.json").read_text(encoding="utf-8")
)


def historical():
    candidate = CandidateClaim.model_validate(TRACE["claims"]["claims"][0]["candidate"])
    sources = {s["source_id"]: s for s in TRACE["evidence"]["sources"]}
    passages = {p["passage_id"]: p for p in TRACE["evidence"]["passages"]}
    evidence = []
    for identifier in candidate.candidate_evidence_ids:
        passage = passages[str(identifier)]
        source = sources[passage["source_id"]]
        assert sha256(source["content"].encode()).hexdigest() == source["content_sha256"]
        assert source["content"][passage["start_char"]:passage["end_char"]] == passage["text"]
        evidence.append(EvidenceInput(
            evidence_id=identifier, source_id=UUID(source["source_id"]),
            original_url=source["original_url"], source_title=source["title"],
            source_content_sha256=source["content_sha256"],
            full_content_sha256=source["full_content_sha256"], text=passage["text"],
            start_char=passage["start_char"], end_char=passage["end_char"],
            locator=passage["locator"],
        ))
    return candidate, tuple(evidence)


def selection(claim, evidence, text):
    return {"contract_version": "exact_quotes_v2", "claim_id": str(claim.claim_id),
            "verdict": "SUPPORTED", "evidence_ids": [str(evidence.evidence_id)],
            "exact_supporting_quotes": [{"evidence_id": str(evidence.evidence_id), "text": text}],
            "explanation": "Explicit semantic fixture; locator correctness is tested separately."}


@pytest.mark.parametrize("index", range(4))
def test_four_real_legacy_coordinate_failures_remain_rejected(index):
    claim, inputs = historical()
    original = FAILURE["quotes"][index]
    evidence = next(e for e in inputs if str(e.evidence_id) == original["evidence_id"])
    body = selection(claim, evidence, original["model_quote_text"])
    body.pop("contract_version")
    body["exact_supporting_quotes"][0].update(start_char=0, end_char=200)
    with pytest.raises(WorkflowError, match="verification_quote_mismatch"):
        parse_and_validate_decision(json.dumps(body), claim, inputs, inputs)


@pytest.mark.parametrize("index", range(4))
def test_four_real_exact_strings_resolve_only_under_explicit_coordinate_free_contract(index):
    claim, inputs = historical()
    original = FAILURE["quotes"][index]
    evidence = next(e for e in inputs if str(e.evidence_id) == original["evidence_id"])
    body = selection(claim, evidence, original["model_quote_text"])
    decision, quotes, contract = parse_and_validate_decision(
        json.dumps(body), claim, inputs, inputs)
    assert contract == "exact_quotes_v2" and decision.claim_id == claim.claim_id
    quote = quotes[0]
    assert quote.start_char == original["first_actual_start_char_for_diagnosis_only"]
    assert quote.end_char == original["first_actual_end_char_for_diagnosis_only"]
    assert quote.source_id == evidence.source_id
    assert quote.source_content_sha256 == evidence.source_content_sha256
    assert quote.full_content_sha256 == evidence.full_content_sha256
    assert evidence.text[quote.start_char:quote.end_char] == quote.text


def fixture(text="Prefix. A true sentence. Suffix.", start=2000):
    identifier = uuid4()
    claim = CandidateClaim(run_id=uuid4(), originating_research_task_id=uuid4(),
                           claim_text="An unverified fixture claim.",
                           candidate_evidence_ids=(identifier,))
    evidence = EvidenceInput(
        evidence_id=identifier, source_id=uuid4(), original_url="https://example.com/fixture",
        source_title="SYNTHETIC", source_content_sha256=sha256(text.encode()).hexdigest(),
        full_content_sha256=sha256(text.encode()).hexdigest(), text=text,
        start_char=start, end_char=start + len(text), locator=f"chars:{start}-{start+len(text)}",
    )
    return claim, evidence


@pytest.mark.parametrize("passage,quote,code", [
    ("same same", "same", "verification_quote_ambiguous"),
    ("aaa", "aa", "verification_quote_ambiguous"),
    ("Actual exact text.", "Missing text.", "verification_quote_missing"),
    ("Actual exact text.", "Actual exact invented text.", "verification_quote_missing"),
    ("cafe\u0301", "café", "verification_quote_missing"),
    ("first\nsecond", "first second", "verification_quote_missing"),
])
def test_rejects_ambiguous_missing_fabricated_or_normalized_quotes(passage, quote, code):
    claim, item = fixture(passage)
    with pytest.raises(WorkflowError, match=code):
        parse_and_validate_decision(
            json.dumps(selection(claim, item, quote)), claim, (item,), (item,))


def test_unicode_multiline_nonzero_passage_start_uses_absolute_python_character_offsets():
    text = "前置🐍\n" + "cafe\u0301\nΔεδομένα 🌍\n" + "suffix"
    wanted = "cafe\u0301\nΔεδομένα 🌍\n"
    claim, item = fixture(text, 2000)
    _, quotes, _ = parse_and_validate_decision(
        json.dumps(selection(claim, item, wanted)), claim, (item,), (item,))
    assert quotes[0].start_char == 2004
    assert quotes[0].end_char == 2004 + len(wanted)
    assert item.text[quotes[0].start_char - 2000:quotes[0].end_char - 2000] == wanted


def test_selected_passage_disambiguates_identical_text_in_different_approved_passages():
    claim, item = fixture("Unique here.")
    other = item.model_copy(update={"evidence_id": uuid4(), "source_id": uuid4()})
    _, quotes, _ = parse_and_validate_decision(
        json.dumps(selection(claim, item, item.text)), claim, (item, other), (item, other))
    assert len(quotes) == 1 and quotes[0].source_id == item.source_id


@pytest.mark.parametrize("field,value", [
    ("start_char", 0), ("end_char", 200), ("source_id", str(uuid4())),
    ("source_content_sha256", "0" * 64), ("run_id", str(uuid4())),
])
def test_v2_rejects_untrusted_coordinates_and_provenance_overrides(field, value):
    claim, item = fixture()
    body = selection(claim, item, "A true sentence.")
    body["exact_supporting_quotes"][0][field] = value
    with pytest.raises(ValidationError):
        parse_and_validate_decision(json.dumps(body), claim, (item,), (item,))


@pytest.mark.parametrize("field,value", [
    ("source_id", uuid4()), ("source_content_sha256", "0" * 64),
    ("full_content_sha256", "0" * 64), ("text", "Changed exact text."),
])
def test_snapshot_or_provenance_change_between_model_call_and_resolution_is_rejected(field, value):
    claim, item = fixture()
    changed = item.model_copy(update={field: value})
    with pytest.raises(WorkflowError, match="evidence_changed_during_verification"):
        parse_and_validate_decision(
            json.dumps(selection(claim, item, "A true sentence.")), claim, (item,), (changed,))


def test_fabricated_passage_id_cannot_be_resolved_against_other_source_text():
    claim, item = fixture()
    body = selection(claim, item, "A true sentence.")
    body["evidence_ids"] = [str(uuid4())]
    body["exact_supporting_quotes"][0]["evidence_id"] = body["evidence_ids"][0]
    with pytest.raises(WorkflowError, match="invalid_verification_citation"):
        parse_and_validate_decision(json.dumps(body), claim, (item,), (item,))


@pytest.mark.parametrize("version", [None, "exact_quotes_v999"])
def test_coordinate_free_contract_must_be_explicit_and_recognized(version):
    claim, item = fixture()
    body = selection(claim, item, "A true sentence.")
    if version is None:
        body.pop("contract_version")
    else:
        body["contract_version"] = version
    with pytest.raises(ValidationError):
        parse_and_validate_decision(json.dumps(body), claim, (item,), (item,))
