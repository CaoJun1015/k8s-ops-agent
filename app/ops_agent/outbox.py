"""Transactional outbox dispatcher for RQ jobs."""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone

import redis
from sqlalchemy import select

from ops_agent.database import Database
from ops_agent.domain import OutboxStatus
from ops_agent.evidence import sanitize_content
from ops_agent.models import OutboxEvent
from ops_agent.queueing import enqueue_diagnosis, enqueue_execution
from ops_agent.recovery import recover_stuck_work_once


MAX_ATTEMPTS = 8


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def dispatch_pending_once(
    session_factory,
    redis_connection,
    database_url: str,
    *,
    now: datetime | None = None,
    batch_size: int = 50,
) -> dict[str, int]:
    now = now or utc_now()
    result = {"published": 0, "failed": 0}
    with session_factory.begin() as session:
        events = list(
            session.scalars(
                select(OutboxEvent)
                .where(
                    OutboxEvent.status == OutboxStatus.PENDING,
                    OutboxEvent.available_at <= now,
                )
                .order_by(OutboxEvent.created_at)
                .limit(batch_size)
                .with_for_update(skip_locked=True)
            ).all()
        )
        for event in events:
            try:
                if event.topic == "agent_run.requested":
                    enqueue_diagnosis(
                        redis_connection,
                        database_url,
                        event.payload["run_id"],
                        job_id=event.id,
                    )
                elif event.topic == "execution.requested":
                    enqueue_execution(
                        redis_connection,
                        database_url,
                        event.payload["execution_id"],
                        job_id=event.id,
                    )
                else:
                    raise ValueError("unsupported outbox topic")
                event.status = OutboxStatus.PUBLISHED
                event.published_at = now
                event.last_error = None
                result["published"] += 1
            except Exception as error:
                event.attempts += 1
                clean, _ = sanitize_content(
                    {"error": str(error)}, max_bytes=2_000
                )
                event.last_error = str(clean.get("error") or clean.get("data"))
                if event.attempts >= MAX_ATTEMPTS:
                    event.status = OutboxStatus.FAILED
                else:
                    event.available_at = now + timedelta(
                        seconds=min(300, 2 ** event.attempts)
                    )
                result["failed"] += 1
    return result


def main() -> None:
    database_url = os.environ["DATABASE_URL"]
    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    poll_seconds = float(os.environ.get("OUTBOX_POLL_SECONDS", "1"))
    database = Database(database_url)
    redis_connection = redis.from_url(redis_url)
    last_recovery = 0.0
    while True:
        dispatch_pending_once(
            database.session_factory,
            redis_connection,
            database_url,
        )
        monotonic_now = time.monotonic()
        if monotonic_now - last_recovery >= 30:
            recover_stuck_work_once(
                database.session_factory,
                agent_timeout_seconds=int(
                    os.environ.get("AGENT_RUN_TIMEOUT_SECONDS", "300")
                ),
                execution_timeout_seconds=int(
                    os.environ.get("EXECUTION_TIMEOUT_SECONDS", "300")
                ),
            )
            last_recovery = monotonic_now
        time.sleep(max(0.1, poll_seconds))


if __name__ == "__main__":
    main()
