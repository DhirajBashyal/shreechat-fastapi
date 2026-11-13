"""Evaluate retrieval (and optionally answers) on a test set (docs/ai/RAG_ARCHITECTURE.md §6).

    make ai-eval WS=<workspace uuid> FILE=evals.json              retrieval only
    make ai-eval WS=<workspace uuid> FILE=evals.json AGENT=<uuid> + answers (uses the LLM)

evals.json:

    [
      {"question": "How long do refunds take?",
       "expected_document_ids": ["<kb_documents.id>"],
       "must_include": ["5-7 business days"],
       "must_not_include": ["30 days"],
       "expect_handoff": false}
    ]

Reports recall@8 (share of expected documents among the top 8 hits), how
often retrieval judged the context sufficient, and, with AGENT, answer pass
rate (must_include / must_not_include / expected handoff), handoff rate,
unsupported-claim rate and refusal correctness on unanswerable questions
(expect_handoff = true). Run it after every change to chunking, embedding
model, thresholds or prompts. Nothing is sent to customers or saved.
"""

import asyncio
import json
import sys
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.ai.graph.state import CustomerView
from app.ai.rag.retriever import hybrid_search
from app.ai.runner import run_playground
from app.core.db import sessionmanager
from app.models import AiAgentKnowledgeBase, KnowledgeBase, Workspace


def _out(line: str = "") -> None:
    sys.stdout.write(line + "\n")


async def evaluate(
    workspace_id: uuid.UUID, cases: list[dict[str, Any]], agent_id: uuid.UUID | None
) -> dict[str, Any]:
    sessionmanager.init_db()
    assert sessionmanager.session_factory is not None
    results: list[dict[str, Any]] = []
    try:
        async with sessionmanager.session_factory() as session:
            ws = await session.get(Workspace, workspace_id)
            if ws is None:
                sys.exit("workspace not found")
            if agent_id:
                kb_ids = list(
                    await session.scalars(
                        select(AiAgentKnowledgeBase.knowledge_base_id).where(
                            AiAgentKnowledgeBase.ai_agent_id == agent_id
                        )
                    )
                )
            else:
                kb_ids = list(
                    await session.scalars(
                        select(KnowledgeBase.id).where(
                            KnowledgeBase.workspace_id == workspace_id,
                            KnowledgeBase.deleted_at.is_(None),
                        )
                    )
                )
            for case in cases:
                q = case["question"]
                found = await hybrid_search(
                    session,
                    workspace_id=workspace_id,
                    knowledge_base_ids=kb_ids,
                    query=q,
                    top_n=8,
                )
                docs = {str(h.document_id) for h in found.hits}
                expected = set(case.get("expected_document_ids") or [])
                row: dict[str, Any] = {
                    "question": q,
                    "recall": (len(expected & docs) / len(expected))
                    if expected
                    else None,
                    "sufficient": found.context_sufficient,
                }
                if agent_id:
                    final = await run_playground(
                        session,
                        organization_id=ws.organization_id,
                        workspace_id=workspace_id,
                        agent_id=agent_id,
                        message=q,
                        history=[],
                        customer=CustomerView(
                            first_name=None,
                            verification_level="none",
                            verification_method=None,
                        ),
                    )
                    reply = (final.get("draft") or "").lower()
                    handed = final.get("outcome") == "handoff"
                    ok = all(s.lower() in reply for s in case.get("must_include", []))
                    ok = ok and not any(
                        s.lower() in reply for s in case.get("must_not_include", [])
                    )
                    if "expect_handoff" in case:
                        ok = ok and handed == bool(case["expect_handoff"])
                    row |= {
                        "outcome": final.get("outcome"),
                        "handoff_reason": final.get("handoff_reason")
                        if handed
                        else None,
                        "unsupported": "unsupported_claim"
                        in final.get("guardrail_flags", []),
                        "passed": ok,
                        "reply": (final.get("draft") or "")[:200],
                    }
                results.append(row)
    finally:
        await sessionmanager.close()

    recalls = [r["recall"] for r in results if r["recall"] is not None]
    summary: dict[str, Any] = {
        "cases": len(results),
        "recall_at_8": round(sum(recalls) / len(recalls), 3) if recalls else None,
        "context_sufficient_rate": round(
            sum(r["sufficient"] for r in results) / len(results), 3
        )
        if results
        else None,
    }
    if agent_id and results:
        unanswerable = [
            r for r, c in zip(results, cases, strict=True) if c.get("expect_handoff")
        ]
        summary |= {
            "answer_pass_rate": round(
                sum(r["passed"] for r in results) / len(results), 3
            ),
            "handoff_rate": round(
                sum(r["outcome"] == "handoff" for r in results) / len(results), 3
            ),
            "unsupported_claim_rate": round(
                sum(r["unsupported"] for r in results) / len(results), 3
            ),
            "refusal_correctness": (
                round(
                    sum(r["outcome"] == "handoff" for r in unanswerable)
                    / len(unanswerable),
                    3,
                )
                if unanswerable
                else None
            ),
        }
    return {"summary": summary, "results": results}


def main() -> None:
    if len(sys.argv) not in (3, 4):
        sys.exit(
            "usage: python -m app.jobs.ai_eval <workspace uuid> <evals.json> [agent uuid]"
        )
    cases = json.loads(Path(sys.argv[2]).read_text())
    if not isinstance(cases, list) or not all("question" in c for c in cases):
        sys.exit("evals.json must be a list of objects with a 'question'")
    agent = uuid.UUID(sys.argv[3]) if len(sys.argv) == 4 else None
    report = asyncio.run(evaluate(uuid.UUID(sys.argv[1]), cases, agent))
    for r in report["results"]:
        recall = "-" if r["recall"] is None else f"{r['recall']:.2f}"
        extra = (
            f"  {r['outcome']:<8} {'PASS' if r['passed'] else 'FAIL'}"
            if "outcome" in r
            else ""
        )
        _out(
            f"recall {recall:>4}  sufficient {str(r['sufficient']):<5}{extra}  {r['question'][:70]}"
        )
    _out()
    _out(json.dumps(report["summary"], indent=2))


if __name__ == "__main__":
    main()
