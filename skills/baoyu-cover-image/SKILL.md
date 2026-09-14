---
name: baoyu-cover-image
description: 生成文章封面图（技能名 baoyu-cover-image），支持 5 个维度（类型、配色、渲染、文字、氛围）的自定义，组合 11 种配色与 7 种渲染风格，支持电影宽幅 (2.35:1)、宽屏 (16:9) 与方形 (1:1) 比例。当用户请求「生成封面图」「制作文章封面」或「做封面」时使用。
triggers: 封面图,文章封面,公众号封面,小红书封面,生成封面
version: 1.117.5
metadata:
  openclaw:
    homepage: https://github.com/JimLiu/baoyu-skills#baoyu-cover-image
---

# 封面图生成器

为文章生成优雅的封面图，支持 5 维自定义。

## 用户输入工具

当本技能需要向用户提问时，请遵循以下工具选择规则（按优先级顺序）：

1. **优先使用当前 agent 运行时提供的内置用户输入工具** —— 例如 `AskUserQuestion`、`request_user_input`、`clarify`、`ask_user` 或任何等价工具。
2. **回退方案**：如果不存在此类工具，则输出一条带编号的纯文本消息，请用户针对每个问题回复所选编号/答案。
3. **批量处理**：如果该工具支持单次调用提交多个问题，则把所有适用问题合并为一次调用；如果只支持单问题，则按优先级顺序逐个提问。

下方具体引用的 `AskUserQuestion` 仅为示例 —— 在其他运行时中请替换为本地等价工具。

## 图像生成工具

当本技能需要渲染图像时，按以下顺序解析后端：

1. **当前请求覆盖** —— 如果用户在当前消息中点名了特定后端，则使用它。
2. **已保存的偏好** —— 如果 `EXTEND.md` 将 `preferred_image_backend` 设为当前可用的某个后端，则使用它。
3. **自动选择**（当偏好为 `auto`、未设置，或所指定的后端当前不可用时）：
   - **星瑶（`hunyuan_generate_image` / `tokenrhythm_generate_image`）** —— 如果你的工具清单中暴露了 `hunyuan_generate_image` 或 `tokenrhythm_generate_image`，说明你正运行在 星瑶 运行时内，必须使用其中之一作为栅格图后端；它的优先级高于任何非原生技能，并且此处有意**不**安装 `baoyu-image-gen`。默认使用 `hunyuan_generate_image`；当用户在当前请求中点名它、需要更接近 3:4 的竖版尺寸，或希望一次调用得到 2-4 个候选图时，切换到 `tokenrhythm_generate_image`。将已保存的 prompt 文件内容作为 `prompt` 传入，并将本技能预期的输出目录作为 `save_dir` 传入。参数映射、尺寸映射、锚定图链（通过 `image` 参考参数）以及批量规则见 [references/ycode-imagegen.md](references/ycode-imagegen.md) —— 首次渲染前请先加载该文件。
   - **Codex（`imagegen`）** —— 首先检查你可用的技能 / 工具清单。如果列出了名为 `imagegen` 的技能，说明你正运行在 Codex 内，必须使用它：通过 `Skill` 工具以 `skill: "imagegen"` 调用，传入已保存的 prompt 文件内容（外加输出路径与宽高比，遵循 Codex `imagegen` 自身的参数）。在该运行时中，Codex `imagegen` 是官方栅格图后端，其优先级高于任何非原生技能（例如 `baoyu-image-gen`），除非用户已显式指定了不同的 `preferred_image_backend`。
   - **经由 `codex exec` 的 Codex（`codex-imagegen`）** —— 如果当前运行时没有暴露原生 `imagegen` 技能，但 `PATH` 上存在 `codex` CLI 且已有激活的 `codex login`，则经由 `baoyu-image-gen --provider codex-cli` 路由（首选）；或者 —— 如果 baoyu-image-gen 不可用 —— 直接调用捆绑的 wrapper。细节、参数与运行时探测流程见 [references/codex-imagegen.md](references/codex-imagegen.md) —— 仅当选中此分支时才加载该文件。
   - **Cursor（`GenerateImage`）** —— 如果运行时暴露了原生 `GenerateImage` 工具，说明你正运行在 Cursor 内，它与 Codex `imagegen` 一样优先于任何非原生技能。两个硬性注意事项：(a) 它没有宽高比参数 —— 需在作为 `description` 传入的 prompt 文本中明确写出目标宽高比 / 尺寸；(b) 它不接受输出目录 —— 它会保存到由工具管理的位置，因此生成后需把文件复制/移动到本技能预期的输出路径（例如 `outputs/.../NN-xxx.png`）。参考图放在 `reference_image_paths` 中。
   - **其他运行时原生工具** —— 如果运行时暴露了其他原生图像工具（例如 Hermes `image_generate`），以同样方式使用它。
   - 否则，如果恰好安装了唯一一个非原生后端（例如 `baoyu-image-gen`），则使用它。
   - 否则（存在多个非原生后端且没有运行时原生工具），询问用户一次 —— 与其他初始问题合并批量提问。
4. **如果都不可用**，告知用户并询问如何继续。

**⛔ 绝不要用 SVG、HTML、canvas 或其他基于代码的渲染来替代栅格图生成。** Codex `imagegen` 自身的描述称，应在「输出应为位图资产而非仓库原生代码或矢量图」时使用它。如果你无法通过第 3 步解析出栅格图后端，请落到第 4 步并询问用户 —— **不要**默默产出 SVG、写入内联 `<svg>` 标记，或用 HTML/CSS 画作作为替代。即便文章/小节看起来「像图表」，此规则同样适用：调用本规则的消费方技能已经确定它需要的就是栅格图。

**⛔ 绝不要通过覆盖绘制来修复已渲染的文本。** 不要使用 ImageMagick、Pillow、Canvas、SVG、HTML/CSS、OCR 脚本或任何其他程序化叠加手段，去覆盖、改写、擦除、描边或替换已生成封面图内的标题/副标题文本。如果文本有误或不清，请用修正后的 prompt 重新生成、切换到文字更少或无标题的变体，或询问用户保留哪一个不完美的候选图。

将 `preferred_image_backend: ask` 设为固定值，会强制每次运行都执行第 3 步的询问，无论有哪些可用后端。用户可通过下方 `## 更改偏好` 小节更改所固定的后端。

**Prompt 文件要求（硬性）**：在调用任何后端之前，必须先把每张图完整、最终的 prompt 写入 `prompts/` 下的独立文件（命名：`NN-{type}-[slug].md`）。后端接收的是该 prompt 文件（或其内容）；该文件是复现记录，让你无需重新生成 prompt 即可切换后端。

上文中出现的具体工具名（`imagegen`、`GenerateImage`、`image_generate`、`baoyu-image-gen`）仅为示例 —— 请在相同规则下替换为本地等价物。

## 确认策略

默认行为：**生成前先确认**。

- 将显式调用技能、文件路径、匹配到的关键词/预设、`EXTEND.md` 默认值，以及任何有文档记载的自动选择，都仅视为**推荐输入**。其中任何一项都不授权跳过确认。
- 在用户确认维度 / 宽高比 / 语言 / 后端选择之前，**不要**开始第 3 步或第 4 步。
- 仅当当前请求明确表示可以跳过时才跳过确认，例如：`--quick`、「直接生成」、「不用确认」、「跳过确认」、「按默认出图」或等价措辞。`EXTEND.md` 中的 `quick_mode: true` 视为长期显式放弃确认 —— 仅当你希望每次运行都跳过第 2 步时才设置它。
- 如果已显式跳过确认，则在生成前，于下一次面向用户的更新中说明所假设的维度 / 宽高比 / 语言 / 后端。

## 选项

| 选项 | 说明 |
|--------|-------------|
| `--type <name>` | hero, conceptual, typography, metaphor, scene, minimal |
| `--palette <name>` | warm, elegant, cool, dark, earth, vivid, pastel, mono, retro, duotone, macaron |
| `--rendering <name>` | flat-vector, hand-drawn, painterly, digital, pixel, chalk, screen-print |
| `--style <name>` | 预设简写（见 [样式预设](references/style-presets.md)） |
| `--text <level>` | none, title-only, title-subtitle, text-rich |
| `--mood <level>` | subtle, balanced, bold |
| `--font <name>` | clean, handwritten, serif, display |
| `--aspect <ratio>` | 16:9（默认）, 2.35:1, 4:3, 3:2, 1:1, 3:4 |
| `--lang <code>` | 标题语言（en、zh、ja 等） |
| `--no-title` | `--text none` 的别名 |
| `--quick` | 跳过确认，使用自动选择 |
| `--ref <files...>` | 用于风格/构图参考的参考图 |

## 五个维度

| 维度 | 取值 | 默认 |
|-----------|--------|---------|
| **类型** | hero, conceptual, typography, metaphor, scene, minimal | auto |
| **配色** | warm, elegant, cool, dark, earth, vivid, pastel, mono, retro, duotone, macaron | auto |
| **渲染** | flat-vector, hand-drawn, painterly, digital, pixel, chalk, screen-print | auto |
| **文字** | none, title-only, title-subtitle, text-rich | title-only |
| **氛围** | subtle, balanced, bold | balanced |
| **字体** | clean, handwritten, serif, display | clean |

自动选择规则：[references/auto-selection.md](references/auto-selection.md)

## 图库

**类型**：hero, conceptual, typography, metaphor, scene, minimal
→ 详情：[references/types.md](references/types.md)

**配色**：warm, elegant, cool, dark, earth, vivid, pastel, mono, retro, duotone, macaron
→ 详情：[references/palettes/](references/palettes/)

**渲染**：flat-vector, hand-drawn, painterly, digital, pixel, chalk, screen-print
→ 详情：[references/renderings/](references/renderings/)

**文字层级**：none（纯视觉） | title-only（默认） | title-subtitle | text-rich（带标签）
→ 详情：[references/dimensions/text.md](references/dimensions/text.md)

**氛围层级**：subtle（低对比度） | balanced（默认） | bold（高对比度）
→ 详情：[references/dimensions/mood.md](references/dimensions/mood.md)

**字体**：clean（无衬线） | handwritten | serif | display（粗体装饰性）
→ 详情：[references/dimensions/font.md](references/dimensions/font.md)

## 文件结构

输出目录取决于 `default_output_dir` 偏好：
- `same-dir`: `{article-dir}/`
- `imgs-subdir`: `{article-dir}/imgs/`
- `independent`（默认）：`cover-image/{topic-slug}/`

```
<output-dir>/
├── source-{slug}.{ext}    # Source files
├── refs/                  # Reference images (if provided)
│   ├── ref-01-{slug}.{ext}
│   └── ref-01-{slug}.md   # Description file
├── prompts/cover.md       # Generation prompt
└── cover.png              # Output image
```

**Slug**：2-4 个单词，kebab-case。冲突时：追加 `-YYYYMMDD-HHMMSS`

## 工作流

### 进度清单

```
Cover Image Progress:
- [ ] Step 0: Check preferences (EXTEND.md) ⛔ BLOCKING
- [ ] Step 1: Analyze content + save refs + determine output dir
- [ ] Step 2: Confirm options (6 dimensions) ⚠️ unless --quick
- [ ] Step 3: Create prompt
- [ ] Step 4: Generate image
- [ ] Step 5: Completion report
```

### 流程

```
Input → [Step 0: Preferences] ─┬─ Found → Continue
                               └─ Not found → First-Time Setup ⛔ BLOCKING → Save EXTEND.md → Continue
        ↓
Analyze + Save Refs → [Output Dir] → [Confirm: 6 Dimensions] → Prompt → Generate → Complete
                                              ↓
                                     (skip if --quick or all specified)
```

### 第 0 步：加载偏好 ⛔ 阻塞

按优先级顺序检查 EXTEND.md —— 最先找到的那个生效：

| 优先级 | 路径 | 范围 |
|----------|------|-------|
| 1 | `.baoyu-skills/baoyu-cover-image/EXTEND.md` | 项目 |
| 2 | `${XDG_CONFIG_HOME:-$HOME/.config}/baoyu-skills/baoyu-cover-image/EXTEND.md` | XDG |
| 3 | `$HOME/.baoyu-skills/baoyu-cover-image/EXTEND.md` | 用户主目录 |

| 结果 | 操作 |
|--------|--------|
| 找到 | 加载、显示摘要 → 继续 |
| 未找到 | ⛔ 运行首次配置（[references/config/first-time-setup.md](references/config/first-time-setup.md)）→ 保存 → 继续 |

**关键**：如果未找到，必须在任何其他步骤或提问之前完成配置。

### 第 1 步：分析内容

1. **保存参考图**（如已提供）→ [references/workflow/reference-images.md](references/workflow/reference-images.md)
2. **保存源内容**（若为粘贴内容，保存到 `source.md`）
3. **分析内容**：主题、语气、关键词、视觉隐喻
4. **深度分析参考图** ⚠️：提取具体、确切的元素（见 reference-images.md）
5. **检测语言**：比较源内容、用户输入、EXTEND.md 偏好
6. **确定输出目录**：遵循文件结构规则

**⚠️ 参考图中的人物：**

如果参考图中包含应出现在封面上的**人物**：

- **模型支持 `--ref`**（默认）：把图像复制到 `refs/`，生成时通过 `--ref` 传入。无需描述文件 —— 模型可直接看到人脸。
- **模型不支持 `--ref`**（Jimeng、Seedream 3.0）：创建 `refs/ref-NN-{slug}.md`，为每个角色写明描述（发型、眼镜、肤色、服装）。将其作为 MUST/REQUIRED 指令嵌入 prompt 文本中。

完整决策表见 [reference-images.md](references/workflow/reference-images.md)。

### 第 2 步：确认选项 ⚠️

**硬性关卡**：根据[确认策略](#confirmation-policy)，本步骤为强制步骤 —— 在用户于此处确认之前，第 3–4 步不得开始（或用户通过 `--quick` / `quick_mode: true` / 当前请求中的等价措辞显式放弃）。

**必须使用 `AskUserQuestion` 工具**，以交互式选择（而非纯文本表格）呈现选项。在单次 `AskUserQuestion` 调用中最多呈现 4 个问题（类型、配色、渲染、字体 + 设置）。每个问题先展示推荐选项及其理由，再列出备选项。

完整的确认流程与问题格式：[references/workflow/confirm-options.md](references/workflow/confirm-options.md)

| 条件 | 跳过 | 仍会询问 |
|-----------|---------|-------------|
| `--quick` 或 `quick_mode: true` | 6 个维度 | 宽高比（除非指定 `--aspect`） |
| 已指定全部 6 项 + `--aspect` | 全部 | 无 |

### 第 3 步：创建 Prompt

保存到 `prompts/cover.md`。模板：[references/workflow/prompt-template.md](references/workflow/prompt-template.md)

**关键 —— frontmatter 中的 references**：
- 保存到 `refs/` 的文件 → 加入 frontmatter 的 `references` 列表
- 仅以文字提取的风格（无文件）→ 省略 `references`，在正文中描述
- 写入之前 → 校验：`test -f refs/ref-NN-{slug}.{ext}`

**正文中的参考元素**必须详细，以 "MUST"/"REQUIRED" 为前缀，并说明融合方式。

### 第 4 步：生成图像

1. 如果重新生成，**先备份现有的** `cover.png`
2. **选择后端**：依据开头的 `## 图像生成工具` 规则：使用任何可用的后端；若有多个，询问用户一次。每次会话在任何生成之前执行一次。
3. 在调用后端**之前**，将完整、最终的 prompt 写入 `prompts/01-cover-[slug].md`（硬性要求）。
4. **处理 prompt frontmatter 中的 references**：
   - `direct` 用法 → 通过 `--ref` 传入（使用支持 ref 的后端）
   - `style`/`palette` → 提取特征，追加到 prompt
5. **生成**：以 prompt 文件、输出路径、宽高比调用所选后端。
   - **`codex-imagegen`**：调用契约见 [references/codex-imagegen.md](references/codex-imagegen.md)（首选的 `baoyu-image-gen --provider codex-cli` 路径、运行时 wrapper 探测、参数说明、stdout schema、批量语义）。
   - **Codex `imagegen`（原生）** 或其他运行时原生工具 / `baoyu-image-gen` 技能：遵循上方 `## 图像生成工具` 规则。
6. 失败时：自动重试一次

### 第 5 步：完成报告

```
Cover Generated!

Topic: [topic]
Type: [type] | Palette: [palette] | Rendering: [rendering]
Text: [text] | Mood: [mood] | Font: [font] | Aspect: [ratio]
Title: [title or "visual only"]
Language: [lang] | Watermark: [enabled/disabled]
References: [N images or "extracted style" or "none"]
Location: [directory path]

Files:
✓ source-{slug}.{ext}
✓ prompts/cover.md
✓ cover.png
```

## 图像修改

| 操作 | 步骤 |
|--------|-------|
| **重新生成** | 备份 → 先更新 prompt 文件 → 重新生成 |
| **更改维度** | 备份 → 确认新值 → 更新 prompt → 重新生成 |

文本修正策略：

- 如果标题/副标题拼写错误、乱码、难以辨认或视觉上薄弱，不要用代码修补位图。
- 对于文本修正类重新生成，请写入新的 prompt 文件和新的输出路径，以便保留有缺陷的候选图用于对比。
- 后处理仅限于不改变文本或主体构图的裁剪、缩放、压缩或格式转换。

## 构图原则

- **留白**：40-60% 的呼吸空间
- **视觉锚点**：主元素居中或偏左
- **人物**：简化的剪影；不要写实人物
- **标题**：使用来自用户/源内容的准确标题；绝不臆造

## 更改偏好

EXTEND.md 位于**第 0 步**中指出的路径。有三种修改方式：

- **直接编辑** —— 打开 EXTEND.md 并修改字段。完整 schema：[references/config/preferences-schema.md](references/config/preferences-schema.md)。
- **交互式重新配置** —— 删除 EXTEND.md（或说 "reconfigure baoyu-cover-image preferences" / "重新配置"）。下次运行会重新触发首次配置。
- **常见单行修改**：
  - `preferred_image_backend: auto` —— 默认；运行时原生工具优先，回退到唯一已安装的后端，仅当存在多个非原生后端时才询问。
  - `preferred_image_backend: codex-imagegen` —— 固定使用 Codex 内置后端。
  - `preferred_image_backend: baoyu-image-gen` —— 固定使用 baoyu-image-gen 技能。
  - `preferred_image_backend: ask` —— 每次运行都确认后端。
  - `watermark.enabled: true`、`preferred_type`、`preferred_palette`、`preferred_rendering`、`default_aspect`、`quick_mode: true`、`language` —— 调整自动选择的默认值与确认流程。

## 参考

**维度**：[text.md](references/dimensions/text.md) | [mood.md](references/dimensions/mood.md) | [font.md](references/dimensions/font.md)
**配色**：[references/palettes/](references/palettes/)
**渲染**：[references/renderings/](references/renderings/)
**类型**：[references/types.md](references/types.md)
**自动选择**：[references/auto-selection.md](references/auto-selection.md)
**样式预设**：[references/style-presets.md](references/style-presets.md)
**兼容性**：[references/compatibility.md](references/compatibility.md)
**视觉元素**：[references/visual-elements.md](references/visual-elements.md)
**工作流**：[confirm-options.md](references/workflow/confirm-options.md) | [prompt-template.md](references/workflow/prompt-template.md) | [reference-images.md](references/workflow/reference-images.md)
**配置**：[preferences-schema.md](references/config/preferences-schema.md) | [first-time-setup.md](references/config/first-time-setup.md) | [watermark-guide.md](references/config/watermark-guide.md)
