"""Budget planning is conservative validation, not an AWS billing hard cap."""
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("cost", ROOT / "platform/live-lab/scripts/estimate_session_cost.py")
cost = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cost)


def test_approved_short_session_fits_with_fx_tax_and_reserve():
    result = cost.estimate(3, 40_000, 5.5, 1)
    assert result["planning_total_usd"] == 5.0594
    assert result["planning_total_usd"] * 1600 * 1.1 < 10_000
    assert result["billed_rds_hours"] == 3
    assert result["uncertainty_reserve_usd"] == 1


@pytest.mark.parametrize("arguments", [(6, 40_000, 5.5, 1), (3, 40_000, 10, 1),
                                        (3, 40_000, 5.5, 0), (float("nan"), 1, 5.5, 1),
                                        (3, 1, float("inf"), 1), (3, True, 5.5, 1)])
def test_unapproved_or_nonfinite_budget_is_rejected(arguments):
    with pytest.raises(ValueError):
        cost.estimate(*arguments)


def test_tiny_run_does_not_escape_rds_minimum_billable_time():
    result = cost.estimate(0.01, 0, 5.5, 1)
    assert result["billed_rds_hours"] == pytest.approx(1 / 6)
    assert result["billed_alb_hours"] == 1


def test_prepare_records_current_plan_and_checks_cost_before_aws():
    source = (ROOT / "platform/live-lab/scripts/prepare_session.sh").read_text()
    assert source.index("estimate_session_cost.py") < source.index("sts get-caller-identity")
    assert '"max_session_hours": float(os.environ["LIVE_LAB_HOURS"])' in source
    assert '"cost_budget_usd": float(os.environ["LIVE_LAB_BUDGET"])' in source
    watchdog = (ROOT / "platform/live-lab/scripts/deadline_watchdog.sh").read_text()
    assert "remaining <= 7200" in watchdog
    assert "SESSION_DEADLINE_EPOCH + 3600" in watchdog
