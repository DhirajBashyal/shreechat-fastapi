# Frontend Implementation Guides

What the frontend must build against the backend in `app/`, one area per document. Read them in order; each lists its prerequisites.

| Doc | Covers | Backend counterpart |
|---|---|---|
| [01_AUTHENTICATION.md](01_AUTHENTICATION.md) | Sign up, log in, tokens and refresh, remember me, logout, password reset, `/auth/me`, auth state | [../10_AUTHENTICATION_AND_RBAC.md](../10_AUTHENTICATION_AND_RBAC.md) |
| [02_ROLES_AND_PERMISSIONS.md](02_ROLES_AND_PERMISSIONS.md) | `permissions` per organization, `can()`, hiding UI, page guards, member-management rules, access errors | [../10_AUTHENTICATION_AND_RBAC.md](../10_AUTHENTICATION_AND_RBAC.md) §4 |
| [03_ORGANIZATIONS_AND_MEMBERS.md](03_ORGANIZATIONS_AND_MEMBERS.md) | Organization and workspace switchers, suspended / pending-deletion screens, members, invitations, transfer / delete / restore, leave | [../12_ORGANIZATION_LEVELS_AND_PERMISSIONS.md](../12_ORGANIZATION_LEVELS_AND_PERMISSIONS.md) |
| [04_PLATFORM_ADMIN.md](04_PLATFORM_ADMIN.md) | Platform console: organization counts and search, suspend / limits, platform admins | [../12_ORGANIZATION_LEVELS_AND_PERMISSIONS.md §7](../12_ORGANIZATION_LEVELS_AND_PERMISSIONS.md#7-platform-admin-our-staff) |
| [05_ORGANIZATION_LEVELS.md](05_ORGANIZATION_LEVELS.md) | All levels together: how the UI detects each level, route tree and guards, navigation per level, error handling, planned work, build order, level × screen checklist | [../implementation/ORGANIZATION_LEVELS_IMPLEMENTATION.md](../implementation/ORGANIZATION_LEVELS_IMPLEMENTATION.md) |
| [06_KNOWLEDGE_BASES.md](06_KNOWLEDGE_BASES.md) | Knowledge bases for the AI: documents (text, FAQ, web page), chunks, test search, statuses and errors | [../ai/RAG_ARCHITECTURE.md](../ai/RAG_ARCHITECTURE.md) |
| [07_AI_AGENTS.md](07_AI_AGENTS.md) | AI provider keys, agent settings, playground, AI activity panel, pause / return to AI | [../ai/](../ai/) |
| [08_WEBSITE_WIDGET.md](08_WEBSITE_WIDGET.md) | Website live chat: widget inbox settings + embed code + identity secret, `widget-sdk` (launcher, `window.converse`, cookie, postMessage), `widget-app` (visitor API, realtime socket, reconnect, errors) | [../integrations/WEBSITE_CHAT.md](../integrations/WEBSITE_CHAT.md) |

Demo accounts for every checklist: run `make migrate && make seed` on the backend. Password `Password123!` for all:

| Email | Level |
|---|---|
| `platform@demo.example.com` | Platform admin only (no organization): the platform console |
| `owner@demo.example.com` | Owner of "Demo Org", also platform admin |
| `admin@demo.example.com` | Administrator of "Demo Org" |
| `agent@demo.example.com` | Agent in "Demo Org" with custom role "Support Lead" |

Demo website widget: `website_token` `demo_website_token_0001`, allowed origin `http://localhost:5173`, identity secret `demo_widget_identity_secret_0001` ([08](08_WEBSITE_WIDGET.md)).
