import React, { useCallback, useEffect, useState } from 'react';
import { Alert, AutoComplete, Button, Input, InputNumber, Modal, Select, Space, Table, Tag, Typography, message } from 'antd';
import { Database } from 'lucide-react';
import { dataPlatformService } from '../../admin/services/dataPlatformService';
import type { DorisConfig, DorisConnection, DorisTableMapping } from '../../admin/services/dataPlatformService';
import { describeError } from '../../admin/components/quantdb/utils';

const DEFAULT_CONNECTION: DorisConnection = {
  host: '', port: 9030, database: '', user: '', table_prefix: 'ods_datayes_',
  lookback_days: 7, mappings: {}, dataset_mappings: {},
};
const FACTOR_LABELS: Record<string, string> = {
  l1_factors: '训练因子宽表（L1 入口）', l1_l2_factors: '训练因子宽表（合并入口）',
  factor_dictionary: '训练因子宽表（动态因子入口）',
};

function dorisError(error: unknown): string {
  const detail = (error as { response?: { data?: { detail?: unknown } } } | null)?.response?.data?.detail;
  return typeof detail === 'string' ? detail : describeError(error);
}

export default function DorisSettings() {
  const [config, setConfig] = useState<DorisConfig | null>(null);
  const [form, setForm] = useState<DorisConnection>(DEFAULT_CONNECTION);
  const [password, setPassword] = useState('');
  const [loading, setLoading] = useState(true);
  const [action, setAction] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [factorTables, setFactorTables] = useState<string[]>([]);
  const [dataType, setDataType] = useState('all');
  const [fieldDataset, setFieldDataset] = useState<string | null>(null);
  const [fieldRows, setFieldRows] = useState<Array<{ source: string; target: string }>>([]);
  const busy = loading || action !== null;

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const value = await dataPlatformService.getDorisConfig();
      setConfig(value);
      setForm(value.connection ?? {...DEFAULT_CONNECTION, mappings: value.preset_mappings ?? {}});
      setPassword('');
    } catch (err) {
      setError(dorisError(err));
    } finally {
      setLoading(false);
    }
  }, []);
  useEffect(() => { void load(); }, [load]);

  const payload = (): DorisConnection => ({
    ...form,
    host: form.host.trim(), database: form.database.trim(), user: form.user.trim(),
    mappings: Object.fromEntries(Object.entries(form.mappings).filter(([, value]) => value.table.trim())),
    dataset_mappings: Object.fromEntries(Object.entries(form.dataset_mappings ?? {}).filter(([, value]) => value.table.trim())),
    ...(password ? { password } : {}),
  });
  const run = async (kind: 'test' | 'discover' | 'save') => {
    if (!form.host.trim() || !form.database.trim() || !form.user.trim()) {
      message.warning('请填写 Doris 主机、数据库和用户名');
      return;
    }
    setAction(kind);
    setError(null);
    try {
      if (kind === 'save') {
        const value = await dataPlatformService.saveDorisConfig(payload());
        setConfig(value);
        setForm(value.connection ?? DEFAULT_CONNECTION);
        setPassword('');
        message.success('Doris 连接与表映射已保存');
      } else if (kind === 'test') {
        await dataPlatformService.testDorisConnection(payload());
        message.success('Doris 连接成功');
      } else {
        const value = await dataPlatformService.discoverDorisTables(payload());
        setForm((current) => ({ ...current, mappings: value.mappings }));
        setFactorTables(value.factor_candidates);
        message.success(`已检查 ${value.table_count} 张表，匹配结果请确认后保存`);
      }
    } catch (err) {
      setError(dorisError(err));
    } finally {
      setAction(null);
    }
  };
  const setMapping = (key: string, patch: Partial<DorisTableMapping>) => {
    setForm((current) => ({ ...current, mappings: {
      ...current.mappings,
      [key]: { table: '', date_format: 'date', ...current.mappings[key], ...patch },
    } }));
  };
  const rows = [...(config?.endpoints ?? []), ...(config?.factor_datasets ?? [])].map((key) => ({ key }));
  const datasetRows = config?.datasets ?? [];
  const dataTypes = [...new Map(datasetRows.map(row => [row.data_type ?? 'other', row.data_type_name ?? '其他'])).entries()];
  const addPresets = () => setForm(current => ({...current, mappings: {...current.mappings,
    ...Object.fromEntries(Object.entries(config?.preset_mappings ?? {}).map(([key, value]) => [key, current.mappings[key]?.table.trim() ? current.mappings[key] : value])),
  }}));
  const setDatasetMapping = (key: string, patch: Partial<DorisTableMapping> & { columns?: Record<string, string> }) => {
    setForm((current) => ({ ...current, dataset_mappings: {
      ...current.dataset_mappings,
      [key]: { table: '', date_format: 'date', columns: {}, ...current.dataset_mappings?.[key], ...patch },
    } }));
  };
  const editFields = (dataset: string) => {
    setFieldDataset(dataset);
    const columns = form.dataset_mappings?.[dataset]?.columns ?? {};
    setFieldRows(Object.entries(columns).map(([target, source]) => ({ source, target })));
  };
  const saveFields = () => {
    const complete = fieldRows.filter((row) => row.source.trim() || row.target.trim());
    const valid = /^[A-Za-z_][A-Za-z0-9_]*$/;
    if (complete.some((row) => !valid.test(row.source.trim()) || !valid.test(row.target.trim()))
      || new Set(complete.map((row) => row.target.trim())).size !== complete.length
      || new Set(complete.map((row) => row.source.trim())).size !== complete.length) {
      message.warning('请填写有效且不重复的源字段和目标字段');
      return;
    }
    if (fieldDataset) setDatasetMapping(fieldDataset, { columns: Object.fromEntries(complete.map((row) => [row.target.trim(), row.source.trim()])) });
    setFieldDataset(null);
  };

  return (
    <div className="w-full space-y-3">
      <div className="bg-white rounded-2xl border border-slate-200/80 px-5 py-3.5 shadow-xs flex items-center gap-3">
        <div className="w-9 h-9 rounded-xl bg-gradient-to-br from-emerald-500 to-teal-600 flex items-center justify-center text-white"><Database className="w-4 h-4" /></div>
        <div><h3 className="text-sm font-black text-slate-800 m-0">数据平台 (Doris)</h3>
          <p className="text-[11px] text-slate-400 m-0">从 Doris 的 Datayes 接口表增量同步到本地数据目录</p></div>
        <Tag color={config?.configured ? 'green' : 'default'}>{config?.configured ? '已配置' : '未配置'}</Tag>
      </div>
      <div className="bg-white rounded-2xl border border-slate-200/80 p-4 shadow-xs space-y-4">
        {error && <Alert type="error" showIcon message="Doris 配置或连接失败" description={error} />}
        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
          <label className="text-xs text-slate-600">主机地址<Input aria-label="Doris 主机地址" value={form.host} disabled={busy} onChange={(e) => setForm({ ...form, host: e.target.value })} placeholder="Doris FE 主机" /></label>
          <label className="text-xs text-slate-600">查询端口<InputNumber aria-label="Doris 查询端口" className="w-full" value={form.port} min={1} max={65535} disabled={busy} onChange={(value) => setForm({ ...form, port: value ?? 9030 })} /></label>
          <label className="text-xs text-slate-600">数据库<Input aria-label="Doris 数据库" value={form.database} disabled={busy} onChange={(e) => setForm({ ...form, database: e.target.value })} /></label>
          <label className="text-xs text-slate-600">用户名<Input aria-label="Doris 用户名" value={form.user} disabled={busy} onChange={(e) => setForm({ ...form, user: e.target.value })} autoComplete="off" /></label>
          <label className="text-xs text-slate-600">密码<Input.Password aria-label="Doris 密码" value={password} disabled={busy} placeholder={config?.password_configured ? '已保存，留空保持原密码' : '输入 Doris 密码'} onChange={(e) => setPassword(e.target.value)} autoComplete="new-password" /></label>
          <label className="text-xs text-slate-600">接口表名前缀<Input aria-label="Doris 表名前缀" value={form.table_prefix} disabled={busy} onChange={(e) => setForm({ ...form, table_prefix: e.target.value })} /></label>
          <label className="text-xs text-slate-600">增量回看天数<InputNumber aria-label="Doris 增量回看天数" className="w-full" min={1} max={365} value={form.lookback_days} disabled={busy} onChange={(value) => setForm({ ...form, lookback_days: value ?? 7 })} /></label>
        </div>
        <Space wrap>
          <Button loading={action === 'test'} disabled={busy && action !== 'test'} onClick={() => void run('test')}>测试连接</Button>
          <Button loading={action === 'discover'} disabled={busy && action !== 'discover'} onClick={() => void run('discover')}>发现并匹配表</Button>
          <Button type="primary" loading={action === 'save'} disabled={busy && action !== 'save'} onClick={() => void run('save')}>保存连接与映射</Button>
          <Button disabled={busy} onClick={() => void load()}>重新加载</Button>
          {config?.preset_mappings && <Button disabled={busy} onClick={addPresets}>补全12项通联映射</Button>}
        </Space>
        <Typography.Paragraph type="secondary" className="text-xs !mb-0">
          密码仅保存在服务端，页面不回显。发现表会优先按填写的前缀匹配，也识别 ods_datayes_ 前缀；匹配完成后请保存。保存配置不会自动启用或同步数据。
        </Typography.Paragraph>
        <div className="rounded-xl border border-slate-100 p-3 space-y-3">
          <Typography.Text strong>表名 → 本地标准 Parquet → 字段映射</Typography.Text>
          <Select aria-label="Doris 数据类型筛选" value={dataType} onChange={setDataType} style={{minWidth:150}} options={[{value:'all',label:'全部数据类型'}, ...dataTypes.map(([value,label]) => ({value,label}))]} />
          <Typography.Paragraph type="secondary" className="text-xs !mb-0">填写后优先使用本行映射，未配置的数据集继续使用下方接口表自动映射。文件路径由标准数据集确定，日期和股票代码会自动替换模板；未映射的其他字段按原名保留。</Typography.Paragraph>
          <Table size="small" rowKey="dataset" dataSource={datasetRows.filter(row => dataType === 'all' || row.data_type === dataType)} pagination={{ pageSize: 12 }} scroll={{ x: 1250 }} columns={[
            { title: '数据类型', dataIndex: 'data_type_name', width: 100 },
            { title: '目标数据集', dataIndex: 'name', width: 120 },
            { title: 'Datayes 接口', dataIndex: 'endpoint', width: 210, render:(value:string) => value ? <Typography.Text code className="text-[11px] break-all">{value}</Typography.Text> : '手动映射' },
            { title: 'Doris 源表', width: 250, render: (_, row) => <><Input aria-label={`${row.dataset} Doris 源表`} value={form.dataset_mappings?.[row.dataset]?.table ?? ''} disabled={busy} placeholder={row.endpoint_key ? form.mappings[row.endpoint_key]?.table || row.doris_table || '留空使用接口自动映射' : '留空使用接口自动映射'} onChange={(e) => setDatasetMapping(row.dataset, { table: e.target.value })} />{row.endpoint_key && form.mappings[row.endpoint_key]?.table && <Typography.Text type="secondary" className="text-[11px] break-all">接口映射：{form.mappings[row.endpoint_key].table}</Typography.Text>}</> },
            { title: '本地 Parquet 文件路径', dataIndex: 'path', width: 300, render: (value: string) => <Typography.Text code className="text-[11px] break-all">{value}</Typography.Text> },
            { title: '日期字段', width: 135, render: (_, row) => <Input value={form.dataset_mappings?.[row.dataset]?.date_column ?? ''} disabled={busy} placeholder="自动识别" onChange={(e) => setDatasetMapping(row.dataset, { date_column: e.target.value || null })} /> },
            { title: '日期格式', width: 150, render: (_, row) => <Select className="w-full" disabled={busy} value={form.dataset_mappings?.[row.dataset]?.date_format ?? 'date'} options={[{ value: 'date', label: 'YYYY-MM-DD' }, { value: 'yyyymmdd', label: 'YYYYMMDD' }]} onChange={(value) => setDatasetMapping(row.dataset, { date_format: value })} /> },
            { title: '字段映射', width: 125, render: (_, row) => <Button size="small" disabled={busy} onClick={() => editFields(row.dataset)}>配置（{Object.keys(form.dataset_mappings?.[row.dataset]?.columns ?? {}).length}）</Button> },
          ]} />
        </div>
        <details className="rounded-xl border border-slate-100 p-3">
          <summary className="cursor-pointer text-sm font-semibold text-slate-700">Datayes 接口表自动映射（{Object.keys(form.mappings).length} 项）</summary>
          <Alert className="my-3" type="info" showIcon message="仅同步已映射的数据集" description="接口表字段需符合对应 Datayes 接口定义。训练因子表可单独选择，需包含股票代码和交易日期；同步时会补入同日后复权行情。Doris 因子不等同于 QuantDB 专有因子，已有模型需核对字段。" />
          <Table size="small" rowKey="key" dataSource={rows} pagination={{ pageSize: 8 }} scroll={{ x: 750 }} columns={[
            { title: '接口 / 数据集', dataIndex: 'key', width: 220, render: (key: string) => FACTOR_LABELS[key] ?? key },
            { title: 'Doris 表名', render: (_, row) => FACTOR_LABELS[row.key]
              ? <AutoComplete allowClear className="w-full min-w-[230px]" disabled={busy} value={form.mappings[row.key]?.table ?? ''} options={factorTables.map((name) => ({ value: name }))} filterOption={(input, option) => String(option?.value ?? '').toLowerCase().includes(input.toLowerCase())} onChange={(value) => setMapping(row.key, { table: value })} placeholder="选择或填写因子表名" />
              : <Input aria-label={`${row.key} 表名`} value={form.mappings[row.key]?.table ?? ''} disabled={busy} placeholder="未映射" onChange={(e) => setMapping(row.key, { table: e.target.value })} /> },
            { title: '日期字段（可选）', width: 150, render: (_, row) => <Input value={form.mappings[row.key]?.date_column ?? ''} disabled={busy} placeholder="自动识别" onChange={(e) => setMapping(row.key, { date_column: e.target.value || null })} /> },
            { title: '日期格式', width: 145, render: (_, row) => <Select className="w-full" disabled={busy} value={form.mappings[row.key]?.date_format ?? 'date'} options={[{ value: 'date', label: '日期 / YYYY-MM-DD' }, { value: 'yyyymmdd', label: 'YYYYMMDD' }]} onChange={(value) => setMapping(row.key, { date_format: value })} /> },
          ]} />
        </details>
        <Typography.Text type="secondary" className="text-xs">本地目录：{config?.root ?? '加载中…'}。启用位置：管理后台 → 数据管理 → A 股 → 引导初始化。</Typography.Text>
      </div>
      <Modal title={`字段映射 · ${config?.datasets.find((row) => row.dataset === fieldDataset)?.name ?? fieldDataset ?? ''}`} open={fieldDataset !== null} onCancel={() => setFieldDataset(null)} onOk={saveFields} okText="应用字段映射" width={700}>
        <Alert type="info" showIcon className="mb-3" message="将 Doris 字段映射为本地标准字段" description="常用目标字段：symbol、time、open、high、low、close、volume、amount；股票资料使用 name、list_date；财务数据使用 m_anntime（披露日）、m_timetag（报告期）。日期字段也可在主表指定。映射同名字段时可留空；请保持价格复权口径及成交量（股）、金额（元）一致。" />
        <div className="space-y-2">
          {fieldRows.map((row, index) => <div key={index} className="flex items-center gap-2">
            <Input aria-label={`源字段 ${index + 1}`} placeholder="Doris 源字段" value={row.source} onChange={(e) => setFieldRows(fieldRows.map((item, i) => i === index ? { ...item, source: e.target.value } : item))} />
            <span>→</span>
            <Input aria-label={`标准字段 ${index + 1}`} placeholder="本地标准字段" value={row.target} onChange={(e) => setFieldRows(fieldRows.map((item, i) => i === index ? { ...item, target: e.target.value } : item))} />
            <Button danger onClick={() => setFieldRows(fieldRows.filter((_, i) => i !== index))}>删除</Button>
          </div>)}
          <Button onClick={() => setFieldRows([...fieldRows, { source: '', target: '' }])}>添加字段映射</Button>
        </div>
        <Typography.Paragraph type="secondary" className="text-xs mt-3 !mb-0">应用后还需点击“保存连接与映射”。修改映射会使原增量进度失效，下次将重取所选日期范围。</Typography.Paragraph>
      </Modal>
    </div>
  );
}
