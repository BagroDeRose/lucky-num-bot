"""add optional users.birth_date for personalized analyses

Additive and nullable on purpose: existing rows keep NULL, so users created
before the birth-date feature keep working and simply get the unchanged
serial-only analysis until they choose to provide a date. No existing data
is read, rewritten or backfilled by this migration.

Revision ID: b7c41d2e9f08
Revises: 24944e1e5e5f
Create Date: 2026-09-16

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'b7c41d2e9f08'
down_revision: str | None = '24944e1e5e5f'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('users', sa.Column('birth_date', sa.Date(), nullable=True))


def downgrade() -> None:
    op.drop_column('users', 'birth_date')
