"""CPU GP search with causal labels, deterministic seeds and independent OOS."""

import random
from dataclasses import asdict

import numpy as np
import pandas as pd

from ..factorminer import dsl
from ..statistical import ic_stats, tradability
from ..statistical.worker import _sanitize
from . import formula
from .configuration import GPConfig


def run_gp_mining(
    start,
    end,
    *,
    price_panel,
    metadata,
    config=None,
    statistical_config=None,
    valid_start=None,
    valid_end=None,
    progress=None,
):
    cfg = (
        config
        if isinstance(config, GPConfig)
        else GPConfig.model_validate(config or {})
    )
    emit = progress or (lambda *_args, **_kwargs: None)
    if bool(valid_start) != bool(valid_end):
        raise ValueError("GP 验证开始和结束日期必须同时提供")
    if pd.Timestamp(start) > pd.Timestamp(end):
        raise ValueError("GP 挖掘开始日期不能晚于结束日期")
    if valid_start and (
        pd.Timestamp(valid_start) <= pd.Timestamp(end)
        or pd.Timestamp(valid_start) > pd.Timestamp(valid_end)
    ):
        raise ValueError("GP 独立验证区间必须晚于挖掘区间")
    panel = price_panel.sort_index()
    if panel.index.has_duplicates or len(panel) > 2_000_000:
        raise ValueError(
            "GP 输入存在重复股票日期，或超过 200 万行容量，请缩小区间/股票池"
        )
    # Physically exclude future validation rows BEFORE labels and fitness are built.
    training = panel.loc[panel.index.get_level_values(0) <= pd.Timestamp(end)].copy()
    if training.empty:
        raise ValueError("GP 挖掘区间没有行情")
    listing = metadata.get("listing_date") if metadata is not None else None
    td_cfg = (
        statistical_config.tradability
        if statistical_config
        else tradability.TradabilityConfig()
    )
    if metadata is not None and "is_st" in metadata:
        from ..statistical.panel import map_to_panel

        training["is_st"] = map_to_panel(training, metadata["is_st"])
    mask, filter_report = tradability.build_mask(
        training, cfg=td_cfg, listing_dates=listing
    )
    dates = training.index.get_level_values(0)
    in_window = dates >= pd.Timestamp(start)
    labels = ic_stats.forward_returns(training, cfg.horizon).where(in_window & mask)
    frames = {
        name: training["amount" if name == "$amt" else name[1:]].unstack("symbol")
        for name in formula.FIELDS
    }
    rng = random.Random(cfg.seed)
    scores = {}
    history = []

    def score(node):
        expression = formula.serialize(node)
        if expression in scores:
            return scores[expression]
        result = {"fitness": -1e9, "rank_ic_mean": None, "icir": None, "n_days": 0}
        try:
            values = formula.evaluate_panel(node, training, frames).where(
                mask & in_window
            )
            daily = ic_stats.daily_ic(values, labels, min_names=cfg.min_names)
            stats = ic_stats.summarize_ic(daily, min_days=cfg.min_days)
            if stats is not None:
                fold_means = [
                    float(fold.mean())
                    for fold in np.array_split(daily["rank_ic"].to_numpy(), 3)
                    if len(fold)
                ]
                consistency = sum(
                    np.sign(v) == np.sign(stats.rank_ic_mean) for v in fold_means
                ) / len(fold_means)
                cx = dsl.complexity(node)
                fitness = (
                    abs(stats.rank_ic_mean) * consistency
                    - cfg.complexity_penalty * cx.op_count
                )
                result = {
                    **asdict(stats),
                    "fitness": float(fitness),
                    "fold_sign_consistency": consistency,
                    "complexity": asdict(cx),
                }
        except (ValueError, ArithmeticError, KeyError):
            pass
        scores[expression] = result
        return result

    def acceptable(node):
        cx = dsl.complexity(node)
        return (
            cx.depth <= cfg.max_depth
            and cx.node_count <= cfg.max_nodes
            and dsl.min_history(node) <= formula.MAX_HISTORY
        )

    population = []
    while len(population) < cfg.population_size:
        node = formula.random_tree(rng, cfg.max_depth, cfg.windows)
        if acceptable(node):
            population.append(node)
    for generation in range(cfg.generations):
        population.sort(
            key=lambda node: (-score(node)["fitness"], formula.serialize(node))
        )
        best = score(population[0])
        history.append(
            {
                "generation": generation + 1,
                "evaluated": len(scores),
                "best_fitness": best["fitness"],
                "best_rank_ic": best.get("rank_ic_mean"),
                "best_formula": formula.serialize(population[0]),
            }
        )
        emit(
            "evolve",
            f"GP 第 {generation + 1}/{cfg.generations} 代，已评估 {len(scores)} 个公式",
            progress_pct=20 + int(65 * (generation + 1) / cfg.generations),
        )
        if generation + 1 == cfg.generations:
            break

        def select(population=population):
            return max(
                rng.choices(population, k=cfg.tournament_size),
                key=lambda node: score(node)["fitness"],
            )

        children = population[: cfg.elite_size]
        while len(children) < cfg.population_size:
            parent = select()
            child = parent
            if rng.random() < cfg.crossover_rate:
                path, _ = rng.choice(formula.paths(child))
                _, subtree = rng.choice(formula.paths(select()))
                child = formula.replace(child, path, subtree)
            if rng.random() < cfg.mutation_rate:
                path, _ = rng.choice(formula.paths(child))
                child = formula.replace(
                    child,
                    path,
                    formula.random_tree(
                        rng, rng.randint(1, cfg.max_depth), cfg.windows
                    ),
                )
            children.append(child if acceptable(child) else parent)
        population = children

    ranked = sorted(scores, key=lambda expr: (-scores[expr]["fitness"], expr))
    selected = []
    selected_values = []
    for expression in ranked:
        evidence = scores[expression]
        if evidence["fitness"] <= -1e8:
            continue
        node = formula.validate(expression)
        values = formula.evaluate_panel(node, training, frames).where(mask & in_window)
        if any(
            abs(ic_stats.daily_rank_corr(values, other, min_names=cfg.min_names)[0])
            >= cfg.dedup_corr
            for other in selected_values
        ):
            continue
        item = formula.candidate(expression)
        report = {
            "key": item.key,
            "name": item.name,
            "family": "gp",
            "selected": True,
            "eligible": True,
            "expression": item.expression,
            "gp_expression": expression,
            **evidence,
            "oos": None,
        }
        # OOS is reporting only: it cannot change fitness, ordering or selected keys.
        if valid_start and valid_end:
            validation_panel = panel.copy()
            if metadata is not None and "is_st" in metadata:
                validation_panel["is_st"] = map_to_panel(
                    validation_panel, metadata["is_st"]
                )
            oos_mask, _ = tradability.build_mask(
                validation_panel, cfg=td_cfg, listing_dates=listing
            )
            oos_values = formula.evaluate_panel(node, validation_panel).where(oos_mask)
            oos_labels = ic_stats.forward_returns(
                panel.loc[panel.index.get_level_values(0) <= pd.Timestamp(valid_end)],
                cfg.horizon,
            )
            window = (
                oos_values.index.get_level_values(0) >= pd.Timestamp(valid_start)
            ) & (oos_values.index.get_level_values(0) <= pd.Timestamp(valid_end))
            daily = ic_stats.daily_ic(
                oos_values[window],
                oos_labels.reindex(oos_values[window].index),
                min_names=cfg.min_names,
            )
            stats = ic_stats.summarize_ic(daily, min_days=cfg.min_days)
            report["oos"] = (
                asdict(stats)
                if stats is not None
                else {"n_days": len(daily), "reason": "验证有效日不足"}
            )
        selected.append(report)
        selected_values.append(values)
        if len(selected) >= cfg.top_k:
            break
    if not selected:
        raise ValueError("GP 未获得可评估公式，请增加交易日、股票数量或检查数据覆盖")
    emit("select", f"GP 搜索完成，保留 {len(selected)} 个去重因子", progress_pct=95)
    return _sanitize(
        {
            "algorithm": "gp",
            "candidates": selected,
            "selected": [r["key"] for r in selected],
            "selected_keys": [r["key"] for r in selected],
            "gp_search": {
                "config": cfg.model_dump(),
                "history": history,
                "evaluated_formulas": len(scores),
                "selection_basis": "mining_window_only",
                "oos_used_for_selection": False,
                "warning": "搜索内 IC 存在选择偏差，请另行执行独立样本外验证",
            },
            "filter_report": filter_report,
            "mining_start": start,
            "mining_end": end,
        }
    )
