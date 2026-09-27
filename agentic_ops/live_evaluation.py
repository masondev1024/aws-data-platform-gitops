"""Fixed synthetic replay cases and model evaluation, never a scripted model.

Values and expected outcomes are fixed. Replay timestamps are instantiated at run
time to exercise the model independently of the wall-clock bundle expiry guard.
The stale case represents source-stale telemetry normalized by the collector.
"""

from dataclasses import dataclass
from datetime import datetime
import time
from uuid import NAMESPACE_URL, uuid5

from .agent import AgentExecutionError, IncidentTriageAgent
from .contracts import Observation, make_bundle, utc_now
from .policy import HEALTH_REQUIRED
from .tools import TOOL_GROUPS


@dataclass(frozen=True)
class EvaluationCase:
    name: str
    diagnosis: str
    evidence: frozenset[str] = frozenset()
    unknowns: frozenset[str] = frozenset()


CASES = (
    EvaluationCase("healthy", "healthy", frozenset(HEALTH_REQUIRED)),
    EvaluationCase("5xx", "api_error_rate_high", frozenset({"api.apply_5xx_ratio"})),
    EvaluationCase("db_down", "database_not_ready", frozenset({"db.readiness"})),
    EvaluationCase("missing", "insufficient_evidence", unknowns=frozenset({"api.apply_p95_seconds"})),
    EvaluationCase("stale", "insufficient_evidence", unknowns=frozenset({
        "api.apply_5xx_ratio", "api.apply_samples_5m", "api.apply_p95_seconds",
    })),
    EvaluationCase("parity", "parity_gap", frozenset({"data.outbox_parity_gap"})),
)


def replay_bundle(case: EvaluationCase, *, now: datetime | None = None):
    now = now or utc_now()
    values = {
        "api.apply_5xx_ratio": (0.0, "ratio"),
        "api.apply_samples_5m": (12, "requests"),
        "api.apply_p95_seconds": (0.2, "seconds"),
        "api.scrape_age_seconds": (15, "seconds"),
        "db.readiness": (1, "boolean_gauge"),
        "db.readiness_age_seconds": (15, "seconds"),
        "data.outbox_parity_gap": (0, "rows"),
        "data.parity_age_seconds": (15, "seconds"),
        "rollout.phase": ("Healthy", None),
        "rollout.analysis_runs": ({"total": 0, "pending": 0, "running": 0, "successful": 0,
                                   "failed": 0, "errored": 0, "inconclusive": 0}, "runs"),
        "platform.pod_readiness": ({"total": 2, "ready": 2, "restarts": 0}, None),
        "platform.warning_events": (0, "events"),
    }
    overrides = {
        "5xx": {"api.apply_5xx_ratio": (0.2, "ratio")},
        "db_down": {"db.readiness": (0, "boolean_gauge")},
        "parity": {"data.outbox_parity_gap": (3, "rows")},
        "stale": {"api.scrape_age_seconds": (300, "seconds")},
    }
    values.update(overrides.get(case.name, {}))
    observations = {}
    for key, (value, unit) in values.items():
        source = "kubernetes" if key.startswith(("rollout.", "platform.")) else "prometheus"
        if key in case.unknowns:
            observations[key] = Observation.unknown(
                key, "prometheus_source_stale" if case.name == "stale" else "prometheus_no_data",
                source=source, observed_at=now,
            )
        else:
            observations[key] = Observation.observed(key, value, unit=unit, source=source, observed_at=now)
    return make_bundle(observations, mode="replay", collected_at=now,
                       bundle_id=str(uuid5(NAMESPACE_URL, "replay-model-eval:" + case.name)))


class MeteredBackend:
    """Retain usage even when the agent rejects a response; no provider payload logs."""

    def __init__(self, backend):
        self.backend = backend
        self.model = backend.model
        self.calls = 0
        self.responses = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.usage_available = True
        self.selected_tools = []

    def respond(self, input_items):
        self.calls += 1
        turn = self.backend.respond(input_items)
        self.responses += 1
        self.input_tokens += max(0, turn.input_tokens)
        self.output_tokens += max(0, turn.output_tokens)
        self.usage_available = self.usage_available and turn.usage_available
        self.selected_tools.extend(call.name if call.name in TOOL_GROUPS else "not_allowlisted"
                                   for call in turn.function_calls)
        return turn


def evaluate_case(backend, bundle, *, case: EvaluationCase | None = None):
    meter = MeteredBackend(backend)
    started = time.monotonic()
    result = None
    reason = None
    try:
        result = IncidentTriageAgent(meter).run(bundle, require_live=case is None)
        passed = case is None or (
            result["diagnosis"] == case.diagnosis
            and case.evidence.issubset(result["evidence_refs"])
            and case.unknowns.issubset(result["unknowns"])
        )
        if not passed:
            reason = "expected_outcome_mismatch"
    except AgentExecutionError as exc:
        passed, reason = False, str(exc)
    except Exception:
        passed, reason = False, "evaluation_failed"
    return {
        "case": case.name if case else "fresh_observation",
        "mode": "replay-model-eval" if case else "fresh-real-observation",
        "pass": passed,
        "assessment": "expected_diagnosis_and_evidence" if case else "evidence_policy_only_no_ground_truth",
        "reason": reason,
        "diagnosis": result["diagnosis"] if result else None,
        "expected_diagnosis": case.diagnosis if case else None,
        "evidence_refs": result["evidence_refs"] if result else [],
        "unknowns": result["unknowns"] if result else [],
        "selected_tools": meter.selected_tools,
        "model_calls": meter.calls,
        "input_tokens": meter.input_tokens,
        "output_tokens": meter.output_tokens,
        "usage_status": ("partial_provider_failure" if meter.calls != meter.responses
                         else "reported" if meter.usage_available else "partial_usage_missing"),
        "elapsed_ms": round((time.monotonic() - started) * 1000),
        "bundle_id": bundle.bundle_id,
        "estimated_cost_usd": None,
        "cost_status": "token_rates_not_configured",
    }
