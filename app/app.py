"""Flask application entrypoint for K8s Ops Agent."""

import os
import time
from datetime import datetime, timedelta, timezone

import redis
from flask import Flask, Response, g, jsonify, render_template, request
from prometheus_client import CONTENT_TYPE_LATEST
from sqlalchemy import text

from ops_agent.api import api
from ops_agent.database import Database
from ops_agent.services import OpsService
from ops_agent.metrics import HTTP_DURATION, HTTP_REQUESTS, render_metrics
from ops_agent.metrics import (
    OPEN_INCIDENTS,
    RECENT_FAILED_EXECUTIONS,
    RECENT_FAILED_RUNS,
)
from ops_agent.domain import AgentRunStatus, ExecutionStatus, IncidentStatus
from ops_agent.models import AgentRun, Execution, Incident
from sqlalchemy import func, select
from ops_agent.kubernetes_adapter import KubernetesAdapter
from ops_agent.evidence import EvidenceCollector
from ops_agent.diagnosis import build_diagnosis_pipeline
from ops_agent.prometheus_adapter import PrometheusAdapter
from ops_agent.verification import IncidentVerifier
from ops_agent.agent_core import (
    AgentOrchestrator,
    FallbackReasoner,
    OpenAIDecisionReasoner,
    RuleReasoner,
)
from ops_agent.tooling import build_read_only_registry


def create_app(overrides=None) -> Flask:
    application = Flask(__name__)
    database_url = os.environ.get(
        "DATABASE_URL", "sqlite+pysqlite:///:memory:"
    )
    application.config.from_mapping(
        DATABASE_URL=database_url,
        REDIS_URL=os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
        QUEUE_MODE=os.environ.get("QUEUE_MODE", "rq"),
        EXECUTION_MODE=os.environ.get("EXECUTION_MODE", "rq"),
        CHECK_REDIS=True,
        AUTO_CREATE_SCHEMA=database_url.startswith("sqlite"),
        DIAGNOSIS_PROVIDER=os.environ.get("DIAGNOSIS_PROVIDER", "rules"),
        AGENT_CORE_ENABLED=os.environ.get("AGENT_CORE_ENABLED", "true").lower()
        == "true",
        AGENT_REASONER_PROVIDER=os.environ.get("AGENT_REASONER_PROVIDER", "rules"),
        OPENAI_API_KEY=os.environ.get("OPENAI_API_KEY", ""),
        OPENAI_MODEL=os.environ.get("OPENAI_MODEL", "gpt-5.4-mini"),
        PROMETHEUS_URL=os.environ.get("PROMETHEUS_URL", ""),
        AUTO_REMEDIATE_ENABLED=os.environ.get(
            "AUTO_REMEDIATE_ENABLED", "false"
        ).lower()
        == "true",
        ALLOWED_NAMESPACES={
            item.strip()
            for item in os.environ.get("ALLOWED_NAMESPACES", "default").split(",")
            if item.strip()
        },
        ALERT_WEBHOOK_TOKEN=os.environ.get("ALERT_WEBHOOK_TOKEN", ""),
        REQUIRE_OPERATOR_AUTH=os.environ.get(
            "REQUIRE_OPERATOR_AUTH", "false"
        ).lower()
        == "true",
        OPERATOR_API_TOKEN=os.environ.get("OPERATOR_API_TOKEN", ""),
    )
    if overrides:
        application.config.update(overrides)
    if application.config["TESTING"]:
        application.config["CHECK_REDIS"] = False
        if not overrides or "AGENT_CORE_ENABLED" not in overrides:
            application.config["AGENT_CORE_ENABLED"] = False

    database = Database(application.config["DATABASE_URL"])
    if application.config["AUTO_CREATE_SCHEMA"]:
        database.create_schema()
    application.extensions["database"] = database
    application.extensions["redis"] = redis.from_url(
        application.config["REDIS_URL"], decode_responses=True
    )
    application.extensions["kubernetes_adapter"] = application.config.get(
        "KUBERNETES_ADAPTER"
    ) or KubernetesAdapter()
    prometheus_adapter = application.config.get("PROMETHEUS_ADAPTER")
    if prometheus_adapter is None and application.config["PROMETHEUS_URL"]:
        prometheus_adapter = PrometheusAdapter(
            application.config["PROMETHEUS_URL"]
        )
    application.extensions["prometheus_adapter"] = prometheus_adapter
    application.extensions["incident_verifier"] = IncidentVerifier(
        application.extensions["kubernetes_adapter"], prometheus_adapter
    )
    registry = build_read_only_registry(
        application.extensions["kubernetes_adapter"],
        prometheus_adapter,
        allowed_namespaces=application.config["ALLOWED_NAMESPACES"],
    )
    reasoner = application.config.get("AGENT_REASONER")
    if reasoner is None:
        provider = application.config["AGENT_REASONER_PROVIDER"]
        if provider == "rules":
            reasoner = RuleReasoner()
        elif provider == "rules+openai":
            client = application.config.get("OPENAI_AGENT_CLIENT")
            if client is None:
                if not application.config["OPENAI_API_KEY"]:
                    raise ValueError("OPENAI_API_KEY is required for rules+openai")
                from openai import OpenAI

                client = OpenAI(
                    api_key=application.config["OPENAI_API_KEY"],
                    timeout=15.0,
                    max_retries=1,
                )
            reasoner = FallbackReasoner(
                OpenAIDecisionReasoner(client, application.config["OPENAI_MODEL"])
            )
        else:
            raise ValueError(f"unsupported Agent reasoner provider: {provider}")
    orchestrator = AgentOrchestrator(
        database.session_factory,
        registry,
        reasoner=reasoner,
    )
    application.extensions["agent_registry"] = registry
    application.extensions["agent_orchestrator"] = orchestrator
    application.extensions["ops_service"] = OpsService(
        database.session_factory,
        evidence_collector=EvidenceCollector(
            application.extensions["kubernetes_adapter"],
            prometheus_adapter,
        ),
        diagnosis_engine=build_diagnosis_pipeline(
            application.config["DIAGNOSIS_PROVIDER"],
            api_key=application.config["OPENAI_API_KEY"],
            model=application.config["OPENAI_MODEL"],
            client=application.config.get("OPENAI_DIAGNOSIS_CLIENT"),
        ),
        agent_orchestrator=orchestrator,
        agent_core_enabled=application.config["AGENT_CORE_ENABLED"],
    )
    application.register_blueprint(api)

    @application.before_request
    def start_request_timer():
        g.request_started_at = time.perf_counter()

    @application.after_request
    def record_request_metrics(response):
        endpoint = request.endpoint or "unmatched"
        HTTP_REQUESTS.labels(
            request.method, endpoint, str(response.status_code)
        ).inc()
        started_at = getattr(g, "request_started_at", None)
        if started_at is not None:
            HTTP_DURATION.labels(request.method, endpoint).observe(
                time.perf_counter() - started_at
            )
        return response

    @application.get("/")
    def index():
        return render_template("index.html")

    @application.get("/live")
    def live():
        return jsonify({"status": "ok"})

    @application.get("/ready")
    def ready():
        checks = {}
        try:
            with database.session_factory() as session:
                session.execute(text("SELECT 1"))
            checks["database"] = "connected"
        except Exception:
            checks["database"] = "disconnected"

        if application.config["CHECK_REDIS"]:
            try:
                application.extensions["redis"].ping()
                checks["redis"] = "connected"
            except Exception:
                checks["redis"] = "disconnected"
        else:
            checks["redis"] = "skipped"

        ready_status = all(
            value in {"connected", "skipped"} for value in checks.values()
        )
        return (
            jsonify(
                {
                    "status": "ok" if ready_status else "error",
                    "checks": checks,
                    "pod": os.environ.get("HOSTNAME", "unknown"),
                    "node": os.environ.get("NODE_NAME", "unknown"),
                }
            ),
            200 if ready_status else 503,
        )

    @application.get("/health")
    def legacy_health():
        return ready()

    @application.get("/metrics")
    def metrics():
        with database.session_factory() as session:
            open_count = session.scalar(
                select(func.count())
                .select_from(Incident)
                .where(
                    Incident.status.notin_(
                        [IncidentStatus.RESOLVED, IncidentStatus.CLOSED]
                    )
                )
            )
        OPEN_INCIDENTS.set(open_count or 0)
        since = datetime.now(timezone.utc) - timedelta(minutes=10)
        with database.session_factory() as session:
            RECENT_FAILED_RUNS.set(
                session.scalar(
                    select(func.count())
                    .select_from(AgentRun)
                    .where(
                        AgentRun.status == AgentRunStatus.FAILED,
                        AgentRun.finished_at >= since,
                    )
                )
                or 0
            )
            RECENT_FAILED_EXECUTIONS.set(
                session.scalar(
                    select(func.count())
                    .select_from(Execution)
                    .where(
                        Execution.status.in_(
                            [
                                ExecutionStatus.FAILED,
                                ExecutionStatus.VERIFICATION_FAILED,
                            ]
                        ),
                        Execution.finished_at >= since,
                    )
                )
                or 0
            )
        return Response(render_metrics(), mimetype=CONTENT_TYPE_LATEST)

    return application


app = create_app()


if __name__ == "__main__":
    debug_mode = os.environ.get("FLASK_DEBUG", "False").lower() == "true"
    app.run(host="0.0.0.0", port=5000, debug=debug_mode)
