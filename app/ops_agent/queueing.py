"""Redis Queue adapter for background AgentRun jobs."""

from rq import Queue


def enqueue_diagnosis(
    redis_connection,
    database_url: str,
    run_id: str,
    *,
    job_id: str | None = None,
) -> None:
    queue = Queue("agent-runs", connection=redis_connection)
    if job_id and queue.fetch_job(job_id) is not None:
        return
    queue.enqueue(
        "ops_agent.jobs.process_diagnosis_job",
        database_url,
        run_id,
        job_id=job_id or f"agent-run:{run_id}",
        job_timeout=330,
        result_ttl=86400,
    )


def enqueue_execution(
    redis_connection,
    database_url: str,
    execution_id: str,
    *,
    job_id: str | None = None,
) -> None:
    queue = Queue("executions", connection=redis_connection)
    if job_id and queue.fetch_job(job_id) is not None:
        return
    queue.enqueue(
        "ops_agent.jobs.process_execution_job",
        database_url,
        execution_id,
        job_id=job_id or f"execution:{execution_id}",
        job_timeout=180,
        result_ttl=86400,
    )
