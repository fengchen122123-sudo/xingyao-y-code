import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, expect, it } from 'vitest';
import { useAppStore } from './appStore';

beforeEach(() => setActivePinia(createPinia()));

function makeEvent(type: string, sessionName: string) {
  return { type, data: { session_name: sessionName } } as any;
}

it('非当前窗口完成/待确认/报错时打点，进入会话后清除', () => {
  const store = useAppStore();
  store.currentSessionName = 'session-a';

  // B 完成 → 绿点
  store.handleBridgeEvent(makeEvent('turn_finish', 'session-b'));
  expect(store.sessionStatusDots['session-b']).toBe('green');

  // C 等待确认 → 黄点
  store.handleBridgeEvent(makeEvent('permission_request', 'session-c'));
  expect(store.sessionStatusDots['session-c']).toBe('yellow');

  // D 报错 → 红点
  store.handleBridgeEvent(makeEvent('turn_error', 'session-d'));
  expect(store.sessionStatusDots['session-d']).toBe('red');
});

it('当前窗口的事件不打点', () => {
  const store = useAppStore();
  store.currentSessionName = 'session-a';

  store.handleBridgeEvent(makeEvent('turn_finish', 'session-a'));
  expect('session-a' in store.sessionStatusDots).toBe(false);

  store.handleBridgeEvent(makeEvent('permission_request', 'session-a'));
  expect('session-a' in store.sessionStatusDots).toBe(false);

  store.handleBridgeEvent(makeEvent('turn_error', 'session-a'));
  expect('session-a' in store.sessionStatusDots).toBe(false);
});

it('turn_cancelled（手动停止）不打点', () => {
  const store = useAppStore();
  store.currentSessionName = 'session-a';

  store.handleBridgeEvent(makeEvent('turn_cancelled', 'session-b'));
  expect('session-b' in store.sessionStatusDots).toBe(false);
});

it('会话重命名时状态点 key 随迁移', () => {
  const store = useAppStore();
  store.currentSessionName = 'session-a';

  store.handleBridgeEvent(makeEvent('turn_finish', 'session-b'));
  expect(store.sessionStatusDots['session-b']).toBe('green');

  store.handleBridgeEvent({
    type: 'session_renamed',
    data: { old: 'session-b', new: 'session-b2' },
  } as any);
  expect(store.sessionStatusDots['session-b2']).toBe('green');
  expect('session-b' in store.sessionStatusDots).toBe(false);
});

it('端到端：用户在 A，B 跑完出绿点，切到 B 点消失，回 A 后 C 报错出红点', () => {
  const store = useAppStore();
  store.currentSessionName = 'session-a';

  // A/B/C 都在侧栏列表中
  store.sessionGroups = [{
    folder_path: 'GLOBAL', folder_name: '对话', is_global: true, is_current: false,
    sessions: [
      { name: 'session-a', display_name: 'A' },
      { name: 'session-b', display_name: 'B' },
      { name: 'session-c', display_name: 'C' },
    ],
  }] as any;

  // 用户在 A，B 任务执行完成 → B 出绿点
  store.handleBridgeEvent(makeEvent('turn_finish', 'session-b'));
  expect(store.sessionStatusDots['session-b']).toBe('green');

  // C 等待用户确认 → C 出黄点
  store.handleBridgeEvent(makeEvent('permission_request', 'session-c'));
  expect(store.sessionStatusDots['session-c']).toBe('yellow');

  // 用户切到 B：B 的点消失，C 的黄点保留
  store._clearSessionStatusDot('session-b');
  store.currentSessionName = 'session-b';
  expect('session-b' in store.sessionStatusDots).toBe(false);
  expect(store.sessionStatusDots['session-c']).toBe('yellow');

  // 用户在 B 时，C 报错 → C 由黄变红
  store.handleBridgeEvent(makeEvent('turn_error', 'session-c'));
  expect(store.sessionStatusDots['session-c']).toBe('red');

  // 此时 B 自身有新事件（B 是当前窗口）→ 不打点
  store.handleBridgeEvent(makeEvent('turn_finish', 'session-b'));
  expect('session-b' in store.sessionStatusDots).toBe(false);
});
