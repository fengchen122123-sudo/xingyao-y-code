// M3-T02 utils 第二批单测：分组包围盒 / 吸附边界（GROUP_PADDING 内外）/ 进出组 id 判定
// + 素材标题编号（canvas-material-title 直译校验）。
// 这些函数是 NodeShell 拖拽收尾与 store.commitGroupSnap 的共同依据，纯函数、无 DOM。
import { describe, expect, it } from 'vitest';

import type { CanvasNodeData } from '../../types';
import {
  GROUP_PADDING,
  findContainingGroupId,
  findGroupDropTarget,
  getNodeBounds,
  getSelectionPlusWorld,
  snapNodesIntoGroup,
} from '../canvas-group';
import {
  materialDerivedTitle,
  nextMaterialTitle,
  nextResourceTitle,
} from '../canvas-material-title';

function makeNode(overrides: Partial<CanvasNodeData> = {}): CanvasNodeData {
  return {
    id: 'n-1',
    type: 'image',
    title: '',
    position: { x: 0, y: 0 },
    width: 340,
    height: 240,
    metadata: {},
    ...overrides,
  };
}

const GROUP = makeNode({ id: 'g-1', type: 'group', title: '分组', position: { x: 0, y: 0 }, width: 760, height: 480 });

describe('canvas-group 包围盒', () => {
  it('getNodeBounds：多节点（含负坐标）取最小 left/top 与最大 right/bottom', () => {
    const nodes = [
      makeNode({ id: 'a', position: { x: -40, y: 100 }, width: 100, height: 50 }),
      makeNode({ id: 'b', position: { x: 200, y: -20 }, width: 60, height: 300 }),
    ];

    expect(getNodeBounds(nodes)).toEqual({ left: -40, top: -20, right: 260, bottom: 280 });
    // 空集合是恒等元（+Infinity / -Infinity）：调用方靠它判断"没有节点"
    expect(getNodeBounds([])).toEqual({ left: Infinity, top: Infinity, right: -Infinity, bottom: -Infinity });
  });

  it('getSelectionPlusWorld：少于两个节点无坐标，多选时落在包围盒右侧', () => {
    expect(getSelectionPlusWorld([makeNode()])).toBeNull();
    const plus = getSelectionPlusWorld(
      [makeNode({ position: { x: 0, y: 0 }, width: 100, height: 100 }), makeNode({ id: 'n-2', position: { x: 200, y: 100 }, width: 100, height: 100 })],
      2,
    );
    // right(300) + 28/scale(14)，纵向取包围盒中线
    expect(plus).toEqual({ x: 314, y: 100 });
  });
});

describe('canvas-group 分组判定', () => {
  it('findContainingGroupId：中心在组内给组 id，中心移出右边界即 undefined', () => {
    const inside = makeNode({ position: { x: 100, y: 100 } });
    expect(findContainingGroupId(inside, [GROUP, inside])).toBe('g-1');

    // 节点左边缘还在组内，但中心已越过 group 右边（760）→ 不算"在组内"
    const outside = makeNode({ position: { x: 600, y: 100 } });
    expect(findContainingGroupId(outside, [GROUP, outside])).toBeUndefined();
  });

  it('findGroupDropTarget：中心进入组矩形即命中（GROUP_PADDING 内也命中），组外返回 null', () => {
    // 左边缘压在 padding 线上、中心仍在组内 → 命中
    const onPadding = makeNode({ position: { x: -30, y: 100 } });
    expect(findGroupDropTarget(new Set([onPadding.id]), [GROUP, onPadding])?.id).toBe('g-1');

    // 中心落到 group 下边界外（480 之下）→ 不命中
    const below = makeNode({ position: { x: 100, y: 400 } });
    expect(findGroupDropTarget(new Set([below.id]), [GROUP, below])).toBeNull();
  });

  it('findGroupDropTarget：拖动分组自身不吸附；多组重叠取最后渲染（最上层）的那个', () => {
    expect(findGroupDropTarget(new Set(['g-1']), [GROUP])).toBeNull();

    const top = makeNode({ id: 'g-2', type: 'group', position: { x: 0, y: 0 }, width: 800, height: 600 });
    const node = makeNode({ id: 'n-9', position: { x: 100, y: 100 } });
    // nodes 顺序 = 渲染顺序，reverse 后 g-2 在前 → 优先命中 g-2
    expect(findGroupDropTarget(new Set(['n-9']), [GROUP, top, node])?.id).toBe('g-2');
  });
});

describe('canvas-group 吸附', () => {
  it('snapNodesIntoGroup：左右越界按 GROUP_PADDING 回推，并写入 metadata.groupId', () => {
    // 左越界：left=-100 → 回推到 group.x + GROUP_PADDING
    const left = makeNode({ id: 'n-left', position: { x: -100, y: 100 } });
    const snappedLeft = snapNodesIntoGroup(new Set(['n-left']), [GROUP, left], GROUP);
    const leftNode = snappedLeft.find((n) => n.id === 'n-left')!;
    expect(leftNode.position).toEqual({ x: GROUP.position.x + GROUP_PADDING, y: 100 });
    expect(leftNode.metadata?.groupId).toBe('g-1');

    // 右越界：right=940 > 760-24 → 回推到右 padding 内
    const right = makeNode({ id: 'n-right', position: { x: 600, y: 200 } });
    const snappedRight = snapNodesIntoGroup(new Set(['n-right']), [GROUP, right], GROUP);
    const rightNode = snappedRight.find((n) => n.id === 'n-right')!;
    expect(rightNode.position.x + rightNode.width).toBe(GROUP.position.x + GROUP.width - GROUP_PADDING);
    expect(rightNode.metadata?.groupId).toBe('g-1');
  });

  it('snapNodesIntoGroup：组内富余空间不挪位置，未在 movedIds 的节点原样返回', () => {
    const inside = makeNode({ id: 'n-in', position: { x: 100, y: 100 } });
    const bystander = makeNode({ id: 'n-bystander', position: { x: 900, y: 900 } });
    const result = snapNodesIntoGroup(new Set(['n-in']), [GROUP, inside, bystander], GROUP);

    expect(result.find((n) => n.id === 'n-in')!.position).toEqual({ x: 100, y: 100 });
    expect(result.find((n) => n.id === 'n-in')!.metadata?.groupId).toBe('g-1');
    expect(result.find((n) => n.id === 'n-bystander')).toBe(bystander);
  });
});

describe('canvas-material-title 标题编号', () => {
  it('nextMaterialTitle 取最大编号 +1，nextResourceTitle 按类型编号，materialDerivedTitle 去后缀', () => {
    const nodes = [
      makeNode({ id: 'a', title: '素材3_尾帧' }),
      makeNode({ id: 'b', title: '素材7' }),
      makeNode({ id: 'c', title: '图片 2' }),
      makeNode({ id: 'd', type: 'video', title: '视频 1' }),
    ];

    expect(nextMaterialTitle(nodes)).toBe('素材8');
    expect(nextResourceTitle('image', nodes)).toBe('图片 3');
    expect(nextResourceTitle('panorama', nodes)).toBe('图片 3');
    expect(nextResourceTitle('video', nodes)).toBe('视频 2');
    // config/director/group 无类型前缀 → 回落素材编号
    expect(nextResourceTitle('config', nodes)).toBe('素材8');
    expect(materialDerivedTitle('素材7_当前帧', '首帧')).toBe('素材7_首帧');
    expect(materialDerivedTitle('   ', '音轨')).toBe('素材_音轨');
  });
});
