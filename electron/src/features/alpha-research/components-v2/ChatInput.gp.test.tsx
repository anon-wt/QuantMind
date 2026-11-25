import React from 'react';
import { Provider } from 'react-redux';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import store from '../../../store';
import { ChatInput } from './ChatInput';

const defaults = vi.hoisted(() => ({
  population_size: 48, generations: 8, top_k: 5, tournament_size: 3,
  elite_size: 2, max_depth: 4, max_nodes: 31, windows: [5, 10, 20],
  crossover_rate: 0.7, mutation_rate: 0.25, complexity_penalty: 0.0001,
  seed: 42, horizon: 1, min_names: 10, min_days: 40, dedup_corr: 0.95,
}));
vi.mock('../services-v2/api', () => ({ getUniverses: vi.fn().mockResolvedValue({ data: { universes: [] } }) }));
vi.mock('../services-v2/algorithmApi', () => ({
  getMiningAlgorithms: vi.fn().mockResolvedValue({ data: { available: true, algorithms: [
    { key: 'rdagent', name: 'RD-Agent' },
    { key: 'gp', name: 'GP 遗传编程', gp_defaults: defaults },
  ] } }),
}));
afterEach(cleanup);

it('requires dates and submits reviewed GP parameters without an LLM prompt', async () => {
  store.dispatch({ type: 'auth/login/fulfilled', payload: {
    require_mfa: false, user: { user_id: 'gp-input' }, token: 'test-token',
    refreshToken: null, tokenExpiryTime: null,
  } });
  const onSubmit = vi.fn();
  const onPreview = vi.fn().mockResolvedValue({
    algorithm: { key: 'gp', name: 'GP 遗传编程' }, gp_config: { ...defaults, seed: 7, horizon: 5 }, prediction_horizon: 5,
    window: { mode: 'server_default' }, datasets: {},
  });
  render(<Provider store={store}><ChatInput onSubmit={onSubmit} onPreview={onPreview} /></Provider>);
  fireEvent.click(await screen.findByRole('button', { name: 'GP 遗传编程' }));
  expect(screen.queryByRole('textbox')).toBeNull();
  const submit = screen.getByTitle('发送 (Enter)');
  expect(submit).toHaveProperty('disabled', true);
  fireEvent.change(screen.getByLabelText('GP挖掘开始日期'), { target: { value: '2023-01-01' } });
  fireEvent.change(screen.getByLabelText('GP挖掘结束日期'), { target: { value: '2023-12-31' } });
  fireEvent.change(screen.getByLabelText('GP 随机种子'), { target: { value: '7' } });
  fireEvent.change(screen.getByLabelText('统一预测周期'), { target: { value: '5' } });
  expect(submit).toHaveProperty('disabled', false);
  fireEvent.click(submit);
  await screen.findByRole('dialog');
  expect(onPreview).toHaveBeenCalledWith(expect.objectContaining({ algorithm: 'gp', predictionHorizon: 5, gpConfig: { ...defaults, seed: 7, horizon: 5 } }));
  expect(onSubmit).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: '确认并开始' }));
  await waitFor(() => expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({ algorithm: 'gp', predictionHorizon: 5, autoMaterialize: false, gpConfig: { ...defaults, seed: 7, horizon: 5 } })));
});
