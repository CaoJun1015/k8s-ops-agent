"""SQLAlchemy persistence models for the Ops Agent domain."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ops_agent.database import Base
from ops_agent.domain import (
    AgentRunMode,
    AgentRunStatus,
    AgentStepStatus,
    AgentStepType,
    EvidenceType,
    ExecutionStatus,
    IncidentSeverity,
    IncidentStatus,
    OutboxStatus,
    PlanStatus,
    Priority,
    RiskLevel,
    TaskStatus,
    TaskType,
    ToolInvocationStatus,
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return str(uuid4())


def enum_column(enum_type: type, default: Any):
    return mapped_column(
        Enum(enum_type, native_enum=False, length=32),
        default=default,
        nullable=False,
    )


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class Incident(TimestampMixin, Base):
    __tablename__ = "incidents"
    __table_args__ = (
        Index("ix_incident_fingerprint_status", "fingerprint", "status"),
        Index(
            "uq_active_incident_fingerprint",
            "fingerprint",
            unique=True,
            postgresql_where=text("status NOT IN ('RESOLVED', 'CLOSED')"),
            sqlite_where=text("status NOT IN ('RESOLVED', 'CLOSED')"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    severity: Mapped[IncidentSeverity] = enum_column(
        IncidentSeverity, IncidentSeverity.MEDIUM
    )
    status: Mapped[IncidentStatus] = enum_column(
        IncidentStatus, IncidentStatus.OPEN
    )
    fingerprint: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    cluster: Mapped[str] = mapped_column(String(100), default="default", nullable=False)
    namespace: Mapped[str] = mapped_column(String(100), nullable=False)
    resource_kind: Mapped[str] = mapped_column(String(100), nullable=False)
    resource_name: Mapped[str] = mapped_column(String(253), nullable=False)
    source: Mapped[str] = mapped_column(String(100), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    source_context: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict, nullable=False
    )
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    occurrence_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    __mapper_args__ = {"version_id_col": version}

    tasks: Mapped[list[Task]] = relationship(
        back_populates="incident", cascade="all, delete-orphan"
    )
    plans: Mapped[list[Plan]] = relationship(
        back_populates="incident", cascade="all, delete-orphan"
    )
    agent_runs: Mapped[list[AgentRun]] = relationship(
        back_populates="incident", cascade="all, delete-orphan"
    )
    evidence: Mapped[list[Evidence]] = relationship(
        back_populates="incident", cascade="all, delete-orphan"
    )


class Task(TimestampMixin, Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    incident_id: Mapped[str] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    task_type: Mapped[TaskType] = enum_column(TaskType, TaskType.MANUAL_CHECK)
    priority: Mapped[Priority] = enum_column(Priority, Priority.MEDIUM)
    status: Mapped[TaskStatus] = enum_column(TaskStatus, TaskStatus.TODO)
    assignee: Mapped[str | None] = mapped_column(String(100))
    created_by: Mapped[str] = mapped_column(String(100), default="user", nullable=False)
    approval_required: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    related_entity_type: Mapped[str | None] = mapped_column(String(100))
    related_entity_id: Mapped[str | None] = mapped_column(String(36))
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    incident: Mapped[Incident] = relationship(back_populates="tasks")


class Plan(TimestampMixin, Base):
    __tablename__ = "plans"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    incident_id: Mapped[str] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    action_type: Mapped[str] = mapped_column(String(100), nullable=False)
    target: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    risk_level: Mapped[RiskLevel] = enum_column(RiskLevel, RiskLevel.LOW)
    status: Mapped[PlanStatus] = enum_column(PlanStatus, PlanStatus.DRAFT)
    dry_run_result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    verification_spec: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict, nullable=False
    )
    rollback_spec: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict, nullable=False
    )

    incident: Mapped[Incident] = relationship(back_populates="plans")
    executions: Mapped[list[Execution]] = relationship(
        back_populates="plan", cascade="all, delete-orphan"
    )


class AgentRun(TimestampMixin, Base):
    __tablename__ = "agent_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    incident_id: Mapped[str] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    mode: Mapped[AgentRunMode] = enum_column(
        AgentRunMode, AgentRunMode.DIAGNOSE_ONLY
    )
    status: Mapped[AgentRunStatus] = enum_column(
        AgentRunStatus, AgentRunStatus.QUEUED
    )
    idempotency_key: Mapped[str | None] = mapped_column(
        String(255), unique=True, index=True
    )
    diagnosis: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    goal: Mapped[str] = mapped_column(
        String(500), default="Diagnose the incident from read-only evidence", nullable=False
    )
    autonomy_level: Mapped[str] = mapped_column(
        String(10), default="L2", nullable=False
    )
    target_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict, nullable=False
    )
    current_step: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_steps: Mapped[int] = mapped_column(Integer, default=8, nullable=False)
    max_tool_calls: Mapped[int] = mapped_column(Integer, default=6, nullable=False)
    tool_calls_used: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    per_tool_timeout_seconds: Mapped[int] = mapped_column(
        Integer, default=15, nullable=False
    )
    run_timeout_seconds: Mapped[int] = mapped_column(
        Integer, default=120, nullable=False
    )
    max_model_calls: Mapped[int] = mapped_column(Integer, default=4, nullable=False)
    model_calls_used: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_total_tokens: Mapped[int] = mapped_column(Integer, default=20000, nullable=False)
    input_tokens_used: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    output_tokens_used: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stop_reason: Mapped[str | None] = mapped_column(String(100))
    model_name: Mapped[str | None] = mapped_column(String(100))
    prompt_version: Mapped[str] = mapped_column(
        String(50), default="agent-context-v1", nullable=False
    )
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_owner: Mapped[str | None] = mapped_column(String(100))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    incident: Mapped[Incident] = relationship(back_populates="agent_runs")
    executions: Mapped[list[Execution]] = relationship(back_populates="agent_run")
    evidence: Mapped[list[Evidence]] = relationship(
        back_populates="agent_run", cascade="all, delete-orphan"
    )
    steps: Mapped[list[AgentStep]] = relationship(
        back_populates="agent_run",
        cascade="all, delete-orphan",
        order_by="AgentStep.sequence",
    )


class AgentStep(Base):
    __tablename__ = "agent_steps"
    __table_args__ = (
        UniqueConstraint("agent_run_id", "sequence", name="uq_agent_step_sequence"),
        UniqueConstraint("idempotency_key", name="uq_agent_step_idempotency"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    agent_run_id: Mapped[str] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    step_type: Mapped[AgentStepType | None] = mapped_column(
        Enum(AgentStepType, native_enum=False, length=32)
    )
    status: Mapped[AgentStepStatus] = enum_column(
        AgentStepStatus, AgentStepStatus.RUNNING
    )
    decision_provider: Mapped[str | None] = mapped_column(String(50))
    decision_summary: Mapped[str | None] = mapped_column(String(500))
    confidence: Mapped[float | None] = mapped_column()
    evidence_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    context_version: Mapped[str] = mapped_column(
        String(50), default="agent-context-v1", nullable=False
    )
    context_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict, nullable=False
    )
    context_hash: Mapped[str | None] = mapped_column(String(64))
    error_code: Mapped[str | None] = mapped_column(String(100))
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    agent_run: Mapped[AgentRun] = relationship(back_populates="steps")
    tool_invocation: Mapped[ToolInvocation | None] = relationship(
        back_populates="agent_step", cascade="all, delete-orphan", uselist=False
    )


class ToolInvocation(Base):
    __tablename__ = "tool_invocations"
    __table_args__ = (
        UniqueConstraint("agent_step_id", name="uq_tool_invocation_step"),
        UniqueConstraint("idempotency_key", name="uq_tool_invocation_idempotency"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    agent_step_id: Mapped[str] = mapped_column(
        ForeignKey("agent_steps.id", ondelete="CASCADE"), nullable=False, index=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(100), nullable=False)
    tool_version: Mapped[str] = mapped_column(String(20), nullable=False)
    sanitized_arguments: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict, nullable=False
    )
    status: Mapped[ToolInvocationStatus] = enum_column(
        ToolInvocationStatus, ToolInvocationStatus.PENDING
    )
    evidence_id: Mapped[str | None] = mapped_column(
        ForeignKey("evidence.id", ondelete="SET NULL"), index=True
    )
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(100))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    agent_step: Mapped[AgentStep] = relationship(back_populates="tool_invocation")
    evidence: Mapped[Evidence | None] = relationship()


class Evidence(Base):
    __tablename__ = "evidence"
    __table_args__ = (
        UniqueConstraint(
            "agent_run_id",
            "evidence_type",
            "content_hash",
            name="uq_evidence_run_type_hash",
        ),
        Index("ix_evidence_incident_collected", "incident_id", "collected_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    incident_id: Mapped[str] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    agent_run_id: Mapped[str] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    evidence_type: Mapped[EvidenceType] = mapped_column(
        Enum(EvidenceType, native_enum=False, length=32), nullable=False
    )
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    content: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    redacted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    collected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    incident: Mapped[Incident] = relationship(back_populates="evidence")
    agent_run: Mapped[AgentRun] = relationship(back_populates="evidence")


class Execution(TimestampMixin, Base):
    __tablename__ = "executions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    plan_id: Mapped[str] = mapped_column(
        ForeignKey("plans.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    agent_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="SET NULL"), index=True
    )
    status: Mapped[ExecutionStatus] = enum_column(
        ExecutionStatus, ExecutionStatus.PREPARED
    )
    idempotency_key: Mapped[str] = mapped_column(
        String(255), unique=True, nullable=False
    )
    requested_by: Mapped[str] = mapped_column(String(100), nullable=False)
    approved_by: Mapped[str | None] = mapped_column(String(100))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)
    verification_result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    rollback_result: Mapped[dict[str, Any] | None] = mapped_column(JSON)

    plan: Mapped[Plan] = relationship(back_populates="executions")
    agent_run: Mapped[AgentRun | None] = relationship(back_populates="executions")


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_entity", "entity_type", "entity_id", "occurred_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    entity_type: Mapped[str] = mapped_column(String(100), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(36), nullable=False)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    actor_type: Mapped[str] = mapped_column(String(50), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(100), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class OutboxEvent(TimestampMixin, Base):
    __tablename__ = "outbox_events"
    __table_args__ = (
        Index("ix_outbox_delivery", "status", "available_at", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    topic: Mapped[str] = mapped_column(String(100), nullable=False)
    aggregate_type: Mapped[str] = mapped_column(String(100), nullable=False)
    aggregate_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[OutboxStatus] = enum_column(
        OutboxStatus, OutboxStatus.PENDING
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
