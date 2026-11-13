"""custom roles only on agents

docs/12 §2: a custom role replaces an AGENT's default permissions; owners and
administrators always have their full set. The application already clears
custom_role_id when someone stops being an agent; this makes the database
enforce it too.

Revision ID: b4e8f1c6d932
Revises: a7c3e9d4f210
Create Date: 2026-11-13 14:00:00.000000

"""

from typing import Sequence, Union

from alembic import op

revision: str = "b4e8f1c6d932"
down_revision: Union[str, Sequence[str], None] = "a7c3e9d4f210"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Existing rows from before the rule: drop custom roles on non-agents.
    op.execute(
        "UPDATE organization_memberships SET custom_role_id = NULL "
        "WHERE role <> 'agent' AND custom_role_id IS NOT NULL"
    )
    op.create_check_constraint(
        "org_memberships_custom_role_agent_chk",
        "organization_memberships",
        "custom_role_id IS NULL OR role = 'agent'",
    )


def downgrade() -> None:
    op.drop_constraint(
        "org_memberships_custom_role_agent_chk",
        "organization_memberships",
        type_="check",
    )
