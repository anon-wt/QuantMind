import React from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import { expect, it, vi } from 'vitest';

const poolApi = vi.hoisted(() => ({
  listFactorPool: vi.fn(async () => []),
  listFactorPoolEdges: vi.fn(async () => []),
  rebuildFactorPoolEdges: vi.fn(async () => ({ factor_count: 0, edge_count: 0 })),
  rebuildFactorPoolValueEdges: vi.fn(async () => ({ panel_count: 0, edge_count: 0 })),
}));

vi.mock('../services-v2/factorJobsApi', () => poolApi);
vi.mock('../services-v2/api', () => ({
  getFactors: vi.fn(async () => ({ success: true, data: { factors: [], total: 0 } })),
  getFactoryFactors: vi.fn(async () => null),
  getUniverses: vi.fn(async () => ({ success: true, data: [] })),
  UNIVERSE_LABELS: {},
}));
vi.mock('../../../store', () => ({
  useAppSelector: (selector: (state: unknown) => unknown) =>
    selector({ auth: { isAuthenticated: true, isLoading: false } }),
}));

import { FactorLibraryPage } from './FactorLibraryPage';

it.each([
  ['重算公式相似', '当前范围有 0 个本人池因子'],
  ['重算值相关', '当前范围有 0 个可用面板'],
])('shows progress and a visible empty result for %s', async (button, emptyResult) => {
  render(<FactorLibraryPage />);

  fireEvent.click(screen.getByRole('button', { name: button }));
  expect(screen.getByRole('status')).toHaveTextContent('正在重算本人因子池');

  const result = await screen.findByText(new RegExp(emptyResult));
  expect(result).toHaveAttribute('role', 'status');
  expect(result).toHaveTextContent('重算完成');
  expect(result).toHaveTextContent('同一股票池的 2 个因子');
});
