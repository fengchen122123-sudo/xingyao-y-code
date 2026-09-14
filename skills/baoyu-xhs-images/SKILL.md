---
name: baoyu-xhs-images
description: 生成小红书风格的信息图图片卡片系列：内置 12 种视觉风格、8 种布局、3 套配色，把内容拆成 1-10 张卡通风格图片卡片，为社媒传播互动优化。当用户提到「小红书图片」「小红书种草」「小绿书」「微信图文」「微信贴图」「image cards」「图片卡片」或想要社媒信息图系列时使用。技能名：baoyu-xhs-images。
triggers: 小红书图片,小红书配图,图片卡片,小绿书,微信图文,图片系列
version: 2.0.1
metadata:
  openclaw:
    homepage: https://github.com/JimLiu/baoyu-skills#baoyu-xhs-images
---

# 图片卡片系列生成器

把复杂内容拆解成吸睛的图片卡片系列，并提供多种风格选项。

## 用户输入工具

当本技能需要向用户提问时，遵循以下工具选择规则（按优先级顺序）：

1. **优先使用当前 agent 运行时暴露的内置用户输入工具** —— 例如 `AskUserQuestion`、`request_user_input`、`clarify`、`ask_user` 或任何等价工具。
2. **兜底**：若不存在这类工具，则输出一条带编号的纯文本消息，请用户针对每个问题回复所选编号／答案。
3. **批量**：若该工具支持一次调用提多个问题，就把所有适用问题合并为一次调用；若只支持单问题，则按优先级顺序逐个提问。

下文出现的具体 `AskUserQuestion` 均是示例 —— 在其他运行时中请替换为本地等价工具。

## 图片生成工具

当本技能需要渲染图片时，按以下顺序确定后端：

1. **当前请求覆盖** —— 若用户在当前消息中指定了某个后端，就用它。
2. **已保存的偏好** —— 若 `EXTEND.md` 把 `preferred_image_backend` 设为此刻可用的某个后端，就用它。
3. **自动选择**（当偏好为 `auto`、未设置，或固定的后端不可用时）：
   - **星瑶（`hunyuan_generate_image` / `tokenrhythm_generate_image`）** —— 如果你的工具清单里暴露了 `hunyuan_generate_image` 或 `tokenrhythm_generate_image`，说明你正运行在 星瑶 运行时内，必须用其中之一作为位图后端；它优先于任何非原生技能，并且此处有意**不**安装 `baoyu-image-gen`。默认使用 `hunyuan_generate_image`；当用户在当前请求中点名它、当需要更接近 3:4 的竖版尺寸、或当希望一次调用产出 2-4 个候选时，改用 `tokenrhythm_generate_image`。把已保存的 prompt 文件内容作为 `prompt` 传入，把本技能期望的输出目录作为 `save_dir` 传入。参数映射、尺寸映射、锚点图链路（通过 `image` 引用参数）与批处理规则见 [references/ycode-imagegen.md](references/ycode-imagegen.md) —— 首次渲染前请先加载该文件。
   - **Codex（`imagegen`）** —— 先检查你可用的技能／工具清单。如果清单中列出了名为 `imagegen` 的技能，说明你正运行在 Codex 内，必须使用它：通过 `Skill` 工具以 `skill: "imagegen"` 调用，并传入已保存的 prompt 文件内容（外加输出路径与宽高比，具体参数以 Codex `imagegen` 自身的定义为准）。Codex `imagegen` 是那个运行时的官方位图后端，优先于任何非原生技能（例如 `baoyu-image-gen`），除非用户已明确固定了另一个 `preferred_image_backend`。
   - **通过 `codex exec` 使用 Codex（`codex-imagegen`）** —— 若当前运行时未暴露原生 `imagegen` 技能，但 `codex` CLI 已在 `PATH` 上且已执行有效的 `codex login`，则经由 `baoyu-image-gen --provider codex-cli` 路由（首选），或者 —— 若 baoyu-image-gen 不可用 —— 直接调用随附的包装脚本。详情、参数与运行时探测流程见 [references/codex-imagegen.md](references/codex-imagegen.md) —— 仅当选中该分支时才加载该文件。
   - **Cursor（`GenerateImage`）** —— 若运行时暴露了原生 `GenerateImage` 工具，说明你正运行在 Cursor 内，它和 Codex `imagegen` 一样优先于任何非原生技能。有两个硬性注意点：(a) 它没有宽高比参数 —— 必须在作为 `description` 传入的 prompt 文本中明确写出目标宽高比／尺寸；(b) 它不接受输出目录 —— 它会保存到工具自管的位置，因此生成后要把文件复制／移动到本技能期望的输出路径（例如 `outputs/.../NN-xxx.png`）。参考图放在 `reference_image_paths` 中。
   - **其他运行时原生工具** —— 若运行时暴露了别的原生图片工具（例如 Hermes `image_generate`），同样按上述方式使用它。
   - 否则，若恰好只安装了一个非原生后端（例如 `baoyu-image-gen`），就用它。
   - 否则（存在多个非原生后端且没有运行时原生工具），向用户询问一次 —— 可与其他初始问题合并。
4. **若一个都不可用**，告知用户并询问如何继续。

**⛔ 绝不允许用 SVG、HTML、canvas 或其他基于代码的渲染方式替代位图图片生成。** Codex `imagegen` 自身的描述就写着：它应当用于 "when the output should be a bitmap asset rather than repo-native code or vector."。若你无法通过第 3 步确定位图后端，就落到第 4 步并询问用户 —— **不要**悄悄输出 SVG、写内联 `<svg>` 标记，或用 HTML/CSS 作图来替代。即使文章／章节看起来「像图表」也同样适用：调用此规则的消费方技能已经决定它要的是位图图片。

**⛔ 绝不允许通过在已生成的位图上覆盖绘制来修补渲染出的文字。** 不要使用 ImageMagick、Pillow、Canvas、SVG、HTML/CSS、OCR 脚本或任何其他程序化叠加手段，去覆盖、改写、擦除、描边或替换已经生成好的图片卡片中的标题、正文、标签或任何其他文字。若文字有误或不清晰，请用修正后的 prompt 重新生成、换成卡片文字更少的布局，或询问用户想保留哪个不完美的候选版本。

把 `preferred_image_backend` 设为 `ask` 会强制每次运行都弹出第 3 步的询问，无论可用后端情况如何。用户可通过下文的 `## 更改偏好` 小节更改所固定的后端。

**Prompt 文件要求（硬性）**：在调用任何后端**之前**，先把每张图的完整、最终 prompt 写入 `prompts/` 下的独立文件（命名：`NN-{type}-[slug].md`）。该文件是可复现性记录，也让你无需重新生成 prompt 即可切换后端。

上文出现的具体工具名（`imagegen`、`GenerateImage`、`image_generate`、`baoyu-image-gen`）均为示例 —— 请按同一规则替换为本地等价物。

## 批量生成策略

当前生成分组的所有 prompt 文件都已保存并校验后，默认按批生成图片。

优先级顺序：

1. 若所选后端存在原生批量／多任务接口，就用它。每个任务必须保留各自的 prompt 文件、输出路径、宽高比、会话 ID 与直接参考图。
2. 若没有原生批量接口，但运行时能发起并行工具调用，则每次最多派发 `generation_batch_size` 张图。默认值：`4`。当前消息中用户的显式要求（例如 `--batch-size 4` 或「并行 4 张一起生成」）会覆盖 EXTEND.md。
3. 若原生批量与并行工具调用都不可用，则串行生成。

规则：

- 遵守 image-1 锚点链路：先生成第 1 张图，再以第 1 张图作为参考批量生成第 2 张及之后的图。
- 在该批次所选的全部 prompt 文件都存在于磁盘之前，绝不启动该批次。
- 失败项重试一次，不重新生成已成功的项。
- 不要仅仅为了并行渲染图片而使用子代理。子代理只用于彼此独立的 prompt 迭代或创意探索。

## 确认策略

默认行为：**生成前先确认**。

- 把显式技能调用、文件路径、命中的信号／预设，以及 `EXTEND.md` 默认值都视为**仅推荐输入**。它们中的任何一项都不构成跳过确认的授权。
- 在用户完成第 2 步之前，**不要**开始第 3 步。
- 仅当当前请求明确要求跳过时才跳过确认，例如：`--yes`、「直接生成」、「不用确认」、「跳过确认」、「按默认出图」或等价措辞。
- 若已显式跳过确认，则在生成前、下一次面向用户的更新中说明所采用的 策略／风格／布局／配色／数量／后端。

## 语言

在提问、进度、错误和完成总结中都使用用户的语言回复。技术性 token（风格名、文件路径、代码）保持英文。

## 选项

| 选项 | 说明 |
|--------|-------------|
| `--style <name>` | 视觉风格（见下文 风格） |
| `--layout <name>` | 信息布局（见下文 布局） |
| `--palette <name>` | 配色覆盖：macaron / warm / neon |
| `--preset <name>` | 风格 + 布局 + 可选配色的简写（见下文 预设；各预设的 prompt 片段见 `references/style-presets.md`） |
| `--ref <files...>` | 参考图，作为系列锚点应用到第 1 张图 |
| `--batch-size <n>` | 本次运行的临时生成批次大小。默认：EXTEND.md 中的 `generation_batch_size`，否则为 4。限制在 1-8。 |
| `--yes` | 非交互：跳过所有确认，使用 EXTEND.md 或内置默认值，自动确认推荐方案（Path A） |

## 维度

三个独立维度可自由组合：

| 维度 | 控制内容 | 可选项 |
|-----------|----------|---------|
| **风格** | 视觉美学（线条、装饰、渲染方式） | 12 种风格（见下文 风格） |
| **布局** | 信息结构（密度、排布方式） | 8 种布局（见下文 布局） |
| **配色**（可选） | 颜色覆盖，替换风格的默认配色 | macaron / warm / neon（见下文 配色） |

示例：`--style notion --layout dense` 做出知识型卡片；再加 `--palette macaron` 可在不改变 notion 渲染规则的前提下柔化颜色。`--preset` 是 风格 + 布局（+ 可选配色）的简写。

**配色行为**：不加 `--palette` → 用风格的内置配色；`--palette <name>` → 仅覆盖颜色，渲染规则不变。有些风格会声明 `default_palette`（例如 sketch-notes 默认 macaron）。

## 风格（12 种）

| 风格 | 说明 |
|-------|-------------|
| `cute`（默认） | 甜美、可爱、少女感审美 |
| `fresh` | 清爽、干净、自然 |
| `warm` | 温馨、友好、亲切 |
| `bold` | 高冲击力、强吸睛 |
| `minimal` | 极度干净、精致 |
| `retro` | 复古、怀旧、潮流 |
| `pop` | 鲜明、有活力、抢眼 |
| `notion` | 极简手绘线条风、知性 |
| `chalkboard` | 黑板上的彩色粉笔、教育感 |
| `study-notes` | 逼真的手写照片风，蓝色圆珠笔 + 红色批注 + 黄色荧光笔 |
| `screen-print` | 醒目海报艺术、半调纹理、有限色彩、符号化叙事 |
| `sketch-notes` | 手绘教育信息图，暖奶油底色上的马卡龙粉彩，抖动的线条 |

各风格的详细规范：`references/presets/<style>.md`。

## 布局（8 种）

| 布局 | 说明 |
|--------|-------------|
| `sparse`（默认） | 1-2 个要点，冲击力最大化 |
| `balanced` | 3-4 个要点，标准 |
| `dense` | 5-8 个要点，知识卡片风格 |
| `list` | 枚举／排行（4-7 项） |
| `comparison` | 并列对比 |
| `flow` | 流程／时间线（3-6 步） |
| `mindmap` | 中心辐射（4-8 个分支） |
| `quadrant` | 四象限／环形分块 |

布局规范：`references/elements/canvas.md`。

## 配色（可选覆盖）

在保留渲染规则（线条处理、纹理）不变的前提下替换风格的颜色。

| 配色 | 背景 | 区域色 | 强调色 | 气质 |
|---------|------------|-------------|--------|------|
| `macaron` | Warm cream #F5F0E8 | Blue #A8D8EA, Lavender #D5C6E0, Mint #B5E5CF, Peach #F8D5C4 | Coral #E8655A | 柔和、教育感 |
| `warm` | Soft peach #FFECD2 | Orange #ED8936, Terracotta #C05621, Golden #F6AD55, Rose #D4A09A | Sienna #A0522D | 大地色、温馨 |
| `neon` | Dark purple #1A1025 | Cyan #00F5FF, Magenta #FF00FF, Green #39FF14, Pink #FF6EC7 | Yellow #FFFF00 | 高能量、未来感 |

配色规范：`references/palettes/<palette>.md`。

## 预设（风格 + 布局快捷组合）

按场景分组的快速起步组合。使用 `--preset <name>`，或在第 2 步中推荐。

**知识与学习**：

| 预设 | 风格 | 布局 | 适用场景 |
|--------|-------|--------|----------|
| `knowledge-card` | notion | dense | 干货知识卡、概念科普 |
| `checklist` | notion | list | 清单、排行榜 |
| `concept-map` | notion | mindmap | 概念图、知识脉络 |
| `swot` | notion | quadrant | SWOT 分析、四象限 |
| `tutorial` | chalkboard | flow | 教程步骤、操作流程 |
| `classroom` | chalkboard | balanced | 课堂笔记、知识讲解 |
| `study-guide` | study-notes | dense | 学习笔记、考试重点 |
| `hand-drawn-edu` | sketch-notes | flow | 手绘教程、流程图解 |
| `sketch-card` | sketch-notes | dense | 手绘知识卡 |
| `sketch-summary` | sketch-notes | balanced | 手绘总结、图文笔记 |

**生活方式与分享**：

| 预设 | 风格 | 布局 | 适用场景 |
|--------|-------|--------|----------|
| `cute-share` | cute | balanced | 少女风分享、日常种草 |
| `girly` | cute | sparse | 甜美封面、氛围感 |
| `cozy-story` | warm | balanced | 生活故事、情感分享 |
| `product-review` | fresh | comparison | 产品对比、测评 |
| `nature-flow` | fresh | flow | 健康流程、自然主题 |

**冲击力与观点**：

| 预设 | 风格 | 布局 | 适用场景 |
|--------|-------|--------|----------|
| `warning` | bold | list | 避坑指南、重要提醒 |
| `versus` | bold | comparison | 正反对比 |
| `clean-quote` | minimal | sparse | 金句、极简封面 |
| `pro-summary` | minimal | balanced | 专业总结、商务内容 |

**潮流与娱乐**：

| 预设 | 风格 | 布局 | 适用场景 |
|--------|-------|--------|----------|
| `retro-ranking` | retro | list | 复古排行、经典盘点 |
| `throwback` | retro | balanced | 怀旧分享 |
| `pop-facts` | pop | list | 趣味冷知识 |
| `hype` | pop | sparse | 炸裂封面、惊叹分享 |

**海报与编辑风**：

| 预设 | 风格 | 布局 | 适用场景 |
|--------|-------|--------|----------|
| `poster` | screen-print | sparse | 海报风封面、影评书评 |
| `editorial` | screen-print | balanced | 观点文章、文化评论 |
| `cinematic` | screen-print | comparison | 电影对比、戏剧张力 |

完整的 prompt 片段定义：`references/style-presets.md`。

## 自动选择

把内容信号匹配到最佳组合。以第一个命中关键词的行为准；若无任何匹配，则回退到 `cute-share`。

| 源内容信号 | 风格 | 布局 | 推荐预设 |
|-------------------|-------|--------|--------------------|
| beauty, fashion, cute, girl, pink | `cute` | sparse/balanced | `cute-share`, `girly` |
| health, nature, fresh, organic | `fresh` | balanced/flow | `product-review`, `nature-flow` |
| life, story, emotion, warm | `warm` | balanced | `cozy-story` |
| warning, important, must, critical | `bold` | list/comparison | `warning`, `versus` |
| professional, business, elegant | `minimal` | sparse/balanced | `clean-quote`, `pro-summary` |
| classic, vintage, traditional | `retro` | balanced | `throwback`, `retro-ranking` |
| fun, exciting, wow, amazing | `pop` | sparse/list | `hype`, `pop-facts` |
| knowledge, concept, productivity, SaaS | `notion` | dense/list | `knowledge-card`, `checklist` |
| education, tutorial, learning, classroom | `chalkboard` | balanced/dense | `tutorial`, `classroom` |
| notes, handwritten, study guide, realistic | `study-notes` | dense/list/mindmap | `study-guide` |
| movie, poster, opinion, editorial, cinematic | `screen-print` | sparse/comparison | `poster`, `editorial`, `cinematic` |
| hand-drawn, infographic, workflow, 手绘，图解 | `sketch-notes` | flow/balanced/dense | `hand-drawn-edu`, `sketch-card`, `sketch-summary` |

## 风格 × 布局矩阵

兼容性评分（✓✓ 强烈推荐，✓ 效果好，✗ 避免）。当用户选了非默认组合、而你想提示匹配不佳时使用。

|              | sparse | balanced | dense | list | comparison | flow | mindmap | quadrant |
|--------------|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| cute         | ✓✓ | ✓✓ | ✓  | ✓✓ | ✓  | ✓  | ✓  | ✓  |
| fresh        | ✓✓ | ✓✓ | ✓  | ✓  | ✓  | ✓✓ | ✓  | ✓  |
| warm         | ✓✓ | ✓✓ | ✓  | ✓  | ✓✓ | ✓  | ✓  | ✓  |
| bold         | ✓✓ | ✓  | ✓  | ✓✓ | ✓✓ | ✓  | ✓  | ✓✓ |
| minimal      | ✓✓ | ✓✓ | ✓✓ | ✓  | ✓  | ✓  | ✓  | ✓  |
| retro        | ✓✓ | ✓✓ | ✓  | ✓✓ | ✓  | ✓  | ✓  | ✓  |
| pop          | ✓✓ | ✓✓ | ✓  | ✓✓ | ✓✓ | ✓  | ✓  | ✓  |
| notion       | ✓✓ | ✓✓ | ✓✓ | ✓✓ | ✓✓ | ✓✓ | ✓✓ | ✓✓ |
| chalkboard   | ✓✓ | ✓✓ | ✓✓ | ✓✓ | ✓  | ✓✓ | ✓✓ | ✓  |
| study-notes  | ✗  | ✓  | ✓✓ | ✓✓ | ✓  | ✓  | ✓✓ | ✓  |
| screen-print | ✓✓ | ✓✓ | ✗  | ✓  | ✓✓ | ✓  | ✗  | ✓✓ |
| sketch-notes | ✓  | ✓✓ | ✓✓ | ✓✓ | ✓  | ✓✓ | ✓✓ | ✓  |

## 大纲策略

三种彼此差异化的思路 —— 每一种产出的 outline 结构都不同。工作流会推荐其中一种；Path C 会生成全部三种并让用户挑选。

| 策略 | 概念 | 适用场景 | 结构 |
|----------|---------|----------|-----------|
| **A — 故事驱动** | 以个人体验为线索，情绪共鸣优先 | 测评、个人分享、转变历程 | 钩子 → 问题 → 发现 → 体验 → 结论 |
| **B — 信息密集** | 价值优先，高效传递信息 | 教程、对比、清单 | 核心结论 → 信息卡 → 优点/缺点 → 建议 |
| **C — 视觉优先** | 以视觉冲击为核心，文字最少 | 高颜值产品、生活方式、氛围内容 | 主视觉 → 细节图 → 生活场景 → CTA |

## 参考图

用户提供的参考图与内部的「以 image-1 为锚点」链路（第 3 步）是**相互独立**的 —— 它们叠加在该链路之上。

**接收**：通过 `--ref <files...>` 或对话中粘贴的路径。
- 文件路径 → 复制到 `refs/NN-ref-{slug}.{ext}`
- 仅粘贴而无路径 → 询问路径，或抽取风格特征作为文本兜底

**使用模式**（逐张参考图指定）：

| 用法 | 效果 |
|-------|--------|
| `direct` | 把文件传给后端（通常只作用于第 1 张图，好让锚点沿链路传播） |
| `style` | 抽取风格特征并附加到每张卡片的 prompt 正文 |
| `palette` | 抽取十六进制颜色并附加到每张卡片的 prompt 正文 |

在受影响的每张卡片的 prompt frontmatter 中记录参考图：

```yaml
references:
  - ref_id: 01
    filename: 01-ref-brand.png
    usage: direct
```

生成时：校验文件是否存在。第 1 张图带 `usage: direct` + 后端接受参考图 → 通过后端的 ref 参数传入（成为链路锚点）。第 2 张及之后的图按第 3 步继续使用 image-1 作为 `--ref` —— 不要在其上再叠加用户参考图（避免信号冲突）。对于 `style`/`palette`，把抽取到的特征嵌入每一个 prompt。

## 文件布局

```
image-cards/{topic-slug}/
├── source-{slug}.{ext}
├── analysis.md
├── outline-strategy-{a,b,c}.md    # Path C only
├── outline.md
├── prompts/NN-{type}-{slug}.md
├── NN-{type}-{slug}.png
└── refs/                          # only if --ref used
```

**Slug**：2-4 个单词，kebab-case。「AI 工具推荐」→ `ai-tools-recommend`。若发生冲突，追加 `-YYYYMMDD-HHMMSS`。

**备份规则**（全程适用）：在覆盖任何文件 —— 源文件、outline、prompt、图片 —— 之前，先把已有文件重命名为 `<name>-backup-YYYYMMDD-HHMMSS.<ext>`。这能保护用户的修改。

## 工作流

```
- [ ] Step 0: Load EXTEND.md ⛔ BLOCKING (interactive only)
- [ ] Step 1: Analyze content → analysis.md
- [ ] Step 2: Smart Confirm ⚠️ REQUIRED (Path A / B / C)
- [ ] Step 3: Generate images
- [ ] Step 4: Completion report
```

### 第 0 步：加载 EXTEND.md ⛔ 阻塞（仅交互模式）

按顺序检查以下路径；命中第一个即用：

| 路径 | 作用域 |
|------|-------|
| `.baoyu-skills/baoyu-xhs-images/EXTEND.md` | 项目 |
| `${XDG_CONFIG_HOME:-$HOME/.config}/baoyu-skills/baoyu-xhs-images/EXTEND.md` | XDG |
| `$HOME/.baoyu-skills/baoyu-xhs-images/EXTEND.md` | 用户主目录 |

- **找到** → 读取、解析并打印摘要（风格／布局／水印／语言），然后继续。
- **未找到 + 交互模式** → 运行首次设置（见 `references/config/first-time-setup.md`），并在做其他任何事之前先保存。在偏好存在之前，不要分析内容、也不要提问风格 —— 这样首次运行的行为才可预测。
- **未找到 + `--yes`** → 跳过设置，使用内置默认值（无水印、风格／布局自动选择、语言取自内容）。不要提问，不要创建 EXTEND.md。

**EXTEND.md 键**：水印、偏好风格／布局、自定义风格定义、语言偏好、偏好图片后端、生成批次大小。Schema：`references/config/preferences-schema.md`。

### 第 1 步：分析内容 → `analysis.md`

1. 保存源文件（若 `source.md` 已存在，则适用备份规则）。
2. 执行 `references/workflows/analysis-framework.md` 中的深度分析：内容类型、钩子潜力、受众、互动信号、视觉机会图谱、滑动流。
3. 检测源语言，挑选推荐的图片数量（2-10）。
4. 使用上文 **自动选择** 表自动推荐 策略 + 风格 + 布局 + 配色。
5. 把所有内容写入 `analysis.md`。

### 第 2 步：智能确认 ⚠️ 必需

**硬性门禁**：按 [确认策略](#confirmation-policy)，本步骤为强制要求 —— 在用户于此处确认（或在当前请求中用 `--yes`／等价措辞明确放弃）之前，第 3 步不能开始。

目标：呈现自动推荐的方案，让用户确认或调整。在 `--yes` 下完全跳过本步骤 —— 直接用分析结果和任何 CLI 覆盖项走 Path A。

**提问前先展示摘要**：

```
📋 内容分析
  主题：[topic] | 类型：[content_type]
  要点：[key points]
  受众：[audience]

🎨 推荐方案（自动匹配）
  策略：[A/B/C] [name]（[reason]）
  风格：[style] · 布局：[layout] · 配色：[palette or 默认] · 预设：[preset]
  图片：[N]张（封面+[N-2]内容+结尾）
  元素：[background] / [decorations] / [emphasis]
```

然后问一个问题 —— 三条路径。逐字选项文案：`references/confirmation.md`。

**Path A — 快速确认**（信任自动推荐）：用推荐的 策略 + 风格 生成单份 outline → 保存到 `outline.md` → 第 3 步。

**Path B — 自定义**：以推荐值为预填，问五个问题（策略／风格、布局、配色、数量、可选备注）—— 留空即保留推荐值。用用户的选择生成一份 outline → `outline.md` → 第 3 步。见 `references/confirmation.md`。

**Path C — 详细模式**：两次子确认。

- *第 2a 步 — 内容理解*：询问卖点（多选）、受众、风格偏好（真实感／专业／美学／自动）、可选背景信息。更新 `analysis.md`。
- *第 2b 步 — 三份 outline 变体*：生成 `outline-strategy-a.md`、`outline-strategy-b.md`、`outline-strategy-c.md`。每一份都必须结构不同**且**推荐风格不同 —— 在 frontmatter 中包含 `style_reason`。页数经验值：A 约 4-6，B 约 3-5，C 约 3-4。模板：`references/workflows/outline-template.md`；frontmatter 示例见 `references/confirmation.md`。
- *第 2c 步 — 选择*：问三个问题（outline 选 A/B/C/合并、风格、视觉元素）。把选定／合并后的 outline 保存到 `outline.md` → 第 3 步。

### 第 3 步：生成图片

在已确认的 outline + 风格 + 布局 + 配色下：

**视觉一致性 —— image-1 锚点链路**：除非你把它们锚住，否则人物／吉祥物／颜色的渲染会在各次调用之间漂移。先生成第 1 张图（封面）且**不带** `--ref`，然后把第 1 张图作为 `--ref` 传给后续每一张图。这是本技能最重要的一致性技巧 —— 即使后端还支持会话 ID，也不要跳过。

生成流程：

1. 用用户偏好的语言把每张图的完整 prompt 写入 `prompts/NN-{type}-{slug}.md`（适用备份规则），然后校验所有选定的 prompt 文件都存在。
2. 先生成**第 1 张图**且不带 `--ref`；PNG 文件适用备份规则。这一步确立了锚点。
3. 为**第 2 张及之后的图**建立任务列表，使用第 1 张图作为 `--ref <path-to-image-01.png>`。
4. 按 `## 批量生成策略` 分批派发第 2 张及之后的图：优先用后端原生批量，其次用运行时并行工具调用，仅在兜底时才串行。
5. 每张图完成后报告进度。失败时，仅用同一份已保存的 prompt 文件重试该失败项一次。

**水印**（若在 EXTEND.md 中启用）：追加到生成 prompt：

```
Include a subtle watermark "[content]" positioned at [position].
The watermark should be legible but not distracting.
```

见 `references/config/watermark-guide.md`。

**后端选择**：按文首的图片生成工具规则 —— 用任何可用的后端；若有多个，则在任何生成之前先问一次。在 `--yes` 下，使用 EXTEND.md 的偏好，并回退到第一个可用后端。在调用任何后端之前，prompt 文件必须已存在。

**`codex-imagegen` 调用**：当规则判定为 `codex-imagegen` 时，见 [references/codex-imagegen.md](references/codex-imagegen.md) 了解调用契约（首选的 `baoyu-image-gen --provider codex-cli` 路径、运行时包装脚本探测、参数说明、stdout schema、批处理语义 —— 每次调用 n=1，因此卡片批次必须每张卡片派发一次包装脚本调用；包装脚本不接受 `--sessionId`，所以链路一致性必须来自上文第 3 步的 `--ref`）。

**会话 ID**（若后端支持 `--sessionId`）：每张图都用 `cards-{topic-slug}-{timestamp}`；与 ref 链路结合可获得最大一致性。

### 第 4 步：完成报告

```
Image Card Series Complete!

Topic: [topic]
Mode: [Quick / Custom / Detailed]
Strategy: [A/B/C/Combined]
Style: [name]
Palette: [name or "default"]
Layout: [name or "varies"]
Location: [directory]
Images: N total

✓ analysis.md
✓ outline.md
✓ outline-strategy-a/b/c.md (detailed mode only)

- 01-cover-[slug].png ✓ Cover (sparse)
- 02-content-[slug].png ✓ Content (balanced)
- ...
- NN-ending-[slug].png ✓ Ending (sparse)
```

## 内容拆解原则

| 位置 | 目的 | 典型布局 |
|----------|---------|----------------|
| 封面（第 1 张） | 钩子 + 视觉冲击 | `sparse` |
| 内容（中间） | 每张图的核心价值 | `balanced` / `dense` / `list` / `comparison` / `flow` |
| 结尾（最后一张） | CTA / 总结 | `sparse` 或 `balanced` |

关于风格 × 布局兼容性矩阵，见上文 **风格 × 布局矩阵**。

## 图片修改

| 动作 | 做法 |
|--------|-----|
| 编辑 | **先**更新 `prompts/NN-{type}-{slug}.md`，然后用同一会话 ID 重新生成 |
| 新增 | 指定位置、创建 prompt、生成，并把后续文件重编号为 `NN+1`、更新 outline |
| 删除 | 删除文件，把后续文件重编号为 `NN-1`、更新 outline |

务必在重新生成之前更新 prompt 文件 —— 它是事实来源，也让改动可复现。

文字修正策略：

- 如果某张卡片的标题、正文、标签或任何其他渲染文字存在拼写错误、乱码、难以辨认或视觉上偏弱的情况，不要用代码去修补位图。
- 对于文字修正类的重新生成，请写新的 prompt 文件并使用新的输出路径，以便保留有缺陷的候选版本用于对比。
- 后处理仅限于不改变文字或主体构图的裁剪、缩放、压缩或格式转换。

## 参考文档

| 文件 | 内容 |
|------|---------|
| `references/confirmation.md` | 每条确认路径的逐字 AskUserQuestion 文案 |
| `references/style-presets.md` | 完整的预设快捷定义 |
| `references/presets/<style>.md` | 各风格的元素定义 |
| `references/palettes/<name>.md` | 各配色的颜色定义 |
| `references/elements/canvas.md` | 宽高比、安全区、网格布局 |
| `references/elements/image-effects.md` | 抠图、描边、滤镜 |
| `references/elements/typography.md` | 装饰文字、标签、文字方向 |
| `references/elements/decorations.md` | 强调标记、背景、涂鸦、边框 |
| `references/workflows/analysis-framework.md` | 内容分析框架 |
| `references/workflows/outline-template.md` | 带布局指南的 outline 模板 |
| `references/workflows/prompt-assembly.md` | prompt 组装指南 |
| `references/config/preferences-schema.md` | EXTEND.md schema |
| `references/config/first-time-setup.md` | 首次设置流程 |
| `references/config/watermark-guide.md` | 水印配置 |

## 注意事项

- 生成失败时先自动重试一次，再报告错误。
- 对敏感的公众人物，使用风格化的卡通替代形象。
- 第 2 步的智能确认是必需的；详细模式会再加一次确认（2a + 2c）。

## 更改偏好

EXTEND.md 位于第 0 步所列出的第一个匹配路径。有三种更改方式：

- **直接编辑** —— 打开 EXTEND.md 修改字段。完整 schema：`references/config/preferences-schema.md`。
- **交互式重新配置** —— 删除 EXTEND.md（或说「reconfigure baoyu-xhs-images preferences」／「重新配置」）。下次运行会重新触发首次设置。
- **常见的一行改动**：
  - `preferred_image_backend: auto` —— 默认；运行时原生工具优先，回退到唯一已安装的后端，仅当存在多个非原生后端时才询问。
  - `preferred_image_backend: codex-imagegen` —— 固定到 Codex 内置。
  - `preferred_image_backend: baoyu-image-gen` —— 固定到 baoyu-image-gen 技能。
  - `preferred_image_backend: ask` —— 每次运行都确认后端。
  - `generation_batch_size: 4` —— 当后端／运行时支持批量或并行生成时，默认并发渲染的图片数量。
  - `preferred_style: notion`、`preferred_layout: dense`、`preferred_palette: macaron`、`language: zh`。
  - `watermark.enabled: true` + `watermark.content: "@handle"` —— 添加水印。
