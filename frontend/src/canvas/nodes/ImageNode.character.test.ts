import { describe, it, expect, vi } from 'vitest';
import { mount } from '@vue/test-utils';
import ImageNode from './ImageNode.vue';
import type { CanvasNode } from '../../stores/canvas';

// 与 ImageNode.click.test.ts 同一套桩：Handle 需要 vue-flow 的注入上下文，
// 单测里不提供，直接换成空组件
vi.mock('@vue-flow/core', () => ({
  Handle: { name: 'Handle', render: () => null },
  Position: { Left: 'left', Right: 'right' },
}));

function makeNode(characterIds?: number[]): CanvasNode {
  return {
    id: 'n1',
    type: 'image',
    x: 0,
    y: 0,
    width: 264,
    title: '小满',
    file: 'C:/tmp/xiaoman.png',
    url: 'http://127.0.0.1:1/xiaoman.png',
    meta: characterIds ? { character_ids: characterIds } : {},
  } as CanvasNode;
}

async function openPanel(node: CanvasNode) {
  const w = mount(ImageNode, { props: { id: 'n1', data: { node } } });
  await w.find('[data-test="canvas-edit-trigger"]').trigger('click');
  return w;
}

// P2「角色库与角色带入」的 UI 契约：入口露不露、提示说什么，全部钉死在
// data-test 上——多角色同框的硬限制必须在节点面板明示，不假装能一键稳定双人
describe('ImageNode 角色库入口与提示', () => {
  it('未绑定角色：有「存为角色参考」入口、无定妆图入口、无角色提示', async () => {
    const w = await openPanel(makeNode());
    expect(w.find('[data-test="canvas-save-character-ref"]').exists()).toBe(true);
    expect(w.find('[data-test="canvas-generate-character-image"]').exists()).toBe(false);
    expect(w.find('[data-test="canvas-character-bound-hint"]').exists()).toBe(false);
    expect(w.find('[data-test="canvas-character-multi-hint"]').exists()).toBe(false);
  });

  it('绑定单角色：露出定妆图入口 + 注入预告提示', async () => {
    const w = await openPanel(makeNode([7]));
    expect(w.find('[data-test="canvas-generate-character-image"]').exists()).toBe(true);
    const hint = w.find('[data-test="canvas-character-bound-hint"]');
    expect(hint.exists()).toBe(true);
    expect(hint.text()).toContain('外貌');
    expect(w.find('[data-test="canvas-character-multi-hint"]').exists()).toBe(false);
  });

  it('绑定多角色：硬限制明示（生成视频会被拒绝），不给定妆图入口', async () => {
    const w = await openPanel(makeNode([7, 9]));
    const warn = w.find('[data-test="canvas-character-multi-hint"]');
    expect(warn.exists()).toBe(true);
    expect(warn.text()).toContain('多人同框');
    // 多角色没有稳定方案：定妆图（单角色语义）不露，避免误导
    expect(w.find('[data-test="canvas-generate-character-image"]').exists()).toBe(false);
  });

  it('点「存为角色参考」emit save-character-ref；点「用角色出定妆图」带 prompt', async () => {
    const w = await openPanel(makeNode([7]));
    await w.find('[data-test="canvas-save-character-ref"]').trigger('click');
    let events = w.emitted('save-character-ref') as unknown[][] | undefined;
    expect(events?.[0]?.[0]).toEqual({ id: 'n1', action: 'save-character-ref', prompt: undefined });

    // 重新开浮层（上一步已收起）并出定妆图
    await w.find('[data-test="canvas-edit-trigger"]').trigger('click');
    await w.find('[data-test="canvas-edit-prompt"]').setValue('正面半身定妆照，浅笑');
    await w.find('[data-test="canvas-generate-character-image"]').trigger('click');
    events = w.emitted('generate-character-image') as unknown[][] | undefined;
    expect(events?.[0]?.[0]).toEqual({
      id: 'n1',
      action: 'generate-character-image',
      prompt: '正面半身定妆照，浅笑',
    });
  });
});
