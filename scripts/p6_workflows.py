"""Measure the existing P5 graph under synthetic HTTP fixtures without rewriting P5 artifacts."""

from pathlib import Path
from typing import Literal

import httpx
from pydantic import Field, model_validator
from sqlalchemy import select

from app.domain.workflow import TypedRecord
from app.evaluation.harness import fixture_settings
from app.infrastructure.database import Database
from app.infrastructure.evidence_models import UsageRow
from app.infrastructure.workflow_models import ModelUsageRow
from app.workflow import WorkflowRuntime
from scripts.offline_p5 import OfflineP5Providers, public_resolver


class ExpectedSubtopic(TypedRecord):
    id: str
    description: str
    claim_text: str
    planning_term: str


class WorkflowCase(TypedRecord):
    case_id: str
    question: str
    retry_result: Literal["new", "unresolved", "empty"]
    initial_empty: bool


class WorkflowDataset(TypedRecord):
    dataset_version: str
    review_status: Literal["UNREVIEWED", "HUMAN_REVIEWED"]
    reviewer: str
    expected_subtopics: tuple[ExpectedSubtopic, ...] = Field(min_length=1)
    cases: tuple[WorkflowCase, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def review_and_identity(self):
        if self.review_status == "HUMAN_REVIEWED" and not self.reviewer.strip():
            raise ValueError("Reviewed coverage rubrics require an owner/reviewer identity.")
        if len({c.case_id for c in self.cases}) != len(self.cases):
            raise ValueError("Duplicate workflow cases.")
        if len({c.id for c in self.expected_subtopics}) != len(self.expected_subtopics):
            raise ValueError("Duplicate expected subtopics.")
        return self


async def evaluate_workflow(case: WorkflowCase, dataset: WorkflowDataset, directory: Path):
    providers = OfflineP5Providers()
    providers.retry_result, providers.initial_empty = case.retry_result, case.initial_empty
    settings = fixture_settings(
        enable_live_requests=False,
        database_url=f"sqlite:///{(directory / (case.case_id + '.db')).as_posix()}",
        mesh_api_key="offline-key",
        mesh_model_id="offline-fixture",
        tavily_api_key="offline-key",
        tavily_max_attempts=1,
        max_retries_per_subquestion=2,
        max_llm_calls_per_run=12,
    )
    database = Database(settings.database_url)
    await database.initialize()
    runtime = WorkflowRuntime(
        settings,
        database,
        mesh_transport=httpx.MockTransport(providers.mesh),
        tavily_transport=httpx.MockTransport(providers.tavily),
        resolver=public_resolver,
    )
    try:
        await runtime.coordinator.start()
        submitted = await runtime.coordinator.submit(case.question)
        await runtime.coordinator.wait_idle()
        state = await runtime.coordinator.get(submitted.run_id)
        report = await runtime.coordinator.report(submitted.run_id)
        claims = await runtime.coordinator.claims(submitted.run_id)
        evidence = await runtime.coordinator.evidence(submitted.run_id)
        if report:
            await runtime.reports.validate(report)
        source_text = "\n".join(s.content for s in evidence.sources)
        findings = {f.claim_text for f in report.verified_findings} if report else set()
        planned = " ".join(
            t.assignment.question for t in state.subquestions if not t.parent_task_id
        )
        coverage = [
            {
                "subtopic_id": t.id,
                "expected_claim": t.claim_text,
                "plan_term_present_PROXY_ONLY": t.planning_term in planned,
                "exact_evidence_retrieved": t.claim_text in source_text,
                "final_assertion_present": t.claim_text in findings,
            }
            for t in dataset.expected_subtopics
        ]
        total = len(coverage)
        coverage_rate = sum(t["final_assertion_present"] for t in coverage) / total
        async with database.sessions() as session:
            usages = (
                await session.scalars(select(UsageRow).where(UsageRow.run_id == str(state.run_id)))
            ).all()
            models = (
                await session.scalars(
                    select(ModelUsageRow).where(ModelUsageRow.run_id == str(state.run_id))
                )
            ).all()
        passage_by_id = {p.passage_id: p for p in evidence.passages}
        retries = []
        for retry in state.retry_history:
            during = [
                u
                for u in usages
                if retry.finished_at and retry.created_at <= u.started_at <= retry.finished_at
            ]
            model_during = [
                m
                for m in models
                if retry.finished_at and retry.created_at <= m.started_at <= retry.finished_at
            ]
            changes = []
            for record in claims.claims:
                for attempt in record.attempts:
                    result = attempt.result
                    if result and result.research_retry_id == retry.retry_id:
                        old_texts = {
                            passage_by_id[p].text
                            for p in retry.previous_evidence_ids
                            if p in passage_by_id
                        }
                        novel = [
                            p
                            for p in result.evidence_ids
                            if p in retry.new_evidence_ids
                            and passage_by_id[p].text not in old_texts
                        ]
                        improved = (
                            result.previous_verdict == "INSUFFICIENT_EVIDENCE"
                            and result.verdict.value == "SUPPORTED"
                            and result.outcome == "completed"
                            and bool(novel)
                            and any(u.operation == "search" for u in during)
                        )
                        changes.append(
                            {
                                "claim_id": str(record.candidate.claim_id),
                                "claim_text": record.candidate.claim_text,
                                "initial_verdict": result.previous_verdict,
                                "reverification_verdict": result.verdict.value,
                                "new_cited_evidence_ids": [str(p) for p in novel],
                                "mechanical_recovery_observed": improved,
                                "human_confirmed_success": None,
                                "result": result.model_dump(mode="json"),
                            }
                        )
            retries.append(
                {
                    "retry": retry.model_dump(mode="json"),
                    "changes": changes,
                    "additional_latency_seconds": retry.duration_seconds,
                    "additional_search_requests": sum(u.operation == "search" for u in during),
                    "additional_extract_requests": sum(u.operation == "extract" for u in during),
                    "additional_llm_calls_after_assignment": sum(
                        m.outbound_called for m in model_during
                    ),
                    "usage_scope": "Retry interval; follow-up planning reported separately",
                }
            )
        results = [a.result for c in claims.claims for a in c.attempts if a.result]
        started_ids = {
            e.details["attempt_id"]
            for e in state.audit_events
            if e.event == "verification_attempt_started"
        }
        finished_ids = {
            e.details["attempt_id"]
            for e in state.audit_events
            if e.event == "verification_attempt_finished"
        }
        audit_valid = started_ids == finished_ids == {str(r.attempt_id) for r in results} and [
            e.sequence for e in state.audit_events
        ] == list(range(1, len(state.audit_events) + 1))
        supported = set(claims.supported_claim_ids)
        rejected = {
            c.candidate.claim_text for c in claims.claims if c.verification_status != "SUPPORTED"
        }
        grounded = (
            all(f.claim_id in supported for f in report.verified_findings) if report else False
        )
        return {
            "case_id": case.case_id,
            "question": case.question,
            "review_status": dataset.review_status,
            "mode": "offline_synthetic_workflow",
            "expected_subtopics": coverage,
            "reviewed_research_coverage": coverage_rate
            if dataset.review_status == "HUMAN_REVIEWED"
            else None,
            "provisional_fixture_coverage": coverage_rate,
            "coverage_denominator": total,
            "planning_quality_human_score": None,
            "retrieval_completeness_provisional": sum(
                t["exact_evidence_retrieved"] for t in coverage
            )
            / total,
            "final_findings_subset_of_current_supported": grounded,
            "unsupported_claims_excluded": not (findings & rejected),
            "report_citation_revalidation_passed": report is not None,
            "report_completeness": report.completion_status if report else "no_report",
            "gap_detection": [g.model_dump(mode="json") for g in state.research_gaps],
            "retry_effectiveness": retries,
            "human_confirmed_successful_retries": 0,
            "followup_planner_calls": providers.calls["followup"],
            "audit_integrity": audit_valid,
            "provider_counts_match_persistence": (
                state.budgets.llm_calls
                == sum(v for k, v in providers.calls.items() if k not in {"search", "extract"})
                and state.budgets.search_requests == providers.calls["search"]
                and state.budgets.extract_requests == providers.calls["extract"]
            ),
            "state": state.model_dump(mode="json", exclude={"pending_report_json"}),
            "report": report.model_dump(mode="json") if report else None,
            "claims": claims.model_dump(mode="json"),
            "provider_calls": providers.calls,
        }
    finally:
        await runtime.aclose()
        await database.aclose()
