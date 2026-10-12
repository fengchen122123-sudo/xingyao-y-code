# DeepSeek / Kimi 缓存命中可靠性实施计划

**目标：** 修复 DeepSeek/Kimi 请求可达性和缓存统计口径，以 provider token usage 交付可复验结果。

**约束：** 严格 TDD；不外发项目系统提示词、工具 schema 或用户历史；不使用 token 估算修补真实用量。

## Task 1：DeepSeek V4 参数透传

- [x] 新增失败测试：thinking 请求必须显式允许 LiteLLM 透传 `reasoning_effort`。
- [x] 新增失败测试：非 thinking/非 DeepSeek 路由不得意外获得该允许项。
- [x] 最小实现并运行聚焦测试。

## Task 2：按 endpoint/scope 保存 token 真值

- [x] 新增失败测试：schema 迁移包含 `api_base/cache_scope`，旧数据标记为 legacy。
- [x] 新增失败测试：`_account_server_usage` 传递 endpoint/scope，NULL cached 语义不变。
- [x] 新增失败测试：历史查询按 scope/endpoint 分组，主会话基线排除后台 scope。
- [x] 实现 schema、持久化和查询展示。

## Task 3：隔离测试数据库

- [x] 通过测试收集失败证明运行日志/DB 仍指向真实目录，并增加 `YCODE_DATA_DIR` 覆盖。
- [x] pytest 在测试收集前设置独立临时 DB。
- [x] 验证全量测试不再增加真实数据库行数。

## Task 4：实际运行数据与配置

- [x] 一致性备份 `~/.ycode/config.json` 和数据库，再迁移真实 token 表。
- [x] 核对实际运行配置：当前 DeepSeek 聚合路由不注入官方 thinking 私有契约；官方路由使用稳定默认 effort；API key 未修改。
- [x] 精确清理 45 条测试 token 行与 466 条测试诊断行；1,066 条真实历史保留为 legacy。

## Task 5：线上与回归验证

- [x] 用无敏感内容的等长固定前缀跑 DeepSeek 官方/聚合安全 A/B；两者热路径均超过 99%。
- [x] 用同一安全前缀跑 Kimi 官方/聚合观察；官方热路径 99.17%，聚合 K2.6 仅 49.83%。
- [x] 运行缓存/token 聚焦测试、全量 pytest、py_compile 与前端回归（762 后端、44 前端、build 成功）。
- [x] 完成两轮只读代码复审，Critical/Important 均清零；endpoint 归一化、敏感字段脱敏与查询分桶均有回归守卫。
- [x] 检查 git diff 和真实数据库行数，输出可复验交付结论。
