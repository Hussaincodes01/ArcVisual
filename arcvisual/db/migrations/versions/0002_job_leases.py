"""Job leases, for advancing a job through many short serverless invocations.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-04

A job used to run start to finish inside one long-lived worker. On a serverless
platform it is instead advanced in bounded steps, and two steps for the same job can
arrive concurrently. ``lease_until`` / ``lease_owner`` are the compare-and-set that
keeps them from doing the same work twice.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("lease_until", sa.DateTime(timezone=True)))
    op.add_column("jobs", sa.Column("lease_owner", sa.String(32)))


def downgrade() -> None:
    op.drop_column("jobs", "lease_owner")
    op.drop_column("jobs", "lease_until")
