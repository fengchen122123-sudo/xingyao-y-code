# Y-code 全局任务与优化清单 (Comprehensive TODO List)

本清单完整覆盖了**前置历史需求验证**与**新增的 cc-mini 进阶内核优化**，确保所有历史功能稳存，且新架构能够完美融合。

---

## 🛠️ 第一部分：历史用户需求验证与清理

- [x] **模型默认配置验证 (Request 1, 3)**
  - [x] 验证首次运行如果没有配置密钥，默认提示输入 DeepSeek 密钥
  - [x] 验证配置 `deepseek-v4-pro` 和 `deepseek-v4-flash` 模型，默认选择 `pro`，并且在 TUI 界面正确显示对应名，不被偷偷改写成原生底层 ID
- [x] **卸载程序逻辑验证 (Request 2, 4)**
  - [x] 验证安装目录下的卸载程序可以独立运行
  - [x] 确认卸载时会弹窗询问用户“是否删除历史会话与模型配置数据”
  - [x] 确认卸载时能干净地删除环境变量，且无 ysdode 等拼写错误
- [x] **代码审计与冗余清理 (Request 8)**
  - [x] 检查并清理本地编译残留的临时目录（包括打包产生的 `build/`、`dist/` 之外的废弃文件）
  - [x] **清理克隆的临时仓库**：删除本次研究遗留的 `E:\fch\cc-mini_temp` 文件夹
- [x] **跨电脑运行及崩溃问题验证 (Request 9, 10)**
  - [x] 确保 `model_manager.py` 迁移兼容性完备，不在其他电脑启动报错
  - [x] 重新生成一键安装包 `ycode_setup.exe` 并准备交付

---

## 🚀 第二部分：cc-mini 进阶内核优化与开发

### 1. 记忆模块：KAIROS 跨会话长期记忆系统
- [x] **基础存储与日记账**：
  - [x] 在 `core/` 下新建 `memory_kairos.py` 模块
  - [x] 创建 `~/.ycode/memory/` 目录以及 `logs/` 子目录
  - [x] 实现 `append_to_daily_log(entry)` 自动追加带时间戳的当日日志
  - [x] 在 `core/llm.py` 中解析流式输出，提取 `<memory>...</memory>` 标签，自动记录到日记账中
- [x] **结构化记忆分类**：
  - [x] 支持 `user`、`feedback`、`project`、`reference` 四大类型的 Markdown 记忆文件与 Frontmatter 标签读写
  - [x] 维护 `MEMORY.md` 作为轻量级的记忆索引文件
  - [x] 实现 `build_memory_system_section()`，自动将当前记忆索引注入大模型的 System Prompt，使其具备跨会话的持续学习与规避错失能力

### 2. Token 经济学：上下文清理与格式优化 (Context Compression)
- [x] **媒体剥离 (Media Stripping)**：
  - [x] 在长会话压缩及梦境整理前，自动剥离大段附件和图像，替换为 `[image]`, `[document]` 等轻量级文本占位符，节约 Token
- [x] **角色交互校正与工具链修复 (Role Alternation & Tool Calls Fix)**：
  - [x] 在请求发起前自动扫描 `messages`，对连续的同角色消息进行物理合并，强制遵守 API 的 User-Assistant 轮流规则，防止报错。
  - [x] **修复 tool_calls 剥离与合并 bug**：在 `_strip_media_and_merge` 方法中完全保留 `tool_calls`、`tool_call_id` 以及 `name` 工具链核心属性，并对包含工具调用的消息进行条件排他，彻底解决 Anthropic 等 API 会话抛出的 `tool_call_id` BadRequestError 兼容性致命故障。
- [x] **Tool Call 边界保护**：
  - [x] 优化历史分割逻辑，确保 `tool_use` 与 `tool_result` 不会跨切割边界，保持上下文语义完整

### 3. 工具并发：只读工具并行批处理 (ThreadPoolExecutor)
- [x] 在 `core/llm.py` 提取 `_execute_single_tool_call(tc)` 单元函数，支持独立的工具参数纠错与执行
- [x] 在 `_chat_with_model_generator` 重构工具链运行控制流：
  - [x] 定义 `read_file`, `list_directory`, `search_code`, `search_news`, `glob_files` 为只读工具
  - [x] 连续只读工具启用 `ThreadPoolExecutor` (最多 10 线程) 并发处理
  - [x] 修改类工具（写入、修改、执行命令）继续保持串行，确保授权确认顺序和 Diff 正确展示

### 4. 容错与重构：API 重试与中断回滚
- [x] 引入 **Jitter 指数退避重试**，在 `completion()` 发生连接失败或限频时进行带抖动的重试，保护 API 连续性
- [x] 引入 **上下文溢出自动恢复**：
  - [x] 捕获 API 返回 of Context Overflow 异常
  - [x] 自动后台触发会话强行压缩，重新拉取压缩后的会话，并重试原请求，保障会话无缝进行
- [x] 引入 **中断安全回滚**：
  - [x] turn 开始时记录 `turn_start_len = len(messages)`
  - [x] 捕获用户 Ctrl+C `KeyboardInterrupt` 时，执行 `del messages[turn_start_len:]`，将状态干净地回滚到当前轮次开始前

### 5. 异步子 Agent 与消息回填 (Asynchronous Subagents)
- [x] 在 `core/tools.py` 内部创建 `SubagentManager`：
  - [x] 托管后台守护线程池，使 `run_subagent` 在子线程独立运行不阻塞 TUI 线程
  - [x] 构建异步通知 Queue，子 Agent 结束后将 XML 格式的 `<task-notification>` 回报推入 Queue
- [x] 在 `main.py` 的主循环交互前增加 Queue 排水机制，将完成的任务回报注入大模型上下文，并刷新 TUI 界面

### 6. 控制命令集成
- [x] 在 `main.py` 新增 `/dream` 命令，在后台触发长期记忆提炼与 `MEMORY.md` 索引更新
- [x] 新增 `/memory` 命令，在 TUI 中弹窗或在终端查看当前的记忆索引
- [x] 新增 `/remember <text>` 命令，支持用户直接手动向今日日记账记录规约

### 7. cc-mini 辅助工具引入与优化
- [x] **搜索性能优化 (ripgrep)**：重构 `search_code` 工具以支持首选 `ripgrep (rg)` 搜索（支持正则表达式及超高速扫描），并在缺少时优雅降级为原生 Python walk 遍历。
- [x] **Glob 文件匹配工具**：新增 `glob_files` 工具，支持快速依据 glob 模式匹配（如 `**/*.py` 或 `core/**/*.py`）来寻找匹配文件并依修改时间排序，大幅提升长上下文分析下的文件寻址速度。

### 8. 数据库细粒度持久化及历史反序列化修复 (v1.0.3)
- [x] **中间步骤实时落地**：将所有多轮对话中产生的 assistant 中间推理消息及 tool 调用返回结果全部在迭代内即时保存到 SQLite 数据库中，杜绝因进程意外退出产生历史断代。
- [x] **消息反序列化合并**：重写 `get_history` 逻辑，支持把加载出的 dictionary JSON 串在顶级解包合并，彻底解决在工具调用之后，加载出来的旧消息无法被大模型正确识别，从而造成“输出多余/读不懂的内容”的致命问题。
