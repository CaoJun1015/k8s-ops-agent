"""Explicit read-only tool registry for v0.3 Agent Core."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from jsonschema import Draft202012Validator

from ops_agent.domain import EvidenceType
from ops_agent.evidence import DEFAULT_LOG_TAIL_LINES, sanitize_content


class ToolError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class ToolResult:
    evidence_type: EvidenceType
    source: str
    content: dict[str, Any]
    redacted: bool = False


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    version: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    read_only: bool
    risk_level: str
    requires_approval: bool
    timeout_seconds: int
    allowed_clusters: frozenset[str]
    allowed_namespaces: frozenset[str]
    handler: Callable[[dict[str, Any], int], ToolResult]


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, ToolDefinition] = {}

    def register(self, definition: ToolDefinition) -> None:
        if not definition.read_only:
            raise ValueError("v0.3 registry accepts read-only tools only")
        if definition.name in self._tools:
            raise ValueError(f"duplicate tool: {definition.name}")
        Draft202012Validator.check_schema(definition.input_schema)
        Draft202012Validator.check_schema(definition.output_schema)
        self._tools[definition.name] = definition

    def get(self, name: str) -> ToolDefinition:
        try:
            return self._tools[name]
        except KeyError as error:
            raise ToolError("TOOL_NOT_REGISTERED", "tool is not registered") from error

    def definitions(self) -> list[ToolDefinition]:
        return [self._tools[name] for name in sorted(self._tools)]

    def invoke(self, name: str, arguments: dict[str, Any], timeout_seconds: int) -> ToolResult:
        definition = self.get(name)
        errors = sorted(
            Draft202012Validator(definition.input_schema).iter_errors(arguments),
            key=lambda item: list(item.path),
        )
        if errors:
            raise ToolError("INVALID_TOOL_ARGUMENTS", errors[0].message)
        try:
            result = definition.handler(arguments, min(timeout_seconds, definition.timeout_seconds))
        except ToolError:
            raise
        except (TimeoutError, ConnectionError) as error:
            raise ToolError("TOOL_TRANSIENT_ERROR", "read-only tool timed out", retryable=True) from error
        except Exception as error:
            raise ToolError("TOOL_EXECUTION_ERROR", type(error).__name__) from error
        output_errors = list(Draft202012Validator(definition.output_schema).iter_errors(result.content))
        if output_errors:
            raise ToolError("INVALID_TOOL_OUTPUT", output_errors[0].message)
        clean, redacted = sanitize_content(result.content)
        return ToolResult(result.evidence_type, result.source, clean, result.redacted or redacted)


def _object_schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": required,
    }


def build_read_only_registry(
    kubernetes_adapter,
    prometheus_adapter=None,
    *,
    cluster: str = "default",
    allowed_namespaces: set[str] | None = None,
    timeout_seconds: int = 15,
) -> ToolRegistry:
    registry = ToolRegistry()
    namespaces = frozenset(allowed_namespaces or {"default"})
    clusters = frozenset({cluster})
    base = {
        "cluster": {"type": "string", "const": cluster},
        "namespace": {"type": "string", "minLength": 1, "maxLength": 100},
    }
    output = {"type": "object"}

    def fallback(key: str, args: dict[str, Any]) -> Any:
        raw = kubernetes_adapter.collect_pod_evidence(
            namespace=args["namespace"],
            pod_name=args["pod_name"],
            log_tail_lines=args.get("tail_lines", DEFAULT_LOG_TAIL_LINES),
        )
        return raw[key]

    def handler(method: str, evidence_type: EvidenceType, fallback_key: str | None = None, **fixed):
        def call(args: dict[str, Any], call_timeout: int) -> ToolResult:
            function = getattr(kubernetes_adapter, method, None)
            if function is None and fallback_key:
                value = fallback(fallback_key, args)
            else:
                kwargs = {key: value for key, value in args.items() if key != "cluster"}
                kwargs.update(fixed)
                kwargs["timeout_seconds"] = call_timeout
                value = function(**kwargs)
            content = value if isinstance(value, dict) else {"items": value}
            return ToolResult(evidence_type, "kubernetes", content)
        return call

    pod_props = {**base, "pod_name": {"type": "string", "minLength": 1, "maxLength": 253}}
    specs = [
        ("get_pod_status", "Read Pod phase and container state", _object_schema(pod_props, ["cluster", "namespace", "pod_name"]), handler("get_pod_status", EvidenceType.POD_STATUS, "pod_status")),
        ("get_pod_logs", "Read bounded current Pod logs", _object_schema({**pod_props, "tail_lines": {"type": "integer", "minimum": 1, "maximum": 200}}, ["cluster", "namespace", "pod_name"]), handler("get_pod_logs", EvidenceType.CURRENT_LOGS, "current_logs", previous=False)),
        ("get_previous_logs", "Read bounded previous Pod logs", _object_schema({**pod_props, "tail_lines": {"type": "integer", "minimum": 1, "maximum": 200}}, ["cluster", "namespace", "pod_name"]), handler("get_pod_logs", EvidenceType.PREVIOUS_LOGS, "previous_logs", previous=True)),
        ("get_resource_limits", "Read Pod requests and limits", _object_schema(pod_props, ["cluster", "namespace", "pod_name"]), handler("get_resource_limits", EvidenceType.RESOURCE_LIMITS, "resource_limits")),
    ]
    for name, description, schema, function in specs:
        registry.register(ToolDefinition(name, "1.0", description, schema, output, True, "LOW", False, timeout_seconds, clusters, namespaces, function))

    event_schema = _object_schema({**base, "resource_uid": {"type": "string", "minLength": 1, "maxLength": 100}}, ["cluster", "namespace", "resource_uid"])
    registry.register(ToolDefinition("get_kubernetes_events", "1.0", "Read events for the Incident resource UID", event_schema, output, True, "LOW", False, timeout_seconds, clusters, namespaces, handler("get_kubernetes_events", EvidenceType.K8S_EVENTS)))

    workload_props = {**base, "resource_kind": {"type": "string", "enum": ["Pod", "Deployment", "StatefulSet"]}, "resource_name": {"type": "string", "minLength": 1, "maxLength": 253}}
    registry.register(ToolDefinition("get_workload_status", "1.0", "Read owner workload replica status", _object_schema(workload_props, ["cluster", "namespace", "resource_kind", "resource_name"]), output, True, "LOW", False, timeout_seconds, clusters, namespaces, handler("get_workload_status", EvidenceType.WORKLOAD_STATUS, "workload_status")))

    related_props = {**base, "workload_kind": {"type": "string", "enum": ["Deployment", "StatefulSet"]}, "workload_name": {"type": "string", "minLength": 1, "maxLength": 253}}
    registry.register(ToolDefinition("list_related_pods", "1.0", "List Pods selected by the Incident workload", _object_schema(related_props, ["cluster", "namespace", "workload_kind", "workload_name"]), output, True, "LOW", False, timeout_seconds, clusters, namespaces, handler("list_related_pods", EvidenceType.POD_STATUS)))

    rollout_props = {**base, "deployment_name": {"type": "string", "minLength": 1, "maxLength": 253}}
    registry.register(ToolDefinition("get_rollout_history", "1.0", "Read Deployment ReplicaSet revisions", _object_schema(rollout_props, ["cluster", "namespace", "deployment_name"]), output, True, "LOW", False, timeout_seconds, clusters, namespaces, handler("get_rollout_history", EvidenceType.WORKLOAD_STATUS)))

    if prometheus_adapter:
        query_names = sorted(getattr(prometheus_adapter, "QUERY_TEMPLATES", {}))
        prom_props = {**base, "query_name": {"type": "string", "enum": query_names}, "resource_name": {"type": "string", "minLength": 1, "maxLength": 253}}
        def prometheus_handler(args: dict[str, Any], call_timeout: int) -> ToolResult:
            result = prometheus_adapter.query_named(
                args["query_name"], namespace=args["namespace"], resource_name=args["resource_name"], timeout=call_timeout
            )
            return ToolResult(EvidenceType.PROMETHEUS_METRICS, "prometheus", result)
        registry.register(ToolDefinition("query_prometheus", "1.0", "Run one bounded named Prometheus query", _object_schema(prom_props, ["cluster", "namespace", "query_name", "resource_name"]), output, True, "LOW", False, timeout_seconds, clusters, namespaces, prometheus_handler))
    return registry
