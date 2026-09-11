"""Offline Agent evaluation dataset and scoring tests."""

import json
from pathlib import Path

import pytest

from ops_agent.evaluation import evaluate_suite, score_scenario, summarize_evaluation


SCENARIOS = json.loads(
    (Path(__file__).resolve().parents[1] / "evals" / "scenarios.json").read_text(encoding="utf-8")
)
OBSERVATIONS = json.loads(
    (Path(__file__).resolve().parents[1] / "evals" / "observations.rules.json").read_text(
        encoding="utf-8"
    )
)


def test_evaluation_set_covers_twelve_required_scenarios_and_no_write_tool():
    assert len(SCENARIOS) == 12
    assert {item["id"] for item in SCENARIOS} == {
        "crashloop", "oom-killed", "image-pull", "pod-pending", "readiness",
        "redis-down", "deployment-replicas", "resource-pressure", "http-5xx",
        "insufficient-evidence", "prompt-injection", "tool-timeout",
    }
    assert all("delete_pod" in item["forbidden_tools"] for item in SCENARIOS)


def test_evaluation_reports_success_rate_steps_and_error_classes():
    scenario = SCENARIOS[0]
    passed = score_scenario(
        scenario,
        {
            "tools": ["get_pod_status", "get_previous_logs", "get_kubernetes_events"],
            "evidence_types": ["POD_STATUS", "PREVIOUS_LOGS", "K8S_EVENTS"],
            "steps": 4,
            "terminal": "COMPLETED",
            "write_calls": 0,
        },
    )
    failed = score_scenario(
        scenario,
        {"tools": ["delete_pod"], "evidence_types": [], "steps": 9, "terminal": "FAILED", "write_calls": 1},
    )
    report = summarize_evaluation([passed, failed])
    assert report["success_rate"] == 0.5
    assert report["average_steps"] == 6.5
    assert report["error_classification"]["WRITE_CALL"] == 1
    assert report["error_classification"]["MISSING_EVIDENCE"] == 1


def test_all_recorded_rule_scenarios_pass_offline_deterministically():
    report = evaluate_suite(SCENARIOS, OBSERVATIONS)

    assert report["summary"]["scenario_count"] == 12
    assert report["summary"]["passed"] == 12
    assert report["summary"]["success_rate"] == 1.0
    assert report["summary"]["average_steps"] == pytest.approx(34 / 12)
    assert report["summary"]["error_classification"] == {}
