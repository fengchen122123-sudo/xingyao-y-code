---
name: baoyu-article-illustrator
description: 分析文章结构，判定需要配图的位置，以 Type × Style × Palette 三维方式为文章生成插图。当用户提出“为文章配图”“给文章加图”“为文章生成图片”或“illustrate article”等需求时使用。
triggers: 文章配图,插图,配图,公众号配图,文章插图
version: 1.117.4
metadata:
  openclaw:
    homepage: https://github.com/JimLiu/baoyu-skills#baoyu-article-illustrator
---

# 文章配图

分析文章，判定插图位置，以 Type × Style × Palette 一致的方式生成图片。

## 用户输入工具

当本技能需要向用户提问时，遵循以下工具选择规则（按优先级排序）：

1. **优先使用内置的用户输入工具** —— 即当前 agent 运行时暴露的工具，例如 `AskUserQuestion`、`request_user_input`、`clarify`、`ask_user` 或任何等价工具。
2. **回退方案**：若不存在此类工具，则输出带编号的纯文本消息，并要求用户针对每个问题回复所选编号/答案。
3. **批量提问**：若该工具支持一次调用提交多个问题，则把所有适用问题合并为一次调用；若仅支持单问题，则按优先级顺序逐个提问。

下文出现的具体 `AskUserQuestion` 只是示例 —— 在其他运行时中请替换为本地等价工具。

## 图像生成工具

当本技能需要渲染图片时，按以下顺序解析后端：

1. **当前请求覆盖** —— 若用户在当前消息中点名了特定后端，则使用它。
2. **已保存的偏好** —— 若 `EXTEND.md` 把 `preferred_image_backend` 设为一个当前可用的后端，则使用它。
3. **自动选择**（当偏好为 `auto`、未设置，或所固定的后端不可用时）：
   - **Y-code（`hunyuan_generate_image` / `tokenrhythm_generate_image`）** —— 若你的工具清单中暴露了 `hunyuan_generate_image` 或 `tokenrhythm_generate_image`，说明你正运行在 Y-code 运行时中，必须使用其中之一作为位图后端；它优先于任何非原生技能，且此处有意未安装 `baoyu-image-gen`。默认使用 `hunyuan_generate_image`；当用户在当前请求中点名 `tokenrhythm_generate_image`、需要更接近 3:4 的竖版尺寸，或希望一次调用得到 2-4 个候选图时，改用 `tokenrhythm_generate_image`。把已保存的 prompt 文件内容作为 `prompt` 传入，把技能预期的输出目录作为 `save_dir` 传入。参数映射、尺寸映射、锚点图链路（通过 `image` 引用参数）以及批量规则见 [references/ycode-imagegen.md](references/ycode-imagegen.md) —— 首次渲染之前请先加载该文件。
   - **Codex（`imagegen`）** —— 首先检查你的可用技能 / 工具清单。若其中列有名为 `imagegen` 的技能，说明你正运行在 Codex 中，必须使用它：通过 `Skill` 工具以 `skill: "imagegen"` 调用，并传入已保存的 prompt 文件内容（以及输出路径和宽高比，遵循 Codex `imagegen` 自身的参数）。Codex `imagegen` 是该运行时的官方位图后端，优先于任何非原生技能（例如 `baoyu-image-gen`），除非用户明确固定了另一个 `preferred_image_backend`。
   - **通过 `codex exec` 使用 Codex（`codex-imagegen`）** —— 若当前运行时没有原生 `imagegen` 技能，但 `codex` CLI 已在 `PATH` 上且 `codex login` 处于有效状态，则经由 `baoyu-image-gen --provider codex-cli` 路由（首选），或者——若 baoyu-image-gen 不可用——直接调用内置 wrapper。细节、参数与运行时发现流程见 [references/codex-imagegen.md](references/codex-imagegen.md) —— 仅当选中该分支时才加载该文件。
   - **Cursor（`GenerateImage`）** —— 若运行时暴露了原生 `GenerateImage` 工具，说明你正运行在 Cursor 中，它与 Codex `imagegen` 一样优先于任何非原生技能。有两个硬性注意点：(a) 它没有宽高比参数 —— 需在作为 `description` 传入的 prompt 文本中明确写出目标宽高比 / 尺寸；(b) 它不接受输出目录 —— 会保存到工具管理的位置，因此生成后要把文件复制/移动到技能预期的输出路径（例如 `outputs/.../NN-xxx.png`）。参考图放在 `reference_image_paths`。
   - **其他运行时原生工具** —— 若运行时暴露了不同的原生图像工具（例如 Hermes `image_generate`），按同样方式使用。
   - 否则，若恰好只安装了一个非原生后端（例如 `baoyu-image-gen`），则使用它。
   - 否则（存在多个非原生后端且没有运行时原生工具），向用户询问一次 —— 与其他初始问题一并批量提问。
4. **若都不可用**，告知用户并询问如何继续。

**⛔ 绝不可用 SVG、HTML、canvas 或其他基于代码的渲染来替代位图图像生成。** Codex `imagegen` 自身的描述指出，应在“输出应当是位图素材而非仓库原生代码或矢量图”时使用它。若你无法通过步骤 3 解析出位图后端，请进入步骤 4 并向用户询问 —— **不要**默默输出 SVG、写入内联 `<svg>` 标记，或用 HTML/CSS 画图作为替代。即便某篇文章/章节看上去“像示意图”也一样：调用本规则的消费方技能已经确定它需要的就是位图。

**⛔ 绝不可通过在已生成的位图上涂抹来修复渲染出的文字。** 不要使用 ImageMagick、Pillow、Canvas、SVG、HTML/CSS、OCR 脚本或任何其他程序化叠加手段，去覆盖、改写、擦除、描边或替换已生成插图内部的标签、说明文字或任何其他文字。若文字有误或不清晰，请用修正后的 prompt 重新生成、改用更少或完全不在图上放文字的绘制方式，或询问用户保留哪张不完美的候选图。

设置 `preferred_image_backend: ask` 会在每次运行时强制执行步骤 3 的询问，无论有哪些可用后端。用户可通过下方 `## Changing Preferences` 小节更改所固定的后端。

**Prompt 文件要求（硬性）**：在调用任何后端之前，把每张图的完整最终 prompt 写入 `prompts/` 下的独立文件（命名：`NN-{type}-[slug].md`）。后端接收的是该 prompt 文件（或其内容）；该文件是可复现记录，也让你在切换后端时无需重新生成 prompt。

上文的具体工具名（`imagegen`、`GenerateImage`、`image_generate`、`baoyu-image-gen`）只是示例 —— 请在同一规则下替换为本地等价工具。

## 批量生成策略

本次运行的所有 prompt 文件都保存并校验完成后，默认以批量方式生成图片。

优先级顺序：

1. 若所选后端有原生批量 / 多任务接口，则使用它。每个任务必须保留各自的 prompt 文件、输出路径、宽高比和直接参考图。
2. 若无原生批量接口，但运行时可以发起并行工具调用，则一次最多派发 `generation_batch_size` 张图。默认值：`4`。当前消息中用户的明确要求，例如 `--batch-size 4` 或“并行4张一起生成”，会覆盖 EXTEND.md。
3. 若既无原生批量、也不支持并行工具调用，则串行生成。

规则：

- 在该批次的所有 prompt 文件都已落盘之前，绝不开始第一批。
- 失败项重试一次，且不重新生成已成功的项。
- 不要仅仅为了并行渲染图片而使用子代理。子代理只用于独立的 prompt 迭代或创意探索。

## 确认策略

默认行为：**生成前先确认**。

- 显式调用技能、提供文件路径、命中的信号/预设，以及 `EXTEND.md` 默认值，都只应视为**推荐输入**。它们中的任何一个都不构成跳过确认的授权。
- 在用户完成步骤 3 之前，**不要**开始步骤 4 及之后的内容。
- 仅当当前请求明确表示可以跳过时才跳过确认，例如：“直接生成”“不用确认”“跳过确认”“按默认出图”或等价措辞。
- 若确认被明确跳过，则在生成前于下一次面向用户的更新中说明所假定的 type / density / style / palette / language / backend。

## 参考图

用户可以通过 `--ref <files...>` 提供参考图，也可以在对话中给出文件路径 / 粘贴图片。参考图用于指导特定插图的风格、配色、构图或主体。

完整的检测、存储与处理规则见 [references/workflow.md](references/workflow.md)（步骤 1.0 保存到 `references/NN-ref-{slug}.{ext}`；步骤 5.3 按插图处理用法 `direct | style | palette`）。当所选后端支持批量输入时，每个 prompt 文件 `references:` frontmatter 中用法为 `direct` 的条目应写入其批量载荷，以便后端透传（例如 `baoyu-image-gen` 支持每个任务传入 `ref`）。

## 三个维度

| 维度 | 控制项 | 示例 |
|-----------|----------|----------|
| **Type** | 信息结构 | infographic, scene, flowchart, comparison, framework, timeline |
| **Style** | 渲染方式 | notion, warm, minimal, blueprint, watercolor, elegant |
| **Palette** | 配色方案（可选） | macaron, warm, neon —— 覆盖 style 的默认配色 |

可自由组合：`--type infographic --style vector-illustration --palette macaron`

或使用预设：`--preset edu-visual` 一个参数即同时指定 type + style + palette。参见 [Style Presets](references/style-presets.md)。

## 类型

| Type | 适用场景 |
|------|----------|
| `infographic` | 数据、指标、技术 |
| `scene` | 叙事、情感 |
| `flowchart` | 流程、工作流 |
| `comparison` | 并列对比、方案选择 |
| `framework` | 模型、架构 |
| `timeline` | 历史、演进 |

## 风格

核心风格、完整风格库以及 Type × Style 兼容性见 [references/styles.md](references/styles.md)。

## 工作流程

```
- [ ] Step 1: Pre-check (EXTEND.md, references, config)
- [ ] Step 2: Analyze content
- [ ] Step 3: Confirm settings (AskUserQuestion)
- [ ] Step 4: Generate outline
- [ ] Step 5: Generate images
- [ ] Step 6: Finalize
```

### 步骤 1：预检查

**1.5 加载偏好设置（EXTEND.md）⛔ 阻塞**

按优先级顺序检查 EXTEND.md —— 找到的第一个即生效：

| 优先级 | 路径 | 作用域 |
|----------|------|-------|
| 1 | `.baoyu-skills/baoyu-article-illustrator/EXTEND.md` | 项目 |
| 2 | `${XDG_CONFIG_HOME:-$HOME/.config}/baoyu-skills/baoyu-article-illustrator/EXTEND.md` | XDG |
| 3 | `$HOME/.baoyu-skills/baoyu-article-illustrator/EXTEND.md` | 用户主目录 |

| 结果 | 操作 |
|--------|--------|
| 找到 | 读取、解析、展示摘要 |
| 未找到 | ⛔ 执行 [first-time-setup](references/config/first-time-setup.md) |

完整流程：[references/workflow.md](references/workflow.md#step-1-pre-check)

### 步骤 2：分析

| 分析 | 输出 |
|----------|--------|
| 内容类型 | Technical / Tutorial / Methodology / Narrative |
| 目的 | information / visualization / imagination |
| 核心论点 | 2-5 个要点 |
| 位置 | 插图能带来价值的地方 |

**关键**：隐喻 → 可视化其背后的概念，而非字面图像。

完整流程：[references/workflow.md](references/workflow.md#step-2-setup--analyze)

### 步骤 3：确认设置 ⚠️

**硬性关卡**：依据[确认策略](#confirmation-policy)，本步骤为强制步骤 —— 在用户于此处确认（或在当前请求中明确以“直接生成”/等价措辞选择跳过）之前，不能开始步骤 4 及之后的内容。

**一次 AskUserQuestion，最多 4 个问题。Q1-Q2 必填。Q3 必填，除非已选择预设。**

| 问题 | 选项 |
|---|---------|
| **Q1：预设或 Type** | [推荐预设]、[备选预设]，或手动指定：infographic, scene, flowchart, comparison, framework, timeline, mixed |
| **Q2：密度** | minimal (1-2)、balanced (3-5)、per-section（推荐）、rich (6+) |
| **Q3：风格** | [推荐]、minimal-flat, sci-fi, hand-drawn, editorial, scene, poster、其他 —— **若已选择预设则跳过** |
| Q4：配色 | Default（style 配色）、macaron、warm、neon —— **若预设已包含 palette，或已设置 preferred_palette，则跳过** |
| Q5：语言 | 当文章语言 ≠ EXTEND.md 设置时 |

完整流程：[references/workflow.md](references/workflow.md#step-3-confirm-settings-)

### 步骤 4：生成大纲

保存 `outline.md`，包含 frontmatter（type, density, style, palette, image_count）与各条目：

```yaml
## Illustration 1
**Position**: [section/paragraph]
**Purpose**: [why]
**Visual Content**: [what]
**Filename**: 01-infographic-concept-name.png
```

完整模板：[references/workflow.md](references/workflow.md#step-4-generate-outline)

### 步骤 5：生成图片

⛔ **阻塞：在任何图像生成之前都必须先保存 prompt 文件。** 无论选择哪个后端，这都是硬性要求 —— prompt 文件是可复现记录。

1. 为每张插图，按 [references/prompt-construction.md](references/prompt-construction.md) 创建 prompt 文件
2. 保存到 `prompts/NN-{type}-{slug}.md`，并带 YAML frontmatter
3. Prompt **必须**使用类型专属模板，包含结构化分节（ZONES / LABELS / COLORS / STYLE / ASPECT）
4. LABELS **必须**包含文章特有的数据：真实数字、术语、指标、引文
5. 未先保存 prompt 文件时，**不要**把临时内联 prompt 直接传给 `--prompt`
6. 通过文首的 `## Image Generation Tools` 规则选择后端：有什么就用什么；若有多个，向用户询问一次。每次会话在任何生成之前执行一次。
   - **`codex-imagegen` 调用**：当规则解析为 `codex-imagegen` 时，参见 [references/codex-imagegen.md](references/codex-imagegen.md) 了解调用契约（首选的 `baoyu-image-gen --provider codex-cli` 路径、运行时 wrapper 发现、参数说明、stdout schema、批量语义）。
7. **执行策略**：按 `## Batch Generation Policy` 分批生成：优先后端原生批量，其次运行时并行工具调用，仅在都不可用时才串行。默认批量大小为 4，除非 EXTEND.md 或当前请求另行指定。
8. 按每个 prompt 的 frontmatter 处理参考图（`direct`/`style`/`palette`）
9. 若 EXTEND.md 启用了水印，则应用水印
10. 基于已保存的 prompt 文件生成；失败时重试一次

完整流程：[references/workflow.md](references/workflow.md#step-5-generate-images)

### 步骤 6：收尾

在段落之后插入 `![description]({relative-path}/NN-{type}-{slug}.png)`。路径根据输出目录设置，相对于文章文件计算。

```
Article Illustration Complete!
Article: [path] | Type: [type] | Density: [level] | Style: [style] | Palette: [palette or default]
Images: X/N generated
```

## 输出目录

输出目录由 EXTEND.md 中的 `default_output_dir` 决定（在首次设置时设定）：

| `default_output_dir` | 输出路径 | Markdown 插入路径 |
|----------------------|-------------|----------------------|
| `imgs-subdir`（默认） | `{article-dir}/imgs/` | `imgs/NN-{type}-{slug}.png` |
| `same-dir` | `{article-dir}/` | `NN-{type}-{slug}.png` |
| `illustrations-subdir` | `{article-dir}/illustrations/` | `illustrations/NN-{type}-{slug}.png` |
| `independent` | `illustrations/{topic-slug}/` | `illustrations/{topic-slug}/NN-{type}-{slug}.png`（相对于 cwd） |

所有辅助文件（outline、prompts）都保存在输出目录内：

```
{output-dir}/
├── outline.md
├── prompts/
│   └── NN-{type}-{slug}.md
└── NN-{type}-{slug}.png
```

当输入是**粘贴内容**（无文件路径）时，始终使用 `illustrations/{topic-slug}/`，并将 `source-{slug}.{ext}` 一并保存。

**Slug**：2-4 个单词，kebab-case。**冲突**：追加 `-YYYYMMDD-HHMMSS`。

## 修改

| 操作 | 步骤 |
|--------|-------|
| 编辑 | 更新 prompt → 重新生成 → 更新引用 |
| 新增 | 确定位置 → 编写 Prompt → 生成 → 更新 outline → 插入 |
| 删除 | 删除文件 → 移除引用 → 更新 outline |

文字纠正策略：

- 若渲染出的任何文字（标签、说明文字等）拼写错误、乱码、难以辨认或视觉上不理想，不要用代码修补位图。
- 为纠正文字而重新生成时，写入新的 prompt 文件并使用新的输出路径，以便保留有缺陷的候选图用于对比。
- 后处理仅限裁剪、缩放、压缩或格式转换，且不得改变文字或主体构图。

## 参考文件

| 文件 | 内容 |
|------|---------|
| [references/workflow.md](references/workflow.md) | 详细流程 |
| [references/usage.md](references/usage.md) | 命令语法 |
| [references/styles.md](references/styles.md) | 风格库 + 配色库 |
| [references/style-presets.md](references/style-presets.md) | 预设快捷方式（type + style + palette） |
| [references/prompt-construction.md](references/prompt-construction.md) | Prompt 模板 |
| [references/config/first-time-setup.md](references/config/first-time-setup.md) | 首次设置 |

## 更改偏好设置

EXTEND.md 位于步骤 1.5 中列出的第一个匹配路径。有三种修改方式：

- **直接编辑** —— 打开 EXTEND.md 修改字段。完整 schema：`references/config/preferences-schema.md`。
- **交互式重新配置** —— 删除 EXTEND.md（或输入 “reconfigure baoyu-article-illustrator preferences” / “重新配置”）。下次运行会重新触发首次设置。
- **常见的一行改动**：
  - `preferred_image_backend: auto` —— 默认值；运行时原生工具优先，否则回退到唯一已安装的后端，仅当存在多个非原生后端时才会询问。
  - `preferred_image_backend: codex-imagegen` —— 固定使用 Codex 的内置后端。
  - `preferred_image_backend: baoyu-image-gen` —— 固定使用 baoyu-image-gen 技能。
  - `preferred_image_backend: ask` —— 每次运行都确认后端。
  - `generation_batch_size: 4` —— 当运行时支持并行生成调用时，默认并发渲染的图片数量。
  - `preferred_type: infographic`, `preferred_style: notion`, `preferred_palette: macaron`, `language: zh`。
  - `default_output_dir: imgs-subdir` —— 生成图片相对于文章的写入位置。
