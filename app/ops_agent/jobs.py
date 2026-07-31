"""Serializable worker entrypoints."""

import os

import redis

from ops_agent.auto_remediation import attempt_auto_remediation
from ops_agent.database import Database
from ops_agent.services import OpsService
from ops_agent.kubernetes_adapter import KubernetesAdapter


def process_diagnosis_job(database_url: str, run_id: str) -> None:
    database = Database(database_url)
    service = OpsService(database.session_factory)
    try:
        run = service.process_diagnosis(run_id)
        if os.environ.get("AUTO_REMEDIATE_ENABLED", "false").lower() == "true":
            redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
            attempt_auto_remediation(
                service=service,
                incident_id=run.incident_id,
                kubernetes_adapter=KubernetesAdapter(),
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
    service.process_execution(execution_id, KubernetesAdapter())
