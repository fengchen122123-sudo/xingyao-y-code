import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { useAppStore } from './appStore';
import type { McpInfo } from '../types/pywebview';

// MCP 服务删除：后端 remove_mcp_server 返回 { ok, msg, mcps }，
// 前端据此刷新列表并给出明确提示；失败与异常都不能把界面带崩。

const mcpsAfter: McpInfo[] = [
  { name: 'filesystem', enabled: true, connected: true, tools_count: 3, last_error: null },
];

function mockBridgeApi(overrides: Record<string, unknown> = {}) {
  (window as any).pywebview = {
    api: {
      remove_mcp_server: vi.fn(async () => ({ ok: true, msg: '已删除 MCP 服务: github', mcps: mcpsAfter })),
      ...overrides,
    },
  };
}

beforeEach(() => {
  setActivePinia(createPinia());
  mockBridgeApi();
});

describe('removeMcpServer', () => {
  it('删除成功时用后端回传的 mcps 覆盖本地列表并提示', async () => {
    const store = useAppStore();
    store.mcps = [
      { name: 'github', enabled: true, connected: false, tools_count: 0, last_error: 'spawn 失败' },
      { name: 'filesystem', enabled: true, connected: true, tools_count: 3, last_error: null },
    ];

    await store.removeMcpServer('github');

    expect((window as any).pywebview.api.remove_mcp_server).toHaveBeenCalledWith('github');
    expect(store.mcps.map(m => m.name)).toEqual(['filesystem']);
    expect(store.confirmModal.show).toBe(true);
    expect(store.confirmModal.notice).toBe(true);
    expect(store.confirmModal.title).toBe('MCP 服务');
    expect(store.confirmModal.message).toBe('已删除 MCP 服务: github');
  });

  it('成功但后端未给 msg 时使用默认文案', async () => {
    mockBridgeApi({
      remove_mcp_server: vi.fn(async () => ({ ok: true, mcps: [] })),
    });
    const store = useAppStore();
    store.mcps = [{ name: 'github', enabled: true, connected: false, tools_count: 0 }];

    await store.removeMcpServer('github');

    expect(store.mcps).toEqual([]);
    expect(store.confirmModal.message).toBe('已删除');
  });

  it('后端返回 ok:false 时保留原列表并给出失败提示', async () => {
    mockBridgeApi({
      remove_mcp_server: vi.fn(async () => ({ ok: false, msg: '未找到该服务' })),
    });
    const store = useAppStore();
    const before: McpInfo[] = [{ name: 'github', enabled: true, connected: false, tools_count: 0 }];
    store.mcps = before;

    await store.removeMcpServer('github');

    expect(store.mcps).toEqual(before);
    expect(store.confirmModal.title).toBe('MCP 服务');
    expect(store.confirmModal.message).toBe('删除失败: 未找到该服务');
  });

  it('后端返回 ok:false 且无 msg 时给出未知错误提示', async () => {
    mockBridgeApi({
      remove_mcp_server: vi.fn(async () => ({ ok: false })),
    });
    const store = useAppStore();

    await store.removeMcpServer('github');

    expect(store.confirmModal.message).toBe('删除失败: 未知错误');
  });

  it('api 抛异常时不崩溃，标记断开并提示异常原因', async () => {
    mockBridgeApi({
      remove_mcp_server: vi.fn(async () => { throw new Error('bridge closed'); }),
    });
    const store = useAppStore();
    store.connected = true;

    await expect(store.removeMcpServer('github')).resolves.toBeUndefined();

    expect(store.connected).toBe(false);
    expect(store.confirmModal.title).toBe('MCP 服务');
    expect(store.confirmModal.message).toContain('删除 MCP 服务失败');
    expect(store.confirmModal.message).toContain('bridge closed');
  });

  it('桥接不可用时安全返回，不抛异常', async () => {
    (window as any).pywebview = undefined;
    const store = useAppStore();

    await expect(store.removeMcpServer('github')).resolves.toBeUndefined();

    expect(store.connected).toBe(false);
    expect(store.confirmModal.title).toBe('连接失败');
  });
});

describe('mcps 列表持有 last_error', () => {
  it('带 last_error 的服务数据可原样持有（旧后端不返回该字段时同样正常）', () => {
    const store = useAppStore();

    store.mcps = [
      { name: 'github', enabled: true, connected: false, tools_count: 0, last_error: '启动命令只支持 npx / node / python 裸名' },
      { name: 'filesystem', enabled: false, connected: false, tools_count: 0 },
    ];

    expect(store.mcps[0].last_error).toBe('启动命令只支持 npx / node / python 裸名');
    expect(store.mcps[1].last_error).toBeUndefined();
  });

  it('删除成功后 mcps 中的 last_error 字段保留', async () => {
    mockBridgeApi({
      remove_mcp_server: vi.fn(async () => ({
        ok: true,
        msg: '已删除',
        mcps: [
          { name: 'github', enabled: true, connected: false, tools_count: 0, last_error: '连接超时' },
          { name: 'filesystem', enabled: true, connected: true, tools_count: 3, last_error: null },
        ],
      })),
    });
    const store = useAppStore();

    await store.removeMcpServer('other');

    expect(store.mcps).toHaveLength(2);
    expect(store.mcps[0].last_error).toBe('连接超时');
    expect(store.mcps[1].last_error).toBeNull();
  });
});
