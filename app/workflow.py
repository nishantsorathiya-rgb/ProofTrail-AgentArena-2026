"""Composition root; all model roles share the existing Mesh boundary and run budget."""

import httpx

from app.agents.correction import SelfCorrection
from app.agents.drafter import ClaimDrafter
from app.agents.followup_planner import FollowupPlanner
from app.agents.graph import ResearchWorkflow
from app.agents.planner import PlannerAgent
from app.agents.researcher import ResearcherAgent
from app.agents.synthesizer import SynthesizerAgent
from app.agents.verifier import VerifierAgent
from app.application.budgeted_model import BudgetedModel
from app.application.collection import EvidenceCollector
from app.application.config import Settings
from app.application.coordinator import LocalRunCoordinator
from app.application.reports import ReportBuilder
from app.domain.workflow import BudgetState
from app.infrastructure.claim_repository import SQLiteClaimRepository
from app.infrastructure.database import Database, SQLiteRunRepository
from app.infrastructure.evidence_repository import SQLiteEvidenceRepository
from app.infrastructure.mesh import MeshLLMAdapter
from app.infrastructure.report_repository import SQLiteReportRepository
from app.infrastructure.tavily_extract import TavilyExtractAdapter
from app.infrastructure.tavily_http import TavilyHTTPClient
from app.infrastructure.tavily_search import TavilySearchAdapter
from app.infrastructure.url_policy import PublicSourceURLPolicy, Resolver
from app.infrastructure.workflow_repository import SQLiteWorkflowRepository


class WorkflowRuntime:
    def __init__(
        self,
        settings: Settings,
        database: Database,
        *,
        mesh_transport: httpx.MockTransport | None = None,
        tavily_transport: httpx.MockTransport | None = None,
        resolver: Resolver | None = None,
        phase: str = "P5",
    ):
        self.store = SQLiteWorkflowRepository(database)
        self.claims = SQLiteClaimRepository(
            database,
            settings.max_claims_per_run,
            max_attempts=1 + settings.max_retries_per_subquestion if phase == "P5" else 2,
        )
        self.reports = SQLiteReportRepository(database, self.claims)
        evidence = SQLiteEvidenceRepository(database)
        urls = PublicSourceURLPolicy(
            timeout_seconds=settings.dns_timeout_seconds, resolver=resolver
        )
        self.mesh = MeshLLMAdapter(settings, mock_transport=mesh_transport)
        self.tavily = TavilyHTTPClient(settings, evidence, mock_transport=tavily_transport)
        collection = EvidenceCollector(
            TavilySearchAdapter(self.tavily, urls),
            TavilyExtractAdapter(self.tavily, urls),
            evidence,
            SQLiteRunRepository(database),
            urls,
            ttl_seconds=settings.page_cache_ttl_seconds,
            max_source_chars=settings.max_source_text_chars,
        )
        model = BudgetedModel(self.mesh, self.store, settings.max_llm_calls_per_run)
        planner = PlannerAgent(
            model,
            min(
                settings.max_research_subquestions,
                settings.max_searches_per_run,
                settings.max_research_tasks_per_run,
            ),
        )
        researcher = ResearcherAgent(collection, self.store, settings.max_parallel_research_tasks)
        drafter = ClaimDrafter(
            model,
            self.claims,
            max_claims=settings.max_claims_per_run,
            max_chars=settings.max_draft_input_chars,
        )
        verifier = VerifierAgent(
            model,
            self.claims,
            self.store,
            concurrency=settings.max_parallel_verifications,
            max_claims=settings.max_claims_per_run,
            max_chars=settings.max_verification_input_chars,
        )
        self.workflow = ResearchWorkflow(
            planner,
            researcher,
            self.store,
            drafter,
            verifier,
            self.claims,
            SelfCorrection(
                FollowupPlanner(model), researcher, drafter, verifier, self.claims, self.store
            ),
            ReportBuilder(self.claims, self.store, self.reports, SynthesizerAgent(model)),
        )
        limits = BudgetState(
            max_llm_calls=settings.max_llm_calls_per_run,
            max_searches=settings.max_searches_per_run,
            max_fetches=settings.max_fetches_per_run,
            max_research_tasks=settings.max_research_tasks_per_run,
            max_retries_per_subquestion=settings.max_retries_per_subquestion,
            max_claims=settings.max_claims_per_run,
        )
        mode = (
            "offline_fixture"
            if mesh_transport or tavily_transport
            else ("live" if settings.enable_live_requests else "offline_disabled")
        )
        self.coordinator = LocalRunCoordinator(
            self.workflow,
            self.store,
            limits,
            mode=mode,
            active_runs=settings.max_active_runs,
            pending_runs=settings.max_pending_runs,
            timeout_seconds=settings.workflow_timeout_seconds,
            phase=phase,
            claims=self.claims,
            reports=self.reports,
        )

    async def aclose(self):
        await self.coordinator.aclose()
        await self.mesh.aclose()
        await self.tavily.aclose()
