import { mount } from '@vue/test-utils';
import { reactive } from 'vue';
import { describe, expect, it } from 'vitest';
import AttachmentChips from './AttachmentChips.vue';
import type { AttachmentItem } from '../types/composer';

// 纯展示组件：不依赖 store，直接传 props 驱动渲染断言。
// items 用 reactive 包裹，保证 onThumbError 原地改写 thumb 能触发重渲染。

const imgItem: AttachmentItem = {
  path: 'C:/tmp/a.png',
  name: 'a.png',
  ext: 'png',
  size: 1024,
  thumb: 'http://localhost:0/media/a.png',
};
const txtItem: AttachmentItem = { path: 'C:/tmp/b.txt', name: 'b.txt', ext: 'txt', size: 8 };

describe('AttachmentChips 图片缩略图 chip', () => {
  it('有 thumb 的附件渲染 img.ac-thumb 且 src 正确；无 thumb 的渲染 emoji 图标', () => {
    const wrapper = mount(AttachmentChips, { props: { items: [imgItem, txtItem] } });

    const thumbs = wrapper.findAll('img.ac-thumb');
    expect(thumbs.length).toBe(1);
    expect(thumbs[0].attributes('src')).toBe('http://localhost:0/media/a.png');
    expect(wrapper.findAll('.ac-icon').length).toBe(1);
  });

  it('点击带缩略图的 chip 触发 open 事件并携带该附件 path', async () => {
    const wrapper = mount(AttachmentChips, { props: { items: [{ ...imgItem }] } });

    await wrapper.get('.attach-chip').trigger('click');
    expect(wrapper.emitted('open')).toEqual([['C:/tmp/a.png']]);
  });

  it('点击 × 触发 remove 且不触发 open（stop 阻止冒泡）', async () => {
    const wrapper = mount(AttachmentChips, { props: { items: [{ ...imgItem }] } });

    await wrapper.get('.ac-remove').trigger('click');
    expect(wrapper.emitted('remove')).toEqual([['C:/tmp/a.png']]);
    expect(wrapper.emitted('open')).toBeUndefined();
  });

  it('缩略图加载失败（img error）后该 chip 回退渲染 emoji 图标且不可再触发 open', async () => {
    const items = reactive<AttachmentItem[]>([{ ...imgItem }]);
    const wrapper = mount(AttachmentChips, { props: { items } });
    expect(wrapper.find('img.ac-thumb').exists()).toBe(true);

    await wrapper.get('img.ac-thumb').trigger('error');
    expect(wrapper.find('img.ac-thumb').exists()).toBe(false);
    expect(wrapper.find('.ac-icon').exists()).toBe(true);

    await wrapper.get('.attach-chip').trigger('click');
    expect(wrapper.emitted('open')).toBeUndefined();
  });

  it('loading 占位 chip 不渲染缩略图，保持 emoji + 加载条', () => {
    const loadingItem: AttachmentItem = { path: 'C:/tmp/c.png', name: 'c.png', ext: 'png', size: null, loading: true };
    const wrapper = mount(AttachmentChips, { props: { items: [loadingItem] } });

    expect(wrapper.find('.ac-thumb').exists()).toBe(false);
    expect(wrapper.find('.ac-icon').exists()).toBe(true);
    expect(wrapper.find('.attach-chip.loading').exists()).toBe(true);
    expect(wrapper.find('.ac-progress').exists()).toBe(true);
  });
});
