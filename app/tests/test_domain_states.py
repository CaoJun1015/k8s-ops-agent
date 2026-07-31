"""领域状态机测试。

场景覆盖：
- 正常状态转换可以执行；
- 跳过审批等非法转换被拒绝；
- 终态不能被重新打开；
- Task、Plan、Execution 和 AgentRun 使用各自独立的状态机。
"""

import pytest

from ops_agent.domain import (
    AgentRunStatus,
    ExecutionStatus,
    IncidentStatus,
    InvalidStateTransition,
    PlanStatus,
    TaskStatus,
    validate_transition,
)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (IncidentStatus.OPEN, IncidentStatus.DIAGNOSING),
        (IncidentStatus.DIAGNOSING, IncidentStatus.DIAGNOSED),
        (IncidentStatus.DIAGNOSED, IncidentStatus.PLAN_READY),
        (IncidentStatus.VERIFYING, IncidentStatus.RESOLVED),
        (IncidentStatus.RESOLVED, IncidentStatus.CLOSED),
        (IncidentStatus.FAILED, IncidentStatus.DIAGNOSING),
    ],
)
def test_incident_allows_defined_transitions(current, target):
    """已在设计中声明的 Incident 转换应被接受。"""
    validate_transition(current, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (IncidentStatus.OPEN, IncidentStatus.REMEDIATING),
        (IncidentStatus.DIAGNOSING, IncidentStatus.RESOLVED),
        (IncidentStatus.CLOSED, IncidentStatus.OPEN),
    ],
)
def test_incident_rejects_unsafe_or_terminal_transitions(current, target):
    """不能绕过诊断/审批，且关闭后的事件不能直接重开。"""
    with pytest.raises(InvalidStateTransition):
        validate_transition(current, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (TaskStatus.TODO, TaskStatus.IN_PROGRESS),
        (TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED),
        (TaskStatus.BLOCKED, TaskStatus.IN_PROGRESS),
        (TaskStatus.IN_PROGRESS, TaskStatus.DONE),
        (PlanStatus.DRAFT, PlanStatus.DRY_RUN_READY),
        (PlanStatus.AWAITING_APPROVAL, PlanStatus.APPROVED),
        (ExecutionStatus.PREPARED, ExecutionStatus.RUNNING),
        (ExecutionStatus.FAILED, ExecutionStatus.ROLLED_BACK),
        (AgentRunStatus.QUEUED, AgentRunStatus.COLLECTING),
        (AgentRunStatus.DIAGNOSING, AgentRunStatus.COMPLETED),
    ],
)
def test_other_domain_state_machines_allow_defined_transitions(current, target):
    """各领域对象只能按各自状态机推进。"""
    validate_transition(current, target)


def test_transition_between_different_state_machine_types_is_rejected():
    """不同实体的枚举不能混用，避免把 Task 状态写入 Incident。"""
    with pytest.raises(InvalidStateTransition):
        validate_transition(TaskStatus.TODO, IncidentStatus.DIAGNOSING)
