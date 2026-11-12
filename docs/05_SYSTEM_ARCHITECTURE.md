# 05 — System Architecture

Converse is a **modular monolith**: one codebase and one database, deployed as a few process types. Modules talk to each other through service interfaces and domain events, never by reaching into each other's tables. This keeps a later split into services possible without paying for it now.

## 1. High-level system architecture

```mermaid
flowchart LR
    subgraph Clients
        DASH["Agent dashboard SPA"]
        WIDGET["Website widget iframe + SDK"]
        EXT["Customer systems: API tokens + webhooks"]
    end

    subgraph Providers
        META["Meta Graph API + Webhooks<br/>Messenger, Instagram, WhatsApp"]
        LLM["LLM providers<br/>OpenAI, Anthropic, ..."]
        STRIPE["Stripe"]
    end

    NGINX["Nginx<br/>TLS, routing, WS upgrade"]

    subgraph Converse["Converse processes"]
        API["api: FastAPI<br/>REST + webhook ingress"]
        WS["realtime: FastAPI<br/>WebSocket gateway"]
        WK["worker pools: Celery<br/>realtime, outbound, inbound, webhooks, ai, default"]
        BEAT["scheduler: Celery beat"]
        RELAY["outbox relay"]
    end

    PG[("PostgreSQL 16<br/>+ pgvector, RLS")]
    RD[("Redis 7<br/>broker, pub/sub, locks, presence")]
    S3[("S3-compatible storage")]

    DASH -- HTTPS --> NGINX
    WIDGET -- HTTPS/WSS --> NGINX
    EXT -- HTTPS --> NGINX
    META -- webhooks --> NGINX
    STRIPE -- webhooks --> NGINX
    NGINX --> API
    NGINX --> WS

    API --> PG
    API --> RD
    API --> S3
    WS --> RD
    WS --> PG
    RELAY --> PG
    RELAY --> RD
    BEAT --> RD
    WK --> PG
    WK --> RD
    WK --> S3
    WK --> META
    WK --> LLM
    WK --> STRIPE
    WK -- signed webhooks --> EXT
```

| Process | Scales by | Notes |
|---|---|---|
| `api` | CPU / request rate | Stateless. Also hosts `/webhooks/*` ingress, which only verifies, records and enqueues |
| `realtime` | Concurrent connections | Same codebase, different entry point (`converse.realtime.app`). Holds WebSocket connections, subscribes to Redis |
| `worker-*` | Queue depth | One Celery worker deployment per queue group so a slow queue can't starve a fast one |
| `beat` | Exactly **1** replica | Periodic tasks (snooze wake-up, auto-resolve, token refresh, outbox sweeper) |
| `relay` | 1–2 replicas | Reads `event_outbox` with `FOR UPDATE SKIP LOCKED` and enqueues handler tasks |

## 2. Backend component architecture

```mermaid
flowchart TB
    subgraph Entry["Entry points"]
        R["api/routes/*"]
        WH["api/webhooks/*"]
        WSH["realtime/gateway"]
        T["workers/tasks/*"]
    end

    subgraph Core["Core modules: converse/modules"]
        TEN["tenancy"]
        AUTH["auth + access"]
        INB["inboxes"]
        CON["contacts"]
        CONV["conversations"]
        MSG["messaging"]
        ASG["assignment"]
        LBL["labels / attributes / canned"]
        AUTO["automation"]
        WHK["webhooks"]
        KB["knowledge"]
        AN["analytics"]
        BILL["billing"]
        AUD["audit"]
    end

    subgraph Edge["Edge modules"]
        CH["channels: adapters<br/>web_widget, meta.messenger,<br/>meta.instagram, meta.whatsapp"]
        AI["ai: LangGraph agent"]
    end

    subgraph Infra["Infrastructure: converse/core"]
        DB["db: engine, session, tenant scope"]
        EV["events: outbox, bus"]
        CR["crypto"]
        ST["storage"]
        RT["realtime publisher"]
        HTTP["http clients"]
    end

    R --> Core
    WH --> CH
    T --> Core
    T --> CH
    T --> AI
    WSH --> RT
    CH -- "normalised types only" --> MSG
    CH --> CON
    AI -- "posts replies via" --> MSG
    AI --> KB
    Core --> Infra
    CH --> HTTP
    CH --> CR
```

**Dependency rules** (enforced with `import-linter`, see [backend/PROJECT_STRUCTURE.md](backend/PROJECT_STRUCTURE.md)):

1. `modules/*` never import `channels/*` or `ai/*`.
2. `channels/*` import only `channels/base`, `core/*` and the public service interfaces of `messaging`, `contacts`, `conversations`.
3. `ai/*` reads through repositories/services and writes only through `messaging.MessageService` and `conversations.ConversationService`.
4. `api/*` and `workers/*` are the only places that wire things together.

## 3. Database architecture

```mermaid
flowchart LR
    subgraph Global["Global (no RLS)"]
        U["users, user_sessions, platform_admins"]
        P["permissions, subscription_plans"]
        CRT["channel_routes"]
        IWE["inbound_webhook_events"]
    end
    subgraph Tenant["Tenant-scoped (RLS on organization_id)"]
        O["organizations, memberships, roles"]
        W["workspaces"]
        OPS["inboxes, channel_connections, provider tables"]
        CUST["contacts, contact_identities"]
        CV["conversations, messages, attachments"]
        CFG["labels, attributes, canned, automation, webhooks"]
        AIT["ai_agents, knowledge_bases, kb_chunks (vector)"]
        BL["subscriptions, billing_records, usage_records"]
        OBX["event_outbox, audit_logs, reporting_events"]
    end
    U --- O
    O --- W --- OPS --- CUST --- CV
    W --- CFG
    W --- AIT
    O --- BL
    CRT -. "resolves tenant for" .-> OPS
```

One PostgreSQL database, one schema. Two DB roles: `converse_app` (RLS enforced) and `converse_system` (BYPASSRLS, used only by ingress routing, the outbox relay, cross-tenant schedulers and platform admin). Details: [06_DATABASE_ARCHITECTURE.md](06_DATABASE_ARCHITECTURE.md).

## 4. Multi-tenant architecture

```mermaid
sequenceDiagram
    participant C as Client
    participant A as FastAPI dependency chain
    participant DB as PostgreSQL (converse_app)
    C->>A: GET /api/v1/workspaces/{ws}/conversations (Bearer JWT)
    A->>A: authenticate(): verify JWT, load user
    A->>DB: BEGIN, then system lookup of workspace → organization_id
    A->>A: authorize_membership(): user ∈ organization? member of workspace?
    A->>DB: SET LOCAL app.current_org_id = org
    A->>A: build TenantContext(org, ws, user, permissions)
    A->>DB: SELECT ... WHERE workspace_id = :ws (RLS also filters organization_id)
    DB-->>A: rows of this tenant only
    A-->>C: 200 JSON
```

Three independent layers protect tenant data: (1) every repository method takes a `TenantContext` and filters by `workspace_id`; (2) composite foreign keys `(workspace_id, parent_id)` make cross-tenant references impossible to store; (3) RLS hides other tenants' rows even if layers 1 and 2 have a bug. See [09_MULTI_TENANCY.md](09_MULTI_TENANCY.md).

## 5. Social media integration architecture

```mermaid
flowchart LR
    subgraph Ingress["api/webhooks (provider-specific, thin)"]
        V["GET verify<br/>hub.challenge"]
        S["POST events<br/>raw body + X-Hub-Signature-256"]
    end
    subgraph Adapters["channels/* adapters"]
        BASE["BaseChannelAdapter"]
        MSN["MessengerAdapter"]
        IG["InstagramAdapter"]
        WA["WhatsAppAdapter"]
        WW["WebWidgetAdapter"]
    end
    subgraph Shared["Shared, provider-independent"]
        ROUTE["ChannelRouter<br/>channel_routes"]
        PIPE["InboundPipeline"]
        OUT["OutboundDispatcher"]
    end
    S --> ROUTE
    ROUTE --> PIPE
    PIPE -- "parse_incoming()" --> BASE
    BASE --- MSN
    BASE --- IG
    BASE --- WA
    BASE --- WW
    PIPE -- "InboundMessage" --> CORE["contacts / conversations / messaging"]
    CORE -- "outbox: message.created (outgoing)" --> OUT
    OUT -- "send_message()" --> BASE
```

Provider-specific: signature secret location, payload parsing, media download, send API, error codes, 24 h window rules, token refresh. Provider-independent: routing, dedup, ordering, contact/conversation resolution, persistence, events, realtime, retries. Full contract: [integrations/CHANNEL_ADAPTER_ARCHITECTURE.md](integrations/CHANNEL_ADAPTER_ARCHITECTURE.md).

## 6. Message processing architecture

```mermaid
flowchart TB
    A["Webhook received"] --> B{"Signature valid?"}
    B -- no --> X1["401, log, metric"]
    B -- yes --> C["Insert inbound_webhook_events<br/>ON CONFLICT (provider, dedup_key) DO NOTHING"]
    C --> D{"New row?"}
    D -- no --> X2["200 OK (duplicate)"]
    D -- yes --> E["Enqueue process_inbound_event(event_id)<br/>queue: inbound, ordering key = provider:external_id:sender"]
    E --> F["200 OK in under 1 s"]
    E --> G["Worker: load event, resolve channel_routes → tenant"]
    G --> H["SET LOCAL app.current_org_id"]
    H --> I["adapter.parse_incoming() → list of InboundMessage / DeliveryUpdate"]
    I --> J["ContactResolver: find or create contact + identity"]
    J --> K["ConversationResolver: open conversation or new / reopen"]
    K --> L["MessageService.create_incoming(): INSERT ... ON CONFLICT (inbox_id, external_id) DO NOTHING"]
    L --> M["Write event_outbox rows in same transaction"]
    M --> N["COMMIT"]
    N --> O["Publish realtime event to Redis"]
    N --> P["Relay → handlers: automation, AI, webhooks, reporting, notifications"]
    L --> Q["Media: enqueue download_inbound_media(attachment_id)"]
```

Details, ordering and failure handling: [backend/MESSAGE_PROCESSING.md](backend/MESSAGE_PROCESSING.md).

## 7. Real-time event architecture

```mermaid
flowchart LR
    SVC["Service commits change"] --> PUB["RealtimePublisher.publish()<br/>after commit"]
    PUB --> R1[("Redis channel<br/>rt:org:{org_id}")]
    PUB --> R2[("rt:user:{user_id}")]
    PUB --> R3[("rt:contact:{identity_token}")]
    R1 --> G1["Gateway replica 1"]
    R1 --> G2["Gateway replica 2"]
    R2 --> G1
    R3 --> G2
    G1 -- "filter by visibility:<br/>inbox membership, permissions" --> AG["Agent sockets"]
    G2 --> WV["Widget visitor sockets"]
```

Publishing happens in-process right after commit (lowest latency, same as Chatwoot's `SyncDispatcher`). If a publish is lost, clients recover on reconnect by refetching `since=<last_event_ts>`. Realtime is a speed layer, never the source of truth. Details: [backend/REALTIME_COMMUNICATION.md](backend/REALTIME_COMMUNICATION.md).

## 8. AI chatbot architecture

```mermaid
flowchart LR
    EVT["outbox: message.created<br/>incoming, ai_active conversation"] --> H["ai handler: should_run()?"]
    H -- "no: human assigned / ai inactive / quota" --> STOP["skip"]
    H -- yes --> Q["Celery queue ai<br/>debounce 2 s per conversation"]
    Q --> G["LangGraph run<br/>thread_id = ai_conversation_sessions.thread_id"]
    G --> TOOLS["tools: knowledge_search, order_lookup,<br/>product_search, customer_info"]
    G --> LLM["LLMProvider<br/>tenant credential + model"]
    G --> DEC{"outcome"}
    DEC -- reply --> MS["MessageService.create_outgoing<br/>sender_kind = ai_agent"]
    DEC -- handoff --> HO["ConversationService.handoff_to_human()"]
    DEC -- discard --> DIS["newer message arrived → stop"]
    MS --> OUT["normal outbound path to channel"]
```

The AI module never touches channel APIs or message tables directly. Details: [ai/AI_ARCHITECTURE.md](ai/AI_ARCHITECTURE.md).

## 9. Authentication and authorization architecture

```mermaid
flowchart TB
    subgraph Principals
        AG["Agent: JWT access 15 min + refresh rotation"]
        TOK["API token: cvs_ prefix, hashed, scoped"]
        VIS["Widget visitor: visitor JWT bound to inbox + identity"]
        PA["Platform admin: agent JWT + platform_admins row + MFA"]
        PRV["Provider: HMAC signature, no principal"]
    end
    AG --> DEP["get_principal()"]
    TOK --> DEP
    VIS --> WDEP["get_widget_visitor()"]
    PA --> PDEP["require_platform_admin()"]
    DEP --> TC["get_tenant_context(workspace_id)"]
    TC --> PERM["require_permission('conversation.manage')"]
    PERM --> VIS2["visibility filter: inbox/team/participant"]
```

Details: [10_AUTHENTICATION_AND_RBAC.md](10_AUTHENTICATION_AND_RBAC.md).

## 10. Background job architecture

```mermaid
flowchart LR
    API["api"] -- "enqueue after commit" --> RQ[("Redis broker")]
    RELAY["outbox relay"] --> RQ
    BEAT["beat"] --> RQ
    RQ --> Q1["realtime"]
    RQ --> Q2["outbound"]
    RQ --> Q3["inbound"]
    RQ --> Q4["webhooks"]
    RQ --> Q5["ai"]
    RQ --> Q6["default"]
    RQ --> Q7["low: media, kb ingestion, reports"]
    Q1 & Q2 & Q3 & Q4 & Q5 & Q6 & Q7 --> FAIL{"max retries exceeded?"}
    FAIL -- yes --> DLQ["dead state in source table<br/>+ alert + admin replay"]
```

Details: [backend/BACKGROUND_JOBS.md](backend/BACKGROUND_JOBS.md).

---

## 11. End-to-end message flows

### 11.1 WhatsApp (customer → agent/AI → customer)

```mermaid
sequenceDiagram
    autonumber
    participant Cu as Customer (WhatsApp)
    participant Meta as Meta Cloud API
    participant In as api /webhooks/meta/whatsapp
    participant DB as PostgreSQL
    participant Q as Celery
    participant Wk as inbound worker
    participant RT as Redis pub/sub + gateway
    participant Ag as Agent browser
    participant AI as ai worker (LangGraph)
    participant Ob as outbound worker

    Cu->>Meta: sends "Where is my order?"
    Meta->>In: POST {object: whatsapp_business_account, entry[].changes[].value.messages[]}
    In->>In: verify X-Hub-Signature-256 = HMAC_SHA256(app_secret, raw_body)
    In->>DB: INSERT inbound_webhook_events (provider, dedup_key = wamid) ON CONFLICT DO NOTHING
    In->>Q: process_inbound_event(event_id)
    In-->>Meta: 200 OK
    Q->>Wk: run task
    Wk->>DB: channel_routes[(whatsapp_cloud, phone_number_id)] → org, workspace, inbox
    Wk->>DB: SET LOCAL app.current_org_id
    Wk->>Wk: WhatsAppAdapter.parse_incoming() → InboundMessage(wa_id, wamid, text, timestamp)
    Wk->>DB: upsert contact + contact_identity (source_id = wa_id or BSUID)
    Wk->>DB: find open conversation for identity, else create (display_number = next_workspace_counter)
    Wk->>DB: INSERT message ON CONFLICT (inbox_id, external_id) DO NOTHING + event_outbox rows
    Wk->>DB: COMMIT
    Wk->>RT: publish message.created to rt:org:{org}
    RT->>Ag: WS event message.created (visible to inbox members)
    Note over DB,AI: relay picks up outbox event message.created
    Q->>AI: run_ai_agent(conversation_id, trigger_message_id)
    AI->>AI: load context → classify intent → order_lookup tool → generate → guardrails
    AI->>DB: MessageService.create_outgoing(sender_kind=ai_agent) + outbox
    Q->>Ob: deliver_outbound_message(message_id)
    Ob->>Ob: check 24 h window (last_incoming_at), choose text vs template
    Ob->>Meta: POST /{phone_number_id}/messages
    Meta-->>Ob: {messages: [{id: wamid.REPLY}]}
    Ob->>DB: set external_id = wamid.REPLY, delivery_status = sent
    Meta->>Cu: delivers reply
    Meta->>In: status webhook delivered / read
    In->>Q: process_inbound_event → DeliveryUpdate
    Q->>Wk: update delivery_status monotonically, publish message.updated
```

Step-by-step:

| # | Step | Component | Notes |
|---|---|---|---|
| 1 | Webhook verification (one-time, at subscription) | `GET /webhooks/meta/whatsapp` | Compare `hub.verify_token` to the platform verify token; echo `hub.challenge` |
| 2 | Signature validation | ingress route | Use the **raw** body bytes; constant-time compare; reject with 401 |
| 3 | Duplicate check | `inbound_webhook_events` | `dedup_key` = `wamid` for messages, `wamid:status` for statuses |
| 4 | Tenant identification | `ChannelRouter` | `value.metadata.phone_number_id` → `channel_routes` |
| 5 | Channel identification | same | Gives `inbox_id` and `channel_connection_id`; inactive connection → event `ignored` |
| 6 | Normalization | `WhatsAppAdapter.parse_incoming` | text, image, document, audio/voice, video, sticker, location, contacts, interactive replies, button replies, reactions (stored as `content_attributes.reaction` on the target), unsupported |
| 7 | Contact identification | `ContactResolver` | `(inbox_id, source_id)` unique; name from `contacts[].profile.name`; phone normalised to E.164 |
| 8 | Conversation identification | `ConversationResolver` | Latest non-resolved conversation for the identity; if resolved and `allow_messages_after_resolved` → new conversation, or reopen if `lock_to_single_conversation` |
| 9 | Persistence | `MessageService.create_incoming` | Idempotent; updates `last_incoming_at`, `last_activity_at`, `waiting_since` |
| 10 | Event publishing | outbox | `message.created`, maybe `conversation.created` / `conversation.opened` |
| 11 | Realtime inbox update | `RealtimePublisher` | After commit |
| 12 | AI processing | `ai` handler | Only when `conversations.ai_active` and no human assignee |
| 13 | Agent or bot response | `MessageService.create_outgoing` | Same path for both |
| 14 | WhatsApp API | `WhatsAppAdapter.send_message` | Outside 24 h → template required, else error `window_closed` shown to agent |
| 15 | Customer receives reply | Meta | Delivery/read statuses update the message |

### 11.2 Facebook Messenger

```mermaid
sequenceDiagram
    autonumber
    participant Cu as Customer (Messenger)
    participant Meta as Meta
    participant In as /webhooks/meta/messenger
    participant Wk as inbound worker
    participant DB as PostgreSQL
    participant Ob as outbound worker
    Cu->>Meta: message to Page
    Meta->>In: POST {object: page, entry[]: {id: PAGE_ID, messaging[]: {sender.id: PSID, recipient.id: PAGE_ID, message: {mid, text, attachments}}}}
    In->>In: verify signature (app secret)
    In->>DB: inbound_webhook_events dedup_key = mid (or watermark for read events)
    In-->>Meta: 200
    Wk->>DB: channel_routes[(facebook_messenger, PAGE_ID)]
    Wk->>Wk: MessengerAdapter.parse_incoming()
    alt is_echo = true
        Wk->>DB: message with external_id = mid exists (sent by us)? → ignore
        Wk->>DB: otherwise store as outgoing (agent replied from Meta Business Suite)
    else customer message
        Wk->>Meta: GET /{PSID}?fields=first_name,last_name,profile_pic (first contact only, cached)
        Wk->>DB: contact + identity (source_id = PSID), conversation, message
    end
    Note over Ob: agent replies
    Ob->>Meta: POST /me/messages {recipient: {id: PSID}, messaging_type: RESPONSE | MESSAGE_TAG, tag: HUMAN_AGENT, message: {...}}
    Meta-->>Ob: {message_id: mid.REPLY}
    Ob->>DB: external_id = mid.REPLY (so the echo is ignored as a duplicate)
    Meta->>In: delivery / read events (watermark based)
    Wk->>DB: mark outgoing messages with created_at <= watermark as delivered / read
```

Differences from WhatsApp: tenant key is the **Page id**; contact key is the **PSID**; profile data comes from a Graph API call; delivery/read events carry a **watermark** timestamp rather than a message id; replies outside 24 h need the `HUMAN_AGENT` tag (allowed up to 7 days, requires Meta approval for the app); echo events arrive for our own sends.

### 11.3 Instagram (Instagram Login)

```mermaid
sequenceDiagram
    autonumber
    participant Cu as Customer (Instagram DM)
    participant Meta as Meta
    participant In as /webhooks/meta/instagram
    participant Wk as inbound worker
    participant DB as PostgreSQL
    participant Ob as outbound worker
    Cu->>Meta: DM to business account
    Meta->>In: POST {object: instagram, entry[]: {id: IG_USER_ID, messaging[]: {sender.id: IGSID, message: {mid, text | attachments}}}}
    In->>In: verify signature (Instagram app secret)
    In->>DB: dedup_key = mid
    In-->>Meta: 200
    Wk->>DB: channel_routes[(instagram, IG_USER_ID)]
    Wk->>Wk: InstagramAdapter.parse_incoming() (skip is_echo of our own sends, story replies → content_attributes.story)
    Wk->>Meta: GET /{IGSID}?fields=name,username,profile_pic (cached)
    Wk->>DB: contact, identity (source_id = IGSID), conversation, message
    Ob->>Meta: POST graph.instagram.com/{IG_USER_ID}/messages {recipient: {id: IGSID}, message}
    Meta-->>Ob: {message_id}
    Ob->>DB: external_id set, delivery_status = sent
```

Differences: long-lived user access token **expires after 60 days** and must be refreshed (beat task `refresh_expiring_tokens`); 24 h window with `HUMAN_AGENT` tag up to 7 days; attachment types are image, video, audio, file, plus story mentions/replies and shares.

### 11.4 Website live chat

```mermaid
sequenceDiagram
    autonumber
    participant V as Visitor browser (widget)
    participant API as api /api/v1/widget
    participant DB as PostgreSQL
    participant GW as realtime gateway
    participant RD as Redis
    participant Ag as Agent browser
    V->>API: POST /widget/sessions {website_token} (Origin checked against allowed_origins)
    API->>DB: channel_routes[(web_widget, website_token)] → inbox
    API->>DB: create contact (anonymous visitor) + identity (source_id = random uuid)
    API-->>V: {visitor_token (JWT), realtime_token, widget config}
    V->>GW: WSS /ws/widget?token=visitor_token
    GW->>RD: SUBSCRIBE rt:contact:{realtime_token}
    V->>API: POST /widget/messages {content, client_message_id}
    API->>DB: find/create conversation, INSERT message (idempotent on client_message_id) + outbox
    API->>RD: publish message.created → rt:org:{org}
    RD->>GW: event
    GW->>Ag: message.created (inbox members)
    Ag->>API: POST /workspaces/{ws}/conversations/{id}/messages {content}
    API->>DB: INSERT outgoing + outbox
    API->>RD: publish to rt:org and rt:contact:{realtime_token}
    GW->>V: message.created (non-private only)
    Note over V,API: optional identify: POST /widget/identify {identifier, email, identifier_hash = HMAC(widget_secret, identifier)}
```

Differences: no provider API. "Sending" to the customer is a realtime publish to the contact channel (the `WebWidgetAdapter.send_message` just publishes, plus an email fallback later). Anonymous visitors become identified contacts via `identify` with HMAC verification. Conversation continuity across devices requires verified identity. Details: [integrations/WEBSITE_CHAT.md](integrations/WEBSITE_CHAT.md).

## 12. Scalability notes

| Component | First bottleneck | Plan |
|---|---|---|
| `messages` table | Size (hundreds of millions of rows) | Indexes on `(conversation_id, sequence_number)`; plan monthly range partitioning by `created_at` once above ~200M rows ([06](06_DATABASE_ARCHITECTURE.md#7-growth-and-partitioning)) |
| Conversation list queries | Sorting by `last_activity_at` with filters | Composite indexes per common filter; keyset pagination; no `COUNT(*)` on large lists (cached counts) |
| Inbound bursts (e.g. campaign replies) | inbound queue | Horizontal workers; per-tenant fairness limits |
| Realtime fan-out | Large orgs with many agents | Per-org channel + client-side filter in gateway; split hot orgs per workspace channel |
| AI | LLM latency and cost | Separate queue and pool; per-tenant concurrency cap; debounce |
| Outbox relay | Single table hot spot | `SKIP LOCKED` batches; prune dispatched rows after 7 days |
