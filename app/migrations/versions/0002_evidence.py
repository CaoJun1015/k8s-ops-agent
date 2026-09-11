"""Add persisted diagnostic evidence.

Revision ID: 0002_evidence
Revises: 0001_initial_domain
"""

from alembic import op
import sqlalchemy as sa


revision = "0002_evidence"
down_revision = "0001_initial_domain"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "evidence",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "incident_id",
            sa.String(36),
            sa.ForeignKey("incidents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "agent_run_id",
            sa.String(36),
            sa.ForeignKey("agent_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("evidence_type", sa.String(32), nullable=False),
        sa.Column("source", sa.String(50), nullable=False),
        sa.Column("content", sa.JSON(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("redacted", sa.Boolean(), nullable=False),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint(
            "agent_run_id",
            "evidence_type",
            "content_hash",
            name="uq_evidence_run_type_hash",
        ),
    )
    op.create_index("ix_evidence_incident_id", "evidence", ["incident_id"])
    op.create_index("ix_evidence_agent_run_id", "evidence", ["agent_run_id"])
    op.create_index(
        "ix_evidence_incident_collected",
        "evidence",
        ["incident_id", "collected_at"],
    )


def downgrade() -> None:
    op.drop_table("evidence")
