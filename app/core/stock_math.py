"""Stock level, demand and order quantity — SPEC §6.7/§6.8 (+ SPEC_RESOLUTIONS #18).

Pure formulas plus the set-based recalculation used by H5's ``stock.recalculate`` job and
the dashboard. ``order_qty = max(0, demand + buffer − stock)``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import DEMAND_STATUSES, STOCK_TYPES, AssetStatus, ServiceRequestStatus

#: Asset statuses that count as stock when located in a hub stock location.
STOCK_ASSET_STATUSES = frozenset(
    {AssetStatus.AVAILABLE, AssetStatus.ASSIGNED, AssetStatus.PREPARED}
)


def order_qty(demand: int, stock: int, buffer: int) -> int:
    """SPEC_RESOLUTIONS #18: ``max(0, demand + buffer − stock)``."""
    return max(0, demand + buffer - stock)


def below_min(stock: int, quantity: int, min_qty: int | None) -> bool:
    return min_qty is not None and stock + quantity < min_qty


def above_max(stock: int, quantity: int, max_qty: int | None) -> bool:
    return max_qty is not None and stock + quantity > max_qty


@dataclass(frozen=True, slots=True)
class RecalculationResult:
    articles: int
    sites: int
    calculated_at: datetime
    rows: int


def _in(values: frozenset[str] | set[str]) -> str:
    return ", ".join(f"'{v}'" for v in sorted(str(x) for x in values))


#: One UPDATE computing stock per type and demand for every (article, hub) setting row.
RECALC_SQL = f"""
WITH stock AS (
    SELECT a.article_id, l.site_id,
           count(*) FILTER (WHERE l.type = 'goods_in') AS goods_in,
           count(*) FILTER (WHERE l.type = 'staging') AS staging,
           count(*) FILTER (WHERE l.type = 'goods_out') AS goods_out
    FROM assets a JOIN stock_locations l ON l.id = a.location_id
    WHERE a.article_id IS NOT NULL
      AND a.status IN ({_in(set(STOCK_ASSET_STATUSES))})
      AND l.type IN ({_in(set(STOCK_TYPES))})
    GROUP BY a.article_id, l.site_id
),
demand AS (
    SELECT i.article_id, s.supplying_site_id AS site_id, count(*) AS demand
    FROM service_request_items i JOIN service_requests s ON s.id = i.service_request_id
    WHERE left(i.slot, 4) = 'new_' AND i.article_id IS NOT NULL AND s.supplying_site_id IS NOT NULL
      AND (s.status IN ({_in(set(DEMAND_STATUSES))})
           OR (s.status = '{ServiceRequestStatus.RESCHEDULE_NEEDED.value}'
               AND s.resume_status IS DISTINCT FROM '{ServiceRequestStatus.SHIPPED.value}'))
    GROUP BY i.article_id, s.supplying_site_id
)
UPDATE article_site_settings ass SET
    stock_goods_in = coalesce(st.goods_in, 0),
    stock_staging = coalesce(st.staging, 0),
    stock_goods_out = coalesce(st.goods_out, 0),
    stock_qty = coalesce(st.goods_in, 0) + coalesce(st.staging, 0) + coalesce(st.goods_out, 0),
    demand_qty = coalesce(d.demand, 0),
    calculated_at = :now,
    updated_at = now()
FROM article_site_settings base
LEFT JOIN stock st ON st.article_id = base.article_id AND st.site_id = base.site_id
LEFT JOIN demand d ON d.article_id = base.article_id AND d.site_id = base.site_id
WHERE ass.id = base.id
"""


ENSURE_SITE_SETTINGS_SQL = """
INSERT INTO article_site_settings (article_id, site_id)
SELECT :article_id, s.id FROM sites s WHERE s.kind = 'hub'
ON CONFLICT (article_id, site_id) DO NOTHING
"""


async def ensure_article_site_settings(session: AsyncSession, article_id: int) -> int:
    """SPEC §5.1 (articles): give a new article one ``article_site_settings`` row per hub.

    Call it right after creating an article (admin form, import). Idempotent: existing rows
    are left untouched. Returns the number of rows inserted. No commit.
    """
    result = await session.execute(text(ENSURE_SITE_SETTINGS_SQL), {"article_id": article_id})
    return result.rowcount or 0  # pyright: ignore[reportAttributeAccessIssue]


async def recalculate(session: AsyncSession, now: datetime) -> RecalculationResult:
    """Full set-based recompute into ``article_site_settings`` (no commit)."""
    result = await session.execute(text(RECALC_SQL), {"now": now})
    counts = (
        await session.execute(
            text(
                "SELECT count(DISTINCT article_id), count(DISTINCT site_id) FROM article_site_settings"
            )
        )
    ).one()
    return RecalculationResult(int(counts[0]), int(counts[1]), now, result.rowcount or 0)  # pyright: ignore[reportAttributeAccessIssue]


#: Demand for items without article (``unmatchedDemand`` in the stock matrix meta).
UNMATCHED_DEMAND_SQL = f"""
SELECT i.requested_type, s.supplying_site_id AS site_id, count(*) AS count
FROM service_request_items i JOIN service_requests s ON s.id = i.service_request_id
WHERE left(i.slot, 4) = 'new_' AND i.article_id IS NULL
  AND (s.status IN ({_in(set(DEMAND_STATUSES))}) OR s.status = 'reschedule_needed')
GROUP BY i.requested_type, s.supplying_site_id
ORDER BY count DESC
"""
