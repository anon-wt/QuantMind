import React from 'react';
import { act, fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { QuantDBDataset } from '../../services/dataPlatformService';
import { QuantDBCatalogPanel } from './QuantDBCatalogPanel';

const mocks = vi.hoisted(() => ({ catalog: vi.fn(), jobs: vi.fn() }));
vi.mock('../../services/dataPlatformService', () => ({
    dataPlatformService: {
        getQuantDBCatalog: mocks.catalog,
        listQuantDBSyncJobs: mocks.jobs,
    },
}));

const mapped: QuantDBDataset = {
    dataset: 'daily_unadjusted',
    name: '日线不复权',
    provider: 'doris',
    available: true,
    group: 'market',
    category_id: '1',
    layout: 'partition',
    rel_dir: '1_kline_data/daily_unadjusted',
    note: '',
    synced: false,
    files: 0,
    size_mb: 0,
};

const dorisCatalog = {
    provider: 'doris',
    data_dir: '/data/doris',
    groups: [
        { id: 'market', name: '行情数据', size_mb: 60 },
        { id: 'ml', name: 'ML数据集', size_mb: 0 },
    ],
    datasets: [
        mapped,
        { ...mapped, dataset: 'daily_forward', name: '日线前复权', available: false, synced: true, size_mb: 60 },
        { ...mapped, dataset: 'l1_factors', name: 'L1 因子', group: 'ml', available: false },
    ],
};

describe('Doris data update catalog', () => {
    beforeEach(() => {
        mocks.catalog.mockReset();
        mocks.jobs.mockResolvedValue({ jobs: [] });
    });

    it('shows mapped datasets and their paths, dropping empty groups and unmapped rows', async () => {
        mocks.catalog.mockResolvedValue(dorisCatalog);
        render(<QuantDBCatalogPanel connected onPreview={() => {}} embedded />);

        expect(await screen.findByText('/data/doris')).toBeInTheDocument();
        expect(screen.queryByText('ML数据集')).toBeNull();
        expect(screen.getByText('0/1 已同步')).toBeInTheDocument();
        expect(screen.getByRole('button', { name: /同步选中的 1 个数据集/ })).toBeEnabled();

        fireEvent.click(screen.getByRole('button', { name: /行情数据/ }));
        expect(await screen.findByText('1_kline_data/daily_unadjusted')).toBeInTheDocument();
        expect(screen.queryByText('日线前复权')).toBeNull();
    });

    it('keeps the Doris catalog when an earlier QuantDB response arrives after switching', async () => {
        let finishQuantDB!: (catalog: unknown) => void;
        mocks.catalog
            .mockImplementationOnce(() => new Promise((resolve) => { finishQuantDB = resolve; }))
            .mockResolvedValueOnce(dorisCatalog);
        const onPreview = vi.fn();
        const { rerender } = render(<QuantDBCatalogPanel connected onPreview={onPreview} embedded />);
        rerender(<QuantDBCatalogPanel connected onPreview={onPreview} refreshSignal={1} embedded />);

        expect(await screen.findByText('/data/doris')).toBeInTheDocument();
        await act(async () => finishQuantDB({ ...dorisCatalog, provider: 'quantdb', data_dir: '/data/quantdb' }));
        expect(screen.getByText('/data/doris')).toBeInTheDocument();
        expect(screen.queryByText('/data/quantdb')).toBeNull();
    });
});
