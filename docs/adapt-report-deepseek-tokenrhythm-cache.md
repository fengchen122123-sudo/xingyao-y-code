# TokenRhythm 网关 DeepSeek 缓存命中率实测报告

> 生成时间：2026-09-23 | 测试对象：TokenRhythm（基元律动）网关的 DeepSeek 系列 | 测试方：Y-code 编码 agent（星瑶）

## 1. 测试目的

1. 确认 TokenRhythm 网关确实返回服务端前缀缓存用量（回答设计文档 §5 风险表第 1 行
   「tokenrhythm 上游未必是官方 DeepSeek 磁盘缓存」）；
2. 给出该网关的**上游缓存上限**，作为线上命中率验收的参照天花板。

## 2. 方法与口径

| 项目 | 值 |
|---|---|
| 脚本 | `bench/adapt_bench.py`（真实 API，`stream=False`） |
| 端点 | `https://tokenrhythm.studio/v1` |
| 模型 | `openai/deepseek-v4-flash-0731` |
| 参数 | `--rounds 3 --tool-rounds 3 --context 3000` |
| 改造前源码 | 基线提交 `419f14e`（本次改造实施前的 dev） |
| 改造后源码 | 提交 `c752110`（三项改造落地后） |
| 数据文件 | `mytemp/bench_tr_before.json`、`mytemp/bench_tr_after.json`（`mytemp/` 被 `.gitignore` 忽略，故只将数字抄录于本报告） |
| 命中率口径 | `cached_tokens / prompt_tokens`，与 `token_usage` 表统计口径一致 |

## 3. 结果

### 3.1 逐轮明细（场景一/场景二）

**改造前（基线 419f14e）**

| 场景 | 轮次 | 步骤 | prompt | completion | cached | 命中率 |
|---|---|---|---|---|---|---|
| chat | 1 | reply | 1249 | 40 | 1024 | 82.0% |
| chat | 2 | reply | 1310 | 40 | 1024 | 78.2% |
| chat | 3 | reply | 1371 | 40 | 1280 | 93.4% |
| tool | 1 | tool_call | 1561 | 183 | 1536 | 98.4% |
| tool | 1 | tool_result | 1758 | 218 | 1536 | 87.4% |
| tool | 2 | tool_call | 2254 | 218 | 1536 | 68.2% |
| tool | 2 | tool_result | 2093 | 427 | 1536 | 73.4% |
| tool | 3 | tool_call | 2798 | 100 | 1536 | 54.9% |
| tool | 3 | tool_result | 2637 | 56 | 2048 | 77.7% |

**改造后（c752110）**

| 场景 | 轮次 | 步骤 | prompt | completion | cached | 命中率 |
|---|---|---|---|---|---|---|
| chat | 1 | reply | 1249 | 47 | 0 | 0.0% |
| chat | 2 | reply | 1317 | 53 | 1024 | 77.8% |
| chat | 3 | reply | 1391 | 50 | 1280 | 92.0% |
| tool | 1 | tool_call | 1561 | 183 | 1024 | 65.6% |
| tool | 1 | tool_result | 1758 | 297 | 1024 | 58.3% |
| tool | 2 | tool_call | 2333 | 231 | 1536 | 65.8% |
| tool | 2 | tool_result | 2172 | 220 | 1536 | 70.7% |
| tool | 3 | tool_call | 2670 | 232 | 1536 | 57.5% |
| tool | 3 | tool_result | 2509 | 260 | 2048 | 81.6% |

### 3.2 总体统计

| 指标 | 改造前 | 改造后 |
|---|---|---|
| 调用次数 | 9（全部成功） | 9（全部成功） |
| prompt tokens | 17031 | 16960 |
| completion tokens | 1322 | 1573 |
| total tokens | 18353 | 18533 |
| cached tokens | 13056 | 11008 |
| **总体命中率** | **76.66%** | **64.91%** |
| 场景一（连续对话 3 轮） | 3930 prompt / 3328 cached = **84.68%** | 3957 prompt / 2304 cached = **58.23%** |
| 场景二（工具调用 6 次） | 13101 prompt / 9728 cached = **74.25%** | 13003 prompt / 8704 cached = **66.94%** |

### 3.3 关于 before/after 的 12 个百分点差

两次运行的**请求形状逐轮相同**（同模型、同端点、同参数、首轮 prompt 均为 1249），
但首轮 cached 分别为 1024 与 0，`tool 1 tool_call` 分别为 1536 与 1024。这与
`bench/adapt_bench.py` 的调用方式直接相关（见 §4）：脚本本身不经过任何本次改造的代码
路径，因此该差异只能来自**上游缓存的时点状态**（同前缀在两次运行间未命中，属服务端
缓存 TTL 驱逐 / 分片命中差异），而不是改造引入的回归。

换句话说：本脚本的运行间抖动量级（±12pt）远大于本次改造在单次请求上的可观测效应，
**它既不能证明改造有效，也不能证明改造有害**。

## 4. 关键局限（必读）

`bench/adapt_bench.py` 直接 `from core.llm import completion` 并以 `stream=False` 调用
（脚本第 41 行），**不经过 `core.llm.chat_with_model_events`**，因此不经过 DeepSeek
Prompt Ledger、不经过 tools 字母序规范化、也不读取 `deepseek_cache_routes`。所以本报告的数字：

- 反映的是**上游网关自身的前缀缓存能力**（实测有效，见 §3）；
- **不能用来度量本次「路由可配置化 + 目录快照 + 换代可观测」改造的收益**。

改造收益的证据来自：① `tests/test_deepseek_prompt_ledger.py` 中配置路由进账本与
tools 排序的测试；② `tests/test_session_stable_snapshot.py` 的快照不换代测试；
③ 线上 `token_usage` 表按 `api_base = https://tokenrhythm.studio/v1` 的按日命中率（§6）。

## 5. 线上历史基线（改造前，只读 `~/.ycode/ycode.db`）

采集时间：2026-09-23 11:44（本地）。脚本：`~/.ycode/tmp/deepseek_hitrate.py`、`~/.ycode/tmp/deepseek_routes.py`。

### 5.1 按模型

| 模型 | 记录数 | prompt | cached | 命中率 |
|---|---|---|---|---|
| deepseek-v4-flash-0731 | 578 | 58916672 | 51914880 | 88.12% |
| deepseek-v4-pro-0813 | 67 | 3027274 | 2854144 | 94.28% |
| deepseek-flash | 16 | 490602 | 450048 | 91.73% |
| **合计（模型名以 deepseek 开头）** | **661** | **62434548** | **55219072** | **88.44%** |

日期范围 2026-08-05 ~ 2026-09-17；近 7 天 91.73%（16 条）、近 30 天 93.93%（83 条）。

### 5.2 按 api_base

| api_base | scope | 记录 | prompt | 命中率 |
|---|---|---|---|---|
| `https://tokenrhythm.studio/v1` | main | 435 | 44143562 | 88.35% |
| （空） | legacy | 226 | 18290986 | 88.68% |

### 5.3 单条分布（prompt>0）

| 区间 | 条数 | 占比 |
|---|---|---|
| =0%（冷启动） | 8 | 1.2% |
| 0~50% | 51 | 7.7% |
| 50~90% | 35 | 5.3% |
| 90~99% | 190 | 28.7% |
| >=99% | 377 | 57.0% |

剔除冷启动后的加权命中率 89.02% —— 低命中记录集中在大 prompt，与设计文档 §1.1 的判断一致。

### 5.4 诊断表现状

`cache_request_diagnostics` 实测 **1087 行**，其中 `api_base = https://tokenrhythm.studio/v1`
689 行、`https://api.kimi.com/coding/v1` 342 行。**该表写入路径本就无路由门禁**，设计文档
§3.1-8「deepseek 家族零记录」不成立；改造前的 `request_reason` 分布为：空 1044、冷启动 28、
上下文压缩或历史重写 14、工具契约变化 1。

## 6. 结论

1. **TokenRhythm 网关确认返回 `cached_tokens`**：两次实测均观测到服务端前缀缓存生效
   （改造前总体 76.66%、改造后 64.91%），上游确为 DeepSeek 磁盘前缀缓存语义，设计文档
   §5 风险表第 1 行的顾虑可以排除。
2. **before 与 after 不一致，但这不是改造收效的证据，也不是回归**：两者差异（76.66% vs
   64.91%）由上游缓存时点状态造成，量级来自 §3.3 的运行间抖动；本脚本按 §4 的构造绕开了
   本次改造的全部代码路径。
3. **本次改造的收益以线上口径验收**：`token_usage` 表（模型名以 deepseek 开头、按
   `api_base = https://tokenrhythm.studio/v1` 拆分）的按日命中率，对照设计文档 §1.2 目标
   （热路径 ≥97~99%、总体 ≥93~96%）观察 3~7 天；低命中事件按
   `cache_request_diagnostics.request_reason` 的换代原因分布归因
   （`first_generation` / `pending_rotation` / `caller_force_new` / `header_tools_changed` /
   `header_system_changed` / `boundary_ambiguous`）。
