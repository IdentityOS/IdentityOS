"""
core/operations — reusable autonomous-operator subsystem.

An operator identity (e.g. Aster) uses these primitives to run a persistent,
evidence-backed loop: observe a project, detect needs, discover and evaluate
opportunities, send permitted individualized outreach, monitor replies, follow
up, and escalate consequential decisions to a human.

Nothing in this package is specific to one mission.  Mission data lives in an
:class:`~core.operations.config.OperatorConfig`.
"""

from __future__ import annotations

from .capability_gap import CapabilityGap, CapabilityGapDetector, CapabilityStatus
from .composition import OutreachBrief, OutreachComposer
from .config import OperatorConfig
from .discovery import (
    Candidate,
    CallableCandidateSource,
    CandidateSource,
    OpportunityDiscoverer,
    SearchCandidateSource,
    StaticCandidateSource,
)
from .engine import OperationsEngine, TickReport
from .evaluation import ContactDecision, DuplicateContactPolicy, TargetEvaluator
from .followups import FollowUpPlanner
from .maintenance import SelfMaintenance, load_maintenance_report
from .models import (
    BudgetState,
    ControlState,
    EmailJob,
    EmailJobStatus,
    Evaluation,
    FollowUp,
    FollowUpStatus,
    Message,
    MessageDirection,
    MessageStatus,
    Need,
    NeedStatus,
    Opportunity,
    OpportunityStatus,
    ProjectState,
    ProvenanceEntry,
    ProvenancePhase,
    Relationship,
    RelationshipStatus,
)
from .monitor import (
    ConversationMonitor,
    InboundDisposition,
    InboundResult,
    classify_intent,
)
from .email_jobs import (
    advance_job,
    claim_job,
    context_version,
    ensure_job,
    fail_job,
    find_sent_reply,
    log_transition,
    reconcile_job,
    response_hash,
)
from .needs import NeedDetector, RequirementRule
from .observer import ProjectStateObserver
from .notify import (
    NotifyEvent,
    NotifyImportance,
    NotifyKind,
    NotificationManager,
)
from .policy import Authority, AuthorityPolicy, AuthorizationDecision
from .presence import PresenceStatus, PresenceStore
from .principal import (
    BUILDER_PURPOSE,
    CONTROL_CHANNEL,
    MAX_MESSAGE_CHARS,
    CommandClass,
    build_principal_context,
    classify_command,
    ensure_builder_relationship,
    find_builder_relationship,
    pending_principal_messages,
    submit_principal_message,
    thread_messages,
)
from .store import OperationsStore

__all__ = [
    "BUILDER_PURPOSE",
    "CONTROL_CHANNEL",
    "MAX_MESSAGE_CHARS",
    "CommandClass",
    "PresenceStatus",
    "PresenceStore",
    "build_principal_context",
    "classify_command",
    "ensure_builder_relationship",
    "find_builder_relationship",
    "pending_principal_messages",
    "submit_principal_message",
    "thread_messages",
    "Authority",
    "AuthorityPolicy",
    "AuthorizationDecision",
    "BudgetState",
    "CallableCandidateSource",
    "Candidate",
    "CandidateSource",
    "CapabilityGap",
    "CapabilityGapDetector",
    "CapabilityStatus",
    "ContactDecision",
    "ControlState",
    "ConversationMonitor",
    "DuplicateContactPolicy",
    "EmailJob",
    "EmailJobStatus",
    "Evaluation",
    "FollowUp",
    "FollowUpPlanner",
    "FollowUpStatus",
    "InboundDisposition",
    "InboundResult",
    "Message",
    "advance_job",
    "claim_job",
    "context_version",
    "ensure_job",
    "fail_job",
    "find_sent_reply",
    "log_transition",
    "reconcile_job",
    "response_hash",
    "MessageDirection",
    "MessageStatus",
    "Need",
    "NeedDetector",
    "NeedStatus",
    "NotificationManager",
    "NotifyEvent",
    "NotifyImportance",
    "NotifyKind",
    "OperationsEngine",
    "OperationsStore",
    "OperatorConfig",
    "Opportunity",
    "OpportunityDiscoverer",
    "OpportunityStatus",
    "OutreachBrief",
    "OutreachComposer",
    "ProjectState",
    "ProjectStateObserver",
    "ProvenanceEntry",
    "ProvenancePhase",
    "Relationship",
    "RelationshipStatus",
    "RequirementRule",
    "SearchCandidateSource",
    "SelfMaintenance",
    "StaticCandidateSource",
    "TargetEvaluator",
    "TickReport",
    "classify_intent",
    "load_maintenance_report",
]
