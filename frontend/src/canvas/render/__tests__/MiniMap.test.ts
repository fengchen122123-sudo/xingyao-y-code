// M3-T06 小地图单测：世界包围盒归一化（含负坐标/空节点集）、视口矩形按比例缩放、点击反算。
// 几何断言直接打纯函数（MiniMap.vue 普通 <script> 块的具名导出），不依赖 DOM 尺寸；
// 点击链路用挂载 + 桩 getBoundingClientRect 验证 navigate payload。
import { mount } from '@vue/test-utils';
import { describe, expect, it } from 'vitest';

import MiniMap, {
  MINI_MAP_MAX_HEIGHT,
  MINI_MAP_MAX_WIDTH,
  computeMiniMapLayout,
  getViewportWorldRect,
  miniMapPointToWorld,
  worldRectToMiniRect,
} from '../MiniMap.vue';
import type { CanvasNodeData } from '../../types';

function node(id: string, x: number, y: number, width = 100, height = 60): CanvasNodeData {
  return { id, type: 'text', title: id, position: { x, y }, width, height, metadata: {} };
}

const container = { width: 800, height: 600 };

describe('MiniMap 包围盒归一化', () => {
  it('负坐标节点参与包围盒；宽度不超过 180px，所有小矩形落在缩略图内', () => {
    const nodes = [node('a', -200, -100, 100, 60), node('b', 300, 200, 200, 100)];
    const layout = computeMiniMapLayout(nodes, { x: 0, y: 0, k: 1 }, container);

    // 视口世界矩形 (0,0,800,600) ∪ 节点包围盒 → 左/上由负坐标节点决定
    expect(layout.world.x).toBeLessThan(-200);
    expect(layout.world.y).toBeLessThan(-100);
    expect(layout.width).toBeLessThanOrEqual(MINI_MAP_MAX_WIDTH);
    expect(layout.width).toBeCloseTo(MINI_MAP_MAX_WIDTH, 6);
    expect(layout.height).toBeLessThanOrEqual(MINI_MAP_MAX_HEIGHT);
    expect(layout.scale).toBeGreaterThan(0);

    for (const item of nodes) {
      const rect = worldRectToMiniRect(
        { x: item.position.x, y: item.position.y, width: item.width, height: item.height },
        layout,
      );
      expect(rect.left).toBeGreaterThanOrEqual(0);
      expect(rect.top).toBeGreaterThanOrEqual(0);
      expect(rect.left + rect.width).toBeLessThanOrEqual(layout.width + 1e-6);
      expect(rect.top + rect.height).toBeLessThanOrEqual(layout.height + 1e-6);
    }
  });

  it('空节点集：只按视口算包围盒，尺寸合法且无 NaN', () => {
    const layout = computeMiniMapLayout([], { x: -400, y: -300, k: 1 }, container);
    const view = getViewportWorldRect({ x: -400, y: -300, k: 1 }, container);

    expect(view.x).toBe(400);
    expect(view.y).toBe(300);
    expect(layout.world.x).toBeLessThanOrEqual(view.x);
    expect(layout.world.width).toBeGreaterThan(view.width);
    expect(Number.isFinite(layout.width)).toBe(true);
    expect(Number.isFinite(layout.height)).toBe(true);
    expect(layout.width).toBeLessThanOrEqual(MINI_MAP_MAX_WIDTH);
  });
});

describe('MiniMap 视口矩形与坐标反算', () => {
  it('视口矩形按比例缩放：k 越大覆盖世界越小，缩略图上的取景框也越小', () => {
    // 节点包围盒远大于视口：k 变化只影响取景框，世界包围盒不变，比例可直接比较
    const nodes = [node('a', 0, 0, 4000, 3000)];
    const wide = computeMiniMapLayout(nodes, { x: 0, y: 0, k: 1 }, container);
    const zoomed = computeMiniMapLayout(nodes, { x: 0, y: 0, k: 2 }, container);

    const wideRect = worldRectToMiniRect(getViewportWorldRect({ x: 0, y: 0, k: 1 }, container), wide);
    const zoomedRect = worldRectToMiniRect(getViewportWorldRect({ x: 0, y: 0, k: 2 }, container), zoomed);

    // k=2 → 视口世界宽度减半（同一容器）
    expect(getViewportWorldRect({ x: 0, y: 0, k: 2 }, container).width).toBe(container.width / 2);
    expect(zoomedRect.width).toBeLessThan(wideRect.width);
    expect(wideRect.width).toBeCloseTo(container.width * wide.scale, 6);
  });

  it('缩略图坐标 → 世界坐标：左上角对 world.x/y，右下角对 world 边界', () => {
    const layout = computeMiniMapLayout([node('a', 120, 80)], { x: 0, y: 0, k: 1 }, container);

    const origin = miniMapPointToWorld(0, 0, layout);
    expect(origin.x).toBeCloseTo(layout.world.x, 6);
    expect(origin.y).toBeCloseTo(layout.world.y, 6);

    const bottomRight = miniMapPointToWorld(layout.width, layout.height, layout);
    expect(bottomRight.x).toBeCloseTo(layout.world.x + layout.width / layout.scale, 6);
    expect(bottomRight.y).toBeCloseTo(layout.world.y + layout.height / layout.scale, 6);
  });

  it('组件：点击缩略图中心 emit navigate（世界坐标为包围盒中心附近）', async () => {
    const nodes = [node('a', 0, 0, 100, 60)];
    const wrapper = mount(MiniMap, {
      props: { nodes, viewport: { x: 0, y: 0, k: 1 }, containerSize: container },
    });
    const layout = computeMiniMapLayout(nodes, { x: 0, y: 0, k: 1 }, container);
    const root = wrapper.find('[data-test="canvas-minimap"]').element as HTMLElement;
    // jsdom 不给真实布局：按组件自身尺寸桩一个左上角在 (0,0) 的盒子
    root.getBoundingClientRect = () =>
      ({ left: 0, top: 0, width: layout.width, height: layout.height, right: layout.width, bottom: layout.height, x: 0, y: 0, toJSON: () => ({}) }) as DOMRect;

    root.dispatchEvent(new MouseEvent('pointerdown', {
      bubbles: true,
      cancelable: true,
      button: 0,
      buttons: 1,
      clientX: layout.width / 2,
      clientY: layout.height / 2,
    }));
    await wrapper.vm.$nextTick();

    const events = wrapper.emitted('navigate') as unknown as Array<[{ x: number; y: number }]> | undefined;
    expect(events).toHaveLength(1);
    const center = events![0][0];
    const expected = miniMapPointToWorld(layout.width / 2, layout.height / 2, layout);
    expect(center.x).toBeCloseTo(expected.x, 6);
    expect(center.y).toBeCloseTo(expected.y, 6);

    wrapper.unmount();
  });
});