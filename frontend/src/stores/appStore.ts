import { defineStore } from 'pinia';
// Git 工作台事件转发（git_state_changed）；gitStore 不反向依赖 appStore，无环
import { useGitStore } from './gitStore';
import type {
  AutomationRunInfo,
  AutomationTaskInfo,
  ModelInfo,
  ProviderInfo,
  ApiProtocolInfo,
  PresetProviderInfo,
  ModelSettingsSnapshot,
  SessionGroup,
  ArchivedSessionInfo,
  ArchiveBatchResult,
  SkillInfo,
  McpInfo,
  PluginInfo,
  HookInfo,
  FileNode,
  BridgeEvent,
  BackgroundVideoInfo,
  PlanData,
  BackgroundTask,
  TokenUsage,
  ContextBreakdownItem,
  SessionTokenTotals,
  TokenUsageOverview,
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
  TurnUndoStateSummary,
  TurnUndoPreviewPayload,
  TurnUndoExecutePayload,
  TurnUndoRecoveryPreviewPayload,
  TurnUndoRecoveryExecutePayload,
} from '../types/pywebview';
import { emitPtyData, emitPtyExit } from '../utils/ptyBus';
import { isMarkdownFilePath, toFileUrl } from '../utils/localPath';
import { fileExt } from '../utils/fileIcon';
import { buildMarkdownPreviewHtml } from '../utils/previewHtml';
// 前端构建产物运行期更新 → 资源版本失配自愈（详见 utils/frontendUpdate.ts）
import { requestFrontendReload } from '../utils/frontendUpdate';
import type { MediaItem } from '../types/media';
import type { AttachmentItem } from '../types/composer';
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
  computeRestoredRatioWidth,
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

/**
 * 右面板宽度启动恢复（视口比例语义）：记忆的是“占视口宽度的比例”，
 * 按当前视口换算成像素（兼容旧版像素记忆），并夹取到
 * [min, min(配置上限, 视口 50%)]；未自定义或存储不可用回默认宽度。
 * 窗口缩放时的等比跟随由 usePanelResize(viewportRatio) 的 resize 监听负责。
 */
function restoredRightPanelWidth(): number {
  try {
    return computeRestoredRatioWidth(
      localStorage.getItem(RIGHT_PANEL_WIDTH_KEY),
      RIGHT_PANEL_MIN_WIDTH,
      RIGHT_PANEL_MAX_WIDTH,
    ) ?? RIGHT_PANEL_DEFAULT_WIDTH;
  } catch {
    return RIGHT_PANEL_DEFAULT_WIDTH;
  }
}

// 底部内嵌终端面板持久化键与尺寸边界（与 'ycode-*' 前缀约定一致）
export const TERMINAL_PANEL_OPEN_KEY = 'ycode-terminal-open';
export const TERMINAL_PANEL_HEIGHT_KEY = 'ycode-terminal-height';
export const TERMINAL_PANEL_DEFAULT_HEIGHT = 300;
export const TERMINAL_PANEL_MIN_HEIGHT = 140;
export const TERMINAL_PANEL_MAX_HEIGHT = 720;

// 秘书名字长度上限：与后端 core.secretary.validate_secretary_name 的口径一致
// （去首尾空白后 1-20 个字符）。前端只做「禁用按钮 + 可见提示」，不做截断——
// 悄悄把用户输入砍短会让身份名字与用户输入不符，后端仍会二次校验。
export const SECRETARY_NAME_MAX_LEN = 20;
// 秘书专属人设长度上限：与后端 core.secretary.MAX_PERSONA_LEN 一致；留空合法
// （= 使用内置默认秘书人设），只在超长时拦下并给出可见提示
export const SECRETARY_PERSONA_MAX_LEN = 20000;

// 右面板页签 id：每种工作台同时最多一个实例；tasks/subagent 是详情查看页
export type RightPanelTabId = 'files' | 'git' | 'browser' | 'tasks' | 'computer' | 'subagent';
// 合法页签 id 白名单：恢复会话快照时据此过滤过期 id，避免 PANE_LABELS 取到 undefined
const RIGHT_PANEL_TAB_IDS = new Set<RightPanelTabId>([
  'files', 'git', 'browser', 'tasks', 'computer', 'subagent',
]);

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

// 上下文来源占比快照：旧后端缺失→[]；过滤非法 source 与非正 chars（容错不炸）
const CONTEXT_BREAKDOWN_SOURCES = new Set<ContextBreakdownItem['source']>([
  'messages',
  'system_prompt',
  'skills',
  'runtime_context',
  'system_tools',
  'mcp_tools',
]);

const normalizeContextBreakdown = (value: unknown): ContextBreakdownItem[] => {
  if (!Array.isArray(value)) return [];
  const items: ContextBreakdownItem[] = [];
  for (const raw of value) {
    if (!raw || typeof raw !== 'object') continue;
    const source = (raw as { source?: unknown }).source;
    if (typeof source !== 'string') continue;
    if (!CONTEXT_BREAKDOWN_SOURCES.has(source as ContextBreakdownItem['source'])) continue;
    const chars = Number((raw as { chars?: unknown }).chars);
    if (!Number.isFinite(chars) || chars <= 0) continue;
    items.push({ source: source as ContextBreakdownItem['source'], chars });
  }
  return items;
};

export const normalizeSessionTokenTotals = (data: any = {}): SessionTokenTotals => ({
  session_id: String(data.session_id ?? ''),
  prompt: Number(data.prompt ?? 0),
  completion: Number(data.completion ?? 0),
  cached: Number(data.cached ?? 0),
  cache_prompt: Number(data.cache_prompt ?? 0),
  total: Number(data.total ?? 0),
  requests: Number(data.requests ?? 0),
  last_ts: data.last_ts === null || data.last_ts === undefined ? null : Number(data.last_ts),
  keepalive_prompt: Number(data.keepalive_prompt ?? 0),
  keepalive_cached: Number(data.keepalive_cached ?? 0),
  keepalive_requests: Number(data.keepalive_requests ?? 0),
});

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
    // 车道身份与保活单列：命中率按 (会话, 模型, 接口, scope) 独立，
    // 界面必须标清楚数字属于哪条车道；旧后端无这些字段时回落 ""/null（不冒充 0%）
    cache_lane_model: String(data.cache_lane_model ?? ''),
    cache_lane_api_base: String(data.cache_lane_api_base ?? ''),
    cache_keepalive_hot_pct: nullableNumber(data.cache_keepalive_hot_pct),
    cache_keepalive_session_pct: nullableNumber(data.cache_keepalive_session_pct),
    // 本会话口径（落库聚合，重启不清零）；旧后端无此字段时回落全零 + 空 session_id
    session_totals: normalizeSessionTokenTotals(data.session_totals),
    context_breakdown: normalizeContextBreakdown(data.context_breakdown),
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
  // 后端历史行的稳定 id（分页游标 / 红点导航按 id 定位用；本地新消息可能缺省）
  _msg_id?: number;
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
  // 按回合撤销：权威回合归属（message_turn_links 持久化元数据，旧消息缺省为空串）。
  // turnId 是撤销定位键；positionKind 标识本条消息在回合中的位置种类
  turnId?: string;
  positionKind?: string;
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
  for (const raw of messages) {
    // 按回合撤销：后端消息携带 snake_case 的回合关联元数据，统一映射
    const m: ChatMessage = (() => {
      const anyRaw = raw as any;
      if (anyRaw.turn_id === undefined && anyRaw.position_kind === undefined) return raw;
      return {
        ...raw,
        turnId: anyRaw.turn_id || '',
        positionKind: anyRaw.position_kind || '',
      } as ChatMessage;
    })();
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
  // 按回合撤销：undo_turn 权限卡附带的服务预检影响摘要（其余工具为 null）
  undoImpact?: Record<string, any> | null;
}

export type SubpageType = 'skills' | 'mcp' | 'plugins' | 'models' | 'automations';

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

    // 飞书集成快照（密钥只回 has_secret，明文按需单独取）
    feishuSettings: {
      enabled: false,
      app_id: '',
      has_secret: false,
      approvers: [] as string[],
      notify_chat_id: '',
      status: {
        state: 'disabled',
        running: false,
        last_error: '',
        received_events: 0,
        duplicate_events: 0,
        uptime_seconds: 0,
      },
    },

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
      // 上下文水位基准：默认未设置（null，跟随模型真实窗口），
      // 真实值一律以后端快照覆盖
      context_watermark_base: null,
      context_watermark_base_min: 50000,
      context_watermark_base_max: 2000000,
      // AI 生成内容目录：默认目录与生效路径一律以后端快照覆盖，前端不猜路径
      ai_output_dir: '',
      ai_output_dir_setting: '',
      ai_output_dir_default: '',
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
    // 每帧截图索引：screen_sha256 → loopback 预览 URL。computer_observation 事件
    // 同时带画面哈希与预览地址，按哈希记住后在对话区的电脑动作行里反查该动作
    // 当帧的画面——不用给每个动作单独存截图，也不会把旧图错配到新动作上。
    computerShots: {} as Record<string, string>,
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
    // 归档会话（get_archived_sessions 的扁平列表，按归档时刻倒序）：设置页
    // 「会话归档」分区的数据源，不参与主列表渲染
    archivedSessions: [] as ArchivedSessionInfo[],
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
    // 按回合撤销：turn_id → 检查点状态摘要（批量查询 + turn_undo_updated 事件维护）。
    // 卡片按权威 turnId 绑定状态；页面刷新/切换会话后由批量查询重建
    turnUndoStates: {} as Record<string, TurnUndoStateSummary>,
    turnUndoReqSeq: 0,
    // M31 修复：各类异步刷新的请求序号，响应返回时序号已过期则丢弃，
    // 避免并发/重复刷新时旧快照覆盖新快照
    sessionsReqSeq: 0,
    // 归档会话列表刷新序号（同上：过期快照不覆盖新快照）
    archivedSessionsReqSeq: 0,
    tokenUsageReqSeq: 0,
    mcpReqSeq: 0,
    pluginsReqSeq: 0,
    // C11：Hook 状态刷新请求序号，过期响应不覆盖新快照
    hooksReqSeq: 0,
    // M27 修复：文件预览请求序号，旧响应不覆盖新打开文件的内容
    filePreviewReqSeq: 0,

    // 技能 / MCP / 插件
    skills: [] as SkillInfo[],
    // 技能列表是否仍在后台扫描中：首屏 get_initial_state 不再同步扫盘（改为
    // skills_loaded 事件推送），初始为空数组并不代表"没有技能"，技能页据此
    // 显示"加载中"而不是"暂无可用技能"，避免误导用户
    skillsLoading: false,
    mcps: [] as McpInfo[],
    plugins: [] as PluginInfo[],
    // C11：Hook 信任卡片数据源（项目级命令逐条信任，信任前不执行）
    hooks: [] as HookInfo[],
    hooksLoading: false,

    // 星轨自动化任务：任务列表 + 每任务运行历史（事件幂等替换）
    automationTasks: [] as AutomationTaskInfo[],
    automationRuns: {} as Record<string, AutomationRunInfo[]>,

    // 对话消息流
    messages: [] as ChatMessage[],
// 会话历史分页（懒加载）：当前会话的工作副本，随 _captureRuntime/_bindSessionRuntime 切换保存恢复
historyHasMore: false,
historyOldestId: null as number | null,
historyLoading: false,
// 红点导航全量索引（user 消息 id+预览，不分页）：MessageNavRail 全量渲染数据源
navItems: [] as Array<{ id: number; preview: string }>,
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
    // 会话切换加载中：切到"已有会话"时，目标会话历史仍在后端返回路上（消息区为空）
    // 的加载态。用于把"加载中"与"真正的新对话/空会话"区分开，避免切换瞬间
    // 先渲染空消息区而闪出首屏空状态（新对话欢迎页）卡片。
    // 仅 switchSession 置位/复位，新建会话（createGlobalSession/createSessionInGroup）直接置 false。
    sessionSwitching: false,
    showThinking: false,

    // 图片大图查看（Lightbox）
    lightbox: { show: false, images: [] as string[], index: 0 },

    // 后端连接状态 / 版本号
    connected: false,
    version: '',
    // 启动初始化是否完成（控制 星瑶Code 启动画面，完成后淡出进入对话）
    initialized: false,
    // JS 桥完成首次验证（initFromBackend 拿到 api）：底部终端等惰性能力
    // 以此为启动信号，避免在 pywebview 注入完成前触达 _api() 弹连接失败框
    bridgeReady: false,

    // 侧栏与面板开合状态
    // 移动端（<=768px）默认收起侧栏：侧栏作为 fixed 覆盖层，展开时滑入，
    // 避免在手机上初始加载即被侧栏遮挡主对话区。
    sidebarCollapsed:
      typeof window !== 'undefined' &&
      typeof window.matchMedia === 'function' &&
      window.matchMedia('(max-width: 768px)').matches,
    // 右侧计划/任务/终端/文件/Git 栏默认收起：启动不摊开主对话区，
    // 仅由顶栏“切换面板”按钮或底栏 Git 分支项手动展开；
    // 展开状态不持久化，每次启动都从收起开始
    rightPanelCollapsed: true,
    // “打开标签页”选择页可见性：顶栏按钮等中性入口展开面板时先选择要看的内容，
    // 底栏 Git 分支项等定向入口经 openRightPanelTab 直接开页签（不持久化）。
    // 页签全部关闭时也回到选择页
    rightPanelPicker: false,
    // 右面板多页签：当前会话打开的页签与激活页签（'' = 停在选择页）。
    // 页签集/激活页签/展开态都会话隔离：随 _captureRuntime/_bindSessionRuntime
    // 按会话保存恢复，A 会话打开的浏览器页签不会出现在 B 会话
    rightPanelTabs: [] as RightPanelTabId[],
    rightPanelActiveTab: '' as RightPanelTabId | '',
    // 右面板画笔截图模式：冻结帧覆盖层挂出中。置位后浏览器页签的
    // shouldShowSurface 门控会让原生 WebView2 表面 hide（冻结帧已含其像素，
    // 原生控件必须让位给画笔覆盖层）；关闭后既有 watch 自动 re-show。
    // 模态瞬态，不持久化也不会话隔离
    rightPanelSnipActive: false,
    // 桌面端左右面板宽度（px）：拖拽手柄实时更新，启动时从 localStorage 恢复；
    // 右面板按视口比例记忆（窗口缩放等比跟随），侧栏按固定像素记忆；
    // 移动端（<=768px）忽略这两个值，布局由 style.css 媒体查询接管
    sidebarWidth: storedPanelWidth(SIDEBAR_WIDTH_KEY, SIDEBAR_DEFAULT_WIDTH, SIDEBAR_MIN_WIDTH, SIDEBAR_MAX_WIDTH),
    rightPanelWidth: restoredRightPanelWidth(),
    // 内嵌浏览器（右侧面板浏览器页签）：真实网页由宿主叠加的原生 WebView2
    // 渲染，前端只持有状态（地址栏/前进后退能力/加载态）并做乐观更新；
    // supported=false 表示宿主平台不支持（非 Windows 桌面端 GUI 或缺桥）。
    embeddedBrowser: {
      url: '',
      title: '',
      can_go_back: false,
      can_go_forward: false,
      loading: false,
      ready: false,
      visible: false,
      supported: true,
      // 星瑶浏览器自动化的项目分辨率（适应窗口呈现）；null = 无自动化视口
      automationViewport: null as [number, number] | null,
    },
    // 右栏浏览器「外部请求导航」（对话正文 / 文件树点击 html 文件触发）：
    // BrowserPane 消费这条请求去导航；nonce 自增保证同一文件重复点击也能被
    // 观察到（内容相同的对象引用不变时 watch 不触发）。null = 无待处理请求。
    browserNavRequest: null as null | { url: string; path: string },
    browserNavNonce: 0,
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
      // 纯选目录模式：非空时确认只把所选路径交给该回调（不登记工作区、
      // 不迁移会话归属），供星轨自动化任务表单等场景使用
      plainPick: null as null | ((path: string) => void),
    },
    // 设置页打开后要落在哪个分区（模型配置/外观与主题等），由调用方指定；
    // 预览返回编辑器时用它跳回"外观与主题"而不是默认的模型配置
    settingsSection: 'models' as 'models' | 'tokens' | 'permissions' | 'hooks' | 'appearance' | 'system' | 'persona' | 'decision' | 'plugins' | 'about' | 'contact' | 'disclaimer',
    // JEV 决策记录实时流：decision_recorded 事件累加未读徽章；面板按 tick 拉取刷新
    decisionUnread: 0,
    decisionEventTick: 0,
    // 决策入口可见性：只有真的产生过决策记录才显示头部「决策」入口，
    // 没做过决策（未启用 / 启用了但从未调用）时不给用户一个空入口
    decisionHasRecords: false,
    // 智能体显示名：当前会话生效名——普通会话为设置页「人设设置」的名字，
    // 秘书会话为该会话自己的秘书名（界面文案/执行过程/思考提示都跟随它）
    agentName: '星瑶',
    // 设置页人设名：秘书会话里 agentName 会被秘书名占住，等切回普通会话 /
    // 进入尚未落库的草稿会话时要回落，故与 agentName 分开单独留一份
    personaAgentName: '星瑶',
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
    // 右面板“后台任务”详情：当前查看的任务 id 与打开 nonce（执行条“详情”触发）
    bgTaskDetailId: '',
    bgTaskOpenNonce: 0,
    // 右面板“子代理”详情：当前查看的任务 id 与打开 nonce（卡片“查看”触发）
    subagentDetailId: '',
    subagentOpenNonce: 0,
    subagentTasks: [] as SubagentTaskSnapshot[],
    tokenUsage: emptyTokenUsage(),
    // 会话 token 弹层（点击状态行时按需拉取；打开即刷新，不做后台轮询）
    tokenUsageOverview: null as TokenUsageOverview | null,
    tokenUsageOverviewLoading: false,
    tokenStatistics: null as TokenStatisticsSnapshot | null,
    tokenStatisticsLoading: false,
    tokenStatisticsDays: 30 as 1 | 7 | 30,
    tokenStatisticsLifetime: null as TokenStatisticsSnapshot | null,
    tokenStatisticsLifetimeLoading: false,

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

    // 会话全局检索模态框（Command Palette）
    sessionSearchModal: {
      show: false,
    },

    // 权限请求弹窗
    permRequest: { show: false, sessionName: '', reqId: '', summary: '', source: '', allowAlways: false, options: [], permissionOptions: [], functionName: '' } as PermReqState,

    // 二级抽屉页面：技能 / MCP / 模型管理
    subpage: { show: false, type: 'skills' as SubpageType },

    // ── 背景图转换（mp4 → 动画 WebP；后端 gui_bridge 的 bg_convert_* 接口）──
    bgConvert: {
      video: null as BackgroundVideoInfo | null,
      outputDir: '',
      fps: 12,
      quality: 78,
      pingpong: true,
      running: false,
      percent: 0,
      stage: '',
      etaSeconds: null as number | null,
      resultPath: '',
      resultBytes: 0,
      message: '',
      error: '',
    },

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

    // 底部内嵌交互式终端面板（ZCode 弹出式终端）：
    // open 持久化到 localStorage，height 记忆拖拽结果；默认收起
    terminalPanelOpen: (() => {
      try { return localStorage.getItem(TERMINAL_PANEL_OPEN_KEY) === '1'; } catch { return false; }
    })(),
    terminalPanelHeight: (() => {
      try {
        const n = Number(localStorage.getItem(TERMINAL_PANEL_HEIGHT_KEY));
        return Number.isFinite(n) && n >= TERMINAL_PANEL_MIN_HEIGHT
          ? Math.min(n, TERMINAL_PANEL_MAX_HEIGHT)
          : TERMINAL_PANEL_DEFAULT_HEIGHT;
      } catch { return TERMINAL_PANEL_DEFAULT_HEIGHT; }
    })(),
  }),

  getters: {
    // 是否需要弹免责条款确认框：未同意过，或已同意的版本落后于当前条款版本
    // （条款版本升级后须重新确认）。
    // requiredVersion 为空表示后端初始状态校验尚未完成，启动期间绝不提前弹窗。
    disclaimerNeedsAccept(state): boolean {
      if (!state.disclaimerRequiredVersion) return false;
      return !isDisclaimerVersionAccepted(state.disclaimerAcceptedVersion, state.disclaimerRequiredVersion);
    },

    // 已加载消息流中首条带 _msg_id 的消息 id（红点导航已加载/未加载分界）
    firstLoadedMsgId(state): number | null {
      const m = (state.messages as any[]).find(x => typeof x?._msg_id === 'number');
      return m ? (m._msg_id as number) : null;
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

    // 当前会话发起的后台任务（底部执行条渲染源）：快照带 session_id 归属，
    // 只显示本会话启动的任务，其他会话（或 CLI）启动的任务不跨会话串显；
    // 无归属的旧记录不属于任何会话，任何会话的执行条都不显示
    currentSessionBackgroundTasks(state): BackgroundTask[] {
      const sess = state.currentSessionName;
      if (!sess) return [];
      return state.backgroundTasks.filter(t => t.session_id === sess);
    },

    // 顶部安全条可见性（设计 16.1 / brief 逐字状态表）：run 活动期才显示。
    // 注意：这是全局判据（RightPanel 自动登记操控页签也用它），不区分会话
    computerControlBarVisible(state): boolean {
      return COMPUTER_BAR_STATUSES.includes(state.computerRun.status);
    },

    // 顶部安全条的会话归属（对齐 currentSessionBackgroundTasks 的既有约定）：
    // 只在本会话发起的 run 里渲染，run 在 A 会话启动、用户正看 B 会话时不串显；
    // 归属未知（owner_session_id 为空，如旧后端快照）时回落显示，
    // 避免用户失去条上的暂停/停止入口
    computerBarVisibleInCurrentSession(state): boolean {
      if (!COMPUTER_BAR_STATUSES.includes(state.computerRun.status)) return false;
      const owner = state.computerRun.owner_session_id;
      if (!owner) return true;
      return owner === state.currentSessionName;
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

    // C11：待信任的项目级 Hook（pending/stale 才需要用户决策；trusted 已生效）
    pendingProjectHooks(state): HookInfo[] {
      return state.hooks.filter(
        h => h.scope === 'project' && (h.status === 'pending' || h.status === 'stale'),
      );
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

    // opts.skipLayout：跳过右面板页签/开合/任务详情等布局态捕获。事件管线
    // （handleBridgeEvent）跨会话临时绑到目标会话时必须传 true——此时顶层
    // 布局仍是当前查看会话的实时状态，捕获进目标会话会用 A 的布局污染 B 的
    // 快照，用户真实切回 B 时恢复出错误页签/开合。
    _captureRuntime(this: any, sessionName = this.currentSessionName, opts?: { skipLayout?: boolean }) {
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
      // 权限档位随会话保存：选择只影响当前会话，不再全局生效
      runtime.permMode = this.permissionMode;
      // 本回合用到的技能随会话保存
      runtime.usedSkills = this.usedSkills;
      // 历史分页游标随会话保存（loading 为瞬态不保存，bind 时强制复位）
      runtime.historyHasMore = this.historyHasMore;
      runtime.historyOldestId = this.historyOldestId;
      // 导航索引随会话保存：切回时展示该会话自己的红点
      runtime.navItems = this.navItems;
      // 右面板多页签随会话保存：页签集、激活页签与面板展开态都按会话隔离，
      // 切回时恢复该会话自己的浏览器/Git 等页签。事件管线的临时捕获
      // （skipLayout）不保存这段：顶层布局此时仍属于当前查看的会话
      if (!opts?.skipLayout) {
      // 注意：本行**每次都换一个新数组引用**。S3-T8 报告里「回写当前 store 的同一批
      // 引用、值相等时 Vue hasChanged 不触发更新」那条论断只适用于上面按引用直赋的
      // 字段（messages / tokenUsage / modelId / navItems 等），**不适用于 rightPanelTabs**。
      // 这里没有性能后果：该字段只在切会话时被读写，不在高频事件路径上。
      runtime.rightPanelTabs = [...this.rightPanelTabs];
      runtime.rightPanelActiveTab = this.rightPanelActiveTab;
      runtime.rightPanelExpanded = !this.rightPanelCollapsed;
      // 后台任务详情查看对象随会话保存：切回时恢复该会话自己的查看对象
      runtime.bgTaskDetailId = this.bgTaskDetailId;
      // 子代理详情查看对象同口径随会话保存：切会话不残留上一个会话的查看 id
      runtime.subagentDetailId = this.subagentDetailId;
      }
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

    // opts.skipLayout：跳过右面板页签/开合/任务详情等布局态恢复。事件管线
    // （handleBridgeEvent）跨会话临时绑回当前会话时必须传 true——布局只随
    // 用户真实切换会话恢复，否则该会话旧快照会把用户刚点的面板开合覆盖掉
    // （表现为"关了又自动打开 / 打开又自动关闭"）。
    _bindSessionRuntime(this: any, sessionName = this.currentSessionName, opts?: { skipLayout?: boolean }) {
      const runtime = this._getSessionRuntime(sessionName);
      this.messages = runtime.messages;
      // 历史分页游标随会话恢复；loading 为瞬态，切会话强制复位防卡死翻页
      this.historyHasMore = runtime.historyHasMore ?? false;
      this.historyOldestId = runtime.historyOldestId ?? null;
      this.historyLoading = false;
      // 导航索引随会话切换：目标会话没拉过索引时立即清空，
      // 否则会显示上一个会话的红点与悬停预览（跨会话串数据）
      this.navItems = runtime.navItems ?? [];
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
      // 权限档位随会话即时恢复（不等后端 switch_session 响应）：
      // 该会话选过档位才恢复，否则保持当前值由后端响应校正
      if (runtime.permMode) {
        this.permissionMode = runtime.permMode as PermissionMode;
      }
      // 右面板多页签随会话恢复：目标会话从未展开过面板（undefined）→ 收起、
      // 无页签；展开过的会话恢复自己的页签集与激活页签。浏览器原生表面随
      // BrowserPane 的 active prop 翻转自动隐藏/重显，不需要额外桥调用。
      // 事件管线的临时回绑（skipLayout）不恢复这段：布局属用户实时操作面，
      // 回滚到旧快照会吃掉用户刚做的开合/页签变更
      if (!opts?.skipLayout) {
      const savedTabs = runtime.rightPanelTabs;
      // 按合法页签白名单过滤：旧快照里可能残留已删除的 id，直接 as 强转会让
      // RightPanel 的 PANE_LABELS[tab] 取到 undefined（页签标题/图标变空白）
      let tabs: RightPanelTabId[] = savedTabs
        ? (savedTabs as string[]).filter(
            (t: string): t is RightPanelTabId => RIGHT_PANEL_TAB_IDS.has(t as RightPanelTabId),
          )
        : [];
      // 操控页签依附于全局唯一的 run：终态自动收场可能发生在别的会话上
      // （closeRightPanelTab 只作用于当前页签集），run 已清场后残留的操控
      // 页签恢复出来只会是一片空白，恢复时一并丢弃
      if (!this.computerRun.run_id) {
        tabs = tabs.filter(t => t !== 'computer');
      }
      this.rightPanelTabs = tabs;
      const savedActive = runtime.rightPanelActiveTab;
      this.rightPanelActiveTab = savedActive && this.rightPanelTabs.includes(savedActive as RightPanelTabId)
        ? savedActive
        : this.rightPanelTabs[this.rightPanelTabs.length - 1] || '';
      // 该会话从未展开过面板（undefined）→ 收起，让它显示正常会话页面；
      // 展开过的会话恢复自己的开合态
      this.rightPanelCollapsed = runtime.rightPanelExpanded === undefined
        ? true
        : !runtime.rightPanelExpanded;
      // 无页签 → 选择页；有页签 → 显示激活页签
      this.rightPanelPicker = this.rightPanelTabs.length === 0;
      // 后台任务详情查看对象随会话恢复：切会话不残留上一个会话的任务详情
      this.bgTaskDetailId = runtime.bgTaskDetailId || '';
      // 子代理详情查看对象同口径恢复：切会话不残留上一个会话的查看 id，
      // 否则 SubagentPane 会用新会话名去查上一会话的任务 id，详情串场
      this.subagentDetailId = runtime.subagentDetailId || '';
      // 画笔截图模态（冻结帧覆盖层）是会话瞬态：切会话后置位残留会让新会话的
      // 浏览器原生表面被持续压住、覆盖层显示上一会话的冻结帧，这里随会话强制退出
      // （覆盖层的本地快照 ref 由 RightPanel 监听 currentSessionName 一并清除）
      this.rightPanelSnipActive = false;
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

    // 底部终端专用的静默桥访问：不用 _api()（它缺桥时会弹窗），终端在
    // bridgeReady 之前根本不发起调用，这里兜底返回 null 即可
    _ptyApi(): PyWebViewAPI | null {
      return (window.pywebview && window.pywebview.api) || null;
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

    // 顶栏“切换面板”按钮等中性入口：收起/展开面板。展开时该会话已有页签 →
    // 直接显示激活页签；一个页签都没开过 → 先进选择页
    toggleRightPanel() {
      this.rightPanelCollapsed = !this.rightPanelCollapsed;
      if (!this.rightPanelCollapsed) {
        this.rightPanelPicker = this.rightPanelTabs.length === 0;
      }
    },

    // ── 右面板多页签（会话级隔离）─────────────────────────────────
    // 打开（或切换到）指定页签：不存在则追加，激活并停在该页签。定向入口
    // （Git 分支项、执行条详情、子代理卡片、浏览器自动化）统一走这里：
    // 面板上已有其他页签时是“加页签”，不再互相覆盖。
    // opts.expand=false：只登记页签不摊开面板（操控 run 自动切入沿用旧设计，
    // 用户手动展开面板时直接落在操控页签）
    openRightPanelTab(this: any, tab: RightPanelTabId, opts?: { expand?: boolean }) {
      if (!this.rightPanelTabs.includes(tab)) this.rightPanelTabs.push(tab);
      this.rightPanelActiveTab = tab;
      this.rightPanelPicker = false;
      if (opts?.expand !== false) this.rightPanelCollapsed = false;
      // 详情页签缺省查看对象：只在本会话有后台任务时自动选第一个，
      // 其他会话的任务不跨会话代选（与执行条的会话过滤口径一致）
      if (tab === 'tasks' && !this.bgTaskDetailId && this.currentSessionBackgroundTasks.length) {
        this.bgTaskDetailId = this.currentSessionBackgroundTasks[0].task_id;
      }
      if (tab === 'subagent' && !this.subagentDetailId && this.subagentTasks.length) {
        this.subagentDetailId = this.subagentTasks[0].id;
      }
    },

    // 关闭单个页签：激活页签被关时优先切换到右侧邻居（保持视线位置），
    // 没有右侧则取最后一个；全部关完回选择页（面板保持展开，可再开新页签）
    closeRightPanelTab(this: any, tab: RightPanelTabId) {
      const idx = this.rightPanelTabs.indexOf(tab);
      if (idx < 0) return;
      this.rightPanelTabs.splice(idx, 1);
      if (this.rightPanelActiveTab === tab) {
        this.rightPanelActiveTab =
          this.rightPanelTabs[Math.min(idx, this.rightPanelTabs.length - 1)] || '';
      }
      if (this.rightPanelTabs.length === 0) {
        this.rightPanelActiveTab = '';
        this.rightPanelPicker = true;
      }
      // 关闭页签同步清理该页签的瞬态查看对象：否则重开瞬间会先渲染旧详情
      // （tasks 残留 bgTaskDetailId、subagent 残留查看 id 与 SubagentPane 内部 detail）
      if (tab === 'tasks') this.bgTaskDetailId = '';
      if (tab === 'subagent') this.subagentDetailId = '';
    },

    // 页签条点击：切到已打开的页签
    activateRightPanelTab(this: any, tab: RightPanelTabId) {
      if (!this.rightPanelTabs.includes(tab)) return;
      this.rightPanelActiveTab = tab;
      this.rightPanelPicker = false;
    },

    // 页签条“+”：展开面板并显示选择页（保留已开页签，选卡后追加/激活）
    showRightPanelPicker(this: any) {
      this.rightPanelCollapsed = false;
      this.rightPanelPicker = true;
    },

    // 把浏览器页签开进指定会话的右面板（会话归属）：当前会话直接开；
    // 其他会话只写入其 runtime（含展开态），不抢当前界面——切回该会话时
    // 浏览器页签随之出现。浏览器原生表面随页签失活自动隐藏，不跨界显示
    openBrowserTabForSession(this: any, sessionName: string) {
      if (!sessionName || sessionName === this.currentSessionName) {
        this.openRightPanelTab('browser');
        return;
      }
      const runtime = this._getSessionRuntime(sessionName);
      const tabs: RightPanelTabId[] = runtime.rightPanelTabs
        ? ([...runtime.rightPanelTabs] as RightPanelTabId[])
        : [];
      if (!tabs.includes('browser')) tabs.push('browser');
      runtime.rightPanelTabs = tabs;
      runtime.rightPanelActiveTab = 'browser';
      runtime.rightPanelExpanded = true;
    },

    // 本地 html 在右栏内嵌浏览器里按网页渲染（对话正文 / 文件树点击 html 的入口）。
    // 路径转成 file:// URL 后写一条导航请求并摊开浏览器页签；转换失败（相对路径
    // 等非绝对路径）直接回退源码预览弹窗，不静默失败。宿主不支持内嵌浏览器
    // （非 Windows 桌面端 GUI）时，由 BrowserPane 消费请求时再回退预览。
    openLocalFileInBrowser(this: any, path: string) {
      const url = toFileUrl(path);
      if (!url) {
        void this.openFilePreview(path);
        return;
      }
      this.browserNavRequest = { url, path };
      this.browserNavNonce += 1;
      this.openRightPanelTab('browser');
    },

    // markdown 在右栏浏览器里按阅读样式打开：浏览器直接打开 .md 只显示源码文本，
    // 所以先用与消息气泡同一套渲染器生成预览 HTML，经桥落盘（core.web_preview）
    // 后再交浏览器打开。任一环节不可用（缺桥 / 读文件失败 / 落盘失败）都回退
    // 源码预览弹窗，不静默失败。
    async openMarkdownInRightPanel(this: any, path: string) {
      const api = this._api();
      if (!api || typeof api.read_file_preview !== 'function'
        || typeof api.write_preview_page !== 'function') {
        await this.openFilePreview(path);
        return;
      }
      try {
        const res = await api.read_file_preview(path);
        if (!res || !res.ok || typeof res.content !== 'string') {
          await this.openFilePreview(path);
          return;
        }
        const name = path.split(/[\\/]/).pop() || path;
        const page = buildMarkdownPreviewHtml(res.content, path, name);
        const written = await api.write_preview_page(page, path);
        if (!written || !written.ok || !written.path) {
          await this.openFilePreview(path);
          return;
        }
        this.openLocalFileInBrowser(String(written.path));
      } catch {
        await this.openFilePreview(path);
      }
    },

    // 本地文件在右栏显示的统一入口（对话正文 / 文件树的点击分流）：
    // html / pdf 直接交给内嵌浏览器（pdf 走 WebView2 内置查看器）；md 先渲染成
    // 预览页；其它类型回退源码预览弹窗。
    async openInRightPanel(this: any, path: string) {
      const target = String(path || '').trim();
      if (!target) return;
      if (isMarkdownFilePath(target)) {
        await this.openMarkdownInRightPanel(target);
        return;
      }
      this.openLocalFileInBrowser(target);
    },

    // 把任意页签开进指定会话的右面板（会话归属）：当前会话直接开；其他会话只
    // 写入其 runtime（含展开态），不抢当前界面——切回该会话时页签随之出现。
    // 供「run 归属会话」的电脑操控页签等使用：run 在 A 会话启动、用户正看 B
    // 会话时，操控页签应写进 A 的 runtime，不能把 B 的页签集写脏。
    openRightPanelTabForSession(this: any, sessionName: string, tab: RightPanelTabId, opts?: { expand?: boolean }) {
      if (!sessionName || sessionName === this.currentSessionName) {
        this.openRightPanelTab(tab, opts);
        return;
      }
      const runtime = this._getSessionRuntime(sessionName);
      const tabs: RightPanelTabId[] = runtime.rightPanelTabs
        ? (runtime.rightPanelTabs as string[]).filter(
            (t: string): t is RightPanelTabId => RIGHT_PANEL_TAB_IDS.has(t as RightPanelTabId),
          )
        : [];
      if (!tabs.includes(tab)) tabs.push(tab);
      runtime.rightPanelTabs = tabs;
      runtime.rightPanelActiveTab = tab;
      if (opts?.expand !== false) runtime.rightPanelExpanded = true;
    },

    // 执行条“详情”：记录目标任务并打开后台任务页签（nonce 每次自增，
    // 同一任务重复点击也能从其他页签切回）
    openBackgroundTaskDetail(this: any, taskId: string) {
      if (!taskId) return;
      this.bgTaskDetailId = taskId;
      this.bgTaskOpenNonce += 1;
      this.openRightPanelTab('tasks');
    },

    // 子代理卡片"查看"按钮：记录目标任务 id 并打开子代理页签
    // （nonce 每次自增，同一任务重复点击也能从其他页签切回）
    openSubagentDetail(taskId: string) {
      if (!taskId) return;
      this.subagentDetailId = taskId;
      this.subagentOpenNonce += 1;
      this.openRightPanelTab('subagent');
    },

    // ── 底部内嵌交互式终端面板 ──────────────────────────────────────
    toggleTerminalPanel() {
      this.terminalPanelOpen = !this.terminalPanelOpen;
      try {
        if (this.terminalPanelOpen) {
          localStorage.setItem(TERMINAL_PANEL_OPEN_KEY, '1');
        } else {
          localStorage.removeItem(TERMINAL_PANEL_OPEN_KEY);
        }
      } catch { /* 存储不可用时仅本次会话生效 */ }
    },

    setTerminalPanelHeight(height: number) {
      this.terminalPanelHeight = Math.round(
        Math.min(TERMINAL_PANEL_MAX_HEIGHT, Math.max(TERMINAL_PANEL_MIN_HEIGHT, height))
      );
      try {
        localStorage.setItem(TERMINAL_PANEL_HEIGHT_KEY, String(this.terminalPanelHeight));
      } catch { /* 忽略 */ }
    },

    /** 确保后端常驻 shell 存在（首次打开面板或上次进程退出后调用） */
    async ensurePtyTerminal(cols: number, rows: number): Promise<{ ok: boolean; cwd?: string; reused?: boolean; msg?: string }> {
      const api = this._ptyApi();
      if (!api?.pty_terminal_ensure) return { ok: false, msg: '后端桥未就绪' };
      try {
        const res = await api.pty_terminal_ensure(cols, rows);
        if (res && res.ok === false && res.msg) {
          this.notifyChatLocal(`终端启动失败：${res.msg}`, 'notice');
        }
        return res || { ok: false, msg: '未知错误' };
      } catch (err) {
        return { ok: false, msg: String(err) };
      }
    },

    async ptyTerminalWrite(text: string) {
      const api = this._ptyApi();
      if (api?.pty_terminal_write) {
        try { await api.pty_terminal_write(text); } catch { /* 输入丢弃即可 */ }
      }
    },

    async ptyTerminalResize(cols: number, rows: number) {
      const api = this._ptyApi();
      if (api?.pty_terminal_resize) {
        try { await api.pty_terminal_resize(cols, rows); } catch { /* 下次 resize 再试 */ }
      }
    },

    async ptyTerminalKill() {
      const api = this._ptyApi();
      if (api?.pty_terminal_kill) {
        try { await api.pty_terminal_kill(); } catch { /* 忽略 */ }
      }
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

    _restoreGuiThemeDomSnapshot(rootStyle: string | null, light: boolean, guiThemeId: string | undefined) {
      if (rootStyle === null) document.documentElement.removeAttribute('style');
      else document.documentElement.setAttribute('style', rootStyle);
      // light 类在 html 与 body 上成对维护（见 runtime.applyGuiTheme）
      document.documentElement.classList.toggle('light', light);
      document.body.classList.toggle('light', light);
      // 主题身份钩子（data-gui-theme-id，主题专属 CSS 的作用域）随快照一起回滚：
      // 候选主题的原生标题栏桥失败时若不回滚该属性，html 上停留的是候选主题的 id，
      // 与 store.guiTheme（旧主题）不一致，旧主题会被候选主题的皮肤 CSS 染色。
      if (guiThemeId === undefined) delete document.documentElement.dataset.guiThemeId;
      else document.documentElement.dataset.guiThemeId = guiThemeId;
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
      const previousGuiThemeId = document.documentElement.dataset.guiThemeId;
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
            this._restoreGuiThemeDomSnapshot(previousRootStyle, previousLight, previousGuiThemeId);
            const rollback = await this._restorePreviousNativeTitlebar(previousTheme);
            if (!rollback.ok) return rollback;
            return result;
          }
          this.nativeTitlebarColorCapability = result.native_titlebar_color_capability;
        } else if (nativeMode === 'restore') {
          const result = await api.restore_native_titlebar();
          if (!result.ok) {
            candidateHandle.dispose();
            this._restoreGuiThemeDomSnapshot(previousRootStyle, previousLight, previousGuiThemeId);
            const rollback = await this._restorePreviousNativeTitlebar(previousTheme);
            if (!rollback.ok) return rollback;
            return result;
          }
          this.nativeTitlebarColorCapability = result.native_titlebar_color_capability;
        }
      } catch (error) {
        candidateHandle.dispose();
        this._restoreGuiThemeDomSnapshot(previousRootStyle, previousLight, previousGuiThemeId);
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

    openSessionSearchModal() {
      this.sessionSearchModal.show = true;
    },

    closeSessionSearchModal() {
      this.sessionSearchModal.show = false;
    },

    async searchSessionsFull(
      query: string,
      scope: 'all' | 'title' | 'content' = 'all',
      workdir?: string,
      limit: number = 30,
      includeArchived: boolean = false,
    ) {
      const api = this._api();
      if (!api || !api.search_sessions_full) {
        return { ok: false, total: 0, results: [], msg: '后端未就绪' };
      }
      try {
        // include_archived 只在开启时作为第 5 个参数透传：默认路径保持原有 4 参
        // 签名，归档前行为与旧后端完全不变。
        if (includeArchived) {
          return await api.search_sessions_full(query, scope, workdir, limit, true);
        }
        return await api.search_sessions_full(query, scope, workdir, limit);
      } catch (err: any) {
        console.error('search_sessions_full 失败:', err);
        return { ok: false, total: 0, results: [], msg: String(err) };
      }
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
      this.bridgeReady = true;
      // 星轨自动化：启动时主动拉一次任务快照（引擎启动推送可能早于页面就绪被丢弃）
      void this.refreshAutomations();
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
          this.personaAgentName = String(personaInit.agent_name || '星瑶');
          this.agentName = this.personaAgentName;
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
        // 初始快照为空即代表后端首屏跳过了同步扫描，正在后台扫：
        // 置为"加载中"态，等 skills_loaded 事件到达后再切回真实空态
        this.skillsLoading = this.skills.length === 0;
        this.mcps = init.mcps || [];
        this.plugins = (init as any).plugins || [];
        // C11：启动时拉取 Hook 信任状态（信任卡片数据源）
        void this.refreshHooks();
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
      this.historyHasMore = !!(init as any).history_has_more;
      this.historyOldestId = (init as any).history_oldest_id ?? null;        void this.refreshNavIndex();
        // 按回合撤销：启动批量拉取当前会话的检查点状态（重建撤销卡片）
        void this.refreshTurnUndoStates();
        // 决策入口按当前会话探测：必须在 currentSessionName 就绪之后调用
        // （App.vue 的启动探测发生在 initFromBackend 未完成的时刻，那时还没有
        // 会话名，会按口径直接跳过，故这里补一次）
        void this.refreshDecisionAvailability();// 批次D：窗口重载/崩溃重建期间终态事件已丢失，后端通过 active_sessions
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
        // 启动自动恢复的会话可能是秘书会话：显示名以桥上的「当前生效名」为准
        // （拉不到时内部回落设置页名字，不打断启动流程）
        void this.refreshActiveAgentName();
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

    // 按模型名预览思考能力（新增/改名时用）：仅查询不落库，
    // 失败或异常一律返回 null，由调用方跳过更新，不弹提示打扰用户
    async getReasoningSnapshotByName(modelName: string) {
      const api = this._api();
      if (!api) return null;
      try {
        const res = await api.get_reasoning_snapshot_by_name(modelName);
        if (res && res.ok) return res.reasoning;
        return null;
      } catch (err) {
        this.connected = false;
        return null;
      }
    },

    async setPermissionMode(mode: PermissionMode) {
      const prev = this.permissionMode;
      const api = this._api();
      if (!api) return;
      // 权限选择是会话级的：只绑定当前会话，不再全局生效
      const sessionName = this.currentSessionName;
      try {
        const res = await api.set_permission_mode(mode, sessionName || undefined);
        if (res && res.ok) {
          this.permissionMode = res.permission_mode || mode;
          // pending 草稿会话后端绑不上（还没落库），先存运行时，
          // 首条消息发送时随 send_user_message 带给后端补绑定
          const runtime = this._getSessionRuntime(sessionName);
          runtime.permMode = this.permissionMode;
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

    // ---- 飞书集成（设置 → 飞书集成）----

    async refreshFeishuSettings() {
      const api = this._api();
      if (!api) return null;
      try {
        const result = await api.get_feishu_settings();
        if (result && result.ok && result.settings) {
          this.feishuSettings = result.settings;
          return result.settings;
        }
        return null;
      } catch (err) {
        this.connected = false;
        this.notifyModal('飞书集成', '加载飞书设置失败: ' + err);
        return null;
      }
    },

    /** 保存飞书设置；app_secret 留空表示不修改已有密钥 */
    async saveFeishuSettings(payload: {
      enabled: boolean;
      app_id: string;
      app_secret?: string;
      approvers: string[];
      notify_chat_id: string;
    }): Promise<{ ok: boolean; msg: string }> {
      const api = this._api();
      if (!api) return { ok: false, msg: '桥接不可用' };
      try {
        const result = await api.set_feishu_settings(payload);
        if (result && result.ok) {
          if (result.settings) this.feishuSettings = result.settings;
          return { ok: true, msg: '' };
        }
        return { ok: false, msg: (result && result.msg) || '保存失败' };
      } catch (err) {
        return { ok: false, msg: String(err) };
      }
    },

    /** 按需取 app_secret 明文（仅用户点「显示」时调用） */
    async revealFeishuSecret(): Promise<string> {
      const api = this._api();
      if (!api) return '';
      try {
        const result = await api.get_feishu_secret();
        return result && result.ok ? String(result.app_secret || '') : '';
      } catch {
        return '';
      }
    },

    /** 测试连接：用传入凭据或已保存凭据请求飞书长连接接入点 */
    async testFeishuConnection(appId: string, appSecret: string): Promise<{ ok: boolean; msg: string }> {
      const api = this._api();
      if (!api) return { ok: false, msg: '桥接不可用' };
      try {
        const result = await api.test_feishu_connection(appId, appSecret);
        return { ok: !!(result && result.ok), msg: (result && result.msg) || '' };
      } catch (err) {
        return { ok: false, msg: String(err) };
      }
    },

    // silent：后台静默刷新（模型切换、其它端推送触发的），失败不弹模态框、
    // 不把连接状态打成断开——用户没有主动请求，不该被打扰
    async refreshSystemSettings(options: { silent?: boolean } = {}) {
      const api = this._api();
      if (!api) return null;
      try {
        const settings = await api.get_system_settings();
        this.systemSettings = settings;
        return settings;
      } catch (err) {
        if (!options.silent) {
          this.connected = false;
          this.notifyModal('系统设置', '加载系统设置失败: ' + err);
        }
        return null;
      }
    },

    // 人设设置（名称/称呼）保存后即时同步全局显示；快照由 PersonaSettings 页传入
    applyPersonaSettings(snapshot: { agent_name?: string; user_title?: string }) {
      if (!snapshot || typeof snapshot !== 'object') return;
      if (snapshot.agent_name) {
        this.personaAgentName = String(snapshot.agent_name);
        this.agentName = this.personaAgentName;
      }
      this.userTitle = String(snapshot.user_title || '');
      this.syncDocumentTitle();
      // 秘书会话显示的是秘书名，不吃设置页名字：这时要多问一次桥把名字换回来
      // （普通会话 / 草稿会话新名字就是生效名，不必多打一次后端）
      if (this._isSecretarySession(this.currentSessionName)) void this.refreshActiveAgentName();
    },

    /** 统一改写生效显示名：agentName 与窗口标题同步，避免两处文案分叉 */
    _applyAgentName(name: string) {
      if (!name) return;
      this.agentName = name;
      this.syncDocumentTitle();
    },

    // 刷新「当前会话生效名」：秘书会话是桥上的秘书名，其余会话是设置页名字。
    // 任何异常 / 旧后端没有该方法 / 返回空都静默回落设置页名字——显示名只是
    // 展示层，绝不能因为它失败而打断切换会话、建会话这些主流程。
    async refreshActiveAgentName() {
      // 草稿会话（尚未落库）：后端当前会话还停在上一个真实会话上，此刻问桥
      // 会把上一个会话（可能是秘书）的名字搬进来，故直接按普通会话呈现
      if (!this.currentSessionName) {
        this._applyAgentName(this.personaAgentName);
        return;
      }
      // 静默取桥：刷新动作可能在启动早期（bridgeReady 之前）被触发，
      // _api() 缺桥时会弹「连接失败」，为此弹窗不值得
      const api = (window.pywebview && window.pywebview.api) as PyWebViewAPI | null;
      if (!api || typeof api.get_active_agent_name !== 'function') {
        this._applyAgentName(this.personaAgentName);
        return;
      }
      const sessionAtCall = this.currentSessionName;
      try {
        const res = await api.get_active_agent_name();
        // 竞态：在途期间用户可能已切走，过期名字不覆盖当前会话的显示名
        if (this.currentSessionName !== sessionAtCall) return;
        const name = res && typeof res === 'object' ? String(res.agent_name || '') : '';
        this._applyAgentName(name || this.personaAgentName);
      } catch (err) {
        console.error('刷新当前会话生效名失败:', err);
        this._applyAgentName(this.personaAgentName);
      }
    },

    /** 在当前分组树里定位会话条目；返回 { session, group }，找不到返回 null */
    _findSessionEntry(sessionName: string): { session: any; group: any } | null {
      if (!sessionName) return null;
      for (const g of this.sessionGroups || []) {
        const hit = (g.sessions || []).find((s: any) => s && s.name === sessionName);
        if (hit) return { session: hit, group: g };
      }
      return null;
    },

    // 会话是否属于「秘书」：条目自带的 secretary_name（后端对秘书会话都会带，
    // 未命名回落「秘书」）与分组标记 is_secretary 任一命中即为真——历史遗留的
    // 未命名会话靠分组标记兜底，避免被当成普通会话改了 session_name
    _isSecretarySession(sessionName: string): boolean {
      const hit = this._findSessionEntry(sessionName);
      if (!hit) return false;
      if (hit.group && (hit.group as any).is_secretary) return true;
      return typeof hit.session.secretary_name === 'string';
    },

    // 页面标题跟随智能体显示名（webview/浏览器预览均可见；默认名时保持 星瑶Code 简洁形态）
    syncDocumentTitle() {
      const name = this.agentName || '星瑶';
      document.title = name === '星瑶' ? '星瑶Code' : `星瑶Code · ${name}`;
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

    // AI 生成内容目录（Task 5）：成功/失败都以返回快照校准本地 systemSettings，
    // 失败时后端快照本身即回滚值；桥接异常兜底回滚到调用前的快照
    async setAiOutputDir(path: string): Promise<boolean> {
      const previous = this.systemSettings;
      const api = this._api();
      if (!api) return false;
      try {
        const result = await api.set_ai_output_dir(path);
        if (result && result.ok) {
          this.systemSettings = result.settings || previous;
          this.notifyModal('系统设置', result.msg || 'AI 生成内容目录已更新');
          return true;
        }
        this.systemSettings = result?.settings || previous;
        this.notifyModal('系统设置', 'AI 生成内容目录设置失败: ' + (result?.msg || '未知错误'));
        return false;
      } catch (err) {
        this.systemSettings = previous;
        this.connected = false;
        this.notifyModal('系统设置', 'AI 生成内容目录设置失败: ' + err);
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

    async setContextWatermarkBase(value: number): Promise<boolean> {
      const previous = this.systemSettings;
      const api = this._api();
      if (!api) return false;
      try {
        const result = await api.set_context_watermark_base(value);
        if (result && result.ok) {
          this.systemSettings = result.settings;
          return true;
        }
        this.systemSettings = result?.settings || previous;
        this.notifyModal('系统设置', '上下文水位基准设置失败: ' + (result?.msg || '未知错误'));
        return false;
      } catch (err) {
        this.systemSettings = previous;
        this.connected = false;
        this.notifyModal('系统设置', '上下文水位基准设置失败: ' + err);
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
        const shotSha = String(raw.screen_sha256 || '');
        if (previewUrl && shotSha) this._rememberComputerShot(shotSha, previewUrl);
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

    // 每帧截图索引：按画面哈希记 loopback 预览地址，供对话区电脑动作行反查
    // 该动作当帧的画面。只保留最近 80 帧（Map 语义按插入顺序淘汰最旧），
    // 避免长回合里无界增长；同一帧重复观察只刷新不新增。
    _rememberComputerShot(sha: string, url: string) {
      const shots = this.computerShots;
      if (shots[sha] === url) return;
      shots[sha] = url;
      const keys = Object.keys(shots);
      if (keys.length > 80) {
        for (const key of keys.slice(0, keys.length - 80)) delete shots[key];
      }
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
      this.computerShots = {};
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
      // 切换到已有会话时丢弃未发送的空白 pending 会话；
      // 同时收起子页面：切会话即要看该会话的聊天视图（删除会话后的
      // 自动切换、通知路由等非侧栏入口同样生效）
      this.subpage.show = false;
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
      // 目标会话还没有内存缓存（无消息且未运行）⇒ 历史还在后端返回路上，消息区暂空：
      // 置加载态，把"加载中"与"真正的新对话/空会话"区分开
      this.sessionSwitching = !runtime.running && runtime.messages.length === 0;

      const api = this._api();
      // 后端不可用时不能残留加载态；显示名只用本地分组树兜底（秘书会话取条目里
      // 的秘书名），同样不能停在上一个会话的名字上
      if (!api) {
        this.sessionSwitching = false;
        const hit = this._findSessionEntry(sessionName);
        const localName = hit && hit.session.secretary_name
          ? String(hit.session.secretary_name)
          : this.personaAgentName;
        this._applyAgentName(localName);
        return;
      }
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
          // 历史分页元信息：后端首页游标写入顶层与 runtime（缓存命中分支靠 bind 恢复）
          this.historyHasMore = !!res.history_has_more;
          this.historyOldestId = res.history_oldest_id ?? null;
          runtime.historyHasMore = this.historyHasMore;
          runtime.historyOldestId = this.historyOldestId;
          // 按回合撤销：切换会话后批量拉取该会话的检查点状态（卡片随会话重建）
          void this.refreshTurnUndoStates();
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

        // 红点索引无条件重拉：只在「首次加载该会话」时刷新会漏掉离开期间
        // 新增的用户消息（后台回合、手机端发送），表现为切回后红点少最后一条
        void this.refreshNavIndex(sessionName);

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

        // 会话级权限档位：以后端落库绑定（或全局兜底）校正本地显示，
        // 避免 A 会话选的档位串到 B 会话
        if (res.permission_mode) {
          this.permissionMode = res.permission_mode;
          const runtimePerm = this._getSessionRuntime(sessionName);
          runtimePerm.permMode = res.permission_mode;
        }

        // 生效显示名随会话走：秘书会话显示秘书名，普通会话回到设置页名字
        // （不 await：显示名刷新失败不能拖住会话切换，内部自带回落）
        void this.refreshActiveAgentName();

        // 决策入口按会话重探：先复位再按目标会话探测，切换后入口只反映本会话
        // （不 await：探测失败不能拖住切换，内部自带复位兜底）
        void this.refreshDecisionAvailability(sessionName);
      } catch (err) {
        this.connected = false;
        this.notifyChat('切换会话失败: ' + err);
      } finally {
        // 加载态复位：仅在仍是当前会话时清除——快速连点不同会话时，
        // 过期响应返回不得清掉当前会话的加载态
        if (this.currentSessionName === sessionName) {
          this.sessionSwitching = false;
          // C11：切换流程末端刷新 Hook 信任状态（项目工作目录随会话变化）
          void this.refreshHooks();
        }
      }
    },

    // 历史分页懒加载：滚动到顶触发，向前翻一页并 prepend 到消息流头部。
    // 守卫（无更多/加载中/无游标/后端不可用）与竞态丢弃（在途切会话）必须保持。
    // force=true（红点跨页跳转通道）：跳过 historyHasMore 入口守卫。该标记随
    // 会话 runtime 保存/恢复，可能是过期值或被失败响应清空的坏值——一旦为
    // false 而游标其实还有更早历史，onNavJump 的「无进展」判定会立刻 break，
    // 表现为点未加载区红点没反应/落点不对（1.3.6 改过循环终止判定，漏了这里）。
    // force 时交后端用游标裁决；historyLoading 重入保护保留（并发翻页仍需挡）。
    async loadOlderMessages(force: boolean = false) {
      // 游标自愈：has_more=false 且游标为空，是「分页状态从未初始化 / 被失败响应
      // 清空」的特征——真到最早时后端会带回非空 oldest_id。不修的话守卫会永久
      // 短路：滚到顶不加载更早消息、点未加载红点没反应，而且这份坏状态会随会话
      // 保存/恢复，切走再切回依然失效（用户反馈过「怎么都无法加载到最上面」）。
      // 兜底口径：用已加载消息流里最旧的 _msg_id 当游标，交后端裁决是否还有更早。
      if (!this.historyOldestId) {
        const oldestLoaded = (this.messages as any[]).find(m => typeof m?._msg_id === 'number');
        if (!oldestLoaded) return;
        this.historyOldestId = oldestLoaded._msg_id as number;
        this.historyHasMore = true;
      }
      if ((!this.historyHasMore && !force) || this.historyLoading) return;
      const api = this._api();
      if (!api || typeof (api as any).load_older_messages !== 'function') return;
      const sessionAtCall = this.currentSessionName;
      const oldestAtCall = this.historyOldestId;
      this.historyLoading = true;
      try {
        const res = await (api as any).load_older_messages(sessionAtCall, oldestAtCall);
        if (this.currentSessionName !== sessionAtCall) return; // 竞态：切会话后丢弃过期响应
        if (res?.ok) {
          const older = pairHistoryToolMessages(res.messages || []);
          this.messages = [...older, ...this.messages];
          this.historyHasMore = !!res.has_more;
          this.historyOldestId = res.oldest_id ?? null;
        }
        // res.ok === false（读库失败等）：保持原分页状态不变，下次滚动/跳转自动重试；
        // 绝不写成「没有更早历史」，否则翻页会被永久关掉
      } catch (err) {
        this.notifyChat('加载更早历史失败: ' + err);
      } finally {
        if (this.currentSessionName === sessionAtCall) this.historyLoading = false;
      }
    },

    // 红点导航全量索引：切会话/回合结束/启动后刷新，供 MessageNavRail 全量渲染。
    // sessionName 缺省为当前显示会话；回合终态事件必须显式传入事件所属会话——
    // 只按当前显示会话刷新时，后台会话跑完的回合不会更新它自己的索引，
    // 切回该会话又命中「有内存缓存不重拉」分支，红点会永远少最后一条
    // （2026-09-18 用户反馈：最后一句「修改更新记录」在红点导航里没有红点）。
    async refreshNavIndex(sessionName?: string) {
      const api = this._api();
      if (!api || typeof (api as any).get_session_nav_index !== 'function') return;
      const sessionAtCall = sessionName || this.currentSessionName;
      try {
        const res = await (api as any).get_session_nav_index(sessionAtCall);
        if (this.currentSessionName !== sessionAtCall) {
          // 竞态：为仍可能切回来的会话补写 runtime，避免切回后暂时空档
          if (res?.ok) this._getSessionRuntime(sessionAtCall).navItems = res.items || [];
          return;
        }
        if (res?.ok) {
          this.navItems = res.items || [];
          this._getSessionRuntime(sessionAtCall).navItems = this.navItems;
        }
      } catch { /* 导航索引失败静默，不影响主流程 */ }
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
      // 新会话意味着回到聊天视图：Skills/MCP/插件/自动化等子页面打开时
      // 点侧栏"+"也要收起子页面，否则状态已切换但界面停在子页（像没反应）
      this.subpage.show = false;
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
      // 新会话/新对话不是"历史加载中"：显式清除切换加载态
      this.sessionSwitching = false;
      this._captureRuntime('');
      this.currentWorkdirLabel = '对话';
      this.workdir = '';
      this.fileTree = [];
      this.fileTreeExpanded = [];
      // 新对话是普通会话：生效名回到设置页名字（草稿态尚未落库，问桥会拿回
      // 上一个会话的名字，故 refreshActiveAgentName 内部对草稿直接走回落）
      void this.refreshActiveAgentName();
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
      // 表现为“点新会话特别慢”）。同 createGlobalSession：收起子页面回聊天视图
      this.subpage.show = false;
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
      // 新会话/新对话不是"历史加载中"：显式清除切换加载态
      this.sessionSwitching = false;
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
      // 同 createGlobalSession：新会话（草稿态）的生效名回到设置页名字
      void this.refreshActiveAgentName();
    },

    // 秘书会话：后端建会话时立即落库并切为当前会话（不存在草稿态），所以这里
    // 不能复用 createGlobalSession 的懒创建分支，要按 switchSession 的口径把
    // 会话级状态整体复位到返回的 session_name 上（旧会话状态先由
    // _captureCurrentRuntime 存回它自己的 runtime）。
    // 名字必填：它是这个秘书的身份标识（显示名 + 提示词自称），没有名字就只能
    // 落一个既无名又已切走的会话。前端先做可见校验、非法值不发请求且不截断
    // （后端 validate_secretary_name 会二次校验并回 ok:false）。
    // persona 可选：每个秘书是用户自己创建的独立 agent，人设只属于它这个会话；
    // 留空就用内置默认秘书人设（与设置页的星瑶人设完全无关）。
    async createSecretarySession(name: string, persona = '') {
      const normalized = (name || '').trim();
      // 用码点计数与后端 len() 对齐：emoji 这类代理对不会被算成两个字符
      const nameLength = [...normalized].length;
      if (!normalized) {
        this.notifyModal('新建秘书会话', '请先给这位秘书起个名字（1-20 个字符）');
        return;
      }
      if (nameLength > SECRETARY_NAME_MAX_LEN) {
        this.notifyModal(
          '新建秘书会话',
          `秘书名字最多 ${SECRETARY_NAME_MAX_LEN} 个字符，当前 ${nameLength} 个`,
        );
        return;
      }
      // 人设空白视为"没填"，正常放行（走默认人设）
      const personaDraft = (persona || '').trim();
      if (personaDraft.length > SECRETARY_PERSONA_MAX_LEN) {
        this.notifyModal(
          '新建秘书会话',
          `秘书人设最多 ${SECRETARY_PERSONA_MAX_LEN} 个字符，当前 ${personaDraft.length} 个`,
        );
        return;
      }
      const api = this._api();
      if (!api) return; // _api() 缺桥时已弹「连接失败」，这里不再二次打扰
      try {
        // 标题固定「新会话」：侧栏与欢迎页显示的都是秘书名（后端 display_name），
        // 会话名（session_xxx）不参与命名，这里不需要用户可见的会话标题
        const res = await api.create_secretary_session('新会话', normalized, personaDraft);
        if (!res || !res.ok) {
          // 建会话是用户显式动作：失败必须可见，不做静默降级
          this.notifyModal('新建秘书会话', (res && res.msg) || '创建失败');
          return;
        }
        // 新会话意味着回到聊天视图：子页面打开时也要收起，否则状态已切换但
        // 界面停在子页（表现为"点了没反应"），与 createGlobalSession 同口径
        this.subpage.show = false;
        this._captureCurrentRuntime();
        // 已落库的真实会话：清掉草稿工作区标记，避免首条消息再走"草稿落库"分支
        this.pendingWorkdir = null;
        // 权限确认框属于上一个会话，新会话不该继承它（switchSession 按会话名判定同口径）
        this.permRequest.show = false;
        this.currentSessionName = res.session_name;
        // 显示名优先用秘书名：侧栏条目、欢迎页、对话头部都读它
        this.currentSessionDisplayName = String(res.secretary_name || res.display_name || normalized);
        this._bindSessionRuntime(res.session_name);
        this.messages = [];
        this.currentStreamingText = '';
        this.currentThinkingText = '';
        this.subagentTasks = [];
        this.planData = { title: '', items: [], progress: 0 };
        this.tokenUsage = emptyTokenUsage();
        this.running = false;
        // 新会话不是"历史加载中"：显式清除切换加载态
        this.sessionSwitching = false;
        this._captureRuntime(res.session_name);
        // 秘书会话固定 workdir=GLOBAL：按「对话」工作区呈现，不加载文件树
        this.workdir = '';
        this.currentWorkdirLabel = '对话';
        this.fileTree = [];
        this.fileTreeExpanded = [];
        // 后端已落库：用最新分组树重建列表，新会话立刻出现在秘书分组里
        await this.refreshSessions();
        // 生效名以后端为准（会话已切过去、秘书名已落库），与侧栏显示同一来源
        await this.refreshActiveAgentName();
      } catch (err) {
        this.connected = false;
        this.notifyModal('新建秘书会话', '创建失败: ' + err);
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
      // 调用方（侧栏分组头 ✕）已用 askConfirm 弹窗完成二次确认；
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
          // 归档区行内删除同样走这里：归档会话不在 sessionGroups 中，
          // 故单独摘除并刷新归档列表，避免已删除的条目残留在「已归档」区
          this.archivedSessions = this.archivedSessions.filter(s => s.name !== sessionName);
          void this.loadArchivedSessions();
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

    // 手动重命名会话（侧栏右键菜单入口）。
    // 成功静默完成：后端撞名会自动加后缀，返回的 sessions 才是最终列表，直接覆盖同步；
    // 会话名迁移（当前会话/运行态/状态点）主要由后端 session_renamed 事件负责，
    // 但事件在重连窗口期可能丢失（emit 失败后端也不改判成功），故此处按 res.name 兜底迁移。
    async renameSession(oldName: string, newName: string) {
      const target = (newName || '').trim();
      // 空白名视为无改动，不发请求
      if (!target) return;
      // 秘书会话改的是「秘书名」（secretary_name），不是会话名：先分流。
      // 秘书名与 session_xxx 永不相等，混在下面「与旧名相同」的判断里虽然不会
      // 误伤，但两者语义完全不同，分开更不容易埋坑
      if (this._isSecretarySession(oldName)) return this.renameSecretarySession(oldName, target);
      // 与原会话名相同视为无改动，不发请求
      if (target === oldName) return;
      const api = this._api();
      if (!api) return;
      try {
        const res = await api.rename_session(oldName, target);
        if (res && res.ok) {
          if (res.sessions) this.sessionGroups = res.sessions;
          // 兜底迁移当前会话名：漏掉会导致侧栏高亮消失、后续请求仍用旧名
          if (res.name && res.name !== oldName && this.currentSessionName === oldName) {
            this.currentSessionName = res.name;
            this.currentSessionDisplayName = res.name;
          }
        } else {
          this.notifyModal('会话管理', '重命名失败: ' + (res && res.msg ? res.msg : '未知错误'));
        }
      } catch (err) {
        this.connected = false;
        this.notifyModal('会话管理', '重命名失败: ' + err);
      }
    },

    // 跨文件夹移动会话（侧栏右键「移动到…」入口）。
    // 只改归属：消息、摘要、token 统计、会话级模型绑定都按 session_id 关联，不受影响。
    // 移动的是当前会话时，工作区标签与文件树要一并切到新目录，否则界面仍显示旧项目的文件。
    async moveSessionToWorkdir(sessionName: string, workdir: string) {
      const target = (workdir || '').trim();
      // 空目标不发请求（后端也拒绝）：侧栏只会给出真实分组或 "GLOBAL"
      if (!target) return;
      // 秘书会话归属由 source 决定，后端会拒绝移动，这里先拦掉不发无谓请求
      if (this._isSecretarySession(sessionName)) return;
      const api = this._api();
      if (!api) return;
      try {
        const res = await api.move_session_to_workdir(sessionName, target);
        if (res && res.ok) {
          // 后端回带最新分组树：整份覆盖，免得前端再算一遍分组归属
          if (Array.isArray(res.sessions)) this.sessionGroups = res.sessions;
          else void this.refreshSessions();
          if (sessionName === this.currentSessionName) {
            const wd = String(res.workdir || 'GLOBAL');
            this.workdir = wd;
            this.currentWorkdirLabel = wd === 'GLOBAL' ? '对话' : (wd.split('\\').pop() || wd);
            if (wd === 'GLOBAL') {
              this.fileTree = [];
              this.fileTreeExpanded = [];
            } else {
              // 与 switchSession 同款：文件树后台异步加载，不阻塞交互
              this.loadFileTreeInBackground(this.currentSessionName, wd);
            }
          }
          this.notifyChat(res.moved === false ? '会话已在目标分组' : '会话已移动到新分组');
        } else {
          this.notifyModal('会话管理', '移动失败: ' + (res && res.msg ? res.msg : '未知错误'));
        }
      } catch (err) {
        this.connected = false;
        this.notifyModal('会话管理', '移动失败: ' + err);
      }
    },

    // 秘书会话改名（右键「重命名」入口，目标为秘书会话时由 renameSession 分流到此）。
    // 只改后端的 secretary_name：session_name 仍是建会话时生成的 session_xxx，
    // 所以消息 / 摘要 / 快照 / 侧栏高亮的键都不需要迁移，与普通会话重命名口径不同。
    async renameSecretarySession(sessionName: string, newName: string) {
      const target = (newName || '').trim();
      if (!target) {
        this.notifyModal('会话管理', '秘书名字不能为空（1-20 个字符）');
        return;
      }
      if ([...target].length > SECRETARY_NAME_MAX_LEN) {
        this.notifyModal(
          '会话管理',
          `秘书名字最多 ${SECRETARY_NAME_MAX_LEN} 个字符，当前 ${[...target].length} 个`,
        );
        return;
      }
      const api = this._api();
      if (!api) return;
      try {
        const res = await api.rename_secretary_session(sessionName, target);
        if (res && res.ok) {
          // 后端回带最新分组树（条目里的秘书名已更新），直接覆盖同步
          if (Array.isArray(res.sessions)) this.sessionGroups = res.sessions;
          else void this.refreshSessions();
          // 改的是当前会话时还要刷新生效名：侧栏条目已变，欢迎页 / 对话头部
          // 读的是 store.agentName，漏掉会出现"侧栏已改名、页面还叫旧名"
          if (this.currentSessionName === sessionName) {
            this.currentSessionDisplayName = String(res.secretary_name || res.display_name || target);
            await this.refreshActiveAgentName();
          }
        } else {
          this.notifyModal('会话管理', '重命名失败: ' + (res && res.msg ? res.msg : '未知错误'));
        }
      } catch (err) {
        this.connected = false;
        this.notifyModal('会话管理', '重命名失败: ' + err);
      }
    },

    // 设置秘书专属人设（右键「编辑人设」入口，仅秘书会话可用）。
    // 人设只落该会话自己的 secretary_persona，不碰会话名 / 消息 / 摘要，
    // 也不影响设置页的星瑶人设；传空串 = 恢复内置默认秘书人设。
    async updateSecretaryPersona(sessionName: string, persona: string) {
      if (!this._isSecretarySession(sessionName)) return;
      const draft = (persona || '').trim();
      if (draft.length > SECRETARY_PERSONA_MAX_LEN) {
        this.notifyModal(
          '秘书人设',
          `秘书人设最多 ${SECRETARY_PERSONA_MAX_LEN} 个字符，当前 ${draft.length} 个`,
        );
        return;
      }
      const api = this._api();
      if (!api) return;
      try {
        const res = await api.set_secretary_persona(sessionName, draft);
        if (res && res.ok) {
          // 后端回带最新分组树（条目里的人设已更新），直接覆盖同步
          if (Array.isArray(res.sessions)) this.sessionGroups = res.sessions;
          else void this.refreshSessions();
          // 人设是在下一轮请求里生效的：当前会话提示"改完即生效"要如实说明
          this.notifyChat(draft ? '秘书人设已更新，下一轮对话生效' : '已恢复默认秘书人设');
        } else {
          this.notifyModal('秘书人设', '保存失败: ' + (res && res.msg ? res.msg : '未知错误'));
        }
      } catch (err) {
        this.connected = false;
        this.notifyModal('秘书人设', '保存失败: ' + err);
      }
    },

    // 加载归档会话列表（设置页「会话归档」分区数据源）。
    // 旧后端 / 纯浏览器预览没有该方法时静默保持现状，不弹连接失败框。
    async loadArchivedSessions() {
      const api = (window.pywebview && window.pywebview.api) as PyWebViewAPI | null;
      if (!api || typeof api.get_archived_sessions !== 'function') return;
      const seq = ++this.archivedSessionsReqSeq;
      try {
        const list = await api.get_archived_sessions();
        if (seq !== this.archivedSessionsReqSeq) return;
        if (Array.isArray(list)) this.archivedSessions = list;
      } catch (err) {
        // 静默失败：归档区不是主路径，下一次归档动作/事件到达时会重新拉取
        console.error('加载归档会话失败:', err);
      }
    },

    /** 取会话的展示名（归档 / 取消归档的反馈文案用；查不到时退回会话名） */
    _sessionDisplayName(sessionName: string): string {
      const hit = this.sessionGroups
        .flatMap(g => g.sessions || [])
        .find(s => s.name === sessionName);
      if (hit && hit.display_name) return hit.display_name;
      const archived = this.archivedSessions.find(s => s.name === sessionName);
      return (archived && archived.display_name) || sessionName;
    },

    // 归档会话（侧栏行内 ⤓ 入口）。入口侧已改为行内二次确认（点确认才真正归档），
    // 成功后与删除一致静默完成——会话从列表消失、归档区出现即是反馈，不再弹提示框。
    // 后端 session_archived 事件在重连窗口期可能丢失，故按返回值/主动刷新兜底，
    // 不做乐观更新（失败时保持原状）。
    async archiveSession(sessionName: string) {
      const api = this._api();
      if (!api || !sessionName) return false;
      // 归档与「禁止删除当前活跃会话」同源的保护：归档态会话正在被使用是自相矛盾的状态
      if (sessionName === this.currentSessionName) {
        this.notifyModal('会话归档', '不能归档当前活跃会话，请先切换到其它会话');
        return false;
      }
      try {
        const res = await api.archive_session(sessionName);
        if (res && res.ok) {
          if (Array.isArray(res.sessions)) {
            this.sessionGroups = res.sessions;
          } else {
            // 桥接方法不回传列表时本地摘除，避免「已归档却仍在主列表」的中间态
            this.sessionGroups = this.sessionGroups.map(g => ({
              ...g,
              sessions: (g.sessions || []).filter(s => s.name !== sessionName),
            }));
          }
          if (Array.isArray(res.archived_sessions)) {
            this.archivedSessions = res.archived_sessions;
          } else {
            await this.loadArchivedSessions();
          }
          // 归档成功静默完成：入口侧的二次确认已表明意图，列表刷新即为反馈
          return true;
        }
        this.notifyModal('会话归档', '归档失败: ' + (res && res.msg ? res.msg : '未知错误'));
        return false;
      } catch (err) {
        this.connected = false;
        this.notifyModal('会话归档', '归档失败: ' + err);
        return false;
      }
    },

    // 取消归档（归档区行内 ↩ 入口）：会话恢复回主列表。同样可逆、不弹确认框。
    async unarchiveSession(sessionName: string) {
      const api = this._api();
      if (!api || !sessionName) return false;
      const display = this._sessionDisplayName(sessionName);
      try {
        const res = await api.unarchive_session(sessionName);
        if (res && res.ok) {
          if (Array.isArray(res.archived_sessions)) {
            this.archivedSessions = res.archived_sessions;
          } else {
            this.archivedSessions = this.archivedSessions.filter(s => s.name !== sessionName);
            await this.loadArchivedSessions();
          }
          if (Array.isArray(res.sessions)) this.sessionGroups = res.sessions;
          else await this.refreshSessions();
          this.notifyModal('会话归档', `已取消归档「${display}」，已恢复到会话列表`);
          return true;
        }
        this.notifyModal('会话归档', '取消归档失败: ' + (res && res.msg ? res.msg : '未知错误'));
        return false;
      } catch (err) {
        this.connected = false;
        this.notifyModal('会话归档', '取消归档失败: ' + err);
        return false;
      }
    },

    /** 批量归档操作的结果收尾：采用后端回传的两侧快照，缺省时主动刷新兜底 */
    _applyArchiveBatchLists(res: ArchiveBatchResult | null | undefined) {
      if (!res) return;
      if (Array.isArray(res.sessions)) this.sessionGroups = res.sessions;
      else void this.refreshSessions();
      if (Array.isArray(res.archived_sessions)) this.archivedSessions = res.archived_sessions;
      else void this.loadArchivedSessions();
    },

    // 批量取消归档（设置页归档区多选恢复）：与单条 unarchiveSession 同语义（可逆、
    // 幂等、不弹确认框），差别只在一次桥接处理多个会话、只弹一次结果提示。
    // 返回本次成功恢复的会话名数组；失败项不回传，调用方据此保留这些行的勾选。
    async batchUnarchiveSessions(sessionNames: string[]): Promise<string[]> {
      const api = this._api();
      const names = (sessionNames || []).filter(n => !!n);
      if (!api || !names.length) return [];
      try {
        const res = await api.unarchive_sessions(names);
        if (!res || !res.ok) {
          this.notifyModal('会话归档', '批量恢复失败: ' + (res && res.msg ? res.msg : '未知错误'));
          return [];
        }
        this._applyArchiveBatchLists(res);
        const done = Array.isArray(res.names) ? res.names : [];
        const failed = Array.isArray(res.failed) ? res.failed : [];
        this.notifyModal(
          '会话归档',
          failed.length
            ? `已恢复 ${done.length} 个会话，${failed.length} 个失败：${failed.map(f => f.msg || f.name).join('；')}`
            : `已恢复 ${done.length} 个会话到会话列表`,
        );
        return done;
      } catch (err) {
        this.connected = false;
        this.notifyModal('会话归档', '批量恢复失败: ' + err);
        return [];
      }
    },

    // 批量删除归档会话（设置页归档区多选删除）：二次确认由调用方（设置页）负责，
    // 这里只落盘并同步本地状态。返回本次真正删除的会话名数组。
    async batchDeleteArchivedSessions(sessionNames: string[]): Promise<string[]> {
      const api = this._api();
      const names = (sessionNames || []).filter(n => !!n);
      if (!api || !names.length) return [];
      try {
        const res = await api.delete_archived_sessions(names);
        const done = res && res.ok && Array.isArray(res.names) ? res.names : [];
        if (!done.length) {
          const failed = res && Array.isArray(res.failed) ? res.failed : [];
          const reason = failed.length
            ? failed.map(f => f.msg || f.name).join('；')
            : (res && res.msg ? res.msg : '未知错误');
          this.notifyModal('会话管理', '批量删除失败: ' + reason);
          // 一项都没删掉时列表可能已被别的入口改动，仍刷新一次兜底
          void this.loadArchivedSessions();
          return [];
        }
        this._applyArchiveBatchLists(res);
        // 逐个清理前端运行态残留（照 deleteSingleSession 的 T11 修复：否则
        // runningSessions / sessionRuntimeByName 里留下已删会话的孤儿条目）
        done.forEach(name => {
          this.runningSessions = this.runningSessions.filter(s => s !== name);
          this.queuedSessions = this.queuedSessions.filter(s => s !== name);
          delete this.messageQueues[name];
          delete this.sessionRuntimeByName[name];
        });
        // 极端并发下删到了当前活跃会话：后端已补建 GLOBAL 会话，跟随切换
        if (res && res.current_session && done.includes(this.currentSessionName)) {
          await this.switchSession(res.current_session, '新会话');
        }
        const failed = Array.isArray(res.failed) ? res.failed : [];
        if (failed.length) {
          this.notifyModal(
            '会话管理',
            `已删除 ${done.length} 个归档会话，${failed.length} 个失败：${failed.map(f => f.msg || f.name).join('；')}`,
          );
        }
        return done;
      } catch (err) {
        this.connected = false;
        this.notifyModal('会话管理', '批量删除失败: ' + err);
        return [];
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
      this.remoteDirBrowser.plainPick = null;
    },

    // ---- 纯选目录（星轨自动化任务表单等）：桌面走原生对话框，失败回退内置浏览器 ----
    async pickFolderPlain(cb: (path: string) => void) {
      const api = this._api();
      if (!api) return;
      if (!(window.matchMedia && window.matchMedia('(pointer: coarse)').matches)) {
        try {
          const res = await (api as any).pick_folder_dialog();
          if (res && res.ok && res.path) {
            cb(String(res.path));
            return;
          }
          // 用户主动取消原生对话框：静默返回，不弹内置浏览器
          if (res && !res.ok && res.msg === '已取消选择') return;
        } catch { /* 原生对话框不可用时回退内置浏览器 */ }
      }
      this.remoteDirBrowser.plainPick = cb;
      await this.openRemoteDirBrowser();
    },

    async confirmRemoteWorkspace(useCurrent = false) {
      const api = this._api();
      if (!api) return;
      // 未勾选时允许直接把当前浏览目录作为工作区（"使用当前目录"按钮）
      const path = this.remoteDirBrowser.selected
        || (useCurrent ? this.remoteDirBrowser.current : '');
      // 纯选目录模式：只把路径交给回调，不登记工作区、不迁移会话归属
      if (this.remoteDirBrowser.plainPick) {
        if (!path) return;
        const picked = this.remoteDirBrowser.plainPick;
        this.closeRemoteDirBrowser();
        picked(path);
        return;
      }
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
          // pending 草稿会话选过的权限档位随首条消息带给后端补绑定
          this._getSessionRuntime(sessionName).permMode || undefined,
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

    // 会话 token 弹层数据：每次打开都重拉（面板是瞬时视图，不做增量维护）
    async loadTokenUsageOverview(limit = 20) {
      const api = this._api();
      if (!api) return null;
      this.tokenUsageOverviewLoading = true;
      try {
        const result = await api.get_token_usage_overview(limit);
        if (result && result.ok) this.tokenUsageOverview = result;
        return result;
      } catch (err) {
        console.error('加载会话 Token 概览失败:', err);
        return null;
      } finally {
        this.tokenUsageOverviewLoading = false;
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

    /** 合并宿主回推的内嵌浏览器状态快照（字段强转，容忍缺省/多余字段） */
    applyEmbeddedBrowserState(this: any, data: Record<string, unknown> | null | undefined) {
      const src = (data || {}) as Record<string, unknown>;
      const s = this.embeddedBrowser;
      s.url = String(src.url ?? '');
      s.title = String(src.title ?? '');
      s.can_go_back = !!src.can_go_back;
      s.can_go_forward = !!src.can_go_forward;
      s.loading = !!src.loading;
      s.ready = !!src.ready;
      s.visible = !!src.visible;
    },

    /**
     * 秘书调度台 open_session 桥事件处理：把界面带到后端已切好的目标会话。
     *
     * 视图切换复用用户点会话的那条路径（switchSession：丢弃 pending 草稿、
     * 清流式态、按会话拉历史、恢复模型/权限/排队消息、刷新生效名），
     * 这里只做「取载荷 + 兜底」两件事，不复制切换逻辑。
     * 会话名缺失（后端解析异常 / 事件字段缺失）时静默记一条日志：跳转失败
     * 不该弹窗、更不该让桥事件管线抛错。切换失败同理，只留日志。
     */
    async _handleOpenSessionEvent(event: BridgeEvent) {
      // 载荷口径：后端信封是 {type, data}；个别来源可能用 {type, payload} 平铺
      const raw = ((event as any).data ?? (event as any).payload ?? {}) as Record<string, unknown>;
      const sessionName = String(raw.session_name || '').trim();
      if (!sessionName) {
        console.warn('[open_session] 桥事件缺少 session_name，已忽略本次跳转');
        return;
      }
      const displayName = String(raw.display_name || '').trim();
      try {
        // 不传 workdir：目标是后端已存在的会话，其归属目录由后端权威副本决定
        // （display_name 只在后端给了生效名时才覆盖本地兜底名）
        await this.switchSession(sessionName, displayName || undefined);
      } catch (err) {
        // switchSession 内部已弹「切换会话失败」提示，这里只补一条静默日志，
        // 绝不让异常冒泡回桥事件派发点
        console.warn(`[open_session] 跳转到会话 ${sessionName} 失败:`, err);
      }
    },

    // ── 背景图转换（视频 → 动画 WebP 动态背景）─────────────────────
    // 后端：GuiBridge.pick_background_video / describe_background_video /
    // pick_background_output_dir / start_background_convert /
    // cancel_background_convert / install_converted_background
    async pickBackgroundVideo() {
      const api = this._api();
      if (!api) return;
      this.bgConvert.message = '';
      this.bgConvert.error = '';
      const res = await api.pick_background_video();
      if (!res || !res.ok) {
        const message = (res as { error?: { message?: string } } | undefined)?.error?.message || '';
        // 用户主动取消不算错误
        if (message && !message.includes('取消')) this.bgConvert.error = message;
        return;
      }
      const info = res as unknown as BackgroundVideoInfo;
      this.bgConvert.video = info;
      this.bgConvert.resultPath = '';
      this.bgConvert.resultBytes = 0;
      this.bgConvert.percent = 0;
      this.bgConvert.etaSeconds = null;
      // 输出目录默认取源视频所在目录（用户仍可自行改选）
      if (!this.bgConvert.outputDir) {
        this.bgConvert.outputDir = info.path.replace(/[\\/][^\\/]*$/, '');
      }
    },
    async pickBackgroundOutputDir() {
      const api = this._api();
      if (!api) return;
      this.bgConvert.error = '';
      const res = await api.pick_background_output_dir();
      if (res && res.ok) {
        this.bgConvert.outputDir = (res as unknown as { path: string }).path;
      } else {
        const message = (res as { error?: { message?: string } } | undefined)?.error?.message || '';
        if (message && !message.includes('取消')) this.bgConvert.error = message;
      }
    },
    async startBackgroundConvert() {
      const api = this._api();
      if (!api) return;
      const video = this.bgConvert.video;
      if (!video) { this.bgConvert.error = '请先选择要转换的视频'; return; }
      if (!video.can_convert) {
        this.bgConvert.error = video.blockers[0] || '该视频无法转换';
        return;
      }
      if (!this.bgConvert.outputDir) { this.bgConvert.error = '请先选择输出文件夹'; return; }
      this.bgConvert.error = '';
      this.bgConvert.message = '';
      this.bgConvert.percent = 0;
      this.bgConvert.etaSeconds = null;
      this.bgConvert.resultPath = '';
      this.bgConvert.resultBytes = 0;
      const res = await api.start_background_convert(
        video.path, this.bgConvert.outputDir,
        this.bgConvert.fps, this.bgConvert.quality, this.bgConvert.pingpong,
      );
      if (!res || !res.ok) {
        this.bgConvert.error = (res && res.msg) || '启动转换失败';
        return;
      }
      this.bgConvert.running = true;
      this.bgConvert.resultPath = res.output || '';
    },
    async cancelBackgroundConvert() {
      const api = this._api();
      if (!api) return;
      await api.cancel_background_convert();
    },
    async installConvertedBackground() {
      const api = this._api();
      if (!api) return;
      const path = this.bgConvert.resultPath;
      if (!path) return;
      this.bgConvert.message = '';
      this.bgConvert.error = '';
      const res = await api.install_converted_background(path);
      if (res && res.ok) {
        this.bgConvert.message = '已设为当前主题的全局背景图（重启应用或切换主题后生效）';
      } else {
        this.bgConvert.error =
          (res as { error?: { message?: string } } | undefined)?.error?.message || '应用到主题失败';
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
      // Git 工作台事件（规格 §10）：外部变化只触发失效刷新，转发给 gitStore，
      // 不参与会话 runtime 归属
      if (type === 'git_state_changed') {
        useGitStore().onExternalChange(data);
        return;
      }
      // 拖放文件路径推送（GUI 线程死锁修复后改走 _js_queue 事件管线）：
      // 转发给 useAttachments 挂在 window 上的匹配逻辑，不参与会话 runtime 归属
      if (type === 'dropped_paths_push') {
        const push = (window as unknown as { __ycode_on_drop_paths?: (p: unknown) => void }).__ycode_on_drop_paths;
        if (push) push(data);
        return;
      }
      // ── 背景图转换进度（GuiBridge 的 bg_convert_* 事件）──
      // 全局单任务，不与会话 runtime 归属相关，处理完直接返回
      if (type === 'bg_convert_progress') {
        this.bgConvert.percent = Number(data.percent) || 0;
        this.bgConvert.stage = String(data.stage || '');
        this.bgConvert.etaSeconds =
          typeof data.eta_seconds === 'number' ? data.eta_seconds : null;
        if (data.stage === 'starting' || data.stage === 'extracting'
            || data.stage === 'assembling' || data.stage === 'encoding'
            || data.stage === 'converting') {
          this.bgConvert.running = true;
        }
        return;
      }
      if (type === 'bg_convert_done') {
        this.bgConvert.running = false;
        this.bgConvert.percent = 100;
        this.bgConvert.stage = 'done';
        this.bgConvert.etaSeconds = null;
        this.bgConvert.resultPath = String(data.path || '');
        this.bgConvert.resultBytes = Number(data.bytes) || 0;
        this.bgConvert.message = '转换完成';
        return;
      }
      if (type === 'bg_convert_error') {
        this.bgConvert.running = false;
        this.bgConvert.etaSeconds = null;
        this.bgConvert.error = String(data.message || '转换失败');
        return;
      }
      // 前端构建产物运行期更新（gui_main 的 dist watchdog 推送 fingerprint）：
      // 触发「资源版本失配自愈」——等界面空闲时自动刷新到最新版本，
      // 避免旧构建哈希 chunk 404（mermaid 等懒加载失败）；
      // 不参与会话 runtime 归属，处理完直接返回
      if (type === 'frontend_dist_updated') {
        // 把后端 fingerprint（mtime:size）纳入判断：与上次触发刷新的指纹相同
        // 说明构建并未真正更替，自愈模块会拦下重复刷新（防同一指纹反复整页重载）
        const fingerprint = typeof data.fingerprint === 'string' ? data.fingerprint : undefined;
        requestFrontendReload('dist-updated', fingerprint);
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
      // 内嵌浏览器状态推送（宿主 WebView2 控件的导航/标题/加载态回推）：
      // 独立支路，不参与会话 runtime 归属，合并完直接返回
      if (type === 'embedded_browser') {
        this.applyEmbeddedBrowserState(data);
        return;
      }
      // 内置浏览器自动化（星瑶经 CDP 驱动右侧面板浏览器）：携带项目分辨率，
      // 面板切到浏览器页签并按该分辨率做适应窗口呈现（用户实时围观）。
      // 独立支路，不参与会话 runtime 归属；页签按会话归属打开——
      // A 会话发起的自动化不在当前查看的 B 会话抢屏
      if (type === 'embedded_browser_automation') {
        const width = Number(data.width);
        const height = Number(data.height);
        if (Number.isFinite(width) && Number.isFinite(height) && width > 0 && height > 0) {
          // 每次换新数组引用：相同分辨率再次下发时组件侧 watch 也能触发
          this.embeddedBrowser.automationViewport = [width, height];
          this.openBrowserTabForSession(String(data.session_name || this.currentSessionName || ''));
        }
        return;
      }
      // 秘书调度台跳转（系统层带用户去看某个会话）：后端 open_session 工具切换
      // 当前会话后推本事件。视图切换一律复用 switchSession（清流式态、按会话拉
      // 历史、刷新生效名），这里不另写一套；独立支路，不参与会话 runtime 归属，
      // 处理完直接返回（切换过程是异步的，故 void 掉，内部自带兜底不抛）
      if (type === 'open_session') {
        void this._handleOpenSessionEvent(event);
        return;
      }
      // 决策记录实时推送（JEV 每次决策落审计后由桥转发）：头部「决策」按钮
      // 累加未读徽章，面板开着时按 tick 拉取刷新；独立支路，不参与会话
      // runtime 归属，处理完直接返回
      if (type === 'decision_recorded') {
        // 入口与徽章口径是「当前会话」：其他会话的决策只让面板按 tick 重拉，
        // 既不累加未读、也不点亮当前会话入口（否则切到 A 会话会看到 B 的决策）
        const decisionSession = String(data.session_id || '').trim();
        if (decisionSession && decisionSession !== this.currentSessionName) {
          this.decisionEventTick += 1;
          return;
        }
        this.decisionEventTick += 1;
        // 探针（scene="probe"）只证明链路通了，不算真实决策：与后端
        // has_business_records 的口径保持一致。否则跑一次探针就会点亮入口，
        // 而下次启动探测又按 has_business_records=false 把它熄灭（入口闪烁）。
        if (String(data.scene || '') === 'probe') return;
        this.decisionUnread += 1;
        // 刚发生一次真实决策：入口立刻出现，不用等下次启动探测
        this.decisionHasRecords = true;
        return;
      }
      // 按回合撤销：撤销/恢复事务状态变更（数据库提交成功后后端才发）。
      // 只更新对应会话和回合，不串到当前正在查看的其他会话（规格 16/25.5）
      if (type === 'turn_undo_updated') {
        const undoSession = String(data.session_name || '');
        if (undoSession === this.currentSessionName && data.turn_id) {
          void this.refreshTurnUndoStates();
        }
        if (data.message) {
          this.notifyChatLocal(String(data.message), 'notice', undoSession || undefined);
        }
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
      if (switchedRuntime) {
        // 绑走前先快照当前会话：翻页 prepend 的历史、推进的分页游标等顶层
        // 实时进度只在顶层、尚未入快照；直接绑走会被目标会话快照覆盖丢弃，
        // finally 回绑时拿到旧快照——后台会话有事件到达时，用户刚翻上去的
        // 历史被反复弹回、再也加载不出来（2026-10-15 用户反馈）。
        this._captureRuntime(currentRuntimeKey, { skipLayout: true });
        // 跨会话临时绑定只切换会话数据，不恢复目标会话布局：顶层布局保持
        // 当前查看会话的实时状态，事件处理完在 finally 原样带回（否则目标
        // 会话的布局快照会覆盖用户刚对面板做的开合/页签操作）
        this._bindSessionRuntime(targetRuntimeKey, { skipLayout: true });
      }

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
        // 按回合撤销：权威 turn_id 绑定到本轮用户消息（撤销卡片定位键）。
        // 乐观渲染的消息在 turn_start 到达时补上权威 turnId
        if (data.turn_id) {
          for (let i = this.messages.length - 1; i >= 0; i--) {
            const m = this.messages[i] as ChatMessage;
            if (m.role === 'user' && m.content === turnUserText && !m.turnId) {
              m.turnId = String(data.turn_id);
              break;
            }
          }
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
          // 事件中转捕获：跨会话事件跳过布局段（顶层布局仍属当前查看会话）
          this._captureRuntime(targetRuntimeKey, { skipLayout: switchedRuntime });
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
          // 事件中转捕获：跨会话事件跳过布局段（顶层布局仍属当前查看会话）
          this._captureRuntime(targetRuntimeKey, { skipLayout: switchedRuntime });
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
          undoImpact: (data.undo_impact && typeof data.undo_impact === 'object')
            ? data.undo_impact
            : null,
        };
        this.permRequest.show = this.permRequest.sessionName === this.currentSessionName;
        // 非当前窗口等待用户确认 → 黄点
        this._setSessionStatusDot(eventSession, 'yellow');
      } else if (type === 'permission_dismiss') {
        // 后端权限队列已清空（请求被超时拒绝/取消）：收起内联权限卡片，避免死请求残留。
        // 必须整体重置：仅 show=false 会残留 reqId/sessionName，
        // switchSession 重显逻辑会把已失效的卡片复活，点击后报“权限响应发送失败”
        this.permRequest = { show: false, sessionName: '', reqId: '', summary: '', source: '', allowAlways: false, options: [], permissionOptions: [], functionName: '', undoImpact: null };
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
      } else if (type === 'automation_tasks_changed') {
        // 星轨自动化：任务全量快照，幂等替换（与 message_queue 快照同模式）
        const tasks = Array.isArray(data.tasks) ? (data.tasks as AutomationTaskInfo[]) : [];
        this.automationTasks = tasks;
      } else if (type === 'automation_run_updated') {
        // 星轨自动化：单条运行行更新——按 id 幂等替换或插入历史头部，上限 50 条
        const run = data.run as AutomationRunInfo | undefined;
        if (run && run.task_id) {
          const list = this.automationRuns[run.task_id] || [];
          const idx = list.findIndex(r => r.id === run.id);
          const next = idx >= 0 ? list.map(r => (r.id === run.id ? run : r)) : [run, ...list];
          this.automationRuns = { ...this.automationRuns, [run.task_id]: next.slice(0, 50) };
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
      } else if (type === 'session_moved') {
        // 跨文件夹移动会话（事件可能来自其他客户端，如手机端）：整份覆盖分组列表即可。
        // 移动只改归属，消息/摘要/token/模型绑定都按 session_id 关联，无需迁移任何会话态
        if (Array.isArray(data.sessions)) this.sessionGroups = data.sessions;
        else void this.refreshSessions();
        // 被移动的正是本端当前会话：工作区标签与文件树跟着切到新目录
        if (data.name && data.name === this.currentSessionName && data.workdir) {
          const wd = String(data.workdir);
          this.workdir = wd;
          this.currentWorkdirLabel = wd === 'GLOBAL' ? '对话' : (wd.split('\\').pop() || wd);
          if (wd === 'GLOBAL') {
            this.fileTree = [];
            this.fileTreeExpanded = [];
          } else {
            this.loadFileTreeInBackground(this.currentSessionName, wd);
          }
        }
      } else if (type === 'session_archived' || type === 'session_unarchived' || type === 'session_deleted') {
        // 归档态变化（多端同步，含归档区批量删除）：负载带最新两侧快照时直接采用；
        // 缺失时主动刷新兜底。不做乐观增删——迟到的旧快照不得让已归档会话在主列表里「复活」。
        if (Array.isArray(data.sessions)) this.sessionGroups = data.sessions;
        else void this.refreshSessions();
        if (Array.isArray(data.archived_sessions)) this.archivedSessions = data.archived_sessions;
        else void this.loadArchivedSessions();
      } else if (type === 'turn_finish' || type === 'turn_idle') {
        this.running = false;
        this.streamingSpeed = 0;
        speedTracker.reset();
        // 回合封闭后检查点进入 ready/no_changes：刷新批量撤销状态，
        // 让本轮文件卡片切到权威净 diff 与撤销按钮
        void this.refreshTurnUndoStates();
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
        // 按事件会话刷新红点索引：用户可能已切走，此时只刷当前显示会话
        // 会让这个会话的红点永久缺最后一条
        void this.refreshNavIndex(eventSession || undefined);
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
        // 取消的回合里用户消息同样已落库：不刷新则红点缺这一条
        void this.refreshNavIndex(eventSession || undefined);
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
        // 报错的回合里用户消息同样已落库：不刷新则红点缺这一条
        void this.refreshNavIndex(eventSession || undefined);
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
          // 系统设置里的「当前模型电脑操控资格」跟着生效模型走，换模型后
          // 旧快照立刻过期；后台静默重拉，设置页再打开时不会是陈旧的模型名
          void this.refreshSystemSettings({ silent: true });
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
      } else if (type === 'skills_loaded') {
        // 异步技能扫描完成（后端 _async_build_skill_list 推送）：
        // get_initial_state 不再同步扫盘（冷盘首次可达 25s+，会让已出现的窗口一直转圈），
        // 改为先返回上次缓存、后台扫完通过此事件推送到前端刷新技能页与 / 补全候选。
        if (Array.isArray(data.skills)) {
          this.skills = data.skills;
          // 事件已到达即代表"扫描结束"，无论结果是否为空：空列表是真实的"无技能"，
          // 技能页据此从"加载中"切回"暂无可用技能"
          this.skillsLoading = false;
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
          ...(t.type ? { type: String(t.type) } : {}),
          ...(t.background ? { background: true } : {}),
          ...(Array.isArray(t.stages) ? {
            stages: t.stages.map((s: any) => ({
              name: String(s.name || ''),
              at: Number(s.at) || 0,
              detail: String(s.detail || ''),
            })),
          } : {}),
        }));
        target.subagent_tasks = snapshots;
        this.subagentTasks = snapshots;
      } else if (type === 'background_subagent_terminal') {
        const statusLabel: Record<string, string> = {
          completed: '已完成', failed: '已失败', timed_out: '已超时', cancelled: '已取消',
        };
        const label = statusLabel[data.status] || data.status;
        const desc = data.description ? `：${data.description}` : '';
        // 按归属会话路由（eventSession 来自 data.session_name）：提示落在
        // 任务所属会话，不串到用户正在查看的其他会话
        if (data.status === 'completed') {
          this.notifyChatLocal(`后台子代理${label}${desc}`, 'notice', eventSession);
        } else {
          // 与 error 事件一致：回合进行中不插队，挂在当前助手消息上展示
          const last = this.messages[this.messages.length - 1];
          if (last && last.role === 'assistant') {
            last.error_note = `后台子代理${label}${desc}`;
            this.notifyChatLocal(`后台子代理${label}${desc}`, 'notice', eventSession);
          } else {
            this.messages.push({ role: 'error', content: `后台子代理${label}${desc}` });
          }
        }
      } else if (type === 'background_task_finished') {
        // 后台命令任务（execute_command 的 run_in_background，如打包）完成通知；
        // 按归属会话路由，缺省（旧后端）才落到当前会话
        const code = Number(data.returncode);
        const tid = String(data.task_id || '');
        const statusText = code === 0 ? '已完成' : `已结束（退出码 ${code}）`;
        this.notifyChatLocal(`后台任务 ${tid} ${statusText}`, 'notice', String(data.session_name || '') || undefined);
      } else if (type === 'pty_data') {
        // 底部内嵌交互式终端：原始输出直接走 ptyBus 给 xterm，不进响应式状态
        emitPtyData(String(data.text ?? ''));
      } else if (type === 'pty_exit') {
        emitPtyExit(Number(data.code ?? 0));
      } else if (type === 'snip_attachment_ready') {
        // 屏幕截图「发到输入框」（宿主 gui_snip_mixin.SnipBridgeMixin）：把
        // 回传的图片路径加入当前会话输入框附件。与 pty_data 同约定——必须
        // 接进本函数这条既有 type-if 链（window.onBridgeEvent 唯一赋值点），
        // 另开通道等于绕开单消费者约定
        void this._attachSnipToComposer(String(data.path || ''));
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
        // 同会话事件全量捕获（用户的面板开合由此落入快照）；跨会话事件
        // 跳过布局段，避免把当前会话的实时布局写进目标会话的快照
        this._captureRuntime(targetRuntimeKey, { skipLayout: switchedRuntime });
        if (type === 'session_renamed' && data.old && data.new) {
          const oldKey = String(data.old);
          const newKey = String(data.new);
          if (this.sessionRuntimeByName[oldKey]) {
            this.sessionRuntimeByName[newKey] = this.sessionRuntimeByName[oldKey];
            delete this.sessionRuntimeByName[oldKey];
          }
          if (this.currentSessionName === newKey) this._bindSessionRuntime(newKey);
        } else if (switchedRuntime) {
          // 回绑只恢复会话数据（消息/流式/运行态等），不恢复面板布局：
          // 用户在事件间隙对右面板做的开合是实时操作面，被旧快照回滚即
          // "切换面板点了没反应/自动弹回"的根因
          this._bindSessionRuntime(currentRuntimeKey, { skipLayout: true });
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
          // 同步交互拿到的是最终列表（后端已顺带更新其缓存），结束"加载中"态
          this.skillsLoading = false;
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
            // 手动刷新拿到的是最终结果，结束"加载中"态
            this.skillsLoading = false;
            return true;
          }
        }
        if (api.get_skills) {
          const skills = await api.get_skills();
          if (Array.isArray(skills)) {
            this.skills = skills;
            this.skillsLoading = false;
            return true;
          }
        }
        return false;
      } catch (err) {
        this.notifyModal('技能', '刷新技能列表失败: ' + err);
        return false;
      }
    },

    // 恢复内置版本：用内置源覆盖用户改动后的技能副本。
    // 与 refreshSkills 同风格——成功时直接用后端回传的 skills 覆盖列表，
    // 失败统一走 notifyModal 提示 msg，不静默吞掉错误。
    async restoreSkill(skillId: string): Promise<boolean> {
      const api = this._api();
      if (!api) return false;
      try {
        const res = await api.restore_skill(skillId);
        if (res && res.ok && Array.isArray(res.skills)) {
          this.skills = res.skills;
          // 同步交互拿到的是最终列表，结束"加载中"态
          this.skillsLoading = false;
          return true;
        }
        this.notifyModal('技能', '恢复内置版本失败: ' + (res && res.msg ? res.msg : '未知错误'));
        return false;
      } catch (err) {
        this.connected = false;
        this.notifyModal('技能', '恢复内置版本失败: ' + err);
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

    // 删除自定义 MCP 服务：从 mcp.json 移除配置，结果通过 mcp_status 事件回推
    async removeMcpServer(name: string) {
      const api = this._api();
      if (!api) return;
      try {
        const res = await api.remove_mcp_server(name);
        if (res && res.ok) {
          this.mcps = res.mcps || this.mcps;
          this.notifyModal('MCP 服务', res.msg || '已删除');
        } else {
          this.notifyModal('MCP 服务', '删除失败: ' + (res && res.msg ? res.msg : '未知错误'));
        }
      } catch (err) {
        this.connected = false;
        this.notifyModal('MCP 服务', '删除 MCP 服务失败: ' + err);
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
          // 插件启停会增删 provider 与模型（opencode_zen 启用时自举 provider、
          // 禁用时按来源标记回收），只更新 plugins 会让模型列表停在旧快照，
          // 用户必须重启星瑶才能看到变化。与 add_provider 同一模式：
          // 后端回传 settings 就即时应用，模型选择器随开关立即刷新。
          if (res.settings) this.applyModelSettings(res.settings);
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

    // 插件设置页：页面快照由插件每次返回完整结构（服务端驱动），前端不做增量合并。
    async loadPluginSettings(name: string): Promise<any> {
      const api = this._api();
      if (!api || typeof api.get_plugin_settings !== 'function') {
        return { ok: false, msg: '当前版本不支持插件设置页' };
      }
      try {
        return await api.get_plugin_settings(name);
      } catch (err) {
        this.connected = false;
        return { ok: false, msg: '读取插件设置页失败: ' + err };
      }
    },

    async runPluginSettingsAction(name: string, action: string, params: Record<string, unknown> = {}): Promise<any> {
      const api = this._api();
      if (!api || typeof api.run_plugin_settings_action !== 'function') {
        return { ok: false, msg: '当前版本不支持插件设置动作' };
      }
      try {
        return await api.run_plugin_settings_action(name, action, params);
      } catch (err) {
        this.connected = false;
        return { ok: false, msg: '执行插件动作失败: ' + err };
      }
    },

    async openExternalUrl(url: string): Promise<boolean> {
      const api = this._api();
      if (!api || typeof api.open_external_url !== 'function') return false;
      try {
        const res = await api.open_external_url(url);
        return !!(res && res.ok);
      } catch {
        return false;
      }
    },

    // C11：Hook 信任状态刷新（启动/切会话/信任动作后重拉最新快照）
    async refreshHooks() {
      const api = this._api();
      // 旧桥接没有 get_hooks：静默跳过，信任卡片不显示
      if (!api || typeof api.get_hooks !== 'function') return;
      // 序号防护：过期响应不覆盖新快照
      const seq = ++this.hooksReqSeq;
      this.hooksLoading = true;
      try {
        const res = await api.get_hooks();
        if (seq !== this.hooksReqSeq) return;
        if (res && res.ok) this.hooks = res.hooks || [];
      } catch {
        // 静默失败，保留现有列表
      } finally {
        if (seq === this.hooksReqSeq) this.hooksLoading = false;
      }
    },

    // C11：Hook 信任动作统一入口（method: grant/deny/revoke/reset_hook_trust）
    async _hookTrustAction(this: any, method: string, digest: string) {
      const api = this._api();
      if (!api || typeof (api as any)[method] !== 'function') return;
      try {
        const res = await (api as any)[method](digest);
        if (res && res.ok) {
          this.hooks = res.hooks || this.hooks;
          // 递增刷新序号：本次动作已拿到最新快照，在途的旧 refreshHooks 响应作废
          this.hooksReqSeq++;
        } else if (res && res.msg) {
          this.notifyModal('Hook 信任', res.msg);
        }
      } catch (err) {
        // 连接状态由事件流维护：信任动作失败不等于桥接断线，不在此误置 connected
        console.error('Hook 信任操作失败:', err);
        this.notifyModal('Hook 信任', 'Hook 信任操作失败: ' + err);
      }
    },

    grantHookTrust(digest: string) { return this._hookTrustAction('grant_hook_trust', digest); },
    denyHookTrust(digest: string) { return this._hookTrustAction('deny_hook_trust', digest); },
    revokeHookTrust(digest: string) { return this._hookTrustAction('revoke_hook_trust', digest); },
    resetHookTrust(digest: string) { return this._hookTrustAction('reset_hook_trust', digest); },

    // C13：Hook 手动配置（新建/编辑/删除/启停；成功后回传刷新列表，失败返回 { ok:false, msg } 由调用方提示）
    async saveHook(payload: Record<string, any>): Promise<{ ok: boolean; msg?: string }> {
      const api = this._api();
      if (!api) return { ok: false, msg: '桥未就绪' };
      try {
        const res = await api.save_hook(payload);
        if (res && res.ok) {
          this.hooks = res.hooks || this.hooks;
          // 递增刷新序号：本次动作已拿到最新快照，在途的旧 refreshHooks 响应作废
          this.hooksReqSeq++;
          return { ok: true };
        }
        return { ok: false, msg: (res && res.msg) || '保存 Hook 失败' };
      } catch (err) {
        return { ok: false, msg: String(err) };
      }
    },

    async deleteHook(sourcePath: string, digest: string): Promise<{ ok: boolean; msg?: string }> {
      const api = this._api();
      if (!api) return { ok: false, msg: '桥未就绪' };
      try {
        const res = await api.delete_hook(sourcePath, digest);
        if (res && res.ok) {
          this.hooks = res.hooks || this.hooks;
          this.hooksReqSeq++;
          return { ok: true };
        }
        return { ok: false, msg: (res && res.msg) || '删除 Hook 失败' };
      } catch (err) {
        return { ok: false, msg: String(err) };
      }
    },

    async setHookDisabled(sourcePath: string, digest: string, disabled: boolean): Promise<{ ok: boolean; msg?: string }> {
      const api = this._api();
      if (!api) return { ok: false, msg: '桥未就绪' };
      try {
        const res = await api.set_hook_disabled(sourcePath, digest, disabled);
        if (res && res.ok) {
          this.hooks = res.hooks || this.hooks;
          this.hooksReqSeq++;
          return { ok: true };
        }
        return { ok: false, msg: (res && res.msg) || (disabled ? '禁用 Hook 失败' : '启用 Hook 失败') };
      } catch (err) {
        return { ok: false, msg: String(err) };
      }
    },

    // C11：一键信任/拒绝全部待决项目级 Hook（先快照数组，逐个串行避免遍历中变更）
    async grantAllPendingHookTrusts() {
      const pending = [...this.pendingProjectHooks];
      for (const hook of pending) {
        await this.grantHookTrust(hook.digest);
      }
    },

    async denyAllPendingHookTrusts() {
      const pending = [...this.pendingProjectHooks];
      for (const hook of pending) {
        await this.denyHookTrust(hook.digest);
      }
    },

    // ---- 星轨自动化任务 ----
    async refreshAutomations(): Promise<boolean> {
      const api = this._api();
      if (!api) return false;
      try {
        const res = await api.get_automation_tasks();
        if (res && res.ok && Array.isArray(res.tasks)) {
          this.automationTasks = res.tasks;
          return true;
        }
        return false;
      } catch (err) {
        this.notifyModal('星轨自动化任务', '读取星轨自动化任务失败: ' + err);
        return false;
      }
    },

    async saveAutomationTask(task: Record<string, any>): Promise<{ ok: boolean; errors?: string[]; msg?: string }> {
      const api = this._api();
      if (!api) return { ok: false, msg: '桥未就绪' };
      try {
        const res = await api.save_automation_task(task);
        // 字段校验错误原样透传给表单逐条展示
        if (res && !res.ok && Array.isArray(res.errors)) return res;
        if (res && res.ok) await this.refreshAutomations();
        return res || { ok: false, msg: '未知错误' };
      } catch (err) {
        return { ok: false, msg: String(err) };
      }
    },

    async toggleAutomationTask(taskId: string, enabled: boolean) {
      const api = this._api();
      if (!api) return;
      try {
        const res = await api.set_automation_task_enabled(taskId, enabled);
        if (!res || !res.ok) {
          this.notifyModal('星轨自动化任务', '启停失败: ' + ((res && res.msg) || '未知错误'));
        }
      } catch (err) {
        this.notifyModal('星轨自动化任务', '启停失败: ' + err);
      }
    },

    async deleteAutomationTask(taskId: string) {
      const api = this._api();
      if (!api) return;
      try {
        const res = await api.delete_automation_task(taskId);
        if (res && res.ok) {
          const runs = { ...this.automationRuns };
          delete runs[taskId];
          this.automationRuns = runs;
          await this.refreshAutomations();
        } else {
          this.notifyModal('星轨自动化任务', '删除失败: ' + ((res && res.msg) || '未知错误'));
        }
      } catch (err) {
        this.notifyModal('星轨自动化任务', '删除失败: ' + err);
      }
    },

    async runAutomationTaskNow(taskId: string): Promise<{ ok: boolean; msg?: string }> {
      const api = this._api();
      if (!api) return { ok: false, msg: '桥未就绪' };
      try {
        const res = await api.run_automation_task_now(taskId);
        if (res && !res.ok && res.msg) {
          this.notifyModal('星轨自动化任务', res.msg);
        }
        return res || { ok: false, msg: '未知错误' };
      } catch (err) {
        this.notifyModal('星轨自动化任务', '执行失败: ' + err);
        return { ok: false, msg: String(err) };
      }
    },

    async loadAutomationRuns(taskId: string, limit = 50) {
      const api = this._api();
      if (!api || !taskId) return;
      try {
        const res = await api.get_automation_runs(taskId, limit);
        if (res && res.ok && Array.isArray(res.runs)) {
          this.automationRuns = { ...this.automationRuns, [taskId]: res.runs };
        }
      } catch {
        // 历史加载失败不打断页面，重开抽屉会重试
      }
    },

    async clearAutomationRuns(taskId: string) {
      const api = this._api();
      if (!api) return;
      try {
        const res = await api.clear_automation_runs(taskId);
        if (res && res.ok) {
          this.automationRuns = { ...this.automationRuns, [taskId]: [] };
        }
      } catch {
        // 同上：静默重试路径
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

    // ---------------- 内嵌终端（右侧面板版已下线，交互式终端见 ptyTerminal*） ----------------

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

    // ------------------------------------------------------------------
    // 按回合撤销（规格 16）：批量状态、预检/执行、惰性 diff、中断恢复
    // ------------------------------------------------------------------

    async refreshTurnUndoStates() {
      const api = this._api();
      const session = this.currentSessionName;
      if (!api || !session || !api.list_turn_undo_states) return;
      const seq = ++this.turnUndoReqSeq;
      try {
        const res = await api.list_turn_undo_states(session);
        if (seq !== this.turnUndoReqSeq || !res || !res.ok) return;
        const map: Record<string, TurnUndoStateSummary> = {};
        for (const state of res.states || []) {
          map[state.turn_id] = state;
        }
        this.turnUndoStates = map;
      } catch {
        // 批量状态查询失败不打断会话；卡片退回旧展示
      }
    },

    async previewTurnUndo(turnId: string):
        Promise<{ preview: TurnUndoPreviewPayload; token: string } | null> {
      const api = this._api();
      if (!api || !api.preview_turn_undo) return null;
      try {
        const res = await api.preview_turn_undo(this.currentSessionName, turnId);
        if (res && res.ok) {
          return { preview: res.preview, token: res.preview_token || '' };
        }
        this.notifyChat(res.msg || '预检失败');
        return null;
      } catch (err) {
        this.notifyChat('预检失败: ' + err);
        return null;
      }
    },

    async executeTurnUndo(turnId: string, previewToken: string): Promise<TurnUndoExecutePayload | null> {
      const api = this._api();
      if (!api || !api.execute_turn_undo) return null;
      try {
        const res = await api.execute_turn_undo(this.currentSessionName, turnId, previewToken);
        if (res && res.ok) {
          await this.refreshTurnUndoStates();
          return res.result;
        }
        this.notifyChat(res.msg || '撤销失败');
        await this.refreshTurnUndoStates();
        return null;
      } catch (err) {
        this.notifyChat('撤销失败: ' + err);
        return null;
      }
    },

    async fetchTurnUndoStatus(turnId: string) {
      const api = this._api();
      if (!api || !api.get_turn_undo_status) return null;
      try {
        const res = await api.get_turn_undo_status(this.currentSessionName, turnId);
        return res && res.ok ? res.status : null;
      } catch {
        return null;
      }
    },

    async fetchTurnUndoFileDiff(turnId: string, fileId: string) {
      const api = this._api();
      if (!api || !api.get_turn_undo_file_diff) return null;
      try {
        const res = await api.get_turn_undo_file_diff(this.currentSessionName, turnId, fileId);
        return res && res.ok ? res.diff : null;
      } catch {
        return null;
      }
    },

    async previewTurnUndoRecovery(operationId: string, action: 'continue_revert' | 'cancel_revert'):
        Promise<{ preview: TurnUndoRecoveryPreviewPayload; token: string } | null> {
      const api = this._api();
      if (!api || !api.preview_turn_undo_recovery) return null;
      try {
        const res = await api.preview_turn_undo_recovery(this.currentSessionName, operationId, action);
        if (res && res.ok) {
          return { preview: res.preview, token: res.preview_token || '' };
        }
        return null;
      } catch {
        return null;
      }
    },

    async executeTurnUndoRecovery(
      operationId: string, action: 'continue_revert' | 'cancel_revert', previewToken: string,
    ): Promise<TurnUndoRecoveryExecutePayload | null> {
      const api = this._api();
      if (!api || !api.execute_turn_undo_recovery) return null;
      try {
        const res = await api.execute_turn_undo_recovery(
          this.currentSessionName, operationId, action, previewToken,
        );
        if (res && res.ok) {
          await this.refreshTurnUndoStates();
          return res.result;
        }
        this.notifyChat(res.msg || '恢复操作失败');
        await this.refreshTurnUndoStates();
        return null;
      } catch (err) {
        this.notifyChat('恢复操作失败: ' + err);
        return null;
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

    // 屏幕截图「发到输入框」桥事件（snip_attachment_ready）落库：把图片路径
    // 加入当前会话输入框附件。不复用 useAttachments.addPaths——composable 绑
    // 定组件生命周期（onUnmounted 注册等），store 内写等价最小实现：
    // 去重 → 查文件信息补全 name/ext/size（失败退化为 baseName）→ 补缩略图
    async _attachSnipToComposer(path: string): Promise<void> {
      if (!path) return;
      const runtime = this._getSessionRuntime();
      if (!runtime.composerAttachments) runtime.composerAttachments = [];
      const target = runtime.composerAttachments;
      // 去重：事件重放或重复截图同一文件时不叠加 chip
      if (target.some(a => a.path === path)) return;
      const infos = await this.getFilesInfo([path]);
      const info = infos.find(i => i.path === path);
      const sepIdx = Math.max(path.lastIndexOf('\\'), path.lastIndexOf('/'));
      const baseName = sepIdx >= 0 ? path.slice(sepIdx + 1) : path;
      const name = info?.name || baseName;
      const item: AttachmentItem = {
        path,
        name,
        ext: info?.ext ?? fileExt(name),
        size: info?.size ?? null,
      };
      target.push(item);
      // 缩略图是增强能力：归档取受控媒体 URL 后补挂，失败静默
      try {
        const thumbs = await this.fetchAttachmentThumbnails([path]);
        const url = thumbs.get(path);
        if (url) item.thumb = url;
      } catch { /* 缩略图失败静默，不影响附件 */ }
      this.notifyChat('截图已加入输入框附件');
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
        this.tokenStatisticsDays = days;
        return result;
      } catch (err) {
        this.connected = false;
        this.notifyModal('Token 统计', '加载 Token 统计失败: ' + err);
        return null;
      } finally {
        this.tokenStatisticsLoading = false;
      }
    },

    async loadTokenStatisticsLifetime(force = false) {
      if (!force && this.tokenStatisticsLifetime) {
        return this.tokenStatisticsLifetime;
      }
      const api = this._api();
      if (!api) return null;
      this.tokenStatisticsLifetimeLoading = true;
      try {
        const result = await api.get_token_usage_statistics(365);
        if (!result || !result.ok) {
          this.notifyModal('Token 统计', '加载 Token 统计失败: ' + (result?.msg || '未知错误'));
          return result;
        }
        this.tokenStatisticsLifetime = result;
        return result;
      } catch (err) {
        this.connected = false;
        this.notifyModal('Token 统计', '加载 Token 统计失败: ' + err);
        return null;
      } finally {
        this.tokenStatisticsLifetimeLoading = false;
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

    async addModel(providerId: string, modelName: string, contextWindow: number, supportsVision = false, supportsFunctionCalling = true, computerProtocol: ComputerProtocolChoice = '', maxTokens: number | null = null, reasoningChoice: string | null = null) {
      const api = this._api();
      if (!api) return null;
      try {
        const res = await api.add_model(providerId, modelName, contextWindow, supportsVision, supportsFunctionCalling, computerProtocol, maxTokens, reasoningChoice);
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

    async updateModel(providerId: string, modelId: string, modelName: string, contextWindow: number, supportsVision: boolean, supportsFunctionCalling = false, computerProtocol: ComputerProtocolChoice = '', maxTokens: number | null = null, reasoningChoice: string | null = null) {
      const api = this._api();
      if (!api) return null;
      try {
        const res = await api.update_model(providerId, modelId, modelName, contextWindow, supportsVision, supportsFunctionCalling, computerProtocol, maxTokens, reasoningChoice);
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

    async fetchProviderModels(providerId: string): Promise<{ ok: boolean; msg?: string; models?: { id: string; model_type?: string }[]; count?: number } | null> {
      const api = this._api();
      if (!api) return null;
      try {
        return await api.fetch_provider_models(providerId);
      } catch (err) {
        this.connected = false;
        this.notifyModal('获取模型列表', '获取失败: ' + err);
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

    // 后台任务快照（list_active_tasks 只返回 running 项）：任务对话框打开、
    // 应用启动等时机调用；桥未就绪（浏览器开发态）时静默保留上次快照
    async refreshBackgroundTasks() {
      if (!(window.pywebview && window.pywebview.api)) return;
      try {
        const list = await window.pywebview.api.get_background_tasks();
        // list_active_tasks 只返回 running 任务且字段为 duration/pid：统一归一为
        // 前端展示字段（status 恒为 running，running_time 取 duration）；
        // session_id 为发起会话名，执行条经 currentSessionBackgroundTasks 按会话过滤；
        // output_size/last_output_age/no_output_yet 为输出心跳，右面板任务页签
        // 运行期提示用（旧后端缺省 undefined/null）
        this.backgroundTasks = (Array.isArray(list) ? list : []).map((t: any) => ({
          task_id: String(t?.task_id ?? ''),
          command: String(t?.command ?? ''),
          status: 'running',
          running_time: String(t?.duration ?? '') || '活跃',
          session_id: String(t?.session_id ?? ''),
          pid: Number(t?.pid ?? 0) || undefined,
          turn_id: String(t?.turn_id ?? ''),
          output_size: Number(t?.output_size ?? 0),
          last_output_age: (t?.last_output_age ?? null) === null ? null : Number(t.last_output_age),
          no_output_yet: t?.no_output_yet === true,
        }));
      } catch {
        // 拉取失败保留上次快照，不打断界面
      }
    },

    // 决策入口可见性探测：入口显示口径是「当前会话是否产生过真实业务决策」，
    // 全局历史不再点亮（别的会话做过决策不能让当前会话出现入口）。
    // 后端只认业务场景记录（排除 scene="probe" 的探针）。启动时调一次；
    // 桥未就绪（浏览器开发态）静默跳过。
    async refreshDecisionAvailability(sessionName?: string) {
      // 每次探测先复位：避免上一会话的 true 残留，把入口错误地留在当前会话上
      this.decisionHasRecords = false;
      if (!(window.pywebview && window.pywebview.api)) return;
      const target = (sessionName ?? this.currentSessionName ?? '').trim();
      // 无会话名（新会话懒创建阶段）时不请求后端：不能退回全局口径
      if (!target) return;
      try {
        const res = await window.pywebview.api.get_decision_audit_summary(1, target);
        if (res?.ok && res.has_business_records === true) {
          this.decisionHasRecords = true;
        }
      } catch {
        // 探测失败按"没有记录"处理：入口不显示，不影响其余头部功能
      }
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
          `星瑶Code v${this.updateState.latestVersion} 安装包下载完成`,
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
          `准备安装 星瑶Code v${this.updateState.latestVersion}`,
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
