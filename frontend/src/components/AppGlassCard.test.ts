import { beforeEach, describe, expect, it } from 'vitest';
import { mount } from '@vue/test-utils';
import { nextTick } from 'vue';

import AppGlassCard from './AppGlassCard.vue';
import {
  GLASS_BLUR_MAX,
  GLASS_OPACITY_MAX,
  glassBlur,
  glassOpacity,
  loadAppGlass,
  setGlassBlur,
  setGlassOpacity,
} from '../composables/appGlass';

// 与 composable 里的 key 保持一致（硬编码是为了「改 key 就会红」）
const STORAGE_KEY = 'ycode.appGlass';

/** 根变量由 CSS 的 body::after 消费；读 documentElement 上的 inline 值即最终效果。 */
function rootVar(name: string): string {
  return document.documentElement.style.getPropertyValue(name).trim();
}

/** composable 的状态是模块级的，同一测试文件内需要手动归零（默认 = 关闭）。 */
async function resetGlass() {
  setGlassOpacity(0);
  setGlassBlur(0);
  await nextTick();
}

function mountCard() {
  return mount(AppGlassCard);
}

describe('appGlass - 全局玻璃层 composable', () => {
  beforeEach(async () => {
    localStorage.clear();
    await resetGlass();
  });

  it('默认 0：两个根变量都是 0，玻璃层不产生可见效果', () => {
    // 冷启动路径：localStorage 里没有偏好 → loadAppGlass() 写回 0%，即 body::after 的
    // color-mix 混入 0% 前景色、blur(0px) 为空操作，与未加玻璃层前完全一致。
    loadAppGlass();
    expect(glassOpacity.value).toBe(0);
    expect(glassBlur.value).toBe(0);
    expect(rootVar('--app-glass-opacity')).toBe('0%');
    expect(rootVar('--app-glass-blur')).toBe('0px');
  });

  it('setGlassOpacity / setGlassBlur 写根变量（百分比 + px）', async () => {
    setGlassOpacity(60);
    setGlassBlur(24);
    await nextTick();
    expect(rootVar('--app-glass-opacity')).toBe('60%');
    expect(rootVar('--app-glass-blur')).toBe('24px');
  });

  it('取值被 clamp 到 0~100 / 0~40，非法数值退化为 0', async () => {
    setGlassOpacity(9999);
    expect(glassOpacity.value).toBe(GLASS_OPACITY_MAX);
    setGlassOpacity(-5);
    expect(glassOpacity.value).toBe(0);
    expect(GLASS_OPACITY_MAX).toBe(100);

    setGlassBlur(9999);
    expect(glassBlur.value).toBe(GLASS_BLUR_MAX);
    setGlassBlur(-1);
    expect(glassBlur.value).toBe(0);
    expect(GLASS_BLUR_MAX).toBe(40);

    setGlassOpacity(Number.NaN);
    setGlassBlur(Number.POSITIVE_INFINITY);
    expect(glassOpacity.value).toBe(0);
    expect(glassBlur.value).toBe(0);
  });

  it('参数变化后持久化到 localStorage 的 ycode.appGlass', async () => {
    setGlassOpacity(40);
    setGlassBlur(10);
    await nextTick();
    expect(JSON.parse(localStorage.getItem(STORAGE_KEY) as string)).toEqual({
      opacity: 40,
      blur: 10,
    });
  });

  it('loadAppGlass 从 localStorage 恢复并立即应用', () => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ opacity: 60, blur: 18 }));
    loadAppGlass();
    expect(glassOpacity.value).toBe(60);
    expect(glassBlur.value).toBe(18);
    // loadAppGlass 内部同步 apply()，无需等 watch 的微任务
    expect(rootVar('--app-glass-opacity')).toBe('60%');
    expect(rootVar('--app-glass-blur')).toBe('18px');
  });

  it('loadAppGlass 对越界存量值同样 clamp', () => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ opacity: 500, blur: -8 }));
    loadAppGlass();
    expect(glassOpacity.value).toBe(GLASS_OPACITY_MAX);
    expect(glassBlur.value).toBe(0);
  });

  it('loadAppGlass 遇到非法 JSON 不崩溃，保持默认关闭', () => {
    localStorage.setItem(STORAGE_KEY, '{不是合法 JSON');
    expect(() => loadAppGlass()).not.toThrow();
    expect(glassOpacity.value).toBe(0);
    expect(glassBlur.value).toBe(0);
    expect(rootVar('--app-glass-opacity')).toBe('0%');
  });
});

describe('AppGlassCard - 设置卡片交互', () => {
  beforeEach(async () => {
    localStorage.clear();
    await resetGlass();
  });

  it('渲染两个滑杆与三个预设按钮，测试标记与源实现一致', () => {
    const wrapper = mountCard();
    expect(wrapper.find('[data-test="app-glass-card"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="glass-opacity"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="glass-blur"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="glass-preset-light"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="glass-preset-strong"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="glass-reset"]').exists()).toBe(true);
  });

  it('拖动透明度滑杆：写 CSS 变量并显示百分比', async () => {
    const wrapper = mountCard();
    await wrapper.find('[data-test="glass-opacity"]').setValue('60');
    await nextTick();
    expect(glassOpacity.value).toBe(60);
    expect(rootVar('--app-glass-opacity')).toBe('60%');
    expect(wrapper.text()).toContain('60%');
  });

  it('拖动模糊滑杆：写 px 变量', async () => {
    const wrapper = mountCard();
    await wrapper.find('[data-test="glass-blur"]').setValue('24');
    await nextTick();
    expect(glassBlur.value).toBe(24);
    expect(rootVar('--app-glass-blur')).toBe('24px');
    expect(wrapper.text()).toContain('24px');
  });

  it('滑杆越界输入仍被 clamp', async () => {
    const wrapper = mountCard();
    await wrapper.find('[data-test="glass-opacity"]').setValue('150');
    await wrapper.find('[data-test="glass-blur"]').setValue('99');
    await nextTick();
    expect(glassOpacity.value).toBe(GLASS_OPACITY_MAX);
    expect(glassBlur.value).toBe(GLASS_BLUR_MAX);
    expect(rootVar('--app-glass-opacity')).toBe('100%');
    expect(rootVar('--app-glass-blur')).toBe('40px');
  });

  it('三个预设按钮：轻磨砂 / 重磨砂 / 恢复默认', async () => {
    const wrapper = mountCard();

    await wrapper.find('[data-test="glass-preset-light"]').trigger('click');
    await nextTick();
    expect([glassOpacity.value, glassBlur.value]).toEqual([25, 16]);
    expect(rootVar('--app-glass-opacity')).toBe('25%');
    expect(rootVar('--app-glass-blur')).toBe('16px');

    await wrapper.find('[data-test="glass-preset-strong"]').trigger('click');
    await nextTick();
    expect([glassOpacity.value, glassBlur.value]).toEqual([45, 28]);
    expect(rootVar('--app-glass-opacity')).toBe('45%');

    await wrapper.find('[data-test="glass-reset"]').trigger('click');
    await nextTick();
    expect([glassOpacity.value, glassBlur.value]).toEqual([0, 0]);
    expect(rootVar('--app-glass-opacity')).toBe('0%');
    expect(rootVar('--app-glass-blur')).toBe('0px');
    // 预设同样是持久化状态：写回 localStorage
    expect(JSON.parse(localStorage.getItem(STORAGE_KEY) as string)).toEqual({
      opacity: 0,
      blur: 0,
    });
  });
});
