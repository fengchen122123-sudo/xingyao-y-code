# DeepSeek / Kimi 缓存命中可靠性设计

## 目标

让 Y-CODE 对 DeepSeek 与 Kimi 的缓存命中结论同时满足三件事：请求真实可达、token 只采信 provider usage、主会话与后台请求不再混算。99% 是线上热路径验收阈值，不是通过本地估算或报表筛选制造的数字。

## 已确认根因

1. 实际运行配置位于 `~/.ycode/config.json`，仓库根目录配置只用于首次初始化；修改后者不会改变现有运行实例。
2. DeepSeek V4 thinking 请求会发送官方支持的 `reasoning_effort`，但当前 LiteLLM 对未知 OpenAI 兼容模型需要显式允许透传；未允许时请求在客户端直接失败。
3. `token_usage` 只保存 provider/model，不保存 `api_base/cache_scope`。同名模型经不同网关、主会话、子代理和保活请求会被聚合成一个命中率。
4. 测试导入 `main.py` 时会打开真实 `~/.ycode/ycode.db`；缓存诊断测试可能污染生产统计。
5. 当前运行进程早于最近代码修复启动，进程内前缀遥测与请求修复不会热加载。

## 方案比较

### 方案 A：只修 DeepSeek 参数透传

优点是改动最小，能让官方 V4 请求到达服务端。缺点是历史报表仍混算、测试仍可能污染真实数据库，用户继续会看到无法解释的低命中率。

### 方案 B：端到端可靠性修复（采用）

同时修复参数透传、作用域落库、查询展示、测试数据库隔离和实际运行配置。每项都以 provider 返回的整数 token 为真值。改动仍局限在请求参数、token 账本和测试基础设施，不重构代理调度。

### 方案 C：关闭 thinking 或只调整提示词

可以绕开 `reasoning_effort`，但会改变旗舰模型能力，并且不能解决统计混算。与用户目标冲突，不采用。

## 请求契约

- DeepSeek V4 thinking 保留官方 `thinking` 与 `reasoning_effort` 语义。
- 通过 LiteLLM 的 `allowed_openai_params` 明确允许 `reasoning_effort`，不使用全局 `drop_params`，避免悄悄丢参数。
- 仅显式配置或 DeepSeek V4 thinking 分支发送相关字段；非 DeepSeek 模型保持现状。
- Kimi 官方路由的缓存亲和身份继续由会话、模型、endpoint、scope 与工具契约的哈希组成，原始会话名不外发。第三方网关未声明支持时不发送 `prompt_cache_key`，也不启用保活。

## Token 账本与展示

- `token_usage` 新增 `api_base` 与 `cache_scope`，旧记录迁移为 `api_base=''`、`cache_scope='legacy'`，不伪造历史归属。
- `_account_server_usage` 把已经归一化并通过守恒校验的 prompt/completion/cached/total 连同 endpoint/scope 一次性落库。
- `cached_tokens=NULL` 继续表示 provider 未报告缓存，不能当作 0% miss。
- 历史查询按 model + endpoint + scope 分组；主链路统计只使用 `cache_scope='main'`，后台成本可独立查看，不再污染主链路命中率。
- 进程累计成本仍包含所有实际请求；界面明确标成“全请求”，主链路的本轮/热路径/会话全量继续单独展示。

## 测试隔离

- `config.py` 支持仅由环境变量指定 DB 文件，默认生产路径不变。
- pytest 在导入 `main.py` 前把 TUI/GUI 数据库指向独立临时目录，测试结束清理。
- 不依赖检测 `pytest` 模块或静默跳过持久化；生产和测试走同一落库代码，只替换物理文件。

## 验证

1. RED：DeepSeek V4 请求选项必须包含允许透传列表；token 落库必须带 endpoint/scope；测试 DB 不得指向 `~/.ycode`。
2. GREEN：聚焦测试与语法检查通过。
3. 本地前缀稳定性：system、tools 和历史前缀逐字节哈希稳定。
4. 线上安全基准：只发送固定合成文本，不发送项目数据或工具 schema；分别验证 DeepSeek 官方/聚合与 Kimi 官方/聚合的 provider usage。
5. 重启运行实例后，以新诊断表中的 `main` 记录验证真实生产会话；不能用旧进程数据宣称已达 99%。

## 失败策略

任何 usage 缺失、token 非整数、cached 大于 prompt、prompt + completion 与 total 不守恒都立即拒绝记账并暴露错误。不存在估算补写、0 值兜底或命中率后处理。
