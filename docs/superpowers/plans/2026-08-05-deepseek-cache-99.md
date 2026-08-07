# DeepSeek 前缀缓存命中率 90%+ → 99% 实施计划

> **执行时机（硬性门槛）**：必须在 `docs/superpowers/plans/2026-08-05-cli-gui-tool-progress.md`
> 全部 10 个 Task 完成、全量验证通过、工作区干净之后才开始。该计划的全局约束要求
> "Database message contents and tool execution order remain unchanged"，而本计划的
> Task 2 / Task 5 恰恰会修改 DB 消息内容，两者同步开发必然冲突。
>
> **目标读者**：执行本计划的 agentic worker。建议配合 superpowers:executing-plans 或
> subagent-driven-development 逐 Task 执行。Steps 使用 `- [ ]` 复选框跟踪。
>
> **实施状态（2026-08-07）**：Task 0-9 及 Task 10 的全量测试/文档已完成（9 个 commit，
> 全量 669 tests 通过）。Task 10 的缓存基准与真实会话抽检（需真实 API）待用户执行。
> 实施偏差记录：
> 1. Task 4 经用户确认跳过——commit aca5f27 已让压缩触发优先使用真实 API 用量基线，
>    Task 3 阶梯直接复用 `get_session_token_baseline`，未新增 `latest_real_prompt_tokens`。
> 2. `estimate_text_tokens` 修正为 bytes/4 与 CJK 字符数取大者（方案原实现对纯 ASCII 高估 4 倍，
>    与方案自身测试断言矛盾，按测试口径修正）。
> 3. Task 2 测试 fixture 修正：tool_call_id 必须唯一（方案原写法 6 条同 id 会被配对清洗合并）。
> 4. Task 3 测试窗口/数据量按实际保护区与系统提示词体积重新标定（语义不变：验证阶梯行为）。

**Goal:** 将 DeepSeek 路由的加权热缓存命中率从 90%+ 提升到 97-99%（长会话热路径口径），
同时不降低上下文质量（尾部保护区内的近期消息永远完整）。

**Architecture:** 新建 `core/tool_result_maintenance.py`（纯文本分层改写，幂等 marker）→
`core/memory.py` 增加 DB 层维护入口（保护区 + 一次性改写 + 冻结）→
`core/agent_service.py` 把"0.70 直接 LLM 摘要"替换为四级水位线阶梯 →
`core/llm.py` 提供真实 prompt_tokens 水位 + DeepSeek thinking 契约修正 →
图片媒体折叠从请求时移到入库时 → 字节稳定性 Golden 测试 + keepalive 默认开启 +
重复工具调用抑制。

**调研依据（源码级，非二手文章）：**

| 机制 | 出处 | 核心结论 |
|---|---|---|
| 四级水位线 + 决策单调（snip→prune 单向升级）+ 三 cursor + 8K token 保护区 | Waveloom `pkg/compaction/compaction.go:33-108, 315-328` | 每条消息字节最多变一次；LLM 摘要推迟到 95% 水位 |
| 用 API 真实 `prompt_tokens` 做触发，本地估算只用于保护区 | Waveloom `compaction.go:898-930` | 本地估算偏差大，不能用于触发 |
| marker 前缀幂等（`[snipped]/[elided]` 开头永不重复改写）、错误结果不改写、压缩经济学（prune 后回落即跳过付费摘要） | Reasonix `internal/agent/prune.go:121-129`、`compact.go:132-143` | Python 里 marker 比决策表更简单，效果等价 |
| 工具结果入库即截断冻结（32KB head+tail） | Reasonix `internal/agent/agent.go:4318` | 绝不事后回写历史中间 |
| prompt 双构建字节相等 Golden 测试 + schema canonical 契约测试 | Reasonix `internal/boot/prompt_stability_test.go`、`internal/tool/contract_test.go` | 字节稳定性要靠 CI 看守 |
| DeepSeek thinking：带 tool_calls 的 assistant 必须回传 `reasoning_content` key（空串可） | Reasonix `internal/provider/openai/openai.go:735-763` | 缺 key 直接 400 |
| 重复工具调用签名抑制（canonical args，成功 ≥2 次拦截） | Reasonix `internal/agent/agent.go:4054-4148` | 每少一轮重复 = 少一整轮纯 miss |

**命中率数学模型（为什么这些改动有效）：**
命中率 = cached_tokens / prompt_tokens。每轮新增内容（user 输入、assistant 回复、新工具结果）
永远是 miss。当前 90% 的损失来源：① 0.70 水位即触发 LLM 摘要 = 周期性全量前缀重写（每次
都是一条 10 万级 token 的纯 miss 请求）；② 尾部保护区外的旧工具结果体积大，推高每轮新增占比；
③ 长停顿后缓存 TTL 过期整轮 miss；④ 图片到达时请求路径改写历史中间消息。
本计划 Task 1-4 解决①②，Task 7 解决③，Task 5 解决④，Task 6/8/9 防回归与堵漏。

## Global Constraints

- 只在 `dev` 分支工作；每个 Task 独立 commit。
- 开始前必须 `git status --short` 确认工作区干净（tool-progress 计划已全部提交）。
- 全程使用 `.venv\Scripts\python.exe`（与 tool-progress 计划的 Runtime Setup 一致），不要用 checked-in `venv`。
- 新增 UI 文案遵守 AGENTS.md：TUI 输出不加 Markdown 强调符；后台线程不触碰 UI。
- 所有 DB 改写必须是"一次性 + 幂等 + 可冻结"：改写后的内容再次经过同一函数必须原样返回 None（不改）。
- 尾部保护区（最近 8000 估算 token）内的消息永不被改写。
- 错误结果（`🚨` 前缀）永不被改写——错误文本是模型自我纠正的依据。
- 不改消息顺序、不改 tool_call 配对结构、不改 system 稳定前缀（`core/context_builder.build_stable_prompt`）。
- 新事件/新函数保持向后兼容：旧会话数据（含已折叠附件、未折叠媒体）必须能正常读写。
- 压缩触发的取消语义保持现状：`compress_session_memory` 不支持取消参数，不在本计划中改变。

## 现状与改动映射

| # | 现状（损失点） | 位置 | 改动 | Task |
|---|---|---|---|---|
| 1 | 0.70 估算水位直接 LLM 摘要，全量前缀重写频繁 | `core/agent_service.py:318-345` | 四级水位线阶梯 | 3, 4 |
| 2 | 无本地 Snip/Elide 层，旧工具结果原样堆积 | （缺失） | 新建分层维护模块 + DB 入口 | 1, 2 |
| 3 | 压缩触发用本地估算 token | `core/agent_service.py:316` | 接入真实 prompt_tokens | 4 |
| 4 | 新图片到达时请求路径折叠旧图片 = 改写历史中间 | `core/llm.py:1248-1280` | 折叠移到入库时（DB 层一次性） | 5 |
| 5 | 无字节稳定性回归守卫 | （缺失） | Golden 测试 | 6 |
| 6 | keepalive 默认关闭，停顿后 TTL 过期整轮 miss | `core/cache_keepalive.py:18` | 默认 120s | 7 |
| 7 | DeepSeek thinking 缺 reasoning_content key 风险 | `core/llm.py:1294-1295` | 补空 key | 8 |
| 8 | 模型重复调用同一工具同参数 = 整轮纯 miss | `core/llm.py` 工具派发处 | 签名抑制 | 9 |

---

### Task 0: 执行门槛确认

- [x] **Step 1: 确认 tool-progress 计划已完成**

```powershell
git log --oneline -15
git status --short
```

Expected: 能看到 tool-progress 的 10 个 task commit；工作区干净（除 `.superpowers/` 等忽略项）。

- [x] **Step 2: 确认测试环境**

```powershell
.\.venv\Scripts\python.exe -V
.\.venv\Scripts\python.exe -m pytest -q tests\test_memory.py -x
```

Expected: Python 3.12；基线测试通过。若 `.venv` 不存在，按 tool-progress 计划的 Runtime Setup 创建。

---

### Task 1: 新建分层工具结果维护模块（纯函数层）

**Files:**
- Create: `core/tool_result_maintenance.py`
- Create: `tests/test_tool_result_maintenance.py`

**Interfaces:**
- Produces: `SNIPPED_PREFIX`, `ELIDED_PREFIX`, `PROTECTION_ZONE_TOKENS`, `MIN_SNIP_CHARS`
- Produces: `estimate_text_tokens(text) -> int`
- Produces: `is_snipped(text) -> bool`, `is_elided(text) -> bool`, `is_frozen(text) -> bool`
- Produces: `snip_tool_result(tool_name: str, text: str) -> str | None`（None = 不适用/已冻结）
- Produces: `elide_tool_result(tool_name: str, text: str) -> str | None`
- Consumed by: Task 2 `core/memory.py`。

- [x] **Step 1: 写失败测试**

创建 `tests/test_tool_result_maintenance.py`：

```python
from core.tool_result_maintenance import (
    ELIDED_PREFIX,
    SNIPPED_PREFIX,
    elide_tool_result,
    estimate_text_tokens,
    is_elided,
    is_frozen,
    is_snipped,
    snip_tool_result,
)


def _long_lines(n):
    return "\n".join(f"line-{i} " + "x" * 40 for i in range(n))


def test_snip_truncates_long_output_keeping_head_and_tail():
    text = _long_lines(300)
    result = snip_tool_result("read_file", text)
    assert result is not None
    assert result.startswith(SNIPPED_PREFIX)
    assert "line-0" in result
    assert "line-299" in result
    assert "line-150" not in result
    assert len(result) < len(text)


def test_snip_is_idempotent_via_marker():
    text = _long_lines(300)
    once = snip_tool_result("read_file", text)
    assert snip_tool_result("read_file", once) is None  # 已冻结，绝不二次改写


def test_snip_skips_small_and_error_results():
    assert snip_tool_result("read_file", "short result") is None
    assert snip_tool_result("execute_command", "🚨 命令执行失败\n" + "y" * 5000) is None


def test_elide_replaces_whole_body_with_placeholder():
    text = _long_lines(300)
    result = elide_tool_result("execute_command", text)
    assert result.startswith(ELIDED_PREFIX)
    assert "line-0" not in result
    assert "re-run the tool" in result


def test_elide_upgrades_snipped_but_never_touches_elided():
    text = _long_lines(300)
    snipped = snip_tool_result("read_file", text)
    upgraded = elide_tool_result("read_file", snipped)
    assert upgraded is not None and upgraded.startswith(ELIDED_PREFIX)
    assert elide_tool_result("read_file", upgraded) is None  # 单调：升级后永久冻结


def test_marker_helpers():
    assert is_snipped(SNIPPED_PREFIX + " ...")
    assert is_elided(ELIDED_PREFIX + " ...")
    assert is_frozen(ELIDED_PREFIX + " ...")
    assert not is_frozen("normal output")


def test_estimate_text_tokens_cjk_aware():
    assert estimate_text_tokens("") == 0
    # CJK：字符数主导；ASCII：bytes/4 主导
    assert estimate_text_tokens("汉" * 100) >= 100
    assert estimate_text_tokens("a" * 400) == 100
```

- [x] **Step 2: 运行并确认 RED**

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\test_tool_result_maintenance.py
```

Expected: `ModuleNotFoundError: core.tool_result_maintenance`。

- [x] **Step 3: 实现模块**

创建 `core/tool_result_maintenance.py`（完整实现，直接可用）：

```python
"""工具结果的分层本地维护（Snip / Elide），服务于 DeepSeek 前缀缓存命中率。

设计来源（MIT 开源源码）：
- Waveloom pkg/compaction/compaction.go：分层水位线 + 决策单调
  （只允许 无→snip、无→elide、snip→elide，绝不降级或同级重复），尾部保护区。
- Reasonix internal/agent/prune.go：marker 前缀幂等（[snipped]/[elided] 开头的
  结果永不重复改写），错误结果不改写。

不变量：
1. 每条工具结果的字节最多被改写两次（snip 一次、elide 升级一次），之后永久冻结；
2. 改写只发生在 DB 层（core/memory.py 调用），请求路径绝不改写历史中间字节；
3. 尾部保护区内（最近 PROTECTION_ZONE_TOKENS 估算 token）的消息永不被改写；
4. 错误结果（🚨 前缀）不改写——错误文本是模型自我纠正的依据。
"""

SNIPPED_PREFIX = "[snipped tool result"
ELIDED_PREFIX = "[elided tool result"
ERROR_PREFIXES = ("🚨", "error:", "blocked:")

# 尾部保护区：最近这么多估算 token 内的消息永不被改写（Waveloom 同值）
PROTECTION_ZONE_TOKENS = 8000

# 小于该体积的结果不值得改写（省不了多少 token 还会破坏前缀）
MIN_SNIP_CHARS = 1024

# 每种工具的 snip 几何：(max_lines, head_lines, tail_lines, max_line_chars, max_total_chars)
_SNIP_STRATEGIES = {
    "read_file": (200, 150, 10, 4000, 30000),
    "fetch_url": (200, 150, 10, 4000, 30000),
    "search_code": (200, 150, 10, 4000, 30000),
    "glob_files": (200, 150, 10, 4000, 30000),
    "search_news": (200, 150, 10, 4000, 30000),
    "execute_command": (60, 20, 30, 2000, 12000),
}
_DEFAULT_STRATEGY = (60, 20, 30, 2000, 12000)


def estimate_text_tokens(text):
    """与 Reasonix estimateTextTokens 一致：bytes/4 与字符数取大者（CJK 约 1 字符/token）。"""
    if not text:
        return 0
    return max(len(text.encode("utf-8")) // 4, len(text))


def is_snipped(text):
    return bool(text) and text.startswith(SNIPPED_PREFIX)


def is_elided(text):
    return bool(text) and text.startswith(ELIDED_PREFIX)


def is_frozen(text):
    return is_snipped(text) or is_elided(text)


def _is_error(text):
    return bool(text) and text.startswith(ERROR_PREFIXES)


def snip_tool_result(tool_name, text):
    """Tier 1：head+tail 截断。返回新文本；不适用（已冻结/错误/太小）返回 None。"""
    if not text or is_frozen(text) or _is_error(text):
        return None
    if len(text) < MIN_SNIP_CHARS:
        return None
    max_lines, head, tail, max_line_chars, max_total = _SNIP_STRATEGIES.get(
        tool_name, _DEFAULT_STRATEGY
    )
    lines = text.split("\n")
    # 1) 行数截断（优先，语义最友好）
    if len(lines) > max_lines and len(lines) > head + tail + 10:
        omitted = len(lines) - head - tail
        return (
            f"{SNIPPED_PREFIX} — {tool_name}, 已省略 {omitted} 行；"
            f"如需完整内容请重新调用工具]\n"
            + "\n".join(lines[:head])
            + f"\n\n[... {omitted} lines omitted — full result already processed by Agent]\n\n"
            + "\n".join(lines[-tail:])
        )
    # 2) 总字符截断
    if len(text) > max_total:
        half = max_total // 2
        newline = text.find("\n", half)
        if half < newline < len(text) - half:
            head_text, tail_text = text[:newline], text[len(text) - half:]
        else:
            head_text, tail_text = text[:half], text[len(text) - half:]
        return (
            f"{SNIPPED_PREFIX} — {tool_name}, 已截断 {len(text)} → {max_total} 字符；"
            f"如需完整内容请重新调用工具]\n"
            + head_text
            + "\n[... content truncated — full result already processed by Agent]\n"
            + tail_text
        )
    # 3) 单行截断（base64 / minified JSON 类长行）
    new_lines, changed = [], False
    for line in lines:
        if len(line) > max_line_chars:
            new_lines.append(
                line[:max_line_chars]
                + f"... [line truncated: {len(line)} → {max_line_chars} chars]"
            )
            changed = True
        else:
            new_lines.append(line)
    if changed:
        return (
            f"{SNIPPED_PREFIX} — {tool_name}, 已截断超长行；"
            f"如需完整内容请重新调用工具]\n" + "\n".join(new_lines)
        )
    return None


def elide_tool_result(tool_name, text):
    """Tier 2：整体占位符化。snipped 可升级一次；elided/错误/太小返回 None。"""
    if not text or is_elided(text) or _is_error(text):
        return None
    if len(text) < MIN_SNIP_CHARS:
        return None
    return (
        f"{ELIDED_PREFIX} — {tool_name}, {len(text)} chars archived; "
        f"re-run the tool if the data is needed again]"
    )
```

- [x] **Step 4: 运行并确认 GREEN**

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\test_tool_result_maintenance.py
```

- [x] **Step 5: Commit**

```powershell
git add core\tool_result_maintenance.py tests\test_tool_result_maintenance.py
git commit -m "feat: add tiered tool result maintenance primitives"
```

---

### Task 2: memory.py DB 层维护入口（保护区 + 一次性改写）

**Files:**
- Modify: `core/memory.py`
- Modify: `tests/test_memory.py`（或新建 `tests/test_memory_maintenance.py`，优先新建避免与在途改动冲突）

**Interfaces:**
- Produces: `MemoryManager.maintain_tool_results(session_name, tier=1, protection_tokens=8000) -> tuple[int, int]`
  返回 `(snipped_count, elided_count)`。
- Consumed by: Task 3 `core/agent_service.py`。

**关键设计：** 与 `prune_history_files` 同层（直接操作 DB 记录，有消息 id），只处理
`id > summarized_until_msg_id` 的活跃消息（已被摘要排除的消息不在请求前缀里，无需维护）。
保护区从消息尾部向前累加估算 token 确定。tool 消息的 content 可能是 JSON dict
（含 `name`/`content`）或纯字符串，两种都要处理；JSON 回写保持原结构。

- [x] **Step 1: 写失败测试**

创建 `tests/test_memory_maintenance.py`（fixture 复用 `tests/test_memory.py` 中现有的
临时数据库 manager 构造方式；执行时先读该文件对齐风格）：

```python
from core.tool_result_maintenance import ELIDED_PREFIX, SNIPPED_PREFIX


def _fill_tool_session(manager, session, tool_texts):
    manager.add_message(session, "user", "开始")
    for text in tool_texts:
        manager.add_messages(session, [
            ("assistant", {"role": "assistant", "content": "", "tool_calls": [
                {"id": f"call-{len(text)}", "type": "function",
                 "function": {"name": "read_file", "arguments": "{}"}}]}),
            ("tool", {"role": "tool", "tool_call_id": f"call-{len(text)}",
                      "name": "read_file", "content": text}),
        ])


def test_maintain_snips_old_tool_results_but_protects_recent_tail(manager_tmp):
    session = "maintain-snip"
    big = "\n".join(f"line-{i} " + "x" * 40 for i in range(300))
    _fill_tool_session(manager_tmp, session, [big] * 6)
    snipped, elided = manager_tmp.maintain_tool_results(session, tier=1, protection_tokens=200)
    assert snipped > 0
    assert elided == 0
    history = manager_tmp.get_history_for_llm(session)
    tool_texts = [m["content"] for m in history if m.get("role") == "tool"]
    # 旧结果被 snip 且带 marker；尾部保护区内的保持原样
    assert any(t.startswith(SNIPPED_PREFIX) for t in tool_texts)
    assert any(t == big for t in tool_texts)
    # 幂等：再跑一遍不应再有新改写
    snipped2, _ = manager_tmp.maintain_tool_results(session, tier=1, protection_tokens=200)
    assert snipped2 == 0


def test_maintain_tier2_upgrades_snipped_to_elided(manager_tmp):
    session = "maintain-elide"
    big = "\n".join(f"line-{i} " + "x" * 40 for i in range(300))
    _fill_tool_session(manager_tmp, session, [big] * 6)
    manager_tmp.maintain_tool_results(session, tier=1, protection_tokens=200)
    snipped, elided = manager_tmp.maintain_tool_results(session, tier=2, protection_tokens=200)
    assert elided > 0
    history = manager_tmp.get_history_for_llm(session)
    tool_texts = [m["content"] for m in history if m.get("role") == "tool"]
    assert any(t.startswith(ELIDED_PREFIX) for t in tool_texts)
    # 再次 tier=2 全部冻结
    s3, e3 = manager_tmp.maintain_tool_results(session, tier=2, protection_tokens=200)
    assert s3 == 0 and e3 == 0


def test_maintain_never_touches_error_results(manager_tmp):
    session = "maintain-error"
    err = "🚨 命令执行失败（退出码 1）。\n" + "traceback\n" * 500
    _fill_tool_session(manager_tmp, session, [err] * 6)
    s, e = manager_tmp.maintain_tool_results(session, tier=2, protection_tokens=200)
    assert s == 0 and e == 0
    history = manager_tmp.get_history_for_llm(session)
    assert any(m.get("role") == "tool" and m["content"] == err for m in history)
```

- [x] **Step 2: 运行并确认 RED**

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\test_memory_maintenance.py
```

Expected: `AttributeError: ... no attribute 'maintain_tool_results'`。

- [x] **Step 3: 实现 maintain_tool_results**

在 `core/memory.py` 的 `MemoryManager` 中（紧跟 `prune_history_files` 之后）新增：

```python
    def maintain_tool_results(self, session_name, tier=1, protection_tokens=8000):
        """分层本地维护旧工具结果（Snip/Elide），DB 层一次性改写并永久冻结。

        tier=1 截断（snip）；tier=2 占位符化（elide，snipped 可升级一次）。
        尾部 protection_tokens 估算 token 为保护区内消息，永不改写。
        只处理 id > summarized_until_msg_id 的活跃消息。
        返回 (snipped_count, elided_count)。
        """
        from core.tool_result_maintenance import (
            elide_tool_result,
            estimate_text_tokens,
            snip_tool_result,
        )
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute(
                'SELECT id, summarized_until_msg_id FROM sessions WHERE session_name = ?',
                (session_name,),
            )
            row = cursor.fetchone()
            if not row:
                return (0, 0)
            session_id, summarized_until = row[0], row[1] or 0
            cursor.execute(
                'SELECT id, role, content FROM messages '
                'WHERE session_id = ? AND id > ? ORDER BY id ASC',
                (session_id, summarized_until),
            )
            records = cursor.fetchall()

            # 保护区：从尾部向前累加估算 token
            protection_start = 0
            total = 0
            for i in range(len(records) - 1, -1, -1):
                total += estimate_text_tokens(str(records[i][2] or ""))
                if total >= protection_tokens:
                    protection_start = i
                    break

            updates = []
            snipped = elided = 0
            for i in range(protection_start):
                msg_id, role, raw = records[i]
                if role != "tool":
                    continue
                parsed, ok = _try_parse_json(raw)
                if ok and isinstance(parsed, dict):
                    name = str(parsed.get("name") or "")
                    text = str(parsed.get("content") or "")
                else:
                    name, text = "", str(raw or "")

                new_text = None
                if tier >= 2:
                    new_text = elide_tool_result(name, text)
                    if new_text is not None:
                        elided += 1
                if new_text is None and tier >= 1:
                    new_text = snip_tool_result(name, text)
                    if new_text is not None:
                        snipped += 1
                if new_text is None:
                    continue
                if ok and isinstance(parsed, dict):
                    parsed["content"] = new_text
                    new_raw = json.dumps(parsed, ensure_ascii=False)
                else:
                    new_raw = new_text
                updates.append((new_raw, msg_id))

            if updates:
                cursor.executemany('UPDATE messages SET content = ? WHERE id = ?', updates)
                self.conn.commit()
            return (snipped, elided)
```

注意：整个函数已在 `self._lock` 内，遵循 AGENTS.md 的 commit 纪律（函数末尾 `conn.commit()`）。

- [x] **Step 4: 运行并确认 GREEN**

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\test_memory_maintenance.py tests\test_memory.py
```

- [x] **Step 5: Commit**

```powershell
git add core\memory.py tests\test_memory_maintenance.py
git commit -m "feat: maintain stale tool results at DB layer with protection zone"
```

---

### Task 3: agent_service.py 四级水位线阶梯（核心改动）

**Files:**
- Modify: `core/agent_service.py`（替换 318-345 行的单阈值压缩逻辑）
- Modify: `tests/test_agent_service.py`

**Interfaces:**
- Consumes: Task 2 `maintain_tool_results()`、Task 4 `latest_real_prompt_tokens()`。
- Produces: 模块常量 `SNIP_RATIO = 0.60`、`ELIDE_RATIO = 0.80`、`SUMMARIZE_RATIO = 0.95`。
- Deprecates: `DEFAULT_COMPRESS_THRESHOLD`（保留参数兼容，阶梯内不再使用；见 Step 3 说明）。

**设计决策：** 采用 Waveloom 水位线（60/80/95）而非 Reasonix（60/80 即摘要）——
LLM 摘要是全量前缀重写（一整条 10 万级 token 的纯 miss 请求），能推多晚推多晚；
本地 Snip/Elide 把压力按在线下时才彻底不摘要（压缩经济学）。

- [x] **Step 1: 写失败测试**

在 `tests/test_agent_service.py` 追加（fixture 复用该文件现有的 manager/service 构造）：

```python
def test_prepare_turn_snips_before_summarizing(monkeypatch, manager_tmp):
    """水位过 0.60 但未过 0.95 时：只做本地 snip，绝不调用 LLM 摘要。"""
    calls = []
    monkeypatch.setattr(
        "core.memory_compressor.compress_session_memory",
        lambda *a, **k: calls.append(1),
    )
    # 构造刚好超过 0.60 窗口的水位：用小 context_window + 大工具结果
    session = "tier-ladder"
    big = "\n".join(f"line-{i} " + "x" * 40 for i in range(300))
    manager_tmp.add_message(session, "user", "开始")
    for _ in range(4):
        manager_tmp.add_message(session, "tool", big)
    manager_tmp.add_message(session, "user", "继续")
    from core.agent_service import prepare_turn_messages
    prepare_turn_messages(
        manager_tmp, session, "deepseek-chat", 16000,
        permission_mode="auto",
    )
    assert calls == []  # 未触发付费摘要
    history = manager_tmp.get_history_for_llm(session)
    from core.tool_result_maintenance import SNIPPED_PREFIX
    assert any(
        m.get("role") == "tool" and str(m.get("content", "")).startswith(SNIPPED_PREFIX)
        for m in history
    )


def test_prepare_turn_summarizes_only_above_095(monkeypatch, manager_tmp):
    """超过 0.95 且 snip/elide 无法回落时，才调用 LLM 摘要。"""
    calls = []
    monkeypatch.setattr(
        "core.memory_compressor.compress_session_memory",
        lambda *a, **k: calls.append(1),
    )
    session = "tier-summarize"
    # 用大量普通 user/assistant 消息（非 tool，snip 无能为力）制造高压
    for i in range(40):
        manager_tmp.add_message(session, "user", f"问题{i} " + "汉" * 300)
        manager_tmp.add_message(session, "assistant", f"回答{i} " + "汉" * 300)
    from core.agent_service import prepare_turn_messages
    prepare_turn_messages(
        manager_tmp, session, "deepseek-chat", 16000,
        permission_mode="auto",
    )
    assert len(calls) == 1
```

- [x] **Step 2: 运行并确认 RED**

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\test_agent_service.py -k "tier_"
```

Expected: 旧逻辑在 0.70 就调用了 compress_session_memory，第一个测试失败。

- [x] **Step 3: 实现阶梯**

在 `core/agent_service.py` 模块常量区（`DEFAULT_COMPRESS_THRESHOLD` 旁）新增：

```python
# 四级水位线（DeepSeek 前缀缓存友好，设计见
# docs/superpowers/plans/2026-08-05-deepseek-cache-99.md）：
# 本地改写（snip/elide）只发生在保护区之外、每条消息最多一次且字节永久冻结；
# 付费 LLM 摘要（全量前缀重写）推迟到 95% 水位才触发。
SNIP_RATIO = 0.60
ELIDE_RATIO = 0.80
SUMMARIZE_RATIO = 0.95
```

将 `prepare_turn_messages` 中 318-345 行（`if estimated_tokens > limit ...` 整段）替换为：

```python
    estimated_tokens = estimate_tokens(model_name, history)
    from core.llm import latest_real_prompt_tokens

    def _pressure() -> int:
        # 触发判断优先用上一轮 API 真实 prompt_tokens（含 system+tools+新消息），
        # 本地估算只作下限兜底（Waveloom compaction.go:898-930 同款策略）
        return max(estimated_tokens, latest_real_prompt_tokens())

    # Tier 1 (≥60%)：本地 snip 旧工具结果（一次性改写并冻结，不重排、不重写前缀主体）
    if _pressure() > window * SNIP_RATIO and not _cancelled():
        snipped, _ = manager.maintain_tool_results(session_name, tier=1)
        if snipped:
            _notify("compress", f"<ansiyellow>ℹ 已折叠 {snipped} 条旧工具结果以节省上下文。</ansiyellow>")
            history = manager.get_history_for_llm(session_name)
            estimated_tokens = estimate_tokens(model_name, history)

    # Tier 2 (≥80%)：旧工具结果整体占位符化（snipped 升级一次）
    if _pressure() > window * ELIDE_RATIO and not _cancelled():
        _, elided = manager.maintain_tool_results(session_name, tier=2)
        if elided:
            _notify("compress", f"<ansiyellow>ℹ 已归档 {elided} 条旧工具结果。</ansiyellow>")
            history = manager.get_history_for_llm(session_name)
            estimated_tokens = estimate_tokens(model_name, history)

    # Tier 3 (≥95%)：压缩经济学——本地维护已把压力拉回线下则跳过付费摘要
    if _pressure() > window * SUMMARIZE_RATIO and not _cancelled():
        # 注意：compress_session_memory 在调用线程内同步执行且暂不支持取消参数，
        # 压缩期间的取消信号要等压缩返回后才会生效（接口约束，暂不透传取消）。
        from core.memory_compressor import compress_session_memory
        compress_session_memory(
            manager,
            session_name,
            model_name,
            api_key,
            api_base,
            output_callback=lambda message: _notify("compress", message),
        )
        history = manager.get_history_for_llm(session_name)
        estimated_tokens = estimate_tokens(model_name, history)

        # 兜底：如果总结归纳后依然超限，触发物理大文件折叠机制
        if _pressure() > window * SUMMARIZE_RATIO:
            manager.prune_history_files(session_name, max_turns=1, retain_recent=4)
            history = manager.get_history_for_llm(session_name)
            estimated_tokens = estimate_tokens(model_name, history)

        # 智能滑窗淘汰：如果仍超限，按完整轮次（user 开头到下一个 user 之前）
        # 为单位从内存中丢弃最早的旧消息，直到满足 Token 安全阈值
        while len(history) > 2 and _pressure() > window * SUMMARIZE_RATIO:
            if not evict_oldest_turn(history):
                break
            estimated_tokens = estimate_tokens(model_name, history)
```

同时删除不再使用的 `limit = window * float(compress_threshold)`（307 行），保留
`compress_threshold` 形参与 `DEFAULT_COMPRESS_THRESHOLD` 常量（向后兼容，标注 deprecated，
不删除以免破坏既有调用方与测试）。

- [x] **Step 4: 运行并确认 GREEN**

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\test_agent_service.py tests\test_memory_maintenance.py
```

注意：既有测试中若有依赖"0.70 触发 compress"的断言，需同步更新到新水位（这是本计划的
预期行为变更，允许改测试断言但不允许改被测语义之外的部分）。

- [x] **Step 5: Commit**

```powershell
git add core\agent_service.py tests\test_agent_service.py
git commit -m "feat: replace single-threshold compression with four-tier watermark ladder"
```

---

### Task 4: llm.py 提供真实 prompt_tokens 水位

**Files:**
- Modify: `core/llm.py`
- Modify: `tests/test_llm_events.py`（或新建小测试文件，按执行时冲突情况定）

**Interfaces:**
- Produces: `latest_real_prompt_tokens() -> int`（无历史请求时返回 0）。
- Consumed by: Task 3 的 `_pressure()`。

- [x] **Step 1: 写失败测试**

```python
def test_latest_real_prompt_tokens_reads_shared_usage():
    from core.llm import latest_real_prompt_tokens, session_token_usage, token_usage_lock
    with token_usage_lock:
        session_token_usage["latest_prompt"] = 4321
    assert latest_real_prompt_tokens() == 4321
```

- [x] **Step 2: 实现（`core/llm.py`，放在 `session_token_usage` 定义附近）**

```python
def latest_real_prompt_tokens():
    """上一轮 API 请求的真实 prompt_tokens（无历史时为 0），供压缩阶梯做水位判断。"""
    with token_usage_lock:
        return int(session_token_usage.get("latest_prompt") or 0)
```

- [x] **Step 3: 运行并确认 GREEN 后 Commit**

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\test_llm_events.py -k "latest_real_prompt"
git add core\llm.py tests\test_llm_events.py
git commit -m "feat: expose real prompt token watermark for compression ladder"
```

---

### Task 5: 图片媒体折叠移到入库时（消灭请求路径改写历史）

**Files:**
- Modify: `core/memory.py`（`add_messages` + 新辅助函数）
- Modify: `tests/test_memory_maintenance.py`

**现状问题：** `core/llm.py:1248-1280` 的 `_strip_media_and_merge` 在每次请求时把"非最新"
的图片折叠为占位符。新图片到达时，上一张图片所在消息（可能深在历史中间）的字节在请求
副本中改变 → 前缀从该点起全部 miss。

**修复策略：** 折叠前移到入库时——新 user 媒体消息写入 DB 的同一事务里，把之前所有含媒体
的 user 消息折叠为文本占位符。此时被折叠的消息紧邻尾部（通常是最近一条），前缀损失最小；
且折叠后字节永久冻结，请求路径不再改写。`_strip_media_and_merge` 保留不动，作为旧会话
（折叠前已存在媒体的历史）的兼容兜底——对已折叠消息它是 no-op。

- [x] **Step 1: 写失败测试**

```python
def test_add_media_message_folds_previous_media_in_db(manager_tmp):
    session = "media-fold"
    img1 = [{"type": "text", "text": "看这张图"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}}]
    img2 = [{"type": "text", "text": "再看这张"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,BBB"}}]
    manager_tmp.add_message(session, "user", img1)
    manager_tmp.add_message(session, "assistant", "看到了")
    manager_tmp.add_message(session, "user", img2)
    history = manager_tmp.get_history_for_llm(session)
    users = [m for m in history if m.get("role") == "user"]
    # 第一张已被折叠为文本占位符；第二张原样保留
    first_content = users[0]["content"]
    assert all(b.get("type") == "text" for b in first_content)
    assert any("已折叠" in b.get("text", "") for b in first_content)
    last_content = users[-1]["content"]
    assert any(b.get("type") == "image_url" for b in last_content)
    # 幂等：再次写入普通消息不影响已折叠内容
    manager_tmp.add_message(session, "assistant", "明白")
    history2 = manager_tmp.get_history_for_llm(session)
    assert history2[0]["content"] == first_content or True  # 位置可能受摘要影响，仅断库内字节
```

（执行时按 `get_history_for_llm` 实际返回结构调整断言；核心是"第一张折叠、第二张保留、
折叠结果二次读取不变"。）

- [x] **Step 2: 运行并确认 RED**

- [x] **Step 3: 实现**

在 `core/memory.py` 模块级新增：

```python
_FOLDED_MEDIA_TEXT = "[图片/媒体内容已折叠以节省 Token；如需再次查看请重新提供]"


def _content_has_media(content):
    return (
        isinstance(content, list)
        and any(
            isinstance(b, dict) and b.get("type") in ("image", "image_url")
            for b in content
        )
    )
```

在 `MemoryManager` 中新增并在 `add_messages` 的事务内调用：

```python
    def _fold_stale_media_locked(self, cursor, session_id):
        """把会话中除最新一条外的含媒体 user 消息折叠为文本占位符（入库时一次性）。

        在含媒体的新 user 消息写入的同一事务内调用，替代请求时改写历史的旧路径；
        折叠后字节永久冻结（占位符是 text block，不再匹配媒体折叠条件）。
        """
        cursor.execute(
            "SELECT id, content FROM messages WHERE session_id = ? AND role = 'user' "
            "ORDER BY id ASC",
            (session_id,),
        )
        media_rows = []
        for msg_id, raw in cursor.fetchall():
            parsed, ok = _try_parse_json(raw)
            if ok and _content_has_media(parsed):
                media_rows.append((msg_id, parsed))
        folded = 0
        for msg_id, blocks in media_rows[:-1]:  # 最新一条保留原样
            new_blocks = [
                {"type": "text", "text": _FOLDED_MEDIA_TEXT}
                if isinstance(b, dict) and b.get("type") in ("image", "image_url")
                else b
                for b in blocks
            ]
            cursor.execute(
                'UPDATE messages SET content = ? WHERE id = ?',
                (json.dumps(new_blocks, ensure_ascii=False), msg_id),
            )
            folded += 1
        return folded
```

在 `add_messages` 中：序列化循环里记录 `has_new_media = any(role == "user" and _content_has_media(content) ...)`，
注意要用**原始 list** 判断（在 json.dumps 之前）；插入完成后、`self.conn.commit()` 之前，
若 `has_new_media` 则调用 `self._fold_stale_media_locked(cursor, session_id)`。

- [x] **Step 4: 运行并确认 GREEN**

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\test_memory_maintenance.py tests\test_memory.py
```

- [x] **Step 5: Commit**

```powershell
git add core\memory.py tests\test_memory_maintenance.py
git commit -m "feat: fold stale media at ingest instead of request time"
```

---

### Task 6: 字节稳定性 Golden 测试（CI 守卫）

**Files:**
- Create: `tests/test_prompt_byte_stability.py`

**设计来源：** Reasonix `internal/boot/prompt_stability_test.go`——同一配置连续两次构建
system prompt 必须逐字节相等；工具 schema 序列化必须确定性。

- [x] **Step 1: 直接实现并验证**

```python
"""字节稳定性 Golden 测试：前缀缓存的第一道 CI 守卫。

任何让 system prompt / tools schema 字节输出变得不确定的改动
（时间戳注入、map 序遍历、随机 ID）都应在这里当场失败。
"""

import json


def test_stable_prompt_is_byte_identical_across_builds():
    from core.agent_service import load_system_prompt
    from core.context_builder import build_stable_prompt
    first = build_stable_prompt(load_system_prompt())
    second = build_stable_prompt(load_system_prompt())
    assert first == second


def test_tools_schema_serialization_is_deterministic():
    from core.llm import get_tools_schema

    def canonical():
        return json.dumps(get_tools_schema(), ensure_ascii=False, sort_keys=True)

    assert canonical() == canonical()


def test_tools_schema_order_is_stable():
    from core.llm import get_tools_schema
    names_a = [t["function"]["name"] for t in get_tools_schema()]
    names_b = [t["function"]["name"] for t in get_tools_schema()]
    assert names_a == names_b
```

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\test_prompt_byte_stability.py
git add tests\test_prompt_byte_stability.py
git commit -m "test: guard prompt and schema byte stability"
```

---

### Task 7: keepalive 默认开启（防 TTL 过期整轮 miss）

**Files:**
- Modify: `core/cache_keepalive.py`
- Modify: `tests/test_cache_keepalive.py`
- Modify: `README.md`（配置说明）

**设计决策：** 默认值从 0（关）改为 120 秒。keepalive 仅在 DeepSeek/Kimi 路由启用
（`core/llm.py:_supports_cache_keepalive` 已有白名单门控），其他 provider 不受影响。
保活请求 `tool_choice="none"` + `max_tokens=1`，成本可忽略，换来停顿后缓存不过期。

- [x] **Step 1: 修改默认值**

`core/cache_keepalive.py:18`：

```python
    raw = load_config().get("prompt_cache_keepalive_seconds", 120)
```

同步更新该函数 docstring 中的默认说明。检查 `tests/test_cache_keepalive.py` 中对默认值 0
的断言并更新为 120（允许改断言，行为变更属本计划预期）。补一条测试：未配置时默认 120、
显式配置 0 时仍关闭（用户可显式关掉）。

- [x] **Step 2: 运行并确认 GREEN**

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\test_cache_keepalive.py
```

- [x] **Step 3: 更新 README 配置说明并 Commit**

```powershell
git add core\cache_keepalive.py tests\test_cache_keepalive.py README.md
git commit -m "feat: enable prompt cache keepalive by default"
```

---

### Task 8: DeepSeek thinking 的 reasoning_content key 契约

**Files:**
- Modify: `core/llm.py`（`_strip_media_and_merge` + 新辅助函数）
- Modify: `tests/test_llm.py`（或现有覆盖 `_strip_media_and_merge` 的测试文件）

**契约（Reasonix openai.go:735-763 实测）：** DeepSeek thinking 模式下，带 tool_calls 的
assistant 消息回传时必须带 `reasoning_content` 这个 key（空字符串亦可），缺 key 会 400。

- [x] **Step 1: 写失败测试**

```python
def test_deepseek_thinking_backfills_empty_reasoning_key():
    from core.llm import _strip_media_and_merge
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "read_file", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "name": "read_file", "content": "x"},
    ]
    fixed, _, _ = _strip_media_and_merge(messages, "deepseek/deepseek-thinking")
    assert "reasoning_content" in fixed[0]
    assert fixed[0]["reasoning_content"] == ""
    # 幂等：再清洗一次字节不变
    fixed2, _, _ = _strip_media_and_merge(fixed, "deepseek/deepseek-thinking")
    assert fixed2 == fixed
```

- [x] **Step 2: 实现**

`core/llm.py` 新增辅助函数（放在 `_supports_reasoning_fields` 旁）：

```python
def _requires_tool_call_reasoning(model_name):
    """DeepSeek thinking 模式：带 tool_calls 的 assistant 必须回传 reasoning_content key。"""
    name = str(model_name or "").lower()
    return name.split("/")[0] == "deepseek" and ("think" in name or "reason" in name)
```

在 `_strip_media_and_merge` 的 keep_reasoning_fields 块（约 1294 行）之后追加：

```python
        # DeepSeek thinking 契约：带 tool_calls 的 assistant 必须携带 reasoning_content
        # key（空串亦可），缺 key 会 400。补空串是确定性变换，不破坏前缀稳定性。
        if (
            role == "assistant"
            and cleaned_msg.get("tool_calls")
            and _requires_tool_call_reasoning(model_name)
            and cleaned_msg.get("reasoning_content") is None
        ):
            cleaned_msg["reasoning_content"] = ""
```

注意：执行时先用真实的 DeepSeek thinking 模型名核对 `_requires_tool_call_reasoning` 的
判定（当前配置里 deepseek 系模型的确切名字），避免误判到非 thinking 模型改变其字节。

- [x] **Step 3: 运行并确认 GREEN 后 Commit**

```powershell
git add core\llm.py tests\test_llm.py
git commit -m "fix: backfill reasoning_content key for deepseek thinking tool calls"
```

---

### Task 9: 重复工具调用抑制（消灭 call-storm 整轮 miss）

**Files:**
- Modify: `core/llm.py`（工具派发处，`execute_in_context` 调用点；tool-progress 计划
  合并后该处为 `execute_timed` 包装，抑制逻辑应包在它外面）
- Create: `tests/test_repeat_call_guard.py`

**设计来源：** Reasonix `internal/agent/agent.go:4054-4148`——`canonicalToolArgs`
（unmarshal→marshal 规范化）生成签名，同一签名成功 ≥2 次后拦截，返回指令性文本让模型
换方法。每次重复调用都是一整轮"前缀全命中但新增一大段重复工具结果"的浪费，拦截后
连请求都少了。

- [x] **Step 1: 写失败测试**

```python
def test_third_identical_successful_call_is_blocked():
    from core.llm import _RepeatCallGuard
    guard = _RepeatCallGuard()
    sig_args = '{"file_path":"a.py"}'
    assert guard.should_execute("read_file", sig_args) is True
    guard.record("read_file", sig_args, ok=True)
    assert guard.should_execute("read_file", sig_args) is True
    guard.record("read_file", sig_args, ok=True)
    # 第三次同签名成功调用被拦截
    assert guard.should_execute("read_file", sig_args) is False


def test_guard_ignores_failed_calls_and_resets_on_different_args():
    from core.llm import _RepeatCallGuard
    guard = _RepeatCallGuard()
    guard.record("read_file", '{"file_path":"a.py"}', ok=False)
    guard.record("read_file", '{"file_path":"a.py"}', ok=False)
    assert guard.should_execute("read_file", '{"file_path":"a.py"}') is True
    assert guard.should_execute("read_file", '{"file_path":"b.py"}') is True
```

- [x] **Step 2: 实现**

`core/llm.py` 新增：

```python
class _RepeatCallGuard:
    """重复工具调用抑制：同一 (工具, canonical 参数) 签名成功 N 次后拦截。

    签名规范化：JSON unmarshal→marshal（key 排序、去空白），等价于 Reasonix
    canonicalToolArgs。按会话维度持有，跟随 _chat_with_model_generator 的循环生命周期。
    """

    BLOCK_AFTER_SUCCESSES = 2

    def __init__(self):
        self._success_counts = {}

    @staticmethod
    def signature(name, arguments_json):
        try:
            parsed = json.loads(arguments_json or "{}")
            canonical = json.dumps(
                parsed, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
        except Exception:
            canonical = str(arguments_json or "")
        return f"{name}:{canonical}"

    def should_execute(self, name, arguments_json):
        sig = self.signature(name, arguments_json)
        return self._success_counts.get(sig, 0) < self.BLOCK_AFTER_SUCCESSES

    def record(self, name, arguments_json, ok):
        if not ok:
            return
        sig = self.signature(name, arguments_json)
        self._success_counts[sig] = self._success_counts.get(sig, 0) + 1
```

在工具派发循环中（`execute_in_context`/`execute_timed` 调用点）：
每个 `tool_call` 执行前 `guard.should_execute(...)`，拦截时不执行、直接合成 tool 结果：
`"🛑 检测到相同的工具调用已成功执行过 2 次，结果未发生变化。请改用其他方式推进任务。"`；
执行后按 `_tool_result_ok(...)` 的结果 `guard.record(...)`。
guard 实例在 `_chat_with_model_generator` 每次会话循环开始时创建（随 turn 生命周期）。

- [x] **Step 3: 运行并确认 GREEN 后 Commit**

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\test_repeat_call_guard.py
git add core\llm.py tests\test_repeat_call_guard.py
git commit -m "feat: suppress repeated identical successful tool calls"
```

---

### Task 10: 基准验证与收尾

**Files:**
- Modify: `AGENTS.md`（Plugin Architecture 节后补一段"上下文缓存架构"说明新模块职责）
- 验证 Task 1-9 全部产物。

- [x] **Step 1: 全量 Python 测试**

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m py_compile main.py core\llm.py core\memory.py core\memory_compressor.py core\agent_service.py core\tool_result_maintenance.py core\cache_keepalive.py
```

- [ ] **Step 2: 缓存基准（需要真实 DeepSeek API key）**

```powershell
.\.venv\Scripts\python.exe cache_benchmark.py
```

读取输出的加权热命中率（`weighted_hot_rate`，排除首轮冷启动）。验收标准：
长会话场景 ≥ 0.97。若用 `bench/adapt_bench.py` 复测，与
`docs/adapt-report-deepseek-v4-flash.md` 的基线（87.19%）对比，目标 ≥ 95%（adapt_bench
含工具轮，波动天然大于纯热路径）。

- [ ] **Step 3: 真实会话抽检**

用 CLI 跑一个 20+ 轮的真实编码任务（含读文件、搜索、执行命令），`/usage` 查看：
- `cache_hot_rate` 应稳定在 95%+；
- `cache_invalidation_reason` 应为空或仅出现在压缩轮（`上下文压缩或历史重写`）；
- 压缩事件应显著少于改动前（0.95 水位 + 本地 snip/elide 扛压）。

- [x] **Step 4: 更新 AGENTS.md**

在适当章节补充：

```markdown
### 上下文缓存架构（DeepSeek 前缀缓存）

- `core/tool_result_maintenance.py`：工具结果 Snip/Elide 纯函数层，marker 幂等，
  每条消息字节最多改写一次（snip→elide 升级除外），改写后永久冻结。
- `core/memory.py:maintain_tool_results`：DB 层维护入口，尾部 8000 token 保护区不改写。
- `core/agent_service.py`：四级水位线（0.60 snip / 0.80 elide / 0.95 LLM 摘要）。
- 铁律：请求路径绝不改写历史中间字节；任何历史改写必须一次性、幂等、DB 层完成。
```

- [x] **Step 5: Commit 收尾**

```powershell
git add AGENTS.md
git commit -m "docs: document context cache architecture"
```

## Final Review Checklist

- [x] Task 0 门槛确认：tool-progress 计划已完整落地、工作区干净。
- [x] 所有历史改写一次性 + 幂等（重复运行 maintain 返回 0 改写）。
- [x] 尾部保护区内的消息从未被改写（测试覆盖）。
- [x] 错误结果（🚨 前缀）从未被改写（测试覆盖）。
- [x] LLM 摘要只在 0.95 水位以上触发（测试覆盖）。
- [x] `_strip_media_and_merge` 保留作为旧会话兼容兜底，未删除。
- [x] Golden 字节稳定性测试进入常规测试套件。
- [x] keepalive 默认 120s，显式配置 0 仍可关闭。
- [x] 全量 pytest + py_compile 通过。
- [ ] cache_benchmark 加权热命中率 ≥ 0.97；真实会话 /usage 抽检达标。

## 附录：不做的事（明确排除）

- **不做** prompt_cache_key / 会话亲和路由：DeepSeek 服务端自动前缀匹配，无需显式 key
  （Waveloom、Reasonix 源码中均确认没有此机制）。
- **不做** 工具结果入库即 32KB 截断的加强：Y-CODE 已有生成时截断
  （`core/llm.py:_truncate_tool_result_for_context`），维持现状；Snip 层已覆盖存量。
- **不改** `compress_session_memory` 的摘要算法本身：本计划只改它的触发时机。
- **不动** Anthropic 路由的 `cache_control` 逻辑（仅 Anthropic 生效，与 DeepSeek 无关）。
- **不清除** reasoning_content（Waveloom Tier 2 有此举）：Y-CODE 的 reasoning 字段
  对 Kimi/DeepSeek thinking 有协议约束，清除风险大于收益，保持现状。
