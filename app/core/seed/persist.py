"""Persisting the planned core story: contacts, VIPs, SRs + items, assets + movements,
status transitions, lease returns + comments (see ``story.py`` for the planning)."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any

from app.core.constants import SEED_INVENTORY_IDS, calendar_for_region
from app.core.enums import (
    DEMAND_STATUSES,
    AssetAction,
    AssetSource,
    AssetStatus,
    Gender,
    HoldSource,
    ItemSlot,
    LeaseReturnStatus,
    OsReinstallTiming,
    ProcurementType,
    ServiceRequestStatus,
    Trigger,
)
from app.core.models import (
    Article,
    Asset,
    AssetMovement,
    Contact,
    LeaseReturn,
    LeaseReturnComment,
    ServiceRequest,
    ServiceRequestItem,
    StatusTransition,
    VipEntry,
)
from app.core.region_resolver import resolve_region
from app.core.seed.reference import RegionDef, buffer_for
from app.core.seed.vocabulary import DEPARTMENTS, FIRST_NAMES, LAST_NAMES, SOFTWARE, STREETS
from app.core.timeutil import to_business_date

if TYPE_CHECKING:
    from app.core.seed.story import Plan, Story

SR = ServiceRequestStatus
LR = LeaseReturnStatus
A = AssetStatus
S = ItemSlot
J, U, IM, Y, SY = Trigger.JOB, Trigger.USER, Trigger.IMPORT, Trigger.SYSTEM, Trigger.SYNC
DANA, ADA = "dana.dispatcher@example.com", "ada.admin@example.com"

OLD_BASE_MODEL = "KD-AERO14-G2"
OLD_PERIPHERAL_MODEL = "KD-DOCK-U3"
UNRESOLVED_CITY = "Greyhaven"
VIP_TITLES = (
    ("Board", "Chair of the Board"),
    ("Board", "Board Member"),
    ("Board", "Board Member"),
    ("Executive", "Chief Operating Officer"),
    ("Executive", "Chief Financial Officer"),
    ("Executive", "Head of Fleet Operations"),
    ("Assistant", "Executive Assistant"),
    ("Assistant", "Executive Assistant"),
)
COMMENTS = (
    "Called the user, replacement will be ordered this week.",
    "User is on leave until next month.",
    "Cost-centre manager informed about the lease end.",
    "User asked for a desk visit instead of the service point.",
    "Device is used as a spare; checking with the team lead.",
    "Reminder sent manually by phone.",
)


@dataclass
class AssetSpec:
    asset: Asset
    events: list[dict[str, Any]]


class Persister:
    def __init__(self, story: Story) -> None:
        self.story = story
        self.session = story.session
        self.rng = story.rng
        self.now = story.now
        self.today = story.today
        self.used_names: set[tuple[str, str]] = set()
        self.used_emails: set[str] = set()
        self.assets: list[AssetSpec] = []
        self.transitions: list[StatusTransition] = []
        self.srs: dict[int, ServiceRequest] = {}  # plan index -> SR
        self.contacts: dict[int, Contact] = {}  # plan index -> contact
        self.old_assets: dict[tuple[int, ItemSlot], AssetSpec] = {}
        self.vip_contacts: list[Contact] = []

    # --- primitives ---------------------------------------------------------------------------

    def loc(self, code: str) -> int:
        return self.story.locations[code].id

    def uid(self, email: str | None) -> int | None:
        return self.story.uid(email)

    def contact(
        self, index: int, region: RegionDef | None, *, campus: str | None = None, email: bool = True
    ) -> Contact:
        rng = self.rng
        while True:
            first, last = rng.choice(FIRST_NAMES), rng.choice(LAST_NAMES)
            if (first, last) not in self.used_names:
                self.used_names.add((first, last))
                break
        address = rng.randint(1, 120)
        if campus == "zip":
            street, zip_, city = "1 Campus Way", "10500", "Riverton"
        elif region is not None:
            while True:
                zip_ = f"{region.zip_prefix}{rng.randint(0, 999):03d}"
                if zip_ != "10500":
                    break
            street, city = f"{address} {rng.choice(STREETS)}", region.name
        else:
            street, zip_, city = (
                f"{address} {rng.choice(STREETS)}",
                f"99{rng.randint(100, 999)}",
                UNRESOLVED_CITY,
            )
        mail = None
        if email:
            base = f"{first.lower()}.{last.lower()}"
            mail, n = f"{base}@example.com", 1
            while mail in self.used_emails:
                n += 1
                mail = f"{base}{n}@example.com"
            self.used_emails.add(mail)
        gender = rng.choices(list(Gender), [45, 45, 5, 5])[0]
        return Contact(
            user_id=f"E{100001 + index:06d}",
            gender=gender,
            first_name=first,
            last_name=last,
            email=mail,
            phone=f"+00 555 0{rng.randint(100, 999)}",
            mobile=f"+00 555 1{rng.randint(100, 999)}" if rng.random() < 0.5 else None,
            department=rng.choice(DEPARTMENTS),
            street=street,
            zip=zip_,
            city=city,
            building=f"B{rng.randint(1, 6)}",
            room=f"{rng.randint(1, 5)}.{rng.randint(1, 40):02d}",
        )

    def new_asset(
        self,
        article: Article | None,
        *,
        status: AssetStatus,
        location: str,
        tag_kind: str = "new",
        model: str | None = None,
        place: str | None = None,
        sr_id: int | None = None,
        contact_id: int | None = None,
        checked_in: datetime | None = None,
        tag: str | None = None,
    ) -> AssetSpec:
        model_number = article.model_number if article else (model or "UNKNOWN")
        prefix = model_number.split("-")[0]
        is_mac = article is not None and article.category in ("laptop", "docking_station")
        asset = Asset(
            serial_number=self.story.serial(prefix),
            asset_tag=tag or self.story.tag(tag_kind),
            article_id=article.id if article else None,
            model=model_number,
            external_asset_id=f"AST{self.rng.randint(100000, 999999)}",
            ci_name=f"CI-{self.rng.randint(10000, 99999)}",
            order_number=f"PO-{self.rng.randint(1000, 9999)}" if tag_kind == "new" else None,
            mac_address="02:00:5E:" + ":".join(f"{self.rng.randint(0, 255):02X}" for _ in range(3))
            if is_mac
            else None,
            status=status,
            location_id=self.loc(location),
            installation_place=place,
            service_request_id=sr_id,
            assigned_contact_id=contact_id,
            checked_in_at=checked_in,
            source=AssetSource.SEED,
        )
        spec = AssetSpec(asset, [])
        self.assets.append(spec)
        return spec

    def move(
        self,
        spec: AssetSpec,
        action: AssetAction,
        frm: tuple[AssetStatus | None, str | None, str | None],
        to: tuple[AssetStatus, str, str | None],
        at: datetime,
        trigger: Trigger,
        *,
        actor: str | None = None,
        sr_id: int | None = None,
        reason: str | None = None,
        source_ref: str | None = None,
    ) -> None:
        spec.events.append(
            {
                "action": action,
                "from_status": frm[0],
                "from_location_id": self.loc(frm[1]) if frm[1] else None,
                "from_installation_place": frm[2],
                "to_status": to[0],
                "to_location_id": self.loc(to[1]),
                "to_installation_place": to[2],
                "service_request_id": sr_id,
                "actor_user_id": self.uid(actor),
                "trigger": trigger,
                "reason": reason,
                "source_ref": source_ref,
                "occurred_at": at,
            }
        )
        spec.asset.status_changed_at = at

    # --- contacts, VIPs, SRs -------------------------------------------------------------------

    async def service_requests(self) -> None:
        story = self.story
        for i, plan in enumerate(story.plans):
            contact = self.contact(
                i, plan.region, campus=plan.campus, email="no_email" not in plan.flags
            )
            self.session.add(contact)
            self.contacts[plan.index] = contact
            if "vip" in plan.flags:
                self.vip_contacts.append(contact)
        await self.session.flush()
        for i, plan in enumerate(story.plans):
            sr = await self._service_request(i, plan, self.contacts[plan.index])
            self.session.add(sr)
            self.srs[plan.index] = sr
        await self.session.flush()

    async def _service_request(self, i: int, plan: Plan, contact: Contact) -> ServiceRequest:
        rng = self.rng
        events = plan.events
        last = events[-1]
        imported_at = events[0].at
        cost_center = "CC-9100" if plan.campus == "cc" else f"CC-{rng.randint(1000, 8999)}"
        requested = rng.sample(SOFTWARE, rng.randint(0, 3))
        staged = plan.status in {
            SR.PERSONALIZATION_DONE,
            SR.READY_FOR_PICKING,
            SR.SHIPPED,
            SR.PROVIDED,
            SR.COMPLETED,
        } or (
            plan.status in (SR.RESCHEDULE_NEEDED, SR.CANCELLED)
            and plan.event_time("finish_personalization") is not None
        )
        installed = list(requested)
        if "software_open" in plan.flags and requested:
            installed = requested[:-1]
        elif "software_open" in plan.flags:
            requested = ["VPN Client"]
            installed = []
        if not staged:
            installed = (
                requested[: len(requested) // 2]
                if plan.status == SR.PERSONALIZATION_IN_PROGRESS
                else []
            )
        itsm = "Open"
        if plan.status in {
            SR.APPOINTMENT_BOOKED,
            SR.PERSONALIZATION_IN_PROGRESS,
            SR.PERSONALIZATION_DONE,
            SR.READY_FOR_PICKING,
            SR.SHIPPED,
            SR.PROVIDED,
            SR.RESCHEDULE_NEEDED,
        }:
            itsm = "Work in Progress"
        elif plan.status == SR.COMPLETED:
            itsm = "Closed Complete"
        elif "cancel_itsm" in plan.flags:
            itsm = "Cancelled"
        elif plan.status == SR.IMPORTED:
            itsm = "New"
        elif plan.status == SR.HARDWARE_UNAVAILABLE:
            itsm = "On Hold"
        mail_events = [
            e
            for e in events
            if e.action
            in (
                "send_invitation",
                "send_campus_info",
                "send_reminder_1",
                "send_reminder_2",
                "escalate",
            )
        ]
        comm_date = to_business_date(mail_events[-1].at) if mail_events else None
        procurement = plan.procurement
        sr = ServiceRequest(
            number=f"SR{12001 + i:07d}",
            task_number=f"TASK{45001 + i:07d}",
            itsm_state=itsm,
            assignment_group="Rollout Hardware Hold"
            if "hold_import" in plan.flags
            else "Workplace Rollout",
            opened_at=imported_at - timedelta(hours=rng.randint(1, 20)),
            catalog_item={
                ProcurementType.REPLACEMENT: "Workplace hardware replacement",
                ProcurementType.NEW_HIRE: "New hire workplace",
                ProcurementType.ADDITIONAL_DEVICE: "Additional device",
                ProcurementType.REMOVAL: "Hardware removal",
            }[procurement],
            short_description={
                ProcurementType.REPLACEMENT: "Replace laptop and accessories",
                ProcurementType.NEW_HIRE: "Equip new team member",
                ProcurementType.ADDITIONAL_DEVICE: "Additional device for existing workplace",
                ProcurementType.REMOVAL: "Collect hardware no longer needed",
            }[procurement],
            contact=contact,
            contact_id=contact.id,
            cost_center=cost_center,
            procurement_type=procurement,
            scope_of_delivery=", ".join(t.split(" ")[0] for t in plan.new.values())
            or "Collection only",
            client_component="Standard laptop" if procurement != ProcurementType.REMOVAL else None,
            requested_software=requested,
            added_software=[],
            installed_software=installed,
            note="Please call before arrival" if rng.random() < 0.25 else None,
            onsite_contact_phone=f"+00 555 0{rng.randint(100, 999)}"
            if rng.random() < 0.6
            else None,
            delivery_room=contact.room,
            delivery_building=contact.building,
            delivery_street=contact.street,
            delivery_zip=contact.zip,
            delivery_city=contact.city,
            delivery_country="Freeland",
            is_vip="vip" in plan.flags,
            status=plan.status,
            status_changed_at=last.at,
            resume_status=plan.resume,
            communication_date=comm_date,
            imported_at=imported_at,
            last_imported_at=imported_at,
            os_reinstall=(
                rng.choice(list(OsReinstallTiming)) if staged and rng.random() < 0.3 else None
            ),
        )
        resolution = await resolve_region(
            self.session,
            zip=sr.delivery_zip,
            city=sr.delivery_city,
            cost_center=sr.cost_center,
            procurement_type=procurement,
        )
        if resolution is not None:
            sr.region_id = resolution.region_id
            sr.region_rule_id = resolution.region_rule_id
            sr.supplying_site_id = resolution.supplying_site_id
            sr.delivery_type = resolution.delivery_type
            sr.is_campus = resolution.is_campus
        pause = next((e for e in events if e.action == "pause_communication"), None)
        if pause is not None:
            assert pause.payload is not None
            sr.communication_pause_reason = pause.payload["reason"]
            if "resumeDate" in pause.payload:
                sr.communication_resume_date = date.fromisoformat(pause.payload["resumeDate"])
            else:
                sr.communication_paused = True
        hold = plan.event_time("mark_hardware_unavailable")
        if hold is not None:
            sr.hardware_unavailable_notified_at = hold + timedelta(minutes=1)
            sr.hold_source = HoldSource.IMPORT if "hold_import" in plan.flags else HoldSource.MANUAL
        if plan.status == SR.PERSONALIZATION_IN_PROGRESS:
            claim = plan.event_time("claim")
            sr.editor_user_id = self.uid(plan.editor)
            sr.claimed_at = claim
        if plan.appt is not None:
            calendar = calendar_for_region(plan.region.code if plan.region else "riverton")
            sr.appointment_start_at = plan.appt
            sr.appointment_end_at = plan.appt + timedelta(minutes=60)
            sr.appointment_calendar_id = calendar.id
            sr.appointment_calendar_name = calendar.name
        if plan.deviation is not None:
            sr.deviation_code_id, sr.deviation_comment = plan.deviation
        sr.personalized_at = plan.event_time("finish_personalization")
        sr.order_closed_at = plan.event_time("close_order")
        sr.shipped_at = plan.event_time("ship")
        sr.provided_at = plan.event_time("mark_provided")
        sr.completed_at = plan.event_time("complete_deployment") or plan.event_time(
            "complete_manually"
        )
        sr.cancelled_at = plan.event_time("cancel") or plan.event_time("cancel_on_site")
        articles = self.story.articles
        for slot, requested_type in plan.new.items():
            article = articles.get(requested_type)
            sr.items.append(
                ServiceRequestItem(
                    slot=slot,
                    requested_type=requested_type,
                    article_id=article.id if article else None,
                )
            )
        for slot in plan.old:
            sr.items.append(ServiceRequestItem(slot=slot, expected_asset_tag=self.story.tag("old")))
        return sr

    async def vip_entries(self) -> list[Contact]:
        """6 SR VIPs + 2 lease-only VIPs (created by ``lease_returns``)."""
        return self.vip_contacts

    # --- assets --------------------------------------------------------------------------------

    def sr_assets(self) -> None:
        story = self.story
        for plan in story.plans:
            sr = self.srs[plan.index]
            contact = self.contacts[plan.index]
            for item in sr.items:
                if item.slot in (S.OLD_BASE, S.OLD_PERIPHERAL_1, S.OLD_PERIPHERAL_2):
                    self._old_asset(plan, sr, contact, item)
                else:
                    self._new_asset(plan, sr, contact, item)

    def _old_asset(
        self, plan: Plan, sr: ServiceRequest, contact: Contact, item: ServiceRequestItem
    ) -> None:
        model = OLD_BASE_MODEL if item.slot == S.OLD_BASE else OLD_PERIPHERAL_MODEL
        article = self.story.article_by_model[model]
        spec = self.new_asset(
            article,
            status=A.IN_USE,
            location="IN_USE",
            tag_kind="old",
            contact_id=contact.id,
            checked_in=sr.imported_at - timedelta(days=900 + self.rng.randint(0, 300)),
            tag=item.expected_asset_tag,
        )
        created = sr.imported_at - timedelta(days=30)
        self.move(
            spec,
            AssetAction.SYNC_IN_USE,
            (None, None, None),
            (A.IN_USE, "IN_USE", None),
            created,
            IM,
        )
        done = plan.event_time("complete_deployment")
        if plan.status == SR.COMPLETED and done is not None and "dv10" not in plan.flags:
            hub = "CMP" if sr.is_campus else (plan.hub or "RIV")
            spec.asset.status, spec.asset.location_id = A.COLLECTED, self.loc(f"{hub}-RETURN")
            spec.asset.service_request_id = sr.id
            self.move(
                spec,
                AssetAction.COLLECT,
                (A.IN_USE, "IN_USE", None),
                (A.COLLECTED, f"{hub}-RETURN", None),
                done,
                Y,
                sr_id=sr.id,
            )
            item.verified_at = done - timedelta(minutes=20)
            item.verified_by = self.uid(plan.technician)
            item.verified_serial = spec.asset.serial_number
        self.old_assets[(plan.index, item.slot)] = spec
        item._seed_spec = spec  # type: ignore[attr-defined]

    def _new_asset(
        self, plan: Plan, sr: ServiceRequest, contact: Contact, item: ServiceRequestItem
    ) -> None:
        status = plan.status
        resume = plan.resume
        hub = plan.hub
        if item.article_id is None or hub is None:
            return
        path: list[str]
        if status == SR.PERSONALIZATION_IN_PROGRESS:
            path = ["assign"] if self.rng.random() < 0.6 else []
        elif status == SR.PERSONALIZATION_DONE or (
            status == SR.RESCHEDULE_NEEDED and resume == SR.PERSONALIZATION_DONE
        ):
            path = ["assign"]
        elif status == SR.READY_FOR_PICKING or (
            status == SR.RESCHEDULE_NEEDED
            and resume == SR.READY_FOR_PICKING
            and "dv04" not in plan.flags
        ):
            path = ["assign", "prepare"]
        elif status == SR.RESCHEDULE_NEEDED and "dv04" in plan.flags:
            path = ["assign", "prepare", "provide", "return_to_hub"]
        elif status in (SR.SHIPPED, SR.PROVIDED) or (
            status == SR.RESCHEDULE_NEEDED and resume == SR.SHIPPED
        ):
            path = ["assign", "prepare", "provide"]
        elif status == SR.COMPLETED:
            path = ["assign", "prepare", "provide", "deploy"]
        elif status == SR.CANCELLED and "cancel_onsite" in plan.flags:
            path = ["assign", "prepare", "provide", "release"]
        else:
            path = []
        if not path:
            return
        article = next(a for a in self.story.articles.values() if a.id == item.article_id)
        claim = plan.event_time("claim") or sr.imported_at
        received = claim - timedelta(days=self.rng.randint(20, 60))
        place = self.story.place(hub)
        spec = self.new_asset(
            article, status=A.AVAILABLE, location=f"{hub}-STAGING", place=place, checked_in=received
        )
        self.move(
            spec,
            AssetAction.RECEIVE,
            (None, None, None),
            (A.AVAILABLE, f"{hub}-GOODS_IN", None),
            received,
            IM,
        )
        self.move(
            spec,
            AssetAction.PLACE,
            (A.AVAILABLE, f"{hub}-GOODS_IN", None),
            (A.AVAILABLE, f"{hub}-STAGING", place),
            received + timedelta(days=1),
            U,
            actor="theo.technician@example.com",
            source_ref="scan:installation_place",
        )
        editor = next((e.actor for e in plan.events if e.action == "claim"), DANA)
        state = (A.AVAILABLE, f"{hub}-STAGING", place)
        asset = spec.asset
        for step in path:
            if step == "assign":
                at = claim + timedelta(minutes=10 + self.rng.randint(0, 50))
                new = (A.ASSIGNED, f"{hub}-STAGING", place)
                self.move(spec, AssetAction.ASSIGN, state, new, at, U, actor=editor, sr_id=sr.id)
                item.assigned_at, item.assigned_by = at, self.uid(editor)
                asset.service_request_id = sr.id
            elif step == "prepare":
                at = plan.event_time("close_order") or claim + timedelta(hours=2)
                new = (A.PREPARED, f"{hub}-GOODS_OUT", None)
                self.move(spec, AssetAction.PREPARE, state, new, at, Y, sr_id=sr.id)
            elif step == "provide":
                at = (
                    plan.event_time("mark_provided")
                    or plan.event_time("ship")
                    or claim + timedelta(hours=4)
                )
                if plan.event_time("ship") is not None:
                    item.packed_at, item.packed_by = at - timedelta(minutes=15), self.uid(DANA)
                new = (A.PROVIDED, "IN_TRANSIT", None)
                self.move(spec, AssetAction.PROVIDE, state, new, at, Y, sr_id=sr.id)
            elif step == "return_to_hub":
                at = plan.event_time("reschedule_on_site") or self.now
                new = (A.PREPARED, f"{hub}-GOODS_OUT", None)
                self.move(spec, AssetAction.RETURN_TO_HUB, state, new, at, Y, sr_id=sr.id)
            elif step == "deploy":
                at = (
                    plan.event_time("complete_deployment")
                    or plan.event_time("complete_manually")
                    or self.now
                )
                new = (A.IN_USE, "IN_USE", None)
                self.move(spec, AssetAction.DEPLOY, state, new, at, Y, sr_id=sr.id)
                asset.assigned_contact_id = contact.id
                if plan.event_time("complete_deployment") is not None:
                    item.verified_at = at - timedelta(minutes=30)
                    item.verified_by = self.uid(plan.technician)
                    item.verified_serial = asset.serial_number
            elif step == "release":
                at = plan.event_time("cancel_on_site") or self.now
                new = (A.AVAILABLE, f"{hub}-GOODS_IN", None)
                self.move(spec, AssetAction.RELEASE, state, new, at, Y, sr_id=sr.id)
                asset.service_request_id = None
            else:  # pragma: no cover - exhaustive
                raise ValueError(step)
            state = new
        asset.status, asset.location_id, asset.installation_place = (
            state[0],
            self.loc(state[1]),
            state[2],
        )
        if status == SR.CANCELLED:
            item.assigned_at = item.assigned_by = (
                None  # SPEC_RESOLUTIONS #50: new items lose the asset
            )
            return
        item._seed_spec = spec  # type: ignore[attr-defined]

    def stock_assets(self) -> None:
        """Available stock per (article, hub) telling the §11.4 stock story."""
        story = self.story
        rng = self.rng
        demand: dict[tuple[int, str], int] = defaultdict(int)
        for plan in story.plans:
            is_demand = (
                plan.resume != SR.SHIPPED
                if plan.status == SR.RESCHEDULE_NEEDED
                else plan.status in DEMAND_STATUSES
            )
            if not is_demand or plan.hub is None:
                continue
            for requested in plan.new.values():
                article = story.articles.get(requested)
                if article is not None:
                    demand[(article.id, plan.hub)] += 1
        existing: dict[tuple[int, str], int] = defaultdict(int)
        hub_of_location = {loc.id: code.split("-")[0] for code, loc in story.locations.items()}
        stock_locations = {
            story.locations[f"{h}-{t}"].id
            for h in ("RIV", "HIL", "EAS")
            for t in ("GOODS_IN", "STAGING", "GOODS_OUT")
        }
        for spec in self.assets:
            asset = spec.asset
            if (
                asset.article_id
                and asset.location_id in stock_locations
                and asset.status in (A.AVAILABLE, A.ASSIGNED, A.PREPARED)
            ):
                existing[(asset.article_id, hub_of_location[asset.location_id])] += 1
        overrides = {
            ("Laptop Performance 16", "HIL"): lambda d, e: max(0, 3 - e),
            ("Monitor 34 Ultrawide", "EAS"): lambda d, e: 0,
            ("Docking Station Thunderbolt", "EAS"): lambda d, e: max(0, d - e - 2),
            ("Docking Station USB-C", "RIV"): lambda d, e: max(0, 32 - e),
        }
        for article in sorted(story.articles.values(), key=lambda a: a.number):
            for hub in ("RIV", "HIL", "EAS"):
                key = (article.id, hub)
                d, e = demand.get(key, 0), existing.get(key, 0)
                buffer = buffer_for(article.category, hub)
                rule = overrides.get((article.internal_description, hub))
                if rule is not None:
                    needed = rule(d, e)
                elif d == 0 and buffer == 0:
                    needed = rng.randint(0, 1)
                else:
                    needed = max(0, d + buffer + rng.randint(0, 2) + (1 if hub == "RIV" else 0) - e)
                for _ in range(needed):
                    self._stock_unit(article, hub)

    def _stock_unit(
        self, article: Article, hub: str, *, status: AssetStatus = A.AVAILABLE
    ) -> AssetSpec:
        rng = self.rng
        if rng.random() < 0.3:
            received = self.story.days_ago(rng.randint(1, 5), 8 + rng.randint(0, 8))
            spec = self.new_asset(
                article, status=status, location=f"{hub}-GOODS_IN", checked_in=received
            )
            self.move(
                spec,
                AssetAction.RECEIVE,
                (None, None, None),
                (A.AVAILABLE, f"{hub}-GOODS_IN", None),
                received,
                IM,
            )
            return spec
        received = self.story.days_ago(rng.randint(5, 60), 8 + rng.randint(0, 8))
        place = self.story.place(hub)
        spec = self.new_asset(
            article, status=status, location=f"{hub}-STAGING", place=place, checked_in=received
        )
        self.move(
            spec,
            AssetAction.RECEIVE,
            (None, None, None),
            (A.AVAILABLE, f"{hub}-GOODS_IN", None),
            received,
            IM,
        )
        self.move(
            spec,
            AssetAction.PLACE,
            (A.AVAILABLE, f"{hub}-GOODS_IN", None),
            (A.AVAILABLE, f"{hub}-STAGING", place),
            received + timedelta(hours=20),
            U,
            actor="theo.technician@example.com",
            source_ref="scan:installation_place",
        )
        return spec

    def missing_and_historic(self) -> None:
        story = self.story
        rng = self.rng
        # 4 missing: 2 at Riverton (inventory T-30), 2 at Eastgate (inventory T-60) — #44
        for hub, days, model in (("RIV", 30, "Laptop Standard 14"), ("EAS", 60, "Headset Stereo")):
            article = story.articles[model]
            inventory_id = SEED_INVENTORY_IDS[hub]
            for _ in range(2):
                received = story.days_ago(days + rng.randint(10, 30), 9)
                place = story.place(hub)
                spec = self.new_asset(
                    article,
                    status=A.MISSING,
                    location=f"{hub}-STAGING",
                    place=place,
                    checked_in=received,
                )
                self.move(
                    spec,
                    AssetAction.RECEIVE,
                    (None, None, None),
                    (A.AVAILABLE, f"{hub}-GOODS_IN", None),
                    received,
                    IM,
                )
                self.move(
                    spec,
                    AssetAction.PLACE,
                    (A.AVAILABLE, f"{hub}-GOODS_IN", None),
                    (A.AVAILABLE, f"{hub}-STAGING", place),
                    received + timedelta(days=1),
                    U,
                    actor="theo.technician@example.com",
                    source_ref="scan:installation_place",
                )
                self.move(
                    spec,
                    AssetAction.MARK_MISSING,
                    (A.AVAILABLE, f"{hub}-STAGING", place),
                    (A.MISSING, f"{hub}-STAGING", place),
                    story.days_ago(days, 16),
                    U,
                    actor=DANA,
                    source_ref=f"inventory:{inventory_id}",
                )
                self.story.handover["missing_asset_ids"][hub].append(spec)
        # 20 collected without SR + 40 deactivated (returned 30 / bought out 6 / disposed 4)
        old = story.article_by_model[OLD_BASE_MODEL]
        returns = ["RIV"] * 7 + ["HIL"] * 5 + ["EAS"] * 5 + ["CMP"] * 3
        self.historic_collected: list[AssetSpec] = []
        for hub in returns:
            spec = self.new_asset(
                old,
                status=A.COLLECTED,
                location=f"{hub}-RETURN",
                tag_kind="old",
                checked_in=story.days_ago(rng.randint(1000, 1300)),
            )
            self.move(
                spec,
                AssetAction.SYNC_IN_USE,
                (None, None, None),
                (A.IN_USE, "IN_USE", None),
                story.days_ago(200),
                IM,
            )
            self.move(
                spec,
                AssetAction.SYNC_COLLECTED,
                (A.IN_USE, "IN_USE", None),
                (A.COLLECTED, f"{hub}-RETURN", None),
                story.days_ago(4, 10),
                IM,
            )
            self.historic_collected.append(spec)
        self.deactivated: list[AssetSpec] = []
        batches = [
            ("deactivated_returned", 30, 20, "deactivation:1", 1),
            ("deactivated_bought_out", 6, 6, "deactivation:2", 2),
        ]
        for status, count, days, ref, batch in batches:
            for n in range(count):
                hub = ("RIV", "HIL", "EAS")[n % 3]
                spec = self.new_asset(
                    old,
                    status=AssetStatus(status),
                    location="OFF_FLEET",
                    tag_kind="old",
                    checked_in=story.days_ago(rng.randint(1100, 1400)),
                )
                self.move(
                    spec,
                    AssetAction.SYNC_IN_USE,
                    (None, None, None),
                    (A.IN_USE, "IN_USE", None),
                    story.days_ago(300),
                    IM,
                )
                self.move(
                    spec,
                    AssetAction.SYNC_COLLECTED,
                    (A.IN_USE, "IN_USE", None),
                    (A.COLLECTED, f"{hub}-RETURN", None),
                    story.days_ago(days + 20, 10),
                    IM,
                )
                self.move(
                    spec,
                    AssetAction.DEACTIVATE,
                    (A.COLLECTED, f"{hub}-RETURN", None),
                    (AssetStatus(status), "OFF_FLEET", None),
                    story.days_ago(days, 11),
                    U,
                    actor=DANA,
                    source_ref=ref,
                )
                self.story.handover["deactivated_asset_ids"][batch].append(spec)
                self.deactivated.append(spec)
        for _ in range(4):
            spec = self.new_asset(
                old,
                status=A.DEACTIVATED_DISPOSED,
                location="OFF_FLEET",
                tag_kind="old",
                checked_in=story.days_ago(rng.randint(1100, 1400)),
            )
            self.move(
                spec,
                AssetAction.SYNC_IN_USE,
                (None, None, None),
                (A.IN_USE, "IN_USE", None),
                story.days_ago(300),
                IM,
            )
            self.move(
                spec,
                AssetAction.SYNC_COLLECTED,
                (A.IN_USE, "IN_USE", None),
                (A.COLLECTED, "HIL-RETURN", None),
                story.days_ago(40),
                IM,
            )
            self.move(
                spec,
                AssetAction.CORRECT,
                (A.COLLECTED, "HIL-RETURN", None),
                (A.DEACTIVATED_DISPOSED, "OFF_FLEET", None),
                story.days_ago(15, 14),
                U,
                actor=DANA,
                reason="Disposed after water damage — lessor approval on file",
            )
            self.deactivated.append(spec)

    async def flush_assets(self) -> None:
        for spec in self.assets:
            self.session.add(spec.asset)
        await self.session.flush()
        for plan in self.story.plans:
            for item in self.srs[plan.index].items:
                spec = getattr(item, "_seed_spec", None)
                if spec is not None:
                    item.asset_id = spec.asset.id
        movements = [
            AssetMovement(asset_id=spec.asset.id, **event)
            for spec in self.assets
            for event in spec.events
        ]
        self.session.add_all(movements)
        await self.session.flush()
        handover = self.story.handover
        handover["missing_asset_ids"] = {
            k: [s.asset.id for s in v] for k, v in handover["missing_asset_ids"].items()
        }
        handover["deactivated_asset_ids"] = {
            k: [s.asset.id for s in v] for k, v in handover["deactivated_asset_ids"].items()
        }

    # --- status transitions --------------------------------------------------------------------

    def sr_transitions(self) -> None:
        for plan in self.story.plans:
            sr = self.srs[plan.index]
            current: str | None = None
            for event in plan.events:
                to = event.to if event.action not in ("pause_communication",) else current
                assert to is not None
                if event.action == "complete_deployment" and event.payload is not None:
                    event.payload["recipientName"] = self.contacts[plan.index].display_name
                self.transitions.append(
                    StatusTransition(
                        entity_type="service_request",
                        entity_id=sr.id,
                        action=event.action,
                        from_status=current,
                        to_status=to,
                        actor_user_id=self.uid(event.actor),
                        trigger=event.trigger,
                        comment=event.comment,
                        payload=event.payload,
                        occurred_at=event.at,
                    )
                )
                current = to
            handover = self.story.handover
            if "theo_today" in plan.flags:
                handover["theo_today_sr_ids"].append(sr.id)
            if "theo_past" in plan.flags:
                handover["theo_past_sr_ids"].append(sr.id)
            if "overdue" in plan.flags:
                handover["overdue_sr_ids"].append(sr.id)
            if plan.reschedule_history is not None:
                history = dict(plan.reschedule_history)
                history["start"] = history["start"].isoformat()
                if "cancelled_at" in history:
                    history["cancelled_at"] = history["cancelled_at"].isoformat()
                handover["reschedule_history"][sr.id] = history

    # --- lease returns -------------------------------------------------------------------------

    async def lease_returns(self) -> None:
        story = self.story
        rng = self.rng
        session = self.session
        leases: list[
            tuple[LeaseReturn, list[tuple[str, str | None, str, datetime, Trigger, str | None]]]
        ] = []

        def chain(
            info_at: datetime, steps: int
        ) -> list[tuple[str, str | None, str, datetime, Trigger, str | None]]:
            names = [
                ("send_info", LR.INFO_SENT),
                ("send_reminder_1", LR.REMINDER_1_SENT),
                ("send_reminder_2", LR.REMINDER_2_SENT),
                ("escalate", LR.ESCALATED),
            ]
            return [
                (names[n][0], None, names[n][1], info_at + timedelta(days=7 * n), J, None)
                for n in range(steps)
            ]

        def base(
            contact: Contact | None,
            user_id: str,
            tag: str,
            retirement: date,
            imported_at: datetime,
            serial: str,
        ) -> LeaseReturn:
            return LeaseReturn(
                asset_tag=tag,
                serial_number=serial,
                model_category="Notebook",
                model="Aero 14 G2 Laptop",
                checked_in_at=imported_at - timedelta(days=1000),
                retirement_date=retirement,
                user_id=user_id,
                contact_id=contact.id if contact else None,
                install_status="In use",
                ci_name=f"NB-{rng.randint(10000, 99999)}",
                cost_center=f"CC-{rng.randint(1000, 8999)}",
                imported_at=imported_at,
                last_seen_at=story.days_ago(8, 9),
                status=LR.IMPORTED,
            )

        # 40 replacement_ordered: linked to SRs whose old base tag matches (38 open + 2 DV10)
        open_with_old = [
            p
            for p in story.plans
            if S.OLD_BASE in p.old and p.status not in (SR.COMPLETED, SR.CANCELLED)
        ]
        dv10 = [p for p in story.plans if "dv10" in p.flags]
        for plan in [*open_with_old[:38], *dv10]:
            sr = self.srs[plan.index]
            spec = self.old_assets[(plan.index, S.OLD_BASE)]
            imported_at = sr.imported_at - timedelta(days=rng.randint(20, 60))
            lease = base(
                self.contacts[plan.index],
                self.contacts[plan.index].user_id,
                spec.asset.asset_tag or "",
                self.today + timedelta(days=rng.randint(-20, 60)),
                imported_at,
                spec.asset.serial_number,
            )
            lease.is_vip = sr.is_vip
            lease.is_campus = sr.is_campus
            events = []
            if rng.random() < 0.5:
                info = imported_at + timedelta(days=rng.randint(1, 5), hours=7)
                if info < sr.imported_at:
                    events += chain(info, 1)
                    lease.info_sent_at = info
            events.append(
                (
                    "link_service_request",
                    None,
                    LR.REPLACEMENT_ORDERED,
                    sr.imported_at + timedelta(minutes=1),
                    IM,
                    sr.number,
                )
            )
            lease.service_request_id, lease.linked_at = sr.id, sr.imported_at + timedelta(minutes=1)
            leases.append((lease, events))
        # 8 closed (device collected) from completed SRs
        collected = [
            p
            for p in story.plans
            if p.status == SR.COMPLETED
            and S.OLD_BASE in p.old
            and "dv10" not in p.flags
            and "manual_complete" not in p.flags
        ][:8]
        for plan in collected:
            sr = self.srs[plan.index]
            spec = self.old_assets[(plan.index, S.OLD_BASE)]
            imported_at = sr.imported_at - timedelta(days=rng.randint(20, 60))
            lease = base(
                self.contacts[plan.index],
                self.contacts[plan.index].user_id,
                spec.asset.asset_tag or "",
                (sr.completed_at or self.now).date() + timedelta(days=rng.randint(-10, 20)),
                imported_at,
                spec.asset.serial_number,
            )
            done = sr.completed_at or self.now
            lease.service_request_id, lease.linked_at = sr.id, sr.imported_at + timedelta(minutes=1)
            lease.closed_at, lease.close_reason = done + timedelta(minutes=1), "device_collected"
            events = [
                (
                    "link_service_request",
                    None,
                    LR.REPLACEMENT_ORDERED,
                    sr.imported_at + timedelta(minutes=1),
                    IM,
                    sr.number,
                ),
                ("close", None, LR.CLOSED, done + timedelta(minutes=1), Y, "device_collected"),
            ]
            leases.append((lease, events))
        # lease-only contacts (E100301+)
        next_contact = 300
        lease_contacts: list[Contact] = []

        def new_contact(*, campus: bool = False, email: bool = True) -> Contact:
            nonlocal next_contact
            region = REGION_LIST[rng.randrange(len(REGION_LIST))]
            contact = self.contact(
                next_contact, region, campus="zip" if campus else None, email=email
            )
            next_contact += 1
            session.add(contact)
            lease_contacts.append(contact)
            return contact

        # 7 closed (device deactivated)
        for spec in self.deactivated[:7]:
            contact = new_contact()
            asset = spec.asset
            closed_at = asset.status_changed_at
            lease = base(
                contact,
                contact.user_id,
                asset.asset_tag or "",
                closed_at.date() - timedelta(days=rng.randint(5, 30)),
                closed_at - timedelta(days=90),
                asset.serial_number,
            )
            lease.closed_at, lease.close_reason = (
                closed_at + timedelta(minutes=1),
                "device_deactivated",
            )
            leases.append(
                (
                    lease,
                    [
                        (
                            "close",
                            None,
                            LR.CLOSED,
                            closed_at + timedelta(minutes=1),
                            Y,
                            "device_deactivated",
                        )
                    ],
                )
            )
        # open leases without SR: their devices are still in use
        plan_rows = (
            [("imported", n) for n in range(30)]
            + [("imported_vip", n) for n in range(4)]
            + [("info_sent", n) for n in range(22)]
            + [("reminder_1_sent", n) for n in range(15)]
            + [("reminder_2_sent", n) for n in range(10)]
            + [("escalated", n) for n in range(8)]
            + [("probably_closed", n) for n in range(6)]
        )
        sr_vips = self.vip_contacts[:2]
        self.lease_vip_contacts: list[Contact] = []
        for status, n in plan_rows:
            if status == "imported_vip" and n < 2:
                contact = sr_vips[n]
            elif status == "imported_vip":
                contact = new_contact()
                self.lease_vip_contacts.append(contact)
            else:
                contact = new_contact(
                    campus=(status == "info_sent" and n < 2),
                    email=not (status == "imported" and n < 3),
                )
            article = story.article_by_model[OLD_BASE_MODEL]
            spec = self.new_asset(
                article,
                status=A.IN_USE,
                location="IN_USE",
                tag_kind="old",
                checked_in=story.days_ago(rng.randint(900, 1200)),
            )
            imported_at = (
                story.days_ago(rng.randint(9, 60), 9)
                if status != "imported"
                else story.days_ago(rng.randint(1, 8), 9)
            )
            self.move(
                spec,
                AssetAction.SYNC_IN_USE,
                (None, None, None),
                (A.IN_USE, "IN_USE", None),
                imported_at - timedelta(days=30),
                IM,
            )
            events: list[tuple[str, str | None, str, datetime, Trigger, str | None]] = []
            if status == "imported":
                retirement = self.today + timedelta(
                    days=rng.randint(30, 42) if n < 5 else rng.randint(43, 120)
                )
            elif status == "imported_vip":
                retirement = self.today + timedelta(days=rng.randint(20, 90))
            elif status == "probably_closed":
                retirement = self.today + timedelta(days=rng.randint(-40, 60))
            else:
                steps = {
                    "info_sent": 1,
                    "reminder_1_sent": 2,
                    "reminder_2_sent": 3,
                    "escalated": 4,
                }[status]
                last_ago = (
                    rng.randint(8, 12) if status == "escalated" and n < 2 else rng.randint(1, 6)
                )
                info_day = self.today - timedelta(days=last_ago + 7 * (steps - 1))
                info_at = self.story.at(info_day, 7, 10 + rng.randint(0, 20))
                imported_at = min(imported_at, info_at - timedelta(days=rng.randint(2, 10)))
                events += chain(info_at, steps)
                retirement = info_day + timedelta(days=rng.randint(10, 42))
            lease = base(
                contact,
                contact.user_id,
                spec.asset.asset_tag or "",
                retirement,
                imported_at,
                spec.asset.serial_number,
            )
            spec.asset.assigned_contact_id = contact.id if contact.id else None
            spec._lease_contact = contact  # type: ignore[attr-defined]
            if status == "imported_vip":
                lease.is_vip = True
            if status == "escalated" and n in (2, 3):
                lease.escalation_email = f"cost-centre-{rng.randint(1000, 8999)}@example.com"
            if status == "probably_closed":
                lease.last_seen_at = story.days_ago(30, 9)
                events.append(
                    (
                        "mark_probably_closed",
                        None,
                        LR.PROBABLY_CLOSED,
                        story.days_ago(8, 9, 30),
                        IM,
                        None,
                    )
                )
            leases.append((lease, events))
        while next_contact < 420:  # E100001–E100420 (SPEC §11.2): spare contacts without records
            new_contact()
        await session.flush()
        for spec in self.assets:
            contact = getattr(spec, "_lease_contact", None)
            if contact is not None:
                spec.asset.assigned_contact_id = contact.id
        # persist leases + transitions + comments
        for lease, events in leases:
            if lease.contact_id is None:
                contact = next((c for c in lease_contacts if c.user_id == lease.user_id), None)
                lease.contact_id = contact.id if contact else None
            for action, _frm, _to, at, _trigger, _ in events:
                if action == "send_info":
                    lease.info_sent_at = at
                elif action == "send_reminder_1":
                    lease.reminder_1_sent_at = at
                elif action == "send_reminder_2":
                    lease.reminder_2_sent_at = at
                elif action == "escalate":
                    lease.escalation_sent_at = at
            mails = [
                e
                for e in events
                if e[0] in ("send_info", "send_reminder_1", "send_reminder_2", "escalate")
            ]
            lease.communication_date = to_business_date(mails[-1][3]) if mails else None
            start = LR.IMPORTED_VIP if lease.is_vip and not events else LR.IMPORTED
            lease.status = LeaseReturnStatus(events[-1][2]) if events else start
            if lease.is_vip and events and events[-1][2] == LR.REPLACEMENT_ORDERED:
                lease.status = LR.REPLACEMENT_ORDERED
            lease.status_changed_at = events[-1][3] if events else lease.imported_at
            session.add(lease)
        await session.flush()
        comments = 0
        for lease, events in leases:
            current: str | None = None
            first = LR.IMPORTED_VIP if lease.is_vip else LR.IMPORTED
            self.transitions.append(
                StatusTransition(
                    entity_type="lease_return",
                    entity_id=lease.id,
                    action="import_create",
                    from_status=None,
                    to_status=first,
                    trigger=IM,
                    occurred_at=lease.imported_at,
                )
            )
            current = first
            for action, _frm, to, at, trigger, extra in events:
                link = action == "link_service_request"
                self.transitions.append(
                    StatusTransition(
                        entity_type="lease_return",
                        entity_id=lease.id,
                        action=action,
                        from_status=current,
                        to_status=to,
                        trigger=trigger,
                        comment=None if link else extra,
                        payload={"serviceRequestNumber": extra} if link else None,
                        occurred_at=at,
                    )
                )
                current = to
        commented = [lease for lease, _ in leases if lease.status not in (LR.CLOSED,)][:20]
        for n, lease in enumerate(commented):
            for k in range(2 if n < 10 else 1):
                author = DANA if (n + k) % 2 == 0 else ADA
                session.add(
                    LeaseReturnComment(
                        lease_return_id=lease.id,
                        body=COMMENTS[(n + k) % len(COMMENTS)],
                        author_user_id=self.uid(author),
                    )
                )
                comments += 1
        await session.flush()
        # VIP list: 6 SR VIPs + 2 lease-only VIPs
        for contact, (vip_type, title) in zip(
            [*self.vip_contacts, *self.lease_vip_contacts], VIP_TITLES, strict=False
        ):
            session.add(
                VipEntry(
                    user_id=contact.user_id,
                    first_name=contact.first_name,
                    last_name=contact.last_name,
                    vip_type=vip_type,
                    title=title,
                )
            )
        await session.flush()

    async def flush_transitions(self) -> None:
        self.session.add_all(self.transitions)
        await self.session.flush()


REGION_LIST: list[RegionDef] = []


async def persist_story(story: Story) -> None:
    from app.core.seed.reference import REGIONS

    REGION_LIST[:] = list(REGIONS)
    persister = Persister(story)
    await persister.service_requests()
    persister.sr_assets()
    persister.missing_and_historic()
    persister.stock_assets()
    await persister.lease_returns()
    await persister.flush_assets()
    persister.sr_transitions()
    await persister.flush_transitions()
