# 星瑶小说工作区设计（P1 + P2）

- 日期：2026-10-28
- 状态：待评审
- 范围：引擎层（插件 + 子 Agent + 技能适配）+ 可视化层（右侧小说页签）

## 1. 背景与目标

星瑶目前能通过技能系统加载 oh-story-claudecode 的 13 个网文技能，但存在三个断点：7 个专业写作子 Agent 没有星瑶格式版本、Agent 内部写死的 `.claude/skills/...` 资料路径在星瑶环境不存在、长文创作缺少确定性工具（建书、读写进度、追踪事务、字数判定）导致模型每次现写 shell 命令。

目标是把"写小说"变成星瑶的一种项目形态：打开小说项目时，引擎工具、写作子 Agent、GUI 面板同时就位；关闭项目后恢复为编程助手。

成功标准：在空目录中从零跑完"建书 → 卷纲 → 细纲 → 写 3 章 → 追踪提交"，第 3 章能正确引用第 1 章埋下的伏笔，右侧面板能展示完整项目状态。

## 2. 非目标

- 不做内嵌富文本编辑器（P3）
- 不做多书并行管理与发布对接（P3）
- 不改动 oh-story 上游技能包本体，所有适配放在星瑶侧
- 不在 P1 实现"技能按项目动态激活"，作为技术债记录（见 §10）

## 3. 架构

四层，自下而上：

| 层 | 职责 | 落点 |
|---|---|---|
| L1 数据层 | 书目目录与状态文件 | 用户的小说项目目录 |
| L2 引擎层 | 确定性操作（建书/读数/追踪/字数） | `plugins/novel_workspace/` |
| L3 编排层 | 创作方法论与流程 | `.ycode/skills/` + `.ycode/agents/` |
| L4 界面层 | 可视化状态 | `frontend/src/components/NovelPane.vue` |

关键边界：L2 只做确定性计算，不调用模型；L3 只做流程编排，不直接解析二进制状态；L4 只读，不写项目文件。

## 4. 数据契约

### 4.1 项目目录

```
{小说项目}/
├── .novel.json
├── 设定/
│   ├── 关系.md
│   ├── 题材定位.md
│   ├── 文风.md
│   └── 角色/{角色名}.md
├── 大纲/
│   ├── 全书大纲.md
│   ├── 卷纲_第001卷.md
│   └── 细纲_第001章.md
├── 正文/
│   └── 第001章_章名.md
├── 追踪/
│   ├── _tracking-state.json
│   ├── 上下文.md
│   └── 逐章记录/
└── 对标/
```

复用 oh-story 的目录契约，使 `outline_view.py`、`tracking_commit.py`、`wordcount_core.py` 可直接运行，不重新发明格式。

### 4.2 项目标记 `.novel.json`

```json
{
  "schema": 1,
  "title": "书名",
  "genre": "玄幻",
  "platform": "起点",
  "target_words": 1000000,
  "engine": "story-ycode",
  "engine_version": "1.0.0",
  "created_at": "2026-10-28T00:00:00Z"
}
```

判定规则：存在 `.novel.json` 即视为小说项目；缺失时所有小说工具返回 `is_novel: false`，不抛异常。

## 5. L2 引擎层：插件 `novel_workspace`

### 5.1 插件结构

```
plugins/novel_workspace/
├── plugin.json          # name/version/description/entry/enabled
├── plugin.py            # 薄壳：只注册工具并转发 core.novel_service
├── agents/              # 7 个星瑶格式子 Agent 模板
├── references/          # agent-references 资料（从上游 vendored）
└── vendor/
    ├── LICENSE-oh-story # MIT 许可原文
    ├── outline_view.py
    ├── tracking_commit.py
    ├── wordcount_core.py
    └── VERSION.lock

core/novel_service.py    # 业务实现，纯函数 + 文件操作（随主程序编译）
tests/test_novel_service.py
```

插件按星瑶既有机制通过 `registry.register(ToolSpec(...))` 注册工具，零核心侵入，单插件失败不影响启动。采用与 `plugins/hunyuan_image` 相同的薄壳模式：业务实现放 `core/novel_service.py`，插件只做注册与转发，GUI bridge 可直接调用核心实现，不依赖插件加载状态。

### 5.2 工具清单（P1 六个）

| 工具 | 入参 | 行为 | 返回 |
|---|---|---|---|
| `novel_init` | workdir, title, genre?, platform?, target_words? | 建目录树、写 `.novel.json`、部署 7 个 Agent 到 `.ycode/agents/`、复制 references 到 `.ycode/skills/story-ycode/references/` | 创建清单 `{created: [...], skipped: [...]}` |
| `novel_status` | workdir | 扫目录 + 读追踪状态 + 算今日字数 | `{is_novel, title, chapters_written, total_words, today_words, next_chapter, outline_ready, active_foreshadows, last_commit}` |
| `novel_outline` | workdir, (unit_id), (stage: write/outline), (toc) | 调 vendored `outline_view.py` | 卷纲段文本 |
| `novel_track` | workdir, mode(init/append/revision), transaction(JSON) | 调 vendored `tracking_commit.py` | 提交结果或结构化错误 |
| `novel_wordcount` | text（直接算）或 workdir + chapter（读文件算） | 调 vendored `wordcount_core.py` 的 visible_chars_v1 口径 | `{visible_chars, chapter}` |
| `novel_audit` | workdir, scope(chapter/arc/all), chapter?, checks(auto/consistency/prose/outline) | 跑确定性检查：追踪状态一致性、伏笔逾期、字数达标、细纲契约、AI 味模式、文本退化；汇总分级 | `{ok, scope, findings: [{level, type, file, message}], summary: {S1..S4}, skipped: [{check, reason}]}` |

工具元数据约定：`novel_init`、`novel_track` 为 `mutates_files=True, requires_permission=True`；其余只读。

### 5.3 工具行为约束

- 所有工具先校验 `.novel.json`，缺失即返回 `is_novel: false`。
- 脚本调用统一用 `sys.executable`，超时 30 秒，stderr 原样进错误消息，不吞。
- `novel_track` 只做转发，不在插件内重算状态；脚本拒绝的事务原样返回给模型。
- `novel_status` 不把正文原文返回给模型（正文不进模型上下文）；本地确定性统计允许读取正文文件，字数由插件在本进程内逐章读正文、按 vendored `wordcount_core` 的 `visible_chars` 口径计算，不经过模型；单次扫描上限 500 章。
- `today_words` 口径：`正文/` 下 mtime 落在本地当天的章节，按 `visible_chars` 求和；追踪逐章记录存在提交日期时以记录为准，两者冲突取记录值。
- `novel_audit` 只执行确定性检查，不调用模型、不产出创作建议正文；需要推理的设定矛盾与人设走样由技能编排 `consistency-checker` 子 Agent 完成，工具负责提供结构化数据。
- `novel_audit` 的分级沿用上游 `consistency-checker` 的 S1-S4 口径；单项检查因依赖缺失（如未安装 Node）无法执行时写入 `skipped` 并继续，不整体失败。
- `novel_audit` 不写任何文件，findings 只随返回值交付；是否落盘由技能决定。

## 6. L3 编排层：子 Agent 与技能

### 6.1 Agent 转换规则

上游 Agent 定义在 `story-setup/references/templates/agents/*.md`，星瑶解析器（`core/subagent_registry.py`）只支持单行 `key: value`，不支持多行块与数组语法，因此必须程序化转换。

字段映射：

| 上游字段 | 星瑶字段 | 规则 |
|---|---|---|
| `tools: [Read, Glob, Grep]` | `allowed_tools` | 逗号分隔，工具名按下表转换 |
| `model: opus/sonnet` | `model: inherit` | 使用主模型 |
| `model: haiku` | `model: secondary` | 使用副模型降本 |
| 无 | `base_type` | 只读 Agent → `explore`；需审查判断 → `review`；需写文件 → `general` |
| `description: \|` | `description` | 合并为单行 |
| `maxTurns` / `memory` / `skills` | 丢弃 | 星瑶不支持 |

工具名映射：`Read→read_file`、`Glob→glob_files`、`Grep→search_code`、`Write→write_file`、`Edit→edit_file`、`Bash→execute_command`、`WebSearch→search_news`、`WebFetch→fetch_url`。

七个 Agent 的最终配置：

| Agent | base_type | model | allowed_tools |
|---|---|---|---|
| story-architect | general | inherit | read_file, glob_files, search_code, write_file, edit_file |
| narrative-writer | general | inherit | read_file, glob_files, search_code, write_file, edit_file, execute_command |
| character-designer | general | inherit | read_file, glob_files, search_code, write_file, edit_file |
| story-researcher | general | inherit | read_file, glob_files, search_code, write_file, execute_command, search_news, fetch_url |
| consistency-checker | review | secondary | read_file, glob_files, search_code |
| chapter-extractor | explore | secondary | read_file, glob_files, search_code |
| story-explorer | explore | secondary | read_file, glob_files, search_code |

### 6.2 Agent 正文路径改写

上游正文写死 `{项目根}/.claude/skills/story-setup/references/agent-references/{文件名}`。转换时改为两级回退：

```
1. {项目根}/.ycode/skills/story-ycode/references/agent-references/{文件名}
2. ~/.agents/skills/story-setup/references/agent-references/{文件名}
```

第一级由 `novel_init` 部署，保证项目自包含；第二级兜底用户已全局安装 oh-story 的场景。

### 6.3 技能适配

不修改上游 13 个技能文件。新增一个薄适配技能 `.ycode/skills/story-ycode/SKILL.md`，职责：

- 声明星瑶环境下的术语映射：`Agent(subagent_type:)` → `run_subagent(subagent_type=)`
- 声明路径回退规则
- 声明降级规则：Agent 不可用时走主线程 solo，并标注 `Fallback: agent unavailable -> direct`
- 路由到上游技能名（`story-long-write` 等），不复制其正文

## 7. L4 界面层：小说页签

### 7.1 类型扩展

`frontend/src/stores/appStore.ts:149`：

```ts
export type RightPanelTabId = 'files' | 'git' | 'browser' | 'tasks' | 'computer' | 'subagent' | 'novel';
```

同步更新 `RIGHT_PANEL_TAB_IDS` 白名单、`PANE_LABELS`（"小说"）、`TAB_ICONS`（book）。

### 7.2 后端接口

`gui_bridge.py` 的 `ApiBridge` 新增：

```python
def get_novel_status(self, workdir: str) -> Dict[str, Any]:
    """返回小说项目状态；非小说项目返回 {"is_novel": False}。"""
```

实现调用 `core/novel_service.py`，与插件工具共用同一份逻辑；GUI 不依赖插件是否加载。

### 7.3 组件 `NovelPane.vue`

只读展示，四个区块：

1. 头部：书名 + 总字数进度条（当前/目标）+ 今日字数
2. 章节树：按卷分组，已写章节显示字数，未写章节灰显并标注"待写"
3. 状态卡：活跃伏笔数、最近追踪提交章号、下一章细纲是否就绪
4. 空状态：检测到非小说项目时显示"初始化小说项目"引导按钮，点击后向对话注入初始化指令，不直接写文件

刷新策略：页签激活时拉一次；对话回合结束（`store.running` true→false）时若页签可见则按需重拉一次
（状态快照 + 多书列表），让对话里初始化出的项目/写出的新章节立刻反映到面板；不做轮询。

## 8. 数据流

```
点击"小说"页签
  → store.openRightPanelTab('novel')
  → bridge.get_novel_status(workdir)
  → service.py 读 .novel.json + 追踪状态 + 扫描正文目录
  → 返回 JSON
  → NovelPane 渲染
```

写作链路：

```
用户："帮我开书"
  → 主 Agent 匹配 story-long-write 技能
  → run_subagent(subagent_type="story-architect")
  → 子 Agent 读 references，产出设定与卷纲
  → 主 Agent 调 novel_init / novel_track 落盘
  → 写完正文后调 novel_track 提交事务
```

## 9. 错误处理

| 场景 | 处理 |
|---|---|
| 无 `.novel.json` | 返回 `is_novel: false`，前端显示引导，不报错 |
| vendored 脚本缺失或退出非 0 | 工具返回 `{ok: false, error, stderr}`，不静默降级 |
| `追踪/_tracking-state.json` 损坏 | 返回错误并要求运行 `story-import` 重建，不自动修复 |
| 未安装 Node | AI 味检测标记 `skipped: true`，不阻塞写作 |
| Agent 未注册 | 主线程 solo 并输出 `Fallback: agent unavailable -> direct` |
| 目录无写权限 | 工具返回权限错误，引导用户换目录 |

## 10. 技术债与后续

- **技能按项目激活**：当前 13 个同步技能的描述常驻系统提示，写代码时属于噪音。P3 通过项目类型感知动态挂载。
- **模型分级**：星瑶自定义 Agent 只有 `inherit`/`secondary` 两档，无法像上游那样 opus/sonnet/haiku 三级路由。若后续需要，扩展 `subagent_registry` 支持显式 model id。
- **上游脚本版本**：`vendor/VERSION.lock` 锁定 commit，升级需人工比对差异。

## 11. 测试与验收

### P1 验收

- `novel_init` 在空目录生成完整目录树，7 个 Agent 文件可被 `subagent_registry.scan_custom_subagents()` 解析且 `name` 正确
- `novel_status` 对 3 章样例返回准确章节数与字数（与 `wordcount_core` 直接调用结果一致）
- `novel_track` 提交后 `check` 通过，重复提交同章报 `chapter delta already exists` 而非静默覆盖
- `novel_audit` 对含 1 处伏笔逾期、1 处字数不足的样例返回对应 findings，`summary` 分级与 findings 一致；未装 Node 时 `skipped` 含 `ai_patterns` 且其余检查照常执行
- 端到端：从零写完 3 章，第 3 章包含第 1 章伏笔的回收，`novel_audit` 返回 0 条 S1

### P2 验收

- 页签打开后 3 秒内完成首屏
- 非小说项目显示引导，不报错
- 章节树点击展开该章摘要（跳转文件面板留 P3）
- 现有 138 个前端测试全部通过

### 测试落点

- `tests/test_novel_service.py`：pytest + tmp_path
- `frontend/src/components/NovelPane.test.ts`：vitest，正常态 + 空态
- `tests/test_novel_bridge.py`：ApiBridge 接口契约

## 12. 实施顺序

1. `core/novel_service.py` 骨架 + 插件薄壳 + `novel_init`（含 Agent 转换器）
2. `novel_status` + `novel_wordcount`
3. `novel_track` + `novel_outline`（vendor 脚本接入）
4. `novel_audit`（确定性检查接入）
5. 端到端 3 章验证
6. 后端 bridge 接口
7. 前端 NovelPane + 页签注册
8. 前端测试与回归
