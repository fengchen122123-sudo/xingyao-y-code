/**
 * 星瑶移动端全局 store：桌面 appStore.ts 的事件语义子集（移动端所需最小集）。
 * 数据源：bridge RPC（get_initial_state / switch_session / send_user_message ...）
 * + bridge 广播事件（turn_start / token_delta / plan_updated / permission_request ...）。
 */
import { defineStore } from 'pinia';
import { onEvent, rpc } from '../bridge';

export interface ToolDisplay {
  category?: string;
  action?: string;
  target?: string;
  command?: string;
  metrics?: Record<string, number>;
  error_summary?: string;
  is_skill?: boolean;
}
export interface ToolCallUi {
  id: string;
  name: string;
  label: string;
  display?: ToolDisplay;
  result?: { ok: boolean; summary: string; result: string; duration_ms?: number; display?: ToolDisplay };
}

export interface ChatMessage {
  role: 'user' | 'assistant' | 'tool' | 'error';
  _msg_id?: number;
  tool_call_id?: string;
  content: string;
  reasoning_content?: string;
  tool_calls?: ToolCallUi[];
  model_name?: string;
  execution_state?: string;
  error_note?: string;
  media?: { url: string; name?: string; media_type?: string }[];
  metrics?: {
    duration_s: number;
    output_duration_s?: number;
    ttft_ms?: number;
    completion_tokens: number;
    token_speed: number;
  };
  turnId?: string;
}

export interface QueuedMsg {
  id: string;
  text: string;
  attachments: string[];
}

export interface FileNode {
  name: string;
  path?: string;
  children?: FileNode[];
}

export interface SessionItem {
  name: string;
  display_name: string;
  count: number;
  workdir: string;
  is_global: boolean;
  updated_at: string;
}

export interface SessionGroup {
  folder_path: string;
  folder_name: string;
  is_global?: boolean;
  is_current: boolean;
  sessions: SessionItem[];
}

export interface PlanItem {
  id: string;
  subject: string;
  status: string;
  execution_mode: string;
  owner: string;
}

export interface PermOption {
  key: string;
  label: string;
}

export interface PermRequest {
  requestId: string;
  sessionName: string;
  summary: string;
  functionName: string;
  allowAlways: boolean;
  options: string[];
  permissionOptions: PermOption[];
  undoImpact?: Record<string, any> | null;
}

interface TokenUsage {
  used: number;
  window: number;
  cached_pct: number;
  cumulative: number;
}

const HEARTBEAT_MS = 20000;

/**
 * 历史会话里的旧形状 tool_call：{ id, function:{ name, arguments }, summary }。
 * 归一成 UI 形状 { id, name, label, display, result }（对齐桌面 appStore.normalizeHistoryCall）。
 */
function normalizeHistoryCall(raw: any, toolMessages: Map<string, ChatMessage>): ToolCallUi {
  const name = String(raw?.name || raw?.function?.name || raw?.tool_name || 'tool');
  let label = String(raw?.label || raw?.summary || '');
  if (!label) {
    let args: any = raw?.function?.arguments;
    if (typeof args === 'string') {
      try { args = JSON.parse(args); } catch { args = null; }
    }
    if (args && typeof args === 'object') {
      const p = args.file_path || args.path || args.dir_path || args.query || args.command || '';
      label = p ? `${name} ${p}` : name;
    } else {
      label = name;
    }
  }
  const call: ToolCallUi = { id: String(raw?.id || ''), name, label, display: raw?.display || undefined };
  const toolMsg = call.id ? toolMessages.get(call.id) : undefined;
  if (toolMsg) {
    const content = String(toolMsg.content ?? '');
    call.result = { ok: true, summary: content, result: content };
  }
  return call;
}

/**
 * 把历史里独立的 role=tool 消息按 tool_call_id 配对进前一条助手消息的 tool_calls，
 * 配对完成后从列表移除（对齐桌面 pairHistoryToolMessages）。
 */
export function pairHistoryToolMessages(messages: ChatMessage[]): ChatMessage[] {
  const toolMessages = new Map<string, ChatMessage>();
  for (const m of messages) {
    if (m.role === 'tool' && m.tool_call_id) toolMessages.set(String(m.tool_call_id), m);
  }
  const paired: ChatMessage[] = [];
  for (const m of messages) {
    if (m.role === 'tool' && m.tool_call_id && toolMessages.has(String(m.tool_call_id))) continue;
    if (m.role === 'assistant') {
      paired.push({
        ...m,
        tool_calls: (m.tool_calls || []).map((raw: any) => normalizeHistoryCall(raw, toolMessages)),
      });
    } else {
      paired.push(m);
    }
  }
  return paired;
}

/** 历史消息归一化：turn_id → turnId、metrics 透传、tool 消息配对进 assistant */
function normalizeMessages(list: any[]): ChatMessage[] {
  const mapped = (list || []).map((m) => ({
    ...m,
    turnId: m.turnId || m.turn_id || '',
    metrics: m.metrics || undefined,
  }));
  return pairHistoryToolMessages(mapped);
}

/** 思考能力快照（对齐桌面 reasoning_capabilities.build_reasoning_snapshot） */
export interface ReasoningSnapshot {
  kind: string;
  visible: boolean;
  disabled: boolean;
  disabled_reason: string;
  choice: string;
  options: { value: string; label: string }[];
}
const REASONING_DEFAULT: ReasoningSnapshot = {
  kind: 'hidden', visible: false, disabled: false, disabled_reason: '', choice: 'default', options: [],
};

export const useAppStore = defineStore('mobile-app', {
  state: () => ({
    ready: false,
    connected: false,
    // 配对失效（token 过期/被轮换，shim 4401 后置位）：需重新扫码
    pairInvalid: false,
    version: '',
    // 人设：仅取智能体名称用于交互层展示（品牌文案不变；移动端不提供人设配置）
    agentName: '星瑶',
    // 会话
    sessionGroups: [] as SessionGroup[],
    currentSession: '',
    currentWorkdir: '',
    activeModelId: '',
    activeModelName: '',
    models: [] as { id: string; name?: string; provider?: string }[],
    permissionMode: 'confirm',
    // 聊天（当前会话）
    messages: [] as ChatMessage[],
    historyHasMore: false,
    historyOldestId: null as number | null,
    runningSessions: [] as string[],
    // 排队消息（session → 快照，message_queue 事件全量幂等替换）
    queues: {} as Record<string, QueuedMsg[]>,
    // 按回合撤销：turn_id → 检查点状态摘要
    turnUndoStates: {} as Record<string, Record<string, any>>,
    // @ 引用与斜杠命令数据源
    fileTree: [] as FileNode[],
    skills: [] as Record<string, any>[],
    mcps: [] as Record<string, any>[],
    // 思考强度（随当前模型变化）
    reasoning: { ...REASONING_DEFAULT } as ReasoningSnapshot,
    // 流式
    currentStreamingText: '',
    currentThinkingText: '',
    // 计划
    planTitle: '当前任务计划',
    planItems: [] as PlanItem[],
    planUpdatedAt: 0,
    // 审批（跨会话聚合收件箱）
    permRequests: [] as PermRequest[],
    // 指标
    tokenUsage: { used: 0, window: 0, cached_pct: 0, cumulative: 0 } as TokenUsage,
    // 通知
    notices: [] as { text: string; kind: string; ts: number }[],
    // 主题
    theme: (localStorage.getItem('ycode-mobile-theme') === 'light' ? 'light' : 'dark') as 'dark' | 'light',
  }),

  getters: {
    isRunning(state): boolean {
      return state.runningSessions.includes(state.currentSession);
    },
    flatSessions(state): SessionItem[] {
      return state.sessionGroups.flatMap((g) => g.sessions);
    },
    liveStreaming(state): boolean {
      return !!(state.currentStreamingText || state.currentThinkingText);
    },
    pendingPermCount(state): number {
      return state.permRequests.length;
    },
    currentQueue(state): QueuedMsg[] {
      return state.queues[state.currentSession] || [];
    },
  },

  actions: {
    applyTheme(mode: 'dark' | 'light') {
      this.theme = mode;
      document.documentElement.classList.toggle('light', mode === 'light');
      localStorage.setItem('ycode-mobile-theme', mode);
      const meta = document.querySelector('meta[name="theme-color"]');
      if (meta) meta.setAttribute('content', mode === 'light' ? '#f4f4f1' : '#1a1a18');
    },

    pushNotice(text: string, kind = 'notice') {
      if (!text) return;
      this.notices.push({ text: String(text), kind, ts: Date.now() });
      if (this.notices.length > 20) this.notices.splice(0, this.notices.length - 20);
    },

    // ---- 初始化 / 重载 ----

    async init() {
      this.applyTheme(this.theme);
      this.bindEvents();
      await this.reloadInitialState();
      this.startHeartbeat();
      this.ready = true;
    },

    async reloadInitialState() {
      try {
        const st = await rpc<Record<string, any>>('get_initial_state');
        this.sessionGroups = (st.sessions || []) as SessionGroup[];
        this.currentSession = String(st.current_session || '');
        this.currentWorkdir = String(st.workdir || '');
        this.activeModelId = String(st.active_model_id || '');
        this.activeModelName = String(st.active_model_name || '');
        this.models = (st.models || []).map((m: any) => ({ id: String(m.id || ''), name: m.name ? String(m.name) : undefined, provider: m.provider ? String(m.provider) : undefined }));
        this.permissionMode = String(st.permission_mode || 'confirm');
        this.version = String(st.version || '');
        // 人设名称（交互层展示用；后端仅在用户自定义时注入提示词）
        const persona = (st.persona_settings || {}) as Record<string, any>;
        this.agentName = String(persona.agent_name || '星瑶');
        this.messages = normalizeMessages(st.messages || []);
        this.historyHasMore = !!st.history_has_more;
        this.historyOldestId = st.history_oldest_id ?? null;
        this.tokenUsage = (st.token_usage || { used: 0, window: 0, cached_pct: 0, cumulative: 0 }) as TokenUsage;
        this.fileTree = (st.file_tree || []) as FileNode[];
        this.skills = (st.skills || []) as Record<string, any>[];
        this.mcps = (st.mcps || []) as Record<string, any>[];
        this.reasoning = (st.reasoning || { ...REASONING_DEFAULT }) as ReasoningSnapshot;
        this.runningSessions = Array.isArray(st.active_sessions) ? st.active_sessions.map(String) : [];
        this.queues = {};
        const qs = st.message_queues as Record<string, any[]> | undefined;
        if (qs) {
          for (const [name, list] of Object.entries(qs)) {
            this.queues[name] = (list || []).map((m: any) => ({ id: String(m?.id || ''), text: String(m?.text || ''), attachments: Array.isArray(m?.attachments) ? m.attachments.map(String) : [] }));
          }
        }
        void this.refreshUndoStates();
        // 恢复正在流式的回合快照（重连/重载时不丢进行中的输出）
        const snap = st.active_turn_snapshot as Record<string, any> | null;
        if (snap && snap.session_name === this.currentSession) {
          this.currentThinkingText = String(snap.thinking_text || '');
          this.currentStreamingText = String(snap.streaming_text || '');
        }
        this.connected = true;
        this.pairInvalid = false;
        void this.fetchActivePermission(true);
      } catch (e: any) {
        this.connected = false;
        throw e;
      }
    },

    /** 权限事件 / 快照统一入队（幂等） */
    upsertPermRequest(data: Record<string, any>, sessionName = '') {
      const req: PermRequest = {
        requestId: String(data.request_id || ''),
        sessionName: sessionName || String(data.session_name || ''),
        summary: String(data.summary || data.function_name || ''),
        functionName: String(data.function_name || ''),
        allowAlways: !!data.allow_always,
        options: Array.isArray(data.options) ? data.options.map(String) : [],
        permissionOptions: Array.isArray(data.permission_options)
          ? data.permission_options.map((o: any) => ({ key: String(o?.key || ''), label: String(o?.label || '') })).filter((o: PermOption) => o.key && o.label)
          : [],
        undoImpact: (data.undo_impact && typeof data.undo_impact === 'object') ? data.undo_impact : null,
      };
      if (req.requestId && !this.permRequests.some((p) => p.requestId === req.requestId)) {
        this.permRequests.push(req);
      }
      return req;
    },

    /**
     * 拉取当前未决权限快照（断线/重连后补齐错过的事件）。
     * clearWhenEmpty：RPC 可用且确认无未决时清掉残留失效项；旧版桥无此方法时忽略。
     */
    async fetchActivePermission(clearWhenEmpty = false) {
      try {
        const r = await rpc<Record<string, any>>('get_active_permission');
        const data = r?.permission;
        if (data && data.request_id) { this.upsertPermRequest(data); return; }
        if (clearWhenEmpty && this.permRequests.length) this.permRequests = [];
      } catch { /* 旧版桥无 get_active_permission：忽略 */ }
    },

    startHeartbeat() {
      if ((this as any)._hbTimer) return;
      (this as any)._hbTimer = setInterval(async () => {
        if (document.visibilityState !== 'visible') return;
        if (this.pairInvalid) return; // 配对失效：不再无谓探测，等重新扫码
        try {
          const u = await rpc<Record<string, any>>('get_token_usage');
          this.applyUsage(u);
          if (!this.connected) {
            this.connected = true;
            // 重连成功：整体恢复快照，避免错过离线期间的事件
            try { await this.reloadInitialState(); } catch { /* 下一轮心跳重试 */ }
          }
        } catch {
          this.connected = false;
        }
      }, HEARTBEAT_MS);
    },

    applyUsage(u: any) {
      if (!u || typeof u !== 'object') return;
      this.tokenUsage = {
        used: Number(u.used || 0),
        window: Number(u.window || 0),
        cached_pct: Number(u.cached_pct || 0),
        cumulative: Number(u.cumulative || 0),
      };
    },

    /** 主动拉取 token 用量（切会话兜底：switch_session 响应不含 usage，事件可能错过） */
    async refreshUsage() {
      try {
        this.applyUsage(await rpc<Record<string, any>>('get_token_usage'));
      } catch { /* 心跳重试 */ }
    },

    // ---- 会话 ----

    async refreshSessions() {
      try {
        this.sessionGroups = (await rpc<any[]>('get_sessions')) as SessionGroup[];
      } catch { /* 心跳会带出连接态 */ }
    },

    async switchSession(name: string) {
      const st = await rpc<Record<string, any>>('switch_session', name);
      this.currentSession = String(st.session_name || name);
      this.currentWorkdir = String(st.workdir || '');
      // switch_session 返回 active_model_id/name 与 reasoning（旧实现误读 session_model）
      this.activeModelId = String(st.active_model_id || this.activeModelId);
      this.activeModelName = String(st.active_model_name || this.activeModelName);
      if (st.reasoning) this.reasoning = st.reasoning as ReasoningSnapshot;
      this.messages = normalizeMessages(st.messages || []);
      this.historyHasMore = !!st.history_has_more;
      this.historyOldestId = st.history_oldest_id ?? null;
      this.currentStreamingText = '';
      this.currentThinkingText = '';
      this.runningSessions = Array.isArray(st.active_sessions)
        ? st.active_sessions.map(String)
        : this.runningSessions;
      await this.refreshSessions();
      await this.refreshUsage();
      void this.refreshUndoStates();
      void this.fetchActivePermission(true);
    },

    async loadOlder() {
      if (!this.historyHasMore || this.historyOldestId == null || !this.currentSession) return;
      const r = await rpc<Record<string, any>>('load_older_messages', this.currentSession, this.historyOldestId);
      const older = normalizeMessages(r.messages || []);
      this.messages = [...older, ...this.messages];
      this.historyHasMore = !!r.has_more;
      this.historyOldestId = r.oldest_id ?? null;
    },

    async createSession(title: string, workdir?: string) {
      const r = await rpc<Record<string, any>>('create_session', title || '新会话', workdir || null);
      const name = String(r.session_name || (r.session || {}).name || '');
      await this.refreshSessions();
      if (name) await this.switchSession(name);
      return name;
    },

    // ---- 聊天 ----

    async send(text: string, attachments?: string[]) {
      const t = String(text || '').trim();
      if (!t || !this.currentSession) return;
      const paths = (attachments || []).filter((p) => p && p.trim());
      // 对齐客户端：运行中发送由后端入队（FIFO），不乐观上屏；
      // 用户气泡与 running 占位等补发回合的 turn_start 到达时再渲染
      if (!this.runningSessions.includes(this.currentSession)) {
        this.messages.push({ role: 'user', content: t });
        this.messages.push({ role: 'assistant', content: '', reasoning_content: '', tool_calls: [], execution_state: 'running' });
      }
      await rpc('send_user_message', t, this.currentSession, paths.length ? paths : null);
    },

    // ---- 排队消息管理 ----

    async cancelQueued(messageId: string) {
      await rpc('cancel_queued_message', this.currentSession, messageId);
      const q = this.queues[this.currentSession] || [];
      this.queues[this.currentSession] = q.filter((m) => m.id !== messageId);
    },

    async jumpQueue(messageId: string) {
      await rpc('jump_queue', this.currentSession, messageId);
    },

    /** 批量取附件展示名（附件 chip 的 name 仅用于展示，取不到时用路径兜底） */
    async filesInfo(paths: string[]): Promise<{ name: string; path: string }[]> {
      const list = paths.filter((p) => p && p.trim());
      if (!list.length) return [];
      const infos = await rpc<any[]>('get_files_info', list);
      return (infos || [])
        .map((i: any) => ({
          path: String(i?.path || ''),
          name: String(i?.name || ''),
        }))
        .filter((a) => a.path)
        .map((a) => ({ ...a, name: a.name || a.path.split(/[\\/]/).pop() || a.path }));
    },

    /**
     * 撤回排队消息：从队列移除并把内容交回调用方回填输入框，供重新编辑。
     * 先取消后回填——取消失败就原样抛错，草稿不动，避免消息既留在队列又在输入框里。
     */
    async takeQueued(messageId: string): Promise<{ text: string; attachments: { name: string; path: string }[] } | null> {
      const item = (this.queues[this.currentSession] || []).find((m) => m.id === messageId);
      if (!item) return null;
      await this.cancelQueued(messageId);
      let attachments: { name: string; path: string }[] = [];
      if (item.attachments?.length) {
        try {
          attachments = await this.filesInfo(item.attachments);
        } catch {
          // 附件名读不到不影响回填文本：至少把路径还原成 chip
          attachments = item.attachments.map((p) => ({ path: p, name: p.split(/[\\/]/).pop() || p }));
        }
      }
      return { text: item.text, attachments };
    },

    // ---- 附件（手机端经 WS 上传 base64，落盘后路径作为附件发送）----

    async uploadAttachment(name: string, dataB64: string): Promise<string> {
      const r = await rpc<Record<string, any>>('upload_file', name, dataB64);
      if (!r || !r.ok) throw new Error(r?.msg || '上传失败');
      return String(r.path);
    },

    // ---- 按回合撤销（与桌面 ChangedFilesPanel 同链路）----

    async refreshUndoStates() {
      if (!this.currentSession) return;
      try {
        const r = await rpc<Record<string, any>>('list_turn_undo_states', this.currentSession);
        if (r && r.ok) {
          const map: Record<string, Record<string, any>> = {};
          for (const state of r.states || []) map[String(state.turn_id)] = state;
          this.turnUndoStates = map;
        }
      } catch { /* 查询失败不打断会话 */ }
    },

    undoStateOf(turnId?: string): Record<string, any> | null {
      return (turnId && this.turnUndoStates[turnId]) || null;
    },

    async previewUndo(turnId: string): Promise<{ preview: Record<string, any>; token: string } | null> {
      const r = await rpc<Record<string, any>>('preview_turn_undo', this.currentSession, turnId);
      if (!r || !r.ok) return null;
      return { preview: r.preview || {}, token: String(r.preview_token || '') };
    },

    async executeUndo(turnId: string, previewToken: string): Promise<Record<string, any>> {
      const r = await rpc<Record<string, any>>('execute_turn_undo', this.currentSession, turnId, previewToken);
      if (!r || !r.ok) throw new Error(r?.msg || '撤销失败');
      return r.result || {};
    },

    // ---- 会话管理（重命名/删除/清空/导出）----

    async renameSession(oldName: string, newName: string) {
      await rpc('rename_session', oldName, newName);
      if (this.currentSession === oldName) this.currentSession = newName;
      await this.refreshSessions();
    },

    async deleteSession(name: string) {
      await rpc('delete_session', name);
      if (this.currentSession === name) {
        const first = this.flatSessions.find((s) => s.name !== name);
        if (first) await this.switchSession(first.name);
      }
      await this.refreshSessions();
    },

    async clearHistory(name: string) {
      await rpc('clear_session_history', name);
      if (this.currentSession === name) {
        this.messages = [];
        this.historyHasMore = false;
        this.historyOldestId = null;
      }
      await this.refreshSessions();
    },

    async exportSession(): Promise<string> {
      const r = await rpc<Record<string, any>>('export_session', 'md');
      if (!r || (!r.ok && !r.path && !r.file_path)) throw new Error(r?.msg || '导出失败');
      return String(r.path || r.file_path || r.msg || '已导出');
    },

    // ---- 目录浏览（新建会话绑定项目用，手机端内置浏览器数据源）----

    async browseDirs(path = ''): Promise<{ isRoot: boolean; current: string; dirs: { name: string; path: string }[] }> {
      const r = await rpc<Record<string, any>>('browse_remote_dirs', path);
      if (!r || !r.ok) throw new Error(r?.msg || '目录读取失败');
      return { isRoot: !!r.is_root, current: String(r.current || ''), dirs: r.dirs || [] };
    },

    async stopTurn() {
      await rpc('stop_current_turn', this.currentSession);
    },

    // ---- 权限 ----

    async respondPermission(requestId: string, allow: boolean, always = false) {
      await rpc('respond_permission', requestId, allow, always);
      this.permRequests = this.permRequests.filter((p) => p.requestId !== requestId);
    },

    async respondPermissionOption(requestId: string, optionKey: string) {
      await rpc('respond_permission_option', requestId, optionKey);
      this.permRequests = this.permRequests.filter((p) => p.requestId !== requestId);
    },

    async respondPermissionChoice(requestId: string, choiceIndex: number, customText?: string) {
      // ask_user 支持自由文本回答（choiceIndex=-1 + customText，对齐桌面 PermCard）
      await rpc('respond_permission_choice', requestId, choiceIndex, customText || '');
      this.permRequests = this.permRequests.filter((p) => p.requestId !== requestId);
    },

    // ---- 设置 ----

    async setPermissionMode(mode: string) {
      await rpc('set_permission_mode', mode, this.currentSession);
      this.permissionMode = mode;
    },

    async setModel(modelId: string) {
      const r = await rpc<Record<string, any>>('set_model', modelId, this.currentSession);
      this.activeModelId = String(r?.active_model_id || modelId);
      if (r?.active_model_name) this.activeModelName = String(r.active_model_name);
      if (r?.reasoning) this.reasoning = r.reasoning as ReasoningSnapshot;
    },

    /** 设置思考强度/开关（下一轮请求生效）；乐观更新 + 失败回滚 */
    async setReasoning(choice: string) {
      const prev = this.reasoning;
      this.reasoning = { ...prev, choice };
      try {
        const r = await rpc<Record<string, any>>('set_reasoning_choice', this.activeModelId, choice);
        if (!r || r.ok === false) throw new Error(r?.msg || '设置失败');
        if (r.reasoning) this.reasoning = r.reasoning as ReasoningSnapshot;
        return r?.msg as string | undefined;
      } catch (e) {
        this.reasoning = prev;
        throw e;
      }
    },

    /** 工作区文件检索（@ 引用文件，与客户端 search_workspace_files 一致） */
    async searchWorkspaceFiles(query: string, limit = 20): Promise<string[]> {
      const r = await rpc<Record<string, any>>('search_workspace_files', query, limit);
      return ((r?.files || []) as string[]).map(String);
    },

    // ---- 事件语义（appStore.ts 子集） ----

    bindEvents() {
      onEvent((e) => this.applyEvent(e.type || '', e.data || {}));
    },

    isForCurrent(data: Record<string, any>): boolean {
      const s = String(data.session_name || '');
      return !s || s === this.currentSession;
    },

    applyEvent(type: string, data: Record<string, any>) {
      const session = String(data.session_name || '');
      const terminal = type === 'turn_finish' || type === 'turn_cancelled' || type === 'turn_error' || type === 'turn_idle';

      // 配对失效（shim 收到 4401）：停止一切交互，等待用户重新扫码
      if (type === 'pair_invalid') {
        this.pairInvalid = true;
        this.connected = false;
        return;
      }

      if (type === 'turn_start') {
        const s = session || this.runningSessions[0] || '';
        if (s && !this.runningSessions.includes(s)) this.runningSessions.push(s);
        if (s === this.currentSession) {
          this.currentStreamingText = '';
          this.currentThinkingText = '';
          const userText = String(data.display_text || data.user_text || '');
          const already = userText && this.messages.some(
            (m) => m.role === 'user' && m.content === userText,
          );
          if (userText && !already) this.messages.push({ role: 'user', content: userText });
          // 按回合撤销：权威 turn_id 绑定到本轮用户消息（撤销卡片定位键）
          if (data.turn_id) {
            for (let i = this.messages.length - 1; i >= 0; i--) {
              const m = this.messages[i];
              if (m.role === 'user' && m.content === userText && !m.turnId) {
                m.turnId = String(data.turn_id);
                break;
              }
            }
          }
          const tail = this.messages[this.messages.length - 1];
          const tailPlaceholder = tail && tail.role === 'assistant' && !tail.content && !tail.reasoning_content && !(tail.tool_calls || []).length;
          if (!tailPlaceholder) {
            this.messages.push({ role: 'assistant', content: '', reasoning_content: '', tool_calls: [], execution_state: 'running' });
          }
        }
        return;
      }

      if (type === 'round_started') {
        if (!this.isForCurrent(data)) return;
        this.currentStreamingText = '';
        this.currentThinkingText = '';
        const last = this.messages[this.messages.length - 1];
        if (last && last.role === 'assistant' &&
            ((last.reasoning_content || '').trim() || (last.content || '').trim() || (last.tool_calls || []).length)) {
          last.execution_state = 'completed';
          this.messages.push({ role: 'assistant', content: '', reasoning_content: '', tool_calls: [], execution_state: 'running' });
        }
        return;
      }

      if (type === 'thinking_delta' || type === 'token_delta') {
        if (!this.isForCurrent(data)) return;
        if (type === 'thinking_delta') this.currentThinkingText += data.text || '';
        else this.currentStreamingText += data.text || '';
        let last = this.messages[this.messages.length - 1];
        if (!last || last.role !== 'assistant') {
          last = { role: 'assistant', content: this.currentStreamingText, reasoning_content: this.currentThinkingText, tool_calls: [], execution_state: 'running' };
          this.messages.push(last);
        } else {
          if (type === 'thinking_delta') last.reasoning_content = this.currentThinkingText;
          else last.content = this.currentStreamingText;
        }
        return;
      }

      if (type === 'message_final') {
        if (!this.isForCurrent(data)) return;
        const last = this.messages[this.messages.length - 1];
        if (last && last.role === 'assistant') {
          const finalText = String(data.content || '');
          const finalReasoning = String(data.reasoning_content || '');
          if (finalText.length > (last.content || '').length) {
            last.content = finalText;
            this.currentStreamingText = finalText;
          }
          if (finalReasoning.length > (last.reasoning_content || '').length) {
            last.reasoning_content = finalReasoning;
            this.currentThinkingText = finalReasoning;
          }
          const finalMedia = data.media as ChatMessage['media'];
          if (finalMedia && finalMedia.length) last.media = finalMedia;
        }
        return;
      }

      if (type === 'step_metrics') {
        if (!this.isForCurrent(data)) return;
        const last = this.messages[this.messages.length - 1];
        if (last && last.role === 'assistant') {
          last.metrics = {
            duration_s: Number(data.duration_s || 0),
            output_duration_s: Number(data.output_duration_s || 0),
            ttft_ms: data.ttft_ms != null ? Number(data.ttft_ms) : undefined,
            completion_tokens: Number(data.completion_tokens || 0),
            token_speed: Number(data.token_speed || 0),
          };
        }
        return;
      }

      if (type === 'user_message_images') {
        if (!this.isForCurrent(data)) return;
        const images = (Array.isArray(data.images) ? data.images : []).map(String).filter(Boolean);
        if (!images.length) return;
        // 挂到最近一条用户气泡（turn_start 先行、图片随后补挂，与桌面一致）
        for (let i = this.messages.length - 1; i >= 0; i--) {
          const m = this.messages[i];
          if (m.role === 'user') {
            m.media = [...(m.media || []), ...images.map((url) => ({ url }))];
            break;
          }
        }
        return;
      }

      if (type === 'message_queue') {
        // 排队消息全量快照：幂等替换该会话队列（与桌面同模式，免疫事件乱序）
        const qSession = session || this.currentSession;
        const raw = Array.isArray(data.queue) ? data.queue : [];
        const list: QueuedMsg[] = raw
          .map((m: any) => ({ id: String(m?.id || ''), text: String(m?.text || ''), attachments: Array.isArray(m?.attachments) ? m.attachments.map(String) : [] }))
          .filter((m) => !!m.id);
        if (qSession) {
          if (list.length) this.queues[qSession] = list;
          else delete this.queues[qSession];
        }
        return;
      }

      if (type === 'file_tree_loaded') {
        if (Array.isArray(data.tree)) this.fileTree = data.tree as FileNode[];
        return;
      }

      if (type === 'skills_loaded') {
        if (Array.isArray(data.skills)) this.skills = data.skills;
        return;
      }

      if (type === 'turn_undo_updated') {
        if (session === this.currentSession) void this.refreshUndoStates();
        if (data.message) this.pushNotice(String(data.message), 'notice');
        return;
      }

      if (type === 'tool_start') {
        if (!this.isForCurrent(data)) return;
        const last = this.messages[this.messages.length - 1];
        if (!last || last.role !== 'assistant') return;
        const calls = last.tool_calls ?? (last.tool_calls = []);
        const id = String(data.id || '');
        if (!calls.some((c) => c.id === id)) {
          calls.push({ id: id || `ui-${calls.length + 1}`, name: String(data.name || 'tool'), label: String(data.label || data.name || 'tool'), display: data.display || undefined });
        }
        return;
      }

      if (type === 'tool_finish') {
        if (!this.isForCurrent(data)) return;
        const last = this.messages[this.messages.length - 1];
        if (!last || last.role !== 'assistant') return;
        const calls = last.tool_calls ?? (last.tool_calls = []);
        const id = String(data.id || '');
        const call = calls.find((c) => c.id === id);
        const display = data.display || undefined;
        const result = { ok: !!data.ok, summary: String(data.summary || ''), result: String(data.result || data.error || ''), duration_ms: data.duration_ms != null ? Number(data.duration_ms) : undefined, display };
        if (call) {
          call.result = result;
          if (display) call.display = display;
        } else {
          calls.push({ id: id || `orphan-${calls.length + 1}`, name: String(data.name || 'tool'), label: String(data.label || data.name || 'tool'), display, result });
        }
        return;
      }

      if (type === 'plan_updated') {
        if (Array.isArray(data.items)) {
          this.planTitle = String(data.title || '当前任务计划');
          this.planItems = data.items.map((it: any) => ({
            id: String(it.id || Math.random()),
            subject: String(it.subject || it.text || it.title || ''),
            status: String(it.status || ''),
            execution_mode: String(it.execution_mode || ''),
            owner: String(it.owner || ''),
          }));
          this.planUpdatedAt = Date.now();
        }
        return;
      }

      if (type === 'plan_item_handoff') {
        const item = (data.item || {}) as any;
        const id = String(item.id || '');
        const target = this.planItems.find((p) => p.id === id);
        if (target) {
          target.execution_mode = String(data.mode || item.execution_mode || target.execution_mode);
        }
        return;
      }

      if (type === 'permission_request') {
        this.upsertPermRequest(data, session);
        return;
      }

      if (type === 'permission_dismiss') {
        // 后端权限队列已清空（超时拒绝/取消）：整体重置，与桌面语义一致
        this.permRequests = [];
        return;
      }

      if (type === 'notice') {
        if (data.message && (!session || session === this.currentSession)) {
          this.pushNotice(String(data.message), data.kind === 'error' ? 'error' : 'notice');
        }
        return;
      }

      if (terminal) {
        const s = session || this.runningSessions[0] || '';
        this.runningSessions = this.runningSessions.filter((x) => x !== s);
        if (s === this.currentSession) {
          const last = this.messages[this.messages.length - 1];
          if (last && last.role === 'assistant' && last.execution_state === 'running') {
            last.execution_state = type === 'turn_error' ? 'error' : 'completed';
          }
          if (type === 'turn_error' && data.error) {
            if (last && last.role === 'assistant') last.error_note = String(data.error);
            else this.pushNotice(String(data.error), 'error');
          }
          this.currentStreamingText = '';
          this.currentThinkingText = '';
        }
        return;
      }

      if (type === 'turn_queued') {
        const s = session || this.currentSession;
        if (s && !this.runningSessions.includes(s)) this.runningSessions.push(s);
        return;
      }

      if (type === 'token_usage' || type === 'token_usage_updated') {
        this.applyUsage(data.usage || data);
        return;
      }

      if (type === 'model_changed') {
        if (!session || session === this.currentSession) {
          this.activeModelId = String(data.model_id || this.activeModelId);
          this.activeModelName = String(data.model_name || data.model_id || this.activeModelName);
          if (data.reasoning) this.reasoning = data.reasoning as ReasoningSnapshot;
        }
        return;
      }

      if (type === 'mcp_status') {
        if (Array.isArray(data.mcps)) this.mcps = data.mcps as Record<string, any>[];
        return;
      }

      if (type === 'session_renamed' || type === 'active_session_changed') {
        void this.refreshSessions();
        return;
      }
    },
  },
});
