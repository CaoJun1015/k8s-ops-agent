"""AgentRun 队列边界测试。

场景覆盖：
- 生产队列使用稳定的字符串任务入口，避免序列化 Flask 对象；
- 数据库 URL 与 Run ID 被显式传给 Worker；
- 作业 ID 与 AgentRun ID 一致，防止重复排队。
"""

from unittest.mock import MagicMock, patch

from ops_agent.queueing import enqueue_diagnosis


@patch("ops_agent.queueing.Queue")
def test_enqueue_diagnosis_uses_stable_job_contract(queue_class):
    """RQ 作业必须只携带可序列化、可追溯的最小参数。"""
    queue = MagicMock()
    queue_class.return_value = queue
    redis_connection = MagicMock()

    enqueue_diagnosis(
        redis_connection,
        "postgresql+psycopg://user:secret@postgres/ops_agent",
        "run-123",
    )

    queue_class.assert_called_once_with("agent-runs", connection=redis_connection)
    queue.enqueue.assert_called_once_with(
        "ops_agent.jobs.process_diagnosis_job",
        "postgresql+psycopg://user:secret@postgres/ops_agent",
        "run-123",
        job_id="agent-run:run-123",
        job_timeout=120,
        result_ttl=86400,
    )
