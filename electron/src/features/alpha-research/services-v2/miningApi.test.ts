import { beforeEach, describe, expect, it, vi } from 'vitest';

const client = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
vi.mock('../../../services/aiStrategyClients', () => ({ apiClient: client }));

import { previewMining, startMining } from './api';

describe('factor mining API', () => {
  beforeEach(() => vi.clearAllMocks());

  it('defaults new mining requests to all A-shares', async () => {
    client.post.mockResolvedValue({ data: { code: 200, data: { algorithm: { key: 'rdagent' } } } });
    await previewMining({ direction: '价量假设', algorithm: 'rdagent' });
    expect(client.post.mock.calls[0][1]).toMatchObject({ universe: 'all_a' });
  });

  it('submits statistical mining as an idempotent JSON request with frozen scope', async () => {
    client.post.mockResolvedValue({
      data: {
        code: 200,
        data: { task_id: 'stat-task-1', status: 'queued', queue_position: 2 },
      },
    });

    const result = await startMining({
      direction: '',
      algorithm: 'statistical',
      miningStart: '2020-01-01',
      miningEnd: '2022-12-31',
      validStart: '2023-01-01',
      validEnd: '2023-12-31',
      market: 'a_share',
      universe: 'csi500',
      dataSource: 'parquet',
      freq: 'day',
    }, 'stat-request-key');

    expect(client.post).toHaveBeenCalledTimes(1);
    const [url, body, config] = client.post.mock.calls[0];
    expect(url).toBe('/alpha-agent/evolve');
    expect(body).toEqual({
      algorithm: 'statistical',
      prediction_horizon: 1,
      market: 'a_share',
      universe: 'csi500',
      data_source: 'qlib_bin',
      freq: 'day',
      mining_start: '2020-01-01',
      mining_end: '2022-12-31',
      valid_start: '2023-01-01',
      valid_end: '2023-12-31',
      auto_materialize: false,
    });
    expect(config.headers['Idempotency-Key']).toEqual(expect.any(String));
    expect(config.headers['Idempotency-Key'].length).toBeGreaterThan(0);
    expect(result.data?.taskId).toBe('stat-task-1');
  });

  it('submits the RD-Agent dataset selection in its durable request body', async () => {
    client.post.mockResolvedValue({
      data: { code: 200, data: { task_id: 'rd-task-1', status: 'queued' } },
    });
    await startMining({
      direction: '价量假设',
      algorithm: 'rdagent',
      market: 'a_share',
      universe: 'csi300',
      datasets: ['l1_factors'],
      freq: 'day',
      miningStart: '2020-01-01',
      miningEnd: '2022-12-31',
      validStart: '2023-01-01',
      validEnd: '2023-12-31',
    }, 'rd-request-key');
    const [url, body, config] = client.post.mock.calls[0];
    expect(url).toBe('/alpha-agent/evolve');
    expect(body).toMatchObject({
      algorithm: 'rdagent',
      freq: 'day',
      datasets: ['l1_factors'],
      mining_start: '2020-01-01',
      mining_end: '2022-12-31',
      valid_start: '2023-01-01',
      valid_end: '2023-12-31',
    });
    expect(config.headers['Idempotency-Key']).toEqual(expect.any(String));
  });

  it('previews and submits the same bounded GP configuration', async () => {
    client.post.mockResolvedValue({ data: { code: 200, data: { task_id: 'gp-1', status: 'queued' } } });
    const params = {
      direction: '', algorithm: 'gp' as const,
      predictionHorizon: 5,
      miningStart: '2023-01-01', miningEnd: '2023-12-31',
      gpConfig: { population_size: 16, generations: 3, seed: 7 },
    };
    await previewMining(params as Parameters<typeof previewMining>[0]);
    await startMining(params as Parameters<typeof startMining>[0], 'gp-request-key');
    expect(client.post.mock.calls[0][1]).toEqual(client.post.mock.calls[1][1]);
    expect(client.post.mock.calls[1][1]).toMatchObject({ algorithm: 'gp', prediction_horizon: 5, gp_config: params.gpConfig, auto_materialize: false });
    expect(client.post.mock.calls[1][2].headers['Idempotency-Key']).toEqual(expect.any(String));
  });

  it('previews the exact mining request body without an idempotency or submit side effect', async () => {
    client.post.mockResolvedValue({ data: { code: 200, data: { algorithm: { key: 'rdagent' } } } });
    await previewMining({
      direction: '价量假设',
      algorithm: 'rdagent',
      market: 'a_share',
      universe: 'csi500',
      datasets: ['l1_factors'],
      freq: 'day',
      maxRounds: 4,
      miningStart: '2020-01-01',
      miningEnd: '2022-12-31',
      validStart: '2023-01-01',
      validEnd: '2023-12-31',
    });
    expect(client.post).toHaveBeenCalledTimes(1);
    const [url, body, options] = client.post.mock.calls[0];
    expect(url).toBe('/alpha-agent/preview');
    expect(body).toMatchObject({
      algorithm: 'rdagent',
      market: 'a_share',
      universe: 'csi500',
      loop_n: 4,
      datasets: ['l1_factors'],
      mining_start: '2020-01-01',
      valid_end: '2023-12-31',
    });
    expect(options).toBeUndefined();
  });
});

import { DEFAULT_QUALITY_CONFIG } from '../components-v2/QualityPanel';
it.each(['rdagent', 'statistical', 'gp'] as const)('freezes user quality thresholds for %s', async algorithm => {
  client.post.mockResolvedValue({data:{data:{algorithm:{key:algorithm}}}});
  const qualityConfig = {...DEFAULT_QUALITY_CONFIG, min_abs_rank_ic:0.04, min_abs_t:4};
  await previewMining({direction:'test', algorithm, qualityConfig});
  expect(client.post.mock.calls.at(-1)?.[1]).toMatchObject({quality_config:qualityConfig});
});
