"""The transactional part of the core seed story (SPEC §11.2–11.5, SPEC_RESOLUTIONS #41–47).

Plans 300 service requests (every status populated, §11.3), their contacts, items, assets
with a consistent ``asset_movements`` history, ``status_transitions`` replayed from
``import_create``, 150 lease returns with comments, and hands over ids slices need for
their own seeders via ``ctx.data["core"]``.
"""

from __future__ import annotations

import itertools
import random
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import SEED_INVENTORY_IDS, calendar_for_region
from app.core.enums import (
    DEMAND_STATUSES,
    AssetStatus,
    DeliveryType,
    ItemSlot,
    LeaseReturnStatus,
    ProcurementType,
    ServiceRequestStatus,
    Trigger,
)
from app.core.models import (
    Article,
    Asset,
    StockLocation,
    User,
)
from app.core.seed.framework import SeedContext
from app.core.seed.reference import REGIONS, RegionDef
from app.core.seed.vocabulary import BASE32

SR = ServiceRequestStatus
LR = LeaseReturnStatus
A = AssetStatus
S = ItemSlot
J, U, IM, Y, SY = Trigger.JOB, Trigger.USER, Trigger.IMPORT, Trigger.SYSTEM, Trigger.SYNC

DANA, ADA = "dana.dispatcher@example.com", "ada.admin@example.com"
THEO, TARA, OMAR, JONAS = (
    "theo.technician@example.com",
    "tara.lindqvist@example.com",
    "omar.reyes@example.com",
    "jonas.weber@example.com",
)
TECHNICIAN_BY_REGION = {
    "riverton": THEO,
    "hillford": TARA,
    "eastgate": OMAR,
    "northport": OMAR,
    "southmere": OMAR,
    "westbay": JONAS,
    "lakeside": JONAS,
    "highmoor": JONAS,
    "oakridge": JONAS,
}

STATUS_COUNTS: tuple[tuple[SR, int], ...] = (
    (SR.IMPORTED, 25),
    (SR.IMPORTED_VIP, 6),
    (SR.HARDWARE_UNAVAILABLE, 10),
    (SR.INVITED, 30),
    (SR.CAMPUS_INFO_SENT, 8),
    (SR.REMINDER_1_SENT, 18),
    (SR.REMINDER_2_SENT, 12),
    (SR.ESCALATED, 8),
    (SR.RESCHEDULE_NEEDED, 8),
    (SR.APPOINTMENT_BOOKED, 35),
    (SR.PERSONALIZATION_IN_PROGRESS, 12),
    (SR.PERSONALIZATION_DONE, 14),
    (SR.READY_FOR_PICKING, 20),
    (SR.SHIPPED, 15),
    (SR.PROVIDED, 15),
    (SR.COMPLETED, 54),
    (SR.CANCELLED, 10),
)
REGION_WEIGHTS = {
    "riverton": 22,
    "hillford": 18,
    "eastgate": 14,
    "northport": 8,
    "westbay": 8,
    "southmere": 8,
    "lakeside": 8,
    "highmoor": 7,
    "oakridge": 7,
}
PARCEL_REGIONS = [r.code for r in REGIONS if r.delivery != DeliveryType.TECHNICIAN]
TECH_REGIONS = [r.code for r in REGIONS if r.delivery == DeliveryType.TECHNICIAN]
REGION_BY_CODE = {r.code: r for r in REGIONS}
BASE_TYPES = (
    ("Laptop Standard 14", 60),
    ("Laptop Performance 16", 20),
    ("Laptop Convertible 13", 10),
    ("Laptop Workstation 15", 10),
)
P1_TYPES = (("Docking Station USB-C", 70), ("Docking Station Thunderbolt", 30))
P2_TYPES = (
    ("Monitor 27", 40),
    ("Monitor 24", 25),
    ("Monitor 34 Ultrawide", 15),
    ("Headset Wireless ANC", 20),
)
PRE_STAGING = {
    SR.IMPORTED,
    SR.IMPORTED_VIP,
    SR.INVITED,
    SR.CAMPUS_INFO_SENT,
    SR.REMINDER_1_SENT,
    SR.REMINDER_2_SENT,
    SR.ESCALATED,
    SR.APPOINTMENT_BOOKED,
}
OLD_MODELS = (
    ("KD-AERO14-G2", "Notebook"),
    ("KD-DOCK-U3", "Docking station"),
    ("LM-VIEW-22", "Monitor"),
)


def _weighted(rng: random.Random, options: tuple[tuple[str, int], ...] | dict[str, int]) -> str:
    items = list(options.items()) if isinstance(options, dict) else list(options)
    return rng.choices([k for k, _ in items], [w for _, w in items])[0]


@dataclass
class Event:
    action: str
    to: str
    at: datetime
    trigger: Trigger
    actor: str | None = None
    comment: str | None = None
    payload: dict[str, Any] | None = None


@dataclass
class Plan:
    index: int
    status: SR
    region: RegionDef | None
    campus: str | None = None  # "zip" | "cc"
    procurement: ProcurementType = ProcurementType.REPLACEMENT
    new: dict[ItemSlot, str] = field(default_factory=dict)
    old: list[ItemSlot] = field(default_factory=list)
    flags: set[str] = field(default_factory=set)
    appt: datetime | None = None
    resume: SR | None = None
    deviation: tuple[int, str | None] | None = None
    editor: str | None = None
    events: list[Event] = field(default_factory=list)
    imported_at: datetime | None = None
    reschedule_history: dict[str, Any] | None = None

    @property
    def hub(self) -> str | None:
        return self.region.hub if self.region else None

    @property
    def technician(self) -> str:
        return TECHNICIAN_BY_REGION[self.region.code if self.region else "riverton"]

    @property
    def parcel(self) -> bool:
        return (
            self.region is not None
            and self.region.delivery != DeliveryType.TECHNICIAN
            and not self.campus
            and self.procurement != ProcurementType.REMOVAL
        )

    def event_time(self, action: str) -> datetime | None:
        return next((e.at for e in self.events if e.action == action), None)


class Story:
    def __init__(self, session: AsyncSession, ctx: SeedContext) -> None:
        self.session = session
        self.ctx = ctx
        self.rng = ctx.rng
        self.now = ctx.now
        self.today = ctx.today
        self.plans: list[Plan] = []
        self.users: dict[str, User] = {}
        self.locations: dict[str, StockLocation] = {}
        self.articles: dict[str, Article] = {}
        self.article_by_model: dict[str, Article] = {}
        self.serials: set[str] = set()
        self.tag_seq = {"old": 100001, "new": 200001}
        self.places: dict[str, int] = defaultdict(int)
        self.movements: list[tuple[Asset, list[dict[str, Any]]]] = []
        self.slots_taken: set[tuple[str, date, int]] = set()
        self.handover: dict[str, Any] = {
            "theo_today_sr_ids": [],
            "theo_past_sr_ids": [],
            "overdue_sr_ids": [],
            "reschedule_history": {},
            "missing_asset_ids": defaultdict(list),
            "deactivated_asset_ids": defaultdict(list),
            "inventory_ids": dict(SEED_INVENTORY_IDS),
        }

    # --- helpers ------------------------------------------------------------------------------

    def at(self, day: date, hour: int, minute: int = 0) -> datetime:
        return self.ctx.at(day, hour, minute)

    def days_ago(self, n: int, hour: int = 9, minute: int = 0) -> datetime:
        return self.at(self.today - timedelta(days=n), hour, minute)

    def past(self, value: datetime) -> datetime:
        return min(value, self.now - timedelta(minutes=5))

    def serial(self, prefix: str) -> str:
        while True:
            value = prefix + "".join(self.rng.choice(BASE32) for _ in range(8))
            if value not in self.serials:
                self.serials.add(value)
                return value

    def tag(self, kind: str) -> str:
        value = self.tag_seq[kind]
        self.tag_seq[kind] += 1
        return f"GX{value:06d}"

    def place(self, hub: str) -> str:
        n = self.places[hub]
        self.places[hub] += 1
        letter = "ABCDEF"[(n // 240) % 6]
        shelf = (n // 40) % 6 + 1
        slot = n % 40 + 1
        return f"{letter}{shelf}-{slot:02d}"

    def slot_time(self, calendar: str, day: date, hour: int | None = None) -> datetime:
        start = hour if hour is not None else 8 + self.rng.randint(0, 7)
        for h in [*range(start, 16), *range(8, start)]:
            if (calendar, day, h) not in self.slots_taken:
                self.slots_taken.add((calendar, day, h))
                return self.at(day, h)
        return self.at(day, start)

    # --- 1. plans -------------------------------------------------------------------------------

    def build_plans(self) -> None:
        rng = self.rng
        index = 0
        for status, count in STATUS_COUNTS:
            for n in range(count):
                plan = Plan(index, status, None)
                index += 1
                self._assign_region(plan, n)
                self._assign_items(plan)
                self.plans.append(plan)
        self._special_flags()
        self._stock_story_types()
        for plan in self.plans:
            if "unknown_type" in plan.flags:
                plan.new[S.NEW_BASE] = "Laptop Ultra 15"
        _ = rng

    def _assign_region(self, plan: Plan, n: int) -> None:
        rng = self.rng
        status = plan.status
        code: str | None
        if status == SR.CAMPUS_INFO_SENT:
            code, plan.campus = "riverton", ("zip" if n < 5 else "cc")
        elif status == SR.SHIPPED:
            code = _weighted(rng, {c: REGION_WEIGHTS[c] for c in PARCEL_REGIONS})
        elif status == SR.PROVIDED:
            code = (
                "riverton"
                if n < 7
                else _weighted(rng, {c: REGION_WEIGHTS[c] for c in TECH_REGIONS})
            )
        elif status == SR.IMPORTED and n < 3:
            code = None
            plan.flags.add("unresolved")
        else:
            code = _weighted(rng, REGION_WEIGHTS)
        plan.region = REGION_BY_CODE[code] if code else None
        # 7 more campus SRs (15 in total): booked 2, provided 2, completed 3 — all Riverton
        if (
            (status == SR.APPOINTMENT_BOOKED and n < 2)
            or (status == SR.PROVIDED and 5 <= n < 7)
            or (status == SR.COMPLETED and n < 3)
        ):
            plan.region, plan.campus = REGION_BY_CODE["riverton"], ("zip" if n % 2 == 0 else "cc")

    def _assign_items(self, plan: Plan) -> None:
        rng = self.rng
        roll = rng.random()
        removal_ok = plan.status in {
            SR.IMPORTED,
            SR.INVITED,
            SR.REMINDER_1_SENT,
            SR.APPOINTMENT_BOOKED,
            SR.COMPLETED,
            SR.CANCELLED,
        }
        if roll < 0.70:
            plan.procurement = ProcurementType.REPLACEMENT
        elif roll < 0.85:
            plan.procurement = ProcurementType.NEW_HIRE
        elif roll < 0.95 or not removal_ok or plan.campus or plan.status == SR.SHIPPED:
            plan.procurement = ProcurementType.ADDITIONAL_DEVICE
        else:
            plan.procurement = ProcurementType.REMOVAL
        if plan.status == SR.SHIPPED and plan.procurement == ProcurementType.REMOVAL:
            plan.procurement = ProcurementType.REPLACEMENT
        if plan.procurement != ProcurementType.REMOVAL:
            plan.new[S.NEW_BASE] = _weighted(rng, BASE_TYPES)
            if rng.random() < 0.65:
                plan.new[S.NEW_PERIPHERAL_1] = _weighted(rng, P1_TYPES)
            if rng.random() < 0.45:
                plan.new[S.NEW_PERIPHERAL_2] = _weighted(rng, P2_TYPES)
        if plan.procurement in (ProcurementType.REPLACEMENT, ProcurementType.REMOVAL):
            plan.old.append(S.OLD_BASE)
            if rng.random() < 0.20:
                plan.old.append(S.OLD_PERIPHERAL_1)

    def _special_flags(self) -> None:
        by_status: dict[SR, list[Plan]] = defaultdict(list)
        for plan in self.plans:
            by_status[plan.status].append(plan)
        imported = [p for p in by_status[SR.IMPORTED] if "unresolved" not in p.flags]
        for plan in imported[:4]:
            plan.flags.add("unknown_type")
            if S.NEW_BASE not in plan.new:
                plan.procurement = ProcurementType.NEW_HIRE
        for plan in imported[4:9]:
            plan.flags.add("no_email")
        for plan in by_status[SR.IMPORTED_VIP]:
            plan.flags.add("vip")
        for n, plan in enumerate(by_status[SR.HARDWARE_UNAVAILABLE]):
            plan.flags.add("hold_import" if n < 7 else "hold_manual")
        for plan in by_status[SR.ESCALATED][:3]:
            plan.flags.add("stale")
        pauses = [
            (by_status[SR.INVITED][0], "paused"),
            (by_status[SR.INVITED][1], "paused_until"),
            (by_status[SR.REMINDER_1_SENT][0], "paused"),
            (by_status[SR.REMINDER_1_SENT][1], "paused_until"),
            (by_status[SR.REMINDER_2_SENT][0], "paused"),
            (by_status[SR.ESCALATED][4], "paused_until"),
        ]
        for plan, flag in pauses:
            plan.flags.add(flag)
        for plan in by_status[SR.APPOINTMENT_BOOKED][-3:]:
            plan.flags.add("soon")
        provided = by_status[SR.PROVIDED]
        for plan in provided[:5]:
            plan.region, plan.campus = REGION_BY_CODE["riverton"], None
            plan.flags.add("theo_today")
        for plan in provided[5:7]:
            plan.flags.add("theo_other_day")
        for plan in provided[7:9]:
            plan.region = REGION_BY_CODE["hillford" if plan is provided[7] else "eastgate"]
            plan.flags.add("overdue")
        completed = by_status[SR.COMPLETED]
        for plan in completed[3:6]:
            plan.region, plan.campus = REGION_BY_CODE["riverton"], None
            plan.flags.add("theo_past")
        deviations = [
            (10, "User keeps the old laptop until the data migration is finished."),
            (10, "Old device not handed over; user asked for a later collection."),
            (12, None),
            (12, None),
            (12, None),
            (13, None),
            (13, None),
            (14, "Network port in the office was inactive; installation partially done."),
        ]
        for plan, deviation in zip(completed[6:14], deviations, strict=True):
            plan.deviation = deviation
            if deviation[0] == 10:
                plan.flags.add("dv10")
                plan.procurement = ProcurementType.REPLACEMENT
                if S.OLD_BASE not in plan.old:
                    plan.old.insert(0, S.OLD_BASE)
                if S.NEW_BASE not in plan.new:
                    plan.new[S.NEW_BASE] = "Laptop Standard 14"
        for plan in completed[14:16]:
            plan.flags.add("manual_complete")
        resume_plan = (
            [SR.APPOINTMENT_BOOKED] * 3
            + [SR.PERSONALIZATION_DONE] * 2
            + [SR.READY_FOR_PICKING] * 2
            + [SR.SHIPPED]
        )
        for plan, resume in zip(by_status[SR.RESCHEDULE_NEEDED], resume_plan, strict=True):
            plan.resume = resume
            if plan.procurement == ProcurementType.REMOVAL or S.NEW_BASE not in plan.new:
                plan.procurement = ProcurementType.REPLACEMENT
                plan.new.setdefault(S.NEW_BASE, "Laptop Standard 14")
            if resume == SR.SHIPPED:
                plan.region, plan.campus = REGION_BY_CODE["westbay"], None
        dv04 = by_status[SR.RESCHEDULE_NEEDED][5]
        dv04.region, dv04.campus = REGION_BY_CODE["riverton"], None
        dv04.flags.add("dv04")
        cancelled = by_status[SR.CANCELLED]
        for n, plan in enumerate(cancelled):
            plan.flags.add(
                "cancel_onsite" if n < 3 else ("cancel_dispatcher" if n < 7 else "cancel_itsm")
            )
            if n < 3 and plan.procurement == ProcurementType.REMOVAL:
                plan.procurement = ProcurementType.REPLACEMENT
                plan.new.setdefault(S.NEW_BASE, "Laptop Standard 14")
        for n, plan in enumerate(by_status[SR.PERSONALIZATION_IN_PROGRESS]):
            plan.editor = DANA if n < 8 else ADA
        for n, plan in enumerate(by_status[SR.PERSONALIZATION_DONE]):
            if n < 6:
                plan.flags.add("software_open")
        # staging statuses need new items
        for plan in self.plans:
            if (
                plan.status
                in {
                    SR.PERSONALIZATION_IN_PROGRESS,
                    SR.PERSONALIZATION_DONE,
                    SR.READY_FOR_PICKING,
                    SR.SHIPPED,
                    SR.PROVIDED,
                }
                and not plan.new
            ):
                plan.procurement = ProcurementType.ADDITIONAL_DEVICE
                plan.new[S.NEW_BASE] = "Laptop Standard 14"

    def _is_demand(self, plan: Plan) -> bool:
        if plan.status == SR.RESCHEDULE_NEEDED:
            return plan.resume != SR.SHIPPED
        return plan.status in DEMAND_STATUSES

    def _stock_story_types(self) -> None:
        """Hillford short on Laptop Performance 16 (demand 9, stock 3); Eastgate short on
        Monitor 34 Ultrawide (demand 6, stock 0) — SPEC §11.4."""
        hil_pre = [
            p
            for p in self.plans
            if p.hub == "HIL"
            and p.status in PRE_STAGING
            and S.NEW_BASE in p.new
            and "unknown_type" not in p.flags
        ]
        for plan in self.plans:
            if (
                plan.hub == "HIL"
                and self._is_demand(plan)
                and plan.new.get(S.NEW_BASE) == "Laptop Performance 16"
            ):
                plan.new[S.NEW_BASE] = "Laptop Standard 14"
        for plan in hil_pre[:9]:
            plan.new[S.NEW_BASE] = "Laptop Performance 16"
        eas_pre = [
            p
            for p in self.plans
            if p.hub == "EAS"
            and p.status in PRE_STAGING
            and p.new
            and "unknown_type" not in p.flags
        ]
        for plan in self.plans:
            if (
                plan.hub == "EAS"
                and self._is_demand(plan)
                and plan.new.get(S.NEW_PERIPHERAL_2) == "Monitor 34 Ultrawide"
            ):
                plan.new[S.NEW_PERIPHERAL_2] = "Monitor 27"
        for plan in eas_pre[:6]:
            plan.new[S.NEW_PERIPHERAL_2] = "Monitor 34 Ultrawide"

    # --- 2. timelines ---------------------------------------------------------------------------

    def build_timelines(self) -> None:
        for plan in self.plans:
            self._appointment(plan)
        for plan in self.plans:
            self._timeline(plan)
            plan.events = self._clamp(plan.events)
            plan.imported_at = plan.events[0].at

    def _appointment(self, plan: Plan) -> None:
        rng = self.rng
        ctx = self.ctx
        region = plan.region or REGION_BY_CODE["riverton"]
        calendar = region.code
        status = plan.status
        day: date | None = None
        hour: int | None = None
        minute = 0
        if "theo_today" in plan.flags:
            idx = sum(1 for p in self.plans if "theo_today" in p.flags and p.appt is not None)
            hour, minute = [(8, 0), (9, 30), (11, 0), (13, 0), (14, 30)][idx]
            plan.appt = self.at(ctx.d0, hour, minute)
            self.slots_taken.add((calendar, ctx.d0, hour))
            return
        if "theo_past" in plan.flags:
            idx = sum(1 for p in self.plans if "theo_past" in p.flags and p.appt is not None)
            hour = [8, 10, 13][idx]
            plan.appt = self.slot_time(calendar, ctx.wd(-1), hour)
            return
        if status == SR.APPOINTMENT_BOOKED:
            day = ctx.wd(1) if "soon" in plan.flags else ctx.wd(rng.randint(2, 15))
        elif status == SR.PERSONALIZATION_IN_PROGRESS:
            day = ctx.wd(rng.randint(1, 5))
        elif status == SR.PERSONALIZATION_DONE:
            day = ctx.wd(rng.randint(1, 4))
        elif status == SR.READY_FOR_PICKING:
            day = ctx.wd(rng.randint(0, 3))
        elif status == SR.SHIPPED:
            day = ctx.wd(rng.randint(0, 2))
        elif status == SR.PROVIDED:
            day = ctx.wd(-2) if "overdue" in plan.flags else ctx.wd(rng.randint(1, 2))
        elif status == SR.COMPLETED:
            day = ctx.wd(-rng.randint(2, 30))
        elif status == SR.CANCELLED and "cancel_onsite" in plan.flags:
            day = ctx.wd(-rng.randint(2, 10))
        elif status == SR.RESCHEDULE_NEEDED:
            day = (
                ctx.wd(-1)
                if "dv04" in plan.flags
                else (
                    ctx.wd(-rng.randint(1, 3))
                    if plan.resume == SR.SHIPPED
                    else ctx.wd(rng.randint(1, 6))
                )
            )
            hour = 11 if "dv04" in plan.flags else None
            start = self.slot_time(calendar, day, hour)
            plan.reschedule_history = {
                "start": start,
                "calendar": calendar_for_region(calendar).id,
                "kind": "on_site"
                if plan.resume in (SR.SHIPPED,) or "dv04" in plan.flags
                else "sync_cancel",
            }
            return
        if day is not None:
            plan.appt = self.slot_time(calendar, day, hour)

    def _chain(self, plan: Plan, first_mail_day: date, steps: list[str]) -> list[Event]:
        """Chain mails at 07:05 business time, 5 calendar days apart (SR defaults)."""
        events = []
        to_status = {
            "send_invitation": SR.INVITED,
            "send_campus_info": SR.CAMPUS_INFO_SENT,
            "send_reminder_1": SR.REMINDER_1_SENT,
            "send_reminder_2": SR.REMINDER_2_SENT,
            "escalate": SR.ESCALATED,
        }
        day = first_mail_day
        for step in steps:
            events.append(
                Event(step, to_status[step], self.at(day, 7, 5 + self.rng.randint(0, 20)), J)
            )
            day = day + timedelta(days=5)
        return events

    def _first_mail(self, plan: Plan) -> str:
        return "send_campus_info" if plan.campus else "send_invitation"

    def _timeline(self, plan: Plan) -> None:
        rng = self.rng
        status = plan.status
        ev = plan.events
        tech = plan.technician

        def imported(at: datetime) -> None:
            ev.insert(
                0,
                Event(
                    "import_create", SR.IMPORTED_VIP if "vip" in plan.flags else SR.IMPORTED, at, IM
                ),
            )

        if status == SR.IMPORTED:
            imported(self.now - timedelta(minutes=rng.randint(60, 30 * 60)))
            return
        if status == SR.IMPORTED_VIP:
            imported(self.days_ago(rng.randint(1, 8), 8, rng.randint(0, 59)))
            return
        if status == SR.HARDWARE_UNAVAILABLE:
            at = self.days_ago(rng.randint(3, 12), 8, rng.randint(0, 59))
            imported(at)
            if "hold_import" in plan.flags:
                ev.append(
                    Event(
                        "mark_hardware_unavailable",
                        SR.HARDWARE_UNAVAILABLE,
                        at + timedelta(minutes=2),
                        IM,
                    )
                )
            else:
                ev.extend(self._chain(plan, (at + timedelta(days=1)).date(), ["send_invitation"]))
                ev.append(
                    Event(
                        "mark_hardware_unavailable",
                        SR.HARDWARE_UNAVAILABLE,
                        at + timedelta(days=2, hours=2),
                        U,
                        DANA,
                    )
                )
            return
        comm_steps = {
            SR.INVITED: ["send_invitation"],
            SR.CAMPUS_INFO_SENT: ["send_campus_info"],
            SR.REMINDER_1_SENT: ["send_invitation", "send_reminder_1"],
            SR.REMINDER_2_SENT: ["send_invitation", "send_reminder_1", "send_reminder_2"],
            SR.ESCALATED: ["send_invitation", "send_reminder_1", "send_reminder_2", "escalate"],
        }
        if status in comm_steps:
            steps = comm_steps[status]
            if status == SR.ESCALATED:
                last_ago = rng.randint(8, 12) if "stale" in plan.flags else rng.randint(1, 6)
            elif status == SR.CAMPUS_INFO_SENT:
                last_ago = rng.randint(1, 6)
            else:
                last_ago = rng.randint(1, 4)
            first_day = self.today - timedelta(days=last_ago + 5 * (len(steps) - 1))
            imported(self.at(first_day - timedelta(days=rng.randint(1, 2)), 9, rng.randint(0, 59)))
            ev.extend(self._chain(plan, first_day, steps))
            last = ev[-1].at
            if "paused" in plan.flags or "paused_until" in plan.flags:
                payload: dict[str, Any] = {
                    "reason": "User on parental leave"
                    if "paused" in plan.flags
                    else "User travelling"
                }
                if "paused_until" in plan.flags:
                    payload["resumeDate"] = (
                        self.today + timedelta(days=rng.randint(3, 10))
                    ).isoformat()
                ev.append(
                    Event(
                        "pause_communication",
                        status,
                        self.past(last + timedelta(hours=5)),
                        U,
                        DANA,
                        payload=payload,
                    )
                )
            return

        # --- statuses with an appointment (or a past one) -----------------------------------
        cancel_at: datetime | None = None
        if status == SR.RESCHEDULE_NEEDED:
            assert plan.reschedule_history is not None
            appt = plan.reschedule_history["start"]
            target = plan.resume
            if plan.reschedule_history["kind"] == "on_site":
                target = SR.SHIPPED if plan.resume == SR.SHIPPED else SR.PROVIDED
            else:
                cancel_at = self.now - timedelta(hours=rng.randint(2, 48))
        else:
            appt = plan.appt
            target = status
        if status == SR.CANCELLED and "cancel_onsite" not in plan.flags:
            self._cancel_timeline(plan)
            return
        assert appt is not None
        in_past = appt < self.now
        book_at = (
            appt - timedelta(days=rng.randint(4, 12))
            if in_past
            else self.now - timedelta(days=rng.randint(1, 8), hours=rng.randint(0, 6))
        )
        staging_end = min(appt - timedelta(hours=16), self.now - timedelta(minutes=30))
        if cancel_at is not None:
            staging_end = min(staging_end, cancel_at - timedelta(hours=1))
            book_at = min(book_at, staging_end - timedelta(hours=6))
        first_mail = (book_at - timedelta(days=rng.randint(2, 6))).date()
        imported(self.at(first_mail - timedelta(days=rng.randint(1, 2)), 9, rng.randint(0, 59)))
        ev.extend(self._chain(plan, first_mail, [self._first_mail(plan)]))
        if plan.campus:
            ev.append(
                Event(
                    "book_appointment",
                    SR.APPOINTMENT_BOOKED,
                    book_at,
                    U,
                    DANA,
                    payload={
                        "calendarId": calendar_for_region("riverton").id,
                        "startAt": appt.isoformat(),
                    },
                )
            )
        else:
            ev.append(Event("appointment_synced", SR.APPOINTMENT_BOOKED, book_at, SY))
        order = [
            SR.PERSONALIZATION_IN_PROGRESS,
            SR.PERSONALIZATION_DONE,
            SR.READY_FOR_PICKING,
            SR.PROVIDED,
            SR.COMPLETED,
        ]
        reach = {
            SR.APPOINTMENT_BOOKED: 0,
            SR.PERSONALIZATION_IN_PROGRESS: 1,
            SR.PERSONALIZATION_DONE: 2,
            SR.READY_FOR_PICKING: 3,
            SR.SHIPPED: 4,
            SR.PROVIDED: 4,
            SR.COMPLETED: 5,
            SR.CANCELLED: 4,
        }[target]  # type: ignore[index]
        steps = order[:reach]
        span = max(staging_end - book_at, timedelta(minutes=10 * (len(steps) + 1)))
        editor = plan.editor or rng.choice([DANA, DANA, ADA])
        for n, step in enumerate(steps, start=1):
            at = book_at + span * n / (len(steps) + 1)
            if step == SR.PERSONALIZATION_IN_PROGRESS:
                ev.append(Event("claim", step, at, U, editor))
            elif step == SR.PERSONALIZATION_DONE:
                ev.append(Event("finish_personalization", step, at, U, editor))
            elif step == SR.READY_FOR_PICKING:
                ev.append(Event("close_order", step, at, U, DANA))
            elif step == SR.PROVIDED:
                if plan.parcel:
                    ev.append(Event("ship", SR.SHIPPED, at, U, DANA))
                else:
                    ev.append(Event("mark_provided", SR.PROVIDED, at, U, DANA))
            elif step == SR.COMPLETED:
                done_at = appt + timedelta(minutes=50)
                if "manual_complete" in plan.flags:
                    ev.append(
                        Event(
                            "complete_manually",
                            SR.COMPLETED,
                            done_at,
                            U,
                            DANA,
                            comment="User installed the device; confirmed by phone.",
                        )
                    )
                else:
                    payload = {
                        "recipientName": "(recipient)",
                        "handoverConfirmed": True,
                        "sendProtocol": True,
                    }
                    comment = None
                    if plan.deviation:
                        payload["deviationCodeId"] = plan.deviation[0]
                        comment = plan.deviation[1]
                    ev.append(
                        Event(
                            "complete_deployment",
                            SR.COMPLETED,
                            done_at,
                            U,
                            tech,
                            comment=comment,
                            payload=payload,
                        )
                    )
        if status == SR.CANCELLED:  # on site
            code = 15 if plan.index % 2 == 0 else 16
            plan.deviation = (
                code,
                "Client confirmed the order was placed by mistake."
                if code == 15
                else "User refused the exchange.",
            )
            ev.append(
                Event(
                    "cancel_on_site",
                    SR.CANCELLED,
                    appt + timedelta(minutes=30),
                    U,
                    tech,
                    comment=plan.deviation[1],
                    payload={"deviationCodeId": code},
                )
            )
        if status == SR.RESCHEDULE_NEEDED:
            history = plan.reschedule_history
            assert history is not None
            if history["kind"] == "on_site":
                code, note = (4, None) if "dv04" in plan.flags else (5, None)
                plan.deviation = (code, note)
                ev.append(
                    Event(
                        "reschedule_on_site",
                        SR.RESCHEDULE_NEEDED,
                        appt + timedelta(minutes=20),
                        U,
                        tech,
                        payload={"deviationCodeId": code},
                    )
                )
                history["deviation_code_id"] = code
            else:
                assert cancel_at is not None
                ev.append(Event("appointment_cancelled", SR.RESCHEDULE_NEEDED, cancel_at, SY))
                history["cancelled_at"] = cancel_at

    def _cancel_timeline(self, plan: Plan) -> None:
        rng = self.rng
        ev = plan.events
        at = self.days_ago(rng.randint(6, 25), 9, rng.randint(0, 59))
        ev.append(Event("import_create", SR.IMPORTED, at, IM))
        ev.extend(self._chain(plan, (at + timedelta(days=1)).date(), [self._first_mail(plan)]))
        cancel_at = ev[-1].at + timedelta(days=rng.randint(1, 4), hours=3)
        if "cancel_itsm" in plan.flags:
            ev.append(Event("cancel", SR.CANCELLED, cancel_at, IM, comment="Cancelled in ITSM"))
        else:
            reason = rng.choice(
                [
                    "Duplicate order",
                    "User left the company",
                    "Order replaced by a new request",
                    "Manager withdrew the request",
                ]
            )
            ev.append(Event("cancel", SR.CANCELLED, cancel_at, U, DANA, comment=reason))

    def _clamp(self, events: list[Event]) -> list[Event]:
        """Ensure strictly increasing timestamps, all in the past."""
        limit = self.now - timedelta(minutes=2)
        for event in reversed(events):
            if event.at > limit:
                event.at = limit
            limit = event.at - timedelta(minutes=1)
        for prev, event in itertools.pairwise(events):
            if event.at <= prev.at:
                event.at = prev.at + timedelta(minutes=1)
        return events

    # --- 3. persist ------------------------------------------------------------------------------

    async def load_refs(self) -> None:
        session = self.session
        self.users = {u.email: u for u in (await session.scalars(select(User))).all()}
        self.locations = {
            item_.code: item_ for item_ in (await session.scalars(select(StockLocation))).all()
        }
        for article in (await session.scalars(select(Article))).all():
            self.article_by_model[article.model_number] = article
            if article.is_active:
                self.articles[article.internal_description] = article

    def uid(self, email: str | None) -> int | None:
        return self.users[email].id if email else None


async def seed_story(session: AsyncSession, ctx: SeedContext) -> None:
    from app.core.seed.persist import persist_story

    story = Story(session, ctx)
    await story.load_refs()
    story.build_plans()
    story.build_timelines()
    story.plans.sort(key=lambda p: (p.imported_at, p.index))
    await persist_story(story)
    ctx.data["core"] = story.handover
