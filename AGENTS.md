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
- 插件扫描结果（含禁用/失败/被覆盖/未信任）由 `plugin_loader.get_plugin_infos()` 提供，TUI 内用 `/plugins` 命令查看。
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

---

## 🗄️ 上下文缓存架构（DeepSeek 前缀缓存）

- `core/tool_result_maintenance.py`：工具结果 Snip/Elide 纯函数层，marker 幂等，
  每条消息字节最多改写两次（snip 一次 + elide 升级一次），改写后永久冻结。
- `core/memory.py:maintain_tool_results`：DB 层维护入口，尾部 8000 估算 token 保护区不改写；
  `_fold_stale_media_locked` 在含媒体新消息入库的同一事务内折叠旧媒体。
- `core/agent_service.py`：四级水位线（0.60 snip / 0.80 elide / 0.95 LLM 摘要），
  触发判断优先用上一轮真实 API 用量基线，本地估算只作兜底与改写后的复算。
- `core/agent_service.py:build_stable_system_message`：稳定前缀 = 基础提示词 + 决策策略 +
  通用编码规范 + 长期记忆 + 技能目录 + 子代理清单（跨轮不变内容全并入此处，~6.5K tokens，
  撑满 DeepSeek 2048 整数倍缓存块）。铁律：跨轮不变的静态内容必须进稳定前缀，
  不能放动态运行上下文块（`build_dynamic_prompt` 只承载模型/权限/目录/计划等可变字段），
  否则前缀漂移导致缓存整体失效。
- `core/llm.py`：`_RepeatCallGuard` 抑制同签名成功 ≥2 次的重复工具调用；
  DeepSeek thinking 路由为带 tool_calls 的 assistant 回补空 `reasoning_content` key。
- `core/llm.py:_stream_chunks_with_idle_notice`：流式 chunk 由守护线程 pump、主线程带超时读队列，
  流中段 stalled 超阈值按间隔上报等待状态（此前首 chunk 后最长静默一整个请求超时）。
- `core/tools.py` 子代理并发闸门：超时/取消是协作式（线程可能仍卡在 LLM 请求里），
  但 `_release_slot` 幂等立即归还名额，排队超过任务自身超时预算直接判 timed_out，
  避免泄漏线程占满 5 个名额导致前台回合无限阻塞。
- `core/cache_keepalive.py`：前缀缓存保活默认 120 秒（0 关闭，最小 60），仅 DeepSeek/Kimi 路由生效。
- `core/llm.py` 在飞请求硬中断：`_request_stream` 把每个流请求登记到 cancel_event 名下
  （`_inflight_streams` 注册表），停止路径（GUI stop / TUI Ctrl+C / 子代理 `_terminate`）
  先 set cancel_event 再调 `abort_inflight_streams()` 直接关闭底层流，阻塞读立即抛错退出，
  不再静默接收数据直到请求超时。铁律：只关单个 response 流（逐层探测 close/completion_stream/
  response），绝不触碰 litellm 共享连接池；关闭动作吞掉一切异常；先 set 事件再 abort，
  让被关闭一侧归一为用户取消（KeyboardInterrupt 打捞路径）而非网络错误重试。
- 铁律：请求路径绝不改写历史中间字节；任何历史改写必须一次性、幂等、DB 层完成。
- 回归守卫：`tests/test_prompt_byte_stability.py`（system prompt / tools schema 字节稳定性）。

---

## 🎨 Code Style & Quality Guidelines

1. **Terminal Output Constraints**:
   - Since the TUI terminal interface prints raw text without a Markdown renderer, **never** output markdown formatting such as `**` (bold) or `*` (italic) around words. Use clean indentations, capitalization, or extra spacing to highlight text.
2. **Concurrency and UI Thread-safety**:
   - Any background worker threads or asynchronous helper threads **must not** directly touch global layout variables, call `print_formatted_text`, or run `app.invalidate()`.
   - Always route UI updates back to the main prompt_toolkit thread loop using `app_instance.loop.call_soon_threadsafe(callback)`.
   - GUI 侧铁律：`evaluate_js` 只允许在 `gui_bridge.py` 的 `gui-js-dispatcher` 单消费者线程中调用；
     事件源线程（回合 worker / delta 冲刷 / 终端泵）只向 `_js_queue` put_nowait，队列满丢弃新事件，
     绝不在 `_delta_lock` 内触碰 JS 桥（曾因渲染进程挂起导致全管线级联冻结）。
   - GUI 运行日志：`~/.ycode/logs/gui-runtime.log`（2MB x 3 滚动，`gui_main._configure_runtime_logging`），
     打包版无控制台，排查 GUI 问题先查此文件。
   - 取消语义铁律：用户主动取消（GUI stop / TUI Ctrl+C）后，cancelled 任务绝不注入“接手完成”指令
     （`agent_service.build_background_task_summary` 只对 failed/timed_out 接手）；自动汇总回合被
     `gui_bridge._user_cancelled_sessions` 抑制、计划续推 nudge 被 `plan.suppress_plan_auto_continue`
     抑制，防止被取消的工作在后续回合复活；用户发新消息或计划项重新进入 in_progress 时解除。
3. **Database Transactions**:
   - Ensure you commit all SQLite operations using `conn.commit()` at the end of functions to prevent thread blockages or data loss.
