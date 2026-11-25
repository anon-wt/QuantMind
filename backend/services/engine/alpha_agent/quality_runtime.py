"""Use the same admission service for trusted formula and isolated RD values."""

from __future__ import annotations

from .quality_gate import evaluate_values, finalize_batch


def gate_formula_result(result: dict, panel, params: dict) -> dict:
    import numpy as np
    import pandas as pd
    from .statistical.materialization import trusted_statistical_candidate

    reports = [r for r in result.get("candidates", []) if r.get("selected")]
    composite = result.get("composite")
    if isinstance(composite, dict) and composite.get("eligible"):
        reports.append(composite)
    evidence = {}
    for report in reports:
        key = report["key"]
        try:
            candidate = trusted_statistical_candidate(
                {
                    "algorithm": params["algorithm"],
                    "candidate_key": key,
                    "evidence": report,
                    "gp_expression": report.get("gp_expression"),
                },
                report["expression"],
            )
            values = np.full(len(panel), np.nan)
            for positions in panel.groupby(level=1, sort=False).indices.values():
                part = panel.iloc[positions].droplevel(1)
                values[positions] = (
                    candidate.compute(part).reindex(part.index).to_numpy()
                )
            evidence[key] = evaluate_values(
                pd.Series(values, index=panel.index), panel["close"], params
            )
        except Exception as exc:
            evidence[key] = {
                "passed": False,
                "reasons": ["数值质量评估失败：" + str(exc)[:400]],
            }
    audit = finalize_batch(evidence, params["quality_config"])
    for report in reports:
        item = evidence[report["key"]]
        report["quality_admission"] = item
        if not item["passed"]:
            report["selected"] = False
            report["eligible"] = False
            report["reject_reason"] = "；".join(item["reasons"])
    result["selected"] = [
        r["key"] for r in reports if r.get("quality_admission", {}).get("passed")
    ]
    result["quality_audit"] = audit
    return result


def gate_rd_factors(
    factors: list[dict],
    params: dict,
    *,
    tenant_id: str,
    user_id: str,
    universe: str,
    symbols=None,
) -> dict:
    import hashlib
    from .factor_job_executor import execute_factor_backtest
    from .label_contract import label_spec

    evidence = {}
    for factor in factors:
        name = factor["name"]
        code = factor.get("code", "")
        captured = []
        try:
            request = {
                "factor_id": "unpersisted-quality-candidate",
                "factor_code": code,
                "factor_revision_no": 1,
                "factor_code_sha256": hashlib.sha256(code.encode()).hexdigest(),
                "universe": universe,
                "data_source": "qlib_bin",
                "universe_symbols": symbols,
                "start_date": params["mining_start"],
                "end_date": params["valid_end"],
                "label_spec": label_spec(params["prediction_horizon"]),
                "datasets": params.get("datasets"),
            }
            execute_factor_backtest(
                request,
                tenant_id=tenant_id,
                user_id=user_id,
                lease=None,
                budget=None,
                lifecycle={},
                progress=lambda _: None,
                panel_callback=captured.append,
            )
            if not captured:
                raise ValueError("公共数值面板不可用")
            evidence[name] = evaluate_values(
                captured[0]["values"], captured[0]["close"], params
            )
        except Exception as exc:
            evidence[name] = {
                "passed": False,
                "reasons": ["数值质量评估失败：" + str(exc)[:400]],
            }
    return finalize_batch(evidence, params["quality_config"])
