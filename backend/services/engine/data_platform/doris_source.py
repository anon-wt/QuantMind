"""Doris 中的 Datayes ODS 表 -> 独立本地 Parquet。

表名可配置；只执行元数据查询和 SELECT，复用 Datayes 字段、单位和落盘契约。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from collections import OrderedDict
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
from pydantic import BaseModel, Field, SecretStr, field_validator

from .datayes_catalog import (
    LOCAL_DATASETS as DATASETS,
    BY_ENDPOINT,
    BY_DATASET,
    EXTRA_DATASETS,
    dataset_type,
    preset_mappings,
    public_catalog,
    local_file_pattern as dataset_file_pattern,
)
from backend.shared.runtime_secrets import get_secret, set_secret
from backend.shared.stock_utils import StockCodeUtil

from . import datayes_sync

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PROJECT_ROOT = Path(__file__).resolve().parents[4]
_CONFIG_LOCK = threading.RLock()

# 不含路径前缀的接口名，同时作为可编辑表映射的键。
ENDPOINTS = {
    endpoint.rsplit("/", 1)[-1] for endpoint, _ in datayes_sync.ENDPOINTS.values()
} | {
    "getEquIndustry",
    "getFundETFCons",
    "getBondConv",
    "getBondConvPriceChg",
    "getCSIIdxWeightHS300",
    "getCSIIdxWeightZZ500",
    "getCSIIdxWeightZZ1000",
    "getMktIdxd",  # historical saved mapping; new configurations use getMktIdxdCsi
}
FACTOR_BINDINGS = {"l1_factors", "l1_l2_factors", "factor_dictionary"}
INTRADAY_DATASETS = {"min1_kline", "min5_kline", "tick_data"}
CUSTOM_DATASETS = {spec.dataset for spec in DATASETS} - INTRADAY_DATASETS
SUPPORTED = datayes_sync.SUPPORTED | FACTOR_BINDINGS
_EXTRA_REQUIRED = {
    "sector_concept": {"getEquIndustry"},
    "etf_pcf": {"getFundETFCons"},
    "convertible_bond": {"getBondConv", "getBondConvPriceChg"},
}
_DATE_COLUMNS = {
    "getTradeCal": "calendarDate",
    "getIdxCloseWeight": "effDate",
    "getMktAdjf": "exDivDate",
    "getEquShare": "publishDate",
    "getEquShareholderNum": "publishDate",
    "getFdmtBS": "publishDate",
    "getFdmtIS": "publishDate",
    "getFdmtCF": "publishDate",
    "getFdmtIndiPSPit": "publishDate",
    "getFdmtIndiPSTTMPIT": "publishDate",
}
_SNAPSHOTS = {
    "getEqu",
    "getSecTypeRel",
    "getEquIndustry",
    "getBondConv",
    "getBondConvPriceChg",
    "getSecHalt",
}


def identifier(value: str) -> str:
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError(
            "库名、表名和字段名只能包含字母、数字及下划线，且不能以数字开头"
        )
    return value


class TableMapping(BaseModel):
    table: str = Field(min_length=1, max_length=200)
    date_column: str | None = None
    date_format: str = "date"

    @field_validator("table")
    @classmethod
    def valid_table(cls, value):
        parts = value.split(".")
        if len(parts) > 2:
            raise ValueError("表名最多包含库名和表名")
        for part in parts:
            identifier(part)
        return value

    @field_validator("date_column")
    @classmethod
    def valid_column(cls, value):
        return identifier(value) if value else None

    @field_validator("date_format")
    @classmethod
    def valid_date_format(cls, value):
        if value not in {"date", "yyyymmdd"}:
            raise ValueError("日期格式必须是 date 或 yyyymmdd")
        return value


class DatasetMapping(TableMapping):
    # 本地标准字段 -> Doris 源字段；未映射的其他列按原名保留（如训练因子）。
    columns: dict[str, str] = Field(default_factory=dict)

    @field_validator("columns")
    @classmethod
    def valid_columns(cls, value):
        for target, source in value.items():
            identifier(target)
            identifier(source)
        if len(set(value.values())) != len(value):
            raise ValueError("同一源字段不能重复映射到多个标准字段")
        return value


class DorisConfigRequest(BaseModel):
    host: str = Field(min_length=1, max_length=253)
    port: int = Field(default=9030, ge=1, le=65535)
    database: str = Field(min_length=1, max_length=128)
    user: str = Field(min_length=1, max_length=128)
    password: SecretStr | None = None
    table_prefix: str = "ods_datayes_"
    lookback_days: int = Field(default=7, ge=1, le=365)
    mappings: dict[str, TableMapping] = Field(default_factory=dict)
    dataset_mappings: dict[str, DatasetMapping] = Field(default_factory=dict)

    @field_validator("host", "user")
    @classmethod
    def valid_connection_text(cls, value):
        value = value.strip()
        if (
            not value
            or any(c.isspace() for c in value)
            or any(c in value for c in "/@?\\")
        ):
            raise ValueError("请输入有效的主机地址或用户名")
        return value

    @field_validator("database")
    @classmethod
    def valid_database(cls, value):
        return identifier(value.strip())

    @field_validator("table_prefix")
    @classmethod
    def valid_prefix(cls, value):
        if value:
            identifier(value)
        return value

    @field_validator("mappings")
    @classmethod
    def valid_mappings(cls, value):
        if set(value) - (ENDPOINTS | FACTOR_BINDINGS):
            raise ValueError("包含未知接口或因子数据集映射")
        return value

    @field_validator("dataset_mappings")
    @classmethod
    def valid_dataset_mappings(cls, value):
        if set(value) - CUSTOM_DATASETS:
            raise ValueError("包含未知数据集，或暂不支持的分钟/逐笔数据集")
        return value


def config_path() -> Path:
    return Path(
        os.getenv("QM_DORIS_CONFIG_FILE") or _PROJECT_ROOT / "config/doris_source.json"
    )


def data_root() -> Path:
    from .modelscope_dataset_sync import resolve_data_root

    official = resolve_data_root().resolve()
    root = Path(os.getenv("QM_DORIS_DATA_DIR") or official.parent / "doris").resolve()
    datayes = datayes_sync.data_root().resolve()
    for other in (official, datayes):
        if root == other or root.is_relative_to(other) or other.is_relative_to(root):
            raise ValueError("Doris 数据目录必须与 QuantDB、Datayes 目录独立")
    return root


def load_config() -> DorisConfigRequest | None:
    path = config_path()
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    for key, env in (
        ("host", "DORIS_FE_HOST"),
        ("port", "DORIS_FE_QUERY_PORT"),
        ("user", "DORIS_USER"),
        ("database", "DORIS_DATABASE"),
    ):
        payload[key] = get_secret(env) or payload.get(key)
    # 密码从不写入映射文件或读取接口响应。
    payload.pop("password", None)
    return DorisConfigRequest.model_validate(payload)


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{threading.get_ident()}.tmp")
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def save_config(config: DorisConfigRequest) -> None:
    with _CONFIG_LOCK:
        if config.password is not None:
            password = config.password.get_secret_value()
            if "\n" in password or "\r" in password or len(password) > 4096:
                raise ValueError("密码不能包含换行且长度不能超过 4096")
            set_secret("DORIS_PASSWORD", password)
        for key, env in (
            ("host", "DORIS_FE_HOST"),
            ("port", "DORIS_FE_QUERY_PORT"),
            ("user", "DORIS_USER"),
            ("database", "DORIS_DATABASE"),
        ):
            set_secret(env, str(getattr(config, key)))
        _write_json(config_path(), config.model_dump(exclude={"password"}))


def configured() -> bool:
    try:
        return load_config() is not None
    except (ValueError, OSError):
        return False


def supported_datasets(config: DorisConfigRequest | None = None) -> set[str]:
    config = config or load_config()
    if config is None:
        return set()
    available = set(config.mappings)
    if "getMktIdxd" in available:
        available.add("getMktIdxdCsi")
    basics = {"getEqu", "getTradeCal"}
    result = (
        {
            dataset
            for dataset, (endpoint, _) in datayes_sync.ENDPOINTS.items()
            if {endpoint.rsplit("/", 1)[-1]} | _EXTRA_REQUIRED.get(dataset, set())
            <= available
        }
        if basics <= available
        else set()
    )
    if {"daily_forward", "valuation"} <= result:
        result.update(datayes_sync.DERIVED)
    if "daily_backward" in result:
        result.update(FACTOR_BINDINGS & available)
    result.update(config.dataset_mappings)
    return result


def plan_datasets(datasets, config: DorisConfigRequest | None = None):
    config = config or load_config()
    if config is None:
        return []
    direct = set(config.dataset_mappings)
    ordinary = [
        name
        for name in datasets
        if name in datayes_sync.SUPPORTED and name not in direct
    ]
    if (FACTOR_BINDINGS & set(datasets)) - direct:
        ordinary.append("daily_backward")
    selected = set(datasets) | (
        set(datayes_sync.plan_datasets(ordinary)) if ordinary else set()
    )
    for name in FACTOR_BINDINGS & set(datasets) & direct:
        if not {"open", "high", "low", "close", "volume", "amount"} <= set(
            config.dataset_mappings[name].columns
        ):
            if "daily_backward" in supported_datasets(config):
                selected.add("daily_backward")
    return [spec.dataset for spec in DATASETS if spec.dataset in selected]


def public_config() -> dict:
    config = load_config()
    catalog = public_catalog()
    native_rows = {row["dataset"]: row for row in catalog}
    return {
        "configured": config is not None,
        "connection": config.model_dump(exclude={"password"}) if config else None,
        "password_configured": bool(get_secret("DORIS_PASSWORD")),
        "root": str(data_root()),
        "supported_datasets": sorted(supported_datasets(config)),
        "endpoints": sorted(ENDPOINTS),
        "factor_datasets": sorted(FACTOR_BINDINGS),
        "source_catalog": catalog,
        "preset_mappings": preset_mappings(),
        "datasets": [
            {
                "dataset": spec.dataset,
                "name": spec.name,
                "path": local_file_pattern(spec),
                "data_type": dataset_type(spec)[0],
                "data_type_name": dataset_type(spec)[1],
                **native_rows.get(spec.dataset, {}),
            }
            for spec in DATASETS
            if spec.dataset in CUSTOM_DATASETS
        ],
    }


class DorisClient:
    """用只读 SQL 实现既有 Datayes 取数契约，不接收任意 SQL。"""

    supports_interval_history = True

    def __init__(
        self, config: DorisConfigRequest | None = None, should_cancel=lambda: False
    ):
        self.config = config or load_config()
        if self.config is None:
            raise ValueError("请先到个人中心 → 数据平台配置 Doris 连接")
        self.should_cancel = should_cancel
        self._schemas: dict[str, dict[str, str]] = {}
        self._cache: OrderedDict = OrderedDict()
        self.skipped_stock_rows = 0
        try:
            import pymysql

            password = (
                self.config.password.get_secret_value()
                if self.config.password is not None
                else get_secret("DORIS_PASSWORD")
            )
            self.connection = pymysql.connect(
                host=self.config.host,
                port=self.config.port,
                user=self.config.user,
                password=password,
                database=self.config.database,
                charset="utf8mb4",
                connect_timeout=8,
                read_timeout=30,
                write_timeout=15,
                cursorclass=pymysql.cursors.SSDictCursor,
            )
        except ImportError:
            raise RuntimeError("Doris 驱动未安装，请更新后端依赖 PyMySQL") from None
        except Exception:
            raise RuntimeError(
                "Doris 连接失败，请检查主机、端口、账号权限及服务器网络"
            ) from None

    def close(self):
        if self.connection.open:
            self.connection.close()

    def _query(self, sql, params=()) -> pd.DataFrame:
        if self.should_cancel():
            raise datayes_sync.SyncCancelled()
        try:
            cursor = self.connection.cursor()
            try:
                cursor.execute(sql, params)
                pieces = []
                while True:
                    if self.should_cancel():
                        raise datayes_sync.SyncCancelled()
                    rows = cursor.fetchmany(5000)
                    if not rows:
                        break
                    pieces.append(pd.DataFrame(rows))
                return (
                    pd.concat(pieces, ignore_index=True) if pieces else pd.DataFrame()
                )
            finally:
                if self.should_cancel():
                    # 流式游标 close 会排空剩余结果；取消时直接关闭只读连接。
                    self.close()
                else:
                    cursor.close()
        except datayes_sync.SyncCancelled:
            raise
        except Exception:
            # 不回传驱动异常，避免连接凭据或内部 SQL 混入日志、作业结果。
            raise RuntimeError("Doris 查询失败，请检查映射表、字段和读取权限") from None

    def _table_sql(self, mapping: TableMapping) -> str:
        return ".".join(f"`{identifier(part)}`" for part in mapping.table.split("."))

    def schema(self, mapping: TableMapping) -> dict[str, str]:
        if mapping.table not in self._schemas:
            rows = self._query(f"DESCRIBE {self._table_sql(mapping)}")
            if rows.empty:
                raise ValueError(f"Doris 表 {mapping.table} 没有可读取字段")
            self._schemas[mapping.table] = dict(
                zip(rows.iloc[:, 0], rows.iloc[:, 1], strict=True)
            )
        return self._schemas[mapping.table]

    def discover(self) -> dict:
        frame = self._query("SHOW TABLES")
        tables = frame.iloc[:, 0].tolist() if not frame.empty else []
        names = {name.lower(): name for name in tables}
        mappings = dict(self.config.mappings)
        for endpoint in ENDPOINTS:
            if endpoint in mappings:
                continue
            for prefix in dict.fromkeys(
                (self.config.table_prefix, "hunter_ods_datayes_", "ods_datayes_")
            ):
                table = names.get((prefix + endpoint).lower())
                if table:
                    native = BY_ENDPOINT.get(endpoint)
                    mappings[endpoint] = TableMapping(
                        table=table,
                        **(
                            {
                                "date_column": native.date_column,
                                "date_format": native.date_format,
                            }
                            if native
                            else {}
                        ),
                    )
                    break
        factor_candidates = sorted(
            name
            for name in tables
            if "backup" not in name.lower()
            and "factor" in name.lower()
            and not name.lower().endswith("info")
        )
        return {
            "mappings": {key: value.model_dump() for key, value in mappings.items()},
            "table_count": len(tables),
            "factor_candidates": factor_candidates,
        }

    def fetch(self, endpoint, **params) -> pd.DataFrame:
        name = endpoint.rsplit("/", 1)[-1]
        mapping = self.config.mappings.get(name)
        if mapping is None and name == "getMktIdxdCsi":
            mapping = self.config.mappings.get("getMktIdxd")
        if mapping is None:
            if name.startswith("getCSIIdxWeight"):
                return pd.DataFrame()  # 仅指数权重接口的可选备用表。
            raise ValueError(f"Doris 尚未配置 {name} 对应表")
        schema = self.schema(mapping)
        conditions, values = [], []
        table = self._table_sql(mapping)
        if name in _SNAPSHOTS and "dt" in schema:
            conditions.append(f"`dt` = (SELECT MAX(`dt`) FROM {table})")
        for key in (
            "secID",
            "ticker",
            "exchangeCD",
            "equTypeCD",
            "listStatusCD",
            "industryVersionCD",
        ):
            if params.get(key):
                if key not in schema:
                    raise ValueError(f"Doris 表 {mapping.table} 缺少 {key} 字段")
                members = str(params[key]).split(",")
                conditions.append(f"`{key}` IN ({','.join(['%s'] * len(members))})")
                values.extend(members)
        start = (
            params.get("_window_start")
            or params.get("tradeDate")
            or params.get("beginDate")
            or params.get("beginPubDate")
            or params.get("publishDateBegin")
            or params.get("beginPublishDate")
            or params.get("beginNewPubDate")
        )
        end = (
            params.get("_window_end")
            or params.get("tradeDate")
            or params.get("endDate")
            or params.get("endPubDate")
            or params.get("publishDateEnd")
            or params.get("endPublishDate")
            or params.get("endNewPubDate")
        )
        base_conditions = list(conditions)
        native = BY_ENDPOINT.get(name)
        if start or end:
            column = (
                mapping.date_column
                or (native.date_column if native else None)
                or _DATE_COLUMNS.get(name, "tradeDate")
            )
            if column not in schema:
                raise ValueError(f"Doris 表 {mapping.table} 缺少日期字段 {column}")
        if native and native.interval_end and start and end:
            if native.interval_end not in schema:
                raise ValueError(
                    f"Doris 表 {mapping.table} 缺少区间结束字段 {native.interval_end}"
                )
            conditions.append(f"`{identifier(column)}` < %s")
            values.append(
                (
                    pd.to_datetime(str(end), format="%Y%m%d").date() + timedelta(days=1)
                ).isoformat()
            )
            conditions.append(
                f"(`{identifier(native.interval_end)}` >= %s OR `{identifier(native.interval_end)}` IS NULL OR `{identifier(native.interval_end)}` = '')"
            )
            values.append(
                pd.to_datetime(str(start), format="%Y%m%d").date().isoformat()
            )
            start = end = None
        for operator, bound in ((">=", start), ("<", end)):
            if bound:
                parsed = pd.to_datetime(str(bound), format="%Y%m%d").date()
                if operator == "<":
                    parsed += timedelta(days=1)
                value = (
                    parsed.strftime("%Y%m%d")
                    if mapping.date_format == "yyyymmdd"
                    else parsed.isoformat()
                )
                conditions.append(f"`{identifier(column)}` {operator} %s")
                values.append(value)
        key = (name, tuple(conditions), tuple(values))
        if key in self._cache:
            return self._cache[key].copy()
        sql = f"SELECT * FROM {table}" + (
            " WHERE " + " AND ".join(conditions) if conditions else ""
        )
        if (
            name == "getIdxConsCsi"
            and {"secID", "consID", "intoDate", "updateTime", "dt"} <= schema.keys()
        ):
            order = "`updateTime` DESC, `dt` DESC"
            sql = (
                f"SELECT * FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY `secID`,`consID`,`intoDate` ORDER BY {order}) AS `_qm_rank` FROM {table}"
                + (" WHERE " + " AND ".join(base_conditions) if base_conditions else "")
                + ") AS `history` WHERE `_qm_rank`=1"
                + (
                    " AND " + " AND ".join(conditions[len(base_conditions) :])
                    if len(conditions) > len(base_conditions)
                    else ""
                )
            )
        result = self._query(sql, tuple(values))
        result = result.drop(columns=["_qm_rank"], errors="ignore")
        if "dt" in result:
            result = result.sort_values("dt", kind="stable")
        if name == "getEqu" and not result.empty:
            valid = result.apply(
                lambda row: bool(
                    StockCodeUtil.from_datayes(
                        row.get("secID", ""), row.get("exchangeCD", "")
                    )
                    or StockCodeUtil.from_datayes(
                        row.get("ticker", ""), row.get("exchangeCD", "")
                    )
                ),
                axis=1,
            )
            self.skipped_stock_rows += int((~valid).sum())
            result = result[valid].copy()
        if result.memory_usage(deep=True).sum() < 8 * 1024 * 1024:
            self._cache[key] = result
        while len(self._cache) > 3:
            self._cache.popitem(last=False)
        return result.copy()

    def fetch_dataset(
        self, dataset: str, mapping: DatasetMapping, start: date, end: date
    ):
        schema = self.schema(mapping)
        for source in mapping.columns.values():
            if source not in schema:
                raise ValueError(f"Doris 表 {mapping.table} 缺少映射字段 {source}")
        spec = next(spec for spec in DATASETS if spec.dataset == dataset)
        native = BY_DATASET.get(dataset)
        column = (
            mapping.date_column
            or mapping.columns.get("time")
            or (mapping.columns.get("halt_begin") if dataset == "stock_halts" else None)
            or (native.date_column if native and native.date_column in schema else None)
            or next(
                (
                    name
                    for name in (
                        "tradeDate",
                        "calendarDate",
                        "publishDate",
                        "time",
                        "date",
                        "dt",
                    )
                    if name in schema
                ),
                None,
            )
        )
        table = self._table_sql(mapping)
        values = ()
        sql = f"SELECT * FROM {table}"
        if dataset == "instrument_detail" and "dt" in schema:
            sql += f" WHERE `dt` = (SELECT MAX(`dt`) FROM {table})"
        elif column and native and native.interval_end:
            ending = mapping.columns.get(
                "halt_end" if dataset == "stock_halts" else "effective_to",
                native.interval_end,
            )
            if column not in schema or ending not in schema:
                raise ValueError("历史区间数据需要映射开始和结束字段")
            sql += f" WHERE `{identifier(column)}` < %s AND (`{identifier(ending)}` >= %s OR `{identifier(ending)}` IS NULL)"
            values = ((end + timedelta(days=1)).isoformat(), start.isoformat())
        elif column:
            if column not in schema:
                raise ValueError(f"Doris 表 {mapping.table} 缺少日期字段 {column}")
            sql += (
                f" WHERE `{identifier(column)}` >= %s AND `{identifier(column)}` < %s"
            )
            fmt = "%Y%m%d" if mapping.date_format == "yyyymmdd" else "%Y-%m-%d"
            values = (start.strftime(fmt), (end + timedelta(days=1)).strftime(fmt))
        elif spec.layout != "single":
            raise ValueError("请配置日期字段，禁止无日期范围读取历史明细")
        frame = self._query(sql, values)
        if frame.empty:
            return frame
        # 仅选择被映射的目标列和未被重命名的列，禁止产生重名标准字段。
        renamed = {source: target for target, source in mapping.columns.items()}
        overwritten = set(mapping.columns) - set(renamed)
        frame = frame.drop(
            columns=[name for name in overwritten if name in frame]
        ).rename(columns=renamed)
        if column and "time" not in frame:
            mapped_column = renamed.get(column, column)
            if mapped_column in frame:
                frame["time"] = frame[mapped_column]
        return normalize_standard(frame, dataset)


def local_file_pattern(spec) -> str:
    return dataset_file_pattern(spec)


def normalize_standard(frame: pd.DataFrame, dataset: str) -> pd.DataFrame:
    frame = frame.copy()
    if dataset in {spec.dataset for spec in EXTRA_DATASETS}:
        # Restore vendor aliases for the shared native normalizer while keeping
        # the user's canonical mappings and all unmapped factor columns.
        native = BY_DATASET[dataset]
        aliases = {
            native.date_column: "time",
            "outDate": "effective_to",
            "industryID1": "industry_code",
            "industryName1": "industry_name",
            "industryVersionCD": "industry_version",
            "STflg": "st_flag",
            "haltBeginTime": "halt_begin",
            "haltEndTime": "halt_end",
            "perCashDiv": "cash_dividend_per_share",
            "perShareDivRatio": "bonus_share_ratio",
            "perShareTransRatio": "transfer_share_ratio",
            "endDate": "m_timetag",
        }
        for original, canonical in aliases.items():
            if original and original not in frame and canonical in frame:
                frame[original] = frame[canonical]
        if dataset == "sw_l1_industry" and "industryVersionCD" not in frame:
            frame["industryVersionCD"] = "010321"
        result = datayes_sync.normalize(frame, dataset)
        result["data_source"] = "doris"
        return result
    if "symbol" in frame:
        frame["symbol"] = frame["symbol"].map(
            lambda value: (
                StockCodeUtil.from_datayes(value) or StockCodeUtil.normalize(value)
            )
        )
        if not frame["symbol"].astype(str).str.fullmatch(r"\d{6}\.(SH|SZ|BJ)").all():
            raise ValueError("股票代码无法转换为 A 股标准后缀格式，请检查字段映射")
    elif dataset not in {"trading_calendar", "market_sentiment"}:
        raise ValueError("请将 Doris 股票代码字段映射到 symbol")
    if "time" in frame:
        # 避免将整数 YYYYMMDD 解释为 Unix 纳秒。
        text = frame["time"].astype(str)
        frame["time"] = pd.to_datetime(
            text, errors="coerce", format="mixed"
        ).dt.normalize()
        if frame["time"].isna().any():
            raise ValueError("日期无法解析，请检查 time 字段映射")
    elif dataset != "instrument_detail":
        raise ValueError("请将 Doris 日期字段映射到 time")
    if dataset in {
        "daily_unadjusted",
        "daily_forward",
        "daily_backward",
        "index_daily",
    }:
        fields = ["open", "high", "low", "close", "volume", "amount"]
        if set(fields) - set(frame):
            raise ValueError("日线需要 open/high/low/close/volume/amount 字段映射")
        frame[fields] = frame[fields].apply(pd.to_numeric, errors="coerce")
        if frame[fields].isna().any().any():
            raise ValueError("日线数值字段包含空值或非法值")
    if dataset == "instrument_detail":
        if "list_date" not in frame:
            raise ValueError("股票资料必须将上市日期映射到 list_date")
        frame["J_start"] = pd.to_datetime(
            frame["list_date"], errors="coerce"
        ).dt.strftime("%Y%m%d")
        if "name" in frame:
            frame["Name"] = frame["name"]
    if dataset in FACTOR_BINDINGS:
        frame["date"] = frame["time"].dt.strftime("%Y-%m-%d")
    if dataset in {"balance", "income", "cashflow", "pershare_index", "per_share"}:
        if {"m_anntime", "m_timetag"} - set(frame):
            raise ValueError("财务数据必须映射披露日期 m_anntime 和报告期 m_timetag")
    frame["data_source"] = "doris"
    return frame


@contextmanager
def _sync_lock(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".doris_sync.lock").open("a+b") as handle:
        if os.name == "nt":
            import msvcrt

            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise ValueError("已有 Doris 同步任务正在执行") from None
        else:
            import fcntl

            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise ValueError("已有 Doris 同步任务正在执行") from None
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def _signature(config: DorisConfigRequest) -> str:
    value = config.model_dump(exclude={"password"})
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _sync_doris(
    datasets,
    start: date,
    end: date,
    *,
    incremental=True,
    progress=None,
    should_cancel=lambda: False,
    client=None,
    root: Path | None = None,
):
    root = root or data_root()
    own_client = client is None
    client = client or DorisClient(should_cancel=should_cancel)
    try:
        if start > end:
            raise ValueError("同步开始日期不能晚于结束日期")
        available = supported_datasets(client.config)
        if not datasets:
            raise ValueError("尚未配置可同步的 Doris 数据集，请先保存表映射")
        if set(datasets) - available:
            raise ValueError(
                "Doris 缺少所选数据集的表映射，请先在个人中心发现并保存映射"
            )
        state_path = root / ".doris_sync_state.json"
        state = json.loads(state_path.read_text()) if state_path.is_file() else {}
        signature = _signature(client.config)
        checkpoints = (
            state.get("datasets", {}) if state.get("signature") == signature else {}
        )
        selected = plan_datasets(list(dict.fromkeys(datasets)), client.config)
        starts = [
            date.fromisoformat(checkpoints[name])
            - timedelta(days=client.config.lookback_days - 1)
            if name in checkpoints
            else start
            for name in selected
        ]
        effective_start = max(start, min(starts)) if incremental and starts else start
        if effective_start > end:
            items = [
                {"dataset": name, "status": "up_to_date", "rows": 0}
                for name in selected
            ]
            if progress:
                for item in items:
                    progress("dataset_done", **item)
            return {
                "root": str(root),
                "datasets": items,
                "errors": 0,
                "cancelled": False,
            }

        def on_progress(event, **details):
            if event == "dataset_done" and details.get("status") in {
                "completed",
                "empty",
            }:
                checkpoints[details["dataset"]] = max(
                    end.isoformat(), checkpoints.get(details["dataset"], "")
                )
                _write_json(
                    state_path, {"signature": signature, "datasets": checkpoints}
                )
            if progress:
                progress(event, **details)

        direct = [name for name in selected if name in client.config.dataset_mappings]
        ordinary = [
            name
            for name in selected
            if name in datayes_sync.SUPPORTED and name not in direct
        ]
        if FACTOR_BINDINGS.intersection(selected) - set(direct):
            ordinary = list(dict.fromkeys([*ordinary, "daily_backward"]))
        result = {"root": str(root), "datasets": [], "errors": 0, "cancelled": False}

        def run_direct(names):
            for dataset in names:
                try:
                    item = _sync_direct_dataset(
                        dataset,
                        client,
                        root,
                        effective_start,
                        end,
                        progress,
                        should_cancel,
                    )
                except datayes_sync.SyncCancelled:
                    result["cancelled"] = True
                    return
                result["datasets"].append(item)
                result["errors"] += item["status"] == "failed"
                on_progress("dataset_done", **item)

        # 自定义行情/估值先落盘，再执行依赖它们的技术派生；自动映射不能覆盖自定义结果。
        run_direct([name for name in direct if name not in FACTOR_BINDINGS])
        if ordinary and not result["cancelled"] and not result["errors"]:
            automatic = datayes_sync.sync_datayes(
                ordinary,
                effective_start,
                end,
                progress=on_progress,
                should_cancel=should_cancel,
                client=client,
                root=root,
                source_name="doris",
                local_datasets=set(direct),
            )
            result["datasets"].extend(automatic["datasets"])
            result["errors"] += automatic["errors"]
            result["cancelled"] = automatic["cancelled"]
        if not result["cancelled"] and not result["errors"]:
            run_direct([name for name in direct if name in FACTOR_BINDINGS])
        if not result["cancelled"] and not result["errors"]:
            for dataset in selected:
                if dataset not in FACTOR_BINDINGS or dataset in direct:
                    continue
                try:
                    rows = 0
                    for begin, stop in datayes_sync._windows(effective_start, end, 5):
                        raw = client.fetch(dataset, beginDate=begin, endDate=stop)
                        frame = datayes_sync.normalize(raw, dataset)
                        if not frame.empty:
                            frame["data_source"] = "doris"
                            frame["date"] = frame["time"].dt.strftime("%Y-%m-%d")
                            frame = _enrich_factors(
                                root,
                                frame,
                                pd.Timestamp(begin).date(),
                                pd.Timestamp(stop).date(),
                            )
                            rows += datayes_sync.write_frame(root, dataset, frame)
                    item = {
                        "dataset": dataset,
                        "status": "completed" if rows else "empty",
                        "rows": rows,
                    }
                except datayes_sync.SyncCancelled:
                    result["cancelled"] = True
                    break
                except Exception as exc:
                    item = {
                        "dataset": dataset,
                        "status": "failed",
                        "rows": 0,
                        "error": str(exc)[:300],
                    }
                    result["errors"] += 1
                result["datasets"].append(item)
                on_progress("dataset_done", **item)
        result["requested_start"] = start.isoformat()
        result["effective_start"] = effective_start.isoformat()
        result["incremental"] = incremental
        result["skipped_stock_rows"] = getattr(client, "skipped_stock_rows", 0)
        return result
    finally:
        if own_client:
            client.close()


def sync_doris(datasets, start: date, end: date, **kwargs):
    root = kwargs.pop("root", None) or data_root()
    with _sync_lock(root):
        return _sync_doris(datasets, start, end, root=root, **kwargs)


def _sync_direct_dataset(dataset, client, root, start, end, progress, cancelled):
    rows = 0
    try:
        if cancelled():
            raise datayes_sync.SyncCancelled()
        if progress:
            progress("dataset_start", dataset=dataset, message=f"Doris：{dataset}")
        windows = (
            [(start, end)]
            if dataset == "instrument_detail"
            else [
                (pd.Timestamp(begin).date(), pd.Timestamp(stop).date())
                for begin, stop in datayes_sync._windows(start, end, 5)
            ]
        )
        for begin, stop in windows:
            if cancelled():
                raise datayes_sync.SyncCancelled()
            frame = client.fetch_dataset(
                dataset, client.config.dataset_mappings[dataset], begin, stop
            )
            if dataset in FACTOR_BINDINGS and not frame.empty:
                frame = _enrich_factors(root, frame, begin, stop)
            rows += datayes_sync.write_frame(root, dataset, frame)
            if progress:
                progress("batch", dataset=dataset, rows=rows)
        return {
            "dataset": dataset,
            "status": "completed" if rows else "empty",
            "rows": rows,
        }
    except datayes_sync.SyncCancelled:
        raise
    except Exception as exc:
        return {
            "dataset": dataset,
            "status": "failed",
            "rows": rows,
            "error": str(exc)[:300],
        }


def _enrich_factors(root, frame, start, end):
    from .quantdb_hub import QuantDBDataHub

    fields = ["open", "high", "low", "close", "volume", "amount"]
    if not set(fields) <= set(frame):
        bars = QuantDBDataHub(root).fetch_daily_kline_batch(
            frame["symbol"].unique().tolist(),
            start,
            end,
            adjust="hfq",
        )
        if bars.empty:
            raise ValueError("Doris 因子训练需要同日后复权行情，请检查行情覆盖")
        bars["time"] = pd.to_datetime(bars["trade_date"])
        frame = frame.drop(columns=[name for name in fields if name in frame]).merge(
            bars[["symbol", "time", *fields]],
            on=["symbol", "time"],
            how="left",
            validate="many_to_one",
        )
    frame[fields] = frame[fields].apply(pd.to_numeric, errors="coerce")
    if frame[fields].isna().any().any():
        raise ValueError("Doris 因子与行情覆盖不一致，未写入不完整训练数据")
    return frame
