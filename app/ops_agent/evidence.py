"""Evidence collection boundaries, redaction and payload limiting."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from ops_agent.domain import EvidenceType


DEFAULT_LOG_TAIL_LINES = 200
DEFAULT_MAX_EVIDENCE_BYTES = 128 * 1024
SENSITIVE_KEYS = {
    "authorization",
    "cookie",
    "set-cookie",
    "password",
    "passwd",
    "pwd",
    "token",
    "access_token",
    "refresh_token",
    "api_key",
    "apikey",
    "secret",
    "client_secret",
}
INLINE_SECRET = re.compile(
    r"(?i)(authorization\s*[:=]\s*(?:bearer\s+)?|"
    r"password\s*[:=]\s*|passwd\s*[:=]\s*|pwd\s*[:=]\s*|"
    r"access[_-]?token\s*[:=]\s*|refresh[_-]?token\s*[:=]\s*|"
    r"api[_-]?key\s*[:=]\s*|secret\s*[:=]\s*|cookie\s*[:=]\s*)"
    r"([^\s,;]+)"
)


@dataclass(frozen=True)
class CollectedEvidence:
    evidence_type: EvidenceType
    source: str
    content: dict[str, Any]
    redacted: bool


def _redact(value: Any) -> tuple[Any, bool]:
    if isinstance(value, dict):
        result = {}
        changed = False
        for key, item in value.items():
            if str(key).lower() in SENSITIVE_KEYS:
                result[key] = "[REDACTED]"
                changed = True
            else:
                result[key], item_changed = _redact(item)
                changed = changed or item_changed
        return result, changed
    if isinstance(value, list):
        result = []
        changed = False
        for item in value:
            clean, item_changed = _redact(item)
            result.append(clean)
            changed = changed or item_changed
        return result, changed
    if isinstance(value, str):
        clean, replacements = INLINE_SECRET.subn(r"\1[REDACTED]", value)
        return clean, replacements > 0
    return value, False


def _truncate_utf8(value: str, max_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def sanitize_content(
    content: dict[str, Any], *, max_bytes: int = DEFAULT_MAX_EVIDENCE_BYTES
) -> tuple[dict[str, Any], bool]:
    """Redact secrets and return an explicitly truncated JSON-safe payload."""
    clean, redacted = _redact(content)
    rendered = json.dumps(clean, ensure_ascii=False, sort_keys=True, default=str)
    if len(rendered.encode("utf-8")) <= max_bytes:
        return clean, redacted

    envelope_overhead = 128
    truncated = _truncate_utf8(rendered, max(0, max_bytes - envelope_overhead))
    return {
        "data": truncated,
        "truncated": True,
        "original_bytes": len(rendered.encode("utf-8")),
    }, redacted


class EvidenceCollector:
    """Collect read-only, bounded diagnostic evidence for one Incident."""

    POD_EVIDENCE_TYPES = {
        "pod_status": EvidenceType.POD_STATUS,
        "current_logs": EvidenceType.CURRENT_LOGS,
        "previous_logs": EvidenceType.PREVIOUS_LOGS,
        "events": EvidenceType.K8S_EVENTS,
        "workload_status": EvidenceType.WORKLOAD_STATUS,
        "resource_limits": EvidenceType.RESOURCE_LIMITS,
    }

    def __init__(
        self,
        kubernetes_adapter=None,
        prometheus_adapter=None,
        *,
        max_bytes=None,
    ):
        self.kubernetes_adapter = kubernetes_adapter
        self.prometheus_adapter = prometheus_adapter
        self.max_bytes = max_bytes or DEFAULT_MAX_EVIDENCE_BYTES

    def _item(self, evidence_type, source, content) -> CollectedEvidence:
        clean, redacted = sanitize_content(content, max_bytes=self.max_bytes)
        return CollectedEvidence(evidence_type, source, clean, redacted)

    def collect(self, incident) -> list[CollectedEvidence]:
        items = [
            self._item(
                EvidenceType.ALERT_PAYLOAD,
                incident.source,
                {
                    **(incident.source_context or {}),
                    "incident": {
                        "title": incident.title,
                        "summary": incident.summary,
                        "severity": incident.severity.value,
                        "fingerprint": incident.fingerprint,
                        "cluster": incident.cluster,
                        "namespace": incident.namespace,
                        "resource_kind": incident.resource_kind,
                        "resource_name": incident.resource_name,
                    },
                },
            )
        ]
        if incident.resource_kind.lower() == "pod" and self.kubernetes_adapter:
            try:
                raw = self.kubernetes_adapter.collect_pod_evidence(
                    namespace=incident.namespace,
                    pod_name=incident.resource_name,
                    log_tail_lines=DEFAULT_LOG_TAIL_LINES,
                )
                for key, evidence_type in self.POD_EVIDENCE_TYPES.items():
                    if key in raw and raw[key] is not None:
                        value = raw[key]
                        content = (
                            value if isinstance(value, dict) else {"items": value}
                        )
                        items.append(
                            self._item(evidence_type, "kubernetes", content)
                        )
            except Exception as error:
                items.append(
                    self._item(
                        EvidenceType.COLLECTION_ERROR,
                        "kubernetes",
                        {
                            "stage": "pod_evidence",
                            "error_type": type(error).__name__,
                            "message": "Kubernetes evidence collection failed",
                        },
                    )
                )

        if self.prometheus_adapter:
            try:
                metrics = self.prometheus_adapter.collect_incident_metrics(incident)
                if metrics.get("queries"):
                    items.append(
                        self._item(
                            EvidenceType.PROMETHEUS_METRICS,
                            "prometheus",
                            metrics,
                        )
                    )
            except Exception as error:
                items.append(
                    self._item(
                        EvidenceType.COLLECTION_ERROR,
                        "prometheus",
                        {
                            "stage": "incident_metrics",
                            "error_type": type(error).__name__,
                            "message": "Prometheus evidence collection failed",
                        },
                    )
                )
        return items
