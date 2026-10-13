"""Real parcel-carrier adapter — SKELETON, never bound in the demo (SPEC §8.2).

Credentials only from env (``CARRIER_API_KEY`` / ``CARRIER_ACCOUNT``); labels arrive
base64-encoded; no hard-coded accounts.
"""

from __future__ import annotations

import base64
from typing import Any
from urllib.parse import quote

import httpx

from app.core.config import get_settings
from app.core.gateways.protocols import (
    CarrierError,
    Parcel,
    PostalAddress,
    ShipmentRequest,
    ShipmentResult,
)


def _address(a: PostalAddress) -> dict[str, Any]:
    return {
        "name": a.name,
        "name2": a.name2,
        "street": a.street,
        "zip": a.zip,
        "city": a.city,
        "country": a.country,
        "email": a.email,
    }


def _parcel(p: Parcel) -> dict[str, Any]:
    return {"weightKg": str(p.weight_kg), "references": p.references}


class RealParcelCarrier:
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        settings = get_settings()
        if settings.carrier_api_key is None or not settings.carrier_account:
            raise CarrierError("CARRIER_API_KEY / CARRIER_ACCOUNT are not configured.")
        self._account = settings.carrier_account
        self._client = client or httpx.AsyncClient(
            base_url=settings.carrier_base_url,
            timeout=30,
            headers={"Authorization": f"Bearer {settings.carrier_api_key.get_secret_value()}"},
        )

    async def create_shipment(self, request: ShipmentRequest) -> ShipmentResult:
        payload = {
            "account": self._account,
            "reference": request.reference,
            "sender": _address(request.sender),
            "recipient": _address(request.recipient),
            "parcels": [_parcel(p) for p in request.parcels],
            "returnParcels": [_parcel(p) for p in request.return_parcels],
            "returnAddress": _address(request.return_address) if request.return_address else None,
            "labelFormat": request.label_format,
        }
        try:
            response = await self._client.post("/shipments", json=payload)
        except httpx.HTTPError as exc:
            raise CarrierError(f"Carrier unreachable: {exc}") from exc
        if response.status_code >= 400:
            message = (
                response.json().get("message", "Carrier rejected the shipment")
                if response.content
                else "Carrier error"
            )
            raise CarrierError(message)
        body = response.json()
        return ShipmentResult(
            carrier_shipment_id=body["shipmentId"],
            parcel_ids=body["parcelIds"],
            tracking_urls=body["trackingUrls"],
            return_tracking_urls=body.get("returnTrackingUrls", []),
            label_pdf=base64.b64decode(body["labelPdf"]),
        )

    async def get_label(self, carrier_shipment_id: str) -> bytes:
        response = await self._client.get(f"/shipments/{quote(carrier_shipment_id, safe='')}/label")
        if response.status_code >= 400:
            raise CarrierError("Label not available")
        return base64.b64decode(response.json()["labelPdf"])
