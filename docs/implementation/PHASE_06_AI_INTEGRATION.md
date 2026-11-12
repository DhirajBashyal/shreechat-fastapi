# Phase 6 — AI Agent, RAG and Human Handoff (Steps 15–17)

## 1. Objective
Tenants configure an AI agent that answers customers from their knowledge base on chosen inboxes, stays within guardrails, and hands off to humans reliably.

## 2. Features
- AI provider credentials (BYO keys), model allow-list, provider factory (OpenAI, Anthropic; Gemini optional).
- AI agent configuration and inbox assignment.
- Knowledge bases, documents (URL/file/text/FAQ), ingestion pipeline, hybrid retrieval.
- LangGraph graph with all nodes in [../ai/LANGGRAPH_NODES.md](../ai/LANGGRAPH_NODES.md) (product/order tools stubbed until integrations exist in R2).
- Trigger handler with `should_run`, debounce, per-conversation lock, per-org concurrency.
- Handoff, human takeover, return-to-AI.
- Memory: history builder, rolling summaries, facts.
- AI activity view (`ai_runs`, `ai_tool_calls`), usage metering, playground endpoint.
- Evaluation harness (internal).

## 3. Files to create
```text
src/converse/ai/handlers.py
src/converse/ai/runtime/{runner.py, deps.py, fallback.py}
src/converse/ai/graph/{state.py, build.py, edges.py}
src/converse/ai/graph/nodes/{input_validation.py, tenant_context.py, conversation_context.py, customer_identification.py,
    intent_classification.py, knowledge_retrieval.py, policy_document_search.py, product_search.py, order_lookup.py,
    customer_information.py, rag.py, response_generation.py, guardrails_validation.py, human_handoff.py,
    response_delivery.py, memory_persistence.py}
src/converse/ai/providers/{base.py, openai.py, anthropic.py, gemini.py, models.py}
src/converse/ai/rag/{retriever.py, chunker.py, extractors.py, embedder.py, fusion.py}
src/converse/ai/tools/{base.py, customer_info.py, product_search.py, order_lookup.py, integration_client.py}
src/converse/ai/memory/{history.py, sessions.py, summarizer.py, facts.py}
src/converse/ai/guardrails/{checks.py, moderation.py, grounding.py, pii.py}
src/converse/ai/prompts/{platform_v1.txt, intent_v1.txt, grounding_v1.txt, summary_v1.txt}
src/converse/modules/knowledge/{models.py, repository.py, service.py, ingestion.py, schemas.py}
src/converse/modules/ai_config/{models.py, repository.py, service.py, schemas.py}   # ai_agents, credentials, inbox links
src/converse/api/routes/{ai.py, knowledge.py}
src/converse/workers/tasks/{ai.py, knowledge.py}
config/llm_models.yaml
tests/unit/ai/nodes/*, tests/integration/ai/*, tests/eval/{datasets/*.json, run_eval.py}
```

(`modules/ai_config` holds configuration CRUD so `modules/` stays free of LangGraph; `ai/` holds runtime.)

## 4. Database tables
`ai_provider_credentials`, `ai_agents`, `ai_agent_inboxes`, `knowledge_bases`, `ai_agent_knowledge_bases`, `kb_documents`, `kb_chunks`, `ai_conversation_sessions`, `ai_runs`, `ai_tool_calls`, `usage_records`; writes `messages`, `conversations`, `conversation_assignments`, `reporting_events`.

## 5. APIs
[../backend/API_DESIGN.md](../backend/API_DESIGN.md) §22 (AI configuration, playground, AI runs), §23 (knowledge bases), plus `POST /conversations/{id}/ai/pause|resume` (§14).

## 6. Dependencies
Phases 3–5 (messages, channels, realtime). Libraries: `langgraph`, `langchain-core`, `langchain-openai`, `langchain-anthropic` (optional `langchain-google-genai`), `tiktoken`, `pgvector` (SQLAlchemy type), `pypdf`/`pdfplumber`, `python-docx`, `readability-lxml`, `markdownify`, `langdetect` or `lingua`. Verify current versions, maintenance and model availability/deprecations before pinning.

## 7. Implementation steps
1. Config CRUD: credentials (validate with a cheap test call; store encrypted + last4), agents (validate models against `llm_models.yaml`), inbox links (one agent per inbox), KB links.
2. Knowledge: KB + document CRUD; `ingest_kb_document` task (fetch via `SafeHttpClient`, extract, hash, chunk, embed, atomic replace); resync task.
3. Retriever: vector + keyword queries, RRF fusion, thresholds, `hnsw.ef_search` / `iterative_scan` settings; isolation test.
4. Provider factory and `LLMProvider` wrappers; structured-output helpers with one repair retry.
5. State, edges and graph build exactly as in [../ai/LANGGRAPH_WORKFLOW.md](../ai/LANGGRAPH_WORKFLOW.md).
6. Nodes one by one, each with unit tests using fake providers (deterministic responses) and fake retrievers.
7. Runner: `ai_runs` idempotency, deps per tenant, deadline, fallback handoff.
8. Trigger handler (`message.created`), debounce key, per-conversation lock, per-org concurrency counter, quota check.
9. `ConversationService.handoff_to_human`, takeover rules in `MessageService`/`ConversationService` (human reply or assignment → `ai_active = false`), `resume_ai`.
10. Memory persistence, summariser task, facts merge.
11. Usage metering + plan quota enforcement.
12. Playground endpoint (runs the graph with delivery replaced by "return the draft").
13. Eval harness: datasets per test KB, metrics (recall@8, groundedness, refusal correctness, handoff precision); run in CI nightly with recorded/cheap models.

## 8. Testing requirements
- Node unit tests: inputs → outputs for every node, including error branches.
- Edge tests: routing table for intents/confidence/turn limits/tool enablement.
- Discard: newer customer message during generation → no reply; human assigned during generation → no reply.
- Idempotency: same `message.created` handled twice → one `ai_runs` row, one reply.
- Handoff: every trigger in [../ai/HUMAN_HANDOFF.md](../ai/HUMAN_HANDOFF.md#3-handoff-triggers) produces the handoff effects (status, ai_active, team, note, events).
- Retrieval isolation across tenants and across KBs not linked to the agent.
- Prompt-injection suite: documents and messages containing "ignore previous instructions…" don't change tool use, don't leak prompts; guardrails flag leaks.
- Private notes never appear in LLM inputs (assert on the fake provider's received messages).
- Quota exhausted → handoff/no reply per policy; BYO key usage not counted against quota.
- Eval thresholds: groundedness ≥ 0.9 on the internal dataset; no answer on unanswerable questions ≥ 0.9.

## 9. Expected output
On a WhatsApp or widget inbox with an AI agent and a KB containing a refund policy: "how long do refunds take?" gets a correct, grounded answer in < 8 s p95; "I want to talk to a person" hands off to the configured team with an internal summary note.

## 10. Completion criteria
- [ ] All tests and eval thresholds pass.
- [ ] AI activity visible per conversation; metrics and alerts live.
- [ ] Cost per AI reply measured on staging and documented for plan pricing.
- [ ] No module under `modules/` imports `ai/`.
