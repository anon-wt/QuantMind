# A 股引导初始化

管理后台 → 数据平台 → A 股 → 引导初始化中提供 QuantDB / Datayes / Doris 勾选项。「本地落盘」保留初始化按钮与 QuantDB 本地扫描。个人中心 → 数据平台提供对应凭据及映射配置。

`/api/v1/admin/data-platform/a-share/initialization` 返回各源初始化覆盖、本地状态及范围说明；弹窗只展示所选源支持的数据集，多源取并集并逐源执行。`POST /initialization/jobs` 按请求中已启用的数据源分发后台任务，`GET /initialization/jobs/{id}` 查询结果，`POST /initialization/jobs/{id}/cancel` 协作取消。请求包含 `sources`、`datasets`、`start_date`、`end_date`。日期范围约束 Datayes 和 Doris，默认 2016-01-01 至今天。

QuantDB 公开初始化支持 26 类日频、基础、财务及因子数据，不含分钟线和逐笔；完整 SDK 目录仍保留 29 类。公开初始化枚举完整仓库，不按日期裁剪；本地大小与已登记 SHA256 均一致时跳过，只下载缺失或变化的文件，并保留原有因子基线发布。页面结果分别显示下载与跳过文件数。分钟/逐笔等 SDK 增量同步需要 QuantDB Key、网络及对应权限/流量，不能通过公开初始化拉取。

## Datayes 覆盖

共有 29 个可映射数据集。接口及字段依据通联接口定义和实际返回验证，每个接口的查询日期和证券参数按其公开定义传入。数据权限、实际字段及历史长度以 Token 的商城订阅为准；无权限与网络失败会返回任务错误，不用空数据或零值代替。

### 本次标准接入与分类

`datayes_catalog.py` 是下列 12 项的统一定义，供两种适配器、初始化目录、个人中心和本地预览共用。
本地新增数据集不扩充 QuantDB 云端目录，也不把通联因子冒充 QuantDB 专有因子。

| 数据类型 | 数据 | 通联接口 | 本地数据集 |
| --- | --- | --- | --- |
| 行情数据 | 不复权日行情 | market/getMktEqud | daily_unadjusted |
| 行情数据 | 后复权日行情 | market/getMktEqudAdjAf | daily_backward |
| 行情数据 | 中证指数日行情 | market/getMktIdxdCsi | index_daily |
| 因子数据 | 专业因子库 | market/getMktStockFactorsDateRangePro | stock_factors_pro |
| 风险暴露 | CNE6 SW21 | equity/getDy1dExposureCNE6SW21 | cne6_exposure |
| 基础资料 | 交易日历 | master/getTradeCal | trading_calendar |
| 基础资料 | 股票基础信息 | equity/getEqu | instrument_detail |
| 基础资料 | 中证1000历史成分 | idx/getIdxConsCsi | index_constituents |
| 基础资料 | 申万2021一级行业 | equity/getEquIndustry | sw_l1_industry |
| 交易状态 | 停复牌 | master/getSecHalt | stock_halts |
| 交易状态 | ST 状态 | equity/getSecST | stock_st |
| 公司行为 | 分红送转 | equity/getEquDiv | equity_dividends |

- 新因子与风险数据分别位于 `6_ml_datasets/stock_factors_pro`、`7_risk_data/cne6_exposure`，按 `dt=YYYYMMDD/data.parquet` 分区，保留源字段名称。
- 成分、行业、停复牌保存历史有效区间，不能以 `isNew=1` 过滤历史。中证1000按 `000852.SH` 保存。
  Datayes `getIdxConsCsi` 未传 `intoDate` 只返回当前成分，因此逐交易日取历史快照后按成员及生效日合并；Doris 直接读取历史区间及最新修订。
- 申万使用 `industryVersionCD=010321`，取 `industryID1/industryName1`，不是最低层行业名称。
- 停复牌包含同步开始之前已停牌、区间内仍有效的事件。时间原字段保留，标准 `halt_begin/halt_end` 为 aware UTC，交易日期仍按上海日期归档。
- ST 保留逐日 `STflg`，不把历史缺失记录改写为当前状态。
- 分红送转使用 `beginNewPubDate/endNewPubDate`，`time` 是最新公告日期；方案按证券及报告期更新，登记/除权/支付日期和方案阶段均保留。不等同于复权因子。
- 指数日线请求沪深交易所数据。字母代码和币种版本保留完整代码，交易所后缀按接口提供的 `exchangeCD` 转换。

### 保留的其他接口

| QuantMind 数据集 | 通联接口 / 计算来源 |
| --- | --- |
| daily_unadjusted / daily_forward / daily_backward | getMktEqud（26）/ getMktEqudAdj（1462）/ getMktEqudAdjAf（2373） |
| index_daily | getMktIdxdCsi，沪深交易所可标准化的中证指数 |
| instrument_detail | getEqu（9），含上市、暂停和退市 A 股 |
| trading_calendar | getTradeCal（103），沪深开市日历；日线请求跳过休市日 |
| sector_concept | getSecTypeRel（1528）与 getEquIndustry（12），板块及行业成分 |
| index_weights | getIdxCloseWeight（24），默认沪深 300 / 中证 500 / 中证 1000；受限指数尝试对应 CSI 权重接口，仅保留请求范围内生效的记录 |
| margin_trading | getFstDetail（107） |
| balance / income / cashflow | getFdmtBS（70）/ getFdmtIS（80）/ getFdmtCF（75），保留披露版本 |
| capital | getEquShare（113） |
| pershare_index / per_share | getFdmtIndiPSPit（2901）/ getFdmtIndiPSTTMPIT（3865），保留披露时间以支持 PIT 读取 |
| dividend_factors | getMktAdjf（1463），按除权日保留累计复权因子 |
| holder_num | getEquShareholderNum（3423） |
| etf_pcf | getFundETFPRList（152）与 getFundETFCons（153），申赎基本信息及成分券 |
| convertible_bond | getMktConsBondPerf（3550）与 getBondConv（3566）；可转债集合来自 getBondConvPriceChg（2553） |
| valuation | getMktEqud 的 PE（TTM）、PB、市值与换手率；PE1 为动态年化 PE，单独保留 |
| technical_indicators / features_daily | 前复权 OHLCV 派生 MA、收益率、RSI、MACD，日频特征另合并同日估值；使用历史窗口，缺少足够历史时保留空值 |

分钟、逐笔、市场情绪和 QuantDB 专有 L1 / L1+L2 / 动态因子宽表共 7 项没有可直接对齐的商城接口或相同定义。市场情绪及专有因子可从 QuantDB 公开初始化获取；分钟及逐笔另走 SDK 同步。Datayes 的日频特征是基础行情及估值特征子集，不填造 QuantDB 专有字段；已有模型应核对所需特征是否覆盖。

股票通过 `StockCodeUtil.from_datayes` 转换为后缀式，成交量保持股、金额和市值保持元。指数权重由百分数转成比例，同时保留 `weight_pct`。财务保留原始字段、`m_anntime`（披露日）和 `m_timetag`（报告期）；ETF 不合并掉各成分券。

## 保存与读取

- QuantDB 官方目录保持独立；Datayes 默认落盘到其同级 `datayes/`，可用 `QM_DATAYES_DATA_DIR` 覆盖。沿用 `DatasetSpec` 文件布局，通联原生扩展由 `datayes_catalog.py` 注册，不互相覆盖文件。
- 只启用 Datayes 时，A 股数据中枢读 Datayes 目录；两源都启用时优先读取 QuantDB。长驻进程在数据源选择变化后重新解析目录，进程重启前也可切换。
- Datayes 股票资料与交易日历是所有同步的基础依赖，技术衍生与日频特征还依赖日线前复权和估值，会一并同步。
- 每次初始化按所选范围请求并安全合并，保留范围外的历史；取消保留已落盘数据，重新发起会重取请求范围。前后复权价格以请求时口径为准，重建历史建议选择完整日期范围。
- 返回结果逐数据集区分已同步、无数据与失败。部分失败标记 `partial`，不会提示全量成功。任务记录在 API 进程内，服务重启后会失效，已落盘文件保留。
- Token 通过现有密钥模块保存到 `config/runtime.env`（Git 已忽略）；接口仅返回掩码，初始化不返回明文，也不在任务参数中传递 Token。
