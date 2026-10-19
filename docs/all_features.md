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

### 5. GUI 自定义主题系统 (`core/gui_theme.py`)

星瑶的 PyWebView 图形界面拥有独立于 CLI 的声明式主题系统：GUI 使用 `gui_theme_id` 配置，CLI 继续使用 `config.theme` 与 `/theme` 命令，两者互不影响（修改 GUI 主题不会改变 CLI 配色，反之亦然）。主题是受严格校验的 Schema v1 JSON（`~/.ycode/gui-themes/<主题ID>/theme.json`），不加载任意 CSS、JavaScript、HTML 或远程 URL。

**设置入口与基本流程**（设置 → 外观与主题）：

- 内置 `builtin.dark`（内置深色）/ `builtin.light`（内置浅色）/ `builtin.qq2009`（内置 QQ2009，致敬 2009 年 QQ 经典界面的浅色皮肤：浅蓝面板、白底工作区、QQ 蓝强调色与橙色强调；该内置主题另带一层主题专属 CSS（`frontend/src/themes/qq2009.css`，以 `html[data-gui-theme-id="builtin.qq2009"]` 为作用域，仅该主题激活时生效），用于还原渐变标题栏与橙色悬停等平色 token 无法表达的质感）/ `builtin.field-notes`（内置 田野笔记 Field Notes，纸质笔记本皮肤：墨绿框架、米色纸面工作区、朱橙强调色，方正圆角与细边框）四套基础主题，只能「启用」或「复制为自定义主题」，不能直接编辑、重命名或删除；顶部太阳/月亮按钮仍是内置深浅主题的快捷切换。
- 用户主题可修改名称、重命名主题 ID（小写字母/数字/连字符）、删除、导入导出 ZIP；删除当前活动主题会被拒绝，需先切换到其他主题。
- 编辑实时预览（含 Windows 原生标题栏颜色），「取消」恢复编辑前状态，「保存」在后端完整校验并原子写入成功后才生效并启用。
- 快速输入时预览自动合并，同一时刻最多一个预览请求，保存/取消前不会丢失最后一次输入。
- 带未保存改动离开编辑器或切换主题时会弹出确认（保存 / 放弃 / 继续编辑）。

**可编辑的分区**：全局语义颜色（背景/文字/强调/边框/圆角/字体等 token），加七个 GUI 分区——`app_header`（顶栏）、`sidebar`（左栏）、`conversation_toolbar`（会话工具栏）、`workspace`（主工作区）、`composer`（输入区）、`right_panel`（右侧面板）、`statusbar`（状态栏）。每个分区可独立设置背景、文字、边框、强调色、背景透明度、背景图片及图片透明度、显示方式（`cover` / `contain` / `auto`）与 Schema 允许的固定图片位置；未覆盖的字段严格继承基础主题。其余 Web 颜色接受 `#RRGGBB` 或 `#RRGGBBAA`。

**全局背景图**（全局颜色页）：设置后「全局大于一切」——所有分区自己的背景图与背景色自动失效，整个窗口统一显示全局图（可调透明度/填充/位置）；移除全局图后各分区设置自动恢复。

**无边框自绘标题栏**：应用窗口为无边框形态，顶栏兼作标题栏（颜色/图片/全局图贯通到顶）。顶栏中部拖动移动窗口、双击最大化；窗口四边/四角通过透明热区调用原生系统循环调整大小，WebView 内容铺满窗口、不显示宿主边带；最小化/最大化带系统动画，拖到屏幕边缘或 Win+方向键触发 Aero Snap；pywebview 延伸进客户区的 1px frame 与 DWM 原生外框均关闭。系统标题栏的背景/文字色字段在无边框模式下仅保留按钮明暗语义。

**图片与大小限制**：背景图仅支持 PNG / JPEG / WebP（扩展名与实际解码内容必须一致，经真实解码校验，拒绝伪装图片），单张最大 10 MiB、最大 8192×8192；单个主题资源总量最大 30 MiB。图片由后端复制到主题目录 `assets/`，主题不保存工作区绝对路径。

**ZIP 导入导出**：用户主题可一键导出为 ZIP（`theme.json` + 引用的图片资源）；导入时限制文件数、解压后总大小与压缩比（防 ZIP 炸弹），先解压到临时目录完整校验（含路径逃逸与符号链接检查）再原子移动到主题目录，失败不留半成品。

**损坏恢复**：活动主题不存在、损坏、含非 UTF-8 清单或 Schema 版本不支持时，GUI 显示阻断式错误页与「重置为内置深色/浅色」按钮，由用户明确选择；绝不自动切换或静默修复。保存、导入、切换失败时原活动主题与 `config.json` 保持不变。

**安装包影响**：主题功能只增加少量 Python / TypeScript / CSS 代码与 JSON 常量，不内置任何图片，安装包增量可忽略；用户上传的图片与导入的主题保存在用户数据目录 `~/.ycode/gui-themes/`，不写入安装目录，也不进入 PyInstaller 或前端构建产物。

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
- **会话历史分页懒加载（GUI）**：切换会话/启动时仅加载最近 50 条消息（`get_history_paged`，回合对齐向前扩展，上限 limit+30）；滚动到顶自动向前翻页拉取更早历史；token 用量重算异步进行，不阻塞界面。

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

---

## 🧩 九、对话可操作系统管理工具（2026-10-19）

此前有一批功能只能「在 GUI 设置页点按钮」或「在 CLI 敲斜杠命令」，Agent 侧没有任何出口。本轮新增 **21 个管理类 Agent 工具**，把这些手点操作变成可以直接对话完成。全部工具在 `core/tool_registry.py` 的 `build_default_registry()` 中通过 `admin_specs` 段注册（`from core import tools_admin_*` + 各模块 `build_specs()`），按域分模块实现。注册表条目由 50 增至 71（本机已装插件的口径；新增的 21 个为内置工具）。

### 1. 会话与上下文（`core/tools_admin_session.py`）

- **`session_manage`**：`list` / `create` / `rename` / `delete` / `archive` / `unarchive` / `move_workdir` / `clear_history` / `compress` / `checkpoint_list` / `checkpoint_restore` / `undo_list` / `undo_preview` / `undo_recover`
  - 覆盖原来的：会话列表的重命名、删除、归档/取消归档、迁移工作目录、新建会话，CLI 的 `/compress`、`/clear-history`、`/checkpoint`，以及 GUI 的撤销记录列表/预览/崩溃恢复面板。
  - 删除会话、清空历史不可恢复，执行前需确认目标会话；`undo_recover` 用于收尾被中断的撤销操作。

### 2. 模型与供应商（`core/tools_admin_model.py`）

- **`model_manage`**：`list_providers` / `list_models` / `add_provider` / `add_preset_provider` / `update_provider` / `delete_provider` / `add_model` / `update_model` / `delete_model` / `set_active` / `set_reasoning` / `test` / `fetch_models`
  - 覆盖原来的：`/models` 全屏表单与设置页模型页的全部增删改、切换活动模型、思考档位、连通性测试、从服务商拉取模型列表。
  - API Key 仍沿用既有加密存储链路，工具只写不回显；`computer_protocol` 等字段沿用原校验规则。

### 3. 技能、插件与 Hook（`core/tools_admin_skill.py`）

- **`skill_manage`**：`list` / `enable` / `disable` / `refresh` / `restore`（替代 `/skills` 的勾选启停、重新扫描与「恢复随包内置版本」）。
- **`plugin_manage`**：`list` / `enable` / `disable` / `settings_get` / `settings_action`（替代插件页启停与插件设置页的读取/动作执行）。
- **`hook_manage`**：`list` / `save` / `delete` / `set_disabled`（替代 Hooks 页的增删改与停用开关，支持用户级与项目级）。
- 边界：这三个工具**不提供** trust/grant/deny/revoke；未信任的插件无法通过工具启用，hook 由模型写入后也仍需用户在交互终端自行信任才会执行，信任状态只在 `list` 结果中原样透出。

### 4. 运维与诊断（`core/tools_admin_ops.py`）

- **`usage_stats`**：按 1 / 7 / 30 / 365 天汇总 token 用量、各模型维度与缓存命中率（只读），用于回答「这个月花了多少」。
- **`session_export`**：把指定会话（默认当前）导出为 md / html / json，返回真实文件路径。
- **`update_manage`**：`check` / `download` / `cancel` / `install` / `cleanup`（替代更新按钮与 `/update` 流程）。
- **`system_settings`**：`get` / `set`，仅限 `command_shell`（默认命令解释器）、`turn_step_limit`（单任务最大轮次）、`search_exclude_dirs`、`ai_output_dir`；权限模式、沙箱与命令白名单被显式拒绝。
- **`diagram_manage`**：`export` / `list` / `open`（Mermaid 代码块导出 HTML、列最近图表、用默认浏览器打开），补上 `/diagram` 的预览入口。
- **`doctor_run`**：聚合自检（环境、配置、凭据、数据目录、数据库、MCP、模型端点、GUI 运行时），`json_output=True` 返回结构化结果，与 `/doctor` 等价。

### 5. 外观与人设（`core/tools_admin_look.py`）

- **`theme_manage`**：`list` / `get` / `active` / `activate` / `save` / `copy` / `rename` / `delete` / `import` / `export` / `read_asset`，覆盖 GUI 主题的切换、编辑保存、复制、重命名、删除、ZIP 导入导出与图片资源读取。内置主题只能启用或复制，不能编辑/删除；删除活动主题会被拒绝；导入导出使用传入的本地路径，不弹文件选择框。
- **`persona_manage`**：`get` / `set`，读写智能体名称、自定义人设正文与对用户的称呼，改动写入本地人设配置并在后续对话的系统提示词中生效（无需重启）。

### 6. 外部集成与协作（`core/tools_admin_integration.py`）

- **`feishu_manage`**：`get` / `set` / `test` / `status`（飞书集成配置、连通性测试与运行状态）。
- **`lan_manage`**：`status` / `start` / `stop`（局域网手机远程服务的启停与状态）。
- **`decision_manage`**：`get` / `set` / `probe` / `scenes` / `set_scene`（决策模型配置与探针；场景为 `router` / `secretary` / `search` / `safety`，三态 `off` / `shadow` / `live`）。

### 7. 交互面板与屏幕（`core/tools_admin_interaction.py`）

- **`pty_manage`**：`ensure` / `write` / `resize` / `kill` / `state`，让需要 TTY 的程序（交互式脚手架、分页器、登录提示）也能由 Agent 驱动，`execute_command` 的非交互子进程不再是唯一选择。
- **`browser_pane`**：`open` / `close` / `navigate` / `back` / `forward` / `reload` / `state` / `set_bounds`，内嵌浏览器面板从「必须用户先开启」变成可对话开合、导航与调整位置尺寸。
- **`screenshot_capture`**：`mode=full`（含多显示器）/ `mode=region`（指定矩形）截取桌面并存盘，返回真实文件路径。
- **`clipboard_image`**：`read` / `save`，把剪贴板中的图片落盘为文件，可直接作为后续输入。
- **`computer_control`**：`status` / `pause` / `resume` / `stop` / `browser`，补齐电脑操控的运行状态查看、暂停恢复、紧急停止与可用浏览器查看；不提供权限档位或信任类操作。

### 8. 明确的非目标

工具化不等于「全部功能都能对话完成」，以下仍是刻意保留的边界：

- **权限档位、沙箱开关、命令白名单**：属用户侧授权模型，`system_settings` 显式拒绝，只能由用户在界面/CLI 修改。
- **Hook 与插件的信任授予**：信任是对本机命令执行的授权，必须由人确认，三个管理工具只做配置与启停。
- **免责声明确认**：合规交互，不应由 Agent 代劳。
- **窗口最小化/最大化/关闭、侧栏分组排序、消息导航**：纯界面交互，无工具化价值。
- **尚未补齐**：会话消息排队（`/queue`）、附件拖拽上传仍无对应工具；主题实时预览仍需 GUI。
