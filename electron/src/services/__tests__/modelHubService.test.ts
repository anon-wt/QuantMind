import { beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({
  directGet: vi.fn(),
  gatewayGet: vi.fn(),
  getAccessToken: vi.fn(),
  gateway: '/api/v1',
}));

vi.mock('axios', () => ({
  default: {
    create: () => ({ get: mocks.directGet }),
    get: mocks.gatewayGet,
  },
}));

vi.mock('../../features/auth/services/authService', () => ({
  authService: { getAccessToken: mocks.getAccessToken },
}));

vi.mock('../../config/services', () => ({
  SERVICE_ENDPOINTS: { get API_GATEWAY() { return mocks.gateway; } },
}));

import { modelHubService } from '../modelHubService';

const response = { total: 0, page: 1, page_size: 12, items: [] };

describe('model hub list gateway fallback', () => {
  beforeEach(() => {
    vi.resetAllMocks();
    mocks.gateway = '/api/v1';
    mocks.getAccessToken.mockReturnValue('session-token');
  });

  it('returns the direct response without making a gateway request', async () => {
    mocks.directGet.mockResolvedValue({ data: response });

    await expect(modelHubService.listModels({ page_size: 12 })).resolves.toEqual(response);

    expect(mocks.gatewayGet).not.toHaveBeenCalled();
  });

  it.each(['/api/v1', 'http://server:8000/api/v1'])(
    'falls back through %s with one API prefix and unchanged upstream filters',
    async (gateway) => {
      mocks.gateway = gateway;
      mocks.directGet.mockRejectedValue(new Error('Network Error'));
      mocks.gatewayGet.mockResolvedValue({ data: response });

      await expect(modelHubService.listModels({
        market: 'ALL', algorithm: 'ALL', query: 'momentum', page: 2, page_size: 12,
      })).resolves.toEqual(response);

      expect(mocks.gatewayGet).toHaveBeenCalledWith(`${gateway}/hub/models`, {
        params: {
          market: undefined, algorithm: undefined, sort_by: 'sharpe', q: 'momentum',
          author: undefined, page: 2, page_size: 12,
        },
        headers: { Authorization: 'Bearer session-token' },
        timeout: 30000,
      });
      expect(mocks.gatewayGet.mock.calls[0][1].params).toEqual(mocks.directGet.mock.calls[0][1].params);
    },
  );

  it('exposes the actionable gateway error instead of the original CORS failure', async () => {
    mocks.directGet.mockRejectedValue(new Error('Network Error'));
    const detail = 'QUANTDB_API_KEY 未配置，请在个人中心配置 API Key。';
    mocks.gatewayGet.mockRejectedValue({ response: { status: 400, data: { detail } } });

    await expect(modelHubService.listModels({})).rejects.toThrow(detail);
  });

  it('explains an upstream connection failure without displaying an HTML response', async () => {
    mocks.directGet.mockRejectedValue(new Error('Network Error'));
    mocks.gatewayGet.mockRejectedValue({ response: { status: 502, data: '模型广场服务不可达' } });

    await expect(modelHubService.listModels({})).rejects.toThrow('后端无法连接云端模型广场');

    mocks.gatewayGet.mockRejectedValue({ response: { status: 502, data: '<html>Bad Gateway</html>' } });
    await expect(modelHubService.listModels({})).rejects.toThrow('HTTP 502');
  });

  it('reports unavailable connections and omits an empty authorization header', async () => {
    mocks.getAccessToken.mockReturnValue(null);
    mocks.directGet.mockRejectedValue(new Error('Network Error'));
    mocks.gatewayGet.mockRejectedValue(new Error('Network Error'));

    await expect(modelHubService.listModels({})).rejects.toThrow('无法连接模型广场及本地网关');
    expect(mocks.gatewayGet.mock.calls[0][1].headers).toEqual({});
  });
});
