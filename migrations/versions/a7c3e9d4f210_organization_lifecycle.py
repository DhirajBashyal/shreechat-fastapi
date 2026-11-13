"""organization lifecycle: deletion grace period and single owner

docs/12_ORGANIZATION_LEVELS_AND_PERMISSIONS.md §4 and §5.1:
- organizations.deletion_scheduled_at: set when the owner deletes the
  organization (status pending_deletion); purged once it has passed.
- At most one owner membership per organization, enforced by the database.

Revision ID: a7c3e9d4f210
Revises: 3b9d1c2e7a41
Create Date: 2026-11-13 10:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a7c3e9d4f210"
down_revision: Union[str, Sequence[str], None] = "3b9d1c2e7a41"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "organizations",
        sa.Column("deletion_scheduled_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "org_memberships_one_owner_uniq",
        "organization_memberships",
        ["organization_id"],
        unique=True,
        postgresql_where=sa.text("role = 'owner'"),
    )


def downgrade() -> None:
    op.drop_index(
        "org_memberships_one_owner_uniq", table_name="organization_memberships"
    )
    op.drop_column("organizations", "deletion_scheduled_at")
