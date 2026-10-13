"""Gateway access + DI binding (SPEC §8). Fakes are bound by default (``GATEWAY_*`` env).

Use ``gateways.booking()`` / ``carrier()`` / ``mailer()`` / ``ticketing()`` in jobs and
services, or the ``*Dep`` annotations in routers. Tests swap implementations with
``override(mailer=MemoryMailer())`` (returns a restore function) or ``reset()``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any

from fastapi import Depends

from app.core.config import get_settings
from app.core.gateways.mailers import ConsoleMailer, MemoryMailer, SmtpMailer
from app.core.gateways.protocols import (
    BookingProvider,
    Mailer,
    ParcelCarrier,
    TicketingImportSource,
)

_instances: dict[str, Any] = {}


def _build(name: str) -> Any:
    settings = get_settings()
    if name == "booking":
        if settings.gateway_booking == "real":
            from app.core.gateways.real.booking import RealBookingProvider

            return RealBookingProvider()
        from app.core.gateways.fakes.booking import FakeBookingProvider

        return FakeBookingProvider()
    if name == "carrier":
        if settings.gateway_carrier == "real":
            from app.core.gateways.real.carrier import RealParcelCarrier

            return RealParcelCarrier()
        from app.core.gateways.fakes.carrier import FakeParcelCarrier

        return FakeParcelCarrier()
    if name == "mailer":
        return {"smtp": SmtpMailer, "memory": MemoryMailer, "console": ConsoleMailer}[
            settings.gateway_mailer
        ]()
    if name == "ticketing":
        from app.core.gateways.fakes.ticketing import FakeTicketingImportSource

        return FakeTicketingImportSource()
    raise KeyError(name)  # pragma: no cover


def _get(name: str) -> Any:
    if name not in _instances:
        _instances[name] = _build(name)
    return _instances[name]


def booking() -> BookingProvider:
    return _get("booking")


def carrier() -> ParcelCarrier:
    return _get("carrier")


def mailer() -> Mailer:
    return _get("mailer")


def ticketing() -> TicketingImportSource:
    return _get("ticketing")


def override(**implementations: Any) -> Callable[[], None]:
    """Bind implementations (``booking=``, ``carrier=``, ``mailer=``, ``ticketing=``)."""
    previous = {name: _instances.get(name) for name in implementations}
    _instances.update(implementations)

    def restore() -> None:
        for name, value in previous.items():
            if value is None:
                _instances.pop(name, None)
            else:
                _instances[name] = value

    return restore


def reset() -> None:
    _instances.clear()


def bindings() -> dict[str, str]:
    """For ``GET /meta/config.gateways``."""
    settings = get_settings()
    return {
        "booking": settings.gateway_booking,
        "carrier": settings.gateway_carrier,
        "mailer": settings.gateway_mailer,
    }


BookingDep = Annotated[BookingProvider, Depends(booking)]
CarrierDep = Annotated[ParcelCarrier, Depends(carrier)]
MailerDep = Annotated[Mailer, Depends(mailer)]
TicketingDep = Annotated[TicketingImportSource, Depends(ticketing)]
