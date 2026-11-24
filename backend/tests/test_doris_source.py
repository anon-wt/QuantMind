"""Doris -> Parquet：字段契约、只读 SQL、增量修订、取消、进度与目录切换。"""

import json
from datetime import date

import pandas as pd
import pytest
from pydantic import ValidationError

from backend.services.engine.data_platform import doris_source as ds
from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("QM_DORIS_CONFIG_FILE", str(tmp_path / "doris.json"))
    monkeypatch.setenv("QM_RUNTIME_ENV_FILE", str(tmp_path / "runtime.env"))
    for key in (
        "DORIS_FE_HOST",
        "DORIS_FE_QUERY_PORT",
        "DORIS_DATABASE",
        "DORIS_USER",
        "DORIS_PASSWORD",
    ):
        monkeypatch.delenv(key, raising=False)
    return ds.DorisConfigRequest(host="doris.test", database="market", user="reader")


def mapping(table="bars"):
    return ds.DatasetMapping(
        table=table,
        date_column="trade_day",
        columns={
            "symbol": "code",
            "time": "trade_day",
            "open": "o",
            "high": "h",
            "low": "l",
            "close": "c",
            "volume": "vol",
            "amount": "amt",
        },
    )


def raw_frame(day="2024-01-02", close=10):
    return pd.DataFrame(
        [
            {
                "code": "SH600036",
                "trade_day": day,
                "o": 10,
                "h": 12,
                "l": 9,
                "c": close,
                "vol": 1000,
                "amt": 10000,
            }
        ]
    )


def test_secret_is_not_returned_or_written_into_table_config(config):
    config.password = ds.SecretStr("test-only-password")
    config.dataset_mappings = {"daily_forward": mapping()}
    ds.save_config(config)
    payload = ds.public_config()
    assert payload["password_configured"]
    assert "password" not in payload["connection"]
    assert "test-only-password" not in json.dumps(payload)
    assert "test-only-password" not in ds.config_path().read_text()
    assert ds.load_config().dataset_mappings["daily_forward"].columns["close"] == "c"
    assert next(
        item for item in payload["datasets"] if item["dataset"] == "daily_forward"
    )["path"].endswith("dt={YYYYMMDD}/data.parquet")


@pytest.mark.parametrize(
    "table", ["bars;DROP TABLE bars", "market.bars.extra", "../bars", "bars` --"]
)
def test_arbitrary_sql_and_paths_are_rejected(table):
    with pytest.raises(ValidationError):
        ds.TableMapping(table=table)


def test_intraday_mapping_is_rejected_instead_of_losing_timestamp_precision(config):
    with pytest.raises(ValidationError, match="分钟/逐笔"):
        ds.DorisConfigRequest(
            host="doris.test",
            database="market",
            user="reader",
            dataset_mappings={
                "tick_data": {"table": "ticks"},
            },
        )


def test_custom_field_mapping_uses_bound_half_open_dates(config, monkeypatch):
    client = object.__new__(ds.DorisClient)
    monkeypatch.setattr(
        client, "schema", lambda _: dict.fromkeys(raw_frame().columns, "varchar")
    )
    calls = []

    def query(sql, params):
        calls.append((sql, params))
        return raw_frame()

    monkeypatch.setattr(client, "_query", query)
    frame = client.fetch_dataset(
        "daily_forward", mapping(), date(2024, 1, 2), date(2024, 1, 2)
    )
    assert calls == [
        (
            "SELECT * FROM `bars` WHERE `trade_day` >= %s AND `trade_day` < %s",
            ("2024-01-02", "2024-01-03"),
        )
    ]
    assert frame["symbol"].tolist() == ["600036.SH"]
    assert frame["close"].tolist() == [10]
    assert frame["data_source"].tolist() == ["doris"]
    assert frame["time"].iloc[0] == pd.Timestamp("2024-01-02")


def test_missing_mapped_column_does_not_silently_create_null_data(config, monkeypatch):
    client = object.__new__(ds.DorisClient)
    monkeypatch.setattr(client, "schema", lambda _: {"code": "varchar"})
    with pytest.raises(ValueError, match="缺少映射字段"):
        client.fetch_dataset(
            "daily_forward", mapping(), date(2024, 1, 2), date(2024, 1, 2)
        )


class DirectClient:
    def __init__(self, config):
        self.config = config
        self.calls = []
        self.close_value = 10
        self.cancel = False

    def fetch_dataset(self, dataset, binding, start, end):
        self.calls.append((start, end))
        return ds.normalize_standard(
            raw_frame(start.isoformat(), self.close_value).rename(
                columns={
                    "code": "symbol",
                    "trade_day": "time",
                    "o": "open",
                    "h": "high",
                    "l": "low",
                    "c": "close",
                    "vol": "volume",
                    "amt": "amount",
                }
            ),
            dataset,
        )


def test_incremental_overlap_replaces_values_and_keeps_history(config, tmp_path):
    config.dataset_mappings = {"daily_forward": mapping()}
    config.lookback_days = 2
    client = DirectClient(config)
    root = tmp_path / "data"
    first = ds.sync_doris(
        ["daily_forward"], date(2024, 1, 1), date(2024, 1, 5), root=root, client=client
    )
    assert first["errors"] == 0
    client.close_value = 11
    second = ds.sync_doris(
        ["daily_forward"], date(2024, 1, 1), date(2024, 1, 6), root=root, client=client
    )
    assert second["effective_start"] == "2024-01-04"
    assert client.calls[-1] == (date(2024, 1, 4), date(2024, 1, 6))
    assert (root / "1_kline_data/daily_forward/dt=20240101/data.parquet").is_file()
    frame = pd.read_parquet(
        root / "1_kline_data/daily_forward/dt=20240104/data.parquet"
    )
    assert frame["close"].tolist() == [11]
    ds.sync_doris(
        ["daily_forward"],
        date(2024, 1, 4),
        date(2024, 1, 4),
        incremental=False,
        root=root,
        client=client,
    )
    assert (
        len(
            pd.read_parquet(
                root / "1_kline_data/daily_forward/dt=20240104/data.parquet"
            )
        )
        == 1
    )
    bars = QuantDBDataHub(root).fetch_daily_kline(
        "600036.SH", date(2024, 1, 1), date(2024, 1, 6)
    )
    assert not bars.empty


def test_mapping_change_resets_watermark_and_cancel_does_not_advance(config, tmp_path):
    config.dataset_mappings = {"daily_forward": mapping()}
    client = DirectClient(config)
    root = tmp_path / "data"
    ds.sync_doris(
        ["daily_forward"], date(2024, 1, 1), date(2024, 1, 5), root=root, client=client
    )
    state = (root / ".doris_sync_state.json").read_text()
    cancelled = ds.sync_doris(
        ["daily_forward"],
        date(2024, 1, 1),
        date(2024, 1, 6),
        root=root,
        client=client,
        should_cancel=lambda: True,
    )
    assert cancelled["cancelled"] is True
    assert (root / ".doris_sync_state.json").read_text() == state
    config.dataset_mappings["daily_forward"] = mapping("new_bars")
    restarted = ds.sync_doris(
        ["daily_forward"], date(2024, 1, 1), date(2024, 1, 6), root=root, client=client
    )
    assert restarted["effective_start"] == "2024-01-01"


def test_same_directory_cannot_sync_concurrently(tmp_path):
    with ds._sync_lock(tmp_path):
        with pytest.raises(ValueError, match="已有 Doris 同步"):
            with ds._sync_lock(tmp_path):
                pytest.fail("second writer acquired lock")


def test_standard_financial_rows_require_publication_time():
    frame = pd.DataFrame([{"symbol": "600036.SH", "time": "2024-01-02", "revenue": 1}])
    with pytest.raises(ValueError, match="披露日期"):
        ds.normalize_standard(frame, "income")


def test_cancel_closes_streaming_connection_without_draining_results():
    class Connection:
        open = True
        cancelled = False
        cursor_closed = False

        def cursor(self):
            return self

        def execute(self, sql, params):
            pass

        def fetchmany(self, size):
            self.cancelled = True
            return [{"value": 1}]

        def close(self):
            assert self.open
            self.open = False

    client = object.__new__(ds.DorisClient)
    client.connection = Connection()
    client.should_cancel = lambda: client.connection.cancelled
    with pytest.raises(ds.datayes_sync.SyncCancelled):
        client._query("SELECT value FROM source")
    assert client.connection.open is False
    client.close()  # 再次清理不应把已取消任务变成失败。


def test_custom_prices_are_used_by_automatic_derived_features(config, tmp_path):
    config.mappings = {
        name: ds.TableMapping(table=name)
        for name in (
            "getEqu",
            "getTradeCal",
            "getMktEqud",
            "getMktEqudAdj",
        )
    }
    config.dataset_mappings = {"daily_forward": mapping()}

    class MixedClient(DirectClient):
        def fetch(self, endpoint, **params):
            if endpoint.endswith("getEqu"):
                return pd.DataFrame(
                    [
                        {
                            "secID": "600036.XSHG",
                            "ticker": "600036",
                            "secShortName": "test",
                            "listDate": "2000-01-01",
                        }
                    ]
                )
            if endpoint.endswith("getTradeCal"):
                return pd.DataFrame([{"calendarDate": "2024-01-02", "isOpen": 1}])
            if endpoint.endswith("getMktEqud"):
                return pd.DataFrame(
                    [
                        {
                            "secID": "600036.XSHG",
                            "tradeDate": "2024-01-02",
                            "closePrice": 10,
                            "PE": 8,
                        }
                    ]
                )
            pytest.fail("automatic sync must not overwrite the custom price mapping")

    client = MixedClient(config)
    client.close_value = 77
    root = tmp_path / "data"
    result = ds.sync_doris(
        ["daily_forward", "features_daily"],
        date(2024, 1, 2),
        date(2024, 1, 2),
        client=client,
        root=root,
    )
    assert result["errors"] == 0
    frame = pd.read_parquet(
        root / "6_ml_datasets/features_daily/dt=20240102/data.parquet"
    )
    assert frame["close"].tolist() == [77]


def test_source_selection_is_shared_by_hub_factor_reader_and_builder(
    tmp_path, monkeypatch
):
    from backend.shared.data_source_config import save_sources
    from backend.services.engine.data_platform.quantdb_factor_reader import (
        market_data_dir,
    )
    from backend.services.engine.qlib_data_builder import _resolve_cn_data_dir
    from backend.shared.qlib_paths import resolve_qlib_provider_uri

    monkeypatch.setenv("QM_DATA_SOURCE_CONFIG_FILE", str(tmp_path / "sources.json"))
    monkeypatch.setenv("QM_DORIS_DATA_DIR", str(tmp_path / "doris"))
    monkeypatch.delenv("QLIB_PROVIDER_URI", raising=False)
    save_sources("A", {"quantdb": False, "datayes": False, "doris": True})
    assert QuantDBDataHub().data_dir == (tmp_path / "doris").resolve()
    assert market_data_dir("CN") == (tmp_path / "doris").resolve()
    assert _resolve_cn_data_dir() == (tmp_path / "doris").resolve()
    assert resolve_qlib_provider_uri("CN") == str(
        (tmp_path / "doris/.qlib_cache/cn_data").resolve()
    )
