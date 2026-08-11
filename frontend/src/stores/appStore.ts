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
  };
};

const emptyTokenUsage = (): TokenUsage => normalizeTokenUsage();

export interface ChatMessage {
  id?: string;
  role: 'user' | 'assistant' | 'tool' | 'error';
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

// 从完整路径中取出所在目录（不依赖 Node path 模块，纯字符串处理，兼容 \ 与 /）
function dirName(p: string): string {
  if (!p) return p;
  const idx = Math.max(p.lastIndexOf('\\'), p.lastIndexOf('/'));
  return idx >= 0 ? p.slice(0, idx) : p;
}

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

    // 权限模式（规范值：confirm | auto | plan | full，展示一律经 formatPermMode）
    permissionMode: 'confirm' as PermissionMode,

    // 当前激活模型的推理强度档位（default = 内置默认）
    reasoningEffort: 'default' as ReasoningEffort,

    // 会话 / 项目工作区
    sessionGroups: [] as SessionGroup[],
    currentSessionName: '',
    currentSessionDisplayName: '新对话',
    // 懒创建会话：点“新会话”只进入空白状态不落库；发首条消息时才真正创建，
    // 值为目标归属（'GLOBAL' 或项目路径），null = 非 pending 状态
    pendingWorkdir: null as string | null,
    currentWorkdirLabel: '对话',
    workdir: '',
    fileTree: [] as FileNode[],
    // 文件树后台加载序号：快速连切会话时过期结果凭序号丢弃
    fileTreeReqSeq: 0,

    // 技能 / MCP / 插件
    skills: [] as SkillInfo[],
    mcps: [] as McpInfo[],
    plugins: [] as PluginInfo[],

    // 对话消息流
    messages: [] as ChatMessage[],
    running: false,
    // 正在执行回合的会话集合（支持多会话并行）
    runningSessions: [] as string[],
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

    // Toast（支持可选的操作按钮，例如"打开所在文件夹"）
    toast: {
      show: false,
      msg: '',
      type: 'ok' as 'ok' | 'warn',
      action: null as { label: string; fn: () => void } | null,
    },
    toastTimer: null as any,

    // 确认弹窗
    confirmModal: { show: false, title: '', sub: '', message: '', confirmLabel: '确定删除 ⏎' } as ConfirmState,

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
      const key = state.currentSessionName || `__pending__:${state.pendingWorkdir || 'GLOBAL'}`;
      return state.sessionRuntimeByName[key]?.running || false;
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
    },

    _captureCurrentRuntime(this: any) {
      this._captureRuntime(this.currentSessionName);
    },

    _bindSessionRuntime(this: any, sessionName = this.currentSessionName) {
      const runtime = this._getSessionRuntime(sessionName);
      this.messages = runtime.messages;
      this.currentStreamingText = runtime.streamingText;
      this.currentThinkingText = runtime.thinkingText;
      this.running = runtime.running;
      this.subagentTasks = runtime.subagentTasks;
      this.planData = runtime.planData || { title: '', items: [], progress: 0 };
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
      }
    },

    // 统一的后端可用性检查：不可用时自动置 connected=false 并 toast 提示。
    // 返回值经 TS 收窄为局部 const，后续 await 调用不会丢失类型narrowing。
    _api(): PyWebViewAPI | null {
      const api = window.pywebview && window.pywebview.api;
      if (!api) {
        this.connected = false;
        this.showToast('后端连接失败：pywebview API 不可用', 'warn');
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

    showToast(msg: string, type: 'ok' | 'warn' = 'ok', action: { label: string; fn: () => void } | null = null) {
      this.toast.msg = msg;
      this.toast.type = type;
      this.toast.action = action;
      this.toast.show = true;
      if (this.toastTimer) clearTimeout(this.toastTimer);
      const duration = action ? 6000 : 2200;
      this.toastTimer = setTimeout(() => {
        this.toast.show = false;
      }, duration);
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
        this.showToast('后端连接失败：' + err, 'warn');
      } finally {
        await finishSplash();
      }
    },

    async setModel(modelId: string) {
      const api = this._api();
      if (!api) return;
      try {
        const res = await api.set_model(modelId);
        if (res && res.ok) {
          this.activeModelId = res.active_model_id || modelId;
          this.activeModelName = res.active_model_name || this.activeModelName;
          // 推理强度按模型保存：切换模型后同步该模型自己的档位
          this.reasoningEffort = res.reasoning_effort || 'default';
          this.showToast('已切换活动模型: ' + this.activeModelName, 'ok');
        } else {
          this.showToast('切换模型失败: ' + (res && res.msg ? res.msg : '未知错误'), 'warn');
        }
      } catch (err) {
        this.connected = false;
        this.showToast('切换模型失败: ' + err, 'warn');
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
          this.showToast(res.msg || ('推理强度已调整: ' + this.reasoningEffort), 'ok');
        } else {
          this.reasoningEffort = prev;
          this.showToast('推理强度调整失败: ' + (res && res.msg ? res.msg : '未知错误'), 'warn');
        }
      } catch (err) {
        this.reasoningEffort = prev;
        this.connected = false;
        this.showToast('推理强度调整失败: ' + err, 'warn');
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
          this.showToast('权限模式已调整: ' + formatPermMode(this.permissionMode), 'ok');
        } else {
          this.permissionMode = prev;
          this.showToast('权限模式调整失败: ' + (res && res.msg ? res.msg : '未知错误'), 'warn');
        }
      } catch (err) {
        this.permissionMode = prev;
        this.connected = false;
        this.showToast('权限模式调整失败: ' + err, 'warn');
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
          this.showToast(res.msg || ('副模型已更新: ' + (this.secondaryModelId || '未设置')), 'ok');
        } else {
          this.showToast('副模型设置失败: ' + (res && res.msg ? res.msg : '未知错误'), 'warn');
        }
      } catch (err) {
        this.connected = false;
        this.showToast('副模型设置失败: ' + err, 'warn');
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
          this.showToast(res.msg || '副模型用途已更新', 'ok');
        } else {
          this.secondaryModelRoles = prev;
          this.showToast('副模型用途设置失败: ' + (res && res.msg ? res.msg : '未知错误'), 'warn');
        }
      } catch (err) {
        this.secondaryModelRoles = prev;
        this.connected = false;
        this.showToast('副模型用途设置失败: ' + err, 'warn');
      }
    },

    async switchSession(sessionName: string, displayName?: string, targetWorkdir?: string) {
      // 切换到已有会话时丢弃未发送的空白 pending 会话
      this._captureCurrentRuntime();
      this.pendingWorkdir = null;
      this.currentSessionName = sessionName;
      if (displayName) this.currentSessionDisplayName = displayName;
      this.permRequest.show = this.permRequest.sessionName === sessionName;
      // 优先恢复内存中的实时 runtime；运行中的会话不能被旧数据库快照覆盖
      const runtime = this._getSessionRuntime(sessionName);
      this._bindSessionRuntime(sessionName);

      const api = this._api();
      if (!api) return;
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
        } else if (res.workdir) {
          this.workdir = res.workdir;
          this.currentWorkdirLabel = res.workdir.split('\\').pop() || res.workdir;
          // 文件树不再随切换响应返回（后端同步扫描大目录要数秒，拖慢切换）：
          // 先清空旧树秒开对话，树在后台异步加载，过期结果丢弃
          this.fileTree = [];
          this.loadFileTreeInBackground(sessionName, res.workdir);
        }

        if (res.sessions) this.sessionGroups = res.sessions;
      } catch (err) {
        this.connected = false;
        this.showToast('切换会话失败: ' + err, 'warn');
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
      this.running = false;
      this._captureRuntime('');
      this.currentWorkdirLabel = '对话';
      this.workdir = '';
      this.fileTree = [];
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
      this.running = false;
      this._captureRuntime('');
      this.workdir = targetWorkdir;
      this.currentWorkdirLabel = targetWorkdir.split('\\').pop() || targetWorkdir;
      // 文件树：当前就在该工作区则直接保留已加载的树；
      // 切到别的项目分组才清空并后台异步加载（过期结果有序号防护）
      if (!treeMatches) {
        this.fileTree = [];
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
          this.showToast(`已移出项目分组: ${folderTitle} (磁盘文件完好)`, 'ok');
        } else {
          this.showToast('移除项目分组失败: ' + (res && res.msg ? res.msg : '未知错误'), 'warn');
        }
      } catch (err) {
        this.connected = false;
        this.showToast('移除项目分组失败: ' + err, 'warn');
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
          if (sessionName === this.currentSessionName && res.current_session) {
            await this.switchSession(res.current_session, '新会话');
          }
          this.showToast('会话记录已删除', 'ok');
        } else {
          this.showToast('删除会话失败: ' + (res && res.msg ? res.msg : '未知错误'), 'warn');
        }
      } catch (err) {
        this.connected = false;
        this.showToast('删除会话失败: ' + err, 'warn');
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
          if (res.file_tree) this.fileTree = res.file_tree;
          if (res.sessions) this.sessionGroups = res.sessions;
          if (res.skills) this.skills = res.skills;
          this.showToast('已成功引入项目文件夹: ' + res.workdir, 'ok');
        }
        // res.ok === false 代表用户取消了文件夹选择，静默忽略即可
      } catch (err) {
        this.connected = false;
        this.showToast('选择文件夹失败: ' + err, 'warn');
      }
    },

    async stopCurrentTurn() {
      const sessionName = this.currentSessionName;
      const api = this._api();
      if (!api) return;
      try {
        const res = await api.stop_current_turn(sessionName);
        if (!res || !res.ok) {
          this._getSessionRuntime(sessionName).messages.push({ role: 'error', content: '中断请求未成功送达' });
        } else {
          this.showToast('已请求中断当前回合', 'warn');
        }
      } catch (err) {
        this.connected = false;
        this._getSessionRuntime(sessionName).messages.push({ role: 'error', content: '中断请求失败: ' + err });
      }
    },

    async sendMessage(text: string, attachments?: string[]) {
      // 生成中禁止再次发送（无论调用方是否已自行判断，均在此兜底拦截）
      const attachList = (attachments || []).filter(p => p && p.trim());
      if ((!text.trim() && !attachList.length) || this.runningSessions.includes(this.currentSessionName)) return;
      const api = window.pywebview && window.pywebview.api;
      if (!api) {
        // 本地无后端离线模拟，方便纯前端调试
        this.messages.push({ role: 'user', content: text.trim() });
        this.messages.push({ role: 'error', content: '无后端连接，仅处于开发离线预览视图' });
        this.running = false;
        return;
      }
      let sessionName = this.currentSessionName;
      try {
        // 懒创建：pending 状态下首条消息才真正创建会话（名称匹配 session_[a-f0-9]{6}
        // 才能被后端自动重命名为消息前几个字）
        let pendingDir: string | undefined;
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
        const res = await api.send_user_message(text.trim(), sessionName, attachList, pendingDir);
        if (res && res.ok && pendingDir) {
          // 让新会话立即出现在左侧列表（自动重命名事件随后会再刷新一次）
          this.refreshSessions();
        }
        if (!res || !res.ok) {
          this._setSessionRunning(sessionName, false);
          this._bindSessionRuntime(sessionName);
          this.messages.push({ role: 'error', content: '消息发送失败: ' + (res && res.msg ? res.msg : '未知错误') });
        }
        // 成功时不主动改 running，由后续 turn_start/turn_finish/turn_cancelled 等事件驱动
      } catch (err) {
        this._setSessionRunning(sessionName, false);
        this._bindSessionRuntime(sessionName);
        this.connected = false;
        this.messages.push({ role: 'error', content: '消息发送失败: ' + err });
      }
    },

    async refreshSessions() {
      const api = this._api();
      if (!api) return;
      try {
        const sessions = await api.get_sessions();
        if (sessions) this.sessionGroups = sessions;
      } catch (err) {
        // 静默失败：下一次切换会话/新建会话时会重新拉取
        console.error('刷新会话列表失败:', err);
      }
    },

    async refreshTokenUsage() {
      const api = window.pywebview && window.pywebview.api;
      if (!api) return;
      try {
        const usage = await api.get_token_usage();
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
      const eventSession = String(data.session_name || this.currentSessionName || '');
      const targetRuntimeKey = eventSession || currentRuntimeKey;
      const switchedRuntime = targetRuntimeKey !== currentRuntimeKey;
      if (switchedRuntime) this._bindSessionRuntime(targetRuntimeKey);

      try {

      // 多会话并行：终态事件从 runningSessions 移除对应会话
      if (type === 'turn_finish' || type === 'turn_cancelled' || type === 'turn_error' || type === 'turn_idle') {
        const finSession = String(data.session_name || '');
        if (finSession) {
          this.runningSessions = this.runningSessions.filter(s => s !== finSession);
        }
        // 当前 reducer 已绑定到目标 runtime，终态只更新目标会话。
        this.running = false;
        this.subagentTasks = [];
        // 没有活跃会话时刷新会话列表
        if (this.runningSessions.length === 0) {
          this.refreshSessions();
        }
      }

      if (type === 'turn_start') {
        const turnSession = String(data.session_name || '');
        // 记录活跃会话
        if (turnSession && !this.runningSessions.includes(turnSession)) {
          this.runningSessions.push(turnSession);
        }
        this.running = true;
        this.currentStreamingText = '';
        this.currentThinkingText = '';
        // 内部回合（如后台任务自动汇总）：展示友好的系统提示文案，
        // 不把内部指令原文渲染成用户气泡
        this.messages.push({ role: 'user', content: data.display_text || data.user_text });
        this.messages.push({
          role: 'assistant',
          content: '',
          reasoning_content: '',
          tool_calls: [],
          legacy_diffs: [],
          execution_state: 'running',
        });
        if (data.internal === 'bg_summary') {
          this.showToast('后台子代理任务已全部结束，正在自动汇总结果', 'ok');
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
      } else if (type === 'permission_dismiss') {
        // 后端权限队列已清空（请求被超时拒绝/取消）：收起内联权限卡片，避免死请求残留
        this.permRequest.show = false;
      } else if (type === 'notice') {
        if (data.message) this.showToast(String(data.message), 'ok');
      } else if (type === 'session_renamed') {
        if (this.currentSessionName === data.old) {
          this.currentSessionName = data.new;
          this.currentSessionDisplayName = data.new;
        }
        // 运行中回合的会话名同步迁移：否则 runningSessions 残留旧名，
        // 后续 turn_finish(新名) 无法正确移除，会导致 running 状态卡死
        if (data.old && data.new) {
          this.runningSessions = this.runningSessions.map(s => (s === data.old ? data.new : s));
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
      } else if (type === 'token_usage') {
        this.tokenUsage = normalizeTokenUsage(data);
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
          this.showToast(`后台子代理${label}${desc}`, 'ok');
        } else {
          // 与 error 事件一致：回合进行中不插队，挂在当前助手消息上展示
          const last = this.messages[this.messages.length - 1];
          if (last && last.role === 'assistant') {
            last.error_note = `后台子代理${label}${desc}`;
            this.showToast(`后台子代理${label}${desc}`, 'warn');
          } else {
            this.messages.push({ role: 'error', content: `后台子代理${label}${desc}` });
          }
        }
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
          last.error_note = msgText;
          this.showToast(msgText, 'warn');
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
          this._getSessionRuntime(sessionName).messages.push({ role: 'error', content: '权限响应发送失败' });
        }
      } catch (err) {
        this.connected = false;
        this._getSessionRuntime(sessionName).messages.push({ role: 'error', content: '权限响应发送失败: ' + err });
      }
    },

    async respondPermissionChoice(choiceIndex: number) {
      const reqId = this.permRequest.reqId;
      const sessionName = this.permRequest.sessionName;
      this.permRequest.show = false;
      if (!reqId) return;
      const api = this._api();
      if (!api) return;
      try {
        const res = await api.respond_permission_choice(reqId, choiceIndex);
        if (!res || !res.ok) {
          this._getSessionRuntime(sessionName).messages.push({ role: 'error', content: '选项响应发送失败' });
        }
      } catch (err) {
        this.connected = false;
        this._getSessionRuntime(sessionName).messages.push({ role: 'error', content: '选项响应发送失败: ' + err });
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
          this.showToast('切换技能状态失败: ' + (res && res.msg ? res.msg : '未知错误'), 'warn');
        }
      } catch (err) {
        this.connected = false;
        this.showToast('切换技能状态失败: ' + err, 'warn');
      }
    },

    async toggleMcpServer(name: string, enabled: boolean) {
      const api = this._api();
      if (!api) return;
      try {
        const res = await api.toggle_mcp_server(name, enabled);
        if (res && res.ok) {
          this.mcps = res.mcps || this.mcps;
          this.showToast(res.msg || 'MCP 状态已更新', 'ok');
        } else {
          this.showToast('MCP 状态更新失败: ' + (res && res.msg ? res.msg : '未知错误'), 'warn');
        }
      } catch (err) {
        this.connected = false;
        this.showToast('MCP 状态更新失败: ' + err, 'warn');
      }
    },

    // 启用 MCP 服务：预置服务需带上用户填写的密钥；已配置服务等价于 toggle
    async enableMcpServer(name: string, envValues?: Record<string, string>) {
      const api = this._api();
      if (!api) return;
      try {
        const res = await api.enable_mcp_server(name, envValues || {});
        if (res && res.ok) {
          this.mcps = res.mcps || this.mcps;
          this.showToast(res.msg || 'MCP 状态已更新', 'ok');
        } else {
          this.showToast('启用失败: ' + (res && res.msg ? res.msg : '未知错误'), 'warn');
        }
      } catch (err) {
        this.connected = false;
        this.showToast('启用 MCP 服务失败: ' + err, 'warn');
      }
    },

    async refreshPlugins() {
      const api = this._api();
      if (!api) return;
      try {
        this.plugins = (await api.get_plugins()) || [];
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
          this.showToast(res.msg || '插件状态已更新', 'ok');
        } else {
          this.showToast('插件状态更新失败: ' + (res && res.msg ? res.msg : '未知错误'), 'warn');
        }
      } catch (err) {
        this.connected = false;
        this.showToast('插件状态更新失败: ' + err, 'warn');
      }
    },

    async reconnectMcp() {
      const api = this._api();
      if (!api) return;
      try {
        const res = await api.reconnect_mcp();
        if (res && res.ok) {
          this.mcps = res.mcps || this.mcps;
          this.showToast(res.msg || 'MCP 已重新连接', 'ok');
        } else {
          this.showToast('MCP 重连失败: ' + (res && res.msg ? res.msg : '未知错误'), 'warn');
        }
      } catch (err) {
        this.connected = false;
        this.showToast('MCP 重连失败: ' + err, 'warn');
      }
    },

    async refreshMcpServers() {
      const api = window.pywebview && window.pywebview.api;
      if (!api) return;
      try {
        const list = await api.get_mcp_servers();
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
          this.showToast((res && res.msg) || '终止命令失败', 'warn');
        }
      } catch (err) {
        this.showToast('终止命令失败: ' + err, 'warn');
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
          const path = res.path || '';
          this.showToast(
            '导出成功: ' + path,
            'ok',
            path ? { label: '打开所在文件夹', fn: () => this.openInSystem(dirName(path)) } : null
          );
        } else {
          this.showToast('导出失败: ' + (res && res.msg ? res.msg : '未知错误'), 'warn');
        }
      } catch (err) {
        this.connected = false;
        this.showToast('导出失败: ' + err, 'warn');
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
          this.messages = [];
          this.showToast('已清空当前会话历史', 'ok');
        } else {
          this.showToast('清空失败: ' + (res && res.msg ? res.msg : '未知错误'), 'warn');
        }
      } catch (err) {
        this.connected = false;
        this.showToast('清空失败: ' + err, 'warn');
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
          this.showToast(res.msg || '已撤销上一次文件修改', 'ok');
        } else {
          this.showToast('撤销失败: ' + (res && res.msg ? res.msg : '未知错误'), 'warn');
        }
      } catch (err) {
        this.connected = false;
        this.showToast('撤销失败: ' + err, 'warn');
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
        this.showToast('选择文件失败: ' + err, 'warn');
        return [];
      }
    },

    async getFilesInfo(paths: string[]) {
      const api = this._api();
      if (!api || !paths.length) return [];
      const list = await api.get_files_info(paths);
      return (list || []) as Array<{ path: string; name: string; ext: string; size: number | null; ok: boolean }>;
    },

    // 保存剪贴板粘贴的图片，返回落盘后的文件路径（失败返回空串并 toast）
    async saveClipboardImage(dataUrl: string): Promise<string> {
      const api = this._api();
      if (!api) return '';
      try {
        const res = await api.save_clipboard_image(dataUrl);
        if (res && res.ok && res.path) return res.path;
        this.showToast('粘贴图片失败: ' + (res && res.msg ? res.msg : '未知错误'), 'warn');
        return '';
      } catch (err) {
        this.showToast('粘贴图片失败: ' + err, 'warn');
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
      try {
        const res = await api.read_file_preview(path);
        if (res && res.ok) {
          this.filePreview.content = res.content || '';
          this.filePreview.truncated = !!res.truncated;
          this.filePreview.loading = false;
        } else {
          this.filePreview.show = false;
          this.showToast('无法预览文件: ' + (res && res.msg ? res.msg : '未知错误'), 'warn');
        }
      } catch (err) {
        this.filePreview.show = false;
        this.connected = false;
        this.showToast('无法预览文件: ' + err, 'warn');
      }
    },

    closeFilePreview() {
      this.filePreview.show = false;
    },

    async openInSystem(path: string) {
      const api = this._api();
      if (!api) return;
      try {
        const res = await api.open_path(path);
        if (!res || !res.ok) {
          this.showToast('打开失败: ' + (res && res.msg ? res.msg : '未知错误'), 'warn');
        }
      } catch (err) {
        this.connected = false;
        this.showToast('打开失败: ' + err, 'warn');
      }
    },

    async addProvider(name: string, baseUrl: string, apiKey: string, protocol: string) {
      const api = this._api();
      if (!api) return null;
      try {
        const res = await api.add_provider(name, baseUrl, apiKey, protocol);
        if (res && res.ok) {
          if (res.settings) this.applyModelSettings(res.settings);
          this.showToast(res.msg || '供应商已添加', 'ok');
          return res;
        } else {
          this.showToast('添加供应商失败: ' + (res && res.msg ? res.msg : '未知错误'), 'warn');
          return null;
        }
      } catch (err) {
        this.connected = false;
        this.showToast('添加供应商失败: ' + err, 'warn');
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
        this.showToast('加载模型配置失败: ' + err, 'warn');
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
          this.showToast('加载 Token 统计失败: ' + (result?.msg || '未知错误'), 'warn');
          return result;
        }
        this.tokenStatistics = result;
        this.tokenStatisticsDays = result.range.days;
        return result;
      } catch (err) {
        this.connected = false;
        this.showToast('加载 Token 统计失败: ' + err, 'warn');
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
          this.showToast(res.msg || '预置厂商已添加', 'ok');
          return res;
        }
        this.showToast('添加预置厂商失败: ' + (res?.msg || '未知错误'), 'warn');
        return null;
      } catch (err) {
        this.connected = false;
        this.showToast('添加预置厂商失败: ' + err, 'warn');
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
          this.showToast(res.msg || '厂商已更新', 'ok');
          return res;
        }
        this.showToast('更新厂商失败: ' + (res?.msg || '未知错误'), 'warn');
        return null;
      } catch (err) {
        this.connected = false;
        this.showToast('更新厂商失败: ' + err, 'warn');
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
          this.showToast('厂商已删除', 'ok');
        } else {
          this.showToast('删除厂商失败: ' + (res?.msg || '未知错误'), 'warn');
        }
      } catch (err) {
        this.connected = false;
        this.showToast('删除厂商失败: ' + err, 'warn');
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
          this.showToast(res.msg || '模型已添加', 'ok');
          return res;
        } else {
          this.showToast('添加模型失败: ' + (res && res.msg ? res.msg : '未知错误'), 'warn');
          return null;
        }
      } catch (err) {
        this.connected = false;
        this.showToast('添加模型失败: ' + err, 'warn');
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
          this.showToast(res.msg || '模型已更新', 'ok');
          return res;
        }
        this.showToast('更新模型失败: ' + (res?.msg || '未知错误'), 'warn');
        return null;
      } catch (err) {
        this.connected = false;
        this.showToast('更新模型失败: ' + err, 'warn');
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
          this.showToast('模型已删除', 'ok');
        } else {
          this.showToast('删除模型失败: ' + (res && res.msg ? res.msg : '未知错误'), 'warn');
        }
      } catch (err) {
        this.connected = false;
        this.showToast('删除模型失败: ' + err, 'warn');
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
