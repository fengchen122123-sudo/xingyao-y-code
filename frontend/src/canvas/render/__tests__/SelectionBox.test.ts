// M3-T03 框选与选区工具条单测：矩形几何、命中判定、additive 合并、拖拽阈值、提交链路。
// 环境：jsdom（无 PointerEvent，沿用 MouseEvent 派发 pointer* ），桥整体 mock，绝不碰 window.pywebview。
import { createPinia, setActivePinia } from 'pinia';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { mount, type VueWrapper } from '@vue/test-utils';

import CanvasPane from '../../CanvasPane.vue';
import InfiniteCanvas from '../InfiniteCanvas.vue';
import SelectionBox from '../SelectionBox.vue';
import SelectionToolbar from '../../tools/SelectionToolbar.vue';
import { __clearCanvasSaveTimer, useCanvasStore } from '../../../stores/canvas';
import {
  isSelectionArmed,
  nodeIntersectsRect,
  type SelectionCommitPayload,
} from '../../utils/canvas-selection';
import type { CanvasNodeData, CanvasProject, SelectionBoxState } from '../../types';

vi.mock('../../bridge', () => ({
  canvasBridge: {
    listProjects: vi.fn(async () => ({ ok: true, projects: [] })),
    loadProject: vi.fn(async () => ({ ok: false, msg: '旧版数据，不支持' })),
    saveProject: vi.fn(async () => ({ ok: true, updatedAt: 1700000000000 })),
    deleteProject: vi.fn(async () => ({ ok: true })),
  },
}));

function box(partial: Partial<SelectionBoxState> = {}): SelectionBoxState {
  return {
    startWorldX: 0,
    startWorldY: 0,
    currentWorldX: 0,
    currentWorldY: 0,
    additive: false,
    initialSelectedNodeIds: [],
    initialSelectedConnectionIds: [],
    ...partial,
  };
}

function node(id: string, x: number, y: number, width = 100, height = 60): CanvasNodeData {
  return { id, type: 'text', title: id, position: { x, y }, width, height, metadata: {} };
}

function fireMouse(target: EventTarget, type: string, init: MouseEventInit = {}): void {
  target.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true, ...init }));
}

function emittedBoxes(wrapper: VueWrapper): SelectionBoxState[] {
  const events = wrapper.emitted('selection-change') as unknown as Array<[SelectionBoxState]> | undefined;
  return (events ?? []).map((args) => args[0]);
}

function emittedCommits(wrapper: VueWrapper): SelectionCommitPayload[] {
  const events = wrapper.emitted('selection-commit') as unknown as
    | Array<[SelectionCommitPayload]>
    | undefined;
  return (events ?? []).map((args) => args[0]);
}

beforeEach(() => {
  setActivePinia(createPinia());
  __clearCanvasSaveTimer();
  vi.clearAllMocks();
});

afterEach(() => {
  __clearCanvasSaveTimer();
});

describe('SelectionBox 几何', () => {
  it('世界坐标 → 绝对定位样式（反向拖拽也取左上角 + 非负宽高）', () => {
    const wrapper = mount(SelectionBox, {
      props: { state: box({ startWorldX: 120, startWorldY: 80, currentWorldX: 40, currentWorldY: 200 }) },
    });

    const el = wrapper.find('[data-test="selection-box"]');
    expect(el.exists()).toBe(true);
    expect((el.element as HTMLElement).style.left).toBe('40px');
    expect((el.element as HTMLElement).style.top).toBe('80px');
    expect((el.element as HTMLElement).style.width).toBe('80px');
    expect((el.element as HTMLElement).style.height).toBe('120px');

    wrapper.unmount();
  });

  it('state=null 不渲染任何框', () => {
    const wrapper = mount(SelectionBox, { props: { state: null } });
    expect(wrapper.find('[data-test="selection-box"]').exists()).toBe(false);
    wrapper.unmount();
  });
});

describe('框选命中判定', () => {
  it('矩形与节点相交即命中（部分重叠、完全包含都算）', () => {
    const target = node('a', 100, 100, 200, 100);
    // 部分重叠：只压住节点左上角
    expect(nodeIntersectsRect(target, { x: 150, y: 120, width: 20, height: 20 })).toBe(true);
    // 完全包含：框比节点大
    expect(nodeIntersectsRect(target, { x: 0, y: 0, width: 500, height: 500 })).toBe(true);
    // 被包含：框落在节点内部
    expect(nodeIntersectsRect(target, { x: 150, y: 130, width: 10, height: 10 })).toBe(true);
  });

  it('不相交/边界相接都不命中（与参考仓严格不等号同口径）', () => {
    const target = node('a', 100, 100, 200, 100);
    // 右侧擦边：rect.x === node.x + node.width
    expect(nodeIntersectsRect(target, { x: 300, y: 100, width: 50, height: 50 })).toBe(false);
    // 上方完全错开
    expect(nodeIntersectsRect(target, { x: 120, y: 0, width: 30, height: 100 })).toBe(false);
    // 左下方错开
    expect(nodeIntersectsRect(target, { x: 0, y: 400, width: 400, height: 100 })).toBe(false);
  });
});

describe('框选阈值', () => {
  it('<4px 不算框选，≥4px 才算（SELECTION_MIN_DRAG）', () => {
    expect(isSelectionArmed({ x: 0, y: 0, width: 3, height: 3 })).toBe(false);
    expect(isSelectionArmed({ x: 0, y: 0, width: 4, height: 0 })).toBe(true);
    expect(isSelectionArmed({ x: 0, y: 0, width: 0, height: 12 })).toBe(true);
  });

  it('InfiniteCanvas：拖 3px 松手按空白点击处理（不发 selection-change / selection-commit）', () => {
    const wrapper = mount(InfiniteCanvas, { props: { viewport: { x: 0, y: 0, k: 1 } } });
    const root = wrapper.find('[data-test="canvas-viewport"]').element;

    fireMouse(root, 'pointerdown', { button: 0, clientX: 50, clientY: 50, buttons: 1 });
    fireMouse(window, 'pointermove', { clientX: 52, clientY: 52, buttons: 1 });
    fireMouse(window, 'pointerup', { button: 0, clientX: 52, clientY: 52 });

    expect(wrapper.emitted('selection-change')).toBeUndefined();
    expect(wrapper.emitted('selection-commit')).toBeUndefined();
    expect(wrapper.emitted('canvas-click-empty')).toHaveLength(1);

    wrapper.unmount();
  });
});

describe('InfiniteCanvas 框选提交', () => {
  it('拖拽中 emit selection-change（世界坐标），松手 emit 世界矩形 + additive=false', () => {
    // k=2、平移 (10,20)：屏幕 (110,120) → 世界 (50,50)，屏幕 (210,220) → 世界 (100,100)
    const wrapper = mount(InfiniteCanvas, { props: { viewport: { x: 10, y: 20, k: 2 } } });
    const root = wrapper.find('[data-test="canvas-viewport"]').element;

    fireMouse(root, 'pointerdown', { button: 0, clientX: 110, clientY: 120, buttons: 1 });
    fireMouse(window, 'pointermove', { clientX: 210, clientY: 220, buttons: 1 });

    const during = emittedBoxes(wrapper);
    expect(during).toHaveLength(1);
    expect(during[0].startWorldX).toBeCloseTo(50, 8);
    expect(during[0].startWorldY).toBeCloseTo(50, 8);
    expect(during[0].currentWorldX).toBeCloseTo(100, 8);
    expect(during[0].currentWorldY).toBeCloseTo(100, 8);

    fireMouse(window, 'pointerup', { button: 0, clientX: 210, clientY: 220 });

    expect(emittedCommits(wrapper)).toEqual([
      { rect: { x: 50, y: 50, width: 50, height: 50 }, additive: false },
    ]);
    // 框选结束不再补一个空白点击
    expect(wrapper.emitted('canvas-click-empty')).toBeUndefined();

    wrapper.unmount();
  });

  it('Shift 框选：additive=true 且快照当前选中集为加选基底', () => {
    const wrapper = mount(InfiniteCanvas, {
      props: {
        viewport: { x: 0, y: 0, k: 1 },
        selectedNodeIds: ['node-a', 'node-b'],
        selectedConnectionIds: ['conn-1'],
      },
    });
    const root = wrapper.find('[data-test="canvas-viewport"]').element;

    fireMouse(root, 'pointerdown', { button: 0, clientX: 0, clientY: 0, buttons: 1, shiftKey: true });
    fireMouse(window, 'pointermove', { clientX: 40, clientY: 40, buttons: 1, shiftKey: true });

    const state = emittedBoxes(wrapper)[0];
    expect(state.additive).toBe(true);
    expect(state.initialSelectedNodeIds).toEqual(['node-a', 'node-b']);
    expect(state.initialSelectedConnectionIds).toEqual(['conn-1']);

    fireMouse(window, 'pointerup', { button: 0, clientX: 40, clientY: 40, shiftKey: true });
    expect(emittedCommits(wrapper)[0]).toEqual({
      rect: { x: 0, y: 0, width: 40, height: 40 },
      additive: true,
    });

    wrapper.unmount();
  });
});

describe('选中集 store action', () => {
  it('setSelection：非 additive 覆盖、additive 并集；clearSelection / selectNode 行为', () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const store = useCanvasStore();
    store.project = {
      id: 'p-test',
      title: '选中集',
      createdAt: 0,
      updatedAt: 0,
      nodes: [],
      connections: [],
      viewport: { x: 0, y: 0, k: 1 },
      backgroundMode: 'grid',
      sidePanel: { open: false, width: 320, nodeId: null },
      nodeCount: 0,
      connectionCount: 0,
      schema_version: 2,
    } satisfies CanvasProject;

    store.setSelection(['a'], ['c1'], false);
    expect([...store.selectedNodeIds]).toEqual(['a']);
    expect([...store.selectedConnectionIds]).toEqual(['c1']);

    // additive：并集合并，原有选中不丢
    store.setSelection(['b'], [], true);
    expect([...store.selectedNodeIds].sort()).toEqual(['a', 'b']);
    expect([...store.selectedConnectionIds]).toEqual(['c1']);

    // 非 additive：覆盖并清掉连线选中
    store.setSelection(['c'], [], false);
    expect([...store.selectedNodeIds]).toEqual(['c']);
    expect(store.selectedConnectionIds.size).toBe(0);

    // 单击：非 additive 只留一个；additive 是切换
    store.setSelection(['a', 'b'], [], false);
    store.selectNode('c', false);
    expect([...store.selectedNodeIds]).toEqual(['c']);
    store.selectNode('d', true);
    expect([...store.selectedNodeIds].sort()).toEqual(['c', 'd']);
    store.selectNode('c', true);
    expect([...store.selectedNodeIds]).toEqual(['d']);

    store.clearSelection();
    expect(store.selectedNodeIds.size).toBe(0);
    expect(store.selectedConnectionIds.size).toBe(0);
  });
});

describe('SelectionToolbar', () => {
  it('visible=false 不渲染；有选中时四键齐全且 emit action', async () => {
    const hidden = mount(SelectionToolbar, {
      props: { selectedNodeCount: 0, selectedConnectionCount: 0, visible: false },
    });
    expect(hidden.find('[data-test="selection-toolbar"]').exists()).toBe(false);
    hidden.unmount();

    const wrapper = mount(SelectionToolbar, {
      props: { selectedNodeCount: 2, selectedConnectionCount: 0, visible: true },
    });
    expect(wrapper.find('[data-test="selection-count"]').text()).toContain('2');

    for (const action of ['delete', 'copy', 'group', 'ungroup'] as const) {
      await wrapper.find(`[data-test="${'selection-' + action}"]`).trigger('click');
    }
    expect(wrapper.emitted('action')).toEqual([['delete'], ['copy'], ['group'], ['ungroup']]);

    // 成组至少要两个节点：单选时置灰（按钮存在但 disabled）
    const single = mount(SelectionToolbar, {
      props: { selectedNodeCount: 1, selectedConnectionCount: 0, visible: true },
    });
    expect(single.find('[data-test="selection-group"]').attributes('disabled')).toBeDefined();
    single.unmount();

    wrapper.unmount();
  });
});

describe('CanvasPane 框选接线', () => {
  it('空白拖框命中节点 → store.setSelection；选中后工具条出现', async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const store = useCanvasStore();
    const project: CanvasProject = {
      id: 'p-pane',
      title: '框选接线',
      createdAt: 0,
      updatedAt: 0,
      nodes: [node('n1', 20, 20, 100, 60), node('n2', 300, 20, 100, 60)],
      connections: [],
      viewport: { x: 0, y: 0, k: 1 },
      backgroundMode: 'grid',
      sidePanel: { open: false, width: 320, nodeId: null },
      nodeCount: 2,
      connectionCount: 0,
      schema_version: 2,
    };
    store.project = project;

    const wrapper = mount(CanvasPane, { global: { plugins: [pinia] } });
    const root = wrapper.find('[data-test="canvas-viewport"]').element;

    // 框住两个节点（世界坐标 = 屏幕坐标，k=1、无平移）
    fireMouse(root, 'pointerdown', { button: 0, clientX: 0, clientY: 0, buttons: 1 });
    fireMouse(window, 'pointermove', { clientX: 450, clientY: 200, buttons: 1 });
    await wrapper.vm.$nextTick();
    expect(wrapper.find('[data-test="selection-box"]').exists()).toBe(true);

    fireMouse(window, 'pointerup', { button: 0, clientX: 450, clientY: 200 });
    await wrapper.vm.$nextTick();

    expect([...store.selectedNodeIds].sort()).toEqual(['n1', 'n2']);
    expect(wrapper.find('[data-test="selection-box"]').exists()).toBe(false);
    expect(wrapper.find('[data-test="selection-toolbar"]').exists()).toBe(true);

    wrapper.unmount();
  });
});
