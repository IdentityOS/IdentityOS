"""
core/operations/store.py

Durable persistence for the Operations subsystem.

All operator state lives in the identity's storage backend under the
``operations.*`` namespace family.  There is no module-level state, so an
operator identity resumes exactly where it left off after a process restart:
the engine is constructed fresh and reloads every collection from storage.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Optional

from .models import (
    BudgetState,
    ControlState,
    Evaluation,
    FollowUp,
    FollowUpStatus,
    MailboxCursor,
    Message,
    Need,
    NeedStatus,
    NotificationEntry,
    Opportunity,
    ProvenanceEntry,
    ProjectState,
    Relationship,
    RelationshipStatus,
    utcnow,
)


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower())


class OperationsStore:
    """Loads and persists all operator state for a single identity."""

    PROJECT = "operations.project_state"
    NEEDS = "operations.needs"
    OPPORTUNITIES = "operations.opportunities"
    EVALUATIONS = "operations.evaluations"
    RELATIONSHIPS = "operations.relationships"
    MESSAGES = "operations.messages"
    PROVENANCE = "operations.provenance"
    FOLLOW_UPS = "operations.followups"
    CONTROLS = "operations.controls"
    BUDGET = "operations.budget"
    NOTIFICATIONS = "operations.notifications"
    MAILBOX_CURSOR = "operations.mailbox_cursor"

    def __init__(self, storage: Any, identity_id: str) -> None:
        self._storage = storage
        self.identity_id = identity_id
        self._project: Optional[ProjectState] = self._load_project()
        self._needs: dict[str, Need] = self._load_needs()
        self._opportunities: dict[str, Opportunity] = self._load_opportunities()
        self._evaluations: dict[str, Evaluation] = self._load_evaluations()
        self._relationships: dict[str, Relationship] = self._load_relationships()
        self._messages: list[Message] = self._load_messages()
        self._provenance: list[ProvenanceEntry] = self._load_provenance()
        self._follow_ups: list[FollowUp] = self._load_follow_ups()
        self._controls: ControlState = self._load_controls()
        self._budget: BudgetState = self._load_budget()
        self._notifications: list[NotificationEntry] = self._load_notifications()
        self._mailbox_cursor: MailboxCursor = self._load_mailbox_cursor()

    # ── load helpers ──────────────────────────────────────────────────

    def _load(self, namespace: str) -> Optional[dict]:
        return self._storage.load(self.identity_id, namespace)

    def refresh(self) -> None:
        """Re-load every namespace from storage.

        A long-running process's in-memory cache goes stale as other writers
        (scripts, other processes) persist newer state; last-writer-wins on
        the next save would silently erase their updates (observed: a daemon
        built days earlier overwrote a relationship added by a fresh script).
        Refreshing at tick start keeps every writer's changes.
        """
        self._project = self._load_project()
        self._needs = self._load_needs()
        self._opportunities = self._load_opportunities()
        self._evaluations = self._load_evaluations()
        self._relationships = self._load_relationships()
        self._messages = self._load_messages()
        self._provenance = self._load_provenance()
        self._follow_ups = self._load_follow_ups()
        self._controls = self._load_controls()
        self._budget = self._load_budget()
        self._notifications = self._load_notifications()
        self._mailbox_cursor = self._load_mailbox_cursor()

    def _load_project(self) -> Optional[ProjectState]:
        raw = self._load(self.PROJECT)
        return ProjectState.from_dict(raw) if raw else None

    def _load_needs(self) -> dict[str, Need]:
        raw = self._load(self.NEEDS) or {}
        return {n["id"]: Need.from_dict(n) for n in raw.get("items", [])}

    def _load_opportunities(self) -> dict[str, Opportunity]:
        raw = self._load(self.OPPORTUNITIES) or {}
        return {o["id"]: Opportunity.from_dict(o) for o in raw.get("items", [])}

    def _load_evaluations(self) -> dict[str, Evaluation]:
        raw = self._load(self.EVALUATIONS) or {}
        return {e["opportunity_id"]: Evaluation.from_dict(e) for e in raw.get("items", [])}

    def _load_relationships(self) -> dict[str, Relationship]:
        raw = self._load(self.RELATIONSHIPS) or {}
        return {r["id"]: Relationship.from_dict(r) for r in raw.get("items", [])}

    def _load_messages(self) -> list[Message]:
        raw = self._load(self.MESSAGES) or {}
        return [Message.from_dict(m) for m in raw.get("items", [])]

    def _load_provenance(self) -> list[ProvenanceEntry]:
        raw = self._load(self.PROVENANCE) or {}
        return [ProvenanceEntry.from_dict(p) for p in raw.get("items", [])]

    def _load_follow_ups(self) -> list[FollowUp]:
        raw = self._load(self.FOLLOW_UPS) or {}
        return [FollowUp.from_dict(f) for f in raw.get("items", [])]

    def _load_controls(self) -> ControlState:
        raw = self._load(self.CONTROLS)
        return ControlState.from_dict(raw) if raw else ControlState()

    def _load_budget(self) -> BudgetState:
        raw = self._load(self.BUDGET)
        budget = BudgetState.from_dict(raw) if raw else BudgetState()
        if budget.day != _today():
            budget = BudgetState(day=_today())
            self._save_budget(budget)
        return budget

    def _load_notifications(self) -> list[NotificationEntry]:
        raw = self._load(self.NOTIFICATIONS) or {}
        return [NotificationEntry.from_dict(n) for n in raw.get("items", [])]

    def _load_mailbox_cursor(self) -> MailboxCursor:
        raw = self._load(self.MAILBOX_CURSOR)
        return MailboxCursor.from_dict(raw) if raw else MailboxCursor()

    # ── save helpers ──────────────────────────────────────────────────

    def _save(self, namespace: str, data: dict) -> None:
        self._storage.save(self.identity_id, namespace, data)

    def _save_project(self) -> None:
        if self._project is not None:
            self._save(self.PROJECT, self._project.to_dict())

    def _save_needs(self) -> None:
        self._save(self.NEEDS, {"items": [n.to_dict() for n in self._needs.values()]})

    def _save_opportunities(self) -> None:
        self._save(self.OPPORTUNITIES, {"items": [o.to_dict() for o in self._opportunities.values()]})

    def _save_evaluations(self) -> None:
        self._save(self.EVALUATIONS, {"items": [e.to_dict() for e in self._evaluations.values()]})

    def _save_relationships(self) -> None:
        self._save(self.RELATIONSHIPS, {"items": [r.to_dict() for r in self._relationships.values()]})

    def _save_messages(self) -> None:
        self._save(self.MESSAGES, {"items": [m.to_dict() for m in self._messages]})

    def _save_provenance(self) -> None:
        self._save(self.PROVENANCE, {"items": [p.to_dict() for p in self._provenance]})

    def _save_follow_ups(self) -> None:
        self._save(self.FOLLOW_UPS, {"items": [f.to_dict() for f in self._follow_ups]})

    def _save_controls(self) -> None:
        self._save(self.CONTROLS, self._controls.to_dict())

    def _save_budget(self, budget: Optional[BudgetState] = None) -> None:
        self._save(self.BUDGET, (budget or self._budget).to_dict())

    def _save_notifications(self) -> None:
        self._save(self.NOTIFICATIONS, {"items": [n.to_dict() for n in self._notifications]})

    def _save_mailbox_cursor(self) -> None:
        self._save(self.MAILBOX_CURSOR, self._mailbox_cursor.to_dict())

    # ── project state ─────────────────────────────────────────────────

    def project_state(self) -> Optional[ProjectState]:
        return self._project

    def set_project_state(self, state: ProjectState) -> None:
        self._project = state
        self._save_project()

    # ── mailbox cursor ─────────────────────────────────────────────────

    def mailbox_cursor(self) -> MailboxCursor:
        return self._mailbox_cursor

    def set_mailbox_cursor(self, cursor: MailboxCursor | dict[str, Any]) -> None:
        cursor = cursor if isinstance(cursor, MailboxCursor) else MailboxCursor.from_dict(cursor)
        cursor.updated_at = utcnow().isoformat()
        self._mailbox_cursor = cursor
        self._save_mailbox_cursor()

    # ── needs ─────────────────────────────────────────────────────────

    def list_needs(self, *, status: Optional[NeedStatus] = None) -> list[Need]:
        items = list(self._needs.values())
        if status is not None:
            items = [n for n in items if n.status is status]
        return sorted(items, key=lambda n: n.priority, reverse=True)

    def get_need(self, need_id: str) -> Optional[Need]:
        return self._needs.get(need_id)

    def find_need(self, category: str, description: str) -> Optional[Need]:
        target = (_norm(category), _norm(description))
        for need in self._needs.values():
            if (_norm(need.category), _norm(need.description)) == target:
                return need
        return None

    def add_need(self, need: Need) -> Need:
        self._needs[need.id] = need
        self._save_needs()
        return need

    def update_need(self, need: Need) -> None:
        need.updated_at = utcnow().isoformat()
        self._needs[need.id] = need
        self._save_needs()

    # ── opportunities ─────────────────────────────────────────────────

    def list_opportunities(self, *, status: Optional[OpportunityStatus] = None) -> list[Opportunity]:
        items = list(self._opportunities.values())
        if status is not None:
            items = [o for o in items if o.status is status]
        return items

    def get_opportunity(self, opp_id: str) -> Optional[Opportunity]:
        return self._opportunities.get(opp_id)

    def add_opportunity(self, opp: Opportunity) -> Opportunity:
        self._opportunities[opp.id] = opp
        self._save_opportunities()
        return opp

    def update_opportunity(self, opp: Opportunity) -> None:
        opp.updated_at = utcnow().isoformat()
        self._opportunities[opp.id] = opp
        self._save_opportunities()

    def find_opportunity(self, target_name: str, organization: str = "") -> Optional[Opportunity]:
        target = (_norm(target_name), _norm(organization))
        for opp in self._opportunities.values():
            if (_norm(opp.target_name), _norm(opp.organization)) == target:
                return opp
        return None

    # ── evaluations ───────────────────────────────────────────────────

    def get_evaluation(self, opp_id: str) -> Optional[Evaluation]:
        return self._evaluations.get(opp_id)

    def save_evaluation(self, evaluation: Evaluation) -> None:
        self._evaluations[evaluation.opportunity_id] = evaluation
        self._save_evaluations()

    # ── relationships ─────────────────────────────────────────────────

    def list_relationships(self, *, status: Optional[RelationshipStatus] = None) -> list[Relationship]:
        items = list(self._relationships.values())
        if status is not None:
            items = [r for r in items if r.status is status]
        return items

    def get_relationship(self, rel_id: str) -> Optional[Relationship]:
        return self._relationships.get(rel_id)

    def find_relationship_by_email(self, email: str) -> Optional[Relationship]:
        target = _norm(email)
        if not target:
            return None
        for rel in self._relationships.values():
            if _norm(rel.email) == target:
                return rel
        return None

    def find_relationship_by_thread(self, thread_id: str) -> Optional[Relationship]:
        if not thread_id:
            return None
        for rel in self._relationships.values():
            if thread_id in rel.thread_ids:
                return rel
        return None

    def add_relationship(self, rel: Relationship) -> Relationship:
        self._relationships[rel.id] = rel
        self._save_relationships()
        return rel

    def update_relationship(self, rel: Relationship) -> None:
        rel.touch()
        self._relationships[rel.id] = rel
        self._save_relationships()

    def has_contacted(self, email: str, organization: str = "") -> bool:
        target_email = _norm(email)
        target_org = _norm(organization)
        for rel in self._relationships.values():
            if target_email and _norm(rel.email) == target_email:
                return True
            if target_org and target_email == "" and _norm(rel.organization) == target_org:
                return True
        return False

    # ── messages ──────────────────────────────────────────────────────

    def list_messages(self, *, relationship_id: Optional[str] = None) -> list[Message]:
        items = list(self._messages)
        if relationship_id is not None:
            items = [m for m in items if m.relationship_id == relationship_id]
        return items

    def get_message(self, message_id: str) -> Optional[Message]:
        return next((m for m in self._messages if m.id == message_id), None)

    def append_message(self, message: Message) -> Message:
        self._messages.append(message)
        self._save_messages()
        return message

    def update_message(self, message: Message) -> None:
        for idx, existing in enumerate(self._messages):
            if existing.id == message.id:
                self._messages[idx] = message
                break
        self._save_messages()

    # ── provenance ────────────────────────────────────────────────────

    def append_provenance(self, entry: ProvenanceEntry) -> ProvenanceEntry:
        self._provenance.append(entry)
        self._save_provenance()
        return entry

    def list_provenance(self, *, limit: Optional[int] = None) -> list[ProvenanceEntry]:
        items = list(self._provenance)
        if limit is not None:
            items = items[-limit:]
        return items

    # ── notifications ───────────────────────────────────────────────

    def append_notification(self, entry: NotificationEntry) -> NotificationEntry:
        self._notifications.append(entry)
        self._save_notifications()
        return entry

    def list_notifications(self, *, unread_only: bool = False) -> list[NotificationEntry]:
        items = list(self._notifications)
        if unread_only:
            items = [n for n in items if not n.read]
        return items

    def unread_notification_count(self) -> int:
        return sum(1 for n in self._notifications if not n.read)

    def mark_notifications_read(self, ids: Optional[list[str]] = None) -> int:
        marked = 0
        for entry in self._notifications:
            if ids is not None and entry.id not in ids:
                continue
            if not entry.read:
                entry.read = True
                marked += 1
        if marked:
            self._save_notifications()
        return marked

    # ── follow-ups ────────────────────────────────────────────────────

    def list_follow_ups(self, *, status: Optional[FollowUpStatus] = None) -> list[FollowUp]:
        items = list(self._follow_ups)
        if status is not None:
            items = [f for f in items if f.status is status]
        return items

    def get_follow_up(self, follow_up_id: str) -> Optional[FollowUp]:
        return next((f for f in self._follow_ups if f.id == follow_up_id), None)

    def add_follow_up(self, follow_up: FollowUp) -> FollowUp:
        self._follow_ups.append(follow_up)
        self._save_follow_ups()
        return follow_up

    def update_follow_up(self, follow_up: FollowUp) -> None:
        for idx, existing in enumerate(self._follow_ups):
            if existing.id == follow_up.id:
                self._follow_ups[idx] = follow_up
                break
        self._save_follow_ups()

    def due_follow_ups(self, now: Optional[datetime] = None) -> list[FollowUp]:
        now = now or datetime.now(timezone.utc)
        due: list[FollowUp] = []
        for follow_up in self._follow_ups:
            if follow_up.status is not FollowUpStatus.SCHEDULED:
                continue
            try:
                due_at = datetime.fromisoformat(follow_up.due_at)
            except ValueError:
                continue
            if due_at.tzinfo is None:
                due_at = due_at.replace(tzinfo=timezone.utc)
            if due_at <= now:
                due.append(follow_up)
        return due

    # ── controls ──────────────────────────────────────────────────────

    def controls(self) -> ControlState:
        return self._controls

    def set_controls(self, controls: ControlState) -> None:
        controls.updated_at = utcnow().isoformat()
        self._controls = controls
        self._save_controls()

    # ── budget ────────────────────────────────────────────────────────

    def budget(self) -> BudgetState:
        if self._budget.day != _today():
            self._budget = BudgetState(day=_today())
            self._save_budget()
        return self._budget

    def record_usage(self, kind: str, amount: int = 1) -> int:
        budget = self.budget()
        if not hasattr(budget, kind):
            raise ValueError(f"Unknown budget kind: {kind}")
        setattr(budget, kind, getattr(budget, kind) + amount)
        self._save_budget(budget)
        return getattr(budget, kind)
