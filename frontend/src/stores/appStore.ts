import { defineStore } from 'pinia';
import type {
  ModelInfo,
  ProviderInfo,
  ApiProtocolInfo,
  PresetProviderInfo,
  ModelSettingsSnapshot,
  SessionGroup,
  SkillInfo,
  McpInfo,
  PluginInfo,
  FileNode,
  BridgeEvent,
  PlanData,
  BackgroundTask,
  TokenUsage,
  TokenStatisticsSnapshot,
  PermissionMode,
  ReasoningEffort,
  PyWebViewAPI,
  ToolStartEventData,
  ToolFinishEventData,
  DiffReadyEventData,
  SubagentProgressEventData,
} from '../types/pywebview';
import { formatPermMode } from '../utils/markdown';
import {
  reduceExecutionState,
  type ExecutionState,
  type LegacyDiff,
  type SubagentTaskSnapshot,
  type ToolDisplay,
  type ToolExecutionCall,
  type ToolExecutionResult,
} from '../utils/toolTrace';
import { createSessionRuntime, type SessionRuntimeState } from './sessionRuntime';

export const normalizeTokenUsage = (data: any = {}): TokenUsage => {
  const nullableNumber = (value: unknown): number | null => {
    if (value === null || value === undefined) return null;
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  };
  return {
    used: Number(data.used ?? 0),
    window: Number(data.window ?? 0),
    cached_pct: Number(data.cached_pct ?? 0),
    cumulative: Number(data.cumulative ?? 0),
    cache_latest_pct: nullableNumber(data.cache_latest_pct),
    cache_hot_pct: nullableNumber(data.cache_hot_pct),
    cache_session_pct: nullableNumber(data.cache_session_pct),
    cache_available: Boolean(data.cache_available),
    cache_hot_turns: Number(data.cache_hot_turns ?? 0),
    cache_invalidation_reason: String(data.cache_invalidation_reason ?? ''),
    prompt_tokens: Number(data.prompt_tokens ?? 0),
    cached_tokens: Number(data.cached_tokens ?? 0),
    // CLI 状态栏口径：输入/输出/命中（计费累计），旧后端无此字段时回落 0
    total_prompt: Number(data.total_prompt ?? 0),
    total_completion: Number(data.total_completion ?? 0),
    total_cached: Number(data.total_cached ?? 0),
  };
};

const emptyTokenUsage = (): TokenUsage => normalizeTokenUsage();

export interface ChatMessage {
  id?: string;
  // notice：对话流内的系统提示（取代原 Toast，不自动消失）
  role: 'user' | 'assistant' | 'tool' | 'error' | 'notice';
  content?: string;
  // 用户消息附带的图片（base64 dataURL 列表，支持多张）
  images?: string[];
  // 后端完成附件解析后补发的持久说明，显示在对应用户消息内
  attachment_notes?: string[];
  reasoning_content?: string;
  tool_calls?: ToolExecutionCall[];
  tool_call_id?: string;
  name?: string;
  cancelled?: boolean;
  // 未绑定到任何 tool_call 的历史文件变更（diff_ready 缺少/失配 tool_call_id 时兜底）
  legacy_diffs?: LegacyDiff[];
  // 本条助手消息所属回合的执行状态：running | completed | cancelled | error
  execution_state?: ExecutionState;
  // 回合进行中到达的非终态错误：挂在当前助手消息上展示，
  // 而不是插队成独立 error 消息（插队会破坏"最后一条是 assistant"的事件契约，
  // 导致后续 tool_finish 抛错、running 状态卡死）
  error_note?: string;
}

const TOOL_CATEGORIES = new Set(['read', 'search', 'command', 'mutation', 'skill', 'other']);

// 后端 display dict 字段可缺省/为空：没有任何有效内容时返回 undefined，
// 让 toolTrace 按工具名回退到 legacy 分类。
function normalizeToolDisplay(raw: any): ToolDisplay | undefined {
  if (!raw || typeof raw !== 'object') return undefined;
  const metrics = raw.metrics && typeof raw.metrics === 'object' ? { ...raw.metrics } : {};
  const hasContent =
    !!(raw.action || raw.target || raw.command || raw.error_summary)
    || !!raw.is_skill
    || Object.keys(metrics).length > 0
    || (typeof raw.category === 'string' && raw.category !== '');
  if (!hasContent) return undefined;
  return {
    category: TOOL_CATEGORIES.has(raw.category) ? raw.category : 'other',
    action: String(raw.action || ''),
    target: String(raw.target || ''),
    command: String(raw.command || ''),
    metrics,
    error_summary: String(raw.error_summary || ''),
    is_skill: !!raw.is_skill,
  };
}

// 历史会话里的旧形状 tool_call：{ id, function: { name, arguments }, summary }
function normalizeHistoryCall(raw: any, toolMessages: Map<string, ChatMessage>): ToolExecutionCall {
  const name = String(raw?.name || raw?.function?.name || raw?.tool_name || 'tool');
  let label = String(raw?.label || raw?.summary || '');
  if (!label) {
    const args = raw?.function?.arguments;
    if (args && typeof args === 'object') {
      const p = args.file_path || args.path || args.query || args.command || '';
      label = p ? `${name} ${p}` : name;
    } else {
      label = name;
    }
  }
  const call: ToolExecutionCall = { id: String(raw?.id || ''), name, label };
  const toolMsg = call.id ? toolMessages.get(call.id) : undefined;
  if (toolMsg) {
    const content = String(toolMsg.content || '');
    call.result = { ok: true, summary: content, result: content };
  }
  return call;
}

// 历史用户消息的多模态 list 内容（[text, image_url, ...]）拆成纯文本 + 图片列表；
// 更早的历史图片已被后端折叠为占位文本，自然留在 text 里
function normalizeUserMediaMessage(m: ChatMessage): ChatMessage {
  if (!Array.isArray(m.content)) return m;
  const texts: string[] = [];
  const images: string[] = [];
  for (const block of m.content as any[]) {
    if (block?.type === 'text') {
      texts.push(String(block.text || ''));
    } else if (block?.type === 'image_url') {
      const url = String(block?.image_url?.url || '');
      if (url) images.push(url);
    }
  }
  return {
    ...m,
    content: texts.join('\n'),
    images: images.length ? images : undefined,
  };
}

// 加载历史会话（启动恢复 / switch_session）时，把旧契约的独立 role=tool 消息
// 按显式 tool_call_id 配对进前一条助手消息的 tool_calls，配对完成后从列表移除。
export function pairHistoryToolMessages(messages: ChatMessage[]): ChatMessage[] {
  const toolMessages = new Map<string, ChatMessage>();
  for (const m of messages) {
    if (m.role === 'tool' && m.tool_call_id) toolMessages.set(m.tool_call_id, m);
  }
  const paired: ChatMessage[] = [];
  for (const m of messages) {
    if (m.role === 'tool' && m.tool_call_id && toolMessages.has(m.tool_call_id)) continue;
    if (m.role === 'assistant') {
      paired.push({
        ...m,
        tool_calls: (m.tool_calls || []).map((raw: any) => normalizeHistoryCall(raw, toolMessages)),
        legacy_diffs: m.legacy_diffs || [],
        execution_state: m.execution_state || 'completed',
      });
    } else if (m.role === 'user') {
      paired.push(normalizeUserMediaMessage(m));
    } else {
      paired.push(m);
    }
  }
  return paired;
}

export interface ConfirmState {
  show: boolean;
  title: string;
  sub: string;
  message: string;
  confirmLabel: string;
  // notice 模式：单按钮提示框（只有"确定"），用于必须让用户看到的非对话类提示
  notice?: boolean;
  resolve?: (ok: boolean) => void;
}

export interface PermReqState {
  show: boolean;
  sessionName: string;
  reqId: string;
  // 人类可读描述（后端 describe_permission_request 生成）：执行什么命令 / 查看什么文件
  summary: string;
  source: string;
  // 沙盒外访问等场景后端只给两项选项，不渲染“总是允许”
  allowAlways: boolean;
  // ask_user 澄清问题的自定义选项（非空时渲染为选项列表而非三按钮）
  options: string[];
  // 请求来源工具名：'ask_user' 时为澄清问题，否则为标准权限请求
  functionName: string;
}

export type SubpageType = 'skills' | 'mcp' | 'plugins' | 'models';

export const useAppStore = defineStore('app', {
  state: () => ({
    // 模型 / 供应商
    models: [] as ModelInfo[],
    providers: [] as ProviderInfo[],
    modelProtocols: [] as ApiProtocolInfo[],
    modelPresets: [] as PresetProviderInfo[],
    activeModelId: '',
    activeModelName: '',
    // 副模型：可选，用于识图(vision)/上下文压缩(compression)/子Agent(subagent)等场景
    secondaryModelId: '',
    secondaryModelRoles: { vision: true, compression: true, subagent: true } as { vision: boolean; compression: boolean; subagent: boolean },

    // 权限模式（规范值：confirm | accept_edits | plan | full，展示一律经 formatPermMode）
    permissionMode: 'confirm' as PermissionMode,

    // 沙盒开关（true=允许访问工作目录外文件），与 CLI /sandbox 共享后端配置
    sandboxEnabled: false,

    // 当前激活模型的推理强度档位（default = 内置默认）
    reasoningEffort: 'default' as ReasoningEffort,

    // 会话 / 项目工作区
    sessionGroups: [] as SessionGroup[],
    currentSessionName: '',
    currentSessionDisplayName: '新对话',
    // 懒创建会话：点“新会话”只进入空白状态不落库；发首条消息时才真正创建，
    // 值为目标归属（'GLOBAL' 或项目路径），null = 非 pending 状态
    pendingWorkdir: null as string | null,
    // pending 草稿会话预选的模型：草稿期不落库（后端 _current_session 还指向上一个
    // 真实会话，此时调 set_model 会污染旧会话），首条消息发送时随 send_user_message
    // 一并提交落库；发送失败回退草稿时保留以便重发
    draftModelId: '',
    // 各会话模型选择的版本号：setModel 递增；switchSession 在途期间若版本变化，
    // 说明用户又切了模型，后端响应携带的旧模型快照不再覆盖本地选择
    _sessionModelVersion: {} as Record<string, number>,
    currentWorkdirLabel: '对话',
    workdir: '',
    fileTree: [] as FileNode[],
    // 文件树刷新中标记（驱动刷新按钮的 loading 态，并防止并发刷新）
    fileTreeRefreshing: false,
    // 文件树展开状态（按完整路径记录）：刷新后据此恢复展开并逐层刷新内容
    fileTreeExpanded: [] as string[],
    // 文件树后台加载序号：快速连切会话时过期结果凭序号丢弃
    fileTreeReqSeq: 0,
    // M31 修复：各类异步刷新的请求序号，响应返回时序号已过期则丢弃，
    // 避免并发/重复刷新时旧快照覆盖新快照
    sessionsReqSeq: 0,
    tokenUsageReqSeq: 0,
    mcpReqSeq: 0,
    pluginsReqSeq: 0,
    // M27 修复：文件预览请求序号，旧响应不覆盖新打开文件的内容
    filePreviewReqSeq: 0,

    // 技能 / MCP / 插件
    skills: [] as SkillInfo[],
    mcps: [] as McpInfo[],
    plugins: [] as PluginInfo[],

    // 对话消息流
    messages: [] as ChatMessage[],
    running: false,
    // 当前会话本轮回合开始时间戳（毫秒）："已工作 xx秒"计时用，随会话切换恢复
    turnStartedAt: 0,
    // 正在执行回合的会话集合（支持多会话并行）
    runningSessions: [] as string[],
    // 批次D：回合已提交但在等 _turn_workdir_lock 的会话（turn_queued 事件）。
    // 排队也算运行中（同时在 runningSessions 中），此集合仅用于 UI 区分
    // "排队等其他会话"与"正在执行"；turn_start/终态事件到达时清除
    queuedSessions: [] as string[],
    // 非当前会话的状态提醒点：sessionName → 'green'(完成) | 'yellow'(待确认) | 'red'(报错)
    // 仅给非当前窗口打点，切换到该会话时清除
    sessionStatusDots: {} as Record<string, 'green' | 'yellow' | 'red'>,
    // 每个会话独立保存流式消息和回合状态；切换会话不丢失后台事件
    sessionRuntimeByName: {} as Record<string, SessionRuntimeState>,
    showThinking: false,

    // 图片大图查看（Lightbox）
    lightbox: { show: false, images: [] as string[], index: 0 },

    // 后端连接状态 / 版本号
    connected: false,
    version: '',
    // 启动初始化是否完成（控制 Y-CODE 启动画面，完成后淡出进入对话）
    initialized: false,

    // 侧栏与面板开合状态
    sidebarCollapsed: false,
    rightPanelCollapsed: false,
    themeLight: false,
    settingsOpen: false,

    // 计划、后台任务、Token 用量
    planData: { title: '', items: [], progress: 0 } as PlanData,
    backgroundTasks: [] as BackgroundTask[],
    subagentTasks: [] as SubagentTaskSnapshot[],
    tokenUsage: emptyTokenUsage(),
    tokenStatistics: null as TokenStatisticsSnapshot | null,
    tokenStatisticsLoading: false,
    tokenStatisticsDays: 30 as 1 | 7 | 30,

    // 确认弹窗（notice 模式时退化为单按钮提示框，用户点击"确定"才关闭）
    confirmModal: { show: false, title: '', sub: '', message: '', confirmLabel: '确定删除 ⏎', notice: false } as ConfirmState,

    // 权限请求弹窗
    permRequest: { show: false, sessionName: '', reqId: '', summary: '', source: '', allowAlways: false, options: [], functionName: '' } as PermReqState,

    // 二级抽屉页面：技能 / MCP / 模型管理
    subpage: { show: false, type: 'skills' as SubpageType },

    // 文件预览弹窗（RightPanel 文件树点击文件时使用）
    filePreview: {
      show: false,
      path: '',
      content: '',
      truncated: false,
      loading: false,
    },

    // 当前流式生成临时对象
    currentStreamingText: '',
    currentThinkingText: '',

    // 内嵌终端（右侧面板）
    terminalOutput: '',
    terminalRunning: false,
  }),

  getters: {
    currentSessionRunning(state): boolean {
      // M30 修复：判定源统一为 runningSessions（与 sendMessage 的拦截判定一致），
      // 避免此前 sessionRuntimeByName[key].running 与 runningSessions 不同步时
      // 发送按钮显示与 store 实际拦截行为不一致（按钮可点但消息被吞）。
      // 注意 runningSessions 只记录真实会话名，pending 草稿会话不在其中
      // （首条消息发出前本就允许发送），与 sendMessage 拦截语义完全一致。
      return !!state.currentSessionName && state.runningSessions.includes(state.currentSessionName);
    },

    // 批次D：当前会话是否在排队等其他会话的回合释放锁（turn_queued）
    currentSessionQueued(state): boolean {
      return !!state.currentSessionName && state.queuedSessions.includes(state.currentSessionName);
    },

    // 按供应商分组的模型列表，ModelCascader 与 Subpage(models) 共用
    modelsByProvider(state): Record<string, ModelInfo[]> {
      const groups: Record<string, ModelInfo[]> = {};
      state.models.forEach(m => {
        const p = m.provider_name || '未分组';
        if (!groups[p]) groups[p] = [];
        groups[p].push(m);
      });
      return groups;
    },

    // 供 "添加模型" 表单使用的供应商下拉选项：
    // 合并显式添加过的供应商(带真实 id) 与 从现有模型列表反推出的供应商名称
    // （契约未提供 get_providers 接口，此处以 provider_name 作为兜底 id）
    providerOptions(state): Array<{ id: string; name: string }> {
      const map = new Map<string, string>();
      state.providers.forEach(p => map.set(p.id, p.name));
      state.models.forEach(m => {
        if (!map.has(m.provider_name)) map.set(m.provider_name, m.provider_name);
      });
      return Array.from(map.entries()).map(([id, name]) => ({ id, name }));
    },
  },

  actions: {
    _pendingRuntimeKey(this: any): string {
      return `__pending__:${this.pendingWorkdir || 'GLOBAL'}`;
    },

    _runtimeKey(this: any, sessionName = this.currentSessionName): string {
      return sessionName || this._pendingRuntimeKey();
    },

    _getSessionRuntime(this: any, sessionName = this.currentSessionName): SessionRuntimeState {
      const key = this._runtimeKey(sessionName);
      if (!this.sessionRuntimeByName[key]) {
        this.sessionRuntimeByName[key] = createSessionRuntime();
      }
      return this.sessionRuntimeByName[key];
    },

    _captureRuntime(this: any, sessionName = this.currentSessionName) {
      const runtime = this._getSessionRuntime(sessionName);
      runtime.messages = this.messages;
      runtime.streamingText = this.currentStreamingText;
      runtime.thinkingText = this.currentThinkingText;
      runtime.running = this.running;
      runtime.subagentTasks = this.subagentTasks;
      runtime.planData = this.planData;
      runtime.tokenUsage = this.tokenUsage;
      runtime.turnStartedAt = this.turnStartedAt;
      // 模型选择随会话保存：切换会话时各会话恢复各自的模型，互不覆盖
      runtime.modelId = this.activeModelId;
      runtime.modelName = this.activeModelName;
    },

    _captureCurrentRuntime(this: any) {
      this._captureRuntime(this.currentSessionName);
    },

    // 状态提醒点：非当前窗口才打点；手动停止(turn_cancelled)不打点
    _setSessionStatusDot(this: any, sessionName: string, kind: 'green' | 'yellow' | 'red') {
      if (!sessionName || sessionName === this.currentSessionName) return;
      this.sessionStatusDots[sessionName] = kind;
    },
    _clearSessionStatusDot(this: any, sessionName: string) {
      if (sessionName && sessionName in this.sessionStatusDots) {
        delete this.sessionStatusDots[sessionName];
      }
    },

    _bindSessionRuntime(this: any, sessionName = this.currentSessionName) {
      const runtime = this._getSessionRuntime(sessionName);
      this.messages = runtime.messages;
      this.currentStreamingText = runtime.streamingText;
      this.currentThinkingText = runtime.thinkingText;
      this.running = runtime.running;
      this.subagentTasks = runtime.subagentTasks;
      this.planData = runtime.planData || { title: '', items: [], progress: 0 };
      // 窗口口径 token 用量随会话切换：先恢复该会话上次快照避免闪烁，
      // 后端 token_usage 事件到达后再校正为最新值
      if (runtime.tokenUsage) this.tokenUsage = runtime.tokenUsage;
      // "已工作 xx秒"计时随会话切换恢复该会话的回合开始时间戳
      this.turnStartedAt = runtime.turnStartedAt || 0;
      // 模型选择随会话即时恢复（不等后端 switch_session 响应）：该会话显式
      // 选过模型才恢复，且该模型仍存在（未被删除）；否则保持当前值，
      // 由后端响应校正为该会话落库的绑定模型
      if (runtime.modelId !== undefined) {
        if (!this.models.length || this.models.some((m: ModelInfo) => m.id === runtime.modelId)) {
          this.activeModelId = runtime.modelId;
          this.activeModelName = runtime.modelName || this.activeModelName;
        }
      }
    },

    _setSessionRunning(this: any, sessionName: string, running: boolean) {
      const runtime = this._getSessionRuntime(sessionName);
      runtime.running = running;
      if (sessionName === this.currentSessionName) this.running = running;
      if (running) {
        if (sessionName && !this.runningSessions.includes(sessionName)) {
          this.runningSessions.push(sessionName);
        }
      } else {
        this.runningSessions = this.runningSessions.filter((s: string) => s !== sessionName);
        // 排队标记随运行态一并清除（stop 兑底/发送失败等本地解锁路径也走这里）
        this.queuedSessions = this.queuedSessions.filter((s: string) => s !== sessionName);
      }
    },

    // 统一的后端可用性检查：不可用时自动置 connected=false 并弹窗提示。
    // 返回值经 TS 收窄为局部 const，后续 await 调用不会丢失类型narrowing。
    _api(): PyWebViewAPI | null {
      const api = window.pywebview && window.pywebview.api;
      if (!api) {
        this.connected = false;
        this.notifyModal('连接失败', '后端连接失败：pywebview API 不可用');
        return null;
      }
      return api;
    },

    // 回合级事件（tool_start / tool_finish / diff_ready）必须落在当前助手消息上；
    // 缺失说明后端事件序列违反契约，直接抛出显式错误而不是静默丢弃。
    activeAssistantOrThrow(): ChatMessage {
      const last = this.messages[this.messages.length - 1];
      if (!last || last.role !== 'assistant') {
        throw new Error('桥接契约错误：回合级事件到达时不存在活动助手消息');
      }
      return last;
    },

    openLightbox(images: string[], index: number) {
      if (!images || !images.length) return;
      this.lightbox = { show: true, images, index: Math.max(0, Math.min(index, images.length - 1)) };
    },

    closeLightbox() {
      this.lightbox.show = false;
    },

    lightboxNav(delta: number) {
      const n = this.lightbox.images.length;
      if (!this.lightbox.show || n <= 1) return;
      this.lightbox.index = (this.lightbox.index + delta + n) % n;
    },

    toggleSidebar() {
      this.sidebarCollapsed = !this.sidebarCollapsed;
    },

    toggleRightPanel() {
      this.rightPanelCollapsed = !this.rightPanelCollapsed;
    },

    toggleTheme() {
      this.themeLight = !this.themeLight;
      // body.light 是唯一的浅色主题开关机制（对应 style.css 中的 body.light 选择器）
      document.body.classList.toggle('light', this.themeLight);
      // localStorage 镜像：下次启动 splash 渲染前即可恢复主题（见 main.ts）
      try { localStorage.setItem('ycode-theme-light', this.themeLight ? '1' : '0'); } catch {}
      // 持久化主题到后端 config.json（关闭重开不变），并同步 Windows 窗口标题栏明暗
      this._api()?.set_gui_theme(this.themeLight)?.catch(() => {});
    },

    toggleShowThinking() {
      this.showThinking = !this.showThinking;
    },

    openSubpage(type: SubpageType) {
      this.subpage = { show: true, type };
    },

    closeSubpage() {
      this.subpage.show = false;
    },

    // 对话类提示：写入当前会话消息流（notice 角色），不自动消失。
    // 回合进行中必须插到 running assistant 之前：push 到末尾会破坏
    // "回合中最后一条是 assistant" 的事件契约（后续 tool_finish 会失配）
    notifyChat(msg: string) {
      const last = this.messages[this.messages.length - 1];
      if (last && last.role === 'assistant' && last.execution_state === 'running') {
        this.messages.splice(this.messages.length - 1, 0, { role: 'notice', content: msg });
      } else {
        this.messages.push({ role: 'notice', content: msg });
      }
    },

    // 非对话类提示：模态提示框，用户点击"确定"才关闭。
    // 有待决确认框时先按"取消"安全收尾：直接覆盖会让其 promise 永久悬置。
    notifyModal(title: string, msg: string) {
      if (this.confirmModal.show && this.confirmModal.resolve) {
        this.confirmModal.resolve(false);
      }
      this.confirmModal = {
        show: true,
        title,
        sub: '',
        message: msg,
        confirmLabel: '确定',
        notice: true,
      };
    },

    askConfirm(title: string, sub: string, message: string, confirmLabel: string = '确定删除 ⏎'): Promise<boolean> {
      return new Promise((resolve) => {
        this.confirmModal = {
          show: true,
          title,
          sub,
          message,
          confirmLabel,
          resolve,
        };
      });
    },

    resolveConfirm(ok: boolean) {
      if (this.confirmModal.resolve) {
        this.confirmModal.resolve(ok);
      }
      this.confirmModal.show = false;
    },

    async initFromBackend() {
      // 无论初始状态拉取是否成功，都先挂好事件监听，保证后续事件不丢失
      window.onBridgeEvent = (event: BridgeEvent) => {
        this.handleBridgeEvent(event);
      };

      // 启动画面最少展示时长：后端太快时避免一闪而过
      const SPLASH_MIN_MS = 900;
      const splashStart = Date.now();
      const finishSplash = async () => {
        const remain = SPLASH_MIN_MS - (Date.now() - splashStart);
        if (remain > 0) await new Promise(r => setTimeout(r, remain));
        this.initialized = true;
      };

      const api = this._api();
      if (!api) {
        await finishSplash();
        return;
      }
      // 启动时按当前主题同步一次标题栏：main.ts 已按 URL #theme= 片段提前恢复
      // body 类（后端真值），以 body 类为准，避免浅色主题启动期标题栏黑几秒
      this.themeLight = document.body.classList.contains('light');
      api.set_titlebar_theme(!this.themeLight)?.catch(() => {});
      try {
        const init = await api.get_initial_state();
        this.models = init.models || [];
        this.providers = init.providers || [];
        this.activeModelId = init.active_model_id || '';
        this.activeModelName = init.active_model_name || '';
        this.secondaryModelId = init.secondary_model_id || '';
        this.secondaryModelRoles = init.secondary_model_roles || { vision: true, compression: true, subagent: true };
        this.permissionMode = init.permission_mode || 'confirm';
        this.sandboxEnabled = !!(init as any).allow_outside_workspace;
        this.reasoningEffort = (init as any).reasoning_effort || 'default';
        this.sessionGroups = init.sessions || [];
        this.skills = init.skills || [];
        this.mcps = init.mcps || [];
        this.plugins = (init as any).plugins || [];
        this.workdir = init.workdir || '';
        this.currentSessionName = (init as any).current_session || '';
        // 工作区标签以“当前会话归属”为准：GLOBAL 会话显示“对话”，
        // 不能直接用启动目录，否则会显示在工作区里、消息却发进自由对话
        const isGlobal = !!(init as any).is_global;
        this.currentWorkdirLabel = (!isGlobal && this.workdir)
          ? (this.workdir.split('\\').pop() || this.workdir)
          : '对话';
        // 启动时后端自动恢复最近一次对话：同步消息列表与标题
        this.messages = pairHistoryToolMessages((init as any).messages || []);
        // 批次D：窗口重载/崩溃重建期间终态事件已丢失，后端通过 active_sessions
        // 告知仍有活跃回合的会话名列表。据此恢复各会话 running 态与顶层标记，
        // 让运行中的会话正确显示运行中（含发送拦截/停止按钮）而不是看似空闲。
        const activeSessions: string[] = Array.isArray((init as any).active_sessions)
          ? (init as any).active_sessions.map(String).filter(Boolean)
          : [];
        this.runningSessions = activeSessions;
        this.queuedSessions = [];
        for (const name of activeSessions) {
          this._getSessionRuntime(name).running = true;
        }
        this.running = !!this.currentSessionName && activeSessions.includes(this.currentSessionName);
        this._captureCurrentRuntime();
        for (const g of this.sessionGroups) {
          const hit = (g.sessions || []).find((s: any) => s.name === this.currentSessionName);
          if (hit) {
            this.currentSessionDisplayName = hit.display_name || hit.name;
            break;
          }
        }
        // 恢复持久化的界面主题（默认深色）：body 类与标题栏一并同步
        this.themeLight = !!(init as any).theme_light;
        document.body.classList.toggle('light', this.themeLight);
        try { localStorage.setItem('ycode-theme-light', this.themeLight ? '1' : '0'); } catch {}
        api.set_titlebar_theme(!this.themeLight)?.catch(() => {});
        this.fileTree = (init as any).file_tree || [];
        this.tokenUsage = normalizeTokenUsage(init.token_usage);
        this.version = init.version || '';
        this.connected = true;
      } catch (err) {
        this.connected = false;
        this.notifyModal('连接失败', '后端连接失败：' + err);
      } finally {
        await finishSplash();
      }
    },

    async setModel(modelId: string) {
      // 竞态防护：整个操作锚定进入时的会话。await 期间用户可能已切到别的会话，
      // 响应回来必须写回原会话的 runtime，否则模型选择会错绑到切换后的会话
      const targetSession = this.currentSessionName;
      if (!targetSession) {
        // pending 草稿会话：尚未落库，后端 _current_session 指向上一个真实会话，
        // 此时调 set_model 会把模型静默绑到旧会话。只更新本地与草稿 runtime，
        // 首条消息发送时随 send_user_message 落库
        const model = this.models.find((m: ModelInfo) => m.id === modelId);
        this.activeModelId = modelId;
        this.activeModelName = (model && model.name) || modelId;
        const runtime = this._getSessionRuntime('');
        runtime.modelId = modelId;
        runtime.modelName = this.activeModelName;
        this.draftModelId = modelId;
        return;
      }
      const api = this._api();
      if (!api) return;
      // 版本号在请求发出前递增：switchSession 在途响应若携带旧模型快照，
      // 凭版本号变化跳过覆盖，保住用户刚做的选择
      this._sessionModelVersion[targetSession] = (this._sessionModelVersion[targetSession] || 0) + 1;
      try {
        const res = await api.set_model(modelId);
        if (res && res.ok) {
          const runtime = this._getSessionRuntime(targetSession);
          runtime.modelId = res.active_model_id || modelId;
          runtime.modelName = res.active_model_name || this.activeModelName;
          // 仅当用户仍停留在原会话时才更新顶层显示，避免覆盖切换后会话的状态栏
          if (this.currentSessionName === targetSession) {
            this.activeModelId = runtime.modelId!;
            this.activeModelName = runtime.modelName!;
            // 推理强度按模型保存：切换模型后同步该模型自己的档位
            this.reasoningEffort = res.reasoning_effort || 'default';
          }
          // 新会话或空会话切换模型不再弹提示/产生新页面：模型绑定静默完成，
          // 仅当该会话已有真实对话内容（user/assistant 消息）时才插入切换提示，
          // 否则 notice 会让空状态页消失，表现为切模型后页面跳转
          const hasDialog = runtime.messages.some(m => m.role === 'user' || m.role === 'assistant');
          if (hasDialog && this.currentSessionName === targetSession) {
            this.notifyChat('已切换活动模型: ' + runtime.modelName);
          }
        } else {
          this.notifyChat('切换模型失败: ' + (res && res.msg ? res.msg : '未知错误'));
        }
      } catch (err) {
        this.connected = false;
        this.notifyChat('切换模型失败: ' + err);
      }
    },

    async setReasoningEffort(level: ReasoningEffort) {
      const prev = this.reasoningEffort;
      this.reasoningEffort = level;
      const api = this._api();
      if (!api) return;
      try {
        const res = await api.set_reasoning_effort(level);
        if (res && res.ok) {
          this.reasoningEffort = res.reasoning_effort || level;
          this.notifyChat(res.msg || ('推理强度已调整: ' + this.reasoningEffort));
        } else {
          this.reasoningEffort = prev;
          this.notifyChat('推理强度调整失败: ' + (res && res.msg ? res.msg : '未知错误'));
        }
      } catch (err) {
        this.reasoningEffort = prev;
        this.connected = false;
        this.notifyChat('推理强度调整失败: ' + err);
      }
    },

    async setPermissionMode(mode: PermissionMode) {
      const prev = this.permissionMode;
      const api = this._api();
      if (!api) return;
      try {
        const res = await api.set_permission_mode(mode);
        if (res && res.ok) {
          this.permissionMode = res.permission_mode || mode;
          this.notifyChat('权限模式已调整: ' + formatPermMode(this.permissionMode));
        } else {
          this.permissionMode = prev;
          this.notifyChat('权限模式调整失败: ' + (res && res.msg ? res.msg : '未知错误'));
        }
      } catch (err) {
        this.permissionMode = prev;
        this.connected = false;
        this.notifyChat('权限模式调整失败: ' + err);
      }
    },

    // 切换沙盒开关（是否允许访问工作目录外文件），与 CLI /sandbox 同一配置
    async setSandbox(enabled: boolean) {
      const prev = this.sandboxEnabled;
      const api = this._api();
      if (!api) return;
      try {
        const res = await api.set_sandbox_enabled(enabled);
        if (res && res.ok) {
          this.sandboxEnabled = !!res.allow_outside_workspace;
          this.notifyChat(
            this.sandboxEnabled ? '沙盒已解除：允许访问工作目录外文件' : '沙盒已开启：仅限工作目录内'
          );
        } else {
          this.sandboxEnabled = prev;
          this.notifyChat('沙盒开关调整失败: ' + (res && res.msg ? res.msg : '未知错误'));
        }
      } catch (err) {
        this.sandboxEnabled = prev;
        this.connected = false;
        this.notifyChat('沙盒开关调整失败: ' + err);
      }
    },

    // 选择/清除副模型（传空字符串清除），对齐 CLI 的 set_secondary
    async setSecondaryModel(modelId: string) {
      const api = this._api();
      if (!api) return;
      try {
        const res = await api.set_secondary_model(modelId);
        if (res && res.ok) {
          this.secondaryModelId = modelId || '';
          this.notifyModal('副模型设置', res.msg || ('副模型已更新: ' + (this.secondaryModelId || '未设置')));
        } else {
          this.notifyModal('副模型设置', '副模型设置失败: ' + (res && res.msg ? res.msg : '未知错误'));
        }
      } catch (err) {
        this.connected = false;
        this.notifyModal('副模型设置', '副模型设置失败: ' + err);
      }
    },

    // 设置副模型用途（识图 / 压缩 / 子Agent）。乐观更新，失败回滚。
    async setSecondaryRoles(roles: { vision: boolean; compression: boolean; subagent: boolean }) {
      const prev = this.secondaryModelRoles;
      this.secondaryModelRoles = roles;
      const api = this._api();
      if (!api) return;
      try {
        const res = await api.set_secondary_roles(roles);
        if (res && res.ok) {
          this.notifyModal('副模型设置', res.msg || '副模型用途已更新');
        } else {
          this.secondaryModelRoles = prev;
          this.notifyModal('副模型设置', '副模型用途设置失败: ' + (res && res.msg ? res.msg : '未知错误'));
        }
      } catch (err) {
        this.secondaryModelRoles = prev;
        this.connected = false;
        this.notifyModal('副模型设置', '副模型用途设置失败: ' + err);
      }
    },

    async switchSession(sessionName: string, displayName?: string, targetWorkdir?: string) {
      // 切换到已有会话时丢弃未发送的空白 pending 会话
      this._captureCurrentRuntime();
      this.pendingWorkdir = null;
      this.currentSessionName = sessionName;
      if (displayName) this.currentSessionDisplayName = displayName;
      this.permRequest.show = this.permRequest.sessionName === sessionName;
      // 进入该会话即视为已读，清除其状态提醒点
      this._clearSessionStatusDot(sessionName);
      // 优先恢复内存中的实时 runtime；运行中的会话不能被旧数据库快照覆盖
      const runtime = this._getSessionRuntime(sessionName);
      this._bindSessionRuntime(sessionName);

      const api = this._api();
      if (!api) return;
      // 模型版本快照：响应在途期间用户若又切了模型（setModel 递增版本），
      // 后端响应携带的是切换前的旧模型快照，不再覆盖本地选择
      const modelVersion = this._sessionModelVersion[sessionName] || 0;
      try {
      const res = await api.switch_session(sessionName, targetWorkdir);
        // 竞态防护：快速连点不同会话时响应可能乱序返回，过期响应直接丢弃，
        // 避免高亮停在 B 会话却渲染出 A 会话的内容
        if (this.currentSessionName !== sessionName) return;
        if (!runtime.running && runtime.messages.length === 0) {
          runtime.messages = pairHistoryToolMessages(res.messages || []);
          this._bindSessionRuntime(sessionName);
        }

        // 多会话并行：切到正在运行回合的会话时，历史最后一条往往不是
        // assistant（本轮 assistant 消息尚未落库，历史停留在 user 或
        // 上一轮 completed）。此时该回合后续的 tool_start/tool_finish/
        // diff_ready 需要挂在活动助手消息上，缺失会触发桥接契约错误，
        // 因此按 running 状态补一条占位消息，让后续回合级事件有处可挂。
        if (this.runningSessions.includes(sessionName)) {
          const last = this.messages[this.messages.length - 1];
          const needsPlaceholder = !last ||
            last.role !== 'assistant' ||
            last.execution_state !== 'running';
          if (needsPlaceholder) {
            this.messages.push({
              role: 'assistant',
              content: '',
              reasoning_content: '',
              tool_calls: [],
              legacy_diffs: [],
              execution_state: 'running',
            });
          }
        }

        if (res.is_global || res.workdir === 'GLOBAL') {
          this.currentWorkdirLabel = '对话';
          this.workdir = '';
          this.fileTree = [];
          this.fileTreeExpanded = [];
        } else if (res.workdir) {
          this.workdir = res.workdir;
          this.currentWorkdirLabel = res.workdir.split('\\').pop() || res.workdir;
          // 文件树不再随切换响应返回（后端同步扫描大目录要数秒，拖慢切换）：
          // 先清空旧树秒开对话，树在后台异步加载，过期结果丢弃
          this.fileTree = [];
          this.fileTreeExpanded = [];
          this.loadFileTreeInBackground(sessionName, res.workdir);
        }

        if (res.sessions) this.sessionGroups = res.sessions;

        // 会话级模型隔离：切到已有会话时恢复该会话绑定的模型，
        // 避免 A 会话切到 B 会话后 A 的模型被全局覆盖。
        // 版本号变化 = 在途期间用户又切了模型，跳过本次恢复（本地已是新值）
        if (res.active_model_id && (this._sessionModelVersion[sessionName] || 0) === modelVersion) {
          this.activeModelId = res.active_model_id;
          this.activeModelName = res.active_model_name || this.activeModelName;
          this.reasoningEffort = res.reasoning_effort || 'default';
          const runtime2 = this._getSessionRuntime(sessionName);
          runtime2.modelId = this.activeModelId;
          runtime2.modelName = this.activeModelName;
        }
      } catch (err) {
        this.connected = false;
        this.notifyChat('切换会话失败: ' + err);
      }
    },

    async loadFileTreeInBackground(sessionName: string, workdir: string) {
      // 会话切换后的文件树后台加载通道（与 switchSession 分离，切换秒开）
      const api = this._api();
      if (!api) return;
      const seq = ++this.fileTreeReqSeq;
      try {
        const res = await api.get_file_tree(workdir);
        // 竞态防护：期间又切了别的会话/项目，本结果直接丢弃
        if (seq !== this.fileTreeReqSeq) return;
        if (this.currentSessionName !== sessionName) return;
        if (this.workdir !== workdir) return;
        if (res && res.ok && res.file_tree) this.fileTree = res.file_tree;
      } catch { /* 后台加载失败静默：树留空，不影响对话 */ }
    },

    // 按完整路径在已加载的文件树中定位节点（FileTreeNode 用 '\\' 拼路径，兼容两种分隔符）
    _findFileNode(fullPath: string): FileNode | null {
      const norm = (s: string) => s.replace(/\//g, '\\').replace(/\\+$/, '');
      const base = norm(this.workdir);
      const target = norm(fullPath);
      if (!base || !target.startsWith(base + '\\')) return null;
      const segs = target.slice(base.length + 1).split('\\').filter(Boolean);
      let level: FileNode[] = this.fileTree;
      let found: FileNode | null = null;
      for (const seg of segs) {
        found = level.find(n => n.n === seg) || null;
        if (!found) return null;
        level = found.children || [];
      }
      return found;
    },

    // 刷新文件树：重拉整棵树后，按展开记录逐层刷新子节点并恢复展开状态；
    // 已被删除的目录从展开记录中清除。手动按钮与窗口聚焦/页签激活的自动刷新共用。
    async refreshFileTree() {
      const api = this._api();
      if (!api || !this.workdir || this.fileTreeRefreshing) return;
      this.fileTreeRefreshing = true;
      const seq = ++this.fileTreeReqSeq;
      try {
        const res = await api.get_file_tree(this.workdir);
        if (seq !== this.fileTreeReqSeq) return; // 期间切换了会话/项目，丢弃过期结果
        if (!res || !res.ok || !res.file_tree) return;
        this.fileTree = res.file_tree;
        const expanded = [...this.fileTreeExpanded].sort((a, b) => a.length - b.length);
        const stillExists: string[] = [];
        for (const p of expanded) {
          const node = this._findFileNode(p);
          if (!node) continue; // 目录已删除
          stillExists.push(p);
          try {
            const r = await api.get_dir_children(p);
            node.children = (r && r.children) || [];
          } catch {
            node.children = [];
          }
        }
        this.fileTreeExpanded = stillExists;
      } catch { /* 刷新失败静默：保留旧树 */ }
      finally { this.fileTreeRefreshing = false; }
    },

    async createGlobalSession() {
      // 懒创建：只切换到空白自由对话状态，首条消息发出时才真正落库
      this._captureCurrentRuntime();
      this.pendingWorkdir = 'GLOBAL';
      this.currentSessionName = '';
      this.currentSessionDisplayName = '新对话';
      this._bindSessionRuntime('');
      this.messages = [];
      this.currentStreamingText = '';
      this.currentThinkingText = '';
      this.subagentTasks = [];
      this.planData = { title: '', items: [], progress: 0 };
      this.tokenUsage = emptyTokenUsage();
      this.running = false;
      this._captureRuntime('');
      this.currentWorkdirLabel = '对话';
      this.workdir = '';
      this.fileTree = [];
      this.fileTreeExpanded = [];
    },

    // 顶栏“新会话”按钮：跟随当前上下文——在项目中就在该项目新建，
    // 在自由对话中就新建自由对话
    async createSessionSmart() {
      if (this.workdir && this.currentWorkdirLabel !== '对话') {
        await this.createSessionInGroup(this.workdir);
      } else {
        await this.createGlobalSession();
      }
    },

    async createSessionInGroup(targetWorkdir: string) {
      // 秒开：前端已有工作区上下文（新会话首条消息发出时才懒落库），
      // 不再同步等后端 get_workspace_state 扫文件树（大目录要数秒，
      // 表现为“点新会话特别慢”）。
      const treeMatches = this.workdir === targetWorkdir;
      this._captureCurrentRuntime();
      this.pendingWorkdir = targetWorkdir;
      this.currentSessionName = '';
      this.currentSessionDisplayName = '新会话';
      this._bindSessionRuntime('');
      this.messages = [];
      this.currentStreamingText = '';
      this.currentThinkingText = '';
      this.subagentTasks = [];
      this.planData = { title: '', items: [], progress: 0 };
      this.tokenUsage = emptyTokenUsage();
      this.running = false;
      this._captureRuntime('');
      this.workdir = targetWorkdir;
      this.currentWorkdirLabel = targetWorkdir.split('\\').pop() || targetWorkdir;
      // 文件树：当前就在该工作区则直接保留已加载的树；
      // 切到别的项目分组才清空并后台异步加载（过期结果有序号防护）
      if (!treeMatches) {
        this.fileTree = [];
        this.fileTreeExpanded = [];
        this.loadFileTreeInBackground('', targetWorkdir);
      }
    },

    async exitWorkspaceToGlobal() {
      // 退出当前项目工作区：优先回到“对话”分组最近的会话，没有则进空白自由对话
      const globalGroup = this.sessionGroups.find(g => g.is_global || g.folder_path === 'GLOBAL');
      const latest = globalGroup && globalGroup.sessions && globalGroup.sessions[0];
      if (latest) {
        await this.switchSession(latest.name, latest.display_name || latest.name, 'GLOBAL');
      } else {
        await this.createGlobalSession();
      }
    },

    async switchToWorkspaceGroup(folderPath: string) {
      // 快捷切换到指定项目分组：进入该分组最近的会话
      const group = this.sessionGroups.find(g => g.folder_path === folderPath);
      const latest = group && group.sessions && group.sessions[0];
      if (latest) {
        await this.switchSession(latest.name, latest.display_name || latest.name, folderPath);
      } else {
        await this.createSessionInGroup(folderPath);
      }
    },

    async deleteFolderGroup(workdir: string, folderTitle: string) {
      // 确认动作已内联在分组头的红色小按钮上，不再弹大确认窗；
      // 后端 delete_folder_group 会连同组内对话记录一起删除（不动磁盘文件）
      const api = this._api();
      if (!api) return;
      try {
        const res = await api.delete_folder_group(workdir);
        if (res && res.success) {
          this.sessionGroups = res.sessions;
          this.notifyModal('项目分组', `已移出项目分组: ${folderTitle} (磁盘文件完好)`);
        } else {
          this.notifyModal('项目分组', '移除项目分组失败: ' + (res && res.msg ? res.msg : '未知错误'));
        }
      } catch (err) {
        this.connected = false;
        this.notifyModal('项目分组', '移除项目分组失败: ' + err);
      }
    },

    async deleteSingleSession(sessionName: string) {
      // 确认动作已内联在侧栏的红色小按钮上，不再弹大确认窗
      const api = this._api();
      if (!api) return;
      try {
        const res = await api.delete_session(sessionName);
        if (res && res.success) {
          this.sessionGroups = res.sessions;
          // T11 修复：同步清理前端运行态中该会话的残留项。
          // 否则 runningSessions 中已删除会话名永久存在（终态事件不会再到来），
          // sessionRuntimeByName 中的孤儿 runtime 也会随会话名复用而串数据。
          // 验证思路：删除正在运行的会话后 runningSessions/sessionRuntimeByName
          // 中均不应再有该会话条目。
          this.runningSessions = this.runningSessions.filter(s => s !== sessionName);
          this.queuedSessions = this.queuedSessions.filter(s => s !== sessionName);
          delete this.sessionRuntimeByName[sessionName];
          if (sessionName === this.currentSessionName && res.current_session) {
            await this.switchSession(res.current_session, '新会话');
          }
          // 删除成功静默完成：侧栏列表已随 res.sessions 刷新，无需再弹成功提示
        } else {
          this.notifyModal('会话管理', '删除会话失败: ' + (res && res.msg ? res.msg : '未知错误'));
        }
      } catch (err) {
        this.connected = false;
        this.notifyModal('会话管理', '删除会话失败: ' + err);
      }
    },

    async openWorkspaceFolder() {
      const api = this._api();
      if (!api) return;
      try {
        const res = await api.select_workspace_folder();
        if (res && res.ok !== false && res.workdir) {
          this.workdir = res.workdir;
          this.currentWorkdirLabel = res.workdir.split('\\').pop() || res.workdir;
          if (res.file_tree) { this.fileTree = res.file_tree; this.fileTreeExpanded = []; }
          if (res.sessions) this.sessionGroups = res.sessions;
          if (res.skills) this.skills = res.skills;
          this.notifyModal('项目分组', '已成功引入项目文件夹: ' + res.workdir);
        }
        // res.ok === false 代表用户取消了文件夹选择，静默忽略即可
      } catch (err) {
        this.connected = false;
        this.notifyModal('项目分组', '选择文件夹失败: ' + err);
      }
    },

    async stopCurrentTurn() {
      const sessionName = this.currentSessionName;
      const api = this._api();
      if (!api) return;
      // T12 修复：若后端未回发 turn_cancelled（进程异常退出/事件丢失），
      // runningSessions 将永久残留该会话，sendMessage 的拦截判定会让用户永远无法再发送。
      // 加 10s 本地兜底定时器：超时后强制清除该会话运行态；
      // 若 turn_cancelled 正常到达，runningSessions 已不含该会话，定时器为无操作。
      // 验证思路：mock stop_current_turn 成功但不派发 turn_cancelled，
      // 10s 后 runningSessions 应不再包含该会话且发送恢复可用。
      const stopFallbackTimer = setTimeout(() => {
        if (this.runningSessions.includes(sessionName)) {
          console.warn(`[T12] stopCurrentTurn 后 10s 未收到 turn_cancelled，本地兜底解锁会话: ${sessionName}`);
          this._setSessionRunning(sessionName, false);
        }
      }, 10000);
      try {
        const res = await api.stop_current_turn(sessionName);
        if (!res || !res.ok) {
          clearTimeout(stopFallbackTimer);
          this._getSessionRuntime(sessionName).messages.push({ role: 'error', content: '中断请求未成功送达' });
        } else {
          this.notifyChat('已中断当前回合');
        }
      } catch (err) {
        clearTimeout(stopFallbackTimer);
        this.connected = false;
        this._getSessionRuntime(sessionName).messages.push({ role: 'error', content: '中断请求失败: ' + err });
      }
    },

    async sendMessage(text: string, attachments?: string[]) {
      // 生成中禁止再次发送（无论调用方是否已自行判断，均在此兜底拦截）
      const attachList = (attachments || []).filter(p => p && p.trim());
      if (!text.trim() && !attachList.length) return;
      // M30 修复：拦截必须用户可见——调用方已清空输入框，
      // 静默 return 会让用户以为消息已发出（实际被吞）
      if (this.runningSessions.includes(this.currentSessionName)) {
        this.notifyChat('当前会话正在生成中，请先停止或稍后再发送');
        return;
      }
      const api = window.pywebview && window.pywebview.api;
      if (!api) {
        // 本地无后端离线模拟，方便纯前端调试
        this.messages.push({ role: 'user', content: text.trim() });
        this.messages.push({ role: 'error', content: '无后端连接，仅处于开发离线预览视图' });
        this.running = false;
        return;
      }
      let sessionName = this.currentSessionName;
      let pendingDir: string | undefined;
      // Low 修复：pending 懒创建会话发送失败时后端并未落库，停留在
      // "幽灵会话"会让后续操作都指向不存在的会话。回退到 pending 草稿状态
      // （运行态迁回草稿 key，刚写入的错误消息仍随绑定可见），用户修正后可重发。
      const revertGhostSession = () => {
        if (!pendingDir || this.currentSessionName !== sessionName) return;
        this.pendingWorkdir = pendingDir;
        const draftKey = this._pendingRuntimeKey();
        if (this.sessionRuntimeByName[sessionName]) {
          this.sessionRuntimeByName[draftKey] = this.sessionRuntimeByName[sessionName];
          delete this.sessionRuntimeByName[sessionName];
        }
        this.currentSessionName = '';
        this.currentSessionDisplayName = pendingDir === 'GLOBAL' ? '新对话' : '新会话';
        this._bindSessionRuntime('');
      };
      try {
        // 懒创建：pending 状态下首条消息才真正创建会话（名称匹配 session_[a-f0-9]{6}
        // 才能被后端自动重命名为消息前几个字）
        if (this.pendingWorkdir !== null) {
          const draftKey = this._runtimeKey();
          pendingDir = this.pendingWorkdir;
          this.pendingWorkdir = null;
          // 6 位随机名需避开已有会话：撞名会被后端 INSERT OR IGNORE 静默
          // 并入已存在的会话，表现为两个会话内容互串
          const existingNames = new Set(
            this.sessionGroups.flatMap(g => (g.sessions || []).map(s => s.name)),
          );
          do {
            sessionName = 'session_' + Math.random().toString(16).slice(2, 8).padEnd(6, '0');
          } while (existingNames.has(sessionName));
          this.sessionRuntimeByName[sessionName] = this.sessionRuntimeByName[draftKey] || createSessionRuntime();
          delete this.sessionRuntimeByName[draftKey];
          this.currentSessionName = sessionName;
        }
        this._setSessionRunning(sessionName, true);
        this._bindSessionRuntime(sessionName);
        // 乐观渲染：串行排队时后端 turn_start 要等拿到 _turn_workdir_lock 才发，
        // 期间新会话若无任何气泡会显示为一片空白（看似"没发送"）。
        // 发送成功后立即渲染用户消息 + running 占位；turn_start 到达时去重占位，
        // 用户消息已有则不重复添加。
        const trimmed = text.trim();
        if (trimmed) {
          const runtimeMsgs = this._getSessionRuntime(sessionName).messages;
          const lastMsg = runtimeMsgs[runtimeMsgs.length - 1];
          if (!(lastMsg && lastMsg.role === 'user' && lastMsg.content === trimmed)) {
            runtimeMsgs.push({ role: 'user', content: trimmed });
          }
          runtimeMsgs.push({
            role: 'assistant',
            content: '',
            reasoning_content: '',
            tool_calls: [],
            legacy_diffs: [],
            execution_state: 'running',
          });
          if (this.currentSessionName === sessionName) {
            this._bindSessionRuntime(sessionName);
          }
        }
        const res = await api.send_user_message(
          text.trim(),
          sessionName,
          attachList,
          pendingDir,
          pendingDir ? (this.draftModelId || undefined) : undefined,
        );
        if (res && res.ok) {
          // 草稿模型已随后端落库（或本就不存在），不再保留待重发状态
          this.draftModelId = '';
          if (pendingDir) {
          // 让新会话立即出现在左侧列表（自动重命名事件随后会再刷新一次）
          this.refreshSessions();
          }
        }
        if (!res || !res.ok) {
          // #13 修复：失败分支不再无条件 _bindSessionRuntime(sessionName)。
          // await 期间用户可能已切换到其他会话，重新绑定会把 store 顶层
          // 引用切回失败会话的运行态，覆盖用户当前正在查看的会话。
          // 改为：错误消息只写入该会话的 runtime（后台静默记录），
          // 仅当用户仍停留在该会话（currentSessionName === sessionName）
          // 时才重新绑定以保持顶层引用与 runtime 一致。
          // 验证思路：发送后立即切换会话并模拟失败返回，当前会话消息不被覆盖。
          this._setSessionRunning(sessionName, false);
          this._getSessionRuntime(sessionName).messages.push({ role: 'error', content: '消息发送失败: ' + (res && res.msg ? res.msg : '未知错误') });
          if (pendingDir) {
            revertGhostSession();
          } else if (this.currentSessionName === sessionName) {
            this._bindSessionRuntime(sessionName);
          }
        }
        // 成功时不主动改 running，由后续 turn_start/turn_finish/turn_cancelled 等事件驱动
      } catch (err) {
        // #13 同上：catch 分支也只向目标会话 runtime 写入错误，
        // 仅用户仍在该会话时才恢复绑定
        this._setSessionRunning(sessionName, false);
        this.connected = false;
        this._getSessionRuntime(sessionName).messages.push({ role: 'error', content: '消息发送失败: ' + err });
        if (pendingDir) {
          revertGhostSession();
        } else if (this.currentSessionName === sessionName) {
          this._bindSessionRuntime(sessionName);
        }
      }
    },

    async refreshSessions() {
      const api = this._api();
      if (!api) return;
      // M31 修复：序号防护，并发刷新时过期快照直接丢弃
      const seq = ++this.sessionsReqSeq;
      try {
        const sessions = await api.get_sessions();
        if (seq !== this.sessionsReqSeq) return;
        if (sessions) this.sessionGroups = sessions;
      } catch (err) {
        // 静默失败：下一次切换会话/新建会话时会重新拉取
        console.error('刷新会话列表失败:', err);
      }
    },

    async refreshTokenUsage() {
      const api = window.pywebview && window.pywebview.api;
      if (!api) return;
      // M31 修复：序号防护，过期响应不覆盖新快照
      const seq = ++this.tokenUsageReqSeq;
      try {
        const usage = await api.get_token_usage();
        if (seq !== this.tokenUsageReqSeq) return;
        if (usage) this.tokenUsage = usage;
      } catch (err) {
        // 静默失败：token_usage 推送事件通常会补上，不打断用户
        console.error('刷新 Token 用量失败:', err);
      }
    },

    handleBridgeEvent(event: BridgeEvent) {
      const type = event.type;
      const data = event.data || {};
      // 只要有事件推送过来，说明桥接链路是通的
      this.connected = true;

      const currentRuntimeKey = this._runtimeKey();
      // H2 修复：终态事件缺失 session_name 时，不能落到当前显示会话
      // （可能覆盖无关会话的运行态，且 runningSessions 残留导致发送被永久拦截）。
      // 兜底策略：仅当 runningSessions 恰有唯一会话时采纳该会话（此时归属无歧义），
      // 并 console.warn 记录契约违例；无法推断时 eventSession 留空，后续 reducer
      // 仅在 runtime 已绑定到当前显示会话时更新顶层引用，不写入其他会话运行态。
      // 验证思路：模拟 turn_finish 无 session_name 且 runningSessions=[s]，
      // 应移除 s 并打印 warn；runningSessions 有多个时应保持不动。
      let eventSession = String(data.session_name || '');
      const isTerminalType = type === 'turn_finish' || type === 'turn_cancelled' || type === 'turn_error' || type === 'turn_idle';
      if (!eventSession && isTerminalType) {
        if (this.runningSessions.length === 1) {
          eventSession = this.runningSessions[0];
          console.warn(`[H2] 终态事件 ${type} 缺少 session_name，已从唯一运行会话推断为: ${eventSession}`);
        } else {
          console.warn(`[H2] 终态事件 ${type} 缺少 session_name 且无法唯一推断（runningSessions=${JSON.stringify(this.runningSessions)}），本次不更新任何会话运行态`);
        }
      } else if (!eventSession) {
        eventSession = String(this.currentSessionName || '');
      }
      const targetRuntimeKey = eventSession || currentRuntimeKey;
      const switchedRuntime = targetRuntimeKey !== currentRuntimeKey;
      if (switchedRuntime) this._bindSessionRuntime(targetRuntimeKey);

      try {

      // 多会话并行：终态事件从 runningSessions 移除对应会话
      if (isTerminalType) {
        const finSession = eventSession;
        if (finSession) {
          this.runningSessions = this.runningSessions.filter(s => s !== finSession);
          // 批次D：终态同时清除排队标记（排队中被停止也会走到这里）
          this.queuedSessions = this.queuedSessions.filter(s => s !== finSession);
        }
        // 当前 reducer 已绑定到目标 runtime，终态只更新目标会话。
        this.running = false;
        this.turnStartedAt = 0;
        this.subagentTasks = [];
        // M31 修复：会话列表刷新统一在各终态分支里只做一次，
        // 不再在此处与 turn_finish/turn_cancelled 分支重复触发
      }

      if (type === 'turn_start') {
        const turnSession = String(data.session_name || '');
        // 记录活跃会话
        if (turnSession && !this.runningSessions.includes(turnSession)) {
          this.runningSessions.push(turnSession);
        }
        // 批次D：拿到锁开始执行，清除排队标记
        if (turnSession) {
          this.queuedSessions = this.queuedSessions.filter(s => s !== turnSession);
        }
        this.running = true;
        // 记录本轮回合开始时间戳，供"已工作 xx秒"计时（随会话切换恢复，不重置）
        if (!this.turnStartedAt) this.turnStartedAt = Date.now();
        this.currentStreamingText = '';
        this.currentThinkingText = '';
        // 内部回合（如后台任务自动汇总）：展示友好的系统提示文案，
        // 不把内部指令原文渲染成用户气泡
        const turnUserText = data.display_text || data.user_text || '';
        // 去重：sendMessage 已乐观渲染用户消息 + running 占位（串行排队时
        // turn_start 滞后，避免新会话长时间空白）。若该用户消息已存在则不重复加。
        const alreadyHasUser = turnUserText && this.messages.some(
          (m: ChatMessage) => m.role === 'user' && m.content === turnUserText,
        );
        if (!alreadyHasUser) {
          this.messages.push({ role: 'user', content: turnUserText });
        }
        // 占位 assistant：仅当末尾不是空 running 占位时才补（乐观渲染已加则复用）
        const tail = this.messages[this.messages.length - 1];
        const tailIsRunningPlaceholder = tail && tail.role === 'assistant'
          && !tail.content && !tail.reasoning_content
          && (!tail.tool_calls || tail.tool_calls.length === 0);
        if (!tailIsRunningPlaceholder) {
          this.messages.push({
            role: 'assistant',
            content: '',
            reasoning_content: '',
            tool_calls: [],
            legacy_diffs: [],
            execution_state: 'running',
          });
        }
        if (data.internal === 'bg_summary') {
          this.notifyChat('后台子代理任务已全部结束，正在自动汇总结果');
        }
      }

      // 批次D：回合已提交但在等 _turn_workdir_lock（超过 1s 未拿到锁时后端推送）。
      // 排队算运行中：加入 runningSessions 以拦截发送/显示停止按钮，
      // 同时记录 queuedSessions 让 UI 能区分"排队等其他会话"与"正在执行"。
      // 拿到锁后后端照常发 turn_start，排队标记在那里清除。
      if (type === 'turn_queued') {
        const queuedSession = String(data.session_name || eventSession || '');
        if (queuedSession) {
          if (!this.queuedSessions.includes(queuedSession)) {
            this.queuedSessions.push(queuedSession);
          }
          if (!this.runningSessions.includes(queuedSession)) {
            this.runningSessions.push(queuedSession);
          }
        }
      }

      if (type === 'round_started') {
        // 每个 LLM 轮次一条 assistant 消息：与历史重放的分段结构对齐（分段思考）。
        // 若当前 assistant 气泡还是空的（回合第一轮），幂等跳过。
        const last = this.messages[this.messages.length - 1];
        if (last && last.role === 'assistant' &&
            ((last.reasoning_content && last.reasoning_content.trim()) ||
             (last.content && last.content.trim()) ||
             (last.tool_calls && last.tool_calls.length))) {
          last.execution_state = 'completed';
          this.currentStreamingText = '';
          this.currentThinkingText = '';
          this.messages.push({
            role: 'assistant',
            content: '',
            reasoning_content: '',
            tool_calls: [],
            legacy_diffs: [],
            execution_state: 'running',
          });
        }
      } else if (type === 'thinking_delta') {
        // 后端约 60ms 批量推送，data.text 可能包含多字符/多行，直接拼接即可
        this.currentThinkingText += data.text || '';
        const last = this.messages[this.messages.length - 1];
        if (last && last.role === 'assistant') {
          last.reasoning_content = this.currentThinkingText;
        }
      } else if (type === 'token_delta') {
        this.currentStreamingText += data.text || '';
        const last = this.messages[this.messages.length - 1];
        if (last && last.role === 'assistant') {
          last.content = this.currentStreamingText;
        }
      } else if (type === 'tool_start') {
        const d = data as ToolStartEventData;
        const msg = this.activeAssistantOrThrow();
        const calls = msg.tool_calls ?? (msg.tool_calls = []);
        const id = String(d.id || '');
        const display = normalizeToolDisplay(d.display);
        const idx = id ? calls.findIndex(c => c.id === id) : -1;
        if (idx >= 0) {
          // 同一调用重复推送：更新元信息，但保留首次插入时记录的 started_at_ms
          calls[idx] = {
            ...calls[idx],
            name: String(d.name || calls[idx].name),
            label: String(d.label || calls[idx].label),
            ...(display ? { display } : {}),
          };
        } else if (id) {
          calls.push({
            id,
            name: String(d.name || 'tool'),
            label: String(d.label || d.name || 'tool'),
            started_at_ms: Date.now(),
            ...(display ? { display } : {}),
          });
        } else {
          // 空 ID 违反后端契约：分配仅用于 UI 的占位 key，并强制归类为 other，
          // 避免不同空 ID 调用被行构建误合并
          console.error('tool_start 事件缺少 id，已分配 UI 占位 key:', d);
          calls.push({
            id: `contract-error-${calls.length + 1}`,
            name: String(d.name || 'tool'),
            label: String(d.label || d.name || 'tool'),
            started_at_ms: Date.now(),
            display: {
              ...(display || {
                action: '', target: '', command: '', metrics: {}, error_summary: '', is_skill: false,
              }),
              category: 'other',
            },
          });
        }
      } else if (type === 'tool_finish') {
        const d = data as ToolFinishEventData;
        const msg = this.activeAssistantOrThrow();
        const calls = msg.tool_calls ?? (msg.tool_calls = []);
        const display = normalizeToolDisplay(d.display);
        const result: ToolExecutionResult = {
          ok: !!d.ok,
          summary: String(d.summary || ''),
          result: String(d.result || d.error || ''),
          ...(d.duration_ms === undefined || d.duration_ms === null
            ? {}
            : { duration_ms: Number(d.duration_ms) }),
          ...(display ? { display } : {}),
        };
        const id = String(d.id || '');
        const idx = id ? calls.findIndex(c => c.id === id) : -1;
        if (idx >= 0) {
          calls[idx] = {
            ...calls[idx],
            name: String(d.name || calls[idx].name),
            label: String(d.label || calls[idx].label),
            result,
          };
        } else {
          // 没有匹配的 tool_start（后端契约异常）：显式创建孤儿完成记录，
          // 让结果仍可追溯而不是静默丢失
          console.error('tool_finish 事件没有匹配的 tool_start，已创建孤儿记录:', d);
          calls.push({
            id: id || `orphan-${calls.length + 1}`,
            name: String(d.name || 'tool'),
            label: String(d.label || d.name || 'tool'),
            result,
          });
        }
      } else if (type === 'plan_updated') {
        if (data.items) {
          const items = data.items.map((it: any) => ({
            id: it.id || String(Math.random()),
            // 后端计划项的正文字段是 subject（见 core/plan.py snapshot）
            text: it.subject || it.text || it.title || '',
            status: it.status === 'completed' || it.status === 'done' || it.status === 'x' ? 'done' : (it.status === 'in_progress' || it.status === '/' ? 'in_progress' : 'todo'),
          }));
          const doneCount = items.filter((i: any) => i.status === 'done').length;
          const progress = items.length > 0 ? Math.round((doneCount / items.length) * 100) : 0;
          this.planData = {
            title: data.title || '当前任务计划',
            items,
            progress,
          };
          this._captureRuntime(targetRuntimeKey);
        }
      } else if (type === 'user_message_images') {
        // 后端解析附件后补发的图片：挂到当前会话最后一条用户气泡上
        const imgs = Array.isArray(data.images)
          ? data.images.map((u: any) => String(u)).filter(Boolean)
          : [];
        if (imgs.length) {
          for (let i = this.messages.length - 1; i >= 0; i--) {
            if (this.messages[i].role === 'user') {
              this.messages[i].images = imgs;
              break;
            }
          }
        }
      } else if (type === 'user_attachment_notes') {
        const notes = Array.isArray(data.notes)
          ? data.notes.map((note: any) => String(note)).filter(Boolean)
          : [];
        if (!notes.length) return;
        for (let i = this.messages.length - 1; i >= 0; i--) {
          if (this.messages[i].role === 'user') {
            this.messages[i].attachment_notes = notes;
            return;
          }
        }
        throw new Error('桥接契约错误：附件提示到达时不存在用户消息');
      } else if (type === 'permission_request') {
        this.permRequest = {
          show: true,
          sessionName: eventSession,
          reqId: data.request_id,
          summary: String(data.summary || data.function_name || ''),
          source: data.source || '',
          allowAlways: !!data.allow_always,
          options: Array.isArray(data.options) ? data.options.map(String) : [],
          functionName: String(data.function_name || ''),
        };
        this.permRequest.show = this.permRequest.sessionName === this.currentSessionName;
        // 非当前窗口等待用户确认 → 黄点
        this._setSessionStatusDot(eventSession, 'yellow');
      } else if (type === 'permission_dismiss') {
        // 后端权限队列已清空（请求被超时拒绝/取消）：收起内联权限卡片，避免死请求残留。
        // 必须整体重置：仅 show=false 会残留 reqId/sessionName，
        // switchSession 重显逻辑会把已失效的卡片复活，点击后报“权限响应发送失败”
        this.permRequest = { show: false, sessionName: '', reqId: '', summary: '', source: '', allowAlways: false, options: [], functionName: '' };
      } else if (type === 'notice') {
        // 后端桥事件 notice：与当前会话相关，写入对话流
        if (data.message) this.notifyChat(String(data.message));
      } else if (type === 'session_renamed') {
        if (this.currentSessionName === data.old) {
          this.currentSessionName = data.new;
          this.currentSessionDisplayName = data.new;
        }
        // 运行中回合的会话名同步迁移：否则 runningSessions 残留旧名，
        // 后续 turn_finish(新名) 无法正确移除，会导致 running 状态卡死
        if (data.old && data.new) {
          this.runningSessions = this.runningSessions.map(s => (s === data.old ? data.new : s));
          // 排队标记同样随重命名迁移
          this.queuedSessions = this.queuedSessions.map(s => (s === data.old ? data.new : s));
          // 状态提醒点随重命名迁移 key，避免旧 key 残留/丢失
          if (data.old in this.sessionStatusDots) {
            this.sessionStatusDots[data.new] = this.sessionStatusDots[data.old];
            delete this.sessionStatusDots[data.old];
          }
          if (this.permRequest.sessionName === data.old) {
            this.permRequest.sessionName = data.new;
          }
        }
        this.sessionGroups.forEach(g => {
          (g.sessions || []).forEach(s => {
            if (s.name === data.old) {
              s.name = data.new;
              s.display_name = data.new;
            }
          });
        });
      } else if (type === 'turn_finish' || type === 'turn_idle') {
        this.running = false;
        // turn_finish 只在仍处于 running 时置为 completed；已取消/出错的状态不被覆盖
        const last = this.messages[this.messages.length - 1];
        if (last && last.role === 'assistant') {
          last.execution_state = reduceExecutionState(last.execution_state, 'turn_finish');
        }
        this.currentStreamingText = '';
        this.currentThinkingText = '';
        this.subagentTasks = [];
        this.refreshTokenUsage();
        // 对话结束后消息数已变化，刷新侧栏会话列表（否则计数停留在启动快照）
        this.refreshSessions();
        // 非当前窗口执行完成 → 绿点
        this._setSessionStatusDot(eventSession, 'green');
      } else if (type === 'turn_cancelled') {
        this.running = false;
        const last = this.messages[this.messages.length - 1];
        if (last && last.role === 'assistant') {
          last.cancelled = true;
          last.execution_state = reduceExecutionState(last.execution_state, 'turn_cancelled');
        }
        this.currentStreamingText = '';
        this.currentThinkingText = '';
        this.subagentTasks = [];
        this.refreshTokenUsage();
        this.refreshSessions();
      } else if (type === 'turn_error') {
        // 终态错误（toast 由随后的 error 事件负责展示）
        this.running = false;
        const last = this.messages[this.messages.length - 1];
        if (last && last.role === 'assistant') {
          last.execution_state = reduceExecutionState(last.execution_state, 'turn_error');
        }
        this.currentStreamingText = '';
        this.currentThinkingText = '';
        this.subagentTasks = [];
        // Low 修复：turn_error 也补一次会话列表刷新（此前仅在公共终态块中
        // 按 runningSessions 为空兜底，与 turn_finish 重复且 turn_error 无覆盖）
        this.refreshSessions();
        // 非当前窗口报错中断 → 红点
        this._setSessionStatusDot(eventSession, 'red');
      } else if (type === 'token_usage') {
        this.tokenUsage = normalizeTokenUsage(data);
      } else if (type === 'token_usage_updated') {
        // 异步 token 重算完成（后端 _async_recalc_token_usage 推送），
        // 数据形状与 token_usage 事件一致，直接复用同一归一化
        this.tokenUsage = normalizeTokenUsage(data);
      } else if (type === 'file_tree_loaded') {
        // 异步文件树加载完成（后端 _async_load_file_tree 推送）：
        // get_initial_state 不再同步扫树（大目录会卡加载页），改为后台扫描，
        // 完成后通过此事件推送到前端刷新文件面板。仅在 workdir 未变时采纳，
        // 防止用户启动后立刻切换会话导致旧树覆盖新会话的文件面板。
        const treeWorkdir = String(data.workdir || '');
        if (treeWorkdir && treeWorkdir === this.workdir) {
          this.fileTree = Array.isArray(data.file_tree) ? data.file_tree : [];
        }
      } else if (type === 'mcp_status') {
        if (data.mcps) this.mcps = data.mcps;
      } else if (type === 'diff_ready') {
        const d = data as DiffReadyEventData;
        const msg = this.activeAssistantOrThrow();
        const diff: LegacyDiff = {
          file_path: String(d.file_path || ''),
          diff_text: String(d.diff_text || ''),
        };
        // 只按非空 tool_call_id 绑定；空 ID 或找不到匹配调用时归入 legacy_diffs
        const toolCallId = String(d.tool_call_id || '');
        const target = toolCallId
          ? (msg.tool_calls || []).find(c => c.id === toolCallId)
          : undefined;
        if (target) {
          target.diff = diff;
        } else {
          (msg.legacy_diffs ??= []).push(diff);
        }
      } else if (type === 'subagent_progress') {
        const d = data as SubagentProgressEventData;
        // 快照归属到当前仍在串行运行的 run_subagent 调用；该调用完成后
        // 不再接受更新，最后一次快照自然冻结在调用上
        const last = this.messages[this.messages.length - 1];
        if (!last || last.role !== 'assistant') return;
        const target = [...(last.tool_calls || [])]
          .reverse()
          .find(c => c.name === 'run_subagent' && !c.result);
        if (!target) return;
        const snapshots = (d.tasks || []).map((t: any): SubagentTaskSnapshot => ({
          id: String(t.id || ''),
          description: String(t.description || ''),
          status: String(t.status || ''),
          stage: String(t.stage || ''),
          ...(t.stage_detail ? { stage_detail: String(t.stage_detail) } : {}),
          elapsed: Number(t.elapsed || 0),
        }));
        target.subagent_tasks = snapshots;
        this.subagentTasks = snapshots;
      } else if (type === 'background_subagent_terminal') {
        const statusLabel: Record<string, string> = {
          completed: '已完成', failed: '已失败', timed_out: '已超时', cancelled: '已取消',
        };
        const label = statusLabel[data.status] || data.status;
        const desc = data.description ? `：${data.description}` : '';
        if (data.status === 'completed') {
          this.notifyChat(`后台子代理${label}${desc}`);
        } else {
          // 与 error 事件一致：回合进行中不插队，挂在当前助手消息上展示
          const last = this.messages[this.messages.length - 1];
          if (last && last.role === 'assistant') {
            last.error_note = `后台子代理${label}${desc}`;
            this.notifyChat(`后台子代理${label}${desc}`);
          } else {
            this.messages.push({ role: 'error', content: `后台子代理${label}${desc}` });
          }
        }
      } else if (type === 'background_task_finished') {
        // 后台命令任务（execute_command 的 run_in_background，如打包）完成通知
        const code = Number(data.returncode);
        const tid = String(data.task_id || '');
        const statusText = code === 0 ? '已完成' : `已结束（退出码 ${code}）`;
        this.notifyChat(`后台任务 ${tid} ${statusText}`);
      } else if (type === 'terminal_output') {
        this.terminalOutput += data.text || '';
        // 输出上限 200K 字符：超出后从头部截断，避免长任务把内存拖爆
        if (this.terminalOutput.length > 200_000) {
          this.terminalOutput = '…[输出过长，头部已截断]\n' + this.terminalOutput.slice(-160_000);
        }
      } else if (type === 'terminal_finished') {
        this.terminalRunning = false;
        const code = data.code;
        this.terminalOutput += `\n[进程已退出，代码 ${code}]\n\n`;
      } else if (type === 'error') {
        const msgText = String(data.message || '运行错误');
        // 回合进行中：错误挂在当前助手消息上展示，避免插队成独立 error 消息
        // 破坏"最后一条是 assistant"的事件契约（否则后续 tool_finish 会抛错、
        // "正在干活"指示器卡死）。回合结束/空闲时的错误仍写入对话流。
        const last = this.messages[this.messages.length - 1];
        if (last && last.role === 'assistant') {
          // Low 修复：多条错误累计展示，不再被最后一条覆盖
          last.error_note = last.error_note ? last.error_note + '\n' + msgText : msgText;
          this.notifyChat(msgText);
        } else {
          this.messages.push({ role: 'error', content: msgText });
        }
      }
      } finally {
        this._captureRuntime(targetRuntimeKey);
        if (type === 'session_renamed' && data.old && data.new) {
          const oldKey = String(data.old);
          const newKey = String(data.new);
          if (this.sessionRuntimeByName[oldKey]) {
            this.sessionRuntimeByName[newKey] = this.sessionRuntimeByName[oldKey];
            delete this.sessionRuntimeByName[oldKey];
          }
          if (this.currentSessionName === newKey) this._bindSessionRuntime(newKey);
        } else if (switchedRuntime) {
          this._bindSessionRuntime(currentRuntimeKey);
        }
      }
    },

    async respondPermission(allowed: boolean, always: boolean) {
      const reqId = this.permRequest.reqId;
      const sessionName = this.permRequest.sessionName;
      this.permRequest.show = false;
      if (!reqId) return;
      const api = this._api();
      if (!api) return;
      try {
        const res = await api.respond_permission(reqId, allowed, always);
        if (!res || !res.ok) {
          const detail = res && res.msg ? `: ${res.msg}` : '';
          this._getSessionRuntime(sessionName).messages.push({ role: 'error', content: '权限响应发送失败' + detail });
        }
      } catch (err) {
        this.connected = false;
        this._getSessionRuntime(sessionName).messages.push({ role: 'error', content: '权限响应发送失败: ' + err });
      } finally {
        // 应答过的请求立即作废本地 reqId（保留新到的请求）：否则 switchSession 的
        // 重显逻辑会把已应答的卡片复活，再次点击必然报“已失效或已被处理”
        if (this.permRequest.reqId === reqId) {
          this.permRequest.reqId = '';
          this.permRequest.sessionName = '';
        }
      }
    },

    async respondPermissionChoice(choiceIndex: number, customText?: string) {
      const reqId = this.permRequest.reqId;
      const sessionName = this.permRequest.sessionName;
      this.permRequest.show = false;
      if (!reqId) return;
      const api = this._api();
      if (!api) return;
      try {
        const res = await api.respond_permission_choice(reqId, choiceIndex, customText);
        if (!res || !res.ok) {
          const detail = res && res.msg ? `: ${res.msg}` : '';
          this._getSessionRuntime(sessionName).messages.push({ role: 'error', content: '选项响应发送失败' + detail });
        }
      } catch (err) {
        this.connected = false;
        this._getSessionRuntime(sessionName).messages.push({ role: 'error', content: '选项响应发送失败: ' + err });
      } finally {
        // 与 respondPermission 同理：应答后作废本地 reqId，防止死卡片被重显
        if (this.permRequest.reqId === reqId) {
          this.permRequest.reqId = '';
          this.permRequest.sessionName = '';
        }
      }
    },

    async toggleSkill(skillId: string, enabled: boolean) {
      const api = this._api();
      if (!api) return;
      try {
        const res = await api.toggle_skill(skillId, enabled);
        if (res && res.ok) {
          this.skills = res.skills || this.skills;
        } else {
          this.notifyModal('技能', '切换技能状态失败: ' + (res && res.msg ? res.msg : '未知错误'));
        }
      } catch (err) {
        this.connected = false;
        this.notifyModal('技能', '切换技能状态失败: ' + err);
      }
    },

    async toggleMcpServer(name: string, enabled: boolean) {
      const api = this._api();
      if (!api) return;
      try {
        const res = await api.toggle_mcp_server(name, enabled);
        if (res && res.ok) {
          this.mcps = res.mcps || this.mcps;
          this.notifyModal('MCP 服务', res.msg || 'MCP 状态已更新');
        } else {
          this.notifyModal('MCP 服务', 'MCP 状态更新失败: ' + (res && res.msg ? res.msg : '未知错误'));
        }
      } catch (err) {
        this.connected = false;
        this.notifyModal('MCP 服务', 'MCP 状态更新失败: ' + err);
      }
    },

    // 添加自定义 MCP 服务：写盘与重连在后端后台线程完成，结果通过 mcp_status 事件回推
    async addMcpServer(name: string, command: string, args: string[], env?: Record<string, string>, enabled = true) {
      const api = this._api();
      if (!api) return;
      try {
        const res = await api.add_mcp_server(name, command, args, env, enabled);
        if (res && res.ok) {
          this.mcps = res.mcps || this.mcps;
          this.notifyModal('MCP 服务', res.msg || '已提交添加');
        } else {
          this.notifyModal('MCP 服务', '添加失败: ' + (res && res.msg ? res.msg : '未知错误'));
        }
      } catch (err) {
        this.connected = false;
        this.notifyModal('MCP 服务', '添加 MCP 服务失败: ' + err);
      }
    },

    async refreshPlugins() {
      const api = this._api();
      if (!api) return;
      // M31 修复：序号防护，过期响应不覆盖新快照
      const seq = ++this.pluginsReqSeq;
      try {
        const list = await api.get_plugins();
        if (seq !== this.pluginsReqSeq) return;
        this.plugins = list || [];
      } catch {
        // 静默失败，保留现有列表
      }
    },

    async togglePlugin(name: string, enabled: boolean) {
      const api = this._api();
      if (!api) return;
      try {
        const res = await api.toggle_plugin(name, enabled);
        if (res && res.ok) {
          this.plugins = res.plugins || this.plugins;
          this.notifyModal('插件', res.msg || '插件状态已更新');
        } else {
          this.notifyModal('插件', '插件状态更新失败: ' + (res && res.msg ? res.msg : '未知错误'));
        }
      } catch (err) {
        this.connected = false;
        this.notifyModal('插件', '插件状态更新失败: ' + err);
      }
    },

    async reconnectMcp() {
      const api = this._api();
      if (!api) return;
      try {
        const res = await api.reconnect_mcp();
        if (res && res.ok) {
          this.mcps = res.mcps || this.mcps;
          this.notifyModal('MCP 服务', res.msg || 'MCP 已重新连接');
        } else {
          this.notifyModal('MCP 服务', 'MCP 重连失败: ' + (res && res.msg ? res.msg : '未知错误'));
        }
      } catch (err) {
        this.connected = false;
        this.notifyModal('MCP 服务', 'MCP 重连失败: ' + err);
      }
    },

    async refreshMcpServers() {
      const api = window.pywebview && window.pywebview.api;
      if (!api) return;
      // M31 修复：序号防护，过期响应不覆盖新快照
      const seq = ++this.mcpReqSeq;
      try {
        const list = await api.get_mcp_servers();
        if (seq !== this.mcpReqSeq) return;
        if (list) this.mcps = list;
      } catch (err) {
        console.error('刷新 MCP 列表失败:', err);
      }
    },

    // ---------------- 内嵌终端 ----------------
    async runTerminalCommand(command: string) {
      const cmd = (command || '').trim();
      if (!cmd || this.terminalRunning) return;
      const api = this._api();
      if (!api) return;
      this.terminalOutput += `$ ${cmd}\n`;
      this.terminalRunning = true;
      try {
        const res = await api.run_terminal_command(cmd);
        if (!res || !res.ok) {
          this.terminalRunning = false;
          this.terminalOutput += `[启动失败: ${(res && res.msg) || '未知错误'}]\n`;
        }
      } catch (err) {
        this.terminalRunning = false;
        this.terminalOutput += `[启动失败: ${err}]\n`;
      }
    },

    async killTerminalCommand() {
      const api = this._api();
      if (!api) return;
      try {
        const res = await api.kill_terminal_command();
        if (!res || !res.ok) {
          this.notifyModal('后台任务', (res && res.msg) || '终止命令失败');
        }
      } catch (err) {
        this.notifyModal('后台任务', '终止命令失败: ' + err);
      }
    },

    clearTerminal() {
      this.terminalOutput = '';
    },

    async exportSession(fmt: 'md' | 'html' | 'json') {
      const api = this._api();
      if (!api) return;
      try {
        const res = await api.export_session(fmt);
        if (res && res.ok) {
          // 导出结果写入对话流（取代 Toast）；路径完整展示，用户可自行到该目录查看
          this.notifyChat('导出成功: ' + (res.path || ''));
        } else {
          this.notifyChat('导出失败: ' + (res && res.msg ? res.msg : '未知错误'));
        }
      } catch (err) {
        this.connected = false;
        this.notifyChat('导出失败: ' + err);
      }
    },

    async clearSessionHistory() {
      const ok = await this.askConfirm(
        '清空会话历史',
        '此操作不可撤销',
        '清空当前会话的全部历史记录？此操作不可撤销',
        '确定清空 ⏎'
      );
      if (!ok) return;

      const api = this._api();
      if (!api) return;
      try {
        const res = await api.clear_session_history(this.currentSessionName);
        if (res && res.ok) {
          // #12 修复：原实现只替换 store 顶层引用（this.messages = []），
          // 而 sessionRuntimeByName 中该会话仍持有旧数组，切走再切回会被
          // _bindSessionRuntime 重新绑定而"复活"已清空历史。
          // 改为：清空顶层数组 + 清空流式文本状态 + _captureCurrentRuntime()
          // 把空状态同步写回会话 runtime，保证切换语义一致。
          // 验证思路：清空后 switchSession 到其他会话再切回，messages 应为空。
          this.messages = [];
          this.currentStreamingText = '';
          this.currentThinkingText = '';
          this._captureCurrentRuntime();
          this.notifyChat('已清空当前会话历史');
        } else {
          this.notifyChat('清空失败: ' + (res && res.msg ? res.msg : '未知错误'));
        }
      } catch (err) {
        this.connected = false;
        this.notifyChat('清空失败: ' + err);
      }
    },

    async undoLastEdit() {
      const ok = await this.askConfirm(
        '撤销文件修改',
        '此操作将撤销星瑶最近一次的文件改动',
        '撤销星瑶上一次的文件修改？',
        '确定撤销 ⏎'
      );
      if (!ok) return;

      const api = this._api();
      if (!api) return;
      try {
        const res = await api.undo_last_edit();
        if (res && res.ok) {
          this.notifyChat(res.msg || '已撤销上一次文件修改');
        } else {
          this.notifyChat('撤销失败: ' + (res && res.msg ? res.msg : '未知错误'));
        }
      } catch (err) {
        this.connected = false;
        this.notifyChat('撤销失败: ' + err);
      }
    },

    async pickFiles(): Promise<string[]> {
      const api = this._api();
      if (!api) return [];
      try {
        const res = await api.pick_files();
        if (res && res.ok && res.paths) return res.paths;
        return [];
      } catch (err) {
        this.connected = false;
        this.notifyChat('选择文件失败: ' + err);
        return [];
      }
    },

    async getFilesInfo(paths: string[]) {
      const api = this._api();
      if (!api || !paths.length) return [];
      // Low 修复：补齐 catch（此前是唯一裸 await 的 API 调用，后端异常会向上抛）
      try {
        const list = await api.get_files_info(paths);
        return (list || []) as Array<{ path: string; name: string; ext: string; size: number | null; ok: boolean }>;
      } catch (err) {
        console.error('获取文件信息失败:', err);
        return [];
      }
    },

    // 保存剪贴板粘贴的图片，返回落盘后的文件路径（失败返回空串并 toast）
    async saveClipboardImage(dataUrl: string): Promise<string> {
      const api = this._api();
      if (!api) return '';
      try {
        const res = await api.save_clipboard_image(dataUrl);
        if (res && res.ok && res.path) return res.path;
        this.notifyChat('粘贴图片失败: ' + (res && res.msg ? res.msg : '未知错误'));
        return '';
      } catch (err) {
        this.notifyChat('粘贴图片失败: ' + err);
        return '';
      }
    },

    async openFilePreview(path: string) {
      this.filePreview = { show: true, path, content: '', truncated: false, loading: true };
      const api = this._api();
      if (!api) {
        this.filePreview.show = false;
        return;
      }
      // M27 修复：与 fileTreeReqSeq 一致的"仍是当前请求"校验，
      // 快速连点不同文件时旧响应不覆盖新文件内容
      const seq = ++this.filePreviewReqSeq;
      try {
        const res = await api.read_file_preview(path);
        if (seq !== this.filePreviewReqSeq) return; // 期间又打开/关闭了预览，丢弃过期响应
        if (res && res.ok) {
          this.filePreview.content = res.content || '';
          this.filePreview.truncated = !!res.truncated;
          this.filePreview.loading = false;
        } else {
          this.filePreview.show = false;
          this.notifyModal('文件预览', '无法预览文件: ' + (res && res.msg ? res.msg : '未知错误'));
        }
      } catch (err) {
        if (seq !== this.filePreviewReqSeq) return;
        this.filePreview.show = false;
        this.connected = false;
        this.notifyModal('文件预览', '无法预览文件: ' + err);
      }
    },

    closeFilePreview() {
      this.filePreview.show = false;
      // M27：关闭也使进行中的旧请求作废，响应返回时不再写回已关闭的弹窗
      this.filePreviewReqSeq++;
    },

    async openInSystem(path: string) {
      const api = this._api();
      if (!api) return;
      try {
        const res = await api.open_path(path);
        if (!res || !res.ok) {
          this.notifyModal('打开文件', '打开失败: ' + (res && res.msg ? res.msg : '未知错误'));
        }
      } catch (err) {
        this.connected = false;
        this.notifyModal('打开文件', '打开失败: ' + err);
      }
    },

    async addProvider(name: string, baseUrl: string, apiKey: string, protocol: string) {
      const api = this._api();
      if (!api) return null;
      try {
        const res = await api.add_provider(name, baseUrl, apiKey, protocol);
        if (res && res.ok) {
          if (res.settings) this.applyModelSettings(res.settings);
          this.notifyModal('供应商', res.msg || '供应商已添加');
          return res;
        } else {
          this.notifyModal('供应商', '添加供应商失败: ' + (res && res.msg ? res.msg : '未知错误'));
          return null;
        }
      } catch (err) {
        this.connected = false;
        this.notifyModal('供应商', '添加供应商失败: ' + err);
        return null;
      }
    },

    applyModelSettings(snapshot: ModelSettingsSnapshot) {
      this.modelProtocols = snapshot.protocols || [];
      this.modelPresets = snapshot.presets || [];
      this.providers = snapshot.providers || [];
      this.models = snapshot.models || [];
      this.activeModelId = snapshot.active_model_id || this.activeModelId;
    },

    async openModelSettings() {
      this.settingsOpen = true;
      this.subpage.show = false;
      const api = this._api();
      if (!api) return;
      try {
        this.applyModelSettings(await api.get_model_settings());
      } catch (err) {
        this.connected = false;
        this.notifyModal('模型配置', '加载模型配置失败: ' + err);
      }
    },

    async loadTokenStatistics(days: 1 | 7 | 30 = 30, force = false) {
      if (!force && this.tokenStatistics && this.tokenStatisticsDays === days) {
        return this.tokenStatistics;
      }
      const api = this._api();
      if (!api) return null;
      this.tokenStatisticsLoading = true;
      try {
        const result = await api.get_token_usage_statistics(days);
        if (!result || !result.ok) {
          this.notifyModal('Token 统计', '加载 Token 统计失败: ' + (result?.msg || '未知错误'));
          return result;
        }
        this.tokenStatistics = result;
        this.tokenStatisticsDays = result.range.days;
        return result;
      } catch (err) {
        this.connected = false;
        this.notifyModal('Token 统计', '加载 Token 统计失败: ' + err);
        return null;
      } finally {
        this.tokenStatisticsLoading = false;
      }
    },

    closeModelSettings() {
      this.settingsOpen = false;
    },

    async addPresetProvider(presetId: string, apiKey: string) {
      const api = this._api();
      if (!api) return null;
      try {
        const res = await api.add_preset_provider(presetId, apiKey);
        if (res?.ok && res.settings) {
          this.applyModelSettings(res.settings);
          this.notifyModal('供应商', res.msg || '预置厂商已添加');
          return res;
        }
        this.notifyModal('供应商', '添加预置厂商失败: ' + (res?.msg || '未知错误'));
        return null;
      } catch (err) {
        this.connected = false;
        this.notifyModal('供应商', '添加预置厂商失败: ' + err);
        return null;
      }
    },

    async updateProvider(providerId: string, name: string, baseUrl: string, apiKey: string, protocol: string) {
      const api = this._api();
      if (!api) return null;
      try {
        const res = await api.update_provider(providerId, name, baseUrl, apiKey, protocol);
        if (res?.ok && res.settings) {
          this.applyModelSettings(res.settings);
          this.notifyModal('供应商', res.msg || '厂商已更新');
          return res;
        }
        this.notifyModal('供应商', '更新厂商失败: ' + (res?.msg || '未知错误'));
        return null;
      } catch (err) {
        this.connected = false;
        this.notifyModal('供应商', '更新厂商失败: ' + err);
        return null;
      }
    },

    async deleteProvider(provider: ProviderInfo) {
      const ok = await this.askConfirm('删除厂商', '旗下模型也会删除', `确定要删除厂商「${provider.name}」吗？`);
      if (!ok) return;
      const api = this._api();
      if (!api) return;
      try {
        const res = await api.delete_provider(provider.id);
        if (res?.ok && res.settings) {
          this.applyModelSettings(res.settings);
          this.notifyModal('供应商', '厂商已删除');
        } else {
          this.notifyModal('供应商', '删除厂商失败: ' + (res?.msg || '未知错误'));
        }
      } catch (err) {
        this.connected = false;
        this.notifyModal('供应商', '删除厂商失败: ' + err);
      }
    },

    async addModel(providerId: string, modelName: string, contextWindow: number, supportsVision = false) {
      const api = this._api();
      if (!api) return null;
      try {
        const res = await api.add_model(providerId, modelName, contextWindow, supportsVision);
        if (res && res.ok) {
          if (res.settings) this.applyModelSettings(res.settings);
          else if (res.models) this.models = res.models;
          this.notifyModal('模型', res.msg || '模型已添加');
          return res;
        } else {
          this.notifyModal('模型', '添加模型失败: ' + (res && res.msg ? res.msg : '未知错误'));
          return null;
        }
      } catch (err) {
        this.connected = false;
        this.notifyModal('模型', '添加模型失败: ' + err);
        return null;
      }
    },

    async updateModel(providerId: string, modelId: string, modelName: string, contextWindow: number, supportsVision: boolean) {
      const api = this._api();
      if (!api) return null;
      try {
        const res = await api.update_model(providerId, modelId, modelName, contextWindow, supportsVision);
        if (res?.ok && res.settings) {
          this.applyModelSettings(res.settings);
          this.notifyModal('模型', res.msg || '模型已更新');
          return res;
        }
        this.notifyModal('模型', '更新模型失败: ' + (res?.msg || '未知错误'));
        return null;
      } catch (err) {
        this.connected = false;
        this.notifyModal('模型', '更新模型失败: ' + err);
        return null;
      }
    },

    async deleteModel(model: ModelInfo) {
      const ok = await this.askConfirm('删除模型', '删除后需要重新添加才能使用', `确定要删除模型「${model.name}」吗？`);
      if (!ok) return;

      const api = this._api();
      if (!api) return;
      try {
        const res = await api.delete_model(model.id);
        if (res && res.ok) {
          if (res.settings) this.applyModelSettings(res.settings);
          else if (res.models) this.models = res.models;
          this.notifyModal('模型', '模型已删除');
        } else {
          this.notifyModal('模型', '删除模型失败: ' + (res && res.msg ? res.msg : '未知错误'));
        }
      } catch (err) {
        this.connected = false;
        this.notifyModal('模型', '删除模型失败: ' + err);
      }
    },

    async testModel(modelId: string): Promise<{ ok: boolean; msg: string }> {
      const api = this._api();
      if (!api) return { ok: false, msg: '后端未连接' };
      try {
        const res = await api.test_model(modelId);
        const ok = !!(res && res.ok);
        return { ok, msg: (res && res.msg) || (ok ? '连接成功' : '连接失败') };
      } catch (err) {
        this.connected = false;
        return { ok: false, msg: String(err) };
      }
    },
  },
});
