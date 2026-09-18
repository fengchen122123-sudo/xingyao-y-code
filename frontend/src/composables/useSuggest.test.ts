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

  it('@ 输入时合并会话与文件候选，会话在前', async () => {
    vi.useFakeTimers();
    const searchSessions = vi.fn().mockResolvedValue({
      ok: true,
      sessions: [{ name: '登录bug排查', count: 3, updated_at: '', workdir: '' }],
    });
    const searchFiles = vi.fn().mockResolvedValue({ files: ['core/login.py'] });
    vi.stubGlobal('pywebview', {
      api: { search_sessions: searchSessions, search_workspace_files: searchFiles },
    });

    const inputVal = ref('@登录');
    const taRef = ref<HTMLTextAreaElement | null>(null);
    const { detectToken, suggest, suggestItems } = useSuggest(inputVal, taRef);

    taRef.value = mockTextarea('@登录', 3);
    detectToken();
    expect(suggest.value?.mode).toBe('file');
    await vi.advanceTimersByTimeAsync(200);
    expect(searchSessions).toHaveBeenCalledWith('登录', 12);
    expect(searchFiles).toHaveBeenCalledWith('登录', 8);
    // 会话候选在前、文件候选在后
    expect(suggestItems.value.length).toBe(2);
    expect(suggestItems.value[0].group).toBe('session');
    expect(suggestItems.value[0].label).toBe('登录bug排查');
    expect(suggestItems.value[0].icon).toBe('💬');
    expect(suggestItems.value[0].desc).toBe('3 条消息');
    expect(suggestItems.value[1].group).toBe('file');
    expect(suggestItems.value[1].label).toBe('core/login.py');

    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it('会话候选命中很多时最多占 4 个名额，文件候选不被挤光', async () => {
    vi.useFakeTimers();
    // 后端已放宽到 12 条：命中会话很多时不能让会话项独占整个面板
    const sessions = Array.from({ length: 8 }, (_, i) => ({
      name: `会话${i}`,
      count: i,
      updated_at: '',
      workdir: '',
    }));
    const searchSessions = vi.fn().mockResolvedValue({ ok: true, sessions });
    const searchFiles = vi.fn().mockResolvedValue({ files: ['core/login.py', 'README.md'] });
    vi.stubGlobal('pywebview', {
      api: { search_sessions: searchSessions, search_workspace_files: searchFiles },
    });

    const inputVal = ref('@');
    const taRef = ref<HTMLTextAreaElement | null>(null);
    const { detectToken, suggestItems } = useSuggest(inputVal, taRef);

    taRef.value = mockTextarea('@', 1);
    detectToken();
    await vi.advanceTimersByTimeAsync(200);

    const groups = suggestItems.value.map(it => it.group);
    // 会话最多 4 条，剩余名额留给文件候选
    expect(groups.filter(g => g === 'session').length).toBe(4);
    expect(groups.filter(g => g === 'file').length).toBe(2);
    expect(suggestItems.value[0].label).toBe('会话0');
    expect(suggestItems.value[4].group).toBe('file');

    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it('选中会话项插入 @{会话名} 并剥离花括号', async () => {
    vi.useFakeTimers();
    const searchSessions = vi.fn().mockResolvedValue({
      ok: true,
      sessions: [{ name: '我的{怪}对话', count: 5, updated_at: '', workdir: '' }],
    });
    const searchFiles = vi.fn().mockResolvedValue({ files: [] });
    vi.stubGlobal('pywebview', {
      api: { search_sessions: searchSessions, search_workspace_files: searchFiles },
    });

    const inputVal = ref('@我的');
    const taRef = ref<HTMLTextAreaElement | null>(null);
    const { detectToken, suggestItems } = useSuggest(inputVal, taRef);

    taRef.value = mockTextarea('@我的', 3);
    detectToken();
    await vi.advanceTimersByTimeAsync(200);
    expect(suggestItems.value.length).toBe(1);
    // 会话名内花括号被剥离，产出 @{...} 标记
    expect(suggestItems.value[0].apply).toBe('@{我的怪对话}');
    expect(suggestItems.value[0].key).toBe('session:我的{怪}对话');

    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it('search_sessions 接口缺失时退化为纯文件补全', async () => {
    vi.useFakeTimers();
    const searchFiles = vi.fn().mockResolvedValue({ files: ['core/login.py'] });
    // api 只实现 search_workspace_files，不提供 search_sessions
    vi.stubGlobal('pywebview', { api: { search_workspace_files: searchFiles } });

    const inputVal = ref('@login');
    const taRef = ref<HTMLTextAreaElement | null>(null);
    const { detectToken, suggestItems } = useSuggest(inputVal, taRef);

    taRef.value = mockTextarea('@login', 6);
    detectToken();
    await vi.advanceTimersByTimeAsync(200);
    // 不抛错，仍返回文件项
    expect(suggestItems.value.map(it => it.apply)).toEqual(['@core/login.py']);
    expect(suggestItems.value[0].group).toBe('file');

    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it('search_sessions reject 时仍返回文件项（会话异常静默退化为纯文件）', async () => {
    vi.useFakeTimers();
    // 后端桥抛异常时 pywebview 会 reject，而非返回 {ok:false}
    const searchSessions = vi.fn().mockRejectedValue(new Error('bridge error'));
    const searchFiles = vi.fn().mockResolvedValue({ files: ['core/login.py', 'README.md'] });
    vi.stubGlobal('pywebview', {
      api: { search_sessions: searchSessions, search_workspace_files: searchFiles },
    });

    const inputVal = ref('@login');
    const taRef = ref<HTMLTextAreaElement | null>(null);
    const { detectToken, suggestItems } = useSuggest(inputVal, taRef);

    taRef.value = mockTextarea('@login', 6);
    detectToken();
    await vi.advanceTimersByTimeAsync(200);
    expect(searchSessions).toHaveBeenCalledWith('login', 12);
    // 会话接口异常不应拖垮文件补全：文件候选仍正常出现
    expect(suggestItems.value.map(it => it.apply)).toEqual(['@core/login.py', '@README.md']);
    expect(suggestItems.value.every(it => it.group === 'file')).toBe(true);

    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it('文件扫描慢时，会话候选先渲染即可见（两侧互不阻塞）', async () => {
    vi.useFakeTimers();
    const searchSessions = vi.fn().mockResolvedValue({
      ok: true,
      sessions: [{ name: '登录bug排查', count: 3, updated_at: '', workdir: '' }],
    });
    // 文件扫描：手工控制 resolve 时机，模拟「很慢的文件接口」一直 pending
    let resolveFiles!: (v: any) => void;
    const searchFiles = vi.fn(
      () => new Promise(resolve => { resolveFiles = resolve; }),
    );
    vi.stubGlobal('pywebview', {
      api: { search_sessions: searchSessions, search_workspace_files: searchFiles },
    });

    const inputVal = ref('@登录');
    const taRef = ref<HTMLTextAreaElement | null>(null);
    const { detectToken, suggestItems } = useSuggest(inputVal, taRef);

    taRef.value = mockTextarea('@登录', 3);
    detectToken();
    // 会话请求不防抖、立即发起：冲刷微任务后「对话」候选已经可见，
    // 而不是等慢文件扫描（旧实现会卡在 Promise.all 上）
    await vi.advanceTimersByTimeAsync(1);
    expect(searchSessions).toHaveBeenCalledWith('登录', 12);
    expect(suggestItems.value.length).toBe(1);
    expect(suggestItems.value[0].group).toBe('session');
    expect(suggestItems.value[0].label).toBe('登录bug排查');

    // 文件请求 120ms 后才发起，此刻仍 pending；不得清空已渲染的会话候选
    await vi.advanceTimersByTimeAsync(130);
    expect(searchFiles).toHaveBeenCalledWith('登录', 8);
    expect(suggestItems.value.map(it => it.group)).toEqual(['session']);

    // 文件候选到达后合并为最终列表：会话在前、文件在后
    resolveFiles({ files: ['core/login.py'] });
    await vi.advanceTimersByTimeAsync(1);
    expect(suggestItems.value.map(it => it.group)).toEqual(['session', 'file']);
    expect(suggestItems.value.map(it => it.apply)).toEqual(['@{登录bug排查}', '@core/login.py']);
    expect(suggestItems.value.map(it => it.key)).toEqual([
      'session:登录bug排查',
      'file:core/login.py',
    ]);

    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it('search_workspace_files reject 时保留已渲染的会话候选（不清空）', async () => {
    vi.useFakeTimers();
    const searchSessions = vi.fn().mockResolvedValue({
      ok: true,
      sessions: [{ name: '登录bug排查', count: 3, updated_at: '', workdir: '' }],
    });
    const searchFiles = vi.fn().mockRejectedValue(new Error('scan failed'));
    vi.stubGlobal('pywebview', {
      api: { search_sessions: searchSessions, search_workspace_files: searchFiles },
    });

    const inputVal = ref('@登录');
    const taRef = ref<HTMLTextAreaElement | null>(null);
    const { detectToken, suggestItems } = useSuggest(inputVal, taRef);

    taRef.value = mockTextarea('@登录', 3);
    detectToken();
    // 会话先到并渲染
    await vi.advanceTimersByTimeAsync(1);
    expect(suggestItems.value.length).toBe(1);

    // 文件扫描 reject：走 .catch(() => null)，绝不能把会话候选一并清空
    await vi.advanceTimersByTimeAsync(200);
    expect(suggestItems.value.length).toBe(1);
    expect(suggestItems.value[0].group).toBe('session');
    expect(suggestItems.value[0].label).toBe('登录bug排查');

    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it('旧 query 的迟到响应不会覆盖新 query 的候选', async () => {
    vi.useFakeTimers();
    let resolveOld!: (v: any) => void;
    const searchSessions = vi
      .fn()
      .mockImplementationOnce(() => new Promise(resolve => { resolveOld = resolve; }))
      .mockResolvedValue({
        ok: true,
        sessions: [{ name: '新会话', count: 1, updated_at: '', workdir: '' }],
      });
    const searchFiles = vi.fn().mockResolvedValue({ files: ['new.ts'] });
    vi.stubGlobal('pywebview', {
      api: { search_sessions: searchSessions, search_workspace_files: searchFiles },
    });

    const inputVal = ref('@旧');
    const taRef = ref<HTMLTextAreaElement | null>(null);
    const { detectToken, suggestItems } = useSuggest(inputVal, taRef);

    // 第一次输入：会话请求挂起不返回
    taRef.value = mockTextarea('@旧', 2);
    detectToken();
    await vi.advanceTimersByTimeAsync(1);

    // 用户继续打字，query 变成 @新
    inputVal.value = '@新';
    taRef.value = mockTextarea('@新', 2);
    detectToken();
    await vi.advanceTimersByTimeAsync(200);
    expect(suggestItems.value.map(it => it.label)).toEqual(['新会话', 'new.ts']);

    // 旧 query 的响应迟到：必须被丢弃，不得覆盖新 query 的列表
    resolveOld({ ok: true, sessions: [{ name: '旧会话', count: 9, updated_at: '', workdir: '' }] });
    await vi.advanceTimersByTimeAsync(1);
    expect(suggestItems.value.map(it => it.label)).toEqual(['新会话', 'new.ts']);

    vi.unstubAllGlobals();
    vi.useRealTimers();
  });
});
