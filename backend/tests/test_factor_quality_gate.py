"""Admission must test actual held-out values, not accept algorithm scores."""

import copy

import numpy as np
import pandas as pd
import pytest

from backend.services.engine.alpha_agent.quality_gate import (
    QualityConfig,
    evaluate_values,
    finalize_batch,
    normalize_quality,
    require_admission,
)


@pytest.fixture
def sample():
    rng = np.random.default_rng(83)
    days = pd.bdate_range("2020-01-01", periods=500)
    symbols = [f"{600000 + i}.SH" for i in range(40)]
    changes = rng.normal(0, 0.01, (500, 40))
    prices = pd.DataFrame(
        100 * np.exp(changes.cumsum(axis=0)), index=days, columns=symbols
    )
    close = prices.stack()
    close.index.names = ["trade_date", "symbol"]
    signal = (
        (prices.shift(-1) / prices - 1 + rng.normal(0, 0.02, prices.shape))
        .stack()
        .reindex(close.index)
    )
    params = {
        "algorithm": "gp",
        "prediction_horizon": 1,
        "mining_start": str(days[0].date()),
        "mining_end": str(days[299].date()),
        "valid_start": str(days[300].date()),
        "valid_end": str(days[-1].date()),
        "quality_config": QualityConfig().model_dump(),
    }
    return signal, close, params


def test_positive_and_negative_stable_signals_qualify(sample):
    signal, close, params = sample
    for direction in (1, -1):
        item = evaluate_values(signal * direction, close, params)
        audit = finalize_batch({"known_predictor": item}, params["quality_config"])
        assert audit["accepted"] == 1, item["reasons"]
        assert item["direction"] == direction
        require_admission({"quality_admission": item}, params["quality_config"])


def test_holdout_reversal_rejected_even_when_absolute_ic_is_high(sample):
    signal, close, params = sample
    signal.loc[signal.index.get_level_values(0) >= params["valid_start"]] *= -1
    item = evaluate_values(signal, close, params)
    finalize_batch({"reversal": item}, params["quality_config"])
    assert not item["passed"]
    assert any("方向反转" in r for r in item["reasons"])


def test_mining_labels_never_read_holdout_prices(sample):
    signal, close, params = sample
    a = evaluate_values(signal, close, params)
    changed = close.copy()
    changed.loc[changed.index.get_level_values(0) >= params["valid_start"]] *= 10
    b = evaluate_values(signal, changed, params)
    assert a["is"] == b["is"]
    assert a["is"]["n_days"] == 299
    assert a["oos"]["n_days"] == 199


@pytest.mark.parametrize("kind", ["constant", "missing", "short", "long_horizon"])
def test_insufficient_evidence_never_passes(sample, kind):
    signal, close, params = sample
    if kind == "constant":
        signal[:] = 1
    if kind == "missing":
        signal.iloc[::2] = np.nan
    if kind == "short":
        params["quality_config"]["min_oos_days"] = 300
    if kind == "long_horizon":
        params["prediction_horizon"] = 50
    item = evaluate_values(signal, close, params)
    finalize_batch({kind: item}, params["quality_config"])
    assert not item["passed"] and item["reasons"]
    if kind == "long_horizon":
        assert item["oos"]["hac_lags"] >= 49


def test_multiple_testing_includes_failed_candidates():
    cfg = QualityConfig().model_dump()
    evidence = {
        "marginal": {"reasons": [], "oos": {"p_value": 0.06}},
        "evaluation_failed": {"reasons": ["计算失败"]},
    }
    audit = finalize_batch(evidence, cfg)
    assert audit["accepted"] == 0
    assert not evidence["marginal"]["fdr_pass"]


def test_thresholds_frozen_and_no_disable_or_nonfinite(sample):
    _, _, params = sample
    normalized = normalize_quality(params)
    assert normalized["gp_config"]["min_days"] == 40
    for bad in ({"enabled": False}, {"min_abs_rank_ic": float("nan")}, {"fdr_q": 1}):
        with pytest.raises(ValueError):
            QualityConfig.model_validate(bad)
    for field in ("valid_start", "mining_end"):
        invalid = dict(params)
        invalid.pop(field)
        with pytest.raises(ValueError):
            normalize_quality(invalid)
    item = {"passed": True, "reasons": [], "config": normalized["quality_config"]}
    tampered = copy.deepcopy(normalized["quality_config"])
    tampered["min_abs_icir"] = 0.1
    with pytest.raises(ValueError):
        require_admission({"quality_admission": item}, tampered)


def test_formula_and_rd_use_identical_numeric_admission(sample, monkeypatch):
    from types import SimpleNamespace
    from backend.services.engine.alpha_agent.quality_runtime import (
        gate_formula_result,
        gate_rd_factors,
    )
    from backend.services.engine.alpha_agent.statistical import materialization
    from backend.services.engine.alpha_agent import factor_job_executor

    signal, close, params = sample
    panel = pd.DataFrame({"close": close, "signal": signal})
    monkeypatch.setattr(
        materialization,
        "trusted_statistical_candidate",
        lambda *_: SimpleNamespace(compute=lambda frame: frame["signal"]),
    )

    def numerical_sandbox(_request, **kwargs):
        kwargs["panel_callback"]({"values": signal, "close": close})

    monkeypatch.setattr(
        factor_job_executor, "execute_factor_backtest", numerical_sandbox
    )
    result = {
        "candidates": [{"key": "formula", "expression": "$close", "selected": True}]
    }
    formula = gate_formula_result(result, panel, params)["quality_audit"]["candidates"][
        "formula"
    ]
    rd = gate_rd_factors(
        [{"name": "rd", "code": "frozen_code"}],
        params,
        tenant_id="t",
        user_id="u",
        universe="csi300",
    )["candidates"]["rd"]
    assert formula == rd


def test_rd_persistence_rejects_before_database_access():
    import asyncio
    import ast
    import json
    from pathlib import Path
    from typing import Any

    source = Path("backend/services/engine/qlib_app/services/rd_agent_persistence.py")
    tree = ast.parse(source.read_text(encoding="utf-8"))
    cls = next(
        n
        for n in tree.body
        if isinstance(n, ast.ClassDef) and n.name == "RDAgentFactorPersistence"
    )
    method = next(
        n
        for n in cls.body
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "save_factor"
    )
    cls.body = [method]
    namespace = {"Any": Any, "json": json}
    exec(
        compile(ast.Module(body=[cls], type_ignores=[]), str(source), "exec"), namespace
    )
    RDAgentFactorPersistence = namespace["RDAgentFactorPersistence"]

    with pytest.raises(ValueError, match="禁止入库"):
        asyncio.run(
            RDAgentFactorPersistence().save_factor(
                "id",
                "name",
                "code",
                metadata={},
                quality_config=QualityConfig().model_dump(),
            )
        )
