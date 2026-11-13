"""Data access for graph nodes.

``DbRepo`` serves a live run on a real conversation. ``PlaygroundRepo`` runs
the same graph on a test question: same agent, same knowledge, but the
conversation and customer are simulated and nothing is written (no messages,
no handoff, no memory, no usage). Nodes only see the interface.
"""

import time
import uuid
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.graph.state import (
    AgentConfigView,
    AgentState,
    ChatTurn,
    ConversationView,
    CustomerView,
)
from app.ai.rag import retriever
from app.ai.rag.chunking import estimate_tokens
from app.ai.working_hours import is_open
from app.core.config import settings
from app.core.security import utcnow
from app.models import (
    AiAgent,
    AiAgentKnowledgeBase,
    AiConversationSession,
    AiProviderCredential,
    Contact,
    ContactIdentity,
    Conversation,
    ConversationLabel,
    CustomAttributeDefinition,
    Inbox,
    Label,
    Message,
    Organization,
    UsageRecord,
)
from app.models.enums import ConversationStatus, MessageDirection, SenderKind
from app.services import conversation_ai
from app.services.limits import plan_info

HISTORY_TOKEN_BUDGET = 3000
SUMMARIZE_AFTER_TOKENS = 2500
MAX_INPUT_CHARS = 4000
VERIFIED_CHANNEL_PROVIDERS = {"whatsapp_cloud", "facebook_messenger", "instagram"}


@dataclass
class DeliveryResult:
    outcome: str
    reply_message_id: uuid.UUID | None = None
    reason: str | None = None


@dataclass
class DbRepo:
    session: AsyncSession
    organization_id: uuid.UUID
    workspace_id: uuid.UUID
    session_id: uuid.UUID | None = None
    playground: bool = False
    credential: AiProviderCredential | None = field(default=None, repr=False)

    # --- input_validation --------------------------------------------------------

    async def load_input(
        self, conversation_id: uuid.UUID | None, trigger_message_id: uuid.UUID | None
    ) -> tuple[str, int] | None:
        if conversation_id is None or trigger_message_id is None:
            return None
        trigger = await self.session.get(Message, trigger_message_id)
        if (
            trigger is None
            or trigger.deleted_at is not None
            or trigger.conversation_id != conversation_id
        ):
            return None
        last_out = await self.session.scalar(
            select(func.max(Message.sequence_number)).where(
                Message.conversation_id == conversation_id,
                Message.direction == MessageDirection.OUTGOING,
                Message.is_private.is_(False),
                Message.deleted_at.is_(None),
            )
        )
        rows = (
            await self.session.execute(
                select(Message.content, Message.sequence_number, Message.created_at)
                .where(
                    Message.conversation_id == conversation_id,
                    Message.direction == MessageDirection.INCOMING,
                    Message.deleted_at.is_(None),
                    Message.sequence_number > (last_out or 0),
                )
                .order_by(Message.sequence_number)
            )
        ).all()
        texts: list[str] = []
        for content, _, _ in rows:
            text = (content or "").strip()
            if text and (not texts or texts[-1] != text):  # drop exact repeats
                texts.append(text)
        if not texts:
            return None
        joined = "\n".join(texts)
        if len(joined) > MAX_INPUT_CHARS:
            joined = joined[:MAX_INPUT_CHARS] + "\n[message truncated]"
        return joined, max(seq for _, seq, _ in rows)

    # --- tenant_context ----------------------------------------------------------

    async def load_agent(
        self, agent_id: uuid.UUID
    ) -> tuple[AgentConfigView, str | None]:
        """(config, handoff_reason if the AI must not answer)."""
        agent = await self.session.get(AiAgent, agent_id)
        if agent is None or agent.deleted_at is not None:
            raise LookupError("AI agent not found")
        org = await self.session.get(Organization, agent.organization_id)
        assert org is not None
        kb_ids = tuple(
            await self.session.scalars(
                select(AiAgentKnowledgeBase.knowledge_base_id).where(
                    AiAgentKnowledgeBase.ai_agent_id == agent.id
                )
            )
        )
        self.credential = (
            await self.session.get(AiProviderCredential, agent.provider_credential_id)
            if agent.provider_credential_id
            else None
        )
        view = AgentConfigView(
            id=agent.id,
            name=agent.name,
            is_active=agent.is_active,
            provider=self.credential.provider
            if self.credential
            else settings.AI_PROVIDER,
            chat_model=agent.chat_model,
            classifier_model=agent.classifier_model
            or settings.AI_CLASSIFIER_MODEL
            or agent.chat_model,
            temperature=float(agent.temperature),
            system_prompt=agent.system_prompt or "",
            persona=agent.persona or {},
            guardrails=agent.guardrails or {},
            handoff_policy=agent.handoff_policy or {},
            enabled_tools=frozenset(agent.enabled_tools or []),
            allowed_actions=frozenset(agent.allowed_actions or []),
            tool_settings=agent.tool_settings or {},
            knowledge_base_ids=kb_ids,
            max_turns_before_handoff=agent.max_turns_before_handoff,
            organization_name=org.name,
        )
        if not agent.is_active and not self.playground:
            return view, "agent_disabled"
        info = await plan_info(self.session, org)
        if "ai_agent" not in info.features:
            return view, "agent_disabled"
        quota = info.limits.get("ai_responses_per_month")
        if quota is not None and not self.playground and self.credential is None:
            used = await self.session.scalar(
                select(func.coalesce(func.sum(UsageRecord.quantity), 0)).where(
                    UsageRecord.organization_id == org.id,
                    UsageRecord.metric == "ai_responses",
                    UsageRecord.period_start == date.today().replace(day=1),
                )
            )
            if (used or 0) >= int(quota):
                return view, "quota_exhausted"
        return view, None

    # --- conversation_context ----------------------------------------------------

    async def load_conversation(
        self, conversation_id: uuid.UUID | None
    ) -> tuple[ConversationView, list[ChatTurn], str, dict[str, Any]]:
        assert conversation_id is not None
        conv = await self.session.get(Conversation, conversation_id)
        assert conv is not None
        inbox = await self.session.get(Inbox, conv.inbox_id)
        labels = tuple(
            await self.session.scalars(
                select(Label.title)
                .join(ConversationLabel, ConversationLabel.label_id == Label.id)
                .where(ConversationLabel.conversation_id == conv.id)
            )
        )
        ai_session = (
            await self.session.get(AiConversationSession, self.session_id)
            if self.session_id
            else None
        )
        since = ai_session.summarized_through_seq if ai_session else 0
        msgs = (
            (
                await self.session.execute(
                    select(Message)
                    .where(
                        Message.conversation_id == conv.id,
                        Message.deleted_at.is_(None),
                        Message.is_private.is_(
                            False
                        ),  # private notes never reach the LLM
                        Message.direction != MessageDirection.ACTIVITY,
                        Message.sequence_number > since,
                    )
                    .order_by(Message.sequence_number.desc())
                    .limit(200)
                )
            )
            .scalars()
            .all()
        )
        history: list[ChatTurn] = []
        used = 0
        for m in msgs:  # newest first, until the budget is spent
            text = (m.content or "").strip() or "[attachment]"
            if m.sender_kind == SenderKind.USER:
                text = f"[human agent] {text}"
            cost = estimate_tokens(text)
            if used + cost > HISTORY_TOKEN_BUDGET:
                break
            history.append(
                ChatTurn(
                    "user" if m.sender_kind == SenderKind.CONTACT else "assistant", text
                )
            )
            used += cost
        history.reverse()
        view = ConversationView(
            id=conv.id,
            channel=str(inbox.provider) if inbox else "web_widget",
            labels=labels,
            ai_turn_count=ai_session.turn_count if ai_session else 0,
            contact_id=conv.contact_id,
        )
        return (
            view,
            history,
            ai_session.summary if ai_session else "",
            dict(ai_session.facts) if ai_session else {},
        )

    async def outside_working_hours(self, conversation_id: uuid.UUID | None) -> bool:
        """True when the conversation's inbox has working hours on and it's closed now."""
        if conversation_id is None:
            return False
        conv = await self.session.get(Conversation, conversation_id)
        inbox = await self.session.get(Inbox, conv.inbox_id) if conv else None
        if inbox is None or not inbox.working_hours_enabled:
            return False
        return not is_open(inbox.working_hours or [], inbox.timezone, utcnow())

    # --- customer_identification -------------------------------------------------

    async def load_customer(self, conversation_id: uuid.UUID | None) -> CustomerView:
        assert conversation_id is not None
        conv = await self.session.get(Conversation, conversation_id)
        assert conv is not None
        contact = await self.session.get(Contact, conv.contact_id)
        identity = await self.session.get(ContactIdentity, conv.contact_identity_id)
        inbox = await self.session.get(Inbox, conv.inbox_id)
        ai_session = (
            await self.session.get(AiConversationSession, self.session_id)
            if self.session_id
            else None
        )
        level, method = "none", None
        if identity is not None and identity.identity_verified:
            level, method = "verified", "identity_verified"
        elif (
            ai_session
            and ai_session.customer_verified_until
            and ai_session.customer_verified_until > utcnow()
        ):
            level, method = "verified", "step_up"
        elif inbox is not None and str(inbox.provider) in VERIFIED_CHANNEL_PROVIDERS:
            level, method = "channel", str(inbox.provider)
        elif contact is not None and contact.email:
            level, method = "channel", "email_given"
        attrs = await self._ai_visible_attributes(contact)
        name = (contact.name or "").strip() if contact else ""
        return CustomerView(
            first_name=name.split()[0] if name else None,
            verification_level=level,  # type: ignore[arg-type]
            verification_method=method,
            has_email=bool(contact and contact.email),
            has_phone=bool(contact and contact.phone_number),
            contact_type=contact.contact_type if contact else None,
            attributes=attrs,
            own_values=tuple(
                v
                for v in ((contact.email, contact.phone_number) if contact else ())
                if v
            ),
        )

    async def _ai_visible_attributes(self, contact: Contact | None) -> dict[str, Any]:
        if contact is None:
            return {}
        keys = set(
            await self.session.scalars(
                select(CustomAttributeDefinition.attribute_key).where(
                    CustomAttributeDefinition.workspace_id == contact.workspace_id,
                    CustomAttributeDefinition.scope == "contact",
                    CustomAttributeDefinition.ai_visible.is_(True),
                )
            )
        )
        return {k: v for k, v in (contact.custom_attributes or {}).items() if k in keys}

    # --- tools -----------------------------------------------------------------

    async def search(
        self, agent: AgentConfigView, query: str, *, policy_only: bool
    ) -> retriever.SearchResult:
        return await retriever.hybrid_search(
            self.session,
            workspace_id=self.workspace_id,
            knowledge_base_ids=list(agent.knowledge_base_ids),
            query=query,
            top_n=4 if policy_only else 8,
            policy_only=policy_only,
        )

    async def customer_info(
        self, conversation: ConversationView, customer: CustomerView
    ) -> dict[str, Any]:
        """AI-visible attributes, labels, and summaries of this contact's last 5
        resolved conversations. Only this contact (data_scope own_contact)."""
        recent: list[str] = []
        if conversation.contact_id is not None:
            recent = [
                s
                for s in await self.session.scalars(
                    select(AiConversationSession.summary)
                    .join(
                        Conversation,
                        Conversation.id == AiConversationSession.conversation_id,
                    )
                    .where(
                        Conversation.contact_id == conversation.contact_id,
                        Conversation.status == ConversationStatus.RESOLVED,
                        Conversation.id != conversation.id,
                        AiConversationSession.summary != "",
                    )
                    .order_by(Conversation.updated_at.desc())
                    .limit(5)
                )
            ]
        return {
            "attributes": customer.attributes,
            "labels": list(conversation.labels),
            "recent_topics": recent,
        }

    # --- response_delivery -------------------------------------------------------

    async def deliver(self, state: AgentState) -> DeliveryResult:
        conv_id = state.get("conversation_id")
        assert conv_id is not None
        conv = await conversation_ai.lock_conversation(self.session, conv_id)
        if conv is None:
            return DeliveryResult("discard", reason="conversation_gone")
        newest_in = await self.session.scalar(
            select(func.max(Message.sequence_number)).where(
                Message.conversation_id == conv.id,
                Message.direction == MessageDirection.INCOMING,
            )
        )
        # Re-check under the row lock (HUMAN_HANDOFF.md §5 race safety).
        if not conv.ai_active or conv.assignee_id is not None:
            await self.session.rollback()
            return DeliveryResult("discard", reason="human_took_over")
        if (newest_in or 0) > state.get("latest_seq", 0):
            await self.session.rollback()
            return DeliveryResult("discard", reason="newer_message")
        if time.time() > state["deadline_ts"] + 5:
            await self.session.rollback()
            return DeliveryResult("discard", reason="deadline")

        agent = state["agent"]
        run_id = state["run_id"]
        if state.get("outcome") == "handoff":
            if state.get("draft"):
                conversation_ai.add_message(
                    self.session,
                    conv,
                    content=state["draft"],
                    sender_kind=SenderKind.AI_AGENT,
                    sender_ai_agent_id=agent.id,
                    client_message_id=f"ai:{run_id}",
                )
            if state.get("handoff_note"):
                conversation_ai.add_message(
                    self.session,
                    conv,
                    content=state["handoff_note"],
                    sender_kind=SenderKind.AI_AGENT,
                    sender_ai_agent_id=agent.id,
                    is_private=True,
                    client_message_id=f"ai-note:{run_id}",
                )
            team = agent.handoff_policy.get("assign_team_id")
            await conversation_ai.handoff_to_human(
                self.session,
                conv,
                reason=state.get("handoff_reason") or "unknown",
                ai_agent_id=agent.id,
                team_id=uuid.UUID(team) if team else None,
            )
            await self.session.commit()
            return DeliveryResult("handoff")

        if "reply" not in agent.allowed_actions:  # service-level action check (§3.3)
            await self.session.rollback()
            return DeliveryResult("discard", reason="ai_action_not_allowed:reply")
        msg = conversation_ai.add_message(
            self.session,
            conv,
            content=state["draft"],
            sender_kind=SenderKind.AI_AGENT,
            sender_ai_agent_id=agent.id,
            client_message_id=f"ai:{run_id}",
            content_attributes={
                "ai": {"run_id": str(run_id), "citations": state.get("citations", [])}
            },
        )
        await self.session.commit()
        return DeliveryResult("reply", reply_message_id=msg.id)

    # --- memory_persistence ------------------------------------------------------

    async def persist(self, state: AgentState) -> None:
        if self.session_id is None:
            return
        ai_session = await self.session.get(
            AiConversationSession, self.session_id, with_for_update=True
        )
        if ai_session is None:
            return
        ai_session.turn_count += 1
        facts = dict(ai_session.facts or {})
        intent = state.get("intent")
        if intent:
            facts["language"] = intent.get("language") or facts.get("language")
            streak = int(facts.get("negative_streak", 0))
            facts["negative_streak"] = (
                streak + 1 if intent.get("sentiment") == "negative" else 0
            )
        ai_session.facts = {k: v for k, v in facts.items() if v is not None}
        if state.get("outcome") == "handoff":
            ai_session.status = "handed_off"
            ai_session.handed_off_at = utcnow()
        # Usage: one ai_response per delivered run, platform key only (BYO keys
        # don't count against the plan quota; docs/ai/AI_ARCHITECTURE.md §4).
        if self.credential is None and state.get("outcome") in ("reply", "handoff"):
            await self.session.execute(
                pg_insert(UsageRecord)
                .values(
                    organization_id=self.organization_id,
                    metric="ai_responses",
                    quantity=1,
                    period_start=date.today().replace(day=1),
                    source_id=str(state["run_id"]),
                )
                .on_conflict_do_nothing(constraint="usage_records_source_uniq")
            )
        await self.session.commit()

    async def unsummarized(self) -> tuple[str, int, list[tuple[int, str]]]:
        """(previous summary, its summarized_through_seq, [(seq, line)] not yet in it)."""
        if self.session_id is None:
            return "", 0, []
        ai_session = await self.session.get(AiConversationSession, self.session_id)
        if ai_session is None:
            return "", 0, []
        rows = (
            await self.session.execute(
                select(Message.sequence_number, Message.sender_kind, Message.content)
                .where(
                    Message.conversation_id == ai_session.conversation_id,
                    Message.is_private.is_(False),
                    Message.direction != MessageDirection.ACTIVITY,
                    Message.deleted_at.is_(None),
                    Message.sequence_number > ai_session.summarized_through_seq,
                )
                .order_by(Message.sequence_number)
            )
        ).all()
        who = {
            SenderKind.CONTACT: "Customer",
            SenderKind.AI_AGENT: "AI",
            SenderKind.USER: "Agent",
        }
        return (
            ai_session.summary,
            ai_session.summarized_through_seq,
            [(s, f"{who.get(k, 'System')}: {c or ''}") for s, k, c in rows],
        )

    async def save_summary(self, summary: str, through_seq: int, old_seq: int) -> None:
        """Optimistic: only if nobody summarised in between (MEMORY_MANAGEMENT.md §3)."""
        if self.session_id is None:
            return
        from sqlalchemy import update

        await self.session.execute(
            update(AiConversationSession)
            .where(
                AiConversationSession.id == self.session_id,
                AiConversationSession.summarized_through_seq == old_seq,
            )
            .values(summary=summary, summarized_through_seq=through_seq)
        )
        await self.session.commit()


@dataclass
class PlaygroundRepo(DbRepo):
    """Same agent and knowledge; simulated conversation; writes nothing."""

    message: str = ""
    turns: list[ChatTurn] = field(default_factory=list)
    customer: CustomerView = field(
        default_factory=lambda: CustomerView(
            first_name=None, verification_level="none", verification_method=None
        )
    )

    async def load_input(
        self, conversation_id: uuid.UUID | None, trigger_message_id: uuid.UUID | None
    ) -> tuple[str, int] | None:
        text = self.message.strip()
        return (text[:MAX_INPUT_CHARS], 0) if text else None

    async def load_conversation(
        self, conversation_id: uuid.UUID | None
    ) -> tuple[ConversationView, list[ChatTurn], str, dict[str, Any]]:
        turns = [*self.turns, ChatTurn("user", self.message)]
        ai_turns = sum(1 for t in self.turns if t.role == "assistant")
        return (
            ConversationView(id=None, channel="playground", ai_turn_count=ai_turns),
            turns,
            "",
            {},
        )

    async def load_customer(self, conversation_id: uuid.UUID | None) -> CustomerView:
        return self.customer

    async def customer_info(
        self, conversation: ConversationView, customer: CustomerView
    ) -> dict[str, Any]:
        return {
            "attributes": {},
            "labels": [],
            "recent_topics": [],
            "note": "playground: no real customer",
        }

    async def outside_working_hours(self, conversation_id: uuid.UUID | None) -> bool:
        return False

    async def deliver(self, state: AgentState) -> DeliveryResult:
        return DeliveryResult(state.get("outcome") or "reply")

    async def persist(self, state: AgentState) -> None:
        return None
