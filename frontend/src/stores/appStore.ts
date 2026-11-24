import { defineStore } from 'pinia';
import { useGitStore } from './gitStore';
import type {
  AutomationRunInfo,
  AutomationTaskInfo,
  WorkflowInfo,
  WorkflowInvalidInfo,
  WorkflowRunInfo,
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
  PlanTaskItem,
  PlanTaskStatus,
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
  FfmpegComponentStatus,
  SearchComponentStatus,
  PlaywrightComponentStatus,
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
  ResumePlanSnapshot,
  ResumeBlockerItem,
  SlashCommandInfo,
  GoalStateInfo,
  GitSnapshotDto,
  WorkspacePromptSuggestion,
  EmbeddedBrowserSurfacePhase,
} from '../types/pywebview';
import { emitPtyData, emitPtyExit } from '../utils/ptyBus';
import { isDocxFilePath, isMarkdownFilePath, isOfficeFilePath, isPdfFilePath, toFileUrl } from '../utils/localPath';
import { fileExt } from '../utils/fileIcon';
import { buildMarkdownPreviewHtml } from '../utils/previewHtml';
import { requestFrontendReload } from '../utils/frontendUpdate';
import type { MediaItem } from '../types/media';
import type { AttachmentItem } from '../types/composer';
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

const stopFallbackTimers = new Map<string, ReturnType<typeof setTimeout>>();

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

function pdfEditTimestamp(now: Date = new Date()): string {
  const pad = (n: number): string => String(n).padStart(2, '0');
  return `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}`
    + `_${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`;
}

export const TERMINAL_PANEL_OPEN_KEY = 'ycode-terminal-open';
export const TERMINAL_PANEL_HEIGHT_KEY = 'ycode-terminal-height';
export const TERMINAL_PANEL_DEFAULT_HEIGHT = 300;
export const TERMINAL_PANEL_MIN_HEIGHT = 140;
export const TERMINAL_PANEL_MAX_HEIGHT = 720;

export const SECRETARY_NAME_MAX_LEN = 20;
export const SECRETARY_PERSONA_MAX_LEN = 20000;

export type RightPanelTabId =
  | 'files' | 'git' | 'browser' | 'doc' | 'tasks' | 'computer' | 'subagent' | 'novel';
// 合法页签 id 白名单：恢复会话快照时据此过滤过期 id，避免 PANE_LABELS 取到 undefined
const RIGHT_PANEL_TAB_IDS = new Set<RightPanelTabId>([
  'files', 'git', 'browser', 'doc', 'tasks', 'computer', 'subagent', 'novel',
]);

function cloneGuiTheme(theme: GuiTheme): GuiTheme {
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
    total_prompt: Number(data.total_prompt ?? 0),
    total_completion: Number(data.total_completion ?? 0),
    total_cached: Number(data.total_cached ?? 0),
    // 车道身份与保活单列：命中率按 (会话, 模型, 接口, scope) 独立，
    // 界面必须标清楚数字属于哪条车道；旧后端无这些字段时回落 ""/null（不冒充 0%）
    cache_lane_model: String(data.cache_lane_model ?? ''),
    cache_lane_api_base: String(data.cache_lane_api_base ?? ''),
    cache_keepalive_hot_pct: nullableNumber(data.cache_keepalive_hot_pct),
    cache_keepalive_session_pct: nullableNumber(data.cache_keepalive_session_pct),
    session_totals: normalizeSessionTokenTotals(data.session_totals),
    context_breakdown: normalizeContextBreakdown(data.context_breakdown),
  };
};

const emptyTokenUsage = (): TokenUsage => normalizeTokenUsage();

const COMPUTER_EVENT_TYPES = new Set([
  'computer_state',
  'computer_action_started',
  'computer_action_finished',
  'computer_observation',
]);

const COMPUTER_SNAPSHOT_FIELDS = [
  'run_id', 'run_generation', 'event_seq', 'owner_session_id', 'owner_turn_id',
  'status', 'target', 'browser', 'browser_mode', 'model_name', 'protocol', 'started_at',
  'action_count', 'current_action', 'current_surface', 'preview_url',
  'pause_reason', 'stop_reason', 'error_code', 'hotkey_available',
  'observation_updated_at_ms', 'observation_error', 'image_omitted', 'current_frame_id',
] as const;

const COMPUTER_BAR_STATUSES = [
  'starting', 'running', 'paused', 'waiting_for_user',
  'provider_safety_required', 'stopping', 'failed',
];

const COMPUTER_TERMINAL_STATUSES = ['completed', 'stopped', 'failed'];

const COMPUTER_TRACE_MAX = 200;
const COMPUTER_PENDING_EVENTS_MAX = 64;

interface ComputerRefreshFlight {
  promise: Promise<ComputerRunSnapshot | null> | null;
  rerun: boolean;
}
const computerRefreshFlights = new WeakMap<object, ComputerRefreshFlight>();

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
  wait_for: '等待条件',
  sequence: '连续操作',
  navigate: '导航',
  back: '后退',
  forward: '前进',
  reload: '刷新',
  request_user_takeover: '请求用户接管',
  list_surfaces: '列出窗口',
  activate_surface: '切换窗口',
  invoke_surface: '唤醒窗口',
  observe_region: '局部观察',
  list_elements: '列出控件',
  click_element: '点击控件',
  set_value_element: '填写控件',
};

const emptyComputerRun = (): ComputerRunSnapshot => ({
  run_id: '',
  run_generation: 0,
  event_seq: 0,
  owner_session_id: '',
  owner_turn_id: '',
  status: 'idle',
  target: '',
  browser: '',
  browser_mode: '',
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
  observation_updated_at_ms: 0,
  observation_error: '',
  image_omitted: '',
  current_frame_id: '',
});

// 平铺信封识别：既有事件是 {"type","data"}（无顶层 run_id），电脑操控事件带顶层
// run_id——按 type 集合 + run_id 存在性分流，绝不误伤普通事件
function isComputerFlatEvent(event: BridgeEvent | undefined): boolean {
  return !!event
    && COMPUTER_EVENT_TYPES.has(String(event.type || ''))
    && typeof event.run_id === 'string';
}

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
  _msg_id?: number;
  role: 'user' | 'assistant' | 'tool' | 'error' | 'notice';
  content?: string;
  images?: string[];
  attachment_notes?: string[];
  reasoning_content?: string;
  tool_calls?: ToolExecutionCall[];
  tool_call_id?: string;
  name?: string;
  cancelled?: boolean;
  // 未绑定到任何 tool_call 的历史文件变更（diff_ready 缺少/失配 tool_call_id 时兜底）
  legacy_diffs?: LegacyDiff[];
  execution_state?: ExecutionState;
  error_note?: string;
  model_id?: string;
  model_name?: string;
  media?: MediaItem[];
  metrics?: MessageMetrics;
  turnId?: string;
  positionKind?: string;
  turnElapsedS?: number;
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

export function pairHistoryToolMessages(messages: ChatMessage[]): ChatMessage[] {
  const toolMessages = new Map<string, ChatMessage>();
  for (const m of messages) {
    if (m.role === 'tool' && m.tool_call_id) toolMessages.set(m.tool_call_id, m);
  }
  const paired: ChatMessage[] = [];
  for (const raw of messages) {
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

export type ConfirmTone = 'danger' | 'warning' | 'primary' | 'info' | 'error';

export interface ConfirmState {
  show: boolean;
  title: string;
  sub: string;
  message: string;
  confirmLabel: string;
  // notice 模式：单按钮提示框（只有"确定"），用于必须让用户看到的非对话类提示
  notice?: boolean;
  tone?: ConfirmTone;
  resolve?: (ok: boolean) => void;
}

export interface PermReqState {
  show: boolean;
  sessionName: string;
  reqId: string;
  summary: string;
  source: string;
  allowAlways: boolean;
  options: string[];
  permissionOptions: Array<{ key: string; label: string }>;
  // 请求来源工具名：'ask_user' 时为澄清问题，否则为标准权限请求
  functionName: string;
  undoImpact?: Record<string, any> | null;
}

export type SubpageType = 'skills' | 'mcp' | 'plugins' | 'models' | 'automations' | 'workflows';

// 星流工作流：导入结果（成功条目 / 失败原因，exists=同名已存在待确认覆盖）
export interface WorkflowImportedItem { name: string; path: string; }
export interface WorkflowImportFailure { name: string; error: string; exists?: boolean; }

const speedTracker = new StreamingSpeedTracker();

// 页面是否由本机回环地址加载（桌面 pywebview 宿主）：
// 手机经局域网 IP 访问时 hostname 是 192.168.x.x 之类，桌面专属桥
// （open_path / preview_office_doc 等）在 LanServer.FORBIDDEN_METHODS 里被
// fail-closed 封禁，调用只会拿到 "method not allowed"。桌面专属分支据此提前
// 收手并给出可读提示，不发起注定失败的调用。无主机名（file:// 等）按本机处理，
// 避免误伤不经过 HTTP 的宿主形态。
export function isLoopbackHostName(host: unknown): boolean {
  const h = String(host ?? '').trim().toLowerCase();
  if (!h) return true;
  return h === 'localhost' || h === '127.0.0.1' || h === '::1' || h === '[::1]';
}

export function isLoopbackPageHost(): boolean {
  try {
    return isLoopbackHostName(window.location.hostname);
  } catch {
    return true;
  }
}

export const useAppStore = defineStore('app', {
  state: () => ({
    models: [] as ModelInfo[],
    providers: [] as ProviderInfo[],
    modelProtocols: [] as ApiProtocolInfo[],
    modelPresets: [] as PresetProviderInfo[],
    activeModelId: '',
    activeModelName: '',
    secondaryModelId: '',
    secondaryModelRoles: { vision: true, compression: true, subagent: true } as { vision: boolean; compression: boolean; subagent: boolean },

    permissionMode: 'confirm' as PermissionMode,

    sandboxEnabled: false,

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

    systemSettings: {
      command_shell: 'auto',
      command_shell_options: [],
      command_shell_available: true,
      command_shell_error: '',
      remote_token_ttl: 86400,
      agent_turn_step_limit: 200,
      agent_turn_step_limit_min: 10,
      agent_turn_step_limit_max: 1000,
      context_watermark_base: null,
      context_watermark_base_min: 50000,
      context_watermark_base_max: 2000000,
      ai_output_dir: '',
      ai_output_dir_setting: '',
      ai_output_dir_default: '',
      subagent_enabled: true,
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
        target: '', browser: '', browser_mode: '', model_name: '', protocol: 'none', started_at: 0,
        action_count: 0, current_action: '', current_surface: '', preview_url: '',
        pause_reason: '', stop_reason: '', error_code: '', hotkey_available: false,
      },
    } as SystemSettingsSnapshot,

    computerRun: emptyComputerRun(),
    computerTrace: [] as ComputerActionTrace[],
    computerShots: {} as Record<string, string>,
    computerFrameShots: {} as Record<string, { url: string; frame_id?: string; image_width?: number; image_height?: number }>,
    computerLastAutoOpenedRunId: '',
    computerControlMutating: false,
    _computerRunGeneration: 0,
    _computerEventSeq: 0,
    _computerActiveActionOrdinal: 0,
    _computerPendingEvents: [] as ComputerControlEvent[],
    _computerActionStartMs: {} as Record<number, number>,
    // 已关闭 run 黑名单（不参与渲染）：清空运行态后拒绝该 run 的迟到事件与快照，
    // 防止「操控」页签被旧事件复活
    _computerClosedRunId: '',

    playwrightComponent: {
      installed: false,
      version: '',
      downloading: false,
      percent: 0,
      downloaded_bytes: 0,
      total_bytes: 0,
      error: '',
      stage: '',
      dir: '',
    } as PlaywrightComponentStatus,

    ffmpegComponent: {
      installed: false,
      version: '',
      downloading: false,
      percent: 0,
      downloaded_bytes: 0,
      total_bytes: 0,
      error: '',
      stage: '',
      dir: '',
    } as FfmpegComponentStatus,

    searchComponent: {
      installed: false,
      version: '',
      downloading: false,
      percent: 0,
      downloaded_bytes: 0,
      total_bytes: 0,
      error: '',
      stage: '',
      dir: '',
    } as SearchComponentStatus,

    reasoning: {
      kind: 'hidden',
      visible: false,
      disabled: false,
      disabled_reason: '',
      choice: 'default',
      options: [],
    } as ReasoningSnapshot,

    sessionGroups: [] as SessionGroup[],
    archivedSessions: [] as ArchivedSessionInfo[],
    currentSessionName: '',
    currentSessionDisplayName: '新对话',
    pendingWorkdir: null as string | null,
    // pending 草稿会话预选的模型：草稿期不落库（后端 _current_session 还指向上一个
    // 真实会话，此时调 set_model 会污染旧会话），首条消息发送时随 send_user_message
    // 一并提交落库；发送失败回退草稿时保留以便重发
    draftModelId: '',
    _sessionModelVersion: {} as Record<string, number>,
    _reasoningChoiceVersion: {} as Record<string, number>,
    currentWorkdirLabel: '对话',
    workdir: '',
    fileTree: [] as FileNode[],
    // 文件树刷新中标记（驱动刷新按钮的 loading 态，并防止并发刷新）
    fileTreeRefreshing: false,
    fileTreeExpanded: [] as string[],
    fileTreeReqSeq: 0,
    workspacePromptSuggestions: [] as WorkspacePromptSuggestion[],
    workspacePromptReqSeq: 0,
    usedSkills: [] as string[],
    turnUndoStates: {} as Record<string, TurnUndoStateSummary>,
    turnUndoReqSeq: 0,
    // M31 修复：各类异步刷新的请求序号，响应返回时序号已过期则丢弃，
    // 避免并发/重复刷新时旧快照覆盖新快照
    sessionsReqSeq: 0,
    archivedSessionsReqSeq: 0,
    tokenUsageReqSeq: 0,
    mcpReqSeq: 0,
    pluginsReqSeq: 0,
    hooksReqSeq: 0,
    filePreviewReqSeq: 0,

    skills: [] as SkillInfo[],
    // 技能列表是否仍在后台扫描中：首屏 get_initial_state 不再同步扫盘（改为
    // skills_loaded 事件推送），初始为空数组并不代表"没有技能"，技能页据此
    // 显示"加载中"而不是"暂无可用技能"，避免误导用户
    skillsLoading: false,
    mcps: [] as McpInfo[],
    plugins: [] as PluginInfo[],
    // 斜杠命令清单（后端注册表只读投影）：输入框 "/" 补全的候选源之一。
    // 默认空数组：接口不可用/加载失败时补全面板退化为「技能 + MCP」两路，不报错
    slashCommands: [] as SlashCommandInfo[],
    // /goal 目标状态快照（GoalBar 的数据源）：null = 当前会话没有目标 / 尚未加载。
    // 取数由组件驱动（挂载探测 + 有目标时每 5 秒轮询），store 只提供 loadGoalState，
    // 类目上跟 slashCommands 一样属于"后端只读投影"，失败静默不打断对话
    goalState: null as GoalStateInfo | null,
    goalStateReqSeq: 0,
    hooks: [] as HookInfo[],
    hooksLoading: false,

    automationTasks: [] as AutomationTaskInfo[],
    automationRuns: {} as Record<string, AutomationRunInfo[]>,

    workflows: [] as WorkflowInfo[],
    workflowInvalid: [] as WorkflowInvalidInfo[],
    workflowsLoading: false,
    workflowRuns: {} as Record<string, WorkflowRunInfo[]>,
    workflowLiveEvents: {} as Record<string, import('../types/pywebview').WorkflowRunEvent[]>,

    messages: [] as ChatMessage[],
historyHasMore: false,
historyOldestId: null as number | null,
historyLoading: false,
navItems: [] as Array<{ id: number; preview: string }>,
    running: false,
    turnStartedAt: 0,
    streamingSpeed: 0,
    runningSessions: [] as string[],
    queuedSessions: [] as string[],
    messageQueues: {} as Record<string, QueuedMessage[]>,
    sessionStatusDots: {} as Record<string, 'green' | 'yellow' | 'red'>,
    // 断点续跑（S4）：后端 resume_plan 判定的可续跑快照（父回合 / 断点位置 /
    // 待确认项）。仅当它存在且没有降级原因时，错误条旁才出现「从断点继续」按钮
    resumePlan: null as ResumePlanSnapshot | null,
    resumeBlockers: [] as ResumeBlockerItem[],
    resumeBusy: false,
    sessionRuntimeByName: {} as Record<string, SessionRuntimeState>,
    // 会话切换加载中：切到"已有会话"时，目标会话历史仍在后端返回路上（消息区为空）
    // 的加载态。用于把"加载中"与"真正的新对话/空会话"区分开，避免切换瞬间
    // 先渲染空消息区而闪出首屏空状态（新对话欢迎页）卡片。
    // 仅 switchSession 置位/复位，新建会话（createGlobalSession/createSessionInGroup）直接置 false。
    sessionSwitching: false,
    showThinking: false,

    lightbox: { show: false, images: [] as string[], index: 0 },

    connected: false,
    version: '',
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
    rightPanelCollapsed: true,
    wideSplitDismissed: false,
    wideSplitAutoExpanded: false,
    rightPanelPicker: false,
    rightPanelTabs: [] as RightPanelTabId[],
    rightPanelActiveTab: '' as RightPanelTabId | '',
    // 用户明确点击折叠过当前会话的右面板：后台/自动开页签只登记并切入页签，绝不推翻手动折叠偏好
    rightPanelUserCollapsed: false,
    // 右面板画笔截图模式：冻结帧覆盖层挂出中。置位后浏览器页签的
    // shouldShowSurface 门控会让原生 WebView2 表面 hide（冻结帧已含其像素，
    // 原生控件必须让位给画笔覆盖层）；关闭后既有 watch 自动 re-show。
    // 模态瞬态，不持久化也不会话隔离
    rightPanelSnipActive: false,
    sidebarWidth: storedPanelWidth(SIDEBAR_WIDTH_KEY, SIDEBAR_DEFAULT_WIDTH, SIDEBAR_MIN_WIDTH, SIDEBAR_MAX_WIDTH),
    rightPanelWidth: restoredRightPanelWidth(),
    embeddedBrowser: {
      url: '',
      title: '',
      can_go_back: false,
      can_go_forward: false,
      loading: false,
      ready: false,
      visible: false,
      phase: 'idle' as EmbeddedBrowserSurfacePhase,
      surface_generation: 0,
      supported: true,
      automationViewport: null as [number, number] | null,
    },
    browserSurfaceResyncNonce: 0,
    browserSessionSyncing: false,
    browserNavRequest: null as null | { url: string; path: string; session?: string },
    browserNavNonce: 0,
    // 右栏「文档」页签的外部打开请求（对话正文 / 文件树点击 PDF、Office 文档）：
    // DocumentPane 消费这条请求去读文件、用 pdf.js 渲染；nonce 自增保证同一文件
    // 重复点击也能被观察到。name 是给用户看的文件名（Office 文档转出的 PDF 用的是
    // 缓存文件名，标题要显示源文档名）；sourcePath 是用户在磁盘上点开的那个源文件
    // （Office 文档的 path 是缓存 PDF，所以「在文件夹中打开」必须用 sourcePath）。
    // null = 无待处理请求。
    docPreviewRequest: null as null | {
      path: string; name: string; sourcePath?: string; session?: string;
    },
    docPreviewNonce: 0,
    // Office 文档转 PDF 预览的进行中提示（首次转换要拉起 Office，10~25 秒）：
    // 非空期间「文档」页签在面板里显示「正在把 xxx 转换成 PDF」浮层，
    // 转换结束（成功/失败）一定清空。null = 没有进行中的转换。
    officePreviewPending: null as null | { path: string; name: string },
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
    closeConfirm: { show: false, trayAvailable: false } as { show: boolean; trayAvailable: boolean },
    remoteDirBrowser: {
      show: false,
      loading: false,
      error: '',
      current: '',
      isRoot: true,
      selected: '',
      dirs: [] as Array<{ name: string; path: string }>,
      plainPick: null as null | ((path: string) => void),
    },
    settingsSection: 'models' as 'models' | 'tokens' | 'tokens-legacy' | 'permissions' | 'hooks' | 'appearance' | 'pet' | 'bg-convert' | 'system' | 'persona' | 'growth' | 'decision' | 'feishu' | 'cloud-relay' | 'session-archive' | 'plugins' | 'about' | 'contact' | 'disclaimer',
    decisionEnabled: false,
    decisionUnread: 0,
    decisionEventTick: 0,
    decisionHasRecords: false,
    agentName: '星瑶',
    personaAgentName: '星瑶',
    userTitle: '',
    disclaimerRequiredVersion: '',
    disclaimerAcceptedVersion: '',
    themePreviewMode: false,

    planData: { title: '', items: [], progress: 0 } as PlanData,
    backgroundTasks: [] as BackgroundTask[],
    bgTaskDetailId: '',
    bgTaskOpenNonce: 0,
    subagentDetailId: '',
    subagentOpenNonce: 0,
    subagentTasks: [] as SubagentTaskSnapshot[],
    tokenUsage: emptyTokenUsage(),
    tokenUsageOverview: null as TokenUsageOverview | null,
    tokenUsageOverviewLoading: false,
    tokenStatistics: null as TokenStatisticsSnapshot | null,
    tokenStatisticsLoading: false,
    tokenStatisticsDays: 30 as 0 | 1 | 7 | 30,
    tokenStatisticsLifetime: null as TokenStatisticsSnapshot | null,
    tokenStatisticsLifetimeLoading: false,

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
      partUrls: [] as string[],
      sha256: '',
      downloadProgress: 0,
      downloadedBytes: 0,
      totalBytes: 0,
      installerPath: '',
      showUpdateModal: false,
      errorMessage: '',
    },

    confirmModal: { show: false, title: '', sub: '', message: '', confirmLabel: '确定删除', notice: false } as ConfirmState,

    sessionSearchModal: {
      show: false,
    },

    // 基元律动（TokenRhythm）账号：登录成功后由后端自动写入 API Key，前端只
    // 维护展示态。status='unknown' 表示还没探测过（桥未就绪），避免把"未探测
    // 到"误显示成"未登录"。
    trAuth: {
      status: 'unknown' as 'unknown' | 'anonymous' | 'loggedIn',
      user: null as Record<string, any> | null,
      configured: false,
      loading: false,
    },
    trLoginModal: { show: false },
    trAccountPanel: { show: false },
    trOverview: {
      loading: false,
      data: null as Record<string, any> | null,
      error: '',
    },

    trDialog: {
      show: false,
      tab: 'usage' as 'usage' | 'profile',
      loading: false,
      error: '',
      data: null as Record<string, any> | null,
      usagePage: 1,
      usageRange: '7d' as string,
      logsPage: 1,
    },

    permRequest: { show: false, sessionName: '', reqId: '', summary: '', source: '', allowAlways: false, options: [], permissionOptions: [], functionName: '' } as PermReqState,

    subpage: { show: false, type: 'skills' as SubpageType },

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

    currentStreamingText: '',
    currentThinkingText: '',

    windowMaximized: false,

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

    // 断点续跑（S4）：仅"快照属于当前会话 + 没有降级原因 + 当前没有回合在跑"时
    // 才显示入口，避免按钮指向别的会话或与运行中的回合并发
    resumeAvailable(state): boolean {
      const plan = state.resumePlan as ResumePlanSnapshot | null;
      if (!plan || !plan.session_name) return false;
      if (state.currentSessionName && plan.session_name !== state.currentSessionName) return false;
      if (plan.aborts && plan.aborts.length > 0) return false;
      return !state.running;
    },

    currentSessionQueued(state): boolean {
      return !!state.currentSessionName && state.queuedSessions.includes(state.currentSessionName);
    },

    currentSessionIsSecretary(state): boolean {
      const name = state.currentSessionName;
      if (!name || !Array.isArray(state.sessionGroups)) return false;
      for (const g of state.sessionGroups) {
        const hit = Array.isArray(g?.sessions)
          ? g.sessions.find(s => s.name === name)
          : undefined;
        if (hit) {
          if ((g as { is_secretary?: boolean }).is_secretary) return true;
          return typeof hit.secretary_name === 'string';
        }
      }
      return false;
    },

    currentMessageQueue(state): QueuedMessage[] {
      return (state.currentSessionName && state.messageQueues[state.currentSessionName]) || [];
    },

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

    computerRunIsBrowser(state): boolean {
      return state.computerRun.target === 'browser';
    },
    computerRunIsEmbeddedBrowser(state): boolean {
      if (state.computerRun.target !== 'browser') return false;
      return state.computerRun.browser_mode === 'embedded' || state.computerRun.browser === 'embedded';
    },
    computerRunIsExternalBrowser(state): boolean {
      if (state.computerRun.target !== 'browser') return false;
      if (state.computerRun.browser_mode === 'external') return true;
      const b = state.computerRun.browser;
      return b === 'edge' || b === 'chrome' || b === 'chromium';
    },
    embeddedBrowserOverlayBlocking(state): boolean {
      return Boolean(
        state.rightPanelSnipActive
        || state.filePreview?.show
        || state.confirmModal?.show
        || state.updateState?.showUpdateModal
        || state.sessionSearchModal?.show
        || state.trLoginModal?.show
        || state.trDialog?.show
        || state.remoteDirBrowser?.show
        || state.closeConfirm?.show
        || state.lightbox?.show
        || state.settingsOpen
        || (state.initialized && (state as any).disclaimerNeedsAccept)
      );
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

    computerRunTerminal(state): boolean {
      return COMPUTER_TERMINAL_STATUSES.includes(state.computerRun.status);
    },

    computerControlPausable(state): 'pause' | 'resume' | 'none' {
      if (state.computerRun.status === 'paused') return 'resume';
      if (state.computerRun.status === 'starting' || state.computerRun.status === 'running') return 'pause';
      return 'none';
    },

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
      runtime.modelId = this.activeModelId;
      runtime.modelName = this.activeModelName;
      runtime.permMode = this.permissionMode;
      runtime.usedSkills = this.usedSkills;
      runtime.historyHasMore = this.historyHasMore;
      runtime.historyOldestId = this.historyOldestId;
      runtime.navItems = this.navItems;
      if (!opts?.skipLayout) {
      // 注意：本行**每次都换一个新数组引用**。S3-T8 报告里「回写当前 store 的同一批
      // 引用、值相等时 Vue hasChanged 不触发更新」那条论断只适用于上面按引用直赋的
      // 字段（messages / tokenUsage / modelId / navItems 等），**不适用于 rightPanelTabs**。
      // 这里没有性能后果：该字段只在切会话时被读写，不在高频事件路径上。
      runtime.rightPanelTabs = [...this.rightPanelTabs];
      runtime.rightPanelActiveTab = this.rightPanelActiveTab;
      runtime.rightPanelExpanded = !this.rightPanelCollapsed;
      runtime.rightPanelUserCollapsed = Boolean(this.rightPanelUserCollapsed);
      runtime.automationViewport = this.embeddedBrowser.automationViewport
        ? ([...this.embeddedBrowser.automationViewport] as [number, number])
        : null;
      runtime.embeddedBrowserUrl = this.embeddedBrowser.url || '';
      runtime.embeddedBrowserTitle = this.embeddedBrowser.title || '';
      runtime.bgTaskDetailId = this.bgTaskDetailId;
      runtime.subagentDetailId = this.subagentDetailId;
      }
    },

    _captureCurrentRuntime(this: any) {
      this._captureRuntime(this.currentSessionName);
    },

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
      this.turnStartedAt = runtime.turnStartedAt || 0;
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
      if (!opts?.skipLayout) {
      const savedTabs = runtime.rightPanelTabs;
      let tabs: RightPanelTabId[] = savedTabs
        ? (savedTabs as string[]).filter(
            (t: string): t is RightPanelTabId => RIGHT_PANEL_TAB_IDS.has(t as RightPanelTabId),
          )
        : [];
      if (!this.computerRun.run_id) {
        tabs = tabs.filter(t => t !== 'computer');
      }
      if (!this.systemSettings.subagent_enabled) {
        tabs = tabs.filter(t => t !== 'subagent');
      }
      this.rightPanelTabs = tabs;
      const savedActive = runtime.rightPanelActiveTab;
      this.rightPanelActiveTab = savedActive && this.rightPanelTabs.includes(savedActive as RightPanelTabId)
        ? savedActive
        : this.rightPanelTabs[this.rightPanelTabs.length - 1] || '';
      this.rightPanelCollapsed = runtime.rightPanelExpanded === undefined
        ? true
        : !runtime.rightPanelExpanded;
      this.rightPanelUserCollapsed = Boolean(runtime.rightPanelUserCollapsed);
      this.embeddedBrowser.automationViewport = runtime.automationViewport
        ? ([...runtime.automationViewport] as [number, number])
        : null;
      this.embeddedBrowser.url = runtime.embeddedBrowserUrl || '';
      this.embeddedBrowser.title = runtime.embeddedBrowserTitle || '';
      this.embeddedBrowser.can_go_back = false;
      this.embeddedBrowser.can_go_forward = false;
      this.embeddedBrowser.loading = false;
      this.rightPanelPicker = this.rightPanelTabs.length === 0;
      this.bgTaskDetailId = runtime.bgTaskDetailId || '';
      // 子代理详情查看对象同口径恢复：切会话不残留上一个会话的查看 id，
      // 否则 SubagentPane 会用新会话名去查上一会话的任务 id，详情串场
      this.subagentDetailId = runtime.subagentDetailId || '';
      this.rightPanelSnipActive = false;
      }
    },

    // 撤掉某个会话的停止兜底定时器：该会话运行态已有权威来源，兜底使命结束
    _clearStopFallbackTimer(this: any, sessionName: string) {
      const timer = stopFallbackTimers.get(sessionName);
      if (timer !== undefined) {
        clearTimeout(timer);
        stopFallbackTimers.delete(sessionName);
      }
    },

    _setSessionRunning(this: any, sessionName: string, running: boolean) {
      // 运行态被本函数权威改写（新回合开始 / 发送失败回滚 / 兜底自身解锁）：
      // 旧兜底定时器不能再对这个会话动手，否则会清掉期间新开的回合
      this._clearStopFallbackTimer(sessionName);
      const runtime = this._getSessionRuntime(sessionName);
      runtime.running = running;
      if (sessionName === this.currentSessionName) this.running = running;
      if (running) {
        if (sessionName && !this.runningSessions.includes(sessionName)) {
          this.runningSessions.push(sessionName);
        }
      } else {
        this.runningSessions = this.runningSessions.filter((s: string) => s !== sessionName);
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

    _restoreActiveTurnSnapshot(snap: any, targetSessionName: string = '') {
      if (!snap || typeof snap !== 'object') return;
      const sess = targetSessionName || snap.session_name || this.currentSessionName;
      const isCurrent = this.currentSessionName === sess;
      const runtime = this._getSessionRuntime(sess);
      const msgs = isCurrent ? this.messages : runtime.messages;

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
      this.rightPanelUserCollapsed = this.rightPanelCollapsed;
      this.wideSplitAutoExpanded = false;
      this.wideSplitDismissed = this.rightPanelCollapsed;
      if (!this.rightPanelCollapsed) {
        this.rightPanelPicker = this.rightPanelTabs.length === 0;
      }
    },

    applyWideSplitExpand(this: any, tab: RightPanelTabId | null) {
      if (this.wideSplitDismissed) return;
      if (this.rightPanelTabs.length > 0) {
        if (!this.rightPanelCollapsed) return;
        this.rightPanelCollapsed = false;
        this.rightPanelPicker = false;
        this.wideSplitAutoExpanded = true;
        return;
      }
      if (tab) {
        this.openRightPanelTab(tab);
        this.wideSplitAutoExpanded = true;
        return;
      }
      if (this.rightPanelCollapsed) {
        this.rightPanelCollapsed = false;
        this.rightPanelPicker = true;
        this.wideSplitAutoExpanded = true;
      }
    },

    leaveWideSplit(this: any) {
      this.wideSplitDismissed = false;
      if (this.wideSplitAutoExpanded) {
        this.rightPanelCollapsed = true;
        this.wideSplitAutoExpanded = false;
      }
    },

    openRightPanelTab(this: any, tab: RightPanelTabId, opts?: { expand?: boolean }) {
      if (!this.rightPanelTabs.includes(tab)) this.rightPanelTabs.push(tab);
      this.rightPanelActiveTab = tab;
      this.rightPanelPicker = false;
      if (opts?.expand !== false) {
        this.rightPanelCollapsed = false;
        this.rightPanelUserCollapsed = false;
        this.wideSplitDismissed = false;
        this.wideSplitAutoExpanded = false;
      }
      if (tab === 'tasks' && !this.bgTaskDetailId && this.currentSessionBackgroundTasks.length) {
        this.bgTaskDetailId = this.currentSessionBackgroundTasks[0].task_id;
      }
      if (tab === 'subagent' && !this.subagentDetailId && this.subagentTasks.length) {
        this.subagentDetailId = this.subagentTasks[0].id;
      }
    },

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

    // 全局子代理开关关闭后，「星舰 · 子代理」页签一并收掉：开关关掉意味着不再
    // 派发子代理，留着页签只会在选择页/页签条上诱人点出一片空白。所有会话的
    // 页签集都清一遍（关闭期间切回旧会话也不会被旧快照复活）；当前激活页签被
    // 清时沿用 closeRightPanelTab 的邻居回退、全关回选择页规则。开关重新打开
    // 只恢复入口，不自动把页签加回来。
    pruneSubagentRightPanelTabs(this: any) {
      if (this.systemSettings.subagent_enabled) return;
      const runtimes: Record<string, any> = this.sessionRuntimeByName || {};
      for (const name of Object.keys(runtimes)) {
        const runtime = runtimes[name];
        if (!runtime || !Array.isArray(runtime.rightPanelTabs)) continue;
        if (!runtime.rightPanelTabs.includes('subagent')) continue;
        runtime.rightPanelTabs = runtime.rightPanelTabs.filter((t: string) => t !== 'subagent');
        if (runtime.rightPanelActiveTab === 'subagent') {
          runtime.rightPanelActiveTab = runtime.rightPanelTabs[runtime.rightPanelTabs.length - 1] || '';
        }
      }
      if (this.rightPanelTabs.includes('subagent')) this.closeRightPanelTab('subagent');
    },

    activateRightPanelTab(this: any, tab: RightPanelTabId) {
      if (!this.rightPanelTabs.includes(tab)) return;
      this.rightPanelActiveTab = tab;
      this.rightPanelPicker = false;
    },

    showRightPanelPicker(this: any) {
      this.rightPanelCollapsed = false;
      this.rightPanelPicker = true;
      this.wideSplitDismissed = false;
      this.wideSplitAutoExpanded = false;
    },

    openBrowserTabForSession(this: any, sessionName: string) {
      if (!sessionName || sessionName === this.currentSessionName) {
        this.openRightPanelTab('browser', { expand: !this.rightPanelUserCollapsed });
        return;
      }
      const runtime = this._getSessionRuntime(sessionName);
      this.openRightPanelTabForSession(sessionName, 'browser', {
        expand: !runtime.rightPanelUserCollapsed,
      });
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
      // session 只在非空时写入：空会话名不产生多余字段，兼容既有导航请求结构
      this.browserNavRequest = {
        url,
        path,
        session: this.currentSessionName || undefined,
      };
      this.browserNavNonce += 1;
      this.openRightPanelTab('browser');
    },

    // 文档在右栏「文档」页签里打开：PDF 由前端 pdf.js 自绘（Word/Excel/PPT 先由
    // 后端转成 PDF），完全不走内嵌浏览器——用户明确要求点文档不要弹出浏览器 UI。
    // 这里只写请求 + 摊开页签，真正的读文件与渲染由 DocumentPane 负责（宿主没有
    // read_file_preview 时由它回退提示「在系统中打开」）。
    // sourcePath：用户在磁盘上的源文件路径。Office 文档的 path 是后端转出的缓存
    // PDF，「在文件夹中打开」要定位的是源文档，所以单独记一份。
    openDocumentInRightPanel(this: any, path: string, displayName?: string, sourcePath?: string) {
      const target = String(path || '').trim();
      if (!target) return;
      const name = String(displayName || '').trim()
        || target.split(/[\\/]/).pop()
        || target;
      this.docPreviewRequest = {
        path: target,
        name,
        sourcePath: String(sourcePath || '').trim() || undefined,
        session: this.currentSessionName || undefined,
      };
      this.docPreviewNonce += 1;
      this.openRightPanelTab('doc', { expand: true });
    },

    // PDF 编辑结果落盘：ops 是 DocumentPane 维护的操作列表（PDF point 坐标契约，
    // 与 core/pdf_edit.py 的 ops 一致）。mode=copy 另存副本，overwrite 由后端先
    // 备份原文件再覆盖。成功后返回 {ok,path,pages,backup}，path 指向落盘的新文件，
    // 并由本动作把它重新开进「文档」页签（再编辑针对的是新文件）。
    //
    // Office 文档（docx/xlsx/pptx）在这里是「后端转出的缓存 PDF + 源文档路径」的
    // 组合：overwrite 会写坏用户的 .docx，一律拦下；copy 的副本必须落在源文档同
    // 目录，否则用户拿到的是缓存目录里的 PDF，回头根本找不到。
    async savePdfEdits(this: any, ops: any[], mode: 'copy' | 'overwrite' = 'copy') {
      const req = this.docPreviewRequest;
      const source = String(req?.sourcePath || req?.path || '').trim();
      if (!source) {
        this.notifyModal('PDF 编辑', '没有正在编辑的文档');
        return null;
      }
      const api = this._api();
      if (!api || typeof api.save_pdf_edits !== 'function') {
        this.notifyModal('PDF 编辑', '当前宿主不支持保存编辑，请重启应用后再试');
        return null;
      }
      const isConverted = !!req?.sourcePath && !/\.pdf$/i.test(String(req.sourcePath));
      if (isConverted && mode === 'overwrite') {
        this.notifyModal('PDF 编辑', '这是 Office 文档转换出来的预览，不能覆盖原文档；请用「保存副本」');
        return null;
      }
      let outPath = '';
      if (isConverted) {
        const dir = source.replace(/[\\/][^\\/]*$/, '');
        const base = source.split(/[\\/]/).pop() || 'document';
        const stem = base.replace(/\.[^.]+$/, '') || base;
        const fileName = `${stem}_已编辑_${pdfEditTimestamp()}.pdf`;
        outPath = dir ? `${dir}\\${fileName}` : fileName;
      }
      let res: any = null;
      try {
        res = await api.save_pdf_edits(source, ops, mode, outPath);
      } catch (err) {
        this.connected = false;
        this.notifyModal('PDF 编辑', '保存失败: ' + String(err || ''));
        return null;
      }
      if (!res || !res.ok) {
        this.notifyModal('PDF 编辑', '保存失败: ' + (res?.msg || '未知错误'));
        return null;
      }
      if (mode === 'overwrite' && res.backup) {
        this.notifyModal('PDF 编辑', `覆盖保存已完成（备份：${res.backup}）`);
      } else {
        this.notifyModal('PDF 编辑', `已保存：${res.path}`);
      }
      const newPath = String(res.path || '');
      if (newPath) {
        this.openDocumentInRightPanel(newPath, newPath.split(/[\\/]/).pop() || '文档.pdf');
      }
      return res;
    },

    // 「在文件夹中打开」：资源管理器定位到该文件（Windows 上是打开所在目录并
    // 选中文件）。旧后端没有 reveal_in_explorer 时退回只打开所在目录；两者都
    // 不可用才提示失败，不静默无反应。
    async revealInExplorer(this: any, path: string) {
      const target = String(path || '').trim();
      if (!target) return;
      const api = this._api();
      if (!api) {
        this.notifyModal('在文件夹中打开', '宿主桥不可用，无法定位文件');
        return;
      }
      const dir = target.replace(/[\\/][^\\/]*$/, '');
      try {
        if (typeof api.reveal_in_explorer === 'function') {
          const res = await api.reveal_in_explorer(target);
          if (res && res.ok) return;
          if (dir && typeof api.open_folder_in_explorer === 'function') {
            const fallback = await api.open_folder_in_explorer(dir);
            if (fallback && fallback.ok) return;
          }
          this.notifyModal('在文件夹中打开', (res && res.msg) || '定位失败');
          return;
        }
        if (dir && typeof api.open_folder_in_explorer === 'function') {
          const res = await api.open_folder_in_explorer(dir);
          if (res && res.ok) return;
          this.notifyModal('在文件夹中打开', (res && res.msg) || '定位失败');
          return;
        }
        this.notifyModal('在文件夹中打开', '当前宿主不支持在文件夹中打开');
      } catch (err) {
        this.connected = false;
        this.notifyModal('在文件夹中打开', '定位失败: ' + err);
      }
    },

    // 基元律动控制台页面（充值 / 用量明细 / 个人信息）在应用内打开：复用右栏
    // 内嵌浏览器页签，不再拉起系统浏览器。宿主确认不支持内嵌浏览器（非 Windows
    // 桌面端 GUI）时回退外部打开，保证任何宿主上点按钮都落得了地。
    // 账号浮层先收起：它相对左下角卡片定位并盖在内容之上，留着会压住刚摊开的右栏。
    openTrConsolePage(this: any, url: string) {
      const target = String(url || '').trim();
      if (!target) return;
      this.closeTrAccountPanel();
      if (this.embeddedBrowser.supported === false) {
        void this.openExternalUrl(target);
        return;
      }
      this.browserNavRequest = {
        url: target,
        path: '',
        session: this.currentSessionName || undefined,
      };
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

    async openOfficeInRightPanel(this: any, path: string) {
      const target = String(path || '').trim();
      if (!target) return;
      if (this.embeddedBrowser.supported === false || !isLoopbackPageHost()) {
        this.notifyModal('Office 预览', 'Office 文档右侧预览仅支持在本机桌面端使用');
        return;
      }
      const name = target.split(/[\\/]/).pop() || target;
      if (isDocxFilePath(target)) {
        this.openDocumentInRightPanel(target, name);
        return;
      }
      const api = this._api();
      // 旧后端 / 桥未注入：没有转换能力，回退源码预览弹窗（至少能看文本）
      if (!api || typeof api.preview_office_doc !== 'function') {
        await this.openFilePreview(target);
        return;
      }
      this.openRightPanelTab('doc', { expand: true });
      this.officePreviewPending = { path: target, name };
      let failMsg = '';
      try {
        const res = await api.preview_office_doc(target);
        if (res && res.ok && res.path) {
          this.openDocumentInRightPanel(String(res.path), name, target);
          return;
        }
        failMsg = (res && res.msg) || '';
      } catch (err) {
        failMsg = String(err || '');
      } finally {
        this.officePreviewPending = null;
      }
      // 兜底：系统默认程序打开源文件（openInSystem 内部失败会自己 notifyModal）
      if (typeof api.open_path === 'function') {
        await this.openInSystem(target);
        return;
      }
      this.notifyModal('Office 预览', failMsg || '转换失败');
    },

    // 本地文件在右栏显示的统一入口（对话正文 / 文件树的点击分流）：
    // pdf / office 文档进「文档」页签（pdf.js 自绘，office 先转 PDF）；md 先渲染成
    // 预览页、html 当网页渲染，两者仍走内嵌浏览器页签；其它类型回退源码预览弹窗。
    async openInRightPanel(this: any, path: string) {
      const target = String(path || '').trim();
      if (!target) return;
      if (isOfficeFilePath(target)) {
        await this.openOfficeInRightPanel(target);
        return;
      }
      if (isMarkdownFilePath(target)) {
        await this.openMarkdownInRightPanel(target);
        return;
      }
      if (isPdfFilePath(target)) {
        this.openDocumentInRightPanel(target);
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

    openBackgroundTaskDetail(this: any, taskId: string) {
      if (!taskId) return;
      this.bgTaskDetailId = taskId;
      this.bgTaskOpenNonce += 1;
      this.openRightPanelTab('tasks');
    },

    openSubagentDetail(taskId: string) {
      if (!taskId) return;
      this.subagentDetailId = taskId;
      this.subagentOpenNonce += 1;
      this.openRightPanelTab('subagent');
    },

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
      } catch { }
    },

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
        try { await api.pty_terminal_write(text); } catch { }
      }
    },

    async ptyTerminalResize(cols: number, rows: number) {
      const api = this._ptyApi();
      if (api?.pty_terminal_resize) {
        try { await api.pty_terminal_resize(cols, rows); } catch { }
      }
    },

    async ptyTerminalKill() {
      const api = this._ptyApi();
      if (api?.pty_terminal_kill) {
        try { await api.pty_terminal_kill(); } catch { }
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
      document.documentElement.classList.toggle('light', light);
      document.body.classList.toggle('light', light);
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

    notifyModal(title: string, msg: string, options?: { tone?: ConfirmTone }) {
      if (this.confirmModal.show && this.confirmModal.resolve) {
        const prevResolve = this.confirmModal.resolve;
        this.confirmModal.resolve = undefined;
        prevResolve(false);
      }
      this.confirmModal = {
        show: true,
        title,
        sub: '',
        message: msg,
        confirmLabel: '确定',
        notice: true,
        tone: options?.tone,
      };
    },

    askConfirm(
      title: string,
      sub: string,
      message: string,
      confirmLabel: string = '确定删除',
      options?: { tone?: ConfirmTone },
    ): Promise<boolean> {
      if (this.confirmModal.show && this.confirmModal.resolve) {
        const prevResolve = this.confirmModal.resolve;
        this.confirmModal.resolve = undefined;
        prevResolve(false);
      }
      return new Promise((resolve) => {
        this.confirmModal = {
          show: true,
          title,
          sub,
          message,
          confirmLabel,
          tone: options?.tone,
          resolve,
        };
      });
    },

    resolveConfirm(ok: boolean) {
      const resolve = this.confirmModal.resolve;
      this.confirmModal.resolve = undefined;
      this.confirmModal.show = false;
      if (resolve) {
        resolve(ok);
      }
    },

    openSessionSearchModal() {
      this.sessionSearchModal.show = true;
    },

    closeSessionSearchModal() {
      this.sessionSearchModal.show = false;
    },

    // ── 基元律动账号（TokenRhythm）────────────────────────────────────────
    // 后端 tr_auth_* 系列不在 PyWebViewAPI 类型声明里（这批接口由后加的解
    // 决方案提供），这里统一做一次存在性检查 + any 收窄，避免登录入口在旧后
    // 端上直接抛错。
    async _trAuthCall(method: string, label: string, args: any[] = []): Promise<any> {
      const api = this._api() as any;
      if (!api) return { ok: false, msg: '后端连接不可用，请启动应用后再试' };
      const fn = api[method];
      if (typeof fn !== 'function') {
        return { ok: false, msg: '当前后端版本不支持基元律动登录，请更新到最新版本' };
      }
      try {
        return await fn(...args);
      } catch (err) {
        console.error(`${method} 失败:`, err);
        return { ok: false, msg: `${label}失败：${String(err)}` };
      }
    },

    openTrLoginModal() {
      this.trLoginModal.show = true;
    },

    closeTrLoginModal() {
      this.trLoginModal.show = false;
    },

    // 账号浮层：打开即拉一次总览（余额 / 今日用量会变，不能只依赖上一次快照）。
    // 拉取失败不清空已有登录态，只在浮层里展示后端原因 + 重试入口。
    openTrAccountPanel() {
      this.trAccountPanel.show = true;
      void this.refreshTrOverview();
    },

    closeTrAccountPanel() {
      this.trAccountPanel.show = false;
    },

    toggleTrAccountPanel() {
      if (this.trAccountPanel.show) this.closeTrAccountPanel();
      else this.openTrAccountPanel();
    },

    // 账号总览：桥接 tr_account_overview()。与 refreshTrAuth 一样按后台探测处理，
    // 桥未就绪时不弹"连接失败"框，而是把原因写进 error 交给浮层展示。
    async refreshTrOverview() {
      const api = (window.pywebview && window.pywebview.api) as any;
      if (!api || typeof api.tr_account_overview !== 'function') {
        this.trOverview.data = null;
        this.trOverview.error = api
          ? '当前后端版本不支持账号总览，请更新到最新版本'
          : '后端连接不可用，请启动应用后再试';
        return null;
      }
      this.trOverview.loading = true;
      this.trOverview.error = '';
      try {
        const res = await api.tr_account_overview();
        if (res && res.ok) {
          this.trOverview.data = res as Record<string, any>;
          this.trOverview.error = '';
        } else {
          // 未登录也走这条分支：后端 msg 是中文原因，原样展示
          this.trOverview.data = null;
          this.trOverview.error = String((res && res.msg) || '账号信息获取失败，请稍后重试');
        }
        return res;
      } catch (err) {
        console.error('tr_account_overview 失败:', err);
        this.trOverview.data = null;
        this.trOverview.error = `账号信息获取失败：${String(err)}`;
        return null;
      } finally {
        this.trOverview.loading = false;
      }
    },

    async refreshTrAuth() {
      const api = (window.pywebview && window.pywebview.api) as any;
      if (!api || typeof api.tr_auth_status !== 'function') return null;
      this.trAuth.loading = true;
      try {
        const res = await api.tr_auth_status();
        if (res && res.ok) {
          this.trAuth.status = res.logged_in ? 'loggedIn' : 'anonymous';
          this.trAuth.user = res.logged_in && res.user && typeof res.user === 'object' ? res.user : null;
          this.trAuth.configured = !!res.configured;
        }
        return res;
      } catch (err) {
        console.error('tr_auth_status 失败:', err);
        return null;
      } finally {
        this.trAuth.loading = false;
      }
    },

    _applyTrLoginResult(res: any) {
      if (!res || !res.ok) return;
      this.trAuth.status = 'loggedIn';
      this.trAuth.user = res.user && typeof res.user === 'object' ? res.user : null;
      this.trAuth.configured = res.bind_ok !== false;
      if (res.settings && Array.isArray(res.settings.models)) {
        this.applyModelSettings(res.settings);
      }
    },

    // 第二个参数是阿里云人机验证 success 回调给出的 captchaVerifyParam，必须原样透传；
    // 人机验证不可用时省略该参数（后端签名已归一为空串，不会因此报错）
    async trSendSms(phone: string, captchaVerifyParam?: string) {
      const args: any[] = [phone];
      if (captchaVerifyParam) args.push(captchaVerifyParam);
      return this._trAuthCall('tr_auth_send_sms', '发送验证码', args);
    },

    async trSmsLogin(phone: string, code: string, inviteCode?: string) {
      const res = await this._trAuthCall('tr_auth_sms_login', '登录', [phone, code, inviteCode ?? '']);
      this._applyTrLoginResult(res);
      return res;
    },

    async trPasswordLogin(account: string, password: string) {
      const res = await this._trAuthCall('tr_auth_password_login', '登录', [account, password]);
      this._applyTrLoginResult(res);
      return res;
    },

    async trLogout() {
      const res = await this._trAuthCall('tr_auth_logout', '退出登录');
      if (res && res.ok) {
        await this.refreshTrAuth();
        // 账号已退出：浮层里的余额/用量属于上一个账号，必须清掉并收起，
        // 否则下次打开会先闪一帧旧数据
        this.trOverview.data = null;
        this.trOverview.error = '';
        this.trAccountPanel.show = false;
      }
      return res;
    },

    openTrDialog(this: any, tab: 'usage' | 'profile') {
      this.trDialog.tab = tab;
      this.trDialog.show = true;
      this.trDialog.loading = false;
      this.trDialog.error = '';
      this.trDialog.data = null;
      this.trDialog.usagePage = 1;
      this.trDialog.logsPage = 1;
      this.trDialog.usageRange = '7d';
      // 浮层若开着先收起，避免两层 UI 叠在一起
      this.closeTrAccountPanel();
      void this.fetchTrDialog();
    },

    closeTrDialog() {
      this.trDialog.show = false;
    },

    async fetchTrDialog(this: any) {
      const api = (window.pywebview && window.pywebview.api) as any;
      if (!api) {
        this.trDialog.error = '后端连接不可用，请启动应用后再试';
        return;
      }
      const methodByTab: Record<string, string> = {
        usage: 'tr_usage_detail',
        profile: 'tr_profile',
      };
      const method = methodByTab[this.trDialog.tab];
      if (typeof api[method] !== 'function') {
        this.trDialog.error = '当前后端版本不支持该账号中心功能，请更新到最新版本';
        return;
      }
      this.trDialog.loading = true;
      this.trDialog.error = '';
      try {
        let res: any;
        if (this.trDialog.tab === 'usage') {
          res = await api[method](this.trDialog.usageRange, this.trDialog.usagePage, 20);
        } else {
          res = await api[method]();
        }
        if (res && res.ok) {
          this.trDialog.data = (res.data && typeof res.data === 'object') ? res.data : null;
          this.trDialog.error = '';
        } else {
          this.trDialog.data = null;
          this.trDialog.error = String((res && res.msg) || (res && res.reason) || '数据获取失败，请稍后重试');
        }
      } catch (err) {
        console.error(`${method} 失败:`, err);
        this.trDialog.data = null;
        this.trDialog.error = `数据获取失败：${String(err)}`;
      } finally {
        this.trDialog.loading = false;
      }
    },

    async trDialogChangeUsagePage(this: any, page: number) {
      if (this.trDialog.tab !== 'usage') return;
      this.trDialog.usagePage = Math.max(1, page);
      await this.fetchTrDialog();
    },

    async trDialogChangeUsageRange(this: any, range: string) {
      if (this.trDialog.tab !== 'usage') return;
      this.trDialog.usageRange = range;
      this.trDialog.usagePage = 1;
      await this.fetchTrDialog();
    },

    async trDialogChangeLogsPage(this: any, page: number) {
      if (this.trDialog.tab !== 'usage') return;
      this.trDialog.logsPage = Math.max(1, page);
      await this.fetchTrDialog();
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
      void this.refreshAutomations();
      void this.refreshTrAuth();
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
        this.decisionEnabled = !!(init as any).decision_enabled;
        this.sandboxEnabled = !!(init as any).allow_outside_workspace;
        if (init.system_settings) {
          this.systemSettings = init.system_settings;
        }
        const personaInit = (init as any).persona_settings;
        if (personaInit && typeof personaInit === 'object') {
          this.personaAgentName = String(personaInit.agent_name || '星瑶');
          this.agentName = this.personaAgentName;
          this.userTitle = String(personaInit.user_title || '');
          this.syncDocumentTitle();
        }
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
        this.skillsLoading = this.skills.length === 0;
        this.mcps = init.mcps || [];
        this.plugins = (init as any).plugins || [];
        void this.refreshHooks();
        void this.loadSlashCommands();
        this.workdir = init.workdir || '';
        this.currentSessionName = (init as any).current_session || '';
        // 工作区标签以“当前会话归属”为准：GLOBAL 会话显示“对话”，
        // 不能直接用启动目录，否则会显示在工作区里、消息却发进自由对话
        const isGlobal = !!(init as any).is_global;
        this.currentWorkdirLabel = (!isGlobal && this.workdir)
          ? (this.workdir.split('\\').pop() || this.workdir)
          : '对话';
        this.messages = pairHistoryToolMessages((init as any).messages || []);
      this.historyHasMore = !!(init as any).history_has_more;
      this.historyOldestId = (init as any).history_oldest_id ?? null;        void this.refreshNavIndex();
        void this.refreshTurnUndoStates();
        // 决策入口按当前会话探测：必须在 currentSessionName 就绪之后调用
        // （App.vue 的启动探测发生在 initFromBackend 未完成的时刻，那时还没有
        // 会话名，会按口径直接跳过，故这里补一次）
        void this.refreshDecisionAvailability();
        const activeSessions: string[] = Array.isArray((init as any).active_sessions)
          ? (init as any).active_sessions.map(String).filter(Boolean)
          : [];
        this.runningSessions = activeSessions;
        this.queuedSessions = [];
        this.messageQueues = ((init as any).message_queues || {}) as Record<string, QueuedMessage[]>;
        for (const name of activeSessions) {
          this._getSessionRuntime(name).running = true;
        }
        this.running = !!this.currentSessionName && activeSessions.includes(this.currentSessionName);
        this._captureCurrentRuntime();
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
        this.checkUpdate(false);
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
      const sessionName = this.currentSessionName;
      try {
        const res = await api.set_permission_mode(mode, sessionName || undefined);
        if (res && res.ok) {
          this.permissionMode = res.permission_mode || mode;
          const runtime = this._getSessionRuntime(sessionName);
          runtime.permMode = this.permissionMode;
          if (res.decision_warning) {
            this.notifyModal('自动决策（JEV）', String(res.decision_warning));
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

    async setSandbox(enabled: boolean) {
      const prev = this.sandboxEnabled;
      const api = this._api();
      if (!api) return;
      try {
        const res = await api.set_sandbox_enabled(enabled);
        if (res && res.ok) {
          this.sandboxEnabled = !!res.allow_outside_workspace;
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

    async refreshSystemSettings(options: { silent?: boolean } = {}) {
      const api = this._api();
      if (!api) return null;
      try {
        const settings = await api.get_system_settings();
        this.systemSettings = settings;
        this.pruneSubagentRightPanelTabs();
        return settings;
      } catch (err) {
        if (!options.silent) {
          this.connected = false;
          this.notifyModal('系统设置', '加载系统设置失败: ' + err);
        }
        return null;
      }
    },

    applyPersonaSettings(snapshot: { agent_name?: string; user_title?: string }) {
      if (!snapshot || typeof snapshot !== 'object') return;
      if (snapshot.agent_name) {
        this.personaAgentName = String(snapshot.agent_name);
        this.agentName = this.personaAgentName;
      }
      this.userTitle = String(snapshot.user_title || '');
      this.syncDocumentTitle();
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
      if (!this.currentSessionName) {
        this._applyAgentName(this.personaAgentName);
        return;
      }
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

    async setSubagentEnabled(enabled: boolean): Promise<boolean> {
      const api = this._api();
      if (!api) return false;
      try {
        const result = await api.set_subagent_enabled(enabled);
        if (result && result.settings) this.systemSettings = result.settings;
        if (!result || !result.ok) {
          this.notifyModal('子代理', '子代理开关设置失败: ' + (result?.msg || '未知错误'));
          return false;
        }
        this.pruneSubagentRightPanelTabs();
        return true;
      } catch (err) {
        this.connected = false;
        this.notifyModal('子代理', '子代理开关设置失败: ' + err);
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

    async refreshPlaywrightComponentStatus(): Promise<boolean> {
      const api = this._api();
      if (!api) return false;
      try {
        const result = await api.get_playwright_component_status();
        if (result && result.component) this.playwrightComponent = result.component;
        if (!result || !result.ok) {
          this.notifyModal('浏览器操控组件', '读取组件状态失败: ' + (result?.msg || '未知错误'));
          return false;
        }
        return true;
      } catch (err) {
        this.connected = false;
        this.notifyModal('浏览器操控组件', '读取组件状态失败: ' + err);
        return false;
      }
    },

    async downloadPlaywrightComponent(): Promise<boolean> {
      const api = this._api();
      if (!api) return false;
      try {
        const result = await api.download_playwright_component();
        if (result && result.component) this.playwrightComponent = result.component;
        if (!result || !result.ok) {
          this.notifyModal('浏览器操控组件', '下载组件失败: ' + (result?.msg || '未知错误'));
          return false;
        }
        return true;
      } catch (err) {
        this.connected = false;
        this.notifyModal('浏览器操控组件', '下载组件失败: ' + err);
        return false;
      }
    },

    async cancelPlaywrightComponentDownload(): Promise<boolean> {
      const api = this._api();
      if (!api) return false;
      try {
        const result = await api.cancel_playwright_component_download();
        if (result && result.component) this.playwrightComponent = result.component;
        if (!result || !result.ok) {
          this.notifyModal('浏览器操控组件', '取消下载失败: ' + (result?.msg || '未知错误'));
          return false;
        }
        return true;
      } catch (err) {
        this.connected = false;
        this.notifyModal('浏览器操控组件', '取消下载失败: ' + err);
        return false;
      }
    },

    async refreshFfmpegComponentStatus(): Promise<boolean> {
      const api = this._api();
      if (!api) return false;
      try {
        const result = await api.get_ffmpeg_component_status();
        if (result && result.component) this.ffmpegComponent = result.component;
        if (!result || !result.ok) {
          this.notifyModal('视频转换组件', '读取组件状态失败: ' + (result?.msg || '未知错误'));
          return false;
        }
        return true;
      } catch (err) {
        this.connected = false;
        this.notifyModal('视频转换组件', '读取组件状态失败: ' + err);
        return false;
      }
    },

    async downloadFfmpegComponent(): Promise<boolean> {
      const api = this._api();
      if (!api) return false;
      try {
        const result = await api.download_ffmpeg_component();
        if (result && result.component) this.ffmpegComponent = result.component;
        if (!result || !result.ok) {
          this.notifyModal('视频转换组件', '下载组件失败: ' + (result?.msg || '未知错误'));
          return false;
        }
        return true;
      } catch (err) {
        this.connected = false;
        this.notifyModal('视频转换组件', '下载组件失败: ' + err);
        return false;
      }
    },

    async cancelFfmpegComponentDownload(): Promise<boolean> {
      const api = this._api();
      if (!api) return false;
      try {
        const result = await api.cancel_ffmpeg_component_download();
        if (result && result.component) this.ffmpegComponent = result.component;
        if (!result || !result.ok) {
          this.notifyModal('视频转换组件', '取消下载失败: ' + (result?.msg || '未知错误'));
          return false;
        }
        return true;
      } catch (err) {
        this.connected = false;
        this.notifyModal('视频转换组件', '取消下载失败: ' + err);
        return false;
      }
    },

    async refreshSearchComponentStatus(): Promise<boolean> {
      const api = this._api();
      if (!api) return false;
      try {
        const result = await api.get_search_component_status();
        if (result && result.component) this.searchComponent = result.component;
        if (!result || !result.ok) {
          this.notifyModal('网页搜索组件', '读取组件状态失败: ' + (result?.msg || '未知错误'));
          return false;
        }
        return true;
      } catch (err) {
        this.connected = false;
        this.notifyModal('网页搜索组件', '读取组件状态失败: ' + err);
        return false;
      }
    },

    async downloadSearchComponent(): Promise<boolean> {
      const api = this._api();
      if (!api) return false;
      try {
        const result = await api.download_search_component();
        if (result && result.component) this.searchComponent = result.component;
        if (!result || !result.ok) {
          this.notifyModal('网页搜索组件', '下载组件失败: ' + (result?.msg || '未知错误'));
          return false;
        }
        return true;
      } catch (err) {
        this.connected = false;
        this.notifyModal('网页搜索组件', '下载组件失败: ' + err);
        return false;
      }
    },

    async cancelSearchComponentDownload(): Promise<boolean> {
      const api = this._api();
      if (!api) return false;
      try {
        const result = await api.cancel_search_component_download();
        if (result && result.component) this.searchComponent = result.component;
        if (!result || !result.ok) {
          this.notifyModal('网页搜索组件', '取消下载失败: ' + (result?.msg || '未知错误'));
          return false;
        }
        return true;
      } catch (err) {
        this.connected = false;
        this.notifyModal('网页搜索组件', '取消下载失败: ' + err);
        return false;
      }
    },

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
      const gen = Number(raw.run_generation) || 0;
      const seq = Number(raw.event_seq) || 0;
      if (!runId) return;
      if (runId === this._computerClosedRunId) return;
      const currentRunId = this.computerRun.run_id;
      const currentGen = this._computerRunGeneration;
      if (gen > 0 && currentGen > 0 && gen < currentGen) return;

      const isNewRun = runId !== currentRunId && (
        (gen > 0 && gen > currentGen)
        || (gen === 0 && (
          currentRunId === ''
          || (type === 'computer_state' && String(raw.status || '') === 'starting')
        ))
      );
      if (!isNewRun) {
        if (runId !== currentRunId) return;
        const effectiveGen = gen || currentGen;
        if (effectiveGen < currentGen) return;
        if (effectiveGen === currentGen && seq <= this._computerEventSeq) return;

        // 带 run_generation 的版本化事件出现序号断档且桥可用：先暂存并发起单飞快照校准，
        // 快照到位后丢弃已覆盖项再按序回放更新的事件；无 run_generation 的旧格式事件仅后台校准。
        const hasStatusBridge = typeof this._ptyApi()?.get_computer_control_status === 'function';
        if (
          hasStatusBridge
          && this._computerEventSeq > 0
          && seq > this._computerEventSeq + 1
        ) {
          if (gen > 0) {
            this._queuePendingComputerEvent(event);
            void this.refreshComputerRun();
            return;
          }
          void this.refreshComputerRun();
        }
      }
      this._applyComputerEventDirect(event);
    },

    _queuePendingComputerEvent(event: ComputerControlEvent) {
      const raw = event as unknown as Record<string, unknown>;
      const runId = String(raw.run_id || '');
      const gen = Number(raw.run_generation) || 0;
      const seq = Number(raw.event_seq) || 0;
      const exists = this._computerPendingEvents.some((item) => {
        const other = item as unknown as Record<string, unknown>;
        return (
          String(other.run_id || '') === runId
          && (Number(other.run_generation) || 0) === gen
          && (Number(other.event_seq) || 0) === seq
        );
      });
      if (exists) return;
      this._computerPendingEvents.push(event);
      this._computerPendingEvents.sort((a, b) => {
        const ra = a as unknown as Record<string, unknown>;
        const rb = b as unknown as Record<string, unknown>;
        const ga = Number(ra.run_generation) || 0;
        const gb = Number(rb.run_generation) || 0;
        if (ga !== gb) return ga - gb;
        return (Number(ra.event_seq) || 0) - (Number(rb.event_seq) || 0);
      });
      if (this._computerPendingEvents.length > COMPUTER_PENDING_EVENTS_MAX) {
        this._computerPendingEvents.splice(
          0,
          this._computerPendingEvents.length - COMPUTER_PENDING_EVENTS_MAX,
        );
      }
    },

    _applyComputerEventDirect(event: ComputerControlEvent) {
      const raw = event as unknown as Record<string, unknown>;
      const type = String(raw.type || '');
      if (!COMPUTER_EVENT_TYPES.has(type)) return;
      const runId = String(raw.run_id || '');
      const gen = Number(raw.run_generation) || 0;
      const seq = Number(raw.event_seq) || 0;
      if (!runId || runId === this._computerClosedRunId) return;

      const currentRunId = this.computerRun.run_id;
      const currentGen = this._computerRunGeneration;
      if (gen > 0 && currentGen > 0 && gen < currentGen) return;

      const isNewRun = runId !== currentRunId && (
        (gen > 0 && gen > currentGen)
        || (gen === 0 && (
          currentRunId === ''
          || (type === 'computer_state' && String(raw.status || '') === 'starting')
        ))
      );
      if (!isNewRun) {
        if (runId !== currentRunId) return;
        const effectiveGen = gen || currentGen;
        if (effectiveGen < currentGen) return;
        if (effectiveGen === currentGen && seq <= this._computerEventSeq) return;
      }

      this._computerEventSeq = seq;
      if (gen > 0) this._computerRunGeneration = Math.max(this._computerRunGeneration, gen);
      const ownerSession = typeof raw.owner_session_id === 'string' ? raw.owner_session_id : '';
      const eventBrowser = typeof raw.browser === 'string' ? raw.browser : '';
      const eventBrowserMode = typeof raw.browser_mode === 'string' ? raw.browser_mode : '';
      if (isNewRun) {
        this.computerRun = {
          ...emptyComputerRun(),
          run_id: runId,
          run_generation: gen || this._computerRunGeneration,
          event_seq: seq,
          status: type === 'computer_state' ? String(raw.status || '') || 'starting' : 'starting',
          owner_session_id: ownerSession,
          browser: eventBrowser,
          browser_mode: eventBrowserMode,
        };
        this.computerTrace = [];
        this.computerFrameShots = {};
        this._computerActionStartMs = {};
        this._computerActiveActionOrdinal = 0;
        this._computerPendingEvents = this._computerPendingEvents.filter((item) => {
          const r = item as unknown as Record<string, unknown>;
          return String(r.run_id || '') === runId && (Number(r.run_generation) || 0) >= this._computerRunGeneration;
        });
      } else {
        const nextBase: ComputerRunSnapshot = {
          ...this.computerRun,
          event_seq: Math.max(Number(this.computerRun.event_seq || 0), seq),
          ...(gen > 0 ? { run_generation: Math.max(Number(this.computerRun.run_generation || 0), gen) } : {}),
          ...(ownerSession && !this.computerRun.owner_session_id ? { owner_session_id: ownerSession } : {}),
          ...(eventBrowser ? { browser: eventBrowser } : {}),
          ...(eventBrowserMode ? { browser_mode: eventBrowserMode } : {}),
        };
        this.computerRun = nextBase;
      }

      if (type === 'computer_state') {
        const status = String(raw.status || '');
        const target = String(raw.target || '');
        const currentlyTerminal = COMPUTER_TERMINAL_STATUSES.includes(this.computerRun.status);
        const incomingTerminal = COMPUTER_TERMINAL_STATUSES.includes(status);
        if (!isNewRun && currentlyTerminal && status && !incomingTerminal) {
          return;
        }
        const isTerminal = incomingTerminal;
        if (isTerminal) this._computerActiveActionOrdinal = 0;
        this.computerRun = {
          ...this.computerRun,
          status: status || this.computerRun.status,
          target: target || this.computerRun.target,
          ...(eventBrowser ? { browser: eventBrowser } : {}),
          ...(eventBrowserMode ? { browser_mode: eventBrowserMode } : {}),
          ...(isTerminal ? { current_action: '' } : {}),
        };
        return;
      }
      if (type === 'computer_observation') {
        const previewUrl = String(raw.preview_url || '');
        const surface = String(raw.origin_or_window || '');
        const shotSha = String(raw.screen_sha256 || '');
        const obsTarget = String(raw.target || '');
        const frameId = String(raw.frame_id || '');
        const imgW = Number(raw.width) || 0;
        const imgH = Number(raw.height) || 0;
        const obsErr = String(raw.observation_error || raw.screenshot_error || '');
        const imgOmitted = String(raw.image_omitted || '');
        const observedAt = Number(raw.observed_at_ms) || Date.now();
        if (previewUrl && shotSha) {
          this._rememberComputerShot(shotSha, previewUrl, {
            frame_id: frameId || undefined,
            image_width: imgW > 0 ? imgW : undefined,
            image_height: imgH > 0 ? imgH : undefined,
          });
        }
        this.computerRun = {
          ...this.computerRun,
          ...(obsTarget ? { target: obsTarget } : {}),
          ...(eventBrowser ? { browser: eventBrowser } : {}),
          ...(eventBrowserMode ? { browser_mode: eventBrowserMode } : {}),
          ...(previewUrl ? { preview_url: previewUrl } : {}),
          ...(surface ? { current_surface: surface } : {}),
          ...(frameId ? { current_frame_id: frameId } : {}),
          observation_updated_at_ms: observedAt,
          observation_error: obsErr,
          image_omitted: imgOmitted,
        };
        return;
      }
      const ordinal = Number(raw.action_ordinal) || 0;
      if (ordinal <= 0) return;
      if (type === 'computer_action_started') {
        this._computerActionStartMs[ordinal] = Date.now();
        this._computerActiveActionOrdinal = ordinal;
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
      if (this._computerActiveActionOrdinal === 0 || ordinal === this._computerActiveActionOrdinal) {
        this._computerActiveActionOrdinal = 0;
        if (this.computerRun.current_action) {
          this.computerRun = { ...this.computerRun, current_action: '' };
        }
      }
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
    _rememberComputerShot(
      sha: string,
      url: string,
      meta?: { frame_id?: string; image_width?: number; image_height?: number },
    ) {
      const shots = this.computerShots;
      const frames = this.computerFrameShots;
      if (meta && (meta.frame_id || meta.image_width || meta.image_height)) {
        frames[sha] = {
          url,
          frame_id: meta.frame_id,
          image_width: meta.image_width,
          image_height: meta.image_height,
        };
      }
      if (shots[sha] === url) return;
      shots[sha] = url;
      const keys = Object.keys(shots);
      if (keys.length > 80) {
        for (const key of keys.slice(0, keys.length - 80)) {
          delete shots[key];
          delete frames[key];
        }
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

    clearComputerRun() {
      const runId = this.computerRun.run_id;
      if (!runId) return;
      this._computerClosedRunId = runId;
      this.computerRun = emptyComputerRun();
      this.computerTrace = [];
      this.computerShots = {};
      this.computerFrameShots = {};
      this._computerActionStartMs = {};
      this._computerActiveActionOrdinal = 0;
      this._computerPendingEvents = [];
      this._computerEventSeq = 0;
    },

    // Bridge 快照校准（设计 16.4）：按 (run_generation, event_seq) 合并权威快照与推送；
    // 忽略更旧代次/序号，丢弃已被快照覆盖的暂存事件，并按序应用比快照更新的暂存事件。
    _applyComputerSnapshot(raw: Record<string, unknown>) {
      const runId = String(raw.run_id || '');
      if (!runId) return;
      // 已关闭 run 的快照不得复活运行态（新 run_id 不受影响）
      if (runId === this._computerClosedRunId) return;

      const snapGen = Number(raw.run_generation) || 0;
      const snapSeq = Number(raw.event_seq) || 0;
      if (snapGen > 0 && this._computerRunGeneration > 0 && snapGen < this._computerRunGeneration) {
        return;
      }
      const sameRun = runId === this.computerRun.run_id;
      if (sameRun && (snapGen === 0 || snapGen === this._computerRunGeneration)) {
        if (snapSeq > 0 && snapSeq < this._computerEventSeq) {
          return;
        }
      }

      if (!sameRun) {
        this.computerRun = { ...emptyComputerRun(), run_id: runId };
        this.computerTrace = [];
        this._computerActionStartMs = {};
        this._computerActiveActionOrdinal = 0;
        this._computerEventSeq = 0;
      }
      if (snapGen > 0) this._computerRunGeneration = Math.max(this._computerRunGeneration, snapGen);
      if (snapSeq > 0) this._computerEventSeq = Math.max(this._computerEventSeq, snapSeq);

      const patch: Partial<ComputerRunSnapshot> = {};
      for (const field of COMPUTER_SNAPSHOT_FIELDS) {
        if (raw[field] !== undefined) {
          (patch as Record<string, unknown>)[field] = raw[field];
        }
      }
      this.computerRun = {
        ...this.computerRun,
        ...patch,
        run_generation: Math.max(Number(this.computerRun.run_generation || 0), this._computerRunGeneration),
        event_seq: Math.max(Number(this.computerRun.event_seq || 0), this._computerEventSeq),
      };
      if (COMPUTER_TERMINAL_STATUSES.includes(this.computerRun.status) || this.computerRun.current_action === '') {
        this._computerActiveActionOrdinal = 0;
        if (COMPUTER_TERMINAL_STATUSES.includes(this.computerRun.status)) {
          this.computerRun = { ...this.computerRun, current_action: '' };
        }
      } else {
        this._computerActiveActionOrdinal = Number(this.computerRun.action_count) || 0;
      }

      if (this._computerPendingEvents.length > 0) {
        const curRunId = this.computerRun.run_id;
        const curGen = this._computerRunGeneration;
        const remaining: ComputerControlEvent[] = [];
        for (const buffered of this._computerPendingEvents) {
          const item = buffered as unknown as Record<string, unknown>;
          const evRunId = String(item.run_id || '');
          const evGen = Number(item.run_generation) || 0;
          const evSeq = Number(item.event_seq) || 0;
          if (evGen > 0 && curGen > 0 && evGen < curGen) continue;
          if (evRunId === curRunId && (evGen === 0 || evGen === curGen) && evSeq <= this._computerEventSeq) {
            continue;
          }
          if (
            (evRunId === curRunId && (evGen === 0 || evGen === curGen) && evSeq === this._computerEventSeq + 1)
            || (evRunId !== curRunId && evGen > curGen)
          ) {
            this._applyComputerEventDirect(buffered);
          } else {
            remaining.push(buffered);
          }
        }
        this._computerPendingEvents = remaining;
      }
    },

    async refreshComputerRun(): Promise<ComputerRunSnapshot | null> {
      const api = this._api();
      if (!api) return null;
      let flight = computerRefreshFlights.get(this);
      if (!flight) {
        flight = { promise: null, rerun: false };
        computerRefreshFlights.set(this, flight);
      }
      if (flight.promise) {
        flight.rerun = true;
        return flight.promise;
      }
      const runSingleFlight = async (): Promise<ComputerRunSnapshot | null> => {
        try {
          do {
            flight!.rerun = false;
            try {
              const res = await api.get_computer_control_status();
              if (res && res.run_id) {
                this._applyComputerSnapshot(res as unknown as Record<string, unknown>);
              } else if (this._computerPendingEvents.length > 0) {
                const pending = this._computerPendingEvents.splice(0);
                for (const ev of pending) this._applyComputerEventDirect(ev);
              }
            } catch (err) {
              this.connected = false;
              this.notifyModal('电脑操控', '操控状态刷新失败: ' + err);
              // 校准失败时按序释放待回放事件，避免 UI 永久丢失后续状态
              const pending = this._computerPendingEvents.splice(0);
              for (const ev of pending) this._applyComputerEventDirect(ev);
              return null;
            }
          } while (flight!.rerun);
          return this.computerRun;
        } finally {
          flight!.promise = null;
        }
      };
      flight.promise = runSingleFlight();
      return flight.promise;
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

    async _computerControlMutation(
      method: 'pause_computer_control' | 'resume_computer_control' | 'stop_computer_control',
      label: string,
    ): Promise<boolean> {
      if (this.computerControlMutating) return false;
      const api = this._api();
      if (!api) return false;
      const runId = this.computerRun.run_id;
      if (!runId) {
        this.notifyModal('电脑操控', label + '失败：当前没有进行中的操控会话');
        return false;
      }
      this.computerControlMutating = true;
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
      } finally {
        this.computerControlMutating = false;
      }
    },

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
      this.subpage.show = false;
      this._captureCurrentRuntime();
      this.pendingWorkdir = null;
      this.currentSessionName = sessionName;
      if (displayName) this.currentSessionDisplayName = displayName;
      // 目标状态条属于会话级数据：切会话先清空再重拉，避免上一个会话的目标串显
      // 在新会话上（在途响应由 loadGoalState 的序号 + 会话名校验丢弃）
      this.goalState = null;
      void this.loadGoalState();
      this.permRequest.show = this.permRequest.sessionName === sessionName;
      this._clearSessionStatusDot(sessionName);
      // 优先恢复内存中的实时 runtime；运行中的会话不能被旧数据库快照覆盖
      const runtime = this._getSessionRuntime(sessionName);
      this._bindSessionRuntime(sessionName);
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
      // 通知宿主把内嵌浏览器归属切到目标会话：先等 switch_session 更新后端
      // _current_session，再同步活跃会话与重同步表面，避免 open 抢跑命中
      // 后端旧会话被误判为后台会话而静默延迟，或 set_active_session 迟到误隐藏。
      const switchSeq = ((this as any)._browserSwitchSeq || 0) + 1;
      (this as any)._browserSwitchSeq = switchSeq;
      this.browserSessionSyncing = true;
      const modelVersion = this._sessionModelVersion[sessionName] || 0;
      try {
      const res = await api.switch_session(sessionName, targetWorkdir);
        // 竞态防护：快速连点不同会话时响应可能乱序返回，过期响应直接丢弃，
        // 避免高亮停在 B 会话却渲染出 A 会话的内容
        if (this.currentSessionName !== sessionName) return;
        try {
          await api.browser_pane_set_active_session?.(sessionName);
        } catch {
        }
        if (this.currentSessionName !== sessionName) return;
        const fetchedMessages = Array.isArray(res.messages) ? res.messages : [];
        if (!runtime.running && (runtime.messages.length === 0 || (runtime.needsHistoryHydration && fetchedMessages.length > 0))) {
          runtime.messages = pairHistoryToolMessages(fetchedMessages);
          runtime.needsHistoryHydration = false;
          runtime.historyHydrated = true;
          this.historyHasMore = !!res.history_has_more;
          this.historyOldestId = res.history_oldest_id ?? null;
          runtime.historyHasMore = this.historyHasMore;
          runtime.historyOldestId = this.historyOldestId;
          void this.refreshTurnUndoStates();
          this._bindSessionRuntime(sessionName);
        } else if (!runtime.running && runtime.messages.length > 0 && runtime.needsHistoryHydration) {
          runtime.needsHistoryHydration = false;
        } else if (!runtime.running && res.messages && res.messages.length > 0) {
          const lastRes = res.messages[res.messages.length - 1];
          const lastLocal = runtime.messages[runtime.messages.length - 1];
          if (lastRes && lastRes.error_note && lastLocal && lastLocal.role === 'assistant') {
            lastLocal.error_note = lastRes.error_note;
            lastLocal.execution_state = 'error';
          }
        }

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
          this.fileTree = [];
          this.fileTreeExpanded = [];
          this.loadFileTreeInBackground(sessionName, res.workdir);
        }

        if (res.sessions) this.sessionGroups = res.sessions;

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
        if ((this as any)._browserSwitchSeq === switchSeq) {
          this.browserSessionSyncing = false;
        }
        // 加载态复位：仅在仍是当前会话时清除——快速连点不同会话时，
        // 过期响应返回不得清掉当前会话的加载态
        if (this.currentSessionName === sessionName) {
          this.sessionSwitching = false;
          this.browserSurfaceResyncNonce += 1;
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
      } catch { }
    },

    async loadFileTreeInBackground(sessionName: string, workdir: string) {
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
      } catch { }
    },

    async loadWorkspacePromptSuggestions(
      workdir: string,
      gitSnapshot?: GitSnapshotDto | null,
    ) {
      const target = String(workdir || '').trim();
      const seq = ++this.workspacePromptReqSeq;
      if (!target || target.toUpperCase() === 'GLOBAL') {
        this.workspacePromptSuggestions = [];
        return;
      }
      const api = window.pywebview?.api;
      if (!api?.get_workspace_prompt_suggestions) {
        this.workspacePromptSuggestions = [];
        return;
      }
      try {
        const res = await api.get_workspace_prompt_suggestions(target, gitSnapshot ?? null);
        if (seq !== this.workspacePromptReqSeq) return;
        if (String(this.workdir || '').trim() !== target) return;
        if (res && res.ok && Array.isArray(res.suggestions)) {
          this.workspacePromptSuggestions = res.suggestions;
        } else {
          this.workspacePromptSuggestions = [];
        }
      } catch {
        if (seq === this.workspacePromptReqSeq) this.workspacePromptSuggestions = [];
      }
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

    async refreshFileTree() {
      const api = this._api();
      if (!api || !this.workdir || this.fileTreeRefreshing) return;
      this.fileTreeRefreshing = true;
      const seq = ++this.fileTreeReqSeq;
      try {
        const res = await api.get_file_tree(this.workdir);
        if (seq !== this.fileTreeReqSeq) return;
        if (!res || !res.ok || !res.file_tree) return;
        this.fileTree = res.file_tree;
        const expanded = [...this.fileTreeExpanded].sort((a, b) => a.length - b.length);
        const stillExists: string[] = [];
        for (const p of expanded) {
          const node = this._findFileNode(p);
          if (!node) continue;
          stillExists.push(p);
          try {
            const r = await api.get_dir_children(p);
            node.children = (r && r.children) || [];
          } catch {
            node.children = [];
          }
        }
        this.fileTreeExpanded = stillExists;
      } catch { }
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
      // 草稿会话没有目标：清掉上一个会话的状态条，避免新对话里挂着旧目标
      this.goalState = null;
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
      this.sessionSwitching = false;
      this._captureRuntime('');
      this.currentWorkdirLabel = '对话';
      this.workdir = '';
      this.fileTree = [];
      this.fileTreeExpanded = [];
      void this.refreshActiveAgentName();
    },

    async createSessionSmart() {
      if (this.workdir && this.currentWorkdirLabel !== '对话') {
        await this.createSessionInGroup(this.workdir);
      } else {
        await this.createGlobalSession();
      }
    },

    async createSessionInGroup(targetWorkdir: string) {
      this.subpage.show = false;
      const treeMatches = this.workdir === targetWorkdir;
      const inheritedModelId = this.activeModelId;
      const inheritedModelName = this.activeModelName;
      this._captureCurrentRuntime();
      this.pendingWorkdir = targetWorkdir;
      this.currentSessionName = '';
      this.currentSessionDisplayName = '新会话';
      this.goalState = null;
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
      this.sessionSwitching = false;
      this._captureRuntime('');
      this.workdir = targetWorkdir;
      this.currentWorkdirLabel = targetWorkdir.split('\\').pop() || targetWorkdir;
      if (!treeMatches) {
        this.fileTree = [];
        this.fileTreeExpanded = [];
        this.loadFileTreeInBackground('', targetWorkdir);
      }
      void this.refreshActiveAgentName();
    },

    async createSecretarySession(name: string, persona = '') {
      const normalized = (name || '').trim();
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
      const personaDraft = (persona || '').trim();
      if (personaDraft.length > SECRETARY_PERSONA_MAX_LEN) {
        this.notifyModal(
          '新建秘书会话',
          `秘书人设最多 ${SECRETARY_PERSONA_MAX_LEN} 个字符，当前 ${personaDraft.length} 个`,
        );
        return;
      }
      const api = this._api();
      if (!api) return;
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
        this.permRequest.show = false;
        this.currentSessionName = res.session_name;
        this.currentSessionDisplayName = String(res.secretary_name || res.display_name || normalized);
        this._bindSessionRuntime(res.session_name);
        this.messages = [];
        this.currentStreamingText = '';
        this.currentThinkingText = '';
        this.subagentTasks = [];
        this.planData = { title: '', items: [], progress: 0 };
        this.tokenUsage = emptyTokenUsage();
        this.running = false;
        this.sessionSwitching = false;
        this._captureRuntime(res.session_name);
        this.workdir = '';
        this.currentWorkdirLabel = '对话';
        this.fileTree = [];
        this.fileTreeExpanded = [];
        await this.refreshSessions();
        await this.refreshActiveAgentName();
      } catch (err) {
        this.connected = false;
        this.notifyModal('新建秘书会话', '创建失败: ' + err);
      }
    },

    async exitWorkspaceToGlobal() {
      const globalGroup = this.sessionGroups.find(g => g.is_global || g.folder_path === 'GLOBAL');
      const latest = globalGroup && globalGroup.sessions && globalGroup.sessions[0];
      if (latest) {
        await this.switchSession(latest.name, latest.display_name || latest.name, 'GLOBAL');
      } else {
        await this.createGlobalSession();
      }
    },

    async switchToWorkspaceGroup(folderPath: string) {
      const group = this.sessionGroups.find(g => g.folder_path === folderPath);
      const latest = group && group.sessions && group.sessions[0];
      if (latest) {
        await this.switchSession(latest.name, latest.display_name || latest.name, folderPath);
      } else {
        await this.createSessionInGroup(folderPath);
      }
    },

    async deleteFolderGroup(workdir: string, folderTitle: string) {
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
          delete this.messageQueues[sessionName];
          delete this.sessionRuntimeByName[sessionName];
          // 归档区行内删除同样走这里：归档会话不在 sessionGroups 中，
          // 故单独摘除并刷新归档列表，避免已删除的条目残留在「已归档」区
          this.archivedSessions = this.archivedSessions.filter(s => s.name !== sessionName);
          void this.loadArchivedSessions();
          if (sessionName === this.currentSessionName && res.current_session) {
            await this.switchSession(res.current_session, '新会话');
          }
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
      if (!target) return;
      // 秘书会话改的是「秘书名」（secretary_name），不是会话名：先分流。
      // 秘书名与 session_xxx 永不相等，混在下面「与旧名相同」的判断里虽然不会
      // 误伤，但两者语义完全不同，分开更不容易埋坑
      if (this._isSecretarySession(oldName)) return this.renameSecretarySession(oldName, target);
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
      if (!target) return;
      if (this._isSecretarySession(sessionName)) return;
      const api = this._api();
      if (!api) return;
      try {
        const res = await api.move_session_to_workdir(sessionName, target);
        if (res && res.ok) {
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
          if (Array.isArray(res.sessions)) this.sessionGroups = res.sessions;
          else void this.refreshSessions();
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
          if (Array.isArray(res.sessions)) this.sessionGroups = res.sessions;
          else void this.refreshSessions();
          this.notifyChat(draft ? '秘书人设已更新，下一轮对话生效' : '已恢复默认秘书人设');
        } else {
          this.notifyModal('秘书人设', '保存失败: ' + (res && res.msg ? res.msg : '未知错误'));
        }
      } catch (err) {
        this.connected = false;
        this.notifyModal('秘书人设', '保存失败: ' + err);
      }
    },

    async loadArchivedSessions() {
      const api = (window.pywebview && window.pywebview.api) as PyWebViewAPI | null;
      if (!api || typeof api.get_archived_sessions !== 'function') return;
      const seq = ++this.archivedSessionsReqSeq;
      try {
        const list = await api.get_archived_sessions();
        if (seq !== this.archivedSessionsReqSeq) return;
        if (Array.isArray(list)) this.archivedSessions = list;
      } catch (err) {
        console.error('加载归档会话失败:', err);
      }
    },

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
      } catch (err) {
        this.connected = false;
        this.notifyModal('项目分组', '选择文件夹失败: ' + err);
      }
    },

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
          if (res && !res.ok && res.msg === '已取消选择') return;
        } catch { /* 原生对话框不可用时回退内置浏览器 */ }
      }
      this.remoteDirBrowser.plainPick = cb;
      await this.openRemoteDirBrowser();
    },

    async confirmRemoteWorkspace(useCurrent = false) {
      const api = this._api();
      if (!api) return;
      const path = this.remoteDirBrowser.selected
        || (useCurrent ? this.remoteDirBrowser.current : '');
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
      // 加 10s 本地兜底定时器：超时后强制清除该会话运行态。
      // 兜底只在「停止之后该会话运行态没被任何权威来源改写」时才该生效：终态事件、
      // 新回合开始、发送失败回滚都会撤掉它（_clearStopFallbackTimer）。否则用户
      // 停止后立刻重发，10s 到点时看到会话又在跑，会把新回合误判成「停止没生效」
      // 一并解锁，界面上「正在干活」消失、停在上一轮的结束态。
      // 验证思路：mock stop_current_turn 成功但不派发 turn_cancelled，
      // 10s 后 runningSessions 应不再包含该会话且发送恢复可用；若 10s 内已收到
      // turn_cancelled 并开始新回合，则新回合的运行态不得被定时器清掉。
      this._clearStopFallbackTimer(sessionName);
      const stopFallbackTimer = setTimeout(() => {
        stopFallbackTimers.delete(sessionName);
        // 判定用「运行会话表 ∪ 顶层运行标记」：两者不一致时（终态事件缺 session_name、
        // 事件错序等边界会留下半残态）只看运行会话表会永远判不到，界面就一直转，
        // 用户点多少次停止都没反应。顶层标记也当运行处理，兜底才能真正兜住。
        const stillRunning = this.runningSessions.includes(sessionName)
          || (sessionName === this.currentSessionName && this.running);
        if (stillRunning) {
          console.warn(`[T12] stopCurrentTurn 后 10s 未收到 turn_cancelled，本地兜底解锁会话: ${sessionName}`);
          this._setSessionRunning(sessionName, false);
        }
      }, 10000);
      stopFallbackTimers.set(sessionName, stopFallbackTimer);
      try {
        const res = await api.stop_current_turn(sessionName);
        if (!res || !res.ok) {
          this._clearStopFallbackTimer(sessionName);
          this.broadcastChatNotice('中断请求未成功送达', 'error', sessionName);
        } else {
          if ((res as { idle?: boolean }).idle) {
            this.notifyChat('当前没有正在执行的回合');
            if (this.runningSessions.includes(sessionName)
                || (sessionName === this.currentSessionName && this.running)) {
              console.warn(`[stop] 后端回报无活跃回合，本地按权威结论解锁: ${sessionName}`);
              this._setSessionRunning(sessionName, false);
            }
          } else {
            this.notifyChat('已中断当前回合');
            // 自愈：后端已受理停止，而本会话有运行标记却没进权威运行会话表——
            // 本地状态已经失真（事件错序 / 终态事件缺 session_name 都会留下半残态），
            // 这种态下不会再有事件来解锁，点多少次停止界面都不会变。
            // 只在「就是当前查看的会话」时动手，绝不误清后台会话的合法运行态。
            if (sessionName === this.currentSessionName && this.running
                && !this.runningSessions.includes(sessionName)) {
              console.warn(`[stop] 运行标记与会话表不一致，按停止请求本地自愈解锁: ${sessionName}`);
              this._setSessionRunning(sessionName, false);
            }
          }
        }
      } catch (err) {
        this._clearStopFallbackTimer(sessionName);
        this.connected = false;
        this.broadcastChatNotice('中断请求失败: ' + err, 'error', sessionName);
      }
    },

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
      const willQueue = this.runningSessions.includes(this.currentSessionName);
      const attachList = (attachments || []).filter(p => p && p.trim());
      if (!text.trim() && !attachList.length) return;
      const api = window.pywebview && window.pywebview.api;
      if (!api) {
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
        if (this.pendingWorkdir !== null) {
          const draftKey = this._runtimeKey();
          pendingDir = this.pendingWorkdir;
          this.pendingWorkdir = null;
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
          const trimmed = text.trim();
          if (trimmed) {
            const targetRt = this._getSessionRuntime(sessionName);
            targetRt.historyHydrated = true;
            const runtimeMsgs = targetRt.messages;
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
          this._getSessionRuntime(sessionName).permMode || undefined,
        );
        if (res && res.ok) {
          this.draftModelId = '';
          if (/^\/goal(\s|$)/i.test(text.trim())) void this.loadGoalState();
          if (pendingDir) {
          this.refreshSessions();
          }
        }
        if (!res || !res.ok) {
          if (res && (res as any).need_login) {
            this.openTrLoginModal();
            void this.refreshTrAuth();
          }
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
      } catch (err) {
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

    async editUserMessage(
      msg: ChatMessage, newText: string, imagePaths: string[] = [],
    ): Promise<boolean> {
      const text = (newText || '').trim();
      const paths = (imagePaths || []).filter(p => !!p);
      if (!text && !paths.length) return false;
      const msgId = msg._msg_id;
      if (!msgId) {
        this.broadcastChatNotice('该消息尚未入库，暂时无法编辑', 'error');
        return false;
      }
      const api = this._api();
      if (!api || typeof api.edit_user_message !== 'function') {
        this.broadcastChatNotice('当前后端不支持编辑消息，请重启软件后重试', 'error');
        return false;
      }
      const sessionName = this.currentSessionName;
      try {
        const res = await api.edit_user_message(sessionName, msgId, text);
        if (!res || !res.ok) {
          this.broadcastChatNotice(
            '编辑失败: ' + ((res && res.msg) || '未知错误'), 'error', sessionName,
          );
          return false;
        }
      } catch (err) {
        this.broadcastChatNotice('编辑失败: ' + err, 'error', sessionName);
        return false;
      }
      // 后端已截断：本地从该条开始（含这条）丢弃，historyHasMore 保持原值——
      // 更早的历史仍在库里，向前翻页游标依然有效。
      // 截断必须落在发起编辑的会话视图上：await 期间用户可能已切走，
      // this.messages 已指向别的会话（绝不能动），故取该会话自己的 runtime；
      // 仍停在原会话时顶层 messages 就是它的视图（_bindSessionRuntime 绑同一数组）
      const view = this.currentSessionName === sessionName
        ? this.messages
        : this._getSessionRuntime(sessionName).messages;
      const idx = view.findIndex(m => m._msg_id === msgId);
      if (idx >= 0) view.splice(idx);
      // 本地历史已截断：右侧红点导航的索引要同步少掉被截断的条目，
      // 否则已删除的消息还能在导轨里点进去（索引是后端按库重建的）
      void this.refreshNavIndex(sessionName);
      // 会话已切换就不能再 sendMessage：它按 currentSessionName 发送，会把编辑后的
      // 文本发到新会话，而两者工作目录可能不同，模型会按错误上下文执行工具
      if (this.currentSessionName !== sessionName) {
        this.broadcastChatNotice('会话已切换，编辑已保存但未重新生成', 'error', sessionName);
        return false;
      }
      // 编辑已落库（截断 + 新文本由本地历史承载），重发生成失败不能把它算成
      // 编辑失败：异常抛出去界面只会显示「编辑失败」，用户会以为改动丢了
      try {
        await this.sendMessage(text, paths);
      } catch {
        this.broadcastChatNotice('编辑已保存，但重新生成失败，请手动重发', 'error', sessionName);
        return false;
      }
      return true;
    },

    stampTurnElapsed() {
      const started = this.turnStartedAt;
      if (!started) return;
      const elapsed = Math.max(0, Math.round((Date.now() - started) / 1000));
      for (let i = this.messages.length - 1; i >= 0; i -= 1) {
        const message = this.messages[i];
        if (message.role === 'user') {
          message.turnElapsedS = elapsed;
          return;
        }
      }
      for (let i = this.messages.length - 1; i >= 0; i -= 1) {
        const message = this.messages[i];
        if (message.role === 'assistant') {
          message.turnElapsedS = elapsed;
          return;
        }
      }
    },

    async refreshSessions() {
      const api = this._api();
      if (!api) return;
      const seq = ++this.sessionsReqSeq;
      try {
        const sessions = await api.get_sessions();
        if (seq !== this.sessionsReqSeq) return;
        if (sessions) this.sessionGroups = sessions;
      } catch (err) {
        console.error('刷新会话列表失败:', err);
      }
    },

    async refreshTokenUsage() {
      const api = window.pywebview && window.pywebview.api;
      if (!api) return;
      const seq = ++this.tokenUsageReqSeq;
      try {
        const usage = await api.get_token_usage();
        if (seq !== this.tokenUsageReqSeq) return;
        if (usage) this.tokenUsage = usage;
      } catch (err) {
        console.error('刷新 Token 用量失败:', err);
      }
    },

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

    async _handleCloseRequested(this: any, hasRunningWork: boolean, trayAvailable: boolean) {
      const api = this._api();
      if (!api) return;
      if (!hasRunningWork) {
        try { await api.window_confirm_close('exit'); } catch (error) { console.warn('关闭失败', error); }
        return;
      }
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

    applyEmbeddedBrowserState(this: any, data: Record<string, unknown> | null | undefined) {
      const src = (data || {}) as Record<string, unknown>;
      // 归属会话：原生控件全进程唯一，状态事件带 session_name。只认领当前
      // 查看会话的状态，后台会话的导航/标题/加载态不得污染当前界面
      // （空值 = 旧后端/未占用，按当前会话处理）。
      const owner = String(src.session_name || '');
      if (owner && owner !== this.currentSessionName) return;
      const s = this.embeddedBrowser;
      s.url = String(src.url ?? '');
      s.title = String(src.title ?? '');
      s.can_go_back = !!src.can_go_back;
      s.can_go_forward = !!src.can_go_forward;
      s.loading = !!src.loading;
      s.ready = !!src.ready;
      s.visible = !!src.visible;
      if (typeof src.phase === 'string' && src.phase) {
        s.phase = src.phase as EmbeddedBrowserSurfacePhase;
      }
      const gen = Number(src.surface_generation);
      if (Number.isFinite(gen) && gen >= 0) {
        s.surface_generation = gen;
      }
    },

    async _handleOpenSessionEvent(event: BridgeEvent) {
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

    async pickBackgroundVideo() {
      const api = this._api();
      if (!api) return;
      this.bgConvert.message = '';
      this.bgConvert.error = '';
      const res = await api.pick_background_video();
      if (!res || !res.ok) {
        const message = (res as { error?: { message?: string } } | undefined)?.error?.message || '';
        if (message && !message.includes('取消')) this.bgConvert.error = message;
        return;
      }
      const info = res as unknown as BackgroundVideoInfo;
      this.bgConvert.video = info;
      this.bgConvert.resultPath = '';
      this.bgConvert.resultBytes = 0;
      this.bgConvert.percent = 0;
      this.bgConvert.etaSeconds = null;
      if (!this.bgConvert.outputDir) {
        this.bgConvert.outputDir = info.path.replace(/[\\/][^\\/]*$/, '');
      }
    },
    // 重新探测「已选视频」的预检信息（不重新弹文件选择框）。
    // 为什么需要：ffmpeg 组件是按需下载的，预检快照是选视频那一刻算出来的，
    // 装好组件后旧快照里的「未检测到 ffmpeg」blocker 不会自己消失，
    // 用户会被迫重选一次视频。下载完成事件与「重新检测」按钮都走这里，
    // 让 can_convert / blockers 按当前环境重算。
    // 只在已有视频时调用：没选视频时不做无意义探测；转换进行中也不重探，
    // 避免覆盖正在展示的预检信息。
    async redescribeBackgroundVideo(): Promise<boolean> {
      const api = this._api();
      if (!api) return false;
      const current = this.bgConvert.video;
      if (!current || !current.path) return false;
      if (this.bgConvert.running) return false;
      try {
        const res = await api.describe_background_video(current.path);
        if (!res || !res.ok) {
          const message = (res as { error?: { message?: string } } | undefined)?.error?.message || '';
          if (message) this.bgConvert.error = message;
          return false;
        }
        this.bgConvert.video = res as unknown as BackgroundVideoInfo;
        this.bgConvert.error = '';
        return true;
      } catch (err) {
        console.warn('[bg_convert] 重新探测视频信息失败:', err);
        return false;
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
      let video = this.bgConvert.video;
      if (!video) { this.bgConvert.error = '请先选择要转换的视频'; return; }
      // 兜底放行：预检快照可能是「ffmpeg 组件还没下载」时算的，此刻组件若已就绪，
      // 先按当前环境重探一次再判定，避免用户装好组件后还得重选视频
      if (!video.can_convert && this.ffmpegComponent.installed) {
        await this.redescribeBackgroundVideo();
        video = this.bgConvert.video;
        if (!video) { this.bgConvert.error = '请先选择要转换的视频'; return; }
      }
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
      this.connected = true;

      if (isComputerFlatEvent(event)) {
        this.reduceComputerEvent(event as ComputerControlEvent);
        return;
      }

      const currentRuntimeKey = this._runtimeKey();
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
      if (type === 'playwright_component_progress') {
        const next: PlaywrightComponentStatus = { ...this.playwrightComponent };
        next.stage = String(data.stage || '');
        next.percent = Number(data.percent) || 0;
        next.downloaded_bytes = Number(data.downloaded_bytes) || 0;
        next.total_bytes = Number(data.total_bytes) || 0;
        next.error = String(data.error || '');
        if (typeof data.installed === 'boolean') next.installed = data.installed;
        if (typeof data.version === 'string') next.version = data.version;
        next.downloading = next.stage === 'downloading' || next.stage === 'extracting';
        if (next.stage === 'done') {
          next.downloading = false;
          next.percent = 100;
        }
        this.playwrightComponent = next;
        if (next.stage === 'done' || next.stage === 'error') {
          void this.refreshPlaywrightComponentStatus();
        }
        return;
      }
      if (type === 'ffmpeg_component_progress') {
        const next: FfmpegComponentStatus = { ...this.ffmpegComponent };
        next.stage = String(data.stage || '');
        next.percent = Number(data.percent) || 0;
        next.downloaded_bytes = Number(data.downloaded_bytes) || 0;
        next.total_bytes = Number(data.total_bytes) || 0;
        next.error = String(data.error || '');
        if (typeof data.installed === 'boolean') next.installed = data.installed;
        if (typeof data.version === 'string') next.version = data.version;
        next.downloading = next.stage === 'downloading' || next.stage === 'extracting';
        if (next.stage === 'done') {
          next.downloading = false;
          next.percent = 100;
        }
        this.ffmpegComponent = next;
        if (next.stage === 'done' || next.stage === 'error') {
          void this.refreshFfmpegComponentStatus();
        }
        // 关键：ffmpeg 就绪后要让「已经选好的视频」立即可用。
        // 预检快照（bgConvert.video）是选视频那一刻算的，里面可能还写着
        // 「未检测到 ffmpeg」的 blocker；不重算的话用户必须重选视频才能转换。
        // 只在 done 且确实已选了视频时才重探，没选视频时不做无意义调用
        if (next.stage === 'done' && this.bgConvert.video) {
          void this.redescribeBackgroundVideo();
        }
        return;
      }
      if (type === 'search_component_progress') {
        const next: SearchComponentStatus = { ...this.searchComponent };
        next.stage = String(data.stage || '');
        next.percent = Number(data.percent) || 0;
        next.downloaded_bytes = Number(data.downloaded_bytes) || 0;
        next.total_bytes = Number(data.total_bytes) || 0;
        next.error = String(data.error || '');
        if (typeof data.installed === 'boolean') next.installed = data.installed;
        if (typeof data.version === 'string') next.version = data.version;
        next.downloading = next.stage === 'downloading' || next.stage === 'extracting';
        if (next.stage === 'done') {
          next.downloading = false;
          next.percent = 100;
        }
        this.searchComponent = next;
        if (next.stage === 'done' || next.stage === 'error') {
          void this.refreshSearchComponentStatus();
        }
        return;
      }
      // 前端构建产物运行期更新（gui_main 的 dist watchdog 推送 fingerprint）：
      // 触发「资源版本失配自愈」——等界面空闲时自动刷新到最新版本，
      // 避免旧构建哈希 chunk 404（mermaid 等懒加载失败）；
      // 不参与会话 runtime 归属，处理完直接返回
      if (type === 'frontend_dist_updated') {
        const fingerprint = typeof data.fingerprint === 'string' ? data.fingerprint : undefined;
        requestFrontendReload('dist-updated', fingerprint);
        return;
      }
      if (type === 'close_requested') {
        void this._handleCloseRequested(!!data.has_running_work, !!data.tray_available);
        return;
      }
      if (type === 'window_maximize_state') {
        this.windowMaximized = !!data.maximized;
        return;
      }
      if (type === 'embedded_browser') {
        this.applyEmbeddedBrowserState(data);
        return;
      }
      if (type === 'embedded_browser_automation') {
        const width = Number(data.width);
        const height = Number(data.height);
        const session = String(data.session_name || '');
        if (Number.isFinite(width) && Number.isFinite(height) && width > 0 && height > 0) {
          if (session && this.currentSessionName && session !== this.currentSessionName) {
            this._getSessionRuntime(session).automationViewport = [width, height];
          } else {
            this.embeddedBrowser.automationViewport = [width, height];
            if (this.currentSessionName) {
              this._getSessionRuntime(this.currentSessionName).automationViewport = [width, height];
            }
          }
        }
        if (!session) return;
        this.openBrowserTabForSession(session);
        return;
      }
      if (type === 'embedded_browser_assigned') {
        const session = String(data.session_name || '');
        if (!session) return;
        this.openBrowserTabForSession(session);
        if (session === this.currentSessionName && data.url) {
          this.browserNavRequest = { url: String(data.url), path: '', session };
          this.browserNavNonce += 1;
        }
        return;
      }
      if (type === 'embedded_browser_activation') {
        const session = String(data.session_name || '');
        if (!session || session !== this.currentSessionName) return;
        this.openBrowserTabForSession(session);
        if (data.url) {
          this.browserNavRequest = { url: String(data.url), path: '', session };
          this.browserNavNonce += 1;
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
      if (type === 'decision_settings_changed') {
        this.decisionEnabled = !!data.enabled;
        if (!this.decisionEnabled && this.permissionMode === 'auto_decision') {
          const nextMode = (data.permission_mode as PermissionMode) || 'confirm';
          this.permissionMode = nextMode === 'auto_decision' ? 'confirm' : nextMode;
          if (this.currentSessionName) {
            this._getSessionRuntime(this.currentSessionName).permMode = this.permissionMode;
          }
        }
        return;
      }

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
        this.decisionHasRecords = true;
        return;
      }
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
        this._captureRuntime(currentRuntimeKey, { skipLayout: true });
        // 跨会话临时绑定只切换会话数据，不恢复目标会话布局：顶层布局保持
        // 当前查看会话的实时状态，事件处理完在 finally 原样带回（否则目标
        // 会话的布局快照会覆盖用户刚对面板做的开合/页签操作）
        this._bindSessionRuntime(targetRuntimeKey, { skipLayout: true });
      }

      try {

      if (isTerminalType) {
        const finSession = eventSession;
        if (finSession) {
          this.runningSessions = this.runningSessions.filter(s => s !== finSession);
          this.queuedSessions = this.queuedSessions.filter(s => s !== finSession);
          // 终态已权威到达：停止兜底定时器到此失效，不再需要（也不允许）它插手
          this._clearStopFallbackTimer(finSession);
          this.running = false;
          this.stampTurnElapsed();
          this.turnStartedAt = 0;
          this.subagentTasks = [];
        } else if (this.running && this.runningSessions.length === 0) {
          console.warn(`[H2] 终态事件 ${type} 无归属且运行表为空，按当前会话 ${currentRuntimeKey} 收尾`);
          this._clearStopFallbackTimer(currentRuntimeKey);
          this.running = false;
          this.stampTurnElapsed();
          this.turnStartedAt = 0;
          this.subagentTasks = [];
        }
        // 无法推断归属且运行表非空：按 H2 契约「不更新任何会话运行态」，
        // 不能像旧实现那样把 this.running 无条件置 false——那会把正在跑的
        // 当前会话按成空闲，留下 running=false 而 runningSessions 仍有条目
        // 的反向错态（界面空闲、发送却被排队拦截）。
        // M31 修复：会话列表刷新统一在各终态分支里只做一次，
        // 不再在此处与 turn_finish/turn_cancelled 分支重复触发
      }

      if (type === 'turn_start') {
        const turnSession = String(data.session_name || '');
        if (switchedRuntime && turnSession) {
          const targetRt = this._getSessionRuntime(turnSession);
          if (!targetRt.historyHydrated) {
            targetRt.needsHistoryHydration = true;
          }
        }
        // 新回合已经开始：上一轮的停止请求必定已生效，兜底定时器不得再对该会话解锁
        this._clearStopFallbackTimer(turnSession);
        if (turnSession && !this.runningSessions.includes(turnSession)) {
          this.runningSessions.push(turnSession);
        }
        if (turnSession) {
          this.queuedSessions = this.queuedSessions.filter(s => s !== turnSession);
        }
        this.running = true;
        this.usedSkills = [];
        this.resumePlan = null;
        this.resumeBlockers = [];
        this.streamingSpeed = 0;
        speedTracker.reset();
        if (!this.turnStartedAt) this.turnStartedAt = Date.now();
        this.currentStreamingText = '';
        this.currentThinkingText = '';
        const turnUserText = data.display_text || data.user_text || '';
        // 去重：sendMessage 已乐观渲染用户消息 + running 占位（串行排队时
        // turn_start 滞后，避免新会话长时间空白）。若该用户消息已存在则不重复加。
        const alreadyHasUser = turnUserText && this.messages.some(
          (m: ChatMessage) => m.role === 'user' && m.content === turnUserText,
        );
        if (!alreadyHasUser) {
          this.messages.push({ role: 'user', content: turnUserText });
        }
        if (data.turn_id) {
          for (let i = this.messages.length - 1; i >= 0; i--) {
            const m = this.messages[i] as ChatMessage;
            if (m.role === 'user' && m.content === turnUserText && !m.turnId) {
              m.turnId = String(data.turn_id);
              break;
            }
          }
        }
        // 编辑已发送消息：乐观渲染的气泡同样缺数据库 id，靠 turn_start 一并补上。
        // 不回填的话刚发出的这条要切会话重载历史才拿得到 _msg_id，表现为
        // 「旧消息能编辑、刚发的不能」，同一个按钮两种行为
        if (data.user_msg_id) {
          for (let i = this.messages.length - 1; i >= 0; i--) {
            const m = this.messages[i] as ChatMessage;
            if (m.role === 'user' && m.content === turnUserText && !m._msg_id) {
              m._msg_id = Number(data.user_msg_id);
              break;
            }
          }
        }
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
          this.notifyChatLocal('后台子代理任务已全部结束，正在自动汇总结果', 'notice', turnSession || undefined);
        } else if (data.internal === 'secretary_dispatch_summary') {
          this.notifyChatLocal('派发任务已产生结果，秘书正在整理汇报', 'notice', turnSession || undefined);
        }
      }

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
        if (display?.is_skill && display.target && !this.usedSkills.includes(display.target)) {
          this.usedSkills.push(display.target);
        }
        const idx = id ? calls.findIndex(c => c.id === id) : -1;
        if (idx >= 0) {
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
          const mapPlanStatus = (raw: unknown): PlanTaskStatus => {
            const s = String(raw || '').trim();
            if (s === 'completed' || s === 'done' || s === 'x') return 'done';
            if (s === 'in_progress' || s === '/') return 'in_progress';
            if (s === 'failed') return 'failed';
            if (s === 'blocked') return 'blocked';
            if (s === 'cancelled') return 'cancelled';
            return 'todo';
          };
          const items: PlanTaskItem[] = data.items.map((it: any) => {
            const dependsOn = Array.isArray(it.depends_on)
              ? it.depends_on.map((d: any) => String(d || '').trim()).filter(Boolean)
              : [];
            const files = Array.isArray(it.files)
              ? it.files.map((f: any) => String(f || '').trim()).filter(Boolean)
              : [];
            const attempts = Number(it.attempts || 0);
            const item: PlanTaskItem = {
              id: it.id || String(Math.random()),
              text: it.subject || it.text || it.title || '',
              status: mapPlanStatus(it.status),
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
            };
            if (it.stage) item.stage = String(it.stage).trim();
            if (it.kind) item.kind = String(it.kind).trim();
            if (dependsOn.length > 0) item.depends_on = dependsOn;
            if (files.length > 0) item.files = files;
            if (it.verification) item.verification = String(it.verification).trim();
            if (it.verification_type) item.verification_type = String(it.verification_type).trim();
            if (attempts > 0) item.attempts = attempts;
            if (it.result) item.result = String(it.result).trim();
            if (it.error) item.error = String(it.error).trim();
            if (it.handoff_task_id) item.handoff_task_id = String(it.handoff_task_id).trim();
            return item;
          });
          const doneCount = items.filter((i: any) => i.status === 'done').length;
          const progress = items.length > 0 ? Math.round((doneCount / items.length) * 100) : 0;
          this.planData = {
            title: data.title || '当前任务计划',
            items,
            progress,
          };
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
          const taskId = String(handoffItem.handoff_task_id || data.source_task_id || '').trim();
          const patched = {
            execution_mode: handoffMode,
            owner: String(handoffItem.owner || ''),
            takeover_required: Boolean(handoffItem.takeover_required),
            handoff_reason: String(handoffItem.handoff_reason || ''),
            takeover_sequence: Number(handoffItem.takeover_sequence || 0),
            handoff_note: handoffNote,
            ...(taskId ? { handoff_task_id: taskId } : {}),
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
          this._captureRuntime(targetRuntimeKey, { skipLayout: switchedRuntime });
        }
      } else if (type === 'user_message_images') {
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
        this._setSessionStatusDot(eventSession, 'yellow');
      } else if (type === 'permission_dismiss') {
        // 后端权限队列已清空（请求被超时拒绝/取消）：收起内联权限卡片，避免死请求残留。
        // 必须整体重置：仅 show=false 会残留 reqId/sessionName，
        // switchSession 重显逻辑会把已失效的卡片复活，点击后报“权限响应发送失败”
        this.permRequest = { show: false, sessionName: '', reqId: '', summary: '', source: '', allowAlways: false, options: [], permissionOptions: [], functionName: '', undoImpact: null };
      } else if (type === 'message_queue') {
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
        const tasks = Array.isArray(data.tasks) ? (data.tasks as AutomationTaskInfo[]) : [];
        this.automationTasks = tasks;
      } else if (type === 'workflow_run_progress') {
        const runId = String(data.run_id || '');
        if (runId) {
          const list = this.workflowLiveEvents[runId] || [];
          list.push({
            run_id: runId,
            kind: String(data.kind || ''),
            payload: (data.payload as Record<string, any>) || {},
          });
          if (list.length > 100) list.splice(0, list.length - 100);
          this.workflowLiveEvents[runId] = list;
        }
      } else if (type === 'automation_run_updated') {
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
          this.queuedSessions = this.queuedSessions.map(s => (s === data.old ? data.new : s));
          // 停止兜底定时器同样跟名字走：首轮自动重命名常发生在停止附近，
          // 留在旧名下到点会因「旧名不在 runningSessions」而静默失效
          const pendingStopTimer = stopFallbackTimers.get(String(data.old));
          if (pendingStopTimer !== undefined) {
            stopFallbackTimers.delete(String(data.old));
            stopFallbackTimers.set(String(data.new), pendingStopTimer);
          }
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
        if (Array.isArray(data.sessions)) this.sessionGroups = data.sessions;
        else void this.refreshSessions();
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
        // 归属不明的终态事件（缺 session_name 且无法唯一推断）不碰运行态：
        // 否则会把正在跑的会话按成空闲，留下 running=false 而 runningSessions
        // 仍有条目的反向错态——界面看着空闲、发送却被排队拦截
        if (eventSession) this.running = false;
        this.streamingSpeed = 0;
        speedTracker.reset();
        void this.refreshTurnUndoStates();
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
        void this.refreshNavIndex(eventSession || undefined);
        this._setSessionStatusDot(eventSession, 'green');
      } else if (type === 'turn_cancelled') {
        if (eventSession) this.running = false;
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
        void this.refreshNavIndex(eventSession || undefined);
      } else if (type === 'turn_error') {
        const errorMsg = String(data.message || data.error || '');
        if (eventSession) this.running = false;
        this.streamingSpeed = 0;
        speedTracker.reset();
        let last = this.messages[this.messages.length - 1];
        if (!last || last.role !== 'assistant') {
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
        void this.refreshNavIndex(eventSession || undefined);
        this._setSessionStatusDot(eventSession, 'red');
        void this.loadResumePlan(eventSession || this.currentSessionName);
      } else if (type === 'token_usage') {
        this.tokenUsage = normalizeTokenUsage(data);
      } else if (type === 'token_usage_updated') {
        this.tokenUsage = normalizeTokenUsage(data);
      } else if (type === 'model_changed') {
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
        if (Array.isArray(data.skills)) {
          this.skills = data.skills;
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
        // 快照按「派生它的 run_subagent 调用」分组：每张卡片只展示自己创建
        // 的子代理，绝不把会话内所有（含历史）子代理堆到同一张卡上。
        const last = this.messages[this.messages.length - 1];
        if (!last || last.role !== 'assistant') return;
        const calls = (last.tool_calls || []).filter(c => c.name === 'run_subagent');
        if (!calls.length) return;
        const snapshots = (d.tasks || []).map((t: any): SubagentTaskSnapshot => ({
          id: String(t.id || ''),
          description: String(t.description || ''),
          status: String(t.status || ''),
          stage: String(t.stage || ''),
          ...(t.stage_detail ? { stage_detail: String(t.stage_detail) } : {}),
          elapsed: Number(t.elapsed || 0),
          ...(t.type ? { type: String(t.type) } : {}),
          ...(t.background ? { background: true } : {}),
          ...(t.tool_call_id ? { tool_call_id: String(t.tool_call_id) } : {}),
          ...(Array.isArray(t.stages) ? {
            stages: t.stages.map((s: any) => ({
              name: String(s.name || ''),
              at: Number(s.at) || 0,
              detail: String(s.detail || ''),
            })),
          } : {}),
        }));
        // 按调用 id 分组后整体替换（事件每轮携带全会话快照，追加会重复）；
        // 无归属 id 的快照（后台任务 / 旧后端）沿用旧语义：挂到仍在串行
        // 运行的那个调用上。有归属 id 但不在本条消息里的，属于更早回合的
        // 卡片，这里不重复展示（否则又退化回"全会话聚合"）。
        const orphans: SubagentTaskSnapshot[] = [];
        const grouped = new Map<string, SubagentTaskSnapshot[]>();
        for (const snapshot of snapshots) {
          if (!snapshot.tool_call_id) {
            orphans.push(snapshot);
            continue;
          }
          const bucket = grouped.get(snapshot.tool_call_id) ?? [];
          bucket.push(snapshot);
          grouped.set(snapshot.tool_call_id, bucket);
        }
        for (const call of calls) {
          const own = grouped.get(call.id);
          if (own) call.subagent_tasks = own;
        }
        if (orphans.length) {
          const target = [...calls].reverse().find(c => !c.result);
          if (target) target.subagent_tasks = orphans;
        }
        this.subagentTasks = snapshots;
      } else if (type === 'background_subagent_terminal') {
        const statusLabel: Record<string, string> = {
          completed: '已完成', failed: '已失败', timed_out: '已超时', cancelled: '已取消',
        };
        const label = statusLabel[data.status] || data.status;
        const desc = data.description ? `：${data.description}` : '';
        if (data.status === 'completed') {
          this.notifyChatLocal(`后台子代理${label}${desc}`, 'notice', eventSession);
        } else {
          const last = this.messages[this.messages.length - 1];
          if (last && last.role === 'assistant') {
            last.error_note = `后台子代理${label}${desc}`;
            this.notifyChatLocal(`后台子代理${label}${desc}`, 'notice', eventSession);
          } else {
            this.messages.push({ role: 'error', content: `后台子代理${label}${desc}` });
          }
        }
      } else if (type === 'background_task_finished') {
        const code = Number(data.returncode);
        const tid = String(data.task_id || '');
        const statusText = code === 0 ? '已完成' : `已结束（退出码 ${code}）`;
        this.notifyChatLocal(`后台任务 ${tid} ${statusText}`, 'notice', String(data.session_name || '') || undefined);
      } else if (type === 'pty_data') {
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

    async restoreSkill(skillId: string): Promise<boolean> {
      const api = this._api();
      if (!api) return false;
      try {
        const res = await api.restore_skill(skillId);
        if (res && res.ok && Array.isArray(res.skills)) {
          this.skills = res.skills;
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

    async deleteSkill(skillId: string) {
      const api = this._api();
      if (!api) return;
      try {
        const res = await api.delete_skill(skillId);
        if (res && res.ok) {
          this.skills = res.skills || this.skills;
          this.skillsLoading = false;
        } else {
          this.notifyModal('技能', '删除技能失败: ' + (res && res.msg ? res.msg : '未知错误'));
        }
      } catch (err) {
        this.connected = false;
        this.notifyModal('技能', '删除技能失败: ' + err);
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
      const seq = ++this.pluginsReqSeq;
      try {
        const list = await api.get_plugins();
        if (seq !== this.pluginsReqSeq) return;
        this.plugins = list || [];
      } catch {
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

    async deletePlugin(name: string) {
      const api = this._api();
      if (!api) return;
      try {
        const res = await api.delete_plugin(name);
        if (res && res.ok) {
          this.plugins = res.plugins || this.plugins;
        } else {
          this.notifyModal('插件', '删除插件失败: ' + (res && res.msg ? res.msg : '未知错误'));
        }
      } catch (err) {
        this.connected = false;
        this.notifyModal('插件', '删除插件失败: ' + err);
      }
    },

    async restorePlugin(name: string) {
      const api = this._api();
      if (!api) return;
      try {
        const res = await api.restore_plugin(name);
        if (res && res.ok) {
          this.plugins = res.plugins || this.plugins;
          this.notifyModal('插件', res.msg || '插件已恢复');
        } else {
          this.notifyModal('插件', '恢复插件失败: ' + (res && res.msg ? res.msg : '未知错误'));
        }
      } catch (err) {
        this.connected = false;
        this.notifyModal('插件', '恢复插件失败: ' + err);
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

    async refreshHooks() {
      const api = this._api();
      if (!api || typeof api.get_hooks !== 'function') return;
      const seq = ++this.hooksReqSeq;
      this.hooksLoading = true;
      try {
        const res = await api.get_hooks();
        if (seq !== this.hooksReqSeq) return;
        if (res && res.ok) this.hooks = res.hooks || [];
      } catch {
      } finally {
        if (seq === this.hooksReqSeq) this.hooksLoading = false;
      }
    },

    async _hookTrustAction(this: any, method: string, digest: string) {
      const api = this._api();
      if (!api || typeof (api as any)[method] !== 'function') return;
      try {
        const res = await (api as any)[method](digest);
        if (res && res.ok) {
          this.hooks = res.hooks || this.hooks;
          this.hooksReqSeq++;
        } else if (res && res.msg) {
          this.notifyModal('Hook 信任', res.msg);
        }
      } catch (err) {
        console.error('Hook 信任操作失败:', err);
        this.notifyModal('Hook 信任', 'Hook 信任操作失败: ' + err);
      }
    },

    grantHookTrust(digest: string) { return this._hookTrustAction('grant_hook_trust', digest); },
    denyHookTrust(digest: string) { return this._hookTrustAction('deny_hook_trust', digest); },
    revokeHookTrust(digest: string) { return this._hookTrustAction('revoke_hook_trust', digest); },
    resetHookTrust(digest: string) { return this._hookTrustAction('reset_hook_trust', digest); },

    async saveHook(payload: Record<string, any>): Promise<{ ok: boolean; msg?: string }> {
      const api = this._api();
      if (!api) return { ok: false, msg: '桥未就绪' };
      try {
        const res = await api.save_hook(payload);
        if (res && res.ok) {
          this.hooks = res.hooks || this.hooks;
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

    async refreshWorkflows(): Promise<boolean> {
      const api = this._api();
      if (!api) return false;
      this.workflowsLoading = true;
      try {
        const res = await api.get_workflows(this.workdir || '');
        if (res && res.ok && Array.isArray(res.workflows)) {
          this.workflows = res.workflows;
          this.workflowInvalid = Array.isArray(res.invalid) ? res.invalid : [];
          return true;
        }
        return false;
      } catch (err) {
        this.notifyModal('星流工作流', '读取工作流清单失败: ' + err);
        return false;
      } finally {
        this.workflowsLoading = false;
      }
    },

    async pickWorkflowFiles(): Promise<string[] | null> {
      const api = this._api();
      if (!api) return null;
      try {
        const res = await api.pick_workflow_files();
        if (res && res.ok && Array.isArray(res.paths) && res.paths.length) return res.paths;
        return null;
      } catch {
        return null;
      }
    },

    async importWorkflows(
      paths: string[],
      scope: 'project' | 'user',
      overwrite = false,
    ): Promise<{ imported: WorkflowImportedItem[]; failed: WorkflowImportFailure[] | null }> {
      const api = this._api();
      if (!api) {
        this.notifyModal('星流工作流', '桥未就绪');
        return { imported: [], failed: null };
      }
      try {
        const res = await api.import_workflows(paths, this.workdir || '', scope, overwrite);
        if (res && res.ok) {
          await this.refreshWorkflows();
          return {
            imported: (res.imported || []).map(i => ({ name: i.name, path: i.path })),
            failed: res.failed || [],
          };
        }
        this.notifyModal('星流工作流', '导入失败: ' + ((res && res.msg) || '未知错误'));
        return { imported: [], failed: null };
      } catch (err) {
        this.notifyModal('星流工作流', '导入失败: ' + err);
        return { imported: [], failed: null };
      }
    },

    async saveWorkflow(payload: {
      name: string;
      description: string;
      when_to_use?: string;
      args?: Record<string, any>;
      body: string;
      scope: 'project' | 'user';
      overwrite?: boolean;
    }): Promise<{ ok: boolean; errors?: string[]; msg?: string }> {
      const api = this._api();
      if (!api) return { ok: false, errors: ['桥未就绪'] };
      try {
        const res = await api.save_workflow({ ...payload, workspace_root: this.workdir || '' });
        if (res && res.ok) await this.refreshWorkflows();
        return res || { ok: false, errors: ['未知错误'] };
      } catch (err) {
        return { ok: false, errors: [String(err)] };
      }
    },

    async deleteWorkflow(name: string, scope: 'project' | 'user'): Promise<boolean> {
      const api = this._api();
      if (!api) return false;
      try {
        const res = await api.delete_workflow(name, scope, this.workdir || '');
        if (res && res.ok) {
          await this.refreshWorkflows();
          return true;
        }
        this.notifyModal('星流工作流', '删除失败: ' + ((res && res.msg) || '未知错误'));
        return false;
      } catch (err) {
        this.notifyModal('星流工作流', '删除失败: ' + err);
        return false;
      }
    },

    async loadWorkflowDetail(name: string): Promise<(WorkflowInfo & { body?: string }) | null> {
      const api = this._api();
      if (!api) return null;
      try {
        const res = await api.get_workflow_detail(name, this.workdir || '');
        if (res && res.ok && res.workflow) return res.workflow;
        return null;
      } catch {
        return null;
      }
    },

    async loadWorkflowRuns(name: string): Promise<boolean> {
      const api = this._api();
      if (!api) return false;
      try {
        const res = await api.get_workflow_runs(name, 20);
        if (res && res.ok) {
          this.workflowRuns[name] = res.runs || [];
          return true;
        }
        return false;
      } catch {
        return false;
      }
    },

    async clearWorkflowRuns(name: string): Promise<void> {
      const api = this._api();
      if (!api) return;
      try {
        const res = await api.clear_workflow_runs(name);
        if (res && res.ok) await this.loadWorkflowRuns(name);
      } catch {
      }
    },

    prefillComposer(text: string) {
      this._getSessionRuntime().composerText = text;
    },

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
      const seq = ++this.mcpReqSeq;
      try {
        const list = await api.get_mcp_servers();
        if (seq !== this.mcpReqSeq) return;
        if (list) this.mcps = list;
      } catch (err) {
        console.error('刷新 MCP 列表失败:', err);
      }
    },

    // 斜杠命令清单：只读接口、无副作用，加载失败时保持空数组（补全面板自动
    // 退化为技能 + MCP 两路候选），绝不向前端抛错打断输入
    async loadSlashCommands() {
      const api = window.pywebview && window.pywebview.api;
      if (!api || typeof api.list_slash_commands !== 'function') return;
      try {
        const res = await api.list_slash_commands();
        if (res && res.ok && Array.isArray(res.commands)) {
          this.slashCommands = res.commands;
        }
      } catch (err) {
        console.error('加载斜杠命令清单失败:', err);
      }
    },

    // 拉取当前会话的目标状态：没有目标、接口缺失或桥报错一律写 null，
    // 状态条据此静默隐藏——目标模式是辅助功能，不能因为读状态失败打断对话。
    // 序号 + 会话名双重防护：轮询请求可能跨会话返回，过期响应不能覆盖新会话。
    async loadGoalState() {
      const api = window.pywebview && window.pywebview.api;
      if (!api || typeof api.get_goal_state !== 'function') return;
      const session = this.currentSessionName;
      const seq = ++this.goalStateReqSeq;
      try {
        const res = await api.get_goal_state(session);
        if (seq !== this.goalStateReqSeq || session !== this.currentSessionName) return;
        this.goalState = res && res.ok ? (res.state ?? null) : null;
      } catch {
        // 静默失败但清空：读不到就当作没有目标，避免旧会话的状态条一直挂着
        if (seq === this.goalStateReqSeq && session === this.currentSessionName) {
          this.goalState = null;
        }
      }
    },

    async stopGoal() {
      const api = window.pywebview && window.pywebview.api;
      if (!api || typeof api.goal_stop !== 'function') return;
      try {
        await api.goal_stop(this.currentSessionName);
      } catch (err) {
        console.error('停止目标失败:', err);
      }
      await this.loadGoalState();
    },

    async resumeGoal() {
      const api = window.pywebview && window.pywebview.api;
      if (!api || typeof api.goal_resume !== 'function') return;
      try {
        await api.goal_resume(this.currentSessionName);
      } catch (err) {
        console.error('恢复目标失败:', err);
      }
      await this.loadGoalState();
    },

    // 状态条「确认达成」：自评档的达成标记不可信，循环停在 awaiting_confirm 等
    // 用户点头；后端 /goal confirm 只认这个状态，调用后重拉让徽标变「已完成」
    async confirmGoal() {
      const api = window.pywebview && window.pywebview.api;
      if (!api || typeof api.goal_confirm !== 'function') return;
      try {
        await api.goal_confirm(this.currentSessionName);
      } catch (err) {
        console.error('确认目标达成失败:', err);
      }
      await this.loadGoalState();
    },

    async exportSession(fmt: 'md' | 'html' | 'json') {
      const api = this._api();
      if (!api) return;
      try {
        const res = await api.export_session(fmt);
        if (res && res.ok) {
          const exportPath = String(res.path || '').trim();
          this.notifyChat(exportPath ? `导出成功: ${exportPath}` : '导出成功');
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
        '确定清空'
      );
      if (!ok) return;

      const api = this._api();
      if (!api) return;
      try {
        const res = await api.clear_session_history(this.currentSessionName);
        if (res && res.ok) {
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
      try {
        const list = await api.get_files_info(paths);
        return (list || []) as Array<{ path: string; name: string; ext: string; size: number | null; ok: boolean }>;
      } catch (err) {
        console.error('获取文件信息失败:', err);
        return [];
      }
    },

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

    async _attachSnipToComposer(path: string): Promise<void> {
      if (!path) return;
      const runtime = this._getSessionRuntime();
      if (!runtime.composerAttachments) runtime.composerAttachments = [];
      const target = runtime.composerAttachments;
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
      try {
        const thumbs = await this.fetchAttachmentThumbnails([path]);
        const url = thumbs.get(path);
        if (url) item.thumb = url;
      } catch { }
      this.notifyChat('截图已加入输入框附件');
    },

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
      const seq = ++this.filePreviewReqSeq;
      try {
        const res = await api.read_file_preview(path);
        if (seq !== this.filePreviewReqSeq) return;
        if (res && res.ok) {
          if (res.kind === 'image') {
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
      this.filePreview.dataUrl = '';
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

    async loadTokenStatistics(days: 0 | 1 | 7 | 30 = 30, force = false) {
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
        const result = await api.get_token_usage_statistics(0);
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

    openUpdateModal() {
      this.updateState.showUpdateModal = true;
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

    async refreshBackgroundTasks() {
      if (!(window.pywebview && window.pywebview.api)) return;
      try {
        const list = await window.pywebview.api.get_background_tasks();
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
      }
    },

    async loadResumePlan(sessionName?: string) {
      const api = window.pywebview && window.pywebview.api;
      const target = (sessionName || this.currentSessionName || '').trim();
      this.resumePlan = null;
      this.resumeBlockers = [];
      if (!api || typeof (api as any).resume_plan !== 'function' || !target) return;
      try {
        const res = await (api as any).resume_plan(target);
        // 只有「存在可续跑回合 + 无降级原因」才保留快照（按钮出现条件）
        if (res && res.ok && res.resumable && res.plan
          && (!res.aborts || res.aborts.length === 0)) {
          this.resumePlan = res.plan as ResumePlanSnapshot;
        }
      } catch {
        // 探测失败按不可续跑处理：不显示入口，也不打断错误条
      }
    },

    async runResume(sessionName?: string) {
      if (this.resumeBusy) return;
      const api = window.pywebview && window.pywebview.api;
      const target = (sessionName || this.currentSessionName || '').trim();
      if (!api || typeof (api as any).resume_execute !== 'function' || !target) return;
      this.resumeBusy = true;
      try {
        const res = await (api as any).resume_execute(target, []);
        this.resumeBlockers = Array.isArray(res?.blockers)
          ? (res.blockers as ResumeBlockerItem[])
          : [];
        if (res && res.ok) {
          this.resumePlan = null;
          this.resumeBlockers = [];
          const suffix = res.dry_run ? '（预演：未发请求）' : '';
          this.notifyChatLocal(`已从断点续跑：新回合 ${String(res.turn_id || '')}${suffix}`);
          return;
        }
        if (res && res.reason === 'confirm_required') {
          this.notifyChatLocal('断点续跑需要确认写操作：含写操作的调用需手动重做', 'notice');
          return;
        }
        this.notifyChatLocal(
          `断点续跑未执行：${String(res?.msg || res?.reason || '未知原因')}`,
          'error',
        );
      } catch (err) {
        this.notifyChatLocal(
          `断点续跑失败：${String((err as Error)?.message || err)}`,
          'error',
        );
      } finally {
        this.resumeBusy = false;
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
      }
    },

    async checkUpdate(manual: boolean = false) {
      const api = this._api();
      if (!api) return;

      // 静默检查防抖：距上次检查不足冷却窗口则跳过，避免崩溃循环/频繁重启
      // 反复请求 Gitee API 触发限流；手动检查不受限制
      const UPDATE_CHECK_COOLDOWN_MS = 3600 * 1000;
      if (!manual) {
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
          this.updateState.partUrls = Array.isArray(res.part_urls) ? res.part_urls.map(String) : [];
          this.updateState.sha256 = String(res.sha256 || '');
          this.updateState.status = res.has_update ? 'available' : 'idle';

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
          '立即安装',
          { tone: 'primary' },
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
          '中断并安装',
          { tone: 'danger' },
        );
        if (ok) {
          await this.launchInstaller();
        }
      } else {
        const ok = await this.askConfirm(
          '启动安装',
          `准备安装 星瑶Code v${this.updateState.latestVersion}`,
          '是否立即启动安装向导并退出当前程序？',
          '立即安装',
          { tone: 'primary' },
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
