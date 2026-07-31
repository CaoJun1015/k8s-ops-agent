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
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ops_agent.database import Base
from ops_agent.domain import (
    AgentRunMode,
    AgentRunStatus,
    ExecutionStatus,
    IncidentSeverity,
    IncidentStatus,
    PlanStatus,
    Priority,
    RiskLevel,
    TaskStatus,
    TaskType,
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
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    tasks: Mapped[list[Task]] = relationship(
        back_populates="incident", cascade="all, delete-orphan"
    )
    plans: Mapped[list[Plan]] = relationship(
        back_populates="incident", cascade="all, delete-orphan"
    )
    agent_runs: Mapped[list[AgentRun]] = relationship(
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
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    incident: Mapped[Incident] = relationship(back_populates="agent_runs")
    executions: Mapped[list[Execution]] = relationship(back_populates="agent_run")


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
