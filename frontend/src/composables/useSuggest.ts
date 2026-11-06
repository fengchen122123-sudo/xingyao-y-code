import { ref, nextTick, type Ref } from 'vue';
import { useAppStore } from '../stores/appStore';
import type { SuggestItem } from '../components/InputSuggest.vue';

/**
 * 输入自动补全 composable：/ 斜杠（内置命令 + 技能 + MCP）与 @ 文件引用。
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
  // @ 模式的空状态提示文案（'' = 不展示）。没有它时，检索结果为 0 条会让
  // 面板整体 v-if 消失，用户看到的是「什么都没出现」，无法区分「没命中」
  // 和「功能坏了」——这正是「删掉 @ 再输入就不出东西」的观感来源。
  const suggestEmpty = ref('');
  let fileSuggestTimer: number | undefined;
  // @ 提及的「长度退场」：@ 在中文正文里也常被当普通符号用（「@了他一下」），
  // 这时没有命中结果的面板会一直挂在输入框上方挡内容，用户还得手动按 Esc。
  // 两档阈值负责让它自己收起来：
  //   软阈值——已经展示「没有匹配」空态后继续输入达到该长度：用户显然在写正文；
  //   硬阈值——无论有无命中一律收起：会话名/文件名前缀不可能这么长。
  const AT_QUERY_SOFT_LIMIT = 14;
  const AT_QUERY_HARD_LIMIT = 40;
  // 收起后记下 token 起点与当时长度：同一段 token 内继续输入保持收起，
  // 否则下一次按键空态又挂回来；用户把 query 改短才允许重新检索。
  let atDismissStart = -1;
  let atDismissLen = 0;
  // 方向键导航后 keyup 会触发 detectToken，用此标志跳过一次避免高亮被重置回第一项
  let suppressDetect = false;

  function buildCommandItems(query: string) {
    const q = query.toLowerCase();
    const items: SuggestItem[] = [];
    // 命令候选排最前：内置命令是产品自带能力，命中时优先级高于用户技能与 MCP。
    // 最多占 4 个名额，避免命令一多就把技能候选整体挤出面板（与 @ 面板给会话
    // 留 4 个名额同理）。label 直接用 name（含前导斜杠），desc 用一句话说明。
    for (const c of store.slashCommands) {
      if (!c.name || !c.name.toLowerCase().startsWith('/' + q)) continue;
      items.push({
        key: 'cmd:' + c.name,
        icon: '⌘',
        label: c.name,
        desc: c.summary || c.usage,
        // 选中后命令名后跟一个空格，用户可直接接着写参数（参数位置由后端解析）
        apply: c.name + ' ',
      });
      if (items.length >= 4) break;
    }
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
    const matched = items.filter(it => it.label.slice(1).toLowerCase().startsWith(q));
    // 同一 label 只保留最先出现的一条：顺序即命令 → 技能 → MCP，因此技能
    // short_id 与内置命令同名时以命令为准，面板里不会出现两行一模一样的候选。
    const seen = new Set<string>();
    const merged: SuggestItem[] = [];
    for (const it of matched) {
      if (seen.has(it.label)) continue;
      seen.add(it.label);
      merged.push(it);
    }
    suggestItems.value = merged.slice(0, 8);
    suggestIndex.value = 0;
  }

  // @ 提及的请求序号：每次 fetchAtItems 调用都递增。异步响应回来时若序号已过期，
  // 说明用户已经改了 query（发起过新一轮请求），旧响应一律丢弃，
  // 否则旧 query 的候选会覆盖新 query 已经渲染出来的列表。
  let atSeq = 0;
  // 同一次按键会连续触发 input / keyup（有时还有 click）三个事件，
  // 三者落在同一个 token 上时只需检索一次——真机会话列表查询单次近百毫秒，
  // 重复两次就是双倍卡顿。250ms 内的同 token 直接复用上一轮，
  // 超过窗口（用户删掉 @ 又主动补回）则照常重新检索。
  let lastTokenKey = '';
  let lastTokenAt = 0;

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
    suggestEmpty.value = '';
    if (!api) {
      suggestItems.value = [];
      return;
    }

    // 两侧候选各自的落点：谁先返回谁先渲染，后到的再合并进来，互不覆盖。
    let sessionItems: SuggestItem[] = [];
    let fileItems: SuggestItem[] = [];
    // 两侧是否都已给出结论：只有都到齐才判断「空结果」，
    // 否则会话候选先到、文件还在防抖窗口里，会误报「没有匹配」。
    let sessionsSettled = typeof api.search_sessions !== 'function';
    let filesSettled = typeof api.search_workspace_files !== 'function';
    // 检索接口异常（桥报错 / SQLite busy）与「确实没有命中」要给不同提示
    let sessionsFailed = false;

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
      const merged = [...sessionItems.slice(0, 4), ...fileItems].slice(0, 8);
      // 文件候选晚到会再次 render：只有索引越界才归零，避免把用户已经用
      // 方向键选好的高亮重置回第一项（那会让紧接着的 Enter 选错条目）。
      if (suggestIndex.value >= merged.length) suggestIndex.value = 0;
      suggestItems.value = merged;
      // 空结果要给出明确提示：面板此前是 v-if="items.length"，0 条时整体消失，
      // 用户看到的只是「什么都没出现」，会误判成功能坏了。
      if (!sessionsSettled || !filesSettled) {
        suggestEmpty.value = '';
      } else if (merged.length) {
        suggestEmpty.value = '';
      } else if (sessionsFailed) {
        suggestEmpty.value = '对话检索失败，改一下关键词可重试';
      } else {
        suggestEmpty.value = '没有匹配的对话或文件';
      }
    }

    // —— 会话候选：不防抖，立即发起 ——
    // 接口缺失（旧版桥）时直接跳过，退化为纯文件补全。
    if (typeof api.search_sessions === 'function') {
      // 第三个参数是「用户实际所在的会话名」，由前端权威给出：非空时后端据此
      // 排除自身；新会话草稿态 currentSessionName 为 ''，显式传空串让后端不排除
      // 任何会话——否则后端 _current_session 仍停留在上一个真实会话，用户在这个
      // 空白新会话里 @ 上一个对话会永远搜不到（改名也无效）。
      api
        .search_sessions(query, 12, store.currentSessionName || '')
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
          sessionsSettled = true;
          // 会话候选一到就渲染，不等文件扫描
          render();
        })
        // 会话接口异常（桥报错等）静默忽略：绝不能清空文件候选，
        // 但要记录失败态，让空列表能区分「检索失败」和「确实没有」
        .catch(() => {
          sessionsSettled = true;
          sessionsFailed = true;
          render();
        });
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
          filesSettled = true;
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
    // 以 textarea 的 DOM 值为准而不是 inputVal：输入法组词（composition）
    // 期间 v-model 的 input 监听会被 composing 守卫跳过，此时 inputVal 还是
    // 旧值，拿它 slice 会漏掉刚输入的 @，表现为「删掉 @ 再输入 @ 不出候选」。
    // DOM 值始终是最新的，与 v-model 是否已同步无关。
    const raw = ta.value ?? inputVal.value;
    const pos = ta.selectionStart ?? raw.length;
    const before = raw.slice(0, pos);
    const sm = before.match(/(?:^|\s)\/([^\s]*)$/);
    if (sm) {
      const query = sm[1];
      suggest.value = { mode: 'command', start: pos - query.length - 1, query };
      suggestEmpty.value = '';
      buildCommandItems(query);
      return;
    }
    const am = before.match(/@([^\s@]*)$/);
    if (am) {
      const query = am[1];
      const start = pos - query.length - 1;
      // 同一段 token 已经因过长收起过、且 query 没有变短：维持收起，
      // 避免下一次按键又重开面板重新检索（那样无命中空态会反复挂回来）。
      if (atDismissStart === start && query.length >= atDismissLen) {
        window.clearTimeout(fileSuggestTimer);
        suggest.value = null;
        suggestItems.value = [];
        return;
      }
      // 无命中空态（两侧检索都完成且 0 条）下继续输入，说明用户在写正文；
      // 一旦有候选命中就不走软阈值分支，逐字输入路径片段照样能看到候选。
      const emptyStuck = !suggestItems.value.length && !!suggestEmpty.value;
      if (
        query.length >= AT_QUERY_HARD_LIMIT ||
        (emptyStuck && query.length >= AT_QUERY_SOFT_LIMIT)
      ) {
        atDismissStart = start;
        atDismissLen = query.length;
        clearSuggest();
        return;
      }
      // 同一次按键会连续触发 input / keyup（有时还有 click），三者落在同一个
      // token 上。250ms 内的同 token 直接复用上一轮检索，避免同一 query 反复
      // 打穿桥接层和 SQL（真机单次近百毫秒，重复两次就是双倍卡顿）；
      // 超过窗口说明是用户删掉 @ 后主动补回，必须重新检索。
      const tokenKey = start + ':' + query;
      const now = Date.now();
      if (
        suggest.value &&
        suggest.value.mode === 'file' &&
        lastTokenKey === tokenKey &&
        now - lastTokenAt < 250
      ) {
        return;
      }
      lastTokenKey = tokenKey;
      lastTokenAt = now;
      suggest.value = { mode: 'file', start, query };
      fetchAtItems(query);
      return;
    }
    // token 结束（@ 被删掉、或后面出现空格/@）：清掉长度退场记忆，
    // 下一次重新打 @ 是一段新 token，该重新检索就得重新检索
    suggest.value = null;
    suggestItems.value = [];
    suggestEmpty.value = '';
    atDismissStart = -1;
    atDismissLen = 0;
  }

  function applySuggest(item: SuggestItem) {
    const s = suggest.value;
    if (!s) return;
    const ta = textareaRef.value;
    const pos = ta ? (ta.selectionStart ?? inputVal.value.length) : inputVal.value.length;
    // 统一在这里补一个尾随空格，因此 apply 自带尾随空格的候选（内置命令名后
    // 要接参数）先剥掉再补，避免写回两个空格
    const inserted = item.apply.replace(/\s+$/, '') + ' ';
    inputVal.value = inputVal.value.slice(0, s.start) + inserted + inputVal.value.slice(pos);
    suggest.value = null;
    suggestItems.value = [];
    suggestEmpty.value = '';
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
        suggestEmpty.value = '';
        return;
      }
    }
  }

  function clearSuggest() {
    window.clearTimeout(fileSuggestTimer);
    suggest.value = null;
    suggestItems.value = [];
    suggestEmpty.value = '';
  }

  return {
    suggest,
    suggestItems,
    suggestIndex,
    suggestEmpty,
    detectToken,
    applySuggest,
    onKeydown,
    clearSuggest,
  };
}
