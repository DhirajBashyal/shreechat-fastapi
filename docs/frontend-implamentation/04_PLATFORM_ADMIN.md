# 04 — Platform Console (Frontend Implementation)

The platform console is the screen for **our staff** (platform admins): it shows how many organizations exist, lists and searches all of them, opens one to see its members and plan, suspends or reactivates it, adjusts limits, and manages who else is a platform admin.

Prerequisites: [01_AUTHENTICATION.md](01_AUTHENTICATION.md) (login, `is_platform_admin`), [03_ORGANIZATIONS_AND_MEMBERS.md](03_ORGANIZATIONS_AND_MEMBERS.md) (organization statuses). Backend rules: [../12_ORGANIZATION_LEVELS_AND_PERMISSIONS.md §7](../12_ORGANIZATION_LEVELS_AND_PERMISSIONS.md#7-platform-admin-our-staff).

## 1. Who gets in

| | Platform admin | Everyone else |
|---|---|---|
| Login | Same `/login` page and `POST /auth/login` | — |
| `is_platform_admin` in login / `/auth/me` | `true` | `false` |
| "Platform console" link in the user menu | Shown | Hidden |
| `/platform/*` routes in the app | Open | Redirect to `/` (the API returns `404` anyway) |

There's no separate platform login or password. A staff account usually has **no organizations**: after login, if `memberships` is empty and `is_platform_admin` is `true`, go to `/platform` instead of the account page.

Being a platform admin doesn't make someone a member of the organizations they look at. They see tenants only inside the console; links to `/orgs/:id` for an organization they don't belong to still redirect to `/`.

**MFA.** Until MFA is built, the backend answers every platform call with `403 mfa_required` outside development. Show: "The platform console needs multi-factor authentication, which isn't available on this server yet." (No retry button.)

```tsx
export function RequirePlatformAdmin({ children }: { children: ReactNode }) {
  const { isPlatformAdmin } = useAuth();
  return isPlatformAdmin ? <>{children}</> : <Navigate to="/" replace />;
}
```

## 2. Routes

```text
/platform                        Dashboard: counts + organization list (§4)
/platform/organizations/:orgId   Organization detail + actions (§5)
/platform/admins                 Platform admins (§6)
```

Use a visually distinct layout (e.g. a coloured top bar labelled "Platform console") so staff never confuse it with a customer's organization.

## 3. Types and endpoints

```ts
export interface OrganizationCounts {
  total: number; active: number; suspended: number; pending_deletion: number;
}

export interface PlatformOrganization {
  id: string; name: string; slug: string; status: OrgStatus;
  created_at: string; deletion_scheduled_at: string | null;
  owner_email: string | null;
  member_count: number; workspace_count: number;
  plan: { code: string | null; name: string | null } | null;
}

export interface PlatformOrganizationList {
  counts: OrganizationCounts;   // global, ignores q/status
  matching: number;             // rows matching q/status, for pagination
  limit: number; offset: number;
  items: PlatformOrganization[];
}

export interface PlatformOrganizationDetail extends PlatformOrganization {
  limits: Record<string, number>;          // effective (plan + override)
  limits_override: Record<string, number>;
  feature_flags: string[];
  workspaces: string[];
  members: { user_id: string; name: string; email: string; role: MembershipRole; joined_at: string }[];
}

export interface PlatformAdmin {
  user_id: string; name: string; email: string;
  granted_by: string | null; created_at: string;
}
```

| Method | URL | Body / query | Success |
|---|---|---|---|
| GET | `/platform/stats` | — | `200 OrganizationCounts` |
| GET | `/platform/organizations` | `?q=&status=&limit=50&offset=0` (`limit` 1–200) | `200 PlatformOrganizationList` |
| GET | `/platform/organizations/{org}` | — | `200 PlatformOrganizationDetail` |
| PATCH | `/platform/organizations/{org}` | `{ status?: "active" \| "suspended", limits_override?: {…}, feature_flags?: string[] }` | `200 PlatformOrganizationDetail` |
| GET | `/platform/admins` | — | `200 PlatformAdmin[]` |
| POST | `/platform/admins` | `{ email }` | `201 PlatformAdmin[]` (the new list) |
| DELETE | `/platform/admins/{user_id}` | — | `204` |

## 4. Dashboard (`/platform`)

**Count tiles** from `counts` (or `GET /platform/stats`): **Total organizations**, **Active**, **Suspended**, **Pending deletion**. Clicking a tile sets the status filter.

**Organization table**:

| Column | From |
|---|---|
| Name (link to detail) + slug | `name`, `slug` |
| Status badge | `status` (Pending deletion: also "purges {date}" from `deletion_scheduled_at`) |
| Owner | `owner_email` |
| Members / Workspaces | `member_count`, `workspace_count` |
| Plan | `plan?.name ?? "No plan"` |
| Created | `created_at` |

- **Search box** → `q` (name, slug or owner email). Debounce 300 ms; reset `offset` to 0.
- **Status filter** → `status`.
- **Pagination**: "Showing {offset+1}–{offset+items.length} of {matching}". Keep `q`, `status` and `offset` in the URL query so the page can be shared and reloaded.

## 5. Organization detail (`/platform/organizations/:orgId`)

Header: name, status badge, plan, created date, owner email. Sections:

| Section | Content |
|---|---|
| Members | Table of `members` (name, email, role badge, joined) |
| Workspaces | `workspaces` names |
| Limits | Each key of `limits` with its value; mark keys that come from `limits_override` as "overridden" |
| Feature flags | `feature_flags` as chips |
| Actions | §5.1 |

### 5.1 Actions

| Action | Shown when | Call | Confirm text |
|---|---|---|---|
| **Suspend** | `status === "active"` | `PATCH {status: "suspended"}` | "Suspend {name}? Its members see a suspended screen and the API stops working for them. Incoming messages are still recorded." |
| **Reactivate** | `status === "suspended"` | `PATCH {status: "active"}` | — |
| **Edit limits** | always (except pending deletion) | `PATCH {limits_override}`; send the **whole** override object (it replaces the old one); `{}` = back to the plan's limits | Number inputs per key (`agents`, `inboxes`, `workspaces`, `ai_responses_per_month`, `storage_gb`), each with "use plan value" |
| **Feature flags** | always (except pending deletion) | `PATCH {feature_flags}` (whole list) | — |

For `pending_deletion` show no actions and the note: "Only the owner can restore this organization, until {deletion_scheduled_at}."

After every successful PATCH, replace the page state with the returned detail.

## 6. Platform admins (`/platform/admins`)

Table: name, email, since (`created_at`), **Revoke** button (hidden on your own row).

**Add platform admin**: email field → `POST /platform/admins {email}`. The person must already have an account. For a brand-new staff member, a backend developer runs `make platform-admin ARGS="grant <email> --create"` (the command asks for the password).

Confirm on revoke: "Remove {name}'s platform admin access? They keep their normal account and organizations."

## 7. Errors

| Status | `code` | Where | Show |
|---|---|---|---|
| 403 | `mfa_required` | any | §1 message |
| 404 | `not_found` | any `/platform/*` | Not (or no longer) a platform admin: reload `/auth/me` → redirect to `/` |
| 404 | `not_found` | `POST /platform/admins` | "No account with this email. They must sign up first." |
| 409 | `conflict` | `POST /platform/admins` | "Already a platform admin." |
| 409 | `conflict` | PATCH on pending deletion | "Only the owner can restore this organization." |
| 422 | `validation_error` | PATCH, revoke self | Field errors (negative limits); "You can't revoke your own access." |

## 8. Acceptance checklist

Run `make migrate && make seed`. Platform admins (password `Password123!`): `platform@demo.example.com` (staff account, no organization) and `owner@demo.example.com` (also owner of Demo Org).

| # | As | Do | Expected |
|---|---|---|---|
| 1 | agent@demo.example.com | Open the user menu | No "Platform console" link; typing `/platform` redirects to `/` |
| 2 | platform@demo.example.com | Log in, open Platform console | Count tiles; total matches the table's "of N" with no filters |
| 3 | platform admin | Search an owner's email | Only that owner's organization listed |
| 4 | platform admin | Click "Suspended" tile | Only suspended organizations |
| 5 | platform admin | Open "Demo Org" detail | Members owner/admin/agent with emails, workspace "Customer Support", plan, limits |
| 6 | platform admin | Suspend Demo Org; log in as agent in another window | Agent sees the suspended screen; reactivate → agent's app works after reload |
| 7 | platform admin | Set agents limit to 3 | Detail shows 3 (overridden); owner's invite fails with the plan-limit message once the org has 3 seats |
| 8 | platform admin | Add a colleague by email | Colleague appears; after they reload, they see the console link |
| 9 | platform admin | Try to revoke yourself | No Revoke button on own row |
| 10 | platform@demo.example.com (no organizations) | Log in | Lands on `/platform`; organization switcher empty |
