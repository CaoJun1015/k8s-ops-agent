"""Create the initial Ops Agent domain schema.

Revision ID: 0001_initial_domain
Revises:
"""

from alembic import op
import sqlalchemy as sa

revision = "0001_initial_domain"
down_revision = None
branch_labels = None
depends_on = None


def timestamps():
    return (
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def upgrade() -> None:
    op.create_table(
        "incidents",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("severity", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("fingerprint", sa.String(255), nullable=False),
        sa.Column("cluster", sa.String(100), nullable=False),
        sa.Column("namespace", sa.String(100), nullable=False),
        sa.Column("resource_kind", sa.String(100), nullable=False),
        sa.Column("resource_name", sa.String(253), nullable=False),
        sa.Column("source", sa.String(100), nullable=False),
        sa.Column("summary", sa.Text()),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("version", sa.Integer(), nullable=False),
        *timestamps(),
    )
    op.create_index(
        "ix_incident_fingerprint_status",
        "incidents",
        ["fingerprint", "status"],
    )
    op.create_index("ix_incidents_fingerprint", "incidents", ["fingerprint"])

    op.create_table(
        "tasks",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "incident_id",
            sa.String(36),
            sa.ForeignKey("incidents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("task_type", sa.String(32), nullable=False),
        sa.Column("priority", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("assignee", sa.String(100)),
        sa.Column("created_by", sa.String(100), nullable=False),
        sa.Column("approval_required", sa.Boolean(), nullable=False),
        sa.Column("related_entity_type", sa.String(100)),
        sa.Column("related_entity_id", sa.String(36)),
        sa.Column("due_at", sa.DateTime(timezone=True)),
        *timestamps(),
    )
    op.create_index("ix_tasks_incident_id", "tasks", ["incident_id"])

    op.create_table(
        "plans",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "incident_id",
            sa.String(36),
            sa.ForeignKey("incidents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("action_type", sa.String(100), nullable=False),
        sa.Column("target", sa.JSON(), nullable=False),
        sa.Column("risk_level", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("dry_run_result", sa.JSON()),
        sa.Column("verification_spec", sa.JSON(), nullable=False),
        sa.Column("rollback_spec", sa.JSON(), nullable=False),
        *timestamps(),
    )
    op.create_index("ix_plans_incident_id", "plans", ["incident_id"])

    op.create_table(
        "agent_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "incident_id",
            sa.String(36),
            sa.ForeignKey("incidents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("mode", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("idempotency_key", sa.String(255), unique=True),
        sa.Column("diagnosis", sa.JSON()),
        sa.Column("error", sa.Text()),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        *timestamps(),
    )
    op.create_index("ix_agent_runs_incident_id", "agent_runs", ["incident_id"])
    op.create_index(
        "ix_agent_runs_idempotency_key",
        "agent_runs",
        ["idempotency_key"],
        unique=True,
    )

    op.create_table(
        "executions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "plan_id",
            sa.String(36),
            sa.ForeignKey("plans.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "agent_run_id",
            sa.String(36),
            sa.ForeignKey("agent_runs.id", ondelete="SET NULL"),
        ),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("idempotency_key", sa.String(255), unique=True, nullable=False),
        sa.Column("requested_by", sa.String(100), nullable=False),
        sa.Column("approved_by", sa.String(100)),
        sa.Column("approved_at", sa.DateTime(timezone=True)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("result", sa.JSON()),
        sa.Column("error", sa.Text()),
        sa.Column("verification_result", sa.JSON()),
        sa.Column("rollback_result", sa.JSON()),
        *timestamps(),
    )
    op.create_index("ix_executions_plan_id", "executions", ["plan_id"])
    op.create_index(
        "ix_executions_agent_run_id", "executions", ["agent_run_id"]
    )

    op.create_table(
        "audit_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("entity_type", sa.String(100), nullable=False),
        sa.Column("entity_id", sa.String(36), nullable=False),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("actor_type", sa.String(50), nullable=False),
        sa.Column("actor_id", sa.String(100), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_audit_entity",
        "audit_events",
        ["entity_type", "entity_id", "occurred_at"],
    )


def downgrade() -> None:
    op.drop_table("audit_events")
    op.drop_table("executions")
    op.drop_table("agent_runs")
    op.drop_table("plans")
    op.drop_table("tasks")
    op.drop_table("incidents")
