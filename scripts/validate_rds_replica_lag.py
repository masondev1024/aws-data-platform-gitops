#!/usr/bin/env python3
"""Fail closed unless two recent RDS replica-lag samples are measurable and healthy."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
from typing import Any


class ReplicaLagError(ValueError):
    """The CloudWatch samples do not prove that the read replica is caught up."""


def validate_replica_lag(
    datapoints: list[dict[str, Any]],
    *,
    threshold_seconds: float,
    now: datetime | None = None,
    max_age_seconds: int = 240,
) -> dict[str, Any]:
    if not math.isfinite(threshold_seconds) or threshold_seconds < 0:
        raise ReplicaLagError("lag threshold must be finite and non-negative")
    now = now or datetime.now(timezone.utc)
    recent: list[dict[str, Any]] = []
    seen_timestamps: set[datetime] = set()
    for point in sorted(datapoints, key=lambda item: item.get("Timestamp", ""), reverse=True):
        raw_timestamp = point.get("Timestamp")
        raw_average = point.get("Average")
        if raw_timestamp is None or raw_average is None:
            continue
        timestamp = datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00"))
        age_seconds = (now - timestamp).total_seconds()
        if age_seconds < 0 or age_seconds > max_age_seconds:
            continue
        average = float(raw_average)
        if not math.isfinite(average) or average < 0:
            raise ReplicaLagError(
                f"ReplicaLag is unavailable or invalid at {raw_timestamp}: {average}"
            )
        if timestamp in seen_timestamps:
            continue
        seen_timestamps.add(timestamp)
        recent.append(
            {
                "Timestamp": raw_timestamp,
                "Average": average,
                "AgeSeconds": age_seconds,
            }
        )
        if len(recent) == 2:
            break
    if len(recent) < 2:
        raise ReplicaLagError("need two distinct, recent, measurable ReplicaLag datapoints")
    over_threshold = [point for point in recent if point["Average"] > threshold_seconds]
    if over_threshold:
        raise ReplicaLagError(
            f"ReplicaLag exceeds threshold {threshold_seconds}: {over_threshold}"
        )
    return {
        "status": "ok",
        "threshold_seconds": threshold_seconds,
        "datapoints": recent,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--threshold-seconds", required=True, type=float)
    args = parser.parse_args()
    try:
        data = json.loads(args.input.read_text(encoding="utf-8"))
        result = validate_replica_lag(
            data.get("Datapoints", []), threshold_seconds=args.threshold_seconds
        )
    except (OSError, json.JSONDecodeError, ReplicaLagError, TypeError, ValueError) as error:
        parser.exit(2, f"replica lag validation failed: {error}\n")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
