"""Real GP search, reproducibility, causal windows and formula parity."""

import numpy as np
import pandas as pd
import pytest

from backend.services.engine.alpha_agent.gp.configuration import GPConfig
from backend.services.engine.alpha_agent.gp.engine import run_gp_mining
from backend.services.engine.alpha_agent.gp import formula
from backend.services.engine.alpha_agent.statistical.materialization import (
    trusted_statistical_candidate,
)


def market_panel():
    rng = np.random.default_rng(8)
    dates = pd.bdate_range("2023-01-02", periods=110)
    symbols = [f"{i:06}.SZ" for i in range(1, 13)]
    index = pd.MultiIndex.from_product([dates, symbols], names=["trade_date", "symbol"])
    close = np.exp(np.cumsum(rng.normal(0.001, 0.02, (110, 12)), axis=0)) * 20
    volume = rng.uniform(1e6, 2e6, (110, 12))
    frame = pd.DataFrame(
        {"close": close.ravel(), "volume": volume.ravel()}, index=index
    )
    frame["open"] = frame["close"] * 0.995
    frame["high"] = frame["close"] * 1.01
    frame["low"] = frame["close"] * 0.99
    frame["amount"] = frame["close"] * frame["volume"]
    metadata = pd.DataFrame({"listing_date": pd.Timestamp("2000-01-01")}, index=symbols)
    return frame, metadata, dates


def test_gp_search_is_reproducible_and_oos_never_selects():
    panel, metadata, dates = market_panel()
    config = GPConfig(
        population_size=8,
        generations=2,
        top_k=2,
        min_days=10,
        min_names=5,
        windows=[2, 5],
        max_depth=3,
    )
    kwargs = {
        "metadata": metadata,
        "config": config,
        "valid_start": str(dates[85].date()),
        "valid_end": str(dates[-1].date()),
    }
    first = run_gp_mining(
        str(dates[30].date()), str(dates[80].date()), price_panel=panel, **kwargs
    )
    changed = panel.copy()
    after = changed.index.get_level_values(0) > dates[80]
    changed.loc[after, ["open", "high", "low", "close", "amount"]] *= 100
    second = run_gp_mining(
        str(dates[30].date()), str(dates[80].date()), price_panel=changed, **kwargs
    )
    assert first["gp_search"]["history"] == second["gp_search"]["history"]
    assert first["selected_keys"] == second["selected_keys"]
    assert first["gp_search"]["oos_used_for_selection"] is False
    assert first["gp_search"]["evaluated_formulas"] > 1
    assert all(item["n_days"] >= 10 for item in first["candidates"])
    assert all(
        item["complexity"]["depth"] <= config.max_depth for item in first["candidates"]
    )


def test_formula_evaluation_and_materialization_share_semantics():
    panel, _, _ = market_panel()
    expression = "Div(Delta($close,5),Mean($volume,5))"
    item = formula.candidate(expression)
    metadata = {
        "algorithm": "gp",
        "candidate_key": item.key,
        "gp_expression": expression,
    }
    restored = trusted_statistical_candidate(metadata, item.expression)
    wide_values = formula.evaluate_panel(formula.validate(expression), panel)
    for symbol, part in panel.groupby(level="symbol"):
        expected = restored.compute(part.droplevel("symbol"))
        actual = wide_values.xs(symbol, level="symbol")
        pd.testing.assert_series_equal(
            actual.iloc[item.min_history :],
            expected.iloc[item.min_history :],
            check_names=False,
        )
    with pytest.raises(ValueError):
        trusted_statistical_candidate(metadata, "$close")


@pytest.mark.parametrize(
    "changes",
    [
        {"population_size": 129},
        {"generations": 100},
        {"windows": [0]},
        {"windows": [5, 5]},
        {"windows": [5.1]},
        {"elite_size": 8, "population_size": 8},
        {"population_size": 128, "generations": 30},
    ],
)
def test_gp_limits_are_enforced(changes):
    with pytest.raises(ValueError):
        GPConfig(**changes)


@pytest.mark.parametrize(
    "expression",
    [
        "__import__('os')",
        "Mean($close,100000)",
        "Delay($close,-1)",
        "SignedPower($close,999)",
    ],
)
def test_gp_rejects_unsafe_or_noncausal_formulas(expression):
    with pytest.raises(ValueError):
        formula.validate(expression)
