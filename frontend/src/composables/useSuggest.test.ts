import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ref } from 'vue';
import { createPinia, setActivePinia } from 'pinia';
import { useAppStore } from '../stores/appStore';
import { useSuggest } from './useSuggest';

// 模拟 textarea 元素：selectionStart + setSelectionRange
function mockTextarea(value: string, cursor: number = value.length) {
  const el = {
    selectionStart: cursor,
    setSelectionRange(start: number, _end: number) { this.selectionStart = start; },
    focus() {},
  } as any;
  return el;
}

// 构造 KeyboardEvent
function key(key: string, opts: { shiftKey?: boolean } = {}): KeyboardEvent {
  return new KeyboardEvent('keydown', { key, shiftKey: opts.shiftKey || false, cancelable: true });
}

describe('useSuggest', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  it('斜杠命令：输入 / 触发候选列表，包含已启用技能', () => {
    const store = useAppStore();
    store.skills = [
      { id: 's1', name: '系统调试', short_id: 'debug', description: '排查 bug', enabled: true } as any,
      { id: 's2', name: '写计划', short_id: 'plan', description: '制定计划', enabled: false } as any,
    ];

    const inputVal = ref('/');
    const taRef = ref<HTMLTextAreaElement | null>(null);
    const { detectToken, suggestItems, suggest } = useSuggest(inputVal, taRef);

    taRef.value = mockTextarea('/', 1);
    detectToken();

    expect(suggest.value?.mode).toBe('command');
    // 只有 enabled 的技能才出现
    expect(suggestItems.value.length).toBe(1);
    expect(suggestItems.value[0].label).toBe('/debug');
  });

  it('斜杠命令：前缀过滤只返回匹配的候选', () => {
    const store = useAppStore();
    store.skills = [
      { id: 's1', name: 'A', short_id: 'alpha', description: '', enabled: true } as any,
      { id: 's2', name: 'B', short_id: 'beta', description: '', enabled: true } as any,
    ];

    const inputVal = ref('/al');
    const taRef = ref<HTMLTextAreaElement | null>(null);
    const { detectToken, suggestItems } = useSuggest(inputVal, taRef);

    taRef.value = mockTextarea('/al', 3);
    detectToken();

    expect(suggestItems.value.length).toBe(1);
    expect(suggestItems.value[0].label).toBe('/alpha');
  });

  it('MCP 服务：已启用的 MCP 出现在候选中', () => {
    const store = useAppStore();
    store.skills = [];
    store.mcps = [
      { id: 'm1', name: 'firecrawl', enabled: true, tools_count: 3, comment: '网页抓取' } as any,
      { id: 'm2', name: 'disabled-one', enabled: false, tools_count: 1 } as any,
    ];

    const inputVal = ref('/mcp');
    const taRef = ref<HTMLTextAreaElement | null>(null);
    const { detectToken, suggestItems } = useSuggest(inputVal, taRef);

    taRef.value = mockTextarea('/mcp', 4);
    detectToken();

    expect(suggestItems.value.length).toBe(1);
    expect(suggestItems.value[0].label).toBe('/mcp:firecrawl');
  });

  it('方向键导航：ArrowDown 移动高亮且 keyup 不重置（suppressDetect 生效）', () => {
    const store = useAppStore();
    store.skills = [
      { id: 's1', name: 'A', short_id: 'aa', description: '', enabled: true } as any,
      { id: 's2', name: 'B', short_id: 'bb', description: '', enabled: true } as any,
      { id: 's3', name: 'C', short_id: 'cc', description: '', enabled: true } as any,
    ];

    const inputVal = ref('/');
    const taRef = ref<HTMLTextAreaElement | null>(null);
    const { detectToken, onKeydown, suggestItems, suggestIndex } = useSuggest(inputVal, taRef);

    taRef.value = mockTextarea('/', 1);
    detectToken();
    expect(suggestItems.value.length).toBe(3);
    expect(suggestIndex.value).toBe(0);

    // ArrowDown → index 1
    const e1 = key('ArrowDown');
    onKeydown(e1);
    expect(suggestIndex.value).toBe(1);
    expect(e1.defaultPrevented).toBe(true);

    // 模拟 keyup 触发 detectToken —— suppressDetect 应跳过，不重置 index
    detectToken();
    expect(suggestIndex.value).toBe(1);

    // ArrowDown → index 2
    const e2 = key('ArrowDown');
    onKeydown(e2);
    expect(suggestIndex.value).toBe(2);

    // keyup 再次跳过
    detectToken();
    expect(suggestIndex.value).toBe(2);

    // ArrowDown 回到 0（循环）
    const e3 = key('ArrowDown');
    onKeydown(e3);
    expect(suggestIndex.value).toBe(0);
  });

  it('方向键导航：ArrowUp 反向循环', () => {
    const store = useAppStore();
    store.skills = [
      { id: 's1', name: 'A', short_id: 'aa', description: '', enabled: true } as any,
      { id: 's2', name: 'B', short_id: 'bb', description: '', enabled: true } as any,
    ];

    const inputVal = ref('/');
    const taRef = ref<HTMLTextAreaElement | null>(null);
    const { detectToken, onKeydown, suggestIndex } = useSuggest(inputVal, taRef);

    taRef.value = mockTextarea('/', 1);
    detectToken();
    expect(suggestIndex.value).toBe(0);

    // ArrowUp 从 0 → 最后一个（循环）
    const e = key('ArrowUp');
    onKeydown(e);
    expect(suggestIndex.value).toBe(1);
    expect(e.defaultPrevented).toBe(true);
  });

  it('Tab/Enter 选中候选：applySuggest 写回输入框并关闭面板', async () => {
    const store = useAppStore();
    store.skills = [
      { id: 's1', name: 'A', short_id: 'aa', description: 'desc', enabled: true } as any,
    ];

    const inputVal = ref('/');
    const taRef = ref<HTMLTextAreaElement | null>(null);
    const { detectToken, onKeydown, suggestItems, suggest } = useSuggest(inputVal, taRef);

    taRef.value = mockTextarea('/', 1);
    detectToken();
    expect(suggest.value).not.toBeNull();

    // Tab 选中
    const tabEvent = key('Tab');
    onKeydown(tabEvent);
    expect(tabEvent.defaultPrevented).toBe(true);
    // applySuggest 写入：/aa + 空格
    expect(inputVal.value).toBe('/aa ');
    expect(suggest.value).toBeNull();
    expect(suggestItems.value.length).toBe(0);

    // 重新触发，用 Enter 选中
    inputVal.value = '/';
    taRef.value = mockTextarea('/', 1);
    detectToken();
    expect(suggest.value).not.toBeNull();

    const enterEvent = key('Enter');
    onKeydown(enterEvent);
    expect(enterEvent.defaultPrevented).toBe(true);
    expect(inputVal.value).toBe('/aa ');
  });

  it('Escape 关闭补全面板', () => {
    const store = useAppStore();
    store.skills = [
      { id: 's1', name: 'A', short_id: 'aa', description: '', enabled: true } as any,
    ];

    const inputVal = ref('/');
    const taRef = ref<HTMLTextAreaElement | null>(null);
    const { detectToken, onKeydown, suggest, suggestItems } = useSuggest(inputVal, taRef);

    taRef.value = mockTextarea('/', 1);
    detectToken();
    expect(suggest.value).not.toBeNull();

    const e = key('Escape');
    onKeydown(e);
    expect(e.defaultPrevented).toBe(true);
    expect(suggest.value).toBeNull();
    expect(suggestItems.value.length).toBe(0);
  });

  it('补全关闭时 Enter 不被 onKeydown 拦截（defaultPrevented=false）', () => {
    const inputVal = ref('普通文本');
    const taRef = ref<HTMLTextAreaElement | null>(null);
    const { onKeydown } = useSuggest(inputVal, taRef);

    const e = key('Enter');
    onKeydown(e);
    expect(e.defaultPrevented).toBe(false);
  });

  it('非补全语境的 keyup 不触发 suppressDetect 残留', () => {
    const inputVal = ref('hello world');
    const taRef = ref<HTMLTextAreaElement | null>(null);
    const { detectToken, onKeydown, suggest } = useSuggest(inputVal, taRef);

    taRef.value = mockTextarea('hello world', 11);
    detectToken();
    expect(suggest.value).toBeNull();

    // 随便按方向键（补全未打开，onKeydown 不处理）
    onKeydown(key('ArrowDown'));
    // detectToken 不应因为 suppressDetect 而跳过（标志未设置）
    detectToken();
    expect(suggest.value).toBeNull();
  });

  it('@ 文件引用：触发 search_workspace_files 调用并映射为候选', async () => {
    vi.useFakeTimers();
    const searchFn = vi.fn().mockResolvedValue({ files: ['src/main.ts', 'README.md'] });
    vi.stubGlobal('pywebview', { api: { search_workspace_files: searchFn } });

    const inputVal = ref('@main');
    const taRef = ref<HTMLTextAreaElement | null>(null);
    const { detectToken, suggest, suggestItems, suggestIndex } = useSuggest(inputVal, taRef);

    taRef.value = mockTextarea('@main', 5);
    detectToken();
    expect(suggest.value?.mode).toBe('file');
    // fetchFileItems 有 150ms 防抖；回调是 async 的，结果映射发生在微任务里，
    // 必须用 advanceTimersByTimeAsync 把微任务一并冲刷
    await vi.advanceTimersByTimeAsync(200);
    expect(searchFn).toHaveBeenCalledWith('main', 8);
    // 返回的 files 必须映射为带 @ 前缀的候选，且高亮重置到第一项
    expect(suggestItems.value.map(it => it.apply)).toEqual(['@src/main.ts', '@README.md']);
    expect(suggestItems.value.map(it => it.label)).toEqual(['src/main.ts', 'README.md']);
    expect(suggestIndex.value).toBe(0);

    vi.unstubAllGlobals();
    vi.useRealTimers();
  });
});
