
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10+-blue.svg)](https://www.python.org/)
[![Version 1.2.17](https://img.shields.io/badge/Version-1.2.17-green.svg)](core/version.py)

# 星瑶 (Y-code)

> 一个国产终端 AI 编程助手，代号「星瑶」。支持 TUI 控制台与 GUI 窗口双形态、多模型切换、子代理编排、计划执行、长期记忆、MCP 扩展与可插拔技能系统。

## ✨ 功能亮点

### 对话与界面

- **终端交互界面**：基于 `prompt_toolkit` 的 Claude Code 风格输入框，支持斜杠命令、Tab 补全、`@文件` 快速挂载、多行粘贴
- **GUI 窗口版**：基于 `pywebview` + Vue 3 + TypeScript 的图形界面，与 CLI 共享同一套核心引擎
- **GUI 拖拽附件**：拖入文件即上屏占位并自动补全真实路径，PDF 等二进制文件注入路径提示供模型用工具解析
- **文件树面板**：懒加载子目录，支持手动刷新与页签激活/窗口聚焦自动刷新，展开状态可恢复
- **9 种终端主题**：cyan / matrix / nord / cyberpunk / dracula / lava / sunset / amber / white，`/theme` 一键切换
- **终端 Markdown 渲染**：代码块语法高亮、表格、列表等富文本展示
- **思考流显示**：实时呈现模型推理过程（`<thinking>` 标签解析 + 句子边界缓冲）

### 模型与工具

- **多模型管理**：通过 `/models` 打开全屏模型选择器，支持 OpenAI / Anthropic / DeepSeek / Agnes 等多种协议，由 `LiteLLM` 统一调用
- **副模型机制**：可配置副模型承担视觉识别、记忆压缩、子代理调用等任务，主副分工
- **22 个内置工具**：文件读写、代码搜索、命令执行、网页抓取、天气查询、LSP 符号查找等
- **工具结果缓存**：相同搜索/读取自动走本地缓存，节省 token 与时间

### 核心内核（Harness）

- **固定主代理内核**：严格状态机（Turn 8 态 / Step 10 态），非法状态转移显式失败；内核非插件化、不可动态替换
- **SQLite Journal**：owner 租约 + 心跳防多进程冲突，事件序列单调分配，消息与事件同事务原子提交
- **崩溃恢复**：未完成回合仅标记为 `interrupted`，绝不自动重放模型请求、命令或文件写入等副作用
- **请求指纹**：对最终 Provider 请求生成不可变指纹，配合 byte-golden 回归防止缓存漂移
- **工具管线**：同签名重复调用抑制（RepeatCallGuard），相邻只读工具并发批量执行

### 高级编排

- **子代理系统**：5 种内置类型（explore / implement / test / review / general）+ 自定义子代理，支持依赖感知并行调度与嵌套（最深 3 层）
- **计划执行模式**：复杂任务先用 `plan_write` 建立可执行计划，按依赖图逐项派发子代理，支持暂停 / 恢复 / 重试 / 取消
- **后台任务管理**：长周期命令后台执行，`/tasks` 查看状态、`/tasks tail` 看日志、`/tasks kill` 终结
- **权限系统**：四种模式（confirm 变更前确认 / accept_edits 自动编辑 / plan 计划模式 / full 完全访问），沙盒控制工作目录访问边界

### 记忆与上下文

- **SQLite 会话记忆**：多会话切换、`/undo` 撤销文件修改并同步抹除模型记忆
- **会话级检查点**：每轮对话前自动打 checkpoint，`/checkpoint restore` 整轮回滚
- **长期记忆系统**：四类记忆（user / feedback / project / reference）+ 每日日志 + Dream 整理线程，支持「记住 XXX」指令
- **智能记忆压缩**：历史 token 占用达 70% 自动压缩，工具结果预截断，tiktoken 精确估算
- **会话导出**：`/share` 一键导出为 Markdown / HTML / JSON

### 扩展生态

- **技能系统（Skills）**：扫描 `SKILL.md` 并动态注入系统提示词，支持项目级与用户级
- **插件系统（Plugins）**：进程内插件扩展工具能力，零核心侵入，单插件异常不影响启动
- **MCP 客户端**：全屏 CLI 配置器 + 10+ 预设模板（飞书 / 高德 / B 站 / SQLite / Postgres 等），AI 可自配置 MCP
- **Git Worktree**：`/worktree` 管理多分支并行工作区

## 🚀 快速开始

### 1. 克隆仓库

```bash
git clone https://gitee.com/feng-chenhao/xingyao-y-code.git
cd xingyao-y-code
```

### 2. 创建虚拟环境并安装依赖

```bash
python -m venv venv

# Windows
venv\Scripts\activate
# macOS / Linux
source venv/bin/activate

pip install -e .
```

> 会自动安装全部运行依赖：`rich`、`litellm`、`prompt_toolkit`、`pyfiglet`、`ddgs`、`mcp`、`psutil`、`pillow`。

### 3. 配置 API 密钥

**⚠️ 安全提醒：仓库内不会提交真实密钥。**

推荐方式：启动后在 GUI 模型页「添加厂商」（或 CLI `/models`）从预置厂商目录选择厂商并填写 API Key，无需手动编辑文件。

也可手动复制示例配置后编辑：

```bash
cp config.example.json config.json
```

编辑 `config.json`，填入你的真实 API 密钥：

```json
{
  "active_model_id": "model_1",
  "providers": [
    {
      "id": "provider_1",
      "name": "Agnes AI",
      "base_url": "https://apihub.agnes-ai.cn/v1",
      "api_key": "sk-your-real-api-key-here",
      "protocol": "openai",
      "models": [
        {
          "id": "model_1",
          "name": "agnes-2.0-flash",
          "litellm_model": "openai/agnes-2.0-flash",
          "context_window": 128000
        },
        {
          "id": "model_2",
          "name": "agnes-2.5-flash",
          "litellm_model": "openai/agnes-2.5-flash",
          "context_window": 128000,
          "supports_vision": true
        }
      ]
    }
  ]
}
```

### 4. 启动星瑶

```bash
# CLI 控制台版（推荐）
python main.py

# 或安装后使用命令
ycode
```

### 5.（可选）启动 GUI 窗口版

GUI 模式基于 `pywebview` + Vue 3 前端。前端构建产物 `frontend/dist/` 不随仓库提交，首次使用需先构建：

```bash
pip install pywebview

cd frontend
npm install
npm run build
cd ..

python gui_main.py
```

> 前端开发调试时也可改为在 `frontend/` 下运行 `npm run dev`（Vite 开发服务器，端口 5173），`gui_main.py` 会优先加载它。两者都不可用时 GUI 无法启动，CLI 模式不受影响。

### 6.（可选）构建 Windows 安装包

```bash
# 完整构建（GUI 主安装包 + 可选 CLI 组件）
venv\Scripts\python build_release.py

# 仅构建 CLI 相关产物
venv\Scripts\python build_release.py --cli-only

# 跳过前端构建（复用现有 frontend/dist）
venv\Scripts\python build_release.py --skip-frontend

# 强制重新构建前端
venv\Scripts\python build_release.py --rebuild-frontend

# 只重建两个安装向导（复用现有载荷与卸载程序，约 2 分钟）
venv\Scripts\python build_release.py --setup-only
```

产出物位于 `dist/` 下：`Y-code.exe`（GUI）、`ycode.exe`（CLI）、`ycode_setup_v{版本}.exe`（主安装包，文件名自动带版本号）、`ycode_uninstall.exe`（卸载向导）。详见 [docs/BUILD.md](docs/BUILD.md)。

## 📦 安装与卸载（Windows）

发布产物是经典 Windows 安装程序样式（NSIS MUI2 风格）的图形化向导，白色顶部横幅 + 底部 上一步/下一步/取消 按钮组。

### 安装向导（`ycode_setup_v{版本}.exe`）

页面流程：**欢迎 → 安装用户 → 路径与选项 → 安装中 → 完成**

- **安装用户选择**：

| 选项 | 安装位置 | 权限 | 卸载注册表 | PATH | 快捷方式 |
|------|---------|------|-----------|------|---------|
| 仅为我安装（推荐） | `%LOCALAPPDATA%\Programs\Y-code` | 无需管理员 | HKCU | 用户 PATH | 个人桌面 |
| 为所有人安装 | `%ProgramFiles%\Y-code` | 需管理员（自动 UAC 提权重启） | HKLM | 系统 PATH | 公共桌面 |

- **可选组件**：同时安装命令行版 CLI（默认勾选）、创建桌面快捷方式（默认勾选）
- **重装检测**：检测到已有安装时自动沿用原安装范围与路径，锁定相关选项并提示覆盖安装
- **秒开体验**：核心程序包以单文件 zip 载荷内嵌，双击几秒出界面（旧方案散文件需 20~95 秒）

### 卸载向导（`ycode_uninstall.exe`，随安装包释放到安装目录）

同款经典样式的图形化向导：**欢迎 → 卸载选项 → 卸载中 → 完成**

- 清理桌面快捷方式（GUI/CLI 及旧版 TUI 命名都尝试）、PATH、注册表卸载入口
- 按安装范围自动适配：machine 安装会清理 HKLM 注册项、系统 PATH 与公共桌面（非管理员运行时自动 UAC 提权）
- 可选删除 `~/.ycode` 用户配置与历史会话（默认保留，重装后可继续使用）
- 完成后退出向导自动自销毁，清空安装目录残留

也可从「Windows 设置 → 应用 → 已安装的应用 → Y-code」发起卸载。

## ⌨️ 斜杠命令

在输入框中输入以下命令（支持 Tab 补全）：

### 会话与历史

| 命令 | 说明 |
|------|------|
| `/help` | 查看所有命令帮助 |
| `/session` | 打开全屏会话管理器（回车切换，Del 删除，R/F2 改名） |
| `/session all` | 列出所有目录下的历史会话 |
| `/session rename <新名称>` | 重命名当前会话 |
| `/session delete <名称>` | 物理删除指定会话 |
| `/new` | 创建并切换到全新会话 |
| `/undo` | 撤销上一次文件修改（同步抹除模型记忆） |
| `/checkpoint` | 检查点管理（`list` 查看 / `restore <turn_id>` 整轮回滚） |
| `/share [md\|html\|json]` | 导出当前会话（默认 md） |

### 模型与外观

| 命令 | 说明 |
|------|------|
| `/models` | 打开全屏模型管理器 |
| `/theme [主题名]` | 列出 / 切换终端配色主题（9 种） |

### 工具与扩展

| 命令 | 说明 |
|------|------|
| `/skills` | 打开技能选择器（空格勾选） |
| `/skills list` | 终端列出所有技能及启用状态 |
| `/skills enable <短ID>` | 启用指定技能 |
| `/skills disable <短ID>` | 禁用指定技能 |
| `/plugins` | 查看已安装的进程内插件 |
| `/mcp` | MCP 服务管理（open / status / tools / enable / disable / reconnect / edit） |

### 任务与计划

| 命令 | 说明 |
|------|------|
| `/tasks` | 查看后台任务列表 |
| `/tasks tail <ID>` | 查看任务日志输出 |
| `/tasks kill <ID>` | 终结指定后台任务 |
| `/plan` | 查看当前任务计划与逐项状态 |
| `/plan pause` / `resume` / `cancel` | 暂停 / 恢复 / 取消计划 |
| `/plan retry <ID>` | 重试指定计划项 |
| `/worktree [list\|create\|remove\|prune]` | Git worktree 管理 |

### 系统控制

| 命令 | 说明 |
|------|------|
| `/permission <confirm\|accept_edits\|plan\|full>` | 切换权限级别 |
| `/sandbox <on\|off>` | 沙盒开关（on=允许访问工作目录外文件） |
| `/clear` | 清屏 |
| `/exit` | 退出程序 |

> 工具执行反馈默认采用精简进度模式：CLI 与 GUI 会合并连续读取和搜索；
> 命令、文件修改、子代理和失败操作单独留痕；普通工具失败会返回模型继续分析，
> 不会被静默隐藏。GUI 可展开完整工具结果和对应文件 Diff。

> 已启用的技能会自动作为 `/<skill_short_id>` 命令加入补全菜单。

### 快捷键

| 快捷键 | 功能 |
|--------|------|
| `Enter` | 发送消息 |
| `Alt + Enter` | 输入框内换行 |
| `Ctrl + C` | 清空输入框 |
| `Ctrl + D` | 退出程序 |
| `Tab` | 命令 / 文件名补全 |

## 🛠️ 内置工具

Y-code 内置 22 个工具，分为只读与变更两组：

### 只读工具

| 工具 | 功能 |
|------|------|
| `read_file` | 读取文件（支持 offset/limit/行号显示/压缩；大文件自动截断） |
| `list_directory` | 列出目录内容 |
| `search_code` | 跨文件代码搜索（基于 ripgrep） |
| `glob_files` | 按 glob 模式查找文件 |
| `find_definition` | 查找符号定义位置（支持 Python/JS/TS/Go/Rust/Java/C/C++/C#） |
| `search_news` | 联网新闻搜索 |
| `fetch_url` | 获取网页或 JSON（含 SSRF 防护） |
| `get_weather` | 查询城市实时天气 |

### 变更工具

| 工具 | 功能 |
|------|------|
| `write_file` | 创建新文件或完整重写小文件（拒绝覆盖 >500 行的已有文件） |
| `edit_file` | 局部修改已有文件（字符串匹配 / 行号两条路径，含智能相似度兜底） |
| `execute_command` | 执行本地命令（默认 30s 超时，支持后台执行） |
| `ask_user` | 向用户提出澄清问题（1-3 个选项） |
| `manage_task` | 查看或终止后台任务 |
| `run_subagent` | 启动辅助 Agent（5 种内置类型 + 自定义） |
| `plan_write` | 创建或替换任务计划 |
| `plan_read` | 读取计划状态 |
| `plan_update` | 更新计划项元数据 |
| `plan_retry` | 重置失败项为 pending |
| `mcp_add_server` | 新增 MCP server 配置 |
| `mcp_remove_server` | 删除 MCP server 配置 |
| `mcp_set_enabled` | 启用 / 禁用 MCP server |
| `mcp_list_servers` | 列出所有 MCP server（API Key 掩码处理） |

## 🧠 子代理与计划系统

### 子代理（Subagent）

通过 `run_subagent` 工具启动辅助 Agent，支持 5 种内置类型：

| 类型 | 用途 | 默认超时 |
|------|------|---------|
| `explore` | 只读探索（预计超过 3 次工具调用的探索优先用） | 240s |
| `implement` | 改代码 | 600s |
| `test` | 跑测试与定位失败 | 480s |
| `review` | 只读评审 | 300s |
| `general` | 综合任务 | 600s |

**自定义子代理**：在 `~/.ycode/agents/*.md`（用户级）或 `<cwd>/.ycode/agents/*.md`（项目级）创建 Markdown 文件，含 frontmatter 字段：`name` / `description` / `when_to_use` / `base_type` / `model` / `allowed_tools`。

**特性**：
- 嵌套深度硬限制 3 层，防止无限递归
- 互不依赖的子任务一次性并行派发
- 失败 / 超时 / 取消时主 Agent 用自己的工具兜底
- 同一子任务最多重试 1 次

### 计划模式（Plan Mode）

复杂任务（3+ 文件、50+ 行改动）先用 `plan_write` 建立可执行计划：

1. `plan_write` 建立计划（含依赖图、阶段、验证方式）
2. 主 Agent 通用工具被限制，只能用 `run_subagent` / `ask_user` / `plan_read` / `plan_retry` / `plan_update`
3. 按依赖顺序对每个执行项 `run_subagent` 派发
4. 失败先 `plan_retry` 重置再重新派发，同一项失败 2 次必须先分析根因
5. 只有子代理真实返回成功且无工具失败才能标 `completed`

**计划项字段**：`id` / `subject` / `status` / `depends_on` / `owner` / `files` / `verification` / `stage`（plan/build/review）/ `kind`（execution/input_preparation）

## 🔒 权限与安全

### 四种权限模式

| 模式 | 说明 |
|------|------|
| `confirm`（默认） | 变更前逐次确认（修改文件、执行命令均需询问） |
| `accept_edits` | 自动编辑（工作区内文件编辑自动放行，命令仍询问） |
| `plan` | 计划模式（只读取与制定计划，不做任何修改） |
| `full` | 完全访问（沙盒内全部放行，沙盒外访问仍询问） |

### 沙盒模式

`/sandbox off`（默认）：文件工具仅限访问当前工作目录内；`/sandbox on`：允许访问工作目录外。用户批准的沙盒外操作可单次或会话级放行。

### 安全措施
- 危险命令黑名单（防御 `rm -rf /`、fork bomb、覆写磁盘、`mkfs`、`format` 等）
- `fetch_url` 内置 SSRF 防护（默认拦截云元数据接口，严格模式开启全 IP 校验）
- 路径穿越防护（任务 ID 格式限制、记忆文件名校验）
- 配置文件原子写 + `.bak` 备份 + 自愈

## 📝 记忆系统

### 短期记忆
- **SQLite 持久化**：聊天记录按会话隔离存储
- **`/undo` 撤销**：按会话隔离，多 worker 线程安全
- **会话级检查点**：每轮对话前自动打 checkpoint，`/checkpoint restore <turn_id>` 整轮回滚

### 长期记忆（`~/.ycode/memory/`）

四类记忆，参考 Claude 的 memory 模式：

| 类型 | 用途 |
|------|------|
| `user` | 用户角色、目标、偏好 |
| `feedback` | 用户给出的指导 / 纠正 |
| `project` | 进行中的工作、决策、事件 |
| `reference` | 外部系统资源指针 |

**两种保存方式**：
- Option A：`<memory>...</memory>` 标签（快速笔记，自动提取到每日日志）
- Option B：直接写 `.md` 文件（含 frontmatter）

**Dream 整理**：后台归纳整理线程，收集最近日志 + MEMORY.md 索引，用大模型生成整理策略，更新分类记忆文件。

### 记忆压缩
- 触发阈值：历史 token 占上下文窗口 70%
- 工具结果预截断（超长结果保留开头 + 末尾 + 占位符）
- token 估算优先 tiktoken（cl100k_base），失败按字符数近似

## 🔌 MCP 集成

### MCP 客户端
- 配置文件：`~/.ycode/mcp.json`
- 惰性连接：首次使用时才启动，独立 asyncio 事件循环在 daemon 线程
- 超时保护：连接 10s、调用工具 60s

### 全屏 CLI 配置器
`/mcp` 打开配置器，内置 10+ 预设模板，用户选中后按提示输入字段即可，无需手动编辑 JSON：

- **国内官方**：高德地图、飞书、微信公众号
- **社区**：B 站搜索
- **数据库**：SQLite、PostgreSQL
- **网页抓取 / 搜索**：Firecrawl、Wigolo
- **通用工具**：Sequential Thinking、Filesystem

### AI 自配置
通过 `mcp_add_server` / `mcp_remove_server` / `mcp_set_enabled` / `mcp_list_servers` 四个工具，AI 能帮用户配置 MCP，env 中 API Key 会掩码处理。

## 🎨 技能与插件

### 技能系统（Skills）
基于 `SKILL.md` 文件，注入系统提示词，影响 LLM 行为。

**扫描目录**（按优先级）：
1. 用户级：`~/.ycode/skills/`
2. 兼容全局：`~/.agents/skills/`（仅已存在时扫描）
3. 项目级：通过 `config.json` 的 `skill_search_paths` 显式配置

**内置技能随安装包发布**：安装包内置的 `skills/` 会在首次启动时释放到 `~/.ycode/skills/`
并默认启用；不覆盖用户已改动的同名技能，也不会复活用户主动删除的技能。

**技能文件格式**：
```markdown
---
name: my-skill
description: 一个示例技能
---

这里是技能的详细说明，会被注入到系统提示词中。
```

### 插件系统（Plugins）
基于 `plugin.py` 代码，注册新工具，扩展工具能力。

**目录结构**：
```
plugins/<plugin_name>/          # 内置插件（随仓库发布）
  plugin.json                    # {"name","version","description","entry","enabled"}
  plugin.py                      # 必须暴露 register(registry) 函数
~/.ycode/plugins/<plugin_name>/  # 用户级插件（同名覆盖内置）
```

**特性**：
- 加载器由 `core/tool_registry.py:build_default_registry()` 末尾自动调用
- 运行中新安装的插件即时可见（增量扫盘热加载，无需重启）
- 单插件异常只记录日志并跳过，绝不影响 ycode 启动
- 插件工具通过 `ToolSpec` 元数据自动获得权限拦截、schema 参数过滤与结果截断
- `/plugins` 命令查看已安装插件

## 📁 项目结构

```
xingyao-y-code/
├── main.py                  # CLI 主入口
├── gui_main.py              # GUI 主入口（pywebview）
├── gui_bridge.py            # GUI JS-API 桥接层
├── config.py                # 配置加载（原子写 + 自愈）
├── config.example.json      # 配置模板
├── system_prompt.md         # 系统提示词
├── core/                    # 核心模块
│   ├── version.py           # 版本号（单一来源）
│   ├── agent_service.py     # 对话服务（CLI/GUI 共源）
│   ├── harness/             # 固定主代理内核（严格状态机 / SQLite Journal / 工具管线）
│   ├── llm.py               # LiteLLM 调用与工具编排
│   ├── tools.py             # 本地工具实现
│   ├── tool_registry.py     # 工具注册表
│   ├── tool_protocol.py     # 工具协议（ToolSpec 元数据）
│   ├── memory.py            # SQLite 会话记忆 + 检查点
│   ├── memory_compressor.py # 记忆压缩
│   ├── memory_kairos.py     # 长期记忆 + Dream 整理
│   ├── plan.py              # 计划管理
│   ├── task_manager.py      # 后台任务管理
│   ├── task_scheduler.py    # 依赖感知执行调度器
│   ├── subagent_registry.py# 子代理注册中心
│   ├── permission_broker.py # 权限中介
│   ├── mcp_client.py        # MCP 客户端
│   ├── mcp_selector.py      # MCP 全屏配置器
│   ├── skills.py            # SKILL.md 扫描与注入
│   ├── skill_selector.py    # 技能选择器
│   ├── plugin_loader.py     # 插件加载器
│   ├── lsp.py               # LSP-lite 符号查找
│   ├── worktree.py          # Git worktree
│   ├── model_manager.py     # 多模型配置管理
│   ├── model_selector.py    # 全屏模型选择器
│   ├── session_selector.py  # 全屏会话选择器
│   ├── thinking_stream.py   # 思考流缓冲
│   ├── reasoning_stream.py  # 推理流解析
│   ├── terminal_markdown.py # 终端 Markdown 渲染
│   ├── diff_viewer.py       # Diff 渲染
│   ├── turn_renderer.py     # 轮次渲染
│   ├── history_renderer.py  # 历史渲染
│   ├── tool_presenter.py    # 工具展示
│   ├── tool_cache.py        # 工具结果缓存
│   ├── context_builder.py   # 上下文构建
│   ├── turn_controller.py   # 轮次控制
│   ├── input_parser.py      # 用户输入解析
│   ├── paste_placeholders.py# 粘贴占位符
│   ├── process_io.py        # 进程输出解码
│   ├── share.py             # 会话导出
│   ├── themes.py            # 终端配色主题
│   ├── ask_user.py          # 用户澄清
│   ├── runtime_warnings.py  # 运行时警告
│   ├── ui_markers.py        # UI 标记
│   └── app_context.py       # 应用上下文
├── frontend/               # GUI 前端（Vue 3 + TypeScript + Vite）
│   ├── src/
│   │   ├── components/      # 25 个 Vue 组件
│   │   ├── stores/          # Pinia 状态管理
│   │   └── utils/
│   ├── package.json
│   └── vite.config.ts
├── skills/                 # 内置技能示例
├── plugins/                # 内置插件目录
├── vendor/ripgrep/         # 内置 ripgrep
├── packaging/              # 打包辅助
├── tests/                  # pytest 测试用例（90 个文件）
├── docs/                   # 文档
│   ├── BUILD.md            # 构建文档
│   ├── all_features.md     # 全功能文档
│   └── superpowers/        # 设计与计划文档
├── build_release.py        # Windows 发布打包流水线
├── ycode.spec              # PyInstaller 双主程序打包 spec
├── ycode_setup.py          # Windows 安装向导（经典样式，user/machine 双范围）
├── ycode_setup.spec        # 安装向导打包 spec（开发者手动构建参考用）
├── ycode_uninstall.py      # Windows 卸载向导（经典样式 GUI）
├── ycode_uninstall.spec    # 卸载向导打包 spec
└── README.md               # 本文件
```

## ⚙️ 配置详解

配置文件位置：`~/.ycode/config.json`。全新安装默认得到干净空配置，用户通过 GUI 模型页「添加厂商」或 CLI `/models` 从预置厂商目录选择并填写 API Key；源码开发也可手动 `cp config.example.json config.json` 后编辑。

### 完整配置项

```json
{
  "active_model_id": "model_1",
  "secondary_model_id": null,
  "secondary_model_roles": {
    "vision": true,
    "compression": true,
    "subagent": true
  },
  "image_compression": {
    "max_edge": 1536,
    "quality": 90,
    "max_output_bytes": 1048576
  },
  "search_exclude_dirs": ["target", "out", "bin", "obj"],
  "memory_compression": {
    "retain_count": 8,
    "retain_count_force": 4,
    "tool_result_truncate_threshold_chars": 2000,
    "tool_result_preview_limit_chars": 800,
    "tool_result_token_limit": 600,
    "summary_length_first": 250,
    "summary_length_merge": 350
  },
  "providers": [...],
  "theme": "cyan",
  "permission_mode": "confirm",
  "allow_outside_workspace": false,
  "command_shell": "auto",
  "skill_search_paths": [],
  "prompt_cache_keepalive_seconds": 120
}
```

### 配置项说明

| 配置项 | 说明 |
|--------|------|
| `active_model_id` | 当前激活的主模型 ID |
| `secondary_model_id` | 副模型 ID（用于视觉 / 压缩 / 子代理） |
| `secondary_model_roles` | 副模型角色开关（vision / compression / subagent） |
| `image_compression` | 图片压缩参数（默认长边 1536、质量 90、目标 1MiB） |
| `search_exclude_dirs` | 代码搜索排除目录 |
| `memory_compression` | 记忆压缩参数 |
| `providers` | 模型供应商列表 |
| `theme` | 终端配色主题（9 种） |
| `permission_mode` | 权限模式（confirm / accept_edits / plan / full） |
| `allow_outside_workspace` | 沙盒开关 |
| `command_shell` | 默认命令解释器（auto / pwsh / powershell / cmd；GUI「系统设置」会按实际安装情况限制选择） |
| `skill_search_paths` | 技能搜索路径（项目级） |
| `prompt_cache_keepalive_seconds` | DeepSeek/Kimi 前缀缓存保活间隔（默认 120；0 关闭；最小 60） |

图片附件最大允许 20MiB，解码后最多 4000 万像素。PNG（含透明通道）保持 PNG，
JPEG/WebP 在必要时逐级降低质量，GIF 保持原始动画；发送给视觉模型时使用与实际编码
一致的 Base64 Data URL。GUI 中的处理结果会固定显示在对应用户消息下方。

### 用户数据目录

`~/.ycode/` 下的文件结构：

| 路径 | 用途 |
|------|------|
| `config.json` | 用户级配置 |
| `ycode.db` | CLI 会话数据库 |
| `ycode_gui.db` | GUI 会话数据库（与 CLI 互不可见） |
| `active_skills.json` | 已启用技能列表 |
| `mcp.json` | MCP 服务配置 |
| `system_prompt.md` | 用户级系统提示词覆盖 |
| `memory/` | 长期记忆（logs / user / feedback / project / reference） |
| `skills/` | 用户级技能 |
| `plugins/` | 用户级插件 |
| `agents/` | 自定义子代理定义 |

## 🧪 运行测试

```bash
pytest
```

测试覆盖 90 个模块，包括 agent_turn_e2e、harness（models/journal/runtime/请求指纹/工具管线）、cancellation、config、memory_kairos、permission_broker、plan、task_scheduler、subagent_registry、tools、setup_engine、uninstall_engine 等。

## 🔒 安全与隐私

- `config.json`、`ycode.db`、`active_skills.json`、`mcp.json` 已加入 `.gitignore`，不会被提交
- 永远不要把真实 API 密钥写进源码或 README
- 如果意外把密钥提交到仓库，请立即到对应平台重置密钥
- 卸载向导（GUI）会按安装范围清理桌面快捷方式、用户/系统 PATH、HKCU/HKLM 注册表卸载入口（兼容新旧命名），可选保留或删除 `~/.ycode` 用户配置

## 🤝 贡献

欢迎提交 Issue 或 Pull Request：

- 提交 Issue：<https://gitee.com/feng-chenhao/xingyao-y-code/issues>
- 提交 PR 前请先本地跑通 `pytest`，并遵循 `AGENTS.md` 中的开发规范
- 如果你发现代码里有敏感信息泄露，请第一时间通过 Issue 提醒维护者

## 📄 License

本项目基于 [MIT License](LICENSE) 开源，可自由使用、修改与分发。
