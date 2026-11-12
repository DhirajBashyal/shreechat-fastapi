"""All SQLAlchemy models. Importing this package registers every table on
``Base.metadata`` (needed by Alembic autogenerate).

Schema source: docs/database/INITIAL_SCHEMA_SQL.md (66 tables).
"""

from app.models.ai import (
    AiAgent,
    AiAgentInbox,
    AiAgentKnowledgeBase,
    AiConversationSession,
    AiProviderCredential,
    AiRun,
    AiToolCall,
    KbChunk,
    KbDocument,
    KnowledgeBase,
)
from app.models.automation import AutomationRule, AutomationRuleExecution
from app.models.base import Base
from app.models.billing import (
    AuditLog,
    BillingRecord,
    BillingWebhookEvent,
    ReportingEvent,
    Subscription,
    UsageRecord,
)
from app.models.channels import (
    ChannelConnection,
    ChannelRoute,
    FacebookPage,
    Inbox,
    InboxMember,
    InstagramAccount,
    WebWidget,
    WhatsappBusinessAccount,
    WhatsappMessageTemplate,
    WhatsappPhoneNumber,
)
from app.models.contacts import Contact, ContactIdentity, ContactLabel, Note
from app.models.conversations import (
    Attachment,
    Conversation,
    ConversationAssignment,
    ConversationLabel,
    ConversationParticipant,
    Mention,
    Message,
)
from app.models.events import (
    EventOutbox,
    IdempotencyKey,
    InboundWebhookEvent,
    WebhookDelivery,
    WebhookSubscription,
)
from app.models.files import StoredFile
from app.models.platform import (
    Permission,
    PlatformAdmin,
    SubscriptionPlan,
    User,
    UserSession,
)
from app.models.productivity import (
    CannedResponse,
    CustomAttributeDefinition,
    Label,
    Macro,
    Notification,
)
from app.models.teams import Team, TeamMember
from app.models.tenancy import (
    ApiAccessToken,
    Invitation,
    Organization,
    OrganizationMembership,
    Role,
    RolePermission,
    Workspace,
    WorkspaceCounter,
    WorkspaceMembership,
)

__all__ = [
    "Base",
    # platform
    "User",
    "PlatformAdmin",
    "UserSession",
    "Permission",
    "SubscriptionPlan",
    # tenancy
    "Organization",
    "Role",
    "RolePermission",
    "OrganizationMembership",
    "Invitation",
    "ApiAccessToken",
    "Workspace",
    "WorkspaceCounter",
    "WorkspaceMembership",
    # teams
    "Team",
    "TeamMember",
    # inboxes & channels
    "Inbox",
    "InboxMember",
    "ChannelConnection",
    "ChannelRoute",
    "FacebookPage",
    "InstagramAccount",
    "WhatsappBusinessAccount",
    "WhatsappPhoneNumber",
    "WhatsappMessageTemplate",
    "WebWidget",
    # files
    "StoredFile",
    # contacts
    "Contact",
    "ContactIdentity",
    "Note",
    "ContactLabel",
    # conversations
    "Conversation",
    "Message",
    "Attachment",
    "ConversationAssignment",
    "ConversationParticipant",
    "Mention",
    "ConversationLabel",
    # productivity
    "Label",
    "CustomAttributeDefinition",
    "CannedResponse",
    "Macro",
    "Notification",
    # events & webhooks
    "EventOutbox",
    "InboundWebhookEvent",
    "WebhookSubscription",
    "WebhookDelivery",
    "IdempotencyKey",
    # automation
    "AutomationRule",
    "AutomationRuleExecution",
    # AI
    "AiProviderCredential",
    "AiAgent",
    "AiAgentInbox",
    "KnowledgeBase",
    "AiAgentKnowledgeBase",
    "KbDocument",
    "KbChunk",
    "AiConversationSession",
    "AiRun",
    "AiToolCall",
    # reporting, billing, audit
    "ReportingEvent",
    "Subscription",
    "BillingRecord",
    "UsageRecord",
    "BillingWebhookEvent",
    "AuditLog",
]
