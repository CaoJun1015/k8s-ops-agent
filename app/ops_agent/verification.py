"""Read-only verification used for resolved alerts."""

from __future__ import annotations


class IncidentVerifier:
    def __init__(self, kubernetes_adapter=None, prometheus_adapter=None):
        self.kubernetes_adapter = kubernetes_adapter
        self.prometheus_adapter = prometheus_adapter

    def verify(self, incident) -> dict:
        checks = []
        resource_healthy = None
        if (
            str(incident.resource_kind).lower() == "pod"
            and self.kubernetes_adapter is not None
        ):
            try:
                inspection = self.kubernetes_adapter.inspect_pod(
                    namespace=incident.namespace,
                    pod_name=incident.resource_name,
                )
                resource_healthy = bool(
                    inspection.get("exists")
                    and inspection.get("ready")
                    and not inspection.get("is_abnormal")
                )
                checks.append(
                    {
                        "source": "kubernetes",
                        "healthy": resource_healthy,
                        "phase": inspection.get("phase"),
                        "reason": inspection.get("reason"),
                    }
                )
            except Exception as error:
                checks.append(
                    {
                        "source": "kubernetes",
                        "healthy": False,
                        "error_type": type(error).__name__,
                    }
                )
                resource_healthy = False

        metrics_healthy = None
        if self.prometheus_adapter is not None:
            try:
                metrics = self.prometheus_adapter.collect_incident_metrics(incident)
                evaluated = self._evaluate_metrics(metrics)
                if evaluated is not None:
                    metrics_healthy = evaluated
                    checks.append(
                        {
                            "source": "prometheus",
                            "healthy": evaluated,
                            "query_count": len(metrics.get("queries") or []),
                        }
                    )
            except Exception as error:
                checks.append(
                    {
                        "source": "prometheus",
                        "healthy": False,
                        "error_type": type(error).__name__,
                    }
                )
                metrics_healthy = False

        determinate = [
            value for value in (resource_healthy, metrics_healthy) if value is not None
        ]
        return {
            "healthy": bool(determinate) and all(determinate),
            "checks": checks,
            "reason": None if determinate else "no verification source available",
        }

    @staticmethod
    def _evaluate_metrics(metrics: dict):
        decisions = []
        for query in metrics.get("queries") or []:
            values = []
            for sample in query.get("result") or []:
                value = sample.get("value") or []
                if len(value) >= 2:
                    try:
                        values.append(float(value[1]))
                    except (TypeError, ValueError):
                        continue
            if query.get("name") == "pod_ready" and values:
                decisions.append(any(value >= 1 for value in values))
            elif query.get("name") == "redis_up" and values:
                decisions.append(any(value >= 1 for value in values))
        return all(decisions) if decisions else None
