# 07 — AI Agents (Frontend Implementation)

Screens for the AI agent that answers customers: AI provider keys (organization), agent settings (workspace), the **playground** to test an agent, the **AI activity** panel on a conversation ("why did the AI say that?"), and the **Pause AI / Return to AI** controls.

Prerequisites: [02](02_ROLES_AND_PERMISSIONS.md) (`can()`), [05](05_ORGANIZATION_LEVELS.md) (routes, guards), [06](06_KNOWLEDGE_BASES.md) (knowledge bases the agent searches). Backend: [../ai/](../ai/), API in [../backend/API_DESIGN.md §22](../backend/API_DESIGN.md#22-ai-configuration--apiv1organizationsorgai-credentials-apiv1workspaceswsai-agents).

## 1. Who sees what

| Screen / action | Permission | Owner | Admin | Agent (default) |
|---|---|---|---|---|
| AI provider keys (org settings) | `ai.credentials.manage` | ✅ | ✅ | ❌ |
| AI agents list + read-only detail | member | ✅ | ✅ | ✅ (no prompt/policies: they're `null`) |
| Create / edit / delete agent, link inboxes and knowledge bases | `ai.agent.manage` | ✅ | ✅ | ❌ |
| Enable `customer_info` tool | also `custom_attribute.manage` | ✅ | ✅ | ❌ |
| Allow `update_contact` action | also `contact.manage` | ✅ | ✅ | ❌ |
| Playground | `ai.playground.use` | ✅ | ✅ | ❌ |
| AI activity panel on a conversation | `ai.activity.view` + can see the conversation | ✅ | ✅ | ✅ |
| Source text in the activity panel | also `ai.knowledge.manage` | ✅ | ✅ | ❌ (titles only) |
| Pause AI / Return to AI | `conversation.ai_control` + can see the conversation | ✅ | ✅ | ✅ |

## 2. Routes

```text
/orgs/:orgId/settings/ai-keys                    provider keys (ai.credentials.manage)
/orgs/:orgId/ws/:wsId/ai-agents                  list (member)
/orgs/:orgId/ws/:wsId/ai-agents/new              create (ai.agent.manage)
/orgs/:orgId/ws/:wsId/ai-agents/:agentId         settings tabs (§5) — read-only view for others
/orgs/:orgId/ws/:wsId/ai-agents/:agentId/playground   (ai.playground.use)
```

The AI activity panel and pause/resume live inside the conversation screen (Phase 3); build them as components now (§7, §8).

## 3. Types and endpoints

```ts
export type AiProvider = "openai" | "gemini" | "anthropic";
export type AiAction = "reply" | "handoff" | "add_label" | "set_priority" | "resolve" | "snooze" | "update_contact" | "add_private_note";
export type AiTool = "knowledge_search" | "policy_search" | "customer_info" | "product_search" | "order_lookup";
export type Verification = "none" | "channel" | "verified";

export interface AiCredential {
  id: string; provider: AiProvider; label: string;
  api_key_last4: string;            // the key itself is never returned
  created_at: string;
}

export interface AiModel {
  provider: AiProvider; model: string; context_window: number; supports_tools: boolean;
  deprecated_at: string | null;     // "YYYY-MM-DD"
  selectable: boolean;              // false after deprecated_at: show greyed out, keep if already set
}

export interface AiModelsResponse {
  platform_provider: AiProvider;    // used by agents without their own key
  platform_configured: boolean;     // false: the server has no AI key
  default_chat_model: string;
  models: AiModel[];
}

export interface HandoffPolicy {
  on_customer_request: boolean;
  on_intents: string[];             // e.g. ["complaint"]
  on_low_confidence: number;        // 0..1, default 0.55
  on_no_answer: "handoff" | "answer";
  on_negative_sentiment_streak: number;   // 0 = off
  on_ai_error: "handoff" | "ignore";
  outside_hours: "ai_continues" | "handoff";
  assign_team_id: string | null;
  handoff_message: Record<string, string>;  // language → text
  post_internal_note: boolean;
}

export interface Guardrails {
  max_reply_chars: number;          // 100..10000, default 1500
  blocked_topics: string[];
  require_citations: boolean;
  pii_redaction: boolean;
  show_sources: boolean;
}

export interface Persona {
  name: string; tone: string;
  language_policy: "match_customer" | "fixed";
  language: string | null;
}

export interface AiAgent {
  id: string; workspace_id: string; name: string; description: string | null; is_active: boolean;
  provider: string; provider_credential_id: string | null;
  chat_model: string; classifier_model: string | null; temperature: number;
  enabled_tools: AiTool[]; allowed_actions: AiAction[]; max_turns_before_handoff: number;
  inboxes: { inbox_id: string; mode: "autonomous" | "assist_only" }[];
  knowledge_base_ids: string[];
  // null unless the viewer has ai.agent.manage:
  system_prompt: string | null; persona: Persona | null; guardrails: Guardrails | null;
  handoff_policy: HandoffPolicy | null;
  tool_settings: Record<string, unknown> | null;
  created_at: string; updated_at: string;
}

export interface PlaygroundResponse {
  outcome: "reply" | "handoff" | "discard" | "failed";
  reply: string | null;             // the reply, or the handoff message
  handoff_reason: string | null;
  intent: { name: string; confidence: number; language: string; sentiment: string; needs: string[]; search_query: string | null } | null;
  route: string[];                  // tools used, ["response_generation"], or ["human_handoff"]
  citations: { n: number; chunk_id: string; document_id: string; title: string; url: string | null }[];
  context_sufficient: boolean | null;
  guardrail_flags: string[];
  tool_calls: ToolCall[];
  usage: { node: string; model: string; input_tokens: number; output_tokens: number }[];
  errors: { node: string; error: string }[];
}

export interface ToolCall {
  tool_name: string; arguments: Record<string, unknown>;
  policy_decision: "allowed" | "denied" | "needs_verification" | "needs_approval";
  denial_reason: string | null; succeeded: boolean; result_excerpt: string | null; latency_ms: number | null;
}

export interface AiRun {
  id: string; status: "running" | "replied" | "handed_off" | "discarded" | "failed";
  intent: string | null; route: string | null; handoff_reason: string | null;
  model: string; input_tokens: number; output_tokens: number; latency_ms: number | null;
  guardrail_flags: string[]; error: string | null;
  sources: { chunk_id: string; title: string; content?: string }[];   // content only with ai.knowledge.manage
  tool_calls: ToolCall[];
  started_at: string; finished_at: string | null;
}
```

| Method | URL (`/api/v1` +) | Body | Success |
|---|---|---|---|
| GET | `/ai-models` | — | `AiModelsResponse` (any logged-in user) |
| GET | `/organizations/{org}/ai-credentials` | — | `AiCredential[]` |
| POST | `/organizations/{org}/ai-credentials` | `{ provider, label, api_key }` | `201 AiCredential` |
| DELETE | `/organizations/{org}/ai-credentials/{id}` | — | `204` |
| GET | `/workspaces/{ws}/ai-agents` | — | `AiAgent[]` |
| POST | `/workspaces/{ws}/ai-agents` | `{ name, chat_model?, …any AiAgent field }` | `201 AiAgent` |
| GET | `/workspaces/{ws}/ai-agents/{id}` | — | `AiAgent` |
| PATCH | `/workspaces/{ws}/ai-agents/{id}` | changed fields only | `AiAgent` |
| DELETE | `/workspaces/{ws}/ai-agents/{id}` | — | `204` |
| PUT | `/workspaces/{ws}/ai-agents/{id}/inboxes` | `{ inboxes: [{ inbox_id, mode }] }` (replaces) | `AiAgent` |
| PUT | `/workspaces/{ws}/ai-agents/{id}/knowledge-bases` | `{ knowledge_base_ids: [] }` (replaces) | `AiAgent` |
| POST | `/workspaces/{ws}/ai-agents/{id}/playground` | `{ message, history?, customer_verification?, customer_name? }` | `PlaygroundResponse` |
| GET | `/workspaces/{ws}/conversations/{id}/ai-runs` | — | `AiRun[]` (newest first, 50) |
| POST | `/workspaces/{ws}/conversations/{id}/ai/pause` | — | `{ conversation_id, ai_active, status }` |
| POST | `/workspaces/{ws}/conversations/{id}/ai/resume` | — | same |

## 4. AI provider keys (`/settings/ai-keys`)

Table: label, provider, `•••• {api_key_last4}`, created date, **Delete**.

**Add key** dialog: Provider (OpenAI / Google Gemini / Anthropic), Label, API key (password field, never stored in the frontend after submit). After `201`, clear the form: the key is never shown again.

Delete confirm: "Agents using this key switch to the platform key. Continue?"

| Error | Show |
|---|---|
| `402 feature_not_in_plan` (`feature: "byo_llm_keys"`) | "Your plan doesn't include your own AI keys. The platform's AI is used." Hide the Add button after this |
| `409` | "A key with this label already exists." |
| `422` | Field errors (empty label or key, unknown provider) |

## 5. Agent settings (`/ai-agents/:agentId`)

**Platform status:** load `GET /ai-models` once per session. When `platform_configured` is false, show a banner on the AI agents pages: "No AI key is set up on the server. Agents without their own key can't answer." (Owners/admins with `ai.credentials.manage`: add a link to AI keys.)

**List:** name, Active/Off badge, model, number of inboxes and knowledge bases. **New agent**: name + chat model; everything else gets safe defaults (actions reply + handoff, tool knowledge search, default handoff and guardrails).

Tabs; save each tab with `PATCH` of only its fields. Read-only users (no `ai.agent.manage`) see General, Inboxes and Knowledge only, with no edit controls.

| Tab | Fields | Notes |
|---|---|---|
| General | Name, description, **Active** toggle, Provider key ("Platform ({platform_provider})" or one of the org's keys), Chat model, Classifier model (optional, cheaper), Temperature (0–2) | Model dropdowns come from `GET /ai-models`, filtered to the provider of the selected key (platform key → `platform_provider`). Show models with `selectable: false` greyed out with "retired {deprecated_at}"; keep showing the current value if the agent already uses one. New agents default to `default_chat_model` |
| Instructions | Persona name, tone, language policy (match customer / fixed + language), **System prompt** (business instructions, ≤ 20,000 chars) | Show: "Platform rules always apply: the AI never claims to be human and only answers from your knowledge." |
| Knowledge | Multi-select of the workspace's knowledge bases → `PUT …/knowledge-bases` | Link to [06](06_KNOWLEDGE_BASES.md) to add content |
| Inboxes | Multi-select of inboxes, mode Autonomous (Assist-only 🔜) → `PUT …/inboxes` | `409` → "{inbox} already has another AI agent." |
| Tools & actions | Tool checkboxes: Knowledge search, Policy search, Customer info, Product search 🔜, Order lookup 🔜. Action checkboxes: Reply, **Handoff (always on, disabled)**, others 🔜 (accepted but not executed yet: show them disabled with "coming soon") | Disable **Customer info** without `custom_attribute.manage` ("Requires permission to manage custom attributes"); disable **Update contact** without `contact.manage` |
| Tool policies | Per tool: "Require verified customer" (only raises the level), "Max calls per run" (≤ default), Allowed labels (for add_label) | The API rejects loosening with `policy_cannot_be_relaxed` |
| Handoff | Hand off when customer asks for a person; on intents (chips: complaint, …); confidence threshold slider (0–1); when no answer found (hand off / say I don't know); negative-sentiment streak (0 = off); on AI error; team to assign; handoff message per language; post internal note | |
| Guardrails | Max reply length, blocked topics (chips), require citations, show sources | |

Delete agent: "Delete {name}? It stops answering in all its inboxes."

| Error (`422` code) | Show on |
|---|---|
| `handoff_required` | Actions: "The AI must always be able to hand off." (shouldn't happen: checkbox disabled) |
| `action_not_supported`, `tool_not_supported` | Tools & actions |
| `policy_cannot_be_relaxed` | Tool policies field from the message |
| `label_not_in_workspace` | Allowed labels |
| `model_not_allowed` | Model field |
| `validation_error` | Field from the message (e.g. `handoff_policy.on_low_confidence`) |
| `403` with `permission` | "You need permission to {permission} to change this." |

## 6. Playground (`/ai-agents/:agentId/playground`)

A chat box that runs the real agent with the real knowledge, but **nothing is sent to customers and nothing is saved**. It costs tokens; say so under the input.

- Left: chat. Each send posts `{ message, history }` where `history` is the previous turns of this playground session (max 20). Append the user turn and `reply` (as assistant) on success.
- Settings strip: **Customer verification** (none / channel / verified) to test tools that need it; customer first name.
- Right panel per answer:
  - Outcome badge: **Reply** / **Handed off: {handoff_reason}** (show the handoff message as the reply, styled as a system bubble).
  - Intent: name, confidence %, language, sentiment.
  - Sources: `citations` (`[n] title`, link to the knowledge doc).
  - "Knowledge was enough": `context_sufficient`.
  - Tool calls: name + decision chip (Allowed / Denied / Needs verification), with `denial_reason`.
  - Guardrail flags, if any (e.g. `too_long` regenerated).
  - Tokens: sum of `usage`.

| Error | Show |
|---|---|
| `503 ai_provider_not_configured` | "No AI key is set up. Ask your administrator to add one under AI keys (or set it on the server)." |
| `outcome: "failed"` / `errors[]` | "The AI couldn't answer: {first error}." |
| `403` | Page guard ([02 §5.3](02_ROLES_AND_PERMISSIONS.md#53-protecting-pages)) |

Handoff reasons to human text: `token_budget` → "used its token budget for this message", `outside_hours` → "outside working hours", `customer_request` → "customer asked for a person", `no_answer` → "no answer in the knowledge base", `low_confidence` → "not sure what the customer wants", `intent:<name>` → "intent {name} is set to hand off", `turn_limit`, `negative_sentiment`, `model_requested`, `guardrail:<flag>` → "safety check: {flag}", `quota_exhausted`, `agent_disabled`, `ai_unavailable`, `ai_timeout`.

## 7. AI activity panel (conversation sidebar)

Shown when `can("ai.activity.view")`. `GET …/ai-runs` when the panel opens and on realtime `conversation.updated` (Phase 5).

Per run (newest first): time, status badge (Replied / Handed off / Discarded / Failed), intent, route, handoff reason (human text, §6), sources (titles; expandable text only when `content` is present), tool calls with decisions, guardrail flags, tokens and latency, error if failed.

`404` → the user can't see this conversation any more; close the panel.

## 8. Pause AI / Return to AI (conversation header)

Shown when `can("conversation.ai_control")`.

| Conversation state | Button | Call | After |
|---|---|---|---|
| `ai_active: true` | **Pause AI** | `POST …/ai/pause` | Badge "Human" |
| `ai_active: false` | **Return to AI** | `POST …/ai/resume` | Badge "AI"; note: "The AI answers the customer's next message." Conversation becomes unassigned and pending |

Replying as a human or assigning someone also turns the AI off (backend, Phase 3); refresh the badge from the conversation.

## 9. Acceptance checklist

Backend: `make migrate && make seed`, `make embeddings` (port 8001), `make ai-worker`, and an AI key in `.env`: `AI_PROVIDER` (`openai` / `gemini` / `anthropic`), `AI_API_KEY`, `AI_CHAT_MODEL` (a model from `GET /ai-models`). Password `Password123!`.

| # | As | Do | Expected |
|---|---|---|---|
| 1 | owner | Create agent "Ava" | Defaults: Reply + Handoff, Knowledge search |
| 2 | owner | Link the "Help center" KB from [06](06_KNOWLEDGE_BASES.md) | Knowledge tab shows it |
| 3 | owner | Playground: "how long do refunds take?" | Reply with `[1] Refund policy` source, intent refund_policy |
| 4 | owner | Playground: "I want to talk to a person" | Handed off: customer asked for a person, handoff message shown |
| 5 | owner | Playground: a question the KB doesn't cover | Handed off: no answer (or "I don't know" if set to answer) |
| 6 | owner | Enable Customer info; playground "my account" with verification none, then channel | Needs verification, then Allowed |
| 7 | owner | Set customer_info "Require verified" lower than default via the API | `policy_cannot_be_relaxed` message |
| 8 | owner | Untick Handoff | Not possible (disabled) |
| 9 | agent | Open the agent | Read-only, no prompt; no Playground link |
| 10 | owner | AI keys: add a key on the Business plan | "Your plan doesn't include your own AI keys" |
| 11 | owner | Unset `AI_API_KEY`, restart, open AI agents | "No AI key is set up" banner; playground shows the 503 message |
| 12 | owner | General tab: switch the provider key between platform and an Anthropic key | Model dropdown switches to that provider's models |
