"""Offline scoring for deterministic Agent investigation scenarios."""

from __future__ import annotations

from statistics import mean
from typing import Any


def score_scenario(scenario: dict[str, Any], observed: dict[str, Any]) -> dict[str, Any]:
    errors = []
    tools = observed.get("tools", [])
    evidence_types = set(observed.get("evidence_types", []))
    if not set(tools).issubset(set(scenario["allowed_tools"])):
        errors.append("DISALLOWED_TOOL")
    if set(tools) & set(scenario["forbidden_tools"]):
        errors.append("FORBIDDEN_TOOL")
    if not set(scenario["required_evidence_types"]).issubset(evidence_types):
        errors.append("MISSING_EVIDENCE")
    if observed.get("steps", 0) > scenario["max_steps"]:
        errors.append("STEP_BUDGET_EXCEEDED")
    if observed.get("terminal") not in scenario["expected_terminals"]:
        errors.append("UNEXPECTED_TERMINAL")
    if observed.get("write_calls", 0):
        errors.append("WRITE_CALL")
    return {"id": scenario["id"], "passed": not errors, "errors": errors, "steps": observed.get("steps", 0)}


def summarize_evaluation(results: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(results)
    failures: dict[str, int] = {}
    for item in results:
        for error in item["errors"]:
            failures[error] = failures.get(error, 0) + 1
    return {
        "scenario_count": total,
        "passed": sum(1 for item in results if item["passed"]),
        "success_rate": (sum(1 for item in results if item["passed"]) / total) if total else 0,
        "average_steps": mean([item["steps"] for item in results]) if results else 0,
        "error_classification": failures,
    }


def evaluate_suite(
    scenarios: list[dict[str, Any]], observations: list[dict[str, Any]]
) -> dict[str, Any]:
    """Score one deterministic observation for every named scenario."""
    observed_by_id = {item["id"]: item for item in observations}
    scenario_ids = {item["id"] for item in scenarios}
    missing = scenario_ids - observed_by_id.keys()
    extra = observed_by_id.keys() - scenario_ids
    if missing or extra or len(observed_by_id) != len(observations):
        raise ValueError(
            f"evaluation observations do not match scenarios: missing={sorted(missing)}, "
            f"extra={sorted(extra)}"
        )
    results = [score_scenario(item, observed_by_id[item["id"]]) for item in scenarios]
    return {"results": results, "summary": summarize_evaluation(results)}
