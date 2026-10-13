"""Compact reference builders (API.md §2 "references")."""

from __future__ import annotations

from app.core.models import (
    Article,
    Asset,
    Contact,
    DeviationCode,
    Region,
    ServiceRequest,
    Site,
    StockLocation,
    User,
)
from app.core.schemas import (
    ArticleRef,
    AssetSummary,
    ContactRef,
    DeviationCodeRef,
    LocationRef,
    RegionRef,
    SiteRef,
    SrRef,
    UserRef,
)


def user_ref(user: User | None) -> UserRef | None:
    if user is None:
        return None
    return UserRef(id=user.id, display_name=user.display_name, email=user.email, role=user.role)


def site_ref(site: Site | None) -> SiteRef | None:
    if site is None:
        return None
    return SiteRef(id=site.id, code=site.code, name=site.name, kind=site.kind)


def region_ref(region: Region | None) -> RegionRef | None:
    if region is None:
        return None
    return RegionRef(id=region.id, code=region.code, name=region.name)


def location_ref(location: StockLocation | None) -> LocationRef | None:
    if location is None:
        return None
    return LocationRef(
        id=location.id,
        code=location.code,
        name=location.name,
        type=location.type,
        site_id=location.site_id,
    )


def article_ref(article: Article | None) -> ArticleRef | None:
    if article is None:
        return None
    return ArticleRef(
        id=article.id,
        number=article.number,
        name=article.name,
        model_number=article.model_number,
        internal_description=article.internal_description,
        category=article.category,
    )


def contact_ref(contact: Contact | None) -> ContactRef | None:
    if contact is None:
        return None
    return ContactRef(
        id=contact.id,
        user_id=contact.user_id,
        display_name=contact.display_name,
        email=contact.email,
    )


def deviation_code_ref(code: DeviationCode | None) -> DeviationCodeRef | None:
    if code is None:
        return None
    return DeviationCodeRef(id=code.id, code=code.code, label=code.label, outcome=code.outcome)


def sr_ref(sr: ServiceRequest | None) -> SrRef | None:
    return SrRef(id=sr.id, number=sr.number) if sr is not None else None


def asset_summary(asset: Asset | None) -> AssetSummary | None:
    if asset is None:
        return None
    location = location_ref(asset.location)
    assert location is not None
    return AssetSummary(
        id=asset.id,
        serial_number=asset.serial_number,
        asset_tag=asset.asset_tag,
        article=article_ref(asset.article),
        model=asset.model,
        status=asset.status,
        location=location,
        installation_place=asset.installation_place,
        service_request=sr_ref(asset.service_request),
    )
