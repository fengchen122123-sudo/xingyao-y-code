---
name: baoyu-format-markdown
description: baoyu-format-markdown 技能：将纯文本或 markdown 文件排版为带 frontmatter、标题、摘要、小标题、加粗、列表和代码块的结构化文档。当用户要求「整理文章」「美化文章」「添加排版」或改进文章版式时使用。输出到 {filename}-formatted.md。
triggers: 整理文章,排版,markdown 格式化,草稿整理,文章结构
version: 1.57.0
metadata:
  openclaw:
    homepage: https://github.com/JimLiu/baoyu-skills#baoyu-format-markdown
    requires:
      anyBins:
        - bun
        - npx
---

# Markdown 排版工具

将纯文本或 markdown 转换为结构清晰、易于阅读的 markdown。目标是帮助读者快速抓住要点、亮点和结构 —— 且不改变任何原有内容。

**核心原则**：只调整排版并修正明显的错别字。绝不新增、删除或改写内容。

## 用户输入工具

当本技能需要向用户提问时，请遵循以下工具选择规则（按优先级排序）：

1. **优先使用内置的用户输入工具** —— 即当前 agent 运行时暴露的工具，例如 `AskUserQuestion`、`request_user_input`、`clarify`、`ask_user` 或任何等价工具。
2. **兜底方案**：如果不存在此类工具，则输出带编号的纯文本消息，请用户针对每个问题回复所选编号/答案。
3. **批量提问**：如果该工具支持一次调用提出多个问题，则把所有适用的问题合并到一次调用中；如果只支持单问题，则按优先级顺序逐个提问。

下文出现的具体 `AskUserQuestion` 仅为示例 —— 在其他运行时中请替换为本地等价工具。

## 脚本目录

脚本位于 `scripts/` 子目录中。`{baseDir}` = 本 SKILL.md 所在的目录路径。解析 `${BUN_X}` 运行时：如果已安装 `bun` → 使用 `bun`；如果 `npx` 可用 → 使用 `npx -y bun`；否则建议安装 bun。请把 `{baseDir}` 和 `${BUN_X}` 替换为实际值。

| 脚本 | 用途 |
|--------|---------|
| `scripts/main.ts` | 主入口，提供 CLI 选项（使用 remark-cjk-friendly 处理中日韩强调） |
| `scripts/quotes.ts` | 将 ASCII 引号替换为全角引号 |
| `scripts/autocorrect.ts` | 通过 autocorrect 添加中英文之间的空格 |

## 偏好设置（EXTEND.md）

按优先级顺序检查 EXTEND.md —— 找到的第一个生效：

| 优先级 | 路径 | 作用域 |
|----------|------|-------|
| 1 | `.baoyu-skills/baoyu-format-markdown/EXTEND.md` | 项目 |
| 2 | `${XDG_CONFIG_HOME:-$HOME/.config}/baoyu-skills/baoyu-format-markdown/EXTEND.md` | XDG |
| 3 | `$HOME/.baoyu-skills/baoyu-format-markdown/EXTEND.md` | 用户主目录 |

如果没有找到任何配置，则使用默认值 —— 本技能无需首次设置。

**EXTEND.md 支持**：

| 设置 | 取值 | 默认值 | 说明 |
|---------|--------|---------|-------------|
| `auto_select` | `true`/`false` | `false` | 同时跳过标题和摘要的选择，自动挑选最佳项 |
| `auto_select_title` | `true`/`false` | `false` | 仅跳过标题选择 |
| `auto_select_summary` | `true`/`false` | `false` | 仅跳过摘要选择 |
| 其他 | — | — | 默认排版选项、字体排版偏好 |

## 使用方法

工作流分为两个阶段：**分析**（理解内容）之后是**排版**（应用排版）。Claude 负责内容分析与排版（步骤 1-5），然后运行脚本进行字体排版修正（步骤 6）。

## 工作流

### 步骤 1：读取并检测内容类型

读取用户指定的文件，然后检测内容类型：

| 判断依据 | 分类 |
|-----------|----------------|
| 含有 `---` YAML frontmatter | Markdown |
| 含有 `#`、`##`、`###` 小标题 | Markdown |
| 含有 `**bold**`、`*italic*`、列表、代码块、引用块 | Markdown |
| 以上均无 | 纯文本 |

**如果检测为 Markdown，使用 `AskUserQuestion` 提问：**

```
Detected existing markdown formatting. What would you like to do?

1. Optimize formatting (Recommended)
   - Analyze content, improve headings, bold, lists for readability
   - Run typography script (spacing, emphasis fixes)
   - Output: {filename}-formatted.md

2. Keep original formatting
   - Preserve existing markdown structure
   - Run typography script only
   - Output: {filename}-formatted.md

3. Typography fixes only
   - Run typography script on original file in-place
   - No copy created, modifies original file directly
```

**根据用户的选择：**
- **优化**：继续步骤 2（完整工作流）
- **保持原样**：跳至步骤 5，复制文件后执行步骤 6
- **仅排版**：跳至步骤 6，直接在原文件上运行

### 步骤 2：分析内容（读者视角）

仔细通读全部内容。站在读者角度思考：什么能帮助他们快速理解并记住关键信息？

产出一份涵盖以下维度的分析：

**2.1 亮点与关键洞见**
- 作者提出的核心论点或结论
- 令人意外的事实、数据点或反直觉的断言
- 令人印象深刻的引语或表述精妙的句子（金句）

**2.2 结构评估**
- 内容是否有清晰的逻辑脉络？它是什么？
- 是否存在缺少小标题的天然分段边界？
- 是否存在大段密集文字、可以通过视觉分隔来改善？

**2.3 对读者重要的信息**
- 可操作的建议或要点
- 关键概念的定义与解释
- 埋没在行文中的列表或枚举
- 改成表格会更清晰的对比或对照

**2.4 排版问题**
- 小标题层级缺失或不一致
- 混杂了多个主题的段落
- 写成散文而非列表的并列项
- 未标记为代码的代码、命令或技术术语
- 明显的错别字或排版错误

**将分析保存到文件**：`{original-filename}-analysis.md`

分析文件是步骤 3 的蓝图。使用以下格式：

```markdown
# Content Analysis: {filename}

## Highlights & Key Insights
- [list findings]

## Structure Assessment
- Current flow: [describe]
- Suggested sections: [list heading candidates with brief rationale]

## Reader-Important Information
- [list actionable items, key concepts, buried lists, potential tables]

## Formatting Issues
- [list specific issues with location references]

## Typos Found
- [list any obvious typos with corrections, or "None found"]
```

### 步骤 3：检查/创建 Frontmatter、标题与摘要

检查 YAML frontmatter（`---` 块）。若缺失则创建。

| 字段 | 处理方式 |
|-------|------------|
| `title` | 见下方 **标题生成** |
| `slug` | 从文件路径推断，或根据标题生成 |
| `summary` | 一句话的精炼摘要（见下方 **摘要生成**） |
| `description` | 更长的描述性摘要（见下方 **摘要生成**） |
| `coverImage` | 检查同目录下是否存在 `imgs/cover.png`；若存在，使用相对路径 |

#### 标题生成

无论标题是否已存在，除非设置了 `auto_select_title`，都要运行标题优化流程。

**准备** —— 通读全文并提取：
- 核心论点（一句话：「这篇文章讲的是什么？」）
- 最具冲击力的观点或结论
- 读者的痛点或好奇点
- 最令人印象深刻的比喻或金句

**生成候选标题**，使用 `references/title-formulas.md` 中的公式：

1. 根据文章的内容、语气和结构，选择**最匹配的 2-3 个钩子公式**（参见参考文档中的「何时选用各个公式」）
2. 生成**1-2 个朴实直白的标题**（描述性或陈述性，不使用公式 —— 清晰准确）
3. 如果用户指定了方向（例如「写得有悬念一些」），优先该方向
4. 总计：**4-5 个候选标题**

通过 `AskUserQuestion` 呈现：

```
Pick a title:

1. [Hook title A] — (recommended) [formula name]
2. [Hook title B] — [formula name]
3. [Hook title C] — [formula name]
4. [Straightforward title D] — straightforward
5. [Straightforward title E] — straightforward

Enter number, or type a custom title:
```

把最强的钩子标题放在第一位，并标记为 `(recommended)`。原则与禁止模式参见 `references/title-formulas.md`。

如果首行是 H1，将其提取到 frontmatter 并从正文中移除。如果 frontmatter 已有 `title`，将其作为上下文纳入，但仍要生成全新的候选标题 —— 现有标题可能不够好。

**跳过行为**：如果 `auto_select: true` 或 `auto_select_title: true`，跳过用户提问，直接使用排名第一的候选标题。

#### 摘要生成

直接生成两个版本（无需用户选择），都存入 frontmatter：

| 字段 | 长度 | 用途 |
|-------|--------|---------|
| `summary` | 1 句话，约 50-80 字符 | 精炼的钩子 —— 用于信息流、社交分享、SEO 元描述 |
| `description` | 2-3 句话，约 100-200 字符 | 更丰富的背景 —— 用于文章预览、邮件简报摘要 |

**原则**：

- 向读者传达**核心价值**，而不只是主题
- 使用具体细节（数字、结果、具体方法），而非空泛的描述
- `summary` 应当有力且自成一体；`description` 可以展开补充支撑细节
- 如果 frontmatter 已有 `summary` 或 `description`，保留原有的，只生成缺失的字段

**禁止的模式**：

- 「本文介绍了……」「本文探讨了……」
- 只描述主题、不含价值主张
- 用不同的措辞重复标题

标题进入 frontmatter 后，正文就不应再包含 H1（避免重复）。

### 步骤 4：排版内容

依据步骤 2 的分析进行排版。目标是让内容易于扫读、让重点无处可藏。

**排版工具箱：**

| 元素 | 何时使用 | 格式 |
|---------|-------------|--------|
| 小标题 | 天然的主题边界、分段处 | `##`、`###` 层级 |
| 加粗 | 关键结论、重要术语、核心要点 | `**bold**` |
| 无序列表 | 并列项、特性清单、示例 | `- item` |
| 有序列表 | 先后步骤、排名项、流程 | `1. item` |
| 表格 | 对比、结构化数据、选项矩阵 | Markdown 表格 |
| 代码 | 命令、文件路径、技术术语、变量名 | `` `inline` `` 或围栏代码块 |
| 引用块 | 值得注意的引语、重要警告、引证文本 | `> quote` |
| 分隔线 | 重大主题转换 | `---` |

**排版原则 —— 不该做什么：**
- 不要新增句子、解释或评论
- 不要删除或缩短任何内容
- 不要改写或重述作者的原话
- 不要添加带主观评价的小标题（例如「惊人的发现」—— 应使用中性的描述性小标题）
- 不要过度排版：不是每句话都需要加粗，不是每个段落都需要小标题

**排版原则 —— 该做什么：**
- 保留作者的语态、语气和每一个字
- **加粗关键结论和核心要点** —— 也就是读者会划重点的句子
- 仅当结构确实清晰时，才把行文中并列的内容提取为列表
- 在主题真正转换处添加小标题 —— 优先使用生动、具体的小标题而非空泛的（例如「3 天搞定 vs 传统方案」优于「方案对比」）
- 对埋没在行文中的对比或结构化数据使用表格
- 对金句、令人印象深刻的表述或重要警告使用引用块
- 修正明显的错别字（依据步骤 2 的发现）

### 步骤 5：保存排版后的文件

保存为 `{original-filename}-formatted.md`

**备份已有文件：**

```bash
if [ -f "{filename}-formatted.md" ]; then
  mv "{filename}-formatted.md" "{filename}-formatted.backup-$(date +%Y%m%d-%H%M%S).md"
fi
```

### 步骤 6：执行字体排版脚本

对输出文件运行排版脚本：

```bash
${BUN_X} {baseDir}/scripts/main.ts {output-file-path} [options]
```

**脚本选项：**

| 选项 | 简写 | 说明 | 默认值 |
|--------|-------|-------------|---------|
| `--quotes` | `-q` | 将 ASCII 引号替换为全角引号 `"..."` | false |
| `--no-quotes` | | 不替换引号 | |
| `--spacing` | `-s` | 通过 autocorrect 添加中英文之间的空格 | true |
| `--no-spacing` | | 不添加中英文之间的空格 | |
| `--emphasis` | `-e` | 修复中日韩强调标点问题 | true |
| `--no-emphasis` | | 不修复中日韩强调问题 | |

**示例：**

```bash
# Default: spacing + emphasis enabled, quotes disabled
${BUN_X} {baseDir}/scripts/main.ts article.md

# Enable all features including quote replacement
${BUN_X} {baseDir}/scripts/main.ts article.md --quotes

# Only fix emphasis issues, skip spacing
${BUN_X} {baseDir}/scripts/main.ts article.md --no-spacing
```

**脚本执行内容（取决于选项）：**
1. 修复中日韩强调/加粗的标点问题（默认：启用）
2. 通过 autocorrect 为中英混排文本添加空格（默认：启用）
3. 将 ASCII 引号替换为全角引号（默认：禁用）
4. 格式化 frontmatter YAML（始终启用）

### 步骤 7：完成报告

显示一份总结所有已做修改的报告：

```
**Formatting Complete**

**Files:**
- Analysis: {filename}-analysis.md
- Formatted: {filename}-formatted.md

**Content Analysis Summary:**
- Highlights found: X key insights
- Golden quotes: X memorable sentences
- Formatting issues fixed: X items

**Changes Applied:**
- Frontmatter: [added/updated] (title, slug, summary)
- Headings added: X (##: N, ###: N)
- Bold markers added: X
- Lists created: X (from prose → list conversion)
- Tables created: X
- Code markers added: X
- Blockquotes added: X
- Typos fixed: X [list each: "original" → "corrected"]

**Typography Script:**
- CJK spacing: [applied/skipped]
- Emphasis fixes: [applied/skipped]
- Quote replacement: [applied/skipped]
```

根据实际修改调整报告内容 —— 若某一类修改未发生，则省略对应类别。

## 注意事项

- 保留原有的写作风格与语气
- 为代码块指定正确的语言（例如 `python`、`javascript`）
- 保持中英文之间空格的标准
- 分析文件是一份工作文档 —— 它有助于保持「已识别的问题」与「已排版的结果」之间的一致性

## 扩展支持

通过 EXTEND.md 进行自定义配置。路径与支持的选项见 **偏好设置** 部分。
