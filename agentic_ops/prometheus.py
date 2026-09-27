"""Fixed-query, loopback-only Prometheus observation collector."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import math
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen

from .contracts import Observation


QUERY_SPECS = {
    "api.apply_5xx_ratio": (
        '(sum(rate(raffle_http_requests_total{route="/api/apply",status=~"5.."}[5m])) or vector(0)) '
        '/ clamp_min(sum(rate(raffle_http_requests_total{route="/api/apply"}[5m])), 1e-9)',
        "ratio",
    ),
    "api.apply_samples_5m": (
        'sum(increase(raffle_http_requests_total{route="/api/apply"}[5m]))',
        "requests",
    ),
    "api.apply_p95_seconds": (
        'histogram_quantile(0.95, sum by (le) '
        '(max by (pod, le, route) '
        '(rate(raffle_http_request_duration_seconds_bucket{route="/api/apply"}[5m]))))',
        "seconds",
    ),
    "api.scrape_age_seconds": (
        'max(time() - timestamp(raffle_http_requests_total{route="/api/apply"}))',
        "seconds",
    ),
    "db.readiness": ("min(raffle_db_readiness)", "boolean_gauge"),
    "db.readiness_age_seconds": (
        "max(time() - timestamp(raffle_db_readiness))",
        "seconds",
    ),
    # Preserve unknown (-1) ahead of any gap; otherwise show the largest gap.
    # Plain min hides positive gaps; plain max hides failed checks beside zero.
    "data.outbox_parity_gap": (
        "(min(raffle_apply_outbox_parity_gap) < 0) or max(raffle_apply_outbox_parity_gap) or vector(-1)",
        "rows",
    ),
    "data.parity_age_seconds": (
        "max(time() - timestamp(raffle_apply_outbox_parity_gap))",
        "seconds",
    ),
}


class PrometheusObservationError(Exception):
    """Provider data is unavailable or violates the expected instant-query contract."""


def _read_json(url: str, timeout: float) -> dict:
    # URL construction is internal and the host is always a numeric loopback address.
    with urlopen(url, timeout=timeout) as response:
        document = json.loads(response.read().decode("utf-8"))
    if not isinstance(document, dict):
        raise PrometheusObservationError("invalid_response")
    return document


def _extract_scalar(document: dict, *, now: datetime, max_age_seconds: int) -> tuple[float, datetime]:
    if document.get("status") != "success":
        raise PrometheusObservationError("invalid_response")
    data = document.get("data")
    if not isinstance(data, dict) or data.get("resultType") != "vector":
        raise PrometheusObservationError("invalid_response")
    rows = data.get("result")
    if not isinstance(rows, list) or not rows:
        raise PrometheusObservationError("no_data")
    if len(rows) != 1 or not isinstance(rows[0], dict):
        raise PrometheusObservationError("ambiguous")
    pair = rows[0].get("value")
    if not isinstance(pair, list) or len(pair) != 2:
        raise PrometheusObservationError("invalid_response")
    try:
        timestamp = datetime.fromtimestamp(float(pair[0]), timezone.utc)
        value = float(pair[1])
    except (TypeError, ValueError, OverflowError) as exc:
        raise PrometheusObservationError("invalid_response") from exc
    if not math.isfinite(value):
        raise PrometheusObservationError("invalid_response")
    age = (now - timestamp).total_seconds()
    if age < -30:
        raise PrometheusObservationError("future_data")
    if age > max_age_seconds:
        raise PrometheusObservationError("stale_data")
    return value, timestamp


def collect_prometheus(
    *,
    port: int = 9090,
    timeout: float = 3.0,
    max_age_seconds: int = 120,
    now: datetime | None = None,
    read_json=_read_json,
) -> dict[str, Observation]:
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("prometheus_port_must_be_valid")
    instant = now or datetime.now(timezone.utc)
    output = {}
    reason_map = {
        "no_data": "prometheus_no_data",
        "ambiguous": "prometheus_ambiguous",
        "stale_data": "prometheus_stale_data",
        "future_data": "prometheus_future_data",
    }
    for observation_id, (query, unit) in QUERY_SPECS.items():
        url = f"http://127.0.0.1:{port}/api/v1/query?{urlencode({'query': query})}"
        try:
            value, observed_at = _extract_scalar(
                read_json(url, timeout), now=instant, max_age_seconds=max_age_seconds
            )
            if observation_id == "db.readiness" and value not in {0.0, 1.0}:
                raise PrometheusObservationError("invalid_response")
            output[observation_id] = Observation.observed(
                observation_id,
                value,
                unit=unit,
                source="prometheus",
                observed_at=observed_at,
            )
        except (PrometheusObservationError, HTTPError, URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
            reason = reason_map.get(str(exc), "prometheus_invalid_response")
            if isinstance(exc, (HTTPError, URLError, TimeoutError, OSError)):
                reason = "prometheus_unreachable"
            output[observation_id] = Observation.unknown(
                observation_id, reason, source="prometheus", observed_at=instant
            )

    # Prometheus instant-query timestamps describe query evaluation time, not necessarily
    # the source sample. Explicit timestamp() queries prevent stale source samples from
    # being presented to the model as current health.
    freshness_dependencies = {
        "api.scrape_age_seconds": (
            "api.apply_5xx_ratio", "api.apply_samples_5m", "api.apply_p95_seconds"
        ),
        "db.readiness_age_seconds": ("db.readiness",),
        "data.parity_age_seconds": ("data.outbox_parity_gap",),
    }
    for freshness_id, dependencies in freshness_dependencies.items():
        freshness = output[freshness_id]
        source_is_stale = (
            freshness.status != "observed"
            or not isinstance(freshness.value, (int, float))
            or freshness.value < 0
            or freshness.value > max_age_seconds
        )
        if source_is_stale:
            for observation_id in dependencies:
                output[observation_id] = Observation.unknown(
                    observation_id,
                    "prometheus_source_stale",
                    source="prometheus",
                    observed_at=instant,
                )
    return output
