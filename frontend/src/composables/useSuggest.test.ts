import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ref } from 'vue';
import { createPinia, setActivePinia } from 'pinia';
import { useAppStore } from '../stores/appStore';
import { useSuggest } from './useSuggest';

// 模拟 textarea 元素：DOM value + selectionStart + setSelectionRange。
// value 必须单独给：detectToken 以 DOM 值为权威（输入法组词期间 v-model
// 尚未同步，只能靠 DOM 拿最新文本），测试用它复现「inputVal 落后于 DOM」。
function mockTextarea(value: string, cursor: number = value.length) {
  const el = {
    value,
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
    expect(searchSessions).toHaveBeenCalledWith('登录', 12, '');
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

  it('@ 会话候选携带当前会话名：草稿态传空串，后端据此不排除任何会话', async () => {
    vi.useFakeTimers();
    const store = useAppStore();
    const searchSessions = vi.fn().mockResolvedValue({ ok: true, sessions: [] });
    const searchFiles = vi.fn().mockResolvedValue({ files: [] });
    vi.stubGlobal('pywebview', {
      api: { search_sessions: searchSessions, search_workspace_files: searchFiles },
    });

    const inputVal = ref('@');
    const taRef = ref<HTMLTextAreaElement | null>(null);
    const { detectToken } = useSuggest(inputVal, taRef);
    taRef.value = mockTextarea('@', 1);

    // 新会话草稿：currentSessionName 为空，必须显式传空串——否则后端会用自己
    // 仍停留在上一个会话的 _current_session 把目标会话排除掉
    store.currentSessionName = '';
    detectToken();
    expect(searchSessions).toHaveBeenLastCalledWith('', 12, '');

    // 已在某个已落库会话里：把会话名交给后端，由它排除自身。
    // 两次 detectToken 之间要跨过 250ms 的同 token 去重窗口，否则第二次
    // 会被当成同一次按键的 input/keyup 重复事件而直接复用上一轮结果。
    await vi.advanceTimersByTimeAsync(300);
    store.currentSessionName = '缓存改造';
    detectToken();
    expect(searchSessions).toHaveBeenLastCalledWith('', 12, '缓存改造');

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
    expect(searchSessions).toHaveBeenCalledWith('login', 12, '');
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
    expect(searchSessions).toHaveBeenCalledWith('登录', 12, '');
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

  it('删掉 @ 后重新输入 @ 能重新拉起候选（以 DOM 值为准）', async () => {
    vi.useFakeTimers();
    const searchSessions = vi.fn().mockResolvedValue({
      ok: true,
      sessions: [{ name: '登录bug排查', count: 3, updated_at: '', workdir: '' }],
    });
    const searchFiles = vi.fn().mockResolvedValue({ files: [] });
    vi.stubGlobal('pywebview', {
      api: { search_sessions: searchSessions, search_workspace_files: searchFiles },
    });

    const inputVal = ref('@');
    const taRef = ref<HTMLTextAreaElement | null>(null);
    const { detectToken, suggest, suggestItems, suggestEmpty } = useSuggest(inputVal, taRef);

    // 输入 @：出候选
    taRef.value = mockTextarea('@', 1);
    detectToken();
    await vi.advanceTimersByTimeAsync(200);
    expect(suggestItems.value.length).toBe(1);

    // 删除 @：面板关闭、空态清空
    inputVal.value = '';
    taRef.value = mockTextarea('', 0);
    detectToken();
    expect(suggest.value).toBeNull();
    expect(suggestEmpty.value).toBe('');

    // 重新输入 @：必须再次检出 token 并拉起候选。
    // 这里刻意让 inputVal 落后于 DOM（模拟输入法组词期间 v-model 尚未同步），
    // 旧实现读 inputVal 会漏掉这个 @，面板就不会出现。
    inputVal.value = '';
    taRef.value = mockTextarea('@', 1);
    await vi.advanceTimersByTimeAsync(300);
    detectToken();
    expect(suggest.value?.mode).toBe('file');
    await vi.advanceTimersByTimeAsync(200);
    expect(suggestItems.value.map(it => it.label)).toEqual(['登录bug排查']);

    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it('同一次按键的 input/keyup 重复触发只检索一次', async () => {
    vi.useFakeTimers();
    const searchSessions = vi.fn().mockResolvedValue({ ok: true, sessions: [] });
    const searchFiles = vi.fn().mockResolvedValue({ files: [] });
    vi.stubGlobal('pywebview', {
      api: { search_sessions: searchSessions, search_workspace_files: searchFiles },
    });

    const inputVal = ref('@登录');
    const taRef = ref<HTMLTextAreaElement | null>(null);
    const { detectToken } = useSuggest(inputVal, taRef);

    taRef.value = mockTextarea('@登录', 3);
    detectToken(); // input 事件
    detectToken(); // 紧跟着的 keyup 事件
    await vi.advanceTimersByTimeAsync(200);

    // 同一个 token 的重复事件必须复用同一轮检索，否则每次按键打两遍 SQL
    expect(searchSessions).toHaveBeenCalledTimes(1);
    expect(searchFiles).toHaveBeenCalledTimes(1);

    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it('两侧都无命中时展示空态提示，而不是面板整体消失', async () => {
    vi.useFakeTimers();
    const searchSessions = vi.fn().mockResolvedValue({ ok: true, sessions: [] });
    const searchFiles = vi.fn().mockResolvedValue({ files: [] });
    vi.stubGlobal('pywebview', {
      api: { search_sessions: searchSessions, search_workspace_files: searchFiles },
    });

    const inputVal = ref('@不存在');
    const taRef = ref<HTMLTextAreaElement | null>(null);
    const { detectToken, suggestEmpty, suggestItems } = useSuggest(inputVal, taRef);

    taRef.value = mockTextarea('@不存在', 4);
    detectToken();
    await vi.advanceTimersByTimeAsync(200);

    expect(suggestItems.value.length).toBe(0);
    expect(suggestEmpty.value).toBe('没有匹配的对话或文件');

    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it('@ 无命中后继续输入达到软阈值：面板自动收起', async () => {
    vi.useFakeTimers();
    const searchSessions = vi.fn().mockResolvedValue({ ok: true, sessions: [] });
    const searchFiles = vi.fn().mockResolvedValue({ files: [] });
    vi.stubGlobal('pywebview', {
      api: { search_sessions: searchSessions, search_workspace_files: searchFiles },
    });

    const inputVal = ref('');
    const taRef = ref<HTMLTextAreaElement | null>(null);
    const { detectToken, suggest, suggestEmpty } = useSuggest(inputVal, taRef);

    // 短 query 两侧都无命中：先展示空态（这是原有行为，不能被破坏）
    const short = '@登录bug';
    inputVal.value = short;
    taRef.value = mockTextarea(short, short.length);
    detectToken();
    await vi.advanceTimersByTimeAsync(200);
    expect(suggest.value?.mode).toBe('file');
    expect(suggestEmpty.value).toBe('没有匹配的对话或文件');

    // 继续输入到超过软阈值（14）：用户显然在写正文，面板自己收起
    const long = '@' + '文'.repeat(14);
    inputVal.value = long;
    taRef.value = mockTextarea(long, long.length);
    detectToken();
    expect(suggest.value).toBeNull();
    expect(suggestEmpty.value).toBe('');

    // 继续输入不会把面板重新挂回来，也不该再发起检索
    const callsBefore = searchSessions.mock.calls.length;
    const longer = long + '字';
    inputVal.value = longer;
    taRef.value = mockTextarea(longer, longer.length);
    detectToken();
    await vi.advanceTimersByTimeAsync(200);
    expect(suggest.value).toBeNull();
    expect(suggestEmpty.value).toBe('');
    expect(searchSessions.mock.calls.length).toBe(callsBefore);

    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it('@ 有命中时逐字输入不受软阈值影响，超硬上限才收起', async () => {
    vi.useFakeTimers();
    const searchSessions = vi.fn().mockResolvedValue({
      ok: true,
      sessions: [{ name: '登录bug排查', count: 3, updated_at: '', workdir: '' }],
    });
    const searchFiles = vi.fn().mockResolvedValue({ files: [] });
    vi.stubGlobal('pywebview', {
      api: { search_sessions: searchSessions, search_workspace_files: searchFiles },
    });

    const inputVal = ref('');
    const taRef = ref<HTMLTextAreaElement | null>(null);
    const { detectToken, suggest } = useSuggest(inputVal, taRef);

    // 20 个字符但有命中：软阈值只管空态，候选照常展示
    const mid = '@' + 'x'.repeat(19);
    inputVal.value = mid;
    taRef.value = mockTextarea(mid, mid.length);
    detectToken();
    await vi.advanceTimersByTimeAsync(200);
    expect(suggest.value?.mode).toBe('file');

    // 达到硬上限（query 40 个字符）：无论有无命中一律收起
    const hard = '@' + 'x'.repeat(40);
    inputVal.value = hard;
    taRef.value = mockTextarea(hard, hard.length);
    detectToken();
    expect(suggest.value).toBeNull();

    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it('会话检索失败且文件无结果时提示检索失败（与真的没命中区分）', async () => {
    vi.useFakeTimers();
    const searchSessions = vi.fn().mockRejectedValue(new Error('bridge error'));
    const searchFiles = vi.fn().mockResolvedValue({ files: [] });
    vi.stubGlobal('pywebview', {
      api: { search_sessions: searchSessions, search_workspace_files: searchFiles },
    });

    const inputVal = ref('@登录');
    const taRef = ref<HTMLTextAreaElement | null>(null);
    const { detectToken, suggestEmpty } = useSuggest(inputVal, taRef);

    taRef.value = mockTextarea('@登录', 3);
    detectToken();
    await vi.advanceTimersByTimeAsync(200);

    expect(suggestEmpty.value).toBe('对话检索失败，改一下关键词可重试');

    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it('文件候选晚到不重置用户已移动的高亮', async () => {
    vi.useFakeTimers();
    const searchSessions = vi.fn().mockResolvedValue({
      ok: true,
      sessions: [
        { name: '会话A', count: 1, updated_at: '', workdir: '' },
        { name: '会话B', count: 2, updated_at: '', workdir: '' },
      ],
    });
    let resolveFiles!: (v: any) => void;
    const searchFiles = vi.fn(() => new Promise(resolve => { resolveFiles = resolve; }));
    vi.stubGlobal('pywebview', {
      api: { search_sessions: searchSessions, search_workspace_files: searchFiles },
    });

    const inputVal = ref('@');
    const taRef = ref<HTMLTextAreaElement | null>(null);
    const { detectToken, onKeydown, suggestIndex, suggestItems } = useSuggest(inputVal, taRef);

    taRef.value = mockTextarea('@', 1);
    detectToken();
    await vi.advanceTimersByTimeAsync(1);
    expect(suggestItems.value.length).toBe(2);

    // 用户按 ↓ 选中第二条
    onKeydown(key('ArrowDown'));
    expect(suggestIndex.value).toBe(1);

    // 文件候选晚到触发再次 render：高亮必须留在原处，不能被重置回第一项
    await vi.advanceTimersByTimeAsync(130);
    resolveFiles({ files: ['a.ts'] });
    await vi.advanceTimersByTimeAsync(1);
    expect(suggestItems.value.length).toBe(3);
    expect(suggestIndex.value).toBe(1);

    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  // ---------------- 内置斜杠命令（后端清单纯读投影） ----------------

  it('斜杠命令：内置命令参与补全，前缀命中只留 /goal', () => {
    const store = useAppStore();
    store.skills = [];
    store.slashCommands = [
      { name: '/goal', usage: '/goal <目标>', summary: '目标模式', kind: 'local' } as any,
      { name: '/fix', usage: '/fix <报错>', summary: '修复报错', kind: 'prompt' } as any,
    ];

    const inputVal = ref('/go');
    const taRef = ref<HTMLTextAreaElement | null>(null);
    const { detectToken, suggest, suggestItems } = useSuggest(inputVal, taRef);

    taRef.value = mockTextarea('/go', 3);
    detectToken();

    expect(suggest.value?.mode).toBe('command');
    // 只有 /goal 命中前缀 /go
    expect(suggestItems.value.map(it => it.label)).toEqual(['/goal']);
    // 右侧说明取 summary，key 带 cmd: 前缀避免与技能/MCP 撞 key
    expect(suggestItems.value[0].desc).toBe('目标模式');
    expect(suggestItems.value[0].key).toBe('cmd:/goal');
  });

  it('斜杠命令：命令候选排在技能之前，同名 label 只出现一次', () => {
    const store = useAppStore();
    store.slashCommands = [
      { name: '/goal', usage: '', summary: '目标模式', kind: 'local' } as any,
    ];
    store.skills = [
      { id: 's1', name: '同名技能', short_id: 'goal', description: '与命令重名', enabled: true } as any,
      { id: 's2', name: '图技能', short_id: 'graph', description: '', enabled: true } as any,
    ];
    store.mcps = [];

    const inputVal = ref('/');
    const taRef = ref<HTMLTextAreaElement | null>(null);
    const { detectToken, suggestItems } = useSuggest(inputVal, taRef);

    taRef.value = mockTextarea('/', 1);
    detectToken();

    // 命令在前；技能里的 /goal 因 label 重复被丢弃，/graph 仍然保留（" /" 单独
    // 输入时技能候选照旧可见，既有行为不变）
    expect(suggestItems.value.map(it => it.label)).toEqual(['/goal', '/graph']);
    expect(suggestItems.value[0].key).toBe('cmd:/goal');
    expect(suggestItems.value[1].key).toBe('skill:graph');
  });

  it('斜杠命令：选中命令后插入 name 加一个空格', () => {
    const store = useAppStore();
    store.skills = [];
    store.slashCommands = [
      { name: '/goal', usage: '', summary: '目标模式', kind: 'local' } as any,
    ];

    const inputVal = ref('/go');
    const taRef = ref<HTMLTextAreaElement | null>(null);
    const { detectToken, onKeydown, suggestItems } = useSuggest(inputVal, taRef);

    taRef.value = mockTextarea('/go', 3);
    detectToken();
    expect(suggestItems.value.length).toBe(1);

    onKeydown(key('Tab'));
    // apply 本身带尾随空格，写回时不能变成两个空格
    expect(inputVal.value).toBe('/goal ');
  });

  it('斜杠命令：命令名后已输入空格时不再展示命令候选', () => {
    const store = useAppStore();
    store.skills = [];
    store.mcps = [];
    store.slashCommands = [
      { name: '/fix', usage: '', summary: '修复报错', kind: 'prompt' } as any,
    ];

    const inputVal = ref('/fix ');
    const taRef = ref<HTMLTextAreaElement | null>(null);
    const { detectToken, suggest, suggestItems } = useSuggest(inputVal, taRef);

    // 空格之后是参数位置，已不属于命令名，不能继续弹候选
    taRef.value = mockTextarea('/fix ', 5);
    detectToken();

    expect(suggest.value).toBeNull();
    expect(suggestItems.value.filter(it => it.key.startsWith('cmd:'))).toEqual([]);
    expect(suggestItems.value.length).toBe(0);
  });

  it('斜杠命令：清单里无前缀命中时不出现命令候选', () => {
    const store = useAppStore();
    store.skills = [];
    store.mcps = [];
    store.slashCommands = [
      { name: '/goal', usage: '', summary: '目标模式', kind: 'local' } as any,
      { name: '/fix', usage: '', summary: '修复报错', kind: 'prompt' } as any,
    ];

    const inputVal = ref('/usr');
    const taRef = ref<HTMLTextAreaElement | null>(null);
    const { detectToken, suggestItems } = useSuggest(inputVal, taRef);

    taRef.value = mockTextarea('/usr', 4);
    detectToken();

    // /usr 未命中任何内置命令，也没有技能/MCP 匹配：面板保持空列表
    expect(suggestItems.value.filter(it => it.key.startsWith('cmd:'))).toEqual([]);
    expect(suggestItems.value.length).toBe(0);
  });

  it('loadSlashCommands：桥返回 ok 时写入 slashCommands', async () => {
    const listFn = vi.fn().mockResolvedValue({
      ok: true,
      commands: [{ name: '/goal', usage: '/goal <目标>', summary: '目标模式', kind: 'local' }],
    });
    vi.stubGlobal('pywebview', { api: { list_slash_commands: listFn } });

    const store = useAppStore();
    expect(store.slashCommands).toEqual([]);

    await store.loadSlashCommands();
    expect(listFn).toHaveBeenCalledTimes(1);
    expect(store.slashCommands.map(c => c.name)).toEqual(['/goal']);

    vi.unstubAllGlobals();
  });

  it('loadSlashCommands：桥报错或缺接口时保持既有清单且不抛错', async () => {
    const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    const listFn = vi.fn().mockRejectedValue(new Error('bridge error'));
    vi.stubGlobal('pywebview', { api: { list_slash_commands: listFn } });

    const store = useAppStore();
    store.slashCommands = [
      { name: '/keep', usage: '', summary: '', kind: 'local' } as any,
    ];

    // reject 被吞掉：补全面板退化为原有候选，不影响输入
    await expect(store.loadSlashCommands()).resolves.toBeUndefined();
    expect(store.slashCommands.map(c => c.name)).toEqual(['/keep']);
    vi.unstubAllGlobals();

    // 旧版桥没有该方法：直接返回，同样不抛错
    vi.stubGlobal('pywebview', { api: {} });
    await expect(store.loadSlashCommands()).resolves.toBeUndefined();
    expect(store.slashCommands.map(c => c.name)).toEqual(['/keep']);

    vi.unstubAllGlobals();
    errSpy.mockRestore();
  });
});
