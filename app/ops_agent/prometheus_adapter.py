"""Small read-only Prometheus HTTP API adapter."""

from __future__ import annotations

import json
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen


class PrometheusQueryError(RuntimeError):
    pass


def _default_transport(url: str, timeout: float) -> dict:
    request = Request(url, headers={"Accept": "application/json"})
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _label_value(value: str) -> str:
    return json.dumps(str(value), ensure_ascii=False)


class PrometheusAdapter:
    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 5.0,
        transport=None,
    ):
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("PROMETHEUS_URL must be an absolute HTTP(S) URL")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.transport = transport or _default_transport

    def query(self, expression: str) -> dict:
        url = f"{self.base_url}/api/v1/query?{urlencode({'query': expression})}"
        payload = self.transport(url, self.timeout)
        if payload.get("status") != "success" or not isinstance(
            payload.get("data"), dict
        ):
            raise PrometheusQueryError("Prometheus query did not succeed")
        data = payload["data"]
        result = data.get("result")
        if not isinstance(result, list):
            raise PrometheusQueryError("Prometheus returned an invalid result")
        return {
            "result_type": data.get("resultType"),
            "result": result[:100],
        }

    def collect_incident_metrics(self, incident) -> dict:
        queries = []
        if str(incident.resource_kind).lower() == "pod":
            namespace = _label_value(incident.namespace)
            pod_name = _label_value(incident.resource_name)
            definitions = {
                "pod_ready": (
                    "kube_pod_status_ready{condition=\"true\","
                    f"namespace={namespace},pod={pod_name}}}"
                ),
                "pod_restarts": (
                    "kube_pod_container_status_restarts_total{"
                    f"namespace={namespace},pod={pod_name}}}"
                ),
            }
        elif "redis" in " ".join(
            filter(None, [incident.title, incident.summary or ""])
        ).lower():
            definitions = {"redis_up": "redis_up"}
        else:
            definitions = {}

        for name, expression in definitions.items():
            queries.append(
                {
                    "name": name,
                    "query": expression,
                    **self.query(expression),
                }
            )
        return {"queries": queries}
