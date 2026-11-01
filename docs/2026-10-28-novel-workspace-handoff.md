# 星瑶小说工作区 · 交接文档

- 日期：2026-10-28
- 分支：dev
- 设计文档：`docs/2026-10-28-novel-workspace-design.md`
- 实施计划：`docs/2026-10-28-novel-workspace-plan.md`

## 1. 一句话状态

P1 引擎层、P2 可视化层、P3 体验层全部落地。建书、写章、追踪事务、确定性审查、章节内嵌编辑、多书切换、md/txt 导出、AI 味扫描全部可用，右侧「小说」页签可完成日常读写。剩余为可选增强，见 §6。

## 2. 交付清单

### 2.1 服务层（新增）

`core/novel_service.py`，1462 行，纯标准库依赖：

| 函数 | 作用 |
|---|---|
| `init_novel_project` | 建目录树、写 `.novel.json`、转换并部署 7 个写作 Agent、复制资料库 |
| `novel_status` | 章节数、总字数、今日字数、下一章、细纲就绪、活跃伏笔、最近提交章 |
| `novel_wordcount` | visible_chars_v1 口径，支持 text 直算与 workdir+chapter 读文件 |
| `novel_outline` | 调 vendor/outline_view.py 取卷纲段 |
| `novel_track` | 调 vendor/tracking_commit.py 提交追踪事务 |
| `novel_audit` | 五项确定性检查，输出 S1-S4 findings 与 skipped 清单 |

### 2.2 插件（新增）

`plugins/novel_workspace/`：

- `plugin.json` / `plugin.py`：薄壳，注册 6 个 ToolSpec
- `agents/`：7 个星瑶格式写作子 Agent（story-architect、narrative-writer、character-designer、story-researcher、consistency-checker、chapter-extractor、story-explorer）
- `references/`：68 份写作技法资料（含 genre-prose-cards 子目录），从 oh-story MIT 包 vendored
- 资源解析优先级：`core/novel_service.py` 的 `bundled_novel_root()` 先找插件内 `agents/` 与 `references/`，缺失才回退 `~/.agents/skills/story-setup`。**换电脑没装上游包也能正常建书**
- `vendor/`：`outline_view.py`、`tracking_commit.py`、`wordcount_core.py` + `LICENSE-oh-story` + `VERSION.lock`（逐文件 sha256）

工具注册结果：`novel_init`、`novel_status`、`novel_wordcount`、`novel_outline`、`novel_track`、`novel_audit`。其中 init/track 会改文件，其余只读。

### 2.3 后端桥（修改）

`gui_bridge.py`：

- 第 49 行新增 `from core import novel_service`
- 第 4058 行附近新增 `ApiBridge.get_novel_status(workdir)`，委托 `novel_service.novel_status`，空路径/GLOBAL/不存在目录返回结构化 error

### 2.4 前端（新增 + 修改）

- `frontend/src/components/NovelPane.vue`：只读面板，loading / 空态 / 正常态三态，展示书名、字数进度、今日字数、章节列表、伏笔与追踪状态
- `frontend/src/components/NovelPane.test.ts`：6 个用例
- `frontend/src/components/RightPanel.vue`：注册 `novel` 页签渲染、标签「小说」、图标 `file`
- `frontend/src/stores/appStore.ts`：`RightPanelTabId` 与 `RIGHT_PANEL_TAB_IDS` 加入 `novel`

### 2.5 测试（新增）

| 文件 | 覆盖 |
|---|---|
| `tests/test_novel_service.py` | 建书、Agent 转换、状态、字数、追踪、审查，25 项 |
| `tests/test_novel_e2e.py` | 建书 → 3 章 → 提交 → 审查 → 伏笔回收，1 项 |
| `tests/test_novel_bridge.py` | get_novel_status 契约，6 项 |

### 2.6 P3 交付（本轮新增）

服务层 `core/novel_service.py`：

| 函数 | 作用 |
|---|---|
| `read_chapter` / `save_chapter` | 单章正文读写。保存走 `_write_text` 的按回合撤销；章节必须已存在，不新建 |
| `discover_novel_projects` | 多书发现：自身 + 2 层子目录，命中 `.novel.json` 即停，跳过隐藏/依赖/导出目录，只数文件不读正文 |
| `export_novel` | 全书导出 md/txt 到 `导出/{书名}.{fmt}`；md 保留原文，txt 去掉 frontmatter 与标题记号 |
| `_audit_ai_patterns`（重写） | 本地 AI 味扫描：一级禁用词（取插件 `banned-words.md`，缺失用内置 19 词）+「不是A，而是B」句式（≥2 次）+ 章末预告，逐条 S4，**不依赖 Node** |

后端桥 `gui_bridge.py` 新增：`get_novel_chapter` / `save_novel_chapter` / `list_novel_projects` / `export_novel`。

前端 `NovelPane.vue` + `pywebview.d.ts`：

- 章节点击进入内联编辑器：textarea 编辑、保存后回填该章字数并重拉整体状态
- 多书下拉（多于一本才显示）+ md/txt 导出按钮与结果提示
- 新增类型 `NovelChapterDto` / `NovelChapterSaveDto` / `NovelProjectDto` / `NovelProjectListDto` / `NovelExportDto`

### 2.7 P3-C 工作台面板（本轮新增）

工作台四个分区全部只读，数据来源逐条对应后端接口：

1. **设定库三表**：`get_novel_workbench` 返回的 `foreshadows` / `characters` / `volumes`，
   一次拉取、前端按 tab 切换渲染（`NovelForeshadowTable` / `NovelCharacterTable` /
   `NovelVolumeList`），切 tab 不重复请求。伏笔行含 `overdue` / `planned_overdue` /
   `stale_chapters`；角色行字符串字段缺省给空串、四个数组字段缺省给 `[]`；卷纲行给
   `no` / `name` / `chars`（visible_chars 口径）/ `current`（按 `next_chapter` 唯一命中
   才为 true）。排序稳定：伏笔按「超期 → 计划超期 → 搁置章数 → 埋设章 → id」，角色按
   「有未结线索优先 → 名字」。
2. **近 7 天字数**：同一接口的 `novel_workbench.recent_daily`，固定 7 项、每项
   `{date, words}`、按日期升序、无数据的天补 0；口径是章节文件 mtime 落在哪一天
   （追踪记录没有日期字段）。`NovelDailyTrend` 用纯 CSS 柱状图渲染（柱高按当日字数占
   窗口最大值等比换算），合计值只在展示层累加，不引图表库。
3. **一致性审查**：`NovelAuditPanel` 的按钮调 `run_novel_audit(workdir, "chapter")`
   （桥接委托 `novel_service.novel_audit`，只读、点击才跑，不自动触发也不轮询）。
   五项检查：追踪一致性 `tracking_state`、伏笔超期 `foreshadow_overdue`、字数达标
   `chapter_wordcount`、细纲缺口 `outline_gap`、AI 味 `ai_patterns`；返回 `findings`
   逐条结论 + `summary` 里的 S1..S4 计数 + `skipped`（前置缺失或单项异常只进 skipped，
   不拖垮整次审查），由 `NovelAuditPanel` 渲染汇总与明细。
4. **文档预览**：点卷纲行调 ``read_novel_doc(workdir, `大纲/{卷名}.md`)``，
   `NovelDocViewer` 在面板内以只读浮层卡片展示（正文区滚动，不跳出右侧面板）。
   白名单在 core 内依次校验：只开放 `大纲/` `设定/` `追踪/` 三个子目录、仅 `.md`、
   单文件上限 2MB；绝对路径、`..` 穿越与符号链接逃逸都在 `resolve()` 后一步挡掉。

**边界与兼容**：老版本桥接缺 `get_novel_workbench` / `run_novel_audit` /
`read_novel_doc` 时，只有对应分区降级为「当前后端版本不支持该功能」的提示文案，
章节列表、内联编辑器、多书切换与导出不受影响。切书（下拉切换）、换工作目录、
对话回合结束三处都会重置工作台状态（同时递增请求序号，丢弃在途结果）并按新目录
重拉，上一本书的伏笔、审查结论与预览文档不会残留。

## 3. 验证记录（本轮实际执行）

```
python -m pytest tests/test_novel_service.py tests/test_novel_e2e.py tests/test_novel_bridge.py tests/test_gui_bridge_decision.py -q
84 passed in 2.58s

npm --prefix frontend run test -- NovelPane
Test Files 1 passed (1)
Tests      12 passed (12)

npm --prefix frontend run test
Test Files 157 passed (157)
Tests      1481 passed (1481)
Duration   65.06s

python -m pytest tests/test_turn_undo_{transaction,tool,storage,recovery,recorder,prompt_context,lifecycle}.py -q
97 passed, 2 skipped in 4.75s
```

**未完成验证**：后端全量回归 `python -m pytest -q -m "not slow"` 在本机跑不完（334 个测试文件、约 7200 项，后台 20 分钟只到 39%）。已用受影响模块回归替代：小说三件套 + gui_bridge 决策 84 项、turn_undo 七个文件 97 项（2 skipped），全部通过。换机器或空闲时段建议补跑全量。

P3-C 工作台面板回归（验证时间 2026-10-30）：

```
python -m pytest tests/test_novel_e2e.py tests/test_novel_service.py tests/test_novel_bridge.py -q
86 passed in 29.80s

npm --prefix frontend run test -- NovelPane
Test Files 1 passed (1)
Tests      23 passed (23)
Duration   15.34s
```

说明：后端 86 项全部通过（含 `novel_workbench` / `read_novel_doc` / `run_novel_audit` 的返回契约与超期口径用例），**未修改任何测试断言**；前端 `NovelPane.test.ts` 由 12 项增至 23 项，新增用例覆盖设定库 tab 切换、近 7 天柱状图、审查按钮与 `ok=false` 分支、卷纲预览、设定库读取失败/老桥接降级、切书与回合结束的重拉与重置。

## 4. 关键设计事实（供接手者直接使用）

- `wordcount_core.py` **没有 CLI**，按模块导入；`visible_body(value: str)` 收字符串，调用方自己读文件
- `tracking_commit.py` 的 `check` 只输出 `{"last_committed_chapter", "state_revision"}`
- 伏笔状态枚举 `("已埋","已回收","已过期","放弃")`，未收 = `"已埋"`
- 追踪逐章记录**没有日期字段**，`today_words` 按文件 mtime 回落
- 星瑶子 Agent 解析器（`core/subagent_registry.py`）只支持单行 `key: value`，不认 `description: |` 多行块与 `tools: [...]` 数组，所以 Agent 必须程序化转换
- 星瑶只有 `inherit` / `secondary` 两档模型，没有 opus/sonnet/haiku 三级
- 伏笔「超期」有**两个互不等价的维度，可同时为真**，接口分开给、界面分别提示：
  - `overdue`：体检口径，与 `novel_audit` 的 `foreshadow_overdue` 逐条一致——状态为「已埋」、埋设章 > 0，且最近提交章 − 埋设章**严格大于** `FORESHADOW_OVERDUE_CHAPTERS`（20 章，正好等于不算超期）；埋设章非法时 audit 跳过，面板同样不报。
  - `planned_overdue`：写作口径——`planned_resolution_chapter` 已小于 `next_chapter`（即 `last_committed_chapter + 1`）却仍标「已埋」，看的是「承诺在第 N 章回收却没回收」，与埋设章搁置多久无关。
  - `FORESHADOW_FIELDS` 契约已含 `planned_overdue`（与 `id` / `summary` / `status` / `importance` / `planted_chapter` / `planned_resolution_chapter` / `updated_chapter` / `overdue` / `stale_chapters` 同一行返回），前端类型 `NovelWorkbenchForeshadowDto` 同步声明，新增字段时三处（service 契约 / 测试断言 / d.ts）要一起改。

## 5. Agent 转换映射（已写入 plugins/novel_workspace/agents/）

| Agent | base_type | model | allowed_tools |
|---|---|---|---|
| story-architect | general | inherit | read_file, glob_files, search_code, write_file, edit_file |
| narrative-writer | general | inherit | 上者 + execute_command |
| character-designer | general | inherit | read_file, glob_files, search_code, write_file, edit_file |
| story-researcher | general | inherit | 上者 + search_news, fetch_url |
| consistency-checker | review | secondary | read_file, glob_files, search_code |
| chapter-extractor | explore | secondary | read_file, glob_files, search_code |
| story-explorer | explore | secondary | read_file, glob_files, search_code |

## 6. 遗留问题（按优先级）

1. **后端全量回归在本机跑不动**：334 个测试文件、约 7200 项，后台跑 20 分钟只到 39%，已终止。本轮以「小说三件套 + gui_bridge 决策」84 项与 turn_undo 97 项替代验证，全部通过。换机器或空闲时段建议补跑 `python -m pytest -q -m "not slow"`。
2. **`ai_patterns` 是本地启发式扫描**：覆盖一级禁用词、套句式与章末预告，不等价于上游 `check-ai-patterns.js` 的全量规则（退化检测、大纲契约未覆盖）。要完全对齐仍需 vendor 上游 JS 检查器。
3. **导出只有 md/txt**：没有 docx/epub、没有按卷拆分，发布平台对接未做。
4. **多书发现受深度限制**：默认只扫工作区下 2 层，命中 `.novel.json` 即停止下钻；书放在更深层级时需手动切换工作区。
5. **编辑器不新建章节**：`save_chapter` 只覆盖已存在章节，新章仍走 Agent 写作流程。
6. **历史遗留已清**：`pywebview.d.ts` 类型声明、设计文档 §5.3 措辞、建书自包含测试契约、revision 与 `scope="chapter"` 的 e2e 覆盖，均已在 P3 阶段修复。

## 7. 下一步（可选增强）

- 技能按项目激活：13 个 story 技能的描述目前常驻系统提示，写代码时是噪音
- 模型分级：给 Agent 支持显式 model id，替代二档 `inherit/secondary`
- 导出扩展：docx/epub、按卷拆分、发布平台对接
- vendor 完整 JS 检查器（退化检测、大纲契约）

## 8. 常用命令

```powershell
# 小说相关测试（后端）
python -m pytest tests/test_novel_service.py tests/test_novel_e2e.py tests/test_novel_bridge.py -q

# 前端页签测试
npm --prefix frontend run test -- NovelPane

# 前端全量（约 65s）
npm --prefix frontend run test

# 后端全量（本机跑不完，慎用）
python -m pytest -q -m "not slow"
```
