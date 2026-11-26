"""Datayes 取数、编码/单位映射、原子合并与数据源读取切换回归。"""

import sys
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.services.engine.data_platform import datayes_sync as sync
from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub
from backend.shared.stock_utils import StockCodeUtil


class FakeClient:
    def __init__(self):
        self.calls = []

    def fetch(self, endpoint, **params):
        self.calls.append((endpoint, params))
        if endpoint == "equity/getEqu":
            return pd.DataFrame(
                [
                    {
                        "secID": "600000.XSHG",
                        "ticker": "600000",
                        "exchangeCD": "XSHG",
                        "secShortName": "test",
                    }
                ]
            )
        if endpoint == "master/getTradeCal":
            return pd.DataFrame(
                [
                    {"calendarDate": "2024-01-02", "exchangeCD": "XSHG", "isOpen": 1},
                    {"calendarDate": "2024-01-03", "exchangeCD": "XSHG", "isOpen": 0},
                    {"calendarDate": "2024-01-04", "exchangeCD": "XSHG", "isOpen": 1},
                ]
            )
        adjustment = {
            "market/getMktEqud": 10,
            "market/getMktEqudAdj": 11,
            "market/getMktEqudAdjAf": 12,
        }[endpoint]
        return pd.DataFrame(
            [
                {
                    "secID": "600000.XSHG",
                    "exchangeCD": "XSHG",
                    "tradeDate": params["tradeDate"],
                    "openPrice": adjustment,
                    "highestPrice": adjustment + 1,
                    "lowestPrice": adjustment - 1,
                    "closePrice": adjustment,
                    "turnoverVol": 1000,
                    "turnoverValue": 10000,
                    "PE": 8,
                    "PE1": 9,
                    "PB": 1.2,
                    "marketValue": 100000000,
                    "negMarketValue": 90000000,
                }
            ]
        )


def test_prices_route_to_verified_adjustment_endpoints_and_skip_holidays(tmp_path):
    client = FakeClient()
    root = tmp_path / "datayes"
    official = tmp_path / "quantdb"
    official.mkdir()
    (official / "untouched").write_text("official")
    result = sync.sync_datayes(
        ["daily_unadjusted", "daily_forward", "daily_backward", "valuation"],
        date(2024, 1, 2),
        date(2024, 1, 4),
        client=client,
        root=root,
    )
    assert result["errors"] == 0
    assert (official / "untouched").read_text() == "official"
    for dataset, expected in [
        ("daily_unadjusted", 10),
        ("daily_forward", 11),
        ("daily_backward", 12),
    ]:
        frame = pd.read_parquet(
            root / f"1_kline_data/{dataset}/dt=20240102/data.parquet"
        )
        assert frame.loc[0, "symbol"] == "600000.SH"
        assert frame.loc[0, "close"] == expected
        assert frame.loc[0, "volume"] == 1000  # 股，不错误乘 100。
        assert frame.loc[0, "amount"] == 10000
        assert not (root / f"1_kline_data/{dataset}/dt=20240103").exists()
    valuation = pd.read_parquet(
        root / "5_technical_derived/valuation/dt=20240102/data.parquet"
    )
    assert valuation.loc[0, "pe_ttm"] == 8  # PE=滚动；PE1=动态年化，不能互换。
    assert valuation.loc[0, "total_mv"] == 100000000  # 元。
    assert valuation.loc[0, "float_mv"] == 90000000
    hub = QuantDBDataHub(root)
    bars = hub.fetch_daily_kline("600000.SH", date(2024, 1, 1), date(2024, 1, 5))
    assert bars["close"].tolist() == [11, 11]


def test_atomic_merge_is_idempotent_and_keeps_other_securities(tmp_path):
    frame = sync.normalize(
        pd.DataFrame(
            [
                {"secID": "600000.XSHG", "tradeDate": "2024-01-02", "closePrice": 10},
                {"secID": "000001.XSHE", "tradeDate": "2024-01-02", "closePrice": 20},
            ]
        ),
        "daily_forward",
    )
    sync.write_frame(tmp_path, "daily_forward", frame)
    changed = frame.iloc[:1].copy()
    changed["close"] = 12
    sync.write_frame(tmp_path, "daily_forward", changed)
    sync.write_frame(tmp_path, "daily_forward", changed)
    result = pd.read_parquet(
        tmp_path / "1_kline_data/daily_forward/dt=20240102/data.parquet"
    )
    assert len(result) == 2
    assert dict(zip(result["symbol"], result["close"], strict=True)) == {
        "600000.SH": 12,
        "000001.SZ": 20,
    }
    assert not list(tmp_path.rglob("*.tmp"))


def test_financial_publication_history_is_not_collapsed(tmp_path):
    frame = sync.normalize(
        pd.DataFrame(
            [
                {
                    "secID": "600000.XSHG",
                    "endDate": "2023-12-31",
                    "publishDate": "2024-01-02",
                    "revenue": 100,
                },
                {
                    "secID": "600000.XSHG",
                    "endDate": "2023-12-31",
                    "publishDate": "2024-01-03",
                    "revenue": 120,
                },
            ]
        ),
        "income",
    )
    sync.write_frame(tmp_path, "income", frame)
    result = QuantDBDataHub(tmp_path).fetch_financial(
        "600000.SH", "income", end=date(2024, 1, 2)
    )
    assert result["revenue"].tolist() == [100]


def test_etf_components_and_index_constituents_are_not_collapsed(tmp_path):
    frame = sync.normalize(
        pd.DataFrame(
            [
                {
                    "secID": "510300.XSHG",
                    "tradeDate": "2024-01-02",
                    "consID": "600000.XSHG",
                    "quantity": 100,
                },
                {
                    "secID": "510300.XSHG",
                    "tradeDate": "2024-01-02",
                    "consID": "000001.XSHE",
                    "quantity": 200,
                },
            ]
        ),
        "etf_pcf",
    )
    sync.write_frame(tmp_path, "etf_pcf", frame)
    assert len(pd.read_parquet(tmp_path / "4_bond_etf/etf_pcf/510300.SH.parquet")) == 2


def test_cancelled_initialization_never_fetches(tmp_path):
    client = FakeClient()
    result = sync.sync_datayes(
        ["daily_forward"],
        date(2024, 1, 2),
        date(2024, 1, 4),
        client=client,
        root=tmp_path,
        should_cancel=lambda: True,
    )
    assert result["cancelled"]
    assert client.calls == []


def test_unknown_and_proprietary_datasets_rejected_before_fetch(tmp_path):
    client = FakeClient()
    with pytest.raises(ValueError):
        sync.sync_datayes(
            ["l1_factors"],
            date(2024, 1, 2),
            date(2024, 1, 4),
            client=client,
            root=tmp_path,
        )
    assert client.calls == []


def test_api_auth_header_redirect_policy_and_error_redaction(tmp_path, monkeypatch):
    token = "test-datayes-private-token"
    monkeypatch.setenv("DATAYES_TOKEN", token)
    monkeypatch.setenv("QM_RUNTIME_ENV_FILE", str(tmp_path / "runtime.env"))
    session = MagicMock()
    session.headers = {}
    response = session.get.return_value
    response.status_code = 200
    response.json.return_value = {"retCode": -1, "retMsg": "invalid token " + token}
    monkeypatch.setattr(sync.requests, "Session", lambda: session)
    client = sync.DatayesClient()
    with pytest.raises(RuntimeError) as error:
        client.fetch("market/getMktEqud", tradeDate="20240102")
    assert token not in str(error.value)
    assert session.headers["Authorization"] == "Bearer " + token
    assert session.get.call_args.kwargs["allow_redirects"] is False
    response.json.return_value = {"retCode": -1, "retMsg": "No Data Returned."}
    assert client.fetch("market/getMktEqud").empty
    response.json.return_value = {"retCode": -1, "retMsg": "No data permission"}
    with pytest.raises(RuntimeError):
        client.fetch("market/getMktEqud")
    response.json.return_value = {"retCode": 1, "data": [], "hasMore": True}
    with pytest.raises(RuntimeError):
        client.fetch("market/getMktEqud")
    client.close()


@pytest.mark.parametrize(
    "code,exchange,expected",
    [
        ("600000.XSHG", "", "600000.SH"),
        ("000001.XSHE", "", "000001.SZ"),
        ("920001.XBEI", "", "920001.BJ"),
        ("000300.ZICN", "XSHG", "000300.SH"),
        ("000300.ZICN", "", ""),
        ("1", "XSHE", "000001.SZ"),
    ],
)
def test_datayes_codes_are_converted_at_the_boundary(code, exchange, expected):
    assert StockCodeUtil.from_datayes(code, exchange) == expected


def test_active_reader_switches_without_overwriting_official_data(
    tmp_path, monkeypatch
):
    from backend.shared.data_source_config import save_sources
    from backend.services.engine.data_platform import quantdb_hub

    official, alternate = tmp_path / "quantdb", tmp_path / "datayes"
    official.mkdir()
    alternate.mkdir()
    (official / "marker").write_text("official")
    (alternate / "marker").write_text("alternate")
    monkeypatch.setenv("QM_DATAYES_DATA_DIR", str(alternate))
    monkeypatch.setenv("QM_DATA_SOURCE_CONFIG_FILE", str(tmp_path / "sources.json"))
    monkeypatch.setattr(quantdb_hub, "_resolve_data_dir", lambda: official)
    save_sources("A", {"quantdb": True, "datayes": True})
    hub = QuantDBDataHub()
    assert hub.data_dir == official
    save_sources("A", {"quantdb": False, "datayes": True})
    assert hub.data_dir == alternate
    save_sources("A", {"quantdb": True})
    assert hub.data_dir == official


class AllDatasetsClient(FakeClient):
    def fetch(self, endpoint, **params):
        if endpoint in {"equity/getEqu", "master/getTradeCal"} or endpoint.startswith(
            "market/getMktEqud"
        ):
            return super().fetch(endpoint, **params)
        self.calls.append((endpoint, params))
        if endpoint == "master/getSecTypeRel":
            return pd.DataFrame(
                [{"secID": "600000.XSHG", "typeID": "concept-1", "typeName": "concept"}]
            )
        if endpoint == "equity/getEquIndustry":
            return pd.DataFrame(
                [
                    {
                        "secID": "600000.XSHG",
                        "industryVersionCD": "v1",
                        "industryID": "industry-1",
                        "industryName1": "industry",
                        "industryID1": "industry-1",
                        "intoDate": "2020-01-01",
                        "outDate": None,
                        "isNew": 1,
                    }
                ]
            )
        native = {
            "market/getMktStockFactorsDateRangePro": {"tradeDate": "2024-01-02", "ROE": 0.1},
            "equity/getDy1dExposureCNE6SW21": {"tradeDate": "20240102", "BETA": 0.2},
            "equity/getSecST": {"tradeDate": "2024-01-02", "STflg": "ST"},
            "master/getSecHalt": {"haltBeginTime": "2024-01-01 09:30:00", "haltEndTime": None},
            "equity/getEquDiv": {"newPubDate": "2024-01-02", "endDate": "2023-12-31", "perCashDiv": 0.2},
            "idx/getIdxConsCsi": {"secID": "000852.ZICN", "consID": "600000.XSHG", "consExchangeCD": "XSHG", "intoDate": "2020-01-01", "outDate": None},
        }
        if endpoint in native:
            return pd.DataFrame([{**{"secID": "600000.XSHG"}, **native[endpoint]}])
        if endpoint == "idx/getIdxCloseWeight":
            return pd.DataFrame(
                [
                    {
                        "secID": params["ticker"] + ".ZICN",
                        "consID": "600000.XSHG",
                        "consExchangeCD": "XSHG",
                        "weight": 5.0,
                        "effDate": "2024-01-02",
                    }
                ]
            )
        if endpoint == "fund/getFundETFPRList":
            return pd.DataFrame(
                [
                    {
                        "secID": "510300.XSHG",
                        "tradeDate": "2024-01-02",
                        "creationUnit": 900000,
                    }
                ]
            )
        if endpoint == "fund/getFundETFCons":
            return pd.DataFrame(
                [
                    {
                        "secID": "510300.XSHG",
                        "tradeDate": "2024-01-02",
                        "consID": "600000.XSHG",
                        "quantity": 100,
                    }
                ]
            )
        if endpoint == "bond/getBondConvPriceChg":
            return pd.DataFrame([{"secID": "110000.XSHG"}])
        if endpoint == "bond/getBondConv":
            return pd.DataFrame([{"secID": "110000.XSHG", "initialConvPrice": 9.0}])
        if endpoint == "market/getMktConsBondPerf":
            return pd.DataFrame(
                [
                    {
                        "secID": "110000.XSHG",
                        "tradeDate": "2024-01-02",
                        "closePriceBond": 100.0,
                    }
                ]
            )
        if endpoint == "market/getMktAdjf":
            return pd.DataFrame(
                [
                    {
                        "secID": "600000.XSHG",
                        "exDivDate": "2024-01-02",
                        "endDate": "2099-12-31",
                        "accumAdjFactor": 2.0,
                    }
                ]
            )
        return pd.DataFrame(
            [
                {
                    "secID": "600000.XSHG",
                    "endDate": "2023-12-31",
                    "publishDate": "2024-01-02",
                    "value": 1,
                }
            ]
        )


def test_all_direct_dataset_families_write_compatible_paths(tmp_path):
    client = AllDatasetsClient()
    datasets = [
        name
        for name in sync.ENDPOINTS
        if name
        not in {
            "daily_unadjusted",
            "daily_forward",
            "daily_backward",
            "index_daily",
            "valuation",
        }
    ]
    result = sync.sync_datayes(
        datasets, date(2024, 1, 2), date(2024, 1, 2), client=client, root=tmp_path
    )
    assert result["errors"] == 0, result
    weights = pd.read_parquet(
        tmp_path / "2_base_sector/index_weights/000300.SH.parquet"
    )
    assert weights["symbol"].tolist() == ["600000.SH"]
    assert weights["weight"].tolist() == [0.05]
    sectors = pd.read_parquet(
        tmp_path / "2_base_sector/sector_concept/sector_members.parquet"
    )
    assert set(sectors["sector_type"]) == {"concept", "industry"}
    assert (tmp_path / "4_bond_etf/convertible_bond/110000.SH.parquet").is_file()
    assert (tmp_path / "4_bond_etf/etf_pcf/510300.SH.parquet").is_file()
    dividends = QuantDBDataHub(tmp_path).fetch_financial(
        "600000.SH", "dividend_factors", end=date(2024, 1, 2)
    )
    assert dividends["adj_factor"].tolist() == [2.0]
    pit = next(
        params
        for endpoint, params in client.calls
        if endpoint == "fundamental/getFdmtIndiPSTTMPIT"
    )
    assert "beginPubDate" in pit and "endPubDate" in pit and "beginDate" not in pit


def test_derived_features_are_backward_looking_and_keep_valuation_columns(tmp_path):
    dates = pd.bdate_range("2023-09-01", "2024-01-04")
    frame = pd.DataFrame(
        {
            "symbol": "600000.SH",
            "time": dates,
            "open": range(1, len(dates) + 1),
            "high": range(2, len(dates) + 2),
            "low": range(len(dates)),
            "close": range(1, len(dates) + 1),
            "volume": 1000,
            "amount": 10000,
            "total_mv": 1,
        }
    )
    sync.write_frame(tmp_path, "daily_forward", frame)
    sync.write_frame(
        tmp_path, "instrument_detail", pd.DataFrame({"symbol": ["600000.SH"]})
    )
    valuation = pd.DataFrame(
        {
            "symbol": ["600000.SH"],
            "time": [pd.Timestamp("2024-01-02")],
            "pe_ttm": [8],
            "total_mv": [100000000],
            "float_mv": [90000000],
        }
    )
    sync.write_frame(tmp_path, "valuation", valuation)
    rows = sync._derived(
        tmp_path, "features_daily", date(2024, 1, 2), date(2024, 1, 4), lambda: False
    )
    assert rows == 3
    first = pd.read_parquet(
        tmp_path / "6_ml_datasets/features_daily/dt=20240102/data.parquet"
    ).iloc[0]
    assert first["pe_ttm"] == 8 and first["total_mv"] == 100000000
    assert first["rsi_14"] == 100
    past = frame.loc[frame["time"] <= pd.Timestamp("2024-01-02"), "close"]
    assert first["ma20"] == past.tail(20).mean()


def test_datayes_root_cannot_overwrite_official_quantdb_directory(
    tmp_path, monkeypatch
):
    from backend.services.engine.data_platform import modelscope_dataset_sync

    monkeypatch.setattr(
        modelscope_dataset_sync, "resolve_data_root", lambda: tmp_path / "quantdb"
    )
    monkeypatch.setenv("QM_DATAYES_DATA_DIR", str(tmp_path / "quantdb" / "overlap"))
    with pytest.raises(ValueError):
        sync.data_root()
