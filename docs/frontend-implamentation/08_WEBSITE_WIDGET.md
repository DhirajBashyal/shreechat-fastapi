# 08 — Website Live Chat (Frontend Implementation)

Three things to build:

1. **Inbox settings** in the dashboard: create a website widget, change it, copy the embed code, manage the identity secret.
2. **`widget-sdk`**: the small script customers paste on their website. It draws the launcher bubble, injects the iframe and exposes `window.converse`.
3. **`widget-app`**: the chat UI inside the iframe. It talks to `/api/v1/widget/*` and `/ws/widget`.

Prerequisites: [02](02_ROLES_AND_PERMISSIONS.md) (`can()`), [05](05_ORGANIZATION_LEVELS.md) (routes, guards), [07](07_AI_AGENTS.md) (an AI agent can answer the widget). Backend: [../integrations/WEBSITE_CHAT.md](../integrations/WEBSITE_CHAT.md), admin API in [../backend/API_DESIGN.md §12](../backend/API_DESIGN.md#12-website-widget-settings).

## 1. Who sees what

| Screen / action | Permission | Owner | Admin | Agent (default) |
|---|---|---|---|---|
| Create widget inbox, settings, embed code, identity secret, disconnect | `inbox.manage` (workspace) | ✅ | ✅ | ❌ |
| Widget conversations in the inbox (Phase 3 screens) | member of the inbox | ✅ | ✅ | ✅ |
| Website visitors (widget-sdk / widget-app) | none: `website_token` + visitor token | — | — | — |

## 2. Inbox settings (dashboard)

### 2.1 Routes

```text
/orgs/:orgId/ws/:wsId/settings/inboxes/new/website     create (inbox.manage)
/orgs/:orgId/ws/:wsId/settings/inboxes/:inboxId        settings tabs (§2.3)
```

### 2.2 Types and endpoints

```ts
export type WidgetFeature = "attachments" | "emoji_picker" | "end_conversation";
export type ReplyTime = "in_a_few_minutes" | "in_a_few_hours" | "in_a_day";

export interface WorkingHoursEntry {
  day: number;            // ISO weekday: 1 = Monday … 7 = Sunday
  open: string;           // "09:00"
  close: string;          // "17:00" (earlier than open = past midnight)
  closed: boolean;
}

export interface WebWidget {
  inbox_id: string;
  name: string;
  status: "active" | "disconnected";
  website_token: string;
  embed_script: string;               // ready-to-copy <script> snippet
  website_url: string | null;
  allowed_origins: string[];          // "https://shop.example.com", "https://*.example.com"; [] = any site
  widget_color: string;               // "#1f93ff"
  welcome_title: string | null;
  welcome_tagline: string | null;
  reply_time: ReplyTime;
  pre_chat_form_enabled: boolean;
  pre_chat_form_options: { require_name?: boolean; require_email?: boolean; require_phone?: boolean; message?: string };
  identity_verification_required: boolean;
  identity_secret_set: boolean;
  feature_flags: WidgetFeature[];
  greeting_enabled: boolean;
  greeting_message: string | null;
  working_hours_enabled: boolean;
  working_hours: WorkingHoursEntry[];
  timezone: string;                   // IANA, e.g. "Asia/Kathmandu"
  out_of_office_message: string | null;
  allow_messages_after_resolved: boolean;
  lock_to_single_conversation: boolean;
  created_at: string;
}

export interface WebWidgetCreated extends WebWidget {
  identity_secret: string;            // shown ONCE
}
```

| Method | URL (`/api/v1` +) | Body | Success |
|---|---|---|---|
| POST | `/workspaces/{ws}/channels/web-widget` | `{ name, …any settings field }` | `201 WebWidgetCreated` |
| GET | `/workspaces/{ws}/inboxes/{inbox}/web-widget` | — | `WebWidget` |
| PATCH | `/workspaces/{ws}/inboxes/{inbox}/web-widget` | changed fields only (`null` clears texts) | `WebWidget` |
| POST | `/workspaces/{ws}/inboxes/{inbox}/web-widget/identity-secret/rotate` | — | `{ identity_secret }` (shown once) |
| DELETE | `/workspaces/{ws}/inboxes/{inbox}/web-widget` | — | `204` (disconnected) |

### 2.3 Screens

**Create** (wizard, 2 steps):

1. Name, website URL, allowed websites (chip input, see below), colour, welcome title/tagline.
2. Done: show the **embed code** (copy button) and the **identity secret** in a box that says "Copy it now. You won't see it again. Put it on your server, never in browser code." Then go to the settings page.

**Settings tabs** (save each tab with `PATCH` of only its fields):

| Tab | Fields | Notes |
|---|---|---|
| Setup | Embed code (read-only, copy), `website_token`, status badge | "Paste this before `</body>` on every page." |
| Appearance | Name, colour picker, welcome title, tagline, reply time (a few minutes / hours / a day) | Live preview of the launcher + header |
| Websites | Allowed websites: chips, validate `https://host[:port]` or `https://*.domain` | Empty list: warn "Any website can embed your widget." |
| Pre-chat form | Enabled, require name / email / phone, intro message | |
| Behaviour | Greeting on/off + text, working hours (7 rows: closed / open–close), time zone (searchable IANA list), out-of-office message, allow messages after resolved, one conversation per visitor (`lock_to_single_conversation`), features: attachments 🔜, emoji picker, visitor can end the chat | Show attachments disabled with "coming soon" |
| Security | Identity verification required (toggle), **Rotate secret** (confirm: "Hashes made with the old secret stop working immediately"), identity-verification guide (§5) | After rotating: show the new secret once |
| Danger zone | **Disconnect** (confirm: "The widget disappears from your website. Conversations stay.") | After: status badge "Disconnected", all other tabs read-only |

| Error | Show |
|---|---|
| `402 plan_limit_reached` (`limit: "inboxes"`) | "Your plan allows {max} inboxes. Upgrade to add more." |
| `422 validation_error` (`error.details[].field` = `allowed_origins` / `widget_color` / `timezone` …) | Field error from `issue` |
| `403` / `404` | Page guard ([02 §5.3](02_ROLES_AND_PERMISSIONS.md#53-protecting-pages)) |

## 3. widget-sdk (the script on the customer's website)

Served at `{WIDGET_APP_URL}/widget/sdk.js` (backend env; defaults to `FRONTEND_URL`). Plain TypeScript, no framework, target ≤ 10 KB gzipped.

### 3.1 Embed code

The backend returns it as `embed_script`:

```html
<script>
  (function (d, s) {
    var js = d.createElement(s); js.async = true;
    js.src = "https://app.example.com/widget/sdk.js";
    js.onload = function () { window.converse.init({ websiteToken: "demo_website_token_0001" }); };
    d.head.appendChild(js);
  })(document, "script");
</script>
```

### 3.2 Public API (`window.converse`)

| Method | What the SDK does |
|---|---|
| `init({ websiteToken, locale?, position?: "left" \| "right", launcherTitle?, hideLauncher? })` | `GET /widget/config` (colour, texts) → render the launcher → inject the hidden iframe → send `cv:init` |
| `identify({ identifier, email?, name?, phone?, avatarUrl?, identifierHash? })` | → `cv:identify` (iframe calls `POST /widget/identify`) |
| `setCustomAttributes({...})` / `deleteCustomAttribute(key)` | → `cv:setCustomAttributes` / `cv:deleteCustomAttribute` |
| `setConversationAttributes({...})` | → `cv:setConversationAttributes` (applies to the open conversation; queued until one exists) |
| `setLabel(label)` / `removeLabel(label)` | → `cv:setLabel` / `cv:removeLabel` (only labels marked widget-settable; others fail with `label_not_allowed`) |
| `open()` / `close()` / `toggle()` | Show / hide the iframe |
| `reset()` | → `cv:reset`; delete the cookie; the iframe starts a new anonymous session |
| `on(event, cb)` | `ready`, `message` (new agent/AI message), `open`, `close`, `unread` (`{ count }`) |

Calls made before `ready` are queued and replayed in order.

### 3.3 Visitor token storage

- First-party cookie `cv_session_<websiteToken>` on the **customer's** site: `SameSite=Lax; Secure; Max-Age=2592000` (30 days), path `/`.
- The SDK reads it and passes it to the iframe in `cv:init`. The iframe sends back `cv:token` whenever it gets a new token (new session, `identify` rotates it); the SDK rewrites the cookie.
- The iframe never relies on its own cookies or localStorage for the token: third-party storage is blocked or partitioned in modern browsers.

### 3.4 postMessage protocol

Origin checks both ways:

- The SDK only accepts messages whose `event.origin` is the iframe's origin (from `sdk.js`'s own URL).
- The iframe only accepts messages from `window.parent` and remembers `event.origin` of `cv:init` as **`parentOrigin`**.

| Direction | `type` | Payload |
|---|---|---|
| SDK → iframe | `cv:init` | `{ websiteToken, visitorToken: string \| null, locale, pageUrl, referrer }` |
| SDK → iframe | `cv:identify`, `cv:setCustomAttributes`, `cv:deleteCustomAttribute`, `cv:setConversationAttributes`, `cv:setLabel`, `cv:removeLabel`, `cv:reset`, `cv:opened`, `cv:closed`, `cv:pageView` | the method's arguments |
| iframe → SDK | `cv:ready` | `{ config }` |
| iframe → SDK | `cv:token` | `{ visitorToken }` (store in the cookie) |
| iframe → SDK | `cv:message` | `{ message }` (for `on("message")` and the bubble preview while closed) |
| iframe → SDK | `cv:unread` | `{ count }` (badge on the launcher) |
| iframe → SDK | `cv:closeRequest` | — (the close button inside the iframe) |
| iframe → SDK | `cv:error` | `{ code, message }` (logged with `console.warn`; `identify` errors also reject the call) |

**`parentOrigin` matters to the server.** The iframe runs on our domain, so every API call and the socket carry our Origin. The backend therefore checks the page that embeds the iframe:

- REST: send `X-Parent-Origin: <parentOrigin>` on every request.
- Socket: send `parent_origin` in the auth frame.
- Not on the widget's allowed list: `403 origin_not_allowed` / socket close `4403`. Show nothing; the SDK hides the launcher.

## 4. widget-app (inside the iframe)

Route: `{WIDGET_APP_URL}/widget/app`. Every request sends `?website_token=…` (or `website_token` in the JSON body), `X-Parent-Origin`, and, after the session, `X-Visitor-Token`.

### 4.1 Types

```ts
export interface WidgetConfig {
  website_token: string; inbox_name: string; widget_color: string;
  welcome_title: string | null; welcome_tagline: string | null; reply_time: string;
  pre_chat_form_enabled: boolean;
  pre_chat_form_options: { require_name?: boolean; require_email?: boolean; require_phone?: boolean; message?: string };
  feature_flags: string[]; identity_verification_required: boolean;
  working_hours_enabled: boolean; working_hours: WorkingHoursEntry[]; timezone: string;
  is_online: boolean;                 // inside working hours (true when hours are off)
  out_of_office_message: string | null;
  agents_online: boolean;             // false until agent presence ships (Phase 5)
  ai_assistant: boolean;              // an AI agent answers first: show the AI disclosure
}

export interface WidgetContact { name: string; email: string | null; phone_number: string | null; identity_verified: boolean }
export interface SessionResponse { visitor_token: string; realtime_token: string; resumed: boolean; contact: WidgetContact }

export interface WidgetMessage {
  id: string; conversation_id: string;
  direction: "incoming" | "outgoing";          // incoming = from the visitor
  content_type: string;                         // "text" (others later)
  content: string | null;                       // plain text / Markdown, never HTML
  content_attributes: Record<string, unknown>;
  sender: { kind: "contact" | "user" | "ai_agent" | "system" | "automation"; name: string | null };
  client_message_id: string | null;
  sequence_number: number;                      // ordering + paging
  delivery_status: "sent" | "delivered" | "read" | "failed" | "pending";
  sources: { title: string; url: string | null }[];   // AI answers, when the agent shows sources
  attachments: { id: string; kind: string; title: string | null; url: string | null }[];
  created_at: string;
}

export interface WidgetConversation {
  id: string; display_number: number;
  status: "open" | "pending" | "snoozed" | "resolved";
  created_at: string; last_activity_at: string;
  contact_last_seen_at: string | null; agent_last_seen_at: string | null;
  unread_count: number; custom_attributes: Record<string, unknown>; labels: string[];
  last_message: WidgetMessage | null;
}
```

### 4.2 Endpoints (`/api/v1/widget` +)

| Method | URL | Body | Success |
|---|---|---|---|
| GET | `/config` | — | `WidgetConfig` |
| POST | `/sessions` | `{ visitor_token? }` | `201` new / `200` resumed `SessionResponse` |
| DELETE | `/sessions` | — | `204` (`reset()`) |
| POST | `/identify` | `{ identifier, identifier_hash?, email?, name?, phone?, avatar_url? }` | `SessionResponse` (**new token**) |
| PATCH | `/contact` | `{ name?, email?, phone?, custom_attributes? }` | `WidgetContact` |
| DELETE | `/contact/custom-attributes/{key}` | — | `WidgetContact` |
| GET | `/conversations` | — | `WidgetConversation[]` (latest first, 25) |
| POST | `/conversations` | `{ content, client_message_id, contact?: {name, email, phone}, custom_attributes?, referer_url?, browser_language? }` | `201` / `200` (retry or single-conversation inbox) `{ conversation, messages }` |
| PATCH | `/conversations/{id}` | `{ custom_attributes }` (`null` value removes a key) | `WidgetConversation` |
| GET | `/conversations/{id}/messages?before=&after=&limit=` | — | `WidgetMessage[]` oldest first (limit ≤ 50) |
| POST | `/conversations/{id}/messages` | `{ content, client_message_id }` | `201` new / `200` same `client_message_id` |
| POST | `/conversations/{id}/typing` | `{ state: "on" \| "off" }` | `204` |
| POST | `/conversations/{id}/seen` | — | `WidgetConversation` (`unread_count: 0`) |
| POST | `/conversations/{id}/resolve` | — | `WidgetConversation` (only with feature `end_conversation`) |
| POST | `/conversations/{id}/labels` · DELETE `/conversations/{id}/labels/{label}` | `{ label }` | `WidgetConversation` |
| POST | `/events` | `{ name, url?, properties? }` | `202` (page views: send on `cv:pageView`) |
| POST | `/uploads` | `{ filename, content_type, byte_size }` | 🔜 `503 uploads_unavailable` today: hide the paperclip |

### 4.3 Boot sequence

1. Receive `cv:init` → store `parentOrigin`, `websiteToken`, `visitorToken`.
2. `GET /config` → apply colour and texts. On `404 widget_not_found` or `403 origin_not_allowed`, post `cv:error` and stay hidden.
3. `POST /sessions { visitor_token }`. Always post `cv:token` with the returned token (it is the same one when `resumed`).
4. `GET /conversations` → show the latest unresolved one, or the home screen (welcome title, tagline, reply time, "Start a conversation").
5. Open the socket (§4.5). Post `cv:ready`.

**Don't create a conversation when the widget opens.** The first `POST /conversations` carries the first message.

### 4.4 Chat screen

- **Sending:** generate `client_message_id = crypto.randomUUID()`, show the bubble as pending, `POST`. On a network error retry with the **same** id (the server returns the same message, `200`). Match the server's message to the pending bubble by `client_message_id`.
- **Pre-chat form:** shown before the first message when `pre_chat_form_enabled`; required fields from `pre_chat_form_options`. Send the values as `contact` in `POST /conversations`. `422 pre_chat_required` (`fields: [...]`) → highlight those fields.
- **History:** open with `GET …/messages?limit=20`; scroll up → `before=<oldest sequence_number>`. Render in `sequence_number` order, never arrival order.
- **Seen:** while the chat is open and visible (`document.visibilityState`), call `POST …/seen` on open and when new agent messages arrive (debounce 1 s). Post `cv:unread` with the conversation's `unread_count` otherwise.
- **Typing:** send `typing` frames over the socket (`on` on first keystroke, `off` after 3 s idle or send). Show "{name} is typing…" from `conversation.typing` events.
- **Senders:** `system` messages (greeting, out-of-office) are centred or plain bubbles. `ai_agent` messages show the agent's name plus an "AI" tag (`config.ai_assistant`: show "You're chatting with an AI assistant. Ask for a person any time." on the home screen). Show `sources` under AI answers as links.
- **Status:** `resolved` → "This conversation has ended" + "Start a new conversation". When `allow_messages_after_resolved` is on (default), typing in it reopens it. Otherwise `409 conversation_resolved` → start a new one.
- **End chat:** menu item only when `feature_flags` includes `end_conversation`.
- **Offline:** `config.is_online === false` → show `out_of_office_message` on the home screen (the server also posts it once as a message).
- **Rendering:** content is plain text or Markdown. Render with a sanitiser (DOMPurify or a Markdown renderer with HTML disabled). Never `innerHTML` raw content. Links get `rel="noopener noreferrer" target="_blank"`.

### 4.5 Realtime (`/ws/widget`)

```text
wss://api.example.com/ws/widget?website_token=…
→ {"type":"auth","token":"<visitor_token>","parent_origin":"<parentOrigin>"}   (first frame, within 5 s)
← {"type":"ready","connection_id":"c_…","heartbeat_seconds":25}
← {"type":"event","event":"message.created","ts":"…","data":{"message":WidgetMessage}}
← {"type":"event","event":"message.updated","data":{"message":WidgetMessage}}
← {"type":"event","event":"conversation.status_changed","data":{"conversation_id":"…","status":"resolved"}}
← {"type":"event","event":"conversation.typing","data":{"conversation_id":"…","actor":{…},"state":"on"}}
→ {"type":"typing","conversation_id":"…","state":"on"}
→ {"type":"ping"}   ← {"type":"pong"}      (every heartbeat_seconds)
```

- Never put the token in the URL.
- `message.created` also arrives for the visitor's own messages (other tabs, other devices after `identify`). De-duplicate by `id`, and by `client_message_id` for pending bubbles.
- Close codes: `4401`: token invalid or expired → `POST /sessions` (a new anonymous session), post `cv:token`, reconnect. `4403`: origin not allowed → stay hidden. `4404`: widget gone → hide.
- Reconnect with exponential backoff 1 s → 2 → 4 … max 30 s, with jitter. After reconnecting, fetch `GET …/messages?after=<last sequence_number>` for the open conversation and refetch `GET /conversations` (status, unread).
- Pub/sub is at-most-once: REST is the source of truth.

### 4.6 Errors

| Code | Where | Show |
|---|---|---|
| `401 invalid_visitor_token` / `token_expired` | any call | New session silently (`POST /sessions`), post `cv:token`, retry once |
| `403 origin_not_allowed` | any | Hide the widget |
| `403 contact_blocked` | send | "You can't send messages in this chat." |
| `403 identity_verification_required` | identify | Reject `identify()`; `console.warn` to the site developer |
| `403 invalid_identifier_hash` | identify | Same: "identifierHash doesn't match" |
| `409 reset_required` | identify | Reject: "Call converse.reset() before identifying a different user." |
| `409 conversation_resolved` | send | "This conversation has ended." + new conversation button |
| `422 pre_chat_required` | start | Highlight `fields` |
| `422 unknown_attribute` / `invalid_attribute_value` | attributes | `console.warn` (developer mistake) |
| `422 label_not_allowed` | setLabel | `console.warn` |
| `413 payload_too_large` | any | "Message is too long." (max 20,000 characters; enforce in the input) |
| `429 rate_limited` | send / start | "You're sending messages too fast. Wait a moment." Keep the pending bubble and retry after 10 s |

## 5. Identity verification (for the customer's developers)

Put this in the Security tab as copyable snippets. The website's **server** computes the hash; the secret never goes to the browser:

```js
// Node.js
const crypto = require("crypto");
const identifierHash = crypto.createHmac("sha256", process.env.CONVERSE_IDENTITY_SECRET)
  .update(user.id).digest("hex");
```

```python
# Python
import hashlib, hmac
identifier_hash = hmac.new(SECRET.encode(), user_id.encode(), hashlib.sha256).hexdigest()
```

```js
// in the page, after login
converse.identify({ identifier: "user-42", email: "u42@example.com", name: "Ana", identifierHash: "<from your server>" });
```

What happens:

- **With a valid hash:** the visitor becomes that customer. Their earlier anonymous chat moves to the customer, and the same customer on another device sees the full history.
- **Without a hash:** only name, email and phone are saved on this visitor, and nothing is merged. So someone typing another person's email can't read their chats.
- **With "Identity verification required" on**, identifying without a hash is rejected.

## 6. Acceptance checklist

Backend: `make migrate && make seed`, `make dev`, `make outbox-relay`. For AI answers also `make ai-worker` and [07](07_AI_AGENTS.md). Demo widget: `website_token` `demo_website_token_0001`, allowed origin `http://localhost:5173`, identity secret `demo_widget_identity_secret_0001`. Serve a test page on port 5173 with the embed code.

| # | As | Do | Expected |
|---|---|---|---|
| 1 | owner | Create a website inbox | Embed code + identity secret shown once; reopening settings shows no secret |
| 2 | agent | Open inbox settings | No access (hidden menu, guard) |
| 3 | owner | Allowed websites: add `not a url` | Field error |
| 4 | visitor | Open the test page | Launcher in the widget colour; welcome title |
| 5 | visitor | Open the widget, close it | No conversation created (`GET /conversations` empty) |
| 6 | visitor | Send "hello" | Conversation appears for agents; greeting message shown under it |
| 7 | visitor | Send while offline (devtools), go online | Same `client_message_id` retried; one message only |
| 8 | agent | Reply (or insert a reply + run `make outbox-relay`) | Reply appears live in the widget; unread badge while closed |
| 9 | agent | Private note | Never visible in the widget |
| 10 | visitor | Reload the page | Same conversation (cookie token resumed) |
| 11 | visitor | `converse.identify({identifier:"user-42", identifierHash: <hmac>})` on a second browser after the same on the first | Second browser shows the first browser's conversation |
| 12 | visitor | `identify` with a wrong hash | Promise rejects (`invalid_identifier_hash`) |
| 13 | visitor | Embed the snippet on `http://localhost:5174` (not allowed) | Widget stays hidden (`403 origin_not_allowed`) |
| 14 | visitor | End chat (feature on) | "Conversation has ended"; typing again reopens it |
| 15 | owner | Disconnect the widget | Widget disappears from the test page (`404`) |
| 16 | owner | Link an AI agent ([07](07_AI_AGENTS.md)) to the inbox, visitor asks a KB question | AI disclosure on the home screen; AI answer with sources (if enabled) |
