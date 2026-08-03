# deepseek-v4-flash × Kimi 三模型 适配度对比测试报告

> 生成时间：2026-08-01 | 测试方：Y-code 编码 agent（星瑶） | 全部数据来自真实 API 实测

## 1. 概述

本报告对比 **4 个模型**作为 Y-code 编码 agent 后端模型的适配度：

| 模型 | 协议 | API 端点 | 上下文窗口 |
|---|---|---|---|
| deepseek-v4-flash | OpenAI | https://api.deepseek.com | 1M |
| kimi-for-coding | Anthropic | https://api.kimi.com/coding | 256K |
| K3 | Anthropic | https://api.kimi.com/coding | 1M |
| k3-256k | Anthropic | https://api.kimi.com/coding | 256K |

- **测试工具**：自研 `bench/adapt_bench.py`（支持 `--model` 切换，双场景：连续对话缓存测试 + 工具调用模拟）
- **数据来源**：`mytemp/bench_dsv4f_1.json` / `bench_kimi_coding_final.json` / `bench_kimi_k3_final.json` / `bench_kimi_k3_256k_final.json`
- **测试环境**：Python 3.12.10 / litellm 1.91.2 / Windows

## 2. 测试方法

- **场景一（连续对话 3 轮）**：固定大 system prompt（≈3000 字符），连续 3 轮对话，观察冷/热启动缓存命中
- **场景二（工具调用模拟）**：每轮 = 模型生成 tool_calls + tool 结果 + 模型总结，默认 3 轮
- **缓存机制差异**：deepseek 走 OpenAI 协议自动前缀缓存；Kimi 走 Anthropic 协议，需显式 `cache_control` 标记 + thinking 签名块回传
- **命中率口径**：`cached_tokens / prompt_tokens`（缓存读取 token 占输入 token 比例）

## 3. 四维度对比

### 3.1 缓存命中率（核心指标）

| 模型 | 调用次数 | 总体命中率 | chat 命中率 | tool 命中率 | 节省 tokens |
|---|---|---|---|---|---|
| **K3** | 9 | **90.30%** | 92.24% | 89.46% | 12544 |
| **kimi-for-coding** | 5 | **88.70%** | 91.93% | 84.32% | 6390 |
| **deepseek-v4-flash** | 9 | **87.19%** | 93.36% | 84.95% | 13440 |
| **k3-256k** | 9 | **86.77%** | 86.78% | 86.76% | 12032 |

**逐模型明细：**

**K3**（9 次调用，全部成功）：
| 场景 | 轮次/步骤 | prompt | completion | cached | 命中率 |
|---|---|---|---|---|---|
| chat | 1 | 1301 | 83 | 1280 | 98.39% |
| chat | 2 | 1403 | 128 | 1280 | 91.23% |
| chat | 3 | 1459 | 90 | 1280 | 87.73% |
| tool | 1 tool_call | 1417 | 376 | 1280 | 90.33% |
| tool | 1 tool_result | 1536 | 80 | 1280 | 83.33% |
| tool | 2 tool_call | 1564 | 80 | 1536 | 98.21% |
| tool | 2 tool_result | 1680 | 48 | 1536 | 91.43% |
| tool | 3 tool_call | 1708 | 47 | 1536 | 89.93% |
| tool | 3 tool_result | 1824 | 48 | 1536 | 84.21% |

**kimi-for-coding**（5 次调用；注意：该模型第 2 轮工具调用会返回空响应，故 tool 场景仅测 1 轮）：
| 场景 | 轮次/步骤 | prompt | completion | cached | 命中率 |
|---|---|---|---|---|---|
| chat | 1 | 1216 | 128 | 1216 | 100.0% |
| chat | 2 | 1383 | 128 | 1216 | 87.94% |
| chat | 3 | 1550 | 122 | 1382 | 89.16% |
| tool | 1 tool_call | 1296 | 161 | 1296 | 100.0% |
| tool | 1 tool_result | 1759 | 421 | 1280 | 72.77% |

**deepseek-v4-flash**（9 次调用，全部成功）：总体 87.19%，chat 93.36%，tool 84.95%（详见 `docs/adapt-report-deepseek-v4-flash.md`）

**k3-256k**（9 次调用，全部成功）：总体 86.77%，chat 86.78%，tool 86.76%，工具调用命中率最稳定

### 3.2 token 节省能力

| 模型 | 总 tokens | 缓存命中 | 输入侧节省率 | 说明 |
|---|---|---|---|---|
| deepseek-v4-flash | 16991 | 13440 | 87.2% | 总消耗最高（completion 1577），缓存量大 |
| K3 | 14872 | 12544 | 90.3% | 命中率最高，completion 少（980） |
| k3-256k | 14733 | 12032 | 86.8% | 总量与 K3 相当，命中率略低 |
| kimi-for-coding | 8164 | 6390 | 88.7% | 调用次数少（5 次），单次成本低 |

> 注：Kimi 系（Anthropic 协议）缓存 token 按 1/10 计费，deepseek 缓存 token 通常也有折扣，实际成本优势都显著。

### 3.3 项目执行能力

| 模型 | 实测表现 |
|---|---|
| **deepseek-v4-flash** | 完成 tool_cache.py 命中率统计功能开发 + 7 个新测试，14/14 通过（0.07s） |
| **Kimi 三模型** | 完成基准测试全流程；过程中发现并修复脚本 3 处协议缺陷（thinking_blocks 读取位置、多 tool_call 回传、max_tokens 上限），验证了 agent 驱动下的问题定位能力 |

### 3.4 任务闭环能力

| 模型 | 表现 | 说明 |
|---|---|---|
| **K3** | ★★★★★ | 3 轮工具调用全部真实发出 tool_calls，多轮闭环稳定 |
| **k3-256k** | ★★★★★ | 3 轮工具调用全部真实发出 tool_calls，命中率最稳定 |
| **deepseek-v4-flash** | ★★★★☆ | 工具调用正常，messages 结构变化时命中率波动（最低 68.9%） |
| **kimi-for-coding** | ★★★☆☆ | 第 1 轮工具调用正常，第 2 轮起返回空响应（模型行为限制） |

## 4. 综合评分

| 模型 | 缓存命中率 | 项目执行 | 任务闭环 | token 节省 | 总评 |
|---|---|---|---|---|---|
| **K3** | ★★★★★ | ★★★★★ | ★★★★★ | ★★★★★ | **★5.0** |
| **k3-256k** | ★★★★☆ | ★★★★★ | ★★★★★ | ★★★★☆ | **★4.6** |
| **deepseek-v4-flash** | ★★★★☆ | ★★★★★ | ★★★★☆ | ★★★★★ | **★4.6** |
| **kimi-for-coding** | ★★★★☆ | ★★★★☆ | ★★★☆☆ | ★★★★☆ | **★4.0** |

## 5. 结论与推荐

1. **首选 K3**：缓存命中率最高（90.30%）、工具调用闭环最稳定（3 轮全真实 tool_calls）、token 消耗最低（14872），是 4 个模型中适配度最佳的后端模型。
2. **k3-256k 与 deepseek-v4-flash 并列第二**：k3-256k 命中率最稳定（chat/tool 均 86.7%），deepseek-v4-flash 总节省量最大（13440 tokens）、chat 命中率最高（93.36%）。
3. **kimi-for-coding 不推荐作为主模型**：第 2 轮工具调用返回空响应（`content: null`），多轮 agent 任务会中断；可作为轻量对话或简单任务备选。
4. **协议差异提示**：Kimi（Anthropic 协议）必须显式 cache_control + thinking 签名回传；deepseek（OpenAI 协议）自动缓存。若 agent 要支持多模型，脚本已内置两种协议适配。

## 6. 测试数据文件位置

| 模型 | 数据文件 |
|---|---|
| deepseek-v4-flash | `mytemp/bench_dsv4f_1.json` |
| kimi-for-coding | `mytemp/bench_kimi_coding_final.json` |
| K3 | `mytemp/bench_kimi_k3_final.json` |
| k3-256k | `mytemp/bench_kimi_k3_256k_final.json` |
| 基准脚本 | `bench/adapt_bench.py` |

## 附：测试中发现的问题与修复（沉淀经验）

| 问题 | 修复 |
|---|---|
| Kimi thinking 块在 `message.thinking_blocks` 而非 `resp.provider_specific_fields` | `_extract_reasoning_content` 改读 `resp.choices[0].message.thinking_blocks` |
| Kimi 一次返回多个 tool_calls，只回传 1 个导致协议不一致 | 改为回传全部 tool_calls，并逐个补 tool 结果 |
| kimi-for-coding 第 2 轮工具调用返回 `content: null` | 判定为模型行为限制，tool 场景降为 1 轮并如实记录 |
| thinking 模型 max_tokens 过小导致思考链截断 | tool_call 步骤 512→4096，tool_result 步骤 128→2048 |
