"""Run-local memory and metered model boundary regression checks."""

import json
import hashlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from ops_agent.agent_core import (
    AgentContextBuilder, DecisionError, FallbackReasoner, ModelBudget,
    OpenAIDecisionReasoner, RuleReasoner,
)
from ops_agent.context_memory import estimate_tokens, request_body, serialized
from ops_agent.agent_core import NEXT_DECISION_SCHEMA
from ops_agent.domain import EvidenceType
from ops_agent.models import AgentStep, AuditEvent, Evidence
from .test_agent_core import agent_app, create_pod_incident


def queued(agent_app):
    agent_app.config["QUEUE_MODE"] = "rq"
    client = agent_app.test_client()
    incident = create_pod_incident(client)
    run_id = client.post(f"/api/incidents/{incident['id']}/agent-runs").get_json()["id"]
    orchestrator = agent_app.extensions["agent_orchestrator"]
    orchestrator._claim(run_id)
    return orchestrator, orchestrator._snapshot(run_id)


def record(run, ident, content, kind=EvidenceType.POD_STATUS, age=0, now=None):
    return Evidence(id=ident, incident_id=run.incident_id, agent_run_id=run.id,
                    evidence_type=kind, source="test", content=content,
                    content_hash=hashlib.sha256(serialized(content).encode()).hexdigest(),
                    collected_at=(now or datetime.now(timezone.utc)) - timedelta(seconds=age))


def test_memory_selection_preserves_facts_and_marks_missing_evidence(agent_app):
    orch, (run, incident, _, steps) = queued(agent_app)
    now = datetime.now(timezone.utc)
    run.target_snapshot = {**run.target_snapshot, "resource_uid": "u"}
    items = [record(run, "oom", {"uid": "u", "containers": [{"last_terminated_reason": "OOMKilled"}]}, now=now)]
    items += [record(run, f"log-{i}", {"uid": "u", "logs": {"app": "password=hidden " + "noise" * 10000}},
                     EvidenceType.CURRENT_LOGS, age=i, now=now) for i in range(20)]
    builder = AgentContextBuilder(max_bytes=10000)
    first = builder.build(run, incident, items, steps, orch.registry, now=now)
    second = builder.build(run, incident, items, steps, orch.registry, now=now)
    assert first == second
    assert "oom" in first["visible_evidence_ids"]
    assert "OOMKilled" in serialized(first["working_memory"]["confirmed"])
    assert first["selection"]["omitted"] > 0
    assert first["selection"]["incomplete"]
    assert "hidden" not in serialized(first)
    assert len(serialized(first).encode()) <= 10000
    assert not first.get("truncated")
    assert len([item for item in first["evidence"] if item["type"] == "CURRENT_LOGS"]) == 1
    # The legacy builder would drop every evidence body after hitting its byte cap.
    from ops_agent.evidence import sanitize_content
    legacy_items = [sanitize_content(item.content, max_bytes=2048)[0] for item in items]
    assert sanitize_content({"evidence": legacy_items}, max_bytes=10000)[0]["truncated"]


def test_memory_scope_freshness_uid_and_conflict(agent_app):
    orch, (run, incident, _, steps) = queued(agent_app)
    now = datetime.now(timezone.utc)
    run.target_snapshot = {"resource_uid": "new"}
    items = [record(run, "old", {"uid": "old", "phase": "Failed"}, now=now),
             record(run, "stale", {"uid": "new", "phase": "Failed"}, age=121, now=now),
             record(run, "unknown", {"ready": True}, now=now),
             record(run, "new-a", {"uid": "new", "phase": "Running"}, now=now),
             record(run, "new-b", {"uid": "new", "phase": "Failed"}, now=now),
             record(run, "foreign", {"phase": "Failed"}, now=now)]
    items[-1].agent_run_id = "another-run"
    items[2].collected_at = None
    context = AgentContextBuilder().build(run, incident, items, steps, orch.registry, now=now)
    by_id = {item["id"]: item for item in context["evidence"]}
    assert "foreign" not in by_id
    assert by_id["old"]["uid_status"] == "mismatch"
    assert by_id["stale"]["freshness"] == "stale"
    assert by_id["unknown"]["freshness"] == by_id["unknown"]["uid_status"] == "unknown"
    assert context["selection"]["conflicting_observations"]
    confirmed = serialized(context["working_memory"]["confirmed"])
    assert '"old"' not in confirmed and '"stale"' not in confirmed and '"unknown"' not in confirmed


def test_fixed_context_cannot_be_removed_to_fit(agent_app):
    orch, (run, incident, evidence, steps) = queued(agent_app)
    with pytest.raises(DecisionError, match="fixed context"):
        AgentContextBuilder(max_bytes=256).build(run, incident, evidence, steps, orch.registry)
    with pytest.raises(DecisionError):
        AgentContextBuilder().build(run, incident, evidence, steps, orch.registry,
                                    model_budget=ModelBudget("test", 100))


@pytest.mark.parametrize("text", ["中文日志" * 100, "english log" * 100])
def test_estimate_includes_full_request(text):
    body = request_body({"evidence": text}, "test", NEXT_DECISION_SCHEMA, 1000)
    assert estimate_tokens(body) == len(serialized(body).encode("utf-8"))
    assert estimate_tokens(body) > len(text.encode("utf-8"))


@pytest.mark.parametrize("window", [0, -1, "unknown", None])
def test_invalid_model_configuration_does_not_send_request(agent_app, window):
    orch, (run, *_rest) = queued(agent_app)
    client = SimpleNamespace(responses=SimpleNamespace(create=lambda **_: pytest.fail("unexpected request")))
    orch.reasoner = FallbackReasoner(OpenAIDecisionReasoner(client, "test", context_window=window))
    result = orch.process(run.id)
    assert result.status.value == "COMPLETED"
    assert result.model_calls_used == 0


@pytest.mark.parametrize("failure,expected_input,expected_output", [
    ("invalid-json", 71, 13), ("invalid-schema", 71, 13),
    ("unknown-reference", 71, 13), ("timeout", None, None), ("missing-usage", None, None),
])
def test_failed_model_call_is_metered_and_rules_complete(agent_app, failure, expected_input, expected_output):
    orch, (run, *_rest) = queued(agent_app)
    calls = []
    def respond(**request):
        calls.append(request)
        # Attempt and reservation must already exist when the client is invoked.
        current = orch._snapshot(run.id)[0]
        assert current.model_calls_used == 1 and current.input_tokens_used > 0
        if failure == "timeout":
            raise TimeoutError("password=do-not-save")
        payload = {"decision_type": "ASK_HUMAN", "tool_name": None, "arguments": {},
                   "decision_summary": "Need evidence", "evidence_ids": ["not-visible"],
                   "confidence": 0.1, "stop_reason": "INSUFFICIENT_EVIDENCE", "diagnosis": None}
        output = "bad json" if failure in {"invalid-json", "missing-usage"} else json.dumps(
            {} if failure == "invalid-schema" else payload)
        return SimpleNamespace(output_text=output, usage=None if failure == "missing-usage" else
                               SimpleNamespace(input_tokens=71, output_tokens=13))
    with orch.session_factory.begin() as session:
        current = session.get(type(run), run.id)
        current.max_model_calls = 1
    orch.reasoner = FallbackReasoner(OpenAIDecisionReasoner(
        SimpleNamespace(responses=SimpleNamespace(create=respond)), "test", context_window=50000))
    result = orch.process(run.id)
    assert result.status.value == "COMPLETED"
    assert len(calls) == result.model_calls_used == 1
    if expected_input is not None:
        assert (result.input_tokens_used, result.output_tokens_used) == (expected_input, expected_output)
    else:
        assert result.input_tokens_used > 0 and result.output_tokens_used == 1000
    with orch.session_factory() as session:
        events = list(session.scalars(select(AuditEvent).where(AuditEvent.entity_id == run.id)))
    finished = [event.payload for event in events if event.event_type == "agent_model.finished"][0]
    assert finished["usage_known"] == (expected_input is not None)
    assert "do-not-save" not in str([event.payload for event in events])


def test_actual_overrun_stops_before_tool_execution(agent_app):
    orch, (run, *_rest) = queued(agent_app)
    def respond(**_request):
        return SimpleNamespace(output_text="invalid", usage=SimpleNamespace(input_tokens=30000, output_tokens=10))
    orch.reasoner = FallbackReasoner(OpenAIDecisionReasoner(
        SimpleNamespace(responses=SimpleNamespace(create=respond)), "test", context_window=50000))
    result = orch.process(run.id)
    assert result.status.value == "STOPPED"
    assert result.input_tokens_used == 30000
    assert result.model_calls_used == 1 and result.tool_calls_used == 0


def test_model_sees_new_tool_evidence_and_can_complete(agent_app):
    orch, (run, *_rest) = queued(agent_app)
    seen = []
    def respond(**request):
        context = json.loads(request["input"])
        seen.append(context)
        _, incident, evidence, _ = orch._snapshot(run.id)
        decision = RuleReasoner().decide(context, incident, evidence, orch.registry)
        return SimpleNamespace(output_text=json.dumps(decision.payload()),
                               usage=SimpleNamespace(input_tokens=100, output_tokens=50))
    orch.reasoner = FallbackReasoner(OpenAIDecisionReasoner(
        SimpleNamespace(responses=SimpleNamespace(create=respond)), "test", context_window=50000))
    result = orch.process(run.id)
    assert result.status.value == "COMPLETED" and result.model_calls_used == 4
    assert seen[1]["working_memory"]["confirmed"]
    assert len(seen[-1]["visible_evidence_ids"]) > len(seen[0]["visible_evidence_ids"])
    with orch.session_factory() as session:
        steps = list(session.scalars(select(AgentStep).where(AgentStep.agent_run_id == run.id)))
    assert all(step.context_version == "agent-context-v2" for step in steps)


def test_cancel_during_model_request_does_not_execute_tool(agent_app):
    orch, (run, *_rest) = queued(agent_app)
    def respond(**request):
        context = json.loads(request["input"])
        _, incident, evidence, _ = orch._snapshot(run.id)
        decision = RuleReasoner().decide(context, incident, evidence, orch.registry)
        orch.request_cancel(run.id)
        return SimpleNamespace(output_text=json.dumps(decision.payload()),
                               usage=SimpleNamespace(input_tokens=100, output_tokens=10))
    orch.reasoner = FallbackReasoner(OpenAIDecisionReasoner(
        SimpleNamespace(responses=SimpleNamespace(create=respond)), "test", context_window=50000))
    result = orch.process(run.id)
    assert result.status.value == "CANCELLED" and result.tool_calls_used == 0
    assert result.model_calls_used == 1


@pytest.mark.parametrize("remaining", [0, -1, 1512])
def test_nonpositive_input_budget_is_rejected(agent_app, remaining):
    _, (run, *_rest) = queued(agent_app)
    run.input_tokens_used = run.max_total_tokens - remaining
    with pytest.raises(DecisionError) as error:
        OpenAIDecisionReasoner(None, "test", context_window=50000).budget(run)
    assert error.value.code == "MODEL_BUDGET_EXHAUSTED"


def test_full_request_fits_and_output_reserve_matches(agent_app):
    orch, (run, incident, evidence, steps) = queued(agent_app)
    model = OpenAIDecisionReasoner(None, "test", context_window=17000, output_tokens=700)
    budget = model.budget(run)
    context = orch.context_builder.build(run, incident, evidence, steps, orch.registry, model_budget=budget)
    body = request_body(context, model.model, NEXT_DECISION_SCHEMA, budget.output_tokens)
    assert estimate_tokens(body) <= budget.input_limit
    assert body["max_output_tokens"] == context["model_budget"]["output_reserve"] == 700
    assert estimate_tokens(body) + 700 + budget.safety_margin <= 17000


@pytest.mark.parametrize("ttls", [[], {"BAD_TYPE": 1}, {"POD_STATUS": 0}, {"POD_STATUS": True}])
def test_invalid_ttl_settings_fail_early(ttls):
    with pytest.raises(ValueError):
        AgentContextBuilder(evidence_ttls=ttls)


def test_log_excerpt_keeps_early_error_and_newest_tool_result(agent_app):
    orch, (run, incident, _, _) = queued(agent_app)
    items = [record(run, "log", {"logs": {"demo": "fatal: startup failed\n" + "noise\n" * 500}},
                    EvidenceType.PREVIOUS_LOGS), record(run, "pod", {"ready": False})]
    step = SimpleNamespace(sequence=1, step_type=None, status=SimpleNamespace(value="SUCCEEDED"),
                           evidence_ids=[], decision_summary=None,
                           tool_invocation=SimpleNamespace(evidence_id="log", tool_name="get_previous_logs",
                               error_code=None, sanitized_arguments={}, status=SimpleNamespace(value="SUCCEEDED")))
    context = orch.context_builder.build(run, incident, items, [step], orch.registry)
    assert context["evidence"][0]["id"] == "log"
    assert "fatal: startup failed" in serialized(context)


def test_policy_denial_is_not_retried_through_rules(agent_app):
    orch, (run, *_rest) = queued(agent_app)
    def respond(**_request):
        payload = {"decision_type": "CALL_TOOL", "tool_name": "get_pod_status",
                   "arguments": {"cluster": "default", "namespace": "kube-system", "pod_name": "x"},
                   "decision_summary": "Read status", "evidence_ids": [], "confidence": 0.5,
                   "stop_reason": None, "diagnosis": None}
        return SimpleNamespace(output_text=json.dumps(payload), usage=SimpleNamespace(input_tokens=10, output_tokens=10))
    orch.reasoner = FallbackReasoner(OpenAIDecisionReasoner(
        SimpleNamespace(responses=SimpleNamespace(create=respond)), "test", context_window=50000))
    result = orch.process(run.id)
    assert result.status.value == "STOPPED" and result.tool_calls_used == 0
    assert result.stop_reason == "SYSTEM_NAMESPACE_DENIED"


def test_stale_evidence_cannot_support_model_completion(agent_app):
    orch, (run, *_rest) = queued(agent_app)
    with orch.session_factory.begin() as session:
        session.add(record(run, "stale", {"phase": "Failed"}, age=1000))
        session.get(type(run), run.id).max_model_calls = 1
    def respond(**_request):
        _, incident, _, _ = orch._snapshot(run.id)
        decision = RuleReasoner._diagnosis(incident, "UNKNOWN", "Observed failure", "Old failure", ["stale"], 0.5)
        return SimpleNamespace(output_text=json.dumps(decision.payload()), usage=SimpleNamespace(input_tokens=10, output_tokens=10))
    orch.reasoner = FallbackReasoner(OpenAIDecisionReasoner(
        SimpleNamespace(responses=SimpleNamespace(create=respond)), "test", context_window=50000))
    result = orch.process(run.id)
    assert result.status.value == "COMPLETED" and result.diagnosis["diagnosis_code"] == "CRASH_LOOP"
    assert "stale" not in result.diagnosis["evidence_ids"]


def test_context_construction_failure_finishes_run(agent_app):
    orch, (run, *_rest) = queued(agent_app)
    orch.context_builder = AgentContextBuilder(max_bytes=100)
    result = orch.process(run.id)
    assert result.status.value == "STOPPED" and result.stop_reason == "CONTEXT_TOO_LARGE"
    assert result.lease_owner is None and result.tool_calls_used == 0


def test_model_cannot_cite_existing_but_omitted_evidence(agent_app):
    orch, (run, *_rest) = queued(agent_app)
    with orch.session_factory.begin() as session:
        session.add(record(run, "omitted", {"data": "x" * 10000}, EvidenceType.COLLECTION_ERROR))
        session.get(type(run), run.id).max_model_calls = 1
    def respond(**request):
        context = json.loads(request["input"])
        assert "omitted" not in context["visible_evidence_ids"]
        _, incident, evidence, _ = orch._snapshot(run.id)
        assert "omitted" in {item.id for item in evidence}
        decision = RuleReasoner._diagnosis(incident, "UNKNOWN", "Failure", "Omitted failure", ["omitted"], 0.5)
        return SimpleNamespace(output_text=json.dumps(decision.payload()), usage=SimpleNamespace(input_tokens=10, output_tokens=10))
    orch.reasoner = FallbackReasoner(OpenAIDecisionReasoner(
        SimpleNamespace(responses=SimpleNamespace(create=respond)), "test", context_window=50000))
    result = orch.process(run.id)
    assert result.status.value == "COMPLETED" and result.diagnosis["diagnosis_code"] == "CRASH_LOOP"
    with orch.session_factory() as session:
        events = list(session.scalars(select(AuditEvent).where(AuditEvent.entity_id == run.id)))
    assert any(event.payload.get("fallback_reason") == "UNKNOWN_EVIDENCE_REFERENCE" for event in events)


def test_conflicting_observations_request_human_without_tool_call(agent_app):
    orch, (run, *_rest) = queued(agent_app)
    now = datetime.now(timezone.utc)
    with orch.session_factory.begin() as session:
        session.get(type(run), run.id).target_snapshot = {"resource_uid": "u"}
        session.add_all([record(run, "a", {"uid": "u", "ready": True}, now=now),
                         record(run, "b", {"uid": "u", "ready": False}, now=now)])
    result = orch.process(run.id)
    assert result.status.value == "AWAITING_HUMAN"
    assert result.stop_reason == "CONFLICTING_EVIDENCE" and result.tool_calls_used == 0
