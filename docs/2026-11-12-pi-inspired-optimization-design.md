# 借鉴 pi 的三项能力建设 · 设计文档

> 状态：待评审
> 日期：2026-11-12
> 作者：星瑶
> 关联调研：pi（earendil-works/pi，MIT）源码分析，快照见 `C:\Users\冯晨昊\.ycode\tmp\pi-src`

## 1. 背景

对 pi 做过一次完整源码调研（12 包、约 54 万行 TS）。结论是：**我们的产品完整度高于 pi**（权限审批、计划模式、子代理、工具输出结构化精简、预压缩水位、闲时维护这些它都没有或更弱），但它在三件底层工程方法上明显更成熟：

1. **模型差异数据化**：每个模型带 `cost.tiers`、`thinkingLevelMap`、`promptCache` 秒数、`compat` 方言声明，由构建期目录生成，运行期只读。
2. **评价统计化**：evals 是配对对照实验，blocked pair 直接扣住结论并让进程非零退出，宁可不给结论也不给被污染的结论。
3. **依赖与体积门禁化**：把包的 exports 入口当性能契约卡传递依赖图体积，配合钉版校验与 install-lock。

我们不照抄它的实现（自研 schema/TUI、零权限内核、durable 事件溯源都不适合我们），只借这三条**方法**。

## 2. 设计原则

- **不引入新运行时依赖**：全部基于标准库 + 现有 pytest/rich 栈。
- **不猜测**：沿用 `core/model_spec_table.py` 已有原则「未收录一律回退默认，绝不猜测」。价格缺失即 NULL，绝不填 0。
- **未报告 ≠ 0**：沿用 `token_usage.cache_write_tokens` 的 NULL 语义（`core/memory.py:988-991`）。
- **可开关、可回滚**：新能力默认旁路，配置项缺省即旧行为。
- **术语本地化**：不叫 tiers/evals/entry-graph，分别叫「阶梯计价」「配对评测」「依赖体积基线」。

## 3. 子项目拆分

| 编号 | 名称 | 一句话 | 依赖 | 预估 |
|---|---|---|---|---|
| S1 | 成本账本 | 让每一轮对话能算出花了多少钱 | 无 | 1 天 |
| S2 | 配对评测 | 让「这个改动变好了」有数据支撑 | 无 | 2~3 天 |
| S3 | 依赖体积基线 | 防 `core/` 依赖图悄悄膨胀 | 无 | 0.5 天 |

三者互相独立，可分别立项、分别回滚。建议顺序 S1 → S3 → S2（S1 收益最直接，S3 最便宜，S2 最重）。

---

## 4. S1 成本账本

### 4.1 现状与缺口

- `token_usage` 表（`core/memory.py:964-980`）记 `prompt/completion/cached/cache_write/total` 与 `api_base`、`cache_scope`，**没有任何价格字段**。
- 全仓无 `estimate_cost` 一类函数（搜索无命中），所以 GUI/`/usage` 只能给 token 数。
- `core/model_spec_table.py:28` 的 `MODEL_SPECS` 只有 `(上下文窗口, 最大输出)` 二元组。
- 模型配置项只有 `id / context_window / max_tokens`（见 `config.example.json`）。

结论：不是「算得不准」，是**根本算不出**。

### 4.2 数据模型

新增 `core/cost_model.py`（只依赖标准库），定义三样东西：

```python
@dataclass(frozen=True)
class PriceTier:
    up_to_input_tokens: int | None   # None 表示该档无上限（末档）
    input: int                        # 微元/百万 token
    output: int
    cache_read: int
    cache_write: int

@dataclass(frozen=True)
class ModelPrice:
    currency: str                     # "CNY" | "USD"，不同币种不混算
    tiers: tuple[PriceTier, ...]      # 按 up_to 升序，末档 up_to=None
    source: str                       # 价格来源 URL 或说明，便于追溯
    verified_at: str                  # "2026-11-12"
```

- 价格统一以**微元/百万 token** 存储：1 元/百万 token 记为 `1_000_000`。计费公式 `cost_micros = round(tokens * price / 1_000_000)`，全程整数运算；落库的 `cost_micros` 是微元整数，避免浮点累计误差。
- 分层计价规则采用 pi 的可解释版本：**按本次请求的 `input + cache_read + cache_write` 总量命中档位，整单按该档单价计**（不做逐 token 分段，简单且与厂商账单口径一致）。这条要写进函数 docstring，防止后人误解。
- 1 小时缓存写入加价：本期**不做**。现有 `_extract_cache_write_tokens`（`core/llm_diagnostics.py:911-933`）只取合并值，拆不出 1h/5m，硬做必然失真。记为后续项。
- 服务端回退模型重算成本：本期**不做**（我们没有 Anthropic 那种 fallback 场景）。若将来接入，`compute_cost` 已是纯函数，传入回退模型的 `ModelPrice` 即可。

### 4.3 价格来源与配置

两级来源，就近优先：

1. **用户级**：`config.json` 的模型条目新增可选 `price` 块（结构同上，字段名用 snake_case）。用于用户自建中转站/私有渠道。
2. **内置级**：`core/model_spec_table.py` 增加 `MODEL_PRICES` 字典，键为模型叶子名小写，值同结构。只收录能给出官方出处的模型，每条带 `source` 与 `verified_at`。

查价顺序：模型配置 → `MODEL_PRICES`（按叶子名，大小写不敏感）→ 命中不到返回 `None`。**绝不按同系列推测**（例如没有 `kimi-k3` 的价格就不许套 `kimi-k2.6`）。

### 4.4 计算与落库

新增纯函数：

```python
def compute_cost(usage: UsageLike, price: ModelPrice | None) -> CostBreakdown | None
```

- `usage` 只读 `input_tokens / output_tokens / cache_read_tokens / cache_write_tokens` 四个数，缺失按 0 参与计算但**在结果里标记 missing**。
- `price is None` → 返回 `None`，调用方落 NULL。
- 返回 `CostBreakdown(total_micros, input_micros, output_micros, cache_read_micros, cache_write_micros, currency, tier_index)`。

存储改动（`core/memory.py`）：

- `token_usage` 表新增三列：`cost_micros INTEGER`、`cost_currency TEXT`、`price_source TEXT`。
- 迁移方式沿用现有 `PRAGMA table_info` + `ALTER TABLE` 模式（`core/memory.py:976-995` 已有先例），**三列全部可空，不加默认值**。
- `add_token_usage`（`core/memory.py:1464`）新增三个同名可选参数，缺省 `None`，保证现有调用点零改动。
- `_persist_token_usage`（`core/llm_diagnostics.py:717`）在组装参数时调用 `compute_cost`，异常一律吞掉只记 debug 日志——与现有「持久化失败不影响主流程」一致。

### 4.5 展示

- `/usage`：现有按模型/日期聚合的输出增加一列「费用」。同币种才能求和；不同币种分行显示，并明确标注「含 N 条未计价记录」。
- 会话结束的统计行：追加 `本轮 ¥0.0123`，无价则显示 `费用未配置`（不显示 ¥0）。
- GUI 用量面板：同源，只读同一聚合查询。

### 4.6 测试

单元（`tests/test_cost_model.py`）：

1. 无 price → `None`。
2. 单档价格：1000 input + 500 output 的微元结果精确等于手算值。
3. 阶梯边界：input 总量正好等于 `up_to` 时命中本档；+1 命中下一档。
4. `cache_read` / `cache_write` 分别按各自单价计，不并进 input。
5. 币种不同时聚合函数拒绝求和并返回告警标记。
6. 价格为空字典/空 tiers → 视为无效配置，返回 `None` 并记 warning。

集成（复用 `tests/test_agent_turn_e2e.py` 的 fake provider 手法）：

7. 一轮假对话跑完后，`token_usage` 里有 `cost_micros` 且与 `compute_cost` 结果一致。
8. 模型无价时该列是 NULL 而不是 0。
9. `add_token_usage` 老调用点（只传 token 参数）行为不变，回归通过现有 `tests/test_token_usage.py`。

### 4.7 不做什么

- 不做实时价格拉取（网络不稳定，且报价≠账单）。
- 不做预算强制中断（本期只做可见性；预算拦截需要新权限语义，另议）。
- 不做跨会话对账/发票导出。

---

## 5. S2 配对评测

### 5.1 目标

回答一个问题：**「我改了 prompt / 压缩阈值 / 工具描述，到底是变好还是变坏」，用配对数据回答，而不是凭感觉。**

### 5.2 任务集格式

`evals/cases/<suite>/<case-id>.json`，一份 case 一个文件：

```json
{
  "id": "edit-restore-bom",
  "prompt": "把这个文件的 BOM 保留住，只改第二行标题",
  "fixture": "fixtures/bom-file",
  "assertions": [
    {"type": "file_contains", "path": "doc.md", "value": "标题"},
    {"type": "file_not_contains", "path": "doc.md", "pattern": "^\\ufeff\\ufeff"},
    {"type": "tool_called", "name": "edit_file"}
  ],
  "timeout_seconds": 180,
  "max_cost_micros": 2000000
}
```

判据只允许确定性断言（文件内容/存在性/工具调用记录/退出码），**不允许「让模型当裁判」**——那会把评测变成另一个不确定源。

### 5.3 运行器

`scripts/eval_run.py`：

- 每个 case 拷一份 `fixture` 到临时工作区（`tempfile.mkdtemp`），以子进程方式跑一次 headless 对话回合。
- 配置从 `--profile A=config-a.json --profile B=config-b.json` 读，**只允许覆盖模型/温度/压缩阈值这类行为参数**，路径类配置由运行器强制统一，防止两个 profile 环境不同导致假差异。
- 每个 case × profile 跑 N 次（默认 3），逐 arm 记录：通过/失败、token、耗时、成本。
- 硬预算：单 case `max_cost_micros`，全套 `--max-total-cost` 超限立即停止并如实报告「预算耗尽，结论不成立」。

### 5.4 统计与门禁

- 只统计**配对完整**的 case（两侧 N 次都跑完）。单侧失败不静默当 0，而是标记 `blocked`。
- 主指标：配对通过率差值（A 通过数 − B 通过数）/ 配对数。
- 置信区间用 bootstrap（纯标准库实现，2000 次重采样），不引 scipy。
- 门禁规则：
  - `blocked` 比例 > 20% → 退出码 2，输出「结论不成立」。
  - case 在某一侧 N 次全通过或全失败 → 标记 `saturated`，不计入 headline delta。
  - 同一 profile 的 N 次结果方差过大（通过率极差 ≥ 0.5）→ 标记 `flaky`，同样不计入。
- 输出：`evals/reports/<timestamp>.md` + `.json`，md 给人看，json 给 CI 比对。

### 5.5 测试

- 用假 runner（固定返回通过/失败序列）测统计层：配对计算、bootstrap 区间、blocked 判定、saturated 判定、flaky 判定各一条。
- 用两个 profile 指向同一个 fake provider 测端到端：应输出 delta=0 且不误报提升。

### 5.6 与 pi 的差异（为什么这样改）

pi 用 Docker 镜像做隔离、每个 arm 一个容器。我们是 Python 单机应用，先用「临时工作区 + 子进程」拿到 80% 的隔离收益，容器化留作可选后端（`--sandbox docker`），不做默认依赖。

---

## 6. S3 依赖体积基线

### 6.1 现状

`scripts/architecture_report.py` 已有 `scan`（行数基线）、`deps`（循环依赖）、`map`（模块地图），基线在 `docs/architecture_baseline.json`。缺的是**传递依赖体积**：一个入口模块最终拖进来多少模块、多少字节，没有任何度量。

### 6.2 设计

在该脚本增加 `closure` 子命令（不新建文件，避免工具碎片化）：

- 输入：入口列表（默认 `main.py`、`core/agent_service.py`、`core/llm.py`、`gui_main.py`）。
- 算法：`ast` 解析 import，只看项目内模块（`core.*`、`plugins.*`），忽略标准库与三方；BFS 求闭包。
- 输出：每个入口的 `module_count`、`total_lines`、`max_depth`，写入 `docs/architecture_baseline.json` 的 `closures` 节。
- 判定：`current * 10 > baseline * 11` 告警并退出 1（沿用现有整数比较，避免浮点）。
- 只读：不修改任何被扫描源码；确定性输出（排序、LF、无时间戳），与现有约定一致。

### 6.3 为什么值得

`core/` 下已有 143 个模块、`tools.py` 单文件 5475 行。启动耗时和打包体积的退化是渐进的、没人会主动去看的——用一个基线把它钉住，比事后优化便宜得多。

---

## 7. 里程碑

| 阶段 | 交付 | 验收 |
|---|---|---|
| M1（S1） | `core/cost_model.py` + 表迁移 + `/usage` 费用列 | 新测试全绿；老 `test_token_usage.py` 无回归；一轮真实对话能看到金额 |
| M2（S3） | `architecture_report.py closure` + 基线写入 | `--check` 在人为扩依赖时退出 1 |
| M3（S2） | `scripts/eval_run.py` + 统计层 + 3 个样例 case | 假 runner 单测全绿；一次真实 A/B 能出报告 |

## 8. 风险

1. **价格数据腐坏**：模型降价/涨价后字典过期，算出错价。缓解：每条价格带 `verified_at`，超过 180 天在 `/usage` 里给出「价格可能过期」提示。
2. **评测烧钱**：S2 默认 N=3、默认关闭（需显式 `--profile`），并强制总预算上限。
3. **币种混算**：已用 `currency` 字段隔离，聚合拒绝跨币种求和。
4. **迁移风险**：三列全部 nullable + `ALTER TABLE`，旧库打开即迁移，失败可回滚（不影响原有列）。

## 9. 待拍板的三个决策点

1. **S1 价格单位**：按「元/百万 token」录入（推荐，和厂商定价页一致），还是按「元/千 token」（和部分国内厂商账单一致）？
2. **S2 默认是否进 CI**：默认只在本地手动跑（推荐，省钱），还是每次 PR 跑小规模冒烟集？
3. **S3 入口清单**：用我列的四个入口，还是你指定（比如把 `gui_bridge.py` 也纳入）？
