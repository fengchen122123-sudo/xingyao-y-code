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
function loadShim(search: string, storage: Record<string, string> = {}): FakeWs {
  window.history.replaceState({}, '', '/m/' + search);
  window.localStorage.clear();
  for (const [k, v] of Object.entries(storage)) window.localStorage.setItem(k, v);
  delete (window as any).pywebview;

  const sockets: FakeWs[] = [];
  class FakeWebSocket {
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
  const orig = (window as any).WebSocket;
  (window as any).WebSocket = FakeWebSocket;
  (0, eval)(fs.readFileSync(SHIM_PATH, 'utf8'));
  (window as any).WebSocket = orig;
  return sockets[0];
}

describe('mobile-shim：token 持久化 + 4401 配对失效', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    window.localStorage.clear();
    window.history.replaceState({}, '', '/m/');
    delete (window as any).pywebview;
  });
  afterEach(() => {
    vi.clearAllTimers();
    vi.useRealTimers();
  });

  it('首次带 URL token：写入 localStorage 并用它鉴权', () => {
    const ws = loadShim('?token=ABC');
    expect(window.localStorage.getItem(STORAGE_KEY)).toBe('ABC');
    ws.onopen?.();
    expect(JSON.parse(ws.sent[0]).token).toBe('ABC');
  });

  it('URL 带 token：写入 localStorage 并从地址栏清理', () => {
    const ws = loadShim('?token=ABC');
    // token 已落 localStorage
    expect(window.localStorage.getItem(STORAGE_KEY)).toBe('ABC');
    // 地址栏 query 已清理、path 保留
    expect(window.location.search).toBe('');
    expect(window.location.pathname).toBe('/m/');
    ws.onopen?.();
    expect(JSON.parse(ws.sent[0]).token).toBe('ABC');
  });

  it('URL 无 token：回退用 localStorage 里的 token', () => {
    const ws = loadShim('', { [STORAGE_KEY]: 'XYZ' });
    ws.onopen?.();
    expect(JSON.parse(ws.sent[0]).token).toBe('XYZ');
  });

  it('收到 4401：清除本地 token、派发 yc-pair-invalid、停止重连', () => {
    const ws = loadShim('?token=ABC');
    let fired = false;
    window.addEventListener('yc-pair-invalid', () => { fired = true; }, { once: true });
    ws.onclose?.({ code: 4401 });
    expect(window.localStorage.getItem(STORAGE_KEY)).toBeNull();
    expect(fired).toBe(true);
    // 不重连：推进定时器后不应再有新的 WebSocket
    vi.advanceTimersByTime(5000);
  });

  it('普通断线（非 4401）：保留 token 并安排重连', () => {
    loadShim('?token=ABC');
    const first = (window as any).WebSocket;
    // 触发一次普通断线
    const ws = loadShim('?token=ABC');
    ws.onclose?.({ code: 1006 });
    expect(window.localStorage.getItem(STORAGE_KEY)).toBe('ABC');
    expect(first).toBeTruthy();
  });
});
