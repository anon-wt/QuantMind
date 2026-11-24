import React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({ get: vi.fn(), save: vi.fn(), test: vi.fn(), discover: vi.fn() }));
vi.mock('../../admin/services/dataPlatformService', () => ({
  dataPlatformService: {
    getDorisConfig: mocks.get, saveDorisConfig: mocks.save,
    testDorisConnection: mocks.test, discoverDorisTables: mocks.discover,
  },
}));
import DorisSettings from './DorisSettings';

const config = {
  configured: true, password_configured: true, root: '/data/doris',
  supported_datasets: ['daily_forward'], endpoints: ['getMktEqudAdj'], factor_datasets: [],
  connection: {
    host: 'doris.test', port: 9030, database: 'market', user: 'reader', table_prefix: 'ods_datayes_',
    lookback_days: 7, mappings: {}, dataset_mappings: {},
  },
  datasets: [{ dataset: 'daily_forward', name: '日线前复权', path: '1_kline_data/daily_forward/dt={YYYYMMDD}/data.parquet' }],
};

describe('Doris personal data platform settings', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.get.mockResolvedValue(structuredClone(config));
    mocks.save.mockResolvedValue(structuredClone(config));
  });

  it('saves table and field mappings without replacing a stored password', async () => {
    render(<DorisSettings />);
    const table = await screen.findByLabelText('daily_forward Doris 源表');
    expect(screen.getByLabelText('Doris 密码')).toHaveValue('');
    fireEvent.change(table, { target: { value: 'market_bars' } });
    fireEvent.click(screen.getByRole('button', { name: '配置（0）' }));
    fireEvent.click(screen.getByRole('button', { name: '添加字段映射' }));
    fireEvent.change(screen.getByLabelText('源字段 1'), { target: { value: 'close_price' } });
    fireEvent.change(screen.getByLabelText('标准字段 1'), { target: { value: 'close' } });
    fireEvent.click(screen.getByRole('button', { name: '应用字段映射' }));
    fireEvent.click(screen.getByRole('button', { name: '保存连接与映射' }));

    await waitFor(() => expect(mocks.save).toHaveBeenCalledWith(expect.objectContaining({
      dataset_mappings: { daily_forward: { table: 'market_bars', columns: { close: 'close_price' }, date_format: 'date' } },
    })));
    expect(mocks.save.mock.calls[0][0]).not.toHaveProperty('password');
    expect(screen.getByText('1_kline_data/daily_forward/dt={YYYYMMDD}/data.parquet')).toBeInTheDocument();
  });

  it('displays a connection failure and preserves the editable connection', async () => {
    mocks.test.mockRejectedValue({ response: { data: { detail: 'Doris 连接失败，请检查服务器网络' } } });
    render(<DorisSettings />);
    await screen.findByDisplayValue('doris.test');
    fireEvent.click(screen.getByRole('button', { name: '测试连接' }));
    await screen.findByText(/Doris 连接失败，请检查服务器网络/);
    expect(screen.getByLabelText('Doris 主机地址')).toHaveValue('doris.test');
    expect(mocks.save).not.toHaveBeenCalled();
  });
});
