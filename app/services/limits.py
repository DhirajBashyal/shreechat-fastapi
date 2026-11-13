"""Plan limits (docs/09_MULTI_TENANCY.md §6, docs/12 §4).

Effective limits = the current subscription plan's ``limits`` overridden by
``organizations.limits_override``. A missing key (or no subscription) means
unlimited.
"""

import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import FeatureNotInPlanError, PlanLimitReachedError
from app.core.security import utcnow
from app.models import (
    Invitation,
    Organization,
    OrganizationMembership,
    Subscription,
    SubscriptionPlan,
)
from app.models.enums import SubscriptionState


@dataclass(frozen=True, slots=True)
class PlanInfo:
    code: str | None
    name: str | None
    limits: dict[str, Any] = field(default_factory=dict)
    # Plan features plus the organization's extra feature_flags.
    features: frozenset[str] = frozenset()


async def plan_info(session: AsyncSession, organization: Organization) -> PlanInfo:
    plan = await session.scalar(
        select(SubscriptionPlan)
        .join(Subscription, Subscription.plan_id == SubscriptionPlan.id)
        .where(
            Subscription.organization_id == organization.id,
            Subscription.state != SubscriptionState.CANCELED,
        )
        .order_by(Subscription.created_at.desc())
        .limit(1)
    )
    limits = {**(plan.limits if plan else {}), **(organization.limits_override or {})}
    return PlanInfo(
        code=plan.code if plan else None,
        name=plan.name if plan else None,
        limits=limits,
        features=frozenset(plan.features if plan else [])
        | frozenset(organization.feature_flags or []),
    )


async def require_feature(
    session: AsyncSession, organization_id: uuid.UUID, feature: str
) -> None:
    """402 feature_not_in_plan unless the plan (or a feature flag) includes it."""
    org = await session.get(Organization, organization_id)
    if org is None or feature not in (await plan_info(session, org)).features:
        raise FeatureNotInPlanError(feature)


def _max(info: PlanInfo, key: str) -> int | None:
    value = info.limits.get(key)
    return int(value) if value is not None else None


async def check_agent_seats(
    session: AsyncSession, organization_id: uuid.UUID, *, include_pending: bool
) -> None:
    """402 plan_limit_reached when the organization has no free seat.

    Every member counts as a seat (owner and administrators too). Inviting
    also counts pending invitations, so you can't over-invite; accepting
    counts members only, because the invitation itself already held a seat.
    """
    org = await session.get(Organization, organization_id)
    if org is None:
        return
    maximum = _max(await plan_info(session, org), "agents")
    if maximum is None:
        return
    used = await session.scalar(
        select(func.count())
        .select_from(OrganizationMembership)
        .where(OrganizationMembership.organization_id == organization_id)
    )
    if include_pending:
        used = (used or 0) + (
            await session.scalar(
                select(func.count())
                .select_from(Invitation)
                .where(
                    Invitation.organization_id == organization_id,
                    Invitation.accepted_at.is_(None),
                    Invitation.expires_at > utcnow(),
                )
            )
            or 0
        )
    if (used or 0) >= maximum:
        raise PlanLimitReachedError("agents", maximum)
