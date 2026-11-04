// M2-T06 InfiniteCanvas 渲染底座单测：坐标互逆、滚轮锚点、缩放钳制、拖拽平移。
// 环境：jsdom（jsdom 无 PointerEvent，用 MouseEvent 派发 pointer* 事件照样能打到 Vue 监听），
// 真实桥不参与：store 相关用例把 canvas/bridge 整体 mock 掉，绝不触碰 window.pywebview。
import { createPinia, setActivePinia } from 'pinia';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { mount, type VueWrapper } from '@vue/test-utils';
import { nextTick } from 'vue';

import CanvasPane from '../../CanvasPane.vue';
import InfiniteCanvas from '../InfiniteCanvas.vue';
import { __clearCanvasSaveTimer, useCanvasStore } from '../../../stores/canvas';
import { canvasBridge } from '../../bridge';
import type { CanvasProject, Position, ViewportTransform } from '../../types';

vi.mock('../../bridge', () => ({
  canvasBridge: {
    listProjects: vi.fn(async () => ({ ok: true, projects: [] })),
    loadProject: vi.fn(async () => ({ ok: false, msg: '旧版数据，不支持' })),
    saveProject: vi.fn(async () => ({ ok: true, updatedAt: 1700000000000 })),
    deleteProject: vi.fn(async () => ({ ok: true })),
  },
}));

type CanvasApi = {
  screenToWorld(x: number, y: number): Position;
  worldToScreen(p: Position): Position;
};

const VIEWPORT: ViewportTransform = { x: 0, y: 0, k: 1 };

function mountCanvas(
  viewport: ViewportTransform = VIEWPORT,
  extraProps: Record<string, unknown> = {},
): VueWrapper {
  return mount(InfiniteCanvas, {
    props: { viewport: { ...viewport }, ...extraProps },
  });
}

/** 取组件 expose 的坐标换算 API（script setup 的 expose 在 $ / vm 两条路径上取） */
function canvasApi(wrapper: VueWrapper): CanvasApi {
  const comp = wrapper.findComponent(InfiniteCanvas);
  const vm = comp.vm as unknown as Record<string, unknown> & { $?: { exposed?: unknown } };
  if (typeof vm.screenToWorld === 'function') return vm as unknown as CanvasApi;
  const exposed = (vm.$?.exposed ?? {}) as CanvasApi;
  if (typeof exposed.screenToWorld !== 'function') throw new Error('InfiniteCanvas 未 expose 坐标换算');
  return exposed;
}

/** 世界层 transform 解析成数值（浮点误差下不能用字符串断言） */
function worldTransform(wrapper: VueWrapper): ViewportTransform {
  const style = wrapper.find('[data-test="canvas-world"]').attributes('style') ?? '';
  const matched = /translate\(([-\d.e+]+)px,\s*([-\d.e+]+)px\)\s*scale\(([-\d.e+]+)\)/.exec(style);
  if (!matched) throw new Error(`世界层 transform 缺失：${style}`);
  return { x: Number(matched[1]), y: Number(matched[2]), k: Number(matched[3]) };
}

function fireMouse(target: EventTarget, type: string, init: MouseEventInit = {}): void {
  target.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true, ...init }));
}

function fireWheel(
  target: EventTarget,
  init: { deltaY: number; clientX?: number; clientY?: number },
): void {
  target.dispatchEvent(
    new WheelEvent('wheel', {
      deltaY: init.deltaY,
      clientX: init.clientX ?? 0,
      clientY: init.clientY ?? 0,
      bubbles: true,
      cancelable: true,
    }),
  );
}

function emittedViewports(wrapper: VueWrapper): ViewportTransform[] {
  const events = wrapper.emitted('update:viewport') as unknown as Array<[ViewportTransform]> | undefined;
  return (events ?? []).map((args) => args[0]);
}

function lastViewport(wrapper: VueWrapper): ViewportTransform {
  const all = emittedViewports(wrapper);
  if (!all.length) throw new Error('没有收到 update:viewport');
  return all[all.length - 1];
}

beforeEach(() => {
  setActivePinia(createPinia());
  __clearCanvasSaveTimer();
  vi.clearAllMocks();
});

afterEach(() => {
  __clearCanvasSaveTimer();
  vi.useRealTimers();
});

describe('InfiniteCanvas 坐标换算', () => {
  it('screenToWorld / worldToScreen 互逆（k≠1 且带平移）', () => {
    const wrapper = mountCanvas({ x: 30, y: -20, k: 2 });
    const api = canvasApi(wrapper);

    expect(api.screenToWorld(100, 50)).toEqual({ x: 35, y: 35 });
    expect(api.worldToScreen({ x: 35, y: 35 })).toEqual({ x: 100, y: 50 });

    // 任意点往返一致
    const world = api.screenToWorld(123.5, 456.5);
    const back = api.worldToScreen(world);
    expect(back.x).toBeCloseTo(123.5, 6);
    expect(back.y).toBeCloseTo(456.5, 6);

    wrapper.unmount();
  });
});

describe('InfiniteCanvas 滚轮缩放', () => {
  it('锚定指针：缩放后指针下的世界坐标不变（k\'=k*1.1^(-deltaY/100)）', async () => {
    const wrapper = mountCanvas();
    const root = wrapper.find('[data-test="canvas-viewport"]').element;
    const api = canvasApi(wrapper);
    const before = api.screenToWorld(100, 60);

    fireWheel(root, { deltaY: -100, clientX: 100, clientY: 60 });

    // 参考仓系数：每 100px 缩放 1.1 倍；锚点公式 x' = mx - (mx - x) * k'/k
    const next = lastViewport(wrapper);
    expect(next.k).toBeCloseTo(1.1, 8);
    expect(next.x).toBeCloseTo(-10, 8);
    expect(next.y).toBeCloseTo(-6, 8);

    const after = api.screenToWorld(100, 60);
    expect(after.x).toBeCloseTo(before.x, 8);
    expect(after.y).toBeCloseTo(before.y, 8);

    // 世界层同步到新视口（DOM 更新走下一帧）
    await nextTick();
    const transform = worldTransform(wrapper);
    expect(transform.k).toBeCloseTo(1.1, 8);
    expect(transform.x).toBeCloseTo(-10, 8);
    expect(transform.y).toBeCloseTo(-6, 8);

    wrapper.unmount();
  });

  it('缩放钳制：下界 0.05、上界 5（参考仓源码 Math.min(Math.max(k, 0.05), 5)）', () => {
    const wrapper = mountCanvas();
    const root = wrapper.find('[data-test="canvas-viewport"]').element;

    for (let index = 0; index < 12; index += 1) {
      fireWheel(root, { deltaY: 600, clientX: 10, clientY: 10 });
    }
    expect(lastViewport(wrapper).k).toBe(0.05);

    // 已经顶到下限：继续滚不再发事件（不做无意义 emit / 落盘）
    const countAtMin = emittedViewports(wrapper).length;
    fireWheel(root, { deltaY: 600, clientX: 10, clientY: 10 });
    expect(emittedViewports(wrapper).length).toBe(countAtMin);

    for (let index = 0; index < 12; index += 1) {
      fireWheel(root, { deltaY: -600, clientX: 10, clientY: 10 });
    }
    expect(lastViewport(wrapper).k).toBe(5);

    wrapper.unmount();
  });

  it('带 data-canvas-no-zoom 的区域（节点内滚动）不缩放画布', () => {
    const wrapper = mountCanvas();
    const root = wrapper.find('[data-test="canvas-viewport"]').element;
    const inner = document.createElement('div');
    inner.setAttribute('data-canvas-no-zoom', '');
    root.appendChild(inner);

    fireWheel(inner, { deltaY: -100, clientX: 10, clientY: 10 });
    expect(wrapper.emitted('update:viewport')).toBeUndefined();

    wrapper.unmount();
  });
});

describe('InfiniteCanvas 拖拽平移', () => {
  it('中键拖拽：帧内只改渲染层，松手 emit 一次正确视口', async () => {
    const wrapper = mountCanvas({ x: 5, y: 7, k: 2 });
    const root = wrapper.find('[data-test="canvas-viewport"]').element;

    fireMouse(root, 'pointerdown', { button: 1, clientX: 100, clientY: 100, buttons: 4 });
    fireMouse(window, 'pointermove', { clientX: 160, clientY: 40, buttons: 4 });

    // 拖拽帧内不 emit（避免每帧写 store 顶住防抖落盘），但画面已经跟上
    expect(wrapper.emitted('update:viewport')).toBeUndefined();
    await nextTick();
    const during = worldTransform(wrapper);
    expect(during.x).toBeCloseTo(65, 8);
    expect(during.y).toBeCloseTo(-53, 8);
    expect(during.k).toBe(2);
    expect(wrapper.find('[data-test="canvas-world"]').attributes('style')).toContain(
      'will-change: transform',
    );

    fireMouse(window, 'pointerup', { button: 1, clientX: 160, clientY: 40 });

    expect(emittedViewports(wrapper)).toEqual([{ x: 65, y: -53, k: 2 }]);
    // 松手后撤掉合成层提示
    await nextTick();
    expect(wrapper.find('[data-test="canvas-world"]').attributes('style') ?? '').not.toContain(
      'will-change',
    );

    wrapper.unmount();
  });

  it('空格 + 左键拖拽平移（空格只在指针悬停画布时生效）', async () => {
    const wrapper = mountCanvas();
    const root = wrapper.find('[data-test="canvas-viewport"]');

    fireMouse(root.element, 'mouseenter');
    window.dispatchEvent(new KeyboardEvent('keydown', { code: 'Space', cancelable: true }));
    await nextTick();
    expect(root.classes()).toContain('is-hand');

    fireMouse(root.element, 'pointerdown', { button: 0, clientX: 50, clientY: 50, buttons: 1 });
    fireMouse(window, 'pointermove', { clientX: 10, clientY: 90, buttons: 1 });
    fireMouse(window, 'pointerup', { button: 0, clientX: 10, clientY: 90 });

    expect(lastViewport(wrapper)).toEqual({ x: -40, y: 40, k: 1 });

    window.dispatchEvent(new KeyboardEvent('keyup', { code: 'Space' }));
    await nextTick();
    expect(root.classes()).not.toContain('is-hand');

    wrapper.unmount();
  });

  it('左键无抓手不平移（画面留给 M3 选择/框选），按下不产生选中副作用', () => {
    const wrapper = mountCanvas();
    const root = wrapper.find('[data-test="canvas-viewport"]').element;

    fireMouse(root, 'pointerdown', { button: 0, clientX: 50, clientY: 50, buttons: 1 });
    fireMouse(window, 'pointermove', { clientX: 120, clientY: 120, buttons: 1 });
    fireMouse(window, 'pointerup', { button: 0, clientX: 120, clientY: 120 });

    expect(wrapper.emitted('update:viewport')).toBeUndefined();
    expect(worldTransform(wrapper)).toEqual({ x: 0, y: 0, k: 1 });

    wrapper.unmount();
  });

  it('没拖动就松手不发事件（例如只是拿回焦点）', () => {
    const wrapper = mountCanvas({ x: 12, y: 24, k: 1 });
    const root = wrapper.find('[data-test="canvas-viewport"]').element;

    fireMouse(root, 'pointerdown', { button: 1, clientX: 80, clientY: 80, buttons: 4 });
    fireMouse(window, 'pointerup', { button: 1, clientX: 81, clientY: 80 });

    expect(wrapper.emitted('update:viewport')).toBeUndefined();
    expect(worldTransform(wrapper)).toEqual({ x: 12, y: 24, k: 1 });

    wrapper.unmount();
  });
});

describe('InfiniteCanvas 渲染与背景', () => {
  it('世界层 transform 与 48px 网格背景按 k 与 x/y 换算', () => {
    const wrapper = mountCanvas({ x: 48, y: 96, k: 2 });

    expect(worldTransform(wrapper)).toEqual({ x: 48, y: 96, k: 2 });

    const gridStyle = wrapper.find('[data-test="canvas-grid"]').attributes('style') ?? '';
    expect(gridStyle).toContain('background-size: 96px 96px');
    // 48 % 96 = 48；96 % 96 = 0 —— 网格随平移错开，视觉上跟着世界走
    expect(gridStyle).toContain('background-position: 48px 0px');
    expect(gridStyle).toContain('linear-gradient');

    wrapper.unmount();
  });

  it('backgroundMode 三态：grid（默认线格）/ dots（点阵）/ plain（纯色无网格）', () => {
    const grid = mountCanvas();
    expect(grid.find('[data-test="canvas-grid"]').attributes('style') ?? '').toContain('linear-gradient');
    grid.unmount();

    const dots = mountCanvas(VIEWPORT, { backgroundMode: 'dots' });
    expect(dots.find('[data-test="canvas-grid"]').attributes('style') ?? '').toContain('radial-gradient');
    dots.unmount();

    const plain = mountCanvas(VIEWPORT, { backgroundMode: 'plain' });
    expect(plain.find('[data-test="canvas-grid"]').exists()).toBe(false);
    plain.unmount();
  });

  it('默认插槽渲染在世界坐标层内，受控 prop 变化同步渲染视口', async () => {
    const wrapper = mount(InfiniteCanvas, {
      props: { viewport: { ...VIEWPORT } },
      slots: { default: '<div data-test="slot-child">世界内容</div>' },
    });

    expect(wrapper.find('[data-test="canvas-world"] [data-test="slot-child"]').exists()).toBe(true);

    await wrapper.setProps({ viewport: { x: 24, y: -12, k: 1.5 } });
    const transform = worldTransform(wrapper);
    expect(transform.x).toBeCloseTo(24, 8);
    expect(transform.y).toBeCloseTo(-12, 8);
    expect(transform.k).toBeCloseTo(1.5, 8);

    wrapper.unmount();
  });
});

describe('CanvasPane 接线', () => {
  function makeProject(): CanvasProject {
    return {
      id: 'p-test',
      title: '接线测试',
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
    };
  }

  it('canvas-mount 挂上 InfiniteCanvas：滚轮缩放经 setViewport 写回 store', () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const store = useCanvasStore();
    store.project = makeProject();

    const wrapper = mount(CanvasPane, { global: { plugins: [pinia] } });

    expect(wrapper.find('[data-test="canvas-empty-hint"]').text()).toContain('拖入或生成素材开始');
    const canvas = wrapper.findComponent(InfiniteCanvas);
    expect(canvas.exists()).toBe(true);

    fireWheel(wrapper.find('[data-test="canvas-viewport"]').element, {
      deltaY: -100,
      clientX: 0,
      clientY: 0,
    });

    // 受控链路：emit → CanvasPane → store.setViewport（落盘由 800ms 防抖负责）
    expect(store.project?.viewport.k).toBeCloseTo(1.1, 8);
    expect(store.project?.viewport.x).toBeCloseTo(0, 8);
    expect(store.project?.viewport.y).toBeCloseTo(0, 8);
    expect(vi.mocked(canvasBridge.saveProject)).not.toHaveBeenCalled();

    wrapper.unmount();
  });
});
