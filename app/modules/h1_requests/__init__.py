"""H1 Requests & Comms slice (see README.md).

Owns the service request / lease return lists, details, edits, histories and exports,
contacts, the pipeline summary, the communication chain (dispatch settings, daily
``comms.dispatch`` job, dispatch runs, mail log, notification templates) and the
SR/lease comms actions of SPEC §2.5.
"""

from __future__ import annotations

from app.core.slices import SliceRegistry

#: SR actions without a handler (pure transitions; effects in ``subscribers``)
SR_ACTIONS = (
    "send_invitation",
    "send_campus_info",
    "send_reminder_1",
    "send_reminder_2",
    "escalate",
    "send_next_mail",
    "mark_hardware_unavailable",
    "release_hardware_hold",
    "release_vip",
    "cancel",
    "reopen",
)
LEASE_ACTIONS = (
    "send_info",
    "send_reminder_1",
    "send_reminder_2",
    "escalate",
    "send_next_mail",
    "release_vip",
    "close",
    "reopen",
)


def register_actions(reg: SliceRegistry) -> None:
    """Actions on the generic endpoints (idempotent; tests re-run it)."""
    from . import actions

    for name in SR_ACTIONS:
        reg.register_action(resource="service_requests", name=name)
    for name in LEASE_ACTIONS:
        reg.register_action(resource="lease_returns", name=name)
    for resource in ("service_requests", "lease_returns"):
        reg.register_action(
            resource=resource,
            name="pause_communication",
            payload_model=actions.PauseBody,
            handler=actions.pause_communication,
        )
        reg.register_action(
            resource=resource,
            name="resume_communication",
            handler=actions.resume_communication,
        )
    reg.register_action(
        resource="lease_returns",
        name="link_service_request",
        payload_model=actions.LinkServiceRequestBody,
        handler=actions.link_service_request,
    )
    reg.register_action(
        resource="lease_returns",
        name="unlink_service_request",
        handler=actions.unlink_service_request,
    )


def register_authorizers(reg: SliceRegistry) -> None:
    """The first mail has two variants; the one that does not apply is hidden instead of
    being shown as permanently blocked (calling it anyway → 403 is_campus / not_campus)."""
    from . import guards

    reg.register_authorizer(
        "service_requests",
        "send_campus_info",
        guards.campus_request,
        code="is_campus",
        message="Only campus requests get the campus info mail.",
    )
    reg.register_authorizer(
        "service_requests",
        "send_invitation",
        guards.standard_request,
        code="not_campus",
        message="Campus users get the campus info mail instead of a booking link.",
    )


def register(reg: SliceRegistry) -> None:
    from app.core.enums import Trigger
    from app.core.events import (
        NotificationFailed,
        NotificationQueued,
        NotificationSent,
        Transitioned,
    )

    from . import dispatch, guards, mails, seed, subscribers
    from .api import ROUTERS
    from .chain import LEASE_CHAIN_ACTIONS, SR_CHAIN_ACTIONS

    for router in ROUTERS:
        reg.include_router(router)

    register_actions(reg)
    register_authorizers(reg)

    job = [Trigger.JOB]
    # evaluated in registration order: the window check first, so its code is reported
    reg.register_guard(
        "lease_return",
        "send_info",
        guards.retirement_window,
        code="retirement_window",
        triggers=job,
    )
    for entity_type, chain in (
        ("service_request", SR_CHAIN_ACTIONS),
        ("lease_return", LEASE_CHAIN_ACTIONS),
    ):
        reg.register_guard(entity_type, chain, guards.chain_due, code="chain_due", triggers=job)
        reg.register_guard(entity_type, chain, guards.not_paused, code="not_paused", triggers=job)

    reg.subscribe(Transitioned, subscribers.on_transition, phase="in_transaction")
    reg.subscribe(NotificationQueued, subscribers.on_notification_queued, phase="in_transaction")
    reg.subscribe(NotificationSent, subscribers.on_notification_sent, phase="in_transaction")
    reg.subscribe(NotificationFailed, subscribers.on_notification_failed, phase="in_transaction")

    for template in mails.notification_templates():
        reg.register_template(template)

    # hourly at :00 UTC; proceeds at 07:xx business time once per day (DST-safe, SPEC §9)
    reg.register_task(
        dispatch.dispatch_task,
        name=dispatch.TASK_NAME,
        queue="default",
        lock="comms.dispatch",
        periodic="0 * * * *",
    )

    reg.register_seeder(10, seed.seed_comms)
    reg.register_seed_exporter(seed.export_comms)
    reg.register_demo_truncate(["mail_log", "dispatch_runs", "dispatch_settings"])
