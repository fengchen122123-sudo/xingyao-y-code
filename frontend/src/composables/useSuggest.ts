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

  function fetchFileItems(query: string) {
    window.clearTimeout(fileSuggestTimer);
    fileSuggestTimer = window.setTimeout(async () => {
      const api = window.pywebview && window.pywebview.api;
      if (!api) {
        suggestItems.value = [];
        return;
      }
      try {
        const res = await api.search_workspace_files(query, 8);
        // M26 修复：异步返回时不仅要校验仍在 file 模式，还要校验 query 未变，
        // 否则旧查询的响应会覆盖新查询的列表（闭包捕获本次 query 比对）
        if (!suggest.value || suggest.value.mode !== 'file') return;
        if (suggest.value.query !== query) return;
        const files: string[] = (res && res.files) || [];
        suggestItems.value = files.map(f => ({
          key: f,
          icon: '📄',
          label: f,
          desc: '',
          apply: '@' + f,
        }));
        suggestIndex.value = 0;
      } catch {
        suggestItems.value = [];
      }
    }, 150);
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
      fetchFileItems(query);
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
