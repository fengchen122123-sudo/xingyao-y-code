# Prompt Cache A/B 基准设计

## 目标

让缓存基准能真实回答三个不同问题：整体成本、稳定热轮能力、是否存在偶发整轮失效；并允许官方 DeepSeek 与聚合 endpoint 使用同一生产前缀和工具契约做可比 A/B。

## 现状问题

- `weighted_hot_rate` 直接排除首轮，只保留一个平均数。一次 0% 与多次 99% 可被平均值掩盖。
- 6000 行人造字符串把稳定前缀比例刻意放大，测到的是合成数据上限，不是 Y-CODE 的真实请求形状。
- 基准无条件发送 `tool_choice=none`，已不符合 DeepSeek V4 thinking 的生产契约。
- CLI 一次只能测一个 endpoint，官方与聚合结果容易使用不同轮数、工具 schema 或前缀，不能严格比较。

## 指标

每次报告以下互不替代的指标：

- `all_rate`：包含冷启动的全部 prompt/cached token 加权命中率，反映实际总成本。
- `hot_rate`：排除第一轮后的加权命中率，反映缓存稳定后上限。
- `minimum_hot_round_rate`：最差单个热轮命中率。
- `catastrophic_miss_count`：热轮中命中率低于 50% 的次数。
- `passed`：`hot_rate >= minimum_hot_rate` 且 `catastrophic_miss_count == 0`。

所有 token 只取 provider usage；usage 缺失或 cached 大于 prompt 时立即失败，不用本地估算。

## 真实请求形状

- 稳定 system 消息直接调用 `core.agent_service.build_stable_system_message()`。
- tools 使用生产 `get_tools_schema()`，每轮只 append user/assistant，不重建旧消息。
- provider options 使用 `_provider_request_options()`；DeepSeek thinking 不发送 `tool_choice`，其他路由保持生产的 `auto`。
- 基准提示明确禁止工具调用；若模型仍发起工具调用立即失败，绝不执行副作用。

## A/B

新增 `run_ab_benchmark(targets, ...)`：一次构建 tools，并对每个 target 使用相同 rounds、阈值与 suite session seed。每个 endpoint 仍通过 endpoint/model/tool contract 生成隔离后的 provider cache identity，不交叉污染。

CLI 保留单 endpoint 参数，并支持一组可选的 compare 参数：

- `--compare-model`
- `--compare-api-base`
- `--compare-api-key-env`

提供 compare 时输出两组逐轮数据和明确 winner；模型不相同只允许做“路由观察”，不宣称同模型 A/B。

## 验收边界

代码/离线测试通过只能证明统计和请求契约正确。达到 99% 必须以 provider 返回的真实 cached token 为证；没有相同模型的聚合 endpoint/凭据时，不生成虚假对比结论。
