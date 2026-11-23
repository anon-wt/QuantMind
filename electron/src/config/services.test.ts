import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const STORAGE_KEY = 'quantmind_server_url_v2';
const SERVER = 'http://10.138.60.228:8000';

describe('service addresses in Web and Electron', () => {
  beforeEach(() => {
    vi.resetModules();
    localStorage.clear();
    delete (window as any).electronAPI;
    vi.stubEnv('VITE_API_BASE_URL', 'http://127.0.0.1:8000');
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true }));
  });

  afterEach(() => {
    delete (window as any).electronAPI;
    localStorage.clear();
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
    vi.restoreAllMocks();
  });

  it('uses the Web proxy with the real compatibility shim and a cached localhost address', async () => {
    localStorage.setItem(STORAGE_KEY, 'http://127.0.0.1:8000');
    await import('../utils/electronCompat');
    const services = await import('./services');

    expect((window as any).electronAPI).toBeDefined();
    expect(services.isElectronEnv()).toBe(false);
    for (const endpoint of Object.values(services.SERVICE_ENDPOINTS)) {
      expect(endpoint).toBe('/api/v1');
    }
    expect(services.resolveWebSafeServiceBase('http://127.0.0.1:8000', '/api/v1')).toBe('/api/v1');
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    expect(services.SERVICE_URLS.WEBSOCKET_MARKET).toBe(`${protocol}//${window.location.host}/ws/api/v1/ws/market`);
  });

  it('skips desktop address probing in Web and preserves the saved preference', async () => {
    localStorage.setItem(STORAGE_KEY, SERVER);
    await import('../utils/electronCompat');
    const services = await import('./services');

    await services.initDynamicServerUrl();

    expect(fetch).not.toHaveBeenCalled();
    expect(localStorage.getItem(STORAGE_KEY)).toBe(SERVER);
    expect(services.SERVICE_ENDPOINTS.API_GATEWAY).toBe('/api/v1');
  });

  it('treats an explicit false Electron marker as Web', async () => {
    (window as any).electronAPI = { isElectron: () => false };
    const services = await import('./services');
    expect(services.isElectronEnv()).toBe(false);
    expect(services.SERVICE_ENDPOINTS.API_GATEWAY).toBe('/api/v1');
  });

  it('restores and probes the real desktop preload server address', async () => {
    (window as any).electronAPI = {
      isElectron: () => true,
      getServerUrl: vi.fn().mockResolvedValue(SERVER),
    };
    const services = await import('./services');

    await services.initDynamicServerUrl();

    expect(services.isElectronEnv()).toBe(true);
    expect(fetch).toHaveBeenCalledWith(`${SERVER}/health`, expect.any(Object));
    expect(services.SERVICE_ENDPOINTS.API_GATEWAY).toBe(`${SERVER}/api/v1`);
    expect(services.SERVICE_URLS.WEBSOCKET_MARKET).toBe(`${SERVER.replace('http:', 'ws:')}/api/v1/ws/market`);
    expect(localStorage.getItem(STORAGE_KEY)).toBe(SERVER);
  });

  it('preserves an unreachable desktop preference for the next retry', async () => {
    localStorage.setItem(STORAGE_KEY, SERVER);
    (window as any).electronAPI = {
      isElectron: () => true,
      getServerUrl: vi.fn().mockResolvedValue(SERVER),
    };
    vi.mocked(fetch).mockResolvedValue({ ok: false } as Response);
    const services = await import('./services');

    await services.initDynamicServerUrl();

    expect(services.getDynamicServerUrl()).toBe(SERVER);
    expect(localStorage.getItem(STORAGE_KEY)).toBe(SERVER);
  });
});
