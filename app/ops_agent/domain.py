"""Pure domain enums and state transition rules."""

from enum import StrEnum


class InvalidStateTransition(ValueError):
    """Raised when a domain entity attempts an undefined transition."""


class IncidentSeverity(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class IncidentStatus(StrEnum):
    OPEN = "OPEN"
    DIAGNOSING = "DIAGNOSING"
    DIAGNOSED = "DIAGNOSED"
    PLAN_READY = "PLAN_READY"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    REMEDIATING = "REMEDIATING"
    VERIFYING = "VERIFYING"
    RESOLVED = "RESOLVED"
    CLOSED = "CLOSED"
    FAILED = "FAILED"


class TaskStatus(StrEnum):
    TODO = "TODO"
    IN_PROGRESS = "IN_PROGRESS"
    BLOCKED = "BLOCKED"
    DONE = "DONE"
    CANCELLED = "CANCELLED"


class TaskType(StrEnum):
    MANUAL_CHECK = "MANUAL_CHECK"
    APPROVAL = "APPROVAL"
    REMEDIATION = "REMEDIATION"
    VERIFICATION = "VERIFICATION"
    FOLLOW_UP = "FOLLOW_UP"


class Priority(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class PlanStatus(StrEnum):
    DRAFT = "DRAFT"
    DRY_RUN_READY = "DRY_RUN_READY"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    APPROVED = "APPROVED"
    EXECUTABLE = "EXECUTABLE"
    EXECUTED = "EXECUTED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


class RiskLevel(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class ExecutionStatus(StrEnum):
    PREPARED = "PREPARED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"
    ROLLED_BACK = "ROLLED_BACK"


class AgentRunStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COLLECTING = "COLLECTING"
    DIAGNOSING = "DIAGNOSING"
    COMPLETED = "COMPLETED"
    AWAITING_HUMAN = "AWAITING_HUMAN"
    STOPPED = "STOPPED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class AgentRunMode(StrEnum):
    DIAGNOSE_ONLY = "DIAGNOSE_ONLY"


class AgentStepType(StrEnum):
    CALL_TOOL = "CALL_TOOL"
    COMPLETE = "COMPLETE"
    ASK_HUMAN = "ASK_HUMAN"
    STOP = "STOP"


class AgentStepStatus(StrEnum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    DENIED = "DENIED"


class ToolInvocationStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    DENIED = "DENIED"


class PolicyDecision(StrEnum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    ASK_HUMAN = "ASK_HUMAN"


class EvidenceType(StrEnum):
    ALERT_PAYLOAD = "ALERT_PAYLOAD"
    POD_STATUS = "POD_STATUS"
    CURRENT_LOGS = "CURRENT_LOGS"
    PREVIOUS_LOGS = "PREVIOUS_LOGS"
    K8S_EVENTS = "K8S_EVENTS"
    WORKLOAD_STATUS = "WORKLOAD_STATUS"
    RESOURCE_LIMITS = "RESOURCE_LIMITS"
    PROMETHEUS_METRICS = "PROMETHEUS_METRICS"
    REDIS_STATUS = "REDIS_STATUS"
    COLLECTION_ERROR = "COLLECTION_ERROR"


class OutboxStatus(StrEnum):
    PENDING = "PENDING"
    PUBLISHED = "PUBLISHED"
    FAILED = "FAILED"


TRANSITIONS = {
    IncidentStatus: {
        IncidentStatus.OPEN: {
            IncidentStatus.DIAGNOSING,
            IncidentStatus.VERIFYING,
            IncidentStatus.CLOSED,
        },
        IncidentStatus.DIAGNOSING: {
            IncidentStatus.DIAGNOSED,
            IncidentStatus.FAILED,
            IncidentStatus.VERIFYING,
            IncidentStatus.CLOSED,
        },
        IncidentStatus.DIAGNOSED: {
            IncidentStatus.PLAN_READY,
            IncidentStatus.DIAGNOSING,
            IncidentStatus.VERIFYING,
            IncidentStatus.CLOSED,
        },
        IncidentStatus.PLAN_READY: {
            IncidentStatus.AWAITING_APPROVAL,
            IncidentStatus.DIAGNOSED,
            IncidentStatus.VERIFYING,
            IncidentStatus.CLOSED,
        },
        IncidentStatus.AWAITING_APPROVAL: {
            IncidentStatus.REMEDIATING,
            IncidentStatus.DIAGNOSED,
            IncidentStatus.VERIFYING,
            IncidentStatus.CLOSED,
        },
        IncidentStatus.REMEDIATING: {
            IncidentStatus.VERIFYING,
            IncidentStatus.FAILED,
        },
        IncidentStatus.VERIFYING: {
            IncidentStatus.RESOLVED,
            IncidentStatus.FAILED,
        },
        IncidentStatus.RESOLVED: {IncidentStatus.CLOSED},
        IncidentStatus.FAILED: {
            IncidentStatus.DIAGNOSING,
            IncidentStatus.VERIFYING,
            IncidentStatus.CLOSED,
        },
        IncidentStatus.CLOSED: set(),
    },
    TaskStatus: {
        TaskStatus.TODO: {
            TaskStatus.IN_PROGRESS,
            TaskStatus.BLOCKED,
            TaskStatus.CANCELLED,
        },
        TaskStatus.IN_PROGRESS: {
            TaskStatus.DONE,
            TaskStatus.BLOCKED,
            TaskStatus.CANCELLED,
        },
        TaskStatus.BLOCKED: {
            TaskStatus.IN_PROGRESS,
            TaskStatus.CANCELLED,
        },
        TaskStatus.DONE: set(),
        TaskStatus.CANCELLED: set(),
    },
    PlanStatus: {
        PlanStatus.DRAFT: {PlanStatus.DRY_RUN_READY, PlanStatus.CANCELLED},
        PlanStatus.DRY_RUN_READY: {
            PlanStatus.AWAITING_APPROVAL,
            PlanStatus.CANCELLED,
        },
        PlanStatus.AWAITING_APPROVAL: {
            PlanStatus.APPROVED,
            PlanStatus.REJECTED,
            PlanStatus.CANCELLED,
        },
        PlanStatus.APPROVED: {PlanStatus.EXECUTABLE, PlanStatus.CANCELLED},
        PlanStatus.EXECUTABLE: {PlanStatus.EXECUTED},
        PlanStatus.EXECUTED: set(),
        PlanStatus.REJECTED: set(),
        PlanStatus.CANCELLED: set(),
    },
    ExecutionStatus: {
        ExecutionStatus.PREPARED: {
            ExecutionStatus.RUNNING,
            ExecutionStatus.FAILED,
        },
        ExecutionStatus.RUNNING: {
            ExecutionStatus.SUCCEEDED,
            ExecutionStatus.FAILED,
        },
        ExecutionStatus.SUCCEEDED: {ExecutionStatus.VERIFICATION_FAILED},
        ExecutionStatus.FAILED: {ExecutionStatus.ROLLED_BACK},
        ExecutionStatus.VERIFICATION_FAILED: {ExecutionStatus.ROLLED_BACK},
        ExecutionStatus.ROLLED_BACK: set(),
    },
    AgentRunStatus: {
        AgentRunStatus.QUEUED: {
            AgentRunStatus.RUNNING,
            AgentRunStatus.COLLECTING,
            AgentRunStatus.FAILED,
            AgentRunStatus.CANCELLED,
        },
        AgentRunStatus.RUNNING: {
            AgentRunStatus.COMPLETED,
            AgentRunStatus.AWAITING_HUMAN,
            AgentRunStatus.STOPPED,
            AgentRunStatus.FAILED,
            AgentRunStatus.CANCELLED,
        },
        AgentRunStatus.COLLECTING: {
            AgentRunStatus.DIAGNOSING,
            AgentRunStatus.FAILED,
        },
        AgentRunStatus.DIAGNOSING: {
            AgentRunStatus.COMPLETED,
            AgentRunStatus.FAILED,
        },
        AgentRunStatus.COMPLETED: set(),
        AgentRunStatus.AWAITING_HUMAN: {AgentRunStatus.CANCELLED},
        AgentRunStatus.STOPPED: set(),
        AgentRunStatus.FAILED: set(),
        AgentRunStatus.CANCELLED: set(),
    },
}


def validate_transition(current: StrEnum, target: StrEnum) -> None:
    """Validate a transition without mutating an entity."""
    if type(current) is not type(target):
        raise InvalidStateTransition(
            f"state machine mismatch: {type(current).__name__} -> {type(target).__name__}"
        )
    allowed = TRANSITIONS.get(type(current), {}).get(current, set())
    if target not in allowed:
        raise InvalidStateTransition(f"invalid transition: {current} -> {target}")
