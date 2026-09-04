"""Serializable worker entrypoints."""

import os

import redis

from ops_agent.auto_remediation import attempt_auto_remediation
from ops_agent.database import Database
from ops_agent.services import OpsService
from ops_agent.kubernetes_adapter import KubernetesAdapter
from ops_agent.evidence import EvidenceCollector
from ops_agent.diagnosis import build_diagnosis_pipeline
from ops_agent.prometheus_adapter import PrometheusAdapter
from ops_agent.agent_core import (
    AgentOrchestrator,
    FallbackReasoner,
    OpenAIDecisionReasoner,
    RuleReasoner,
)
from ops_agent.tooling import build_read_only_registry


def process_diagnosis_job(database_url: str, run_id: str) -> None:
    database = Database(database_url)
    kubernetes_adapter = KubernetesAdapter()
    prometheus_url = os.environ.get("PROMETHEUS_URL", "")
    prometheus_adapter = (
        PrometheusAdapter(prometheus_url) if prometheus_url else None
    )
    agent_core_enabled = os.environ.get("AGENT_CORE_ENABLED", "true").lower() == "true"
    registry = build_read_only_registry(
        kubernetes_adapter,
        prometheus_adapter,
        allowed_namespaces={
            item.strip()
            for item in os.environ.get("ALLOWED_NAMESPACES", "default").split(",")
            if item.strip()
        },
    )
    reasoner = RuleReasoner()
    if os.environ.get("AGENT_REASONER_PROVIDER", "rules") == "rules+openai":
        from openai import OpenAI

        reasoner = FallbackReasoner(
            OpenAIDecisionReasoner(
                OpenAI(
                    api_key=os.environ["OPENAI_API_KEY"], timeout=15.0, max_retries=1
                ),
                os.environ.get("OPENAI_MODEL", "gpt-5.4-mini"),
            )
        )
    orchestrator = AgentOrchestrator(
        database.session_factory, registry, reasoner=reasoner
    )
    service = OpsService(
        database.session_factory,
        evidence_collector=EvidenceCollector(
            kubernetes_adapter, prometheus_adapter
        ),
        diagnosis_engine=build_diagnosis_pipeline(
            os.environ.get("DIAGNOSIS_PROVIDER", "rules"),
            api_key=os.environ.get("OPENAI_API_KEY"),
            model=os.environ.get("OPENAI_MODEL", "gpt-5.4-mini"),
        ),
        agent_orchestrator=orchestrator,
        agent_core_enabled=agent_core_enabled,
    )
    try:
        run = service.process_diagnosis(run_id)
        if (
            not agent_core_enabled
            and os.environ.get("AUTO_REMEDIATE_ENABLED", "false").lower() == "true"
        ):
            redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
            attempt_auto_remediation(
                service=service,
                incident_id=run.incident_id,
                kubernetes_adapter=kubernetes_adapter,
                allowed_namespaces={
                    item.strip()
                    for item in os.environ.get(
                        "ALLOWED_NAMESPACES", "default"
                    ).split(",")
                    if item.strip()
                },
                execution_mode=os.environ.get("EXECUTION_MODE", "rq"),
                redis_connection=redis.from_url(redis_url),
                database_url=database_url,
            )
    except Exception as error:
        service.fail_agent_run(run_id, str(error))
        raise


def process_execution_job(database_url: str, execution_id: str) -> None:
    database = Database(database_url)
    service = OpsService(database.session_factory)
    try:
        service.process_execution(execution_id, KubernetesAdapter())
    except Exception as error:
        service.fail_execution(execution_id, str(error))
        raise
