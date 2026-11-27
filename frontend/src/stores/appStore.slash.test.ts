import { beforeEach, describe, expect, it, vi } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';
import { useAppStore } from './appStore';

describe('appStore.sendMessage slash handling', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  it('后端返回 slash=true 成功时清除运行态与空助手占位且保留已投递的 notice', async () => {
    const store = useAppStore();
    store.currentSessionName = 'sess-1';
    store._bindSessionRuntime('sess-1');

    const send_user_message = vi.fn(async () => {
      // 模拟后端在返回前通过 post_notice 回传结果
      store.notifyChatLocal('节点【笔记本】目录列表', 'notice', 'sess-1');
      return { ok: true, slash: true };
    });
    vi.stubGlobal('pywebview', { api: { send_user_message } });

    await store.sendMessage('/笔记本目录');

    expect(store.runningSessions).not.toContain('sess-1');
    expect(store.running).toBe(false);
    expect(store.messages.some(m => m.role === 'assistant')).toBe(false);
    expect(store.messages.some(m => m.role === 'notice' && String(m.content || '').includes('目录列表'))).toBe(true);
    vi.unstubAllGlobals();
  });

  it('草稿会话发斜杠命令失败时回退草稿态且不重复弹出消息发送失败', async () => {
    const store = useAppStore();
    store.currentSessionName = '';
    store.pendingWorkdir = 'GLOBAL';
    store._bindSessionRuntime('');

    const send_user_message = vi.fn(async (_text, targetSess) => {
      store.notifyChatLocal('未找到节点命令', 'error', targetSess);
      return { ok: false, msg: '未找到节点命令', slash: true };
    });
    vi.stubGlobal('pywebview', { api: { send_user_message } });

    await store.sendMessage('/笔记本未知');

    expect(store.currentSessionName).toBe('');
    expect(store.pendingWorkdir).toBe('GLOBAL');
    expect(store.running).toBe(false);
    const errors = store.messages.filter(m => m.role === 'error');
    expect(errors.length).toBe(1);
    expect(errors[0].content).toBe('未找到节点命令');
    vi.unstubAllGlobals();
  });

  it('草稿会话或异步 notice 未先行到达时仍由 slash 返回值兜底落地且不重复', async () => {
    const store = useAppStore();
    store.currentSessionName = '';
    store.pendingWorkdir = 'GLOBAL';
    store._bindSessionRuntime('');

    const send_user_message = vi.fn(async () => ({
      ok: true,
      slash: true,
      notice: '节点【笔记本】工作目录（共 1 个）',
    }));
    vi.stubGlobal('pywebview', { api: { send_user_message } });

    await store.sendMessage('/笔记本目录');
    // 模拟异步 notice 事件随后重复到达
    store.notifyChatLocal('节点【笔记本】工作目录（共 1 个）', 'notice');

    const notices = store.messages.filter(m => m.role === 'notice');
    expect(notices.length).toBe(1);
    expect(notices[0].content).toBe('节点【笔记本】工作目录（共 1 个）');
    vi.unstubAllGlobals();
  });
});
