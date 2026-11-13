# 06 — Knowledge Bases (Frontend Implementation)

Screens for the knowledge the AI agent answers from: knowledge bases per workspace, their documents (pasted text, FAQ pairs, web pages), the chunks each document was split into, and a **test search** that shows exactly what the AI would retrieve for a question.

Prerequisites: [02_ROLES_AND_PERMISSIONS.md](02_ROLES_AND_PERMISSIONS.md) (`can()`), [05_ORGANIZATION_LEVELS.md](05_ORGANIZATION_LEVELS.md) (route tree, `RequireWorkspace`). Backend: [../ai/RAG_ARCHITECTURE.md](../ai/RAG_ARCHITECTURE.md), API in [../backend/API_DESIGN.md §23](../backend/API_DESIGN.md#23-knowledge-bases--apiv1workspaceswsknowledge-bases).

All URLs are relative to `/api/v1/workspaces/{wsId}`.

## 1. Who sees what

| | Owner / Administrator | Agent (default) | Agent + custom role with `ai.knowledge.manage` |
|---|---|---|---|
| "Knowledge" menu item (workspace) | ✅ | ✅ (read-only list) | ✅ |
| Knowledge base list (names, counts) | ✅ | ✅ | ✅ |
| Create / rename / delete knowledge base | ✅ | ❌ | ✅ |
| Documents, add / resync / delete, view chunks | ✅ | ❌ | ✅ |
| Test search | ✅ | ❌ | ✅ |

Use `can(membership, "ai.knowledge.manage")` for everything except the list. An agent opening a knowledge base sees its name, description and counts, plus: "Only people who manage the AI's knowledge can see and change documents."

## 2. Routes

```text
/orgs/:orgId/ws/:wsId/knowledge                       list (every member)
/orgs/:orgId/ws/:wsId/knowledge/:kbId                 detail: documents (manage) or summary (others)
/orgs/:orgId/ws/:wsId/knowledge/:kbId/docs/:docId     chunks of one document (manage)
/orgs/:orgId/ws/:wsId/knowledge/search                test search (manage)
```

In `ORG_NAV`/workspace nav ([05 §5.1](05_ORGANIZATION_LEVELS.md#51-navigation)): `{ to: "knowledge", label: "Knowledge", show: () => true, ready: true }`. Guard `docs/:docId` and `search` with `RequirePermission("ai.knowledge.manage")`.

## 3. Types and endpoints

```ts
// src/api/types.ts
export interface KnowledgeBase {
  id: string; workspace_id: string; name: string; description: string | null;
  embedding_model: string;          // "paraphrase-multilingual-MiniLM-L12-v2"
  embedding_dimensions: number;     // 384
  chunk_size_tokens: number;        // 100
  chunk_overlap_tokens: number;     // 15
  document_count: number; chunk_count: number;
  created_at: string;
}

export type DocumentStatus = "pending" | "processing" | "ready" | "failed";

export interface KbDocument {
  id: string; knowledge_base_id: string; title: string;
  source_type: "text" | "faq" | "url";
  source_url: string | null;
  status: DocumentStatus;
  error: string | null;             // set when status = "failed"
  category: "general" | "policy";
  chunk_count: number;
  last_synced_at: string | null; created_at: string;
}

export type DocumentCreate =
  | { source_type: "text"; title: string; text: string; category?: "general" | "policy" }
  | { source_type: "faq"; title: string; faq: { question: string; answer: string }[]; category?: "general" | "policy" }
  | { source_type: "url"; url: string; title?: string; category?: "general" | "policy" };

export interface KbChunk {
  id: string; chunk_index: number; content: string; token_count: number;
  metadata: { heading_path?: string[]; source_url?: string | null; title?: string };
}

export interface SearchHit {
  chunk_id: string; document_id: string; knowledge_base_id: string;
  title: string; content: string; heading_path: string[]; source_url: string | null;
  similarity: number | null;        // 0..1, vector match (null = keyword-only hit)
  keyword_rank: number | null;      // null = vector-only hit
  score: number;                    // fused rank score, for ordering only
}

export interface SearchResponse {
  context_sufficient: boolean;      // would the AI consider this enough to answer?
  hits: SearchHit[];
  context_block: string;            // the exact text the AI receives
}
```

| Method | URL | Body | Success | Permission |
|---|---|---|---|---|
| GET | `/knowledge-bases` | — | `200 KnowledgeBase[]` | member |
| POST | `/knowledge-bases` | `{ name, description? }` | `201 KnowledgeBase` | manage |
| GET | `/knowledge-bases/{kbId}` | — | `200 KnowledgeBase` | member |
| PATCH | `/knowledge-bases/{kbId}` | `{ name?, description? }` | `200 KnowledgeBase` | manage |
| DELETE | `/knowledge-bases/{kbId}` | — | `204` | manage |
| GET | `/knowledge-bases/{kbId}/documents` | — | `200 KbDocument[]` | manage |
| POST | `/knowledge-bases/{kbId}/documents` | `DocumentCreate` | `201 KbDocument` (already `ready` or `failed`) | manage |
| POST | `/knowledge-bases/{kbId}/documents/{docId}/resync` | — | `200 KbDocument` | manage |
| DELETE | `/knowledge-bases/{kbId}/documents/{docId}` | — | `204` | manage |
| GET | `/knowledge-bases/{kbId}/documents/{docId}/chunks` | — | `200 KbChunk[]` | manage |
| POST | `/knowledge-search` | `{ query, knowledge_base_ids?, policy_only?, top_n? }` | `200 SearchResponse` | manage |

`manage` = `ai.knowledge.manage`.

**Adding a document is synchronous today:** the request returns after fetching, chunking and embedding, usually 1–5 s, longer for big pages. Show a spinner on the submit button and keep the dialog open until it returns. When a job queue is added (🔜), the response becomes `202` with `status: "pending"`; the polling in §5.4 already handles that, so build it now.

## 4. Knowledge base list (`/knowledge`)

Cards or table: **name**, description, "N documents · M chunks", created date. Empty state: "No knowledge yet. Add your help articles, policies or FAQs so the AI can answer from them." with **Create knowledge base** (manage only).

**Create / rename** dialog: Name (required, ≤ 255), Description (optional, ≤ 2000).

**Delete** confirm: "Delete {name}? Its {n} documents are removed and the AI stops using them. This can't be undone." → `DELETE` → remove from list.

## 5. Knowledge base detail (`/knowledge/:kbId`)

Header: name, description, counts, **Rename**, **Delete**, **Test search** (links to §7 with this KB pre-selected).

### 5.1 Documents table (manage)

| Column | From | Notes |
|---|---|---|
| Title | `title` | Links to chunks page; URL docs also show `source_url` (open in new tab) |
| Type | `source_type` | Icon: text / FAQ / link |
| Category | `category` | "Policy" badge for `policy` |
| Status | `status` | Badge: Ready (green), Processing / Pending (spinner), Failed (red, `error` in a tooltip and under the row) |
| Chunks | `chunk_count` | |
| Last synced | `last_synced_at` | relative time |
| Actions | | **Resync** (all types), **Delete** |

### 5.2 Add document dialog

Three tabs, one per `source_type`:

| Tab | Fields | Rules |
|---|---|---|
| **Text** | Title (required), Text (textarea, ≤ 500,000 chars), Category | Markdown headings (`#`, `##`) become sections; tell the user: "Use headings: each answer is found faster when it sits under a clear heading." |
| **FAQ** | Title (required), repeatable Question + Answer rows (≥ 1, ≤ 500; question ≤ 1000, answer ≤ 10,000), Category | "+ Add question" button; remove row |
| **Web page** | URL (required, `https://…`), Title (optional: the page title is used), Category | One page only (site crawl 🔜). Public pages only |

Category: radio "General" / "Policy" with help text: "Policy documents (refunds, shipping, privacy, terms) are what the AI quotes when customers ask about rules."

After `201`:
- `status === "ready"` → close the dialog, toast "Added {title}: {chunk_count} chunks", prepend the row.
- `status === "failed"` → keep the row (it exists), show the `error` (e.g. "Could not fetch URL: HTTP 404", "Embedding service failed: …"). The user can fix the cause and **Resync**, or delete it.

### 5.3 Resync and delete

- **Resync** → `POST …/resync` → replace the row with the response. For URL documents this fetches the page again; for text/FAQ it re-embeds.
- **Delete** confirm: "Delete {title}? The AI stops using it immediately." → `204` → remove row; decrement counts.

### 5.4 Status polling (ready for the job queue)

```ts
/** Re-fetch the documents list while any document is still being processed. */
export function useDocumentsPolling(wsId: string, kbId: string, docs: KbDocument[], refetch: () => void) {
  const busy = docs.some((d) => d.status === "pending" || d.status === "processing");
  useEffect(() => {
    if (!busy) return;
    const t = setInterval(refetch, 3000);
    return () => clearInterval(t);
  }, [busy, refetch]);
}
```

## 6. Document chunks (`/knowledge/:kbId/docs/:docId`)

For checking how a document was split. List each chunk: `#chunk_index`, heading path (`metadata.heading_path.join(" › ")`) as a small label, `content` (first line is the heading path itself, render it muted), `token_count`.

Note under the header: "Each chunk is about 100 tokens: the embedding model only reads the first 128 tokens of a text, so long passages are split." Don't offer editing chunks; edit the document and resync instead.

## 7. Test search (`/knowledge/search`)

The tool admins use to check "will the AI find the answer to this question?".

| Control | Sent as |
|---|---|
| Question (text field, Enter to search) | `query` |
| Knowledge bases (multi-select, default all) | `knowledge_base_ids` (omit for all) |
| "Policy documents only" checkbox | `policy_only` |
| Results (5 / 8 / 12) | `top_n` (default 8) |

Results:

- **Verdict banner** from `context_sufficient`:
  - `true` → green "The AI has enough information to answer this."
  - `false` → amber "The AI would not find an answer. Add a document that covers this question, or rephrase the headings."
- **Hits list** in returned order: title + heading path, content, and match chips: "Meaning {similarity×100}%" when `similarity` is set, "Keyword" when `keyword_rank` is set. Don't show `score` (it's only for ordering). Link to the document's chunks page.
- **"What the AI sees"** collapsible panel with `context_block` in a monospace box.

Tips text: "Search works across languages: a question in Nepali can match an English answer." "Exact codes (order numbers, SKUs) are matched by keyword."

## 8. Errors

| Status | `code` | Where | Show |
|---|---|---|---|
| 403 | `permission_denied` | any manage call | [02 §8](02_ROLES_AND_PERMISSIONS.md#8-api-errors-related-to-access) |
| 404 | `not_found` | KB / document / search with a foreign `knowledge_base_ids` | "This knowledge base no longer exists." Refetch the list |
| 409 | `conflict` | add URL | "This page is already in the knowledge base." |
| 422 | `url_not_allowed` | add URL | "This address can't be used. Use a public `http(s)` page on port 80/443." |
| 422 | `validation_error` | add document | Field errors (`details[].field`: `title`, `text`, `faq.0.question`, `url`…) |
| 503 | `embedding_unavailable` | test search | "Search is unavailable right now (the embedding service isn't responding). Try again in a minute." |

A document whose embedding failed is **not** an HTTP error: it comes back `201` with `status: "failed"` and `error` (§5.2).

## 9. Planned (🔜)

| Backend item | Frontend then |
|---|---|
| File uploads (PDF, DOCX, TXT, MD) | Fourth tab "File" with drag-and-drop, ≤ 20 MB |
| Job queue (202 + background ingestion) | Already handled by §5.4 polling; close the dialog right after `202` |
| Site crawl, weekly auto re-sync | "Crawl whole site" option on the Web page tab; "Auto-sync weekly" toggle |
| AI agents (`ai.agent.manage`) ✅ | Agent settings → Knowledge tab links these KBs ([07 §5](07_AI_AGENTS.md#5-agent-settings-ai-agentsagentid)) |
| Playground (`ai.playground.use`) ✅ | [07 §6](07_AI_AGENTS.md#6-playground-ai-agentsagentidplayground) |

## 10. Acceptance checklist

Backend: `make migrate && make seed`, embedding service running (`make embeddings`, port 8001). Password `Password123!`.

| # | As | Do | Expected |
|---|---|---|---|
| 1 | owner@demo.example.com | Open Knowledge in "Customer Support" | Empty state with Create button |
| 2 | owner | Create "Help center" | Card with 0 documents |
| 3 | owner | Add Text doc "Refund policy", category Policy, with `## Timelines` / `## Damaged items` headings | Row Ready, 2+ chunks; chunks page shows heading paths |
| 4 | owner | Add FAQ with 2 questions | Ready, 2 chunks |
| 5 | owner | Add Web page `http://127.0.0.1:8001/docs` | "This address can't be used…" |
| 6 | owner | Add a public page that returns 404 | Row Failed with "Could not fetch URL: HTTP 404"; Resync after fixing works |
| 7 | owner | Test search "how long until I get my money back" | Green banner; top hit "Refund policy › Timelines"; "What the AI sees" shows `[1] …` |
| 8 | owner | Test search in another language for an English FAQ | Matching FAQ is found |
| 9 | owner | Test search something not covered | Amber banner |
| 10 | owner | Stop the embedding service, Test search | 503 message; start it again → works |
| 11 | agent (plain) | Open Knowledge | List visible, no Create; opening a KB shows the read-only note; `/knowledge/search` shows "Your role doesn't include access…" |
| 12 | owner | Delete a document | Row gone; counts drop; test search no longer returns it |
