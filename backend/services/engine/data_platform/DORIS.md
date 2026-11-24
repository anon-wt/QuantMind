# Doris 数据源

个人中心 → 数据平台 → Doris 配置连接、测试连接、发现表并保存映射。
权限与 QuantDB / Datayes 相同，使用现有管理员会话。密码只通过
`runtime_secrets` 保存至服务端 `config/runtime.env`，接口不返回密码。
连接信息和映射保存至 `config/doris_source.json`（运行时配置，不提交 Git）。

## 映射与文件

- 自动接口映射：复用 `datayes_sync.py` 的 22 个数据集及字段/单位契约。
  按配置前缀匹配接口名，同时识别 `hunter_ods_datayes_`、`ods_datayes_`，
  实际表名以发现结果为准；缺少关联表的数据集不可选，不用空表冒充完整覆盖。
- 自定义映射：配置「Doris 表 → 本地标准数据集 → 源字段到标准字段」。
  其优先级高于同一数据集的自动接口映射。未重命名的其他列保留，便于接入因子宽表。
- 目标路径来自 `backend/shared/quantdb_datasets.py`，页面显示实际文件模板：
  分区 `dt={YYYYMMDD}/data.parquet`、标的 `{symbol}.parquet`、
  股票资料 `instrument_list.parquet` 等。禁止任意路径写入，保证现有读取契约。
- 日线标准字段为 `symbol,time,open,high,low,close,volume,amount`；股票资料需
  `symbol,name,list_date`；财务保留 `m_anntime`（披露时间）与 `m_timetag`（报告期）。
  日期字段独立配置，支持 DATE / ISO 日期和 YYYYMMDD。成交量为股，金额为元。
- 首批为日频数据及训练因子，暂不接入分钟与逐笔；配置接口会拒绝这些映射，
  避免用日频日期/主键合并损失日内时间精度和成交记录。
- 自定义映射中的价格复权和数值单位由源表保证，不根据字段名猜测复权方式或倍数。
  因子缺少 OHLCV 时从同目录后复权日线补给；覆盖不完整则失败，不填造模型特征。
- Doris 预置因子表映射是可选项，原始因子名称不等同于 QuantDB 专有因子。
  已有模型必须检查其特征定义；字段映射不负责重新计算或更改因子经济含义。

## 同步与读取

默认独立落盘到 QuantDB 同级的 `doris/`，可用 `QM_DORIS_DATA_DIR` 覆盖。
目录不能与 QuantDB / Datayes 重叠。A 股启用优先级保持 QuantDB → Datayes → Doris；
只使用 Doris 时关闭前两项。配置保存本身不启用数据源或自动触发同步。

引导初始化、目录中的增量同步、用户配置的同步调度共用 `sync_doris`。
同步只执行 SHOW / DESCRIBE / SELECT；表/字段标识符校验，日期与证券条件绑定参数。
不存在任意 SQL 执行入口。源数据不会被修改。

首次同步读取选定区间；后续从所选数据集最早成功进度回看 `lookback_days`（默认 7）
个自然日。`.doris_sync_state.json` 记录成功数据集的进度，连接或映射变更使进度失效。
旧区间修订可关闭增量后重取指定日期。按业务键合并更新，源库删除不会自动删除历史本地行。
分批原子写入；失败或取消保留已落盘文件，不提前推进失败数据集进度。源数据集过大时
可缩小区间；连接/查询超时有界，取消在批次边界生效。目录文件锁防止手动/调度并发写入。

训练因子、行情/投研 Hub、训练容器挂载与推理共用 A 股读取源选择；Doris Qlib 缓存
单独位于 `doris/.qlib_cache/cn_data`，切换后需构建本源缓存，不复用 QuantDB 缓存。
`QLIB_PROVIDER_URI` 显式覆盖仍优先。当前使用本地 Docker 节点训练，远程直读训练会明确
拒绝，避免节点偷偷下载 QuantDB 数据代替 Doris。本接入不将 Doris 凭据下发给训练容器，
也不自动把中心 Parquet 推送到其他训练节点。

## 运行依赖与限制

后端需要 PyMySQL（已加入根 `requirements.txt`），训练容器仅读取 Parquet，无需连接 Doris。
`QM_DORIS_CONFIG_FILE` 可覆盖映射文件位置。连接环境键为 `DORIS_FE_HOST`、
`DORIS_FE_QUERY_PORT`、`DORIS_USER`、`DORIS_DATABASE`、`DORIS_PASSWORD`；沿用现有
环境变量优先规则。初始化任务沿用现有 API 线程任务及进度查询，API 进程重启后任务
记录失效，成功进度和 Parquet 保留，可重新提交恢复。服务端必须能连接 Doris FE 查询端口。
