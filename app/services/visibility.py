"""Conversation visibility (docs/implementation/ORGANIZATION_LEVELS_IMPLEMENTATION.md §8).

One filter for every conversation query. Agents see conversations of their
inboxes or teams, ones assigned to them, and ones they participate in;
``conversation.view_all`` (owner, administrator, or a custom role) sees all.
"""

import uuid

from sqlalchemy import ColumnElement, Select, false, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError
from app.core.tenancy import TenantContext
from app.models import Conversation, ConversationParticipant, InboxMember, TeamMember
from app.models.enums import MembershipRole


def visible_conversations(ctx: TenantContext, stmt: Select) -> Select:  # type: ignore[type-arg]
    stmt = stmt.where(Conversation.workspace_id == ctx.require_workspace())
    if ctx.has("conversation.view_all"):
        return stmt
    user_id = ctx.principal.user_id
    my_inboxes = select(InboxMember.inbox_id).where(
        InboxMember.user_id == user_id, InboxMember.workspace_id == ctx.workspace_id
    )
    my_teams = select(TeamMember.team_id).where(
        TeamMember.user_id == user_id, TeamMember.workspace_id == ctx.workspace_id
    )
    mine = or_(
        Conversation.inbox_id.in_(my_inboxes), Conversation.team_id.in_(my_teams)
    )
    clauses: list[ColumnElement[bool]] = []
    if ctx.has("conversation.view_unassigned"):
        clauses.append(Conversation.assignee_id.is_(None) & mine)
    if ctx.has("conversation.view_participating"):
        clauses.append(Conversation.assignee_id == user_id)
        clauses.append(
            Conversation.id.in_(
                select(ConversationParticipant.conversation_id).where(
                    ConversationParticipant.user_id == user_id
                )
            )
        )
    if ctx.membership_role == MembershipRole.AGENT and ctx.custom_role_id is None:
        clauses.append(mine)  # default agents: everything in their inboxes/teams
    return stmt.where(or_(*clauses)) if clauses else stmt.where(false())


async def get_visible_conversation(
    session: AsyncSession,
    ctx: TenantContext,
    conversation_id: uuid.UUID,
    *,
    lock: bool = False,
) -> Conversation:
    stmt = visible_conversations(
        ctx, select(Conversation).where(Conversation.id == conversation_id)
    )
    conv = await session.scalar(stmt.with_for_update() if lock else stmt)
    if conv is None:
        raise NotFoundError()
    return conv
