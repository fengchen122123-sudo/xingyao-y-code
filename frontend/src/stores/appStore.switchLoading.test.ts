import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, expect, it, vi } from 'vitest';
import { useAppStore } from './appStore';

// 会话切换加载态（sessionSwitching）：区分"目标会话历史还在后端路上（消息区暂空）"
// 与"真正的新对话/空会话"，避免切换瞬间闪出首屏空状态卡片。
// mock 方式与 appStore.historyPaged.test.ts 一致：直接挂 window.pywebview.api。

beforeEach(() => {
  setActivePinia(createPinia());
  (window as any).pywebview = undefined;
});

it('切到无内存缓存的会话：请求在途时为加载中，历史返回后复位并填充消息', async () => {
  const store = useAppStore();
  // 用未 resolve 的 Promise 卡住 switch_session，模拟历史还在后端返回途中
  let resolveSwitch: ((v: any) => void) | null = null;
  const switchSession = vi.fn().mockImplementation(
    () => new Promise(resolve => { resolveSwitch = resolve; }),
  );
  (window as any).pywebview = { api: { switch_session: switchSession } };

  const p = store.switchSession('s1', '会话1');

  // 目标会话本地无历史（runtime 为空）⇒ 处于加载态，而不是"新对话"
  expect(store.sessionSwitching).toBe(true);
  expect(store.messages).toHaveLength(0);

  resolveSwitch!({ messages: [{ role: 'user', content: '历史' }] });
  await p;

  // 历史已填充 ⇒ 加载态复位，界面不会再显示空状态卡片
  expect(store.sessionSwitching).toBe(false);
  expect(store.messages.map(m => m.content)).toEqual(['历史']);
});

it('目标会话已有内存消息时不误报加载中', async () => {
  const store = useAppStore();
  // 预置该会话的内存运行时（消息已在本地，无需从后端取回）
  store.sessionRuntimeByName['s1'] = {
    messages: [{ role: 'user', content: '缓存消息' }],
    streamingText: '', thinkingText: '', running: false, subagentTasks: [],
  } as any;
  let resolveSwitch: ((v: any) => void) | null = null;
  const switchSession = vi.fn().mockImplementation(
    () => new Promise(resolve => { resolveSwitch = resolve; }),
  );
  (window as any).pywebview = { api: { switch_session: switchSession } };

  const p = store.switchSession('s1', '会话1');

  // 缓存命中：切到该会话时消息区立刻有内容，不应被判定为"加载中"
  expect(store.messages.map(m => m.content)).toEqual(['缓存消息']);
  expect(store.sessionSwitching).toBe(false);

  resolveSwitch!({ messages: [{ role: 'user', content: '缓存消息' }] });
  await p;

  expect(store.sessionSwitching).toBe(false);
});

it('竞态：过期响应的 finally 不清掉当前会话的加载态', async () => {
  const store = useAppStore();
  // 每个会话一个手工 resolve 的挂起请求，模拟乱序返回
  const pending: Record<string, (v: any) => void> = {};
  const switchSession = vi.fn().mockImplementation(
    (name: string) => new Promise(resolve => { pending[name] = resolve; }),
  );
  (window as any).pywebview = { api: { switch_session: switchSession } };

  const pA = store.switchSession('A', '会话A');
  expect(store.sessionSwitching).toBe(true);
  // 快速连点：A 还没回来就切到 B
  const pB = store.switchSession('B', '会话B');
  expect(store.currentSessionName).toBe('B');
  expect(store.sessionSwitching).toBe(true);

  // B 的响应先返回：加载态按当前会话 B 复位
  pending['B']({ messages: [{ role: 'user', content: 'B 的历史' }] });
  await pB;
  expect(store.sessionSwitching).toBe(false);

  // A 的旧响应后到：过期响应不得清当前会话（B）的加载态，也不能改高亮
  pending['A']({ messages: [{ role: 'user', content: 'A 的历史' }] });
  await pA;
  expect(store.sessionSwitching).toBe(false);
  expect(store.currentSessionName).toBe('B');
  expect(store.messages.map(m => m.content)).toEqual(['B 的历史']);
});

it('createGlobalSession：新建会话不是加载态，清掉可能残留的 sessionSwitching', async () => {
  const store = useAppStore();
  // 模拟上一步切换留下的加载态残留（如后端异常路径）
  store.sessionSwitching = true;

  await store.createGlobalSession();

  // 空白 pending 新会话是"真正的新对话"，不能显示加载态
  expect(store.sessionSwitching).toBe(false);
  expect(store.currentSessionName).toBe('');
});

it('后端 API 不可用时切换会话不残留加载态', async () => {
  const store = useAppStore();
  // 没有 window.pywebview.api：switchSession 会在 _api() 处直接返回
  await expect(store.switchSession('s1', '会话1')).resolves.toBeUndefined();
  expect(store.sessionSwitching).toBe(false);
});
