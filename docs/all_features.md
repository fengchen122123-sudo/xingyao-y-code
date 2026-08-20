# 星瑶 (Y-code CLI) 全功能与系统设计文档

本文档是星瑶 (Y-code CLI) 项目的官方全功能开发手册与系统架构文档，详尽收录了目前项目中**所有模块、底层类库、命令工具、第三方扩展以及配套脚本**的完整技术实现与细节。

---

## 🎨 一、终端用户交互系统 (CLI & UI Layout)

星瑶拥有一个由 `prompt_toolkit` 构建的完全响应式、高颜值 CLI 界面，支持各种控制台特技与多窗口层叠交互。

### 1. 双栏/多窗口全屏交互 UI
- **会话管理器 UI (`/session` 或快捷键进入)**：
  - 调取全屏交互式列表，用户可上下键浏览所有本地 SQLite 中存储的会话。
  - 支持快捷键：按下 `Enter` 键即时载入会话；`Delete` 键执行物理级安全删除（同步清空数据库表）；`F2` 或 `R` 键触发轻量级行内重命名，修改会话标识。
- **模型管理器 UI (`/models`)**：
  - 全屏表单交互窗口，直观展示已配置的大模型厂商。
  - 支持实时编辑 Base URL、API Key、切换 API 传输协议（如 OpenAI 协议、Anthropic 协议），以及实时指定默认的活动模型。
- **技能选择器 UI (`/skills` 或主菜单)**：
  - 全屏展示所有从 `.agents/skills` 或 `~/.ycode/skills` 中扫描出的技能插件。
  - 提供行内复选框，使用 `Space` 空格键一键勾选激活/禁用。

### 2. 状态指示与 Token 精密仪表盘
- **底部状态栏 (StatusBar)**：
  - 动态计算并展示当前会话消耗的 Token 指标，包含：
    - **Latest Turn Tokens**：上一轮对话产生的 Token 数。
    - **Cumulative Tokens**：该会话自创建以来消耗的总 Token 计数。
    - **Cache Hit (Prompt Caching)**：展示由于命中了 API 服务端的 Prompt 缓存而为您节省的 Token 量，帮助用户感知成本。
- **思考动画与秒数计时**：
  - 星瑶在后台调用 completion 接口时，右下角状态栏会浮现转圈 Loading 动效与自增秒数计时（`is_thinking` 状态），提示程序活跃中。

### 3. 主动授权与 Diff 变化展示
- **安全沙箱授权框**：
  - 进行写文件、局部修改或终端执行命令等敏感动作时，暂停思考并从底部弹出一个交互式确认面板，让用户选择 `[批准 (y)]`、`[拒绝 (n)]` 或 `[总是批准此命令 (a)]`。
- **Kimi 级 Diff 对比面板 (`core/diff_viewer.py`)**：
  - 星瑶通过大模型修改代码后，系统会计算修改前后的文本，使用 `difflib` 并通过 `rich` 渲染出美观的高对比度 Diff 框。
  - 行前加 `+` 且整行绿色高亮代表新增；行前加 `-` 且整行红色高亮代表删除。
  - 自动检测输出长度，对于超长改动自动进行截断显示，保证屏幕干净。

### 4. 配色主题定制系统 (`core/themes.py`)
星瑶内置了多套 premium 精美配色主题，通过命令 `/theme <名称>` 或全屏列表切换：
- **`cyan` (极光青)**：默认主题，冷色调，适合现代编程。
- **`matrix` (骇客绿)**：黑客帝国经典绿字，具有极强的极客感。
- **`nord` (北欧蓝)**：优雅高冷的冷灰蓝，降低长时间编程的眼部疲劳。
- **`cyberpunk` (赛博粉)**：霓虹粉配深紫，极具视觉冲击。
- **`dracula` (德古拉紫)**：经典暗黑德古拉配色。
- **`lava` (熔岩红)**：高饱和度的警示红，适合高强度开发。
- **`sunset` (落日橙)** / **`amber` (琥珀黄)** / **`white` (象牙白)**。

---

## 🧠 二、KAIROS 长期记忆系统 (`core/memory_kairos.py`)

KAIROS 系统是星瑶的跨会话长期记忆核，保证大模型能够进行跨越时间周期的持续学习。

### 1. 每日日记账 (Logs Directory)
- 记忆物理路径位于 `~/.ycode/memory/`。
- 每日生成以日期命名的 Markdown 文件 `logs/YYYY/MM/YYYY-MM-DD.md`。
- 星瑶在对话中产生的灵感、得到的纠正或记录的事实，都会打上高精时间戳（如 `-[23:15] 用户偏好 4 空格缩进`）自动追加写入。

### 2. 流式 XML 记忆提取
- 在 `core/llm.py` 中，星瑶的输出流会被实时检测。一旦发现大模型输出了 `<memory>...</memory>` 标签，其内容会被系统无缝截获并自动写入每日日记账，无需用户干预。

### 3. 结构化分类归档
- **记忆库分类**：
  - `user.md`：记录用户的开发角色、使用的技术栈偏好。
  - `feedback.md`：记录用户对星瑶的指引或纠错规则（如“不要返回冗长的废话”）。
  - `project.md`：记录长期的项目事实、未修复的问题、技术债。
  - `reference.md`：记录指向外部文档、API 或系统路径 of 指针。
- **`MEMORY.md` 索引**：作为轻量级记忆地图，保存着各分类文件的超链接与单行描述。

### 4. 梦境整理任务与 Prompt 注入
- **后台梦境提炼**：通过 `/dream` 命令或后台触发，启动提炼任务。它拉取近期的 Logs 内容，使用大模型生成整理 JSON 策略，更新分类 Markdown 并更新 `MEMORY.md`。
- **系统提示词注入**：每次对话开始，系统会自动读取 `MEMORY.md` 索引并以 `# Auto Memory` 结构动态拼接在 System Prompt 尾部，使星瑶立即拥有过往的全部记忆背景。

---

## 📂 三、上下文动态注入与 `@` 文件引用

这是星瑶在命令行模式下非常强大的“上下文挂载”特征（实现于 `core/input_parser.py`）：

- **`@` 文件即时读取**：
  - 用户在输入框中提问时，可以随时打出 `@relative/path/file.py` 或 `@absolute_path`。
  - 智能解析引擎会自动识别输入文本中的 `@` 标记，将对应的文件在后台无声读取。
  - 在最终发送给 LLM 的 Payload 中，系统自动将文件路径、文件名以及文件的完整内容以 Markdown 代码块的形式追加到 User 消息头部，作为精准的上下文参考，免去了用户手动复制粘贴代码的痛苦。

---

## 🚀 四、并行执行引擎与超高速搜索工具

### 1. 只读工具 ThreadPoolExecutor 并发化
- 每次大模型产生一组 tool_calls 时，星瑶会进行依赖性分类。
- 连续的只读工具（如 `read_file`、`glob_files`、`search_code`、`search_news`）会被一并分发到 `ThreadPoolExecutor` 中，采用**多线程并发读取**（默认最大 10 线程），并发执行完毕后再将所有结果一同返回给大模型。
- 该机制让长依赖工具链的分析性能得到了质的飞跃。

### 2. Ripgrep (rg) 搜索匹配加速
- **`search_code` 性能飞跃**：优先尝试在系统 PATH 中拉取 `ripgrep`，通过 `rg -n -i --no-heading` 命令进行瞬时多文件正则查找；如果本地未安装，则自动平滑退避到 Python line-by-line walk 遍历，实现超高兼容性。
- **`glob_files` 模式查找**：新增的 glob 文件名定位工具。支持以 `**/*.py` 等表达式快速模糊匹配项目目录中的文件，同样优先采用 `rg --files --glob` 实现，支持结果数量自动截断（默认 100 条）。

---

## 🤖 五、多智能体异步托管 (Subagents)

星瑶支持创建辅助的子 Agent 去后台执行琐碎、长周期的代码查阅或分析工作。

- **守护线程执行器 (`SubagentManager`)**：
  - 后台子线程独立创建子 Agent 的 LLM 对话上下文。子 Agent 同样拥有完整的工具箱，可以使用读写文件、执行命令等能力来解决特定任务。
  - Coordinator 主线程保持 100% 闲置交互，不阻塞用户的 CLI 界面。
- **异步通知 Queue 与回填**：
  - 子 Agent 完成或发生异常时，其最终报告会被捕获并封装进标准的 `<task-notification>` XML 消息。
  - 消息会被送入 Queue，Coordinator 在下一轮对话发起前自动排干 Queue，将通知文本注入大模型上下文，实现主副智能体的信息同步。

---

## 💻 六、常驻后台进程管理 (`core/task_manager.py`)

当星瑶通过 `execute_command` 运行一些不会退出的常驻服务（如 `npm run dev`，`python app.py`）时：

- **后台进程托管**：
  - 只需将 `run_in_background` 参数置为 `True`，星瑶便会使用 `subprocess.Popen` 将进程挂起在后台，并将其 PID 与任务分配一个 Task ID（如 `task_1`）。
  - 进程的 stdout/stderr 会被重定向输出到本地特殊的日志文件。
- **任务管理器命令 (`/tasks`)**：
  - `/tasks` : 列出所有当前活跃的后台进程 ID、运行时间、PID 以及对应启动命令。
  - `/tasks tail <Task_ID>`：实时读取并输出该后台进程最近的 30 行日志，便于开发调试。
  - `/tasks kill <Task_ID>`：强制终结（Terminate）该后台进程，释放系统端口与资源。

---

## 🛡️ 七、会话恢复、容错与撤销系统

- **Jitter API 退避重试**：当 API 遇到连接不稳定、超时或限频时，计算指数退避时延加随机数挂起重试，最大重试 3 次。
- **上下文超限自动压缩**：API 提示 Prompt 超长时，自动拉取历史，调用 `compress_session` 生成摘要并物理截断旧历史，无缝重试，使用户不会感到中断。
- **会话撤销 (`/undo` 命令)**：
  - 每当星瑶对文件进行修改（`write_file` 或 `edit_file`）前，系统自动在 `core/memory.py` 中备份该文件的旧版本（`set_undo_state`）。
  - 用户输入 `/undo` 后，系统会自动还原受改动的文件到修改前，并同步**从 SQLite 中删除最近一轮大模型的 assistant 回复与 tool_call 记录**，擦除大模型的错误记忆，彻底解决幻觉残留。
- **中断状态回滚**：Ctrl+C 中断时立即擦除本轮尚未完成 of `messages` 碎片，保证会话一致。
- **LiteLLM 与多厂商 API 兼容性修正**：在 `core/llm.py` 内优化了 `_strip_media_and_merge` 方法，完全保留了 `tool_calls`、`tool_call_id` 以及 `name` 工具链核心属性，并对包含工具动作的消息自动切断合并，彻底消除了由于消息格式不匹配导致的 `AnthropicException - 'tool_call_id'` 协议兼容报错，打通了 Anthropic 与各主流模型对工具的完美对接。
- **SQLite 细粒度实时消息流持久化与反序列化修复 (v1.0.3)**：
  - **实时持久化**：重构了 `main.py` 和 `core/llm.py`。当大模型运行时，所有中间步骤生成的 assistant 消息（含 `tool_calls` 结构）以及并发/串行工具的返回结果（`tool` 角色消息）全部在生成生成迭代中**实时、细粒度地写入本地 SQLite 数据库**。这不仅能避免进程突发死亡时数据丢失，而且彻底解决了此前由于只保存最终拼接文本，导致中间步骤的工具调用参数及执行输出丢失的缺陷。
  - **Top-Level 展开反序列化**：重写了 `core/memory.py` 内的 `get_history` 数据加载逻辑。如果数据库中加载的聊天记录反序列化为 dict，会自动将其解包并合并在消息体第一层（保留顶层的 `tool_calls` 或 `tool_call_id`），而不是错误地作为 `content` 的子 dict 嵌套，保证历史记录重新载入大模型时格式 100% 准确。

---

## 📦 八、打包安装与安全卸载系统

### 1. 编译脚本 (`build_release.py`)
- 调用虚拟环境 PyInstaller 依次对核心主程序、卸载程序和安装程序进行打包。
- 使用 `--collect-all` 显式收集 `litellm` 的本地成本定价映射 json、`tiktoken` 离线分词器、`pyfiglet` 字体文件等，打包为完全无需 Python 环境的单体 Executable。

### 2. 双击安装包 (`ycode_setup.py` / `ycode_setup.exe`)
- 解压释放 `ycode.exe` 和 `ycode_uninstall.exe` 到目标目录。
- 自动创建桌面快捷方式。
- 将目标目录写入 Windows 注册表当前用户的环境变量 `PATH` 键中，并通过 `SendMessageTimeout` 向系统广播变更，实现双击安装后立刻在任意新 cmd 中输入 `ycode` 启动。

### 3. 官方卸载向导 (`ycode_uninstall.py` / `ycode_uninstall.exe`)
- 物理删除桌面快捷方式，从环境变量 `PATH` 移除安装目录并向全局广播。
- 弹出选项供用户选择是否彻底抹除用户目录下的 `.ycode` 文件夹（会话数据库与模型配置）。
- **自销毁命令行设计**：进程退出前拉起一个独立的 cmd choice 命令，在 1 秒后强制静默删除当前正在运行 of `ycode_uninstall.exe` 并清空删除原安装文件夹，做到 0 残留。
