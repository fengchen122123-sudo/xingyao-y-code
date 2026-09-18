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

export type PermissionMode = 'confirm' | 'accept_edits' | 'plan' | 'full';
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
  // 电脑操控能力契约（设计 9.1）：旧模型缺省 false / 'none'，不自动获得资格
  supports_function_calling: boolean;
  computer_protocol: ComputerProtocolChoice;
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

// core.computer.settings.BrowserOption.to_dict()：单个受支持浏览器的独立探测结果
export interface ComputerBrowserOption {
  browser: string;
  label: string;
  available: boolean;
  executable: string;
  unavailable_reason: string;
}

// core.computer.models.ComputerRunSnapshot.to_public_dict()：脱敏后的公开字段
export interface ComputerRunSnapshot {
  run_id: string;
  owner_session_id: string;
  owner_turn_id: string;
  status: string;
  target: string;
  browser: string;
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
  // 电脑操控设置（设计 8.1/8.2）：两个开关默认关闭，浏览器选项独立探测
  browser_control_enabled: boolean;
  computer_control_enabled: boolean;
  computer_browser: string;
  computer_browser_options: ComputerBrowserOption[];
  computer_profile_path: string;
  computer_platform_supported: boolean;
  computer_hotkey_available: boolean;
  computer_hotkey: string;
  computer_run: ComputerRunSnapshot;
  // 当前活动模型的电脑操控资格（与 Turn 起点同一判定函数的只读展示）
  computer_active_model_capability: {
    model: string;
    eligible: boolean;
    protocol?: string;
    error_code?: string;
    reason?: string;
  };
}

// 电脑操控三个严格 setter 的统一返回形状（Task 12）：
// 失败时携带稳定 error_code + 后端真实 settings 快照供前端回滚
export interface SystemSettingsMutationResult {
  ok: boolean;
  error_code?: string;
  msg?: string;
  settings: SystemSettingsSnapshot;
}

// 电脑操控控制 API 统一返回（Task 13a）：快照字段平铺 + ok/msg，
// 失败时 error_code/msg 覆盖快照同名字段（与 gui_bridge._run_computer_mutation 一致），
// 前端用完整快照校准 Store（设计 16.4）
export interface ComputerControlResult extends ComputerRunSnapshot {
  ok: boolean;
  msg?: string;
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
  // 契约扩展位：后端可能补发脱敏摘要/耗时（brief ComputerActionTrace 字段）
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
  // 只带 "/computer-preview/" 开头的 loopback token URL，无截图内容
  preview_url?: string;
  // R5 受控画面本地身份：browser 为 "page-N"，desktop 为 "hwnd:<句柄>"；
  // generation 在受控页/前台窗口/几何变化时递增（仅本地展示用）
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
  source?: string;     // user | builtin（旧版本后端可能不返回）
  // 该技能曾由内置释放、且内置源里仍有同名技能——用于判断能否「恢复内置版本」。
  // 旧后端不返回时按 undefined（不显示恢复入口）处理，避免误导用户
  from_builtin?: boolean;
}

export interface McpInfo {
  name: string;
  enabled: boolean;
  connected: boolean;
  tools_count: number;
}

export interface PluginInfo {
  name: string;
  version: string;
  description: string;
  source: string;      // user | builtin
  status: string;      // loaded | disabled | failed | overridden | pending_enable | pending_disable | untrusted
  enabled: boolean;
  tools: string[];
  error?: string;
}

// 星轨自动化任务（待开发/星轨自动化任务/01-完整设计规格.md 11.3）
export interface AutomationTaskInfo {
  id: string;
  name: string;
  enabled: boolean;
  trigger_type: 'interval' | 'daily' | 'weekly' | 'startup';
  trigger_config: Record<string, any>;
  target_session: string;
  // 任务指定的工作目录：'' = 跟随目标会话归属；'GLOBAL' = 新会话挂自由对话；
  // 其余为懒创建新会话时绑定的项目目录路径
  workdir?: string;
  // 任务指定的执行模型 id：'' = 跟随会话绑定/全局默认
  model_id?: string;
  prompt: string;
  permission_mode: 'confirm' | 'accept_edits' | 'plan' | 'full';
  missed_run_policy: 'skip' | 'run_once';
  notify_on_success: boolean;
  created_at: string;
  updated_at: string;
  last_run_at?: string | null;
  // unix 秒；startup 类型恒 null
  next_run_at?: number | null;
  // 后端生成的面向用户触发器摘要（如「每天 09:30」）
  trigger_summary?: string;
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

export interface SessionInfo {
  name: string;
  // 不可变主键 sessions.id（按回合撤销的稳定归属标识；旧后端缺省为 undefined）
  session_id?: number;
  display_name: string;
  count: number;
  workdir: string;
  is_global?: boolean;
  updated_at?: string;
}

// 会话消息排队：回合运行期间发送的消息（快照展示文本，不含 @路径拼接）
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
  sessions: SessionInfo[];
}

export interface FileNode {
  n: string;
  d: number;
  children?: FileNode[];
}

export interface PlanTaskItem {
  id: string;
  text: string;
  status: 'todo' | 'in_progress' | 'done';
  // 子 Agent 超时接管可见性；plan_updated 快照是可恢复事实源
  execution_mode?: string;
  owner?: string;
  takeover_required?: boolean;
  handoff_reason?: string;
  takeover_sequence?: number;
  handoff_note?: string;
}

// get_changelog() 的单条版本条目
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
  /** CLI 状态栏口径：会话计费累计输入/输出/缓存命中 tokens */
  total_prompt: number;
  total_completion: number;
  total_cached: number;
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
    days: 1 | 7 | 30;
    start_date: string;
    end_date: string;
  };
  summary: TokenStatisticsMetrics;
  models: TokenStatisticsModel[];
  days: TokenStatisticsDay[];
}

// get_initial_state() 的返回结构
export interface InitialState {
  models: ModelInfo[];
  providers?: ProviderInfo[];
  active_model_id: string;
  active_model_name: string;
  // 副模型（用于识图 / 压缩 / 子 Agent 等后台任务）
  secondary_model_id?: string;
  secondary_model_roles?: { vision: boolean; compression: boolean; subagent: boolean };
  permission_mode: PermissionMode;
  // 沙盒开关（true=允许访问工作目录外文件），与 CLI /sandbox 共享配置
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
  // 仍有活跃回合的会话名（窗口重载时恢复运行态）
  active_sessions?: string[];
  // 进行中回合快照（供中途扫码或切换会话时还原正在运行的消息）
  active_turn_snapshots?: Record<string, ActiveTurnSnapshot>;
  active_turn_snapshot?: ActiveTurnSnapshot | null;
  // 各会话排队中的消息（窗口重载时恢复排队条目）
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

// 工具事件 display dict：后端原样透传，字段可缺省
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
  // 按回合撤销：权威回合身份（新协议为 32 位随机 UUID），与 session_id 一起
  // 构成撤销定位键；旧后端/内部回合可能缺省
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
  }>;
}

export interface BridgeEvent {
  type: string;
  // 既有事件信封为 {"type","data"}；电脑操控事件（Task 13a）为平铺信封，
  // data 缺省、run_id/event_seq 直接在顶层，识别规则见 appStore
  data?: any;
  run_id?: string;
  event_seq?: number;
}

// ---------------------------------------------------------------------------
// 按回合撤销 DTO（规格 19；与后端 core/turn_undo 状态值严格对应）
// ---------------------------------------------------------------------------

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
  // 分卷下载（Gitee 单附件 100MB 限制）：非空时 download_url 不再是直链
  part_urls?: string[];
  part_count?: number;
  is_split?: boolean;
  // 官方 SHA256（来自 Release 描述），下载完成后后端强制校验
  sha256?: string;
  msg?: string;
}

// 后端所有方法均返回 Promise<纯 JSON 对象>；大多数写操作的返回值形状
// 为 { ok: boolean, msg?: string, ...其余按方法而定 }，此处以 any 承接，
// 具体字段读取处自行按契约文档取用，避免过度约束导致类型摩擦。
export interface PyWebViewAPI {
  get_initial_state(): Promise<InitialState>;
  get_changelog(): Promise<{ ok: boolean; entries: ChangelogEntry[]; msg?: string }>;
  select_workspace_folder(): Promise<any>;
  // 纯选目录：弹原生文件夹对话框只返回路径，不切换工作区（自动化任务表单用）
  pick_folder_dialog(): Promise<{ ok: boolean; path?: string; msg?: string }>;
  get_workspace_state(workdir: string): Promise<any>;
  get_sessions(): Promise<SessionGroup[]>;
  create_session(title?: string, workdir?: string): Promise<any>;
  create_global_session(title?: string): Promise<any>;
  switch_session(sessionName: string, targetWorkdir?: string): Promise<any>;
  load_older_messages(sessionName: string, beforeId: number): Promise<any>;
  get_session_nav_index(sessionName: string): Promise<any>;
  delete_session(sessionName: string): Promise<any>;
  delete_folder_group(workdir: string): Promise<any>;
  rename_session(oldName: string, newName: string): Promise<any>;

  set_model(modelId: string, sessionName?: string): Promise<any>;
  get_reasoning_snapshot(modelId: string): Promise<any>;
  set_reasoning_choice(modelId: string, choice: ReasoningChoice): Promise<any>;
  // 权限档位是会话级的：传 sessionName 只绑定该会话；不传则改全局默认
  set_permission_mode(mode: PermissionMode, sessionName?: string): Promise<any>;
  set_sandbox_enabled(enabled: boolean): Promise<any>;
  get_system_settings(): Promise<SystemSettingsSnapshot>;
  set_command_shell(shellId: string): Promise<{ ok: boolean; settings: SystemSettingsSnapshot; msg?: string }>;
  set_remote_token_ttl(seconds: number): Promise<{ ok: boolean; settings: SystemSettingsSnapshot; msg?: string }>;
  set_agent_turn_step_limit(value: number): Promise<{ ok: boolean; settings: SystemSettingsSnapshot; msg?: string }>;
  // 电脑操控三个严格 setter（Task 12）：失败返回 error_code + 后端真实快照
  set_browser_control_enabled(value: boolean): Promise<SystemSettingsMutationResult>;
  set_computer_control_enabled(value: boolean): Promise<SystemSettingsMutationResult>;
  set_computer_browser(value: string): Promise<SystemSettingsMutationResult>;
  // 电脑操控运行控制（Task 13a）：快照字段平铺返回（含失败），前端校准 Store
  get_computer_control_status(): Promise<ComputerControlResult>;
  pause_computer_control(runId: string): Promise<ComputerControlResult>;
  resume_computer_control(runId: string): Promise<ComputerControlResult>;
  stop_computer_control(runId: string): Promise<ComputerControlResult>;
  // Provider 强制安全检查确认：官方契约验证完成前固定不可用（Task 11b 条件）
  acknowledge_computer_safety(runId: string, checkIds?: string[]): Promise<ComputerControlResult>;
  get_permission_rules(): Promise<any>;
  add_permission_rule(ruleType: 'command' | 'write' | 'edit', value: string): Promise<any>;
  remove_permission_rule(ruleType: 'command' | 'write' | 'edit', value: string): Promise<any>;
  clear_permission_rules(): Promise<any>;
  respond_permission(requestId: string, allow: boolean, always: boolean): Promise<any>;
  respond_permission_option(requestId: string, optionKey: string): Promise<any>;
  respond_permission_choice(requestId: string, choiceIndex: number, customText?: string): Promise<any>;

  send_user_message(text: string, sessionName?: string, attachments?: string[], workdir?: string, modelId?: string, permissionMode?: string): Promise<any>;
  // 客户端本地提示（notice/error 卡片）经桥广播到全部客户端（桌面 + 手机端）；
  // 各端收到 notice 桥事件后统一回环插入，提示为瞬态、不落历史
  post_notice(message: string, sessionName?: string, kind?: 'notice' | 'error'): Promise<any>;
  stop_current_turn(sessionName?: string): Promise<any>;
  // 会话消息排队：查询 / 取消排队 / 插队发送（停止当前回合优先补发该消息）
  get_message_queue(sessionName?: string): Promise<{ ok: boolean; session_name: string; queue: QueuedMessage[] }>;
  cancel_queued_message(sessionName: string, messageId: string): Promise<any>;
  jump_queue(sessionName: string, messageId: string): Promise<any>;

  get_token_usage(): Promise<TokenUsage>;
  get_token_usage_statistics(days: 1 | 7 | 30): Promise<TokenStatisticsSnapshot | { ok: false; msg: string }>;

  toggle_skill(skillId: string, enabled: boolean): Promise<any>;
  refresh_skills(): Promise<{ ok: boolean; skills?: SkillInfo[]; msg?: string }>;
  // 用内置源覆盖用户改动后的技能副本；成功时回传最新技能列表
  restore_skill(skill_id: string): Promise<{ ok: boolean; msg?: string; skills?: SkillInfo[] }>;
  get_skills(): Promise<SkillInfo[]>;
  // 星轨自动化任务
  get_automation_tasks(): Promise<{ ok: boolean; tasks?: AutomationTaskInfo[]; msg?: string }>;
  save_automation_task(task: Record<string, any>): Promise<{ ok: boolean; id?: string; errors?: string[]; msg?: string }>;
  delete_automation_task(taskId: string): Promise<{ ok: boolean; deleted?: boolean; msg?: string }>;
  set_automation_task_enabled(taskId: string, enabled: boolean): Promise<{ ok: boolean; msg?: string }>;
  run_automation_task_now(taskId: string): Promise<{ ok: boolean; run_id?: string; msg?: string }>;
  get_automation_runs(taskId?: string, limit?: number): Promise<{ ok: boolean; runs?: AutomationRunInfo[]; msg?: string }>;
  clear_automation_runs(taskId: string): Promise<{ ok: boolean; msg?: string }>;
  get_plugins(): Promise<any>;
  toggle_plugin(name: string, enabled: boolean): Promise<any>;
  trust_plugin(name: string): Promise<any>;
  get_mcp_servers(): Promise<McpInfo[]>;
  toggle_mcp_server(name: string, enabled: boolean): Promise<any>;
  add_mcp_server(name: string, command: string, args: string[], env?: Record<string, string>, enabled?: boolean): Promise<any>;
  reconnect_mcp(): Promise<any>;

  export_session(fmt: 'md' | 'html' | 'json'): Promise<any>;
  clear_session_history(sessionName: string): Promise<any>;

  // ------------------------------------------------------------------
  // 按回合撤销（规格 19.1 窄桥接）：会话名由后端解析为 sessions.id
  // ------------------------------------------------------------------
  list_turn_undo_states(sessionName: string): Promise<TurnUndoStatesResponse>;
  get_turn_undo_status(sessionName: string, turnId: string): Promise<TurnUndoStatusResponse>;
  get_turn_undo_file_diff(sessionName: string, turnId: string, fileId: string): Promise<TurnUndoFileDiffResponse>;
  preview_turn_undo(sessionName: string, turnId: string): Promise<TurnUndoPreviewResponse>;
  execute_turn_undo(sessionName: string, turnId: string, previewToken: string): Promise<TurnUndoExecuteResponse>;
  preview_turn_undo_recovery(sessionName: string, operationId: string, action: 'continue_revert' | 'cancel_revert'): Promise<TurnUndoRecoveryPreviewResponse>;
  execute_turn_undo_recovery(sessionName: string, operationId: string, action: 'continue_revert' | 'cancel_revert', previewToken: string): Promise<TurnUndoRecoveryExecuteResponse>;

  read_file_preview(path: string): Promise<any>;
  check_paths_kind(paths: string[]): Promise<Array<{ path: string; kind: 'file' | 'dir' | 'missing' }>>;
  open_path(path: string): Promise<any>;
  open_folder_in_explorer(path: string): Promise<any>;
  // 侧栏排序快照：权威副本在数据目录 ui_layout.json，localStorage 只是缓存
  // （WebView2 的 localStorage 在"拖完立刻重启"时会被 leveldb 丢弃）
  get_ui_layout(): Promise<{ ok: boolean; group_order?: string[]; session_order?: Record<string, string[]>; msg?: string }>;
  save_ui_layout(layout: { group_order: string[]; session_order: Record<string, string[]> }): Promise<{ ok: boolean; msg?: string }>;
  pick_files(): Promise<any>;
  get_files_info(paths: string[]): Promise<any>;   save_clipboard_image(dataUrl: string): Promise<any>;
  get_attachment_thumbnails(paths: string[]): Promise<any>;
   // 手机/平板端文件上传（WS RPC）
   upload_file(name: string, dataB64: string): Promise<any>;// 拖拽 drop 桥接：取回 Python 侧已存储的拖拽文件完整路径
  report_dropped_files(event: any): Promise<{ ok: boolean; paths?: string[] }>;
  // @ 引用自动补全：工作区文件子串匹配
  search_workspace_files(query: string, limit?: number): Promise<any>;
  // @ 提及对话自动补全：历史会话子串匹配（排除当前会话）
  search_sessions(query: string, limit?: number): Promise<{
    ok: boolean;
    sessions?: { name: string; count: number; updated_at: string; workdir: string }[];
    msg?: string;
  }>;
  // 会话多维度全局检索（标题模糊匹配 + 对话内容全文检索）
  search_sessions_full(
    query: string,
    scope?: 'all' | 'title' | 'content',
    workdir?: string,
    limit?: number,
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
  // 自绘标题栏窗口控制（frameless 模式）
  window_minimize(): Promise<{ ok: boolean; error?: string }>;
  window_toggle_maximize(): Promise<{ ok: boolean; maximized?: boolean; error?: string }>;
  // 原生最大化/还原（SC_MAXIMIZE/SC_RESTORE），等价标题栏 HTCAPTION 双击；可缺省
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
  kill_task(taskId: string): Promise<any>;

  run_terminal_command(command: string): Promise<any>;
  kill_terminal_command(): Promise<any>;

  add_provider(name: string, baseUrl: string, apiKey: string, protocol: string): Promise<any>;
  get_model_settings(): Promise<ModelSettingsSnapshot>;
  add_preset_provider(presetId: string, apiKey: string): Promise<any>;
  sync_preset_models(presetId: string): Promise<any>;
  update_provider(providerId: string, name: string, baseUrl: string, apiKey: string, protocol: string): Promise<any>;
  get_provider_api_key(providerId: string): Promise<{ ok: boolean; api_key?: string; msg?: string }>;
  delete_provider(providerId: string): Promise<any>;
  add_model(providerId: string, modelName: string, contextWindow: number, supportsVision: boolean, supportsFunctionCalling?: boolean, computerProtocol?: ComputerProtocolChoice): Promise<any>;
  update_model(providerId: string, modelId: string, modelName: string, contextWindow: number, supportsVision: boolean, supportsFunctionCalling?: boolean, computerProtocol?: ComputerProtocolChoice): Promise<any>;
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
}

declare global {
  interface Window {
    pywebview?: {
      api: PyWebViewAPI;
    };
    onBridgeEvent?: (event: BridgeEvent) => void;
  }
}
