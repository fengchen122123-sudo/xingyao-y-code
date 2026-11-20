/// <reference types="node" />
import { beforeEach, describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { mount } from '@vue/test-utils';
import { nextTick } from 'vue';

import ChatLayoutWidthCard from './ChatLayoutWidthCard.vue';
import {
  chatLayoutWidth,
  loadChatLayoutWidth,
  setChatLayoutWidth,
} from '../composables/chatLayoutWidth';

const STORAGE_KEY = 'ycode.chatLayoutWidth';

describe('chatLayoutWidth - 对话区宽度偏好', () => {
  beforeEach(async () => {
    localStorage.clear();
    delete document.documentElement.dataset.chatWidth;
    setChatLayoutWidth('wide');
    await nextTick();
  });

  it('默认为 wide（宽屏自适应）并同步写入 html[data-chat-width]', () => {
    loadChatLayoutWidth();
    expect(chatLayoutWidth.value).toBe('wide');
    expect(document.documentElement.dataset.chatWidth).toBe('wide');
  });

  it('切换选项会更新 dataset 与 localStorage，非法值回落 wide', async () => {
    const wrapper = mount(ChatLayoutWidthCard);
    expect(wrapper.find('[data-test="chat-width-card"]').exists()).toBe(true);

    await wrapper.get('[data-test="chat-width-full"]').trigger('click');
    await nextTick();
    expect(chatLayoutWidth.value).toBe('full');
    expect(document.documentElement.dataset.chatWidth).toBe('full');
    expect(localStorage.getItem(STORAGE_KEY)).toBe('full');
    expect(wrapper.get('[data-test="chat-width-full"]').attributes('aria-checked')).toBe('true');

    await wrapper.get('[data-test="chat-width-compact"]').trigger('click');
    await nextTick();
    expect(chatLayoutWidth.value).toBe('compact');
    expect(document.documentElement.dataset.chatWidth).toBe('compact');
    expect(localStorage.getItem(STORAGE_KEY)).toBe('compact');

    localStorage.setItem(STORAGE_KEY, 'unknown-mode');
    loadChatLayoutWidth();
    expect(chatLayoutWidth.value).toBe('wide');
    expect(document.documentElement.dataset.chatWidth).toBe('wide');
    wrapper.unmount();
  });

  it('style.css 与 App.vue 将对话列宽绑定到 #center 容器维度', () => {
    const styleSource = readFileSync(resolve(process.cwd(), 'src/style.css'), 'utf-8');
    const appSource = readFileSync(resolve(process.cwd(), 'src/App.vue'), 'utf-8');
    const bgSource = readFileSync(resolve(process.cwd(), 'src/components/BackgroundTasksBar.vue'), 'utf-8');

    expect(appSource).toMatch(/#center\s*\{[^}]*container-type:\s*inline-size/);
    expect(styleSource).toContain('#center > *');
    expect(styleSource).toContain('html[data-chat-width="compact"] #center > *');
    expect(styleSource).toContain('html[data-chat-width="full"] #center > *');
    expect(styleSource).toContain('clamp(820px, 92cqw, 1060px)');
    expect(styleSource).toContain('var(--empty-col-width, 660px)');
    expect(bgSource).toContain('.bg-entry-wrap');
    expect(appSource).toContain('<BackgroundTasksBar');
  });
});
