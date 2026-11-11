import { mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, describe, expect, it } from 'vitest';
import ChangedFilesPanel from './ChangedFilesPanel.vue';
import type { TurnUndoStateSummary } from '../types/pywebview';

beforeEach(() => setActivePinia(createPinia()));

function change() {
  return {
    name: 'a.py',
    path: 'a.py',
    added: 1,
    removed: 0,
    diffText: '+x',
  };
}

function undoState(overrides: Partial<TurnUndoStateSummary> = {}): TurnUndoStateSummary {
  return {
    checkpoint_id: 'ckpt-1',
    turn_id: 'turn-1',
    status: 'ready',
    snapshot_availability: 'available',
    record_version: 3,
    owner_kind: 'foreground_turn',
    parent_checkpoint_id: null,
    description: '',
    started_at_ms: 0,
    completed_at_ms: null,
    reverted_at_ms: null,
    has_uncovered_command_effects: 0,
    unavailable_code: null,
    file_count: 1,
    net_added: 1,
    net_removed: 0,
    ...overrides,
  };
}

function mountPanel(undo: TurnUndoStateSummary | null) {
  return mount(ChangedFilesPanel, {
    props: {
      changes: [change()],
      undoState: undo,
      turnId: undo ? 'turn-1' : '',
    },
  });
}

describe('ChangedFilesPanel 按回合撤销', () => {
  it('ready 状态显示可点击的撤销按钮，展开区与按钮互不嵌套', () => {
    const wrapper = mountPanel(undoState());
    const undoBtn = wrapper.get('.cfp-undo-btn');
    expect(undoBtn.text()).toContain('撤销');
    expect(undoBtn.attributes('aria-label')).toBe('撤销本轮文件修改');
    // 展开区按钮与撤销按钮是兄弟节点，不互相嵌套
    expect(undoBtn.element.parentElement).toBe(wrapper.get('.cfp-head-row').element);
    expect(undoBtn.attributes('disabled')).toBeUndefined();
  });

  it('reverted 状态按钮不可点击，标题切换为已撤销', () => {
    const wrapper = mountPanel(undoState({ status: 'reverted', reverted_at_ms: 42 }));
    const btn = wrapper.get('.cfp-undo-btn');
    expect(btn.text()).toContain('已撤销');
    expect(btn.attributes('disabled')).toBeDefined();
    expect(wrapper.get('.cfp-title').text()).toContain('已撤销 1 个文件');
  });

  it('unavailable 状态显示不可撤销；expired 显示撤销记录已过期', () => {
    const u1 = mountPanel(undoState({ status: 'unavailable', unavailable_code: 'snapshot_missing' }));
    expect(u1.get('.cfp-undo-btn').text()).toContain('不可撤销');
    const u2 = mountPanel(undoState({ status: 'ready', snapshot_availability: 'expired' }));
    expect(u2.get('.cfp-undo-btn').text()).toContain('撤销记录已过期');
    const u3 = mountPanel(undoState({ status: 'reverted', snapshot_availability: 'expired' }));
    expect(u3.get('.cfp-undo-btn').text()).toContain('已撤销');
    expect(u3.get('.cfp-undo-btn').text()).toContain('原 diff 已过期');
  });

  it('无撤销状态（旧回合/运行中）不渲染撤销按钮', () => {
    const wrapper = mountPanel(null);
    expect(wrapper.find('.cfp-undo-btn').exists()).toBe(false);
    expect(wrapper.get('.cfp-title').text()).toContain('1 个文件已更改');
  });

  it('recording 与 no_changes 不显示撤销入口', () => {
    expect(mountPanel(undoState({ status: 'recording' })).find('.cfp-undo-btn').exists()).toBe(false);
    expect(mountPanel(undoState({ status: 'no_changes', file_count: 0 })).find('.cfp-undo-btn').exists()).toBe(false);
  });

  it('interrupted 状态显示恢复选项入口', () => {
    const wrapper = mountPanel(undoState({ status: 'interrupted' }));
    expect(wrapper.get('.cfp-undo-btn').text()).toContain('撤销中断');
  });

  it('权威 ready 状态使用持久化净统计展示头部数字', () => {
    const wrapper = mountPanel(undoState({ file_count: 4, net_added: 10, net_removed: 3 }));
    expect(wrapper.get('.cfp-title').text()).toContain('4 个文件已编辑');
    expect(wrapper.get('.cfp-add').text()).toBe('+10');
    expect(wrapper.get('.cfp-del').text()).toBe('-3');
  });
});

import { vi } from 'vitest';

describe('ChangedFilesPanel 中断恢复流程（M2）', () => {
  it('interrupted 状态点击按钮进入恢复选项视图（不再走预检死胡同）', async () => {
    setActivePinia(createPinia());
    const { useAppStore } = await import('../stores/appStore');
    const store = useAppStore();
    store.fetchTurnUndoStatus = vi.fn().mockResolvedValue({
      checkpoint_id: 'ckpt-1',
      turn_id: 'turn-1',
      status: 'interrupted',
      last_operation_id: 'op-interrupted',
      files: [],
    });

    const wrapper = mount(ChangedFilesPanel, {
      props: {
        changes: [change()],
        undoState: undoState({ status: 'interrupted' }),
        turnId: 'turn-1',
      },
    });

    await wrapper.get('.cfp-undo-btn').trigger('click');
    await vi.waitFor(() => {
      expect(wrapper.find('.cfp-recovery-actions').exists()).toBe(true);
    });
    // 两个恢复方向按钮存在
    const buttons = wrapper.findAll('.cfp-recovery-actions button');
    expect(buttons.length).toBe(2);
    expect(buttons[0].text()).toContain('继续撤销');
    expect(buttons[1].text()).toContain('取消撤销');
    expect(store.fetchTurnUndoStatus).toHaveBeenCalledWith('turn-1');
  });

  it('正常 ready 状态点击仍走撤销预检（不进恢复视图）', async () => {
    setActivePinia(createPinia());
    const { useAppStore } = await import('../stores/appStore');
    const store = useAppStore();
    const previewSpy = vi.fn().mockResolvedValue(null);
    store.previewTurnUndo = previewSpy;

    const wrapper = mount(ChangedFilesPanel, {
      props: {
        changes: [change()],
        undoState: undoState(),
        turnId: 'turn-1',
      },
    });
    await wrapper.get('.cfp-undo-btn').trigger('click');
    await vi.waitFor(() => expect(previewSpy).toHaveBeenCalled());
    expect(wrapper.find('.cfp-recovery-actions').exists()).toBe(false);
  });
});
