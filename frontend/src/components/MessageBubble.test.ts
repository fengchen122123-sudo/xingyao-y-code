import { mount, flushPromises } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import MessageBubble from './MessageBubble.vue';
import { useAppStore, type ChatMessage } from '../stores/appStore';
import type { ToolExecutionCall } from '../utils/toolTrace';


const finishedCommandCall = (id: string): ToolExecutionCall => ({
  id, name: 'execute_command', label: '运行命令 npm test',
  display: {
    category: 'command', action: '运行命令', target: 'npm test', command: 'npm test',
    metrics: {}, error_summary: '', is_skill: false,
  },
  result: { ok: true, summary: '完成', result: 'ok', duration_ms: 100 },
});

function assistantMessage(overrides: Partial<ChatMessage> = {}): ChatMessage {
  return {
    role: 'assistant',
    content: '',
    reasoning_content: '',
    tool_calls: [],
    legacy_diffs: [],
    execution_state: 'completed',
    ...overrides,
  };
}

beforeEach(() => setActivePinia(createPinia()));

describe('MessageBubble', () => {
  it('renders notice messages as a hint card instead of an error card', () => {
    const wrapper = mount(MessageBubble, {
      props: { msg: { role: 'notice', content: '已切换活动模型: k3-256k' } },
    });

    expect(wrapper.get('.notice-card').text()).toContain('提示');
    expect(wrapper.get('.notice-card').text()).toContain('已切换活动模型: k3-256k');
    expect(wrapper.find('.error-card').exists()).toBe(false);
  });

  it('renders persistent attachment notes inside the user bubble', () => {
    const wrapper = mount(MessageBubble, {
      props: {
        msg: {
          role: 'user',
          content: '请分析图片',
          attachment_notes: ['🖼️ 已附加图片: sample.png（压缩后 320KB）'],
        },
      },
    });

    expect(wrapper.get('.attachment-notes').text()).toContain('sample.png');
    expect(wrapper.get('.attachment-note').text()).toContain('压缩后 320KB');
    wrapper.unmount();
  });

  it('orders thinking block, execution trace, then markdown answer', () => {
    const msg = assistantMessage({
      reasoning_content: '先想想怎么做',
      content: '最终答案',
      tool_calls: [finishedCommandCall('c1')],
      execution_state: 'completed',
    });
    const wrapper = mount(MessageBubble, { props: { msg } });

    const think = wrapper.get('.think-block').element;
    const trace = wrapper.get('.exec-trace').element;
    const md = wrapper.get('.md-body').element;
    expect(think.compareDocumentPosition(md) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(md.compareDocumentPosition(trace) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(wrapper.get('.md-body').text()).toContain('最终答案');
    wrapper.unmount();
  });

  it('hides the empty working line after the first tool call arrives', async () => {
    const store = useAppStore();
    store.messages.push(assistantMessage({ execution_state: 'running' }));
    store.running = true;
    // 用 store 内部的响应式代理作为 props，模拟 App.vue 的 v-for 传参
    const msg = store.messages[store.messages.length - 1];
    const wrapper = mount(MessageBubble, { props: { msg } });
    expect(wrapper.find('.working-line').exists()).toBe(true);

    msg.tool_calls!.push({
      id: 'c1', name: 'execute_command', label: '运行命令 npm test',
      started_at_ms: Date.now(),
    });
    await wrapper.vm.$nextTick();
    expect(wrapper.find('.working-line').exists()).toBe(false);
    wrapper.unmount();
  });

  it('renders no legacy tool-card or standalone diff-list', () => {
    const msg = assistantMessage({
      tool_calls: [{
        ...finishedCommandCall('c1'),
        diff: { file_path: 'a.py', diff_text: '--- a.py\n+++ a.py\n-old\n+new' },
      }],
      legacy_diffs: [{ file_path: 'old.py', diff_text: '--- old.py\n+++ old.py\n-x\n+y' }],
      execution_state: 'completed',
    });
    const wrapper = mount(MessageBubble, { props: { msg } });

    expect(wrapper.find('.tool-card').exists()).toBe(false);
    expect(wrapper.find('.diff-list').exists()).toBe(false);
    const trace = wrapper.get('.exec-trace');
    expect(trace.text()).toContain('运行命令');
    expect(trace.text()).toContain('未绑定的历史文件变更');
    wrapper.unmount();
  });

  it('用户消息下方操作栏有复制按钮，点击复制消息文本', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });

    const wrapper = mount(MessageBubble, {
      props: { msg: { role: 'user', content: '帮我查一下库存' } },
    });

    const btn = wrapper.find('[data-test="user-copy-btn"]');
    expect(btn.exists()).toBe(true);
    await btn.trigger('click');
    await flushPromises();
    expect(writeText).toHaveBeenCalledWith('帮我查一下库存');
    wrapper.unmount();
  });

  it('纯图片消息（无文本）不显示复制按钮', () => {
    const wrapper = mount(MessageBubble, {
      props: { msg: { role: 'user', images: ['data:image/png;base64,xxx'] } },
    });
    expect(wrapper.find('[data-test="user-copy-btn"]').exists()).toBe(false);
    wrapper.unmount();
  });

  it('点击本地文件链接用绝对路径打开内嵌预览', async () => {
    const store = useAppStore();
    store.workdir = 'D:\\proj';
    const readFilePreview = vi.fn().mockResolvedValue({ ok: true, content: 'hello', truncated: false, path: 'D:\\proj\\docs\\foo.md' });
    (window as any).pywebview = { api: { read_file_preview: readFilePreview } };

    const wrapper = mount(MessageBubble, {
      props: { msg: assistantMessage({ content: '见 docs/foo.md 文件' }) },
    });

    const link = wrapper.find('a[data-file-link]');
    expect(link.exists()).toBe(true);
    await link.trigger('click');
    await flushPromises();

    expect(readFilePreview).toHaveBeenCalledWith('D:\\proj\\docs\\foo.md');
    wrapper.unmount();
    delete (window as any).pywebview;
  });

  it('相对路径且未绑定工作目录时提示，不发起预览', async () => {
    const store = useAppStore();
    store.workdir = '';
    const readFilePreview = vi.fn();
    (window as any).pywebview = { api: { read_file_preview: readFilePreview } };
    const notify = vi.spyOn(store, 'notifyModal').mockImplementation(() => {});

    const wrapper = mount(MessageBubble, {
      props: { msg: assistantMessage({ content: 'docs/foo.md' }) },
    });

    await wrapper.find('a[data-file-link]').trigger('click');
    await flushPromises();

    expect(notify).toHaveBeenCalled();
    expect(readFilePreview).not.toHaveBeenCalled();
    wrapper.unmount();
    delete (window as any).pywebview;
  });

  it('目录与不存在的路径链接降级为纯文本，真实文件保持链接', async () => {
    const store = useAppStore();
    store.workdir = 'D:\\proj';
    // 组件可能因 immediate watcher + ref 变化多次触发、且只传未缓存的子集，
    // mock 必须按路径语义返回而不是依赖数组顺序
    vi.spyOn(store, 'checkPathsKind').mockImplementation(async (paths: string[]) =>
      paths.map(p => ({
        path: p,
        kind: p.endsWith('dist') ? 'dir' as const : p.endsWith('gone.md') ? 'missing' as const : 'file' as const,
      })),
    );

    const wrapper = mount(MessageBubble, {
      props: { msg: assistantMessage({ content: '目录 D:\\proj\\dist，文件 D:\\proj\\ok.md，缺失 D:\\proj\\gone.md' }) },
    });
    await flushPromises();


    const links = wrapper.findAll('a[data-file-link]');
    expect(links.length).toBe(1);
    expect(links[0].text()).toBe('D:\\proj\\ok.md');
    const text = wrapper.get('.md-body').text();
    expect(text).toContain('D:\\proj\\dist');
    expect(text).toContain('D:\\proj\\gone.md');
    wrapper.unmount();
  });

  it('路径类型校验失败时保留链接原样', async () => {
    const store = useAppStore();
    store.workdir = 'D:\\proj';
    vi.spyOn(store, 'checkPathsKind').mockRejectedValue(new Error('api down'));

    const wrapper = mount(MessageBubble, {
      props: { msg: assistantMessage({ content: '见 D:\\proj\\docs\\foo.md 文件' }) },
    });
    await flushPromises();

    expect(wrapper.findAll('a[data-file-link]').length).toBe(1);
    wrapper.unmount();
  });

  it('点击 mermaid 图打开灯箱：参数为单元素 data URL 数组', async () => {
    const store = useAppStore();
    const open = vi.spyOn(store, 'openLightbox');
    const wrapper = mount(MessageBubble, {
      props: { msg: assistantMessage({ content: '下面是流程图' }) },
    });

    // 模拟 mermaidRenderer.injectSvg 注入的净化后 SVG 容器（v-html 异步注入，
    // 测试直接补 DOM，走同一个点击委托入口）
    const holder = document.createElement('div');
    holder.className = 'mermaid-svg';
    holder.innerHTML = '<svg xmlns="http://www.w3.org/2000/svg" width="10"><text>节点</text></svg>';
    wrapper.get('.md-body').element.appendChild(holder);

    holder.querySelector('text')!.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    await wrapper.vm.$nextTick();

    expect(open).toHaveBeenCalledTimes(1);
    const [images, index] = open.mock.calls[0];
    expect(index).toBe(0);
    expect(images).toHaveLength(1);
    // 灯箱走 v-html 直注 SVG（与气泡同路径），传的就是净化后的 SVG 字符串本身
    expect(images[0]).toBe(holder.innerHTML);
    expect(images[0]).toMatch(/^\s*<svg/);
    wrapper.unmount();
  });

  it('点击空的 mermaid 容器不进灯箱', async () => {
    const store = useAppStore();
    const open = vi.spyOn(store, 'openLightbox');
    const wrapper = mount(MessageBubble, {
      props: { msg: assistantMessage({ content: '下面是流程图' }) },
    });

    const holder = document.createElement('div');
    holder.className = 'mermaid-svg';
    wrapper.get('.md-body').element.appendChild(holder);

    holder.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    await wrapper.vm.$nextTick();

    expect(open).not.toHaveBeenCalled();
    wrapper.unmount();
  });
});
