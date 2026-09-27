"""Deterministic guardrails for model-suggested diagnoses and actions."""

from __future__ import annotations

import json
from typing import Any

from .contracts import ContractError, IncidentBundle


DIAGNOSES = {
    "healthy",
    "parity_gap",
    "parity_measurement_unavailable",
    "database_not_ready",
    "api_error_rate_high",
    "apply_latency_high",
    "rollout_unhealthy",
    "insufficient_evidence",
}
CONFIDENCE = {"low", "medium", "high"}
ACTION_BY_DIAGNOSIS = {
    "healthy": "no_change_recommended",
    "parity_gap": "hold_rollout_and_reconcile_writer_database_records",
    "parity_measurement_unavailable": "hold_rollout_and_restore_parity_measurement",
    "database_not_ready": "hold_rollout_and_investigate_database_readiness",
    "api_error_rate_high": "hold_rollout_and_inspect_apply_api_errors",
    "apply_latency_high": "hold_rollout_and_investigate_apply_latency",
    "rollout_unhealthy": "keep_stable_version_and_inspect_rollout_health",
    "insufficient_evidence": "collect_missing_or_fresh_observations_before_deciding",
}
HEALTH_REQUIRED = {
    "api.apply_5xx_ratio",
    "api.apply_samples_5m",
    "api.apply_p95_seconds",
    "api.scrape_age_seconds",
    "db.readiness",
    "db.readiness_age_seconds",
    "data.outbox_parity_gap",
    "data.parity_age_seconds",
    "rollout.phase",
    "platform.pod_readiness",
}


def _healthy_checks(bundle: IncidentBundle) -> tuple[bool, set[str]]:
    refs = set()
    if not HEALTH_REQUIRED.issubset(bundle.observations):
        return False, refs
    values = {}
    for observation_id in HEALTH_REQUIRED:
        item = bundle.observations[observation_id]
        if item.status != "observed":
            return False, refs
        refs.add(observation_id)
        values[observation_id] = item.value
    samples = values["api.apply_samples_5m"]
    pods = values["platform.pod_readiness"]
    return (
        isinstance(samples, (int, float))
        and samples > 0
        and isinstance(values["api.apply_5xx_ratio"], (int, float))
        and 0 <= values["api.apply_5xx_ratio"] < 0.01
        and isinstance(values["api.apply_p95_seconds"], (int, float))
        and 0 <= values["api.apply_p95_seconds"] < 0.5
        and values["api.scrape_age_seconds"] <= 120
        and values["db.readiness"] == 1
        and values["db.readiness_age_seconds"] <= 120
        and values["data.outbox_parity_gap"] == 0
        and values["data.parity_age_seconds"] <= 120
        and values["rollout.phase"] == "Healthy"
        and isinstance(pods, dict)
        and pods["total"] > 0
        and pods["ready"] == pods["total"],
        refs,
    )


def _diagnosis_supported(diagnosis: str, bundle: IncidentBundle) -> tuple[bool, set[str]]:
    if diagnosis == "healthy":
        ok, refs = _healthy_checks(bundle)
        return ok, refs
    predicates = {
        "parity_gap": lambda: bundle.observations.get("data.outbox_parity_gap") is not None
        and bundle.observations["data.outbox_parity_gap"].status == "observed"
        and isinstance(bundle.observations["data.outbox_parity_gap"].value, (int, float))
        and bundle.observations["data.outbox_parity_gap"].value > 0,
        "parity_measurement_unavailable": lambda: bundle.observations.get("data.outbox_parity_gap") is not None
        and bundle.observations["data.outbox_parity_gap"].status == "observed"
        and bundle.observations["data.outbox_parity_gap"].value == -1,
        "database_not_ready": lambda: bundle.observations.get("db.readiness") is not None
        and bundle.observations["db.readiness"].status == "observed"
        and bundle.observations["db.readiness"].value == 0,
        "api_error_rate_high": lambda: bundle.observations.get("api.apply_5xx_ratio") is not None
        and bundle.observations["api.apply_5xx_ratio"].status == "observed"
        and isinstance(bundle.observations["api.apply_5xx_ratio"].value, (int, float))
        and bundle.observations["api.apply_5xx_ratio"].value >= 0.01,
        "apply_latency_high": lambda: bundle.observations.get("api.apply_p95_seconds") is not None
        and bundle.observations["api.apply_p95_seconds"].status == "observed"
        and isinstance(bundle.observations["api.apply_p95_seconds"].value, (int, float))
        and bundle.observations["api.apply_p95_seconds"].value >= 0.5,
    }
    if diagnosis == "rollout_unhealthy":
        required = set()
        phase = bundle.observations.get("rollout.phase")
        pods = bundle.observations.get("platform.pod_readiness")
        if phase is not None and phase.status == "observed" and phase.value != "Healthy":
            required.add("rollout.phase")
        if (
            pods is not None
            and pods.status == "observed"
            and pods.value["total"] > 0
            and pods.value["ready"] < pods.value["total"]
        ):
            required.add("platform.pod_readiness")
        if required:
            return True, required
        return False, set()
    if diagnosis == "insufficient_evidence":
        return True, set()
    predicate = predicates.get(diagnosis)
    if predicate is None or not predicate():
        return False, set()
    evidence_for_diagnosis = {
        "parity_gap": "data.outbox_parity_gap",
        "parity_measurement_unavailable": "data.outbox_parity_gap",
        "database_not_ready": "db.readiness",
        "api_error_rate_high": "api.apply_5xx_ratio",
        "apply_latency_high": "api.apply_p95_seconds",
        "rollout_unhealthy": "rollout.phase",
    }
    return True, {evidence_for_diagnosis[diagnosis]}


def validate_model_result(
    raw_text: str,
    bundle: IncidentBundle,
    exposed_evidence: set[str],
    exposed_unknowns: set[str],
) -> dict[str, Any]:
    try:
        result = json.loads(raw_text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ContractError("model_result_not_json") from exc
    if not isinstance(result, dict) or set(result) != {
        "diagnosis", "confidence", "evidence_refs", "unknowns", "rationale"
    }:
        raise ContractError("model_result_shape_invalid")
    if result["diagnosis"] not in DIAGNOSES or result["confidence"] not in CONFIDENCE:
        raise ContractError("model_result_enum_invalid")
    evidence_refs = result["evidence_refs"]
    unknowns = result["unknowns"]
    rationale = result["rationale"]
    if (
        not isinstance(evidence_refs, list)
        or any(not isinstance(ref, str) for ref in evidence_refs)
        or len(evidence_refs) != len(set(evidence_refs))
        or not set(evidence_refs).issubset(exposed_evidence)
    ):
        raise ContractError("model_cited_unobserved_evidence")
    if (
        not isinstance(unknowns, list)
        or any(not isinstance(ref, str) for ref in unknowns)
        or len(unknowns) != len(set(unknowns))
        or not set(unknowns).issubset(set(bundle.observations))
        or any(bundle.observations[ref].status != "unknown" for ref in unknowns)
        or not set(unknowns).issubset(exposed_unknowns)
    ):
        raise ContractError("model_unknowns_invalid")
    if not isinstance(rationale, str) or len(rationale) > 280:
        raise ContractError("model_rationale_invalid")

    diagnosis = result["diagnosis"]
    supported, required_refs = _diagnosis_supported(diagnosis, bundle)
    if not supported or not required_refs.issubset(evidence_refs):
        raise ContractError("model_diagnosis_not_supported_by_measurements")
    if diagnosis == "healthy" and not HEALTH_REQUIRED.issubset(evidence_refs):
        raise ContractError("healthy_claim_requires_all_health_evidence")

    actual_unknowns = {
        observation_id
        for observation_id, item in bundle.observations.items()
        if item.status == "unknown"
    }
    if diagnosis == "insufficient_evidence":
        if not actual_unknowns.intersection(exposed_unknowns).issubset(unknowns):
            raise ContractError("insufficient_diagnosis_must_name_unknowns")
        if unknowns:
            pass
        else:
            # Zero traffic is an observed value, but does not support a health claim.
            zero_traffic = bundle.observations.get("api.apply_samples_5m")
            if not (
                zero_traffic
                and zero_traffic.status == "observed"
                and zero_traffic.value == 0
                and "api.apply_samples_5m" in evidence_refs
            ):
                raise ContractError("insufficient_diagnosis_requires_observed_unknown_or_zero_traffic")
    result["proposed_operator_action"] = ACTION_BY_DIAGNOSIS[diagnosis]
    return result
