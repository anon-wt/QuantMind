"""One Datayes API / Doris ODS / local dataset and classification catalogue.

Local extensions do not change the authenticated/public QuantDB cloud catalogue.
"""

from dataclasses import dataclass

from backend.shared.quantdb_datasets import DATASETS, GROUPS, DatasetSpec


DATA_TYPES = (
    ("market", "行情数据"),
    ("factor", "因子数据"),
    ("risk", "风险暴露"),
    ("reference", "基础资料"),
    ("status", "交易状态"),
    ("corporate", "公司行为"),
)

EXTRA_DATASETS = (
    DatasetSpec(
        "stock_factors_pro",
        "专业因子库",
        "6",
        "factor",
        "6_ml_datasets/stock_factors_pro",
        "partition",
        "通联专业因子原始宽表，保留原因子名",
    ),
    DatasetSpec(
        "cne6_exposure",
        "CNE6 风格暴露",
        "7",
        "risk",
        "7_risk_data/cne6_exposure",
        "partition",
        "CNE6 SW21 Trading Model 风格及行业暴露",
    ),
    DatasetSpec(
        "index_constituents",
        "中证1000历史成分",
        "2",
        "reference",
        "2_base_sector/index_constituents",
        "symbol",
        "000852.SH 成分生效/失效区间，不冒充指数权重",
    ),
    DatasetSpec(
        "sw_l1_industry",
        "申万一级行业",
        "2",
        "reference",
        "2_base_sector/sw_l1_industry",
        "symbol",
        "申万2021一级行业及历史生效区间",
    ),
    DatasetSpec(
        "stock_halts",
        "停复牌",
        "2",
        "status",
        "2_base_sector/stock_halts",
        "symbol",
        "停复牌历史区间；包含同步开始前仍在停牌的记录",
    ),
    DatasetSpec(
        "stock_st",
        "ST状态",
        "2",
        "status",
        "2_base_sector/stock_st",
        "partition",
        "按交易日保存通联 ST 标记，不用当前状态覆盖历史",
    ),
    DatasetSpec(
        "equity_dividends",
        "分红送转",
        "3",
        "corporate",
        "3_financial_data/equity_dividends",
        "symbol",
        "按最新公告日同步；保留方案阶段、登记/除权/支付日期",
    ),
)
LOCAL_DATASETS = DATASETS + EXTRA_DATASETS
_BY_NAME = {spec.dataset: spec for spec in LOCAL_DATASETS}


@dataclass(frozen=True)
class SourceDataset:
    dataset: str
    endpoint: str
    mode: str
    data_type: str
    date_column: str | None = "tradeDate"
    date_format: str = "date"
    interval_end: str | None = None

    @property
    def table(self):
        return "ods_datayes_" + self.endpoint.rsplit("/", 1)[-1].lower()


CATALOG = (
    SourceDataset("daily_unadjusted", "market/getMktEqud", "day", "market"),
    SourceDataset("daily_backward", "market/getMktEqudAdjAf", "day", "market"),
    SourceDataset("index_daily", "market/getMktIdxdCsi", "index", "market"),
    SourceDataset(
        "stock_factors_pro", "market/getMktStockFactorsDateRangePro", "batch", "factor"
    ),
    SourceDataset(
        "cne6_exposure",
        "equity/getDy1dExposureCNE6SW21",
        "batch",
        "risk",
        date_format="yyyymmdd",
    ),
    SourceDataset(
        "trading_calendar",
        "master/getTradeCal",
        "calendar",
        "reference",
        "calendarDate",
    ),
    SourceDataset("instrument_detail", "equity/getEqu", "stocks", "reference", None),
    SourceDataset(
        "index_constituents",
        "idx/getIdxConsCsi",
        "constituents",
        "reference",
        "intoDate",
        interval_end="outDate",
    ),
    SourceDataset(
        "sw_l1_industry",
        "equity/getEquIndustry",
        "industry_history",
        "reference",
        "intoDate",
        interval_end="outDate",
    ),
    SourceDataset(
        "stock_halts",
        "master/getSecHalt",
        "halt_history",
        "status",
        "haltBeginTime",
        interval_end="haltEndTime",
    ),
    SourceDataset("stock_st", "equity/getSecST", "batch", "status"),
    SourceDataset(
        "equity_dividends", "equity/getEquDiv", "dividends", "corporate", "newPubDate"
    ),
)
BY_DATASET = {row.dataset: row for row in CATALOG}
BY_ENDPOINT = {row.endpoint.rsplit("/", 1)[-1]: row for row in CATALOG}
TYPE_NAMES = dict(DATA_TYPES)


def get_local_spec(dataset):
    return _BY_NAME[dataset]


def dataset_type(spec):
    row = BY_DATASET.get(spec.dataset)
    group = row.data_type if row else spec.group
    names = {g["id"]: g["name"] for g in GROUPS}
    return group, TYPE_NAMES.get(group, names.get(group, group))


def local_file_pattern(spec):
    if spec.layout == "partition":
        filename = "dt={YYYYMMDD}/data.parquet"
    elif spec.layout == "symbol":
        filename = "{symbol}.parquet"
    else:
        filename = {
            "instrument_detail": "instrument_list",
            "sector_concept": "sector_members",
        }.get(spec.dataset, spec.dataset) + ".parquet"
    return f"{spec.rel_dir}/{filename}"


def public_catalog():
    return [
        {
            "dataset": row.dataset,
            "name": get_local_spec(row.dataset).name,
            "data_type": row.data_type,
            "data_type_name": TYPE_NAMES[row.data_type],
            "endpoint": "/" + row.endpoint + ".json",
            "endpoint_key": row.endpoint.rsplit("/", 1)[-1],
            "path": local_file_pattern(get_local_spec(row.dataset)),
            "doris_table": row.table,
            "date_column": row.date_column,
            "date_format": row.date_format,
            "note": {"instrument_detail": "通联股票基础资料，保留上市及退市日期"}.get(
                row.dataset, get_local_spec(row.dataset).note
            ),
        }
        for row in CATALOG
    ]


def preset_mappings():
    return {
        row.endpoint.rsplit("/", 1)[-1]: {
            "table": row.table,
            "date_column": row.date_column,
            "date_format": row.date_format,
        }
        for row in CATALOG
    }
