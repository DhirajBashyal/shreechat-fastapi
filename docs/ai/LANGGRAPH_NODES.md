# LangGraph Nodes

Each node is an `async def node(state: AgentState, config: RunnableConfig) -> dict` that returns **only the keys it changes**. Dependencies come from `config["configurable"]["deps"]`. Nodes live in `converse/ai/graph/nodes/<name>.py`, one per file, each with unit tests that call it with a hand-built state and fake dependencies.

Legend: **R** = required (every run), **O** = optional (router/tool-enabled), **C** = conditional (only on certain outcomes).

## Summary

| # | Node | Kind | Uses LLM | Reads | Writes |
|---|---|---|---|---|---|
| 1 | `input_validation` | R | No | trigger message | `input_text`, `outcome=discard`? |
| 2 | `tenant_context` | R | No | ai_agents, credentials, quota | `agent`, `handoff_reason`? |
| 3 | `conversation_context` | R | No | conversation, messages, session | `conversation`, `history`, `summary`, `facts` |
| 4 | `customer_identification` | R | No | contact, identity | `customer` |
| 5 | `intent_classification` | R | Yes (small model) | history, input | `intent` |
| — | router | edge | No | intent, policy | (routing only) |
| 6 | `knowledge_retrieval` | O | Embeddings | kb_chunks | `tool_results` |
| 7 | `policy_document_search` | O | Embeddings | kb_chunks (policy docs) | `tool_results` |
| 8 | `product_search` | O | No | commerce integration | `tool_results` |
| 9 | `order_lookup` | O | No | commerce integration | `tool_results` |
| 10 | `customer_information` | O | No | contact, past conversations | `tool_results` |
| 11 | `rag` | O | Optional (rerank) | tool_results | `context_block`, `citations`, `context_sufficient` |
| 12 | `response_generation` | R* | Yes | everything above | `draft`, `usage` |
| 13 | `guardrails_validation` | R* | Optional (moderation, grounding) | draft, context | `guardrail_flags`, `guardrail_attempts` |
| 14 | `human_handoff` | C | No (template) | policy, reason | `outcome=handoff`, `draft` (handoff message) |
| 15 | `response_delivery` | R | No | conversation (locked) | `outcome`, `reply_message_id` |
| 16 | `memory_persistence` | R | Yes (summary, sometimes) | history, facts | session row |

\* Either `response_generation` → `guardrails_validation` or `human_handoff` runs.

---

## 1. `input_validation` (required)

| | |
|---|---|
| **Purpose** | Cheap checks before spending money |
| **Receives** | `trigger_message_id`, `conversation_id` |
| **Does** | Loads the trigger message and all unanswered incoming messages after the last outgoing one. Discards if: message deleted; content empty and no attachments; only stickers/reactions; content > 4,000 chars (truncate to 4,000 with a note rather than discard); message is a duplicate of the previous one within 10 s. Attachments: images are described as `[image]` (vision support is Future); audio is `[voice message]`, or the transcript when transcription is enabled (R2) |
| **Returns** | `{"input_text": "..."}` or `{"outcome": "discard"}` |
| **Errors** | Missing message → discard (it was deleted) |

## 2. `tenant_context` (required)

| | |
|---|---|
| **Purpose** | Load and freeze the tenant's AI configuration for this run |
| **Receives** | `organization_id`, `workspace_id`, `ai_agent_id` |
| **Does** | Loads `ai_agents` (+ KB ids, enabled tools), validates the model against the allow-list, checks the plan's `ai_agent` feature and monthly `ai_responses` quota, checks inbox working hours against `handoff_policy.outside_hours` |
| **Returns** | `{"agent": AgentConfigView}`; or `{"agent": ..., "handoff_reason": "quota_exhausted" \| "agent_disabled" \| "outside_hours"}` |
| **Notes** | The provider instance is built by the deps factory from the same config (it's not in state) |

## 3. `conversation_context` (required)

| | |
|---|---|
| **Purpose** | Give the model the conversation so far within a token budget |
| **Receives** | `conversation_id` |
| **Does** | Loads the conversation view (status, channel, inbox, labels, custom attributes, `last_incoming_at`, AI turn count). Loads `ai_conversation_sessions` (summary, `summarized_through_seq`, facts). Loads messages with `sequence_number > summarized_through_seq`, excluding private notes and activity messages, newest last, until a 3,000-token budget is reached. Maps senders to roles: contact → `user`, AI → `assistant`, human agent → `assistant` with a `[human agent]` prefix (so the model knows a person said it) |
| **Returns** | `{"conversation", "history", "summary", "facts"}` |
| **Notes** | Private notes are **never** sent to the LLM. They often contain internal reasoning the customer must not see |

## 4. `customer_identification` (required)

| | |
|---|---|
| **Purpose** | Know who the customer is and what we may reveal to them |
| **Receives** | `conversation.contact_id`, `conversation.contact_identity_id` |
| **Does** | Loads the contact (name, email presence, phone presence, `contact_type`, custom attributes whose definition has `ai_visible = true`) and computes the customer's verification level (`none` / `channel` / `verified`) from the identity's `identity_verified` flag, the channel, and any unexpired step-up verification in `ai_conversation_sessions`. Rules in [AI_ROLES_AND_PERMISSIONS.md §4](AI_ROLES_AND_PERMISSIONS.md#4-what-a-customer-may-obtain-through-the-ai) |
| **Returns** | `{"customer": CustomerView(name, first_name, verification_level, verification_method, attributes, language_hint)}` |
| **Notes** | Email/phone values are **not** included in the prompt unless a tool needs them; only presence flags are |

## 5. `intent_classification` (required)

| | |
|---|---|
| **Purpose** | Decide what the customer wants and what's needed to answer |
| **Receives** | `input_text`, last 6 turns of `history`, `summary`, `agent.enabled_tools` |
| **Does** | One structured-output call on `agent.classifier_model` (falls back to `chat_model`), temperature 0 |
| **Returns** | `{"intent": Intent}`: `name`, `confidence`, `language`, `sentiment`, `needs` ⊆ {`knowledge`, `policy`, `product`, `order`, `customer`}, `search_query` (a standalone rewrite: "where is it?" → "order shipping status for order placed last week") |
| **Errors** | Invalid JSON → one repair retry → fallback intent `unknown`, confidence 0 |

```python
class IntentOutput(BaseModel):
    name: Literal["greeting", "smalltalk", "thanks", "product_question", "order_status", "refund_policy",
                  "shipping_policy", "account_help", "complaint", "human_request", "other"]
    confidence: float = Field(ge=0, le=1)
    language: str
    sentiment: Literal["positive", "neutral", "negative"]
    needs: list[Literal["knowledge", "policy", "product", "order", "customer"]]
    search_query: str | None
```

Tenants can add custom intents (R2) as extra labels with descriptions; the enum becomes dynamic per agent.

## Router (conditional edge, not a node)

See [LANGGRAPH_WORKFLOW.md §4](LANGGRAPH_WORKFLOW.md#4-conditional-edges). Order of checks: customer asked for a human → configured handoff intents → low confidence → turn limit → smalltalk direct to generation → fan-out to enabled tool nodes.

## 6. `knowledge_retrieval` (optional)

| | |
|---|---|
| **Purpose** | Find relevant knowledge chunks |
| **Receives** | `intent.search_query` (or `input_text`), `agent.knowledge_base_ids` |
| **Does** | Hybrid search (vector + full-text, fused with RRF) over `kb_chunks` restricted to the agent's KBs, top 8 (details in [RAG_ARCHITECTURE.md](RAG_ARCHITECTURE.md)) |
| **Returns** | `{"tool_results": [ToolResult(tool="knowledge_search", data=[chunks...], sources=[...])]}` |
| **Errors** | Embedding API failure → `ok=False`, falls back to full-text-only results if any |

## 7. `policy_document_search` (optional)

Same as `knowledge_retrieval`, restricted to documents with `metadata.category = 'policy'` (refunds, shipping, privacy, terms) and with a higher similarity threshold. Policy answers must quote the document; `guardrails_validation` enforces citations for policy intents even when `require_citations` is off.

## 8. `product_search` (optional, R2)

| | |
|---|---|
| **Receives** | `intent.search_query`, integration config |
| **Does** | `IntegrationClient.search_products(query, limit=5)` through the tenant's commerce integration (timeout 5 s) |
| **Returns** | `ToolResult(data=[{"title", "price", "currency", "in_stock", "url"}])` |
| **Notes** | Prices and stock come only from this tool, never from the model's memory |

## 9. `order_lookup` (optional, R2)

| | |
|---|---|
| **Receives** | `customer`, `facts.order_ids`, order ids extracted by the classifier |
| **Does** | If the customer isn't verified and no order id + matching email/phone pair is available → returns `ok=False, error="verification_required"` (generation asks for the order number and email). Else `IntegrationClient.get_order(order_id, customer_ref)` |
| **Returns** | `ToolResult(data={"order_id", "status", "items", "shipping": {"carrier", "tracking_url", "eta"}})` |
| **Security** | Never returns orders that don't belong to the conversation's contact |

## 10. `customer_information` (optional)

| | |
|---|---|
| **Receives** | `customer`, `conversation.contact_id` |
| **Does** | Loads AI-visible custom attributes (plan, signup date), labels, and summaries of the last 3 resolved conversations (from their AI sessions or a stored resolution summary) |
| **Returns** | `ToolResult(data={"attributes": {...}, "recent_topics": [...]})` |

## 11. `rag` (optional; runs when any tool node ran)

| | |
|---|---|
| **Purpose** | Turn raw tool output into a compact, cited context block and decide if it's enough |
| **Receives** | `tool_results` |
| **Does** | Deduplicates chunks; optional rerank (cross-encoder or LLM rerank on the top 8 → top 4) when the KB is large; renders `<context>` with numbered sources `[1] Refund policy — "Refunds are processed within 5–7 business days…"`; computes `context_sufficient` = at least one chunk above the similarity threshold, or a successful structured tool result |
| **Returns** | `{"context_block", "citations", "context_sufficient"}` |

## 12. `response_generation` (required unless handoff)

| | |
|---|---|
| **Receives** | platform + tenant prompt, `summary`, `facts`, `history`, `input_text`, `context_block`, `intent.language`, `customer.first_name`, channel constraints |
| **Does** | One call to `agent.chat_model`. Channel constraints added to the prompt: WhatsApp/Instagram/Messenger → plain text, ≤ 1,000 chars, no Markdown tables; widget → Markdown allowed. If `context_sufficient` is false, instructs the model to say it doesn't have that information and offer a human. On regeneration, includes the guardrail feedback ("previous draft was too long / made an unsupported claim about X") |
| **Returns** | `{"draft": str, "citations": [...] (ids the model referenced), "usage": [{node, input_tokens, output_tokens}]}` |
| **Output format** | Structured: `{"reply": str, "used_sources": [int], "wants_handoff": bool}`. `wants_handoff = true` → guardrails routes to handoff with reason `model_requested` |

## 13. `guardrails_validation` (required after generation)

Checks, in order (cheap first):

| Check | Method | Flag |
|---|---|---|
| Length vs `guardrails.max_reply_chars` and channel limit | code | `too_long` |
| System prompt / instruction leakage (n-gram overlap with the platform/tenant prompt) | code | `prompt_leak` |
| PII of other people (emails/phones not belonging to this customer, card numbers) | regex + allow-list of this contact's own values | `pii_leak` |
| Blocked topics from `guardrails.blocked_topics` | small classifier call | `blocked_topic` |
| Unsafe content | provider moderation API | `unsafe` |
| Grounding: claims supported by context (when `require_citations` or a policy intent) | LLM judge on `chat_model`/`classifier_model` with the draft + context → list of unsupported claims | `unsupported_claim` |
| Language matches `intent.language` (unless persona forces one) | language detection | `wrong_language` |
| Human impersonation ("I'm a real person") | regex/classifier | `unsafe` |

**Returns** `{"guardrail_flags": [...], "guardrail_attempts": n+1}`. Routing: fixable flags → regenerate (max 2), blocking flags → handoff.

## 14. `human_handoff` (conditional)

| | |
|---|---|
| **Receives** | `handoff_reason` (or derived from route/flags), `agent.handoff_policy`, `intent.language` |
| **Does** | Prepares the handoff: the customer-facing message (tenant-configurable per language, default "I'm connecting you with a member of our team. They'll reply here shortly."), the target team (`handoff_policy.assign_team_id`), and an internal private note for agents summarising the conversation, intent and why the AI handed off |
| **Returns** | `{"outcome": "handoff", "handoff_reason": ..., "draft": handoff_message}` |
| **Notes** | Applying the handoff happens in `response_delivery` (same locking and discard rules). Details: [HUMAN_HANDOFF.md](HUMAN_HANDOFF.md) |

## 15. `response_delivery` (required)

| | |
|---|---|
| **Purpose** | Make the run's effect real exactly once, or discard it safely |
| **Does** | Opens a transaction; `SELECT … FOR UPDATE` the conversation; re-checks: `ai_active` still true, still no human assignee, no newer incoming message than the run's latest input (`last_incoming_at`/max sequence), deadline not passed. If any check fails → `outcome = discard`. Otherwise: reply → `MessageService.create_outgoing(sender_kind="ai_agent", content=draft, content_attributes={"ai": {"run_id", "citations"}}, client_message_id=f"ai:{run_id}")`; handoff → create the customer message (if configured), the private note, then `ConversationService.handoff_to_human(reason, team_id)`. Commits (outbox events: `message.created`, `conversation.handoff`) |
| **Returns** | `{"outcome": "reply" \| "handoff" \| "discard", "reply_message_id"}` |
| **Idempotency** | `client_message_id = "ai:{run_id}"` + unique index → a re-executed delivery can't double-send |

## 16. `memory_persistence` (required after delivery)

| | |
|---|---|
| **Does** | Updates `ai_conversation_sessions`: `turn_count += 1`; merges new facts (classifier-extracted order ids, language, product of interest) into `facts`; if unsummarised history exceeds ~2,500 tokens, enqueues `summarize_ai_session` (cheap model, async, not blocking the reply); on handoff sets `status = handed_off`, `handed_off_at`. Writes `usage_records (metric = ai_responses, source_id = run_id)` |
| **Returns** | `{}` |
| **Details** | [MEMORY_MANAGEMENT.md](MEMORY_MANAGEMENT.md) |
