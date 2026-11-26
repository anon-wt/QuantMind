"""Datayes 数据商城 → 独立的 QuantMind 规范 parquet 目录。

协议与字段依据 https://data-api-doc.wmcloud.com/doc/apis/{id}。
不写入 QuantDB 官方目录，不将通联财务/技术字段冒充 QuantDB 专有因子。
"""

from __future__ import annotations

import os
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from collections.abc import Callable

import pandas as pd
import requests

from .datayes_catalog import (
    LOCAL_DATASETS as DATASETS,
    get_local_spec as get_dataset_spec,
    CATALOG,
    BY_DATASET,
)
from backend.shared.runtime_secrets import get_secret
from backend.shared.stock_utils import StockCodeUtil

API_ROOT = "https://api.datayes.com/data/v1/api"
# method/category/mode; day=全市场单日，batch=50 个证券的日期窗口。
ENDPOINTS = {
    "daily_forward": ("market/getMktEqudAdj", "day"),
    "sector_concept": ("master/getSecTypeRel", "sectors"),
    "index_weights": ("idx/getIdxCloseWeight", "weights"),
    "margin_trading": ("equity/getFstDetail", "batch"),
    "balance": ("fundamental/getFdmtBS", "publication"),
    "income": ("fundamental/getFdmtIS", "publication"),
    "cashflow": ("fundamental/getFdmtCF", "publication"),
    "capital": ("equity/getEquShare", "publication"),
    "pershare_index": ("fundamental/getFdmtIndiPSPit", "publication"),
    "per_share": ("fundamental/getFdmtIndiPSTTMPIT", "pit"),
    "dividend_factors": ("market/getMktAdjf", "batch"),
    "holder_num": ("equity/getEquShareholderNum", "publication_alt"),
    "valuation": ("market/getMktEqud", "day"),
    "etf_pcf": ("fund/getFundETFPRList", "etf"),
    "convertible_bond": ("market/getMktConsBondPerf", "bonds"),
}
ENDPOINTS.update({row.dataset: (row.endpoint, row.mode) for row in CATALOG})
DERIVED = {"technical_indicators", "features_daily"}
SUPPORTED = set(ENDPOINTS) | DERIVED
UNSUPPORTED_REASON = "暂无可直接对齐的商城接口或数据定义，使用 QuantDB 同步此数据集"


def data_root() -> Path:
    from .modelscope_dataset_sync import resolve_data_root

    official = resolve_data_root().resolve()
    configured = os.getenv("QM_DATAYES_DATA_DIR", "").strip()
    root = Path(configured).resolve() if configured else official.parent / "datayes"
    if root == official or root.is_relative_to(official):
        raise ValueError("Datayes 数据目录必须独立于 QuantDB 官方目录")
    return root


class SyncCancelled(Exception):
    pass


class DatayesClient:
    def __init__(self, should_cancel: Callable[[], bool] = lambda: False):
        self._token = get_secret("DATAYES_TOKEN")
        if not self._token:
            raise ValueError("请先到个人中心 → 数据平台配置 Datayes Token")
        self._session = requests.Session()
        self._session.headers["Authorization"] = f"Bearer {self._token}"
        self._cancel = should_cancel

    def close(self):
        self._session.close()

    def fetch(self, endpoint: str, **params) -> pd.DataFrame:
        # The SQL adapter consumes these bounds for interval overlap; the vendor
        # endpoints for complete membership/industry history have no such args.
        params.pop("_window_start", None)
        params.pop("_window_end", None)
        for attempt in range(3):
            if self._cancel():
                raise SyncCancelled()
            try:
                response = self._session.get(
                    f"{API_ROOT}/{endpoint}.json",
                    params=params,
                    timeout=(10, 45),
                    allow_redirects=False,
                )
            except requests.RequestException:
                raise RuntimeError("Datayes 网络请求失败，请检查服务器网络") from None
            if response.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                for _ in range(2**attempt):
                    if self._cancel():
                        raise SyncCancelled()
                    time.sleep(1)
                continue
            if response.status_code in (401, 403):
                raise RuntimeError("Datayes Token 无效或该接口尚未开通数据权限")
            if response.status_code != 200:
                raise RuntimeError(f"Datayes 请求失败（HTTP {response.status_code}）")
            try:
                payload = response.json()
            except ValueError:
                raise RuntimeError("Datayes 返回了无效 JSON") from None
            if payload.get("retCode") != 1:
                reason = str(payload.get("retMsg") or "未知接口错误")
                if reason.strip().casefold().rstrip(".") in {
                    "no data returned",
                    "no data",
                    "无数据",
                    "暂无数据",
                }:
                    return pd.DataFrame()
                reason = reason.replace(self._token, "[已隐藏]")[:300]
                raise RuntimeError(f"Datayes 接口失败：{reason}")
            if not isinstance(payload.get("data"), list):
                raise RuntimeError("Datayes 返回的数据格式不正确")
            if any(payload.get(key) for key in ("truncated", "hasMore")):
                raise RuntimeError("Datayes 返回结果被截断，请缩小日期或证券范围")
            return pd.DataFrame(payload["data"])
        raise RuntimeError("Datayes 暂时不可用")


def normalize(frame: pd.DataFrame, dataset: str) -> pd.DataFrame:
    if frame.empty:
        return frame
    frame = frame.copy()
    if dataset == "index_constituents":
        # secID denotes the index here, not the constituent equity.
        if (
            "secID" in frame
            and not frame["secID"]
            .astype(str)
            .isin(["000852.ZICN", "000852.XSHG", "000852.SH"])
            .all()
        ):
            raise ValueError("中证1000成分接口返回了其他指数的数据")
        frame["index_sec_id"] = frame.get("secID")
        frame["index_symbol"] = StockCodeUtil.normalize("000852.SH")
        if "consID" in frame:
            frame["secID"] = frame["consID"]
            frame["exchangeCD"] = frame.get("consExchangeCD", "")
    if dataset != "trading_calendar":

        def symbol(row):
            return StockCodeUtil.from_datayes(
                row.get("secID") or row.get("indexID") or row.get("ticker", ""),
                row.get("exchangeCD", ""),
            ) or StockCodeUtil.from_datayes(
                row.get("ticker", ""), row.get("exchangeCD", "")
            )

        frame["symbol"] = (
            frame["symbol"].map(
                lambda value: (
                    StockCodeUtil.from_datayes(value) or StockCodeUtil.normalize(value)
                )
            )
            if "symbol" in frame
            else frame.apply(symbol, axis=1)
        )
        if not frame["symbol"].astype(str).str.fullmatch(r"\d{6}\.(SH|SZ|BJ)").all():
            raise ValueError("Datayes 返回无法识别的证券/交易所编码")
    aliases = {
        "openPrice": "open",
        "highestPrice": "high",
        "lowestPrice": "low",
        "closePrice": "close",
        "turnoverVol": "volume",
        "turnoverValue": "amount",
        "openIndex": "open",
        "highestIndex": "high",
        "lowestIndex": "low",
        "closeIndex": "close",
        "preClosePrice": "pre_close",
        "closePriceBond": "close",
        "secShortNameBond": "stock_name",
        "PE": "pe_ttm",
        "PE1": "pe_dynamic",
        "PB": "pb",
        "marketValue": "total_mv",
        "negMarketValue": "circ_mv",
        "turnoverRate": "turnover_rate",
        "secShortName": "stock_name",
        "fundShortName": "stock_name",
        "EPS": "eps",
        "nAssetPS": "bps",
        "listDate": "list_date",
        "accumAdjFactor": "adj_factor",
        "publishDate": "m_anntime",
        "endDate": "m_timetag",
        "totalShares": "total_shares",
        "nonrestFloatShares": "float_shares",
        "finVal": "rzye",
        "finBuyVal": "rzmre",
        "secVal": "rqye",
        "secVol": "rqyl",
        "tradeVal": "rzrqye",
        "shNum": "holder_num",
        "typeID": "sector_code",
        "typeName": "sector_name",
    }
    for original, canonical in aliases.items():
        if original in frame and canonical not in frame:
            frame[canonical] = frame[original]
    if "circ_mv" in frame:
        frame["float_mv"] = frame["circ_mv"]
    if dataset == "instrument_detail" and "stock_name" in frame:
        frame["name"] = frame["stock_name"]
        frame["Name"] = frame["stock_name"]
    if dataset == "instrument_detail" and "list_date" in frame:
        frame["J_start"] = pd.to_datetime(
            frame["list_date"], errors="coerce"
        ).dt.strftime("%Y%m%d")
    if dataset == "instrument_detail" and "listStatusCD" in frame:
        frame["IsQuitGP"] = frame["listStatusCD"].eq("DE")
    date_col = next(
        (
            c
            for c in (
                "tradeDate",
                "calendarDate",
                "effDate",
                "endDate",
                "changeDate",
                "exDivDate",
            )
            if c in frame
        ),
        None,
    )
    if date_col:
        frame["time"] = pd.to_datetime(frame[date_col], errors="coerce").dt.normalize()
    native = BY_DATASET.get(dataset)
    if native and native.date_column:
        if native.date_column not in frame:
            raise ValueError(f"{dataset} 缺少日期字段 {native.date_column}")
        business_date = pd.to_datetime(
            frame[native.date_column].astype(str), format="mixed", errors="coerce"
        )
        if business_date.dt.tz is not None:
            business_date = business_date.dt.tz_convert("Asia/Shanghai").dt.tz_localize(
                None
            )
        frame["time"] = business_date.dt.normalize()
        if frame["time"].isna().any():
            raise ValueError(f"{dataset} 包含无法解析的业务日期")
    if dataset in {"stock_factors_pro", "cne6_exposure"}:
        frame["date"] = frame["time"].dt.strftime("%Y-%m-%d")
    if dataset in {"index_constituents", "sw_l1_industry"}:
        frame["effective_from"] = frame["time"]
        frame["effective_to"] = pd.to_datetime(
            frame.get("outDate", pd.Series(pd.NaT, index=frame.index)), errors="coerce"
        )
    if dataset == "sw_l1_industry":
        frame["industry_code"] = frame["industryID1"]
        frame["industry_name"] = frame["industryName1"]
        frame["industry_version"] = frame["industryVersionCD"]
    if dataset == "stock_halts":
        for original, target in (
            ("haltBeginTime", "halt_begin"),
            ("haltEndTime", "halt_end"),
        ):
            parsed = pd.to_datetime(
                frame.get(original, pd.Series(pd.NaT, index=frame.index)),
                errors="coerce",
            )
            frame[target] = (
                parsed.dt.tz_localize("Asia/Shanghai")
                if parsed.dt.tz is None
                else parsed
            ).dt.tz_convert("UTC")
    if dataset == "stock_st":
        frame["st_flag"] = frame["STflg"]
        frame["is_st"] = (
            frame["STflg"].astype(str).str.upper().str.contains("ST", regex=False)
        )
    if dataset == "equity_dividends":
        if "endDate" not in frame:
            raise ValueError("分红送转缺少报告期 endDate / m_timetag")
        frame["endDate"] = pd.to_datetime(
            frame["endDate"].astype(str), format="mixed", errors="coerce"
        ).dt.strftime("%Y-%m-%d")
        if frame["endDate"].isna().any():
            raise ValueError("分红送转报告期无法解析")
        frame["publication_date"] = frame["time"]
        frame["cash_dividend_per_share"] = frame.get("perCashDiv")
        frame["bonus_share_ratio"] = frame.get("perShareDivRatio")
        frame["transfer_share_ratio"] = frame.get("perShareTransRatio")
    if dataset == "dividend_factors" and "exDivDate" in frame:
        frame["m_timetag"] = frame["exDivDate"]
        frame["time"] = pd.to_datetime(
            frame["exDivDate"], errors="coerce"
        ).dt.normalize()
    frame["data_source"] = "datayes"
    return frame


def write_frame(root: Path, dataset: str, frame: pd.DataFrame) -> int:
    if frame.empty:
        return 0
    spec = get_dataset_spec(dataset)
    directory = root / spec.rel_dir
    if spec.layout == "partition":
        if "time" not in frame or frame["time"].isna().any():
            raise ValueError(f"{dataset} 缺少有效交易日期")
        pieces = [
            (directory / f"dt={dt}" / "data.parquet", part)
            for dt, part in frame.groupby(frame["time"].dt.strftime("%Y%m%d"))
        ]
    elif spec.layout == "symbol":
        key = (
            "index_symbol"
            if dataset in {"index_weights", "index_constituents"}
            else "symbol"
        )
        pieces = [
            (directory / f"{symbol}.parquet", part)
            for symbol, part in frame.groupby(key)
        ]
    else:
        filename = {
            "instrument_detail": "instrument_list",
            "sector_concept": "sector_members",
        }.get(dataset, dataset)
        pieces = [(directory / f"{filename}.parquet", frame)]
    for path, part in pieces:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            part = pd.concat([pd.read_parquet(path), part], ignore_index=True)
        identity = [
            c
            for c in (
                "symbol",
                "index_symbol",
                "time",
                "m_anntime",
                "m_timetag",
                "reportType",
                "mergedFlag",
                "sector_code",
                "exchangeCD",
                "exDivDate",
                "changeDate",
                "consID",
                "annoID",
            )
            if c in part
        ]
        if dataset == "instrument_detail":
            identity = ["symbol"]
        identity = {
            "index_constituents": ["index_symbol", "symbol", "time"],
            "sw_l1_industry": ["symbol", "time", "industry_version"],
            "stock_halts": ["symbol", "halt_begin"],
            "stock_st": ["symbol", "time"],
            "equity_dividends": ["symbol", "endDate"],
        }.get(dataset, identity)
        if dataset == "equity_dividends":
            part = part.sort_values("time", kind="stable")
        part = part.drop_duplicates(subset=identity or None, keep="last")
        temporary = path.with_name(f".{path.name}.{threading.get_ident()}.tmp")
        try:
            part.to_parquet(temporary, index=False)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    return len(frame)


def _windows(start: date, end: date, days: int = 90):
    while start <= end:
        stop = min(end, start + timedelta(days=days - 1))
        yield start.strftime("%Y%m%d"), stop.strftime("%Y%m%d")
        start = stop + timedelta(days=1)


def filter_native_window(frame, dataset, start, end):
    """Interval datasets include records starting before the requested window."""
    if frame.empty or dataset not in BY_DATASET or dataset == "instrument_detail":
        return frame
    native = BY_DATASET[dataset]
    keep = frame["time"] <= pd.Timestamp(end)
    if native.interval_end:
        until = pd.to_datetime(
            frame.get(native.interval_end, pd.Series(pd.NaT, index=frame.index)),
            errors="coerce",
        )
        if until.dt.tz is not None:
            until = until.dt.tz_convert("Asia/Shanghai").dt.tz_localize(None)
        keep &= until.isna() | (until >= pd.Timestamp(start))
    else:
        keep &= frame["time"] >= pd.Timestamp(start)
    return frame.loc[keep].copy()


def _derived(
    root: Path, dataset: str, start: date, end: date, cancelled, source_name="datayes"
):
    from .quantdb_hub import QuantDBDataHub

    if (end - start).days > 365:
        rows = 0
        for begin, stop in _windows(start, end, 365):
            if cancelled():
                raise SyncCancelled()
            rows += _derived(
                root,
                dataset,
                datetime.strptime(begin, "%Y%m%d").date(),
                datetime.strptime(stop, "%Y%m%d").date(),
                cancelled,
                source_name,
            )
        return rows
    hub = QuantDBDataHub(root)
    prices = hub.fetch_daily_kline_batch(
        hub.fetch_stock_list()["symbol"].tolist(),
        start - timedelta(days=400),
        end,
    )
    if prices.empty:
        raise ValueError("派生指标需要日线前复权数据，请先同步日线")
    prices = prices[
        ["symbol", "trade_date", "open", "high", "low", "close", "volume", "amount"]
    ]
    prices = prices.sort_values(["symbol", "trade_date"])
    pieces = []
    for _, part in prices.groupby("symbol", sort=False):
        if cancelled():
            raise SyncCancelled()
        part = part.copy()
        close = pd.to_numeric(part["close"], errors="coerce")
        for window in (5, 10, 20, 60):
            part[f"ma{window}"] = close.rolling(window, min_periods=window).mean()
            part[f"ret_{window}"] = close.pct_change(window, fill_method=None)
        diff = close.diff()
        up = diff.clip(lower=0).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
        down = (
            (-diff.clip(upper=0)).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
        )
        part["rsi_14"] = (100 - 100 / (1 + up / down)).mask((up == 0) & (down == 0), 50)
        part["macd_dif"] = (
            close.ewm(span=12, adjust=False).mean()
            - close.ewm(span=26, adjust=False).mean()
        )
        part["macd_dea"] = part["macd_dif"].ewm(span=9, adjust=False).mean()
        part["macd"] = 2 * (part["macd_dif"] - part["macd_dea"])
        part["macd_hist"] = part["macd"]
        part["time"] = pd.to_datetime(part["trade_date"]).dt.normalize()
        pieces.append(part[part["time"].dt.date >= start])
    result = pd.concat(pieces, ignore_index=True)
    if dataset == "features_daily":
        valuation = hub.fetch_valuation(start=start, end=end)
        if not valuation.empty:
            valuation["time"] = pd.to_datetime(
                valuation["trade_date"]
                if "trade_date" in valuation
                else valuation["time"]
            ).dt.normalize()
            columns = [
                c
                for c in (
                    "symbol",
                    "time",
                    "pe",
                    "pe_ttm",
                    "pb",
                    "total_mv",
                    "circ_mv",
                    "float_mv",
                    "turnover_rate",
                )
                if c in valuation
            ]
            result = result.merge(valuation[columns], on=["symbol", "time"], how="left")
    result["data_source"] = source_name
    return write_frame(root, dataset, result)


def plan_datasets(datasets: list[str]) -> list[str]:
    selected = ["instrument_detail", "trading_calendar", *datasets]
    if DERIVED.intersection(selected):
        selected = ["daily_forward", "valuation", *selected]
    return list(dict.fromkeys(selected))


def _special(client, mode, params):
    if mode == "sectors":
        concept = client.fetch("master/getSecTypeRel", **params)
        concept["sector_type"] = "concept"
        industry = client.fetch("equity/getEquIndustry", **params)
        if not industry.empty:
            if "isNew" in industry:
                industry = industry[
                    pd.to_numeric(industry["isNew"], errors="coerce") == 1
                ].copy()
            if industry.empty:
                return concept
            industry["typeID"] = (
                industry["industryVersionCD"].astype(str)
                + ":"
                + industry["industryID"].astype(str)
            )
            names = industry[
                [
                    name
                    for name in ("industryName3", "industryName2", "industryName1")
                    if name in industry
                ]
            ]
            if names.empty:
                raise ValueError("Datayes 行业数据缺少行业名称")
            industry["typeName"] = names.bfill(axis=1).iloc[:, 0]
            industry["sector_type"] = "industry"
        return pd.concat([concept, industry], ignore_index=True)
    if mode == "weights":
        index = params.pop("index_symbol")
        frame = client.fetch("idx/getIdxCloseWeight", **params)
        if frame.empty:
            fallback = {
                "000300.SH": "getCSIIdxWeightHS300",
                "000905.SH": "getCSIIdxWeightZZ500",
                "000852.SH": "getCSIIdxWeightZZ1000",
            }.get(index)
            if fallback and params["endDate"] >= date.today().replace(day=1).strftime(
                "%Y%m%d"
            ):
                frame = client.fetch("idx/" + fallback)
                if not frame.empty:
                    dates = pd.to_datetime(
                        frame["effDate"], errors="coerce"
                    ).dt.strftime("%Y%m%d")
                    frame = frame[
                        (dates >= params["beginDate"]) & (dates <= params["endDate"])
                    ]
        if not frame.empty:
            frame["datayes_index_id"] = frame["secID"]
            frame["secID"] = frame["consID"]
            frame["exchangeCD"] = frame["consExchangeCD"]
            frame["index_symbol"] = index
            frame["weight_pct"] = pd.to_numeric(frame["weight"], errors="coerce")
            frame["weight"] = frame["weight_pct"] / 100
        return frame
    if mode == "bonds":
        frame = client.fetch("market/getMktConsBondPerf", **params)
        if not frame.empty:
            terms = client.fetch("bond/getBondConv", secID=params["secID"])
            if not terms.empty:
                frame = frame.merge(
                    terms, on="secID", how="left", suffixes=("", "_terms")
                )
        return frame
    frame = client.fetch("fund/getFundETFPRList", **params)
    if frame.empty:
        return frame
    result = []
    identifiers = frame["secID"].drop_duplicates().tolist()
    for i in range(0, len(identifiers), 10):
        components = client.fetch(
            "fund/getFundETFCons", secID=",".join(identifiers[i : i + 10]), **params
        )
        if not components.empty:
            result.append(
                components.merge(
                    frame, on=["secID", "tradeDate"], how="left", suffixes=("", "_pcf")
                )
            )
    return pd.concat(result, ignore_index=True) if result else frame


def sync_datayes(
    datasets: list[str],
    start: date,
    end: date,
    *,
    progress=None,
    should_cancel=lambda: False,
    client=None,
    root: Path | None = None,
    source_name: str = "datayes",
    local_datasets: set[str] | None = None,
):
    unknown = set(datasets) - SUPPORTED
    if unknown:
        raise ValueError("Datayes 暂不支持：" + ", ".join(sorted(unknown)))
    root = root or data_root()
    selected = plan_datasets(datasets)
    local_datasets = local_datasets or set()
    order = [
        s.dataset
        for s in DATASETS
        if s.dataset in selected
        and s.dataset not in DERIVED
        and s.dataset not in local_datasets
    ]
    order += [
        s.dataset
        for s in DATASETS
        if s.dataset in selected
        and s.dataset in DERIVED
        and s.dataset not in local_datasets
    ]
    own_client = client is None
    client = client or DatayesClient(should_cancel)
    results = []

    def normalize_source(frame, dataset):
        normalized = normalize(frame, dataset)
        if not normalized.empty:
            normalized["data_source"] = source_name
        return normalized

    try:
        if should_cancel():
            raise SyncCancelled()
        stocks = client.fetch("equity/getEqu", equTypeCD="A", listStatusCD="L,S,DE")
        stocks = normalize_source(stocks, "instrument_detail")
        if stocks.empty:
            raise ValueError("Datayes 股票列表为空，无法初始化")
        if "instrument_detail" not in local_datasets:
            write_frame(root, "instrument_detail", stocks)
        calendar = normalize_source(
            client.fetch(
                "master/getTradeCal",
                exchangeCD="XSHG,XSHE",
                beginDate=(
                    start - timedelta(days=400)
                    if DERIVED.intersection(selected)
                    else start
                ).strftime("%Y%m%d"),
                endDate=end.strftime("%Y%m%d"),
            ),
            "trading_calendar",
        )
        if calendar.empty or "isOpen" not in calendar:
            raise ValueError("Datayes 交易日历为空或缺少开市标记")
        trading_days = sorted(
            set(
                calendar.loc[
                    pd.to_numeric(calendar["isOpen"], errors="coerce") == 1, "time"
                ].dt.date
            )
        )
        if "trading_calendar" not in local_datasets:
            write_frame(root, "trading_calendar", calendar)
        sec_ids = stocks["secID"].drop_duplicates().tolist()
        batches = [sec_ids[i : i + 50] for i in range(0, len(sec_ids), 50)]
        for dataset in order:
            if should_cancel():
                raise SyncCancelled()
            rows = 0
            try:
                if progress:
                    progress(
                        "dataset_start",
                        dataset=dataset,
                        message=f"{source_name}：{get_dataset_spec(dataset).name}",
                    )
                if dataset in DERIVED:
                    rows = _derived(
                        root, dataset, start, end, should_cancel, source_name
                    )
                else:
                    endpoint, mode = ENDPOINTS[dataset]
                    if mode == "stocks":
                        requests_to_make = [None]
                    elif mode == "calendar":
                        requests_to_make = [None]
                    elif mode in ("day", "index"):
                        requests_to_make = []
                        for day in trading_days:
                            if day < start and not (
                                dataset == "daily_forward"
                                and DERIVED.intersection(selected)
                            ):
                                continue
                            params = {"tradeDate": day.strftime("%Y%m%d")}
                            if mode == "index":
                                params["exchangeCD"] = "XSHG,XSHE"
                            requests_to_make.append(params)
                    elif mode == "sectors":
                        requests_to_make = [
                            {"secID": ",".join(batch)} for batch in batches
                        ]
                    elif mode == "constituents":
                        requests_to_make = [
                            {
                                "ticker": "000852",
                                "_window_start": start.strftime("%Y%m%d"),
                                "_window_end": end.strftime("%Y%m%d"),
                            }
                        ]
                        if not getattr(client, "supports_interval_history", False):
                            # No date defaults to current members in Datayes; query
                            # every trading date so transient historical members survive.
                            requests_to_make = [
                                {
                                    **requests_to_make[0],
                                    "intoDate": day.strftime("%Y%m%d"),
                                }
                                for day in trading_days
                                if day >= start
                            ]
                    elif mode == "industry_history":
                        requests_to_make = [
                            {
                                "secID": ",".join(batch),
                                "industryVersionCD": "010321",
                                "_window_start": start.strftime("%Y%m%d"),
                                "_window_end": end.strftime("%Y%m%d"),
                            }
                            for batch in batches
                        ]
                    elif mode == "halt_history":
                        requests_to_make = [
                            {
                                "secID": ",".join(batch),
                                "beginDate": "19900101",
                                "endDate": end.strftime("%Y%m%d"),
                                "_window_start": start.strftime("%Y%m%d"),
                                "_window_end": end.strftime("%Y%m%d"),
                            }
                            for batch in batches
                        ]
                    elif mode == "weights":
                        requests_to_make = [
                            {
                                "index_symbol": index,
                                "ticker": StockCodeUtil.digits(index),
                                "beginDate": begin,
                                "endDate": stop,
                            }
                            for index in ("000300.SH", "000905.SH", "000852.SH")
                            for begin, stop in _windows(start, end, 30)
                        ]
                    elif mode == "etf":
                        requests_to_make = [
                            {
                                "beginDate": day.strftime("%Y%m%d"),
                                "endDate": day.strftime("%Y%m%d"),
                            }
                            for day in trading_days
                            if day >= start
                        ]
                    elif mode == "bonds":
                        bonds = client.fetch("bond/getBondConvPriceChg", field="secID")
                        identifiers = (
                            bonds["secID"].drop_duplicates().tolist()
                            if not bonds.empty
                            else []
                        )
                        requests_to_make = [
                            {
                                "secID": ",".join(identifiers[i : i + 20]),
                                "beginDate": begin,
                                "endDate": stop,
                            }
                            for begin, stop in _windows(start, end, 30)
                            for i in range(0, len(identifiers), 20)
                        ]
                    else:
                        requests_to_make = []
                        for begin, stop in _windows(start, end):
                            for batch in batches:
                                params = {"secID": ",".join(batch)}
                                keys = {
                                    "pit": ("beginPubDate", "endPubDate"),
                                    "publication": (
                                        "publishDateBegin",
                                        "publishDateEnd",
                                    ),
                                    "publication_alt": (
                                        "beginPublishDate",
                                        "endPublishDate",
                                    ),
                                    "dividends": ("beginNewPubDate", "endNewPubDate"),
                                }.get(mode, ("beginDate", "endDate"))
                                params.update(zip(keys, (begin, stop), strict=True))
                                requests_to_make.append(params)
                    for number, params in enumerate(requests_to_make, 1):
                        if should_cancel():
                            raise SyncCancelled()
                        if params is None:
                            frame = calendar if mode == "calendar" else stocks
                        else:
                            raw = (
                                _special(client, mode, dict(params))
                                if mode in ("weights", "etf", "bonds", "sectors")
                                else client.fetch(endpoint, **params)
                            )
                            frame = normalize_source(raw, dataset)
                        frame = filter_native_window(frame, dataset, start, end)
                        if not frame.empty and dataset in {
                            "daily_forward",
                            "daily_backward",
                            "daily_unadjusted",
                            "index_daily",
                        }:
                            required = {
                                "symbol",
                                "time",
                                "open",
                                "high",
                                "low",
                                "close",
                                "volume",
                                "amount",
                            }
                            if required - set(frame.columns):
                                raise ValueError(
                                    "Datayes 行情缺少必要字段，不能写入完整日线数据集"
                                )
                        if mode == "day" and not frame.empty:
                            frame = frame[frame["symbol"].isin(stocks["symbol"])]
                        rows += write_frame(root, dataset, frame)
                        if progress:
                            progress(
                                "batch",
                                dataset=dataset,
                                done=number,
                                total=len(requests_to_make),
                                rows=rows,
                            )
                item = {
                    "dataset": dataset,
                    "status": "completed" if rows else "empty",
                    "rows": rows,
                }
            except SyncCancelled:
                raise
            except Exception as exc:
                item = {
                    "dataset": dataset,
                    "status": "failed",
                    "rows": rows,
                    "error": str(exc)[:300],
                }
            results.append(item)
            if progress:
                progress("dataset_done", **item)
        return {
            "root": str(root),
            "datasets": results,
            "cancelled": False,
            "errors": sum(r["status"] == "failed" for r in results),
        }
    except SyncCancelled:
        return {"root": str(root), "datasets": results, "cancelled": True, "errors": 0}
    finally:
        if own_client:
            client.close()
