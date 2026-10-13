"""Real booking-provider adapter — SKELETON, never bound in the demo (SPEC §8.1).

Request signing: headers ``publickey``, ``salt`` (random per request) and
``signature = base64(HMAC-SHA256(key=private_key, msg=salt))``. Keys come only from env
(``BOOKING_PUBLIC_KEY`` / ``BOOKING_PRIVATE_KEY``). Responses are paged; the sync token is
read from a *named* response header; ``since`` windows are always explicit.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from collections.abc import AsyncIterator
from datetime import date, datetime
from typing import Any
from urllib.parse import quote

import httpx
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.core.config import get_settings
from app.core.gateways.protocols import (
    AppointmentChange,
    AppointmentChanges,
    BookingProviderError,
    CreateAppointmentRequest,
    ProviderAppointment,
    ProviderCalendar,
    ProviderService,
)

SYNC_TOKEN_HEADER = "X-Sync-Token"  # noqa: S105 - header name
PAGE_SIZE = 200


def sign_headers(public_key: str, private_key: str) -> dict[str, str]:
    salt = secrets.token_hex(16)
    digest = hmac.new(private_key.encode(), salt.encode(), hashlib.sha256).digest()
    return {"publickey": public_key, "salt": salt, "signature": base64.b64encode(digest).decode()}


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _to_appointment(raw: dict[str, Any]) -> ProviderAppointment:
    return ProviderAppointment(
        external_id=str(raw["id"]),
        calendar_external_id=str(raw["calendarId"]),
        start_at=_parse_dt(raw["start"]),
        end_at=_parse_dt(raw["end"]),
        status="cancelled" if raw.get("cancelled") else "booked",
        reference=(raw.get("customFields") or {}).get("reference"),
        title=raw.get("title"),
        contact_name=raw.get("contactName"),
        contact_email=raw.get("contactEmail"),
        street=raw.get("street"),
        town=raw.get("town"),
        notes=raw.get("notes"),
        created_at=_parse_dt(raw["createdAt"]),
        updated_at=_parse_dt(raw["updatedAt"]),
    )


def _seg(value: str) -> str:
    """One URL path segment: provider ids never add path levels or switch the host."""
    return quote(str(value), safe="")


class RealBookingProvider:
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        settings = get_settings()
        if settings.booking_public_key is None or settings.booking_private_key is None:
            raise BookingProviderError(
                "BOOKING_PUBLIC_KEY / BOOKING_PRIVATE_KEY are not configured."
            )
        self._public = settings.booking_public_key.get_secret_value()
        self._private = settings.booking_private_key.get_secret_value()
        self._client = client or httpx.AsyncClient(base_url=settings.booking_base_url, timeout=20)

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=1, max=10),
            retry=retry_if_exception_type(httpx.TransportError),
            reraise=True,
        ):
            with attempt:
                response = await self._client.request(
                    method, path, headers=sign_headers(self._public, self._private), **kwargs
                )
                if response.status_code >= 400:
                    raise BookingProviderError(f"Booking provider returned {response.status_code}.")
                return response
        raise BookingProviderError("unreachable")  # pragma: no cover

    async def list_calendars(self) -> list[ProviderCalendar]:
        data = (await self._request("GET", "/calendars")).json()
        return [
            ProviderCalendar(str(c["id"]), c["name"], bool(c.get("enabled", True)), c.get("type"))
            for c in data
        ]

    async def list_calendar_services(self, calendar_external_id: str) -> list[ProviderService]:
        data = (
            await self._request("GET", f"/calendars/{_seg(calendar_external_id)}/services")
        ).json()
        return [ProviderService(str(s["id"]), s["name"], int(s["duration"])) for s in data]

    async def list_appointments(
        self, calendar_external_id: str, *, since: date, until: date | None = None
    ) -> AsyncIterator[ProviderAppointment]:
        page = 1
        while True:
            params: dict[str, Any] = {
                "since": since.isoformat(),
                "page": page,
                "pageSize": PAGE_SIZE,
            }
            if until is not None:
                params["until"] = until.isoformat()
            items = (
                await self._request(
                    "GET", f"/calendars/{_seg(calendar_external_id)}/appointments", params=params
                )
            ).json()
            for raw in items:
                yield _to_appointment(raw)
            if len(items) < PAGE_SIZE:
                return
            page += 1

    async def sync_appointments(self, sync_token: str | None) -> AppointmentChanges:
        response = await self._request(
            "GET", "/appointments/changes", params={"token": sync_token or ""}
        )
        if response.status_code == 410:
            return AppointmentChanges([], "", True)
        body = response.json()
        changes = [
            AppointmentChange(c["kind"], _to_appointment(c["appointment"])) for c in body["changes"]
        ]
        return AppointmentChanges(changes, response.headers.get(SYNC_TOKEN_HEADER, ""), False)

    async def create_appointment(self, request: CreateAppointmentRequest) -> ProviderAppointment:
        payload = {
            "calendarId": request.calendar_external_id,
            "serviceId": request.service_id,
            "start": request.start_at.isoformat(),
            "end": request.end_at.isoformat(),
            "customFields": {"reference": request.reference},
            "contactName": request.contact_name,
            "contactEmail": request.contact_email,
            "street": request.street,
            "town": request.town,
            "notes": request.notes,
        }
        return _to_appointment((await self._request("POST", "/appointments", json=payload)).json())

    async def cancel_appointment(self, external_id: str, *, reason: str | None = None) -> None:
        await self._request(
            "POST", f"/appointments/{_seg(external_id)}/cancel", json={"reason": reason}
        )

    def build_booking_link(self, *, destination_id: str, service_id: str, reference: str) -> str:
        base = get_settings().booking_base_url.rstrip("/")
        return f"{base}/book/{destination_id}/{service_id}?ref={reference}"
