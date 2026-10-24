"""Deterministic gates around, rather than in place of, semantic verification."""

from pydantic import TypeAdapter

from app.domain.claims import (
    CandidateClaim,
    EvidenceInput,
    ProposedQuote,
    QuoteSelectionDecision,
    SemanticDecision,
    VerifiedQuote,
)
from app.domain.workflow import WorkflowError


def validate_decision(
    claim: CandidateClaim,
    proposed: SemanticDecision,
    before: tuple[EvidenceInput, ...],
    current: tuple[EvidenceInput, ...],
) -> tuple[VerifiedQuote, ...]:
    if proposed.claim_id != claim.claim_id:
        raise WorkflowError("verification_claim_mismatch")
    if before != current:
        raise WorkflowError("evidence_changed_during_verification")
    inputs = {item.evidence_id: item for item in current}
    if not set(proposed.evidence_ids).issubset(inputs):
        raise WorkflowError("invalid_verification_citation")
    quotes = []
    for quote in proposed.exact_supporting_quotes:
        item = inputs.get(quote.evidence_id)
        if item is None:
            raise WorkflowError("invalid_verification_citation")
        start, end = quote.start_char - item.start_char, quote.end_char - item.start_char
        if start < 0 or end > len(item.text) or end <= start or item.text[start:end] != quote.text:
            raise WorkflowError("verification_quote_mismatch")
        quotes.append(
            VerifiedQuote(
                **quote.model_dump(),
                source_id=item.source_id,
                source_content_sha256=item.source_content_sha256,
                full_content_sha256=item.full_content_sha256,
                locator=f"extracted_text:chars:{quote.start_char}-{quote.end_char}",
            )
        )
    return tuple(quotes)


_MODEL_DECISION = TypeAdapter(QuoteSelectionDecision | SemanticDecision)


def resolve_quote_selection(
    claim: CandidateClaim,
    proposed: QuoteSelectionDecision,
    before: tuple[EvidenceInput, ...],
    current: tuple[EvidenceInput, ...],
) -> SemanticDecision:
    """Locate unique exact text in repository-approved, version-checked passages.

    Callers must obtain both snapshots from ClaimStore.evidence_for(claim), which
    checks run/task/source approvals, extraction, source hashes and claim links.
    Never supply provider-authored EvidenceInput objects. Selection is passage-local;
    persisted coordinates are absolute Python str indices in retained source text.
    No normalization, fuzzy matching, occurrence guessing or legacy-offset repair.
    This constructs locators, not semantic verdicts or usable SUPPORTED claims.
    """
    if proposed.claim_id != claim.claim_id:
        raise WorkflowError("verification_claim_mismatch")
    if before != current:
        raise WorkflowError("evidence_changed_during_verification")
    inputs = {item.evidence_id: item for item in current}
    if not set(proposed.evidence_ids).issubset(inputs):
        raise WorkflowError("invalid_verification_citation")
    quotes = []
    for quote in proposed.exact_supporting_quotes:
        item = inputs.get(quote.evidence_id)
        if item is None:
            raise WorkflowError("invalid_verification_citation")
        local_start = item.text.find(quote.text)
        if local_start < 0:
            raise WorkflowError("verification_quote_missing")
        # Include overlapping matches: 'aa' in 'aaa' is ambiguous as well.
        if item.text.find(quote.text, local_start + 1) >= 0:
            raise WorkflowError("verification_quote_ambiguous")
        start = item.start_char + local_start
        quotes.append(ProposedQuote(
            evidence_id=quote.evidence_id, text=quote.text,
            start_char=start, end_char=start + len(quote.text),
        ))
    return SemanticDecision(
        **proposed.model_dump(exclude={"contract_version", "exact_supporting_quotes"}),
        exact_supporting_quotes=tuple(quotes),
    )


def parse_and_validate_decision(
    response_text: str,
    claim: CandidateClaim,
    before: tuple[EvidenceInput, ...],
    current: tuple[EvidenceInput, ...],
) -> tuple[SemanticDecision, tuple[VerifiedQuote, ...], str]:
    """Explicit v2 selection or unchanged strict v1; never fall back after rejection."""
    proposed = _MODEL_DECISION.validate_json(response_text)
    contract = "strict_offsets_v1"
    if isinstance(proposed, QuoteSelectionDecision):
        contract = proposed.contract_version
        proposed = resolve_quote_selection(claim, proposed, before, current)
    return proposed, validate_decision(claim, proposed, before, current), contract
