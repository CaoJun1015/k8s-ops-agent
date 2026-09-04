"""Gunicorn hooks required by prometheus-client multiprocess mode."""

from prometheus_client import multiprocess


def child_exit(_server, worker):
    multiprocess.mark_process_dead(worker.pid)
