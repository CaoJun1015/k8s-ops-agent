"""Add v0.3 Agent Core persistence.

Revision ID: 0006_agent_core
Revises: 0005_incident_source_context
"""

from alembic import op
import sqlalchemy as sa


revision = "0006_agent_core"
down_revision = "0005_incident_source_context"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = [
        sa.Column("goal", sa.String(500), nullable=False, server_default="Diagnose the incident from read-only evidence"),
        sa.Column("autonomy_level", sa.String(10), nullable=False, server_default="L2"),
        sa.Column("target_snapshot", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("current_step", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_steps", sa.Integer(), nullable=False, server_default="8"),
        sa.Column("max_tool_calls", sa.Integer(), nullable=False, server_default="6"),
        sa.Column("tool_calls_used", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("per_tool_timeout_seconds", sa.Integer(), nullable=False, server_default="15"),
        sa.Column("run_timeout_seconds", sa.Integer(), nullable=False, server_default="120"),
        sa.Column("max_model_calls", sa.Integer(), nullable=False, server_default="4"),
        sa.Column("model_calls_used", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_total_tokens", sa.Integer(), nullable=False, server_default="20000"),
        sa.Column("input_tokens_used", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens_used", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("deadline_at", sa.DateTime(timezone=True)),
        sa.Column("stop_reason", sa.String(100)),
        sa.Column("model_name", sa.String(100)),
        sa.Column("prompt_version", sa.String(50), nullable=False, server_default="agent-context-v1"),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True)),
        sa.Column("lease_owner", sa.String(100)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True)),
    ]
    for column in columns:
        op.add_column("agent_runs", column)

    op.create_table(
        "agent_steps",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("agent_run_id", sa.String(36), sa.ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("step_type", sa.String(32)),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("decision_provider", sa.String(50)),
        sa.Column("decision_summary", sa.String(500)),
        sa.Column("confidence", sa.Float()),
        sa.Column("evidence_ids", sa.JSON(), nullable=False),
        sa.Column("context_version", sa.String(50), nullable=False),
        sa.Column("context_snapshot", sa.JSON(), nullable=False),
        sa.Column("context_hash", sa.String(64)),
        sa.Column("error_code", sa.String(100)),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("agent_run_id", "sequence", name="uq_agent_step_sequence"),
        sa.UniqueConstraint("idempotency_key", name="uq_agent_step_idempotency"),
    )
    op.create_index("ix_agent_steps_agent_run_id", "agent_steps", ["agent_run_id"])
    op.create_table(
        "tool_invocations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("agent_step_id", sa.String(36), sa.ForeignKey("agent_steps.id", ondelete="CASCADE"), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("tool_name", sa.String(100), nullable=False),
        sa.Column("tool_version", sa.String(20), nullable=False),
        sa.Column("sanitized_arguments", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("evidence_id", sa.String(36), sa.ForeignKey("evidence.id", ondelete="SET NULL")),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("duration_ms", sa.Integer()),
        sa.Column("error_code", sa.String(100)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("agent_step_id", name="uq_tool_invocation_step"),
        sa.UniqueConstraint("idempotency_key", name="uq_tool_invocation_idempotency"),
    )
    op.create_index("ix_tool_invocations_agent_step_id", "tool_invocations", ["agent_step_id"])
    op.create_index("ix_tool_invocations_evidence_id", "tool_invocations", ["evidence_id"])


def downgrade() -> None:
    op.drop_table("tool_invocations")
    op.drop_table("agent_steps")
    for name in [
        "heartbeat_at", "lease_expires_at", "lease_owner", "cancel_requested_at",
        "prompt_version", "model_name", "stop_reason", "deadline_at",
        "output_tokens_used", "input_tokens_used", "max_total_tokens",
        "model_calls_used", "max_model_calls", "run_timeout_seconds", "per_tool_timeout_seconds",
        "tool_calls_used", "max_tool_calls", "max_steps", "current_step",
        "target_snapshot", "autonomy_level", "goal",
    ]:
        op.drop_column("agent_runs", name)
