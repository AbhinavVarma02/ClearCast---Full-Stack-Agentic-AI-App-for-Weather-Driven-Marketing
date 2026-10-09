"""Human-in-the-loop review: plan hashing, state transitions, and session storage.

States: Draft -> Validation Failed | Pending Review -> Approved | Rejected.
Only validated plans can be approved; approval binds to an exact plan hash,
and any revision produces a new hash, which invalidates an earlier approval.
Storage is in-memory and session-scoped (lost on restart, by design).
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections import OrderedDict
from datetime import datetime

from agent.errors import PlanNotFoundError, ReviewConflictError
from agent.schemas import CampaignPlan, ReviewEvent, ReviewState, ReviewStatus

_HASH_EXCLUDE = {"plan_hash", "review", "status", "diagnostics", "updated_at"}


def compute_plan_hash(plan: CampaignPlan) -> str:
    """SHA-256 over the plan content a reviewer sees (excludes review bookkeeping)."""
    content = plan.model_dump(mode="json", exclude=_HASH_EXCLUDE)
    canonical = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def initial_review(status: ReviewStatus, revision: int, plan_hash: str, at: datetime) -> ReviewState:
    events = [
        ReviewEvent(
            at=at,
            action="generated",
            from_status=None,
            to_status=ReviewStatus.DRAFT,
            revision=revision,
            plan_hash=None,
            note=None,
        ),
        ReviewEvent(
            at=at,
            action="validated" if status == ReviewStatus.PENDING_REVIEW else "validation_failed",
            from_status=ReviewStatus.DRAFT,
            to_status=status,
            revision=revision,
            plan_hash=plan_hash,
            note=None,
        ),
    ]
    return ReviewState(status=status, decided_at=None, note=None, approved_plan_hash=None, history=events)


def finalize(plan: CampaignPlan, status: ReviewStatus, now: datetime) -> CampaignPlan:
    """Set status fields and recompute the hash after content changes."""
    plan.status = status
    plan.review.status = status
    plan.diagnostics.review_status = status.value
    plan.updated_at = now
    plan.plan_hash = compute_plan_hash(plan)
    return plan


def apply_decision(plan: CampaignPlan, decision: str, plan_hash: str, note: str, now: datetime) -> CampaignPlan:
    """Approve or reject the exact plan revision the reviewer inspected."""
    if plan.status == ReviewStatus.VALIDATION_FAILED:
        raise ReviewConflictError(
            "This plan failed validation and cannot be approved or rejected.", code="plan_not_validated"
        )
    if plan_hash != plan.plan_hash:
        raise ReviewConflictError(
            "The plan changed since it was displayed. Review the latest revision before deciding.",
            code="stale_plan",
        )
    if plan.status != ReviewStatus.PENDING_REVIEW:
        raise ReviewConflictError(
            f"Only plans pending review can be decided (current status: {plan.status.value}).",
            code="invalid_transition",
        )
    target = ReviewStatus.APPROVED if decision == "approve" else ReviewStatus.REJECTED
    updated = plan.model_copy(deep=True)
    updated.review.history.append(
        ReviewEvent(
            at=now,
            action=decision,
            from_status=plan.status,
            to_status=target,
            revision=plan.revision,
            plan_hash=plan.plan_hash,
            note=note or None,
        )
    )
    updated.review.decided_at = now
    updated.review.note = note or None
    updated.review.approved_plan_hash = plan.plan_hash if target == ReviewStatus.APPROVED else None
    return finalize(updated, target, now)


def approval_is_current(plan: CampaignPlan) -> bool:
    return plan.status == ReviewStatus.APPROVED and plan.review.approved_plan_hash == plan.plan_hash


class PlanStore:
    """Session-scoped, bounded, in-memory storage for plans and review state."""

    def __init__(self, *, max_plans_per_session: int = 20):
        self._sessions: dict[str, OrderedDict[str, CampaignPlan]] = {}
        self._lock = threading.Lock()
        self._max_plans = max_plans_per_session

    def put(self, session_id: str, plan: CampaignPlan) -> None:
        with self._lock:
            plans = self._sessions.setdefault(session_id, OrderedDict())
            plans[plan.request_id] = plan
            plans.move_to_end(plan.request_id)
            while len(plans) > self._max_plans:
                plans.popitem(last=False)

    def get(self, session_id: str, request_id: str) -> CampaignPlan:
        """Return a plan only to the session that created it."""
        with self._lock:
            plan = self._sessions.get(session_id, {}).get(request_id)
        if plan is None:
            raise PlanNotFoundError("No plan with that request ID exists for this session.")
        return plan

    def remove_session(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)

    def __len__(self) -> int:
        with self._lock:
            return len(self._sessions)
