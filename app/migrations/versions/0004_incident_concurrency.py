"""Separate alert occurrence count and enforce one active fingerprint.

Revision ID: 0004_incident_concurrency
Revises: 0003_outbox
"""

from alembic import op
import sqlalchemy as sa


revision = "0004_incident_concurrency"
down_revision = "0003_outbox"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "incidents",
        sa.Column(
            "occurrence_count",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
    )
    op.alter_column("incidents", "occurrence_count", server_default=None)
    op.create_index(
        "uq_active_incident_fingerprint",
        "incidents",
        ["fingerprint"],
        unique=True,
        postgresql_where=sa.text("status NOT IN ('RESOLVED', 'CLOSED')"),
        sqlite_where=sa.text("status NOT IN ('RESOLVED', 'CLOSED')"),
    )


def downgrade() -> None:
    op.drop_index("uq_active_incident_fingerprint", table_name="incidents")
    op.drop_column("incidents", "occurrence_count")
