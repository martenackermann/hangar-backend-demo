"""H3 Staging & Fulfilment slice. Owner of everything listed in README.md."""

from __future__ import annotations

from pathlib import Path

from app.core.slices import SliceRegistry

TEMPLATES = Path(__file__).parent / "templates"

#: SR actions owned by H3 (SPEC §2.5). ``ship`` is registered so it shows up in
#: ``availableActions``; the core map marks it ``via_endpoint=False``, so the generic endpoint
#: answers 404 and the work happens in ``POST /service-requests/{id}/shipments`` (#15).
SR_ACTIONS = (
    "claim",
    "release",
    "finish_personalization",
    "skip_preparation",
    "reopen_personalization",
    "close_order",
    "mark_provided",
    "close_without_label",
    "ship",
)


def register_actions(reg: SliceRegistry) -> None:
    """Pure transitions: permission, bulk flag and ``requiresComment`` come from the core map;
    the effects run in :mod:`.effects` (one in-transaction subscriber)."""
    for name in SR_ACTIONS:
        reg.register_action(resource="service_requests", name=name)


def register(reg: SliceRegistry) -> None:
    from app.core.demo import table_exporter
    from app.core.events import Transitioned
    from app.core.notifications import NotificationTemplate

    from . import api, effects, search, seed
    from .services.shipping import TEMPLATE_ID, preview_context

    for router in api.routers:
        reg.include_router(router)

    register_actions(reg)
    reg.subscribe(Transitioned, effects.on_transition, phase="in_transaction")

    reg.register_template(
        NotificationTemplate(
            id=TEMPLATE_ID,
            owner="h3",
            name="Shipping confirmation",
            subject="Your hardware for {{ sr_number }} is on its way",
            html=TEMPLATES / f"{TEMPLATE_ID}.html.j2",
            text=TEMPLATES / f"{TEMPLATE_ID}.txt.j2",
            description=(
                "Sent to the contact when a parcel shipment is created with "
                "'Send shipping confirmation': tracking links, delivery or pickup-point "
                "address, return label instructions and the appointment date."
            ),
            trigger="ship (sendConfirmation)",
            recipients="Contact",
            entity_type="service_request",
            sample_context={
                "sr_number": "SR0012345",
                "gender": "female",
                "first_name": "Mia",
                "last_name": "Turner",
                "tracking_urls": ["https://track.example.com/PC20261013000042-1"],
                "return_tracking_urls": ["https://track.example.com/return/PC20261013000042-2R"],
                "pickup_point": True,
                "recipient_name": "Northport Office",
                "name2": "Attn: Mia Turner",
                "street": "4 Quay Street",
                "zip": "40100",
                "city": "Northport",
                "country": "Freeland",
                "appointment_start_at": "2026-10-15T07:30:00Z",
                "carrier_shipment_id": "PC20261013000042",
                "items": [{"name": "Aero 14 G3 Laptop", "tag": "GX204811"}],
            },
            build_context=preview_context,
        )
    )

    reg.register_search_provider(search.provider)

    reg.register_seeder(30, seed.seed_staging)
    reg.register_seed_exporter(table_exporter("shipments"))
    reg.register_demo_truncate(["shipments"])
