import { mount } from '@vue/test-utils';
import { defineComponent, ref } from 'vue';
import { describe, expect, it } from 'vitest';
import ComposerEditor from './ComposerEditor.vue';

// 富文本输入框：对外是纯文本（@{会话名} / @路径），对内是「文本节点 + 胶囊元素」。
// 这里覆盖三件要紧的事：胶囊渲染、序列化回文本、以及「退格一次删掉整个引用」。

function mountEditor(modelValue: string) {
  return mount(ComposerEditor, {
    props: { modelValue, placeholder: '说点什么' },
    attachTo: document.body,
  });
}

/** 把光标折叠到编辑内容末尾（jsdom 的选区 API 够用，但必须 attachTo document） */
function caretToEnd(el: HTMLElement) {
  const range = document.createRange();
  range.selectNodeContents(el);
  range.collapse(false);
  const sel = document.getSelection();
  sel?.removeAllRanges();
  sel?.addRange(range);
}

describe('ComposerEditor @ 引用胶囊', () => {
  it('会话引用与文件引用渲染成不同样式的胶囊，标签只显示名字', () => {
    const wrapper = mountEditor('看下 @{缓存改造} 和 @src/a.ts');
    const root = wrapper.get('.ce-input').element;

    const session = root.querySelectorAll('.ce-chip-session');
    const file = root.querySelectorAll('.ce-chip-file');
    expect(session).toHaveLength(1);
    expect(file).toHaveLength(1);
    expect(session[0].textContent).toContain('缓存改造');
    expect(file[0].textContent).toContain('src/a.ts');
    // 胶囊是不可编辑的原子元素，光标不会钻进里面改坏文本
    expect((session[0] as HTMLElement).contentEditable).toBe('false');
    wrapper.unmount();
  });

  it('胶囊内部仍保留原始 token，序列化后与原文逐字符相等', async () => {
    const wrapper = mountEditor('先 @{对话A} 再 @docs/接口文档.md 结束');

    // 触发一次输入事件即可看到组件回写出去的文本
    await wrapper.get('.ce-input').trigger('input');
    expect(wrapper.emitted('update:modelValue')?.at(-1)).toEqual([
      '先 @{对话A} 再 @docs/接口文档.md 结束',
    ]);
    wrapper.unmount();
  });

  it('退格紧邻胶囊时整块删除引用，而不是删掉最后一个字符', async () => {
    const wrapper = mountEditor('看下 @{缓存改造}');
    const root = wrapper.get('.ce-input').element as HTMLElement;

    caretToEnd(root);
    await wrapper.get('.ce-input').trigger('keydown', { key: 'Backspace' });

    expect(wrapper.emitted('update:modelValue')?.at(-1)).toEqual(['看下 ']);
    expect(root.querySelector('.ce-chip')).toBeNull();
    wrapper.unmount();
  });

  it('光标在普通文字中间时退格仍是逐字删除（不误删相邻引用）', async () => {
    const wrapper = mountEditor('@{缓存改造} 后面还有字');
    const root = wrapper.get('.ce-input').element as HTMLElement;

    // 光标放到「后面」两个字之间
    const textNode = Array.from(root.childNodes).find(
      n => n.nodeType === Node.TEXT_NODE && (n.textContent || '').includes('后面'),
    ) as Text;
    const range = document.createRange();
    range.setStart(textNode, 2);
    range.collapse(true);
    const sel = document.getSelection();
    sel?.removeAllRanges();
    sel?.addRange(range);

    await wrapper.get('.ce-input').trigger('keydown', { key: 'Backspace' });
    // 未被组件接管（返回 false），引用完好
    expect(root.querySelector('.ce-chip')).not.toBeNull();
    wrapper.unmount();
  });

  it('Enter 提交发送，Shift+Enter 插入换行', async () => {
    const wrapper = mountEditor('第一行');
    const root = wrapper.get('.ce-input').element as HTMLElement;

    caretToEnd(root);
    await wrapper.get('.ce-input').trigger('keydown', { key: 'Enter' });
    expect(wrapper.emitted('submit')).toHaveLength(1);

    await wrapper.get('.ce-input').trigger('keydown', { key: 'Enter', shiftKey: true });
    expect(wrapper.emitted('submit')).toHaveLength(1);
    expect(root.textContent).toContain('第一行\n');
    wrapper.unmount();
  });

  it('外部改值（切换会话/补全插入）时重建内容，胶囊随之更新', async () => {
    const wrapper = mountEditor('');
    await wrapper.setProps({ modelValue: '@{新的对话}' });
    expect(wrapper.findAll('.ce-chip-session')).toHaveLength(1);
    wrapper.unmount();
  });

  it('空内容时带 is-empty 标记（占位文案靠它显示）', async () => {
    const wrapper = mountEditor('');
    expect(wrapper.get('.ce-input').classes()).toContain('is-empty');
    await wrapper.setProps({ modelValue: '有内容了' });
    expect(wrapper.get('.ce-input').classes()).not.toContain('is-empty');
    wrapper.unmount();
  });

  it('父组件在 keydown 中 preventDefault 并修改 v-model（如 Enter 发送清空）时，不回写旧文本且 DOM 清空', async () => {
    const Host = defineComponent({
      components: { ComposerEditor },
      setup() {
        const text = ref('');
        const sent: string[] = [];
        function onKeydown(e: KeyboardEvent) {
          if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            sent.push(text.value);
            text.value = '';
          }
        }
        return { text, sent, onKeydown };
      },
      template: '<ComposerEditor v-model="text" @keydown="onKeydown" />',
    });

    const wrapper = mount(Host, { attachTo: document.body });
    const editor = wrapper.get('.ce-input');
    editor.element.textContent = '为什么会被拦住';
    await editor.trigger('input');
    expect(wrapper.vm.text).toBe('为什么会被拦住');

    await editor.trigger('keydown', { key: 'Enter' });

    expect(wrapper.vm.sent).toEqual(['为什么会被拦住']);
    expect(wrapper.vm.text).toBe('');
    expect(editor.element.textContent).toBe('');
    expect(editor.classes()).toContain('is-empty');
    wrapper.unmount();
  });

  it('输入法一开始组词就收起占位提示，确认前不提交文本', async () => {
    const wrapper = mountEditor('');
    const editor = wrapper.get('.ce-input');
    expect(editor.classes()).toContain('is-empty');

    await editor.trigger('compositionstart');
    expect(editor.classes()).not.toContain('is-empty');
    expect(wrapper.emitted('update:modelValue')).toBeUndefined();

    editor.element.textContent = 'nihao';
    await editor.trigger('input');
    expect(editor.classes()).not.toContain('is-empty');
    expect(wrapper.emitted('update:modelValue')).toBeUndefined();

    await editor.trigger('compositionend');
    expect(wrapper.emitted('update:modelValue')?.at(-1)).toEqual(['nihao']);
    expect(editor.classes()).not.toContain('is-empty');

    // 取消组词后内容为空，占位提示回来
    editor.element.textContent = '';
    await editor.trigger('compositionstart');
    expect(editor.classes()).not.toContain('is-empty');
    await editor.trigger('compositionend');
    expect(editor.classes()).toContain('is-empty');
    wrapper.unmount();
  });

  it('输入法组词期间按 Enter 不向父组件透传 keydown，也不触发 submit', async () => {
    const wrapper = mountEditor('nihao');
    const editor = wrapper.get('.ce-input');

    await editor.trigger('compositionstart');
    await editor.trigger('keydown', { key: 'Enter' });
    expect(wrapper.emitted('keydown')).toBeUndefined();
    expect(wrapper.emitted('submit')).toBeUndefined();

    await editor.trigger('compositionend');

    // 原生 isComposing=true（即使 compositionstart 未先到）同样拦截
    editor.element.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Enter', isComposing: true, bubbles: true, cancelable: true }),
    );
    expect(wrapper.emitted('keydown')).toBeUndefined();
    expect(wrapper.emitted('submit')).toBeUndefined();

    await editor.trigger('keydown', { key: 'Enter' });
    expect(wrapper.emitted('keydown')).toHaveLength(1);
    expect(wrapper.emitted('submit')).toHaveLength(1);
    wrapper.unmount();
  });
});