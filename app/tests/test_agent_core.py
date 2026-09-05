"""v0.3 Agent Core scenarios.

Scenarios cover a multi-step read-only investigation, strict tool/decision
contracts, budget stopping, cancellation, prompt-injection rejection and the
legacy feature-flag fallback.
"""

from types import SimpleNamespace

import pytest

from app import create_app
from ops_agent.agent_core import (
    AgentStepType,
    DecisionError,
    FallbackReasoner,
    NextDecision,
    RuleReasoner,
    validate_next_decision,
)
from ops_agent.tooling import ToolDefinition, ToolError, ToolRegistry, ToolResult
from ops_agent.domain import EvidenceType


class ReadOnlyFakeKubernetes:
    def __init__(self):
        self.calls = []

    def get_pod_status(self, **kwargs):
        self.calls.append(("get_pod_status", kwargs))
        return {
            "exists": True,
            "uid": "pod-uid-1",
            "phase": "Running",
            "ready": False,
            "containers": [
                {
                    "name": "demo",
                    "ready": False,
                    "restart_count": 4,
                    "waiting_reason": "CrashLoopBackOff",
                    "last_exit_code": 1,
                }
            ],
        }

    def get_pod_logs(self, **kwargs):
        self.calls.append(("get_previous_logs" if kwargs["previous"] else "get_pod_logs", kwargs))
        return {"uid": "pod-uid-1", "logs": {"demo": "startup failed"}, "previous": kwargs["previous"]}

    def get_kubernetes_events(self, **kwargs):
        self.calls.append(("get_kubernetes_events", kwargs))
        return {"resource_uid": kwargs["resource_uid"], "items": [{"reason": "BackOff", "message": "back-off restarting"}]}

    def get_resource_limits(self, **kwargs):
        return {"uid": "pod-uid-1", "containers": {"demo": {"limits": {"memory": "64Mi"}}}}

    def get_workload_status(self, **kwargs):
        return {"kind": kwargs["resource_kind"], "name": kwargs["resource_name"], "desired_replicas": 1, "ready_replicas": 0}

    def list_related_pods(self, **kwargs):
        return {"items": []}

    def get_rollout_history(self, **kwargs):
        return {"items": []}


@pytest.fixture()
def agent_app():
    adapter = ReadOnlyFakeKubernetes()
    application = create_app(
        {
            "TESTING": True,
            "DATABASE_URL": "sqlite+pysqlite:///:memory:",
            "QUEUE_MODE": "inline",
            "AGENT_CORE_ENABLED": True,
            "AGENT_REASONER_PROVIDER": "rules",
            "DIAGNOSIS_PROVIDER": "rules",
            "KUBERNETES_ADAPTER": adapter,
        }
    )
    application.extensions["test_agent_adapter"] = adapter
    return application


def create_pod_incident(client):
    return client.post(
        "/api/incidents",
        json={
            "title": "demo CrashLoopBackOff",
            "severity": "HIGH",
            "fingerprint": "agent-core-crashloop",
            "namespace": "default",
            "resource_kind": "Pod",
            "resource_name": "demo-crashloop-1",
            "source": "test",
            "summary": "container restarts",
        },
    ).get_json()


def test_agent_core_selects_multiple_tools_and_persists_timeline(agent_app):
    client = agent_app.test_client()
    incident = create_pod_incident(client)

    accepted = client.post(
        f"/api/incidents/{incident['id']}/agent-runs",
        json={"goal": "定位崩溃根因"},
        headers={"Idempotency-Key": "agent-core-once"},
    )

    assert accepted.status_code == 202
    run = client.get(accepted.get_json()["location"]).get_json()
    steps = client.get(run["steps_url"]).get_json()
    evidence = client.get(f"/api/agent-runs/{run['id']}/evidence").get_json()
    assert run["status"] == "COMPLETED"
    assert run["diagnosis"]["diagnosis_code"] == "CRASH_LOOP"
    assert [step["sequence"] for step in steps] == [1, 2, 3, 4]
    assert [
        step["tool_invocation"]["tool_name"]
        for step in steps
        if step["tool_invocation"]
    ] == ["get_pod_status", "get_previous_logs", "get_kubernetes_events"]
    assert set(run["diagnosis"]["evidence_ids"]).issubset({item["id"] for item in evidence})
    assert run["budget"]["tool_calls_used"] == 3
    assert all(step["context_hash"] for step in steps)
    assert all(
        link["url"] == f"/api/agent-runs/{run['id']}/evidence"
        for step in steps
        for link in step["evidence_links"]
    )
    assert all(
        step["tool_invocation"]["evidence_url"]
        == f"/api/agent-runs/{run['id']}/evidence"
        for step in steps
        if step["tool_invocation"]
    )


def test_step_budget_stops_loop_without_write_action(agent_app):
    client = agent_app.test_client()
    incident = create_pod_incident(client)
    accepted = client.post(
        f"/api/incidents/{incident['id']}/agent-runs",
        json={"budget": {"max_steps": 1}},
    )
    run = client.get(accepted.get_json()["location"]).get_json()
    assert run["status"] == "STOPPED"
    assert run["stop_reason"] == "MAX_STEPS_EXHAUSTED"
    assert run["budget"]["tool_calls_used"] == 1


def test_queued_run_can_be_cancelled_idempotently(agent_app):
    agent_app.config["QUEUE_MODE"] = "rq"
    client = agent_app.test_client()
    incident = create_pod_incident(client)
    accepted = client.post(f"/api/incidents/{incident['id']}/agent-runs")
    location = accepted.get_json()["location"]
    first = client.post(f"{location}/cancel")
    second = client.post(f"{location}/cancel")
    assert first.status_code == second.status_code == 202
    assert first.get_json()["status"] == second.get_json()["status"] == "CANCELLED"


def test_registry_rejects_write_tools_and_invalid_arguments_before_handler():
    registry = ToolRegistry()
    called = []
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
    }
    with pytest.raises(ValueError, match="read-only"):
        registry.register(
            ToolDefinition("delete_pod", "1", "write", schema, {"type": "object"}, False, "HIGH", True, 5, frozenset({"default"}), frozenset({"default"}), lambda *_: None)
        )
    registry.register(
        ToolDefinition(
            "read_one",
            "1",
            "read",
            schema,
            {"type": "object"},
            True,
            "LOW",
            False,
            5,
            frozenset({"default"}),
            frozenset({"default"}),
            lambda arguments, timeout: called.append(arguments) or ToolResult(EvidenceType.POD_STATUS, "test", {}),
        )
    )
    with pytest.raises(ToolError) as invalid:
        registry.invoke("read_one", {"unexpected": "x"}, 5)
    assert invalid.value.code == "INVALID_TOOL_ARGUMENTS"
    assert called == []


def test_decision_rejects_prompt_injection_and_unknown_evidence(agent_app):
    registry = agent_app.extensions["agent_registry"]
    injection = NextDecision(
        AgentStepType.CALL_TOOL,
        "get_pod_status",
        {"cluster": "default", "namespace": "default", "pod_name": "x"},
        "run kubectl delete pod x",
        [],
        0.5,
    )
    with pytest.raises(DecisionError) as rejected:
        validate_next_decision(injection, registry=registry, visible_evidence_ids=set())
    assert rejected.value.code == "EXECUTABLE_CONTENT_REJECTED"


def test_model_failure_falls_back_to_rules():
    class BrokenModel:
        def decide(self, *_args):
            raise DecisionError("MODEL_RESPONSE_INVALID", "bad model output")

    fallback = FallbackReasoner(BrokenModel(), RuleReasoner())
    incident = SimpleNamespace(
        resource_kind="Service",
        title="unknown",
        summary="",
        cluster="default",
        namespace="default",
        resource_name="api",
    )
    decision = fallback.decide({"history": []}, incident, [], ToolRegistry())
    assert decision.decision_type == AgentStepType.ASK_HUMAN
    assert decision.fallback_error_code == "MODEL_RESPONSE_INVALID"


def test_budget_validation_rejects_unknown_or_out_of_range_values(agent_app):
    client = agent_app.test_client()
    incident = create_pod_incident(client)
    too_large = client.post(
        f"/api/incidents/{incident['id']}/agent-runs",
        json={"budget": {"max_steps": 17}},
    )
    unknown = client.post(
        f"/api/incidents/{incident['id']}/agent-runs",
        json={"budget": {"dollars": 1}},
    )
    assert too_large.status_code == unknown.status_code == 400


def test_duplicate_worker_delivery_does_not_repeat_completed_steps(agent_app):
    agent_app.config["QUEUE_MODE"] = "rq"
    client = agent_app.test_client()
    incident = create_pod_incident(client)
    accepted = client.post(f"/api/incidents/{incident['id']}/agent-runs")
    run_id = accepted.get_json()["id"]
    orchestrator = agent_app.extensions["agent_orchestrator"]

    first = orchestrator.process(run_id)
    second = orchestrator.process(run_id)
    steps = client.get(f"/api/agent-runs/{run_id}/steps").get_json()

    assert first.status.value == second.status.value == "COMPLETED"
    assert len(steps) == 4


def test_tool_timeout_hands_off_and_creates_action_task(agent_app):
    def timeout(**_kwargs):
        raise TimeoutError("untrusted timeout detail token=secret")

    agent_app.extensions["test_agent_adapter"].get_pod_status = timeout
    client = agent_app.test_client()
    incident = create_pod_incident(client)
    accepted = client.post(f"/api/incidents/{incident['id']}/agent-runs")
    run = client.get(accepted.get_json()["location"]).get_json()
    tasks = client.get(f"/api/tasks?incident_id={incident['id']}").get_json()
    steps = client.get(run["steps_url"]).get_json()

    assert run["status"] == "AWAITING_HUMAN"
    assert run["stop_reason"] == "TOOL_TRANSIENT_ERROR"
    assert tasks[0]["related_entity_id"] == run["id"]
    assert steps[0]["tool_invocation"]["attempt_count"] == 2
    assert "secret" not in str(steps)
