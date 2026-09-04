"""Persist sanitized source context for evidence collection.

Revision ID: 0005_incident_source_context
Revises: 0004_incident_concurrency
"""

from alembic import op
import sqlalchemy as sa


revision = "0005_incident_source_context"
down_revision = "0004_incident_concurrency"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "incidents",
        sa.Column(
            "source_context",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )
    with op.batch_alter_table("incidents") as batch:
        batch.alter_column("source_context", server_default=None)


def downgrade() -> None:
    op.drop_column("incidents", "source_context")
