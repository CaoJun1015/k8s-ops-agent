"""Initialize a fresh metrics directory, then replace this process with Gunicorn."""

import os
from pathlib import Path


def main() -> None:
    metrics_dir = Path(
        os.environ.setdefault("PROMETHEUS_MULTIPROC_DIR", "/tmp/prometheus")
    )
    if not metrics_dir.is_absolute() or metrics_dir == Path(metrics_dir.anchor):
        raise RuntimeError("PROMETHEUS_MULTIPROC_DIR must be a safe absolute path")
    metrics_dir.mkdir(parents=True, exist_ok=True)
    for metric_file in metrics_dir.glob("*.db"):
        metric_file.unlink()
    os.execvp(
        "gunicorn",
        [
            "gunicorn",
            "--config",
            "gunicorn.conf.py",
            "--bind",
            "0.0.0.0:5000",
            "--workers",
            "2",
            "app:app",
        ],
    )


if __name__ == "__main__":
    main()
