import { readFileSync } from 'node:fs';
import { mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, describe, expect, it } from 'vitest';
import ImageLightbox from './ImageLightbox.vue';
import { useAppStore } from '../stores/appStore';

// 灯箱缩放/平移是组件本地状态，测试通过真实 store 打开灯箱后
// 驱动 DOM 事件（wheel / pointer / dblclick / Esc）断言 img 的 transform。

beforeEach(() => setActivePinia(createPinia()));

function openSingleImage() {
  const store = useAppStore();
  store.openLightbox(['data:image/svg+xml;charset=utf-8,x'], 0);
  return store;
}

function imgTransform(wrapper: ReturnType<typeof mount>): string {
  return wrapper.get('.lb-img').attributes('style') || '';
}

describe('ImageLightbox 缩放与拖动', () => {
  it('滚轮放大/缩小，倍率写入 transform 并显示百分比角标', async () => {
    openSingleImage();
    const wrapper = mount(ImageLightbox);
    const mask = wrapper.get('.lightbox-mask');

    await mask.trigger('wheel', { deltaY: -100 });
    expect(imgTransform(wrapper)).toContain('scale(1.2)');
    expect(wrapper.get('.lb-zoom').text()).toBe('120%');

    await mask.trigger('wheel', { deltaY: 100 });
    expect(imgTransform(wrapper)).toContain('scale(1)');
    expect(wrapper.find('.lb-zoom').exists()).toBe(false);
    wrapper.unmount();
  });

  it('缩放 clamp 在 [0.5, 6]', async () => {
    openSingleImage();
    const wrapper = mount(ImageLightbox);
    const mask = wrapper.get('.lightbox-mask');

    for (let i = 0; i < 20; i++) await mask.trigger('wheel', { deltaY: -100 });
    expect(imgTransform(wrapper)).toContain('scale(6)');

    for (let i = 0; i < 30; i++) await mask.trigger('wheel', { deltaY: 100 });
    expect(imgTransform(wrapper)).toContain('scale(0.5)');
    expect(wrapper.get('.lb-zoom').text()).toBe('50%');
    wrapper.unmount();
  });

  it('双击复位 zoom 与平移', async () => {
    openSingleImage();
    const wrapper = mount(ImageLightbox);
    const mask = wrapper.get('.lightbox-mask');
    const img = wrapper.get('.lb-img');

    await mask.trigger('wheel', { deltaY: -100 });
    img.element.dispatchEvent(new MouseEvent('pointerdown', { bubbles: true, clientX: 100, clientY: 100 }));
    img.element.dispatchEvent(new MouseEvent('pointermove', { bubbles: true, clientX: 130, clientY: 120 }));
    await wrapper.vm.$nextTick();
    expect(imgTransform(wrapper)).toContain('translate(30px, 20px)');

    await img.trigger('dblclick');
    expect(imgTransform(wrapper)).toContain('translate(0px, 0px) scale(1)');
    wrapper.unmount();
  });

  it('zoom=1 时不允许拖动（平移保持 0）', async () => {
    openSingleImage();
    const wrapper = mount(ImageLightbox);
    const img = wrapper.get('.lb-img');

    img.element.dispatchEvent(new MouseEvent('pointerdown', { bubbles: true, clientX: 100, clientY: 100 }));
    img.element.dispatchEvent(new MouseEvent('pointermove', { bubbles: true, clientX: 160, clientY: 150 }));
    await wrapper.vm.$nextTick();
    expect(imgTransform(wrapper)).toContain('translate(0px, 0px)');
    wrapper.unmount();
  });

  it('切换图片时复位缩放与平移', async () => {
    const store = useAppStore();
    store.openLightbox(['a', 'b'], 0);
    const wrapper = mount(ImageLightbox);
    await wrapper.get('.lightbox-mask').trigger('wheel', { deltaY: -100 });
    expect(imgTransform(wrapper)).toContain('scale(1.2)');

    store.lightboxNav(1);
    await wrapper.vm.$nextTick();
    expect(imgTransform(wrapper)).toContain('scale(1)');
    wrapper.unmount();
  });

  it('Esc 关闭灯箱（既有行为不回归）', async () => {
    const store = openSingleImage();
    const wrapper = mount(ImageLightbox);
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    await wrapper.vm.$nextTick();
    expect(store.lightbox.show).toBe(false);
    expect(wrapper.find('.lightbox-mask').exists()).toBe(false);
    wrapper.unmount();
  });

  it('SVG 分支垫主题底板（浅色主题边线在纯黑遮罩上隐形的修复）', async () => {
    // 根因：mermaid 浅色主题边线为深色描边且 SVG 背景透明，气泡里有卡片
    // 底色承托，灯箱里直接贴黑色遮罩导致边线隐形。底板样式被删除时本用例失败
    const svg = '<svg xmlns="http://www.w3.org/2000/svg" width="10"><text>x</text></svg>';
    const store = useAppStore();
    store.openLightbox([svg], 0);
    const wrapper = mount(ImageLightbox);

    // 运行时断言：渲染出的 .lb-svg 容器存在（底板类挂在它身上）
    expect(wrapper.find('.lb-svg').exists()).toBe(true);
    wrapper.unmount();

    // 样式断言：底板 background/border 必须存在于组件样式
    //（jsdom 不做 CSSOM 计算，读 SFC 源码兜底防止样式被静默移除；
    // vitest 默认以 frontend 为工作目录，import.meta.url 在 jsdom 下非
    // file 协议，不能走 new URL 拼路径）
    const source = readFileSync('src/components/ImageLightbox.vue', 'utf-8');
    const css = source.slice(source.indexOf('<style'));
    expect(css).toMatch(/\.lb-svg\s*{[^}]*background:\s*var\(--bg2\)/);
    expect(css).toMatch(/\.lb-svg\s*{[^}]*border:\s*1px solid var\(--border\)/);
  });

  it('mermaid SVG 字符串走 v-html 直注分支（不绕 data URL + img）', async () => {
    // 灯箱裂图修复回归：SVG 必须直注 DOM（与气泡同渲染路径），
    // 任何把它再包成 data URL 喂 img 的改动都应被本用例拦下
    const svg = '<svg xmlns="http://www.w3.org/2000/svg" width="10"><text>节点</text></svg>';
    const store = useAppStore();
    store.openLightbox([svg], 0);
    const wrapper = mount(ImageLightbox);

    const holder = wrapper.get('.lb-svg');
    expect(wrapper.find('.lb-img').exists()).toBe(false);
    expect(holder.html()).toContain('<text>节点</text>');

    // 缩放/平移对 SVG 分支同样生效
    await wrapper.get('.lightbox-mask').trigger('wheel', { deltaY: -100 });
    expect(holder.attributes('style')).toContain('scale(1.2)');
    wrapper.unmount();
  });
});
