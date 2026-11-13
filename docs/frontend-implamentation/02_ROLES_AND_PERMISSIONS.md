# 02 — Roles and Permissions (Frontend Implementation)

How the frontend uses roles and permissions to decide **what to show** in a multi-tenant app: which organization is active, which menu items, buttons and pages are visible, and what to do when the API says no.

Prerequisite: [01_AUTHENTICATION.md](01_AUTHENTICATION.md) (the auth state holds `memberships`). How all levels fit together in the app (route tree, guards, navigation): [05_ORGANIZATION_LEVELS.md](05_ORGANIZATION_LEVELS.md). Backend rules: [../10_AUTHENTICATION_AND_RBAC.md](../10_AUTHENTICATION_AND_RBAC.md) §4, [../09_MULTI_TENANCY.md](../09_MULTI_TENANCY.md).

## 1. The one rule

> **The backend enforces every permission. The frontend only hides what the user can't do.**

| Frontend does | Frontend never does |
|---|---|
| Hide menu items, buttons and pages the role can't use | Treat a hidden button as security; every API call is checked again on the server |
| Handle `403` and `404` from the API even when it expected success | Compute permissions itself from the role name; always use the `permissions` list from the API |
| Re-load permissions when they may have changed | Cache permissions in `localStorage` |

## 2. Where permissions come from

There is **no separate roles API call**. Login, signup and `GET /auth/me` return one `Membership` per organization ([01 §2.5](01_AUTHENTICATION.md#25-shared-response-objects)):

```json
{
  "organization_id": "00000000-0000-0000-0000-00000000b001",
  "organization_name": "Demo Org",
  "organization_slug": "demo",
  "organization_status": "active",
  "deletion_scheduled_at": null,
  "membership_id": "00000000-0000-0000-0000-00000000d002",
  "role": "agent",
  "custom_role_id": "00000000-0000-0000-0000-00000000c001",
  "permissions": [
    "ai.activity.view", "canned_response.manage", "contact.manage", "conversation.ai_control",
    "conversation.manage", "conversation.view_all", "report.view"
  ],
  "workspaces": [{ "id": "00000000-0000-0000-0000-00000000e001", "name": "Customer Support", "slug": "support" }]
}
```

| Field | Meaning for the UI |
|---|---|
| `role` | `owner` · `administrator` · `agent`. Use it for **labels** (role badge) and the member-management rules in §6, nothing else |
| `custom_role_id` | Not `null` = the agent has a custom role; its permissions **replace** the agent defaults (already applied in `permissions`). Only ever set on agents (the database enforces it) |
| `organization_status` | `active` · `suspended` · `pending_deletion`; anything but `active` shows a blocking screen ([03 §3](03_ORGANIZATIONS_AND_MEMBERS.md#3-organization-status-screens)) |
| `permissions` | The **final, effective** list for this organization. Use it for every show/hide decision |
| `workspaces` | Workspaces this user may open: **all** of them for owners/administrators, only **assigned** ones for agents |

Permissions are **per organization**: the same user can be owner in one organization and agent in another. Always read them from the membership of the **current** organization (§4).

Optional: `GET /api/v1/permissions` (logged in) returns the catalog `[{ "code", "description" }]`, which is useful to show human-readable descriptions on a "Your role" screen.

## 3. Roles and permission catalog

### 3.1 Base roles

| | owner | administrator | agent (no custom role) |
|---|---|---|---|
| Permissions | **All 25** | All except `billing.manage` (24) | 6 (below) |
| Exactly one per organization | Yes | No | No |
| Workspaces | All | All | Only assigned |
| Can manage members | Administrators and agents | Administrators and agents (never the owner) | No (can only leave) |
| Owner-only actions | Transfer ownership, delete / restore organization | — | — |

Agent defaults: `conversation.view_participating`, `conversation.view_unassigned`, `conversation.manage`, `contact.manage`, `ai.activity.view`, `conversation.ai_control`.

**Custom roles** apply to agents only and can grant any permission **except** `billing.manage`, `organization.manage`, `member.manage`, `ai.credentials.manage`. The demo agent's "Support Lead" role is an example (7 permissions above).

### 3.2 Permission → UI

Status: **now** = backend endpoints exist today; **later** = screen comes with a future backend module (hide the menu item until then).

| Permission | Unlocks in the UI | Status |
|---|---|---|
| `organization.manage` | Edit organization settings (name, locale, timezone); **Create workspace** button | now |
| `member.manage` | Invite member, pending invitations list + revoke, **Edit** / **Remove** on member rows, member emails | now |
| `audit_log.view` | "Audit log" menu item and page | now |
| `billing.manage` | Billing / subscription page | later |
| `inbox.manage` | Inboxes and channel connections settings | later |
| `team.manage` | Teams settings | later |
| `label.manage` | Labels settings | later |
| `canned_response.manage` | Canned responses settings | later |
| `custom_attribute.manage` | Custom attributes settings | later |
| `automation.manage` | Automation rules and macros | later |
| `webhook.manage` | Webhooks and API tokens | later |
| `report.view` | Reports menu item | later |
| `conversation.view_all` | "All conversations" filter | later |
| `conversation.view_unassigned` | "Unassigned" filter | later |
| `conversation.view_participating` | "Mine" filter | later |
| `conversation.manage` | Reply, assign, label, change status | later |
| `conversation.delete` | Delete conversation | later |
| `conversation.ai_control` | Pause / resume AI on a conversation ([07 §8](07_AI_AGENTS.md#8-pause-ai--return-to-ai-conversation-header)) | now (API) |
| `contact.manage` | Create / edit / merge / delete contacts | later |
| `contact.export` | Export contacts | later |
| `ai.agent.manage` | AI agent settings, link inboxes/knowledge ([07](07_AI_AGENTS.md)) | now |
| `ai.credentials.manage` | AI provider keys ([07 §4](07_AI_AGENTS.md#4-ai-provider-keys-settingsai-keys)) | now |
| `ai.knowledge.manage` | Knowledge bases: documents, chunks, test search (list is visible to every member) ([06](06_KNOWLEDGE_BASES.md)) | now |
| `ai.playground.use` | AI playground ([07 §6](07_AI_AGENTS.md#6-playground-ai-agentsagentidplayground)) | now |
| `ai.activity.view` | AI activity panel on conversations ([07 §7](07_AI_AGENTS.md#7-ai-activity-panel-conversation-sidebar)); API ready, panel lives in the conversation screen | now (API) |

Visible to **every member** (no permission needed): organization overview (plan name; limits only for owners/administrators), workspace list (filtered by `workspaces`), workspace details, member list (read-only, names and availability; `email` is `null` without `member.manage`), **Leave organization** (everyone except the owner), own account page.

Owner only (check `role === "owner"`, not a permission): **Transfer ownership**, **Delete organization**, **Restore organization**. See [03_ORGANIZATIONS_AND_MEMBERS.md](03_ORGANIZATIONS_AND_MEMBERS.md).

## 4. Current organization (multi-tenancy)

The active organization is part of the URL: **`/orgs/:orgId/...`**. Don't keep it in hidden global state, so links can be shared and the Back button keeps working.

| Situation | Behaviour |
|---|---|
| After login / at `/` | Redirect to `/orgs/<first membership's organization_id>`; if the user has no memberships, go to the account page |
| Organization switcher | A dropdown of `memberships` (`organization_name`); selecting one navigates to `/orgs/<id>` |
| `:orgId` not in `memberships` | Redirect to `/` (same as the API, which returns `404` for organizations you don't belong to) |
| Header | Organization name + role badge (Owner / Administrator / Agent) |

```ts
/** Membership for the :orgId in the URL; undefined if not a member. */
export function useCurrentMembership(): Membership | undefined {
  const { orgId } = useParams();
  const { memberships } = useAuth();
  return memberships.find((m) => m.organization_id === orgId);
}
```

**Workspaces:** the list shown to the user is `membership.workspaces`, already filtered by the backend. Agents not added to any workspace see: "You haven't been added to a workspace yet. Ask an administrator to add you."

## 5. Implementation

### 5.1 Helpers (`src/lib/permissions.ts`)

```ts
import type { Membership, MembershipRole } from "../api/types";

export const ROLE_LABEL: Record<MembershipRole, string> = {
  owner: "Owner",
  administrator: "Administrator",
  agent: "Agent",
};

/** Does the current membership grant this permission? */
export function can(m: Membership | undefined, code: string): boolean {
  return !!m && m.permissions.includes(code);
}

export function canAny(m: Membership | undefined, codes: string[]): boolean {
  return codes.some((c) => can(m, c));
}
```

### 5.2 Hiding UI

```tsx
// Component form
export function Can({ permission, children }: { permission: string; children: ReactNode }) {
  return can(useCurrentMembership(), permission) ? <>{children}</> : null;
}

<Can permission="member.manage">
  <Button onClick={openInviteDialog}>Invite member</Button>
</Can>

// Menu
{can(membership, "audit_log.view") && <NavItem to="audit-log">Audit log</NavItem>}
```

Hide, don't disable, when the user can never perform the action. Disable (with a tooltip) only when the action is temporarily unavailable (e.g. while saving).

### 5.3 Protecting pages

Hiding the menu item isn't enough: the user can type the URL. Wrap permission-only pages:

```tsx
export function RequirePermission({ permission, children }: { permission: string; children: ReactNode }) {
  const membership = useCurrentMembership();
  if (!can(membership, permission)) {
    return <Alert>Your role doesn't include access to this page.</Alert>;
  }
  return <>{children}</>;
}

<Route path="audit-log" element={
  <RequirePermission permission="audit_log.view"><AuditLog /></RequirePermission>
} />
```

Don't call the API from a page the user isn't allowed to see.

## 6. Member management rules

These mirror the backend exactly; the backend rejects anything else with `403`.

| Current user | Can invite as | Can change role of | Can remove | Role options in the dropdown |
|---|---|---|---|---|
| owner | administrator, agent | administrators and agents | administrators and agents | administrator, agent |
| administrator | administrator, agent | administrators and agents | administrators and agents | administrator, agent |
| agent (any custom role) | — | — | — | — |

Always:
- **Nobody** can edit or remove the **owner**; ownership only changes through **Transfer ownership** ([03 §5](03_ORGANIZATIONS_AND_MEMBERS.md#5-owner-only-actions)). Never offer "Owner" in a role dropdown.
- Don't show Edit/Remove on the **current user's own row** (the backend rejects it with `422`); show **Leave organization** instead (not for the owner).
- Workspace checkboxes only matter for **agents**; owners and administrators always see every workspace (show "All workspaces").
- Changing someone to administrator clears their custom role (backend does this).

```ts
/** Roles `me` may assign when inviting or editing. */
export function assignableRoles(me: Membership | undefined): MembershipRole[] {
  return can(me, "member.manage") ? ["administrator", "agent"] : [];
}

/** May `me` edit/remove this member? Never the owner, never yourself. */
export function canManageMember(
  me: Membership | undefined,
  target: { role: MembershipRole; membership_id: string },
): boolean {
  return (
    !!me &&
    can(me, "member.manage") &&
    target.role !== "owner" &&
    target.membership_id !== me.membership_id
  );
}
```

## 7. When permissions change

Permissions are resolved by the backend on **every request**, so a change applies to API calls immediately. The frontend's copy (from login / `/auth/me`) can be stale.

| Event | Frontend action |
|---|---|
| An admin changed **my** role or workspaces (I may be in another tab or device) | On the next `403 permission_denied` or unexpected `404`, call `GET /auth/me` and update `memberships`; the UI re-renders with the new permissions |
| I was **removed** from the current organization | `404` on its routes → reload `/auth/me` → the org disappears from `memberships` → §4 redirects to `/` |
| I created a workspace / accepted an invitation | Call `GET /auth/me` after success (new workspace or organization appears) |
| Tab regains focus after a long time (optional) | Reload `/auth/me` |

## 8. API errors related to access

| Status | `code` | Body extra | Show |
|---|---|---|---|
| 403 | `permission_denied` | `"permission": "<code>"` (or `"owner"` for owner-only actions) | "Your role doesn't allow this action." Then reload `/auth/me` (§7) |
| 403 | `organization_suspended` | — | Full-page "suspended" screen ([03 §3](03_ORGANIZATIONS_AND_MEMBERS.md#3-organization-status-screens)); reload `/auth/me` |
| 403 | `organization_pending_deletion` | — | Owner's "scheduled for deletion" screen with **Restore** ([03 §3](03_ORGANIZATIONS_AND_MEMBERS.md#3-organization-status-screens)) |
| 403 | `password_incorrect` | — | Under the password field: "Password is incorrect." (Not a logout: the session is fine) |
| 402 | `plan_limit_reached` | `"limit"`, `"max"` | "Your plan allows at most {max} members. Upgrade or remove someone first." |
| 404 | `not_found` | — | "This page doesn't exist or you don't have access to it." The API returns `404` (not `403`) for other organizations' and unassigned workspaces' resources, so don't try to tell "missing" and "forbidden" apart |
| 422 | `validation_error` | message | e.g. "Use transfer-ownership to change the owner" when trying to make someone owner, or editing your own row (the UI should never offer either) |
| 422 | `owner_cannot_leave` | — | "Transfer ownership before leaving." (Hide Leave for the owner) |

```json
{
  "error": {
    "code": "permission_denied",
    "message": "You do not have permission to perform this action",
    "permission": "member.manage",
    "request_id": "…"
  }
}
```

## 9. Acceptance checklist

Use the demo accounts from [01 §2.6](01_AUTHENTICATION.md#26-test-accounts-development) (password `Password123!`).

| # | Log in as | Check | Expected |
|---|---|---|---|
| 1 | owner@demo.example.com | Header | "Demo Org" + **Owner** badge |
| 2 | owner | Menu | Overview, Workspaces, Members, **Audit log** |
| 3 | owner | Members page | Invite form with roles **Administrator, Agent**; Edit/Remove on admin and agent rows; none on own row |
| 4 | owner | Create workspace | Button visible; works |
| 5 | admin@demo.example.com | Menu | Includes Audit log |
| 6 | admin | Members page | Invite roles: **Administrator, Agent**; Edit/Remove on agent and other administrator rows (not owner, not self) |
| 7 | agent@demo.example.com | Header | **Agent** badge |
| 8 | agent | Menu | No Audit log |
| 9 | agent | Members page | Read-only list, no invite form, no Edit/Remove, text "Only owners and administrators can change roles or invite people." |
| 10 | agent | Workspaces page | No "Create workspace"; only "Customer Support" listed |
| 11 | agent | Type `/orgs/<id>/audit-log` in the address bar | "Your role doesn't include access to this page."; no API call made |
| 12 | agent | Type `/orgs/<another org id>` | Redirected to `/` |
| 13 | owner (tab A) + agent (tab B) | Owner promotes agent to administrator; agent clicks something in tab B | After the next `/auth/me` reload, agent sees administrator UI |
| 14 | owner | Removes the agent; agent clicks in tab B | Agent is redirected away from Demo Org |
| 15 | user in two organizations | Switch organization | Menu, badge and buttons change to that organization's role |
