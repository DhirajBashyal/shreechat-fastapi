# Phase 2 — Authentication, Tenancy, Roles and Teams (Steps 3–5)

## 1. Objective
Users can sign up, log in securely, belong to organizations and workspaces with roles, and every tenant-scoped request is authorized and isolated.

## 2. Features
- Signup (user + organization + default workspace + owner membership + trial subscription in one transaction).
- Login, refresh rotation with reuse detection, logout / logout-all, password reset, email verification.
- `get_principal`, `get_tenant_context`, `require_permission`, `require_feature` dependencies.
- Organizations, workspaces, memberships, invitations, availability.
- Base roles and the permission catalog; custom roles schema in place (UI/API in R2).
- Teams and team members (R1 scope, but cheap to add now).
- API tokens (hashed, scoped) — can slip to R1 if needed.
- Audit log writes for security events.
- Rate limits on auth endpoints.
- MFA (TOTP) — R1.

## 3. Files to create
```text
src/converse/modules/auth/{models.py, repository.py, service.py, passwords.py, jwt.py, schemas.py, errors.py}
src/converse/modules/tenancy/{models.py, repository.py, service.py, schemas.py, invitations.py}
src/converse/modules/access/{models.py, permissions.py, service.py, visibility.py}
src/converse/modules/teams/{models.py, repository.py, service.py, schemas.py}
src/converse/modules/audit/{models.py, service.py}
src/converse/modules/billing/{models.py, limits.py}            # plan limits only, used by LimitsService
src/converse/api/deps/{auth.py, tenancy.py, permissions.py, ratelimit.py}
src/converse/api/routes/{auth.py, organizations.py, workspaces.py, members.py, teams.py}
tests/api/test_auth.py, tests/api/test_organizations.py, tests/api/test_members.py, tests/api/test_teams.py
tests/security/test_cross_tenant_access.py, tests/security/test_refresh_reuse.py
```

## 4. Database tables
`users`, `user_sessions`, `platform_admins`, `permissions`, `organizations`, `roles`, `role_permissions`, `organization_memberships`, `invitations`, `api_access_tokens`, `workspaces`, `workspace_memberships`, `teams`, `team_members`, `subscription_plans`, `subscriptions`, `audit_logs`.

## 5. APIs
[../backend/API_DESIGN.md](../backend/API_DESIGN.md) §2 (auth), §3 (organizations, members, invitations, API tokens, audit logs read), §4 (workspaces), §6 (teams).

## 6. Dependencies
Phase 1. Libraries: `argon2-cffi`, `pyjwt`, `pyotp` (MFA), `email-validator`.

## 7. Implementation steps
1. ORM models for the tables above (mirror the SQL exactly; `alembic check` must stay clean).
2. Password hashing (argon2id params from [../10_AUTHENTICATION_AND_RBAC.md](../10_AUTHENTICATION_AND_RBAC.md#21-passwords)) with rehash-on-login.
3. JWT issue/verify with `kid` rotation; access tokens carry no tenant claims.
4. `AuthService`: signup, login (generic errors, throttled), refresh rotation (family revocation on reuse), logout, password reset, email verification.
5. `get_principal`: Bearer → access JWT or API token (`cvs_` prefix) → `Principal`.
6. Membership resolution with a 60 s Redis cache; invalidate on membership/role changes (publish `permissions_changed` to `rt:user:{id}` later in Phase 5).
7. `get_tenant_context(workspace_id)` / `get_org_context(organization_id)`: system lookup of owning org, membership check (404 if none), suspended check, `bind_tenant()`.
8. `require_permission(code)` and `require_feature(flag)`.
9. Tenancy services: organizations, workspaces (respect plan `workspaces` limit), invitations (hashed tokens, 7-day expiry, email via a mail adapter), member role changes (exactly one owner), member removal (unassign their open conversations — wired in Phase 3).
10. Teams CRUD + members (members must be workspace members).
11. API tokens CRUD (plaintext once; prefix + hash).
12. Audit writes in services for the events listed in [../10_AUTHENTICATION_AND_RBAC.md](../10_AUTHENTICATION_AND_RBAC.md#8-security-checklist-for-auth).
13. Rate-limit dependencies on auth routes.
14. A test-only route walker asserting every tenant route has a permission dependency.

## 8. Testing requirements
- Signup creates all five records atomically; duplicate email → 409.
- Login: wrong password and unknown email give identical responses and timings within tolerance; throttling after 5 attempts.
- Refresh: rotation works; reusing an old refresh token revokes the family.
- Access token from a removed member stops working within the cache TTL.
- Agent cannot call `inbox.manage`/`member.manage` endpoints (403); administrator can; owner-only actions.
- Cross-tenant: user of org A gets 404 for every org/workspace route of org B (parametrised over routes).
- Invitation: expired/used token rejected; accepting adds the membership and workspace memberships.
- API token: scopes limit permissions; revoked token 401.

## 9. Expected output
A user can sign up, log in, create a second workspace, invite an agent, and the agent can log in and see only the workspaces they were added to.

## 10. Completion criteria
- [ ] All tests above pass; route-walker test passes.
- [ ] No endpoint returns 403 for resources of other tenants (always 404).
- [ ] Audit log rows exist for login, invite, role change, token create.
