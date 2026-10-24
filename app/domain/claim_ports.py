from typing import Protocol
from uuid import UUID

from app.domain.claims import CandidateClaim, ClaimCollection, EvidenceInput, VerificationResult


class ClaimStore(Protocol):
    async def save_candidates(self, run_id: UUID, claims: tuple[CandidateClaim, ...]) -> None: ...

    async def claims(self, run_id: UUID) -> tuple[CandidateClaim, ...]: ...

    async def evidence_for(self, claim: CandidateClaim) -> tuple[EvidenceInput, ...]: ...

    async def begin_attempt(
        self, claim: CandidateClaim, *, research_retry_id: UUID | None = None
    ) -> UUID: ...

    async def finish_attempt(self, result: VerificationResult) -> VerificationResult: ...

    async def collection(self, run_id: UUID) -> ClaimCollection: ...

    async def attach_retry_evidence(
        self,
        run_id: UUID,
        retry_id: UUID,
        claim_ids: tuple[UUID, ...],
        evidence_ids: tuple[UUID, ...],
    ) -> None: ...
