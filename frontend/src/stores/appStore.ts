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
  ReasoningChoice,
  ReasoningSnapshot,
  SystemSettingsSnapshot,
  ComputerProtocolChoice,
  ComputerActionTrace,
  ComputerControlEvent,
  ComputerRunSnapshot,
  PyWebViewAPI,
  ToolStartEventData,
  ToolFinishEventData,
  DiffReadyEventData,
  SubagentProgressEventData,
  QueuedMessage,
} from '../types/pywebview';
import type { MediaItem } from '../types/media';
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
import { StreamingSpeedTracker } from '../utils/tokenSpeed';
import { applyGuiTheme, type AppliedTheme } from '../themes/runtime';
import type {
  GuiTheme,
  GuiThemeError,
  GuiThemeFailure,
  GuiThemeResult,
  GuiThemeSummary,
  NativeTitlebarColorCapability,
  ThemeRegionId,
} from '../themes/types';
import {
  RIGHT_PANEL_DEFAULT_WIDTH,
  RIGHT_PANEL_MAX_WIDTH,
  RIGHT_PANEL_MIN_WIDTH,
  RIGHT_PANEL_WIDTH_KEY,
  SIDEBAR_DEFAULT_WIDTH,
  SIDEBAR_MAX_WIDTH,
  SIDEBAR_MIN_WIDTH,
  SIDEBAR_WIDTH_KEY,
  computeRestoredWidth,
} from '../composables/usePanelResize';
import {
  DISCLAIMER_VERSION,
  isDisclaimerVersionAccepted,
} from '../data/disclaimer';

/**
 * 面板宽度启动恢复：读取 localStorage 中上次拖拽记忆的宽度，
 * 非法/越界值经 computeRestoredWidth 夹取或回默认；存储不可用（隐私模式）
 * 时静默回默认。视口上限夹取只在拖拽时发生，启动保持用户上次拖出的值。
 */
function storedPanelWidth(key: string, def: number, min: number, max: number): number {
  try {
    return computeRestoredWidth(localStorage.getItem(key), min, max) ?? def;
  } catch {
    return def;
  }
}

function cloneGuiTheme(theme: GuiTheme): GuiTheme {
  // Pinia exposes state through Vue proxies, which structuredClone rejects.
  // The bridge contract is JSON-only, so a JSON round trip is the exact clone boundary.
  return JSON.parse(JSON.stringify(theme)) as GuiTheme;
}

function guiThemeFailure(error: unknown, path = 'theme'): GuiThemeFailure {
  if (error && typeof error === 'object' && 'ok' in error && error.ok === false && 'error' in error) {
    return guiThemeFailure(error.error, path);
  }
  if (error && typeof error === 'object' && 'code' in error && 'message' in error) {
    const value = error as { code: unknown; path?: unknown; message: unknown };
    return {
      ok: false,
      error: {
        code: String(value.code),
        path: String(value.path ?? path),
        message: String(value.message),
      },
    };
  }
  return {
    ok: false,
    error: {
      code: 'theme_runtime_failed',
      path,
      message: error instanceof Error ? error.message : String(error),
    },
  };
}

type GuiThemeActionResult = GuiThemeResult<{ theme: GuiTheme }>;
type GuiThemeListResult = GuiThemeResult<{ themes: GuiThemeSummary[] }>;
type GuiThemeAssetResult = GuiThemeResult<{ asset_path: string }>;
type GuiThemePathResult = GuiThemeResult<{ path: string }>;
const guiThemeOperationQueues = new WeakMap<object, Promise<void>>();

function enqueueGuiThemeOperation<T extends object>(
  store: object,
  operation: () => Promise<GuiThemeResult<T>>,
): Promise<GuiThemeResult<T>> {
  const previous = guiThemeOperationQueues.get(store) ?? Promise.resolve();
  const result = previous.then(operation);
  guiThemeOperationQueues.set(store, result.then(() => undefined, () => undefined));
  return result;
}

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

// ── 电脑操控运行态（Task 13b，设计 16.4）：纯 reducer + 快照校准 ──

// 四类平铺信封事件（top-level run_id + 严格递增 event_seq）
const COMPUTER_EVENT_TYPES = new Set([
  'computer_state',
  'computer_action_started',
  'computer_action_finished',
  'computer_observation',
]);

// ComputerRunSnapshot 的公开字段（Task 12 已有类型，17 字段复用不重复定义）
const COMPUTER_SNAPSHOT_FIELDS = [
  'run_id', 'owner_session_id', 'owner_turn_id', 'status', 'target', 'browser',
  'model_name', 'protocol', 'started_at', 'action_count', 'current_action',
  'current_surface', 'preview_url', 'pause_reason', 'stop_reason', 'error_code',
  'hotkey_available',
] as const;

// 顶部安全条只在 run 活动期显示（brief 逐字）：idle/stopped/completed/completing 不显示
const COMPUTER_BAR_STATUSES = [
  'starting', 'running', 'paused', 'waiting_for_user',
  'provider_safety_required', 'stopping', 'failed',
];

// run 终态（2026-09-10 设计）：页签关闭入口与自动关闭计时的共同判据
const COMPUTER_TERMINAL_STATUSES = ['completed', 'stopped', 'failed'];

// 内存轨迹上限（设计 16.2）：只保留最新 200 条
const COMPUTER_TRACE_MAX = 200;

// 动作脱敏摘要：type_text 只显示长度（事件不带正文），其余按动作类型命名
const COMPUTER_ACTION_KIND_LABELS: Record<string, string> = {
  observe: '观察画面',
  click: '点击',
  double_click: '双击',
  move: '移动鼠标',
  drag: '拖拽',
  scroll: '滚动',
  type_text: '输入文本',
  keypress: '按键',
  wait: '等待',
  navigate: '导航',
  back: '后退',
  forward: '前进',
  reload: '刷新',
  request_user_takeover: '请求用户接管',
};

const emptyComputerRun = (): ComputerRunSnapshot => ({
  run_id: '',
  owner_session_id: '',
  owner_turn_id: '',
  status: 'idle',
  target: '',
  browser: '',
  model_name: '',
  protocol: 'none',
  started_at: 0,
  action_count: 0,
  current_action: '',
  current_surface: '',
  preview_url: '',
  pause_reason: '',
  stop_reason: '',
  error_code: '',
  hotkey_available: false,
});

// 平铺信封识别：既有事件是 {"type","data"}（无顶层 run_id），电脑操控事件带顶层
// run_id——按 type 集合 + run_id 存在性分流，绝不误伤普通事件
function isComputerFlatEvent(event: BridgeEvent | undefined): boolean {
  return !!event
    && COMPUTER_EVENT_TYPES.has(String(event.type || ''))
    && typeof event.run_id === 'string';
}

// 轨迹行脱敏摘要：只信任契约提供的 summary / text_length，其余字段一律不透传
function computerTraceSummary(event: Record<string, unknown>): string {
  const explicit = event.summary;
  if (typeof explicit === 'string' && explicit.trim()) return explicit;
  const kind = String(event.action_kind || '');
  if (kind === 'type_text') {
    const length = event.text_length;
    return typeof length === 'number' && length >= 0 ? `输入 ${length} 个字符` : '输入文本';
  }
  return COMPUTER_ACTION_KIND_LABELS[kind] || (kind ? `动作 ${kind}` : '电脑动作');
}

export interface MessageMetrics {
  duration_s?: number;
  output_duration_s?: number;
  ttft_ms?: number;
  completion_tokens?: number;
  token_speed?: number;
}

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
  // 本条助手消息生成时所用的模型快照（后端历史回放透传；旧消息可能缺省）
  model_id?: string;
  model_name?: string;
  // 助手消息媒体清单（media_v1，桥接层已拼好带令牌 URL；旧消息缺省）
  media?: MediaItem[];
  // 生成耗时与速率指标（后端权威结算或历史持久化透传）
  metrics?: MessageMetrics;
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
  permissionOptions: Array<{ key: string; label: string }>;
  // 请求来源工具名：'ask_user' 时为澄清问题，否则为标准权限请求
  functionName: string;
}

export type SubpageType = 'skills' | 'mcp' | 'plugins' | 'models';

const speedTracker = new StreamingSpeedTracker();

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

    // 系统设置使用独立快照，后续新增系统级选项时继续扩展该对象。
    systemSettings: {
      command_shell: 'auto',
      command_shell_options: [],
      command_shell_available: true,
      command_shell_error: '',
      remote_token_ttl: 86400,
      agent_turn_step_limit: 200,
      agent_turn_step_limit_min: 10,
      agent_turn_step_limit_max: 1000,
      // 电脑操控默认关闭（设计 8.1）：真实值一律以后端快照覆盖
      browser_control_enabled: false,
      computer_control_enabled: false,
      computer_browser: 'edge',
      computer_browser_options: [],
      computer_profile_path: '',
      computer_platform_supported: true,
      computer_hotkey_available: false,
      computer_hotkey: 'Ctrl+Alt+Esc',
      computer_active_model_capability: { model: '', eligible: false },
      computer_run: {
        run_id: '', owner_session_id: '', owner_turn_id: '', status: 'idle',
        target: '', browser: '', model_name: '', protocol: 'none', started_at: 0,
        action_count: 0, current_action: '', current_surface: '', preview_url: '',
        pause_reason: '', stop_reason: '', error_code: '', hotkey_available: false,
      },
    } as SystemSettingsSnapshot,

    // 电脑操控运行态（Task 13b，设计 16.4）：reduceComputerEvent 纯 reducer 维护的
    // 当前 run 快照（事件 + Bridge 快照校准），与 systemSettings.computer_run
    // （设置读取快照）相互独立；轨迹为内存态，run 结束保留到下一个 run
    computerRun: emptyComputerRun(),
    computerTrace: [] as ComputerActionTrace[],
    // 操控页签自动打开标志：run 开始时首次自动切到操控页签一次，
    // 用户手动切走后不抢回（设计 16.2），新 run 再开一次
    computerLastAutoOpenedRunId: '',
    // reducer 记账（不参与渲染）：当前 run 已接受的最大 event_seq（严格递增门闩）
    // 与动作起始毫秒（finished 事件缺 duration_ms 时计算耗时）
    _computerEventSeq: 0,
    _computerActionStartMs: {} as Record<number, number>,
    // 已关闭 run 黑名单（不参与渲染）：清空运行态后拒绝该 run 的迟到事件与快照，
    // 防止「操控」页签被旧事件复活
    _computerClosedRunId: '',

    // 当前激活模型的思考能力与选择；未知模型保持隐藏，不在前端猜测能力。
    reasoning: {
      kind: 'hidden',
      visible: false,
      disabled: false,
      disabled_reason: '',
      choice: 'default',
      options: [],
    } as ReasoningSnapshot,

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
    // 同一模型的思考选择请求版本：只有最后一次请求可以回写或回滚 UI。
    _reasoningChoiceVersion: {} as Record<string, number>,
    currentWorkdirLabel: '对话',
    workdir: '',
    fileTree: [] as FileNode[],
    // 文件树刷新中标记（驱动刷新按钮的 loading 态，并防止并发刷新）
    fileTreeRefreshing: false,
    // 文件树展开状态（按完整路径记录）：刷新后据此恢复展开并逐层刷新内容
    fileTreeExpanded: [] as string[],
    // 文件树后台加载序号：快速连切会话时过期结果凭序号丢弃
    fileTreeReqSeq: 0,
    // 本回合实际用到的技能名（从 is_skill 工具调用收集）：turn_start 时清空，
    // 回合结束后固化为本回合结果，状态栏"技能"项据此显示；随会话 runtime 保存/恢复
    usedSkills: [] as string[],
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
    // 当前流式生成瞬时速度（t/s）：随会话切换恢复
    streamingSpeed: 0,
    // 正在执行回合的会话集合（支持多会话并行）
    runningSessions: [] as string[],
    // 批次D：回合已提交但在等 _turn_workdir_lock 的会话（turn_queued 事件）。
    // 排队也算运行中（同时在 runningSessions 中），此集合仅用于 UI 区分
    // "排队等其他会话"与"正在执行"；turn_start/终态事件到达时清除
    queuedSessions: [] as string[],
    // 会话消息排队：回合运行期间发送的消息按 FIFO 入队（键为会话名）。
    // 后端每次入队/取消/插队/补发都推送 message_queue 全量快照，
    // 前端幂等替换渲染，回合结束后自动补发队首
    messageQueues: {} as Record<string, QueuedMessage[]>,
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
    // 移动端（<=768px）默认收起侧栏：侧栏作为 fixed 覆盖层，展开时滑入，
    // 避免在手机上初始加载即被侧栏遮挡主对话区。
    sidebarCollapsed:
      typeof window !== 'undefined' &&
      typeof window.matchMedia === 'function' &&
      window.matchMedia('(max-width: 768px)').matches,
    rightPanelCollapsed: false,
    // 桌面端左右面板宽度（px）：拖拽手柄实时更新，启动时从 localStorage 恢复；
    // 移动端（<=768px）忽略这两个值，布局由 style.css 媒体查询接管
    sidebarWidth: storedPanelWidth(SIDEBAR_WIDTH_KEY, SIDEBAR_DEFAULT_WIDTH, SIDEBAR_MIN_WIDTH, SIDEBAR_MAX_WIDTH),
    rightPanelWidth: storedPanelWidth(RIGHT_PANEL_WIDTH_KEY, RIGHT_PANEL_DEFAULT_WIDTH, RIGHT_PANEL_MIN_WIDTH, RIGHT_PANEL_MAX_WIDTH),
    themeLight: false,
    guiTheme: null as GuiTheme | null,
    savedGuiTheme: null as GuiTheme | null,
    guiThemeDraft: null as GuiTheme | null,
    guiThemes: [] as GuiThemeSummary[],
    guiThemeError: null as GuiThemeError | null,
    guiThemeListError: null as GuiThemeError | null,
    nativeTitlebarColorCapability: 'unknown' as NativeTitlebarColorCapability,
    _appliedGuiTheme: null as AppliedTheme | null,
    settingsOpen: false,
    // frameless 关闭确认框（替代 win32 原生 MessageBox）：有回合运行时三选
    closeConfirm: { show: false, trayAvailable: false } as { show: boolean; trayAvailable: boolean },
    // 手机端内置目录浏览器（引入新项目工作区用，桌面不使用）
    remoteDirBrowser: {
      show: false,
      loading: false,
      error: '',
      current: '',
      isRoot: true,
      selected: '',
      dirs: [] as Array<{ name: string; path: string }>,
    },
    // 设置页打开后要落在哪个分区（模型配置/外观与主题等），由调用方指定；
    // 预览返回编辑器时用它跳回"外观与主题"而不是默认的模型配置
    settingsSection: 'models' as 'models' | 'tokens' | 'permissions' | 'appearance' | 'system' | 'persona' | 'about' | 'contact' | 'disclaimer',
    // 智能体显示名：设置页「人设设置」自定义后全局生效（界面文案/执行过程/思考提示）
    agentName: '星瑶',
    // 智能体对用户的称呼；空串表示不使用特定称呼
    userTitle: '',
    // 免责条款同意状态：requiredVersion 为当前生效的条款版本，acceptedVersion
    // 为空或与之不一致时弹首次使用确认框。两者均由后端落盘到
    // ~/.ycode/disclaimer_consent.json，前端只负责展示判定与触发确认。
    disclaimerRequiredVersion: '',
    disclaimerAcceptedVersion: '',
    // 主题预览模式：设置页"预览效果"收起设置、直接看真实首页（草稿已实时渲染），
    // 顶部浮条提供返回编辑；保存/取消/切主题时退出该模式
    themePreviewMode: false,

    // 计划、后台任务、Token 用量
    planData: { title: '', items: [], progress: 0 } as PlanData,
    backgroundTasks: [] as BackgroundTask[],
    subagentTasks: [] as SubagentTaskSnapshot[],
    tokenUsage: emptyTokenUsage(),
    tokenStatistics: null as TokenStatisticsSnapshot | null,
    tokenStatisticsLoading: false,
    tokenStatisticsDays: 30 as 1 | 7 | 30,

    // 软件更新状态
    updateState: {
      status: 'idle' as 'idle' | 'checking' | 'available' | 'downloading' | 'downloaded' | 'installing' | 'error',
      hasUpdate: false,
      latestVersion: '',
      currentVersion: '',
      releaseTitle: '',
      releaseNotes: '',
      downloadUrl: '',
      assetName: '',
      assetSize: 0,
      // 分卷下载（Gitee 单附件 100MB 限制）：非空时 downloadUrl 不再直接使用
      partUrls: [] as string[],
      // 官方 SHA256（来自 Release 描述），下载完成后后端强制校验
      sha256: '',
      downloadProgress: 0,
      downloadedBytes: 0,
      totalBytes: 0,
      installerPath: '',
      showUpdateModal: false,
      errorMessage: '',
    },

    // 确认弹窗（notice 模式时退化为单按钮提示框，用户点击"确定"才关闭）
    confirmModal: { show: false, title: '', sub: '', message: '', confirmLabel: '确定删除 ⏎', notice: false } as ConfirmState,

    // 权限请求弹窗
    permRequest: { show: false, sessionName: '', reqId: '', summary: '', source: '', allowAlways: false, options: [], permissionOptions: [], functionName: '' } as PermReqState,

    // 二级抽屉页面：技能 / MCP / 模型管理
    subpage: { show: false, type: 'skills' as SubpageType },

    // 文件预览弹窗（RightPanel 文件树点击文件时使用）
    // kind=image 时正文渲染 dataUrl（base64 图片），否则渲染 content 文本
    filePreview: {
      show: false,
      path: '',
      kind: 'text' as 'text' | 'image',
      content: '',
      dataUrl: '',
      truncated: false,
      loading: false,
    },

    // 当前流式生成临时对象
    currentStreamingText: '',
    currentThinkingText: '',

    // frameless 窗口最大化状态（后端 window_maximize_state 事件推送）
    windowMaximized: false,

    // 内嵌终端（右侧面板）
    terminalOutput: '',
    terminalRunning: false,
  }),

  getters: {
    // 是否需要弹免责条款确认框：未同意过，或已同意的版本落后于当前条款版本
    // （条款版本升级后须重新确认）。
    // requiredVersion 为空表示后端初始状态校验尚未完成，启动期间绝不提前弹窗。
    disclaimerNeedsAccept(state): boolean {
      if (!state.disclaimerRequiredVersion) return false;
      return !isDisclaimerVersionAccepted(state.disclaimerAcceptedVersion, state.disclaimerRequiredVersion);
    },

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

    // 当前会话排队中的消息（输入框上方排队条渲染源）
    currentMessageQueue(state): QueuedMessage[] {
      return (state.currentSessionName && state.messageQueues[state.currentSessionName]) || [];
    },

    // 顶部安全条可见性（设计 16.1 / brief 逐字状态表）：run 活动期才显示
    computerControlBarVisible(state): boolean {
      return COMPUTER_BAR_STATUSES.includes(state.computerRun.status);
    },

    // run 是否处于终态：驱动页签关闭入口与终态自动关闭计时
    computerRunTerminal(state): boolean {
      return COMPUTER_TERMINAL_STATUSES.includes(state.computerRun.status);
    },

    // 暂停/恢复按钮语义（设计 16.1）：starting/running 显示“暂停”，paused 显示“恢复”，
    // waiting_for_user / provider_safety_required / stopping / failed 只保留停止
    computerControlPausable(state): 'pause' | 'resume' | 'none' {
      if (state.computerRun.status === 'paused') return 'resume';
      if (state.computerRun.status === 'starting' || state.computerRun.status === 'running') return 'pause';
      return 'none';
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

    themeDirty(state): boolean {
      return JSON.stringify(state.guiThemeDraft) !== JSON.stringify(state.savedGuiTheme);
    },
  },

  actions: {
    /** 记录用户已接受免责条款；返回空串表示成功，否则为错误提示文案。 */
    async acceptDisclaimer(this: any): Promise<string> {
      const api = this._api();
      if (!api?.accept_disclaimer) return '无法连接本地服务，请重启软件后重试。';
      try {
        const res = await api.accept_disclaimer();
        if (res?.ok) {
          // 版本号以服务端回执为准：前端缓存旧版本时不得据此跳过再次确认
          this.disclaimerRequiredVersion = String(res.disclaimer_version || this.disclaimerRequiredVersion);
          this.disclaimerAcceptedVersion = String(res.accepted_version || '');
          return '';
        }
        return String(res?.msg || '保存同意记录失败，请重试。');
      } catch {
        return '保存同意记录失败，请重试。';
      }
    },

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
      runtime.streamingSpeed = this.streamingSpeed;
      // 模型选择随会话保存：切换会话时各会话恢复各自的模型，互不覆盖
      runtime.modelId = this.activeModelId;
      runtime.modelName = this.activeModelName;
      // 本回合用到的技能随会话保存
      runtime.usedSkills = this.usedSkills;
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
      this.streamingSpeed = runtime.streamingSpeed || 0;
      this.subagentTasks = runtime.subagentTasks;
      this.planData = runtime.planData || { title: '', items: [], progress: 0 };
      // 窗口口径 token 用量随会话切换：先恢复该会话上次快照避免闪烁，
      // 后端 token_usage 事件到达后再校正为最新值
      if (runtime.tokenUsage) this.tokenUsage = runtime.tokenUsage;
      // "已工作 xx秒"计时随会话切换恢复该会话的回合开始时间戳
      this.turnStartedAt = runtime.turnStartedAt || 0;
      // 本回合用到的技能随会话恢复
      this.usedSkills = runtime.usedSkills || [];
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
    // 缺失时：若当前会话处于运行态（如中途扫码接入），自愈补齐 running 占位卡片避免崩溃；
    // 若非运行态到达则严格抛出契约错误。
    activeAssistantOrThrow(): ChatMessage {
      let last = this.messages[this.messages.length - 1];
      if (!last || last.role !== 'assistant') {
        const isRunning = this.running || this.runningSessions.includes(this.currentSessionName);
        if (isRunning) {
          console.warn('[Harness] 活跃会话收到回合级事件时不存在活动助手消息，自动补齐 running 占位卡片');
          const placeholder: ChatMessage = {
            role: 'assistant',
            content: this.currentStreamingText,
            reasoning_content: this.currentThinkingText,
            tool_calls: [],
            legacy_diffs: [],
            execution_state: 'running',
          };
          this.messages.push(placeholder);
          last = placeholder;
        } else {
          throw new Error('桥接契约错误：回合级事件到达时不存在活动助手消息');
        }
      }
      return last;
    },

    // 恢复运行中回合的快照（中途扫码连入手机或切换到正在执行的会话）
    _restoreActiveTurnSnapshot(snap: any, targetSessionName: string = '') {
      if (!snap || typeof snap !== 'object') return;
      const sess = targetSessionName || snap.session_name || this.currentSessionName;
      const isCurrent = this.currentSessionName === sess;
      const runtime = this._getSessionRuntime(sess);
      const msgs = isCurrent ? this.messages : runtime.messages;

      // 1. 用户消息去重与补齐
      const userText = String(snap.display_text || snap.user_text || '').trim();
      if (userText) {
        const hasUser = msgs.some(m => m.role === 'user' && m.content === userText);
        if (!hasUser) {
          msgs.push({ role: 'user', content: userText });
        }
      }

      // 2. 助手卡片还原/补齐
      // 快照是「整回合」粒度：只有末尾气泡确实是当前正在执行的轮次（running）时才允许
      // 就地补齐；末尾是已完成气泡 / 非 assistant / 无消息时必须新建 running 气泡，
      // 否则整回合文本会被灌进已完成的旧气泡，后续 token_delta 继续往同一气泡堆。
      const lastMsg = msgs[msgs.length - 1];
      const runningMsg: ChatMessage | null =
        lastMsg && lastMsg.role === 'assistant' && lastMsg.execution_state === 'running' ? lastMsg : null;
      let assistantMsg: ChatMessage;
      const streamingText = String(snap.streaming_text || '');
      const thinkingText = String(snap.thinking_text || '');
      const toolCalls = Array.isArray(snap.tool_calls)
        ? snap.tool_calls.map((tc: any): ToolExecutionCall => ({
            id: String(tc.id || ''),
            name: String(tc.name || 'tool'),
            label: String(tc.label || tc.name || 'tool'),
            started_at_ms: tc.started_at_ms || Date.now(),
            display: normalizeToolDisplay(tc.display),
            result: (tc.summary || tc.result || tc.error) ? {
              ok: !!tc.ok,
              summary: String(tc.summary || ''),
              result: String(tc.result || tc.error || ''),
              duration_ms: tc.duration_ms !== undefined && tc.duration_ms !== null ? Number(tc.duration_ms) : undefined,
            } : undefined,
            diff: tc.diff,
          }))
        : [];

      if (!runningMsg) {
        assistantMsg = {
          role: 'assistant',
          content: streamingText,
          reasoning_content: thinkingText,
          tool_calls: toolCalls,
          legacy_diffs: [],
          execution_state: 'running',
        };
        msgs.push(assistantMsg);
      } else {
        assistantMsg = runningMsg;
        assistantMsg.execution_state = 'running';
        if (streamingText && (!assistantMsg.content || streamingText.length > assistantMsg.content.length)) {
          assistantMsg.content = streamingText;
        }
        if (thinkingText && (!assistantMsg.reasoning_content || thinkingText.length > assistantMsg.reasoning_content.length)) {
          assistantMsg.reasoning_content = thinkingText;
        }
        if (toolCalls.length > 0 && (!assistantMsg.tool_calls || assistantMsg.tool_calls.length === 0)) {
          assistantMsg.tool_calls = toolCalls;
        }
      }

      // 3. 恢复运行态与流式变量
      runtime.running = true;
      if (!this.runningSessions.includes(sess)) {
        this.runningSessions.push(sess);
      }
      if (isCurrent) {
        this.running = true;
        this.currentStreamingText = assistantMsg.content || streamingText;
        this.currentThinkingText = assistantMsg.reasoning_content || thinkingText;
        if (snap.started_at_ms) {
          this.turnStartedAt = Number(snap.started_at_ms);
        }
      }
    },

    // 把消息里的图片另存到用户选择的位置（后端弹原生"另存为"对话框，
    // 只接受本地媒体仓库 URL；取消不算错误，用 cancelled 标记区分）
    async saveMediaImage(url: string, suggestedName = '') {
      const api = this._api();
      if (!api) return { ok: false, msg: '后端连接失败，无法另存' };
      try {
        const res = await api.save_media_image(url, suggestedName);
        return res || { ok: false, msg: '另存失败：桥接无返回' };
      } catch (err) {
        this.connected = false;
        return { ok: false, msg: `另存失败：${err}` };
      }
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

    async _resyncAuthoritativeGuiTheme(this: any, failure: GuiThemeFailure): Promise<GuiThemeFailure> {
      const api = this._api();
      this.guiThemeError = failure.error;
      if (!api) return failure;
      try {
        const authoritative = await api.get_active_gui_theme();
        if (!authoritative.ok) {
          const recoveryFailure = guiThemeFailure({
            code: 'theme_recovery_failed',
            path: 'gui_theme_id',
            message: `${failure.error.message}；重新读取活动主题失败：${authoritative.error.message}`,
          });
          this.guiThemeError = recoveryFailure.error;
          return recoveryFailure;
        }
        this.savedGuiTheme = cloneGuiTheme(authoritative.theme);
        this.guiThemeDraft = cloneGuiTheme(authoritative.theme);
        this.nativeTitlebarColorCapability = authoritative.native_titlebar_color_capability;
        return failure;
      } catch (error) {
        const recovery = guiThemeFailure(error, 'gui_theme_id');
        const recoveryFailure = guiThemeFailure({
          code: 'theme_recovery_failed',
          path: 'gui_theme_id',
          message: `${failure.error.message}；重新读取活动主题失败：${recovery.error.message}`,
        });
        this.guiThemeError = recoveryFailure.error;
        return recoveryFailure;
      }
    },

    async _restorePreviousNativeTitlebar(
      this: any,
      theme: GuiTheme | null,
    ): Promise<GuiThemeResult> {
      const api = this._api();
      if (!api) return this._resyncAuthoritativeGuiTheme(
        guiThemeFailure(new Error('pywebview API 不可用'), 'native_titlebar'),
      );
      try {
        const result = theme
          ? await api.preview_native_titlebar(theme.native_titlebar)
          : await api.restore_native_titlebar();
        if (!result.ok) return this._resyncAuthoritativeGuiTheme(result);
        this.nativeTitlebarColorCapability = result.native_titlebar_color_capability;
        return { ok: true };
      } catch (error) {
        return this._resyncAuthoritativeGuiTheme(guiThemeFailure(error, 'native_titlebar'));
      }
    },

    _restoreGuiThemeDomSnapshot(rootStyle: string | null, light: boolean) {
      if (rootStyle === null) document.documentElement.removeAttribute('style');
      else document.documentElement.setAttribute('style', rootStyle);
      // light 类在 html 与 body 上成对维护（见 runtime.applyGuiTheme）
      document.documentElement.classList.toggle('light', light);
      document.body.classList.toggle('light', light);
    },

    async _applyGuiThemeCandidate(
      this: any,
      theme: GuiTheme,
      nativeMode: 'preview' | 'restore' | 'unchanged',
    ): Promise<GuiThemeResult<{ theme: GuiTheme }>> {
      const api = this._api();
      if (!api) return guiThemeFailure(new Error('pywebview API 不可用'));
      const previousTheme = this.guiTheme as GuiTheme | null;
      const previousHandle = this._appliedGuiTheme as AppliedTheme | null;
      const previousRootStyle = document.documentElement.getAttribute('style');
      const previousLight = document.body.classList.contains('light');
      let candidateHandle: AppliedTheme;
      try {
        candidateHandle = await applyGuiTheme(theme, api);
      } catch (error) {
        return guiThemeFailure(error);
      }

      try {
        if (nativeMode === 'preview') {
          const result = await api.preview_native_titlebar(theme.native_titlebar);
          if (!result.ok) {
            candidateHandle.dispose();
            this._restoreGuiThemeDomSnapshot(previousRootStyle, previousLight);
            const rollback = await this._restorePreviousNativeTitlebar(previousTheme);
            if (!rollback.ok) return rollback;
            return result;
          }
          this.nativeTitlebarColorCapability = result.native_titlebar_color_capability;
        } else if (nativeMode === 'restore') {
          const result = await api.restore_native_titlebar();
          if (!result.ok) {
            candidateHandle.dispose();
            this._restoreGuiThemeDomSnapshot(previousRootStyle, previousLight);
            const rollback = await this._restorePreviousNativeTitlebar(previousTheme);
            if (!rollback.ok) return rollback;
            return result;
          }
          this.nativeTitlebarColorCapability = result.native_titlebar_color_capability;
        }
      } catch (error) {
        candidateHandle.dispose();
        this._restoreGuiThemeDomSnapshot(previousRootStyle, previousLight);
        const rollback = await this._restorePreviousNativeTitlebar(previousTheme);
        if (!rollback.ok) return rollback;
        return guiThemeFailure(error, 'native_titlebar');
      }

      previousHandle?.dispose();
      this._appliedGuiTheme = candidateHandle;
      this.guiTheme = cloneGuiTheme(theme);
      this.themeLight = theme.appearance === 'light';
      return { ok: true, theme: cloneGuiTheme(theme) };
    },

    async _previewGuiTheme(this: any, theme: GuiTheme): Promise<GuiThemeActionResult> {
      const result = await this._applyGuiThemeCandidate(cloneGuiTheme(theme), 'preview');
      if (result.ok) this.guiThemeDraft = cloneGuiTheme(theme);
      return result;
    },

    previewGuiTheme(theme: GuiTheme): Promise<GuiThemeActionResult> {
      const requestedTheme = cloneGuiTheme(theme);
      return enqueueGuiThemeOperation(this, () => this._previewGuiTheme(requestedTheme));
    },

    async _cancelGuiThemePreview(this: any): Promise<GuiThemeActionResult> {
      const api = this._api();
      if (!api) return guiThemeFailure(new Error('pywebview API 不可用'));
      let restored;
      try {
        restored = await api.restore_native_titlebar();
      } catch (error) {
        return this._resyncAuthoritativeGuiTheme(guiThemeFailure(error, 'native_titlebar'));
      }
      if (!restored.ok) return this._resyncAuthoritativeGuiTheme(restored);

      const result = await this._applyGuiThemeCandidate(restored.theme, 'unchanged');
      if (!result.ok) return this._resyncAuthoritativeGuiTheme(result);
      this.nativeTitlebarColorCapability = restored.native_titlebar_color_capability;
      this.savedGuiTheme = cloneGuiTheme(restored.theme);
      this.guiThemeDraft = cloneGuiTheme(restored.theme);
      this.guiThemeError = null;
      return { ok: true, theme: cloneGuiTheme(restored.theme) };
    },

    cancelGuiThemePreview(): Promise<GuiThemeActionResult> {
      return enqueueGuiThemeOperation(this, () => this._cancelGuiThemePreview());
    },

    async _activateGuiThemeId(this: any, themeId: string): Promise<GuiThemeActionResult> {
      const api = this._api();
      if (!api) return guiThemeFailure(new Error('pywebview API 不可用'));
      const previousSaved = this.savedGuiTheme ? cloneGuiTheme(this.savedGuiTheme) : null;
      let activated;
      try {
        activated = await api.set_active_gui_theme(themeId);
      } catch (error) {
        return guiThemeFailure(error, 'gui_theme_id');
      }
      if (!activated.ok) return activated;

      const applied = await this._applyGuiThemeCandidate(activated.theme, 'unchanged');
      if (!applied.ok) {
        if (previousSaved && previousSaved.id !== activated.theme.id) {
          let rollback;
          try {
            rollback = await api.set_active_gui_theme(previousSaved.id);
          } catch (error) {
            const cause = guiThemeFailure(error, 'gui_theme_id');
            return this._resyncAuthoritativeGuiTheme(
              guiThemeFailure({
                code: 'theme_rollback_failed',
                path: 'gui_theme_id',
                message: `恢复原活动主题失败：${cause.error.message}`,
              }),
            );
          }
          if (!rollback.ok) return this._resyncAuthoritativeGuiTheme(rollback);
          this.savedGuiTheme = cloneGuiTheme(rollback.theme);
          this.nativeTitlebarColorCapability = rollback.native_titlebar_color_capability;
          return applied;
        }
        return this._resyncAuthoritativeGuiTheme(applied);
      }
      this.nativeTitlebarColorCapability = activated.native_titlebar_color_capability;
      this.savedGuiTheme = cloneGuiTheme(activated.theme);
      this.guiThemeDraft = cloneGuiTheme(activated.theme);
      this.guiThemeError = null;
      return activated;
    },

    activateGuiTheme(themeId: string): Promise<GuiThemeActionResult> {
      return enqueueGuiThemeOperation(this, () => this._activateGuiThemeId(themeId));
    },

    async _saveGuiThemeDraft(this: any, draft?: GuiTheme): Promise<GuiThemeActionResult> {
      // 编辑器可在仍有预览排队时直接保存最终草稿：显式传入的草稿优先于
      // 异步预览尚未回写的 store 草稿，保证快速输入的最后值不丢失。
      if (draft) this.guiThemeDraft = cloneGuiTheme(draft);
      if (!this.guiThemeDraft) return guiThemeFailure(new Error('没有可保存的主题草稿'));
      const api = this._api();
      if (!api) return guiThemeFailure(new Error('pywebview API 不可用'));
      const theme = cloneGuiTheme(this.guiThemeDraft);
      let saved;
      try {
        saved = await api.save_gui_theme(theme);
      } catch (error) {
        return guiThemeFailure(error);
      }
      if (!saved.ok) return saved;
      return this._activateGuiThemeId(theme.id);
    },

    saveGuiThemeDraft(draft?: GuiTheme): Promise<GuiThemeActionResult> {
      return enqueueGuiThemeOperation(this, () => this._saveGuiThemeDraft(draft));
    },

    resetInvalidGuiTheme(themeId: 'builtin.dark' | 'builtin.light'): Promise<GuiThemeActionResult> {
      return enqueueGuiThemeOperation(this, async () => {
        const result = await this._activateGuiThemeId(themeId);
        if (result.ok) this.guiThemeError = null;
        return result;
      });
    },

    toggleTheme(): Promise<GuiThemeActionResult> {
      return enqueueGuiThemeOperation(this, async () => {
        const result = await this._activateGuiThemeId(
          this.themeLight ? 'builtin.dark' : 'builtin.light',
        );
        if (result.ok) this.guiThemeError = null;
        return result;
      });
    },

    // ── 主题库管理（Task 6）：组件只调用这些 action，不直接触碰桥 ──

    async _refreshGuiThemeSummaries(this: any): Promise<GuiThemeListResult> {
      const api = this._api();
      if (!api) return guiThemeFailure(new Error('pywebview API 不可用'), 'gui_themes');
      let result;
      try {
        result = await api.list_gui_themes();
      } catch (error) {
        return guiThemeFailure(error, 'gui_themes');
      }
      if (!result.ok) return result;
      this.guiThemes = result.themes || [];
      this.guiThemeListError = null;
      return result;
    },

    // 列表刷新失败只进 guiThemeListError（非阻断），绝不写 guiThemeError。
    refreshGuiThemes(): Promise<GuiThemeListResult> {
      return enqueueGuiThemeOperation(this, async () => {
        const result = await this._refreshGuiThemeSummaries();
        if (!result.ok) this.guiThemeListError = result.error;
        return result;
      });
    },

    loadGuiTheme(themeId: string): Promise<GuiThemeActionResult> {
      return enqueueGuiThemeOperation(this, async () => {
        const api = this._api();
        if (!api) return guiThemeFailure(new Error('pywebview API 不可用'));
        try {
          const result = await api.get_gui_theme(themeId);
          if (!result.ok) return result;
          return { ok: true, theme: result.theme };
        } catch (error) {
          return guiThemeFailure(error);
        }
      });
    },

    copyGuiTheme(sourceId: string, newId: string, newName: string): Promise<GuiThemeActionResult> {
      return enqueueGuiThemeOperation(this, async () => {
        const api = this._api();
        if (!api) return guiThemeFailure(new Error('pywebview API 不可用'));
        let result;
        try {
          result = await api.copy_gui_theme(sourceId, newId, newName);
        } catch (error) {
          return guiThemeFailure(error);
        }
        if (!result.ok) return result;
        await this._refreshGuiThemeSummaries();
        return { ok: true, theme: result.theme };
      });
    },

    renameGuiTheme(themeId: string, newId: string, newName: string): Promise<GuiThemeActionResult> {
      return enqueueGuiThemeOperation(this, async () => {
        const api = this._api();
        if (!api) return guiThemeFailure(new Error('pywebview API 不可用'));
        let result;
        try {
          result = await api.rename_gui_theme(themeId, newId, newName);
        } catch (error) {
          return guiThemeFailure(error);
        }
        if (!result.ok) return result;
        // 活动主题被重命名时同步三份主题快照的 ID/名称，保留其余草稿内容。
        for (const key of ['guiTheme', 'savedGuiTheme', 'guiThemeDraft'] as const) {
          const snapshot = this[key] as GuiTheme | null;
          if (snapshot && snapshot.id === themeId) {
            snapshot.id = newId;
            snapshot.name = newName;
          }
        }
        await this._refreshGuiThemeSummaries();
        return { ok: true, theme: result.theme };
      });
    },

    deleteGuiTheme(themeId: string): Promise<GuiThemeResult> {
      return enqueueGuiThemeOperation(this, async () => {
        const api = this._api();
        if (!api) return guiThemeFailure(new Error('pywebview API 不可用'));
        let result;
        try {
          result = await api.delete_gui_theme(themeId);
        } catch (error) {
          return guiThemeFailure(error);
        }
        if (!result.ok) return result;
        await this._refreshGuiThemeSummaries();
        return { ok: true };
      });
    },

    // ZIP 导入：文件选择对话框由后端桥负责；取消对话框返回 dialog_cancelled。
    importGuiTheme(): Promise<GuiThemeActionResult> {
      return enqueueGuiThemeOperation(this, async () => {
        const api = this._api();
        if (!api) return guiThemeFailure(new Error('pywebview API 不可用'));
        let result;
        try {
          result = await api.import_gui_theme();
        } catch (error) {
          return guiThemeFailure(error, 'source');
        }
        if (!result.ok) return result;
        await this._refreshGuiThemeSummaries();
        return { ok: true, theme: result.theme };
      });
    },

    exportGuiTheme(themeId: string): Promise<GuiThemePathResult> {
      return enqueueGuiThemeOperation(this, async () => {
        const api = this._api();
        if (!api) return guiThemeFailure(new Error('pywebview API 不可用'));
        try {
          return await api.export_gui_theme(themeId);
        } catch (error) {
          return guiThemeFailure(error, 'destination');
        }
      });
    },

    // 分区背景图：只做桥调用并返回 asset 路径，草稿与预览由调用方（编辑器）驱动。
    importGuiThemeImage(themeId: string, region: ThemeRegionId): Promise<GuiThemeAssetResult> {
      return enqueueGuiThemeOperation(this, async () => {
        const api = this._api();
        if (!api) return guiThemeFailure(new Error('pywebview API 不可用'));
        try {
          return await api.import_gui_theme_image(themeId, region);
        } catch (error) {
          return guiThemeFailure(error, 'source');
        }
      });
    },

    removeGuiThemeImage(themeId: string, region: ThemeRegionId): Promise<GuiThemeActionResult> {
      return enqueueGuiThemeOperation(this, async () => {
        const api = this._api();
        if (!api) return guiThemeFailure(new Error('pywebview API 不可用'));
        let result;
        try {
          result = await api.remove_gui_theme_image(themeId, region);
        } catch (error) {
          return guiThemeFailure(error);
        }
        if (!result.ok) return result;
        return { ok: true, theme: result.theme };
      });
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

    // 对话类提示：写入消息流（notice 角色），不自动消失，双端同步可见。
    // 经桥 post_notice 广播到全部客户端（桌面 GUI + 局域网手机端），各端
    // （含发起端）收到 notice 桥事件后统一回环插入，本地不直插、无重复；
    // 桥不可用（离线预览）或调用失败时降级为本地插入。
    notifyChat(msg: string) {
      this.broadcastChatNotice(msg, 'notice');
    },

    // 仅本端可见的本地插入：后端桥事件（notice 等）的落点与离线兜底路径。
    // 桥事件处理器必须走这里，不能再调 notifyChat/broadcastChatNotice，
    // 否则会把已广播的事件回环再广播，造成各端重复插入。
    // sessionName 缺省为当前会话；目标会话不是当前会话时写入其 runtime，
    // 但该会话 runtime 为空（本端从未加载）时直接丢弃——否则 length>0 会
    // 阻断打开会话时的数据库历史回填，只剩一条提示。
    // 回合进行中 notice 必须插到 running assistant 之前：push 到末尾会破坏
    // "回合中最后一条是 assistant" 的事件契约（后续 tool_finish 会失配）。
    notifyChatLocal(msg: string, kind: 'notice' | 'error' = 'notice', sessionName?: string) {
      const target = sessionName || this.currentSessionName;
      let msgs: ChatMessage[];
      if (target === this.currentSessionName) {
        msgs = this.messages;
      } else {
        const runtime = this._getSessionRuntime(target);
        if (runtime.messages.length === 0) return;
        msgs = runtime.messages;
      }
      const last = msgs[msgs.length - 1];
      if (kind === 'notice' && last && last.role === 'assistant' && last.execution_state === 'running') {
        msgs.splice(msgs.length - 1, 0, { role: 'notice', content: msg });
      } else {
        msgs.push({ role: kind, content: msg });
      }
    },

    // 双端同步的对话提示：经桥 post_notice 广播到全部客户端（桌面 GUI + 手机端），
    // 各端（含发起端）收到 notice 桥事件后统一回环插入，本地不再直插，避免重复。
    // 桥不可用（离线预览）或调用失败时降级为本地插入。
    broadcastChatNotice(msg: string, kind: 'notice' | 'error' = 'notice', sessionName?: string) {
      const target = sessionName || this.currentSessionName;
      const api = this._api();
      if (!api || typeof api.post_notice !== 'function') {
        this.notifyChatLocal(msg, kind, target);
        return;
      }
      Promise.resolve(api.post_notice(msg, target, kind)).catch(() => {
        this.notifyChatLocal(msg, kind, target);
      });
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
      this.themeLight = document.body.classList.contains('light');
      try {
        const init = await api.get_initial_state();
        this.guiThemes = init.gui_themes || [];
        this.guiThemeError = init.gui_theme_error || null;
        this.guiThemeListError = init.gui_theme_list_error || null;
        this.nativeTitlebarColorCapability = init.native_titlebar_color_capability || 'unknown';
        if (!this.guiThemeError && init.gui_theme) {
          const startupTheme = cloneGuiTheme(init.gui_theme);
          const applied = await enqueueGuiThemeOperation(
            this,
            () => this._applyGuiThemeCandidate(startupTheme, 'unchanged'),
          );
          if (applied.ok) {
            this.savedGuiTheme = cloneGuiTheme(init.gui_theme);
            this.guiThemeDraft = cloneGuiTheme(init.gui_theme);
          } else {
            this.guiThemeError = applied.error;
          }
        }
        this.models = init.models || [];
        this.providers = init.providers || [];
        this.activeModelId = init.active_model_id || '';
        this.activeModelName = init.active_model_name || '';
        this.secondaryModelId = init.secondary_model_id || '';
        this.secondaryModelRoles = init.secondary_model_roles || { vision: true, compression: true, subagent: true };
        this.permissionMode = init.permission_mode || 'confirm';
        this.sandboxEnabled = !!(init as any).allow_outside_workspace;
        if (init.system_settings) {
          this.systemSettings = init.system_settings;
        }
        // 人设设置：智能体显示名与对用户的称呼（启动快照随 init 一起下发）
        const personaInit = (init as any).persona_settings;
        if (personaInit && typeof personaInit === 'object') {
          this.agentName = String(personaInit.agent_name || '星瑶');
          this.userTitle = String(personaInit.user_title || '');
          this.syncDocumentTitle();
        }
        // 免责条款同意状态：未同意过（或已同意版本落后）时弹确认框，
        // 同意后由 acceptDisclaimer 就地更新状态，弹窗随之卸载
        const disclaimerInit = (init as any).disclaimer;
        if (disclaimerInit && typeof disclaimerInit === 'object') {
          const backendVer = String(disclaimerInit.disclaimer_version || '');
          this.disclaimerRequiredVersion = backendVer || DISCLAIMER_VERSION;
          this.disclaimerAcceptedVersion = String(disclaimerInit.accepted_version || '');
        } else {
          this.disclaimerRequiredVersion = DISCLAIMER_VERSION;
        }
        if (init.reasoning) this.reasoning = init.reasoning;
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
        // 排队消息随初始状态恢复：窗口重载期间 message_queue 事件已丢失，
        // 后端在初始状态里带回所有会话的队列快照
        this.messageQueues = ((init as any).message_queues || {}) as Record<string, QueuedMessage[]>;
        for (const name of activeSessions) {
          this._getSessionRuntime(name).running = true;
        }
        this.running = !!this.currentSessionName && activeSessions.includes(this.currentSessionName);
        this._captureCurrentRuntime();
        // 活跃回合快照还原：手机扫码接入或前端重载时，还原当前及后台会话正在执行的思考、流式正文与工具卡片
        const snapshots = (init as any).active_turn_snapshots || {};
        const curSnap = (init as any).active_turn_snapshot || snapshots[this.currentSessionName];
        if (curSnap) {
          this._restoreActiveTurnSnapshot(curSnap, this.currentSessionName);
        }
        for (const [sName, snap] of Object.entries(snapshots)) {
          if (sName !== this.currentSessionName) {
            this._restoreActiveTurnSnapshot(snap, sName);
          }
        }
        this._captureCurrentRuntime();
        for (const g of this.sessionGroups) {
          const hit = (g.sessions || []).find((s: any) => s.name === this.currentSessionName);
          if (hit) {
            this.currentSessionDisplayName = hit.display_name || hit.name;
            break;
          }
        }
        this.fileTree = (init as any).file_tree || [];
        this.tokenUsage = normalizeTokenUsage(init.token_usage);
        this.version = init.version || '';
        this.connected = true;
        // 启动后静默检查更新（不打扰用户）
        this.checkUpdate(false);
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
        const api = this._api();
        if (api) {
          try {
            const res = await api.get_reasoning_snapshot(modelId);
            if (res && res.ok && this.activeModelId === modelId) {
              this.reasoning = res.reasoning;
            }
          } catch (err) {
            this.connected = false;
            this.notifyChat('读取模型思考设置失败: ' + err);
          }
        }
        return;
      }
      const api = this._api();
      if (!api) return;
      // 版本号在请求发出前递增：switchSession 在途响应若携带旧模型快照，
      // 凭版本号变化跳过覆盖，保住用户刚做的选择
      this._sessionModelVersion[targetSession] = (this._sessionModelVersion[targetSession] || 0) + 1;
      try {
        const res = await api.set_model(modelId, targetSession);
        if (res && res.ok) {
          const runtime = this._getSessionRuntime(targetSession);
          runtime.modelId = res.active_model_id || modelId;
          runtime.modelName = res.active_model_name || this.activeModelName;
          // 仅当用户仍停留在原会话时才更新顶层显示，避免覆盖切换后会话的状态栏
          if (this.currentSessionName === targetSession) {
            this.activeModelId = runtime.modelId!;
            this.activeModelName = runtime.modelName!;
            this.reasoning = res.reasoning;
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

    async setReasoningChoice(choice: ReasoningChoice) {
      const targetModelId = this.activeModelId;
      const requestVersion = (this._reasoningChoiceVersion[targetModelId] || 0) + 1;
      this._reasoningChoiceVersion[targetModelId] = requestVersion;
      const previous: ReasoningSnapshot = {
        ...this.reasoning,
        options: this.reasoning.options.map(option => ({ ...option })),
      };
      this.reasoning = { ...this.reasoning, choice };
      const api = this._api();
      if (!api) {
        this.reasoning = previous;
        return;
      }
      try {
        const res = await api.set_reasoning_choice(targetModelId, choice);
        if (
          this.activeModelId !== targetModelId
          || this._reasoningChoiceVersion[targetModelId] !== requestVersion
        ) return;
        if (res && res.ok) {
          this.reasoning = res.reasoning;
          // 空会话插提示会让欢迎页消失（表现为页面跳转），仅会话已有真实对话时才提示
          if (this.messages.some(m => m.role === 'user' || m.role === 'assistant')) {
            this.notifyChat(res.msg || ('思考设置已调整: ' + this.reasoning.choice));
          }
        } else {
          this.reasoning = previous;
          this.notifyChat('思考设置调整失败: ' + (res && res.msg ? res.msg : '未知错误'));
        }
      } catch (err) {
        if (
          this.activeModelId !== targetModelId
          || this._reasoningChoiceVersion[targetModelId] !== requestVersion
        ) return;
        this.reasoning = previous;
        this.connected = false;
        this.notifyChat('思考设置调整失败: ' + err);
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
          // 空会话插提示会让欢迎页消失（表现为页面跳转），仅会话已有真实对话时才提示
          if (this.messages.some(m => m.role === 'user' || m.role === 'assistant')) {
            this.notifyChat('权限模式已调整: ' + formatPermMode(this.permissionMode));
          }
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
          // 空会话插提示会让欢迎页消失（表现为页面跳转），仅会话已有真实对话时才提示
          if (this.messages.some(m => m.role === 'user' || m.role === 'assistant')) {
            this.notifyChat(
              this.sandboxEnabled ? '沙盒已解除：允许访问工作目录外文件' : '沙盒已开启：仅限工作目录内'
            );
          }
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

    async refreshSystemSettings() {
      const api = this._api();
      if (!api) return null;
      try {
        const settings = await api.get_system_settings();
        this.systemSettings = settings;
        return settings;
      } catch (err) {
        this.connected = false;
        this.notifyModal('系统设置', '加载系统设置失败: ' + err);
        return null;
      }
    },

    // 人设设置（名称/称呼）保存后即时同步全局显示；快照由 PersonaSettings 页传入
    applyPersonaSettings(snapshot: { agent_name?: string; user_title?: string }) {
      if (!snapshot || typeof snapshot !== 'object') return;
      if (snapshot.agent_name) this.agentName = String(snapshot.agent_name);
      this.userTitle = String(snapshot.user_title || '');
      this.syncDocumentTitle();
    },

    // 页面标题跟随智能体显示名（webview/浏览器预览均可见；默认名时保持 Y-CODE 简洁形态）
    syncDocumentTitle() {
      const name = this.agentName || '星瑶';
      document.title = name === '星瑶' ? 'Y-CODE' : `Y-CODE · ${name}`;
    },

    async setCommandShell(shellId: string): Promise<boolean> {
      const previous = this.systemSettings;
      const api = this._api();
      if (!api) return false;
      try {
        const result = await api.set_command_shell(shellId);
        if (result && result.ok) {
          this.systemSettings = result.settings;
          return true;
        }
        this.systemSettings = result?.settings || previous;
        this.notifyModal('系统设置', '命令解释器设置失败: ' + (result?.msg || '未知错误'));
        return false;
      } catch (err) {
        this.systemSettings = previous;
        this.connected = false;
        this.notifyModal('系统设置', '命令解释器设置失败: ' + err);
        return false;
      }
    },

    async setRemoteTokenTtl(seconds: number): Promise<boolean> {
      const previous = this.systemSettings;
      const api = this._api();
      if (!api) return false;
      try {
        const result = await api.set_remote_token_ttl(seconds);
        if (result && result.ok) {
          this.systemSettings = result.settings;
          return true;
        }
        this.systemSettings = result?.settings || previous;
        this.notifyModal('系统设置', '远程控制 token 过期时间设置失败: ' + (result?.msg || '未知错误'));
        return false;
      } catch (err) {
        this.systemSettings = previous;
        this.connected = false;
        this.notifyModal('系统设置', '远程控制 token 过期时间设置失败: ' + err);
        return false;
      }
    },

    async setAgentTurnStepLimit(value: number): Promise<boolean> {
      const previous = this.systemSettings;
      const api = this._api();
      if (!api) return false;
      try {
        const result = await api.set_agent_turn_step_limit(value);
        if (result && result.ok) {
          this.systemSettings = result.settings;
          return true;
        }
        this.systemSettings = result?.settings || previous;
        this.notifyModal('系统设置', '单任务最大模型轮次设置失败: ' + (result?.msg || '未知错误'));
        return false;
      } catch (err) {
        this.systemSettings = previous;
        this.connected = false;
        this.notifyModal('系统设置', '单任务最大模型轮次设置失败: ' + err);
        return false;
      }
    },

    // ── 电脑操控系统设置（Task 12）：只以后端返回的 settings 快照覆盖本地
    //    状态，不做乐观持久值；失败时后端快照本身就是回滚值 ──

    async setBrowserControlEnabled(enabled: boolean): Promise<boolean> {
      const api = this._api();
      if (!api) return false;
      try {
        const result = await api.set_browser_control_enabled(enabled);
        if (result && result.settings) this.systemSettings = result.settings;
        if (!result || !result.ok) {
          this.notifyModal('电脑操控', '浏览器操控设置失败: ' + (result?.msg || '未知错误'));
          return false;
        }
        return true;
      } catch (err) {
        this.connected = false;
        this.notifyModal('电脑操控', '浏览器操控设置失败: ' + err);
        return false;
      }
    },

    async setComputerControlEnabled(enabled: boolean): Promise<boolean> {
      const api = this._api();
      if (!api) return false;
      try {
        const result = await api.set_computer_control_enabled(enabled);
        if (result && result.settings) this.systemSettings = result.settings;
        if (!result || !result.ok) {
          this.notifyModal('电脑操控', '完整电脑操控设置失败: ' + (result?.msg || '未知错误'));
          return false;
        }
        return true;
      } catch (err) {
        this.connected = false;
        this.notifyModal('电脑操控', '完整电脑操控设置失败: ' + err);
        return false;
      }
    },

    async setComputerBrowser(browser: string): Promise<boolean> {
      const api = this._api();
      if (!api) return false;
      try {
        const result = await api.set_computer_browser(browser);
        if (result && result.settings) this.systemSettings = result.settings;
        if (!result || !result.ok) {
          this.notifyModal('电脑操控', '默认浏览器设置失败: ' + (result?.msg || '未知错误'));
          return false;
        }
        return true;
      } catch (err) {
        this.connected = false;
        this.notifyModal('电脑操控', '默认浏览器设置失败: ' + err);
        return false;
      }
    },

    // ── 电脑操控运行态（Task 13b，设计 16.4）：
    //    纯事件 reducer + Bridge 快照校准 + 暂停/恢复/停止控制 ──

    // 四类平铺信封事件的纯 reducer：只接受当前 run 且 event_seq 严格大于已记录值；
    // 旧 run 的迟到事件按 run_id 拒绝。run 归属两路建立：Store 尚无 run 时首个
    // 事件直接认领（brief 样例：无 starting 前导的 action 事件流也能建 run），
    // 已有 run 时只有 computer_state/starting 开启新 run 并清空轨迹。
    // action_ordinal 只表示动作顺序，不承担状态事件排序
    reduceComputerEvent(event: ComputerControlEvent) {
      const raw = event as unknown as Record<string, unknown>;
      const type = String(raw.type || '');
      if (!COMPUTER_EVENT_TYPES.has(type)) return;
      const runId = String(raw.run_id || '');
      const seq = Number(raw.event_seq) || 0;
      if (!runId) return;
      // 已关闭 run 的迟到事件一律丢弃：清空后 Store 无 run，若无此门闩
      // reducer 的「首个事件直接认领」分支会把旧事件当成新 run
      if (runId === this._computerClosedRunId) return;
      const currentRunId = this.computerRun.run_id;
      const isNewRun = runId !== currentRunId && (
        currentRunId === ''
        || (type === 'computer_state' && String(raw.status || '') === 'starting')
      );
      if (!isNewRun) {
        if (runId !== currentRunId) return;
        if (seq <= this._computerEventSeq) return;
      }
      this._computerEventSeq = seq;
      if (isNewRun) {
        this.computerRun = {
          ...emptyComputerRun(),
          run_id: runId,
          status: type === 'computer_state' ? String(raw.status || '') || 'starting' : 'starting',
        };
        this.computerTrace = [];
        this._computerActionStartMs = {};
      }
      if (type === 'computer_state') {
        const status = String(raw.status || '');
        const target = String(raw.target || '');
        this.computerRun = {
          ...this.computerRun,
          status: status || this.computerRun.status,
          target: target || this.computerRun.target,
        };
        return;
      }
      if (type === 'computer_observation') {
        // observation 只带 loopback token URL 与当前窗口/页面摘要，无截图内容
        const previewUrl = String(raw.preview_url || '');
        const surface = String(raw.origin_or_window || '');
        this.computerRun = {
          ...this.computerRun,
          ...(previewUrl ? { preview_url: previewUrl } : {}),
          ...(surface ? { current_surface: surface } : {}),
        };
        return;
      }
      // computer_action_started / computer_action_finished：轨迹行 upsert
      const ordinal = Number(raw.action_ordinal) || 0;
      if (ordinal <= 0) return;
      if (type === 'computer_action_started') {
        this._computerActionStartMs[ordinal] = Date.now();
        this.computerRun = {
          ...this.computerRun,
          current_action: String(raw.action_kind || ''),
          action_count: Math.max(this.computerRun.action_count, ordinal),
        };
        this._upsertComputerTrace(ordinal, computerTraceSummary(raw), 'running', 0, '');
        return;
      }
      const ok = raw.ok !== false;
      const startedAt = this._computerActionStartMs[ordinal];
      delete this._computerActionStartMs[ordinal];
      const durationMs = typeof raw.duration_ms === 'number' && raw.duration_ms >= 0
        ? raw.duration_ms
        : (startedAt ? Math.max(0, Date.now() - startedAt) : 0);
      this._upsertComputerTrace(
        ordinal,
        computerTraceSummary(raw),
        ok ? 'done' : 'failed',
        durationMs,
        String(raw.error_code || ''),
      );
    },

    // 轨迹行 upsert：行内只有五个契约字段（绝不透传事件原始字段），新行插到
    // 最前（新到旧），超过 200 条时丢弃最旧（尾部）
    _upsertComputerTrace(
      ordinal: number,
      summary: string,
      status: string,
      durationMs: number,
      errorCode: string,
    ) {
      const row: ComputerActionTrace = {
        action_ordinal: ordinal,
        summary,
        status,
        duration_ms: durationMs,
        error_code: errorCode,
      };
      const idx = this.computerTrace.findIndex(r => r.action_ordinal === ordinal);
      if (idx >= 0) {
        this.computerTrace.splice(idx, 1, row);
      } else {
        this.computerTrace.unshift(row);
      }
      if (this.computerTrace.length > COMPUTER_TRACE_MAX) {
        this.computerTrace.length = COMPUTER_TRACE_MAX;
      }
    },

    // 关闭「操控」面板（用户点 × 或终态自动关闭共用）：
    // 先把当前 run_id 记入黑名单，再清空运行态与内存轨迹
    clearComputerRun() {
      const runId = this.computerRun.run_id;
      if (!runId) return;
      this._computerClosedRunId = runId;
      this.computerRun = emptyComputerRun();
      this.computerTrace = [];
      this._computerActionStartMs = {};
      this._computerEventSeq = 0;
    },

    // Bridge 快照校准（设计 16.4）：_js_queue 满时允许丢新事件，终态不能只依赖
    // 事件推送——控制操作返回值、窗口重新聚焦、切到操控页签都走这里复核。
    // 快照发现不同 run_id 时按新 run 重置轨迹与记账；同一 run 只合并已知字段，
    // 已记录的 event_seq 门闩不下调（迟到旧事件仍被拒绝）
    _applyComputerSnapshot(raw: Record<string, unknown>) {
      const runId = String(raw.run_id || '');
      if (!runId) return;
      // 已关闭 run 的快照不得复活运行态（新 run_id 不受影响）
      if (runId === this._computerClosedRunId) return;
      if (runId !== this.computerRun.run_id) {
        this.computerRun = { ...emptyComputerRun(), run_id: runId };
        this.computerTrace = [];
        this._computerActionStartMs = {};
        this._computerEventSeq = 0;
      }
      const patch: Partial<ComputerRunSnapshot> = {};
      for (const field of COMPUTER_SNAPSHOT_FIELDS) {
        if (raw[field] !== undefined) {
          (patch as Record<string, unknown>)[field] = raw[field];
        }
      }
      this.computerRun = { ...this.computerRun, ...patch };
    },

    // 拉取当前 run 完整快照校准 Store：窗口重新聚焦 / 切到操控页签时调用
    async refreshComputerRun(): Promise<ComputerRunSnapshot | null> {
      const api = this._api();
      if (!api) return null;
      try {
        const res = await api.get_computer_control_status();
        if (res && res.run_id) this._applyComputerSnapshot(res as unknown as Record<string, unknown>);
        return this.computerRun;
      } catch (err) {
        this.connected = false;
        this.notifyModal('电脑操控', '操控状态刷新失败: ' + err);
        return null;
      }
    },

    async pauseComputerControl(): Promise<boolean> {
      return this._computerControlMutation('pause_computer_control', '暂停操控');
    },

    async resumeComputerControl(): Promise<boolean> {
      return this._computerControlMutation('resume_computer_control', '恢复操控');
    },

    async stopComputerControl(): Promise<boolean> {
      return this._computerControlMutation('stop_computer_control', '停止操控');
    },

    // 三个控制的统一入口：run_id 取当前 Store（旧 UI 事件的 run id 由后端拒绝），
    // 成功与失败的返回值都携带完整快照，直接校准 Store 防终态永久丢失
    async _computerControlMutation(
      method: 'pause_computer_control' | 'resume_computer_control' | 'stop_computer_control',
      label: string,
    ): Promise<boolean> {
      const api = this._api();
      if (!api) return false;
      const runId = this.computerRun.run_id;
      if (!runId) {
        this.notifyModal('电脑操控', label + '失败：当前没有进行中的操控会话');
        return false;
      }
      try {
        const res = await api[method](runId);
        if (res && res.run_id) this._applyComputerSnapshot(res as unknown as Record<string, unknown>);
        if (!res || !res.ok) {
          this.notifyModal('电脑操控', label + '失败: ' + (res?.msg || res?.error_code || '未知错误'));
          return false;
        }
        return true;
      } catch (err) {
        this.connected = false;
        this.notifyModal('电脑操控', label + '失败: ' + err);
        return false;
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
        } else if (!runtime.running && res.messages && res.messages.length > 0) {
          // 同步最新的 error_note 与 execution_state（如后台会话已落库或附带了错误标注）
          const lastRes = res.messages[res.messages.length - 1];
          const lastLocal = runtime.messages[runtime.messages.length - 1];
          if (lastRes && lastRes.error_note && lastLocal && lastLocal.role === 'assistant') {
            lastLocal.error_note = lastRes.error_note;
            lastLocal.execution_state = 'error';
          }
        }

        // 目标会话运行态恢复：优先用后端下发的实时流式快照完整还原（正文/思考/工具卡片），
        // 若后端无快照但处于 runningSessions，则回退构造空 running 占位防止事件挂载报错。
        if (res.active_turn_snapshot) {
          this._restoreActiveTurnSnapshot(res.active_turn_snapshot, sessionName);
        } else if (this.runningSessions.includes(sessionName)) {
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

        // 排队中的消息随切换响应恢复：切换到该会话即可见排队条目
        // （后端在该响应里带回该会话的队列快照）
        if (Array.isArray(res.message_queue)) {
          this.messageQueues[sessionName] = res.message_queue as QueuedMessage[];
        } else {
          delete this.messageQueues[sessionName];
        }

        // 会话级模型隔离：切到已有会话时恢复该会话绑定的模型，
        // 避免 A 会话切到 B 会话后 A 的模型被全局覆盖。
        // 版本号变化 = 在途期间用户又切了模型，跳过本次恢复（本地已是新值）
        if (res.active_model_id && (this._sessionModelVersion[sessionName] || 0) === modelVersion) {
          this.activeModelId = res.active_model_id;
          this.activeModelName = res.active_model_name || this.activeModelName;
          this.reasoning = res.reasoning;
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
      const inheritedModelId = this.activeModelId;
      const inheritedModelName = this.activeModelName;
      this._captureCurrentRuntime();
      this.pendingWorkdir = 'GLOBAL';
      this.currentSessionName = '';
      this.currentSessionDisplayName = '新对话';
      this._bindSessionRuntime('');
      // 新会话继承用户当前窗口正在使用的模型。pending 会话尚未落库，
      // 首条消息发送时通过 draftModelId 一并提交，不能回退到全局默认模型。
      const runtime = this._getSessionRuntime('');
      runtime.modelId = inheritedModelId;
      runtime.modelName = inheritedModelName;
      this.activeModelId = inheritedModelId;
      this.activeModelName = inheritedModelName;
      this.draftModelId = inheritedModelId;
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
      const inheritedModelId = this.activeModelId;
      const inheritedModelName = this.activeModelName;
      this._captureCurrentRuntime();
      this.pendingWorkdir = targetWorkdir;
      this.currentSessionName = '';
      this.currentSessionDisplayName = '新会话';
      this._bindSessionRuntime('');
      const runtime = this._getSessionRuntime('');
      runtime.modelId = inheritedModelId;
      runtime.modelName = inheritedModelName;
      this.activeModelId = inheritedModelId;
      this.activeModelName = inheritedModelName;
      this.draftModelId = inheritedModelId;
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
          // 排队消息随会话删除一并清理（后端已同步清空，此处防快照事件丢失残留）
          delete this.messageQueues[sessionName];
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
      // 触摸设备（手机/平板）：原生文件夹对话框只在电脑屏幕弹出，手机端
      // 走内置目录浏览器（browse_remote_dirs + select_workspace_by_path）
      if (window.matchMedia && window.matchMedia('(pointer: coarse)').matches) {
        this.openRemoteDirBrowser();
        return;
      }
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

    // ---- 手机端内置目录浏览器（引入新项目工作区） ----
    async openRemoteDirBrowser() {
      this.remoteDirBrowser.show = true;
      this.remoteDirBrowser.loading = true;
      this.remoteDirBrowser.error = '';
      this.remoteDirBrowser.dirs = [];
      this.remoteDirBrowser.selected = '';
      await this.browseRemoteDirs('');
    },

    async browseRemoteDirs(path: string) {
      const api = this._api();
      if (!api) return;
      this.remoteDirBrowser.loading = true;
      this.remoteDirBrowser.error = '';
      try {
        const res = await (api as any).browse_remote_dirs(path);
        if (res && res.ok) {
          this.remoteDirBrowser.current = res.current || '';
          this.remoteDirBrowser.isRoot = !!res.is_root;
          this.remoteDirBrowser.dirs = res.dirs || [];
        } else {
          this.remoteDirBrowser.error = (res && res.msg) || '目录读取失败';
        }
      } catch (err) {
        this.remoteDirBrowser.error = String(err);
      } finally {
        this.remoteDirBrowser.loading = false;
      }
    },

    closeRemoteDirBrowser() {
      this.remoteDirBrowser.show = false;
    },

    async confirmRemoteWorkspace(useCurrent = false) {
      const api = this._api();
      if (!api) return;
      // 未勾选时允许直接把当前浏览目录作为工作区（"使用当前目录"按钮）
      const path = this.remoteDirBrowser.selected
        || (useCurrent ? this.remoteDirBrowser.current : '');
      if (!path) return;
      try {
        const res = await (api as any).select_workspace_by_path(path);
        if (res && res.ok && res.workdir) {
          this.workdir = res.workdir;
          this.currentWorkdirLabel = res.workdir.split('\\').pop() || res.workdir;
          if (res.file_tree) { this.fileTree = res.file_tree; this.fileTreeExpanded = []; }
          if (res.sessions) this.sessionGroups = res.sessions;
          if (res.skills) this.skills = res.skills;
          this.remoteDirBrowser.show = false;
          this.notifyModal('项目分组', '已成功引入项目文件夹: ' + res.workdir);
        } else {
          this.notifyModal('项目分组', '引入项目文件夹失败: ' + (res && res.msg ? res.msg : '未知错误'));
        }
      } catch (err) {
        this.notifyModal('项目分组', '引入项目文件夹失败: ' + err);
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
          this.broadcastChatNotice('中断请求未成功送达', 'error', sessionName);
        } else {
          this.notifyChat('已中断当前回合');
        }
      } catch (err) {
        clearTimeout(stopFallbackTimer);
        this.connected = false;
        this.broadcastChatNotice('中断请求失败: ' + err, 'error', sessionName);
      }
    },

    // 取消一条排队中的消息：仅从队列移除，不影响正在执行的回合。
    // 队列条目的移除由后端 message_queue 快照事件驱动，本地不增量删
    async cancelQueuedMessage(sessionName: string, messageId: string) {
      const api = this._api();
      if (!api) return;
      try {
        const res = await api.cancel_queued_message(sessionName, messageId);
        if (!res || !res.ok) {
          this.notifyChat('取消排队失败: ' + (res && res.msg ? res.msg : '未知错误'));
        }
      } catch (err) {
        this.connected = false;
        this.notifyChat('取消排队失败: ' + err);
      }
    },

    // 插队发送：把排队消息移到队首并停止当前回合，回合结束后优先补发它
    // （其余排队消息顺位后移）。停止与补发均由后端完成，前端只透传意图
    async jumpQueue(sessionName: string, messageId: string) {
      const api = this._api();
      if (!api) return;
      try {
        const res = await api.jump_queue(sessionName, messageId);
        if (!res || !res.ok) {
          this.notifyChat('插队发送失败: ' + (res && res.msg ? res.msg : '未知错误'));
        }
      } catch (err) {
        this.connected = false;
        this.notifyChat('插队发送失败: ' + err);
      }
    },

    async sendMessage(text: string, attachments?: string[]) {
      // 生成中不再拦截发送：后端把消息加入该会话排队（FIFO），回合结束后
      // 自动补发。本地跳过乐观渲染——排队消息还没真正进入回合，用户气泡
      // 与 running 占位等补发回合的 turn_start 到达时再渲染；排队条目由
      // 后端 message_queue 快照事件驱动显示在输入框上方
      const willQueue = this.runningSessions.includes(this.currentSessionName);
      const attachList = (attachments || []).filter(p => p && p.trim());
      if (!text.trim() && !attachList.length) return;
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
        if (!willQueue) {
          this._setSessionRunning(sessionName, true);
          this._bindSessionRuntime(sessionName);
          // 乐观渲染：串行排队时后端 turn_start 要等拿到 _turn_workdir_lock 才发，
          // 期间新会话若无任何气泡会显示为一片空白（看似"没发送"）。
          // 发送成功后立即渲染用户消息 + running 占位；turn_start 到达时去重占位，
          // 用户消息已有则不重复添加。（willQueue 时整段跳过：排队消息由
          // message_queue 快照渲染排队条目，气泡等补发回合的 turn_start）
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
        }
        const res = await api.send_user_message(
          text.trim(),
          sessionName,
          attachList,
          pendingDir,
          pendingDir ? (this.draftModelId || undefined) : (this.activeModelId || undefined),
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
          // willQueue 时该会话原有回合仍在跑（入队失败不影响它），
          // 不能清除运行态，否则停止按钮消失、重复发送不再入队
          if (!willQueue) this._setSessionRunning(sessionName, false);
          if (pendingDir) {
            // 先回退幽灵会话再广播：runtime 已迁回草稿 key，
            // notice 事件回环插入才能落到草稿视图（否则目标会话 runtime 为空被丢弃）
            revertGhostSession();
            this.broadcastChatNotice('消息发送失败: ' + (res && res.msg ? res.msg : '未知错误'), 'error');
          } else {
            this.broadcastChatNotice('消息发送失败: ' + (res && res.msg ? res.msg : '未知错误'), 'error', sessionName);
            if (this.currentSessionName === sessionName) {
              this._bindSessionRuntime(sessionName);
            }
          }
        }
        // 成功时不主动改 running，由后续 turn_start/turn_finish/turn_cancelled 等事件驱动
      } catch (err) {
        // #13 同上：catch 分支也只向目标会话 runtime 写入错误，
        // 仅用户仍在该会话时才恢复绑定（willQueue 时原有回合仍在跑，不清运行态）
        if (!willQueue) this._setSessionRunning(sessionName, false);
        this.connected = false;
        if (pendingDir) {
          // 同上：幽灵会话先回退再广播，保证回环插入落到草稿视图
          revertGhostSession();
          this.broadcastChatNotice('消息发送失败: ' + err, 'error');
        } else {
          this.broadcastChatNotice('消息发送失败: ' + err, 'error', sessionName);
          if (this.currentSessionName === sessionName) {
            this._bindSessionRuntime(sessionName);
          }
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

    // ── frameless 关闭确认（现代三选框） ──
    async _handleCloseRequested(this: any, hasRunningWork: boolean, trayAvailable: boolean) {
      const api = this._api();
      if (!api) return;
      // 没有回合在跑：直接退出（与原生 closing 无确认路径一致）
      if (!hasRunningWork) {
        try { await api.window_confirm_close('exit'); } catch (error) { console.warn('关闭失败', error); }
        return;
      }
      // 有回合：弹现代三选框，选择结果交回后端执行
      this.closeConfirm = { show: true, trayAvailable };
    },

    async resolveCloseConfirm(this: any, choice: 'exit' | 'background' | 'cancel') {
      this.closeConfirm.show = false;
      if (choice === 'cancel') return;
      const api = this._api();
      if (!api) return;
      try {
        await api.window_confirm_close(choice);
      } catch (error) {
        console.warn('关闭确认执行失败', error);
        this.notifyModal('关闭失败', '窗口关闭失败：' + error);
      }
    },

    handleBridgeEvent(event: BridgeEvent) {
      const type = event.type;
      const data = event.data || {};
      // 只要有事件推送过来，说明桥接链路是通的
      this.connected = true;

      // 电脑操控事件是平铺信封（top-level run_id + event_seq，Task 13a 契约），
      // 与既有 {"type","data"} 信封不同：先识别分流给纯 reducer，
      // 不参与会话 runtime 归属，处理完直接返回
      if (isComputerFlatEvent(event)) {
        this.reduceComputerEvent(event as ComputerControlEvent);
        return;
      }

      const currentRuntimeKey = this._runtimeKey();
      // 拖放文件路径推送（GUI 线程死锁修复后改走 _js_queue 事件管线）：
      // 转发给 useAttachments 挂在 window 上的匹配逻辑，不参与会话 runtime 归属
      if (type === 'dropped_paths_push') {
        const push = (window as unknown as { __ycode_on_drop_paths?: (p: unknown) => void }).__ycode_on_drop_paths;
        if (push) push(data);
        return;
      }
      // frameless 自绘标题栏关闭请求：弹现代确认框（替代 win32 原生 MessageBox），
      // 不参与会话 runtime 归属逻辑，处理完直接返回
      if (type === 'close_requested') {
        void this._handleCloseRequested(!!data.has_running_work, !!data.tray_available);
        return;
      }
      // frameless 最大化状态变更（原生双击/Snap/拖拽还原路径）：同步到 store
      if (type === 'window_maximize_state') {
        this.windowMaximized = !!data.maximized;
        return;
      }
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
        // 本回合技能收集重置：从这一刻起的 is_skill 工具调用归入本回合
        this.usedSkills = [];
        this.streamingSpeed = 0;
        speedTracker.reset();
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
          this.notifyChatLocal('后台子代理任务已全部结束，正在自动汇总结果');
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
        // 轮次边界一律清零流式累积变量：即使尾部混入 error/notice 等非 assistant 消息，
        // 新气泡也不能继承上一轮正文（否则整回合正文会被塞进同一个气泡）。
        this.currentStreamingText = '';
        this.currentThinkingText = '';
        this.streamingSpeed = 0;
        speedTracker.reset();
        const last = this.messages[this.messages.length - 1];
        if (last && last.role === 'assistant' &&
            ((last.reasoning_content && last.reasoning_content.trim()) ||
             (last.content && last.content.trim()) ||
             (last.tool_calls && last.tool_calls.length))) {
          last.execution_state = 'completed';
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
        this.streamingSpeed = speedTracker.addChunk(data.text || '');
        let last = this.messages[this.messages.length - 1];
        if (!last || last.role !== 'assistant') {
          const placeholder: ChatMessage = {
            role: 'assistant',
            content: this.currentStreamingText,
            reasoning_content: this.currentThinkingText,
            tool_calls: [],
            legacy_diffs: [],
            execution_state: 'running',
          };
          this.messages.push(placeholder);
          last = placeholder;
        } else {
          last.reasoning_content = this.currentThinkingText;
        }
      } else if (type === 'token_delta') {
        this.currentStreamingText += data.text || '';
        this.streamingSpeed = speedTracker.addChunk(data.text || '');
        let last = this.messages[this.messages.length - 1];
        if (!last || last.role !== 'assistant') {
          const placeholder: ChatMessage = {
            role: 'assistant',
            content: this.currentStreamingText,
            reasoning_content: this.currentThinkingText,
            tool_calls: [],
            legacy_diffs: [],
            execution_state: 'running',
          };
          this.messages.push(placeholder);
          last = placeholder;
        } else {
          last.content = this.currentStreamingText;
        }
      } else if (type === 'message_final') {
        // 自愈校正：流式 delta 经 evaluate_js 投递可能静默丢失（表现为回复尾部
        // 缺一段），后端在 turn_finish 前补发最后一条助手消息全文。仅在全文更长
        // （确有尾部缺失）时覆盖——正常路径长度相等，是无害 no-op，绝不回退内容。
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
          // 媒体清单独立于文本长度门控：非空即挂（流式回合的媒体画廊在收尾补齐）
          const finalMedia = (data as { media?: MediaItem[] }).media;
          if (finalMedia && finalMedia.length) {
            last.media = finalMedia;
          }
        }
      } else if (type === 'step_metrics') {
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
      } else if (type === 'tool_start') {
        const d = data as ToolStartEventData;
        const msg = this.activeAssistantOrThrow();
        const calls = msg.tool_calls ?? (msg.tool_calls = []);
        const id = String(d.id || '');
        const display = normalizeToolDisplay(d.display);
        // 技能调用收集：display.is_skill 时把技能名记入本回合 usedSkills（去重），
        // 供状态栏"技能"项与回合结束后的统计展示
        if (display?.is_skill && display.target && !this.usedSkills.includes(display.target)) {
          this.usedSkills.push(display.target);
        }
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
        if (Array.isArray(data.items)) {
          const terminalStatuses = new Set(['completed', 'done', 'x', 'failed', 'blocked', 'cancelled']);
          const queued = data.items
            .filter((it: any) => it?.execution_mode === 'takeover_queued' && !terminalStatuses.has(String(it?.status || '')))
            .sort((a: any, b: any) => {
              const sequenceDiff = Number(a?.takeover_sequence || 0) - Number(b?.takeover_sequence || 0);
              return sequenceDiff || String(a?.id || '').localeCompare(String(b?.id || ''));
            });
          const queuePositions = new Map(
            queued.map((it: any, index: number) => [String(it?.id || ''), index + 1]),
          );
          const items = data.items.map((it: any) => ({
            id: it.id || String(Math.random()),
            // 后端计划项的正文字段是 subject（见 core/plan.py snapshot）
            text: it.subject || it.text || it.title || '',
            status: it.status === 'completed' || it.status === 'done' || it.status === 'x' ? 'done' : (it.status === 'in_progress' || it.status === '/' ? 'in_progress' : 'todo'),
            execution_mode: String(it.execution_mode || ''),
            owner: String(it.owner || ''),
            takeover_required: Boolean(it.takeover_required),
            handoff_reason: String(it.handoff_reason || ''),
            takeover_sequence: Number(it.takeover_sequence || 0),
            handoff_note: terminalStatuses.has(String(it.status || ''))
              ? undefined
              : it.execution_mode === 'main_takeover'
                ? '子 Agent 已超时，主 Agent 正在接管并核查现场'
                : it.execution_mode === 'takeover_queued'
                  ? `等待主 Agent 接管（队列第 ${queuePositions.get(String(it.id || '')) || 1} 项）`
                  : undefined,
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
      } else if (type === 'plan_item_handoff') {
        // 子 Agent 超时接管可见性：只更新本会话计划项的执行模式/owner，
        // 不改变调度状态（真实状态唯一来源仍是 PlanManager/TaskScheduler）；
        // 旧子 Agent 卡片保持 timed_out，计划卡片显示 main_takeover，
        // 两者不得合并成 completed
        const handoffItem = (data.item || {}) as any;
        const handoffId = String(handoffItem.id || '');
        if (handoffId) {
          const handoffMode = String(data.mode || handoffItem.execution_mode || '');
          const queuePos = Number(data.queue_position || 0);
          const handoffNote = handoffMode === 'takeover_queued'
            ? (queuePos > 0
              ? `等待主 Agent 接管（队列第 ${queuePos} 项）`
              : '等待主 Agent 接管')
            : '子 Agent 已超时，主 Agent 正在接管并核查现场';
          const items = Array.isArray(this.planData.items) ? this.planData.items : [];
          const hidx = items.findIndex((i: any) => i.id === handoffId);
          const patched = {
            execution_mode: handoffMode,
            owner: String(handoffItem.owner || ''),
            takeover_required: Boolean(handoffItem.takeover_required),
            handoff_reason: String(handoffItem.handoff_reason || ''),
            takeover_sequence: Number(handoffItem.takeover_sequence || 0),
            handoff_note: handoffNote,
          };
          if (hidx >= 0) {
            items[hidx] = { ...items[hidx], ...patched };
          } else {
            items.push({
              id: handoffId,
              text: String(handoffItem.subject || handoffId),
              status: 'in_progress',
              ...patched,
            } as any);
          }
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
          permissionOptions: Array.isArray(data.permission_options)
            ? data.permission_options.map((option: any) => ({
              key: String(option?.key || ''),
              label: String(option?.label || ''),
            })).filter((option: { key: string; label: string }) => option.key && option.label)
            : [],
          functionName: String(data.function_name || ''),
        };
        this.permRequest.show = this.permRequest.sessionName === this.currentSessionName;
        // 非当前窗口等待用户确认 → 黄点
        this._setSessionStatusDot(eventSession, 'yellow');
      } else if (type === 'permission_dismiss') {
        // 后端权限队列已清空（请求被超时拒绝/取消）：收起内联权限卡片，避免死请求残留。
        // 必须整体重置：仅 show=false 会残留 reqId/sessionName，
        // switchSession 重显逻辑会把已失效的卡片复活，点击后报“权限响应发送失败”
        this.permRequest = { show: false, sessionName: '', reqId: '', summary: '', source: '', allowAlways: false, options: [], permissionOptions: [], functionName: '' };
      } else if (type === 'message_queue') {
        // 排队消息全量快照：幂等替换该会话队列。后端在入队/取消/插队/补发
        // 后都会推送，前端不做增量合并，天然免疫事件乱序与丢失漂移
        const qSession = String(data.session_name || eventSession || '');
        const rawQueue = Array.isArray(data.queue) ? data.queue : [];
        const normalized: QueuedMessage[] = (rawQueue as any[])
          .map((m: any) => ({
            id: String((m && m.id) || ''),
            text: String((m && m.text) || ''),
            attachments: Array.isArray(m && m.attachments) ? m.attachments.map(String) : [],
          }))
          .filter((m: QueuedMessage) => !!m.id);
        if (qSession) {
          if (normalized.length) {
            this.messageQueues[qSession] = normalized;
          } else {
            delete this.messageQueues[qSession];
          }
        }
      } else if (type === 'notice') {
        // 后端桥事件 notice：写入对应会话的对话流（双端同步的统一回环落点）。
        // 注意必须走本地插入，不能再调 broadcastChatNotice，否则会回环再广播。
        if (data.message) {
          const kind = data.kind === 'error' ? 'error' : 'notice';
          const sessionName = data.session_name ? String(data.session_name) : undefined;
          this.notifyChatLocal(String(data.message), kind, sessionName);
        }
      } else if (type === 'active_session_changed') {
        // 多端会话同步：电脑端切换了活动会话，手机端若处于非该会话，点亮提醒点以提示用户
        const remoteSession = String(data.session_name || '');
        if (remoteSession && remoteSession !== this.currentSessionName) {
          this._setSessionStatusDot(remoteSession, 'yellow');
        }
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
        this.streamingSpeed = 0;
        speedTracker.reset();
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
        this.streamingSpeed = 0;
        speedTracker.reset();
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
        const errorMsg = String(data.message || data.error || '');
        this.running = false;
        this.streamingSpeed = 0;
        speedTracker.reset();
        let last = this.messages[this.messages.length - 1];
        if (!last || last.role !== 'assistant') {
          // 末尾不是 assistant 时（如中途接入、缺少占位或提问后立即报错），
          // 补齐带 execution_state='error' 与 error_note 的助手卡片，确保各端必能看到报错
          const placeholder: ChatMessage = {
            role: 'assistant',
            content: this.currentStreamingText,
            reasoning_content: this.currentThinkingText,
            tool_calls: [],
            legacy_diffs: [],
            execution_state: 'error',
            error_note: errorMsg,
          };
          this.messages.push(placeholder);
          last = placeholder;
        } else {
          last.execution_state = reduceExecutionState(last.execution_state, 'turn_error');
          if (errorMsg) {
            if (!last.error_note || !last.error_note.split('\n').includes(errorMsg)) {
              last.error_note = last.error_note ? last.error_note + '\n' + errorMsg : errorMsg;
            }
          }
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
      } else if (type === 'model_changed') {
        // 其他端（手机/桌面）切换了模型：更新对应会话的模型与思考强度，
        // 并递增版本号使在途 switchSession 的旧模型快照不再回覆盖。
        // 事件管线已把 store 绑定到事件会话：同会话直接改顶层（finally 捕获
        // 回 runtime）；跨会话则先把原会话模型钉进原 runtime（finally 只捕获
        // 目标会话），顶层临时切到事件模型，重绑回原会话时精确还原显示
        {
          const newId = String(data.model_id || '');
          const newName = String(data.model_name || '');
          if (switchedRuntime) {
            const origRuntime = this._getSessionRuntime(currentRuntimeKey);
            origRuntime.modelId = this.activeModelId;
            origRuntime.modelName = this.activeModelName;
            if (newId) this.activeModelId = newId;
            if (newName) this.activeModelName = newName;
          } else {
            if (newId) this.activeModelId = newId;
            if (newName) this.activeModelName = newName;
            if (data.reasoning) this.reasoning = data.reasoning;
          }
          if (newId || newName) {
            this._sessionModelVersion[eventSession] = (this._sessionModelVersion[eventSession] || 0) + 1;
          }
        }
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
          ...(Number.isFinite(d.added) ? { added: Number(d.added) } : {}),
          ...(Number.isFinite(d.removed) ? { removed: Number(d.removed) } : {}),
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
          this.notifyChatLocal(`后台子代理${label}${desc}`);
        } else {
          // 与 error 事件一致：回合进行中不插队，挂在当前助手消息上展示
          const last = this.messages[this.messages.length - 1];
          if (last && last.role === 'assistant') {
            last.error_note = `后台子代理${label}${desc}`;
            this.notifyChatLocal(`后台子代理${label}${desc}`);
          } else {
            this.messages.push({ role: 'error', content: `后台子代理${label}${desc}` });
          }
        }
      } else if (type === 'background_task_finished') {
        // 后台命令任务（execute_command 的 run_in_background，如打包）完成通知
        const code = Number(data.returncode);
        const tid = String(data.task_id || '');
        const statusText = code === 0 ? '已完成' : `已结束（退出码 ${code}）`;
        this.notifyChatLocal(`后台任务 ${tid} ${statusText}`);
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
      } else if (type === 'update_download_progress') {
        this.updateState.status = 'downloading';
        this.updateState.downloadProgress = Number(data.percent) || 0;
        this.updateState.downloadedBytes = Number(data.downloaded) || 0;
        this.updateState.totalBytes = Number(data.total) || 0;
      } else if (type === 'update_download_complete') {
        this.updateState.status = 'downloaded';
        this.updateState.downloadProgress = 100;
        this.updateState.installerPath = String(data.installer_path || '');
        this.handleDownloadComplete();
      } else if (type === 'update_download_error') {
        this.updateState.status = 'error';
        this.updateState.errorMessage = String(data.message || '下载失败');
        this.notifyModal('更新下载失败', `下载安装包时出错：${data.message || '未知错误'}`);
      } else if (type === 'error') {
        const msgText = String(data.message || '运行错误');
        const last = this.messages[this.messages.length - 1];
        if (last && last.role === 'assistant') {
          // 避免与 turn_error 重复追加完全相同的错误文本
          if (!last.error_note || !last.error_note.split('\n').includes(msgText)) {
            last.error_note = last.error_note ? last.error_note + '\n' + msgText : msgText;
          }
          this.notifyChatLocal(msgText, 'notice', eventSession);
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
          this.broadcastChatNotice('权限响应发送失败' + detail, 'error', sessionName);
        }
      } catch (err) {
        this.connected = false;
        this.broadcastChatNotice('权限响应发送失败: ' + err, 'error', sessionName);
      } finally {
        // 应答过的请求立即作废本地 reqId（保留新到的请求）：否则 switchSession 的
        // 重显逻辑会把已应答的卡片复活，再次点击必然报“已失效或已被处理”
        if (this.permRequest.reqId === reqId) {
          this.permRequest.reqId = '';
          this.permRequest.sessionName = '';
        }
      }
    },

    async respondPermissionOption(optionKey: string) {
      const reqId = this.permRequest.reqId;
      const sessionName = this.permRequest.sessionName;
      this.permRequest.show = false;
      if (!reqId) return;
      const api = this._api();
      if (!api) return;
      try {
        const res = await api.respond_permission_option(reqId, optionKey);
        if (!res?.ok) {
          this._getSessionRuntime(sessionName).messages.push({
            role: 'error',
            content: '权限响应发送失败: ' + (res?.msg || '未知错误'),
          });
        }
      } catch (err) {
        this.connected = false;
        this._getSessionRuntime(sessionName).messages.push({
          role: 'error',
          content: '权限响应发送失败: ' + err,
        });
      } finally {
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
          this.broadcastChatNotice('选项响应发送失败' + detail, 'error', sessionName);
        }
      } catch (err) {
        this.connected = false;
        this.broadcastChatNotice('选项响应发送失败: ' + err, 'error', sessionName);
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

    async refreshSkills(): Promise<boolean> {
      const api = this._api();
      if (!api) return false;
      try {
        if (api.refresh_skills) {
          const res = await api.refresh_skills();
          if (res && res.ok && Array.isArray(res.skills)) {
            this.skills = res.skills;
            return true;
          }
        }
        if (api.get_skills) {
          const skills = await api.get_skills();
          if (Array.isArray(skills)) {
            this.skills = skills;
            return true;
          }
        }
        return false;
      } catch (err) {
        this.notifyModal('技能', '刷新技能列表失败: ' + err);
        return false;
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

    async trustPlugin(name: string) {
      const api = this._api();
      if (!api) return;
      try {
        const res = await api.trust_plugin(name);
        if (res && res.ok) {
          this.plugins = res.plugins || this.plugins;
          this.notifyModal('插件', res.msg || '插件已信任');
        } else {
          this.notifyModal('插件', '插件信任失败: ' + (res && res.msg ? res.msg : '未知错误'));
        }
      } catch (err) {
        this.connected = false;
        this.notifyModal('插件', '插件信任失败: ' + err);
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
          this.usedSkills = [];
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
        `此操作将撤销${this.agentName}最近一次的文件改动`,
        `撤销${this.agentName}上一次的文件修改？`,
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

    // 附件缩略图：批量归档图片并取受控媒体 URL；失败静默返回空 Map（增强能力）
    async fetchAttachmentThumbnails(paths: string[]): Promise<Map<string, string>> {
      const result = new Map<string, string>();
      const api = this._api();
      if (!api || !paths.length) return result;
      try {
        const res = await api.get_attachment_thumbnails(paths);
        if (res && res.ok && Array.isArray(res.items)) {
          for (const it of res.items) {
            if (it && it.path && it.url) result.set(String(it.path), String(it.url));
          }
        }
      } catch (err) {
        console.error('获取附件缩略图失败:', err);
      }
      return result;
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

    // 手机/平板端经 WS RPC 上传文件（base64），返回落盘路径（失败返回空串并 toast）
    async uploadFile(name: string, dataB64: string): Promise<string> {
      const api = this._api();
      if (!api) return '';
      try {
        const res = await api.upload_file(name, dataB64);
        if (res && res.ok && res.path) return res.path;
        this.notifyChat('上传失败: ' + (res && res.msg ? res.msg : '未知错误'));
        return '';
      } catch (err) {
        this.notifyChat('上传失败: ' + err);
        return '';
      }
    },

    async checkPathsKind(paths: string[]) {
      const api = this._api();
      if (!api) return [];
      try {
        return (await api.check_paths_kind(paths)) || [];
      } catch {
        return [];
      }
    },

    async openFilePreview(path: string) {
      this.filePreview = { show: true, path, kind: 'text', content: '', dataUrl: '', truncated: false, loading: true };
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
          if (res.kind === 'image') {
            // 图片：base64 data URL 直接交 <img> 渲染
            this.filePreview.kind = 'image';
            this.filePreview.dataUrl = res.data_url || '';
          } else {
            this.filePreview.content = res.content || '';
            this.filePreview.truncated = !!res.truncated;
          }
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
      // 释放图片 base64（大图可达数 MB 字符串）
      this.filePreview.dataUrl = '';
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
      // active_model_id 兜底而非覆盖：GUI 的激活模型按会话绑定，快照值即使
      // 来自旧版后端也只是全局默认，可能属于另一厂商的同名模型；仅当当前
      // 会话模型为空或已从列表中删除时才采纳，否则界面选中态会跳厂商
      const stillValid = !!this.activeModelId
        && this.models.some(m => m.id === this.activeModelId);
      if (!stillValid) {
        this.activeModelId = snapshot.active_model_id || this.activeModelId;
      }
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

    async syncPresetModels(presetId: string) {
      const api = this._api();
      if (!api) return null;
      try {
        const res = await api.sync_preset_models(presetId);
        if (res?.ok && res.settings) {
          this.applyModelSettings(res.settings);
          this.notifyModal('供应商', res.msg || '预置模型已同步');
          return res;
        }
        this.notifyModal('供应商', '同步预置模型失败: ' + (res?.msg || '未知错误'));
        return null;
      } catch (err) {
        this.connected = false;
        this.notifyModal('供应商', '同步预置模型失败: ' + err);
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

    async getProviderApiKey(providerId: string): Promise<string | null> {
      const api = this._api();
      if (!api) return null;
      try {
        const res = await api.get_provider_api_key(providerId);
        if (res?.ok) return res.api_key || '';
        this.notifyModal('供应商', '读取 API Key 失败: ' + (res?.msg || '未知错误'));
        return null;
      } catch (err) {
        this.connected = false;
        this.notifyModal('供应商', '读取 API Key 失败: ' + err);
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

    async addModel(providerId: string, modelName: string, contextWindow: number, supportsVision = false, supportsFunctionCalling = false, computerProtocol: ComputerProtocolChoice = '') {
      const api = this._api();
      if (!api) return null;
      try {
        const res = await api.add_model(providerId, modelName, contextWindow, supportsVision, supportsFunctionCalling, computerProtocol);
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

    async updateModel(providerId: string, modelId: string, modelName: string, contextWindow: number, supportsVision: boolean, supportsFunctionCalling = false, computerProtocol: ComputerProtocolChoice = '') {
      const api = this._api();
      if (!api) return null;
      try {
        const res = await api.update_model(providerId, modelId, modelName, contextWindow, supportsVision, supportsFunctionCalling, computerProtocol);
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

    // ============================================================================
    // 软件更新（Gitee Releases 联动）
    // ============================================================================
    openUpdateModal() {
      this.updateState.showUpdateModal = true;
      // 打开弹窗时刷新一次更新信息：后端会顺带探测本地缓存安装包，
      // 已下载过的包直接呈现为"已就绪"。复用静默检查（沿用其冷却窗口
      // 与在途去重），不会重复请求、也不会再次打开弹窗。
      void this.checkUpdate(false);
    },

    closeUpdateModal() {
      this.updateState.showUpdateModal = false;
    },

    hasActiveTasks(): boolean {
      const busyTurns = this.running || this.runningSessions.length > 0;
      const busyTasks = Array.isArray(this.backgroundTasks) && this.backgroundTasks.some(t => t.status === 'running');
      return busyTurns || busyTasks;
    },

    async checkUpdate(manual: boolean = false) {
      const api = this._api();
      if (!api) return;

      // 静默检查防抖：距上次检查不足冷却窗口则跳过，避免崩溃循环/频繁重启
      // 反复请求 Gitee API 触发限流；手动检查不受限制
      const UPDATE_CHECK_COOLDOWN_MS = 3600 * 1000;
      if (!manual) {
        // 在途去重：已有静默/手动检查正在跑时不重复发请求（如启动静默检查
        // 与打开更新弹窗几乎同时触发）
        if (this.updateState.status === 'checking') return;
        try {
          const last = Number(localStorage.getItem('ycode-last-update-check') || '0');
          if (last && Date.now() - last < UPDATE_CHECK_COOLDOWN_MS) {
            return;
          }
        } catch {}
      }

      this.updateState.status = 'checking';
      try {
        const res = await api.check_update(manual);
        try { localStorage.setItem('ycode-last-update-check', String(Date.now())); } catch {}
        if (res && res.ok) {
          this.updateState.hasUpdate = !!res.has_update;
          this.updateState.latestVersion = res.latest_version || '';
          this.updateState.currentVersion = res.current_version || this.version || '';
          this.updateState.releaseTitle = res.release_title || '';
          this.updateState.releaseNotes = res.release_notes || '';
          this.updateState.downloadUrl = res.download_url || '';
          this.updateState.assetName = res.asset_name || '';
          this.updateState.assetSize = Number(res.asset_size || 0);
          // 分卷信息：非空时下载走分卷拼接路径；SHA256 由后端下载完成后强制校验
          this.updateState.partUrls = Array.isArray(res.part_urls) ? res.part_urls.map(String) : [];
          this.updateState.sha256 = String(res.sha256 || '');
          this.updateState.status = res.has_update ? 'available' : 'idle';

          // 缓存命中：后端在更新缓存目录里找到了目标版本安装包且校验通过，
          // 直接进入"已就绪"态（不触发下载），"立即安装并重启"即安装该包
          const cachedInstaller = String(res.cached_installer || '');
          if (res.has_update && cachedInstaller) {
            this.updateState.status = 'downloaded';
            this.updateState.installerPath = cachedInstaller;
            this.updateState.downloadProgress = 100;
            this.updateState.downloadedBytes = this.updateState.assetSize;
            this.updateState.totalBytes = this.updateState.assetSize;
          }

          if (res.has_update && manual) {
            this.openUpdateModal();
          } else if (!res.has_update && manual) {
            this.notifyModal('检查更新', `当前已是最新版本 (v${this.version || res.current_version || '1.2.5'})`);
          }
        } else {
          this.updateState.status = 'idle';
          if (manual) {
            this.notifyModal('检查更新', res?.msg || '检查更新失败，请检查网络');
          }
        }
      } catch (err) {
        this.updateState.status = 'idle';
        if (manual) {
          this.notifyModal('检查更新', '检查更新失败：' + err);
        }
      }
    },

    async startDownloadUpdate() {
      const api = this._api();
      // 分卷模式下 downloadUrl 是 Release 页面链接（非直链），启动条件放宽为
      // "有直链或有分卷列表"二者其一即可
      if (!api) return;
      if (!this.updateState.downloadUrl && this.updateState.partUrls.length === 0) return;

      this.updateState.status = 'downloading';
      this.updateState.downloadProgress = 0;
      this.updateState.downloadedBytes = 0;

      try {
        const res = await api.download_update(
          this.updateState.downloadUrl,
          this.updateState.latestVersion,
          this.updateState.partUrls,
          this.updateState.sha256,
          this.updateState.assetSize,
        );
        if (!res || !res.ok) {
          this.updateState.status = 'available';
          this.notifyModal('下载失败', res?.msg || '无法启动下载任务');
        }
      } catch (err) {
        this.updateState.status = 'available';
        this.notifyModal('下载失败', '启动下载异常：' + err);
      }
    },

    async cancelDownloadUpdate() {
      const api = this._api();
      if (!api) return;

      try {
        await api.cancel_download_update();
        this.updateState.status = 'available';
        this.updateState.downloadProgress = 0;
      } catch {}
    },

    async handleDownloadComplete() {
      const busy = this.hasActiveTasks();
      if (!busy) {
        const ok = await this.askConfirm(
          '新版本已就绪',
          `Y-CODE v${this.updateState.latestVersion} 安装包下载完成`,
          '安装包已下载就绪，是否立即启动安装程序并重启软件？',
          '立即安装 ⏎'
        );
        if (ok) {
          await this.launchInstaller();
        }
      }
    },

    async handleInstallClick() {
      if (!this.updateState.installerPath) {
        this.openUpdateModal();
        return;
      }

      const busy = this.hasActiveTasks();
      if (busy) {
        const ok = await this.askConfirm(
          '任务进行中警告',
          '当前仍有对话或后台任务正在执行中',
          '继续安装将关闭当前软件并中断正在执行的任务。是否确定现在立即安装？',
          '中断并安装'
        );
        if (ok) {
          await this.launchInstaller();
        }
      } else {
        const ok = await this.askConfirm(
          '启动安装',
          `准备安装 Y-CODE v${this.updateState.latestVersion}`,
          '是否立即启动安装向导并退出当前程序？',
          '立即安装 ⏎'
        );
        if (ok) {
          await this.launchInstaller();
        }
      }
    },

    async launchInstaller() {
      const api = this._api();
      if (!api || !this.updateState.installerPath) return;

      this.updateState.status = 'installing';
      try {
        const res = await api.launch_installer(this.updateState.installerPath);
        if (!res || !res.ok) {
          this.updateState.status = 'downloaded';
          this.notifyModal('安装失败', res?.msg || '无法唤起安装向导');
        }
      } catch (err) {
        this.updateState.status = 'downloaded';
        this.notifyModal('安装失败', '唤起安装程序异常：' + err);
      }
    },
  },
});
