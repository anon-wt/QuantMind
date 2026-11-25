"""Keep post-search admission dates outside RD-Agent's numerical source files."""

import os
from pathlib import Path


def bound_mining_inputs(task_log_dir: str, first: str, last: str, symbols=None):
    import pandas as pd
    from backend.shared.stock_utils import StockCodeUtil

    allowed = {StockCodeUtil.normalize(s) for s in symbols} if symbols else None
    earliest = pd.Timestamp(first) - pd.Timedelta(days=550)
    for name in (
        "factor_implementation_source_data",
        "factor_implementation_source_data_debug",
    ):
        source = Path(task_log_dir) / "git_ignore_folder" / name / "daily_pv.h5"
        chunks = []
        rows = 0
        # Fixed-format HDF supports positional selection, avoiding a full-history load.
        with pd.HDFStore(source, mode="r") as store:
            size = store.get_storer("data").shape[0]
            for offset in range(0, size, 200_000):
                chunk = store.select("data", start=offset, stop=offset + 200_000)
                symbol_first = str(chunk.index.names[0]).lower() in {
                    "instrument",
                    "symbol",
                }
                dates = pd.to_datetime(
                    chunk.index.get_level_values(1 if symbol_first else 0)
                )
                keep = (dates >= earliest) & (dates <= pd.Timestamp(last))
                if allowed is not None:
                    keep &= (
                        chunk.index.get_level_values(0 if symbol_first else 1)
                        .map(StockCodeUtil.normalize)
                        .isin(allowed)
                    )
                part = chunk.loc[keep]
                rows += len(part)
                if rows > 1_000_000:
                    raise ValueError(
                        "RD 质量评估输入超过 100 万行，请缩小股票池或日期区间"
                    )
                if len(part):
                    chunks.append(part)
        if not chunks:
            raise ValueError("RD 挖掘窗口没有行情")
        temporary = source.with_name(".quality-mining.h5")
        try:
            pd.concat(chunks).sort_index().to_hdf(temporary, key="data", mode="w")
            os.replace(
                temporary, source
            )  # replace task link, never shared cache contents
        finally:
            temporary.unlink(missing_ok=True)
