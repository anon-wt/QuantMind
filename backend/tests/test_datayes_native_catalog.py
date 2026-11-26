"""Native Datayes/Doris datasets keep event/history semantics and source scope."""

from datetime import date

import pandas as pd
import pytest

from backend.services.engine.data_platform import datayes_sync as sync
from backend.services.engine.data_platform import doris_source as ds
from backend.services.engine.data_platform.datayes_catalog import (
    CATALOG,
    EXTRA_DATASETS,
    LOCAL_DATASETS,
    preset_mappings,
    public_catalog,
)
from backend.services.engine.data_platform.modelscope_dataset_sync import (
    PUBLIC_INITIALIZATION_DATASETS,
)
from backend.shared.quantdb_datasets import DATASETS


class NativeClient:
    def __init__(self):
        self.calls = []

    def fetch(self, endpoint, **params):
        self.calls.append((endpoint, dict(params)))
        if endpoint == "equity/getEqu":
            return pd.DataFrame(
                [
                    {
                        "secID": "600000.XSHG",
                        "ticker": "600000",
                        "exchangeCD": "XSHG",
                        "secShortName": "bank",
                        "listDate": "1999-11-10",
                        "listStatusCD": "L",
                    }
                ]
            )
        if endpoint == "master/getTradeCal":
            return pd.DataFrame(
                [
                    {"calendarDate": day, "isOpen": opened, "exchangeCD": "XSHG"}
                    for day, opened in [
                        ("2024-01-02", 1),
                        ("2024-01-03", 0),
                        ("2024-01-04", 1),
                    ]
                ]
            )
        row = {
            "secID": "600000.XSHG",
            "exchangeCD": "XSHG",
            "tradeDate": params.get("tradeDate", "2024-01-02"),
        }
        if endpoint in {"market/getMktEqud", "market/getMktEqudAdjAf"}:
            return pd.DataFrame(
                [
                    {
                        **row,
                        "openPrice": 10,
                        "highestPrice": 12,
                        "lowestPrice": 9,
                        "closePrice": 11,
                        "turnoverVol": 1000,
                        "turnoverValue": 11000,
                    }
                ]
            )
        if endpoint == "market/getMktIdxdCsi":
            return pd.DataFrame(
                [
                    {
                        **row,
                        "secID": "000852.ZICN",
                        "openIndex": 100,
                        "highestIndex": 120,
                        "lowestIndex": 90,
                        "closeIndex": 110,
                        "turnoverVol": 1000,
                        "turnoverValue": 11000,
                    }
                ]
            )
        if endpoint == "market/getMktStockFactorsDateRangePro":
            return pd.DataFrame([{**row, "ROE": 0.1, "PE": 8, "CustomFactor": 3.5}])
        if endpoint == "equity/getDy1dExposureCNE6SW21":
            return pd.DataFrame(
                [{**row, "tradeDate": "20240102", "BETA": -0.2, "SIZE": 1.3}]
            )
        if endpoint == "idx/getIdxConsCsi":
            return pd.DataFrame(
                [
                    {
                        "secID": "000852.ZICN",
                        "consID": "600000.XSHG",
                        "consExchangeCD": "XSHG",
                        "intoDate": "2020-01-01",
                        "outDate": "2024-01-05",
                        "isNew": 0,
                    }
                ]
            )
        if endpoint == "equity/getEquIndustry":
            return pd.DataFrame(
                [
                    {
                        **row,
                        "intoDate": "2020-01-01",
                        "outDate": None,
                        "industryID1": "01032121",
                        "industryName1": "bank",
                        "industryVersionCD": "010321",
                        "isNew": 0,
                    }
                ]
            )
        if endpoint == "master/getSecHalt":
            return pd.DataFrame(
                [{**row, "haltBeginTime": "2023-12-29 09:30:00", "haltEndTime": None}]
            )
        if endpoint == "equity/getSecST":
            return pd.DataFrame([{**row, "STflg": "*ST"}])
        if endpoint == "equity/getEquDiv":
            return pd.DataFrame(
                [
                    {
                        **row,
                        "endDate": "2023-12-31",
                        "newPubDate": "2024-01-02",
                        "exDivDate": "2024-02-01",
                        "perCashDiv": 0.3,
                    }
                ]
            )
        raise AssertionError(endpoint)


def test_twelve_classified_native_datasets_do_not_expand_quantdb_cloud():
    rows = public_catalog()
    assert len(rows) == 12 and len({r["dataset"] for r in rows}) == 12
    assert {r["data_type"] for r in rows} == {
        "market",
        "factor",
        "risk",
        "reference",
        "status",
        "corporate",
    }
    assert len(DATASETS) == 29 and len(PUBLIC_INITIALIZATION_DATASETS) == 26
    assert {s.dataset for s in EXTRA_DATASETS}.isdisjoint(
        PUBLIC_INITIALIZATION_DATASETS
    )
    assert len(LOCAL_DATASETS) == 36
    assert preset_mappings()["getDy1dExposureCNE6SW21"]["date_format"] == "yyyymmdd"
    assert all(
        row["doris_table"] == "ods_datayes_" + row["endpoint_key"].lower()
        for row in rows
    )


def test_all_twelve_roundtrip_and_event_dates_are_not_future_exdates(tmp_path):
    client = NativeClient()
    result = sync.sync_datayes(
        [r.dataset for r in CATALOG],
        date(2024, 1, 2),
        date(2024, 1, 4),
        client=client,
        root=tmp_path,
    )
    assert result["errors"] == 0, result
    assert len(result["datasets"]) == 12
    native = tmp_path / "6_ml_datasets/stock_factors_pro/dt=20240102/data.parquet"
    factors = pd.read_parquet(native)
    assert factors.loc[0, "CustomFactor"] == 3.5 and factors.loc[0, "ROE"] == 0.1
    assert factors.loc[0, "symbol"] == "600000.SH"
    risk = pd.read_parquet(
        tmp_path / "7_risk_data/cne6_exposure/dt=20240102/data.parquet"
    )
    assert risk.loc[0, "BETA"] == -0.2 and "PE" not in risk
    constituents = pd.read_parquet(
        tmp_path / "2_base_sector/index_constituents/000852.SH.parquet"
    )
    assert len(constituents) == 1 and constituents.loc[0, "isNew"] == 0
    assert constituents.loc[0, "symbol"] == "600000.SH"
    assert {p["intoDate"] for e, p in client.calls if e == "idx/getIdxConsCsi"} == {
        "20240102",
        "20240104",
    }
    industry = pd.read_parquet(
        tmp_path / "2_base_sector/sw_l1_industry/600000.SH.parquet"
    )
    assert (
        industry.loc[0, "industry_code"] == "01032121" and industry.loc[0, "isNew"] == 0
    )
    halts = pd.read_parquet(tmp_path / "2_base_sector/stock_halts/600000.SH.parquet")
    assert halts.loc[0, "halt_begin"] == pd.Timestamp("2023-12-29 01:30:00Z")
    dividends = pd.read_parquet(
        tmp_path / "3_financial_data/equity_dividends/600000.SH.parquet"
    )
    assert dividends.loc[0, "time"] == pd.Timestamp("2024-01-02")
    assert pd.isna(dividends.loc[0, "bonus_share_ratio"])
    params = next(p for e, p in client.calls if e == "equity/getEquDiv")
    assert "beginNewPubDate" in params and "beginDate" not in params


def test_ongoing_and_closed_interval_revisions_merge_without_duplicate_events(tmp_path):
    frame = NativeClient().fetch("master/getSecHalt")
    sync.write_frame(tmp_path, "stock_halts", sync.normalize(frame, "stock_halts"))
    frame["haltEndTime"] = "2024-01-04 15:00:00"
    sync.write_frame(tmp_path, "stock_halts", sync.normalize(frame, "stock_halts"))
    saved = pd.read_parquet(tmp_path / "2_base_sector/stock_halts/600000.SH.parquet")
    assert len(saved) == 1 and saved.loc[0, "halt_end"] == pd.Timestamp(
        "2024-01-04 07:00:00Z"
    )
    div = NativeClient().fetch("equity/getEquDiv")
    for day, amount in [("2024-01-02", 0.2), ("2024-01-04", 0.4), ("2024-01-02", 0.2)]:
        div["newPubDate"] = day
        div["perCashDiv"] = amount
        sync.write_frame(
            tmp_path, "equity_dividends", sync.normalize(div, "equity_dividends")
        )
    saved = pd.read_parquet(
        tmp_path / "3_financial_data/equity_dividends/600000.SH.parquet"
    )
    assert len(saved) == 1 and saved.loc[0, "cash_dividend_per_share"] == 0.4


def test_doris_readonly_bounds_use_business_dates_and_rank_history_before_overlap():
    client = ds.DorisClient.__new__(ds.DorisClient)
    client.config = ds.DorisConfigRequest(
        host="test", database="market", user="reader", mappings=preset_mappings()
    )
    client.should_cancel = lambda: False
    from collections import OrderedDict

    client._cache = OrderedDict()
    client.schema = lambda _: dict.fromkeys(
        [
            "secID",
            "consID",
            "intoDate",
            "outDate",
            "updateTime",
            "dt",
            "ticker",
            "tradeDate",
            "haltBeginTime",
            "haltEndTime",
        ],
        "VARCHAR",
    )
    calls = []
    client._query = lambda sql, params=(): calls.append((sql, params)) or pd.DataFrame()
    client.fetch(
        "equity/getDy1dExposureCNE6SW21", beginDate="20240102", endDate="20240104"
    )
    assert calls[-1][1] == ("20240102", "20240105")
    client.fetch(
        "master/getSecHalt",
        secID="600000.XSHG",
        beginDate="19900101",
        endDate="20240104",
        _window_start="20240102",
        _window_end="20240104",
    )
    sql, params = calls[-1]
    assert "`haltBeginTime` < %s" in sql and "`haltEndTime` >= %s" in sql
    assert params[-2:] == ("2024-01-05", "2024-01-02")
    client.fetch(
        "idx/getIdxConsCsi",
        ticker="000852",
        _window_start="20240102",
        _window_end="20240104",
    )
    sql, params = calls[-1]
    assert "ROW_NUMBER() OVER" in sql and "`updateTime` DESC" in sql
    assert "MAX(`dt`)" not in sql and sql.index("`_qm_rank`=1") < sql.index(
        "`outDate` >="
    )
    assert not any(word in sql.upper() for word in ("DELETE ", "UPDATE ", "INSERT "))


@pytest.mark.parametrize(
    "dataset,columns",
    [
        ("stock_factors_pro", {"symbol": "600000.XSHG", "time": 20240102, "ROE": 0.1}),
        (
            "stock_halts",
            {
                "symbol": "600000.SH",
                "halt_begin": "2024-01-02 09:30:00",
                "halt_end": None,
            },
        ),
        (
            "sw_l1_industry",
            {
                "symbol": "600000.SH",
                "time": "2024-01-02",
                "industry_code": "bank",
                "industry_name": "Bank",
            },
        ),
    ],
)
def test_doris_custom_mappings_share_native_normalization(dataset, columns):
    if dataset == "stock_halts":
        columns["time"] = columns["halt_begin"]
    result = ds.normalize_standard(pd.DataFrame([columns]), dataset)
    assert (
        result.loc[0, "symbol"] == "600000.SH"
        and result.loc[0, "data_source"] == "doris"
    )
