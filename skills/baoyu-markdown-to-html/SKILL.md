---
name: baoyu-markdown-to-html
description: 将 Markdown 转换为带微信兼容主题的精美 HTML。支持代码高亮、数学公式、Mermaid（经无头 Chrome 渲染为 PNG）、PlantUML、脚注、提示块、信息图，以及可选的普通外链底部引用。适用于用户提出 "markdown to html"、"convert md to html"、"md 转 html"、"微信外链转底部引用"，或需要由 markdown 输出带样式 HTML 的场景。技能名 baoyu-markdown-to-html。
triggers: markdown 转 html,公众号排版,html 转换
version: 1.117.3
metadata:
  openclaw:
    homepage: https://github.com/JimLiu/baoyu-skills#baoyu-markdown-to-html
    requires:
      anyBins:
        - bun
        - npx
---

# Markdown 转 HTML 转换器

将 Markdown 文件转换为内联 CSS 的精美 HTML，针对微信公众号及其他平台优化。

## 用户输入工具

当本技能需要向用户提问时，遵循以下工具选择规则（按优先级排序）：

1. **优先使用当前 Agent 运行时内置的用户输入工具** —— 例如 `AskUserQuestion`、`request_user_input`、`clarify`、`ask_user` 或任何等价工具。
2. **兜底方案**：如果不存在这类工具，则输出带编号的纯文本消息，请用户针对每个问题回复所选编号/答案。
3. **批量提问**：如果该工具支持一次调用提出多个问题，则把所有适用问题合并为一次调用；如果只支持单题，则按优先级顺序逐个提问。

下文中出现的具体 `AskUserQuestion` 均为示例 —— 在其他运行时中请替换为本地等价工具。

## 脚本目录

**Agent 执行**：将本 SKILL.md 所在目录确定为 `{baseDir}`。解析 `${BUN_X}` 运行时：若已安装 `bun` → `bun`；若 `npx` 可用 → `npx -y bun`；否则建议安装 bun。将 `{baseDir}` 和 `${BUN_X}` 替换为实际值。

| 脚本 | 用途 |
|--------|---------|
| `scripts/main.ts` | 主入口 |

## 偏好设置（EXTEND.md）

按优先级顺序检查 EXTEND.md —— 第一个找到的生效：

| 优先级 | 路径 | 作用范围 |
|----------|------|-------|
| 1 | `.baoyu-skills/baoyu-markdown-to-html/EXTEND.md` | 项目 |
| 2 | `${XDG_CONFIG_HOME:-$HOME/.config}/baoyu-skills/baoyu-markdown-to-html/EXTEND.md` | XDG |
| 3 | `$HOME/.baoyu-skills/baoyu-markdown-to-html/EXTEND.md` | 用户主目录 |

如果都没有找到，则使用默认值。

**EXTEND.md 支持**：默认主题、自定义 CSS 变量、代码块样式、mermaid 默认值（`mermaid_theme`、`mermaid_scale`、`mermaid_background`）。

## 工作流

### Step 0：预检查（中文内容）

**条件**：仅当输入文件包含中文文本时才执行。

**检测**：
1. 读取输入的 markdown 文件
2. 检查内容是否包含 CJK 字符（中文/日文/韩文）
3. 如果没有 CJK 内容 → 跳到 Step 1

**格式建议**：

如果检测到 CJK 内容，且 `baoyu-format-markdown` 技能可用：

使用 `AskUserQuestion` 询问是否先进行格式化。格式化可以修复：
- 加粗标记内含标点导致的 `**` 解析失败
- 中英文间距问题

**如果用户同意**：调用 `baoyu-format-markdown` 技能格式化该文件，然后以格式化后的文件作为输入。

**如果用户拒绝**：继续使用原始文件。

### Step 1：确定主题

**主题解析顺序**（第一个匹配的生效）：
1. 用户显式指定的主题（CLI `--theme` 或对话中说明）
2. EXTEND.md 的 `default_theme`（本技能自己的 EXTEND.md，已在 Step 0 检查）
3. `baoyu-post-to-wechat` 的 EXTEND.md 中的 `default_theme`（跨技能兜底）
4. 如果都没有找到 → 使用 AskUserQuestion 确认

**跨技能 EXTEND.md 检查**（仅当本技能的 EXTEND.md 中没有 `default_theme` 时）：

如果 `$HOME/.baoyu-skills/baoyu-post-to-wechat/EXTEND.md` 存在，则读取它并查找 `default_theme:` 行。如果存在则使用该值；否则继续向下查找。

**如果主题由 EXTEND.md 解析得到**：直接使用，不要询问用户。

**如果未找到默认主题**：使用 `AskUserQuestion` 从下方的 [主题](#themes) 表格中确认一个主题。

### Step 1.5：确定引用模式

**默认**：关闭。默认情况下不询问。

**仅当用户显式要求** "微信外链转底部引用"、"底部引用"、"文末引用"，或传入 `--cite` 时才启用。

**启用后的行为**：
- 普通外链会被渲染为带编号的上标，并收集到末尾的 `引用链接` 小节中。
- `https://mp.weixin.qq.com/...` 链接保持为直接链接，不会被移到文末。
- 链接文字与 URL 相同的裸链接保持内联。

### Step 2：转换

```bash
${BUN_X} {baseDir}/scripts/main.ts <markdown_file> --theme <theme> [--cite]
```

### Step 3：报告结果

显示 JSON 结果中的输出路径。如果创建了备份，请一并提及。

## 用法

```bash
${BUN_X} {baseDir}/scripts/main.ts <markdown_file> [options]
```

**选项：**

| 选项 | 说明 | 默认值 |
|--------|-------------|---------|
| `--theme <name>` | 主题名（default、grace、simple、modern） | default |
| `--color <name\|hex>` | 主色：预设名称或十六进制值 | 主题默认值 |
| `--font-family <name>` | 字体：sans、serif、serif-cjk、mono 或 CSS 值 | 主题默认值 |
| `--font-size <N>` | 字号：14px、15px、16px、17px、18px | 16px |
| `--title <title>` | 覆盖 frontmatter 中的标题 | |
| `--cite` | 将外链转换为底部引用，并追加 `引用链接` 小节 | false（关闭） |
| `--keep-title` | 保留内容中的第一个标题 | false（移除） |
| `--mermaid-theme <name>` | Mermaid 主题：`default`、`forest`、`dark`、`neutral`、`base` | default |
| `--mermaid-scale <N>` | Mermaid 渲染缩放倍数（正数且 ≤ 4） | 2 |
| `--mermaid-width <N>` | Mermaid 目标显示宽度（单位 CSS 像素）；当图表宽度小于该值时，PNG 按 `width × scale` 像素渲染 | 860 |
| `--mermaid-bg <value>` | Mermaid 背景：`white`、`transparent` 或 `#hex` | white |
| `--no-mermaid` | 跳过 Mermaid PNG 渲染；改用 `<pre class="mermaid">` 兜底 | false |
| `--help` | 显示帮助 | |

**颜色预设：**

| 名称 | 十六进制 | 标签 |
|------|-----|-------|
| blue | #0F4C81 | 经典蓝 |
| green | #009874 | 翡翠绿 |
| vermilion | #FA5151 | 明快朱红 |
| yellow | #FECE00 | 柠檬黄 |
| purple | #92617E | 薰衣草紫 |
| sky | #55C9EA | 天蓝 |
| rose | #B76E79 | 玫瑰金 |
| olive | #556B2F | 橄榄绿 |
| black | #333333 | 石墨黑 |
| gray | #A9A9A9 | 烟灰 |
| pink | #FFB7C5 | 樱花粉 |
| red | #A93226 | 中国红 |
| orange | #D97757 | 暖橙（modern 主题默认） |

**示例：**

```bash
# Basic conversion (uses default theme, removes first heading)
${BUN_X} {baseDir}/scripts/main.ts article.md

# With specific theme
${BUN_X} {baseDir}/scripts/main.ts article.md --theme grace

# Theme with custom color
${BUN_X} {baseDir}/scripts/main.ts article.md --theme modern --color red

# Enable bottom citations for ordinary external links
${BUN_X} {baseDir}/scripts/main.ts article.md --cite

# Keep the first heading in content
${BUN_X} {baseDir}/scripts/main.ts article.md --keep-title

# Override title
${BUN_X} {baseDir}/scripts/main.ts article.md --title "My Article"
```

## 输出

**文件位置**：与输入 markdown 文件所在目录相同。
- 输入：`/path/to/article.md`
- 输出：`/path/to/article.html`

**冲突处理**：如果 HTML 文件已存在，会先进行备份：
- 备份：`/path/to/article.html.bak-YYYYMMDDHHMMSS`

**输出到 stdout 的 JSON：**

```json
{
  "title": "Article Title",
  "author": "Author Name",
  "summary": "Article summary...",
  "htmlPath": "/path/to/article.html",
  "backupPath": "/path/to/article.html.bak-20260128180000",
  "contentImages": [
    {
      "placeholder": "MDTOHTMLIMGPH_1",
      "localPath": "/path/to/img.png",
      "originalPath": "imgs/image.png"
    }
  ],
  "mermaidImages": [
    {
      "hash": "a1b2c3d4e5f6",
      "localPath": "/path/to/imgs/.mermaid-cache/mermaid-a1b2c3d4e5f6.png",
      "cached": false
    }
  ]
}
```

**Mermaid 渲染**：以 ` ```mermaid ` 围栏标记的代码块会经无头 Chrome（CDP）渲染为 PNG，并缓存到 `imgs/.mermaid-cache/mermaid-<hash>.png`。缓存键包含代码内容、主题、缩放倍数、目标宽度、背景和 mermaid 版本。如果不希望生成的图表被提交入库，请把 `imgs/.mermaid-cache/` 加入 `.gitignore`。这需要系统上安装 Chrome/Chromium/Edge；否则该代码块会回退为 `<pre class="mermaid">…</pre>`，转换仍然可以成功。

## 主题

| 主题 | 说明 |
|-------|-------------|
| `default` | 经典 —— 传统版式，标题居中并带底边框，H2 为彩色底白字 |
| `grace` | 优雅 —— 文字阴影、圆角卡片、精致引用块（by @brzhang） |
| `simple` | 简约 —— 现代极简风，非对称圆角，留白干净（by @okooo5km） |
| `modern` | 现代 —— 大圆角、胶囊形标题、舒展行高（搭配 `--color red` 可得传统红金风格） |

## 支持的 Markdown 特性

| 功能 | 语法 |
|---------|--------|
| 标题 | `# H1` 至 `###### H6` |
| 粗体/斜体 | `**bold**`、`*italic*` |
| 代码块 | ` ```lang ` 带语法高亮 |
| 行内代码 | `` `code` `` |
| 表格 | GitHub 风格的 markdown 表格 |
| 图片 | `![alt](src)` |
| 链接 | `[text](url)`；加上 `--cite` 可将普通外链移入底部引用 |
| 引用块 | `> quote` |
| 列表 | `-` 无序，`1.` 有序 |
| 提示块（Alerts） | `> [!NOTE]`、`> [!WARNING]` 等 |
| 脚注 | `[^1]` 引用 |
| 注音文本（Ruby） | `{base|annotation}` |
| Mermaid | ` ```mermaid ` 代码块经无头 Chrome 渲染为本地 PNG（缓存于 `imgs/.mermaid-cache/`）；若 Chrome 不可用或渲染失败则回退为 `<pre class="mermaid">` |
| PlantUML | ` ```plantuml ` 图表 |

## Frontmatter（前置元数据）

支持使用 YAML frontmatter 记录元数据：

```yaml
---
title: Article Title
author: Author Name
description: Article summary
---
```

如果没有找到 title，则从第一个 H1/H2 标题中提取，或使用文件名。

## 扩展支持

通过 EXTEND.md 进行自定义配置。路径与支持的选项参见 **偏好设置** 章节。
