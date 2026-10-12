# DeepSeek Immutable Ledger Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 DeepSeek 官方主会话增加独立不可变请求账本，同时保持 Kimi 和其他 Provider 的现有请求行为不变。

**Architecture:** 普通会话历史继续作为产品事实来源；新增 SQLite append-only generation 作为 DeepSeek 缓存请求视图。LLM 请求层只在官方 DeepSeek 主会话选择该视图；system/tools 稳定时复用 generation，真实 request-header 变化时切代。

**Tech Stack:** Python、SQLite、pytest、LiteLLM 兼容请求层

## Global Constraints

- Kimi、Anthropic、聚合 endpoint、子代理和辅助调用不得进入 DeepSeek 账本路径。
- 旧 generation 消息只读，只能追加新 generation 或新尾部消息。
- 不使用降级、猜测对齐或后处理修补掩盖不一致。
- 所有 SQLite 写操作必须显式 `commit()`，异常时 `rollback()`。

---

### Task 1: DeepSeek 账本持久化

**Files:**
- Modify: `core/memory.py`
- Test: `tests/test_deepseek_prompt_ledger.py`

**Interfaces:**
- Produces: generation 创建/读取、消息追加、工具快照读取接口。

- [x] 编写失败测试，验证消息只能追加、旧 generation 不被修改、工具快照被冻结。
- [x] 运行测试并确认因接口缺失而失败。
- [x] 实现最小 SQLite schema 与事务接口。
- [x] 运行测试并确认通过。

### Task 2: Provider 路由与请求视图

**Files:**
- Modify: `core/llm.py`
- Test: `tests/test_deepseek_prompt_ledger.py`

**Interfaces:**
- Consumes: Task 1 的账本接口。
- Produces: 官方 DeepSeek 主会话选择独立视图；其他请求保持原路径。

- [x] 编写失败测试，验证仅官方 DeepSeek 主会话启用账本。
- [x] 编写失败测试，验证连续请求的消息前缀和 tools 快照稳定。
- [x] 实现最小路由、seed、尾部追加和切代逻辑。
- [x] 运行相关测试并确认通过。

### Task 3: 工具循环同步与切代

**Files:**
- Modify: `core/llm.py`
- Modify: `core/memory.py`
- Test: `tests/test_deepseek_prompt_ledger.py`

**Interfaces:**
- Consumes: 当前 `pending_persistence` 顺序批次。
- Produces: 普通历史与 DeepSeek 账本的同序追加。

- [x] 编写工具调用轮失败测试。
- [x] 实现请求循环中的同序账本追加。
- [x] 对历史不一致、超窗压缩、system 和工具契约变化创建新 generation。
- [x] 运行相关测试并确认通过。

### Task 4: 全量回归与缓存验证

**Files:**
- Modify: `tests/test_prompt_byte_stability.py`

**Interfaces:**
- Consumes: 最终请求 payload。
- Produces: DeepSeek 字节前缀与 Kimi 行为隔离回归守卫。

- [x] 增加两轮真实 payload 前缀测试。
- [x] 运行 DeepSeek、Kimi、agent service、memory 和 llm 相关测试。
- [ ] 运行项目规定的 `py_compile`。
- [ ] 检查 diff、未跟踪文件和测试输出。
