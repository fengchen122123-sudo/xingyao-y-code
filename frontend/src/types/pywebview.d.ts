// Python <-> pywebview 桥接层类型定义
// 本文件按后端桥接 API 契约编写（而非当前 gui_bridge.py 的实现），
// 后端会单独重写以匹配此契约。

import type {
  GuiTheme,
  GuiThemeResult,
  GuiThemeSummary,
  NativeTitlebarColorCapability,
  NativeTitlebarTheme,
  ThemeRegionId,
} from '../themes/types';

export type PermissionMode = 'confirm' | 'accept_edits' | 'auto_decision' | 'plan' | 'full';
export type ReasoningChoice = 'default' | 'off' | 'on' | 'low' | 'medium' | 'high' | 'xhigh' | 'max';
export type ComputerProtocolChoice = '' | 'none' | 'generic' | 'native_openai';

export interface ReasoningOption {
  value: ReasoningChoice;
  label: string;
}

export interface ReasoningSnapshot {
  kind: 'effort' | 'toggle' | 'fixed' | 'hidden';
  visible: boolean;
  disabled: boolean;
  disabled_reason: string;
  choice: ReasoningChoice;
  options: ReasoningOption[];
}

export interface ModelInfo {
  id: string;
  name: string;
  provider_id: string;
  provider_name: string;
  context_window: number;
  supports_vision: boolean;
  supports_function_calling: boolean;
  computer_protocol: ComputerProtocolChoice;
  max_tokens?: number | null;
  // 思考设置：choice 为当前持久化选择（未设置时 "default"）；
  // options 为该模型真实支持的档位（后端按模型能力表给出，不猜测）；
  // visible=false 的模型不展示思考设置，kind=fixed 的模型不可调（只读提示）。
  reasoning_choice?: string;
  reasoning_options?: { value: string; label: string }[];
  reasoning_visible?: boolean;
  reasoning_kind?: string;
  reasoning_default_choice?: string;
  reasoning_disabled_reason?: string;
}

export interface ProviderInfo {
  id: string;
  name: string;
  base_url?: string;
  protocol?: string;
  preset_id?: string;
  has_api_key?: boolean;
  models?: ModelInfo[];
}

export interface ApiProtocolInfo {
  id: 'anthropic' | 'openai' | 'responses';
  label: string;
  path: string;
}

export interface PresetModelInfo {
  name: string;
  context_window: number;
  supports_vision: boolean;
}

export interface PresetProviderInfo {
  id: string;
  name: string;
  base_url: string;
  protocol: ApiProtocolInfo['id'];
  models: PresetModelInfo[];
}

export interface ModelSettingsSnapshot {
  protocols: ApiProtocolInfo[];
  presets: PresetProviderInfo[];
  providers: ProviderInfo[];
  models: ModelInfo[];
  active_model_id: string;
  secondary_model_id: string;
  secondary_model_roles: {
    vision: boolean;
    compression: boolean;
    subagent: boolean;
  };
}

export interface CommandShellOption {
  id: string;
  label: string;
  description: string;
  available: boolean;
  executable: string;
  unavailable_reason: string;
}

export interface ComputerBrowserOption {
  browser: string;
  label: string;
  available: boolean;
  executable: string;
  unavailable_reason: string;
}

export interface ComputerRunSnapshot {
  run_id: string;
  run_generation?: number;
  event_seq?: number;
  owner_session_id: string;
  owner_turn_id: string;
  status: string;
  target: string;
  browser: string;
  browser_mode?: string;
  model_name: string;
  protocol: string;
  started_at: number;
  action_count: number;
  current_action: string;
  current_surface: string;
  preview_url: string;
  pause_reason: string;
  stop_reason: string;
  error_code: string;
  hotkey_available: boolean;
  observation_updated_at_ms?: number;
  observation_error?: string;
  image_omitted?: string;
  current_frame_id?: string;
}

export interface FeishuSettingsSnapshot {
  enabled: boolean;
  app_id: string;
  has_secret: boolean;
  approvers: string[];
  notify_chat_id: string;
  status: {
    state: string;
    running: boolean;
    last_error: string;
    received_events: number;
    duplicate_events: number;
    uptime_seconds: number;
  };
}

export interface DecisionSettingsSnapshot {
  version: number;
  enabled: boolean;
  provider_ref: string;
  base_url: string;
  model_id: string;
  endpoint_style: 'decision' | 'systemone';
  key_mode: string;
  timeout_ms: number;
  scenes: Record<string, { enabled: boolean; threshold: number }>;
  audit_enabled: boolean;
}

export interface DecisionProbeKeyResult {
  key_index: number;
  status: 'available' | 'no_permission' | 'invalid_key' | 'timeout' | 'network_error' | 'rate_limited' | 'error';
  latency_ms: number;
  error: string;
}

export interface DecisionRuntimeState {
  key_count: number;
  active_key_index: number | null;
  denied_key_indexes: number[];
  cooldown_key_indexes: number[];
  last_probe: {
    ok: boolean;
    results: DecisionProbeKeyResult[];
    model_id: string;
    msg: string;
  } | null;
}

export interface DecisionModelOption {
  id: string;
  name: string;
  context: number;
  price_text: string;
}

export interface DecisionProviderOption {
  ref: string;
  name: string;
  base_url: string;
  key_count: number;
  auto_match: boolean;
}

export interface DecisionAuditRecentItem {
  ts: string;
  scene: string;
  adopted: boolean;
  latency_ms: number | null;
  fallback_reason: string;
  model_id: string;
  answers_digest: string;
  state_digest?: string;
  answer?: string;
  trace_id?: string;
  session_id?: string;
}

export interface DecisionRecordDetail {
  ok: boolean;
  record?: {
    ts?: string;
    scene?: string;
    adopted?: boolean;
    model_id?: string;
    latency_ms?: number | null;
    fallback_reason?: string | null;
    trace_id?: string;
    session_id?: string;
    request?: {
      model?: string;
      state?: string;
      questions?: unknown;
      state_truncated?: boolean;
    } | null;
    answers?: unknown;
    extra?: unknown;
  };
  msg?: string;
}

export type DecisionSceneMode = 'off' | 'live';

export interface DecisionSceneRow {
  scene: string;
  label: string;
  description: string;
  mode: DecisionSceneMode;
  mode_text: string;
  enabled: boolean;
  threshold: number;
  timeout_ms: number;
}

export interface DecisionAuditSummary {
  ok: boolean;
  today_count: number;
  today_adopted_count: number;
  today_fallback_count: number;
  session_total_count?: number;
  global_today_count?: number;
  has_business_records?: boolean;
  session_scoped?: boolean;
  recent: DecisionAuditRecentItem[];
  msg?: string;
}

export interface SystemSettingsSnapshot {
  command_shell: string;
  command_shell_options: CommandShellOption[];
  command_shell_available: boolean;
  command_shell_error: string;
  remote_token_ttl: number;
  agent_turn_step_limit: number;
  agent_turn_step_limit_min: number;
  agent_turn_step_limit_max: number;
  context_watermark_base: number | null;
  context_watermark_base_min: number;
  context_watermark_base_max: number;
  ai_output_dir: string;
  ai_output_dir_setting: string;
  ai_output_dir_default: string;
  browser_control_enabled: boolean;
  computer_control_enabled: boolean;
  computer_browser: string;
  computer_browser_options: ComputerBrowserOption[];
  computer_profile_path: string;
  computer_platform_supported: boolean;
  computer_hotkey_available: boolean;
  computer_hotkey: string;
  computer_run: ComputerRunSnapshot;
  // 全局子代理开关：关闭后不派发子代理，计划编排工具一并不可用（后端快照为准）
  subagent_enabled: boolean;
  // 当前活动模型的电脑操控资格（与 Turn 起点同一判定函数的只读展示）
  computer_active_model_capability: {
    model: string;
    eligible: boolean;
    protocol?: string;
    error_code?: string;
    reason?: string;
  };
}

export interface SystemSettingsMutationResult {
  ok: boolean;
  error_code?: string;
  msg?: string;
  settings: SystemSettingsSnapshot;
}

export interface ComputerControlResult extends ComputerRunSnapshot {
  ok: boolean;
  msg?: string;
}

export interface PlaywrightComponentStatus {
  installed: boolean;
  version: string;
  downloading: boolean;
  percent: number;
  downloaded_bytes: number;
  total_bytes: number;
  error: string;
  stage: string;
  dir: string;
}

export interface FfmpegComponentStatus {
  installed: boolean;
  version: string;
  downloading: boolean;
  percent: number;
  downloaded_bytes: number;
  total_bytes: number;
  error: string;
  stage: string;
  dir: string;
}

export interface SearchComponentStatus {
  installed: boolean;
  version: string;
  downloading: boolean;
  percent: number;
  downloaded_bytes: number;
  total_bytes: number;
  error: string;
  stage: string;
  dir: string;
}

export interface EmbeddedBrowserRect {
  x: number;
  y: number;
  width: number;
  height: number;
}

export type EmbeddedBrowserSurfacePhase = 'idle' | 'opening' | 'ready' | 'hidden' | 'error';

export interface EmbeddedBrowserState {
  url: string;
  title: string;
  can_go_back: boolean;
  can_go_forward: boolean;
  loading: boolean;
  ready: boolean;
  visible: boolean;
  phase?: EmbeddedBrowserSurfacePhase;
  surface_generation?: number;
}

export interface EmbeddedBrowserPaneResult {
  ok: boolean;
  state?: EmbeddedBrowserState | null;
  error?: string;
  ack?: boolean;
  stale?: boolean;
  surface_generation?: number;
  phase?: EmbeddedBrowserSurfacePhase;
}

export type DoctorCheckStatus = 'ok' | 'warn' | 'fail' | 'skip';

export interface DoctorCheck {
  name: string;
  status: DoctorCheckStatus;
  detail: string;
  hint: string;
}

// run_doctor 桥接返回：ok=false 时 checks 为 []（防御性兜底），
// summary 为各状态计数（键固定四态），供设置页顶部汇总展示
export interface DoctorReport {
  ok: boolean;
  msg?: string;
  checks: DoctorCheck[];
  summary: Record<DoctorCheckStatus, number>;
}

// 电脑操控四类事件（Task 13a）：平铺信封——run_id 与严格递增的 event_seq 都在
// 顶层，与既有 {"type","data"} 信封不同；事件体只含脱敏小 JSON，绝不携带截图
// base64 或输入正文。载荷字段按防御性解析声明为可选并容忍未知扩展字段
export type ComputerEventType =
  | 'computer_state'
  | 'computer_action_started'
  | 'computer_action_finished'
  | 'computer_observation';

export interface ComputerEventBase {
  run_id: string;
  run_generation?: number;
  event_seq: number;
  type: string;
}

export interface ComputerStateEvent extends ComputerEventBase {
  type: 'computer_state';
  status?: string;
  target?: string;
  reason?: string;
  [key: string]: unknown;
}

export interface ComputerActionStartedEvent extends ComputerEventBase {
  type: 'computer_action_started';
  action_ordinal?: number;
  action_kind?: string;
  target?: string;
  [key: string]: unknown;
}

export interface ComputerActionFinishedEvent extends ComputerEventBase {
  type: 'computer_action_finished';
  action_ordinal?: number;
  action_kind?: string;
  ok?: boolean;
  error_code?: string;
  summary?: string;
  duration_ms?: number;
  // R9 生产实测分段（仅本地事件数据）：queue/input/wait/observe 缺省表示
  // 该阶段未发生（“未测量”），绝不以 0ms 伪装瞬间完成
  queue_ms?: number;
  input_ms?: number;
  wait_ms?: number;
  observe_ms?: number;
  [key: string]: unknown;
}

export interface ComputerObservationEvent extends ComputerEventBase {
  type: 'computer_observation';
  action_ordinal?: number;
  target?: string;
  screen_sha256?: string;
  width?: number;
  height?: number;
  origin_or_window?: string;
  preview_url?: string;
  surface_id?: string;
  surface_generation?: number;
  [key: string]: unknown;
}

export type ComputerControlEvent =
  | ComputerStateEvent
  | ComputerActionStartedEvent
  | ComputerActionFinishedEvent
  | ComputerObservationEvent;

// 前端内存操作轨迹行（脱敏）：只含五个契约字段；type_text 只显示长度，
// 绝不存输入正文（设计 16.2 / 22）
export interface ComputerActionTrace {
  action_ordinal: number;
  summary: string;
  status: string;
  duration_ms: number;
  error_code: string;
}

export interface SkillInfo {
  id: string;
  short_id?: string;
  name: string;
  description: string;
  enabled: boolean;
  source?: string;
  // 该技能曾由内置释放、且内置源里仍有同名技能——用于判断能否「恢复内置版本」。
  // 旧后端不返回时按 undefined（不显示恢复入口）处理，避免误导用户
  from_builtin?: boolean;
}

export interface McpInfo {
  name: string;
  enabled: boolean;
  connected: boolean;
  tools_count: number;
  // 连接失败原因（正常为 null / 旧后端不返回该字段时为 undefined）
  last_error?: string | null;
}

// 斜杠命令清单（后端 core/slash_commands 注册表的只读投影，供输入补全使用）
export interface SlashCommandInfo {
  name: string;
  usage: string;
  summary: string;
  kind: string;
}

// /goal 目标状态码：取值必须与 core/slash_commands/goal/state.py 的 STATUS_* 一致
// （后端是权威定义，这里只是给前端徽标/按钮分支用的类型收窄）
export type GoalStatus =
  | 'running'
  | 'awaiting_confirm'
  | 'achieved'
  | 'blocked'
  | 'stopped'
  | 'interrupted';

export interface GoalStateInfo {
  goal: string;
  round: number;
  max_rounds: number;
  status: GoalStatus;
  last_summary: string;
  check: string;
}

export interface PluginInfo {
  name: string;
  version: string;
  description: string;
  source: string;
  status: string;
  enabled: boolean;
  tools: string[];
  has_settings?: boolean;
  error?: string;
}

export interface HookInfo {
  event: string;
  matcher: string;
  command: string;
  timeout_ms: number;
  status_message: string;
  source_path: string;
  scope: 'user' | 'project';
  digest: string;
  status: 'pending' | 'trusted' | 'stale' | 'blocked' | 'revoked';
  // 是否已禁用；后端总返回，可选以兼容旧桥接
  disabled?: boolean;
}

export interface AutomationTaskInfo {
  id: string;
  name: string;
  enabled: boolean;
  trigger_type: 'interval' | 'daily' | 'weekly' | 'startup' | 'once';
  trigger_config: Record<string, any>;
  target_session: string;
  session_mode: 'per_run' | 'fixed';
  workdir?: string;
  model_id?: string;
  prompt: string;
  permission_mode: 'confirm' | 'accept_edits' | 'auto_decision' | 'plan' | 'full';
  missed_run_policy: 'skip' | 'run_once';
  notify_on_success: boolean;
  created_at: string;
  updated_at: string;
  last_run_at?: string | null;
  next_run_at?: number | null;
  trigger_summary?: string;
  max_runs?: number | null;
  scheduled_run_count?: number;
  status?: 'active' | 'completed';
  remaining_runs?: number | null;
}

export interface AutomationRunInfo {
  id: string;
  task_id: string;
  task_name: string;
  trigger_kind: 'scheduled' | 'startup' | 'manual' | 'catchup';
  status: 'queued' | 'running' | 'completed' | 'cancelled' | 'error' | 'skipped' | 'interrupted';
  session_name: string;
  turn_id?: string | null;
  error?: string | null;
  queued_at: string;
  started_at?: string | null;
  finished_at?: string | null;
}

export interface WorkflowArgDecl {
  type: 'string' | 'number' | 'boolean' | 'json';
  description?: string;
  required?: boolean;
  default?: unknown;
}

export interface WorkflowInfo {
  name: string;
  scope: 'user' | 'project';
  path: string;
  description: string;
  when_to_use?: string;
  args?: Record<string, WorkflowArgDecl>;
}

export interface WorkflowInvalidInfo {
  name: string;
  scope?: string;
  path?: string;
  error: string;
}

export interface WorkflowImportReport {
  imported: Array<{ name: string; scope: string; path: string; skill?: string; frontmatter_error?: string }>;
  skipped: Array<{ name: string; scope?: string; reason?: string }>;
  invalid: Array<{ name: string; scope?: string; error?: string }>;
}

export interface WorkflowRunInfo {
  id: string;
  workflow_name: string;
  scope: string;
  session_name: string;
  args?: Record<string, any>;
  status: 'running' | 'completed' | 'failed' | 'interrupted';
  summary: string;
  error: string;
  artifact_dir: string;
  started_at: string;
  finished_at: string;
}

export interface WorkflowRunEvent {
  seq?: number;
  run_id: string;
  kind: string;
  payload: Record<string, any>;
}

export interface SessionInfo {
  name: string;
  // 不可变主键 sessions.id（按回合撤销的稳定归属标识；旧后端缺省为 undefined）
  session_id?: number;
  display_name: string;
  count: number;
  workdir: string;
  is_global?: boolean;
  secretary_name?: string;
  secretary_persona?: string;
  updated_at?: string;
}

export interface QueuedMessage {
  id: string;
  text: string;
  attachments: string[];
}

export interface SessionGroup {
  folder_path: string;
  folder_name: string;
  is_current?: boolean;
  is_global?: boolean;
  // 秘书归属分组：folder_path='SECRETARY' 只是分组标识，**不是磁盘目录**，
  // 因此不能拿它拼路径 / 打开资源管理器 / 删除分组；组内会话与「对话」同口径
  // （workdir='GLOBAL'、is_global=true）。后端固定把它排在 GLOBAL 之前。
  is_secretary?: boolean;
  sessions: SessionInfo[];
}

export interface ArchivedSessionInfo extends SessionInfo {
  archived_at?: string;
}

export interface ArchiveSessionResult {
  ok: boolean;
  name?: string;
  archived_at?: string | null;
  sessions?: SessionGroup[];
  archived_sessions?: ArchivedSessionInfo[];
  msg?: string;
}

// unarchive_sessions / delete_archived_sessions 的返回契约：names 是本次真正处理成功
// 的会话名，failed 是逐项失败原因（一项失败不阻断其它项）。故 ok 表示「是否至少成功
// 一项」而非「是否全部成功」；全失败时 names 为空、msg 给出整体原因。
export interface ArchiveBatchResult {
  ok: boolean;
  success?: boolean;
  count?: number;
  names?: string[];
  failed?: { name: string; msg?: string }[];
  sessions?: SessionGroup[];
  archived_sessions?: ArchivedSessionInfo[];
  current_session?: string;
  msg?: string;
}

export interface FileNode {
  n: string;
  d: number;
  children?: FileNode[];
}

export type PlanTaskStatus =
  | 'todo'
  | 'in_progress'
  | 'done'
  | 'failed'
  | 'blocked'
  | 'cancelled';

export interface PlanTaskItem {
  id: string;
  text: string;
  status: PlanTaskStatus;
  stage?: string;
  kind?: string;
  depends_on?: string[];
  files?: string[];
  verification?: string;
  verification_type?: string;
  attempts?: number;
  result?: string;
  error?: string;
  execution_mode?: string;
  owner?: string;
  takeover_required?: boolean;
  handoff_task_id?: string;
  handoff_reason?: string;
  takeover_sequence?: number;
  handoff_note?: string;
}

export interface ChangelogEntry {
  version: string;
  date: string;
  notes: string[];
}

export interface PlanData {
  title: string;
  items: PlanTaskItem[];
  progress: number;
}

export interface BackgroundTask {
  task_id: string;
  command: string;
  status: string;
  running_time?: string;
  session_id?: string;
  pid?: number;
  turn_id?: string;
  output_size?: number;
  last_output_age?: number | null;
  no_output_yet?: boolean;
}

export interface SubagentStagePayload {
  name: string;
  at: number;
  detail: string;
}

export interface SubagentToolRecord {
  id: string;
  seq: number;
  round: number;
  name: string;
  category: string;
  action: string;
  target: string;
  args_preview?: string;
  status: 'running' | 'ok' | 'err' | 'cancelled';
  started_at: number;
  finished_at?: number | null;
  duration_ms?: number | null;
  duration_text?: string;
  error_summary?: string;
  result_preview?: string;
}

export interface SubagentTaskDetail {
  id: string;
  description: string;
  type: string;
  status: string;
  stage: string;
  stage_detail?: string;
  elapsed: number;
  execution_elapsed?: number;
  approval_wait_elapsed?: number;
  timeout_seconds?: number;
  background: boolean;
  plan_item_id?: string;
  handoff?: string;
  model_name?: string;
  approval_summary?: string;
  rounds?: number;
  tool_ok?: number;
  tool_err?: number;
  changed_count?: number;
  started_at?: number | null;
  finished_at?: number | null;
  stages: SubagentStagePayload[];
  tool_records?: SubagentToolRecord[];
  changed_files?: string[];
  result: string;
  result_truncated?: boolean;
  error: string;
  partial: string;
}

export interface SubagentTaskListItem {
  id: string;
  description: string;
  type: string;
  status: string;
  stage: string;
  stage_detail?: string;
  elapsed: number;
  execution_elapsed?: number;
  approval_wait_elapsed?: number;
  timeout_seconds?: number;
  started_at?: number | null;
  finished_at?: number | null;
  background: boolean;
  model_name?: string;
  rounds?: number;
  tool_ok?: number;
  tool_err?: number;
  changed_count?: number;
}

export type ContextBreakdownSource =
  | 'messages'
  | 'system_prompt'
  | 'skills'
  | 'runtime_context'
  | 'system_tools'
  | 'mcp_tools';

export interface ContextBreakdownItem {
  source: ContextBreakdownSource;
  chars: number;
}

export interface TokenUsage {
  used: number;
  window: number;
  cached_pct: number;
  cumulative: number;
  cache_latest_pct: number | null;
  cache_hot_pct: number | null;
  cache_session_pct: number | null;
  cache_available: boolean;
  cache_hot_turns: number;
  cache_invalidation_reason: string;
  prompt_tokens: number;
  cached_tokens: number;
  cache_lane_model: string;
  cache_lane_api_base: string;
  cache_keepalive_hot_pct: number | null;
  cache_keepalive_session_pct: number | null;
  total_prompt: number;
  total_completion: number;
  total_cached: number;
  session_totals: SessionTokenTotals;
  context_breakdown: ContextBreakdownItem[];
}

export interface SessionTokenTotals {
  session_id: string;
  prompt: number;
  completion: number;
  cached: number;
  cache_prompt: number;
  total: number;
  requests: number;
  last_ts: number | null;
  keepalive_prompt: number;
  keepalive_cached: number;
  keepalive_requests: number;
}

export interface TokenUsageOverviewSession {
  session_id: string;
  prompt: number;
  completion: number;
  cached: number;
  cache_prompt: number;
  total: number;
  requests: number;
  last_ts: number | null;
}

export interface TokenUsageOverview {
  ok: boolean;
  msg?: string;
  current: string;
  totals: {
    prompt: number;
    completion: number;
    cached: number;
    cache_prompt: number;
    total: number;
    requests: number;
  };
  sessions: TokenUsageOverviewSession[];
}

export interface TokenStatisticsMetrics {
  prompt: number;
  completion: number;
  cached: number;
  cache_prompt: number;
  total: number;
  requests: number;
  cache_hit_rate: number | null;
}

export interface TokenStatisticsModel extends TokenStatisticsMetrics {
  key: string;
  provider: string;
  model: string;
}

export interface TokenStatisticsDay extends TokenStatisticsMetrics {
  date: string;
  models: TokenStatisticsModel[];
}

export interface TokenStatisticsSnapshot {
  ok: true;
  range: {
    days: 1 | 7 | 30 | 365;
    start_date: string;
    end_date: string;
  };
  summary: TokenStatisticsMetrics;
  models: TokenStatisticsModel[];
  days: TokenStatisticsDay[];
}

export interface InitialState {
  models: ModelInfo[];
  providers?: ProviderInfo[];
  active_model_id: string;
  active_model_name: string;
  secondary_model_id?: string;
  secondary_model_roles?: { vision: boolean; compression: boolean; subagent: boolean };
  permission_mode: PermissionMode;
  allow_outside_workspace?: boolean;
  system_settings?: SystemSettingsSnapshot;
  reasoning?: ReasoningSnapshot;
  sessions: SessionGroup[];
  skills: SkillInfo[];
  mcps: McpInfo[];
  workdir: string;
  file_tree?: FileNode[];
  token_usage: TokenUsage;
  version: string;
  active_sessions?: string[];
  active_turn_snapshots?: Record<string, ActiveTurnSnapshot>;
  active_turn_snapshot?: ActiveTurnSnapshot | null;
  message_queues?: Record<string, QueuedMessage[]>;
  theme_light?: boolean;
  gui_theme: GuiTheme | null;
  gui_themes: GuiThemeSummary[];
  gui_theme_error: import('../themes/types').GuiThemeError | null;
  gui_theme_list_error: import('../themes/types').GuiThemeError | null;
  native_titlebar_color_capability: NativeTitlebarColorCapability;
}

export interface ActiveTurnSnapshot {
  session_name: string;
  user_text: string;
  display_text: string;
  started_at_ms: number;
  thinking_text: string;
  streaming_text: string;
  tool_calls: Array<{
    id: string;
    name: string;
    label: string;
    display?: ToolDisplayPayload;
    started_at_ms?: number;
    ok?: boolean;
    summary?: string;
    result?: string;
    error?: string;
    duration_ms?: number;
    diff?: LegacyDiff;
  }>;
  internal_kind?: string;
}

export interface ToolDisplayPayload {
  category?: string;
  action?: string;
  target?: string;
  command?: string;
  metrics?: Record<string, number>;
  error_summary?: string;
  is_skill?: boolean;
  background?: boolean;
}

export interface TurnStartEventData {
  user_text: string;
  display_text?: string;
  session_name?: string;
  internal?: string;
  turn_id?: string;
  session_id?: number;
}

export interface TurnUndoUpdatedEventData {
  session_id: number;
  session_name: string;
  turn_id: string;
  operation_id: string;
  status: string;
  file_count: number;
  reverted_at: number;
  owner_kind?: string;
  message: string;
}

export interface UserAttachmentNotesEventData {
  session_name: string;
  notes: string[];
}

export interface ToolStartEventData {
  id: string;
  name: string;
  label: string;
  display?: ToolDisplayPayload;
  started_at_ms?: number;
}

export interface ToolFinishEventData {
  id: string;
  name: string;
  label: string;
  ok: boolean;
  summary: string;
  result: string;
  error?: string;
  duration_ms?: number | null;
  display?: ToolDisplayPayload;
}

export interface DiffReadyEventData {
  tool_call_id: string;
  file_path: string;
  diff_text: string;
  added?: number;
  removed?: number;
}

export interface TurnErrorEventData {
  message: string;
}

// ---------------------------------------------------------------------------
// 断点续跑（S4）：resume.enabled 默认关闭；关闭时两个接口都回 {ok:false,reason:'disabled'}。
// 快照只含判定与摘要（sha256 / 序号 / 原因），不含参数与结果正文。
// ---------------------------------------------------------------------------
export interface ResumeCallDecision {
  call_ordinal: number;
  tool_name: string;
  klass: 'replay_with_result' | 'replay_fresh' | 'block' | string;
  reason: string;
  step_id?: string;
  arguments_sha256?: string | null;
  result_sha256?: string | null;
  message_row_present?: boolean;
}

export interface ResumeAnchorSnapshot {
  complete: boolean;
  message_count: number;
  messages_sha256?: string;
  system_sha256?: string;
  tools_sha256?: string;
  cache_lane_sha256?: string;
}

export interface ResumePlanSnapshot {
  session_name: string;
  parent_turn_id: string;
  resume_from_step_ordinal: number;
  anchor: ResumeAnchorSnapshot;
  decisions: ResumeCallDecision[];
  blockers: string[];
  replayable_count: number;
  fresh_count: number;
  blocked_count: number;
  aborts: string[];
}

export interface ResumePlanResponse {
  ok: boolean;
  reason?: string;
  msg?: string;
  resumable?: boolean;
  plan?: ResumePlanSnapshot;
  aborts?: string[];
}

export interface ResumeBlockerItem {
  call_ordinal: number;
  tool_name: string;
  arguments_sha256?: string | null;
}

export interface ResumeExecuteResponse {
  ok: boolean;
  reason?: string;
  reasons?: string[];
  msg?: string;
  turn_id?: string;
  parent_turn_id?: string;
  dry_run?: boolean;
  replayed?: number;
  anchored?: boolean;
  blockers?: ResumeBlockerItem[];
}

export interface SubagentProgressEventData {
  tasks: Array<{
    id: string;
    description: string;
    type?: string;
    status: string;
    stage: string;
    stage_detail?: string;
    elapsed: number;
    background?: boolean;
    tool_call_id?: string;
    stages?: SubagentStagePayload[];
  }>;
}

export interface BridgeEvent {
  // 已知事件名的联合（供类型提示与检索用，不是闭集：末尾的 (string & {})
  // 兜底允许后端先推送、前端后适配的新事件；桥梁事件唯一赋值点在 appStore）
  type: BridgeEventType;
  data?: any;
  run_id?: string;
  event_seq?: number;
}

export interface BackgroundVideoInfo {
  path: string;
  name: string;
  bytes: number;
  ffmpeg_available: boolean;
  max_bytes: number;
  max_dimension: number;
  duration: number | null;
  width: number | null;
  height: number | null;
  can_convert: boolean;
  blockers: string[];
  warnings: string[];
}

export interface OpenSessionBridgeData {
  session_name: string;
  display_name?: string;
  workdir?: string;
}

export interface DecisionRecordedBridgeData {
  ts: string;
  scene: string;
  shadow: boolean;
  adopted: boolean;
  latency_ms: number | null;
  fallback_reason: string;
  model_id: string;
  answers_digest: string;
  session_id?: string;
}

/**
 * 桥事件名联合。只登记前端 `handleBridgeEvent` 里确有处理分支的事件，
 * `(string & {})` 兜底保证既有 `BridgeEvent.type` 的宽松口径不收紧
 * （未知事件仍可传入，不会因类型收窄而编译失败）。
 */
export type BridgeEventType =
  | 'open_session'
  | 'decision_recorded'
  | 'decision_settings_changed'
  | 'bg_convert_progress'
  | 'bg_convert_done'
  | 'bg_convert_error'
  | (string & {});

export type TurnUndoStatusValue =
  | 'recording' | 'ready' | 'no_changes' | 'unavailable' | 'conflicted'
  | 'reverting' | 'reverted' | 'interrupted' | 'recovering' | 'failed';

export type SnapshotAvailabilityValue =
  | 'available' | 'expired' | 'missing' | 'corrupt' | 'not_required';

export interface TurnUndoFileSummary {
  file_id: string;
  display_path: string;
  before_kind: string;
  after_kind: string | null;
  net_added: number;
  net_removed: number;
  diff_kind: string | null;
}

export interface TurnUndoStateSummary {
  checkpoint_id: string;
  turn_id: string;
  status: TurnUndoStatusValue;
  snapshot_availability: SnapshotAvailabilityValue;
  record_version: number;
  owner_kind: string;
  parent_checkpoint_id: string | null;
  description: string;
  started_at_ms: number;
  completed_at_ms: number | null;
  reverted_at_ms: number | null;
  has_uncovered_command_effects: number | boolean;
  unavailable_code: string | null;
  file_count: number;
  net_added: number;
  net_removed: number;
}

export interface TurnUndoNodeState {
  kind: string;
  exists: boolean;
  size: number | null;
  sha256: string | null;
  identity: string | null;
  link_count: number | null;
  symlink_target: string | null;
}

export interface TurnUndoFileConflict {
  file_id: string;
  display_path: string;
  expected: TurnUndoNodeState;
  actual: TurnUndoNodeState;
  reasons: string[];
}

export interface TurnUndoPreviewPayload {
  checkpoint_id: string;
  turn_id: string;
  status: TurnUndoStatusValue;
  snapshot_availability: SnapshotAvailabilityValue;
  record_version: number;
  file_count: number;
  restore_content_count: number;
  delete_created_count: number;
  restore_deleted_count: number;
  restore_path_state_count: number;
  created_files: string[];
  conflicts: TurnUndoFileConflict[];
  has_uncovered_command_effects: boolean;
  subsequent_completed_turns: number;
  workspace_root: string;
}

export interface TurnUndoExecutePayload {
  checkpoint_id: string;
  turn_id: string;
  status: string;
  operation_id: string;
  reverted_at_ms: number;
  restored_files: string[];
  deleted_created_files: string[];
  restored_deleted_files: string[];
  idempotent: boolean;
  original_reverted_at_ms: number | null;
}

export interface TurnUndoRecoveryPreviewPayload {
  operation_id: string;
  checkpoint_id: string;
  action: 'continue_revert' | 'cancel_revert';
  processed_files: string[];
  remaining_files: string[];
  unknown_files: string[];
  recovery_required: boolean;
  child_operation_id?: string | null;
}

export interface TurnUndoRecoveryExecutePayload {
  operation_id: string;
  parent_operation_id: string | null;
  action: 'continue_revert' | 'cancel_revert';
  status: string;
  checkpoint_status: string;
  affected_files: string[];
}

export interface TurnUndoErrorResponse {
  ok: false;
  code: string;
  msg: string;
  detail?: Record<string, any>;
}

export type TurnUndoStatesResponse =
  | { ok: true; states: TurnUndoStateSummary[] }
  | TurnUndoErrorResponse;

export type TurnUndoStatusResponse =
  | { ok: true; status: (TurnUndoStateSummary & { files: TurnUndoFileSummary[]; last_operation_id?: string | null }) | null }
  | TurnUndoErrorResponse;

export type TurnUndoFileDiffResponse =
  | {
      ok: true;
      diff: {
        file_id: string;
        display_path: string;
        diff_kind: string;
        diff_text: string | null;
        net_added: number;
        net_removed: number;
        before_size: number | null;
        after_size: number | null;
        truncated: boolean;
        undo_result: string | null;
      };
    }
  | TurnUndoErrorResponse;

export type TurnUndoPreviewResponse =
  | { ok: true; preview: TurnUndoPreviewPayload; preview_token: string }
  | TurnUndoErrorResponse;

export type TurnUndoExecuteResponse =
  | { ok: true; result: TurnUndoExecutePayload }
  | TurnUndoErrorResponse;

export type TurnUndoRecoveryPreviewResponse =
  | { ok: true; preview: TurnUndoRecoveryPreviewPayload; preview_token: string }
  | TurnUndoErrorResponse;

export type TurnUndoRecoveryExecuteResponse =
  | { ok: true; result: TurnUndoRecoveryExecutePayload }
  | TurnUndoErrorResponse;

// check_update() 的返回结构。cached_installer 为后端探测到的本地可复用
// 安装包绝对路径（缓存命中且通过官方 SHA256 / 大小校验），空串表示需下载；
// 旧后端不返回该字段时视为空串（向后兼容）。
export interface UpdateCheckResult {
  ok: boolean;
  has_update: boolean;
  cached_installer?: string;
  latest_version?: string;
  current_version?: string;
  release_title?: string;
  release_notes?: string;
  download_url?: string;
  asset_name?: string;
  asset_size?: number;
  part_urls?: string[];
  part_count?: number;
  is_split?: boolean;
  sha256?: string;
  msg?: string;
}

export interface NovelChapterSummaryDto {
  number?: number;
  name?: string;
  // 历史兼容保留：后端当前只下发 name，不再返回 title（旧代码仍可能读它），
  // 新代码请用 name，不要按 title 实现章名逻辑
  title?: string;
  words?: number;
  submitted?: boolean;
  outline_ready?: boolean;
  volume?: number;
}

export interface NovelStatusDto {
  is_novel: boolean;
  error?: string;
  title?: string;
  target_words?: number;
  chapters_written?: number;
  chapters?: NovelChapterSummaryDto[];
  total_words?: number;
  today_words?: number;
  next_chapter?: number;
  outline_ready?: boolean;
  active_foreshadows?: number;
  last_commit?: number;
}

export interface NovelWorkbenchForeshadowDto {
  id: string;
  summary: string;
  status: string;
  importance: string;
  // 以下章号在伏笔尚未埋设时不存在（缺省表示未知，不要当 0 处理）
  planted_chapter?: number;
  planned_resolution_chapter?: number;
  updated_chapter?: number;
  overdue: boolean;
  planned_overdue: boolean;
  stale_chapters: number;
}

export interface NovelWorkbenchCharacterDto {
  name: string;
  identity: string;
  state: string;
  location: string;
  goal: string;
  knowledge: string[];
  abilities_resources: string[];
  open_threads: string[];
  relationships: string[];
}

export interface NovelWorkbenchVolumeDto {
  no: number;
  name: string;
  chars: number;
  current: boolean;
}

export interface NovelWorkbenchDailyDto {
  date: string;
  words: number;
}

// get_novel_workbench 聚合快照：一次只读扫描设定库四张表（伏笔 / 角色 / 卷纲）
// 与近 7 天字数，供「小说工作台」页签渲染。非小说项目返回
// { is_novel: false }，扫描异常时额外带 error（与 NovelStatusDto 同风格），
// 此时四个数组不下发，前端按空数组兜底。
export interface NovelWorkbenchDto {
  is_novel: boolean;
  error?: string;
  foreshadows?: NovelWorkbenchForeshadowDto[];
  characters?: NovelWorkbenchCharacterDto[];
  volumes?: NovelWorkbenchVolumeDto[];
  recent_daily?: NovelWorkbenchDailyDto[];
}

// read_novel_doc 读取设定文档结果。成功 { ok: true, text, chars, path }：
// text 为文档全文（UTF-8），chars 与 novel_status 同口径字数，path 为相对
// 项目根的 posix 路径；失败 { ok: false, error }（文档不存在 / 不可读 /
// 越出项目根），前端据此显示错误文本。
export interface NovelDocReadDto {
  ok: boolean;
  error?: string;
  text?: string;
  chars?: number;
  path?: string;
}

// novel_audit 的单条结论（core.novel_service.novel_audit 五项只读检查）。
// level 是分级 S1~S4（S1 最高：状态断链、S2 伏笔超期、S3 进度/字数风险、
// S4 风格提示），type 为检查项名（tracking_state / foreshadow_overdue /
// chapter_wordcount / outline_gap / ai_patterns），file 为相对项目根的 posix
// 路径，message 是后端给出的可直接展示的中文结论。
export interface NovelAuditFindingDto {
  level: string;
  type: string;
  file: string;
  message: string;
}

// novel_audit 的跳过项：某项前置素材缺失（如没有细纲文件）或检查器抛错时，
// 只把该检查记进 skipped，不影响其余检查；check 为检查项名，reason 为后端
// 给出的跳过原因文本（如「没有细纲」「正文缺失」）。
export interface NovelAuditSkippedDto {
  check: string;
  reason: string;
}

// run_novel_audit 返回快照（只读体检，不写文件、不跑子进程）。成功时 ok=true，
// scope 为 chapter / all，findings 为逐条结论，summary 是 S1..S4 四个等级的计数
// （键固定四态，缺项按 0 展示），skipped 是单项前置缺失时的跳过原因；非小说项目
// 回 { is_novel:false }，参数非法或读取失败回 ok=false 且带 error。
export interface NovelAuditDto {
  is_novel?: boolean;
  ok?: boolean;
  scope?: string;
  findings?: NovelAuditFindingDto[];
  summary?: Record<string, number>;
  skipped?: NovelAuditSkippedDto[];
  error?: string;
}

export interface NovelAuditFixSnapshotDto {
  planned_resolution_chapter?: number | null;
  updated_chapter?: number | null;
}

export interface NovelAuditFixDto {
  ok: boolean;
  is_novel?: boolean;
  supported?: boolean;
  dry_run?: boolean;
  foreshadow_id?: string;
  before?: NovelAuditFixSnapshotDto;
  after?: NovelAuditFixSnapshotDto;
  would_change?: boolean;
  state_path?: string;
  table_path?: string;
  table_updated?: boolean;
  still_reported?: boolean;
  error?: string;
}

// get_novel_chapter 单章正文（章节编辑器用）。只读接口：ok=false 时带 error
// （非小说项目 / 章节不存在 / 正文不可读），前端据此显示错误文本。
// path 是相对项目根的 posix 路径，visible_chars 与 novel_status 同口径。
export interface NovelChapterDto {
  ok: boolean;
  error?: string;
  chapter?: number;
  path?: string;
  content?: string;
  visible_chars?: number;
}

// save_novel_chapter 保存结果。章节必须已存在（本接口不新建章节），
// visible_chars 为保存后按同一口径重算的字数，前端用它更新该章字数。
export interface NovelChapterSaveDto {
  ok: boolean;
  error?: string;
  chapter?: number;
  path?: string;
  visible_chars?: number;
}

export interface ProjectProfileDto {
  ok?: boolean;
  error?: string;
  workdir?: string;
  exists?: boolean;
  updated?: string;
  sections?: Record<string, string>;
}

// get_workspace_prompt_suggestions：新会话按项目特征生成的推荐提示词（只读）
export interface WorkspacePromptSuggestion {
  id: string;
  label: string;
  prompt: string;
}

export interface WorkspacePromptSuggestionsDto {
  ok: boolean;
  workdir?: string;
  suggestions: WorkspacePromptSuggestion[];
  msg?: string;
}

// list_novel_projects 单个小说项目（多书切换用）。path 是相对工作区的 posix
// 路径，工作区自身（.novel.json 在根目录）为 "."；chapters_written 只给章数，
// 不逐章读正文（只读扫描，快）。
export interface NovelProjectDto {
  path: string;
  title: string;
  chapters_written: number;
}

export interface NovelProjectListDto {
  ok: boolean;
  error?: string;
  root?: string;
  projects?: NovelProjectDto[];
}

export interface NovelExportDto {
  ok: boolean;
  error?: string;
  path?: string;
  chapters?: number;
  visible_chars?: number;
}

export interface PetStatusDto {
  running: boolean;
  pid: number | null;
  electron_path: string;
  pet_dir: string;
  available: boolean;
  enabled: boolean;
  notify_enabled?: boolean;
  runtime?: PetRuntimeStatus;
  // 当前激活的宠物形象（皮肤）id 与显示名：设置页的卡片高亮以它为准，
  // 因为只有后端知道磁盘上真正加载的是哪一个形象（前端列表可能是过期快照）
  skin_id?: string;
  skin_name?: string;
  ok?: boolean;
  msg?: string;
}

export interface PetSkinDto {
  id: string;
  name: string;
  description: string;
  persona?: string;
  author: string;
  source: 'builtin' | 'user' | (string & {});
  dir: string;
  assets_dir: string;
  config_path: string;
  active: boolean;
  thumbnail: string;
}

export interface PetImportPayload {
  mode: 'images' | 'zip';
  name: string;
  persona?: string;
  description?: string;
  activate?: boolean;
  files: Array<{ name: string; data: string }>;
}

export interface PetSkinMutationResult {
  ok: boolean;
  msg?: string;
  skin?: PetSkinDto;
  status?: PetStatusDto;
  list?: PetSkinResult;
  was_active?: boolean;
}

export interface PetSkinResult {
  ok: boolean;
  msg?: string;
  skins: PetSkinDto[];
  active: string;
  user_dir: string;
}

// 「无聊时自己说话」：桌宠闲置到一定时长后，由模型生成一句台词冒泡。
// 与「任务提示」是两条完全独立的气泡来源，各自保存自己的开关。
// idle_seconds 由后端钳在 60~86400 秒；model_id 为空串表示「自动」
// （优先副模型，没配副模型就用主模型），所以空串是合法值、不能省略成 undefined。
export interface PetChatModelOption {
  id: string;
  name: string;
  provider_name?: string;
}

export interface PetChatSettings {
  enabled: boolean;
  idle_seconds: number;
  model_id: string;
  models: PetChatModelOption[];
  status?: Record<string, unknown>;
}

export interface PetChatSettingsResult extends PetChatSettings {
  ok: boolean;
  msg?: string;
}

// Electron 运行时按需安装状态（core/pet_runtime.py::status() 与 detect() 合并后的结构，
// 由 gui_pet_mixin._pet_runtime_snapshot 组装）：运行时不再随安装包分发，首次启用桌宠时
// 后台下载约 151MB 分卷、解压到用户目录 ~/.ycode/pet-runtime，装一次后续复用。
// state 为下载状态机的当前阶段：idle/downloading/verifying/extracting/done/failed/unknown；
// downloaded/total/size 均为字节数，percent 为 0-100 的整数，msg 为给人看的提示或失败原因。
export interface PetRuntimeStatus {
  installed: boolean;
  exe: string | null;
  version: string | null;
  size: number;
  state: 'idle' | 'downloading' | 'verifying' | 'extracting' | 'done' | 'failed' | 'unknown' | (string & {});
  downloaded: number;
  total: number;
  percent: number;
  msg: string;
}

export interface PetRuntimeResult {
  ok: boolean;
  runtime?: PetRuntimeStatus;
  msg?: string;
}

export interface PetMutationResult {
  ok: boolean;
  msg?: string;
  status?: PetStatusDto;
}

// 后端所有方法均返回 Promise<纯 JSON 对象>；大多数写操作的返回值形状
// 为 { ok: boolean, msg?: string, ...其余按方法而定 }，此处以 any 承接，
// 具体字段读取处自行按契约文档取用，避免过度约束导致类型摩擦。
export interface PyWebViewAPI {
  get_initial_state(): Promise<InitialState>;
  get_changelog(): Promise<{ ok: boolean; entries: ChangelogEntry[]; msg?: string }>;
  select_workspace_folder(): Promise<any>;
  pick_folder_dialog(): Promise<{ ok: boolean; path?: string; msg?: string }>;
  get_workspace_state(workdir: string): Promise<any>;
  get_workspace_prompt_suggestions(
    workdir: string,
    gitSnapshot?: GitSnapshotDto | null,
  ): Promise<WorkspacePromptSuggestionsDto>;
  // 小说工作台状态（只读扫描，不写文件、不轮询）；非小说项目 is_novel=false
  get_novel_status(workdir: string): Promise<NovelStatusDto>;
  get_novel_chapter(workdir: string, chapter: number | string): Promise<NovelChapterDto>;
  save_novel_chapter(
    workdir: string,
    chapter: number | string,
    content: string,
  ): Promise<NovelChapterSaveDto>;
  // 项目状态（特性 1）：读当前项目画像三节（技术栈 / 架构决策 / 硬约束）。
  // 后端委托 core.project_profile.read_state，只读不写；失败回 { ok:false, error }
  get_project_profile(workdir: string): Promise<ProjectProfileDto>;
  save_project_profile(
    workdir: string,
    section: string,
    content: string,
  ): Promise<ProjectProfileDto>;
  // 列出工作区内的小说项目（多书切换数据源）：只读扫描 .novel.json，不逐章
  // 读正文；工作区自身不是小说项目时 projects 为空数组，不算错误
  list_novel_projects(workdir: string): Promise<NovelProjectListDto>;
  export_novel(workdir: string, fmt: 'md' | 'txt'): Promise<NovelExportDto>;
  // 小说工作台聚合（只读扫描，不写文件、不轮询）：一次读回伏笔 / 角色 / 卷纲
  // 三张设定表 + 近 7 天字数；非小说项目回 { is_novel:false }，异常回 error
  get_novel_workbench(workdir: string): Promise<NovelWorkbenchDto>;
  read_novel_doc(workdir: string, rel_path: string): Promise<NovelDocReadDto>;
  // 确定性体检（只读五项检查，不写文件）：scope 只接受 'chapter' | 'all'，
  // chapter 在 scope='chapter' 时指定章号（缺省 = 最新已写章节）；成功回
  // { ok:true, scope, findings, summary, skipped }，失败回 { ok:false, error }
  run_novel_audit(
    workdir: string,
    scope?: 'chapter' | 'all',
    chapter?: number | string,
  ): Promise<NovelAuditDto>;
  // 按一条审计结论修一处可显式验证的状态字段（只支持 foreshadow_overdue，
  // 只改追踪状态与伏笔表的「计划回收章」）：action 只接受
  // 'defer' | 'reschedule' | 'resolve'，其中 resolve 后端明确拒绝（回收需要
  // 正文证据）；reschedule 需给正整数 planned_resolution_chapter 且必须大于
  // 当前已提交章。dry_run=true 只算 before/after 不写文件；真写时写完自检并
  // 回报 still_reported
  run_novel_audit_fix(
    workdir: string,
    finding: NovelAuditFindingDto | null,
    action?: 'defer' | 'reschedule' | 'resolve',
    planned_resolution_chapter?: number | null,
    dry_run?: boolean,
  ): Promise<NovelAuditFixDto>;
  get_sessions(): Promise<SessionGroup[]>;
  create_session(title?: string, workdir?: string): Promise<any>;
  create_secretary_session(title: string, name: string, persona?: string): Promise<any>;
  set_secretary_persona(sessionName: string, persona: string): Promise<any>;
  get_active_agent_name(): Promise<{ agent_name: string; is_secretary: boolean; msg?: string }>;
  rename_secretary_session(sessionName: string, newName: string): Promise<any>;
  switch_session(sessionName: string, targetWorkdir?: string): Promise<any>;
  load_older_messages(sessionName: string, beforeId: number): Promise<any>;
  get_session_nav_index(sessionName: string): Promise<any>;
  delete_session(sessionName: string): Promise<any>;
  delete_folder_group(workdir: string): Promise<any>;
  rename_session(oldName: string, newName: string): Promise<any>;
  move_session_to_workdir(sessionName: string, workdir: string): Promise<any>;
  get_archived_sessions(): Promise<ArchivedSessionInfo[]>;
  archive_session(sessionName: string): Promise<ArchiveSessionResult>;
  unarchive_session(sessionName: string): Promise<ArchiveSessionResult>;
  unarchive_sessions(sessionNames: string[]): Promise<ArchiveBatchResult>;
  delete_archived_sessions(sessionNames: string[]): Promise<ArchiveBatchResult>;

  set_model(modelId: string, sessionName?: string): Promise<any>;
  get_reasoning_snapshot(modelId: string): Promise<any>;
  get_reasoning_snapshot_by_name(modelName: string): Promise<any>;
  set_reasoning_choice(modelId: string, choice: ReasoningChoice): Promise<any>;
  set_permission_mode(mode: PermissionMode, sessionName?: string): Promise<any>;
  set_sandbox_enabled(enabled: boolean): Promise<any>;
  get_system_settings(): Promise<SystemSettingsSnapshot>;
  set_command_shell(shellId: string): Promise<{ ok: boolean; settings: SystemSettingsSnapshot; msg?: string }>;
  // AI 生成内容目录：不可用路径显式拒绝（ok=false + msg），不做静默降级
  set_ai_output_dir(path: string): Promise<{ ok: boolean; settings: SystemSettingsSnapshot; msg?: string }>;
  set_remote_token_ttl(seconds: number): Promise<{ ok: boolean; settings: SystemSettingsSnapshot; msg?: string }>;
  // 子代理开关：关闭后不派发子代理，计划编排工具一并不可用
  set_subagent_enabled(value: boolean): Promise<{ ok: boolean; settings: SystemSettingsSnapshot; msg?: string }>;
  get_feishu_settings(): Promise<{ ok: boolean; settings?: FeishuSettingsSnapshot; msg?: string }>;
  set_feishu_settings(payload: {
    enabled: boolean;
    app_id: string;
    app_secret?: string;
    approvers: string[];
    notify_chat_id: string;
  }): Promise<{ ok: boolean; settings?: FeishuSettingsSnapshot; msg?: string }>;
  get_feishu_secret(): Promise<{ ok: boolean; app_secret?: string; msg?: string }>;
  test_feishu_connection(appId?: string, appSecret?: string): Promise<{ ok: boolean; msg?: string; endpoint_ready?: boolean }>;
  get_decision_settings(): Promise<{ ok: boolean; settings?: DecisionSettingsSnapshot; runtime?: DecisionRuntimeState; msg?: string }>;
  // 保存决策配置 patch（深合并 + 校验，非法值拒绝并回中文原因；settings 为保存后快照）
  set_decision_settings(patch: Partial<DecisionSettingsSnapshot>): Promise<{ ok: boolean; msg?: string; settings?: DecisionSettingsSnapshot }>;
  probe_decision(): Promise<{ ok: boolean; results: DecisionProbeKeyResult[]; model_id: string; msg?: string }>;
  get_decision_scenes(): Promise<{ ok: boolean; scenes?: DecisionSceneRow[]; enabled?: boolean; msg?: string }>;
  set_decision_scene(scene: string, mode: DecisionSceneMode, threshold?: number): Promise<{
    ok: boolean;
    msg?: string;
    hint?: string;
    scene?: DecisionSceneRow;
    scenes?: DecisionSceneRow[];
    mode?: DecisionSceneMode;
    mode_text?: string;
  }>;
  list_decision_models(): Promise<{ ok: boolean; models: DecisionModelOption[]; msg?: string }>;
  list_decision_providers(): Promise<{ ok: boolean; providers?: DecisionProviderOption[]; msg?: string }>;
  get_decision_audit_summary(limit?: number, session_id?: string, session_only?: boolean): Promise<DecisionAuditSummary>;
  get_decision_record_detail(trace_id: string): Promise<DecisionRecordDetail>;
  open_decision_audit_dir(): Promise<{ ok: boolean; path?: string; msg?: string }>;
  get_decision_audit_tail(limit?: number): Promise<{ ok: boolean; lines: string[]; msg?: string }>;
  set_agent_turn_step_limit(value: number): Promise<{ ok: boolean; settings: SystemSettingsSnapshot; msg?: string }>;
  set_context_watermark_base(value: number): Promise<{ ok: boolean; settings: SystemSettingsSnapshot; msg?: string }>;
  run_doctor(skipNetwork?: boolean): Promise<DoctorReport>;
  set_browser_control_enabled(value: boolean): Promise<SystemSettingsMutationResult>;
  set_computer_control_enabled(value: boolean): Promise<SystemSettingsMutationResult>;
  set_computer_browser(value: string): Promise<SystemSettingsMutationResult>;
  get_computer_control_status(): Promise<ComputerControlResult>;
  pause_computer_control(runId: string): Promise<ComputerControlResult>;
  resume_computer_control(runId: string): Promise<ComputerControlResult>;
  stop_computer_control(runId: string): Promise<ComputerControlResult>;
  // Provider 强制安全检查确认：官方契约验证完成前固定不可用（Task 11b 条件）
  acknowledge_computer_safety(runId: string, checkIds?: string[]): Promise<ComputerControlResult>;
  get_playwright_component_status(): Promise<{ ok: boolean; msg?: string; component: PlaywrightComponentStatus }>;
  download_playwright_component(): Promise<{ ok: boolean; msg?: string; component: PlaywrightComponentStatus }>;
  cancel_playwright_component_download(): Promise<{ ok: boolean; msg?: string; component: PlaywrightComponentStatus }>;
  get_ffmpeg_component_status(): Promise<{ ok: boolean; msg?: string; component: FfmpegComponentStatus }>;
  download_ffmpeg_component(): Promise<{ ok: boolean; msg?: string; component: FfmpegComponentStatus }>;
  cancel_ffmpeg_component_download(): Promise<{ ok: boolean; msg?: string; component: FfmpegComponentStatus }>;
  get_search_component_status(): Promise<{ ok: boolean; msg?: string; component: SearchComponentStatus }>;
  download_search_component(): Promise<{ ok: boolean; msg?: string; component: SearchComponentStatus }>;
  cancel_search_component_download(): Promise<{ ok: boolean; msg?: string; component: SearchComponentStatus }>;
  get_permission_rules(): Promise<any>;
  add_permission_rule(ruleType: 'command' | 'write' | 'edit', value: string): Promise<any>;
  remove_permission_rule(ruleType: 'command' | 'write' | 'edit', value: string): Promise<any>;
  clear_permission_rules(): Promise<any>;
  respond_permission(requestId: string, allow: boolean, always: boolean): Promise<any>;
  respond_permission_option(requestId: string, optionKey: string): Promise<any>;
  respond_permission_choice(requestId: string, choiceIndex: number, customText?: string): Promise<any>;

  send_user_message(text: string, sessionName?: string, attachments?: string[], workdir?: string, modelId?: string, permissionMode?: string): Promise<any>;
  edit_user_message(sessionName: string, msgId: number, newText: string, interrupt?: boolean): Promise<{ ok: boolean; msg?: string; removed?: number; session_name?: string }>;
  post_notice(message: string, sessionName?: string, kind?: 'notice' | 'error'): Promise<any>;
  stop_current_turn(sessionName?: string): Promise<any>;
  get_message_queue(sessionName?: string): Promise<{ ok: boolean; session_name: string; queue: QueuedMessage[] }>;
  cancel_queued_message(sessionName: string, messageId: string): Promise<any>;
  jump_queue(sessionName: string, messageId: string): Promise<any>;

  get_token_usage(): Promise<TokenUsage>;
  get_token_usage_overview(limit?: number): Promise<TokenUsageOverview>;
  get_token_usage_statistics(days: 0 | 1 | 7 | 30 | 365): Promise<TokenStatisticsSnapshot | { ok: false; msg: string }>;

  toggle_skill(skillId: string, enabled: boolean): Promise<any>;
  refresh_skills(): Promise<{ ok: boolean; skills?: SkillInfo[]; msg?: string }>;
  restore_skill(skill_id: string): Promise<{ ok: boolean; msg?: string; skills?: SkillInfo[] }>;
  delete_skill(skill_id: string): Promise<{ ok: boolean; msg?: string; skills?: SkillInfo[] }>;
  get_skills(): Promise<SkillInfo[]>;
  get_automation_tasks(): Promise<{ ok: boolean; tasks?: AutomationTaskInfo[]; msg?: string }>;
  save_automation_task(task: Record<string, any>): Promise<{ ok: boolean; id?: string; errors?: string[]; msg?: string }>;
  delete_automation_task(taskId: string): Promise<{ ok: boolean; deleted?: boolean; msg?: string }>;
  set_automation_task_enabled(taskId: string, enabled: boolean): Promise<{ ok: boolean; msg?: string }>;
  run_automation_task_now(taskId: string): Promise<{ ok: boolean; run_id?: string; msg?: string }>;
  get_automation_runs(taskId?: string, limit?: number): Promise<{ ok: boolean; runs?: AutomationRunInfo[]; msg?: string }>;
  clear_automation_runs(taskId: string): Promise<{ ok: boolean; msg?: string }>;
  get_workflows(workspaceRoot?: string): Promise<{ ok: boolean; workflows?: WorkflowInfo[]; invalid?: WorkflowInvalidInfo[]; msg?: string }>;
  get_workflow_detail(name: string, workspaceRoot?: string): Promise<{ ok: boolean; workflow?: WorkflowInfo & { body?: string }; msg?: string }>;
  pick_workflow_files(): Promise<{ ok: boolean; paths?: string[]; msg?: string }>;
  import_workflows(paths: string[], workspaceRoot?: string, scope?: 'project' | 'user', overwrite?: boolean): Promise<{ ok: boolean; imported?: Array<{ name: string; scope: string; path: string }>; failed?: Array<{ name: string; error: string; exists?: boolean }>; msg?: string }>;
  save_workflow(payload: { name: string; description: string; when_to_use?: string; args?: Record<string, WorkflowArgDecl>; body: string; scope: 'project' | 'user'; overwrite?: boolean; workspace_root?: string }): Promise<{ ok: boolean; name?: string; errors?: string[]; msg?: string }>;
  delete_workflow(name: string, scope: 'project' | 'user', workspaceRoot?: string): Promise<{ ok: boolean; msg?: string }>;
  get_workflow_runs(name?: string, limit?: number): Promise<{ ok: boolean; runs?: WorkflowRunInfo[]; msg?: string }>;
  clear_workflow_runs(name: string): Promise<{ ok: boolean; msg?: string }>;
  get_plugins(): Promise<any>;
  toggle_plugin(name: string, enabled: boolean): Promise<any>;
  delete_plugin(name: string): Promise<any>;
  restore_plugin(name: string): Promise<any>;
  trust_plugin(name: string): Promise<any>;
  get_plugin_settings(name: string): Promise<any>;
  run_plugin_settings_action(name: string, action: string, params?: Record<string, unknown>): Promise<any>;
  open_external_url(url: string): Promise<{ ok: boolean; msg?: string }>;
  // 基元律动账号中心只读查询（供原生弹窗直连官方接口，免去内嵌浏览器二次登录）
  tr_profile(): Promise<{ ok: boolean; data?: Record<string, any>; reason?: string; msg?: string }>;
  tr_usage_detail(range?: string, page?: number, page_size?: number): Promise<{ ok: boolean; data?: Record<string, any>; reason?: string; msg?: string }>;
  tr_wallet_transactions(): Promise<{ ok: boolean; data?: Record<string, any>; reason?: string; msg?: string }>;
  tr_call_logs(page?: number, page_size?: number, status?: string): Promise<{ ok: boolean; data?: Record<string, any>; reason?: string; msg?: string }>;
  get_hooks(): Promise<{ ok: boolean; hooks?: HookInfo[]; workdir?: string; msg?: string }>;
  grant_hook_trust(digest: string): Promise<any>;
  deny_hook_trust(digest: string): Promise<any>;
  revoke_hook_trust(digest: string): Promise<any>;
  reset_hook_trust(digest: string): Promise<any>;
  save_hook(payload: Record<string, any>): Promise<{ ok: boolean; msg?: string; hooks?: HookInfo[]; workdir?: string }>;
  delete_hook(sourcePath: string, digest: string): Promise<{ ok: boolean; msg?: string; hooks?: HookInfo[]; workdir?: string }>;
  set_hook_disabled(sourcePath: string, digest: string, disabled: boolean): Promise<{ ok: boolean; msg?: string; hooks?: HookInfo[]; workdir?: string }>;
  get_mcp_servers(): Promise<McpInfo[]>;
  // 只读：返回全部内置斜杠命令的元数据（无副作用，异常时回 {ok:false,msg}）
  list_slash_commands(): Promise<{ ok: boolean; commands?: SlashCommandInfo[]; msg?: string }>;
  get_goal_state(sessionName?: string): Promise<{ ok: boolean; state?: GoalStateInfo | null; msg?: string }>;
  goal_stop(sessionName?: string): Promise<{ ok: boolean; msg?: string }>;
  goal_resume(sessionName?: string): Promise<{ ok: boolean; msg?: string }>;
  goal_confirm(sessionName?: string): Promise<{ ok: boolean; msg?: string }>;
  toggle_mcp_server(name: string, enabled: boolean): Promise<any>;
  add_mcp_server(name: string, command: string, args: string[], env?: Record<string, string>, enabled?: boolean): Promise<any>;
  remove_mcp_server(name: string): Promise<any>;
  reconnect_mcp(): Promise<any>;

  export_session(fmt: 'md' | 'html' | 'json'): Promise<any>;
  clear_session_history(sessionName: string): Promise<any>;

  list_turn_undo_states(sessionName: string): Promise<TurnUndoStatesResponse>;
  get_turn_undo_status(sessionName: string, turnId: string): Promise<TurnUndoStatusResponse>;
  get_turn_undo_file_diff(sessionName: string, turnId: string, fileId: string): Promise<TurnUndoFileDiffResponse>;
  preview_turn_undo(sessionName: string, turnId: string): Promise<TurnUndoPreviewResponse>;
  execute_turn_undo(sessionName: string, turnId: string, previewToken: string): Promise<TurnUndoExecuteResponse>;
  preview_turn_undo_recovery(sessionName: string, operationId: string, action: 'continue_revert' | 'cancel_revert'): Promise<TurnUndoRecoveryPreviewResponse>;
  execute_turn_undo_recovery(sessionName: string, operationId: string, action: 'continue_revert' | 'cancel_revert', previewToken: string): Promise<TurnUndoRecoveryExecuteResponse>;

  resume_plan(sessionName: string): Promise<ResumePlanResponse>;
  resume_execute(sessionName: string, confirmed?: number[]): Promise<ResumeExecuteResponse>;

  read_file_preview(path: string): Promise<any>;
  check_paths_kind(paths: string[]): Promise<Array<{ path: string; kind: 'file' | 'dir' | 'missing' }>>;
  open_path(path: string): Promise<any>;
  open_folder_in_explorer(path: string): Promise<any>;
  reveal_in_explorer(path: string): Promise<any>;
  preview_office_doc(path: string): Promise<any>;
  save_pdf_edits(
    path: string,
    ops: any[],
    mode: 'copy' | 'overwrite',
    out_path: string,
  ): Promise<{ ok: boolean; path?: string; pages?: number; backup?: string; msg?: string }>;
  get_ui_layout(): Promise<{ ok: boolean; group_order?: string[]; session_order?: Record<string, string[]>; msg?: string }>;
  save_ui_layout(layout: { group_order: string[]; session_order: Record<string, string[]> }): Promise<{ ok: boolean; msg?: string }>;
  pick_files(): Promise<any>;
  get_files_info(paths: string[]): Promise<any>;   save_clipboard_image(dataUrl: string): Promise<any>;
  get_attachment_thumbnails(paths: string[]): Promise<any>;
   upload_file(name: string, dataB64: string): Promise<any>;
  report_dropped_files(event: any): Promise<{ ok: boolean; paths?: string[] }>;
  search_workspace_files(query: string, limit?: number): Promise<any>;
  search_sessions(query: string, limit?: number, currentSession?: string): Promise<{
    ok: boolean;
    sessions?: { name: string; count: number; updated_at: string; workdir: string }[];
    msg?: string;
  }>;
  search_sessions_full(
    query: string,
    scope?: 'all' | 'title' | 'content',
    workdir?: string,
    limit?: number,
    includeArchived?: boolean,
  ): Promise<{
    ok: boolean;
    total: number;
    results: {
      type: 'title' | 'content';
      session_name: string;
      display_name: string;
      workdir: string;
      workdir_label: string;
      updated_at: string;
      msg_count: number;
      role?: 'user' | 'assistant' | null;
      snippet?: string | null;
    }[];
    msg?: string;
  }>;
  get_dir_children(path: string): Promise<any>;
  get_file_tree(workdir: string): Promise<any>;
  set_titlebar_theme(dark: boolean): Promise<any>;
  window_minimize(): Promise<{ ok: boolean; error?: string }>;
  window_toggle_maximize(): Promise<{ ok: boolean; maximized?: boolean; error?: string }>;
  window_native_maximize?(): Promise<{ ok: boolean; maximized?: boolean; error?: string }>;
  window_close(): Promise<{ ok: boolean; error?: string }>;
  window_is_maximized(): Promise<{ ok: boolean; maximized?: boolean }>;
  window_begin_drag(): Promise<{ ok: boolean; error?: string }>;
  window_begin_resize(edge: 'left' | 'right' | 'top' | 'bottom' | 'top_left' | 'top_right' | 'bottom_left' | 'bottom_right'): Promise<{ ok: boolean; error?: string | null }>;
  window_confirm_close(choice: 'exit' | 'background' | 'cancel'): Promise<{ ok: boolean; error?: string }>;
  set_gui_theme(theme_light: boolean): Promise<GuiThemeResult<{
    theme: GuiTheme;
    native_titlebar_color_capability: NativeTitlebarColorCapability;
  }>>;
  list_gui_themes(): Promise<GuiThemeResult<{ themes: GuiThemeSummary[] }>>;
  get_gui_theme(themeId: string): Promise<GuiThemeResult<{ theme: GuiTheme }>>;
  get_active_gui_theme(): Promise<GuiThemeResult<{
    theme: GuiTheme;
    native_titlebar_color_capability: NativeTitlebarColorCapability;
  }>>;
  save_gui_theme(theme: GuiTheme): Promise<GuiThemeResult<{ theme: GuiTheme }>>;
  set_active_gui_theme(themeId: string): Promise<GuiThemeResult<{
    theme: GuiTheme;
    native_titlebar_color_capability: NativeTitlebarColorCapability;
  }>>;
  copy_gui_theme(sourceId: string, newId: string, newName: string): Promise<GuiThemeResult<{ theme: GuiTheme }>>;
  rename_gui_theme(themeId: string, newId: string, newName: string): Promise<GuiThemeResult<{ theme: GuiTheme }>>;
  delete_gui_theme(themeId: string): Promise<GuiThemeResult>;
  import_gui_theme(): Promise<GuiThemeResult<{ theme: GuiTheme }>>;
  export_gui_theme(themeId: string): Promise<GuiThemeResult<{ path: string }>>;
  // 图片另存为：url 必须是本进程媒体服务签发的地址；cancelled 表示用户取消
  save_media_image(url: string, suggestedName?: string): Promise<{
    ok: boolean;
    path?: string;
    msg?: string;
    cancelled?: boolean;
  }>;
  import_gui_theme_image(themeId: string, region: ThemeRegionId): Promise<GuiThemeResult<{ asset_path: string }>>;
  remove_gui_theme_image(themeId: string, region: ThemeRegionId): Promise<GuiThemeResult<{ theme: GuiTheme }>>;
  read_gui_theme_asset(themeId: string, assetPath: string): Promise<GuiThemeResult<{
    mime: string;
    base64: string;
  }>>;
  // ── 背景图转换（视频 → 动画 WebP 动态背景，后端 GuiBridge 同名前缀方法）──
  // 进度经 pywebview 事件推送：bg_convert_progress / bg_convert_done / bg_convert_error
  pick_background_video(): Promise<GuiThemeResult<BackgroundVideoInfo>>;
  describe_background_video(source: string): Promise<GuiThemeResult<BackgroundVideoInfo>>;
  pick_background_output_dir(): Promise<GuiThemeResult<{ path: string }>>;
  start_background_convert(
    source: string, outputDir: string, fps: number, quality: number, pingpong: boolean,
  ): Promise<{ ok: boolean; output?: string; msg?: string }>;
  cancel_background_convert(): Promise<{ ok: boolean; msg?: string }>;
  install_converted_background(webpPath: string): Promise<GuiThemeResult<{
    theme_id: string;
    asset_path: string;
  }>>;
  get_gui_theme_asset(themeId: string, assetPath: string): Promise<GuiThemeResult<{
    mime: string;
    base64: string;
  }>>;
  preview_native_titlebar(nativeTitlebar: NativeTitlebarTheme): Promise<GuiThemeResult<{
    native_titlebar_color_capability: NativeTitlebarColorCapability;
  }>>;
  restore_native_titlebar(): Promise<GuiThemeResult<{
    theme: GuiTheme;
    native_titlebar_color_capability: NativeTitlebarColorCapability;
  }>>;

  get_background_tasks(): Promise<BackgroundTask[]>;
  get_background_task_output(taskId: string, tailLines?: number): Promise<{ ok: boolean; output: string; msg?: string }>;
  kill_task(taskId: string): Promise<any>;

  get_subagent_task_detail(taskId: string, sessionName?: string): Promise<{
    ok: boolean;
    task?: SubagentTaskDetail | null;
    tasks: SubagentTaskListItem[];
    msg?: string;
  }>;
  cancel_subagent_task?(taskId: string, sessionName?: string): Promise<{
    ok: boolean;
    already_terminal?: boolean;
    status?: string;
    msg?: string;
  }>;

  browser_pane_open(rect: EmbeddedBrowserRect, url?: string, zoom?: number, sessionName?: string, surfaceGeneration?: number): Promise<EmbeddedBrowserPaneResult>;
  browser_pane_assign(url?: string, sessionName?: string): Promise<EmbeddedBrowserPaneResult>;
  browser_pane_set_active_session(sessionName?: string): Promise<EmbeddedBrowserPaneResult>;
  browser_pane_set_bounds(rect: EmbeddedBrowserRect, zoom?: number, sessionName?: string, surfaceGeneration?: number): Promise<EmbeddedBrowserPaneResult>;
  browser_pane_navigate(url: string): Promise<EmbeddedBrowserPaneResult>;
  browser_pane_back(): Promise<EmbeddedBrowserPaneResult>;
  browser_pane_forward(): Promise<EmbeddedBrowserPaneResult>;
  browser_pane_reload(): Promise<EmbeddedBrowserPaneResult>;
  browser_pane_stop(): Promise<EmbeddedBrowserPaneResult>;
  browser_pane_hide(sessionName?: string, surfaceGeneration?: number): Promise<EmbeddedBrowserPaneResult>;
  browser_pane_get_state(): Promise<EmbeddedBrowserPaneResult>;
  write_preview_page(
    htmlText: string,
    sourcePath?: string,
  ): Promise<{ ok: boolean; path?: string; error?: string }>;

  // ---------------------------------------------------------------------------
  // 右面板画笔截图（宿主 gui_snip_mixin.SnipBridgeMixin）。rect 为右面板的
  // CSS px 矩形（与 browser_pane_open 同一坐标系），冻结帧必须在其原生表面
  // 仍可见时捕获（快照要包含浏览器内容）。所有失败归一 {ok:false,msg}。
  // ---------------------------------------------------------------------------
  capture_right_panel_region(rect: EmbeddedBrowserRect): Promise<{
    ok: boolean;
    data_url?: string;
    width?: number;
    height?: number;
    msg?: string;
  }>;
  snip_copy_clipboard(dataUrl: string): Promise<{ ok: boolean; msg?: string }>;
  snip_save_image(dataUrl: string, suggestedName?: string): Promise<{
    ok: boolean;
    path?: string;
    msg?: string;
  }>;
  screen_snip_start(): Promise<{ ok: boolean; msg?: string }>;

  pty_terminal_ensure(cols: number, rows: number): Promise<{
    ok: boolean; pid?: number; cwd?: string; reused?: boolean; msg?: string;
  }>;
  pty_terminal_write(data: string): Promise<{ ok: boolean; msg?: string }>;
  pty_terminal_resize(cols: number, rows: number): Promise<{ ok: boolean; msg?: string }>;
  pty_terminal_kill(): Promise<{ ok: boolean; msg?: string }>;

  add_provider(name: string, baseUrl: string, apiKey: string, protocol: string): Promise<any>;
  get_model_settings(): Promise<ModelSettingsSnapshot>;
  add_preset_provider(presetId: string, apiKey: string): Promise<any>;
  update_provider(providerId: string, name: string, baseUrl: string, apiKey: string, protocol: string): Promise<any>;
  get_provider_api_key(providerId: string): Promise<{ ok: boolean; api_key?: string; msg?: string }>;
  delete_provider(providerId: string): Promise<any>;
  add_model(providerId: string, modelName: string, contextWindow: number, supportsVision: boolean, supportsFunctionCalling?: boolean, computerProtocol?: ComputerProtocolChoice, maxTokens?: number | null, reasoningChoice?: string | null): Promise<any>;
  fetch_provider_models(providerId: string): Promise<{ ok: boolean; msg?: string; models?: { id: string; model_type?: string; context_window?: number | null; supports_vision?: boolean | null; max_tokens?: number | null }[]; count?: number }>;
  update_model(providerId: string, modelId: string, modelName: string, contextWindow: number, supportsVision: boolean, supportsFunctionCalling?: boolean, computerProtocol?: ComputerProtocolChoice, maxTokens?: number | null, reasoningChoice?: string | null): Promise<any>;
  delete_model(modelId: string): Promise<any>;
  test_model(modelId: string): Promise<any>;
  set_secondary_model(modelId: string): Promise<any>;
  set_secondary_roles(roles: { vision: boolean; compression: boolean; subagent: boolean }): Promise<any>;

  check_update(manual?: boolean): Promise<UpdateCheckResult>;
  // 分卷模式：partUrls 为 .part1..partN 直链序列（Gitee 单附件 100MB 限制），
  // 下载器顺序下载拼接还原 exe；sha256 为 Release 描述里发布的官方校验和，
  // 非空时强制校验，不匹配即拒绝安装；totalSize 为各分卷字节数之和（进度分母）
  download_update(downloadUrl: string, targetVersion: string, partUrls?: string[], sha256?: string, totalSize?: number): Promise<any>;
  cancel_download_update(): Promise<any>;
  has_active_tasks(): Promise<any>;
  launch_installer(filePath: string): Promise<any>;

  pet_get_status(): Promise<PetStatusDto>;
  pet_set_enabled(enabled: boolean): Promise<PetMutationResult>;
  pet_set_notify_enabled(enabled: boolean): Promise<PetMutationResult>;
  pet_set_electron_path(path: string): Promise<PetMutationResult>;
  pet_restart(): Promise<PetMutationResult>;
  pet_get_runtime_status(): Promise<PetRuntimeResult>;
  pet_install_runtime(): Promise<PetRuntimeResult>;
  pet_list_skins(): Promise<PetSkinResult>;
  pet_set_skin(skin_id: string): Promise<PetMutationResult>;
  pet_import_skin(payload: PetImportPayload): Promise<PetSkinMutationResult>;
  pet_update_skin(skin_id: string, payload: { name?: string; persona?: string; description?: string }): Promise<PetSkinMutationResult>;
  pet_delete_skin(skin_id: string): Promise<PetSkinMutationResult>;
  pet_test_bubble(text?: string): Promise<{ ok: boolean; msg?: string }>;
  pet_chat_get_settings(): Promise<PetChatSettingsResult>;
  pet_chat_set_settings(payload: { enabled?: boolean; idle_seconds?: number; model_id?: string }): Promise<{ ok: boolean; msg?: string; status?: Record<string, unknown> }>;

  git_get_status(workdir: string): Promise<GitBridgeResult>;
  git_stage(workdir: string, paths: string[]): Promise<GitBridgeResult>;
  git_stage_all(workdir: string): Promise<GitBridgeResult>;
  git_unstage(workdir: string, paths: string[]): Promise<GitBridgeResult>;
  git_commit(workdir: string, title: string, body?: string, amend?: boolean): Promise<GitBridgeResult>;
  git_list_branches(workdir: string): Promise<GitBridgeResult>;
  git_switch_branch(workdir: string, target: string): Promise<GitBridgeResult>;
  git_create_branch(workdir: string, name: string, startPoint?: string, switchAfter?: boolean): Promise<GitBridgeResult>;
  git_fetch(workdir: string, remote?: string, prune?: boolean): Promise<GitBridgeResult>;
  git_pull(workdir: string): Promise<GitBridgeResult>;
  git_push(workdir: string, remote?: string, refspec?: string, forceWithLease?: boolean, token?: string): Promise<GitBridgeResult>;
  git_load_history(workdir: string, skip?: number, limit?: number): Promise<GitBridgeResult>;
  git_load_diff(workdir: string, path?: string, staged?: boolean, left?: string, right?: string): Promise<GitBridgeResult>;
  git_load_reflog(workdir: string, skip?: number, limit?: number): Promise<GitBridgeResult>;
  git_stash_create(workdir: string, message: string, includeUntracked: boolean): Promise<GitBridgeResult>;
  git_preview_risk(workdir: string, operation: string, params: Record<string, any>): Promise<GitBridgeResult>;
  git_confirm_operation(workdir: string, operation: string, params: Record<string, any>, token: string): Promise<GitBridgeResult>;
  git_get_trust(workdir: string): Promise<GitBridgeResult>;
  git_allow_once(workdir: string): Promise<GitBridgeResult>;
  git_trust_repository(workdir: string): Promise<GitBridgeResult>;
  git_load_conflict(workdir: string, path: string): Promise<GitBridgeResult>;
  git_resolve_conflict(workdir: string, path: string, resultText?: string, side?: string): Promise<GitBridgeResult>;
  git_recover_branch(workdir: string, oid: string, name: string): Promise<GitBridgeResult>;
  git_ai_assist(workdir: string, kind: string): Promise<GitBridgeResult>;
}

export interface GitBridgeResult {
  ok: boolean;
  snapshot?: GitSnapshotDto;
  error?: GitErrorDto;
  branches?: GitBranchDto[];
  commits?: GitCommitDto[];
  has_more?: boolean;
  diff?: GitDiffDto;
  entries?: GitReflogDto[];
  tags?: GitTagDto[];
  remotes?: GitRemoteDto[];
  stashes?: GitStashDto[];
  lines?: [string, number, string][];
  preview?: GitRiskPreviewDto;
  worktrees?: GitWorktreeDto[];
  conflict?: GitConflictContentDto;
  plan?: GitRebasePlanLineDto[];
  level?: 'trusted' | 'allowed_once' | 'untrusted';
  fingerprint?: string;
}

export interface GitErrorDto {
  code: string;
  message: string;
  retryable: boolean;
  command: string;
}

export interface GitFileChangeDto {
  path: string;
  index_status: string;
  worktree_status: string;
  orig_path: string | null;
  is_binary: boolean;
}

export interface GitConflictDto {
  path: string;
  kind: string;
}

export interface GitSnapshotDto {
  repository: { worktree_root: string; common_git_dir: string; git_version: string; is_linked_worktree: boolean };
  head_oid: string;
  branch_name: string | null;
  head_state: 'normal' | 'detached' | 'unborn';
  upstream: string | null;
  ahead: number;
  behind: number;
  staged: GitFileChangeDto[];
  unstaged: GitFileChangeDto[];
  untracked: string[];
  ignored_count: number;
  conflicts: GitConflictDto[];
  stash_count: number;
  operation_kind: string;
  operation_phase: string;
  identity_state: string;
  worktree_version: string;
  refs_version: string;
}

export interface GitBranchDto {
  name: string;
  is_remote: boolean;
  is_current: boolean;
  upstream: string | null;
  ahead: number;
  behind: number;
  upstream_gone: boolean;
  oid: string;
}

export interface GitCommitDto {
  oid: string;
  short: string;
  parents: string[];
  author_name: string;
  author_email: string;
  author_time: number;
  committer_time: number;
  summary: string;
  refs: string[];
}

export interface GitDiffHunkDto {
  old_start: number;
  old_lines: number;
  new_start: number;
  new_lines: number;
  header: string;
  lines: string[];
}

export interface GitDiffDto {
  raw_text: string;
  is_binary: boolean;
  hunks: GitDiffHunkDto[];
}

export interface GitReflogDto {
  oid: string;
  selector: string;
  message: string;
}

export interface GitTagDto {
  name: string;
  oid: string;
  is_annotated: boolean;
  message: string;
}

export interface GitRemoteDto {
  name: string;
  fetch_url: string;
  push_url: string;
}

export interface GitStashDto {
  index: number;
  message: string;
  oid: string;
}

export interface GitRiskPreviewDto {
  operation: string;
  risk_level: string;
  confirm_required: boolean;
  typed_confirm_required: boolean;
  confirm_text: string;
  versions: { worktree_version: string | null; refs_version: string | null };
  token: string | null;
}

export interface GitWorktreeDto {
  path: string;
  head_oid: string;
  branch: string | null;
  is_bare: boolean;
}

export interface GitConflictContentDto {
  path: string;
  base: string | null;
  ours: string | null;
  theirs: string | null;
  worktree: string | null;
}

export interface GitRebasePlanLineDto {
  action: string;
  oid: string;
  summary: string;
  plan?: GitRebasePlanLineDto[];
  conflict?: GitConflictContentDto;
}

declare global {
  interface Window {
    pywebview?: {
      api: PyWebViewAPI;
    };
    onBridgeEvent?: (event: BridgeEvent) => void;
  }
}
