
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10+-blue.svg)](https://www.python.org/)
[![Version 1.3.7](https://img.shields.io/badge/Version-1.3.7-green.svg)](core/version.py)

# 星瑶 (Y-code)

> 一个国产终端 AI 编程助手，代号「星瑶」。支持 TUI 控制台与 GUI 窗口双形态、多模型切换、子代理编排、计划执行、长期记忆、MCP 扩展与可插拔技能系统。

> **使用须知**：本项目是运行在你本地设备上的 AI 编程辅助工具，自身不提供模型服务，模型需你自行配置并直接与其通信。AI 生成内容可能不准确、需你自行审查验证；软件具备文件读写与命令执行能力，请在使用前阅读下方「免责条款」——安装向导与首次启动都会提示确认。

## ✨ 功能亮点

### 对话与界面

- **终端交互界面**：基于 `prompt_toolkit` 的 Claude Code 风格输入框，支持斜杠命令、Tab 补全、`@文件` 快速挂载、多行粘贴
- **GUI 窗口版**：基于 `pywebview` + Vue 3 + TypeScript 的图形界面，与 CLI 共享同一套核心引擎
- **GUI 拖拽附件**：拖入文件即上屏占位并自动补全真实路径，PDF 等二进制文件注入路径提示供模型用工具解析
- **文件树面板**：懒加载子目录，支持手动刷新与页签激活/窗口聚焦自动刷新，展开状态可恢复；点击图片（png/jpg/gif/webp/bmp/ico/svg）可直接弹窗预览
- **GUI 面板宽度自定义**：侧栏 / 右面板分界线可拖拽调整宽度（双击恢复默认），宽度记忆到本地重启仍生效
- **9 种终端主题**：cyan / matrix / nord / cyberpunk / dracula / lava / sunset / amber / white，`/theme` 一键切换
- **终端 Markdown 渲染**：代码块语法高亮、表格、列表等富文本展示
- **思考流显示**：实时呈现模型推理过程（`<thinking>` 标签解析 + 句子边界缓冲）

### 模型与工具

- **多模型管理**：通过 `/models` 打开全屏模型选择器，支持 OpenAI / Anthropic / DeepSeek / Agnes 等多种协议，由自研 HTTPX 引擎（`core/llm_native_transport.py`）统一调用
- **副模型机制**：可配置副模型承担视觉识别、记忆压缩、子代理调用等任务，主副分工
- **29 个内置工具**：文件读写、代码搜索、命令执行、网页抓取、图片下载与生图、天气查询、LSP 符号查找、计划编排、智能路由等
- **工具结果缓存**：相同搜索/读取自动走本地缓存，节省 token 与时间

### 核心内核（Harness）

- **固定主代理内核**：严格状态机（Turn 8 态 / Step 10 态），非法状态转移显式失败；内核非插件化、不可动态替换
- **SQLite Journal**：owner 租约 + 心跳防多进程冲突，事件序列单调分配，消息与事件同事务原子提交
- **崩溃恢复**：未完成回合仅标记为 `interrupted`，绝不自动重放模型请求、命令或文件写入等副作用
- **请求指纹**：对最终 Provider 请求生成不可变指纹，配合 byte-golden 回归防止缓存漂移
- **工具管线**：同签名重复调用抑制（RepeatCallGuard），相邻只读工具并发批量执行

### 高级编排

- **子代理系统**：5 种内置类型（explore / implement / test / review / general）+ 自定义子代理，支持依赖感知并行调度与嵌套（最深 3 层）
- **计划执行模式**：多步任务（拆解后子任务超过 3 条）先用 `plan_write` 建立可执行计划，按依赖图逐项派发子代理，支持暂停 / 恢复 / 重试 / 取消
- **后台任务管理**：长周期命令后台执行，`/tasks` 查看状态、`/tasks tail` 看日志、`/tasks kill` 终结
- **权限系统**：四种模式（confirm 变更前确认 / accept_edits 自动编辑 / plan 计划模式 / full 完全访问），沙盒控制工作目录访问边界

### 记忆与上下文

- **SQLite 会话记忆**：多会话切换；撤销不改写历史，聊天完整保留
- **按回合撤销**：每个回合的受控文件修改持久化快照，`/undo` 整轮预检后一键撤销；冲突零写入、崩溃可恢复，模型通过上下文得知文件结果已失效
- **长期记忆系统**：四类记忆（user / feedback / project / reference）+ 每日日志 + Dream 整理线程，支持「记住 XXX」指令
- **智能记忆压缩**：历史 token 占用达 70% 自动压缩，工具结果预截断，tiktoken 精确估算
- **会话导出**：`/share` 一键导出为 Markdown / HTML / JSON

### 扩展生态

- **技能系统（Skills）**：扫描 `SKILL.md` 并动态注入系统提示词，支持项目级与用户级
- **插件系统（Plugins）**：进程内插件扩展工具能力，零核心侵入，单插件异常不影响启动；内置混元文生图插件，支持用户级插件热加载
- **MCP 客户端**：全屏 CLI 配置器 + 10+ 预设模板（飞书 / 高德 / B 站 / SQLite / Postgres 等），AI 可自配置 MCP
- **Git Worktree**：`/worktree` 管理多分支并行工作区
- **Git 工作台**：GUI「源代码管理」页签 + CLI `/git` 命令（状态/Diff/暂存/提交/分支/同步/Stash/历史/冲突），支持外部变化自动刷新、仓库信任确认与高风险操作二次确认；AI 在对话中的 Git 操作仍走原有权限流程

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

> 会自动安装全部运行依赖：`rich`、`prompt_toolkit`、`pyfiglet`、`ddgs`、`mcp`、`psutil`、`pillow`。

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
          "name": "agnes-3.0-flash",
          "litellm_model": "openai/agnes-3.0-flash",
          "context_window": 512000,
          "supports_vision": true,
          "supports_function_calling": true,
          "max_tokens": 65535
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

### 4.（可选）接入飞书：在飞书里和星瑶对话

配好后在飞书里给机器人发一句话即可驱动一轮任务；执行完成卡、权限审批卡、提问卡、
待拍板收件箱、卡住提醒与指令集都在飞书闭环。

**飞书侧（一次性，三步）**

1. 在[飞书开放平台](https://open.feishu.cn/)建一个**企业自建应用**，拿到 App ID 与 App Secret。
2. 给应用开通权限：`im:message`（读消息）、`im:message:send_as_bot`（发消息）；
   需要在群里 @ 机器人时再加 `im:message.group_at_msg`。
3. 在「事件与回调」里把订阅方式切到**使用长连接接收事件**，然后**两块都要加**：
   - **事件订阅**：`im.message.receive_v1`（收消息）
   - **回调 / 卡片回调**：`card.action.trigger`（卡片按钮回传）

   > 第二块最容易漏。只配事件不配回调时，消息能收到、机器人也能回话，但卡片上的
   > 按钮（批准 / 拒绝 / 面板 / 翻页）点了没有任何反应。改完订阅记得**重新发布版本**。

> 长连接由星瑶主动连飞书，**不需要公网 IP、也不需要内网穿透**，更不用配回调地址与校验密钥。

**星瑶侧**：打开 GUI → **设置 → 飞书集成**，填入 App ID / App Secret，
可选填「审批白名单」（open_id 列表）与「固定推送会话 ID」，点「测试连接」校验后保存即生效。

- **审批白名单**决定谁能在飞书点「批准 / 拒绝」。留空时飞书侧**不可审批**（卡片只做提醒，
  仍需回桌面端处理）——这是有意的 fail-closed 设计。
  **不知道自己的 open_id？** 在飞书里给机器人发 `/whoami`，它会回你的 open_id，复制进白名单即可
  （open_id 按应用区分，必须用星瑶看到的这个值）。
- 设置页内置飞书侧**六步配置说明**与**配置检查清单**；连不上时先看说明里的排查顺序
  （事件订阅是否选了长连接、应用是否已发布、权限是否已开通）。
- 也可以手改 `config.json`：`feishu_enabled` / `feishu_app_id` / `feishu_app_secret` /
  `feishu_approvers` / `feishu_notify_chat_id`；密钥建议在界面里填（会自动加密存储）。
- 默认关闭；关闭或未配置时星瑶行为与未接入前完全一致。
- 在飞书里发 `/help` 看全部指令（`/panel` `/status` `/new` `/list` `/switch` `/model` `/inbox` `/stop`）。

### 5. 启动星瑶

```bash
# CLI 控制台版（推荐）
python main.py

# 或安装后使用命令
ycode
```

### 6.（可选）启动 GUI 窗口版

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

### 7.（可选）构建 Windows 安装包

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
| `/undo` | 按回合撤销文件修改（`list` / `preview` / `status` / `recover <操作ID>`；默认确认 N） |
| `/checkpoint` | 检查点管理（`list` 查看 / `restore <turn_id>` 整轮回滚） |
| `/share [md\|html\|json]` | 导出当前会话（默认 md） |

### 模型与外观

| 命令 | 说明 |
|------|------|
| `/models` | 打开全屏模型管理器 |
| `/effort` | 切换当前模型推理强度（low / high / max / default） |
| `/usage [日期 [截止日期]]` | Token 用量统计（按模型：今日 / 近7天 / 近30天，支持指定日期或区间） |
| `/theme [主题名]` | 列出 / 切换终端配色主题（9 种） |

### 工具与扩展

| 命令 | 说明 |
|------|------|
| `/skills` | 打开技能选择器（空格勾选） |
| `/skills list` | 终端列出所有技能及启用状态 |
| `/skills enable <短ID>` | 启用指定技能 |
| `/skills disable <短ID>` | 禁用指定技能 |
| `/<短ID> [提问内容]` | 主动且强制调用指定技能执行任务 |
| `/plugins` | 查看已安装的进程内插件 |
| `/plugins enable\|disable <name>` / `reload` | 启用 / 禁用指定插件，重新扫描插件目录 |
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

### 记忆与绘制

| 命令 | 说明 |
|------|------|
| `/remember` | 手动追加一条规则 / 偏好 / 事实到记忆日志 |
| `/dream` | 后台触发梦境总结，提炼归纳记忆日志 |
| `/memory` | 查阅长期记忆索引（MEMORY.md）内容 |
| `/compress [force]` | 手动触发会话记忆压缩（force 强制保留更少条） |
| `/diagram open <序号>\|list` | 在浏览器打开 / 列出导出的 Mermaid 流程图网页 |

### 系统控制

| 命令 | 说明 |
|------|------|
| `/permission <confirm\|accept_edits\|plan\|full>` | 切换权限级别 |
| `/sandbox <on\|off>` | 沙盒开关（on=允许访问工作目录外文件） |
| `/turns <10-1000>` | 设置单任务最大模型轮次（默认 100） |
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

Y-code 内置 29 个工具（不含 iMOM 项目定位插件栈的 5 个定位工具），分为只读与变更两组：

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
| `fetch_image` | 把网页图片安全下载到本地（仅 http/https、png/jpg/gif/webp 白名单，10MB 上限） |
| `hunyuan_generate_image` | 腾讯混元文生图：根据文字描述生成图片并保存到本地（支持多种尺寸与自定义水印，10~60 秒） |
| `tokenrhythm_generate_image` | 基元律动（TokenRhythm）文生图：qwen-image-2.0 / wan2.7-image 模型，生成后立即下载到本地（需在插件页启用并配置该提供商 Key，默认关闭） |
| `tokenrhythm_edit_image` | 基元律动图生图：上传 1 张原图（≤10MB）+ 文字指令改图（同一前置条件） |
| `ask_user` | 向用户提出澄清问题（1-3 个选项） |
| `manage_task` | 查看或终止后台任务 |
| `run_subagent` | 启动辅助 Agent（5 种内置类型 + 自定义） |
| `plan_write` | 创建或替换任务计划 |
| `plan_read` | 读取计划状态 |
| `plan_update` | 更新计划项元数据 |
| `plan_retry` | 重置失败项为 pending |
| `plan_finish` | 完成主 Agent 接管的计划项（并执行验证命令） |
| `plan_resume_takeover` | 恢复因超时移交主 Agent 的计划项 |
| `smart_router_status` | 查看智能路由当前状态与统计 |
| `smart_router_config` | 配置智能路由（模型分层 / 开关） |
| `smart_router_toggle` | 快速开关智能路由 |
| `mcp_add_server` | 新增 MCP server 配置 |
| `mcp_remove_server` | 删除 MCP server 配置 |
| `mcp_set_enabled` | 启用 / 禁用 MCP server |
| `mcp_list_servers` | 列出所有 MCP server（API Key 掩码处理） |

> 内置「iMOM 智能制造系统定位」插件另行提供 `imom_register_project` / `imom_list_projects` /
> `imom_use_project` / `imom_build_index` / `imom_locate` 五个工具，用于 iMOM/MES 项目代码的
> 页面→Controller→SqlMapping 四层定位，适合在 iMOM 相关项目中使用。

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

多步任务（拆解后子任务超过 3 条）先用 `plan_write` 建立可执行计划：

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
| `confirm`（默认） | 查询自动放行；任何修改、删除或执行类变更都先询问 |
| `accept_edits` | 查询与普通编辑、构建自动放行；删除、依赖安装、进程终止、Git 提交/推送及其他敏感操作仍询问 |
| `plan` | 只允许读取、只读查询、计划元数据与用户澄清；直接拒绝修改和删除，不弹授权绕过 |
| `full` | 沙盒内所有非灾难性操作自动放行；沙盒外是否询问由沙盒开关决定 |

### 沙盒模式

`/sandbox off`（默认）：访问当前工作目录外需要确认；`/sandbox on`：非凭据路径的沙盒外访问自动放行。沙盒确认支持“允许本次”“拒绝”“本任务内放行沙盒外访问”。任务授权绑定任务 ID，切换界面不会失效，并行任务互不共享；删除任务或重启应用后清除。

### 项目白名单

CLI 与 GUI 共用 `permissions.json` 中的项目白名单。设置页可手动新增、逐条删除或清空；CLI 使用：

```text
/whitelist list
/whitelist add command git status
/whitelist remove command git status
/whitelist add write path/to/file.txt
/whitelist remove write path/to/file.txt
/whitelist clear confirm
```

命令规则按 token 前缀匹配，文件规则只匹配规范化后的精确路径，不递归授权目录。普通权限询问可选择“允许本次”“拒绝”“本任务同类操作放行”或“加入项目白名单”。项目白名单不能越过沙盒边界、计划模式和灾难命令拒绝规则。

### 安全措施
- 灾难性命令硬拒绝（`rm -rf /`、fork bomb、覆写磁盘、`mkfs`、`format` 等），任何模式、授权或白名单均不能放行
- `fetch_url` 内置 SSRF 防护（默认拦截云元数据接口，严格模式开启全 IP 校验）
- 路径穿越防护（任务 ID 格式限制、记忆文件名校验）
- 配置文件原子写 + `.bak` 备份 + 自愈

## 📝 记忆系统

### 短期记忆
- **SQLite 持久化**：聊天记录按会话隔离存储
- **按回合撤销**：回合内 write/edit/download 的原始字节在写入前快照到内容寻址仓库；`/undo` 预检（指纹冲突零写入）→ 一次性令牌 → 逐文件原子恢复；中断事务冻结影响路径并支持继续/取消恢复
- **会话级检查点**：每轮对话前自动创建持久化检查点，`/checkpoint restore <turn_id>` 整轮回滚

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
并默认启用；**已释放且内容未被用户改动过的内置技能，会随新版本自动升级**；
用户改动过的一律保留，用户主动删除的也不会复活。

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

**已内置 / 随附插件**：

| 插件 | 位置 | 功能 |
|------|------|------|
| 混元文生图 | 仓库 `plugins/hunyuan_image/` | 腾讯混元文本生图，数据随包编译进 PYZ；Key 仅从环境变量 `HUNYUAN_API_KEY` 读取，代码中不存储 Key，未配置时工具明确报错 |
| 智能路由 | `~/.ycode/plugins/smart_router/` | 按任务复杂度自动选择合适模型，省 token 且保质量，关闭后不影响原有行为 |
| iMOM 定位器 | `~/.ycode/plugins/imom_locator/` | iMOM/MES 多项目代码定位：动态发现 publish、系统识别、页面索引构建与毫秒级定位 |

> 用户级插件放在 `~/.ycode/plugins/<插件名>/`，同名插件会覆盖内置/仓库版本。

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
│   ├── llm.py               # 模型请求入口与工具编排（自研 HTTPX 引擎）
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
│   │   ├── components/      # 29 个 Vue 组件
│   │   ├── composables/     # 组合式函数（面板拖拽、视口判断等）
│   │   ├── stores/          # Pinia 状态管理
│   │   └── utils/
│   ├── package.json
│   └── vite.config.ts
├── skills/                 # 内置技能示例
├── plugins/                # 内置插件目录
├── vendor/ripgrep/         # 内置 ripgrep
├── packaging/              # 打包辅助
├── tests/                  # pytest 测试用例（117 个文件）
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

### config.toml 只读覆盖层（可选）

除 `config.json` 外，星瑶还支持读取 `~/.ycode/config.toml` 作为**只读覆盖层**，
方便沿用 Codex / Kimi CLI 等工具的 TOML 配置习惯或迁移部分配置片段：

- **读取生效**：`config.toml` 与 `config.json` 深合并后生效，TOML 键优先级高于 JSON
  （最终优先级：命令行参数 > config.toml > config.json > 内置默认）。
- **写回永远落 JSON**：GUI / `/config` 等保存路径只写 `config.json`，绝不生成或改写 TOML；
  若保存的键同时被 TOML 覆盖，会提示"保存成功但实际生效值仍以 TOML 为准"。
- **容错**：TOML 语法错误或读取失败仅警告并回退纯 JSON，绝不影响启动；Python < 3.11 自动忽略。
- **注意取舍**：TOML 值经一次写操作会固化进 `config.json`，此后删除 TOML 不会回退
  （可手动改回 JSON 或删除对应键后重新设置）。

示例 `config.toml`：

```toml
theme = "nord"
permission_mode = "accept_edits"

[memory_compression]
retain_count = 12
```

图片附件最大允许 20MiB，解码后最多 4000 万像素。PNG（含透明通道）保持 PNG，
JPEG/WebP 在必要时逐级降低质量，GIF 保持原始动画；发送给视觉模型时使用与实际编码
一致的 Base64 Data URL。GUI 中的处理结果会固定显示在对应用户消息下方。

### 用户数据目录

`~/.ycode/` 下的文件结构：

| 路径 | 用途 |
|------|------|
| `config.json` | 用户级配置 |
| `config.toml` | 可选只读覆盖层（优先级高于 config.json，见「配置详解」） |
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

测试覆盖 117 个模块，包括 agent_turn_e2e、harness（models/journal/runtime/请求指纹/工具管线）、cancellation、config、memory_kairos、permission_broker、plan、task_scheduler、subagent_registry、tools、setup_engine、uninstall_engine 等。

### 电脑操控人工烟测（绝不进 pytest）

`scripts/smoke_computer_control.py` 只由人工显式运行（会打开可见系统浏览器窗口或产生真实键鼠/截屏动作），自动化测试只验证其代码路径（fake 注入）：

```powershell
# 浏览器通道探针（系统 Edge / Chrome，about:blank，不下载浏览器）
venv\Scripts\python.exe scripts\smoke_computer_control.py --probe-browser edge
venv\Scripts\python.exe scripts\smoke_computer_control.py --browser-observe chrome

# 桌面观察（预检 + 截图一次，不输入）
venv\Scripts\python.exe scripts\smoke_computer_control.py --desktop-observe

# 真实键鼠（固定一次 move/click/type，每步打印脱敏动作并要求单次 Enter 确认；
# 需先打开无敏感数据的测试窗口）
venv\Scripts\python.exe scripts\smoke_computer_control.py --desktop-input --confirm-real-input I_UNDERSTAND
```

人工验收矩阵：Edge/Chrome 专用 profile 首次登录与重启保留、浏览器关闭明确失败、多屏/缩放、pause 后无新动作、resume 首动作 observe、GUI stop / CLI `/computer stop` / Ctrl+C / Ctrl+Alt+Esc 紧急停止、锁屏与 UAC fail-closed、退出后无残留 Playwright 子进程与临时 PNG。

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

## ⚖️ 免责条款

使用本软件即表示你已阅读并同意以下要点（完整条款共 8 章，可在软件内「设置 → 法律 → 免责条款」查看；安装向导与首次启动均会提示确认）：

- **软件性质**：本软件是在你本地设备运行的 AI 编程辅助工具，自身不提供、不运营任何生成式人工智能模型服务；模型服务由你自行选择、配置并直接与其通信。
- **AI 生成内容**：模型基于概率机制生成内容，可能不准确、不完整或已过时；本软件不对其真实性、准确性、完整性、合法性或适用性作任何保证，生成内容亦不构成任何专业领域意见。
- **你的责任**：采纳、执行或分发生成内容前，你应自行审查、验证与测试，并对最终结果负全部责任；软件具备文件读写、命令执行与电脑操控能力，请自行确认影响范围并做好数据备份。
- **数据与隐私**：配置、会话记录与记忆数据默认存储在你的本地设备；使用过程中会话内容将按你的配置直接发送至你所选的模型服务提供者。
- **合规使用**：你不得利用本软件从事违反法律法规的活动，不得生成、传播违法有害信息。
- **责任限制**：**在法律允许的最大范围内**，本软件的著作权人及贡献者不对因使用或无法使用本软件造成的任何损失承担责任（包括但不限于数据丢失、设备损坏、业务中断、利润损失及第三方索赔等直接或间接损失）；本软件按「现状」提供，不作任何明示或默示担保。

## 📄 License

本项目基于 [MIT License](LICENSE) 开源，可自由使用、修改与分发。
