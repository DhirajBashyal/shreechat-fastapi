"""website live chat

docs/integrations/WEBSITE_CHAT.md:

- contact_identities.token_version: visitor tokens carry it; bumping it on
  identify (and on reset) invalidates every older token of the identity (§8).
- labels.widget_settable: only these labels can be set from the website
  widget SDK (``converse.setLabel``, §2).

Revision ID: c8e2a4f7b913
Revises: b4e8f1c6d932
Create Date: 2026-11-13 18:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c8e2a4f7b913"
down_revision: Union[str, Sequence[str], None] = "b4e8f1c6d932"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "contact_identities",
        sa.Column(
            "token_version", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
    )
    op.add_column(
        "labels",
        sa.Column(
            "widget_settable",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("labels", "widget_settable")
    op.drop_column("contact_identities", "token_version")
