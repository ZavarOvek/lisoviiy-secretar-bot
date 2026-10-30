"""forest_map.position: legend order of the map

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-30 18:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Рядки, імпортовані до цієї міграції, отримують 0 і лишаються за алфавітом, доки карту
    # не імпортують знову: порядку легенди в базі для них просто немає.
    op.add_column(
        "forest_map",
        sa.Column("position", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("forest_map", "position")
