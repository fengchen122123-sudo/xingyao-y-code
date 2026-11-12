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

2. **GUI 用量面板还没接成本**。`tools_admin_ops.usage_stats`（CLI `/usage` 与工具共用）已经有 `cost_text`，但 `gui_bridge.get_token_usage_statistics` 走的是另一条聚合路径（`gui_bridge.py:12199`），没有成本列。下一步照搬 `_add_metrics` / `_with_cache_rate` 的改法即可。

3. **既有测试的精确断言会被契约变更打脸**。`tests/test_token_usage.py` 里对返回字典做 `==` 比较，加字段必须同步改断言。以后改 `query_token_usage_by_day_and_model` 的返回结构要注意同类影响。

4. **`architecture_report.py closure` 只扫单一 root**。默认 root 是 `core`，所以 `main.py` / `gui_main.py` 这类包外入口暂时进不来。若要覆盖，需要给 `_module_name` 支持空前缀（当前会拼出 `.core.x`），这是个已知的小坑。

## 四、建议的下一步（按优先级）

1. **GUI 成本列**（约 1 小时）：改 `gui_bridge.get_token_usage_statistics`，与 CLI 口径对齐。
2. **价格数据落地**（需要你提供/确认报价）：填 `MODEL_PRICES` 或 config `price`，然后跑一轮真实对话看 `/usage` 出金额。
3. **S2 运行器**（1~2 天）：`scripts/eval_run.py` + 3 个样例 case，先用「临时工作区 + 子进程」跑，容器化留作可选后端。
4. **入口闭包区分度**：四个入口闭包完全一样，说明 `core/llm` 被 `core.tools`/`core.memory` 反向依赖。如果要降低启动耦合，这是切入点。

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
scripts/architecture_report.py
docs/architecture_baseline.json
docs/2026-11-12-pi-inspired-optimization-design.md   新增
docs/2026-11-12-pi-optimization-handoff.md            新增（本文件）
tests/test_cost_model.py               新增
tests/test_eval_stats.py               新增
tests/test_architecture_closure.py     新增
tests/test_token_usage.py
```