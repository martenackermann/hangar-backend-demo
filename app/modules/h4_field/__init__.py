"""H4 Field service slice (see README.md)."""

from __future__ import annotations

from pathlib import Path

from app.core.slices import SliceRegistry

TEMPLATES = Path(__file__).parent / "templates"
TABLES = ("booking_calendars", "appointments", "booking_sync_state", "tours", "tour_stops")
ON_SITE = ["complete_deployment", "reschedule_on_site", "cancel_on_site"]


def register(reg: SliceRegistry) -> None:
    from app.core.demo import table_exporter
    from app.core.enums import DeviationOutcome
    from app.core.events import Transitioned
    from app.core.notifications import NotificationTemplate

    from . import actions, guards, protocol, seed, subscribers, tasks
    from .api import routers
    from .common import (
        GUARD_CALENDAR_ENABLED,
        GUARD_DEVIATION_OUTCOME,
        GUARD_SCANS_COMPLETE,
        GUARD_TOUR_STARTABLE,
        NOT_OWN_TOUR,
    )

    for router in routers:
        reg.include_router(router)

    # --- SR actions (SPEC §4.1.1 #13, #16, #26–#29) ------------------------------------------
    reg.register_action(
        resource="service_requests",
        name="book_appointment",
        payload_model=actions.BookAppointmentBody,
        handler=actions.book_appointment,
    )
    reg.register_action(
        resource="service_requests",
        name="request_reschedule",
        payload_model=actions.RequestRescheduleBody,
        handler=actions.request_reschedule,
    )
    reg.register_action(
        resource="service_requests",
        name="complete_deployment",
        payload_model=actions.CompleteDeploymentBody,
        handler=actions.complete_deployment,
    )
    reg.register_action(
        resource="service_requests",
        name="reschedule_on_site",
        payload_model=actions.OnSiteDeviationBody,
        handler=actions.reschedule_on_site,
    )
    reg.register_action(
        resource="service_requests",
        name="cancel_on_site",
        payload_model=actions.OnSiteDeviationBody,
        handler=actions.cancel_on_site,
    )
    reg.register_action(
        resource="service_requests",
        name="complete_manually",
        handler=actions.complete_manually,
    )

    # --- guards (SPEC_RESOLUTIONS #3, #32, #33) + own-tour authorizer (#2) --------------------
    reg.register_guard(
        "service_request",
        "book_appointment",
        guards.calendar_enabled,
        code=GUARD_CALENDAR_ENABLED,
        needs_payload=True,
    )
    reg.register_guard("service_request", ON_SITE, guards.tour_startable, code=GUARD_TOUR_STARTABLE)
    outcomes = {
        "complete_deployment": DeviationOutcome.COMPLETE,
        "reschedule_on_site": DeviationOutcome.RESCHEDULE,
        "cancel_on_site": DeviationOutcome.CANCEL,
    }
    for action, outcome in outcomes.items():
        reg.register_guard(
            "service_request",
            action,
            guards.deviation_outcome(outcome),
            code=GUARD_DEVIATION_OUTCOME,
            needs_payload=True,
        )
    reg.register_guard(
        "service_request",
        "complete_deployment",
        guards.deployment_scans_complete,
        code=GUARD_SCANS_COMPLETE,
    )
    reg.register_authorizer(
        "service_requests",
        ON_SITE,
        guards.own_tour,
        code=NOT_OWN_TOUR,
        message="This stop belongs to another technician's tour.",
    )

    # --- events -------------------------------------------------------------------------------
    reg.subscribe(Transitioned, subscribers.on_sr_transition, phase="in_transaction")

    # --- mail ---------------------------------------------------------------------------------
    reg.register_template(
        NotificationTemplate(
            id="sr.protocol",
            owner="h4",
            name="Handover protocol",
            subject="Handover protocol for {{ sr_number }}",
            html=TEMPLATES / "sr.protocol.html.j2",
            text=TEMPLATES / "sr.protocol.txt.j2",
            description="Summary of delivered and collected devices with the protocol PDF.",
            trigger="complete_deployment (sendProtocol) or protocol re-send",
            recipients="protocolEmail (default: contact)",
            entity_type="service_request",
            sample_context=dict(protocol.PROTOCOL_SAMPLE),
            build_context=protocol.protocol_context,
        )
    )
    reg.register_template(
        NotificationTemplate(
            id="tour.assigned",
            owner="h4",
            name="Tour assigned",
            subject=(
                "Tour {{ calendar_name }} on {{ tour_date | date }} assigned to you "
                "({{ stop_count }} stops)"
            ),
            html=TEMPLATES / "tour.assigned.html.j2",
            text=TEMPLATES / "tour.assigned.txt.j2",
            description="Sent to the technician when a tour is created with or assigned to them.",
            trigger="tour created with technician / assign_technician",
            recipients="Technician",
            entity_type="tour",
            sample_context={
                "technician_first_name": "Theo",
                "calendar_name": "Riverton route",
                "tour_date": "2026-10-13",
                "stop_count": 2,
                "stops": [
                    {"time": "2026-10-13T06:00:00Z", "sr_number": "SR0012101", "town": "Riverton"},
                    {"time": "2026-10-13T07:30:00Z", "sr_number": "SR0012102", "town": "Riverton"},
                ],
                "my_tour_path": "/field/my-tour?date=2026-10-13",
            },
        )
    )

    # --- jobs ---------------------------------------------------------------------------------
    reg.register_task(
        tasks.sync_appointments_task,
        name=tasks.TASK_NAME,
        queue="sync",
        lock=tasks.TASK_NAME,
        periodic=tasks.PERIODIC_CRON,
    )

    # --- seed ---------------------------------------------------------------------------------
    reg.register_seeder(40, seed.seed_field)
    reg.register_seed_exporter(table_exporter(*TABLES))
    reg.register_demo_truncate(
        ["tour_stops", "tours", "appointments", "booking_sync_state", "booking_calendars"]
    )
