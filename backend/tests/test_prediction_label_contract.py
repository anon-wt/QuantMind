"""Golden outcomes, sparse calendars, target provenance and non-overlap NAV."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

from backend.services.engine.alpha_agent.label_contract import (
    factor_label,
    forward_returns,
    horizon,
    label_spec,
    normalize_request,
)
from backend.services.engine.alpha_agent.factor_job_series import build_factor_series
from backend.services.engine.alpha_agent.factor_job_budget import (
    record_container_intent,
)


def test_factor_sandbox_records_only_its_own_lease_and_pool():
    lease = SimpleNamespace(
        task_id="job", tenant_id="tenant", user_id="user", token="token"
    )
    budget = SimpleNamespace(
        pool="alpha_factor_jobs",
        store=SimpleNamespace(transition=MagicMock(return_value=True)),
    )
    labels = {
        "quantmind.job_id": "job",
        "quantmind.tenant_id": "tenant",
        "quantmind.user_id": "user",
        "quantmind.lease_token": "token",
        "quantmind.resource_pool": "alpha_factor_jobs",
        "quantmind.role": "sandbox",
    }
    keeper = {**labels, "quantmind.role": "output_keeper"}
    arguments = {
        "daemon_id": "daemon", "container_name": "sandbox", "labels": labels,
        "related_containers": [{"name": "keeper", "labels": keeper}],
        "volume_name": "volume", "volume_labels": keeper, "budget": budget,
    }
    assert record_container_intent(lease, **arguments)
    assert budget.store.transition.call_args.args == (
        "alpha_factor_jobs",
        lease,
        "running",
    )
    assert (
        budget.store.transition.call_args.kwargs["executor"]["container_labels"]
        == labels
    )
    budget.store.transition.reset_mock()
    assert not record_container_intent(
        lease, **{**arguments, "labels": {**labels, "quantmind.user_id": "other"}}
    )
    budget.store.transition.assert_not_called()


from backend.services.engine.rd_agent.label_adapter import (
    rewrite_label_template,
    purge_segment_ends,
)


def test_rd_split_tails_do_not_use_prices_in_the_next_split():
    dates = pd.bdate_range("2024-01-01", periods=50)
    env = {
        "train_start": str(dates[0].date()),
        "train_end": str(dates[19].date()),
        "valid_start": str(dates[20].date()),
        "valid_end": str(dates[34].date()),
        "test_start": str(dates[35].date()),
        "test_end": str(dates[49].date()),
    }
    result = purge_segment_ends(env, dates, 5)
    assert result["train_end"] == str(dates[14].date())
    assert result["valid_end"] == str(dates[29].date())
    assert result["test_end"] == str(dates[44].date())
    assert env["train_end"] == str(dates[19].date())


@pytest.mark.parametrize("h", [1, 5, 20])
def test_same_y_in_both_panel_orders_and_no_symbol_bleed(h):
    dates = pd.bdate_range("2024-01-01", periods=30)
    index = pd.MultiIndex.from_product(
        [dates, ["600000.SH", "000001.SZ"]], names=["date", "symbol"]
    )
    close = pd.Series(
        np.array([[10 + i, 100 + i * 3] for i in range(30)]).ravel(), index=index
    )
    values = forward_returns(close, h)
    assert values.loc[(dates[0], "600000.SH")] == pytest.approx((10 + h) / 10 - 1)
    assert values.loc[(dates[0], "000001.SZ")] == pytest.approx((100 + h * 3) / 100 - 1)
    assert values.loc[dates[-h:]].isna().all()
    opposite = (
        forward_returns(close.swaplevel().sort_index(), h, symbol_level=0)
        .swaplevel()
        .sort_index()
    )
    pd.testing.assert_series_equal(values.sort_index(), opposite)
    sparse = close.drop((dates[h], "600000.SH"))
    assert pd.isna(forward_returns(sparse, h).loc[(dates[0], "600000.SH")])


@pytest.mark.parametrize("algorithm", ["rdagent", "statistical", "gp"])
def test_one_user_period_normalizes_every_algorithm(algorithm):
    values = normalize_request(
        {
            "algorithm": algorithm,
            "prediction_horizon": 5,
            "horizons": [1, 20],
            "gp_config": {"horizon": 1},
        }
    )
    assert values["prediction_horizon"] == 5
    if algorithm == "statistical":
        assert values["horizons"] == [5]
    if algorithm == "gp":
        assert values["gp_config"]["horizon"] == 5


@pytest.mark.parametrize("value", [0, -1, 251, True, 1.5, "5"])
def test_bad_period_is_rejected(value):
    with pytest.raises(ValueError):
        horizon(value)


def test_saved_target_and_historical_selected_period_are_inherited():
    assert factor_label({"metadata": {"label_spec": label_spec(5)}}) == label_spec(5)
    assert factor_label({"metadata": {"evidence": {"best_horizon": 20}}}) == label_spec(
        20
    )
    assert factor_label({"metadata": {"gp_config": {"horizon": 10}}}) == label_spec(10)
    assert factor_label({}) == label_spec(1)
    with pytest.raises(ValueError):
        factor_label({"metadata": {"label_spec": {**label_spec(5), "start_offset": 1}}})
    with pytest.raises(ValueError):
        normalize_request({"algorithm": "statistical", "horizons": [1, 5]})


def test_rd_templates_are_rewritten_to_the_same_raw_y():
    root = (
        Path(__file__).resolve().parents[2]
        / "rd-agent/rdagent/scenarios/qlib/experiment/factor_template"
    )
    for name in [
        "conf_baseline.yaml",
        "conf_combined_factors.yaml",
        "conf_combined_factors_sota_model.yaml",
    ]:
        text = rewrite_label_template((root / name).read_text(encoding="utf8"), 5)
        assert "label:\n" in text and label_spec(5)["expression"] in text
        assert "Ref($close, -2)/Ref($close, -1)" not in text
        assert "CSZScoreNorm" in text


def test_five_day_nav_does_not_compound_overlapping_daily_outcomes():
    dates = pd.bdate_range("2024-01-01", periods=20)
    index = pd.MultiIndex.from_product([dates, range(12)], names=["date", "symbol"])
    values = pd.Series(np.tile(np.arange(12), 20), index=index, dtype=float)
    returns = pd.Series(0.1, index=index)
    draw = build_factor_series(values, returns, cost_rate=0, prediction_horizon=5)
    assert len(draw["dates"]) == 4
    assert draw["nav_long"][-1] == pytest.approx(1.1**4)
    assert draw["rebalance_days"] == 5
    assert len(draw["lifecycle_ic_series"]["dates"]) <= 20
