"""Small, strict contracts shared by the collector, tools, and agent runner."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math
import re
from typing import Any


OBSERVATION_IDS = {
    "api.apply_5xx_ratio",
    "api.apply_samples_5m",
    "api.apply_p95_seconds",
    "api.scrape_age_seconds",
    "db.readiness",
    "db.readiness_age_seconds",
    "data.outbox_parity_gap",
    "data.parity_age_seconds",
    "rollout.phase",
    "rollout.analysis_runs",
    "platform.pod_readiness",
    "platform.warning_events",
}
UNKNOWN_REASONS = {
    "prometheus_unreachable",
    "prometheus_invalid_response",
    "prometheus_no_data",
    "prometheus_ambiguous",
    "prometheus_stale_data",
    "prometheus_future_data",
    "prometheus_source_stale",
    "kubernetes_unavailable",
    "kubernetes_missing_rollout",
    "kubernetes_ambiguous_rollout",
    "kubernetes_unrecognized_phase",
    "kubernetes_missing_pods",
    "kubernetes_invalid_observation",
}
ROLLOUT_PHASES = {"Healthy", "Degraded", "Progressing", "Paused"}
ANALYSIS_RUN_PHASES = {"Pending", "Running", "Successful", "Failed", "Error", "Inconclusive"}


class ContractError(ValueError):
    """Raised when telemetry or model output violates a local contract."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ContractError("invalid_timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError("invalid_timestamp") from exc
    if parsed.tzinfo is None:
        raise ContractError("timezone_required")
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class Observation:
    observation_id: str
    status: str
    value: int | float | str | dict[str, int] | None
    unit: str | None
    source: str
    observed_at: str
    reason: str | None = None

    @classmethod
    def observed(
        cls,
        observation_id: str,
        value: int | float | str | dict[str, int],
        *,
        unit: str | None,
        source: str,
        observed_at: datetime | None = None,
    ) -> "Observation":
        instant = observed_at or utc_now()
        result = cls(
            observation_id=observation_id,
            status="observed",
            value=value,
            unit=unit,
            source=source,
            observed_at=instant.astimezone(timezone.utc).isoformat(),
        )
        result.validate()
        return result

    @classmethod
    def unknown(
        cls,
        observation_id: str,
        reason: str,
        *,
        source: str,
        observed_at: datetime | None = None,
    ) -> "Observation":
        result = cls(
            observation_id=observation_id,
            status="unknown",
            value=None,
            unit=None,
            source=source,
            observed_at=(observed_at or utc_now()).astimezone(timezone.utc).isoformat(),
            reason=reason,
        )
        result.validate()
        return result

    @classmethod
    def from_dict(cls, value: Any, expected_id: str) -> "Observation":
        if not isinstance(value, dict) or set(value) != {
            "observation_id", "status", "value", "unit", "source", "observed_at", "reason"
        }:
            raise ContractError("invalid_observation_shape")
        result = cls(**value)
        if result.observation_id != expected_id:
            raise ContractError("observation_id_mismatch")
        result.validate()
        return result

    def validate(self) -> None:
        if self.observation_id not in OBSERVATION_IDS:
            raise ContractError("unknown_observation_id")
        if self.status not in {"observed", "unknown"}:
            raise ContractError("invalid_observation_status")
        if self.source not in {"prometheus", "kubernetes"}:
            raise ContractError("invalid_observation_source")
        parse_timestamp(self.observed_at)
        if self.status == "unknown":
            if self.value is not None or self.reason not in UNKNOWN_REASONS:
                raise ContractError("invalid_unknown_observation")
            return
        if self.reason is not None or self.value is None:
            raise ContractError("invalid_observed_observation")
        if isinstance(self.value, bool):
            raise ContractError("boolean_not_numeric_observation")
        if isinstance(self.value, (int, float)) and not math.isfinite(self.value):
            raise ContractError("non_finite_observation")
        if isinstance(self.value, dict):
            if self.observation_id == "platform.pod_readiness":
                if set(self.value) != {"total", "ready", "restarts"}:
                    raise ContractError("invalid_pod_observation")
                if any(type(item) is not int or item < 0 for item in self.value.values()):
                    raise ContractError("invalid_pod_counts")
                if self.value["ready"] > self.value["total"]:
                    raise ContractError("ready_exceeds_total")
            elif self.observation_id == "rollout.analysis_runs":
                expected = {"total", "pending", "running", "successful", "failed", "errored", "inconclusive"}
                if set(self.value) != expected or any(type(item) is not int or item < 0 for item in self.value.values()):
                    raise ContractError("invalid_analysis_run_counts")
                if sum(self.value[key] for key in expected - {"total"}) != self.value["total"]:
                    raise ContractError("analysis_run_total_mismatch")
            else:
                raise ContractError("unexpected_object_observation")
        elif not isinstance(self.value, (int, float, str)):
            raise ContractError("invalid_observation_value")
        if self.observation_id == "rollout.phase":
            if self.value not in ROLLOUT_PHASES:
                raise ContractError("invalid_rollout_phase")
        elif self.observation_id == "platform.pod_readiness":
            if not isinstance(self.value, dict):
                raise ContractError("pod_counts_required")
        elif self.observation_id == "rollout.analysis_runs":
            if not isinstance(self.value, dict):
                raise ContractError("analysis_run_counts_required")
        elif not isinstance(self.value, (int, float)):
            raise ContractError("numeric_observation_required")
        if self.observation_id in {"db.readiness", "db.readiness_age_seconds", "data.parity_age_seconds", "api.scrape_age_seconds"}:
            if self.observation_id == "db.readiness" and self.value not in {0, 1, 0.0, 1.0}:
                raise ContractError("invalid_readiness_value")
            if self.observation_id.endswith("age_seconds") and self.value < 0:
                raise ContractError("invalid_sample_age")
        if self.observation_id == "api.apply_5xx_ratio" and not 0 <= self.value <= 1:
            raise ContractError("invalid_error_ratio")
        if self.observation_id in {"api.apply_samples_5m", "api.apply_p95_seconds", "platform.warning_events"} and self.value < 0:
            raise ContractError("negative_measurement")
        if self.observation_id == "data.outbox_parity_gap" and (
            self.value < -1 or not float(self.value).is_integer()
        ):
            raise ContractError("invalid_parity_gap")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "observation_id": self.observation_id,
            "status": self.status,
            "value": self.value,
            "unit": self.unit,
            "source": self.source,
            "observed_at": self.observed_at,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class IncidentBundle:
    bundle_id: str
    mode: str
    collected_at: str
    observations: dict[str, Observation]

    @classmethod
    def from_dict(cls, value: Any) -> "IncidentBundle":
        if not isinstance(value, dict) or set(value) != {
            "schema_version", "bundle_id", "mode", "collected_at", "observations"
        }:
            raise ContractError("invalid_bundle_shape")
        if value["schema_version"] != 1:
            raise ContractError("unsupported_bundle_version")
        if value["mode"] not in {"live_observation", "replay"}:
            raise ContractError("invalid_bundle_mode")
        bundle_id = value["bundle_id"]
        if not isinstance(bundle_id, str) or not re.fullmatch(r"[a-f0-9-]{36}", bundle_id):
            raise ContractError("invalid_bundle_id")
        parse_timestamp(value["collected_at"])
        raw_observations = value["observations"]
        if not isinstance(raw_observations, dict) or not set(raw_observations).issubset(OBSERVATION_IDS):
            raise ContractError("invalid_bundle_observations")
        if set(raw_observations) != OBSERVATION_IDS:
            raise ContractError("incomplete_bundle_observations")
        observations = {
            observation_id: Observation.from_dict(observation, observation_id)
            for observation_id, observation in raw_observations.items()
        }
        return cls(bundle_id, value["mode"], value["collected_at"], observations)

    def validate_fresh(self, *, now: datetime | None = None, max_age_seconds: int = 180) -> None:
        instant = now or utc_now()
        for observation in self.observations.values():
            if observation.status != "observed":
                continue
            age = (instant - parse_timestamp(observation.observed_at)).total_seconds()
            if age < -30 or age > max_age_seconds:
                raise ContractError("bundle_observation_stale")

    def to_dict(self) -> dict[str, Any]:
        value = {
            "schema_version": 1,
            "bundle_id": self.bundle_id,
            "mode": self.mode,
            "collected_at": self.collected_at,
            "observations": {
                key: observation.to_dict() for key, observation in self.observations.items()
            },
        }
        return value


def make_bundle(
    observations: dict[str, Observation], *, mode: str, bundle_id: str, collected_at: datetime | None = None
) -> IncidentBundle:
    value = {
        "schema_version": 1,
        "bundle_id": bundle_id,
        "mode": mode,
        "collected_at": (collected_at or utc_now()).astimezone(timezone.utc).isoformat(),
        "observations": {key: item.to_dict() for key, item in observations.items()},
    }
    return IncidentBundle.from_dict(value)
