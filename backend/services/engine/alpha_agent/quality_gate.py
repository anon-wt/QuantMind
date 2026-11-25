"""Algorithm-independent admission evidence; missing evidence never qualifies."""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict, Field


class QualityConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    min_is_days: int = Field(252, ge=20, le=5000, strict=True)
    min_oos_days: int = Field(126, ge=20, le=5000, strict=True)
    min_names: int = Field(30, ge=10, le=10000, strict=True)
    min_coverage: float = Field(0.8, gt=0, le=1)
    min_abs_rank_ic: float = Field(0.02, gt=0, le=1)
    min_abs_icir: float = Field(0.3, gt=0, le=100)
    min_abs_t: float = Field(3.0, gt=0, le=100)
    min_fold_consistency: float = Field(0.75, gt=0, le=1)
    min_oos_retention: float = Field(0.5, ge=0, le=2)
    min_oos_periods: int = Field(20, ge=5, le=1000, strict=True)
    fdr_q: float = Field(0.1, gt=0, le=0.2)


def normalize_quality(values: dict) -> dict:
    """New submissions always freeze the full policy and explicit holdout."""
    result = dict(values)
    result["quality_config"] = QualityConfig.model_validate(
        result.get("quality_config") or {}
    ).model_dump()
    cfg = result["quality_config"]
    if result.get("algorithm") == "statistical":
        # Algorithm stage proposes candidates; only this shared gate admits them.
        result.update(
            min_names_per_day=cfg["min_names"],
            min_valid_days=min(40, cfg["min_is_days"]),
            require_fdr=False,
            require_stability=False,
        )
    if result.get("algorithm") == "gp":
        result["gp_config"] = {
            **(result.get("gp_config") or {}),
            "min_names": cfg["min_names"],
            "min_days": min(
                (result.get("gp_config") or {}).get("min_days", 40), cfg["min_is_days"]
            ),
        }
    keys = ("mining_start", "mining_end", "valid_start", "valid_end")
    if not all(result.get(key) for key in keys):
        raise ValueError("质量入库检查必须填写挖掘区间和独立样本外验证区间")
    days = [date.fromisoformat(result[key]) for key in keys]
    if days[0] > days[1] or days[2] > days[3] or days[2] <= days[1]:
        raise ValueError("独立验证区间必须完整且晚于挖掘区间")
    return result


def evaluate_values(values, close, params: dict) -> dict:
    """Raw factor daily Spearman, h-aware HAC; direction fixed on mining only."""
    import numpy as np
    import pandas as pd

    from .label_contract import forward_returns, label_spec
    from .statistical.ic_stats import (
        daily_ic,
        newey_west_lag,
        newey_west_t_stat,
        two_sided_p,
    )

    cfg = QualityConfig.model_validate(params["quality_config"])
    h = params.get("prediction_horizon", 1)
    if values.index.has_duplicates or close.index.has_duplicates:
        raise ValueError("质量评估输入存在重复股票日期")
    values = values.reindex(close.index).replace([np.inf, -np.inf], np.nan)
    dates = pd.to_datetime(close.index.get_level_values(0))
    summaries = {}
    for name, first, last in (
        ("is", params["mining_start"], params["mining_end"]),
        ("oos", params["valid_start"], params["valid_end"]),
    ):
        # Each outcome must end within its own window; no future holdout leakage.
        window = (dates >= pd.Timestamp(first)) & (dates <= pd.Timestamp(last))
        prices = close.loc[window]
        labels = forward_returns(prices, h)
        eligible = labels.notna() & np.isfinite(labels)
        factor = values.reindex(prices.index)
        coverage = float(factor[eligible].notna().mean()) if eligible.any() else 0.0
        daily = daily_ic(factor, labels, min_names=cfg.min_names)["rank_ic"]
        n = len(daily)
        mean = float(daily.mean()) if n else None
        std = float(daily.std(ddof=1)) if n > 1 else float("nan")
        icir = mean / std if mean is not None and std > 1e-12 else None
        t, lags = newey_west_t_stat(daily, lags=max(h - 1, newey_west_lag(n)))
        p = two_sided_p(t, n - 1)
        folds = [
            float(part.mean())
            for part in np.array_split(daily.to_numpy(), 4)
            if len(part)
        ]
        summaries[name] = {
            "n_days": n,
            "coverage": coverage,
            "rank_ic_mean": mean,
            "icir": icir,
            "t_stat": float(t) if np.isfinite(t) else None,
            "p_value": float(p) if np.isfinite(p) else None,
            "hac_lags": lags,
            "fold_means": folds,
        }
    training, holdout = summaries["is"], summaries["oos"]
    direction = 1 if (training["rank_ic_mean"] or 0) > 0 else -1
    reasons = []
    for name, summary, minimum in (
        ("样本内", training, cfg.min_is_days),
        ("样本外", holdout, cfg.min_oos_days),
    ):
        if summary["n_days"] < minimum:
            reasons.append(f"{name}有效 IC 日不足（{summary['n_days']}/{minimum}）")
        if summary["coverage"] < cfg.min_coverage:
            reasons.append(f"{name}覆盖率不足")
        if (
            summary["rank_ic_mean"] is None
            or direction * summary["rank_ic_mean"] < cfg.min_abs_rank_ic
        ):
            reasons.append(f"{name}方向固定后的 RankIC 未达标")
        if summary["icir"] is None or direction * summary["icir"] < cfg.min_abs_icir:
            reasons.append(f"{name}非年化 ICIR 未达标")
        consistency = sum(direction * v > 0 for v in summary["fold_means"]) / 4
        summary["fold_sign_consistency"] = consistency
        if consistency < cfg.min_fold_consistency:
            reasons.append(f"{name}四段方向稳定性不足")
    # Number of non-overlapping h-day starts, not an independence claim.
    periods = (holdout["n_days"] + h - 1) // h
    holdout["non_overlapping_periods"] = periods
    if periods < cfg.min_oos_periods:
        reasons.append("样本外不重叠持有期数量不足")
    if holdout["t_stat"] is None or direction * holdout["t_stat"] < cfg.min_abs_t:
        reasons.append("样本外 HAC t 值未达标")
    a, b = training["rank_ic_mean"], holdout["rank_ic_mean"]
    retention = direction * b / abs(a) if a and b is not None else None
    holdout["retention"] = retention
    if retention is None or retention < cfg.min_oos_retention:
        reasons.append("样本外方向反转或衰减过大")
    return {
        "version": 1,
        "label_spec": label_spec(h),
        "direction": direction,
        "window": {
            key: params[key]
            for key in ("mining_start", "mining_end", "valid_start", "valid_end")
        },
        "metric_basis": "raw_factor_daily_spearman_nonannual_icir",
        "config": cfg.model_dump(),
        **summaries,
        "passed": False,
        "reasons": reasons,
    }


def finalize_batch(evidence: dict[str, dict], config: dict) -> dict:
    """One BH family includes every frozen candidate, including failed evaluations."""
    import math
    from .statistical.ic_stats import bh_fdr

    cfg = QualityConfig.model_validate(config)
    keys = list(evidence)
    p_values = [(evidence[k].get("oos") or {}).get("p_value") for k in keys]
    flags = bh_fdr(
        [p if p is not None and math.isfinite(p) else 1.0 for p in p_values],
        q=cfg.fdr_q,
    )
    for key, passed in zip(keys, flags, strict=True):
        item = evidence[key]
        item["fdr_pass"] = bool(passed)
        if not passed:
            item["reasons"].append("样本外多重检验未通过（BH-FDR）")
        item["passed"] = not item["reasons"]
    accepted = sum(item["passed"] for item in evidence.values())
    return {
        "version": 1,
        "config": cfg.model_dump(),
        "evaluated": len(keys),
        "accepted": accepted,
        "rejected": len(keys) - accepted,
        "candidates": evidence,
    }


def require_admission(metadata: dict, config: dict) -> None:
    """Persistence boundary check, independent of UI and algorithm selection."""
    item = metadata.get("quality_admission") or {}
    if (
        item.get("passed") is not True
        or item.get("config") != config
        or item.get("reasons")
        or item.get("version") != 1
        or item.get("fdr_pass") is not True
        or type(item.get("direction")) is not int
        or item.get("direction") not in (-1, 1)
        or (
            metadata.get("label_spec") is not None
            and metadata["label_spec"] != item.get("label_spec")
        )
    ):
        raise ValueError("因子缺少通过统一质量检查的证据，禁止入库")


def factor_direction(factor: dict) -> int:
    metadata = factor.get("metadata") or {}
    admission = metadata.get("quality_admission") or {}
    direction = admission.get("direction", 1) if admission.get("passed") else 1
    if type(direction) is not int or direction not in (-1, 1):
        raise ValueError("因子方向快照无法核验")
    return direction
