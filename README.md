# Y-code（星瑶）

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

> 一个运行在终端里的 AI 编程助手，代号「星瑶」。支持多模型切换、文件操作、命令执行、联网搜索、会话记忆与可扩展技能。

---

## ✨ 功能亮点

- **终端交互界面**：基于 `prompt_toolkit` 的 Claude Code 风格输入框，支持斜杠命令、Tab 补全、`@文件` 快速挂载。
- **多模型管理**：通过 `/models` 打开全屏模型选择器，支持 OpenAI / Anthropic / DeepSeek / Agnes 等多种协议，由 `LiteLLM` 统一调用。
- **工具调用（Tools）**：
  - `read_file` 读取文件
  - `write_file` 写入/修改文件（自动渲染 Diff）
  - `list_directory` 扫描目录
  - `search_code` 跨文件代码搜索
  - `execute_command` 执行本地命令
  - `search_news` 联网新闻搜索
- **会话记忆**：基于 SQLite 持久化聊天记录，支持多会话切换与 `/undo` 撤销上一次文件修改。
- **文件挂载**：输入 `@文件名` 或粘贴绝对路径，自动把文件内容（含图片 base64）送入上下文。
- **Token 监控**：实时显示当前窗口占用、累计消耗与缓存命中率。
- **技能系统（Skills）**：扫描 `SKILL.md` 并动态注入系统提示词，支持项目级与用户级技能。
- **MCP 客户端**：预留 `mcp.json` 配置，可接入 MCP Servers。

---

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

> `setup.py` 已声明依赖：`rich`、`litellm`、`pytest`、`prompt_toolkit`、`pyfiglet`。运行时如需联网搜索，请额外安装 `ddgs`：
> ```bash
> pip install ddgs
> ```

### 3. 配置 API 密钥

**⚠️ 安全提醒：仓库内不会提交真实密钥，请把示例配置复制为正式配置后填写。**

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
      "base_url": "https://apihub.agnes-ai.com/v1",
      "api_key": "sk-your-real-api-key-here",
      "protocol": "openai",
      "models": [
        {
          "id": "model_1",
          "name": "agnes-2.0-flash",
          "litellm_model": "openai/agnes-2.0-flash",
          "context_window": 128000
        }
      ]
    }
  ]
}
```

### 4. 启动星瑶

```bash
ycode
```

或直接从源码运行：

```bash
python main.py
```

---

## ⌨️ 常用指令

在输入框中输入以下斜杠命令：

| 命令 | 说明 |
|------|------|
| `/help` | 查看帮助 |
| `/models` | 打开模型选择器 |
| `/session <名称>` | 切换或创建会话 |
| `/sessions` | 打开会话选择器 |
| `/skills` | 查看/安装技能插件 |
| `/undo` | 撤销上一次文件修改 |
| `/clear` | 清屏 |
| `/exit` | 退出 |

### 快捷键

- `Enter`：发送消息
- `Alt + Enter`：输入框内换行
- `Ctrl + C`：清空输入框
- `Ctrl + D`：退出程序

---

## 📁 项目结构

```
Y-code_tui/
├── main.py                 # 主入口，TUI 与后台工作线程
├── config.py               # 配置加载（优先读取 config.json）
├── config.example.json     # 配置模板（可安全提交）
├── setup.py                # 包安装配置
├── core/                   # 核心模块
│   ├── llm.py              # LiteLLM 调用与工具编排
│   ├── tools.py            # 本地工具实现
│   ├── memory.py           # SQLite 会话记忆
│   ├── input_parser.py     # 用户输入与文件挂载解析
│   ├── model_manager.py    # 多模型配置管理
│   ├── model_selector.py   # 全屏模型选择器
│   ├── skill_selector.py   # 技能选择器
│   ├── session_selector.py # 会话选择器
│   ├── skills.py           # SKILL.md 扫描与注入
│   ├── mcp_client.py       # MCP 客户端
│   └── diff_viewer.py      # Diff 渲染
├── skills/                 # 内置技能示例
├── tests/                  # pytest 测试用例
├── .gitignore              # 忽略密钥、数据库、缓存等
└── README.md               # 本文件
```

---

## 🧪 运行测试

```bash
pytest
```

---

## 🔒 安全与隐私

- `config.json`、`ycode.db`、`active_skills.json`、`mcp.json` 已加入 `.gitignore`，不会被提交。
- 永远不要把真实 API 密钥写进源码或 README。
- 如果意外把密钥提交到仓库，请立即到对应平台重置密钥。

---

## 📝 技能扩展

在以下任一位置创建 `SKILL.md`，即可被星瑶加载：

- 项目级：`.agents/skills/<skill-name>/SKILL.md`
- 用户级：`~/.agents/skills/<skill-name>/SKILL.md` 或 `~/.ycode/skills/<skill-name>/SKILL.md`

技能文件支持 YAML frontmatter：

```markdown
---
name: my-skill
description: 一个示例技能
---

这里是技能的详细说明，会被注入到系统提示词中。
```

---

## 🤝 贡献

欢迎提交 Issue 或 Pull Request：

- 提交 Issue：<https://gitee.com/feng-chenhao/xingyao-y-code/issues>
- 提交 PR 前请先本地跑通 `pytest`，并遵循 `AGENTS.md` 中的开发规范
- 如果你发现代码里有敏感信息泄露，请第一时间通过 Issue 提醒维护者

---

## 📄 License

本项目基于 [MIT License](LICENSE) 开源，可自由使用、修改与分发。
