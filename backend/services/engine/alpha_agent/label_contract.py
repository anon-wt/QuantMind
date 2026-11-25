"""One forecast label for mining and its immutable downstream factor jobs."""

from __future__ import annotations

import json

MAX_HORIZON = 250


def horizon(value=1) -> int:
    if type(value) is not int or not 1 <= value <= MAX_HORIZON:
        raise ValueError("预测周期须为 1～250 个交易日的整数")
    return value


def label_spec(value=1) -> dict:
    h = horizon(value)
    return {
        "version": 1,
        "kind": "close_to_close",
        "start_offset": 0,
        "horizon": h,
        "expression": f"Ref($close, -{h})/$close - 1",
    }


def normalize_request(values: dict) -> dict:
    """Canonical field wins; accept old single-period inputs without ambiguity."""
    result = dict(values)
    h = result.get("prediction_horizon")
    gp = result.get("gp_config") or {}
    periods = result.get("horizons")
    if h is None:
        if result.get("algorithm") == "gp":
            h = gp.get("horizon", 1)
        elif periods:
            if len(periods) != 1:
                raise ValueError("请指定一个预测周期；新任务不再在多个 Y 周期中选优")
            h = periods[0]
        else:
            h = 1
    h = horizon(h)
    result["prediction_horizon"] = h
    if result.get("algorithm") == "statistical":
        result["horizons"] = [h]
    if result.get("algorithm") == "gp":
        result["gp_config"] = {**gp, "horizon": h}
    return result


def factor_label(factor: dict) -> dict:
    """Preserve explicit labels; infer the selected period for historical factors."""
    metadata = factor.get("metadata") or factor.get("metadata_json") or {}
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    if not isinstance(metadata, dict):
        raise ValueError("因子元数据无法核验")
    spec = metadata.get("label_spec")
    if spec is not None:
        if not isinstance(spec, dict) or spec != label_spec(spec.get("horizon")):
            raise ValueError("因子标签快照无法核验")
        return spec
    evidence = metadata.get("evidence") or {}
    h = metadata.get("prediction_horizon") or evidence.get("best_horizon")
    if h is None:
        h = (metadata.get("gp_config") or {}).get("horizon", 1)
    return label_spec(h)


def forward_returns(close, value=1, *, symbol_level=1):
    """Calendar-aligned close[t+h]/close[t]-1; missing prices never shift dates."""
    h = horizon(value)
    wide = close.unstack(symbol_level).sort_index()
    future = wide.shift(-h)
    result = future / wide - 1
    # Avoid pandas-version-dependent stack(dropna=...) behavior.
    import pandas as pd

    index = pd.MultiIndex.from_product(
        [wide.index, wide.columns],
        names=[
            close.index.names[1 - symbol_level],
            close.index.names[symbol_level],
        ],
    )
    out = pd.Series(result.to_numpy().ravel(), index=index)
    if symbol_level == 0:
        out = out.swaplevel()
    return out.reindex(close.index).rename(f"fwd_ret_{h}d")
