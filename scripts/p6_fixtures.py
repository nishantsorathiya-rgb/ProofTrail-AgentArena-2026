"""Frozen OFFLINE provider contract. Never reads dataset labels, explanations or review status.

This small controlled-grammar interpreter is NOT an LLM or a general semantic verifier.
Adversarial modes intentionally emulate model errors to exercise production rejection gates.
"""

import json
import re
from uuid import UUID

import httpx

RECORD = re.compile(r"In (\d{4}), ([A-Za-z ]+) recorded (\d+) ([A-Za-z ]+)\.")
MODEL_ID = "offline-controlled-record-fixture-v1"
POLICY_VERSION = "controlled-record-v1-frozen-before-held-out"


class OfflineSemanticFixture:
    def __init__(self, mode="normal", review_fault=None):
        self.mode = mode
        self.proposed = None
        self.messages = []
        self.calls = 0
        self.review_fault = review_fault

    async def mesh(self, request):
        assert str(request.url) == "https://api.meshapi.ai/v1/chat/completions"
        body = json.loads(request.content)
        self.calls += 1
        self.messages = body["messages"]
        assert "tools" not in body
        payload = json.loads(self.messages[1]["content"])
        support_review = "independent support review" in self.messages[0]["content"]
        assert set(payload) == {"claim", "UNTRUSTED_EVIDENCE", "output_schema"}
        if self.mode == "timeout" or (support_review and self.review_fault == "timeout"):
            raise httpx.ReadTimeout("Synthetic offline timeout")
        if self.mode == "malformed" or (support_review and self.review_fault == "malformed"):
            content = "{invalid JSON"
        else:
            claim = payload["claim"]
            evidence = payload["UNTRUSTED_EVIDENCE"]
            parsed = RECORD.fullmatch(claim["claim_text"])
            verdict, selected = "INSUFFICIENT_EVIDENCE", []
            matches = []
            for item in evidence:
                for fact in RECORD.finditer(item["text"]):
                    if parsed and (fact[1], fact[2], fact[4]) == (parsed[1], parsed[2], parsed[4]):
                        matches.append((item, fact))
            if matches:
                values = {m[1][3] for m in matches}
                verdict = (
                    "CONFLICTING"
                    if len(values) > 1
                    else "SUPPORTED"
                    if parsed[3] in values
                    else "CONTRADICTED"
                )
                selected = matches[:2] if verdict == "CONFLICTING" else matches[:1]
            quotes = []
            for item, fact in selected:
                start = item["start_char"] + fact.start()
                quotes.append(
                    {
                        "evidence_id": item["evidence_id"],
                        "text": fact[0],
                        "start_char": start,
                        "end_char": start + len(fact[0]),
                    }
                )
            if not support_review and self.mode in {
                "false_support",
                "fabricated_reference",
                "bad_quote",
                "wrong_claim",
                "wrong_offset",
            }:
                item = evidence[0]
                quotes = [
                    {
                        "evidence_id": item["evidence_id"],
                        "text": item["text"],
                        "start_char": item["start_char"],
                        "end_char": item["end_char"],
                    }
                ]
                verdict = "SUPPORTED"
            if support_review and self.mode == "false_support":
                # Explicit mocked semantic answers, not production lexical decision rules.
                if claim["claim_text"] == "In 2024, Test Archive did not record 42 records.":
                    verdict = "CONTRADICTED"
                    item = evidence[0]
                    quotes = [
                        {
                            "evidence_id": item["evidence_id"],
                            "text": item["text"],
                            "start_char": item["start_char"],
                            "end_char": item["end_char"],
                        }
                    ]
                elif claim["claim_text"] == "In 2024, Test Archive recorded 42 records.":
                    verdict, quotes = "INSUFFICIENT_EVIDENCE", []
            output = {
                "claim_id": claim["claim_id"],
                "verdict": verdict,
                "evidence_ids": list(dict.fromkeys(q["evidence_id"] for q in quotes)),
                "exact_supporting_quotes": quotes,
                "explanation": "Explicit offline controlled-record fixture; no live reasoning.",
            }
            if self.mode == "fabricated_reference" or (
                support_review and self.review_fault == "citation"
            ):
                invented = str(UUID(int=999))
                output["evidence_ids"] = [invented]
                output["exact_supporting_quotes"][0]["evidence_id"] = invented
            elif self.mode == "bad_quote":
                output["exact_supporting_quotes"][0]["text"] = "Fabricated quotation."
            elif self.mode == "wrong_offset":
                output["exact_supporting_quotes"][0]["start_char"] += 1
            elif self.mode == "wrong_claim":
                output["claim_id"] = str(UUID(int=998))
            self.proposed = output
            content = json.dumps(output)
        return httpx.Response(
            200,
            json={
                "id": "offline-p6",
                "object": "chat.completion",
                "created": 0,
                "model": MODEL_ID,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
            },
        )
