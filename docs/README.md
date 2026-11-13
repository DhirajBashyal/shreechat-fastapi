# Converse — Python Customer Communication Platform

Converse is a multi-tenant, Python-based customer communication platform with capabilities equivalent to Chatwoot: a shared inbox for website chat, Facebook Messenger, Instagram and WhatsApp, with contacts, assignment, automation, webhooks, analytics, billing and an AI agent built with LangGraph.

This directory holds the **architecture and implementation documentation**. No application code has been written yet. Implementation starts after these documents are approved.

> **Name.** "Converse" is a working name used for the Python package (`converse`) and in the examples. Rename it before the first commit if you prefer something else.

> **Relationship to Chatwoot.** These documents were written by reading the Chatwoot source code in this repository (Rails 7.2, Ruby 3.4.4, Vue 3). The Python system is designed independently. It reuses ideas and documented behaviour, not code. Chatwoot's core is MIT-licensed, and its `enterprise/` directory is under a separate proprietary licence (see `enterprise/LICENSE`). Enterprise code was only read to understand *which capabilities exist*. No Enterprise implementation details are reproduced.

## How to read these documents

| If you want to… | Read |
|---|---|
| Understand what Chatwoot does internally | [01_CHATWOOT_ANALYSIS.md](01_CHATWOOT_ANALYSIS.md) |
| See what we will build and when | [02_FEATURE_MAPPING.md](02_FEATURE_MAPPING.md), [03_PROJECT_OVERVIEW.md](03_PROJECT_OVERVIEW.md) |
| Understand the architecture | [04_TECHNOLOGY_STACK.md](04_TECHNOLOGY_STACK.md), [05_SYSTEM_ARCHITECTURE.md](05_SYSTEM_ARCHITECTURE.md) |
| Work on the database | [06](06_DATABASE_ARCHITECTURE.md), [07](07_DATABASE_SCHEMA.md), [08](08_DATABASE_RELATIONSHIPS.md), [database/](database/) |
| Understand tenancy and security | [09_MULTI_TENANCY.md](09_MULTI_TENANCY.md), [10_AUTHENTICATION_AND_RBAC.md](10_AUTHENTICATION_AND_RBAC.md), [11_SECURITY_AND_PRODUCTION_READINESS.md](11_SECURITY_AND_PRODUCTION_READINESS.md) |
| Know who sees and does what at each level (platform, organization, workspace), and the organization lifecycle | [12_ORGANIZATION_LEVELS_AND_PERMISSIONS.md](12_ORGANIZATION_LEVELS_AND_PERMISSIONS.md); how it's built: [implementation/ORGANIZATION_LEVELS_IMPLEMENTATION.md](implementation/ORGANIZATION_LEVELS_IMPLEMENTATION.md) |
| Build the frontend | [frontend-implamentation/README.md](frontend-implamentation/README.md) |
| Build a channel integration | [integrations/](integrations/) |
| Build the AI agent | [ai/](ai/) |
| Decide who may configure the AI, what the bot may do, what customers may obtain | [ai/AI_ROLES_AND_PERMISSIONS.md](ai/AI_ROLES_AND_PERMISSIONS.md) |
| Write backend code | [backend/](backend/) |
| Deploy | [deployment/](deployment/) |
| Know what to build next | [implementation/IMPLEMENTATION_ROADMAP.md](implementation/IMPLEMENTATION_ROADMAP.md) |
| Look up a term | [GLOSSARY.md](GLOSSARY.md) |

## Directory map

```text
docs/python-platform/
├── README.md                          ← you are here
├── GLOSSARY.md
├── 01_CHATWOOT_ANALYSIS.md            Source-verified analysis of Chatwoot
├── 02_FEATURE_MAPPING.md              Chatwoot module → Converse module, MVP vs later
├── 03_PROJECT_OVERVIEW.md             Goals, scope, non-goals, principles
├── 04_TECHNOLOGY_STACK.md             Each technology, why, and alternatives
├── 05_SYSTEM_ARCHITECTURE.md          Diagrams + end-to-end message flows
├── 06_DATABASE_ARCHITECTURE.md        Key decisions: IDs, tenancy columns, JSONB, encryption
├── 07_DATABASE_SCHEMA.md              Every table's DDL, grouped by domain
├── 08_DATABASE_RELATIONSHIPS.md       ER diagrams and cascade rules
├── 09_MULTI_TENANCY.md                Organizations, workspaces, RLS, request scoping
├── 10_AUTHENTICATION_AND_RBAC.md      JWT, refresh rotation, API tokens, permissions
├── 11_SECURITY_AND_PRODUCTION_READINESS.md
├── 12_ORGANIZATION_LEVELS_AND_PERMISSIONS.md  Levels, roles, who sees/does what, org lifecycle
├── frontend-implamentation/           Frontend build guides: auth, roles, organizations & members, platform console
├── integrations/                      Channel adapter contract + Meta + widget
├── ai/                                LangGraph agent, RAG, memory, handoff, AI roles & permissions
├── backend/                           Folder structure, API, pipelines, realtime, jobs, storage
├── database/                          Table docs, indexes, RLS, executable SQL, seed SQL
├── deployment/                        Docker, env vars, production, monitoring
└── implementation/                    Roadmap and phase-by-phase build guides
```

## Validation status

| Check | Result |
|---|---|
| Schema SQL (`database/INITIAL_SCHEMA_SQL.md`) | Ran against PostgreSQL 17.6 + pgvector 0.8.1 in a fresh scratch database: 66 tables, no errors |
| RLS SQL (`database/TENANT_ISOLATION.md`) | Ran. Tested: no tenant set → 0 rows; correct tenant → rows visible; other tenant → 0 rows; cross-tenant insert rejected; `channel_routes` denied to the app role; every `organization_id` table has RLS enabled + forced |
| Composite tenant FKs | Tested as the table owner (no RLS): a message pointing at another workspace's conversation and a label claiming another org's workspace are both rejected |
| Seed SQL (`database/SEED_DATA_SQL.md`) | Ran twice in a row (idempotent) |
| AI permission constraints | Tested: a new AI agent defaults to `{reply, handoff}`; an agent without `handoff` and one with an unknown action (`issue_refund`) are both rejected by `ai_agents_actions_chk` |
| Generated docs | `07_DATABASE_SCHEMA.md`, `database/INITIAL_SCHEMA_SQL.md`, `database/SEED_DATA_SQL.md`, `database/RELATIONSHIPS.md` (149 FKs) and `database/INDEXES_AND_CONSTRAINTS.md` (210 indexes, 50 checks) were generated from the executed SQL / live catalog, so they match what ran |
| Internal links and anchors | All relative links and `#anchors` across the 51 files resolve |
| `table.column` references | Every reference to a Converse column exists in the schema (the only non-matches are Chatwoot column names in "Chatwoot equivalent" cells) |
| Chatwoot file references | All 68 cited Chatwoot paths exist in this repository. See the checklist in 01 for modules that were *not* inspected |
| Mermaid diagrams | All 42 diagrams parse with Mermaid 11.4.1 |
| Not validated | Python code snippets are illustrative and were not executed; Meta Graph API details should be re-checked against Meta's current docs at implementation time |

Approval is needed before implementation begins (see [implementation/IMPLEMENTATION_ROADMAP.md](implementation/IMPLEMENTATION_ROADMAP.md)).
