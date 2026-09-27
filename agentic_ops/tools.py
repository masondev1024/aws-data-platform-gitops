"""Model-callable read-only tools. Tool arguments never become queries or commands."""

from __future__ import annotations

from typing import Any

from .contracts import IncidentBundle


TOOL_GROUPS = {
    "inspect_api_health": (
        "Read the bounded five-minute apply API sample count, 5xx ratio, and p95 latency."
    , ["api.apply_samples_5m", "api.apply_5xx_ratio", "api.apply_p95_seconds", "api.scrape_age_seconds"]),
    "inspect_data_integrity": (
        "Read database readiness and the database-derived transactional-outbox parity gap."
    , ["db.readiness", "db.readiness_age_seconds", "data.outbox_parity_gap", "data.parity_age_seconds"]),
    "inspect_rollout_health": (
        "Read Argo Rollouts phase and retained AnalysisRun phase counts, pod readiness/restart totals, and warning-event count."
    , ["rollout.phase", "rollout.analysis_runs", "platform.pod_readiness", "platform.warning_events"]),
}


TOOL_DEFINITIONS = [
    {
        "type": "function",
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
        "strict": True,
    }
    for name, (description, _) in TOOL_GROUPS.items()
]


class ToolPolicyError(ValueError):
    """A model-requested tool call is outside the read-only allowlist."""


def execute_tool(
    name: str,
    arguments: Any,
    bundle: IncidentBundle,
    *,
    previously_called: set[str],
) -> dict[str, Any]:
    if name not in TOOL_GROUPS:
        raise ToolPolicyError("tool_not_allowlisted")
    if arguments != {} or not isinstance(arguments, dict):
        raise ToolPolicyError("tool_arguments_not_allowed")
    if name in previously_called:
        raise ToolPolicyError("tool_already_called")
    _, observation_ids = TOOL_GROUPS[name]
    output = []
    for observation_id in observation_ids:
        observation = bundle.observations.get(observation_id)
        if observation is None:
            output.append({
                "evidence_ref": observation_id,
                "status": "unknown",
                "value": None,
                "reason": "observation_not_collected",
            })
        elif observation.status == "unknown":
            output.append({
                "evidence_ref": observation_id,
                "status": "unknown",
                "value": None,
                "reason": observation.reason,
                "observed_at": observation.observed_at,
            })
        else:
            output.append({
                "evidence_ref": observation_id,
                "status": "observed",
                "value": observation.value,
                "unit": observation.unit,
                "source": observation.source,
                "observed_at": observation.observed_at,
            })
    previously_called.add(name)
    return {"tool": name, "observations": output}
