import { describe, it, expect, vi } from 'vitest';
import { mount } from '@vue/test-utils';
import ImageNode from './ImageNode.vue';
import type { CanvasNode } from '../../stores/canvas';

// Handle 需要 vue-flow 的 NodeId 注入上下文，单测里不提供，直接换成空组件
vi.mock('@vue-flow/core', () => ({
  Handle: { name: 'Handle', render: () => null },
  Position: { Left: 'left', Right: 'right' },
}));

function makeNode(): CanvasNode {
  return {
    id: 'n1',
    type: 'image',
    x: 0,
    y: 0,
    width: 264,
    title: '橘猫',
    file: 'C:/tmp/cat.png',
    url: 'http://127.0.0.1:1/cat.png',
    meta: {},
  } as CanvasNode;
}

describe('ImageNode 就地改图浮层', () => {
  it('点「改图」开浮层，写指令后点「生成」应 emit edit-inline', async () => {
    const w = mount(ImageNode, { props: { id: 'n1', data: { node: makeNode() } } });
    await w.find('[data-test="canvas-edit-trigger"]').trigger('click');
    expect(w.find('[data-test="canvas-edit-panel"]').exists()).toBe(true);

    await w.find('[data-test="canvas-edit-prompt"]').setValue('把月亮换成落日');
    const btn = w.find('[data-test="canvas-edit-generate"]');
    expect(btn.attributes('disabled')).toBeUndefined();
    await btn.trigger('click');

    const events = w.emitted('edit-inline') as unknown[][] | undefined;
    console.log('emitted keys =', Object.keys(w.emitted()));
    expect(events?.[0]?.[0]).toEqual({ id: 'n1', action: 'edit-inline', prompt: '把月亮换成落日' });
    // 发出去后浮层应立即收起
    expect(w.find('[data-test="canvas-edit-panel"]').exists()).toBe(false);
  });

  it('空指令点「生成」：只给内联提示，不发事件', async () => {
    const w = mount(ImageNode, { props: { id: 'n1', data: { node: makeNode() } } });
    await w.find('[data-test="canvas-edit-trigger"]').trigger('click');
    await w.find('[data-test="canvas-edit-generate"]').trigger('click');
    expect(w.emitted('edit-inline')).toBeFalsy();
    expect(w.find('[data-test="canvas-edit-hint"]').exists()).toBe(true);
  });

  it('editPending=true 时入口按钮禁用（不该开出浮层）', async () => {
    // editPending 是节点自己的 prop（由 CanvasPane 从 data.editPending 提出后透传），
    // 不是塞在 data 里的字段
    const w = mount(ImageNode, {
      props: { id: 'n1', editPending: true, data: { node: makeNode() } },
    });
    expect(w.find('[data-test="canvas-edit-trigger"]').attributes('disabled')).toBeDefined();
    await w.find('[data-test="canvas-edit-trigger"]').trigger('click');
    expect(w.find('[data-test="canvas-edit-panel"]').exists()).toBe(false);
  });
});
