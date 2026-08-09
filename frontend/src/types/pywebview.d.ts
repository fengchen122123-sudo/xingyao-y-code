// Python <-> pywebview 桥接层类型定义
// 本文件按后端桥接 API 契约编写（而非当前 gui_bridge.py 的实现），
// 后端会单独重写以匹配此契约。

export type PermissionMode = 'confirm' | 'auto' | 'plan' | 'full';
// 推理强度档位：default = 模型条目未配置，用请求层内置默认
export type ReasoningEffort = 'default' | 'low' | 'high' | 'max';

export interface ModelInfo {
  id: string;
  name: string;
  provider_name: string;
}

export interface ProviderInfo {
  id: string;
  name: string;
  base_url?: string;
  protocol?: string;
}

export interface SkillInfo {
  id: string;
  short_id?: string;
  name: string;
  description: string;
  enabled: boolean;
}

export interface McpInfo {
  name: string;
  enabled: boolean;
  connected: boolean;
  tools_count: number;
  configured?: boolean;        // false = 预置模板，尚未写入 mcp.json
  required_inputs?: string[];  // 启用前需要用户填写的密钥项
  comment?: string;
}

export interface PluginInfo {
  name: string;
  version: string;
  description: string;
  source: string;      // user | builtin
  status: string;      // loaded | disabled | failed | overridden | pending_enable | pending_disable
  enabled: boolean;
  tools: string[];
  error?: string;
}

export interface SessionInfo {
  name: string;
  display_name: string;
  count: number;
  workdir: string;
  is_global?: boolean;
  updated_at?: string;
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
}

// get_initial_state() 的返回结构
export interface InitialState {
  models: ModelInfo[];
  active_model_id: string;
  active_model_name: string;
  permission_mode: PermissionMode;
  reasoning_effort?: ReasoningEffort;
  sessions: SessionGroup[];
  skills: SkillInfo[];
  mcps: McpInfo[];
  workdir: string;
  file_tree?: FileNode[];
  token_usage: TokenUsage;
  version: string;
}

// window.onBridgeEvent 收到的事件信封；具体 data 形状随 type 变化，
// 回合/工具时间线事件的 payload 形状见下方接口（对应 gui_bridge.py
// _consume_turn_events 的发射字段），其余事件详见 appStore.ts 的
// handleBridgeEvent 分支注释。

// 工具事件 display dict：后端原样透传，字段可缺省
export interface ToolDisplayPayload {
  category?: string;
  action?: string;
  target?: string;
  command?: string;
  metrics?: Record<string, number>;
  error_summary?: string;
  is_skill?: boolean;
}

export interface TurnStartEventData {
  user_text: string;
  display_text?: string;
  session_name?: string;
  internal?: string;
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
  data: any;
}

// 后端所有方法均返回 Promise<纯 JSON 对象>；大多数写操作的返回值形状
// 为 { ok: boolean, msg?: string, ...其余按方法而定 }，此处以 any 承接，
// 具体字段读取处自行按契约文档取用，避免过度约束导致类型摩擦。
export interface PyWebViewAPI {
  get_initial_state(): Promise<InitialState>;
  select_workspace_folder(): Promise<any>;
  get_sessions(): Promise<SessionGroup[]>;
  create_session(title?: string, workdir?: string): Promise<any>;
  create_global_session(title?: string): Promise<any>;
  switch_session(sessionName: string, targetWorkdir?: string): Promise<any>;
  delete_session(sessionName: string): Promise<any>;
  delete_folder_group(workdir: string): Promise<any>;
  rename_session(oldName: string, newName: string): Promise<any>;

  set_model(modelId: string): Promise<any>;
  set_reasoning_effort(level: ReasoningEffort): Promise<any>;
  set_permission_mode(mode: PermissionMode): Promise<any>;
  respond_permission(requestId: string, allow: boolean, always: boolean): Promise<any>;

  send_user_message(text: string, sessionName?: string, attachments?: string[], workdir?: string): Promise<any>;
  stop_current_turn(): Promise<any>;

  get_token_usage(): Promise<TokenUsage>;

  toggle_skill(skillId: string, enabled: boolean): Promise<any>;
  get_plugins(): Promise<any>;
  toggle_plugin(name: string, enabled: boolean): Promise<any>;
  get_mcp_servers(): Promise<McpInfo[]>;
  toggle_mcp_server(name: string, enabled: boolean): Promise<any>;
  enable_mcp_server(name: string, envValues?: Record<string, string>): Promise<any>;
  reconnect_mcp(): Promise<any>;

  export_session(fmt: 'md' | 'html' | 'json'): Promise<any>;
  clear_session_history(sessionName: string): Promise<any>;
  undo_last_edit(): Promise<any>;

  read_file_preview(path: string): Promise<any>;
  open_path(path: string): Promise<any>;
  pick_files(): Promise<any>;
  get_files_info(paths: string[]): Promise<any>;
  save_clipboard_image(dataUrl: string): Promise<any>;
  set_titlebar_theme(dark: boolean): Promise<any>;
  set_gui_theme(theme_light: boolean): Promise<any>;

  get_background_tasks(): Promise<BackgroundTask[]>;
  kill_task(taskId: string): Promise<any>;

  run_terminal_command(command: string): Promise<any>;
  kill_terminal_command(): Promise<any>;

  add_provider(name: string, baseUrl: string, apiKey: string, protocol: string): Promise<any>;
  add_model(providerId: string, modelName: string, contextWindow: number): Promise<any>;
  delete_model(modelId: string): Promise<any>;
  test_model(modelId: string): Promise<any>;
}

declare global {
  interface Window {
    pywebview?: {
      api: PyWebViewAPI;
    };
    onBridgeEvent?: (event: BridgeEvent) => void;
  }
}
