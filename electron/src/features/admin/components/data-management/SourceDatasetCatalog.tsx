import React, { useState } from 'react';
import { Select, Space, Table, Tag, Typography } from 'antd';
import type { SourceDatasetEntry } from '../../services/dataPlatformService';

export function SourceDatasetCatalog({ datasets }: { datasets: SourceDatasetEntry[] }) {
  const [category, setCategory] = useState('all');
  const types = Array.from(new Map(datasets.map(row => [row.data_type ?? 'other', row.data_type_name ?? '其他'])).entries());
  return <div className="space-y-3">
    <Space wrap><Typography.Text strong>通联标准数据集（{datasets.length} 项）</Typography.Text>
      <Select aria-label="数据类型筛选" value={category} onChange={setCategory} style={{minWidth:140}} options={[{value:'all',label:'全部数据类型'}, ...types.map(([value,label]) => ({value,label}))]} />
    </Space>
    <Table size="small" rowKey="dataset" pagination={false} scroll={{x:720}} dataSource={datasets.filter(row => category === 'all' || row.data_type === category)} columns={[
      {title:'数据类型',dataIndex:'data_type_name',width:100,render:(value:string) => <Tag>{value}</Tag>},
      {title:'数据集',dataIndex:'name',width:150},
      {title:'Datayes 接口',dataIndex:'endpoint',render:(value:string) => <Typography.Text code className="text-xs break-all">{value}</Typography.Text>},
      {title:'本地 Parquet',dataIndex:'path',render:(value:string) => <Typography.Text code className="text-xs break-all">{value}</Typography.Text>},
    ]} />
  </div>;
}
