# 星瑶跨渠道缓存对标报告

日期：2026-09-24
数据源：`~/.ycode/ycode_gui.db`（GUI 库，**只读**连接）
对应工作包：`待开发/跨渠道缓存命中率修复/设计规格与实施计划.md` · §五 Task 8（V7）

> **口径声明（先读这一段再看表）**
> 1. **只读**：本报告全部数字取自 `sqlite3.connect("file:...?mode=ro", uri=True)` 的只读连接，未对用户库做任何写入（未通过 `MemoryManager` 打开真实库，因为它会建表/加列）。
> 2. **未报告 ≠ 0**：`cache_reported=0` 或 `usage_valid=0` 的轮次**不进任何分母、不记 0**。计入分母的只有「已报告且有效」的轮次。
> 3. **热轮口径**：`request_reason == ''`（纯追加，客户端字节零改动）且已报告且有效且"上一轮 prompt 可判定"的轮次。冷启动与客户端换代（历史重写/工具契约变化/模型或接口变化/账本换代）**都不进热轮分母**。
> 4. **命中率一律 token 加权**：`sum(cached_tokens) / sum(prompt_tokens)`，不是逐轮命中率的算术平均。
> 5. **归因只做「客户端 vs 上游」二分**：本报告中的 `upstream_loss` 一律指"**客户端字节零改动（纯追加）但上游未命中**"，**不构成**任何"已证明的上游驱逐/淘汰"结论——渠道侧为什么丢前缀，本报告没有证据回答。
> 6. 报告只含聚合数字与车道标识，**不含任何 prompt 正文或消息内容**。

---

## 一、数据范围与车道定位

| 项 | 值 |
|---|---|
| 诊断表时间范围 | 2026-08-10 09:22 .. 2026-09-24 14:10 |
| 用量表时间范围 | 2026-08-05 12:56 .. 2026-09-24 14:10 |
| 诊断行总数 | 25,527 |
| 用量行总数 | 27,679 |

本报告覆盖三条车道（均为 `cache_scope='main'`）：

| 代号 | 模型 | api_base |
|---|---|---|
| 阿里云 | `openai/deepseek-v4.1-flash` | `https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1` |
| 基元律动 | `openai/deepseek-flash` | `https://tokenrhythm.studio/v1` |
| 官方 | `openai/deepseek-flash` | `https://api.deepseek.com` |

> 注意：阿里云车道是**多模型聚合域**（`.maas.aliyuncs.com`）。修复前它的域名兜底会把挂在该域上的 deepseek 模型一起判进 Qwen 家族——这正是本次修复的病灶（设计文档 §1.4）。

---

## 二、三车道总表（真实查询结果）

| 车道 | 诊断行 | 已报告且有效 | 未报告 | 冷启动 | 客户端换代 | **热轮分母** | **热命中率** | **上游丢前缀率** |
|---|---|---|---|---|---|---|---|---|
| 阿里云 | 518 | 518 | 0 | 7 | 2 | 509 | **88.39%** | **10.41%** |
| 基元律动 | 4,794 | 4,777 | 17 | 119 | 148 | 4,502 | **98.84%** | **0.36%** |
| 官方 | 577 | 577 | 0 | 13 | 2 | 562 | **99.42%** | **0.18%** |

（阿里云那 2 条"客户端换代"是 DeepSeek 账本换代原因 `first_generation`；基元律动的 148 条含"上下文压缩或历史重写""工具契约变化""模型或接口变化"。）

**token 权重：这些丢前缀轮占了多少**

| 车道 | 丢前缀轮 | 占本车道 prompt token | 占本车道**全部未命中 token** |
|---|---|---|---|
| 阿里云 | 53 | 10.6% | **85.0%** |
| 基元律动 | 16 | 0.2% | 6.6% |
| 官方 | 1 | 0.1% | 15.7% |

**结论（本项目最干净的一条度量）**：阿里云车道的未命中 token 有 85.0% 来自那 53 个「客户端字节零改动、上游却整段丢前缀」的轮次；同一模型挂在基元律动与官方直连上时，这个比例只有 6.6% / 15.7%（绝对量 16 轮 / 1 轮）。**渠道责任被隔离出来了。**

---

## 三、逐车道细节

### 3.1 逐轮 `cached / 上一轮 prompt` 分桶

| 车道 | ≥99% | 90–99% | 60–90% | <60% | =0 |
|---|---|---|---|---|---|
| 阿里云 | 399 | 50 | 7 | 31 | **22** |
| 基元律动 | 4,469 | 15 | 2 | 14 | 2 |
| 官方 | 560 | 0 | 0 | 1 | 1 |

形态是**整段蒸发**而不是块粒度截断：阿里云有 22 轮直接 `cached = 0`、31 轮掉到 60% 以下，而基元律动几乎全部落在 ≥99%。

### 3.2 归因分布（`request_reason`）

| reason | 阿里云 | 基元律动 | 官方 |
|---|---|---|---|
| 纯追加（空 reason） | 509 | 4,527 | 562 |
| 冷启动 | 7 | 119 | 13 |
| 上下文压缩或历史重写 | 0 | 137 | 1 |
| 工具契约变化 | 0 | 4 | 0 |
| 模型或接口变化 | 0 | 7 | 1 |
| 账本换代（`first_generation`） | 2 | 0 | 0 |
| **未报告**（`cache_reported=0`/`usage_valid=0`） | 0 | 17 | 0 |

**阿里云车道的客户端侧极其干净**：509/518 是纯追加、0 条历史重写。也就是说它那 10.41% 的丢前缀**不是星瑶自己改历史造成的**（这一条与设计文档 §1.3 的结论一致，本轮用新数据复核确认）。

**未报告轮次处理**：只有基元律动 main 有 17 条（占 0.35%），它们既不进热轮分母、也没有被记成 0 命中。另有一条本地 vLLM 车道（非本报告范围）是 71/71 全 NULL 的典型 `unreported` 形态。

### 3.3 按日趋势（关键：渠道在波动，不要用单点当结论）

阿里云车道「纯追加轮 / 丢前缀轮 / 率」：

| 日期 | 热轮分母 | 丢前缀 | 丢前缀率 |
|---|---|---|---|
| 09-23 | 181 | 34 | **18.78%** |
| 09-24 | 330 | 19 | **5.76%** |

同日 `token_usage` 的当日命中率：09-23 = 76.80%（186 轮）、09-24 = **92.68%**（333 轮）。

基元律动按日（09-10 .. 09-23）：0.00% / 0.83% / 0.21% / 0.00% / 0.00% / 0.00% / 0.00% / 0.00% / 0.00% / 1.03% / 0.00%。

> **必须连带读的局限**：阿里云车道的表现**在两天之间变化了 13pp**。所以"阿里云低"这个方向可信，但"低多少"会波动，任何单日快照都不能当基线；这也意味着修复效果的判定**只能在修复切点之后用足够长的观察窗**来做。

---

## 四、保活独立命中率（`cache_scope='keepalive'`，来自 `token_usage`）

| 车道 | 轮次 | prompt | cached | 独立命中率 |
|---|---|---|---|---|
| 阿里云 / `deepseek-v4.1-flash` | 177 | 17,817,940 | 6,221,952 | **34.92%** |
| 基元律动 / `deepseek-flash` | 61 | 3,368,268 | 3,036,416 | **90.15%** |
| 官方 / `deepseek-flash` | 64 | 8,984,092 | 8,364,672 | **93.11%** |
| 对照组：阿里云 / `qwen3.8-flash` | 23 | 1,621,086 | 1,270,566 | 78.38% |

**这是 Task 5 证据门的实测基线**：按「命中率 < 0.5 且轮次 ≥ 10」的双条件，阿里云车道（177 轮 / 34.92%）**应当被止损**，基元律动（61 轮 / 90.15%）与官方（64 轮 / 93.11%）**应当保留**。三者的判定方向与实测一致。

> 注意：Task 5 的证据门读的是**进程内** `_cache_usage_states` 聚合（不读库、不改记账），因此每次进程重启都从零开始观察——该车道累计到 ≥10 轮保活后才会触发判定。这是刻意的取舍：热路径上不做 O(桶数) 之外的 I/O。

---

## 五、修复后效果：**待采样**（不给预测当结论）

### 5.1 为什么这里没有结论

「修掉域名兜底之后阿里云能不能到 95%+」**必须用修复切点之后的新采样回答**，本报告不具备该数据：本轮改动刚落地，库里最早的数据也在修复之前。**本报告不对修复效果给任何数字预测。**

### 5.2 修复改变了什么（可推断的部分）

修复前，挂在 `.maas.aliyuncs.com` 上的 deepseek 模型会被域名兜底判进 Qwen 家族，从而：
- 消息 content 由字符串被改写成带 `cache_control` 的文本块数组（前缀字节被改写，`messages_sha256` 从 `ce796db1…` 变为 `81fdbbb6…`，`request_bytes` 188 → 374）；
- 多出 `x-dashscope-session-cache: enable` 请求头；
- 被推到显式缓存路径，而工具轮里断点只剩 system 一个。

修复后该车道的请求形状回到「只保证前缀字节稳定」的最小集合（本次已新增 `deepseek 名 × maas 域` 的 provider kwargs 字节金样守住这一点）。

### 5.3 采样方法（可直接执行）

1. **确认改动生效**：重启后确认该车道请求不再带 `x-dashscope-session-cache`，且诊断行的 `cache_control_markers` 为 0（该列是 Task 2 新增的"线上字节事实"）。
2. **固定脚本采样**：在同一会话、同一车道上连续跑 **≥ 30 轮**固定脚本（推荐复用 `bench/adapt_bench.py`；**不要**用 `cache_benchmark.py` 的旧数字直接对比，见 §6 的 R7 口径警告）。
3. **出数字**：用 `core.cache_health.classify_lane_health`（Task 3 交付的纯函数）或 `MemoryManager.query_lane_cache_health` 读出该车道的 `hot_hit_rate` 与 `upstream_loss_rate`。
4. **三方对照**：同样脚本在基元律动 / 官方车道各跑一遍，保证同口径可比。

### 5.4 判定准则（先定好，避免事后解释）

| 修复后阿里云表现 | 结论 |
|---|---|
| `upstream_loss_rate` 降到个位数百分比、`hot_hit_rate` ≥ 95% | 客户端侧止血生效，达到基元律动水平 |
| `upstream_loss_rate` 仍 ≈10%（与修复前同量级） | 属**渠道责任**（网关侧缓存亲和/保留行为），转"网关缓存亲和专项"另立工作包 |
| 首轮出现全量 miss 后回升 | 这是 R1（缓存前缀因请求形状改变而重建一次）的**预期现象**，不是回归 |

**观察期要求**：3~7 天，且诊断曲线必须**标注修复切点**（否则会把 R1 的首轮 miss 误判成回归）。

---

## 六、局限与不可用证据

- **R1（切点首轮 miss）**：请求头少 1 个、messages 少 3 个 content 标记 → 该车道缓存前缀会重建一次。首轮全量 miss 属预期。
- **R2（诊断口径跳变）**：该车道的 `cache_lane_sha256` 从"qwen lane"变为空、`messages_sha256` 因 Task 2 的口径统一而变化。**历史曲线不可直接续接**，这正是 Task 2 新增 `cache_control_markers` / `request_bytes_final` 两列的原因。
- **R7（基准工具口径）**：`cache_benchmark.py` 使用 `cache_scope="benchmark:main"`，在修复前属于"带 Qwen 头/断点"的测量口径。**改后必须用同一工具复测才可比**；`bench/adapt_bench.py` 用的是自带独立副本，不受影响。
- **机制指纹是推断性对齐**：设计文档 §1.4 里"该车道 97.6% 的步带 `cache_lane_sha256`"这类数字来自**发送时间戳对齐**（`harness_steps.model` / `api_base` 两库 100% 为 NULL，且与诊断表按 sha 三元组 JOIN 匹配 0 行），不是表间外键。方向可信，但引用时不得当成硬外键证据。
- **未复算的历史数字**：设计文档 §1.2 / §1.3 里的 82.45% / 82.71% / 14.0% 等值，本轮**不再复现**（库已增长：阿里云车道从 307 轮长到 518 轮）。本报告全部数字为本轮真实查询结果，与文档数字的差异来自数据增长与渠道波动，不是矛盾。

---

## 七、附录：可复现的查询脚本

```python
import sqlite3, os, collections
p = os.path.expanduser('~/.ycode/ycode_gui.db').replace(chr(92), '/')
c = sqlite3.connect('file:' + p + '?mode=ro', uri=True)   # 只读

def analyze(model, api_base, scope='main'):
    rows = list(c.execute(
        "select id, session_id, request_reason, prompt_tokens, cached_tokens, "
        "cache_reported, usage_valid from cache_request_diagnostics "
        "where model=? and api_base=? and cache_scope=? order by session_id, id",
        (model, api_base, scope)))
    prev, buckets, reasons = {}, collections.Counter(), collections.Counter()
    hot_prompt = hot_cached = loss = samples = unreported = 0
    for _id, sid, reason, pt, ct, rep, val in rows:
        reason = reason or ''
        reasons[reason or '<append>'] += 1
        pp = prev.get(sid)
        ok = bool(rep) and bool(val)
        if not ok:
            unreported += 1
        if reason == '' and ok and pt is not None and ct is not None and pp is not None:
            samples += 1
            hot_prompt += pt
            hot_cached += ct
            ratio = (ct / pp) if pp else 0.0
            buckets['>=0.99' if ratio >= .99 else '0.90-0.99' if ratio >= .90
                    else '0.60-0.90' if ratio >= .60 else '<0.60' if ratio > 0 else '=0'] += 1
            if ct < min(pp, pt) * 0.6:
                loss += 1
        prev[sid] = pt if (ok and pt is not None) else None   # 未报告 → 下一行不可判定
    return {
        'rows': len(rows), 'unreported': unreported, 'samples': samples,
        'hot_prompt': hot_prompt, 'hot_cached': hot_cached,
        'hot_hit_rate': 100.0 * hot_cached / hot_prompt if hot_prompt else 0.0,
        'upstream_loss_rate': 100.0 * loss / samples if samples else 0.0,
        'buckets': dict(buckets), 'reasons': dict(reasons),
    }
```

口径三处关键：① `prev[sid]` 在上一行未报告时置 `None`（"无法判定就不猜"）；② loss 阈值用 `min(prev_prompt, 本轮 prompt) × 0.6`；③ 分母只收 `reason == '' and reported and valid and prev 可判定` 的轮次。

本轮真实输出（节选）：

```
=== aliyun | openai/deepseek-v4.1-flash | ...maas.aliyuncs.com... | main
  rows=518 reported_valid=518 unreported=0 cold=7 rewrite=2
  eligible=509 hot_prompt=79808203 hot_cached=70540416 hot_hit_rate=88.39%
  upstream_loss=53 rate=10.41%
  buckets: {'>=99%': 399, '90-99%': 50, '60-90%': 7, '<60%': 31, '=0': 22}
=== tokenrhythm | openai/deepseek-flash | https://tokenrhythm.studio/v1 | main
  rows=4794 reported_valid=4777 unreported=17 cold=119 rewrite=148
  eligible=4502 hot_prompt=786925803 hot_cached=777808640 hot_hit_rate=98.84%
  upstream_loss=16 rate=0.36%
=== official | openai/deepseek-flash | https://api.deepseek.com | main
  rows=577 reported_valid=577 unreported=0 cold=13 rewrite=2
  eligible=562 hot_prompt=149175241 hot_cached=148307456 hot_hit_rate=99.42%
  upstream_loss=1 rate=0.18%
```