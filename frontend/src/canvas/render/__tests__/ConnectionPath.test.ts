// M3-T04 连线渲染单测：贝塞尔路径 d（世界坐标、控制点水平外延）+ 选中态样式 + 点选事件
// + 与渲染同公式的框选命中（connectionIntersectsWorldRect）。
// 环境：jsdom。SVG 不做像素比对，只断 d / class / stroke 这些确定性属性。
import { describe, expect, it } from 'vitest';
import { mount } from '@vue/test-utils';

import ConnectionPath from '../ConnectionPath.vue';
import { buildConnectionPathD, connectionIntersectsWorldRect } from '../../utils/canvas-connection-path';
import type { CanvasNodeData, CanvasConnection } from '../../types';

function node(id: string, x: number, y: number, width = 100, height = 60): CanvasNodeData {
  return { id, type: 'image', title: id, position: { x, y }, width, height, metadata: {} };
}

function connection(): CanvasConnection {
  return { id: 'c_1', fromNodeId: 'a', toNodeId: 'b' };
}

describe('连线路径 d', () => {
  it('两节点相对位置不同 → 不同的贝塞尔 d（控制点水平外延 max(水平距/2, 50)）', () => {
    // 正向右：起点右侧中点 (100,30) → 终点左侧中点 (300,30)，水平距 200 → 曲率 100
    expect(buildConnectionPathD(node('a', 0, 0), node('b', 300, 0))).toBe(
      'M 100 30 C 200 30, 200 30, 300 30',
    );
    // 反向 + 偏下：起点 (400,30) → 终点 (100,230)，水平距 300 → 曲率 150（控制点会跑到 x<0）
    expect(buildConnectionPathD(node('a', 300, 0), node('b', 100, 200))).toBe(
      'M 400 30 C 550 30, -50 230, 100 230',
    );
    // 近距离：水平距 20 → 曲率触底 50，保证仍是一条弧而不是死直线
    expect(buildConnectionPathD(node('a', 0, 0), node('b', 120, 0))).toBe(
      'M 100 30 C 150 30, 70 30, 120 30',
    );
  });

  it('框选命中与渲染同公式：路径附近命中，远处不命中', () => {
    const from = node('a', 0, 0);
    const to = node('b', 300, 0);
    expect(connectionIntersectsWorldRect(from, to, { x: 200, y: 20, w: 20, h: 20 })).toBe(true);
    expect(connectionIntersectsWorldRect(from, to, { x: 200, y: 200, w: 20, h: 20 })).toBe(false);
    // 退化矩形（宽或高为 0）不算命中，避免「点一下空白就选中整批连线」
    expect(connectionIntersectsWorldRect(from, to, { x: 200, y: 30, w: 0, h: 0 })).toBe(false);
  });
});

describe('ConnectionPath 组件', () => {
  it('未选中：拿到路径 d，描边是默认灰、线宽按 1/k 反缩', () => {
    const wrapper = mount(ConnectionPath, {
      props: {
        connection: connection(),
        from: node('a', 0, 0),
        to: node('b', 300, 0),
        selected: false,
        scale: 2,
      },
    });

    const line = wrapper.find('[data-test="connection-line"]');
    expect(line.attributes('d')).toBe('M 100 30 C 200 30, 200 30, 300 30');
    expect(line.attributes('stroke')).toBe('#7a7a86');
    // 世界层被 scale(2) 放大，线宽 2/2=1 才在屏幕上仍是 2px
    expect(line.attributes('stroke-width')).toBe('1');
    expect(wrapper.classes()).not.toContain('is-selected');
    expect(wrapper.attributes('data-connection-id')).toBe('c_1');

    wrapper.unmount();
  });

  it('选中态：根节点带 is-selected，描边换主题蓝并加粗', () => {
    const wrapper = mount(ConnectionPath, {
      props: {
        connection: connection(),
        from: node('a', 0, 0),
        to: node('b', 300, 0),
        selected: true,
      },
    });

    expect(wrapper.classes()).toContain('is-selected');
    const line = wrapper.find('[data-test="connection-line"]');
    expect(line.attributes('stroke')).toBe('#2f80ff');
    expect(line.attributes('stroke-width')).toBe('3.5');
    expect(line.attributes('stroke-opacity')).toBe('1');

    wrapper.unmount();
  });

  it('点击命中区 emit select(connectionId)，视觉线自身不吃事件', async () => {
    const wrapper = mount(ConnectionPath, {
      props: {
        connection: connection(),
        from: node('a', 0, 0),
        to: node('b', 300, 0),
        selected: false,
      },
    });

    await wrapper.find('[data-test="connection-hit"]').trigger('click');
    expect(wrapper.emitted('select')).toEqual([['c_1']]);

    wrapper.unmount();
  });
});
