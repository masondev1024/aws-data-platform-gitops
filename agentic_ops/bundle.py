"""Convert bounded platform and Prometheus observations into an agent-safe bundle."""

from __future__ import annotations

from datetime import datetime, timezone
import uuid
from typing import Any

from .contracts import ANALYSIS_RUN_PHASES, IncidentBundle, Observation, make_bundle
from .prometheus import QUERY_SPECS


def _unknown_kubernetes(observation_id: str, reason: str, now: datetime) -> Observation:
    return Observation.unknown(
        observation_id, reason, source="kubernetes", observed_at=now
    )


def build_incident_bundle(
    platform_report: dict[str, Any],
    prometheus_observations: dict[str, Observation],
    *,
    now: datetime | None = None,
) -> IncidentBundle:
    instant = now or datetime.now(timezone.utc)
    observations = dict(prometheus_observations)
    for observation_id in QUERY_SPECS:
        if observation_id not in observations:
            observations[observation_id] = Observation.unknown(
                observation_id,
                "prometheus_invalid_response",
                source="prometheus",
                observed_at=instant,
            )
    raw = platform_report.get("observations", {}) if isinstance(platform_report, dict) else {}
    if not isinstance(raw, dict):
        raw = {}

    rollouts = raw.get("rollouts", {})
    rollout_items = rollouts.get("items") if isinstance(rollouts, dict) else None
    if not isinstance(rollout_items, list):
        observations["rollout.phase"] = _unknown_kubernetes(
            "rollout.phase", "kubernetes_unavailable", instant
        )
    else:
        matches = [item for item in rollout_items if isinstance(item, dict) and item.get("name") == "data-pipeline-rollout"]
        if len(matches) != 1:
            reason = "kubernetes_missing_rollout" if not matches else "kubernetes_ambiguous_rollout"
            observations["rollout.phase"] = _unknown_kubernetes("rollout.phase", reason, instant)
        else:
            phase = matches[0].get("phase")
            if phase not in {"Healthy", "Degraded", "Progressing", "Paused"}:
                observations["rollout.phase"] = _unknown_kubernetes(
                    "rollout.phase", "kubernetes_unrecognized_phase", instant
                )
            else:
                observations["rollout.phase"] = Observation.observed(
                    "rollout.phase", phase, unit=None, source="kubernetes", observed_at=instant
                )

    analysis_runs = raw.get("analysisruns", {})
    analysis_items = analysis_runs.get("items") if isinstance(analysis_runs, dict) else None
    if not isinstance(analysis_items, list):
        observations["rollout.analysis_runs"] = _unknown_kubernetes(
            "rollout.analysis_runs", "kubernetes_unavailable", instant
        )
    else:
        matching_runs = [
            run for run in analysis_items
            if isinstance(run, dict)
            and isinstance(run.get("name"), str)
            and run["name"].startswith("data-pipeline-rollout-")
        ]
        phases = [run.get("phase") for run in matching_runs]
        if any(phase not in ANALYSIS_RUN_PHASES for phase in phases):
            observations["rollout.analysis_runs"] = _unknown_kubernetes(
                "rollout.analysis_runs", "kubernetes_invalid_observation", instant
            )
        else:
            phase_counts = {
                {"Error": "errored"}.get(phase, phase.lower()): phases.count(phase)
                for phase in ANALYSIS_RUN_PHASES
            }
            observations["rollout.analysis_runs"] = Observation.observed(
                "rollout.analysis_runs",
                {"total": len(phases), **phase_counts},
                unit="runs",
                source="kubernetes",
                observed_at=instant,
            )

    pods = raw.get("pods", {})
    pod_items = pods.get("items") if isinstance(pods, dict) else None
    app_pods = [pod for pod in pod_items if isinstance(pod, dict) and pod.get("application") is True] if isinstance(pod_items, list) else []
    if not app_pods:
        observations["platform.pod_readiness"] = _unknown_kubernetes(
            "platform.pod_readiness", "kubernetes_missing_pods", instant
        )
    else:
        valid = all(
            isinstance(pod, dict)
            and pod.get("application") is True
            and type(pod.get("ready")) is bool
            and type(pod.get("restarts")) is int
            and pod.get("restarts") >= 0
            for pod in app_pods
        )
        if not valid:
            observations["platform.pod_readiness"] = _unknown_kubernetes(
                "platform.pod_readiness", "kubernetes_invalid_observation", instant
            )
        else:
            observations["platform.pod_readiness"] = Observation.observed(
                "platform.pod_readiness",
                {
                    "total": len(app_pods),
                    "ready": sum(1 for pod in app_pods if pod["ready"]),
                    "restarts": sum(pod["restarts"] for pod in app_pods),
                },
                unit=None,
                source="kubernetes",
                observed_at=instant,
            )

    events = raw.get("events", {})
    event_items = events.get("items") if isinstance(events, dict) else None
    if not isinstance(event_items, list):
        observations["platform.warning_events"] = _unknown_kubernetes(
            "platform.warning_events", "kubernetes_unavailable", instant
        )
    elif any(
        not isinstance(event, dict)
        or type(event.get("count")) is not int
        or event["count"] < 0
        or event.get("type") not in {"Warning", "Normal"}
        for event in event_items
    ):
        observations["platform.warning_events"] = _unknown_kubernetes(
            "platform.warning_events", "kubernetes_invalid_observation", instant
        )
    else:
        observations["platform.warning_events"] = Observation.observed(
            "platform.warning_events",
            sum(event["count"] for event in event_items if event["type"] == "Warning"),
            unit="events",
            source="kubernetes",
            observed_at=instant,
        )

    return make_bundle(
        observations,
        mode="live_observation",
        bundle_id=str(uuid.uuid4()),
        collected_at=instant,
    )
