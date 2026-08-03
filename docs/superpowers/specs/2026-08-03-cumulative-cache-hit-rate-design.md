# 状态栏累计缓存命中率设计

## 目标

状态栏同时展示会话累计输入 Token、累计输出 Token、累计缓存命中 Token，以及由同一组累计数据计算出的缓存命中率，使用户可以直接复算显示结果。

## 展示格式

```text
输入 315.1K · 输出 3.4K · 命中 312.0K · 命中率 99%
```

## 数据口径

- `输入`：`session_token_usage.cumulative_prompt`
- `输出`：`session_token_usage.cumulative_completion`
- `命中`：`session_token_usage.cumulative_cached`
- `命中率`：`cumulative_cached / cumulative_prompt * 100`
- 累计输入为零时，命中率显示为 `0%`
- 命中率按现有状态栏风格四舍五入显示为整数百分比

## 实现边界

- 只调整状态栏的读取、计算和展示。
- 不修改底层 Token 采集、厂商适配、缓存亲和或持久化逻辑。
- 不再用最近一次请求命中率、热启动平均值或其他无法通过当前展示数字复算的口径。

## 验证

- 增加单元测试，验证累计输入、累计输出、累计命中及计算所得命中率的完整展示。
- 增加累计输入为零的测试，确保不会除零且显示 `0%`。

