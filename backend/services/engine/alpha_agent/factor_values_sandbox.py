"""Fixed functional-factor entrypoint inside the existing isolated container."""

from __future__ import annotations

import inspect
import json
import os
from pathlib import Path


def compute_values(request: dict, output_dir: Path):
    import numpy as np
    import pandas as pd
    from backend.shared.stock_utils import StockCodeUtil

    if request.get("factor_result_mode") != "functional_values":
        raise ValueError("Unsupported functional factor operation")
    source = Path(request["factor_data_path"])
    if source != Path("/input/daily_pv.parquet"):
        raise ValueError("Functional factor input must be controller-mounted")
    import pyarrow.parquet as pq

    names = pq.read_schema(source).names
    date_column = next(
        (name for name in ("datetime", "trade_date", "date") if name in names), None
    )
    symbol_column = next(
        (name for name in ("instrument", "symbol") if name in names), None
    )
    filters = []
    if date_column and request.get("end_date"):
        date_type = pq.read_schema(source).field(date_column).type
        import pyarrow as pa

        end = request["end_date"]
        cutoff = pd.Timestamp(end) if pa.types.is_timestamp(date_type) else end
        filters.append((date_column, "<=", cutoff))
        if request.get("start_date"):
            # Fixed CPU factor input contract: retain 250+ trading bars of warmup.
            first = pd.Timestamp(request["start_date"]) - pd.Timedelta(days=550)
            cutoff = (
                first
                if pa.types.is_timestamp(date_type)
                else first.strftime("%Y-%m-%d")
            )
            filters.append((date_column, ">=", cutoff))
    if symbol_column and request.get("universe_symbols"):
        from backend.shared.stock_utils import StockCodeUtil

        codes = set()
        for symbol in request["universe_symbols"]:
            codes.update(
                [
                    StockCodeUtil.to_qlib(symbol),
                    StockCodeUtil.normalize(symbol),
                    StockCodeUtil.to_prefix(symbol),
                ]
            )
        filters.append((symbol_column, "in", sorted(codes)))
    data = pd.read_parquet(source, filters=filters or None)
    if len(data) > 1_000_000:
        raise ValueError("因子输入超过 100 万行，请缩小股票池或日期区间")
    if {"symbol", "trade_date"}.issubset(data.columns):
        data = data.set_index(["trade_date", "symbol"])
    if not isinstance(data.index, pd.MultiIndex) or data.index.nlevels != 2:
        raise ValueError("Functional input requires a date/symbol panel")
    if str(data.index.names[0]).lower() in {"instrument", "symbol"}:
        data = data.swaplevel()
    data.index = data.index.set_names(["datetime", "instrument"])
    data = data.sort_index()
    # The conventional RD-Agent calculate_* function reads this task-local HDF.
    data.to_hdf("daily_pv.h5", key="data", mode="w")
    namespace = {
        "__name__": "quantmind_factor",
        "__file__": str(Path("factor.py").resolve()),
    }
    code = request.get("code")
    if not isinstance(code, str) or len(code.encode()) > 256 * 1024:
        raise ValueError("Functional source is invalid")
    exec(compile(code, "factor.py", "exec"), namespace)
    functions = [
        value
        for key, value in namespace.items()
        if key.startswith("calculate_") and inspect.isfunction(value)
    ]
    if len(functions) != 1:
        raise ValueError("Factor must define one calculate_* function")
    fn = functions[0]
    arguments = {}
    for name, parameter in inspect.signature(fn).parameters.items():
        if name == "data_path":
            arguments[name] = "daily_pv.h5"
        elif name == "output_path":
            arguments[name] = "result.h5"
        elif parameter.default is inspect.Parameter.empty:
            raise ValueError("Unsupported calculate_* argument: " + name)
    result = fn(**arguments)
    if result is None and Path("result.h5").is_file():
        result = pd.read_hdf("result.h5")
    if isinstance(result, pd.DataFrame) and len(result.columns) == 1:
        result = result.iloc[:, 0]
    if (
        not isinstance(result, pd.Series)
        or not isinstance(result.index, pd.MultiIndex)
        or result.index.nlevels != 2
    ):
        raise ValueError(
            "Factor must return a date/symbol Series or single-column frame"
        )
    if str(result.index.names[0]).lower() in {"instrument", "symbol"}:
        result = result.swaplevel()
    dates = pd.to_datetime(result.index.get_level_values(0))
    symbols = result.index.get_level_values(1).map(StockCodeUtil.normalize)
    values = pd.to_numeric(result.to_numpy(), errors="coerce")
    output = pd.DataFrame(
        {"symbol": symbols, "trade_date": dates.strftime("%Y-%m-%d"), "value": values}
    )
    finite = np.isfinite(output["value"])
    for field, op in [("start_date", "ge"), ("end_date", "le")]:
        if request.get(field):
            finite &= getattr(output["trade_date"], op)(request[field])
    if request.get("universe_symbols"):
        allowed = {
            StockCodeUtil.normalize(symbol) for symbol in request["universe_symbols"]
        }
        finite &= output["symbol"].isin(allowed)
    output = output.loc[finite].sort_values(["trade_date", "symbol"])
    if (
        output.empty
        or len(output) > 1_000_000
        or output.duplicated(["symbol", "trade_date"]).any()
    ):
        raise ValueError(
            "Functional factor produced empty, oversized or duplicate values"
        )
    output.to_parquet(output_dir / "factor_values.parquet", index=False)
    (output_dir / "result.json").write_text(
        json.dumps({"run_id": request["run_id"], "row_count": len(output)})
    )


def main():
    request = json.loads(Path("/input/request.json").read_text())
    output = Path(os.getenv("USER_CODE_OUTPUT_DIR", "/output"))
    compute_values(request, output)


if __name__ == "__main__":
    main()
