"""``GET /meta/config`` (public) and ``GET /meta/enums`` (API.md §3.3, SPEC_RESOLUTIONS #17/#25)."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal, cast

from fastapi import APIRouter, Depends
from pydantic import Field, create_model, model_serializer

from app import __version__
from app.core import gateways
from app.core.auth import get_current_user
from app.core.config import get_settings
from app.core.constants import SCAN_CODES
from app.core.enums import ENUM_API_KEYS, ENUM_META, PipelineStage, ScanVerdict, Tone, Trigger
from app.core.errors import problem_responses
from app.core.pagination import MAX_PER_PAGE
from app.core.permissions import PERMISSIONS
from app.core.schemas import ApiModel
from app.core.statemachine import NEW, Transition, TransitionMap
from app.core.timeutil import business_today
from app.core.transitions import ASSET_TRANSITIONS, LEASE_TRANSITIONS, SR_TRANSITIONS

router = APIRouter(prefix="/meta", tags=["meta"])


class EnumValue(ApiModel):
    value: str
    label: str
    description: str | None
    tone: Tone | None
    order: int
    stage: PipelineStage | None = None
    terminal: bool | None = None

    @model_serializer(mode="wrap")
    def _drop_optional(self, handler: Any) -> dict[str, Any]:
        data = handler(self)
        for key in ("stage", "terminal"):
            if data.get(key) is None:
                data.pop(key, None)
        return data


class ScanCodeValue(ApiModel):
    value: str
    label: str
    verdict: ScanVerdict
    tone: Tone
    sound: Literal["ok", "warn", "error"]
    order: int


class TransitionDef(ApiModel):
    action: str
    label: str
    from_: list[str] = Field(alias="from")
    to: str | None
    triggers: list[Trigger]
    permission: str | None
    bulk: bool
    requires_comment: bool
    destructive: bool
    owner: str
    internal: bool
    via_endpoint: bool


class TransitionsMeta(ApiModel):
    service_request: list[TransitionDef]
    lease_return: list[TransitionDef]
    asset: list[TransitionDef]


class PermissionDef(ApiModel):
    key: str
    label: str


_enum_fields: dict[str, Any] = {
    key: (list[EnumValue], Field(..., alias=key)) for key in ENUM_API_KEYS
}
_EnumsBase: type[ApiModel] = cast(Any, create_model)("EnumsBase", __base__=ApiModel, **_enum_fields)


class EnumsResponse(_EnumsBase):
    scan_code: list[ScanCodeValue]
    transitions: TransitionsMeta
    permissions: list[PermissionDef]


class GatewayBindings(ApiModel):
    booking: Literal["fake", "real"]
    carrier: Literal["fake", "real"]
    mailer: Literal["smtp", "memory", "console"]


class Limits(ApiModel):
    import_max_bytes: int
    import_max_rows: int
    deactivation_max_rows: int
    bulk_max_ids: int
    per_page_max: int


class AppConfig(ApiModel):
    app_name: Literal["Hangar"] = "Hangar"
    version: str
    demo_mode: bool
    business_time_zone: str
    today: str
    gateways: GatewayBindings
    mailpit_url: str | None
    limits: Limits


def enum_values(enum_cls: type[StrEnum]) -> list[EnumValue]:
    meta = ENUM_META.get(enum_cls, {})
    values: list[EnumValue] = []
    for order, member in enumerate(enum_cls, start=1):
        item = meta.get(member.value)
        values.append(
            EnumValue(
                value=member.value,
                label=item.label if item else member.value.replace("_", " ").capitalize(),
                description=item.description if item else None,
                tone=item.tone if item else None,
                order=order,
                stage=item.stage if item else None,
                terminal=item.terminal if item else None,
            )
        )
    return values


def _transition_def(t: Transition) -> TransitionDef:
    permission = (
        t.permission if isinstance(t.permission, str) or t.permission is None else t.permission[0]
    )
    return TransitionDef.model_validate(
        {
            "action": t.action,
            "label": t.label,
            "from": sorted(s for s in t.from_ if s != NEW),
            "to": t.to if isinstance(t.to, str) else None,
            "triggers": sorted(t.triggers, key=lambda trig: list(Trigger).index(trig)),
            "permission": permission,
            "bulk": t.bulk,
            "requires_comment": t.requires_comment,
            "destructive": t.destructive,
            "owner": t.owner,
            "internal": t.internal,
            "via_endpoint": t.via_endpoint,
        }
    )


def _defs(tmap: TransitionMap) -> list[TransitionDef]:
    return [_transition_def(t) for t in tmap]


def build_enums() -> EnumsResponse:
    data: dict[str, Any] = {key: enum_values(cls) for key, cls in ENUM_API_KEYS.items()}
    return EnumsResponse(
        **data,
        scan_code=[
            ScanCodeValue(
                value=code,
                label=d.label,
                verdict=d.verdict,
                tone=d.tone,
                sound=cast(Literal["ok", "warn", "error"], d.sound),
                order=i,
            )
            for i, (code, d) in enumerate(SCAN_CODES.items(), start=1)
        ],
        transitions=TransitionsMeta(
            service_request=_defs(SR_TRANSITIONS),
            lease_return=_defs(LEASE_TRANSITIONS),
            asset=_defs(ASSET_TRANSITIONS),
        ),
        permissions=[PermissionDef(key=k, label=label) for k, (label, _) in PERMISSIONS.items()],
    )


def build_config() -> AppConfig:
    settings = get_settings()
    return AppConfig(
        version=__version__,
        demo_mode=settings.demo_mode,
        business_time_zone=settings.business_tz,
        today=business_today().isoformat(),
        gateways=GatewayBindings(**gateways.bindings()),  # type: ignore[arg-type]
        mailpit_url=settings.mailpit_url if settings.demo_mode else None,
        limits=Limits(
            import_max_bytes=settings.import_max_bytes,
            import_max_rows=settings.import_max_rows,
            deactivation_max_rows=settings.deactivation_max_rows,
            bulk_max_ids=200,
            per_page_max=MAX_PER_PAGE,
        ),
    )


@router.get("/config", response_model=AppConfig, summary="Public app configuration")
async def get_config() -> AppConfig:
    return build_config()


@router.get(
    "/enums",
    response_model=EnumsResponse,
    summary="Enum labels, tones, transitions and permissions",
    dependencies=[Depends(get_current_user)],
    responses=problem_responses(401),
)
async def get_enums() -> EnumsResponse:
    return build_enums()
