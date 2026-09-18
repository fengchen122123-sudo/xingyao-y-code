import { ref, nextTick, type Ref } from 'vue';
import { useAppStore } from '../stores/appStore';
import type { SuggestItem } from '../components/InputSuggest.vue';

/**
 * 输入自动补全 composable：/ 斜杠（技能 + MCP）与 @ 文件引用。
 * InputArea 与 KimiEmptyState 共用，确保空会话页也有补全能力。
 */
export function useSuggest(
  inputVal: Ref<string>,
  textareaRef: Ref<HTMLTextAreaElement | null>,
) {
  const store = useAppStore();
  const suggest = ref<{ mode: 'command' | 'file'; start: number; query: string } | null>(null);
  const suggestItems = ref<SuggestItem[]>([]);
  const suggestIndex = ref(0);
  let fileSuggestTimer: number | undefined;
  // 方向键导航后 keyup 会触发 detectToken，用此标志跳过一次避免高亮被重置回第一项
  let suppressDetect = false;

  function buildCommandItems(query: string) {
    const q = query.toLowerCase();
    const items: SuggestItem[] = [];
    for (const s of store.skills) {
      if (!s.enabled || !s.short_id) continue;
      items.push({
        key: 'skill:' + s.short_id,
        icon: '⚡',
        label: '/' + s.short_id,
        desc: s.description || s.name,
        apply: '/' + s.short_id,
      });
    }
    for (const m of store.mcps) {
      if (!m.enabled) continue;
      items.push({
        key: 'mcp:' + m.name,
        icon: '🔌',
        label: '/mcp:' + m.name,
        desc: `MCP 服务 · ${m.tools_count} 个工具`,
        apply: '/mcp:' + m.name,
      });
    }
    suggestItems.value = items
      .filter(it => it.label.slice(1).toLowerCase().startsWith(q))
      .slice(0, 8);
    suggestIndex.value = 0;
  }

  // @ 提及的请求序号：每次 fetchAtItems 调用都递增。异步响应回来时若序号已过期，
  // 说明用户已经改了 query（发起过新一轮请求），旧响应一律丢弃，
  // 否则旧 query 的候选会覆盖新 query 已经渲染出来的列表。
  let atSeq = 0;

  /**
   * 拉取 @ 提及候选：会话候选与文件候选「各自独立渲染、互不阻塞」。
   * 会话查询走本地 SQL、毫秒级返回，所以不防抖，敲下 @ 立刻发起；
   * 文件扫描要遍历整个工作区、可能很慢，所以保留 120ms 防抖 + 独立延迟渲染，
   * 绝不能让慢文件扫描把快的会话候选一起拖住（旧的 Promise.all 就是这个问题）。
   */
  function fetchAtItems(query: string) {
    window.clearTimeout(fileSuggestTimer);
    const seq = ++atSeq;
    const api = window.pywebview && window.pywebview.api;
    if (!api) {
      suggestItems.value = [];
      return;
    }

    // 两侧候选各自的落点：谁先返回谁先渲染，后到的再合并进来，互不覆盖。
    let sessionItems: SuggestItem[] = [];
    let fileItems: SuggestItem[] = [];

    // 异步响应写回前必须校验：① 仍是当前最新一轮请求（seq 未过期）；
    // ② 面板仍处于 @ 补全模式；③ query 未变。
    // 用户在等待期间可能继续打字（query 变了）或已关闭面板（suggest 为 null），
    // 若不校验，旧查询的响应会覆盖新查询的列表、甚至撑开已关闭的面板。
    function canWrite() {
      return (
        seq === atSeq &&
        !!suggest.value &&
        suggest.value.mode === 'file' &&
        suggest.value.query === query
      );
    }

    // 合并渲染：会话在前、文件在后，任一侧未返回时只展示已到达的一侧；
    // 每侧响应只会触发一次 render（Promise 只 settle 一次），不会重复追加。
    function render() {
      if (!canWrite()) return;
      // 会话候选最多占 4 个名额：后端已按「前缀命中优先 + 名字短的优先」排序，
      // 前 4 条即最相关；若不设上限，命中会话一多就会把文件候选整体挤出面板，
      // 反而回归了 @ 引用文件的能力（修复前两侧就是各 4 条的平衡）。
      suggestItems.value = [...sessionItems.slice(0, 4), ...fileItems].slice(0, 8);
      suggestIndex.value = 0;
    }

    // —— 会话候选：不防抖，立即发起 ——
    // 接口缺失（旧版桥）时直接跳过，退化为纯文件补全。
    if (typeof api.search_sessions === 'function') {
      api
        .search_sessions(query, 12)
        .then(res => {
          if (!canWrite()) return;
          sessionItems = ((res && res.sessions) || []).map(s => ({
            key: 'session:' + s.name,
            icon: '💬',
            label: s.name,
            desc: `${s.count} 条消息`,
            // 会话名内禁止出现花括号，插入时剥离
            apply: '@{' + s.name.replace(/[{}]/g, '') + '}',
            group: 'session',
          }));
          // 会话候选一到就渲染，不等文件扫描
          render();
        })
        // 会话接口异常（桥报错等）静默忽略：绝不能清空文件候选
        .catch(() => {});
    }

    // —— 文件候选：保留 120ms 防抖，避免每次按键都全量扫描工作区 ——
    fileSuggestTimer = window.setTimeout(() => {
      if (!canWrite()) return;
      api
        .search_workspace_files(query, 8)
        // 文件扫描 reject 时退化为 null（后续按空列表合并），
        // 绝不能把已经渲染出来的会话候选一并清空
        .catch(() => null)
        .then(res => {
          if (!canWrite()) return;
          const files: string[] = (res && res.files) || [];
          fileItems = files.map(f => ({
            // key 加 'file:' 前缀，避免与会话项撞 key
            key: 'file:' + f,
            icon: '📄',
            label: f,
            desc: '',
            apply: '@' + f,
            group: 'file',
          }));
          // 文件候选到达后与会话候选合并（会话在前），两侧都到即最终列表
          render();
        });
    }, 120);
  }

  function detectToken() {
    if (suppressDetect) {
      suppressDetect = false;
      return;
    }
    const ta = textareaRef.value;
    if (!ta) {
      suggest.value = null;
      return;
    }
    const pos = ta.selectionStart ?? inputVal.value.length;
    const before = inputVal.value.slice(0, pos);
    const sm = before.match(/(?:^|\s)\/([^\s]*)$/);
    if (sm) {
      const query = sm[1];
      suggest.value = { mode: 'command', start: pos - query.length - 1, query };
      buildCommandItems(query);
      return;
    }
    const am = before.match(/@([^\s@]*)$/);
    if (am) {
      const query = am[1];
      suggest.value = { mode: 'file', start: pos - query.length - 1, query };
      fetchAtItems(query);
      return;
    }
    suggest.value = null;
    suggestItems.value = [];
  }

  function applySuggest(item: SuggestItem) {
    const s = suggest.value;
    if (!s) return;
    const ta = textareaRef.value;
    const pos = ta ? (ta.selectionStart ?? inputVal.value.length) : inputVal.value.length;
    const inserted = item.apply + ' ';
    inputVal.value = inputVal.value.slice(0, s.start) + inserted + inputVal.value.slice(pos);
    suggest.value = null;
    suggestItems.value = [];
    nextTick(() => {
      if (ta) {
        ta.focus();
        const cursor = s.start + inserted.length;
        ta.setSelectionRange(cursor, cursor);
      }
    });
  }

  function onKeydown(e: KeyboardEvent) {
    if (suggest.value && suggestItems.value.length) {
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        suppressDetect = true;
        suggestIndex.value = (suggestIndex.value + 1) % suggestItems.value.length;
        return;
      }
      if (e.key === 'ArrowUp') {
        e.preventDefault();
        suppressDetect = true;
        suggestIndex.value = (suggestIndex.value - 1 + suggestItems.value.length) % suggestItems.value.length;
        return;
      }
      if (e.key === 'Tab' || (e.key === 'Enter' && !e.shiftKey)) {
        e.preventDefault();
        applySuggest(suggestItems.value[suggestIndex.value]);
        return;
      }
      if (e.key === 'Escape') {
        e.preventDefault();
        e.stopPropagation();
        suggest.value = null;
        suggestItems.value = [];
        return;
      }
    }
  }

  function clearSuggest() {
    window.clearTimeout(fileSuggestTimer);
    suggest.value = null;
    suggestItems.value = [];
  }

  return {
    suggest,
    suggestItems,
    suggestIndex,
    detectToken,
    applySuggest,
    onKeydown,
    clearSuggest,
  };
}
