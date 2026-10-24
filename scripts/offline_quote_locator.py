"""Offline P8C graph replay: retained Session B sources and explicit semantic fixtures.

This never loads owner credentials or uses real provider transports. Historical
files are read only; all persistence belongs to fresh temporary fixture databases.
The model decisions are synthetic, not new Mesh semantic-accuracy measurements.
"""

import asyncio
import json
import tempfile
from hashlib import sha256
from pathlib import Path

import httpx
from sqlalchemy import text

from app.application.reports import markdown_report
from app.domain.retrieval import RetrievalError
from app.evaluation.harness import fixture_settings
from app.infrastructure.database import Database
from app.workflow import WorkflowRuntime

ROOT = Path(__file__).resolve().parents[1]
HISTORY = ROOT / "results/p8_session_b_001_trace.json"
FAILURES = ROOT / "results/p8_session_b_001_failure_analysis.json"


class HistoricalProviders:
    def __init__(self, *, negative=False, tamper=None, database=None):
        history = json.loads(HISTORY.read_text(encoding="utf-8"))
        failures = json.loads(FAILURES.read_text(encoding="utf-8"))
        self.history = history
        source_map = {s["source_id"]: s for s in history["evidence"]["sources"]}
        passage_map = {p["passage_id"]: p for p in history["evidence"]["passages"]}
        self.sources = {}
        for identifier in history["claims"]["claims"][0]["candidate"]["candidate_evidence_ids"]:
            passage = passage_map[identifier]
            source = source_map[passage["source_id"]]
            # Four exact saved passages as bounded fixture documents. This avoids depending
            # on random new passage-ID ordering in the drafter's existing eight-passage cap.
            # New hashes/timestamps describe these fixture documents, not a live re-extraction.
            self.sources[source["original_url"]] = {**source, "content": passage["text"]}
        self.quotes = {q["original_url"]: q["model_quote_text"] for q in failures["quotes"]}
        self.claim_text = history["claims"]["claims"][0]["candidate"]["claim_text"]
        if negative:
            self.claim_text = self.claim_text.replace("was added", "was not added", 1)
        self.negative, self.tamper, self.database = negative, tamper, database
        self.calls = dict.fromkeys(("planner", "drafter", "primary", "blind_review",
                                   "synthesizer", "search", "extract"), 0)
        self.decisions = []
        self.payloads = []

    async def mesh(self, request):
        assert str(request.url) == "https://api.meshapi.ai/v1/chat/completions"
        body = json.loads(request.content)
        system = body["messages"][0]["content"]
        payload = json.loads(body["messages"][1]["content"])
        self.payloads.append(body)
        if "Planner agent" in system:
            self.calls["planner"] += 1
            output = self.history["state"]["research_plan"]
        elif "candidate claim Drafter" in system:
            self.calls["drafter"] += 1
            task = payload["tasks"][0]
            ids = [e["evidence_id"] for e in task["UNTRUSTED_EVIDENCE"]
                   if e["original_url"] in self.quotes
                   and self.quotes[e["original_url"]] in e["text"]]
            output = {"claims": [{"claim_text": self.claim_text,
                                  "originating_research_task_id": task["task_id"],
                                  "candidate_evidence_ids": ids}]}
        elif "distinct Verifier" in system or "independent support review" in system:
            role = "blind_review" if "independent support review" in system else "primary"
            self.calls[role] += 1
            assert set(payload) == {"claim", "UNTRUSTED_EVIDENCE", "output_schema"}
            assert payload["output_schema"]["properties"]["contract_version"]["const"] == (
                "exact_quotes_v2")
            assert "primary_decision" not in payload
            selected = payload["UNTRUSTED_EVIDENCE"]
            output = {
                "contract_version": "exact_quotes_v2", "claim_id": payload["claim"]["claim_id"],
                "verdict": (
                    "CONTRADICTED" if self.negative and role == "blind_review" else "SUPPORTED"),
                "evidence_ids": [e["evidence_id"] for e in selected],
                "exact_supporting_quotes": [
                    {"evidence_id": e["evidence_id"], "text": self.quotes[e["original_url"]]}
                    for e in selected
                ],
                "explanation": "Explicit offline semantic fixture: affirmative source context "
                + ("contradicts the negated candidate." if self.negative and role == "blind_review"
                   else "supports the candidate according to this fixture response."),
            }
            self.decisions.append({"role": role, "output": output})
            if self.tamper and role == "primary":
                await self._tamper(selected[0])
        elif "distinct Synthesizer" in system:
            self.calls["synthesizer"] += 1
            ids = [f["claim_id"] for f in payload["UNTRUSTED_DATA"]["supported_claims"]]
            output = {"executive_summary_claim_ids": ids,
                      "sections": [{"heading": "Key findings", "claim_ids": ids}]}
        else:
            raise AssertionError("Unexpected offline role")
        return httpx.Response(200, json={
            "id": "offline-p8c-response", "object": "chat.completion", "created": 0,
            "model": "offline-p8c-semantic-fixture", "choices": [{"index": 0,
                "message": {"role": "assistant", "content": json.dumps(output)},
                "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        })

    async def _tamper(self, item):
        # Corrupt only a temporary fixture DB, never the saved Session B database.
        from uuid import uuid4

        from app.infrastructure.evidence_models import PassageRow, SourceRow

        async with self.database.sessions.begin() as session:
            source = await session.get(SourceRow, item["source_id"])
            if self.tamper == "stale_hash":
                source.content = "X" + source.content[1:]
            elif self.tamper == "wrong_source":
                unexpected = str(uuid4())
                session.add(SourceRow(**{
                    column.name: unexpected if column.name == "source_id"
                    else getattr(source, column.name) for column in SourceRow.__table__.columns
                }))
                await session.flush()
                passage = await session.get(PassageRow, item["evidence_id"])
                passage.source_id = unexpected
            elif self.tamper == "cross_run":
                from app.domain.workflow import utc_now
                from app.infrastructure.database import ResearchRunRow

                other = "00000000-0000-0000-0000-000000000123"
                session.add(ResearchRunRow(run_id=other, question="OFFLINE other run",
                                          status="completed", created_at=utc_now(),
                                          updated_at=utc_now()))
                await session.flush()
                source.run_id = other
            else:
                raise AssertionError("Unknown fixture mutation")

    async def tavily(self, request):
        payload = json.loads(request.content)
        if request.url.path == "/search":
            self.calls["search"] += 1
            assert payload["search_depth"] == "basic" and payload["include_answer"] is False
            return httpx.Response(200, json={"results": [
                {"url": url, "title": s["title"], "content": "OFFLINE discovery clue only"}
                for url, s in self.sources.items()
            ], "usage": {"credits": 0}})
        assert request.url.path == "/extract" and payload["extract_depth"] == "basic"
        self.calls["extract"] += 1
        return httpx.Response(200, json={"results": [
            {"url": url, "raw_content": self.sources[url]["content"]} for url in payload["urls"]
        ], "failed_results": [], "usage": {"credits": 0}})


async def fixture_resolver(_host, _port):
    return ["93.184.216.34"]


async def run_case(path, *, negative=False, tamper=None):
    settings = fixture_settings(
        enable_live_requests=False, database_url="sqlite:///" + path.as_posix(),
        mesh_api_key="offline-key", mesh_model_id="offline-p8c-semantic-fixture",
        tavily_api_key="offline-key", max_llm_calls_per_run=8, max_searches_per_run=2,
        max_fetches_per_run=10, max_claims_per_run=1, max_research_subquestions=1,
        max_research_tasks_per_run=2, max_retries_per_subquestion=1,
        max_parallel_fetches=1, max_parallel_research_tasks=1, max_parallel_verifications=1,
        tavily_max_attempts=1,
    )
    database = Database(settings.database_url)
    await database.initialize()
    providers = HistoricalProviders(negative=negative, tamper=tamper, database=database)
    runtime = WorkflowRuntime(settings, database,
                              mesh_transport=httpx.MockTransport(providers.mesh),
                              tavily_transport=httpx.MockTransport(providers.tavily),
                              resolver=fixture_resolver)
    try:
        await runtime.coordinator.start()
        submitted = await runtime.coordinator.submit(
            providers.history["state"]["original_question"])
        await runtime.coordinator.wait_idle()
        state = await runtime.coordinator.get(submitted.run_id)
        claims = await runtime.coordinator.claims(state.run_id)
        evidence = None
        evidence_error = None
        try:
            evidence = await runtime.coordinator.evidence(state.run_id)
        except RetrievalError as error:
            if tamper != "stale_hash" or error.code != "evidence_integrity_failed":
                raise
            evidence_error = error.code  # Expected secondary read gate on the corrupted fixture.
        report = await runtime.coordinator.report(state.run_id)
        assert report is not None
        await runtime.reports.validate(report)
        async with database.engine.connect() as connection:
            head = await connection.scalar(text("SELECT version_num FROM alembic_version"))
            foreign_keys = (await connection.execute(text("PRAGMA foreign_key_check"))).all()
        return {
            "mode": "OFFLINE_RETAINED_HISTORY_RETRIEVAL_AND_SYNTHETIC_SEMANTIC_FIXTURES",
            "negative_semantic_case": negative, "mutation": tamper,
            "run": state.model_dump(mode="json"), "claims": claims.model_dump(mode="json"),
            "evidence": evidence.model_dump(mode="json") if evidence else None,
            "evidence_read_error": evidence_error, "report": report.model_dump(mode="json"),
            "markdown_report": markdown_report(report), "fixture_calls": providers.calls,
            "fixture_model_outputs": providers.decisions,
            "graph": runtime.workflow.graph.get_graph().to_json(),
            "alembic_head": head, "foreign_key_violations": [tuple(r) for r in foreign_keys],
            "final_repository_validation": "PASSED", "live_provider_requests": 0,
        }
    finally:
        await runtime.aclose()
        await database.aclose()


async def run():
    historical_hashes = {p.relative_to(ROOT).as_posix(): sha256(p.read_bytes()).hexdigest()
                         for p in (HISTORY, FAILURES)}
    with tempfile.TemporaryDirectory(prefix="prooftrail-p8c-offline-") as directory:
        positive = await run_case(Path(directory) / "positive.db")
        negative = await run_case(Path(directory) / "negative.db", negative=True)
    assert len(positive["report"]["verified_findings"]) == 1
    assert positive["fixture_calls"]["blind_review"] == 1
    assert positive["report"]["generation_status"] == "model_organized"
    assert negative["report"]["verified_findings"] == []
    assert negative["claims"]["claims"][0]["verification_status"] == "CONTRADICTED"
    assert all(sha256((ROOT / name).read_bytes()).hexdigest() == value
               for name, value in historical_hashes.items())
    result = {
        "phase": "P8C", "live_provider_requests": 0, "actual_api_cost_usd": 0,
        "measurement_note": "Graph timing measured locally; model outputs/tokens are fixtures, "
        "not live model accuracy. Extract fixtures replay the four exact saved candidate passages "
        "as bounded fixture documents with new hashes/timestamps, not complete live webpages. "
        "Historical files are unchanged; no lost full response is reconstructed.",
        "historical_file_sha256_unchanged": historical_hashes,
        "positive": positive, "negative": negative,
    }
    target = ROOT / "results/p8_quote_locator_offline_trace.json"
    with target.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2)
        stream.write("\n")
    print("P8C offline graph replay passed: trusted locators + two semantic calls; "
          "negative claim withheld. Live calls 0; actual spending USD 0.")
    return result


if __name__ == "__main__":
    asyncio.run(run())
