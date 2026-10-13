"""Daily communication dispatch (SPEC §6.3 algorithm, §9 ``comms.dispatch``; #10).

``execute`` is the testable core: it takes a session, selects candidates per chain step
(status ∧ due ∧ not paused ∧ master switch, ordered by id, pages of 200), processes the
steps in reverse order (leases before SRs) and applies each transition in its own
transaction (``commit`` per entity; a guard failure rolls only that entity back). A dry run
evaluates the same selection and guards without writing and returns ``planned``.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import commit, open_session
from app.core.enums import JobKind, JobStatus, Trigger
from app.core.errors import ApiError, GuardFailed
from app.core.jobs import create_job, tracked
from app.core.loading import ensure_loaded
from app.core.models import LeaseReturn, ServiceRequest, User
from app.core.statemachine import GuardFailure, guards
from app.core.timeutil import business_hour, business_today, now_utc, to_business_date
from app.core.transitions import transition_service

from .chain import (
    DISPATCH_HOUR,
    DISPATCH_ORDER,
    ChainStep,
    candidates_stmt,
    load_settings,
    master_switch,
    sr_first_action,
    template_for,
)
from .models import DispatchRun, DispatchSettings

log = structlog.get_logger(__name__)

PAGE_SIZE = 200
TASK_NAME = "comms.dispatch"
#: a queued/running real run younger than this blocks "Run now" (409)
IN_PROGRESS_WINDOW = timedelta(minutes=30)

type Progress = Callable[[int, int | None, str | None], Awaitable[None]]


@dataclass
class Outcome:
    counts: Counter[str] = field(default_factory=Counter)
    skipped: Counter[str] = field(default_factory=Counter)
    errors: list[dict[str, Any]] = field(default_factory=list)
    planned: list[dict[str, Any]] | None = None

    @property
    def sent(self) -> int:
        return sum(self.counts.values())


def snapshot(settings: DispatchSettings) -> DispatchSettings:
    """Detached copy (rollbacks expire ORM rows; the run keeps its settings)."""
    columns = [c.key for c in DispatchSettings.__table__.columns]
    return DispatchSettings(**{name: getattr(settings, name) for name in columns})


def _model(step: ChainStep) -> Any:
    return ServiceRequest if step.entity_type == "service_request" else LeaseReturn


def _action(step: ChainStep, entity: Any) -> str:
    if step.entity_type == "service_request" and step.action == "send_invitation":
        return sr_first_action(entity)
    return step.action


def _label(entity: Any) -> str:
    return entity.number if isinstance(entity, ServiceRequest) else entity.asset_tag


def _template(step: ChainStep, entity: Any, action: str) -> str:
    template = template_for(step.entity_type, action, is_campus=bool(entity.is_campus))
    return template or action


async def evaluate_guards(session: AsyncSession, entity: Any, action: str) -> GuardFailure | None:
    """Job-trigger guard evaluation without side effects (dry runs)."""
    await ensure_loaded(session, entity)
    transition = transition_service.candidate(entity, action, None, Trigger.JOB)
    if transition is None:
        return GuardFailure("transition_not_allowed", "Status changed in the meantime.")
    entity_type = "service_request" if isinstance(entity, ServiceRequest) else "lease_return"
    for guard in guards.for_action(entity_type, transition.action):
        if not guard.applies(Trigger.JOB, evaluating_availability=False):
            continue
        failure = await guard.fn(session, entity, None, None)
        if failure is not None:
            return failure
    return None


async def count_candidates(session: AsyncSession, settings: DispatchSettings) -> int:
    today = business_today()
    total = 0
    for step in DISPATCH_ORDER:
        if not master_switch(settings, step.entity_type):
            continue
        stmt = candidates_stmt(step, settings, today, after_id=0, limit=1_000_000)
        total += int(
            (await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
        )
    return total


async def execute(
    session: AsyncSession,
    *,
    dry_run: bool,
    progress: Progress | None = None,
) -> Outcome:
    settings = snapshot(await load_settings(session))
    today = business_today()
    budget = settings.max_mails_per_run
    outcome = Outcome(planned=[] if dry_run else None)
    total = await count_candidates(session, settings) if progress else None
    processed = 0

    for step in DISPATCH_ORDER:
        if not master_switch(settings, step.entity_type):
            continue
        model = _model(step)
        after_id = 0
        while True:
            stmt = candidates_stmt(step, settings, today, after_id=after_id, limit=PAGE_SIZE)
            ids = list((await session.scalars(stmt.with_only_columns(model.id))).all())
            if not ids:
                break
            after_id = ids[-1]
            for entity_id in ids:
                processed += 1
                if budget <= 0:
                    outcome.skipped["cap_reached"] += 1
                    continue
                entity = await session.get(model, entity_id, populate_existing=True)
                if entity is None:  # pragma: no cover - deleted concurrently
                    continue
                if dry_run:
                    budget -= await _plan(session, step, entity, outcome)
                else:
                    budget -= await _apply(session, step, entity, outcome)
                if progress and processed % 25 == 0:
                    await progress(processed, total, f"{outcome.sent} mails sent")
    if progress:
        await progress(processed, total, f"{outcome.sent} mails sent")
    return outcome


async def _plan(session: AsyncSession, step: ChainStep, entity: Any, outcome: Outcome) -> int:
    action = _action(step, entity)
    failure = await evaluate_guards(session, entity, action)
    if failure is not None:
        outcome.skipped[failure.code] += 1
        return 0
    template = _template(step, entity, action)
    outcome.counts[template] += 1
    email = entity.contact.email if entity.contact else None
    assert outcome.planned is not None
    outcome.planned.append(
        {
            "entityType": step.entity_type,
            "entityId": entity.id,
            "label": _label(entity),
            "action": action,
            "templateId": template,
            "to": [email] if email else [],
        }
    )
    return 1


async def _apply(session: AsyncSession, step: ChainStep, entity: Any, outcome: Outcome) -> int:
    action = _action(step, entity)
    template = _template(step, entity, action)
    entity_id, label = entity.id, _label(entity)
    try:
        await transition_service.apply(session, entity, action, actor=None, trigger=Trigger.JOB)
        await commit(session)
    except GuardFailed as exc:
        await session.rollback()
        outcome.skipped[exc.guard_code] += 1
        return 0
    except Exception as exc:  # noqa: BLE001 - SPEC §6.3: record and continue
        await session.rollback()
        message = exc.detail if isinstance(exc, ApiError) and exc.detail else str(exc)
        log.warning("dispatch_entity_failed", entity_type=step.entity_type, entity_id=entity_id)
        outcome.errors.append(
            {
                "entityType": step.entity_type,
                "entityId": entity_id,
                "label": label,
                "action": action,
                "message": (message or type(exc).__name__)[:500],
            }
        )
        return 0
    outcome.counts[template] += 1
    return 1


def store_outcome(run: DispatchRun, outcome: Outcome, status: JobStatus) -> None:
    run.counts = dict(outcome.counts)
    run.skipped = dict(outcome.skipped)
    run.errors = list(outcome.errors)
    run.planned = outcome.planned
    run.status = status
    run.finished_at = now_utc()


def job_result(run_id: int, outcome: Outcome) -> dict[str, Any]:
    return {
        "kind": "comms_dispatch",
        "dispatchRunId": run_id,
        "counts": dict(outcome.counts),
        "skipped": dict(outcome.skipped),
        "errorCount": len(outcome.errors),
    }


# --- runs ----------------------------------------------------------------------------------------


async def dry_run(session: AsyncSession, actor: User | None) -> DispatchRun:
    """Synchronous dry run, stored with ``dry_run = true`` (#10). Caller commits."""
    run = DispatchRun(
        trigger=Trigger.USER if actor else Trigger.JOB,
        dry_run=True,
        status=JobStatus.RUNNING,
        started_at=now_utc(),
        triggered_by=actor.id if actor else None,
        counts={},
        skipped={},
        errors=[],
    )
    session.add(run)
    await session.flush()
    outcome = await execute(session, dry_run=True)
    store_outcome(run, outcome, JobStatus.SUCCEEDED)
    await session.flush()
    return run


async def run_in_progress(session: AsyncSession) -> DispatchRun | None:
    return (
        await session.scalars(
            select(DispatchRun)
            .where(
                DispatchRun.dry_run.is_(False),
                DispatchRun.status.in_((JobStatus.QUEUED, JobStatus.RUNNING)),
                DispatchRun.started_at > now_utc() - IN_PROGRESS_WINDOW,
            )
            .order_by(DispatchRun.id.desc())
        )
    ).first()


async def scheduled_run_today(session: AsyncSession) -> bool:
    rows = (
        await session.scalars(
            select(DispatchRun.started_at).where(
                DispatchRun.trigger == Trigger.JOB,
                DispatchRun.dry_run.is_(False),
                DispatchRun.status.in_((JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.SUCCEEDED)),
                DispatchRun.started_at > now_utc() - timedelta(days=2),
            )
        )
    ).all()
    today = business_today()
    return any(to_business_date(started) == today for started in rows)


async def perform_run(job_id: int, dispatch_run_id: int) -> None:
    """Real run inside its ``background_jobs`` row (progress, result, failure)."""
    async with tracked(job_id) as job:
        async with open_session() as session:
            run = await session.get(DispatchRun, dispatch_run_id)
            if run is None:
                raise LookupError(f"dispatch run {dispatch_run_id} not found")
            run.status = JobStatus.RUNNING
            run.started_at = now_utc()
            await commit(session)
            try:
                outcome = await execute(session, dry_run=False, progress=job.progress)
            except Exception:
                await session.rollback()
                failed = await session.get(DispatchRun, dispatch_run_id, populate_existing=True)
                if failed is not None:
                    store_outcome(failed, Outcome(), JobStatus.FAILED)
                    await commit(session)
                raise
            run = await session.get(DispatchRun, dispatch_run_id, populate_existing=True)
            assert run is not None
            store_outcome(run, outcome, JobStatus.SUCCEEDED)
            await commit(session)
        job.result = job_result(dispatch_run_id, outcome)
        job.message = f"{outcome.sent} mails sent"
    log.info("comms_dispatch_done", dispatch_run_id=dispatch_run_id, sent=outcome.sent)


async def dispatch_task(
    timestamp: int | None = None, job_id: int | None = None, dispatch_run_id: int | None = None
) -> None:
    """Task ``comms.dispatch``: hourly periodic (proceeds at 07:xx business time once per day,
    DST-safe, SPEC §9) or deferred by ``POST /comms/dispatch-runs`` with its ids."""
    if job_id is None or dispatch_run_id is None:
        if business_hour() != DISPATCH_HOUR:
            return
        async with open_session() as session:
            if await scheduled_run_today(session):
                return
            job = await create_job(session, JobKind.COMMS_DISPATCH, None)
            run = DispatchRun(
                background_job_id=job.id,
                trigger=Trigger.JOB,
                dry_run=False,
                status=JobStatus.QUEUED,
                started_at=now_utc(),
                counts={},
                skipped={},
                errors=[],
            )
            session.add(run)
            await session.flush()
            job_id, dispatch_run_id = job.id, run.id
            await commit(session)
    await perform_run(job_id, dispatch_run_id)
