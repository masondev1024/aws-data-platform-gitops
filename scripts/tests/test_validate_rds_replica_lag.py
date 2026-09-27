from datetime import datetime, timezone

import pytest

from scripts.validate_rds_replica_lag import ReplicaLagError, validate_replica_lag


NOW = datetime(2026, 9, 23, 8, 30, tzinfo=timezone.utc)


def point(minute: int, average: float) -> dict[str, object]:
    return {"Timestamp": f"2026-09-23T08:{minute:02d}:00+00:00", "Average": average}


def test_two_recent_zero_lag_samples_pass():
    result = validate_replica_lag(
        [point(29, 0), point(28, 0)], threshold_seconds=30, now=NOW
    )

    assert result["status"] == "ok"
    assert [sample["Average"] for sample in result["datapoints"]] == [0, 0]


def test_negative_lag_sentinel_fails_closed_instead_of_being_treated_as_zero():
    with pytest.raises(ReplicaLagError, match="unavailable or invalid"):
        validate_replica_lag(
            [point(29, -1), point(28, 0)], threshold_seconds=30, now=NOW
        )


def test_two_distinct_recent_samples_are_required():
    with pytest.raises(ReplicaLagError, match="two distinct"):
        validate_replica_lag([point(29, 0)], threshold_seconds=30, now=NOW)


def test_duplicate_timestamps_do_not_count_as_two_measurements():
    with pytest.raises(ReplicaLagError, match="two distinct"):
        validate_replica_lag([point(29, 0), point(29, 0)], threshold_seconds=30, now=NOW)


@pytest.mark.parametrize("threshold", [float("nan"), float("inf"), -1])
def test_invalid_threshold_cannot_disable_the_guard(threshold):
    with pytest.raises(ReplicaLagError, match="finite and non-negative"):
        validate_replica_lag([point(29, 100), point(28, 100)], threshold_seconds=threshold, now=NOW)


def test_sample_above_threshold_fails_closed():
    with pytest.raises(ReplicaLagError, match="exceeds threshold"):
        validate_replica_lag(
            [point(29, 31), point(28, 0)], threshold_seconds=30, now=NOW
        )


def test_stale_samples_do_not_satisfy_the_gate():
    with pytest.raises(ReplicaLagError, match="two distinct"):
        validate_replica_lag(
            [point(20, 0), point(19, 0)], threshold_seconds=30, now=NOW
        )
