# 05 — Organization Levels in the Frontend (Implementation Guide)

How the frontend implements the five levels from [../12_ORGANIZATION_LEVELS_AND_PERMISSIONS.md](../12_ORGANIZATION_LEVELS_AND_PERMISSIONS.md): how the app knows which level a user is at, which guard protects which route, what each level sees, the shared code (types, hooks, guards), and what's built on the backend today versus planned. It's the frontend twin of the backend guide [../implementation/ORGANIZATION_LEVELS_IMPLEMENTATION.md](../implementation/ORGANIZATION_LEVELS_IMPLEMENTATION.md).

The screens themselves are specified in the other docs; this one ties them together:

| Doc | Covers |
|---|---|
| [01_AUTHENTICATION.md](01_AUTHENTICATION.md) | Login, tokens, `/auth/me`, auth state |
| [02_ROLES_AND_PERMISSIONS.md](02_ROLES_AND_PERMISSIONS.md) | `can()`, hiding UI, member-management rules, permission errors |
| [03_ORGANIZATIONS_AND_MEMBERS.md](03_ORGANIZATIONS_AND_MEMBERS.md) | Organization switcher, status screens, members page, invitations, owner actions |
| [04_PLATFORM_ADMIN.md](04_PLATFORM_ADMIN.md) | Platform console |

**Status legend:** ✅ backend endpoints exist (build the screen now) · 🔜 backend planned (hide the entry point; build when the API lands).

## 1. The levels and how the UI detects them

| Level | Who | Sees | Detected from | Status |
|---|---|---|---|---|
| Platform | Platform admin | All organizations, in the platform console only | `is_platform_admin` (login / `GET /auth/me`) | ✅ (dev only until MFA, §8) |
| Organization | Owner (exactly one) | Everything in the organization, incl. billing and owner actions | `membership.role === "owner"` | ✅ (billing 🔜) |
| Organization | Administrator | Everything except billing and owner actions | `membership.role === "administrator"` | ✅ |
| Workspace | Agent | Only assigned workspaces; in them, only conversations from their inboxes, teams, or assigned to them | `membership.workspaces` (already filtered); conversation lists filtered by the API | ✅ workspaces · 🔜 conversations |
| Agent + custom role | Agent | Exactly what the role grants | `membership.permissions` (already resolved); `custom_role_id !== null` | ✅ resolution · 🔜 roles editor |

Three rules follow from the backend design and apply everywhere:

1. **Show/hide by `permissions`, never by role name** ([02 §1](02_ROLES_AND_PERMISSIONS.md#1-the-one-rule)). The role is used only for the badge, member-management rules and the owner-only actions, which aren't permissions.
2. **Never filter data yourself.** The API returns only what the user may see (workspaces, members' emails, limits, and later conversations and inboxes). Render what comes back; don't hide rows client-side "for safety".
3. **`404` means "not for you" as often as "doesn't exist".** The backend uses `404` for other organizations, unassigned workspaces and the platform API, so the UI shows one neutral message and never tries to tell them apart.

### One person, many levels

A single account can hold several levels at once: owner of A, agent in B, and platform admin. The UI always shows **one** organization at a time (from the URL), with that membership's role and permissions. The platform console is a separate area with its own layout. Nothing from one organization leaks into another view: no cached lists keyed only by workspace id, no "recent conversations" across organizations.

## 2. Where each level's data comes from

All level information arrives in **one** response: login, signup and `GET /auth/me` ([01 §2.5](01_AUTHENTICATION.md#25-shared-response-objects)). There is no separate roles call.

```ts
// src/api/types.ts (the parts that carry levels)
export type MembershipRole = "owner" | "administrator" | "agent";
export type OrgStatus = "active" | "suspended" | "pending_deletion";

export interface Membership {
  organization_id: string;
  organization_name: string;
  organization_slug: string;
  organization_status: OrgStatus;          // gate, §4
  deletion_scheduled_at: string | null;
  membership_id: string;
  role: MembershipRole;                    // badge, member rules, owner actions
  custom_role_id: string | null;           // only ever set for agents (DB-enforced)
  permissions: string[];                   // every show/hide decision
  workspaces: WorkspaceSummary[];          // all for owner/admin, assigned for agents
}

export interface MeResponse {
  user: User;
  memberships: Membership[];
  is_platform_admin: boolean;              // platform console link + route
}
```

Keep this in the auth store; reload it with `reloadMe()` after anything that may change it ([03 §2.3](03_ORGANIZATIONS_AND_MEMBERS.md#23-after-a-successful-call-refresh-the-auth-state)) and on the error codes in §6. Never persist it in `localStorage`.

## 3. Files

```text
src/api/types.ts                     Membership, MeResponse, Organization, Member, Invitation, Platform* types
src/api/client.ts                    fetch wrapper: headers, refresh on 401, error → ApiError (01 §5)
src/auth/AuthProvider.tsx            user, memberships, isPlatformAdmin, reloadMe()
src/lib/permissions.ts               ROLE_LABEL, can(), canAny(), isOwner(), assignableRoles(), canManageMember()
src/lib/levels.ts                    useCurrentMembership(), useCurrentWorkspace(), homePath()
src/routes/guards.tsx                RequireAuth, RequireMembership, OrgStatusGate, RequireWorkspace,
                                     RequirePermission, RequireOwner, RequirePlatformAdmin
src/routes/router.tsx                route tree (§4)
src/layouts/AppLayout.tsx            org header, switchers, navigation (§5)
src/layouts/PlatformLayout.tsx       platform console chrome
src/pages/org/…                      overview, members, settings, workspaces, audit log (03)
src/pages/platform/…                 dashboard, organization detail, admins (04)
src/pages/status/…                   SuspendedScreen, PendingDeletionScreen (03 §3)
```

## 4. Route tree and guards

Every route sits behind the guards for its level. Guards only decide what to render; the API still checks everything.

```text
/login, /signup, /forgot-password, /reset-password   public (01)
/accept-invitation                                   public; logged-in and logged-out paths (03 §7)
RequireAuth
├── /                                                → homePath()
├── /account                                         own account
├── RequirePlatformAdmin
│   └── /platform/**                                 platform console (04)
└── /orgs/:orgId                RequireMembership → OrgStatusGate
    ├── (index)                                      overview: every member
    ├── members                                      every member (actions by permission)
    ├── workspaces                                   every member (create: organization.manage)
    ├── settings                RequirePermission("organization.manage"); danger zone: RequireOwner
    ├── audit-log               RequirePermission("audit_log.view")
    ├── billing 🔜              RequirePermission("billing.manage")
    ├── roles 🔜                RequirePermission("member.manage")
    └── ws/:wsId                RequireWorkspace
        ├── (index)                                  workspace home
        ├── conversations 🔜    any of conversation.view_*
        ├── contacts 🔜         contact.manage
        ├── knowledge           every member (list); documents/search: ai.knowledge.manage (06)
        ├── ai-agents           every member (read); edit: ai.agent.manage; playground: ai.playground.use (07)
        ├── reports 🔜          report.view
        └── settings/** 🔜      inbox.manage / team.manage / label.manage / …
```

| Guard | Passes when | Otherwise |
|---|---|---|
| `RequireAuth` | `status === "authenticated"` | `/login?next=…` ([01 §5.5](01_AUTHENTICATION.md#55-route-protection)) |
| `RequirePlatformAdmin` | `isPlatformAdmin` | Redirect `/` |
| `RequireMembership` | `:orgId` is in `memberships` | Redirect `/` (same as the API's `404`) |
| `OrgStatusGate` | `organization_status === "active"` | Suspended / pending-deletion screen ([03 §3](03_ORGANIZATIONS_AND_MEMBERS.md#3-organization-status-screens)) |
| `RequireWorkspace` | `:wsId` is in `membership.workspaces` | "This workspace doesn't exist or you don't have access to it." + link to the organization |
| `RequirePermission(code)` | `can(membership, code)` | "Your role doesn't include access to this page." No API call |
| `RequireOwner` | `membership.role === "owner"` | Not rendered (owner-only sections are inside other pages) |

```tsx
// src/routes/guards.tsx
export function RequireMembership({ children }: { children: ReactNode }) {
  return useCurrentMembership() ? <>{children}</> : <Navigate to="/" replace />;
}

export function RequireWorkspace({ children }: { children: ReactNode }) {
  const m = useCurrentMembership();
  const { wsId } = useParams();
  if (!m?.workspaces.some((w) => w.id === wsId)) return <NoAccess />;
  return <>{children}</>;
}

export function RequireOwner({ children }: { children: ReactNode }) {
  return isOwner(useCurrentMembership()) ? <>{children}</> : null;
}

export function RequirePlatformAdmin({ children }: { children: ReactNode }) {
  return useAuth().isPlatformAdmin ? <>{children}</> : <Navigate to="/" replace />;
}
// RequirePermission and OrgStatusGate: see 02 §5.3 and 03 §3.
```

```ts
// src/lib/levels.ts
export function useCurrentMembership(): Membership | undefined {
  const { orgId } = useParams();
  return useAuth().memberships.find((m) => m.organization_id === orgId);
}

/** Where to land after login or at "/". */
export function homePath(me: { memberships: Membership[]; isPlatformAdmin: boolean }): string {
  const first = me.memberships.find((m) => m.organization_status !== "pending_deletion")
    ?? me.memberships[0];
  if (first) return `/orgs/${first.organization_id}`;
  if (me.isPlatformAdmin) return "/platform";        // staff account with no organization
  return "/account";
}

// src/lib/permissions.ts (additions to 02 §5.1)
export const isOwner = (m: Membership | undefined) => m?.role === "owner";
```

## 5. What each level sees

### 5.1 Navigation

| Menu item | Platform admin* | Owner | Administrator | Agent | Agent + custom role | Status |
|---|---|---|---|---|---|---|
| Platform console | ✅ | — | — | — | — | ✅ |
| Overview | — | ✅ | ✅ | ✅ | ✅ | ✅ |
| Workspaces | — | ✅ all | ✅ all | assigned only | assigned only | ✅ |
| Members | — | ✅ manage | ✅ manage | read-only | read-only | ✅ |
| Settings | — | ✅ + danger zone | ✅ | — | — | ✅ |
| Audit log | — | ✅ | ✅ | — | if granted | ✅ |
| Billing | — | ✅ | — | — | never | 🔜 |
| Roles | — | ✅ | ✅ | — | never | 🔜 |
| Conversations | — | All / Unassigned / Mine | All / Unassigned / Mine | Unassigned / Mine | per granted `view_*` | 🔜 |
| Contacts | — | ✅ | ✅ | ✅ | if granted | 🔜 |
| Reports | — | ✅ | ✅ | — | if granted | 🔜 |
| Knowledge (workspace) | — | ✅ manage | ✅ manage | list only | manage if granted | ✅ ([06](06_KNOWLEDGE_BASES.md)) |
| AI agents (workspace) | — | ✅ manage + playground | ✅ manage + playground | read-only list | per granted `ai.*` | ✅ ([07](07_AI_AGENTS.md)) |
| Inbox / team / label / automation settings | — | ✅ | ✅ | — | each if granted | 🔜 |

\* A platform admin sees these only in organizations they're also a **member** of, with that membership's role. Being a platform admin adds the console, nothing inside tenants.

Build the menu from one table so the rules live in one place:

```ts
// src/layouts/nav.ts
type NavItem = { to: string; label: string; show: (m: Membership) => boolean; ready: boolean };

export const ORG_NAV: NavItem[] = [
  { to: "",          label: "Overview",   show: () => true,                               ready: true },
  { to: "workspaces",label: "Workspaces", show: () => true,                               ready: true },
  { to: "members",   label: "Members",    show: () => true,                               ready: true },
  { to: "settings",  label: "Settings",   show: (m) => can(m, "organization.manage"),     ready: true },
  { to: "audit-log", label: "Audit log",  show: (m) => can(m, "audit_log.view"),          ready: true },
  { to: "billing",   label: "Billing",    show: (m) => can(m, "billing.manage"),          ready: false },
  { to: "roles",     label: "Roles",      show: (m) => can(m, "member.manage"),           ready: false },
];

export const visibleNav = (m: Membership) => ORG_NAV.filter((i) => i.ready && i.show(m));
```

Flip `ready` to `true` when the backend endpoint ships (see the status column in [02 §3.2](02_ROLES_AND_PERMISSIONS.md#32-permission--ui)).

### 5.2 Data each level gets from the API

The backend already trims responses per level. Design the components for the trimmed shape:

| Data | Owner / Administrator | Agent | Where |
|---|---|---|---|
| `GET /organizations/{org}` `limits` | object | `null`: hide seat usage and limits | [03 §4](03_ORGANIZATIONS_AND_MEMBERS.md#4-organization-overview-and-settings) |
| `plan.name` | ✅ | ✅ | overview |
| Member `email` | ✅ | `null`: hide the column | [03 §6.1](03_ORGANIZATIONS_AND_MEMBERS.md#61-layout-by-role) |
| `membership.workspaces` | every workspace | assigned only | switcher |
| Conversations 🔜 | all in workspace | inbox/team/assigned only | list renders what's returned |
| Platform lists | only via `/platform/*` for platform admins | — | [04](04_PLATFORM_ADMIN.md) |

### 5.3 Example: Acme Corp (from doc 12 §6)

| Person | Level | What the UI shows |
|---|---|---|
| Priya | Owner | All menu items that are ready; Members with emails and Edit/Remove on every non-owner row; Settings with the **danger zone** (transfer, delete); no Leave (link to transfer instead) |
| Omar | Administrator | Same as Priya minus the danger zone (and Billing when it ships); Edit/Remove on agent and other administrator rows, not on Priya or himself; **Leave** available |
| Arjun | Agent | Overview, Workspaces (only "Support"), Members read-only without emails; **Leave**; opening `/orgs/acme/ws/<sales>` shows "no access"; opening `/orgs/acme/settings` shows "Your role doesn't include access to this page." |
| Lena | Agent + "Support Lead" | Arjun's view plus whatever her permissions grant: Reports and the "All conversations" filter once those screens exist. Badge "Agent", with a "Support Lead" sub-label when the roles API ships |

## 6. Handling the backend's answers per level

| Response | Means | Frontend action |
|---|---|---|
| `401` (`unauthenticated`, `token_expired`) | Access token expired | Silent refresh, retry once ([01 §5.2](01_AUTHENTICATION.md#52-expired-access-token-during-use)) |
| `403 permission_denied` | My role/permissions changed | Message + `reloadMe()`; the UI re-renders for the new level |
| `403 permission_denied` with `permission: "owner"` | I'm no longer the owner | `reloadMe()`; the danger zone disappears |
| `403 organization_suspended` / `organization_pending_deletion` | Organization status changed | `reloadMe()` → `OrgStatusGate` shows the screen |
| `403 password_incorrect` | Wrong password in an owner action | Field error; **not** a logout |
| `403 mfa_required` | Platform console on a server without MFA | Message from [04 §1](04_PLATFORM_ADMIN.md#1-who-gets-in) |
| `404` on an org/workspace route | Removed from org / workspace, or never had access | `reloadMe()`; guards redirect or show "no access" |
| `404` on `/platform/*` | Platform admin role revoked | `reloadMe()` → redirect `/` |
| `402 plan_limit_reached` | Seat limit | Plan message ([03 §6.2](03_ORGANIZATIONS_AND_MEMBERS.md#62-invite)) |

Put this in one place, the API client's error hook, so every page behaves the same:

```ts
// src/api/client.ts (error hook)
const RELOAD_ON = new Set([
  "permission_denied", "organization_suspended", "organization_pending_deletion", "not_found",
]);

export function onApiError(err: ApiError, auth: AuthApi) {
  if (err.status === 403 && err.code === "password_incorrect") return;   // the form shows it
  if (RELOAD_ON.has(err.code)) void auth.reloadMe();                     // guards react to the new state
}
```

Debounce `reloadMe()` (one in flight, at most every few seconds) so a page that fires several failing calls reloads only once.

## 7. Planned levels work (🔜), and how to prepare now

| Backend item | When | Frontend now | Frontend when it lands |
|---|---|---|---|
| Conversation visibility filter | Phase 3 | Nothing to build | Conversation list with filter tabs from `conversation.view_all` / `view_unassigned` / `view_participating`; render exactly what the API returns; `404` on a conversation id → "no access" |
| Inbox and team membership | Phase 3 / R1 | Member edit dialog only has role + workspaces | Add inbox and team pickers to the member edit dialog (agents only) |
| Custom roles API | R2 | Show only the role badge; `custom_role_id` exists but has no name to show | Roles page (`member.manage`): list, create, edit with a permission checklist from `GET /permissions`; **disable the 4 "never" codes** (`billing.manage`, `member.manage`, `organization.manage`, `ai.credentials.manage`); custom-role picker in member edit, only when the role is Agent |
| Billing | Phase 8 | Hide Billing | Billing page (`billing.manage`); owner's link from the suspended screen |
| MFA | R1 | Platform console works in dev only | MFA enrolment in the account page; second step on login; platform console requires it |
| Impersonation | R2 | — | Platform console "View as": a banner across the whole app while active, with "Stop" |

## 8. Platform level specifics

- Separate layout (`PlatformLayout`) and colour so staff never confuse it with a tenant.
- Entry: "Platform console" in the user menu when `isPlatformAdmin`; staff accounts with no organizations land there (`homePath`).
- Being a platform admin **does not** open `/orgs/:id` for organizations the user isn't a member of; the console's organization detail page is the only view of other tenants.
- Until MFA ships, every platform call outside development returns `403 mfa_required`; show the message from [04 §1](04_PLATFORM_ADMIN.md#1-who-gets-in) instead of the console.

## 9. Build order

| # | Step | Depends on |
|---|---|---|
| 1 | Types + API client with the error hook (§2, §6) | 01 |
| 2 | Auth store with `memberships`, `isPlatformAdmin`, debounced `reloadMe()` | 01 §5 |
| 3 | `levels.ts`, `permissions.ts` helpers | — |
| 4 | Guards + route tree (§4); `homePath()` | 2, 3 |
| 5 | App layout: organization switcher with status tags, workspace switcher, nav from `ORG_NAV` | 4 |
| 6 | Status screens (suspended, pending deletion) | 03 §3 |
| 7 | Organization overview, members page, invitations, accept-invitation page | 03 §4–7 |
| 8 | Settings + owner danger zone (transfer, delete, restore) | 03 §5 |
| 9 | Audit log page | 02 |
| 10 | Platform console | 04 |
| 11 | 🔜 Conversations, roles, billing, MFA as their APIs land | §7 |

## 10. Acceptance checklist (level × screen)

Run `make migrate && make seed` on the backend. Demo accounts (password `Password123!`): `platform@demo.example.com` (platform admin, no organization), `owner@demo.example.com` (owner, also platform admin), `admin@demo.example.com` (administrator), `agent@demo.example.com` (agent with custom role "Support Lead"). Make a plain agent by inviting a new email as Agent.

| # | Level | Check | Expected |
|---|---|---|---|
| 1 | Owner | Menu | Overview, Workspaces, Members, Settings, Audit log, Platform console (user menu) |
| 2 | Owner | Settings | Danger zone with Transfer ownership and Delete organization |
| 3 | Administrator | Menu / Settings | Same menu, no Platform console; Settings without danger zone |
| 4 | Administrator | Members | Edit/Remove on agent and other administrator rows only; Leave visible |
| 5 | Agent (plain) | Menu | Overview, Workspaces, Members; no Settings, no Audit log |
| 6 | Agent (plain) | Members | No email column, no actions except Leave |
| 7 | Agent (plain) | Address bar `/orgs/<id>/settings` | "Your role doesn't include access to this page."; no API call |
| 8 | Agent (plain) | Address bar `/orgs/<id>/ws/<unassigned ws>` | "no access" page; no API call |
| 9 | Agent + custom role | Menu | Plain agent's menu plus every item its permissions grant that is `ready` (e.g. Audit log if granted) |
| 10 | Any member | Owner promotes / demotes them in another window, then they click something | After the `403`/`404` → `reloadMe()`, the menu matches the new level |
| 11 | Any member | Platform admin suspends the organization | Next call → suspended screen; switcher shows "Suspended" tag |
| 12 | `platform@demo.example.com` | Log in | Lands on `/platform` |
| 13 | Non-admin | Address bar `/platform` | Redirect to `/` |
| 14 | User in two organizations with different roles | Switch organization | Badge, menu and actions change; nothing from the other organization stays on screen |
