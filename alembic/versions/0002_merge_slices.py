"""merge slice branches

Joins the slice branches (SPEC §2.6) into a single head. QA re-creates this
revision (`alembic merge heads --rev-id 0002_merge_slices`) whenever another
slice branch lands, so `alembic heads` always shows exactly one head.

Revision ID: 0002_merge_slices
Revises: h2_0001_imports, h3_0001_staging, h4_0001_field, h5_0001_stock
Create Date: 2026-10-13 12:51:54.705605+00:00
"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "0002_merge_slices"
down_revision: str | Sequence[str] | None = (
    "h2_0001_imports",
    "h3_0001_staging",
    "h4_0001_field",
    "h5_0001_stock",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
