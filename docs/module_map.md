# 模块地图（core）

> 本文件由 `scripts/architecture_report.py map` 自动生成，请勿手工编辑。
> 重新生成：`python scripts/architecture_report.py map`

- 模块数：170
- 行数合计：70244

| 模块 | 行数 | 摘要（docstring 首行） |
| --- | ---: | --- |
| `core/__init__.py` | 0 | — |
| `core/agent_service.py` | 1091 | 无界面（headless）的一轮对话准备逻辑。 |
| `core/ai_output_paths.py` | 138 | AI 生成内容目录：AI 产物（生图 / 下载图 / 临时脚本）的统一落点。 |
| `core/app_context.py` | 294 | 运行时上下文中间层。 |
| `core/ask_user.py` | 122 | Interactive user question adapter backed by the existing CLI prompt. |
| `core/automation/__init__.py` | 17 | 星轨自动化任务：GUI 常驻调度引擎的领域包。 |
| `core/automation/engine.py` | 119 | 星轨自动化任务：GUI 常驻调度引擎。 |
| `core/automation/models.py` | 127 | 星轨自动化任务：常量、任务字段校验与 UI 摘要。 |
| `core/automation/scheduling.py` | 104 | 触发器校验与下一次触发时间计算。 |
| `core/automation/service.py` | 86 | 星轨自动化任务：core 层服务句柄（Agent 工具访问自动化能力的唯一入口）。 |
| `core/automation/storage.py` | 667 | 星轨自动化任务：SQLite 存储层。 |
| `core/cache_keepalive.py` | 333 | 可选的 provider prompt-cache 保活服务；默认 120 秒间隔（DeepSeek/Kimi 路由生效）。 |
| `core/command_policy.py` | 511 | Pure policy helpers for session-scoped command authorization. |
| `core/command_shell.py` | 324 | Default command-interpreter discovery, persistence, and invocation. |
| `core/computer/__init__.py` | 39 | core.computer 包公共导出；不创建任何全局副作用。 |
| `core/computer/actions.py` | 500 | 统一电脑动作的白名单 schema、严格解析与 Provider 投影。 |
| `core/computer/browser_backend.py` | 1989 | Edge/Chrome 专用 Playwright 浏览器 backend（设计 12、21、22）。 |
| `core/computer/browser_worker.py` | 147 | 受管浏览器执行进程入口（R2 第二段）：``python -m core.computer.browser_worker``。 |
| `core/computer/browser_worker_protocol.py` | 165 | 受管浏览器执行进程的 IPC 协议（R2 第二段）。 |
| `core/computer/capability.py` | 144 | Turn 起点电脑操控能力决策（设计 9.3）。 |
| `core/computer/cli.py` | 312 | CLI 电脑操控运行控制与脱敏显示（设计第 17 节）。 |
| `core/computer/controller.py` | 1133 | 单租约电脑操控 Controller：显式状态机、排他租约、暂停恢复与固定停止顺序。 |
| `core/computer/hotkey.py` | 253 | 全局紧急停止热键 Ctrl+Alt+Esc（设计 3.3、15.1、17.3）。 |
| `core/computer/models.py` | 260 | 电脑操控领域的不可变值对象、枚举、错误类型与 backend 协议。 |
| `core/computer/platforms/__init__.py` | 22 | 固定内核的平台选择入口；不提供插件注册或跨平台降级。 |
| `core/computer/platforms/base.py` | 75 | 平台接入契约；不加载任何本机输入或图形依赖。 |
| `core/computer/platforms/linux.py` | 8 | Linux 接入位置；X11/Wayland 必须分别实现与验收，不默认互换。 |
| `core/computer/platforms/macos.py` | 8 | macOS 接入位置；授权、坐标、输入与热键未实现前保持不可用。 |
| `core/computer/platforms/windows.py` | 66 | Windows 接入层；执行依赖仅在相应方法调用时导入。 |
| `core/computer/platforms/windows_elements.py` | 117 | Windows UIA 元素定位器（2026-09-06 阶段二，a11y-first 仿做）。 |
| `core/computer/platforms/windows_elements_uia.py` | 212 | RealUIAHost：真实 COM 线程 UIA 元素收集与语义操作（2026-09-06 阶段二）。 |
| `core/computer/platforms/windows_surfaces.py` | 592 | Windows 语义定位实现（R10）：窗口枚举/激活 + UIA 托盘图标候选。 |
| `core/computer/preview_server.py` | 187 | loopback 令牌化截图预览服务（设计 16.3）。 |
| `core/computer/provider_generic.py` | 307 | generic 协议的 Provider 投影：固定工具 schema、动态提示、批处理约束与观察消息。 |
| `core/computer/provider_openai.py` | 734 | OpenAI Responses 原生 Computer Use 协议（设计 11，Task 11a）。 |
| `core/computer/runtime.py` | 405 | 电脑操控进程级运行时：轻量启动容器与确定性退出（设计 23.2，Task 16）。 |
| `core/computer/settings.py` | 220 | 电脑操控设置：严格配置读取、显式浏览器探测、专用 profile 路径与内存 generation。 |
| `core/computer/som_annotator.py` | 138 | SoM 编号标注截图（通用化重构 Task B；对标 microsoft/UFO、Agent-S、SoM）。 |
| `core/computer/surfaces.py` | 105 | 平台无关的软件/控件语义定位契约（R10）。 |
| `core/computer/tool_adapter.py` | 109 | computer_action 的固定 Harness 适配器（设计 19.2）。 |
| `core/computer/win32_adapter.py` | 825 | Win32 SendInput 输入适配器（设计 13.2-13.4、21）。 |
| `core/computer/windows_backend.py` | 839 | Windows 桌面 backend：截图、坐标映射与 SendInput 调度（设计 13、21、22）。 |
| `core/context_breakdown.py` | 108 | 输入框上下文用量面板：上下文来源字符占比快照（纯旁路统计）。 |
| `core/context_builder.py` | 166 | Compose stable instructions and per-turn runtime context. |
| `core/context_injection.py` | 370 | 上下文注入描述符与注册表（面向 ZCode 能力借鉴 · 规格 02 · 阶段 B1-B6）。 |
| `core/credentials.py` | 114 | Windows DPAPI helpers for user-bound credential encryption at rest. |
| `core/diagram_export.py` | 311 | Mermaid 图表本地导出（CLI 端）。 |
| `core/diff_viewer.py` | 82 | — |
| `core/disclaimer_consent.py` | 155 | 免责条款同意记录持久化。 |
| `core/doctor.py` | 402 | core/doctor.py — `/doctor` 聚合自检（CLI 部分）。 |
| `core/encrypted_secret_store.py` | 98 | 带密钥的对称加解密模块。 |
| `core/frameless.py` | 407 | 无边框（frameless）窗口的原生窗口行为支持。 |
| `core/git/__init__.py` | 1 | 星瑶 Git 工作台领域层（规格见 待开发/星瑶Git管理/01-完整设计规格.md）。 |
| `core/git/bootstrap.py` | 38 | 进程级 Git 服务与监控单例（GUI 与 CLI 共用，规格 §6 架构图）。 |
| `core/git/conflict.py` | 106 | 三方冲突读取与解决（规格 §5.10 冲突界面后端）。 |
| `core/git/coordinator.py` | 162 | 双级锁、快照版本与操作兼容矩阵（规格 §9）。 |
| `core/git/models.py` | 195 | Git 工作台不可变值对象、错误码与操作状态机。 |
| `core/git/monitor.py` | 284 | GitStateMonitor：外部 Git 状态同步（规格 §10）。 |
| `core/git/mutation.py` | 491 | 白名单写操作层（规格 §5.4–§5.14）。 |
| `core/git/patch.py` | 151 | Hunk/行级 Patch 的生成、预检与应用（规格 §5.3/§5.13）。 |
| `core/git/process_registry.py` | 98 | Workspace Process Registry（规格 §9.2）。 |
| `core/git/query.py` | 262 | 只读查询层（规格 §5.2/§5.9）：porcelain v2 解析、操作状态检测与各只读查询。 |
| `core/git/query_refs.py` | 520 | 只读查询（规格 §5.5/§5.7/§5.9/§5.12）：分支、历史、Diff、Blame、Reflog、Tag、Remote、Stash。 |
| `core/git/rebase_plan.py` | 143 | Interactive Rebase 计划读取与改写（规格 §5.10/§7 阶段任务）。 |
| `core/git/repository.py` | 133 | 仓库发现与能力检测（规格 §5.1/§7.1）。 |
| `core/git/risk.py` | 204 | 风险分级与两段式确认（规格 §13）。 |
| `core/git/runner.py` | 392 | 唯一 Git 进程执行器（规格 §8）。 |
| `core/git/service.py` | 775 | GitApplicationService：GUI/CLI 唯一应用服务门面（规格 §6/§9.3/§13）。 |
| `core/git/trust.py` | 162 | 仓库信任策略（规格 §4.5）。 |
| `core/git_cli.py` | 716 | CLI Git 工作台（规格 §12）。 |
| `core/gui_lifecycle.py` | 304 | Windows GUI window/tray lifecycle with deterministic application shutdown. |
| `core/gui_path_policy.py` | 113 | GUI 预览 / 打开路径的统一访问策略（read_file_preview / open_path 共用）。 |
| `core/gui_runtime_check.py` | 151 | GUI 运行环境自检：WebView2 Runtime 探测 + 不依赖 WebView 的原生告警。 |
| `core/gui_theme.py` | 854 | GUI 主题 Schema v1、持久化及活动主题选择。 |
| `core/harness/__init__.py` | 6 | 固定 Harness 内核。 |
| `core/harness/journal.py` | 243 | Durable harness lifecycle journal and process-owner leases. |
| `core/harness/models.py` | 168 | Immutable models and explicit state transitions for the harness kernel. |
| `core/harness/progress_guard.py` | 79 | 确定性 Turn 进展观察：只比较状态哈希与修改类提交标记，不读模型自然语言。 |
| `core/harness/request_envelope.py` | 275 | Immutable fingerprints captured from the exact provider request. |
| `core/harness/responses_adapter.py` | 269 | Adapters for converting OpenAI Responses protocol stream events into Chat completion chunks. |
| `core/harness/runtime.py` | 351 | In-process and durable lifecycle coordination for one model turn. |
| `core/harness/stream_assembler.py` | 277 | Provider stream chunk assembly for one Harness Step. |
| `core/harness/tool_adapters.py` | 880 | Fixed adapters for the model-visible tool execution boundary. |
| `core/harness/tool_contracts.py` | 54 | Strict, source-aware contracts for built-in, plugin and MCP tools. |
| `core/harness/tool_pipeline.py` | 1197 | Deterministic single-tool execution pipeline. |
| `core/harness/tool_scheduler.py` | 218 | 拓扑感知的工具批次规划（「面向 ZCode 能力借鉴」工作包 A5，规格 03 §2.2）。 |
| `core/harness/turn_limits.py` | 39 | Strict per-Turn Provider Step limit configuration. |
| `core/history_renderer.py` | 160 | — |
| `core/hooks/__init__.py` | 78 | Hooks 体系公开 API（面向 ZCode 能力借鉴 · 规格 01 · 阶段 C1-C5）。 |
| `core/hooks/executor.py` | 189 | 单 hook 执行器：spawn 系统 shell + stdin/stdout 编解码（面向 ZCode 能力借鉴 · 规格 01 · C4）。 |
| `core/hooks/lifecycle.py` | 294 | C8/C9 接入辅助：SessionStart 注入、Stop 续跑上限与 hook 输入构造。 |
| `core/hooks/registry.py` | 265 | Hooks 配置加载与 matcher 匹配（面向 ZCode 能力借鉴 · 规格 01 · C2）。 |
| `core/hooks/runner.py` | 160 | HookRunner：匹配 → 信任校验 → 串行执行 → 合并（面向 ZCode 能力借鉴 · 规格 01 · C5）。 |
| `core/hooks/trust.py` | 419 | Hooks 信任机制：指纹计算 + 状态机 + 存储（面向 ZCode 能力借鉴 · 规格 01 · C3）。 |
| `core/hooks/types.py` | 206 | Hooks 契约层：事件、注册项与 stdin/stdout 数据契约（面向 ZCode 能力借鉴 · 规格 01 · C1）。 |
| `core/hunyuan_image.py` | 191 | 腾讯混元生图核心实现（CloudBase 网关接口）。 |
| `core/input_parser.py` | 399 | — |
| `core/lan_server.py` | 806 | 局域网手机远程控制的 HTTP + WebSocket 单端口服务。 |
| `core/llm.py` | 2956 | — |
| `core/llm_diagnostics.py` | 720 | Token usage accounting and prompt cache diagnostics. |
| `core/llm_dialects.py` | 1481 | Provider dialects, model features, prompt ledger, cache affinity, and payload shaping. |
| `core/llm_error_mapper.py` | 439 | 大模型上游报错中文映射与排查建议引擎。 |
| `core/llm_execution.py` | 1063 | Tool parsing, JSON repair, pipeline context preparation, and vision fallback. |
| `core/llm_native_transport.py` | 1348 | Y-code 原生 HTTPX 同步流式传输核与解析引擎。 |
| `core/llm_policy.py` | 173 | 声明式模型方言与提供商能力策略体系。 |
| `core/llm_transport.py` | 288 | 自研 HTTPX 传输层：请求入口、在飞请求硬中断注册表与重试退避常量。 |
| `core/lsp.py` | 252 | 轻量级代码符号定位模块（LSP-lite）。 |
| `core/managed_policy.py` | 227 | 企业/团队分发场景的管理员强制基线（借鉴 Codex ``requirements.toml``）。 |
| `core/mcp_client.py` | 788 | — |
| `core/mcp_selector.py` | 837 | /mcp 指令触发的全屏 CLI 配置器。 |
| `core/media_migration.py` | 90 | 历史消息媒体清单一次性迁移（幂等，DB 层）。 |
| `core/media_refs.py` | 169 | 媒体引用解析器：从助手消息正文提取图片/视频语法并组装 media_v1 清单。 |
| `core/media_server.py` | 256 | 受控媒体 HTTP 服务：向 GUI 前端提供媒体仓库 Blob 的只读访问。 |
| `core/media_store.py` | 230 | 永久媒体仓库：SHA-256 内容寻址的本地 Blob 存储。 |
| `core/memory.py` | 2883 | — |
| `core/memory_compressor.py` | 783 | — |
| `core/memory_kairos.py` | 388 | — |
| `core/model_catalog.py` | 124 | CLI 与 GUI 共用的模型协议和预置厂商目录。 |
| `core/model_manager.py` | 835 | 多厂商模型管理器：读写 config.json，提供 get/set 激活模型的接口。 |
| `core/model_selector.py` | 1492 | /models 指令触发的全屏 CLI 选择器。 |
| `core/model_spec_table.py` | 89 | 已知模型的官方规格表（上下文窗口 / 最大输出）。 |
| `core/paste_placeholders.py` | 62 | 粘贴大段文本时折叠为占位符（类 Kimi Code CLI 体验）。 |
| `core/permission_broker.py` | 263 | Serialize permission requests from root and child agent threads. |
| `core/permission_grants.py` | 207 | In-memory permission grants isolated by stable task/session identifiers. |
| `core/permission_policy.py` | 89 | — |
| `core/permission_store.py` | 290 | 项目级权限白名单持久化存储。 |
| `core/permission_terminal.py` | 805 | Interactive tool permission authorization and terminal prompts. |
| `core/persona_settings.py` | 198 | 智能体人设设置持久化。 |
| `core/plan.py` | 1152 | Persisted dependency-aware execution plans for one active session. |
| `core/plugin_loader.py` | 560 | 通用插件加载框架。 |
| `core/process_io.py` | 54 | Deterministic decoding for subprocess output across terminal platforms. |
| `core/reasoning_capabilities.py` | 296 | 按模型名维护思考模式能力，不把供应商当作能力来源。 |
| `core/reasoning_stream.py` | 75 | — |
| `core/runtime_warnings.py` | 40 | Route host-process warnings to a diagnostic file instead of the CLI. |
| `core/session_selector.py` | 188 | — |
| `core/share.py` | 311 | 会话分享模块。 |
| `core/skill_selector.py` | 172 | — |
| `core/skills.py` | 977 | — |
| `core/subagent_registry.py` | 318 | 自定义子 Agent 注册中心。 |
| `core/task_manager.py` | 960 | — |
| `core/task_scheduler.py` | 1061 | Dependency-aware execution scheduler for persisted plan items. |
| `core/terminal_markdown.py` | 49 | — |
| `core/themes.py` | 93 | — |
| `core/thinking_stream.py` | 59 | Stable display buffering for streamed model reasoning. |
| `core/tool_cache.py` | 304 | Lightweight in-process LRU caches for read-only tool results. |
| `core/tool_presenter.py` | 263 | — |
| `core/tool_progress.py` | 261 | Pure state machine for compact CLI tool-progress output. |
| `core/tool_protocol.py` | 61 | Contracts shared by tool registration, execution and rendering. |
| `core/tool_registry.py` | 1306 | Capability registry for the built-in tools. |
| `core/tool_result_maintenance.py` | 129 | 工具结果的分层本地维护（Snip / Elide），服务于 DeepSeek 前缀缓存命中率。 |
| `core/tools.py` | 4774 | — |
| `core/turn_controller.py` | 33 | Turn lifecycle state used to cancel and roll back one model turn. |
| `core/turn_events.py` | 50 | Structured events emitted while an agent turn is running. |
| `core/turn_renderer.py` | 352 | Deterministic, terminal-only rendering for ordered turn events. |
| `core/turn_undo/__init__.py` | 28 | 星瑶按回合撤销包：持久化、可审计、可崩溃恢复的按回合文件撤销。 |
| `core/turn_undo/coordinator.py` | 177 | 星瑶按回合撤销：规范化工作区互斥协调器。 |
| `core/turn_undo/models.py` | 496 | 星瑶按回合撤销：严格枚举、值对象与结构化错误。 |
| `core/turn_undo/prompt_context.py` | 82 | 星瑶按回合撤销：模型动态纠正上下文（规格 15.3）。 |
| `core/turn_undo/recorder.py` | 962 | 星瑶按回合撤销：受控文件修改前后的记录与验证（FileMutationRecorder）。 |
| `core/turn_undo/recovery.py` | 749 | 星瑶按回合撤销：崩溃中断检测、路径冻结与显式恢复。 |
| `core/turn_undo/service.py` | 517 | 星瑶按回合撤销：GUI、CLI、模型工具共用的编排入口（TurnUndoService）。 |
| `core/turn_undo/storage.py` | 1728 | 星瑶按回合撤销：SQLite 元数据、内容寻址快照仓库与 GC。 |
| `core/turn_undo/transaction.py` | 829 | 星瑶按回合撤销：预检、一次性确认令牌与多文件恢复事务。 |
| `core/ui_layout.py` | 132 | 界面布局偏好持久化（侧栏「项目分组 / 分组内会话」的自定义排序）。 |
| `core/ui_markers.py` | 78 | 统一的终端标记与 ANSI 颜色常量（所有渲染路径共用）。 |
| `core/updater.py` | 895 | Y-CODE 自动与手动更新管理模块： |
| `core/version.py` | 3 | ycode 版本号单一来源。 |
| `core/webview_no_cache.py` | 69 | 修复 pywebview 内置静态服务器的缓存控制头从未真正发出的问题。 |
| `core/workspace_context.py` | 46 | 线程本地工作目录上下文，替代进程级 os.chdir/Path.cwd 以支持多会话并行。 |
| `core/worktree.py` | 195 | Git worktree 管理模块。 |
