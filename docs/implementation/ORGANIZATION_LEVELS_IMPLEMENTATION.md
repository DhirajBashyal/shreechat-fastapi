# Implementing Organization Levels and Permissions

How the levels in [../12_ORGANIZATION_LEVELS_AND_PERMISSIONS.md](../12_ORGANIZATION_LEVELS_AND_PERMISSIONS.md) are built in this codebase (`app/`), what each part of the code does, and what is still to come. Doc 12 says **what** the rules are; this guide says **where and how** they are enforced.

| Level | Who | Sees |
|---|---|---|
| Platform | Platform admin | All organizations (through `/api/v1/platform` only) |
| Organization | Owner (exactly one) | Everything in the organization, including billing |
| Organization | Administrator | Everything except billing and the owner-only actions |
| Workspace | Agent | Only workspaces they're added to; in those, only conversations from their inboxes, their teams, or assigned to them (filter: Phase 3) |
| Agent + custom role | Agent | Exactly the permissions the role grants |

Part of Phase 2 ([PHASE_02_AUTHENTICATION.md](PHASE_02_AUTHENTICATION.md)). The design docs ([../backend/PROJECT_STRUCTURE.md](../backend/PROJECT_STRUCTURE.md), the Phase guides) describe a planned `src/converse/modules/...` layout; the code that exists today lives in `app/` with the layout in §2. When a design snippet and this guide disagree, this guide (and the code) wins.

Frontend counterpart: [../frontend-implamentation/05_ORGANIZATION_LEVELS.md](../frontend-implamentation/05_ORGANIZATION_LEVELS.md).

**Status legend:** ✅ built and tested · 🔜 planned (where it will go is given).

## 1. How each level is enforced

| Level | Data that defines it | Code that enforces it | Status |
|---|---|---|---|
| Platform admin | `platform_admins` row (+ MFA, not built) | `require_platform_admin` in `app/api/deps.py`: `404` for non-admins, `403 mfa_required` outside `ENV=local/test` | ✅ (MFA 🔜 R1) |
| Owner | `organization_memberships.role = 'owner'`; unique index `org_memberships_one_owner_uniq` | `BASE_ROLE_PERMISSIONS[OWNER]`; `require_owner()` for owner-only actions; member rules in `app/services/tenancy.py` | ✅ |
| Administrator | `role = 'administrator'` | `BASE_ROLE_PERMISSIONS[ADMINISTRATOR]` (all but `billing.manage`); `ROLE_RANK` checks | ✅ |
| Agent | `role = 'agent'` + `workspace_memberships` (+ `inbox_members`, `team_members`) | `can_access_workspace()` in `app/services/access.py` → `404`; conversation filter `visible_conversations()` | ✅ workspaces · 🔜 conversations (Phase 3) |
| Custom role | `custom_role_id` → `roles` + `role_permissions`; check `org_memberships_custom_role_agent_chk` | `effective_permissions()` replaces agent defaults and strips `NEVER_IN_CUSTOM_ROLES` | ✅ resolution · 🔜 roles API (R2) |
| Organization boundary | `organization_id` on every tenant row | `bind_tenant()` (`SET LOCAL app.current_org_id`) on every tenant request; RLS policies ([../database/TENANT_ISOLATION.md](../database/TENANT_ISOLATION.md)) | ✅ binding · 🔜 RLS policies in migrations (Phase 3) |
| Organization status | `organizations.status`, `deletion_scheduled_at` | `_context()` in `app/api/deps.py` | ✅ |

### Request flow

```mermaid
sequenceDiagram
    participant C as Client
    participant U as get_current_user
    participant T as get_org_context / get_workspace_context
    participant A as access.resolve_membership
    participant DB as PostgreSQL
    participant H as require_permission + route + service
    C->>U: Authorization: Bearer <access JWT>
    U->>DB: user + session (must be live, not revoked)
    U->>T: CurrentUser
    T->>DB: workspace → organization_id (workspace routes)
    T->>A: resolve_membership(user, org)
    A->>DB: membership + organization + custom role permissions
    A-->>T: ResolvedMembership or None (→ 404)
    T->>T: workspace access (agents) → 404; status → 403 suspended / pending_deletion
    T->>DB: SET LOCAL app.current_org_id (bind_tenant)
    T-->>H: TenantContext
    H->>H: permission check (403) → service
```

Permissions are resolved from the database **on every request**, not from the token and not from a cache. A role change, removal or custom-role edit applies on the user's very next request; there is nothing to invalidate. If load ever requires a cache (§11), it must be invalidated on every change in §9's table and expire within 60 s.

## 2. Files

```text
app/core/permissions.py            Catalog (25 codes), BASE_ROLE_PERMISSIONS, NEVER_IN_CUSTOM_ROLES, effective_permissions()
app/core/tenancy.py                Principal, TenantContext, bind_tenant()
app/core/exceptions.py             permission_denied, not_found, organization_suspended, organization_pending_deletion,
                                   password_incorrect, plan_limit_reached, mfa_required
app/models/tenancy.py              Organization, OrganizationMembership, WorkspaceMembership, Role, RolePermission, Invitation
app/models/platform.py             User, PlatformAdmin, UserSession, SubscriptionPlan
app/services/access.py             resolve_membership(), can_access_workspace(), visible_workspaces(), list_memberships()
app/services/tenancy.py            Member rules, invitations, leave, transfer ownership, delete / restore / purge
app/services/limits.py             plan_info(), check_agent_seats()  → 402 plan_limit_reached
app/services/platform.py           Platform admin: organization list/counts/detail/update, grant/revoke admins
app/api/deps.py                    get_current_user, get_org_context, get_workspace_context, require_permission,
                                   require_owner, require_platform_admin
app/api/v1/endpoints/              organizations.py, workspaces.py, platform.py, auth.py
app/jobs/                          purge_organizations.py (daily), platform_admin.py (CLI)
migrations/versions/               a7c3e9d4f210 (deletion date + one owner), b4e8f1c6d932 (custom role on agents only)
tests/                             test_permissions.py, test_rbac_tenancy.py, test_organization_lifecycle.py,
                                   test_platform_admin.py, test_route_permissions.py
```

🔜 Phase 3 adds `app/services/visibility.py` (§8); R2 adds `app/services/roles.py` and `app/api/v1/endpoints/roles.py` (§10).

## 3. Data model and database guarantees

ORM models mirror the tested SQL ([../database/INITIAL_SCHEMA_SQL.md](../database/INITIAL_SCHEMA_SQL.md)). The parts that carry the levels:

```python
# app/models/tenancy.py (excerpt)
class OrganizationMembership(Base):
    __tablename__ = "organization_memberships"
    __table_args__ = (
        UniqueConstraint("organization_id", "user_id", name="org_memberships_user_uniq"),
        UniqueConstraint("organization_id", "id", name="org_memberships_org_id_uniq"),
        # Custom roles replace agent defaults only (docs/12 §2).
        CheckConstraint("custom_role_id IS NULL OR role = 'agent'",
                        name="org_memberships_custom_role_agent_chk"),
        org_fk("custom_role_id", "roles", ondelete="SET NULL"),
        Index("org_memberships_user_idx", "user_id"),
        # Exactly one owner per organization (docs/12 §2).
        Index("org_memberships_one_owner_uniq", "organization_id", unique=True,
              postgresql_where=text("role = 'owner'")),
    )
    role: Mapped[MembershipRole]
    custom_role_id: Mapped[uuid.UUID | None]
```

`WorkspaceMembership` rows exist for agents (and for whoever created a workspace); owners and administrators see every workspace without rows.

| Guarantee | Enforced by | Test |
|---|---|---|
| At most one owner | `org_memberships_one_owner_uniq` (partial unique index) | `test_database_allows_one_owner_and_custom_roles_on_agents_only` |
| At least one owner | Sign-up creates org + owner in one transaction; member endpoints refuse to touch the owner; leave refuses the owner; transfer demotes and promotes in one transaction | `test_administrators_manage_admins_and_agents_but_not_the_owner`, `test_members_can_leave_but_the_owner_cannot`, `test_transfer_ownership_to_an_administrator` |
| Custom roles only on agents | `org_memberships_custom_role_agent_chk`; `update_member` and `transfer_ownership` clear `custom_role_id` when someone stops being an agent | same DB test |

## 4. Permission catalog

`app/core/permissions.py` holds the 25 codes (with descriptions, served by `GET /permissions`), the base roles and the custom-role rule:

```python
def effective_permissions(role: MembershipRole, custom_role_permissions: frozenset[str] | None) -> frozenset[str]:
    """A custom role REPLACES the agent defaults. Custom roles only apply to
    agents; owners and administrators always have their full set."""
    if role == MembershipRole.AGENT and custom_role_permissions is not None:
        return custom_role_permissions - NEVER_IN_CUSTOM_ROLES
    return BASE_ROLE_PERMISSIONS[role]
```

The same catalog is seeded into `permissions` by migration `3b9d1c2e7a41_reference_data`. `tests/test_permissions.py` checks the count, base roles and the "never" rule. 🔜 Add a DB-sync test (codes in table == `PERMISSIONS`) when a DB fixture lands.

## 5. Principals and tenant context

```python
# app/core/tenancy.py
@dataclass(frozen=True, slots=True)
class Principal:
    user_id: uuid.UUID
    session_id: uuid.UUID

@dataclass(frozen=True, slots=True)
class TenantContext:
    organization_id: uuid.UUID
    workspace_id: uuid.UUID | None      # None for /organizations/{org}/... routes
    principal: Principal
    membership_id: uuid.UUID
    membership_role: MembershipRole
    custom_role_id: uuid.UUID | None
    permissions: frozenset[str]

    def has(self, permission: str) -> bool: ...
    def require_workspace(self) -> uuid.UUID: ...
```

🔜 R1 (API tokens): `Principal` becomes a union of a user principal and an API-token principal; a token's permissions are its owner's permissions ∩ its scopes (organization bot tokens: administrator set ∩ scopes); owner-only actions refuse tokens. 🔜 Phase 3: `TenantContext` gains the member's `inbox_ids` and `team_ids` for the conversation filter (§8).

## 6. Resolving a member's access

```python
# app/services/access.py (shape)
async def resolve_membership(session, user_id, organization_id) -> ResolvedMembership | None:
    # membership JOIN organization, organization not deleted
    # pending deletion → only the owner resolves (others: None → 404)
    # custom role permissions loaded when custom_role_id is set
    # permissions = effective_permissions(role, custom)

async def can_access_workspace(session, resolved, workspace_id) -> bool:
    # owner/administrator: always; agent: a workspace_memberships row
```

The lookup runs before the tenant is bound (that's how we learn which tenant to bind) and is always filtered by both `user_id` and `organization_id`, so it can't return another tenant's data. Once RLS policies are enabled (🔜 Phase 3), this lookup must run through the system role or a `SECURITY DEFINER` function, and everything after `bind_tenant()` through the RLS-enforced role.

## 7. FastAPI dependencies

```python
# app/api/deps.py (excerpt)
def _context(resolved, principal, workspace_id, *, allow_pending_deletion=False) -> TenantContext:
    # Checked after membership, so non-members still get a 404 and can't
    # learn that an organization exists or is suspended.
    status = resolved.organization.status
    if status == OrgStatus.SUSPENDED:
        raise OrganizationSuspendedError()                 # 403 organization_suspended
    if status == OrgStatus.PENDING_DELETION and not allow_pending_deletion:
        raise OrganizationPendingDeletionError()           # 403 (owner only gets this far)
    ...

async def get_workspace_context(workspace_id, current, session) -> TenantContext:
    org_id = <workspace → organization_id, not deleted>      # else 404
    resolved = await resolve_membership(...)                 # None → 404
    if not await can_access_workspace(...): raise NotFoundError()
    ctx = _context(resolved, current.principal, workspace_id)
    await bind_tenant(session, ctx.organization_id)
    return ctx

def require_permission(code, *, scope="organization"):     # → 403 permission_denied {"permission": code}
def require_owner():                                        # → 403 permission_denied {"permission": "owner"}
async def require_platform_admin(current, session):         # → 404 if not admin; 403 mfa_required outside local/test
```

Suspended organizations are closed for **everyone**, owner included. When billing ships (Phase 8), the billing routes will use a context that lets the owner through while suspended, the same way `get_org_context_for_restore` lets the owner reach `POST /restore` while pending deletion.

One route per level:

```python
# Workspace level (any member with access)
@router.get("/{workspace_id}/members")
async def list_workspace_members(ctx: WorkspaceContext, session: DbSession) -> list[MemberRead]: ...

# Organization level, permission-gated
ManageMembers = Annotated[TenantContext, Depends(require_permission("member.manage"))]
@router.delete("/organizations/{organization_id}/members/{membership_id}", status_code=204)
async def remove_member(request, membership_id, ctx: ManageMembers, session: DbSession) -> Response: ...

# Owner only
@router.post("/organizations/{organization_id}/transfer-ownership", status_code=204)
async def transfer_ownership(request, body: TransferOwnershipRequest, ctx: OwnerContext, session: DbSession): ...

# Platform level
@router.get("/organizations")                               # mounted at /platform
async def list_organizations(admin: PlatformAdminDep, session: DbSession, q=None, org_status=None, ...): ...
```

**Every route must declare its access rule.** `tests/test_route_permissions.py` walks all routes and compares them with its `EXPECTED` table (login required, tenant scope `O`/`W`, permission code, `owner` or `platform_admin`). A new route without an entry fails CI.

## 8. Agent visibility: the conversation filter (🔜 Phase 3)

Not built yet: there are no conversation endpoints. When they're added, every conversation query (list, count, detail, search, bulk) goes through one filter so they all agree, and the realtime gateway (Phase 5) uses an in-memory twin of the same rules.

```python
# app/services/visibility.py (to build)
def visible_conversations(ctx: TenantContext, stmt: Select) -> Select:
    stmt = stmt.where(Conversation.workspace_id == ctx.require_workspace())
    if ctx.has("conversation.view_all"):
        return stmt                                    # owner, administrator, or granted by a custom role
    mine = or_(Conversation.inbox_id.in_(ctx.inbox_ids) if ctx.inbox_ids else false(),
               Conversation.team_id.in_(ctx.team_ids) if ctx.team_ids else false())
    clauses = []
    if ctx.has("conversation.view_unassigned"):
        clauses.append(Conversation.assignee_id.is_(None) & mine)
    if ctx.has("conversation.view_participating"):
        clauses.append(Conversation.assignee_id == ctx.principal.user_id)
        clauses.append(Conversation.id.in_(select(ConversationParticipant.conversation_id)
                                           .where(ConversationParticipant.user_id == ctx.principal.user_id)))
    if ctx.membership_role == MembershipRole.AGENT and ctx.custom_role_id is None:
        clauses.append(mine)                           # default agents: everything in their inboxes/teams
    return stmt.where(or_(*clauses)) if clauses else stmt.where(false())
```

Single-record reads apply the same filter and return `404` when it excludes the row. Inbox lists: owners/administrators see all inboxes; agents see their `inbox_ids` unless they hold `inbox.manage`. Check the plan with `EXPLAIN ANALYZE` on a large tenant (`conversations_inbox_status_idx`, `conversations_team_idx`). A property test must show the SQL filter and the in-memory twin agree on random data.

## 9. Member management rules

All in `app/services/tenancy.py`; HTTP in `app/api/v1/endpoints/organizations.py`.

```python
ROLE_RANK = {MembershipRole.AGENT: 0, MembershipRole.ADMINISTRATOR: 1, MembershipRole.OWNER: 2}

def _check_can_manage(ctx, target_role):              # caller already holds member.manage
    if target_role == MembershipRole.OWNER:
        raise PermissionDeniedError("owner")           # nobody edits, removes or creates an owner here
    if ROLE_RANK[target_role] > ROLE_RANK[ctx.membership_role]:
        raise PermissionDeniedError("member.manage")   # never above your own role

def _check_not_self(ctx, membership):                  # edit/remove yourself → 422; use /leave
```

| Operation | Endpoint | Rules | Side effects |
|---|---|---|---|
| Invite | `POST /organizations/{org}/invitations {email, role}` | `member.manage`; role ≤ yours, never `owner`; seat check (members + pending invites) → `402` | Email with 7-day token; audit `invitation.created` |
| Accept | `POST /invitations/accept {token}` or sign-up with `invitation_token` | Email must match; seat check (members only) | Membership with the invited role; workspaces are assigned afterwards |
| Change role / workspaces | `PATCH /organizations/{org}/members/{id} {role?, workspace_ids?}` | Not the owner, not yourself, ≤ your role | `workspace_ids` replaces the set; promotion clears `custom_role_id`; audit `member.updated` |
| Remove | `DELETE /organizations/{org}/members/{id}` | Not the owner, not yourself, ≤ your role | `_detach_member()`; audit `member.removed` |
| Leave | `POST /organizations/{org}/leave` | Anyone but the owner (`422 owner_cannot_leave`) | `_detach_member()`; audit `member.left` |
| Transfer ownership | `POST /organizations/{org}/transfer-ownership {membership_id, password}` | Owner; target is an administrator (`422 new_owner_not_admin`); password (`403 password_incorrect`) | Demote, flush, promote (one-owner index is per statement); audit `organization.ownership_transferred` |
| Delete / restore | `DELETE /organizations/{org} {password}` · `POST /organizations/{org}/restore` | Owner; restore only before `deletion_scheduled_at` | Status `pending_deletion` + date; `make purge-organizations` sets `deleted_at` after 30 days |

`_detach_member()` (remove and leave): unassigns the person's unresolved conversations, deletes their `inbox_members` / `team_members` rows in the organization, revokes their personal API tokens there. Sessions are not revoked: a session is global and the person may work in other organizations; access to this one already ends on the next request (§1).

## 10. Custom roles (🔜 R2)

Resolution works today (`effective_permissions`, the DB check; the `invitations.custom_role_id` column exists but the invite API doesn't accept it yet). The management API is still to build:

```python
# app/services/roles.py (to build)
def validate_permissions(codes: set[str]) -> None:
    unknown = codes - ALL_PERMISSIONS
    if unknown:
        raise ValidationFailedError("Unknown permissions", code="unknown_permissions", extra={"details": sorted(unknown)})
    forbidden = codes & NEVER_IN_CUSTOM_ROLES
    if forbidden:
        raise ValidationFailedError("Not grantable to custom roles", code="permission_not_grantable",
                                    extra={"details": sorted(forbidden)})
```

Endpoints `GET/POST/PATCH/DELETE /organizations/{org}/roles[/{id}]` (`member.manage`), every change audited (`role.created` / `role.updated` / `role.deleted`). Assigning a custom role: `PATCH .../members/{id} {custom_role_id}` only when the member is an agent (else `422`, also the DB check). Deleting a role sets members' `custom_role_id` to `NULL` (FK `ON DELETE SET NULL`), so they fall back to the agent defaults. No cache to invalidate (§1).

## 11. Platform admin

- Router `app/api/v1/endpoints/platform.py`, mounted at `/api/v1/platform`; every route depends on `require_platform_admin`. Implemented: `GET /stats`, `GET /organizations` (search `q`, `status`, pagination, global `counts`), `GET/PATCH /organizations/{org}` (suspend/reactivate, `limits_override`, `feature_flags`), `GET/POST/DELETE /admins`.
- Reads across organizations. Today no RLS policies are enabled, so it uses the normal session; 🔜 when RLS lands (Phase 3) these queries move to the system role (`converse_system`, BYPASSRLS), allowed only in `app/services/platform.py`.
- Every change writes `audit_logs` with `actor_kind = 'platform_admin'` and the affected `organization_id`. 🔜 Add a required `reason` field to PATCH bodies.
- Platform admins are **not** members of any organization: the tenant API still returns `404` for them. Seeing a tenant's conversations needs impersonation (🔜 R2: 30-minute token with an `act` claim, banner, audited).
- First admin: `make platform-admin ARGS="grant <email> [--create --name ...]"` (password prompted, never on the command line). Further admins: `POST /platform/admins` or the same command.
- 🔜 R1 MFA: TOTP enrolment, an `mfa` claim in the access token after the second factor, and `require_platform_admin` checking that claim instead of `ENV`.

## 12. Tests

| File | Covers |
|---|---|
| `tests/test_permissions.py` | 25 codes, base roles, custom role replaces defaults and never grants reserved codes, custom roles ignored for owner/admin |
| `tests/test_route_permissions.py` | Every route declares login / tenant scope / permission / `owner` / `platform_admin`; required codes exist |
| `tests/test_rbac_tenancy.py` | Agent forbidden from admin actions; workspace access 404; admins manage admins and agents but not the owner or themselves; agents don't see emails; plan/limits by role; role change and removal apply immediately; cross-organization 404 |
| `tests/test_organization_lifecycle.py` | Transfer ownership; leave; removal unassigns open conversations and inbox membership; delete / restore / purge; suspension (403 for members, 404 for strangers); plan limit 402; one-owner index and custom-role check in the database |
| `tests/test_platform_admin.py` | Hidden (404) from non-admins; counts and search across organizations; suspend / reactivate / limits with audit; grant and revoke admins; MFA guard outside local |

Run: `ENV_FILE=.env.test uv run pytest` (needs the test DB migrated: `ENV_FILE=.env.test uv run alembic upgrade head`).

🔜 Still to add: a full role × endpoint matrix once conversation, label and billing endpoints exist (owner / administrator / agent / custom-role agent → expected status per route); the automatic cross-organization suite over every route ([TESTING_STRATEGY.md](TESTING_STRATEGY.md#4-tenant-isolation-suite-automatic)); the SQL vs in-memory visibility property test.

## 13. Build order and status

| # | Step | Status |
|---|---|---|
| 1 | ORM models matching the tested schema | ✅ |
| 2 | Permission catalog + base roles + `effective_permissions` | ✅ |
| 3 | `Principal`, `TenantContext`, `bind_tenant` | ✅ |
| 4 | `resolve_membership`, `can_access_workspace` (per request, no cache) | ✅ |
| 5 | `get_org_context`, `get_workspace_context`, `require_permission`, `require_owner`, `require_platform_admin` | ✅ |
| 6 | Sign-up (creates owner) and member rules: invite, accept, change role, remove, leave, transfer ownership, with audit | ✅ |
| 7 | Organization lifecycle: suspended / pending deletion gates, delete, restore, purge job | ✅ |
| 8 | Plan seat limit (`402`) | ✅ |
| 9 | Platform router + CLI | ✅ (MFA 🔜 R1) |
| 10 | DB guarantees: one owner, custom roles on agents only | ✅ |
| 11 | RLS policies in migrations; system role for pre-binding lookups and the platform API | 🔜 Phase 3 |
| 12 | `visible_conversations` + repository usage | 🔜 Phase 3; in-memory twin Phase 5 |
| 13 | Teams and API tokens with scopes | 🔜 R1 |
| 14 | Custom roles API | 🔜 R2 |
| 15 | Full role matrix + cross-organization suite | 🔜 with the endpoints above |

## 14. Database guarantees added for this

Migrations `a7c3e9d4f210_organization_lifecycle` and `b4e8f1c6d932_custom_role_agents_only`, applied and tested on PostgreSQL 17 (pgvector image); also added to [../database/INITIAL_SCHEMA_SQL.md](../database/INITIAL_SCHEMA_SQL.md) and [../database/INDEXES_AND_CONSTRAINTS.md](../database/INDEXES_AND_CONSTRAINTS.md):

```sql
ALTER TABLE organizations ADD COLUMN deletion_scheduled_at timestamptz;

CREATE UNIQUE INDEX org_memberships_one_owner_uniq
    ON organization_memberships (organization_id) WHERE role = 'owner';

ALTER TABLE organization_memberships
    ADD CONSTRAINT org_memberships_custom_role_agent_chk CHECK (custom_role_id IS NULL OR role = 'agent');
```

| Test | Result |
|---|---|
| Make a second member `owner` | `IntegrityError … "org_memberships_one_owner_uniq"` |
| Give a custom role to an administrator | `IntegrityError … "org_memberships_custom_role_agent_chk"` |
| Transfer: demote owner, flush, promote administrator, one transaction | Succeeds; exactly one owner afterwards |
