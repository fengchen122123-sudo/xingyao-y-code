# Y-CODE CLI Development Guidelines

This file outlines the standard commands, code styles, and guidelines for developing and maintaining the Y-CODE CLI project.

## 🌿 Git Workflow

- 日常代码拉取与提交只在 `dev` 分支进行。

- 合并到其他分支（如 `master`）必须由用户明确要求后才执行，不要主动合并。

## 🛠️ Build & Run Commands

### Run Application

```bash
# Run using default python (venv should be activated)
python main.py

# Run explicitly with venv python on Windows
venv\Scripts\python main.py
```

### Validate Syntax (Compile Check)

```bash
venv\Scripts\python -m py_compile main.py core/llm.py core/memory.py core/memory_compressor.py core/themes.py
```

---

## 🔌 Plugin Architecture

ycode 支持进程内插件扩展工具能力，无需修改核心调度逻辑。

### Plugin Layout

```
plugins/<plugin_name>/          # 内置插件（随仓库发布）
  plugin.json                   # {"name","version","description","entry","enabled"}
  plugin.py                     # 必须暴露 register(registry) 函数
~/.ycode/plugins/<plugin_name>/ # 用户级插件（同名覆盖内置）
```

- 加载器：`core/plugin_loader.py`，由 `core/tool_registry.py:build_default_registry()` 末尾自动调用。

- 插件扫描结果（含禁用/失败/被覆盖/未信任）由 `plugin_loader.get_plugin_infos()` 提供，CLI 内用 `/plugins` 命令查看。

- 单插件异常只记录日志并跳过，绝不影响 ycode 启动。

- 插件内兄弟模块用 `from core.plugin_loader import import_sibling; m = import_sibling(__file__, "module")` 加载。

- 插件工具通过 `ToolSpec` 元数据自动获得权限拦截、schema 参数过滤与结果截断。

- 信任机制：加载前按插件目录内容指纹判定。内置 `plugins/` 记录指纹并自动信任；
  用户级 `~/.ycode/plugins/` 首次见到或指纹变化必须确认（交互终端 input 确认，
  入口层可用 `set_trust_prompt` 注册回调；headless 默认跳过并 warning）。
  已确认指纹持久化到 `~/.ycode/plugin_trust.json`（YCODE_DATA_DIR 优先）。

## 🔌 MCP 安全基线

- `core/mcp_client.py:_connect_server` 连接前强制复核 `_validate_mcp_launch`（command 白名单 +
  env 危险键拒绝），手工编辑 mcp.json 绕过 `add_server` 也生效；env 合并时剔除对
  PATH/COMSPEC/SYSTEMROOT 等继承键的覆盖。

- mcp.json 的 env 值写盘前用 `core/credentials.py` 加密（dpapi:/keyring: 前缀），读取连接时解密；
  无前缀旧值按明文兼容，连接成功后一次性回写加密版。postgres 等含密码连接串走 env 而非 args。

- `_connect_server` 幂等：同名重连先关旧连接，schema 按 server 名替换不追加；
  取消竞态会清理已入 `self.servers` 的孤儿连接；`call_tool_sync` 超时返回 🚨 友好文本并取消底层调用。

- mcp.json 的 env 值加密失败按明文落盘（可用性优先的显式降级），`list_servers`
  会以 `env_plaintext` 字段标出未加密的键名；server 名禁止含 `__`（与
  `f"{server}__{tool}"` 工具名前缀分隔符冲突，否则重连按前缀替换会剥掉别家工具）。

## 📡 LAN/MCP 远程服务安全基线

- `LanServer.FORBIDDEN_METHODS` 是 fail-closed 黑名单：新增桌面桥方法若属
  「写持久配置→让桌面长期执行」（hook/插件信任/MCP/权限白名单/自动化任务）、
  「明文凭据读取」或「桌面专属生命周期」，必须同步入名单；判定标准见名单内
  注释，frontend-mobile 零引用是入名单的必要条件。
- `LanServer.broadcast` 只向每客户端有界出站队列 put_nowait（满则丢弃并计数），
  网络 IO 收敛到每客户端发送线程——事件生产线程（回合 worker）绝不因单个
  失联手机阻塞在 send 上；测试直连桩无队列时回落同步发送。

---

## 🧭 固定 Harness 内核

- `core/harness/` 是非插件化的主代理内核：`models.py` 定义严格状态与事件值对象，
  `journal.py` 管理 owner lease、Turn/Step 生命周期与事件序列，`runtime.py` 协调一次
  Turn 及其 Step，`request_envelope.py` 只观察最终 Provider 请求并生成无正文指纹，
  `stream_assembler.py` 负责流与 reasoning/tool call 组装，`tool_contracts.py`、
  `tool_adapters.py`、`tool_pipeline.py` 负责严格工具解析、固定适配与有序执行。
  `core/llm.py` 保留 GUI/CLI 和外部调用方兼容入口；插件仍只通过现有 `ToolRegistry`
  扩展工具集合，不得动态替换 Harness 阶段。

- `messages` 表是会话内容的唯一事实源。Harness Journal 只能记录状态、ID、序号、
  时间、耗时与哈希；严禁写入用户/助手正文、reasoning、工具参数、工具结果、凭据或
  Kimi 缓存亲和值原文。

- 一个 Step 对应一次真实模型请求；Provider 内部重试必须留在同一个 Step，工具结果后
  再次请求模型才创建下一个 Step。非法状态转换与状态迁移的 CAS 写库必须显式失败；
  journal 的事件 sink（STEP_CREATED 等辅助事件）失败只告警不阻断回合——续跑对
  started-not-finished 一律 BLOCK，遥测缺失不会伪装成已提交。

- 崩溃恢复只把未完成 Turn/Step 标记为 `interrupted`，绝不自动重放模型请求、命令、
  文件写入或其他副作用工具。

- owner 心跳双通道：journal 操作搭车续租之外，`TurnRuntime` 为持久回合起
  `harness-heartbeat-*` 守护线程按 2s 节拍续租（turn 终态即退）——长工具执行
  >10s 期间不再因零心跳被另一进程的 `interrupt_stale_harness_runs` 误标
  interrupted（GUI 与 CLI 共享 ycode.db 的场景）。

- 修改请求构造、消息规范化、system prompt、tools、metadata、`extra_body` 或缓存亲和
  逻辑前，必须先运行 Provider 请求 byte-golden 与 prompt byte-stability 回归；黄金字节
  不得为迁就实现改写。

- DeepSeek 官方主会话继续使用不可变 Prompt Ledger，保持 CAS、追加、切代、tools 顺序
  以及 thinking tool call 的字符串 `reasoning_content`（允许空串）语义。Kimi 不新增
  Prompt Ledger，保持现有 tools 顺序、OpenAI reasoning 占位、Anthropic thinking blocks、
  metadata/cache-control 与缓存亲和材料不变。

---

## 🗄️ 上下文缓存架构（DeepSeek 前缀缓存）

- `core/tool_result_maintenance.py`：工具结果 Snip/Elide 纯函数层，marker 幂等，
  每条消息字节最多改写两次（snip 一次 + elide 升级一次），改写后永久冻结。
  注意 tier>=1 的规则精简 reducer（`core/memory.py:maintain_tool_results` 前置）
  是另一类独立改写：精简 → snip → elide 三段各自最多一次、方向单调，
  snip/elide 的两次上限在自身类别内成立。

- `core/memory.py:maintain_tool_results`：DB 层维护入口，尾部 8000 估算 token 保护区不改写；
  `_fold_stale_media_locked` 在含媒体新消息入库的同一事务内折叠旧媒体。

- `core/agent_service.py`：四级水位线（0.60 snip / 0.80 elide / 0.95 LLM 摘要），
  触发判断优先用上一轮真实 API 用量基线，本地估算只作兜底与改写后的复算。

- `core/agent_service.py:build_stable_system_message`：稳定前缀 = 基础提示词 + 决策策略 +
  通用编码规范 + 长期记忆 + 技能目录 + 子代理清单（跨轮不变内容全并入此处，~6.5K tokens，
  撑满 DeepSeek 2048 整数倍缓存块）。铁律：跨轮不变的静态内容必须进稳定前缀，
  不能放动态运行上下文块（`build_dynamic_prompt` 只承载模型/权限/目录/计划等可变字段），
  否则前缀漂移导致缓存整体失效。

- `core/harness/tool_pipeline.py:RepeatCallGuard` 抑制同签名成功 ≥2 次的重复工具调用；
  计划状态机工具（plan_write/plan_update/plan_read/plan_retry）豁免
  （`_REPEAT_GUARD_EXEMPT_TOOLS`），否则会卡死 plan_retry 的“提示→强制重试”两段式流程。
  计划门禁另一死锁防护：回合被标记为计划回合后若计划被清空（snapshot 为空），
  执行工具门禁必须解除，不能继续要求 plan_item_id。
  DeepSeek thinking 路由为带 tool_calls 的 assistant 回补空 `reasoning_content` key。

- `core/task_scheduler.py` 接管队列约定：FIFO 只决定优先级，不保证队首一定可执行。
  `claim_takeover_for_turn` / `promote_next_takeover` 在队首 files 与在途租约冲突时必须
  顺延到下一个可认领项，绝不让单个占位项堵死整条队列（否则 queued 项会永久停在原状态）。
  完成/失败回写只认 in_progress；被拒时经 `_report_writeback_rejected` 写日志并发
  `plan_writeback_rejected` 事件，由 CLI/GUI 提示用户，不再静默 return False。
  铁律：该诊断事件名不得使用 `plan_item_` 前缀——CLI 把该前缀一律当"计划项状态变化"
  渲染，复用会把计划项显示成它原有的 pending 状态，反而误导用户。

- `core/llm.py:_stream_chunks_with_idle_notice`：流式 chunk 由守护线程 pump、主线程带超时读队列，
  流中段 stalled 超阈值按间隔上报等待状态（此前首 chunk 后最长静默一整个请求超时）。

- `core/tools.py` 子代理并发闸门：超时/取消是协作式（线程可能仍卡在 LLM 请求里），
  但 `_release_slot` 幂等立即归还名额，排队超过任务自身超时预算直接判 timed_out，
  避免泄漏线程占满 5 个名额导致前台回合无限阻塞。

- 权限档位传播铁律：会话级档位由 `core/app_context.py` 的线程级绑定承载
  （`set_thread_permission_mode` / `permission_mode_scope`），鉴权路径
  `get_permission_mode()` 线程级优先、回退全局。任何新建执行线程都必须显式
  传播该档位，否则回退全局默认，出现"会话开了 full 仍弹审批"：
  GUI 回合在 `gui_bridge._run_turn_worker` 绑定；子代理在 `core/tools.py`
  的 `SubagentTask.permission_mode` 创建时快照、`_run_task` 内 scope 恢复；
  并行工具线程在 `core/harness/tool_pipeline._submit_daemon_call` 传播。
  `set_permission_mode()` 对已绑定线程同步更新绑定，避免语义割裂。

- `core/cache_keepalive.py`：前缀缓存保活默认 120 秒（0 关闭，最小 60），仅 DeepSeek/Kimi 路由生效。

- 插入式压缩（`memory_compression.insert_mode` 默认开）：复用 `get_session_snapshot()`
  的会话最后主请求字节（ledger 消息 + tools + 亲和参数原样）尾部追加压缩指令，单请求
  完成摘要后走既有 CAS 落库。快照缺失（保活关闭/路由不支持/主请求在飞）、model/key/base
  三元组不符或窗口预算不足一律静默回退分段路径，绝不硬失败——因此 insert_mode 依赖
  保活开关：`prompt_cache_keepalive_seconds=0` 时恒走分段路径。

- `session_summary_generations` 归档表（memory.py）+ `recall_history` 只读工具：每次压缩
  成功追加一代（摘要/topics/until_msg_id），`get_history_for_llm` 在 history[0] 摘要块尾部
  注入 ≤10 代回溯索引。会话改名必须随迁、undo/清空/删会话/删工作区必须同事务清理该表，
  否则回溯索引静默丢失并留孤儿行。

- `core/idle_maintenance.py`：用户空闲 N 秒（`memory_compression.idle_maintenance_seconds`
  默认 240，0 关闭）后跑本地水位维护与压缩。GUI/CLI 在回合开始/结束接线
  `on_turn_started`/`on_turn_finished`；回合开始撤销未触发任务并协作中断在跑压缩——
  cancel_event 注册期由 `done_event` 延长到压缩真正结束（`_trigger_background_precompress`
  发射后即返回，worker 必须等 done 才弹事件），注册表复用不覆盖。

- 智谱 GLM 缓存路线：官方隐式自动缓存（无亲和参数、无显式标记、命中五折、TTL 不透明），
  客户端走「稳定字节 + 保活」。`core/llm.py:_supports_cache_keepalive` 对 DeepSeek/
  GLM/Kimi/Qwen 四族按模型名判定、任意渠道路由放行（第三方中转同样保活），无名模型
  回退域名判定（官方 open.bigmodel.cn 放行）；上有车道证据门兜底——某车道实测命中率
  < 0.5 且样本 >= 10 轮即停该车道保活（小时级自愈），第三方中转若确实不缓存会自动停。
  特例：GLM 强制思考模型（glm-5.3/5.3-flash，能力表无关闭档，`thinking.type=disabled`
  直接报错）的保活快照必须保留 thinking.enabled 并把 max_tokens 抬到
  `KEEPALIVE_THINKING_MODEL_MAX_TOKENS=1024`（`core/cache_keepalive.py`，
  判定入口 `_glm_keepalive_thinking_locked` 显式限定 GLM 家族——Kimi-K3/minimax 等
  同样无关闭档的非 GLM 模型不得进此分支）；否则首次保活即报错并令服务自我停用。

- `core/llm.py` 在飞请求硬中断：`_request_stream` 把每个流请求登记到 cancel_event 名下
  （`_inflight_streams` 注册表），停止路径（GUI stop / CLI Ctrl+C / 子代理 `_terminate`）
  先 set cancel_event 再调 `abort_inflight_streams()` 直接关闭底层流，阻塞读立即抛错退出，
  不再静默接收数据直到请求超时。铁律：只关单个 response 流（逐层探测 close/completion_stream/
  response），绝不触碰共享 HTTP 连接池；关闭动作吞掉一切异常；先 set 事件再 abort，
  让被关闭一侧归一为用户取消（KeyboardInterrupt 打捞路径）而非网络错误重试。

- 铁律：请求路径绝不改写历史中间字节；任何历史改写必须一次性、幂等、DB 层完成。

- 回归守卫：`tests/test_prompt_byte_stability.py`（system prompt / tools schema 字节稳定性）。

---

## 🎨 Code Style & Quality Guidelines

1. **Terminal Output Constraints**:

   - CLI 实时流式输出（`assistant_delta`）原样透传文本，不渲染 Markdown，加粗/斜体符号会显示为字面星号；历史回放经 `core/terminal_markdown.py:render_markdown_to_ansi` 渲染为 ANSI。模型侧输出规则以 system_prompt.md 为准（支持标准 Markdown）；在 CLI 界面直接打印开发/调试信息时避免依赖 `**`/`*` 强调，用缩进、大小写或空行突出重点。
2. **Concurrency and UI Thread-safety**:

   - Any background worker threads or asynchronous helper threads **must not** directly touch global layout variables, call `print_formatted_text`, or run `app.invalidate()`.

   - Always route UI updates back to the main prompt_toolkit thread loop using `app_instance.loop.call_soon_threadsafe(callback)`.

   - GUI 侧铁律：`evaluate_js` 只允许在 `gui_bridge.py` 的 `gui-js-dispatcher` 单消费者线程中调用；
     事件源线程（回合 worker / delta 冲刷 / 终端泵）只向 `_js_queue` put_nowait，队列满丢弃新事件，
     绝不在 `_delta_lock` 内触碰 JS 桥（曾因渲染进程挂起导致全管线级联冻结）。

   - GUI 运行日志：`~/.ycode/logs/gui-runtime.log`（2MB x 3 滚动，`gui_main._configure_runtime_logging`），
     打包版无控制台，排查 GUI 问题先查此文件。

   - 取消语义铁律：用户主动取消（GUI stop / CLI Ctrl+C）后，cancelled 任务绝不注入“接手完成”指令
     （`agent_service.build_background_task_summary` 只对 failed/timed_out 接手）；自动汇总回合被
     `gui_bridge._user_cancelled_sessions` 抑制、计划续推 nudge 被 `plan.suppress_plan_auto_continue`
     抑制，防止被取消的工作在后续回合复活；用户发新消息或计划项重新进入 in_progress 时解除。
3. **Database Transactions**:

   - Ensure you commit all SQLite operations using `conn.commit()` at the end of functions to prevent thread blockages or data loss.

