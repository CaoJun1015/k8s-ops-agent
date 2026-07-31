"""Prometheus metrics owned by the Ops Agent process."""

from prometheus_client import Counter, Gauge, Histogram, Info

APP_INFO = Info("ops_agent", "K8s Ops Agent build information")
APP_INFO.info({"version": "0.1.0"})

HTTP_REQUESTS = Counter(
    "ops_agent_http_requests_total",
    "HTTP requests handled by the Ops Agent",
    ("method", "endpoint", "status"),
)
HTTP_DURATION = Histogram(
    "ops_agent_http_request_duration_seconds",
    "HTTP request duration",
    ("method", "endpoint"),
)
INCIDENTS_CREATED = Counter(
    "ops_agent_incidents_created_total",
    "Incidents created from all sources",
    ("severity", "source"),
)
AGENT_RUNS = Counter(
    "ops_agent_runs_total",
    "Agent runs by terminal status",
    ("mode", "status"),
)
REMEDIATION_EXECUTIONS = Counter(
    "ops_agent_remediation_executions_total",
    "Remediation executions by action and status",
    ("action_type", "status"),
)
OPEN_INCIDENTS = Gauge(
    "ops_agent_open_incidents",
    "Current incidents not resolved or closed",
)
RECENT_FAILED_RUNS = Gauge(
    "ops_agent_recent_failed_runs",
    "Agent runs failed in the last ten minutes",
)
RECENT_FAILED_EXECUTIONS = Gauge(
    "ops_agent_recent_failed_executions",
    "Remediation executions failed verification in the last ten minutes",
)
