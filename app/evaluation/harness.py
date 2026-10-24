"""Evaluation composition root: real persistence, budgets, Mesh boundary and VerifierAgent.

Offline fixtures use explicit HTTP MockTransport. The separately gated live runner can supply
the same Mesh adapter. Ground truth is never sent to the model or to a provider fixture.
"""

import json
from hashlib import sha256
from pathlib import Path
from time import perf_counter
from uuid import UUID, uuid4

import httpx
from sqlalchemy import select

from app.agents.verifier import VerifierAgent
from app.application.budgeted_model import BudgetedModel
from app.application.config import Settings
from app.domain.claims import CandidateClaim
from app.domain.retrieval import (
    ExtractedPage,
    ExtractionRecord,
    SavedEvidence,
    SearchHit,
    make_snapshot,
    passages_for,
)
from app.domain.workflow import BudgetState, ResearchTask, Subquestion, WorkflowState, utc_now
from app.evaluation.dataset import EvaluationCase
from app.infrastructure.claim_models import ClaimEvidenceRow
from app.infrastructure.claim_repository import SQLiteClaimRepository
from app.infrastructure.database import Database
from app.infrastructure.evidence_models import PassageRow, SourceRow
from app.infrastructure.evidence_repository import SQLiteEvidenceRepository
from app.infrastructure.mesh import MeshLLMAdapter
from app.infrastructure.workflow_repository import SQLiteWorkflowRepository


def fixture_settings(**overrides):
    """Explicit defaults prevent shell/.env values changing an offline evaluation silently."""
    defaults = {name: field.get_default() for name, field in Settings.model_fields.items()}
    return Settings(_env_file=None, **(defaults | overrides))


class PredictionCapture:
    """Observe the existing adapter's output without adding another model boundary or request."""

    def __init__(self, adapter):
        self.adapter, self.proposed, self.calls = adapter, None, 0

    async def complete(self, messages, *, max_tokens=2048):
        self.calls += 1
        result = await self.adapter.complete(messages, max_tokens=max_tokens)
        try:
            parsed = json.loads(result.text)
            if self.proposed is None:
                self.proposed = parsed if isinstance(parsed, dict) else None
        except (ValueError, TypeError):
            pass
        return result


class VerificationHarness:
    def __init__(self, database: Database):
        self.database = database
        self.store = SQLiteWorkflowRepository(database)
        self.claims = SQLiteClaimRepository(database)
        self.evidence = SQLiteEvidenceRepository(database)

    async def evaluate(
        self,
        case: EvaluationCase,
        provider=None,
        *,
        mutation=None,
        mesh_adapter=None,
        max_calls=2,
    ):
        started = perf_counter()
        run_id, task_id = uuid4(), uuid4()
        state = WorkflowState(
            run_id=run_id,
            original_question=case.claim_text,
            phase="P4",
            mode="live" if mesh_adapter is not None else "offline_fixture",
            budgets=BudgetState(max_llm_calls=max_calls, max_searches=1, max_fetches=1),
        )
        await self.store.create(state)
        snapshots, passage_list = [], []
        # The separator is only a synthetic test convention for conflicting-source fixtures.
        contents = (
            case.evidence_text.split("\n---SOURCE---\n")
            if case.evidence_kind == "synthetic"
            else [case.evidence_text]
        )
        for index, content in enumerate(contents):
            url = (
                f"https://example.com/synthetic-p6/{case.case_id}/{index}"
                if case.evidence_kind == "synthetic"
                else case.evidence_source
            )
            digest = sha256(content.encode()).hexdigest()
            source = make_snapshot(
                run_id,
                SearchHit(url, url, "SYNTHETIC evaluation fixture"),
                ExtractedPage(url, url, utc_now(), content, digest, len(content)),
            )
            passages = passages_for(source)
            await self.evidence.save_evidence(
                SavedEvidence(
                    ExtractionRecord(
                        run_id, url, url, source.retrieved_at, "success", source.source_id
                    ),
                    source,
                    passages,
                )
            )
            snapshots.append(source)
            passage_list.extend(passages)
        task = ResearchTask(
            task_id=task_id,
            assignment=Subquestion(
                question=case.claim_text,
                search_query="Explicit synthetic fixture seed; NO search performed",
                evidence_requirement="Exact persisted fixture text",
            ),
            status="collected",
            source_ids=tuple(s.source_id for s in snapshots),
            passage_ids=tuple(p.passage_id for p in passage_list),
        )
        state = await self.store.save(
            state.evolve(subquestions=(task,), retrieved_evidence_ids=task.passage_ids),
            agent="researcher",
            event="evaluation_fixture_seeded",
            details={"live_retrieval": 0},
        )
        ids = task.passage_ids
        if mutation == "invalid_candidate_id":
            ids = (UUID(int=997),)
        candidate = CandidateClaim(
            run_id=run_id,
            claim_text=case.claim_text,
            originating_research_task_id=task_id,
            candidate_evidence_ids=ids,
        )
        await self.claims.save_candidates(run_id, (candidate,))
        if mutation == "changed_source":
            async with self.database.sessions.begin() as session:
                row = await session.get(SourceRow, str(snapshots[0].source_id))
                row.content = "X" + row.content[1:]  # Same size; deliberately stale stored hash.
        settings = fixture_settings(
            enable_live_requests=False,
            database_url="sqlite:///:memory:",
            mesh_api_key="offline-fixture-key",
            mesh_model_id="offline-controlled-record-fixture-v1",
        )
        if mesh_adapter is None and provider is None:
            raise ValueError("An explicit offline fixture or approved Mesh adapter is required.")
        mesh = mesh_adapter or MeshLLMAdapter(
            settings, mock_transport=httpx.MockTransport(provider.mesh)
        )
        capture = PredictionCapture(mesh)
        model = BudgetedModel(capture, self.store, max_calls)
        if mutation == "budget_exhausted":
            reservation = await self.store.reserve_model(run_id, 1)
            await self.store.finish_model(
                reservation,
                result=None,
                error_code="fixture_reserved",
                elapsed_seconds=0,
                outbound_called=False,
            )
        agent = VerifierAgent(
            model, self.claims, self.store, concurrency=3, max_claims=18, max_chars=12000
        )
        try:
            result = await agent.verify(candidate)
        finally:
            if mesh_adapter is None:
                await mesh.aclose()
        citations = await self._citations(candidate, capture.proposed, result.primary_decision)
        collection = await self.claims.collection(run_id)
        persisted = collection.claims[0].attempts[-1].result
        current = await self.store.get(run_id)
        audit = [e.model_dump(mode="json") for e in current.audit_events]
        return {
            "case_id": case.case_id,
            "dataset_split": case.dataset_split,
            "evidence_kind": case.evidence_kind,
            "review_status": case.review_status,
            "expected_verdict": case.expected_verdict.value,
            "claim_text": case.claim_text,
            "evidence_text": case.evidence_text,
            "evidence_source": case.evidence_source,
            "evidence_provenance": case.evidence_provenance,
            "prediction": result.model_dump(mode="json"),
            "proposed_citations": citations,
            "provider_calls": capture.calls,
            "persistence_roundtrip_equal": persisted == result,
            "usable_supported_claim": candidate.claim_id in collection.supported_claim_ids,
            "audit": audit,
            "duration_seconds": perf_counter() - started,
            "provisional_semantic_agreement": result.outcome == "completed"
            and result.verdict == case.expected_verdict,
            "reviewed_semantic_correctness": (
                result.verdict == case.expected_verdict and result.outcome == "completed"
            )
            if case.review_status == "HUMAN_REVIEWED"
            else None,
        }

    async def _citations(self, candidate, proposed, resolved_primary=None):
        if proposed is None:
            return []
        # V2 deliberately has no model-authored coordinates. Measure the actual trusted
        # primary resolution, including when blind semantics later reject the claim.
        # Never synthesize coordinates for invalid v2 output or repair strict v1 output.
        if proposed.get("contract_version") == "exact_quotes_v2" and resolved_primary is not None:
            proposed = resolved_primary.model_dump(mode="json")
        raw_references = proposed.get("evidence_ids")
        raw_quotes = proposed.get("exact_supporting_quotes")
        references = (
            {r for r in raw_references if isinstance(r, str)}
            if isinstance(raw_references, list)
            else set()
        )
        quotes = (
            [q for q in raw_quotes if isinstance(q, dict) and isinstance(q.get("evidence_id"), str)]
            if isinstance(raw_quotes, list)
            else []
        )
        references.update(q["evidence_id"] for q in quotes)
        checks = []
        async with self.database.sessions() as session:
            links = (
                await session.scalars(
                    select(ClaimEvidenceRow).where(
                        ClaimEvidenceRow.claim_id == str(candidate.claim_id)
                    )
                )
            ).all()
            linked = {row.passage_id: row.source_sha256 for row in links}
            for reference in sorted(references):
                passage = await session.get(PassageRow, reference)
                source = await session.get(SourceRow, passage.source_id) if passage else None
                relevant = [q for q in quotes if q["evidence_id"] == reference]
                exact = bool(
                    source
                    and passage
                    and relevant
                    and all(
                        isinstance(q.get("text"), str)
                        and isinstance(q.get("start_char"), int)
                        and isinstance(q.get("end_char"), int)
                        and q["text"] == source.content[q["start_char"] : q["end_char"]]
                        and passage.start_char
                        <= q["start_char"]
                        < q["end_char"]
                        <= passage.end_char
                        for q in relevant
                    )
                )
                entry = {
                    "evidence_id": reference,
                    "reference_exists": bool(passage and source),
                    "exact_quote_valid": exact,
                    "source_hash_consistent": bool(
                        source
                        and sha256(source.content.encode()).hexdigest() == source.content_sha256
                        and (
                            source.original_chars > len(source.content)
                            or source.full_content_sha256 == source.content_sha256
                        )
                        and linked.get(reference) == source.content_sha256
                    ),
                    "claim_evidence_link_valid": bool(
                        source and reference in linked and source.run_id == str(candidate.run_id)
                    ),
                    "correct_claim_id": proposed.get("claim_id") == str(candidate.claim_id),
                }
                entry["structurally_valid"] = all(v for k, v in entry.items() if k != "evidence_id")
                entry["proposed_quotes"] = relevant
                checks.append(entry)
        return checks


async def create_harness(path: Path):
    database = Database(f"sqlite:///{path.as_posix()}")
    await database.initialize()
    return VerificationHarness(database)
