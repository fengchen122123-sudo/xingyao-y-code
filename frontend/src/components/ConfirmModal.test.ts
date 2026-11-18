import { nextTick } from 'vue';
import { mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, describe, expect, it } from 'vitest';

import ConfirmModal from './ConfirmModal.vue';
import { useAppStore } from '../stores/appStore';

function mountModal() {
  return mount(ConfirmModal, {
    attachTo: document.body,
    global: {
      stubs: {
        // 停用 Transition 动画包裹延迟，确保 show 切换同步反映在 DOM 上
        Transition: false,
      },
    },
  });
}

describe('ConfirmModal', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  it('渲染确认弹窗标题、副标题、正文与纯文本按钮（不带多余按键符号）', async () => {
    const store = useAppStore();
    const wrapper = mountModal();

    expect(wrapper.find('[data-test="confirm-modal"]').exists()).toBe(false);

    const promise = store.askConfirm(
      '删除项目分组',
      '仅移出侧栏分组，磁盘文件不受影响',
      '确定要删除项目分组「.ycode」吗？',
    );
    await nextTick();

    expect(wrapper.find('[data-test="confirm-modal"]').exists()).toBe(true);
    expect(wrapper.get('.cm-title').text()).toBe('删除项目分组');
    expect(wrapper.get('.cm-sub').text()).toBe('仅移出侧栏分组，磁盘文件不受影响');
    expect(wrapper.get('.cm-body').text()).toBe('确定要删除项目分组「.ycode」吗？');
    expect(wrapper.get('[data-test="confirm-modal-cancel"]').text()).toBe('取消');
    expect(wrapper.get('[data-test="confirm-modal-ok"]').text()).toBe('确定删除');
    expect(wrapper.get('[data-test="confirm-modal-ok"]').classes()).toContain('tone-danger');

    await wrapper.get('[data-test="confirm-modal-ok"]').trigger('click');
    expect(await promise).toBe(true);
    expect(store.confirmModal.show).toBe(false);
    wrapper.unmount();
  });

  it('无副标题时不渲染占位副标题；历史调用传入的 ⏎ 也会被自动清理', async () => {
    const store = useAppStore();
    const wrapper = mountModal();

    const promise = store.askConfirm('清空历史', '', '确认清空？', '确定清空 ⏎');
    await nextTick();

    expect(wrapper.find('.cm-sub').exists()).toBe(false);
    expect(wrapper.get('[data-test="confirm-modal-ok"]').text()).toBe('确定清空');

    await wrapper.get('[data-test="confirm-modal-cancel"]').trigger('click');
    expect(await promise).toBe(false);
    wrapper.unmount();
  });

  it('支持 Esc 键、右上角关闭按钮与遮罩点击取消，且从弹窗内拖出到遮罩松手不误关', async () => {
    const store = useAppStore();
    const wrapper = mountModal();

    // 1. Esc 取消
    const p1 = store.askConfirm('删除模型', 'sub', '确定删除？');
    await nextTick();
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    expect(await p1).toBe(false);
    await nextTick();
    expect(wrapper.find('[data-test="confirm-modal"]').exists()).toBe(false);

    // 2. 右上角关闭钮取消
    const p2 = store.askConfirm('删除模型', 'sub', '确定删除？');
    await nextTick();
    await wrapper.get('[data-test="confirm-modal-close"]').trigger('click');
    expect(await p2).toBe(false);
    await nextTick();

    // 3. 弹窗内按住拖出到遮罩松手（仅有 click 没有 mousedown on mask）不应关闭
    const p3 = store.askConfirm('删除模型', 'sub', '确定删除？');
    await nextTick();
    await wrapper.get('[data-test="confirm-modal"]').trigger('mousedown');
    await wrapper.get('[data-test="confirm-modal-mask"]').trigger('click');
    expect(store.confirmModal.show).toBe(true);

    // 4. 在遮罩上完整 mousedown + click 才会取消
    await wrapper.get('[data-test="confirm-modal-mask"]').trigger('mousedown');
    await wrapper.get('[data-test="confirm-modal-mask"]').trigger('click');
    expect(await p3).toBe(false);

    wrapper.unmount();
  });

  it('非破坏性 confirm 支持 primary tone，不强行染红危险按钮', async () => {
    const store = useAppStore();
    const wrapper = mountModal();

    const promise = store.askConfirm(
      '新版本已就绪',
      '星瑶Code v1.3.0',
      '是否立即启动安装程序？',
      '立即安装',
      { tone: 'primary' },
    );
    await nextTick();

    expect(wrapper.get('.cm-icon').classes()).toContain('tone-primary');
    expect(wrapper.get('[data-test="confirm-modal-ok"]').classes()).toContain('tone-primary');
    expect(wrapper.get('[data-test="confirm-modal-ok"]').classes()).not.toContain('tone-danger');

    await wrapper.get('[data-test="confirm-modal-ok"]').trigger('click');
    expect(await promise).toBe(true);
    wrapper.unmount();
  });

  it('notice 模式按提示语义匹配柔和图标并隐藏取消按钮，点遮罩不误关', async () => {
    const store = useAppStore();
    const wrapper = mountModal();

    store.notifyModal('连接失败', '后端连接失败：不可用');
    await nextTick();
    expect(wrapper.find('[data-test="confirm-modal-cancel"]').exists()).toBe(false);
    expect(wrapper.get('.cm-icon').classes()).toContain('tone-error');
    expect(wrapper.get('[data-test="confirm-modal-ok"]').classes()).toContain('tone-primary');

    // 点遮罩不关闭通知弹窗
    await wrapper.get('[data-test="confirm-modal-mask"]').trigger('mousedown');
    await wrapper.get('[data-test="confirm-modal-mask"]').trigger('click');
    expect(store.confirmModal.show).toBe(true);

    // 成功提示归入 success 语义
    store.notifyModal('导出成功', '主题已导出到桌面');
    await nextTick();
    expect(wrapper.get('.cm-icon').classes()).toContain('tone-success');

    // Esc 可关闭通知弹窗
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    await nextTick();
    expect(store.confirmModal.show).toBe(false);
    wrapper.unmount();
  });

  it('连续调用 askConfirm 时会安全取消上一个未决确认框', async () => {
    const store = useAppStore();
    let firstAnswer: boolean | null = null;
    void store.askConfirm('操作一', '', '确认一？').then((ok) => {
      firstAnswer = ok;
    });

    const secondPromise = store.askConfirm('操作二', '', '确认二？');
    await new Promise((r) => setTimeout(r, 0));
    expect(firstAnswer).toBe(false);

    store.resolveConfirm(true);
    expect(await secondPromise).toBe(true);
  });
});
