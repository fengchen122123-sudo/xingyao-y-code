# 子代理面板扁平时间线改版（方案 B 定稿）

日期：2026-09-28
状态：已确认（用户在 A/B/C 三方案 HTML 对比页中选定方案 B）
对比页：`mockups/subagent-pane-redesign.html`（可交互预览，含运行中/已完成两场景）

## 背景与问题

GUI 右面板「子代理」工作台（`frontend/src/components/SubagentPane.vue`）原样平铺后端
`stages` 事件流水账：

- 每次工具调用占两行（`tool_started` 一行、`tool_finished` 一行），开始/结束不配对；
- 每轮之间穿插「调用模型 / 思考中 / 生成回复」模板行，与工具行同样式等权重；
- 一个 10 次工具调用的子代理产生 40+ 行同质文本，无层级、无统计，长摘要挤断行。

## 目标（方案 B：扁平轻整理）

保持时间顺序原貌，不引入分组结构，只做信息分层：

1. **工具开始/结束配对成一行**：行锚定在开始时刻；完成显示 `✓ 耗时`（绿）、
   失败显示 `✗ 摘要`（红，允许换行）、未返回显示实时 `运行 Ns`（橙）。
2. **模板阶段弱化**：`model/thinking/composing/summarizing` 等渲染为小号灰行；
   `waiting_approval` 单独琥珀色提亮。
3. **当前行标记**：任务未到终态时最后一行左侧加 accent 色条。
4. **头部任务卡增强**：状态徽标 + 类型/后台徽标 + 统计行
   （已用 `2m18s` 格式、开始→结束时刻、轮次数、工具成功/失败计数）。
5. **执行结论卡加复制按钮**（`navigator.clipboard`，1.5s 反馈提示）。
6. 排队显示：`started_at` 从获得并发名额时刻起算（后端现状），排队期不污染耗时。

不做的事（用户明确未选）：轮次分组折叠（方案 A）、仪表盘大卡（方案 C）、
时间线区域独立滚动与吸底（随 A/C 一并搁置）。

## 实现边界

纯前端改造，不动 `gui_bridge.py` / `core/tools.py`：

- `frontend/src/utils/toolTrace.ts` 新增纯函数 `buildSubagentFlatView(stages)`：
  - `tool_started`（detail=工具名）→ `running` 行并入 `pending` 表；
  - `tool_finished`（detail=`"name ✓ 0.8s"` / `"name ✗ 摘要"`）→ LIFO 配对最近同名
    running 行；开始行被后端 64 条上限淘汰时就近配对，仍无则生成孤立完成行；
  - `model` 阶段计数为轮次；✓/✗ 完成行分别计数；
  - 输出 `{ rows, rounds, toolOk, toolErr }`。
- `SubagentPane.vue` 重写模板/样式；轮询、切换条、终态区块逻辑保持原结构。
- 失败原因 / 执行结论 / 过程产出三区互斥显示逻辑不变。

## 测试

- `toolTrace.test.ts`：配对锚定开始时刻、未完成 running、失败摘要、同名工具
  顺序执行 LIFO、64 条淘汰后的孤立完成行、轮次/计数、空输入。
- `RightPanel.test.ts`：子代理页签用例改断言 `.suba-row`，payload 补
  start/finish 配对与头部统计（`轮次 1`、`工具 1 成功`）。

## 关联调研（另行推进，不在本次样式改动内）

用户同报「子代理经常超时失败」。已核实 ycode 超时为**按角色固定总时长**
（explore 240s / review 300s / test 480s / implement/general 600s，
`core/tools.py:4302`），看门狗对在飞 LLM 请求与 API 重试照常计时；而
Claude Code/ZCode 采用**无总时长上限 + 静止看门狗**（默认 10 分钟无流式输出
才终止）。结论与建议（总时长→无进展语义、schema 暴露 timeout、重试暂停计时）
见会话报告，后续单独立项。
