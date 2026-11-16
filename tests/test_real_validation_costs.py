"""비용 정책 버전과 실제 순손익 — 미끄러짐 중복 차감 방지 (설계 §4.1)."""

from __future__ import annotations

import inspect

import pytest

from src import db
from src.modules import cost_model
from src.real_validation import costs
from src.real_validation.core import canonical_hash
from src.real_validation.costs import CostBasis, CostPolicy, CostPolicyVersion, Fill


def _split(effective_from="20270101", **overrides):
    values = {
        "effective_from": effective_from,
        "market": "KRX",
        "account_condition": "SYNTHETIC",
        "basis": CostBasis.ACCOUNT_SCHEDULE,
        "buy_fee_rate": 0.0002,
        "sell_fee_rate": 0.0002,
        "sell_tax_rate": 0.0015,
    }
    values.update(overrides)
    return CostPolicyVersion(**values)


def test_canonical_hash_follows_the_existing_config_hash_convention():
    config = {"b": 1, "a": {"가": 0.18, "z": [1, 2]}}

    assert canonical_hash(config) == db._canonical_config_hash(config)


def test_realized_pnl_has_no_way_to_deduct_slippage():
    parameters = inspect.signature(costs.realized_pnl).parameters

    assert not [name for name in parameters if "slip" in name]


def test_realized_net_is_gross_minus_fees_and_taxes_only():
    # 결정가 10,000원에 10,050원으로 체결됐다. 이 0.5%는 이미 체결가에 들어 있다.
    result = costs.realized_pnl(
        [Fill(100, 10_050.0)], [Fill(100, 10_000.0)], _split(), decision_price=10_000.0
    )

    assert result["gross_amount"] == -5_000.0
    assert result["fees"] == pytest.approx(1_005_000 * 0.0002 + 1_000_000 * 0.0002)
    assert result["taxes"] == pytest.approx(1_000_000 * 0.0015)
    assert result["net_amount"] == pytest.approx(-5_000.0 - 401.0 - 1_500.0)
    assert result["slippage_deducted_pct"] == 0.0
    assert result["entry_slippage_vs_decision_pct"] == pytest.approx(0.5)
    assert result["pnl_basis"] == "ACTUAL_FILL_NET"


def test_decision_price_is_informational_and_never_changes_the_net():
    fills = ([Fill(100, 10_050.0)], [Fill(100, 10_000.0)], _split())

    with_decision = costs.realized_pnl(*fills, decision_price=10_000.0)
    without = costs.realized_pnl(*fills)

    assert with_decision["net_amount"] == without["net_amount"]
    assert without["entry_slippage_vs_decision_pct"] is None


def test_existing_conservative_figure_is_the_actual_net_minus_slippage_again():
    """기존 conservative 손익은 실제 체결 손익에서 사유별 슬리피지를 한 번 더 뺀 값이다."""
    research = costs.research_scenario_policy().versions[0]
    actual = costs.realized_pnl([Fill(10, 10_000.0)], [Fill(10, 9_800.0)], research)
    legacy = cost_model.pnl_breakdown(10_000.0, 9_800.0, "HARD_STOP")

    assert actual["net_pct"] == pytest.approx(legacy["cost_net_pct"])
    assert legacy["conservative_net_pct"] == pytest.approx(
        actual["net_pct"] - cost_model.HARD_STOP_SLIPPAGE_PCT
    )


def test_stress_pnl_is_labelled_as_an_additional_adverse_adjustment():
    actual = costs.realized_pnl([Fill(100, 10_000.0)], [Fill(100, 10_100.0)], _split())

    stressed = costs.stress_pnl(actual, extra_adverse_pct=0.30)

    assert stressed["is_additional_adverse_adjustment"] is True
    assert stressed["additional_adverse_adjustment_pct"] == 0.30
    assert stressed["stress_net_pct"] == pytest.approx(actual["net_pct"] - 0.30)
    assert stressed["stress_net_amount"] == pytest.approx(actual["net_amount"] - 3_000.0)
    assert stressed["pnl_basis"] == "STRESS_ON_ACTUAL_FILL"


def test_stress_adjustment_cannot_be_stacked_or_negative():
    actual = costs.realized_pnl([Fill(100, 10_000.0)], [Fill(100, 10_100.0)], _split())
    stressed = costs.stress_pnl(actual, extra_adverse_pct=0.30)

    with pytest.raises(ValueError):
        costs.stress_pnl(stressed, extra_adverse_pct=0.30)
    with pytest.raises(ValueError):
        costs.stress_pnl(actual, extra_adverse_pct=-0.1)


def test_partial_fills_use_the_weighted_average_price():
    result = costs.realized_pnl(
        [Fill(60, 10_000.0), Fill(40, 10_100.0)],
        [Fill(30, 10_300.0), Fill(70, 10_200.0)],
        _split(),
    )

    assert result["qty"] == 100
    assert result["avg_buy_price"] == pytest.approx(10_040.0)
    assert result["avg_sell_price"] == pytest.approx(10_230.0)
    assert result["gross_amount"] == pytest.approx(19_000.0)


def test_open_position_is_not_a_realized_result():
    with pytest.raises(ValueError):
        costs.realized_pnl([Fill(100, 10_000.0)], [Fill(60, 10_100.0)], _split())
    with pytest.raises(ValueError):
        costs.realized_pnl([Fill(100, 10_000.0)], [], _split())


def test_settled_values_are_distinguished_from_estimates():
    fills = ([Fill(100, 10_000.0)], [Fill(100, 10_100.0)], _split())

    estimated = costs.realized_pnl(*fills)
    settled = costs.realized_pnl(*fills, settled_fees=350.0, settled_taxes=1_818.0)

    assert estimated["cost_basis"] == "ACCOUNT_SCHEDULE" and estimated["is_estimate"] is True
    assert settled["cost_basis"] == "SETTLED" and settled["is_estimate"] is False
    assert settled["net_amount"] == pytest.approx(10_000.0 - 350.0 - 1_818.0)
    with pytest.raises(ValueError):
        costs.realized_pnl(*fills, settled_fees=350.0)


def test_research_constants_stay_a_scenario_assumption():
    policy = costs.research_scenario_policy()
    version = policy.versions[0]

    assert policy.problems() == []
    assert version.basis is CostBasis.SCENARIO_ASSUMPTION
    assert version.round_trip_rate == pytest.approx(cost_model.BASE_ROUND_TRIP_COST_PCT / 100)
    result = costs.realized_pnl([Fill(10, 10_000.0)], [Fill(10, 10_000.0)], version)
    assert result["cost_basis"] == "SCENARIO_ASSUMPTION"
    assert result["net_pct"] == pytest.approx(-cost_model.BASE_ROUND_TRIP_COST_PCT)


def test_policy_version_is_chosen_by_effective_date_without_backdating():
    old = _split("20270101")
    new = _split("20270301", sell_tax_rate=0.0010)
    policy = CostPolicy("synthetic", (new, old))

    assert policy.version_for("20270228", "KRX", "SYNTHETIC") is old
    assert policy.version_for("20270301", "KRX", "SYNTHETIC") is new
    with pytest.raises(LookupError):
        policy.version_for("20261231", "KRX", "SYNTHETIC")
    with pytest.raises(LookupError):
        policy.version_for("20270301", "KRX", "OTHER_ACCOUNT")


def test_policy_hash_changes_with_any_rate():
    base = CostPolicy("synthetic", (_split(),))
    changed = CostPolicy("synthetic", (_split(sell_tax_rate=0.0018),))

    assert base.policy_hash() != changed.policy_hash()
    assert base.policy_hash() == CostPolicy("synthetic", (_split(),)).policy_hash()


@pytest.mark.parametrize(
    ("overrides", "problem"),
    [
        ({"round_trip_rate": 0.0018}, "RATE_FORM_AMBIGUOUS"),
        ({"buy_fee_rate": None, "sell_fee_rate": None, "sell_tax_rate": None},
         "RATE_FORM_AMBIGUOUS"),
        ({"sell_tax_rate": None}, "RATE_UNDETERMINED"),
        ({"buy_fee_rate": -0.1}, "RATE_OUT_OF_RANGE"),
        ({"effective_from": "2027-01-01"}, "EFFECTIVE_FROM_INVALID"),
        ({"account_condition": ""}, "MARKET_OR_ACCOUNT_CONDITION_UNDETERMINED"),
    ],
)
def test_invalid_policy_versions_are_reported(overrides, problem):
    version = _split(**overrides)

    assert problem in version.problems()
    with pytest.raises(ValueError):
        costs.realized_pnl([Fill(1, 100.0)], [Fill(1, 100.0)], version)


def test_policy_round_trips_through_its_file_form_and_rejects_unknown_keys():
    policy = CostPolicy("synthetic", (_split(),))
    raw = {"policy_id": "synthetic", "versions": [v.as_dict() for v in policy.versions]}

    assert CostPolicy.from_dict(raw).policy_hash() == policy.policy_hash()
    raw["versions"][0]["sell_tax_ratee"] = 0.1
    with pytest.raises(ValueError):
        CostPolicy.from_dict(raw)
    assert "NO_VERSIONS" in CostPolicy("synthetic", ()).problems()
