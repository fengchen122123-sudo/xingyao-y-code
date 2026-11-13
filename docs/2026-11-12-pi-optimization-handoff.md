# pi 借鉴优化 · 实施交接

> 日期：2026-11-12
> 设计文档：`docs/2026-11-12-pi-inspired-optimization-design.md`
> 状态：S1、S3 完成；S2 只完成统计层，运行器与用例集未做

## 一、这次做了什么

### S1 成本账本（完成）

让每一轮对话能算出花了多少钱，而不是只知道 token 数。

| 文件 | 变更 |
|---|---|
| `core/cost_model.py` | **新增**。价格模型（`PriceTier` / `ModelPrice` / `CostBreakdown`）、价格解析、阶梯计价、跨币种聚合。纯标准库、纯函数 |
| `core/model_spec_table.py` | **新增** `MODEL_PRICES = {}`（内置价格表，当前故意留空，见下文「必须知道的坑」） |
| `core/memory.py` | `token_usage` 表新增 `cost_micros` / `cost_currency` / `price_source` 三列（全部 nullable，`ALTER TABLE` 迁移，旧库打开自动升级）；`add_token_usage` 新增三个可选参数；`query_token_usage_by_day_and_model` 返回 `cost_micros` / `unpriced` / `cost_currency` |
| `core/llm_diagnostics.py` | 新增 `_resolve_model_price` / `compute_cost_fields`；`_persist_token_usage` 在记账时算成本（无价时不写成本键，保持旧契约） |
| `core/model_manager.py` | `get_active_model` 返回值透传模型级 `price` 块 |
| `core/tools_admin_ops.py` | `/usage` 统计（CLI 与工具共用）新增 `cost_micros` / `cost_text` / `unpriced`，跨币种不混算 |
| `gui_bridge.py` | `get_token_usage_statistics`（GUI 用量面板）补同样的成本聚合与 `cost_text`，与 CLI 口径一致 |
| `tests/test_cost_model.py` | **新增** 22 个用例：计价、阶梯边界、缺失字段、币种、解析校验、记账集成 |
| `tests/test_token_usage.py` | 既有精确断言同步新增三个字段（契约变更，非放水） |

**关键语义（改代码前务必知道）**：

- 价格单位是**微元/百万 token**：1 元/百万 token 记为 `1_000_000`，计费 `round(tokens * price / 1_000_000)`，全程整数。
- **阶梯按整单命中**：用 `input + cache_read + cache_write` 总量找第一档，整单按该档价，不做逐 token 分段。
- `prompt_tokens` 含缓存命中与缓存写入，计费前先扣减，避免重复计费（`compute_cost_fields` 里做）。
- **未计价落 NULL，绝不写 0**。`unpriced` 单独计数，避免「没配价」看起来像「省钱」。

### S3 依赖体积基线（完成）

| 文件 | 变更 |
|---|---|
| `scripts/architecture_report.py` | 新增 `closure` 子命令：复用既有 `collect_dependencies` 的 ast 解析，算每个入口的传递依赖闭包（模块数 / 行数 / 最大深度）；超基线 +10% 告警并退出 1；`--check` 只读 |
| `docs/architecture_baseline.json` | 新增 `closures` 节（保留既有 `files` 节） |
| `tests/test_architecture_closure.py` | **新增** 6 个用例：闭包计数、忽略三方库、增长率告警、首次登记不告警、写回保留 files、确定性输出 |

跑法：

```powershell
venv\Scripts\python.exe scripts\architecture_report.py closure --json
venv\Scripts\python.exe scripts\architecture_report.py closure --check   # CI 用
```

**当前实测基线**（2026-11-12）：

| 入口 | 模块数 | 行数 | 最大深度 |
|---|---:|---:|---:|
| core.agent_service | 177 | 92927 | 7 |
| core.llm | 177 | 92927 | 6 |
| core.memory | 177 | 92927 | 7 |
| core.tools | 177 | 92927 | 7 |

四个入口闭包完全相同，说明 `core/` 内部基本全连通——这本身是个值得注意的耦合信号，后续要做的是「让入口闭包有区分度」，而不是继续加基线。

### S2 配对评测（只完成统计层）

| 文件 | 变更 |
|---|---|
| `core/eval_stats.py` | **新增**。`ArmRun` / `CasePair`、按 case 配对、噪声剔除（blocked / saturated / flaky）、bootstrap 置信区间（固定种子）、`summarize`、`gate_exit_code` |
| `tests/test_eval_stats.py` | **新增** 11 个用例 |

尚未实现：`scripts/eval_run.py` 运行器、`evals/cases/*.json` 用例集与判据执行器、报告渲染（md/json）。

## 二、验证结果

```
venv\Scripts\python.exe -m pytest tests -q -k "architecture or cost or token_usage or eval_stats"
→ 129 passed, 6903 deselected
```

覆盖：新增 3 个测试文件（39 个用例）+ 既有 `tests/test_token_usage.py` 全绿。

## 三、必须知道的坑

1. **内置价格表是空的，这是故意的**。仓库里的模型名多为渠道挂载名（`deepseek-v4-pro`、`kimi-k3` 等），官方价格需要人工核对。没有任何价格时，`cost_micros` 落 NULL、`/usage` 显示「未计价」——这是正确行为，不是 bug。
   启用方式二选一：
   - 在 `config.json` 的模型条目里加 `price` 块（优先）；
   - 或往 `core/model_spec_table.py` 的 `MODEL_PRICES` 里按叶子名登记，必须带 `source` 与 `verified_at`。

   `price` 块结构：

   ```json
   {
     "currency": "CNY",
     "tiers": [
       {"up_to_input_tokens": 32000, "input": 2000000, "output": 8000000,
        "cache_read": 200000, "cache_write": 2500000},
       {"up_to_input_tokens": null, "input": 1000000, "output": 4000000,
        "cache_read": 100000, "cache_write": 1250000}
     ],
     "source": "https://...",
     "verified_at": "2026-11-12"
   }
   ```

   末档 `up_to_input_tokens` 必须为 `null`；中间档出现 `null` 会被判为非法配置并整块丢弃（宁可未计价，也不要错档）。

2. **既有测试的精确断言会被契约变更打脸**。`tests/test_token_usage.py` 与 `tests/test_gui_token_statistics.py` 里对返回字典做 `==` 比较，加字段必须同步改断言。以后改 `query_token_usage_by_day_and_model` / `get_token_usage_statistics` 的返回结构要注意同类影响。

4. **`architecture_report.py closure` 只扫单一 root**。默认 root 是 `core`，所以 `main.py` / `gui_main.py` 这类包外入口暂时进不来。若要覆盖，需要给 `_module_name` 支持空前缀（当前会拼出 `.core.x`），这是个已知的小坑。

## 四、建议的下一步（按优先级）

1. **价格数据落地**（需要你提供/确认报价）：填 `MODEL_PRICES` 或 config `price`，然后跑一轮真实对话看 `/usage` 出金额。
2. **S2 运行器**（1~2 天）：`scripts/eval_run.py` + 3 个样例 case，先用「临时工作区 + 子进程」跑，容器化留作可选后端。
3. **入口闭包区分度**：四个入口闭包完全一样，说明 `core/llm` 被 `core.tools`/`core.memory` 反向依赖。如果要降低启动耦合，这是切入点。
4. **前端展示**：`cost_text` 已由后端返回，前端面板按需渲染即可（后端字段名固定为 `cost_text` / `unpriced`）。

## 五、恢复工作的第一步

```powershell
cd D:\fengchenhao\ycode\xingyao-y-code
venv\Scripts\python.exe -m pytest tests -q -k "cost or eval_stats or architecture"
venv\Scripts\python.exe scripts\architecture_report.py closure --check
```

两条都通过，说明当前状态干净，可以继续往上叠。

## 六、本次提交范围说明

工作区里还有**不属于本次任务**的改动（`core/turn_undo/*`、`frontend/*`、`core/tools.py`、`core/hunyuan_image.py`、`tests/test_turn_undo*`），提交时**没有**包含它们。如果换机器后看到这些文件是「未提交状态」，那是正常的。

本次提交包含：

```
core/cost_model.py                     新增
core/eval_stats.py                     新增
core/memory.py
core/llm_diagnostics.py
core/model_manager.py
core/model_spec_table.py
core/tools_admin_ops.py
gui_bridge.py
scripts/architecture_report.py
docs/architecture_baseline.json
docs/2026-11-12-pi-inspired-optimization-design.md   新增
docs/2026-11-12-pi-optimization-handoff.md            新增（本文件）
tests/test_cost_model.py               新增
tests/test_eval_stats.py               新增
tests/test_architecture_closure.py     新增
tests/test_token_usage.py
tests/test_gui_token_statistics.py
```

## 七、第二批改动（2026-11-12 下午补记）

### 已完成

| 文件 | 变更 |
|---|---|
| `main.py` | CLI `/usage` 表格新增「费用」列（`_format_usage_cost` + `_render_usage_section`），无价显示 `—`、部分未计价加 `*` 并在表下提示条数；状态栏追加累计费用片段（`_compose_status_cost_segment`） |
| `core/llm_diagnostics.py` | `session_token_usage` 新增 `cumulative_cost_micros` / `cumulative_cost_currency` / `cumulative_cost_unpriced`，记账时累加；**跨币种先归零再累加**，不混算 |
| `gui_bridge.py` | `get_token_usage` 暴露 `cost_micros` / `cost_currency` / `cost_unpriced`（取自已持锁的快照） |
| `scripts/architecture_report.py` | `closure` 默认 `--root` 从 `core` 改为仓库根 `.`，支持 `main` / `gui_main` 入口；`plugins.*` 进入依赖图；新增 `CLOSURE_EXCLUDED_DIRS` 排除 venv/node_modules 等 |

验证：`pytest tests/test_cost_model.py tests/test_token_usage.py tests/test_session_cost_accumulation.py tests/test_gui_token_statistics.py tests/test_architecture_closure.py tests/test_config_key_validation.py -q` → **119 passed**。

### 未完成（如实记录）

1. **前端用量面板费用列**：未做。`frontend/` 里没有任何 `cost_text` 引用。方案已定：`tokenStatistics.ts` 加 `formatCostCell(costText, unpriced)`；`TokenUsageSettings.vue` 的 model-list 加费用列；`pywebview.d.ts` 的 `TokenStatisticsMetrics` 补可选字段。**注意**：现有测试断言摘要条固定 5 格、`find('table')===false`，改动不能破坏它们。
2. **S2 评测运行器**：`scripts/eval_run.py` 未落盘（曾因目录写锁连续失败）。统计层 `core/eval_stats.py` 已完成可用。落地只需：`load_cases` → `check_assertions`（file_exists / file_not_exists / file_contains / file_not_contains / tool_called）→ `core.eval_stats.group_runs` / `summarize` → 报告 md/json。
3. **closure 基线未按新口径重算**：`docs/architecture_baseline.json` 的 `closures` 节仍是旧口径（core root，177 模块）。需跑一次：
   `venv\Scripts\python.exe scripts\architecture_report.py closure`
   注意 `--check` 在重算前可能因口径差异报增长（+1.13%，未超 10% 阈值，实际不会告警）。

### 关于计划状态的说明

本轮执行中出现过计划项被标记 `completed` 但实际未落盘的情况（P3 前端、P5 运行器）。以上「未完成」清单以**磁盘实际状态**为准，不以计划状态为准。


## 八、第三批改动（S2 运行器 + 前端工具层）

### 已完成

| 文件 | 变更 |
|---|---|
| `scripts/eval_run.py` | **新增**。用例加载校验（id 唯一 / assertions 非空 / 未知判据在加载期报错）、5 种确定性判据（file_exists / file_not_exists / file_contains / file_not_contains / tool_called，路径越界判失败）、`--dry-run` + `--simulate A=1,0`、复用 `core.eval_stats` 出配对报告（json + md）、退出码 0/2 门禁 |
| `evals/cases/smoke/bom.json` + `fixtures/bom/doc.md` | **新增** 首个样例用例 |
| `tests/test_eval_run.py` | **新增** 9 个用例：重复 id / 未知判据 / 空 assertions / 判据正反例 / 路径越界 / tool_called / dry-run 端到端 / 模拟序列不足报错 |
| `frontend/src/utils/tokenStatistics.ts` | 新增 `formatCostCell(costText, unpriced)` 与 `COST_UNPRICED_TEXT`：未计价显示 `—`，含未计价记录加 `*` |
| `frontend/src/utils/tokenStatistics.test.ts` | 新增 3 个用例 |
| `frontend/src/types/pywebview.d.ts` | `TokenStatisticsMetrics` 补可选 `cost_micros` / `cost_currency` / `cost_text` / `unpriced` |

验证：
- `pytest tests/test_eval_run.py tests/test_eval_stats.py -q` → **20 passed**
- `npm --prefix frontend run test -- --run src/utils/tokenStatistics.test.ts` → **39 passed**
- CLI 实跑：`python scripts/eval_run.py --dry-run --cases evals\cases --profile A=x --profile B=y --runs 1 --simulate "A=1" "B=0" --out <tmp>` → `paired=1 informative=1 delta=1.000 verdict=ok`，退出码 0

### 未完成：前端面板把费用显示出来（一行改动）

`frontend/src/components/TokenUsageSettings.vue` **被另一回合持续占用**，本轮尝试写入 4 次全部被拒（`edit_file` 报「文件在本次读取后已被外部修改」），因此没能接上。

当前该文件状态：
- **已加**：`formatCostCell` 的 import（第 232 行附近）
- **未加**：`donutRows` 里缺 `cost: formatCostCell(model?.cost_text, model?.unpriced ?? 0),`，模板 `model-row` 里也还缺渲染 `row.cost` 的位置

补法（3 分钟）：
```ts
// TokenUsageSettings.vue，donutRows 返回值里加
cost: formatCostCell(model?.cost_text, model?.unpriced ?? 0),
```
```html
<!-- model-row 内，model-meta 之后 -->
<span class="model-cost">{{ row.cost }}</span>
```
注意：现有测试断言摘要条固定 5 格、`find('table')===false`，新增 span 不要动这两处结构。

**另外**：该文件现在有一个「已 import 未使用」的 `formatCostCell`，在接上之前 eslint 可能报 unused，属已知中间态。


## 九、前端面板费用列已接上（补记）

`frontend/src/components/TokenUsageSettings.vue` 已补完：
- `donutRows` 增加 `cost: formatCostCell(model?.cost_text, model?.unpriced ?? 0)`
- 模型行副文案追加 `· 费用 ¥0.0123`（未配价显示 `—`，含未计价记录加 `*`）
- 修复了此前编辑造成的 import 缩进损坏（`formatDayFull` 行丢失缩进、`const store` 前空行被吞）

新增用例 `模型行展示费用，未计价显示破折号并标注星号`。验证：
`npm --prefix frontend run test -- --run src/components/TokenUsageSettings.test.ts` → **8 passed**

### 剩余（都不影响已有功能）

- 价格数据：`MODEL_PRICES` 仍为空，界面会显示「—」。填入报价即可生效（config 的 `price` 块优先）。
- 设计文档 4.6 第 7 条的「整轮 e2e 成本测试」未写（约 20 分钟）。
- S2 运行器只支持 `--dry-run`；真实 provider 调用按其设计显式 NotImplementedError。


## 十、范围变更：成本账本与配对评测已下线（2026-11-13）

产品决策：本项目是免费开源 agent，模型与渠道由用户自行配置，**我们拿不到可信价格**，因此：

- **成本账本（S1）整体下线**：删除 `core/cost_model.py`、成本三列迁移、CLI 费用列、状态栏金额、GUI/前端费用展示、`MODEL_PRICES`、config 的 `price` 键白名单。
- **配对评测（S2）整体下线**：删除 `core/eval_stats.py`、`scripts/eval_run.py`、`evals/` 目录与相关测试。理由是「同一任务跑多次比对」会让开源用户实际多花 token，与定位冲突。
- **保留**：`token_usage` 表的原有 token 记录（prompt/completion/cached/cache_write/total）、`/usage` 统计、GUI 用量面板——这些都不受本次下线影响。
- **保留**：S3 依赖体积基线（`scripts/architecture_report.py closure` 与 `docs/architecture_baseline.json` 的 `closures` 节），与成本无关。

回退方式：上述 16 个文件已回退到 `5364c8ff`（本次工作开始前的状态），验证：
- `pytest tests/test_token_usage.py tests/test_gui_token_statistics.py tests/test_config_key_validation.py -q` → **78 passed**
- `vitest run src/utils/tokenStatistics.test.ts src/components/TokenUsageSettings.test.ts` → **43 passed**
- 全仓搜索 `cost_model|eval_stats|cost_micros|cost_text|MODEL_PRICES` → 零命中
