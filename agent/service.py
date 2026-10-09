"""Campaign planning service: the Python orchestration layer behind the API.

Flow for one request:

1. LangGraph tool-calling loop (chatbot -> ToolNode -> chatbot) on the
   session's own thread gathers weather evidence through MCP tools.
2. Python rebuilds the evidence ledger from this request's tool outputs and
   evaluates every forecast block against the client's hard constraints.
3. The model drafts a structured ``CampaignDraft`` (strict JSON schema).
4. Deterministic validation checks every claim; failures get a bounded number
   of repair attempts. Plans that still fail are returned as Validation Failed.
5. Validated plans enter Pending Review; humans approve or reject them.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from collections.abc import Callable
from datetime import UTC, datetime

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.errors import GraphRecursionError

from agent.clients import client_snapshot, get_profile
from agent.errors import (
    ClearCastError,
    ModelProviderError,
    ReviewConflictError,
    SessionBusyError,
    classify_model_exception,
)
from agent.evidence import build_ledger, daily_summary, extract_tool_results, turn_messages
from agent.graph import RECURSION_LIMIT, graph_config, thread_id_for
from agent.llm import MODEL_TIMEOUT_SECONDS
from agent.observability import RequestMetrics, session_ref
from agent.prompts import (
    DRAFTING_SYSTEM_PROMPT,
    build_agent_brief,
    build_drafting_message,
    evidence_row,
)
from agent.report import render_report
from agent.review import PlanStore, apply_decision, compute_plan_hash, finalize, initial_review
from agent.runtime import AgentRuntime
from agent.schemas import (
    CampaignDraft,
    CampaignPlan,
    CampaignPlanRequest,
    CampaignPlanResponse,
    CampaignWindow,
    ClientSnapshot,
    ConstraintCheck,
    EvidenceLedger,
    GroundingCheck,
    ReviewEvent,
    ReviewRequest,
    ReviewStatus,
    RevisionRequest,
    Risk,
    RiskDraft,
    ValidationIssue,
    ValidationReport,
)
from agent.validation import (
    BlockAssessment,
    DraftValidation,
    WindowValidation,
    ad_copy_issues,
    assess_blocks,
    repair_feedback,
    validate_draft,
)

logger = logging.getLogger("clearcast.service")

MAX_REPAIR_ATTEMPTS = 2
REQUEST_DEADLINE_SECONDS = 150.0
SESSION_TTL_SECONDS = 6 * 3600
MAX_SESSIONS = 500
MAX_NOTES_LENGTH = 2000


def daypart(moment: datetime | None) -> str:
    if moment is None:
        return "unknown"
    hour = moment.hour
    if 5 <= hour < 11:
        return "morning"
    if 11 <= hour < 14:
        return "midday"
    if 14 <= hour < 17:
        return "afternoon"
    if 17 <= hour < 21:
        return "evening"
    return "night"


def _error(code: str, message: str, window_id: str | None = None) -> ValidationIssue:
    return ValidationIssue(code=code, message=message, severity="error", window_id=window_id)


def _risks(drafts: list[RiskDraft]) -> list[Risk]:
    return [
        Risk(risk=d.risk.strip(), mitigation=d.mitigation.strip(), observation_ids=list(d.observation_ids))
        for d in drafts
    ]


def _no_eligible_issue(assessments: dict[str, BlockAssessment]) -> ValidationIssue:
    reasons: dict[str, int] = {}
    keywords = (
        ("temperature", "temperature outside the allowed range"),
        ("precipitation", "precipitation probability above the limit"),
        ("wind", "wind above the limit"),
        ("AQI", "AQI above the limit or not verifiable"),
    )
    for assessment in assessments.values():
        labels = set()
        for violation in assessment.weather_violations:
            labels.update(label for key, label in keywords if key in violation)
        for note in assessment.time_notes:
            labels.add(note.split(":")[0] if note.startswith("excluded") else note)
        for label in labels:
            reasons[label] = reasons.get(label, 0) + 1
    top = sorted(reasons.items(), key=lambda item: -item[1])[:4]
    detail = "; ".join(f"{label} ({count} blocks)" for label, count in top)
    return _error(
        "no_eligible_blocks",
        "No forecast block satisfies the client's hard constraints"
        + (f". Most common reasons: {detail}." if detail else "."),
    )


class CampaignPlanningService:
    def __init__(
        self,
        runtime: AgentRuntime,
        *,
        store: PlanStore | None = None,
        clock: Callable[[], datetime] | None = None,
        max_repair_attempts: int = MAX_REPAIR_ATTEMPTS,
        request_deadline_seconds: float = REQUEST_DEADLINE_SECONDS,
    ) -> None:
        self.runtime = runtime
        self.store = store or PlanStore()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._max_repairs = max_repair_attempts
        self._deadline = request_deadline_seconds
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._sessions: OrderedDict[str, float] = OrderedDict()

    async def aclose(self) -> None:
        await self.runtime.aclose()

    # -- session bookkeeping -------------------------------------------------
    def _touch(self, session_id: str) -> None:
        self._sessions[session_id] = time.monotonic()
        self._sessions.move_to_end(session_id)
        cutoff = time.monotonic() - SESSION_TTL_SECONDS
        expired = [sid for sid, seen in self._sessions.items() if seen < cutoff]
        while len(self._sessions) - len(expired) > MAX_SESSIONS:
            oldest = next(sid for sid in self._sessions if sid not in expired)
            expired.append(oldest)
        for sid in expired:
            lock = self._session_locks.get(sid)
            if lock is not None and lock.locked():
                continue
            self._sessions.pop(sid, None)
            self._session_locks.pop(sid, None)
            self.store.remove_session(sid)
            self.runtime.checkpointer.delete_thread(thread_id_for(sid))

    def _lock_for(self, session_id: str) -> asyncio.Lock:
        lock = self._session_locks.setdefault(session_id, asyncio.Lock())
        if lock.locked():
            raise SessionBusyError("A campaign request for this session is already running.")
        return lock

    # -- planning --------------------------------------------------------------
    async def create_plan(self, request: CampaignPlanRequest, *, request_id: str) -> CampaignPlanResponse:
        now = self._clock()
        sid = request.session_id
        profile = get_profile(request.client_id)
        client = client_snapshot(profile, request.constraints)
        metrics = RequestMetrics(request_id, session_ref(sid), request.client_id, self.runtime.model)
        lock = self._lock_for(sid)
        async with lock:
            self._touch(sid)
            try:
                plan = await self._plan(request, request_id, profile, client, metrics, now)
            except ClearCastError as exc:
                logger.warning(
                    "campaign plan failed",
                    extra={"event": "plan.failed", "error_code": exc.code, **metrics.log_fields()},
                )
                raise
            self.store.put(sid, plan)
        logger.info(
            "campaign plan completed",
            extra={
                "event": "plan.completed",
                **metrics.log_fields(),
                "validation_status": plan.validation.status,
                "review_status": plan.status.value,
                "windows": len(plan.windows),
                "rejected_windows": len(plan.rejected_windows),
            },
        )
        return CampaignPlanResponse(plan=plan, report_markdown=render_report(plan))

    async def _run_graph(self, graph, brief_text: str, turn_id: str, session_id: str, metrics: RequestMetrics):
        config = graph_config(session_id)
        started = time.perf_counter()
        try:
            return await asyncio.wait_for(
                graph.ainvoke({"messages": [HumanMessage(content=brief_text, id=turn_id)]}, config=config),
                timeout=self._deadline,
            )
        except GraphRecursionError:
            # Keep the evidence gathered before the bound was reached.
            metrics.step_limit_reached = True
            return (await graph.aget_state(config)).values
        except TimeoutError:
            raise ModelProviderError(
                "The agent did not finish in time.", code="agent_timeout", http_status=504, retryable=True
            ) from None
        except ClearCastError:
            raise
        except Exception as exc:
            mapped = classify_model_exception(exc)
            if mapped is not None:
                raise mapped from None
            raise
        finally:
            metrics.graph_seconds = time.perf_counter() - started

    async def _plan(self, request, request_id, profile, client, metrics, now) -> CampaignPlan:
        graph = await self.runtime.get_graph()
        turn_id = f"turn-{request_id}"
        brief_text = build_agent_brief(request.brief, profile, client)
        state = await self._run_graph(graph, brief_text, turn_id, request.session_id, metrics)

        turn = turn_messages(state["messages"], turn_id)
        results = extract_tool_results(turn)
        cache_hits = sum(1 for r in results if r.payload and (r.payload.get("cache") or {}).get("hit"))
        metrics.record_turn(turn, [r.error_category for r in results if r.error_category], cache_hits)
        ledger, findings = build_ledger(results, now=now)
        notes = next(
            (
                m.content
                for m in reversed(turn)
                if isinstance(m, AIMessage) and not m.tool_calls and isinstance(m.content, str) and m.content.strip()
            ),
            None,
        )
        if metrics.step_limit_reached:
            findings.append(
                ValidationIssue(
                    code="step_limit_reached",
                    message=f"The agent reached the graph recursion limit ({RECURSION_LIMIT}).",
                    severity="warning",
                )
            )
        errors = [f for f in findings if f.severity == "error"]
        warnings = [f for f in findings if f.severity == "warning"]
        assessments = assess_blocks(ledger, client.constraints, now) if ledger.forecast else {}
        if not errors and not any(a.eligible for a in assessments.values()):
            errors.append(_no_eligible_issue(assessments))

        validation: DraftValidation | None = None
        draft: CampaignDraft | None = None
        drafting_attempted = not errors
        if drafting_attempted:
            draft, validation, draft_errors, notes_warnings = await self._draft_and_validate(
                request, profile, client, ledger, assessments, notes, metrics, now
            )
            errors.extend(draft_errors)
            warnings.extend(notes_warnings)
            if validation is not None:
                errors.extend(validation.plan_errors)
                window_issues = [issue for w in validation.windows for issue in w.grounding_issues]
                if validation.passed:
                    # Verified windows survive; failed ones are reported, never recommended.
                    warnings.extend(
                        issue.model_copy(update={"severity": "warning", "message": f"Rejected window: {issue.message}"})
                        for issue in window_issues
                    )
                else:
                    errors.extend(window_issues)
                    if not validation.eligible_windows:
                        errors.append(
                            _error(
                                "no_eligible_windows",
                                "No proposed window passed every grounding check and hard client constraint.",
                            )
                        )

        passed = validation is not None and validation.passed and not errors
        status = ReviewStatus.PENDING_REVIEW if passed else ReviewStatus.VALIDATION_FAILED
        windows: list[CampaignWindow] = []
        rejected: list[CampaignWindow] = []
        if validation is not None:
            for result in validation.windows:
                converted = self._window(result)
                (windows if passed and result.eligible else rejected).append(converted)

        report = ValidationReport(
            status="passed" if passed else "failed",
            errors=errors,
            warnings=warnings,
            repair_attempts=metrics.repair_attempts,
            drafting_attempted=drafting_attempted,
            checked_at=now,
        )
        location = ledger.location
        disclaimers = self._disclaimers(client, ledger)
        plan = CampaignPlan(
            request_id=request_id,
            session_ref=metrics.session_ref,
            revision=1,
            plan_hash="0" * 64,
            generated_at=now,
            updated_at=now,
            status=status,
            brief=request.brief,
            campaign_goal=request.brief.campaign_goal or "General awareness",
            client=client,
            resolved_location=location,
            strategy_summary=draft.strategy_summary.strip() if passed and draft else None,
            windows=windows,
            rejected_windows=rejected,
            risks=_risks(draft.overall_risks) if passed and draft else [],
            forecast_summary=daily_summary(ledger.forecast),
            evidence=ledger,
            analyst_notes=(notes or "").strip()[:MAX_NOTES_LENGTH] or None,
            validation=report,
            review=initial_review(status, 1, "0" * 64, now),
            diagnostics=metrics.diagnostics(
                validation_status=report.status, review_status=status.value, recursion_limit=RECURSION_LIMIT
            ),
            disclaimers=disclaimers,
        )
        plan.plan_hash = compute_plan_hash(plan)
        plan.review = initial_review(status, 1, plan.plan_hash, now)
        plan.diagnostics = metrics.diagnostics(
            validation_status=report.status, review_status=status.value, recursion_limit=RECURSION_LIMIT
        )
        return plan

    async def _draft_and_validate(self, request, profile, client, ledger, assessments, notes, metrics, now):
        """Draft with strict structured output; repair at most ``max_repairs`` times."""
        drafter = self.runtime.drafter
        location = ledger.location
        location_label = (
            f"{location.name}, {location.state or location.country}" if location else request.brief.location
        )
        rows = [evidence_row(obs, assessments.get(obs.observation_id)) for obs in ledger.forecast]
        messages = [
            SystemMessage(content=DRAFTING_SYSTEM_PROMPT),
            HumanMessage(
                content=build_drafting_message(
                    request.brief,
                    profile,
                    client,
                    location_label,
                    location.timezone_label if location else "unresolved",
                    rows,
                    notes,
                    now,
                )
            ),
        ]
        best: tuple[CampaignDraft, DraftValidation] | None = None
        last: tuple[CampaignDraft | None, DraftValidation | None, list[ValidationIssue]] = (None, None, [])
        warnings: list[ValidationIssue] = []
        for attempt in range(self._max_repairs + 1):
            started = time.perf_counter()
            try:
                result = await asyncio.wait_for(drafter.draft(messages), timeout=MODEL_TIMEOUT_SECONDS * 2)
            except TimeoutError:
                raise ModelProviderError(
                    "The language model did not respond in time.",
                    code="model_timeout",
                    http_status=504,
                    retryable=True,
                ) from None
            except ClearCastError:
                raise
            except Exception as exc:
                mapped = classify_model_exception(exc)
                if mapped is not None:
                    raise mapped from None
                raise
            finally:
                metrics.drafting_seconds += time.perf_counter() - started
            metrics.record_usage(result.usage)

            if result.parsed is None:
                issue = _error("schema_invalid", result.parse_error or "The draft did not match the schema.")
                last = (None, None, [issue])
                feedback = (
                    f"Your previous output was rejected: {issue.message}. Return only one complete JSON "
                    "object that matches the required schema."
                )
            else:
                validation = validate_draft(result.parsed, ledger, assessments, client.constraints, now)
                last = (result.parsed, validation, [])
                if validation.passed:
                    best = (result.parsed, validation)
                if not validation.needs_repair:
                    break
                feedback = repair_feedback(validation, assessments)
            if attempt == self._max_repairs:
                break
            metrics.repair_attempts += 1
            messages = [
                *messages,
                AIMessage(content=result.raw_text or "(no output)"),
                HumanMessage(content=feedback),
            ]

        draft, validation, errors = last
        if best is not None and (validation is None or not validation.passed):
            # A later repair made things worse; keep the best validated draft.
            warnings.append(
                ValidationIssue(
                    code="repair_regressed",
                    message="A later repair attempt failed validation; the last validated draft was kept.",
                    severity="warning",
                )
            )
            draft, validation = best
            errors = []
        return draft, validation, errors, warnings

    @staticmethod
    def _window(result: WindowValidation) -> CampaignWindow:
        draft = result.draft
        return CampaignWindow(
            window_id=result.window_id,
            title=draft.title.strip()[:120] or result.window_id,
            daypart=daypart(result.start_local),
            start_local=result.start_local,
            end_local=result.end_local,
            start_utc=result.start_local.astimezone(UTC) if result.start_local else None,
            end_utc=result.end_local.astimezone(UTC) if result.end_local else None,
            observation_ids=list(draft.observation_ids),
            evidence=result.blocks,
            observed_conditions=result.observed,
            claimed_conditions=draft.claimed_conditions,
            weather_reasoning=draft.weather_reasoning.strip(),
            marketing_hypothesis=draft.marketing_hypothesis.strip(),
            ad_copy=[line.strip() for line in draft.ad_copy],
            risks=_risks(draft.risks),
            constraint_check=ConstraintCheck(
                eligible=not result.constraint_violations, violations=list(result.constraint_violations)
            ),
            grounding=GroundingCheck(
                verified=result.grounded, issues=[issue.message for issue in result.grounding_issues]
            ),
        )

    @staticmethod
    def _disclaimers(client: ClientSnapshot, ledger: EvidenceLedger) -> list[str]:
        notes = [
            "Marketing hypotheses are untested; nothing here is evidence of demand, campaign lift, or ROI.",
            "ClearCast does not publish advertisements or make external business transactions.",
            "Forecasts change; weather values reflect the fetch times listed in the evidence.",
        ]
        if client.fictional:
            notes.insert(0, f"{client.display_name} is a fictional demo configuration, not a real customer.")
        if any((source.source or "").startswith("fixture:") for source in ledger.sources):
            notes.insert(0, "OFFLINE FIXTURE DATA: synthetic weather used for testing, not real conditions.")
        return notes

    # -- review ----------------------------------------------------------------
    async def review(self, request_id: str, request: ReviewRequest) -> CampaignPlanResponse:
        plan = self.store.get(request.session_id, request_id)
        updated = apply_decision(plan, request.decision, request.plan_hash, request.note, self._clock())
        self.store.put(request.session_id, updated)
        logger.info(
            "review decision recorded",
            extra={
                "event": "review.decision",
                "request_id": request_id,
                "session_ref": updated.session_ref,
                "decision": request.decision,
                "review_status": updated.status.value,
                "revision": updated.revision,
            },
        )
        return CampaignPlanResponse(plan=updated, report_markdown=render_report(updated))

    async def revise(self, request_id: str, request: RevisionRequest) -> CampaignPlanResponse:
        plan = self.store.get(request.session_id, request_id)
        if plan.status == ReviewStatus.VALIDATION_FAILED:
            raise ReviewConflictError("Plans that failed validation cannot be revised.", code="plan_not_validated")
        if request.base_plan_hash != plan.plan_hash:
            raise ReviewConflictError(
                "The plan changed since it was displayed. Reload the latest revision.", code="stale_plan"
            )
        windows = {window.window_id: window for window in plan.windows}
        unknown = [wid for wid in request.ad_copy if wid not in windows]
        if unknown:
            raise ClearCastError(
                f"Unknown window id(s): {', '.join(unknown)}.", code="invalid_revision", http_status=422
            )
        issues = [issue for wid, lines in request.ad_copy.items() for issue in ad_copy_issues(lines, wid)]
        if issues:
            raise ClearCastError(
                "; ".join(dict.fromkeys(issue.message for issue in issues)), code="invalid_revision", http_status=422
            )

        now = self._clock()
        updated = plan.model_copy(deep=True)
        for window in updated.windows:
            if window.window_id in request.ad_copy:
                window.ad_copy = [line.strip() for line in request.ad_copy[window.window_id]]
        updated.revision += 1
        if plan.status == ReviewStatus.APPROVED:
            updated.review.history.append(
                ReviewEvent(
                    at=now,
                    action="approval_invalidated",
                    from_status=ReviewStatus.APPROVED,
                    to_status=ReviewStatus.PENDING_REVIEW,
                    revision=updated.revision,
                    plan_hash=plan.plan_hash,
                    note="Plan content changed after approval.",
                )
            )
        updated.review.decided_at = None
        updated.review.note = None
        updated.review.approved_plan_hash = None
        finalize(updated, ReviewStatus.PENDING_REVIEW, now)
        updated.review.history.append(
            ReviewEvent(
                at=now,
                action="revised",
                from_status=plan.status,
                to_status=ReviewStatus.PENDING_REVIEW,
                revision=updated.revision,
                plan_hash=updated.plan_hash,
                note=request.note or None,
            )
        )
        self.store.put(request.session_id, updated)
        logger.info(
            "plan revised",
            extra={
                "event": "review.revision",
                "request_id": request_id,
                "session_ref": updated.session_ref,
                "revision": updated.revision,
                "approval_invalidated": plan.status == ReviewStatus.APPROVED,
            },
        )
        return CampaignPlanResponse(plan=updated, report_markdown=render_report(updated))
