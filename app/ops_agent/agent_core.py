"""Bounded, auditable and read-only v0.3 Agent loop."""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from jsonschema import Draft202012Validator
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from ops_agent.diagnosis import DIAGNOSIS_SCHEMA, RuleDiagnosis
from ops_agent.domain import (
    AgentRunStatus,
    AgentStepStatus,
    AgentStepType,
    EvidenceType,
    IncidentStatus,
    PolicyDecision,
    Priority,
    TaskStatus,
    TaskType,
    ToolInvocationStatus,
)
from ops_agent.evidence import sanitize_content
from ops_agent.models import (
    AgentRun,
    AgentStep,
    AuditEvent,
    Evidence,
    Incident,
    Task,
    ToolInvocation,
)
from ops_agent.metrics import (
    AGENT_BUDGET_EXHAUSTED,
    AGENT_DECISION_VALIDATION_FAILURES,
    AGENT_HUMAN_HANDOFF,
    AGENT_RUN_DURATION,
    AGENT_RUNS_V3,
    AGENT_STEPS,
    AGENT_STOP,
    AGENT_TOOL_CALLS,
    AGENT_TOOL_DURATION,
    AGENT_CONTEXT_BYTES,
    AGENT_CONTEXT_TRIMMED,
)
from ops_agent.tooling import ToolError, ToolRegistry
from ops_agent.context_memory import (
    CONTEXT_VERSION, DEFAULT_TTLS, evidence_item, select_context,
    request_body, estimate_tokens, serialized as context_json,
)

PROMPT_VERSION = "agent-decision-v2"
SCHEMA_VERSION = "next-decision-v1"
SYSTEM_NAMESPACES = {"kube-system", "kube-public", "kube-node-lease"}
EXECUTABLE_TEXT = re.compile(
    r"(?i)(?:^|\s)(?:kubectl|bash|powershell|cmd\.exe|sh\s+-c)(?:\s|$)|[;&|`]"
)


NEXT_DECISION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "decision_type",
        "tool_name",
        "arguments",
        "decision_summary",
        "evidence_ids",
        "confidence",
        "stop_reason",
        "diagnosis",
    ],
    "properties": {
        "decision_type": {
            "type": "string",
            "enum": [item.value for item in AgentStepType],
        },
        "tool_name": {"type": ["string", "null"], "maxLength": 100},
        "arguments": {"type": "object"},
        "decision_summary": {"type": "string", "minLength": 1, "maxLength": 500},
        "evidence_ids": {
            "type": "array",
            "items": {"type": "string"},
            "uniqueItems": True,
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "stop_reason": {"type": ["string", "null"], "maxLength": 100},
        "diagnosis": {"oneOf": [{"type": "null"}, DIAGNOSIS_SCHEMA]},
    },
}


class DecisionError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class NextDecision:
    decision_type: AgentStepType
    tool_name: str | None
    arguments: dict[str, Any]
    decision_summary: str
    evidence_ids: list[str]
    confidence: float
    stop_reason: str | None = None
    diagnosis: dict[str, Any] | None = None
    provider: str = "rules"
    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    fallback_error_code: str | None = None
    usage_known: bool = False

    def payload(self) -> dict[str, Any]:
        return {
            "decision_type": self.decision_type.value,
            "tool_name": self.tool_name,
            "arguments": self.arguments,
            "decision_summary": self.decision_summary,
            "evidence_ids": self.evidence_ids,
            "confidence": self.confidence,
            "stop_reason": self.stop_reason,
            "diagnosis": self.diagnosis,
        }


def validate_next_decision(
    decision: NextDecision,
    *,
    registry: ToolRegistry,
    visible_evidence_ids: set[str],
) -> None:
    errors = list(Draft202012Validator(NEXT_DECISION_SCHEMA).iter_errors(decision.payload()))
    if errors:
        raise DecisionError("DECISION_SCHEMA_INVALID", errors[0].message)
    if not set(decision.evidence_ids).issubset(visible_evidence_ids):
        raise DecisionError("UNKNOWN_EVIDENCE_REFERENCE", "decision cites unknown Evidence")
    rendered = json.dumps(
        {"summary": decision.decision_summary, "arguments": decision.arguments},
        ensure_ascii=False,
    )
    if EXECUTABLE_TEXT.search(rendered):
        raise DecisionError("EXECUTABLE_CONTENT_REJECTED", "decision contains executable content")

    if decision.decision_type == AgentStepType.CALL_TOOL:
        if not decision.tool_name or decision.diagnosis is not None or decision.stop_reason is not None:
            raise DecisionError("DECISION_SEMANTICS_INVALID", "CALL_TOOL fields are inconsistent")
        registry.get(decision.tool_name)
    elif decision.decision_type == AgentStepType.COMPLETE:
        if decision.tool_name is not None or decision.arguments or decision.diagnosis is None:
            raise DecisionError("DECISION_SEMANTICS_INVALID", "COMPLETE requires only diagnosis")
        if not decision.evidence_ids:
            raise DecisionError("INSUFFICIENT_EVIDENCE", "COMPLETE requires Evidence")
        if set(decision.diagnosis["evidence_ids"]) != set(decision.evidence_ids):
            raise DecisionError("EVIDENCE_MISMATCH", "diagnosis references do not match decision")
    else:
        if decision.tool_name is not None or decision.arguments or decision.diagnosis is not None:
            raise DecisionError("DECISION_SEMANTICS_INVALID", "terminal decision fields are inconsistent")
        if not decision.stop_reason:
            raise DecisionError("STOP_REASON_REQUIRED", "terminal decision needs stop_reason")


class AgentContextBuilder:
    def __init__(self, *, max_bytes: int = 64 * 1024, evidence_max_bytes: int = 2048, evidence_ttls=None):
        self.max_bytes = max_bytes
        if evidence_ttls is None:
            evidence_ttls = {}
        if isinstance(evidence_ttls, str):
            evidence_ttls = json.loads(evidence_ttls or "{}")
        if not isinstance(evidence_ttls, dict) or any(
            key not in DEFAULT_TTLS or not isinstance(value, int) or isinstance(value, bool) or value <= 0
            for key, value in (evidence_ttls or {}).items()
        ):
            raise ValueError("evidence TTLs must map known types to positive seconds")
        self.evidence_ttls = {**DEFAULT_TTLS, **(evidence_ttls or {})}
        self.evidence_max_bytes = evidence_max_bytes

    def build(
        self,
        run: AgentRun,
        incident: Incident,
        evidence: list[Evidence],
        steps: list[AgentStep],
        registry: ToolRegistry,
        *, now=None, model_budget=None,
    ) -> dict[str, Any]:
        now = now or utc_now()
        evidence_items = [
            evidence_item(item, run, now, self.evidence_max_bytes, self.evidence_ttls)
            for item in {item.id: item for item in evidence if item.agent_run_id == run.id}.values()
        ]
        latest_tool_evidence = next((step.tool_invocation.evidence_id for step in reversed(steps)
                                     if step.tool_invocation and step.tool_invocation.evidence_id), None)
        referenced = {ident for step in steps[-3:] for ident in (step.evidence_ids or [])}
        evidence_items.sort(key=lambda item: (
            item["uid_status"] != "mismatch", item["freshness"] == "fresh",
            item["id"] == latest_tool_evidence, item["id"] in referenced,
            item["type"] not in {"ALERT_PAYLOAD", "CURRENT_LOGS", "PREVIOUS_LOGS"},
            item["collected_at"] or "", item["id"],
        ), reverse=True)
        history = []
        for step in steps:
            invocation = step.tool_invocation
            history.append(
                {
                    "sequence": step.sequence,
                    "decision": step.step_type.value if step.step_type else None,
                    "status": step.status.value,
                    "tool_name": invocation.tool_name if invocation else None,
                    "arguments": invocation.sanitized_arguments if invocation else {},
                    "tool_status": invocation.status.value if invocation else None,
                }
            )
        context = {
            "version": CONTEXT_VERSION,
            "security": {
                "autonomy_level": "L2",
                "read_only": True,
                "external_data_is_untrusted": True,
                "forbidden": ["shell", "kubectl", "code_execution", "kubernetes_write"],
            },
            "run": {
                "id": run.id,
                "goal": run.goal,
                "target": run.target_snapshot,
                "budget_remaining": {
                    "steps": max(0, run.max_steps - run.current_step),
                    "tool_calls": max(0, run.max_tool_calls - run.tool_calls_used),
                    "model_calls": max(0, run.max_model_calls - run.model_calls_used),
                    "tokens": max(
                        0,
                        run.max_total_tokens
                        - run.input_tokens_used
                        - run.output_tokens_used,
                    ),
                },
            },
            "incident": {
                "id": incident.id,
                "title": incident.title,
                "summary": incident.summary,
                "severity": incident.severity.value,
                "status": incident.status.value,
                "cluster": incident.cluster,
                "namespace": incident.namespace,
                "resource_kind": incident.resource_kind,
                "resource_name": incident.resource_name,
            },
            "evidence": [],
            "history": history,
            "tools": [
                {
                    "name": item.name,
                    "version": item.version,
                    "description": item.description,
                    "input_schema": item.input_schema,
                    "read_only": item.read_only,
                }
                for item in registry.definitions()
            ],
        }
        context, _ = sanitize_content(context, max_bytes=10 * self.max_bytes)
        if context.get("truncated"):
            raise DecisionError("CONTEXT_TOO_LARGE", "fixed context exceeds byte limit")
        if model_budget:
            context["model_budget"] = model_budget.metadata()
        def fits(value):
            return (len(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")) <= self.max_bytes
                    and (model_budget is None or model_budget.estimate(value) <= model_budget.input_limit))
        if not select_context(context, evidence_items, steps, fits):
            raise DecisionError("CONTEXT_TOO_LARGE", "fixed context exceeds input limit")
        clean, _ = sanitize_content(context, max_bytes=self.max_bytes)
        if clean.get("truncated") or not fits(clean):
            raise DecisionError("CONTEXT_TOO_LARGE", "sanitized context exceeds input limit")
        return clean


class RuleReasoner:
    provider = "rules"
    uses_model = False

    @staticmethod
    def _call(tool_name: str, arguments: dict[str, Any], summary: str, evidence_ids=None) -> NextDecision:
        return NextDecision(
            AgentStepType.CALL_TOOL,
            tool_name,
            arguments,
            summary,
            list(evidence_ids or []),
            0.9,
        )

    @staticmethod
    def _diagnosis(
        incident: Incident,
        code: str,
        summary: str,
        probable_cause: str,
        evidence_ids: list[str],
        confidence: float,
    ) -> NextDecision:
        diagnosis = {
            "summary": summary,
            "probable_cause": probable_cause,
            "confidence": confidence,
            "severity": incident.severity.value,
            "evidence_ids": evidence_ids,
            "recommended_actions": [
                {"action_type": "INVESTIGATE", "reason": "根据已保存证据进行人工确认"}
            ],
            "diagnosis_code": code,
        }
        return NextDecision(
            AgentStepType.COMPLETE,
            None,
            {},
            summary,
            evidence_ids,
            confidence,
            diagnosis=diagnosis,
        )

    def decide(
        self,
        context: dict[str, Any],
        incident: Incident,
        evidence: list[Evidence],
        registry: ToolRegistry,
    ) -> NextDecision:
        by_type: dict[str, list[Evidence]] = {}
        for item in evidence:
            by_type.setdefault(item.evidence_type.value, []).append(item)
        invoked = {item["tool_name"] for item in context["history"] if item["tool_name"]}
        tools = {item.name for item in registry.definitions()}
        base = {
            "cluster": incident.cluster,
            "namespace": incident.namespace,
        }
        kind = incident.resource_kind.lower()
        text = " ".join(filter(None, [incident.title, incident.summary or ""])).lower()

        if "redis" in text and "query_prometheus" in tools and "query_prometheus" not in invoked:
            return self._call(
                "query_prometheus",
                {**base, "query_name": "redis_up", "resource_name": incident.resource_name},
                "查询 Redis 可用性指标",
            )

        metric_query = None
        if any(marker in text for marker in ("cpu", "processor")):
            metric_query = "pod_cpu_usage"
        elif any(marker in text for marker in ("memory", "内存")):
            metric_query = "pod_memory_usage"
        elif any(marker in text for marker in ("5xx", "http error", "错误率")):
            metric_query = "http_5xx_rate"

        if kind == "pod":
            if EvidenceType.POD_STATUS.value not in by_type:
                return self._call(
                    "get_pod_status", {**base, "pod_name": incident.resource_name}, "读取 Pod 状态"
                )
            pod = by_type[EvidenceType.POD_STATUS.value][-1]
            containers = pod.content.get("containers") or []
            reasons = {
                value
                for item in containers
                for value in (item.get("waiting_reason"), item.get("last_terminated_reason"))
                if value
            }
            crash = "CrashLoopBackOff" in reasons or any(
                int(item.get("restart_count") or 0) > 0
                and int(item.get("last_exit_code") or 0) != 0
                for item in containers
            )
            oom = "OOMKilled" in reasons
            image_pull = bool(reasons & {"ImagePullBackOff", "ErrImagePull"})
            if (crash or oom) and EvidenceType.PREVIOUS_LOGS.value not in by_type:
                return self._call(
                    "get_previous_logs",
                    {**base, "pod_name": incident.resource_name, "tail_lines": 200},
                    "读取崩溃前日志",
                    [pod.id],
                )
            uid = pod.content.get("uid")
            if (crash or oom or image_pull or pod.content.get("ready") is False) and EvidenceType.K8S_EVENTS.value not in by_type and uid:
                return self._call(
                    "get_kubernetes_events",
                    {**base, "resource_uid": uid},
                    "读取目标资源事件",
                    [pod.id],
                )
            if oom and EvidenceType.RESOURCE_LIMITS.value not in by_type:
                return self._call(
                    "get_resource_limits",
                    {**base, "pod_name": incident.resource_name},
                    "读取容器资源限制",
                    [pod.id],
                )
            related = [
                item.id
                for item in evidence
                if item.evidence_type
                in {
                    EvidenceType.POD_STATUS,
                    EvidenceType.PREVIOUS_LOGS,
                    EvidenceType.K8S_EVENTS,
                    EvidenceType.RESOURCE_LIMITS,
                }
            ]
            if oom:
                return self._diagnosis(incident, "OOM_KILLED", "容器因内存不足被终止", "容器状态和资源限制显示 OOMKilled", related, 0.95)
            if image_pull:
                return self._diagnosis(incident, "IMAGE_PULL_ERROR", "容器镜像拉取失败", "Pod 状态或事件显示镜像无法拉取", related, 0.95)
            if crash:
                result = RuleDiagnosis().diagnose(incident, evidence)
                diagnosis = {key: result[key] for key in DIAGNOSIS_SCHEMA["required"]}
                return NextDecision(AgentStepType.COMPLETE, None, {}, diagnosis["summary"], diagnosis["evidence_ids"], diagnosis["confidence"], diagnosis=diagnosis)
            if pod.content.get("phase") == "Pending":
                return self._diagnosis(incident, "POD_PENDING", "Pod 长时间处于 Pending", "调度条件或依赖资源尚未满足", related, 0.8)
            if pod.content.get("ready") is False:
                return self._diagnosis(incident, "READINESS_FAILURE", "Pod 未通过 Ready 条件", "探针、容器或依赖未达到就绪状态", related, 0.8)
            if metric_query and "query_prometheus" in tools and "query_prometheus" not in invoked:
                return self._call(
                    "query_prometheus",
                    {**base, "query_name": metric_query, "resource_name": incident.resource_name},
                    "查询与告警对应的受控指标",
                    [pod.id],
                )
            metric_evidence = by_type.get(EvidenceType.PROMETHEUS_METRICS.value, [])
            if metric_evidence:
                code = "HTTP_5XX" if metric_query == "http_5xx_rate" else "RESOURCE_PRESSURE"
                return self._diagnosis(
                    incident,
                    code,
                    "指标显示运行压力或错误率异常",
                    "Pod 状态和受控 Prometheus 查询共同支持该结论",
                    [pod.id, metric_evidence[-1].id],
                    0.8,
                )
            return self._diagnosis(incident, "UNKNOWN", "目标 Pod 当前未显示明确异常", "现有证据不足以确认告警根因", [pod.id], 0.3)

        if kind in {"deployment", "statefulset"}:
            if EvidenceType.WORKLOAD_STATUS.value not in by_type:
                return self._call(
                    "get_workload_status",
                    {**base, "resource_kind": incident.resource_kind, "resource_name": incident.resource_name},
                    "读取工作负载副本状态",
                )
            workload = by_type[EvidenceType.WORKLOAD_STATUS.value][-1]
            if "list_related_pods" in tools and "list_related_pods" not in invoked:
                return self._call(
                    "list_related_pods",
                    {**base, "workload_kind": incident.resource_kind, "workload_name": incident.resource_name},
                    "列出工作负载关联 Pod",
                    [workload.id],
                )
            ids = [item.id for item in evidence if item.evidence_type in {EvidenceType.WORKLOAD_STATUS, EvidenceType.POD_STATUS}]
            return self._diagnosis(incident, "DEPLOYMENT_UNAVAILABLE", "工作负载可用副本不足", "副本状态和关联 Pod 表明工作负载尚未完全可用", ids, 0.85)

        metrics = by_type.get(EvidenceType.PROMETHEUS_METRICS.value, [])
        if metrics:
            code = "REDIS_UNAVAILABLE" if "redis" in text else "RESOURCE_PRESSURE"
            return self._diagnosis(incident, code, "指标显示服务异常", "受控 Prometheus 查询返回异常信号", [metrics[-1].id], 0.8)
        if metric_query and "query_prometheus" in tools and "query_prometheus" not in invoked:
            return self._call(
                "query_prometheus",
                {**base, "query_name": metric_query, "resource_name": incident.resource_name},
                "查询与告警对应的受控指标",
            )
        return NextDecision(
            AgentStepType.ASK_HUMAN,
            None,
            {},
            "当前资源类型没有足够的安全只读调查路径",
            [item.id for item in evidence],
            0.1,
            stop_reason="INSUFFICIENT_EVIDENCE",
        )


@dataclass(frozen=True)
class ModelBudget:
    model: str
    input_limit: int
    output_tokens: int = 1000
    safety_margin: int = 512

    def estimate(self, context):
        return estimate_tokens(request_body(context, self.model, NEXT_DECISION_SCHEMA, self.output_tokens))

    def metadata(self):
        return {"input_limit": self.input_limit, "output_reserve": self.output_tokens,
                "safety_margin": self.safety_margin, "estimate_method": "utf8_bytes_conservative"}


class OpenAIDecisionReasoner:
    provider = "openai"
    uses_model = True

    def __init__(self, client, model: str, *, context_window=0, input_limit=12000,
                 output_tokens=1000, safety_margin=512):
        self.client = client
        self.model = model
        self.context_window = context_window
        self.input_limit = input_limit
        self.output_tokens = output_tokens
        self.safety_margin = safety_margin

    def budget(self, run):
        try:
            values = (self.context_window, self.input_limit, self.output_tokens, self.safety_margin)
            if any(isinstance(value, bool) or not isinstance(value, (str, int)) for value in values):
                raise ValueError("integer configuration required")
            window, limit, output, margin = map(int, values)
        except (TypeError, ValueError):
            raise DecisionError("MODEL_CONFIG_INVALID", "invalid model budget configuration")
        if min(window, limit, output) <= 0 or margin < 0:
            raise DecisionError("MODEL_CONFIG_INVALID", "explicit model window is required")
        remaining = run.max_total_tokens - run.input_tokens_used - run.output_tokens_used
        limit = min(window - output - margin, remaining - output - margin, limit)
        if limit <= 0 or run.model_calls_used >= run.max_model_calls:
            raise DecisionError("MODEL_BUDGET_EXHAUSTED", "model budget exhausted")
        return ModelBudget(self.model, limit, output, margin)

    def decide(self, context, incident, evidence, registry) -> NextDecision:
        # Accounting belongs to the orchestrator and is persisted before this call.
        response = self.client.responses.create(**request_body(
            context, self.model, NEXT_DECISION_SCHEMA, context["model_budget"]["output_reserve"]))
        usage = getattr(response, "usage", None)
        accounting = {}
        if usage is not None:
            values = (getattr(usage, "input_tokens", None), getattr(usage, "output_tokens", None))
            if all(isinstance(value, int) and value >= 0 for value in values):
                accounting = {"input_tokens": values[0], "output_tokens": values[1]}
        try:
            payload = json.loads(response.output_text)
            if list(Draft202012Validator(NEXT_DECISION_SCHEMA).iter_errors(payload)):
                raise ValueError("invalid schema")
            return NextDecision(
                decision_type=AgentStepType(payload["decision_type"]),
                tool_name=payload["tool_name"], arguments=payload["arguments"],
                decision_summary=payload["decision_summary"], evidence_ids=payload["evidence_ids"],
                confidence=payload["confidence"], stop_reason=payload["stop_reason"],
                diagnosis=payload["diagnosis"], provider="openai", model_calls=1,
                usage_known=bool(accounting), **accounting,
            )
        except Exception as error:
            failure = DecisionError("MODEL_RESPONSE_INVALID", "model returned invalid structured output")
            failure.accounting = accounting
            raise failure from error


class FallbackReasoner:
    def __init__(self, primary, fallback=None):
        self.primary = primary
        self.fallback = fallback or RuleReasoner()
        self.uses_model = getattr(primary, "uses_model", False)

    def decide(self, context, incident, evidence, registry) -> NextDecision:
        # Production model fallback is coordinated with accounting by AgentOrchestrator.
        if self.uses_model:
            return self.primary.decide(context, incident, evidence, registry)
        try:
            return self.primary.decide(context, incident, evidence, registry)
        except Exception as error:
            result = self.fallback.decide(context, incident, evidence, registry)
            return replace(result, fallback_error_code=getattr(error, "code", "MODEL_UNAVAILABLE"))


@dataclass(frozen=True)
class PolicyResult:
    decision: PolicyDecision
    reason_code: str


class AgentPolicy:
    def evaluate(
        self,
        run: AgentRun,
        incident: Incident,
        registry: ToolRegistry,
        tool_name: str,
        arguments: dict[str, Any],
        previous_fingerprints: set[str],
    ) -> PolicyResult:
        definition = registry.get(tool_name)
        if run.autonomy_level != "L2":
            return PolicyResult(PolicyDecision.DENY, "AUTONOMY_LEVEL_DENIED")
        if not definition.read_only or definition.requires_approval:
            return PolicyResult(PolicyDecision.DENY, "WRITE_TOOL_DENIED")
        if arguments.get("cluster") != incident.cluster or incident.cluster not in definition.allowed_clusters:
            return PolicyResult(PolicyDecision.DENY, "CLUSTER_SCOPE_DENIED")
        namespace = arguments.get("namespace")
        if namespace in SYSTEM_NAMESPACES:
            return PolicyResult(PolicyDecision.DENY, "SYSTEM_NAMESPACE_DENIED")
        if namespace != incident.namespace or namespace not in definition.allowed_namespaces:
            return PolicyResult(PolicyDecision.DENY, "NAMESPACE_SCOPE_DENIED")
        target_names = [
            arguments.get(key)
            for key in ("pod_name", "resource_name", "deployment_name", "workload_name")
            if arguments.get(key)
        ]
        if target_names and any(value != incident.resource_name for value in target_names):
            return PolicyResult(PolicyDecision.ASK_HUMAN, "INCIDENT_TARGET_MISMATCH")
        resource_uid = arguments.get("resource_uid")
        expected_uid = (run.target_snapshot or {}).get("resource_uid")
        if resource_uid and not expected_uid:
            return PolicyResult(PolicyDecision.ASK_HUMAN, "INCIDENT_UID_UNBOUND")
        if resource_uid and resource_uid != expected_uid:
            return PolicyResult(PolicyDecision.ASK_HUMAN, "INCIDENT_UID_MISMATCH")
        if run.tool_calls_used >= run.max_tool_calls:
            return PolicyResult(PolicyDecision.DENY, "TOOL_BUDGET_EXHAUSTED")
        fingerprint = invocation_fingerprint(tool_name, arguments)
        if fingerprint in previous_fingerprints:
            return PolicyResult(PolicyDecision.DENY, "DUPLICATE_TOOL_CALL")
        return PolicyResult(PolicyDecision.ALLOW, "POLICY_ALLOWED")


def invocation_fingerprint(tool_name: str, arguments: dict[str, Any]) -> str:
    payload = json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"{tool_name}:{payload}".encode()).hexdigest()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _comparable(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


class AgentOrchestrator:
    TERMINAL = {
        AgentRunStatus.COMPLETED,
        AgentRunStatus.AWAITING_HUMAN,
        AgentRunStatus.STOPPED,
        AgentRunStatus.FAILED,
        AgentRunStatus.CANCELLED,
    }

    def __init__(
        self,
        session_factory,
        registry: ToolRegistry,
        *,
        reasoner=None,
        context_builder=None,
        policy=None,
        worker_id: str | None = None,
    ):
        self.session_factory = session_factory
        self.registry = registry
        self.reasoner = reasoner or RuleReasoner()
        self.context_builder = context_builder or AgentContextBuilder()
        self.policy = policy or AgentPolicy()
        self.worker_id = worker_id or f"agent-{uuid4()}"

    @staticmethod
    def _audit(session, run_id: str, event_type: str, payload=None) -> None:
        clean, _ = sanitize_content(payload or {}, max_bytes=8000)
        session.add(
            AuditEvent(
                entity_type="AgentRun",
                entity_id=run_id,
                event_type=event_type,
                actor_type="AGENT",
                actor_id="agent-core-v0.3",
                payload=clean,
            )
        )

    def _ensure_alert_evidence(self, session, run: AgentRun, incident: Incident) -> None:
        exists = session.scalar(
            select(Evidence.id).where(
                Evidence.agent_run_id == run.id,
                Evidence.evidence_type == EvidenceType.ALERT_PAYLOAD,
            )
        )
        if exists:
            return
        content, redacted = sanitize_content(
            {
                **(incident.source_context or {}),
                "incident": run.target_snapshot,
                "summary": incident.summary,
            }
        )
        serialized = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        session.add(
            Evidence(
                incident_id=incident.id,
                agent_run_id=run.id,
                evidence_type=EvidenceType.ALERT_PAYLOAD,
                source=incident.source,
                content=content,
                content_hash=hashlib.sha256(serialized.encode()).hexdigest(),
                redacted=redacted,
            )
        )

    def _claim(self, run_id: str) -> AgentRun:
        now = utc_now()
        with self.session_factory.begin() as session:
            run = session.get(AgentRun, run_id)
            if not run:
                raise LookupError("agent run not found")
            if run.status in self.TERMINAL:
                return run
            lease = _comparable(run.lease_expires_at)
            if lease and lease > now and run.lease_owner not in {None, self.worker_id}:
                return run
            run.lease_owner = self.worker_id
            run.lease_expires_at = now + timedelta(seconds=30)
            run.heartbeat_at = now
            if run.status == AgentRunStatus.QUEUED:
                run.status = AgentRunStatus.RUNNING
                run.prompt_version = PROMPT_VERSION
                run.started_at = now
                if not run.deadline_at:
                    run.deadline_at = now + timedelta(seconds=run.run_timeout_seconds)
                incident = session.get(Incident, run.incident_id)
                if incident.status in {IncidentStatus.OPEN, IncidentStatus.FAILED, IncidentStatus.DIAGNOSED}:
                    incident.status = IncidentStatus.DIAGNOSING
                self._ensure_alert_evidence(session, run, incident)
                self._audit(session, run.id, "agent_run.started", {"goal": run.goal})
            return run

    def _finish(self, run_id: str, status: AgentRunStatus, reason: str, diagnosis=None) -> AgentRun:
        with self.session_factory.begin() as session:
            run = session.get(AgentRun, run_id)
            if run.status in self.TERMINAL:
                return run
            run.status = status
            run.stop_reason = reason
            run.finished_at = utc_now()
            run.lease_owner = None
            run.lease_expires_at = None
            if diagnosis is not None:
                run.diagnosis = diagnosis
            incident = session.get(Incident, run.incident_id)
            if status == AgentRunStatus.COMPLETED:
                if incident.status == IncidentStatus.DIAGNOSING:
                    incident.status = IncidentStatus.DIAGNOSED
            elif incident.status == IncidentStatus.DIAGNOSING:
                incident.status = IncidentStatus.FAILED
            if status == AgentRunStatus.AWAITING_HUMAN:
                task = session.scalar(
                    select(Task).where(
                        Task.incident_id == incident.id,
                        Task.related_entity_type == "AgentRun",
                        Task.related_entity_id == run.id,
                        Task.status.in_([TaskStatus.TODO, TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED]),
                    )
                )
                if not task:
                    session.add(
                        Task(
                            incident_id=incident.id,
                            title="Agent 调查需要人工接管",
                            description=f"停止原因：{reason}",
                            task_type=TaskType.MANUAL_CHECK,
                            priority=Priority.HIGH,
                            created_by="agent-core",
                            related_entity_type="AgentRun",
                            related_entity_id=run.id,
                        )
                    )
            self._audit(session, run.id, f"agent_run.{status.value.lower()}", {"reason": reason})
            provider = (
                diagnosis.get("provider", "rules") if diagnosis else "rules"
            )
            AGENT_RUNS_V3.labels(status.value, provider).inc()
            if run.started_at:
                started = _comparable(run.started_at)
                AGENT_RUN_DURATION.observe(max(0, (utc_now() - started).total_seconds()))
            if status == AgentRunStatus.AWAITING_HUMAN:
                AGENT_HUMAN_HANDOFF.labels(reason).inc()
            if status == AgentRunStatus.STOPPED:
                AGENT_STOP.labels(reason).inc()
            return run

    def request_cancel(self, run_id: str) -> AgentRun:
        with self.session_factory.begin() as session:
            run = session.get(AgentRun, run_id)
            if not run:
                raise LookupError("agent run not found")
            if run.status in self.TERMINAL:
                return run
            run.cancel_requested_at = run.cancel_requested_at or utc_now()
            self._audit(session, run.id, "agent_run.cancel_requested")
            if run.status == AgentRunStatus.QUEUED:
                run.status = AgentRunStatus.CANCELLED
                run.stop_reason = "USER_CANCELLED"
                run.finished_at = utc_now()
            return run

    def _snapshot(self, run_id: str):
        with self.session_factory() as session:
            run = session.get(AgentRun, run_id)
            incident = session.get(Incident, run.incident_id)
            evidence = list(session.scalars(select(Evidence).where(Evidence.agent_run_id == run.id).order_by(Evidence.collected_at, Evidence.id)).all())
            steps = list(
                session.scalars(
                    select(AgentStep)
                    .options(selectinload(AgentStep.tool_invocation))
                    .where(AgentStep.agent_run_id == run.id)
                    .order_by(AgentStep.sequence)
                ).all()
            )
            return run, incident, evidence, steps

    def _decide(self, run, step_id, context, incident, evidence, budget, fallback_code):
        decision = None
        if budget is not None:
            reserved_input = budget.estimate(context) + budget.safety_margin
            reserved_output = budget.output_tokens
            with self.session_factory.begin() as session:
                current = session.get(AgentRun, run.id)
                current.model_calls_used += 1
                current.input_tokens_used += reserved_input
                current.output_tokens_used += reserved_output
                self._audit(session, run.id, "agent_model.started", {
                    "step_id": step_id, "usage_known": False,
                    "reserved_input": reserved_input, "reserved_output": reserved_output})
            accounting = {}
            try:
                decision = self.reasoner.decide(context, incident, evidence, self.registry)
                if decision.usage_known:
                    accounting = {"input_tokens": decision.input_tokens, "output_tokens": decision.output_tokens}
                validate_next_decision(decision, registry=self.registry,
                                       visible_evidence_ids=set(context["visible_evidence_ids"]))
                if decision.decision_type == AgentStepType.COMPLETE:
                    current_ids = {item["id"] for item in context["evidence"]
                                   if item["freshness"] == "fresh" and item["uid_status"] != "mismatch"}
                    if (not set(decision.evidence_ids).issubset(current_ids)
                            or context["selection"]["conflicting_observations"]):
                        raise DecisionError("INSUFFICIENT_CURRENT_EVIDENCE", "conclusion needs current consistent evidence")
            except Exception as error:
                accounting = getattr(error, "accounting", accounting)
                fallback_code = getattr(error, "code", "MODEL_UNAVAILABLE")
                decision = None
            with self.session_factory.begin() as session:
                current = session.get(AgentRun, run.id)
                if accounting:
                    current.input_tokens_used += accounting["input_tokens"] - reserved_input
                    current.output_tokens_used += accounting["output_tokens"] - reserved_output
                self._audit(session, run.id, "agent_model.finished", {
                    "step_id": step_id, "usage_known": bool(accounting),
                    "actual_usage": accounting or None, "fallback_reason": fallback_code})
                exhausted = current.input_tokens_used + current.output_tokens_used > current.max_total_tokens
            if exhausted:
                AGENT_BUDGET_EXHAUSTED.labels("model").inc()
                raise DecisionError("MODEL_BUDGET_EXHAUSTED", "actual usage exceeded run budget")
        if decision is None:
            if context["selection"]["conflicting_observations"]:
                decision = NextDecision(AgentStepType.ASK_HUMAN, None, {},
                                        "Conflicting observations require confirmation", [], 0.0,
                                        stop_reason="CONFLICTING_EVIDENCE", fallback_error_code=fallback_code)
                validate_next_decision(decision, registry=self.registry, visible_evidence_ids=set())
                return decision
            fallback = getattr(self.reasoner, "fallback", RuleReasoner()) if getattr(self.reasoner, "uses_model", False) else self.reasoner
            now = utc_now()
            eligible = []
            for item in evidence:
                metadata = evidence_item(item, run, now, self.context_builder.evidence_max_bytes,
                                         self.context_builder.evidence_ttls)
                if (item.agent_run_id == run.id and metadata["freshness"] == "fresh"
                        and metadata["uid_status"] != "mismatch"):
                    eligible.append(item)
            decision = fallback.decide(context, incident, eligible, self.registry)
            decision = replace(decision, fallback_error_code=fallback_code or decision.fallback_error_code)
            validate_next_decision(decision, registry=self.registry,
                                   visible_evidence_ids={item.id for item in eligible})
        return decision

    def process(self, run_id: str) -> AgentRun:
        claimed = self._claim(run_id)
        if claimed.status != AgentRunStatus.RUNNING or claimed.lease_owner != self.worker_id:
            return claimed
        while True:
            run, incident, evidence, steps = self._snapshot(run_id)
            now = utc_now()
            if run.cancel_requested_at:
                return self._finish(run_id, AgentRunStatus.CANCELLED, "USER_CANCELLED")
            if _comparable(run.deadline_at) and now >= _comparable(run.deadline_at):
                return self._finish(run_id, AgentRunStatus.STOPPED, "RUN_DEADLINE_EXCEEDED")
            if run.current_step >= run.max_steps:
                AGENT_BUDGET_EXHAUSTED.labels("steps").inc()
                return self._finish(run_id, AgentRunStatus.STOPPED, "MAX_STEPS_EXHAUSTED")
            if run.tool_calls_used >= run.max_tool_calls:
                AGENT_BUDGET_EXHAUSTED.labels("tool_calls").inc()
                return self._finish(run_id, AgentRunStatus.STOPPED, "TOOL_BUDGET_EXHAUSTED")

            model_budget = None
            fallback_code = None
            if getattr(self.reasoner, "uses_model", False):
                primary = getattr(self.reasoner, "primary", self.reasoner)
                try:
                    model_budget = primary.budget(run)
                except DecisionError as error:
                    fallback_code = error.code
                    if error.code == "MODEL_BUDGET_EXHAUSTED":
                        AGENT_BUDGET_EXHAUSTED.labels("model").inc()
            try:
                context = self.context_builder.build(
                    run, incident, evidence, steps, self.registry, now=now, model_budget=model_budget)
            except DecisionError as error:
                if model_budget is None:
                    return self._finish(run_id, AgentRunStatus.STOPPED, error.code)
                model_budget, fallback_code = None, error.code
                try:
                    context = self.context_builder.build(run, incident, evidence, steps, self.registry, now=now)
                except DecisionError as error:
                    return self._finish(run_id, AgentRunStatus.STOPPED, error.code)
            sequence = run.current_step + 1
            rendered = context_json(context)
            AGENT_CONTEXT_BYTES.observe(len(rendered.encode("utf-8")))
            if context["selection"]["incomplete"]:
                AGENT_CONTEXT_TRIMMED.inc()
            with self.session_factory.begin() as session:
                step = AgentStep(
                    agent_run_id=run.id,
                    sequence=sequence,
                    idempotency_key=f"{run.id}:{sequence}",
                    context_version=CONTEXT_VERSION,
                    context_snapshot=context,
                    context_hash=hashlib.sha256(rendered.encode()).hexdigest(),
                )
                session.add(step)
                session.flush()
                step_id = step.id
                self._audit(session, run.id, "agent_step.started", {
                    "sequence": sequence, "context_version": CONTEXT_VERSION,
                    "selection": context["selection"], "budget": context.get("model_budget"),
                    "estimated_input": model_budget.estimate(context) if model_budget else None,
                })

            try:
                decision = self._decide(run, step_id, context, incident, evidence, model_budget, fallback_code)
            except (DecisionError, ToolError) as error:
                with self.session_factory.begin() as session:
                    step = session.get(AgentStep, step_id)
                    step.status = AgentStepStatus.FAILED
                    step.error_code = getattr(error, "code", "DECISION_FAILED")
                    step.finished_at = utc_now()
                    current = session.get(AgentRun, run.id)
                    current.current_step = sequence
                    self._audit(session, run.id, "agent_decision.failed", {"error_code": step.error_code})
                AGENT_DECISION_VALIDATION_FAILURES.labels(
                    getattr(error, "code", "DECISION_FAILED")
                ).inc()
                AGENT_STEPS.labels("UNKNOWN", "FAILED").inc()
                return self._finish(run_id,
                    AgentRunStatus.STOPPED if error.code == "MODEL_BUDGET_EXHAUSTED" else AgentRunStatus.FAILED,
                    getattr(error, "code", "DECISION_FAILED"))

            with self.session_factory.begin() as session:
                step = session.get(AgentStep, step_id)
                step.step_type = decision.decision_type
                step.decision_provider = decision.provider
                step.decision_summary = decision.decision_summary
                step.confidence = decision.confidence
                step.evidence_ids = decision.evidence_ids
                if decision.fallback_error_code:
                    self._audit(session, run.id, "agent_decision.fallback", {"error_code": decision.fallback_error_code})
                self._audit(session, run.id, "agent_decision.made", {"sequence": sequence, "type": decision.decision_type.value, "provider": decision.provider, "evidence_ids": decision.evidence_ids})

            with self.session_factory() as session:
                latest = session.get(AgentRun, run_id)
                if latest.status in self.TERMINAL:
                    return latest
                cancelled = latest.cancel_requested_at is not None
                expired = _comparable(latest.deadline_at) <= utc_now() if latest.deadline_at else False
            if cancelled or expired:
                with self.session_factory.begin() as session:
                    step = session.get(AgentStep, step_id)
                    step.status = AgentStepStatus.DENIED
                    step.finished_at = utc_now()
                    step.error_code = "USER_CANCELLED" if cancelled else "RUN_DEADLINE_EXCEEDED"
                return self._finish(run_id, AgentRunStatus.CANCELLED if cancelled else AgentRunStatus.STOPPED,
                                    "USER_CANCELLED" if cancelled else "RUN_DEADLINE_EXCEEDED")

            if decision.decision_type == AgentStepType.COMPLETE:
                with self.session_factory.begin() as session:
                    step = session.get(AgentStep, step_id)
                    step.status = AgentStepStatus.SUCCEEDED
                    step.finished_at = utc_now()
                    session.get(AgentRun, run.id).current_step = sequence
                AGENT_STEPS.labels("COMPLETE", "SUCCEEDED").inc()
                return self._finish(
                    run_id,
                    AgentRunStatus.COMPLETED,
                    "GOAL_COMPLETED",
                    {**decision.diagnosis, "provider": decision.provider},
                )
            if decision.decision_type == AgentStepType.ASK_HUMAN:
                with self.session_factory.begin() as session:
                    step = session.get(AgentStep, step_id)
                    step.status = AgentStepStatus.SUCCEEDED
                    step.finished_at = utc_now()
                    session.get(AgentRun, run.id).current_step = sequence
                AGENT_STEPS.labels("ASK_HUMAN", "SUCCEEDED").inc()
                return self._finish(run_id, AgentRunStatus.AWAITING_HUMAN, decision.stop_reason or "HUMAN_INPUT_REQUIRED")
            if decision.decision_type == AgentStepType.STOP:
                with self.session_factory.begin() as session:
                    step = session.get(AgentStep, step_id)
                    step.status = AgentStepStatus.SUCCEEDED
                    step.finished_at = utc_now()
                    session.get(AgentRun, run.id).current_step = sequence
                AGENT_STEPS.labels("STOP", "SUCCEEDED").inc()
                return self._finish(run_id, AgentRunStatus.STOPPED, decision.stop_reason or "REASONER_STOPPED")

            fingerprints = {
                invocation_fingerprint(item.tool_invocation.tool_name, item.tool_invocation.sanitized_arguments)
                for item in steps
                if item.tool_invocation
            }
            policy = self.policy.evaluate(run, incident, self.registry, decision.tool_name, decision.arguments, fingerprints)
            if policy.decision != PolicyDecision.ALLOW:
                with self.session_factory.begin() as session:
                    step = session.get(AgentStep, step_id)
                    step.status = AgentStepStatus.DENIED
                    step.error_code = policy.reason_code
                    step.finished_at = utc_now()
                    current = session.get(AgentRun, run.id)
                    current.current_step = sequence
                    self._audit(session, run.id, "agent_tool.denied", {"tool": decision.tool_name, "reason_code": policy.reason_code})
                AGENT_STEPS.labels("CALL_TOOL", "DENIED").inc()
                AGENT_TOOL_CALLS.labels(decision.tool_name, "DENIED").inc()
                terminal = AgentRunStatus.AWAITING_HUMAN if policy.decision == PolicyDecision.ASK_HUMAN else AgentRunStatus.STOPPED
                return self._finish(run_id, terminal, policy.reason_code)

            definition = self.registry.get(decision.tool_name)
            clean_args, _ = sanitize_content(decision.arguments, max_bytes=8000)
            with self.session_factory.begin() as session:
                invocation = ToolInvocation(
                    agent_step_id=step_id,
                    idempotency_key=f"{run.id}:{sequence}:{decision.tool_name}",
                    tool_name=decision.tool_name,
                    tool_version=definition.version,
                    sanitized_arguments=clean_args,
                    status=ToolInvocationStatus.RUNNING,
                    attempt_count=0,
                    started_at=utc_now(),
                )
                session.add(invocation)
                current = session.get(AgentRun, run.id)
                current.tool_calls_used += 1
                current.heartbeat_at = utc_now()
                current.lease_expires_at = utc_now() + timedelta(seconds=30)
                session.flush()
                invocation_id = invocation.id
                self._audit(session, run.id, "agent_tool.allowed", {"tool": decision.tool_name})

            started = time.monotonic()
            result = None
            tool_error = None
            for attempt in range(1, 3):
                try:
                    result = self.registry.invoke(decision.tool_name, decision.arguments, run.per_tool_timeout_seconds)
                    tool_error = None
                    break
                except ToolError as error:
                    tool_error = error
                    if not error.retryable or attempt >= 2:
                        break
                    time.sleep(0.25)
            duration_ms = int((time.monotonic() - started) * 1000)
            with self.session_factory.begin() as session:
                invocation = session.get(ToolInvocation, invocation_id)
                invocation.attempt_count = attempt
                invocation.duration_ms = duration_ms
                invocation.finished_at = utc_now()
                current = session.get(AgentRun, run.id)
                current.current_step = sequence
                step = session.get(AgentStep, step_id)
                if result is not None:
                    serialized = json.dumps(result.content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                    record = Evidence(
                        incident_id=incident.id,
                        agent_run_id=run.id,
                        evidence_type=result.evidence_type,
                        source=result.source,
                        content=result.content,
                        content_hash=hashlib.sha256(serialized.encode()).hexdigest(),
                        redacted=result.redacted,
                    )
                    session.add(record)
                    session.flush()
                    invocation.status = ToolInvocationStatus.SUCCEEDED
                    invocation.evidence_id = record.id
                    if decision.tool_name == "get_pod_status" and result.content.get("uid"):
                        target = dict(current.target_snapshot or {})
                        if not target.get("resource_uid"):
                            target["resource_uid"] = result.content["uid"]
                            current.target_snapshot = target
                    step.status = AgentStepStatus.SUCCEEDED
                    step.finished_at = utc_now()
                    self._audit(session, run.id, "agent_tool.succeeded", {"tool": decision.tool_name, "evidence_id": record.id, "duration_ms": duration_ms})
                    AGENT_STEPS.labels("CALL_TOOL", "SUCCEEDED").inc()
                    AGENT_TOOL_CALLS.labels(decision.tool_name, "SUCCEEDED").inc()
                else:
                    code = tool_error.code if tool_error else "TOOL_EXECUTION_ERROR"
                    error_content, _ = sanitize_content({"tool": decision.tool_name, "error_code": code})
                    serialized = json.dumps(error_content, sort_keys=True, separators=(",", ":"))
                    record = Evidence(
                        incident_id=incident.id,
                        agent_run_id=run.id,
                        evidence_type=EvidenceType.COLLECTION_ERROR,
                        source=decision.tool_name,
                        content=error_content,
                        content_hash=hashlib.sha256(serialized.encode()).hexdigest(),
                        redacted=True,
                    )
                    session.add(record)
                    session.flush()
                    invocation.status = ToolInvocationStatus.TIMED_OUT if code == "TOOL_TRANSIENT_ERROR" else ToolInvocationStatus.FAILED
                    invocation.error_code = code
                    invocation.evidence_id = record.id
                    step.status = AgentStepStatus.FAILED
                    step.error_code = code
                    step.finished_at = utc_now()
                    self._audit(session, run.id, "agent_tool.failed", {"tool": decision.tool_name, "error_code": code})
                    AGENT_STEPS.labels("CALL_TOOL", "FAILED").inc()
                    AGENT_TOOL_CALLS.labels(decision.tool_name, invocation.status.value).inc()
                AGENT_TOOL_DURATION.labels(decision.tool_name).observe(duration_ms / 1000)
            if result is None:
                return self._finish(
                    run_id,
                    AgentRunStatus.AWAITING_HUMAN,
                    tool_error.code if tool_error else "TOOL_EXECUTION_ERROR",
                )
