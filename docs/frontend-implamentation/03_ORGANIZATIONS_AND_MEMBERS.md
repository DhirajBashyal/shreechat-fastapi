# 03 — Organizations, Workspaces and Members (Frontend Implementation)

How the frontend implements the organization level of the app: switching organizations and workspaces, the blocking screens for suspended and deleted organizations, the members page (invite, edit, remove, leave), invitation acceptance, and the owner-only actions (transfer ownership, delete, restore).

Prerequisites: [01_AUTHENTICATION.md](01_AUTHENTICATION.md) (auth state, `memberships`, error format) and [02_ROLES_AND_PERMISSIONS.md](02_ROLES_AND_PERMISSIONS.md) (`can()`, `useCurrentMembership()`, member-management rules). Backend rules: [../12_ORGANIZATION_LEVELS_AND_PERMISSIONS.md](../12_ORGANIZATION_LEVELS_AND_PERMISSIONS.md).

All URLs below are relative to `/api/v1`. Every request sends the headers from [01 §2.2](01_AUTHENTICATION.md#22-headers-on-every-request).

## 1. Levels as the UI sees them

| Level | In the URL | Where the list comes from | Who sees what |
|---|---|---|---|
| Organization | `/orgs/:orgId` | `memberships` (login / `GET /auth/me`) | Only organizations the user is a member of. Never a list of all organizations (that's the platform admin console, not this app) |
| Workspace | `/orgs/:orgId/ws/:wsId` | `membership.workspaces` | Owner/administrator: all workspaces. Agent: only assigned ones |
| Inbox, team | `/orgs/:orgId/ws/:wsId/...` | Workspace endpoints (Phase 3+) | Filtered by the backend |

```text
/orgs/:orgId                       Organization overview (every member)
/orgs/:orgId/members               Members + invitations (§6)
/orgs/:orgId/settings              Organization settings (organization.manage) + danger zone (owner, §5)
/orgs/:orgId/workspaces            Workspace list (+ create with organization.manage)
/orgs/:orgId/audit-log             Audit log (audit_log.view)
/orgs/:orgId/ws/:wsId              Workspace home (inbox, conversations later)
/accept-invitation?token=…         Invitation link from the email (§7)
```

**Switchers.** The organization switcher lists `memberships` (name + role badge). The workspace switcher lists `membership.workspaces` of the current organization. Remember the last workspace per organization in `localStorage` as a convenience only (key `lastWs:<orgId>`); if it isn't in `membership.workspaces` any more, fall back to the first one.

```ts
/** Redirect target for /orgs/:orgId (organization home → a workspace). */
export function defaultWorkspaceId(m: Membership): string | undefined {
  let last: string | null = null;
  try { last = localStorage.getItem(`lastWs:${m.organization_id}`); } catch { /* private mode */ }
  return m.workspaces.find((w) => w.id === last)?.id ?? m.workspaces[0]?.id;
}
```

Agents with no workspace see: "You haven't been added to a workspace yet. Ask an administrator to add you." ([02 §4](02_ROLES_AND_PERMISSIONS.md#4-current-organization-multi-tenancy)).

## 2. Types and endpoints

### 2.1 Types (`src/api/types.ts`, additions)

```ts
export type OrgStatus = "active" | "suspended" | "pending_deletion";
export type Availability = "online" | "busy" | "offline";

export interface Organization {
  id: string; name: string; slug: string; status: OrgStatus;
  locale: string; timezone: string; created_at: string;
  deletion_scheduled_at: string | null;
  /** Only on GET /organizations/{org}; every member sees the plan name. */
  plan?: { code: string | null; name: string | null } | null;
  /** null for agents. A missing key means unlimited. */
  limits?: { agents?: number; inboxes?: number; workspaces?: number; [k: string]: unknown } | null;
}

export interface Workspace {
  id: string; organization_id: string; name: string; slug: string;
  timezone: string; created_at: string;
}

export interface Member {
  membership_id: string; user_id: string; name: string;
  /** null unless the current user has member.manage */
  email: string | null;
  role: MembershipRole; custom_role_id: string | null;
  availability: Availability;
  /** Explicit workspace assignments (meaningful for agents only). */
  workspace_ids: string[];
}

export interface Invitation {
  id: string; email: string; role: MembershipRole;
  expires_at: string; accepted_at: string | null; created_at: string;
}

export interface AuditLogEntry {
  id: number; action: string; actor_kind: string; actor_id: string | null;
  resource_type: string; resource_id: string | null;
  changes: Record<string, unknown>; created_at: string;
}
```

`Membership` (from [01 §2.5](01_AUTHENTICATION.md#25-shared-response-objects)) now also has `organization_status: OrgStatus` and `deletion_scheduled_at: string | null`.

### 2.2 Endpoints

| Method | URL | Body | Success | Who (hide otherwise) |
|---|---|---|---|---|
| GET | `/organizations/{org}` | — | `200 Organization` (with `plan`, `limits`) | every member |
| PATCH | `/organizations/{org}` | `{ name?, locale?, timezone? }` | `200 Organization` | `organization.manage` |
| GET | `/organizations/{org}/workspaces` | — | `200 Workspace[]` | every member |
| POST | `/organizations/{org}/workspaces` | `{ name, slug?, timezone? }` (`slug`: `a-z 0-9 -`) | `201 Workspace` | `organization.manage` |
| GET | `/workspaces/{ws}` | — | `200 Workspace` | members with access |
| GET | `/workspaces/{ws}/members` | — | `200 Member[]` | members with access |
| GET | `/organizations/{org}/members` | — | `200 Member[]` | every member |
| PATCH | `/organizations/{org}/members/{membership_id}` | `{ role?, workspace_ids? }` (`workspace_ids` **replaces** the set) | `200 Member` | `member.manage`, not owner row, not own row |
| DELETE | `/organizations/{org}/members/{membership_id}` | — | `204` | `member.manage`, not owner row, not own row |
| POST | `/organizations/{org}/leave` | — | `204` | every member **except the owner** |
| POST | `/organizations/{org}/invitations` | `{ email, role }` (`administrator` or `agent`) | `201 Invitation` | `member.manage` |
| GET | `/organizations/{org}/invitations` | — | `200 Invitation[]` (pending only) | `member.manage` |
| DELETE | `/organizations/{org}/invitations/{id}` | — | `204` | `member.manage` |
| POST | `/invitations/accept` | `{ token }` (logged in) | `200 MeResponse` | invited user |
| POST | `/organizations/{org}/transfer-ownership` | `{ membership_id, password }` | `204` | **owner** |
| DELETE | `/organizations/{org}` | `{ password }` (JSON body on DELETE) | `200 Organization` (`pending_deletion`) | **owner** |
| POST | `/organizations/{org}/restore` | — | `200 Organization` (`active`) | **owner**, while pending deletion |
| GET | `/organizations/{org}/audit-logs` | — | `200 AuditLogEntry[]` (latest 100) | `audit_log.view` |

`DELETE` with a body works with `fetch` (`{ method: "DELETE", body: JSON.stringify({ password }) }`); with axios pass it as `axios.delete(url, { data: { password } })`.

### 2.3 After a successful call, refresh the auth state

The auth state's `memberships` is the source for roles, permissions, workspaces and organization status, so reload it (`reloadMe()`, [01 §3.8](01_AUTHENTICATION.md#38-get-authme)) after anything that changes them:

| Action | Then |
|---|---|
| Create workspace | `reloadMe()`; the new workspace appears in the switcher |
| Change **my own** role or workspaces (another admin did it) | Handled by [02 §7](02_ROLES_AND_PERMISSIONS.md#7-when-permissions-change) |
| Accept invitation | Use the returned `MeResponse` directly; navigate to `/orgs/<new org id>` |
| Leave organization | `reloadMe()`, navigate to `/` |
| Transfer ownership | `reloadMe()` (I'm now administrator); refetch members |
| Delete organization | `reloadMe()`; the status gate (§3) shows the "scheduled for deletion" screen |
| Restore organization | `reloadMe()`; the app comes back |
| Edit / remove someone else, invite, revoke | Refetch the members / invitations list only |

## 3. Organization status screens

Put a gate around everything under `/orgs/:orgId`. It reads `organization_status` from the current membership and shows a full-page screen instead of the app when the organization isn't active. Don't call organization APIs behind the gate: they return `403`.

| `organization_status` | Who reaches it | Screen | Actions |
|---|---|---|---|
| `active` | members | The app | — |
| `suspended` | every member | "**{org} is suspended.** Your data is safe and incoming messages are still recorded, but nothing can be sent or changed until it's reactivated." Owner: "Update billing to reactivate." Others: "Contact {org}'s owner." | Organization switcher, Log out. Owner: link to billing (when billing ships, Phase 8) |
| `pending_deletion` | **owner only** (for everyone else the organization is no longer in `memberships`) | "**{org} will be permanently deleted on {deletion_scheduled_at, as a date}.** All conversations, contacts and settings will be erased." | **Restore organization** (`POST /restore`), organization switcher, Log out |

```tsx
export function OrgStatusGate({ children }: { children: ReactNode }) {
  const m = useCurrentMembership();
  if (!m) return <Navigate to="/" replace />;
  if (m.organization_status === "suspended") return <SuspendedScreen membership={m} />;
  if (m.organization_status === "pending_deletion") return <PendingDeletionScreen membership={m} />;
  return <>{children}</>;
}
```

The status in `memberships` can be stale (e.g. a platform admin suspended the organization a minute ago). When any call returns `403 organization_suspended` or `403 organization_pending_deletion`, call `reloadMe()`; the gate then shows the right screen.

In the organization switcher, show a small "Suspended" / "Deletion scheduled" tag next to non-active organizations so the user knows before clicking.

## 4. Organization overview and settings

**Overview (`/orgs/:orgId`, every member):** organization name, the user's role badge, plan name (`plan.name`), workspaces the user can open.

**Plan and seats (owners/administrators, because `limits` is `null` for agents):** "Members: {members.length + pendingInvitations.length} of {limits.agents}". Hide the "of N" part when `limits.agents` is missing (unlimited).

**Settings (`/orgs/:orgId/settings`, `organization.manage`):** name, locale, timezone → `PATCH /organizations/{org}`. Below it, the **danger zone**, rendered for the owner only (§5).

## 5. Owner-only actions

Show these only when `membership.role === "owner"`. They are not permissions, so don't use `can()` for them. Each one asks for the password again. On `403 password_incorrect` show "Password is incorrect." under the field and keep the dialog open: it's **not** a logout, so the 401 handling from [01 §5.2](01_AUTHENTICATION.md#52-expired-access-token-during-use) must not run.

### 5.1 Transfer ownership

Where: Settings → Danger zone → **Transfer ownership**.

| Step | UI |
|---|---|
| 1 | Dropdown of members with `role === "administrator"`. If there are none: "Make someone an administrator first," with a link to Members |
| 2 | Explain: "{name} becomes the owner. You become an administrator and lose access to billing and these owner actions." |
| 3 | Password field + **Transfer ownership** button |
| 4 | `POST /organizations/{org}/transfer-ownership { membership_id, password }` → `204` → `reloadMe()`, toast "{name} is now the owner." |

| Error | Show |
|---|---|
| `403 password_incorrect` | Under the password field |
| `422 new_owner_not_admin` | "The new owner must be an administrator." (refetch members: their role changed) |
| `404` | "That member no longer exists." Refetch members |

### 5.2 Delete organization

Where: Settings → Danger zone → **Delete organization**.

| Step | UI |
|---|---|
| 1 | Explain: "Everyone loses access immediately. You can restore it within 30 days; after that all data is erased permanently." |
| 2 | Ask the owner to type the organization name (client-side check only), then the password |
| 3 | `DELETE /organizations/{org} { password }` → `200` (status `pending_deletion`) → `reloadMe()` → the gate shows the pending-deletion screen |

### 5.3 Restore organization

On the pending-deletion screen: **Restore organization** → `POST /organizations/{org}/restore` → `200` → `reloadMe()` → navigate to `/orgs/<id>`. No password.

| Error | Show |
|---|---|
| `404` | "The restore period has ended." `reloadMe()` (the organization disappears) |
| `409 conflict` | Already restored (another tab): `reloadMe()` |

## 6. Members page (`/orgs/:orgId/members`)

### 6.1 Layout by role

| Element | Owner | Administrator | Agent |
|---|---|---|---|
| Member table: name, role badge, availability dot | ✅ | ✅ | ✅ |
| Email column | ✅ | ✅ | ❌ (`email` is `null`: hide the column) |
| Workspaces column ("All workspaces" for owner/admin rows, else names from `workspace_ids`) | ✅ | ✅ | ❌ |
| **Invite member** button | ✅ | ✅ | ❌ |
| Pending invitations section + **Revoke** | ✅ | ✅ | ❌ |
| **Edit** / **Remove** on a row | Admin and agent rows, not own row | Admin and agent rows, not owner, not own row | ❌ |
| **Leave organization** (own row menu or page footer) | ❌ (show "Transfer ownership to leave" link) | ✅ | ✅ |
| Seat usage "N of M" | ✅ | ✅ | ❌ |

Use `assignableRoles()` and `canManageMember()` from [02 §6](02_ROLES_AND_PERMISSIONS.md#6-member-management-rules). Agents only see the list and the text "Only owners and administrators can change roles or invite people."

### 6.2 Invite

Dialog fields: **Email**, **Role** (`assignableRoles(me)`: Administrator, Agent). After sending: "Invitation sent to {email}. It expires in 7 days." Add it to the pending list.

Workspaces are **not** part of the invitation. Once the person accepts, they appear in the member list with no workspaces (if they're an agent); show a hint on their row: "Not in any workspace yet: **Add to workspace**" (opens Edit).

| Error | Show |
|---|---|
| `402 plan_limit_reached` (`limit: "agents"`, `max`) | "Your plan allows at most {max} members, including pending invitations. Upgrade the plan or remove someone first." |
| `409 conflict` | The message from the API ("already a member" / "invitation already pending") |
| `403 permission_denied` | Shouldn't happen with the dropdown above: `reloadMe()` |
| `422 validation_error` | Field errors (invalid email) |

### 6.3 Edit member

Dialog fields:

| Field | Shown when | Sent as |
|---|---|---|
| Role (Administrator / Agent) | always | `role` |
| Workspaces (checkboxes from `GET /organizations/{org}/workspaces`) | selected role is **agent** | `workspace_ids` (full list: it replaces the existing set) |

Owners and administrators always work in every workspace; when the role is Administrator, replace the checkboxes with "Administrators can access all workspaces." Changing someone to administrator clears their custom role (the backend does this).

### 6.4 Remove member

Confirm dialog: "Remove {name} from {org}? They lose access immediately. Their open conversations go back to the unassigned queue, and they're removed from all inboxes and teams." → `DELETE /organizations/{org}/members/{id}` → `204` → refetch members.

### 6.5 Leave organization

Confirm dialog: "Leave {org}? You'll lose access immediately; an administrator can invite you again." → `POST /organizations/{org}/leave` → `204` → `reloadMe()` → navigate to `/` (next organization or the account page).

For the owner, don't show **Leave**. Show "To leave, transfer ownership first" linking to §5.1. (The API returns `422 owner_cannot_leave` if called anyway.)

## 7. Accepting an invitation (`/accept-invitation?token=…`)

The email link opens this page. The token is used once; don't log it or send it anywhere except the API.

| Auth state | What to do |
|---|---|
| Logged in | Show "Join the organization?" + **Accept** → `POST /invitations/accept { token }` → `200 MeResponse` → replace `user` + `memberships` in the auth state → navigate to `/orgs/<organization_id of the new membership>` |
| Logged out, has an account | Show **Log in to accept** → `/login?next=/accept-invitation?token=…` |
| Logged out, no account | Show **Create account** → `/signup?invitation=<token>` (invitation mode from [01 §6.2](01_AUTHENTICATION.md#62-sign-up-signup)); the signup response already contains the new membership |

To find the new organization after accepting, compare the `memberships` from before and after (or take the one whose `organization_id` wasn't there before).

| Error | Show |
|---|---|
| `404 not_found` | "This invitation is invalid, has expired, or was sent to a different email address. Ask for a new one." |
| `409 conflict` | "You're already a member of this organization." + link to it |
| `402 plan_limit_reached` | "This organization has no free seats. Ask its administrator to free one." |

## 8. Errors on these pages

On top of [02 §8](02_ROLES_AND_PERMISSIONS.md#8-api-errors-related-to-access):

| Status | `code` | Where | Show |
|---|---|---|---|
| 402 | `plan_limit_reached` | Invite, accept | §6.2 / §7 texts |
| 403 | `password_incorrect` | Transfer, delete | Under the password field; **not** a logout |
| 403 | `organization_suspended` | Any org/workspace call | `reloadMe()` → gate (§3) |
| 403 | `organization_pending_deletion` | Any org/workspace call | `reloadMe()` → gate (§3) |
| 403 | `permission_denied` (`permission: "owner"`) | Owner actions | "Only the owner can do this." `reloadMe()` (ownership may have moved) |
| 404 | `not_found` | Restore | §5.3 |
| 409 | `conflict` | Invite, accept, restore | API message |
| 422 | `owner_cannot_leave` | Leave | §6.5 |
| 422 | `new_owner_not_admin` | Transfer | §5.1 |
| 422 | `validation_error` | Edit own row, make someone owner | Shouldn't be reachable from the UI |

## 9. Acceptance checklist

Use the demo accounts from [01 §2.6](01_AUTHENTICATION.md#26-test-accounts-development) (password `Password123!`), plus fresh signups where noted.

| # | As | Do | Expected |
|---|---|---|---|
| 1 | new signup | Sign up with a new company | Lands in organization with workspace **"Support"**, Owner badge |
| 2 | owner | Members page | Email column, Invite button, Edit/Remove on admin + agent rows, none on own row, no Leave (link to transfer instead) |
| 3 | admin | Members page | Invite roles **Administrator, Agent**; Edit/Remove on agent rows; no Edit/Remove on owner or own row; **Leave** visible |
| 4 | agent | Members page | Names + availability only (no email column), no Invite, no Edit/Remove, **Leave** visible |
| 5 | owner | Invite an agent, accept in a private window via signup | New member listed with "Not in any workspace yet"; after Edit → tick "Customer Support", the agent sees that workspace after `reloadMe()` |
| 6 | owner | Edit agent → role Administrator | Workspace checkboxes replaced by "Administrators can access all workspaces" |
| 7 | owner | Remove a member | Confirm text mentions unassigning conversations; row disappears |
| 8 | agent | Leave organization | Redirected to `/`; organization gone from switcher |
| 9 | owner (fresh org) | Invite until the plan limit | `402` message with the limit number |
| 10 | owner | Transfer ownership, wrong password | "Password is incorrect." stays in dialog, **no logout** |
| 11 | owner | Transfer ownership to an administrator | Badge changes to Administrator; danger zone disappears; the other user sees Owner badge after `reloadMe()` |
| 12 | owner (fresh org) | Delete organization | Pending-deletion screen with the date and **Restore** |
| 13 | admin of that org | Reload | Organization gone from switcher; its URL redirects to `/` |
| 14 | owner | Restore | App comes back; admin sees the organization again after `reloadMe()` |
| 15 | any member | Organization suspended (backend: `UPDATE organizations SET status='suspended'`) | Suspended screen; switcher tag "Suspended"; other organizations still work |
| 16 | logged-in user | Open an invitation link for a different email | "invalid, has expired, or was sent to a different email" |
