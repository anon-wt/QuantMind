import React, { useCallback, useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Alert, Button, Checkbox, Input, Modal, Progress, Select, Space, Table, Tag, Typography, message } from 'antd';
import { CloudDownloadOutlined, CloudServerOutlined, FileSearchOutlined, ReloadOutlined, StopOutlined } from '@ant-design/icons';
import dayjs from 'dayjs';
import { AShareInitCatalog, AShareInitJob, AShareSource, dataPlatformService } from '../../services/dataPlatformService';
import { describeError, httpStatusOf } from '../quantdb/utils';
import { SectionCard, VerticalStep } from './SectionCard';

const SOURCE_LABELS: Record<AShareSource, string> = { quantdb: 'QuantDB', datayes: 'Datayes', doris: 'Doris' };
const busy = (job: AShareInitJob | null) => job?.status === 'running' || job?.status === 'cancelling';

export function AShareInitialization({ onScan, onInitialized }: { onScan: () => void; onInitialized: () => void }) {
  const navigate = useNavigate();
  const [catalog, setCatalog] = useState<AShareInitCatalog | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const [selected, setSelected] = useState<string[]>([]);
  const [dataType, setDataType] = useState('all');
  const [startDate, setStartDate] = useState('2016-01-01');
  const [endDate, setEndDate] = useState(dayjs().format('YYYY-MM-DD'));
  const [incremental, setIncremental] = useState(true);
  const [job, setJob] = useState<AShareInitJob | null>(null);
  const [starting, setStarting] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const running = busy(job);
  const providers = catalog?.providers ?? [];
  const enabled = providers.filter((provider) => provider.enabled);
  const sources = enabled.map((provider) => provider.source);
  const availableDatasets = (catalog?.datasets ?? []).filter((row) => row.sources.some((source) => sources.includes(source)));
  const dataTypes = [...new Map(availableDatasets.map(row => [row.data_type ?? row.group, row.data_type_name ?? row.group])).entries()];
  const hasDatayes = sources.includes('datayes');
  const hasDoris = sources.includes('doris');
  const missingToken = enabled.some((provider) => !provider.configured);

  const loadCatalog = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setCatalog(await dataPlatformService.getAShareInitCatalog());
    } catch (err: unknown) {
      setError(describeError(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadCatalog();
    void dataPlatformService.listAShareInitializationJobs().then((result) => setJob(result.jobs[0] ?? null)).catch(() => {});
  }, [loadCatalog]);

  useEffect(() => {
    if (!job || !busy(job)) return;
    let stopped = false;
    const timer = setInterval(async () => {
      try {
        const result = await dataPlatformService.getAShareInitializationJob(job.job_id);
        if (stopped) return;
        setJob(result.job);
        if (!busy(result.job)) {
          clearInterval(timer);
          void loadCatalog();
          onInitialized();
          if (result.job.status === 'completed') message.success('数据初始化已完成');
          else if (result.job.status === 'partial') message.warning('部分数据集同步失败，请查看初始化结果');
        }
      } catch (err: unknown) {
        if (!stopped && httpStatusOf(err) === 404) {
          setJob({ ...job, status: 'failed', error: '任务记录已失效，已落盘数据保留，可重新初始化。' });
          clearInterval(timer);
        }
      }
    }, 2500);
    return () => { stopped = true; clearInterval(timer); };
  }, [job?.job_id, job?.status, loadCatalog, onInitialized]);

  const saveSource = async (source: AShareSource, value: boolean) => {
    setSaving(true);
    try {
      await dataPlatformService.saveMarketDataSources('quantdb', { [source]: value });
      await loadCatalog();
      onInitialized();
      message.success('数据源配置已保存');
    } catch (err: unknown) {
      message.error(`保存失败：${describeError(err)}`);
    } finally {
      setSaving(false);
    }
  };

  const openInitialization = () => {
    if (!running) {
      setDataType('all');
      setSelected(availableDatasets.map((row) => row.dataset));
    }
    setOpen(true);
  };

  const start = async () => {
    if (!sources.length || !selected.length) return;
    if ((hasDatayes || hasDoris) && (!startDate || !endDate || startDate > endDate || endDate > dayjs().format('YYYY-MM-DD'))) {
      message.warning('请选择有效的同步日期范围');
      return;
    }
    setStarting(true);
    try {
      const result = await dataPlatformService.startAShareInitialization({ sources, datasets: selected, start_date: startDate, end_date: endDate, incremental });
      setJob(result.job);
      message.success('初始化已在后台启动');
    } catch (err: unknown) {
      message.error(`启动失败：${describeError(err)}`);
    } finally {
      setStarting(false);
    }
  };

  const cancel = async () => {
    if (!job) return;
    setCancelling(true);
    try {
      setJob((await dataPlatformService.cancelAShareInitialization(job.job_id)).job);
    } catch (err: unknown) {
      message.error(`取消失败：${describeError(err)}`);
    } finally {
      setCancelling(false);
    }
  };

  const resultRows = (job?.results ?? []).flatMap((result) => {
    if (Array.isArray(result.datasets)) return result.datasets.map((item) => ({ ...item, source: result.source, summary: `同步 ${item.rows ?? 0} 行` }));
    return Object.entries(result.datasets ?? {}).map(([dataset, item]) => ({
      dataset, source: result.source, status: item.errors ? 'failed' : 'completed', rows: item.downloaded ?? 0,
      summary: `下载 ${item.downloaded ?? 0} 个文件，跳过 ${item.skipped ?? 0} 个文件`,
      error: item.errors ? `${item.errors} 个文件失败` : undefined,
    }));
  });
  const tokenNotice = missingToken && (
    <Alert type="warning" showIcon message={`请先配置 ${enabled.filter((provider) => !provider.configured).map((provider) => SOURCE_LABELS[provider.source]).join('、')} 的连接凭据或表映射`}
      action={<Button size="small" onClick={() => navigate('/user-center?tab=data-platform')}>前往个人中心</Button>} />
  );

  return (
    <>
      <SectionCard index="01" title="引导初始化" desc="A 股 · 数据源与本地环境" icon={<CloudServerOutlined />} tone="indigo"
        extra={<Tag className="m-0 rounded-full">{enabled.length}/{providers.length || 3} 源已启用</Tag>}>
        <VerticalStep done={enabled.length > 0} title="数据源配置" desc="按勾选的数据源分发初始化任务，配置自动保存"
          extra={
            <div className="rounded-2xl border border-slate-100 bg-slate-50/50 p-3 space-y-3">
              {error ? <Alert type="error" message={error} action={<Button size="small" onClick={() => void loadCatalog()}>重试</Button>} /> : (
                <Space wrap>
                  {providers.map((provider) => (
                    <Checkbox key={provider.source} checked={provider.enabled} disabled={loading || saving || running}
                      className="bg-white rounded-lg border border-slate-100 px-2.5 py-1.5 m-0"
                      onChange={(event) => void saveSource(provider.source, event.target.checked)}>
                      <span className="text-xs font-medium">{provider.label}</span>
                      <span className="text-[11px] text-slate-400 ml-1">({provider.source})</span>
                    </Checkbox>
                  ))}
                  {loading && !catalog && <Typography.Text type="secondary">加载中…</Typography.Text>}
                </Space>
              )}
              {tokenNotice}
              <Typography.Text type="secondary" className="text-xs">连接凭据在个人中心 → 数据平台配置。多源启用时读取优先级为 QuantDB → Datayes → Doris；只读取 Doris 时请关闭前两项。</Typography.Text>
            </div>
          } />
        <VerticalStep done={enabled.length > 0 && enabled.every((provider) => provider.dataset_count > 0 && provider.local_dataset_count >= provider.dataset_count)}
          active={enabled.length > 0} isLast title="本地落盘"
          desc={enabled.length ? enabled.map((provider) => `${SOURCE_LABELS[provider.source]} · ${provider.local_dataset_count}/${provider.dataset_count} 已同步`).join('；') : '选择数据源后初始化本地数据'}
          extra={
            <div className="bg-white rounded-xl border border-slate-100 px-3 py-3 space-y-3">
              {enabled.map((provider) => <div key={provider.source} className="text-xs text-slate-500">{SOURCE_LABELS[provider.source]} 目录 <code className="bg-slate-50 px-1 py-0.5 rounded border">{provider.root}</code></div>)}
              <Space wrap>
                <Button type="primary" icon={<CloudDownloadOutlined />} disabled={!enabled.length || loading || saving || Boolean(error)} onClick={openInitialization}>
                  {running ? '查看初始化进度' : hasDoris ? '初始化 / 增量同步' : '初始化数据'}
                </Button>
                {sources.includes('quantdb') && <Button icon={<FileSearchOutlined />} onClick={onScan} disabled={running}>本地扫描</Button>}
                <Button size="small" icon={<ReloadOutlined />} loading={loading} onClick={() => void loadCatalog()}>刷新</Button>
              </Space>
            </div>
          } />
      </SectionCard>

      <Modal title={`初始化数据 · ${sources.map((source) => SOURCE_LABELS[source]).join(' + ')}`} open={open} onCancel={() => setOpen(false)} width={960}
        styles={{ body: { maxHeight: '65vh', overflowY: 'auto' } }}
        footer={<Space>{running && <Button danger icon={<StopOutlined />} loading={cancelling} onClick={() => void cancel()}>取消任务</Button>}
          <Button onClick={() => setOpen(false)}>关闭</Button>
          <Button type="primary" loading={starting} disabled={running || missingToken || !selected.length || !sources.length} onClick={() => void start()}>开始初始化</Button></Space>}>
        <Space direction="vertical" className="w-full" size="middle">
          {tokenNotice}
          <div className="rounded-xl border border-slate-200 p-3 space-y-3" aria-label="当前初始化范围">
            <Typography.Text strong>当前初始化范围：{availableDatasets.length} 类数据集，已选择 {selected.filter((name) => availableDatasets.some((row) => row.dataset === name)).length} 类</Typography.Text>
            {enabled.map((provider) => <div key={provider.source} className="space-y-1">
              <div><Tag>{SOURCE_LABELS[provider.source]}</Tag><Typography.Text>将同步 {availableDatasets.filter((row) => selected.includes(row.dataset) && row.sources.includes(provider.source)).length}/{provider.dataset_count} 类数据集</Typography.Text></div>
              <div className="text-xs text-slate-600">{provider.scope_hint}</div>
              <div className="text-xs text-slate-500">{provider.sync_hint}</div>
            </div>)}
          </div>
          {sources.includes('quantdb') && <Alert type="info" showIcon message="QuantDB 公开初始化免 API Key 和 QuantDB 流量；分钟线与逐笔数据不在公开初始化范围内，需通过 SDK 同步。" />}
          {hasDatayes && <>
            <Alert type="info" showIcon message="Datayes 按所选日期同步商城可映射数据集，并生成技术指标及基础日频特征；数据权限和调用额度以订阅为准。QuantDB 专有因子需从 QuantDB 初始化；分钟和逐笔不在本页初始化范围内。" />
            <Typography.Text type="secondary" className="text-xs">会一并同步股票资料、交易日历和派生数据所需的日线、估值；分批安全写入，可取消任务。</Typography.Text>
          </>}
          {hasDoris && <Alert type="info" showIcon message="Doris 只读查询映射表，写入独立本地 Parquet。增量同步从上次成功日期回看已配置的天数；历史修订超出回看范围时，可关闭增量并重取指定区间。" />}
          {(hasDatayes || hasDoris) && <Space wrap><span>同步范围</span><Input aria-label="同步开始日期" type="date" value={startDate} min="1990-01-01" max={endDate} disabled={running} onChange={(event) => setStartDate(event.target.value)} />
            <span>至</span><Input aria-label="同步结束日期" type="date" value={endDate} min={startDate} max={dayjs().format('YYYY-MM-DD')} disabled={running} onChange={(event) => setEndDate(event.target.value)} /></Space>}
          {hasDoris && <Checkbox checked={incremental} disabled={running} onChange={(event) => setIncremental(event.target.checked)}>Doris 按本地成功进度增量同步</Checkbox>}
          <Select aria-label="初始化数据类型筛选" value={dataType} onChange={setDataType} style={{minWidth:160}} options={[{value:'all',label:'全部数据类型'}, ...dataTypes.map(([value,label]) => ({value,label}))]} />
          <Table size="small" pagination={false} rowKey="dataset" dataSource={availableDatasets.filter(row => dataType === 'all' || (row.data_type ?? row.group) === dataType)}
            rowSelection={{ preserveSelectedRowKeys: true, selectedRowKeys: selected, onChange: (keys) => setSelected(keys as string[]), getCheckboxProps: (row) => ({ disabled: running || !row.sources.some((source) => sources.includes(source)) }) }}
            columns={[
              { title: '数据类型', dataIndex: 'data_type_name', render:(value:string, row) => value ?? row.group },
              { title: '数据集', dataIndex: 'name' },
              { title: '可用数据源', render: (_, row) => <Space>{row.sources.filter((source) => sources.includes(source)).map((source) => <Tag key={source}>{SOURCE_LABELS[source]}</Tag>)}{!row.sources.some((source) => sources.includes(source)) && <Tag>当前数据源不支持</Tag>}</Space> },
              { title: 'Datayes 覆盖说明', dataIndex: 'datayes_note', hidden: !hasDatayes },
              { title: 'Doris 覆盖说明', dataIndex: 'doris_note', hidden: !hasDoris },
            ]} />
          {job && <div className="space-y-3">
            <Tag color={running ? 'processing' : job.status === 'completed' ? 'success' : 'warning'}>{job.status === 'completed' ? '初始化完成' : job.status === 'partial' ? '部分失败' : job.status === 'cancelled' ? '已取消' : job.status === 'failed' ? '初始化失败' : '后台执行中'}</Tag>
            <Typography.Text>{job.current ?? `已处理 ${job.done} 个数据集`}</Typography.Text>
            <Progress percent={job.total ? Math.min(100, Math.round(job.done / job.total * 100)) : 0} status={job.status === 'failed' || job.status === 'partial' ? 'exception' : undefined} />
            {job.detail?.total ? <Typography.Text type="secondary">当前数据集 {job.detail.done ?? 0}/{job.detail.total} 批次</Typography.Text> : null}
            {job.error && <Alert type="error" message={job.error} />}
            {job.results.filter((result) => result.error).map((result) => <Alert key={result.source} type="error" message={result.error} />)}
            {job.results.filter((result) => (result.skipped_stock_rows ?? 0) > 0).map((result) => <Alert key={`${result.source}-skipped`} type="warning" showIcon message={`${SOURCE_LABELS[result.source]} 已跳过 ${result.skipped_stock_rows} 条无法识别的证券记录，请核对源表编码。`} />)}
            {resultRows.length > 0 && <Table size="small" pagination={false} rowKey={(row) => `${row.source}:${row.dataset}`} dataSource={resultRows}
              columns={[{ title: '数据源', dataIndex: 'source' }, { title: '数据集', dataIndex: 'dataset' }, { title: '结果', render: (_, row) => <Tag color={row.status === 'failed' ? 'error' : row.status === 'empty' ? 'default' : 'success'}>{row.status === 'failed' ? '失败' : row.status === 'empty' ? '无数据' : '已同步'}</Tag> }, { title: '说明', render: (_, row) => row.error || row.summary }]} />}
          </div>}
        </Space>
      </Modal>
    </>
  );
}
