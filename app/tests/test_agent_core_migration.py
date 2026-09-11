"""Agent Core migration contracts on a disposable SQLite database."""

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect


ROOT = Path(__file__).resolve().parents[2]


def test_agent_core_migration_upgrades_and_downgrades(tmp_path):
    database_path = tmp_path / "migration.db"
    url = f"sqlite+pysqlite:///{database_path.as_posix()}"
    config = Config(str(ROOT / "app" / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "app" / "migrations"))
    config.set_main_option("sqlalchemy.url", url)

    command.upgrade(config, "head")
    inspector = inspect(create_engine(url))
    assert {"agent_steps", "tool_invocations"}.issubset(inspector.get_table_names())
    columns = {item["name"] for item in inspector.get_columns("agent_runs")}
    assert {"goal", "max_steps", "max_tool_calls", "deadline_at", "stop_reason"}.issubset(columns)
    step_uniques = {tuple(item["column_names"]) for item in inspector.get_unique_constraints("agent_steps")}
    assert ("agent_run_id", "sequence") in step_uniques

    command.downgrade(config, "0005_incident_source_context")
    inspector = inspect(create_engine(url))
    assert "agent_steps" not in inspector.get_table_names()
    columns = {item["name"] for item in inspector.get_columns("agent_runs")}
    assert "goal" not in columns
