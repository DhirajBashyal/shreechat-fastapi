# Memory Management

How the AI agent remembers a conversation without unbounded prompts, and without becoming a second source of truth for messages.

## 1. Memory tiers

| Tier | Lifetime | Stored in | Contents |
|---|---|---|---|
| **Working memory** | One graph run | LangGraph state (`AgentState`) | Loaded context, intent, tool results, draft |
| **Short-term memory** | The conversation | `messages` table (source of truth) | Recent turns, rebuilt for each run within a token budget |
| **Long-term conversation memory** | The conversation (and its AI session) | `ai_conversation_sessions.summary`, `summarized_through_seq`, `facts` | Rolling summary of older turns, extracted facts |
| **Customer memory** | Across conversations | `contacts.custom_attributes` (AI-visible ones), resolved conversations' summaries | Plan, preferences, past topics (read by `customer_information`) |
| **Knowledge** | Tenant-managed | `kb_chunks` | Not memory: business facts, retrieved by RAG |

**Key decision: messages are always read from the `messages` table.** Human agents, automations and other channels also write to the conversation. If the AI kept its own message list (e.g. in a LangGraph checkpoint), it would drift from what the customer actually saw. The checkpointer is therefore not used as conversation memory.

## 2. Building short-term memory for a run

```python
def build_history(messages: list[MessageView], budget_tokens: int = 3000) -> list[ChatTurn]:
    turns: list[ChatTurn] = []
    used = 0
    for m in reversed(messages):                       # newest first
        if m.is_private or m.direction == "activity":
            continue                                   # internal notes never reach the LLM
        role = "user" if m.sender_kind == "contact" else "assistant"
        text = m.content or describe_attachments(m.attachments)
        if m.sender_kind == "user":
            text = f"[human agent {m.sender_first_name}] {text}"
        cost = count_tokens(text)
        if used + cost > budget_tokens:
            break
        turns.append(ChatTurn(role=role, content=text, at=m.created_at))
        used += cost
    return list(reversed(turns))
```

Only messages with `sequence_number > summarized_through_seq` are loaded. Older ones are represented by the summary.

## 3. Rolling summary

- Triggered by `memory_persistence` when unsummarised turns exceed ~2,500 tokens (and on handoff, so the human gets a summary).
- Task `summarize_ai_session` (queue `ai`, low priority): loads the previous summary + messages from `summarized_through_seq` to the latest, asks the classifier model for an updated summary (≤ 250 words, factual, no PII beyond the first name), and saves `summary` + new `summarized_through_seq` with an optimistic check (`WHERE summarized_through_seq = :old`), so two summarisers can't overwrite each other.
- The summary is also shown to agents in the conversation sidebar ("AI summary").

## 4. Facts

`ai_conversation_sessions.facts` holds small structured memory, merged after each run:

```json
{
  "language": "es",
  "order_ids": ["1234"],
  "product_interest": ["blue hoodie"],
  "customer_stated_name": "Casey",
  "verified_for_orders": false
}
```

- Facts come only from structured outputs (classifier fields, tool results), never from free-form model text.
- Values are validated by a Pydantic `SessionFacts` model; unknown keys are dropped.
- Size cap: 4 KB.

## 5. LangGraph checkpointing

| Release | Checkpointer | Why |
|---|---|---|
| MVP / R1 | **None** (graph compiled without a checkpointer) | Runs are short (< 45 s), idempotent (unique `ai_runs` per trigger message) and safe to restart from scratch; nothing else needs resumption |
| R2 | `PostgresSaver` in a separate schema `langgraph` | Needed for interrupt/resume flows: copilot approvals, "ask the customer for verification then continue" across turns |

When the checkpointer is introduced:
- `thread_id = "org:{organization_id}:conv:{conversation_id}:agent:{ai_agent_id}"` (stored in `ai_conversation_sessions.thread_id`).
- The `langgraph.*` tables don't have `organization_id` columns, so they can't use our RLS policy. They are accessed **only** by the AI worker through `converse_system`, keyed by thread ids that embed the organization id, and never exposed through the API.
- Checkpoints older than 7 days are pruned. They're operational state, not memory.
- Message history in checkpoints is still not used as the history source (see §1).

## 6. Memory and tenant isolation

- Every memory read/write goes through tenant-scoped repositories (`ai_conversation_sessions` has RLS).
- Summaries and facts of one conversation are never used in another, except through `customer_information` for the **same contact** in the same workspace.
- No vector memory of conversations in MVP. If "semantic memory across conversations" is added later, it gets its own table with `organization_id`, `workspace_id`, `contact_id` and RLS, never mixed into `kb_chunks`.

## 7. Retention and deletion

- Sessions are deleted with their conversation (`ON DELETE CASCADE`).
- `ContactErasureService` clears summaries and facts of the contact's conversations.
- Tenants can set `organizations.settings.data_retention_days`; the purge job deletes AI sessions and runs together with old conversations.
