# Tenant Isolation (Database Level)

How PostgreSQL itself keeps tenants apart, with the exact SQL and the tests that were run. Application-level scoping is in [../09_MULTI_TENANCY.md](../09_MULTI_TENANCY.md).

## 1. Three layers

| Layer | Mechanism | Stops |
|---|---|---|
| 1. Application | Repositories filter by `ctx.workspace_id`; visibility filters | Normal access to other workspaces |
| 2. Referential | Composite FKs `(workspace_id, parent_id)` and `(organization_id, workspace_id)` | Storing a row that points at another tenant's data |
| 3. Row-level security | `organization_id = current_org_id()` on every tenant table, `FORCE`d | Reading or writing another tenant's rows even when layer 1 has a bug |

## 2. Database roles

| Role | RLS | Used by | Connection string env |
|---|---|---|---|
| Schema owner (e.g. `converse_owner`) | Owner (RLS forced anyway) | Migrations only | `DATABASE_MIGRATION_URL` |
| `converse_app` | Enforced | API, tenant-scoped Celery tasks, realtime gateway | `DATABASE_URL` |
| `converse_system` | `BYPASSRLS` | Webhook routing, outbox relay, cross-tenant schedulers, login membership lookup, platform admin | `DATABASE_SYSTEM_URL` |

`converse_app` and `converse_system` are `NOLOGIN` group roles. Deployments create login users that are members of them, e.g. `CREATE ROLE converse_api LOGIN PASSWORD '…' IN ROLE converse_app;`.

## 3. SQL

The full block is part of [INITIAL_SCHEMA_SQL.md](INITIAL_SCHEMA_SQL.md#2-02_functions_rlssql--functions-roles-grants-row-level-security). The essential parts:

```sql
CREATE OR REPLACE FUNCTION current_org_id() RETURNS uuid
LANGUAGE sql STABLE AS $$
    SELECT NULLIF(current_setting('app.current_org_id', true), '')::uuid;
$$;

-- For every table with organization_id (except channel_routes, inbound_webhook_events):
ALTER TABLE messages ENABLE ROW LEVEL SECURITY;
ALTER TABLE messages FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON messages
    USING (organization_id = current_org_id())
    WITH CHECK (organization_id = current_org_id());

-- organizations uses its own id
CREATE POLICY tenant_isolation ON organizations
    USING (id = current_org_id())
    WITH CHECK (id = current_org_id());

-- Ingress-only tables are closed to the app role entirely
REVOKE ALL ON channel_routes, inbound_webhook_events, billing_webhook_events FROM converse_app;

-- Platform-managed reference data is read-only for tenants
REVOKE INSERT, UPDATE, DELETE ON subscription_plans, permissions FROM converse_app;

-- Audit logs are append-only for the app
REVOKE UPDATE, DELETE ON audit_logs FROM converse_app;
```

Binding the tenant from Python (parameterised, transaction-scoped):

```python
await session.execute(select(func.set_config("app.current_org_id", str(org_id), True)))
```

`is_local = true` makes it equivalent to `SET LOCAL`: it ends with the transaction and cannot leak to the next user of a pooled connection.

## 4. Tests that were run

Executed against the tested schema + seed in PostgreSQL 17.6 (output abbreviated):

```sql
SELECT count(*) AS msgs_as_owner FROM messages;                  -- 2   (owner; seed data present)

BEGIN; SET LOCAL ROLE converse_app;
SELECT count(*) FROM conversations;                              -- 0   (no tenant bound → nothing visible)

SET LOCAL app.current_org_id = '00000000-0000-0000-0000-00000000b001';
SELECT count(*) FROM conversations;                              -- 1
SELECT count(*) FROM messages;                                   -- 2

SET LOCAL app.current_org_id = '11111111-1111-1111-1111-111111111111';
SELECT count(*) FROM conversations;                              -- 0   (other tenant)

INSERT INTO labels (organization_id, workspace_id, title)
VALUES ('00000000-0000-0000-0000-00000000b001', '00000000-0000-0000-0000-00000000e001', 'x');
-- ERROR: new row violates row-level security policy for table "labels"

SELECT count(*) FROM channel_routes;
-- ERROR: permission denied for table channel_routes
ROLLBACK;

BEGIN;
SET LOCAL app.current_org_id = '00000000-0000-0000-0000-00000000b001';
SET LOCAL ROLE converse_app;
SELECT next_workspace_counter('00000000-0000-0000-0000-00000000e001', 'conversation');   -- 2
ROLLBACK;
```

Composite foreign keys, tested as the **table owner** (RLS doesn't stop the owner here, so only the FKs protect):

```sql
-- a second tenant
INSERT INTO organizations (id, name, slug) VALUES ('22222222-2222-2222-2222-222222222222', 'Other', 'other');
INSERT INTO workspaces (id, organization_id, name, slug)
VALUES ('33333333-3333-3333-3333-333333333333', '22222222-2222-2222-2222-222222222222', 'W', 'w');

-- message in tenant B's workspace pointing at tenant A's conversation
INSERT INTO messages (organization_id, workspace_id, inbox_id, conversation_id, direction, content, sender_kind)
VALUES ('22222222-2222-2222-2222-222222222222', '33333333-3333-3333-3333-333333333333',
        '00000000-0000-0000-0000-000000010001', '00000000-0000-0000-0000-000000030001', 'incoming', 'x', 'system');
-- ERROR: violates foreign key constraint "messages_workspace_id_conversation_id_fkey"

-- label claiming tenant B's organization but tenant A's workspace
INSERT INTO labels (organization_id, workspace_id, title)
VALUES ('22222222-2222-2222-2222-222222222222', '00000000-0000-0000-0000-00000000e001', 'y');
-- ERROR: violates foreign key constraint "labels_organization_id_workspace_id_fkey"
```

## 5. Automated tests to add in the codebase

| Test | Assertion |
|---|---|
| `test_every_tenant_table_has_rls` | Query `pg_class` + `information_schema.columns`: every table with `organization_id` (except the two ingress tables) has `relrowsecurity` and `relforcerowsecurity` |
| `test_unbound_session_sees_nothing` | For each tenant table: as `converse_app` without `app.current_org_id`, `SELECT count(*)` = 0 |
| `test_cross_tenant_read` | Create tenants A and B with data; bound to A, every list/get endpoint returns only A's rows; ids of B return 404 |
| `test_cross_tenant_write_rejected` | Bound to A, inserting a row with B's `organization_id` raises; inserting A's org with B's `workspace_id` fails the composite FK |
| `test_api_ids_from_other_tenant` | Parametrised over every route with a path id: using an id from tenant B under tenant A's workspace gives 404 |
| `test_set_local_does_not_leak` | Two sequential transactions on the same pooled connection: the second, without binding, sees 0 rows |
| `test_system_role_usage_is_confined` | Static check: `system_session()` only imported from an allow-listed set of modules |

## 6. Caveats

- **RLS does not apply to the table owner unless `FORCE` is set.** It is set for every policy table. Migrations run as the owner, so data migrations must bind a tenant or explicitly run as `converse_system`.
- **`BYPASSRLS` code is powerful.** Keep `converse_system` usage to read-mostly routing and scanning; tenant writes happen in a subsequent tenant-scoped task.
- **Performance.** The policy is a simple equality on an indexed column (`*_org_idx` indexes exist on the large tables). Query plans should show the `organization_id` filter combined with the workspace index. Check `EXPLAIN` for list endpoints.
- **Views and functions.** Any future `SECURITY DEFINER` function bypasses RLS of the definer's tables. Avoid them, or check `current_org_id()` inside.
- **`workspace_counters`** has no `organization_id` (keyed by workspace) and therefore no RLS. It is only touched through `next_workspace_counter()` with an already-authorized workspace id. Worst case, a bug could advance another workspace's counter; it cannot read or change tenant data.
