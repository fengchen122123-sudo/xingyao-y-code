// 锁定 S3-T12 评审记录的一条边界：调试控制台的行缓冲是 DebugPane 的组件本地 ref，
// 而 dapBus 只有广播、没有回放缓冲（见 utils/dapBus.ts 文件头）。
//
// 后果（已知且已在 DebugPane.vue 的 consoleLines 注释里显式声明）：
// - 挂载之前发出的输出不会补显（总线没有回放）；
// - 任何卸载 DebugPane 的路径都会连缓冲一起丢，最典型的是退出 IDE 模式
//   （App.vue 用 `v-if="store.ideMode()"` 整体卸载 IdeLayout）。
//
// 计划只要求「收起面板不丢」——BottomPanel 用 v-show 让终端与调试控制台常驻，
// 已由 BottomPanel.debugTab.test.ts 锁住。本文件把另一侧的边界也钉住：将来若把缓冲
// 提升到 dapBus / store 层（更彻底的做法），本用例会失败，必须一并更新声明。
import { beforeEach, expect, it } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';
import { nextTick } from 'vue';
import { mount } from '@vue/test-utils';

import DebugPane from './DebugPane.vue';
import { emitDapOutput } from '../utils/dapBus';

beforeEach(() => {
  setActivePinia(createPinia());
  (window as any).pywebview = { api: {} };
});

it('挂载前发出的输出不会回放（dapBus 只有广播，没有缓冲）', async () => {
  emitDapOutput({ session_id: 'dap-1', category: 'stdout', text: 'before-mount\n' });

  const wrapper = mount(DebugPane);
  emitDapOutput({ session_id: 'dap-1', category: 'stdout', text: 'after-mount\n' });
  await nextTick();

  const text = wrapper.get('[data-test="debug-console-output"]').text();
  expect(text).toContain('after-mount');
  expect(text).not.toContain('before-mount');
  wrapper.unmount();
});

it('卸载 DebugPane（退出 IDE 模式）会丢弃本地行缓冲：该边界已记录', async () => {
  const first = mount(DebugPane);
  emitDapOutput({ session_id: 'dap-1', category: 'stdout', text: 'session-1\n' });
  await nextTick();
  expect(first.get('[data-test="debug-console-output"]').text()).toContain('session-1');
  first.unmount();

  // 重新进入 IDE 模式：控制台从空开始（旧输出不跨模式保留）
  const second = mount(DebugPane);
  expect(second.get('[data-test="debug-console-output"]').text()).not.toContain('session-1');
  second.unmount();
});
