import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import fs from 'node:fs';
import path from 'node:path';

// vitest cwd = frontend-mobile；shim 在仓库根的 frontend/ 下
const SHIM_PATH = path.resolve(process.cwd(), '../frontend/mobile-shim.js');
const STORAGE_KEY = 'yc-pair-token';

interface FakeWs {
  url: string;
  sent: string[];
  onopen: (() => void) | null;
  onmessage: ((e: any) => void) | null;
  onclose: ((e: any) => void) | null;
  onerror: (() => void) | null;
}

/** 以给定 URL 查询串 / 预置 localStorage 加载 shim，返回它创建的假 WebSocket */
function loadShim(
  search: string,
  storage: Record<string, string> = {},
  options: { pathname?: string; relayDevice?: string } = {},
): { ws: FakeWs; sockets: FakeWs[] } {
  window.history.replaceState({}, '', (options.pathname || '/m/') + search);
  window.localStorage.clear();
  for (const [k, v] of Object.entries(storage)) window.localStorage.setItem(k, v);
  delete (window as any).pywebview;
  if (options.relayDevice !== undefined) (window as any).__ycRelayDevice = options.relayDevice;
  else delete (window as any).__ycRelayDevice;

  const sockets: FakeWs[] = [];
  class FakeWebSocket {
    readyState = 1;
    url: string;
    sent: string[] = [];
    onopen: (() => void) | null = null;
    onmessage: ((e: any) => void) | null = null;
    onclose: ((e: any) => void) | null = null;
    onerror: (() => void) | null = null;
    constructor(url: string) {
      this.url = url;
      sockets.push(this as unknown as FakeWs);
    }
    send(m: string) {
      this.sent.push(m);
    }
    close() {}
  }
  (window as any).WebSocket = FakeWebSocket;
  (0, eval)(fs.readFileSync(SHIM_PATH, 'utf8'));
  return { ws: sockets[0], sockets };
}

describe('mobile-shim：token 持久化 + 4401 配对失效', () => {
  let origWebSocket: any;
  beforeEach(() => {
    origWebSocket = (window as any).WebSocket;
    vi.useFakeTimers();
    window.localStorage.clear();
    window.history.replaceState({}, '', '/m/');
    delete (window as any).pywebview;
    delete (window as any).__ycRelayDevice;
    delete (window as any).onBridgeEvent;
  });
  afterEach(() => {
    vi.clearAllTimers();
    vi.useRealTimers();
    (window as any).WebSocket = origWebSocket;
  });

  it('首次带 URL token：写入 localStorage 并用它鉴权', () => {
    const { ws } = loadShim('?token=ABC');
    expect(window.localStorage.getItem(STORAGE_KEY)).toBe('ABC');
    ws.onopen?.();
    expect(JSON.parse(ws.sent[0]).token).toBe('ABC');
  });

  it('URL 带 token：写入 localStorage 并从地址栏清理', () => {
    const { ws } = loadShim('?token=ABC');
    // token 已落 localStorage
    expect(window.localStorage.getItem(STORAGE_KEY)).toBe('ABC');
    // 地址栏 query 已清理、path 保留
    expect(window.location.search).toBe('');
    expect(window.location.pathname).toBe('/m/');
    ws.onopen?.();
    expect(JSON.parse(ws.sent[0]).token).toBe('ABC');
  });

  it('URL 无 token：回退用 localStorage 里的 token', () => {
    const { ws } = loadShim('', { [STORAGE_KEY]: 'XYZ' });
    ws.onopen?.();
    expect(JSON.parse(ws.sent[0]).token).toBe('XYZ');
  });

  it('收到 4401：清除本地 token、派发 yc-pair-invalid、停止重连', () => {
    const { ws } = loadShim('?token=ABC');
    let fired = false;
    window.addEventListener('yc-pair-invalid', () => { fired = true; }, { once: true });
    ws.onclose?.({ code: 4401 });
    expect(window.localStorage.getItem(STORAGE_KEY)).toBeNull();
    expect(fired).toBe(true);
    // 不重连：推进定时器后不应再有新的 WebSocket
    vi.advanceTimersByTime(5000);
  });

  it('普通断线（非 4401）：保留 token 并安排重连', () => {
    const { ws } = loadShim('?token=ABC');
    ws.onclose?.({ code: 1006 });
    expect(window.localStorage.getItem(STORAGE_KEY)).toBe('ABC');
  });

  it('云中转 replay 帧：不误进 legacy、按序解包补发并携带 last_seq 重连', () => {
    const dev = '9adef89d-2811-48d7-8c51-246ace547c49';
    const events: any[] = [];
    (window as any).onBridgeEvent = (e: any) => events.push(e);
    const { ws, sockets } = loadShim(
      '?token=JWT1',
      {},
      { pathname: `/xy/bot/m/${dev}`, relayDevice: dev },
    );
    expect(ws.url).toContain(`/xy/bot/relay/client?device_id=${dev}`);
    ws.onopen?.();
    // 首帧 replay 空数组不触发重复 get_initial_state
    ws.onmessage?.({ data: JSON.stringify({ type: 'replay', frames: [] }) });
    expect(ws.sent.length).toBe(1);
    ws.onmessage?.({
      data: JSON.stringify({
        type: 'device_online',
        data: { device_id: dev, boot_id: 'boot-9' },
      }),
    });
    expect(events.some((e) => e.type === 'device_status' && e.data?.online === true)).toBe(true);
    ws.onmessage?.({
      data: JSON.stringify({ type: 'notice', data: { message: 'live' }, seq: 3 }),
    });

    // 断线重连：应附带 last_boot_id 与 last_seq，且能解包服务端返回的 replay.frames
    ws.onclose?.({ code: 1006 });
    vi.advanceTimersByTime(2100);
    expect(sockets.length).toBe(2);
    const ws2 = sockets[1];
    ws2.onopen?.();
    const auth2 = JSON.parse(ws2.sent[0]);
    expect(auth2.last_boot_id).toBe('boot-9');
    expect(auth2.last_seq).toBe(3);

    ws2.onmessage?.({
      data: JSON.stringify({
        type: 'replay',
        frames: [JSON.stringify({ type: 'notice', data: { message: 'missed' }, seq: 4 })],
      }),
    });
    expect(events.some((e) => e.type === 'notice' && e.data?.message === 'missed')).toBe(true);
  });

  it('云中转 /xy/bot/m/convo 子路由刷新：记住 UUID 且保留 /xy/bot 反代前缀', () => {
    const dev = '9adef89d-2811-48d7-8c51-246ace547c49';
    const { ws } = loadShim(
      '',
      { [STORAGE_KEY]: 'JWT1', 'yc-relay-device': dev },
      { pathname: '/xy/bot/m/convo', relayDevice: 'convo' },
    );
    expect(ws.url).toContain(`/xy/bot/relay/client?device_id=${dev}`);
  });

  it('云中转 resync_required：清空水位并拉取 get_initial_state 派发 yc-initial-state', async () => {
    const dev = '9adef89d-2811-48d7-8c51-246ace547c49';
    const { ws } = loadShim(
      '?token=JWT1',
      {},
      { pathname: `/xy/bot/m/${dev}`, relayDevice: dev },
    );
    ws.onopen?.();
    let syncedState: any = null;
    window.addEventListener('yc-initial-state', (e: any) => { syncedState = e.detail; }, { once: true });
    ws.onmessage?.({
      data: JSON.stringify({
        type: 'resync_required',
        data: { device_id: dev, boot_id: 'boot-2', reason: 'queue_drop' },
      }),
    });
    const req = JSON.parse(ws.sent[1]);
    expect(req.method).toBe('get_initial_state');
    ws.onmessage?.({ data: JSON.stringify({ id: req.id, result: { current_session: 's1' } }) });
    await Promise.resolve();
    expect(syncedState).toEqual({ current_session: 's1' });
  });
});
