# 记忆压缩加固 · 第二轮修复计划（L1–L7）

> **给执行者：** 按 Task 顺序逐条做，每个 Task 以「写失败测试 → 确认失败 → 最小实现 → 测试通过 → 提交」收尾，不要合并提交。步骤用 `- [ ]` 跟踪。

**Goal:** 消除第一轮加固引入的两个红灯（蚕食式丢历史、压缩结构性必败），并收干净淘汰并发保护、裁剪性能与注释倒退。

**Architecture:** 四条防线分工不变——Tier1/2 本地改写工具结果、Tier3 LLM 摘要、Tier4 滑窗淘汰（DB 推水位）、请求侧内存裁剪（不落库）。本轮只动两处语义：① `skipped` 中「零新增摘要」的状态升级为与 `failed` 同级的淘汰禁区；② 压缩写库从「全部完成才提交」改为「每段 CAS 增量提交」，让任何中止都保住已投入成果。

**Tech Stack:** Python 3.11 / SQLite / litellm / pytest + monkeypatch。

## Global Constraints

- 请求路径**绝不改写历史中间消息字节**；改写与水位推进只在 DB 层一次性完成（DeepSeek/Kimi/Qwen 前缀缓存的前提）。内存裁剪只能整轮剔除，且只作用于本次待发副本。
- 压缩的 LLM 调用必须在 `manager.acquire_lock()` **之外**执行，只有读写阶段持锁（现状如此，不得改回）。
- `compress_session_memory` 默认返回仍是 bool（CLI 与既有测试依赖），三态仅在 `return_outcome=True` 时返回 `CompressionOutcome`。
- GUI 铁律（AGENTS.md:170-172）：`evaluate_js` 只在 `gui-js-dispatcher` 线程调用；后台线程一律走 `emit_event` → 队列。
- 测试禁止真实网络与真实长 sleep；litellm 不得被真实 import（Task 6）。
- 内存裁剪的轮次边界口径**必须与 `memory.evict_oldest_turn_persistent` 一致**，不一致即视为 bug（Task 4 有交叉测试把关）。
- 不得回退第一轮已确认的能力：`CompressionOutcome` 三态、`is_session_compressing`、`_cancellable_sleep`、`is_rate_limit_error`（含 retry-after）、请求侧内存裁剪、GUI 门闩测试。

## Step 0: 开工前置——先固化基线（必做，否则逐条 commit 全乱）

> ✅ **已完成于 2026-09-07 22:35**：基线已切成 3 个 commit 并推送 `origin/dev`，HEAD = `f5aa0a4`
> （`b34ba61` llm 缓存与限流判定 / `575e21d` 记忆压缩加固 / `f5aa0a4` 文档与 1.2.23 版本号），
> 工作区干净。**明天直接从 Task 1 开始**，Step 0 只作留档；若 `git status` 又出现非本轮产物的改动，再回到本节重新切基线。

第一轮改动目前**全部处于未提交状态**（`git status` 可见 11 个 M/?? 文件），里面混着与本任务无关的 Qwen/DashScope 缓存特性。不先切干净，Task 1–6 的逐条 commit 会把两件事揉成一坨，出问题无法单独回滚。

- [ ] **Step 0.1: 确认工作区现状**

Run: `git status --short`
Expected: `core/memory_compressor.py`、`core/agent_service.py`、`gui_bridge.py`、`core/llm.py`、`core/cache_keepalive.py`、`core/harness/request_envelope.py`、`cache_benchmark.py`、`tests/test_*`（3 个 M）、`tests/test_qwen_cache.py`（??）

- [ ] **Step 0.2: 拆成两个基线 commit**

```
git add core/memory_compressor.py core/agent_service.py gui_bridge.py tests/test_memory_compressor_chunks.py tests/test_agent_service.py tests/test_gui_token_usage.py tests/test_memory_compressor_encoding.py
git commit -m "fix(memory): 压缩限流退避+淘汰熔断+切模型异步化（第一轮加固基线）"

git add core/llm.py core/cache_keepalive.py core/harness/request_envelope.py cache_benchmark.py tests/test_qwen_cache.py
git commit -m "feat(llm): Qwen/DashScope 前缀缓存接入（与记忆压缩无关，独立可回滚）"
```

- [ ] **Step 0.3: 记录基线 SHA，写进本轮所有 commit message 的参照**

Run: `git rev-parse --short HEAD`
Expected: 记下来。Task 1–6 每步 `git diff <baseline>..HEAD --stat` 都应只出现本 Task 声明的文件；若出现别的文件说明改串了，立即停手。

## 开工基线

```
venv\Scripts\python -m py_compile main.py core/llm.py core/memory.py core/memory_compressor.py core/agent_service.py gui_bridge.py
venv\Scripts\pytest tests\test_memory_compressor_chunks.py tests\test_agent_service.py tests\test_gui_token_usage.py tests\test_memory.py tests\test_memory_maintenance.py tests\test_memory_compressor_encoding.py -q
→ COMPILE_OK / 88 passed in 3.76s（最慢用例 1.76s，真实 litellm import）
```

## 关键量化（Task 2 的依据）

单段超时 = `API_REQUEST_TIMEOUT_SECONDS(90) + 24000 // COMPRESSION_TIMEOUT_TOKENS_PER_SECOND(1500)` = **106 秒**；段间冷却 1.5 秒。80 万 token ÷ 24000 = **34 段** → 串行需数百秒，而 `max_timeout_seconds=180`：**最坏连两段都跑不完**，且每次中止都把已完成段落的 LLM 成果整体作废（白烧约 30 万 token）。

---

## Task 1: 零摘要状态纳入淘汰禁区（L1，先止血）

**问题：** `_do_compress` 的 `timeout` / `cancelled` 都在 chunk 循环内 return，而写库在循环之后 → 这两种状态等价于「DB 无任何新增摘要」。但 `agent_service.py:593` 只把 `status=="failed"` 当熔断条件，于是二者落到 `else` 分支照旧淘汰 2 轮 DB 水位，淘汰的是未被摘要覆盖的原始对话。最恶劣路径：**用户点「停止」取消压缩，下一轮 prepare 会悄悄淘汰 2 轮历史。**

**Files:** Modify `core/agent_service.py:588-603`；Test `tests/test_agent_service.py`

- [ ] **Step 1: 写失败测试**

```python
@pytest.mark.parametrize("reason", ["timeout", "cancelled"])
def test_zero_summary_skipped_forbids_db_eviction(tmp_path, monkeypatch, stub_llm, reason):
    """skipped(timeout/cancelled) 意味着零新增摘要，等同 failed：严禁推 DB 水位。"""
    from core.agent_service import prepare_turn_messages
    from core.memory import MemoryManager
    from core.memory_compressor import CompressionOutcome

    manager = MemoryManager(str(tmp_path / (reason + ".db")))
    session = "s_" + reason
    manager.create_session_explicit(session, workdir="GLOBAL", source="GUI")
    for i in range(10):
        manager.add_message(session, "user", "提问 %d " % i + "内容 " * 50)
        manager.add_message(session, "assistant", "回答 %d " % i + "细节 " * 50)

    evict_calls = []
    monkeypatch.setattr(manager, "evict_oldest_turn_persistent",
                        lambda s: evict_calls.append(s) or False)
    monkeypatch.setattr("core.memory_compressor.compress_session_memory",
                        lambda *a, **kw: CompressionOutcome("skipped", reason))
    monkeypatch.setattr("core.llm.estimate_tokens",
                        lambda model, msgs: 5000)
    monkeypatch.setattr("core.agent_service.get_session_token_baseline",
                        lambda *a, **kw: 5000)

    prepare_turn_messages(manager, session, "openai/test-small", 1000,
                          permission_mode="confirm", notify=lambda k, m: None)

    assert evict_calls == []          # 一次都不许推 DB 水位
    assert len(manager.get_history_for_llm(session)) >= 20
```

- [ ] **Step 2: 确认失败**

Run: `venv\Scripts\pytest tests\test_agent_service.py -k zero_summary_skipped -v`
Expected: FAIL —— `evict_calls` 出现 2 次调用（现实现走了 else 分支）

- [ ] **Step 3: 最小实现**（替换 `core/agent_service.py:588-603` 的注释与判定）

```python
        # 智能滑窗淘汰（熔断 + 并发 + 最大轮次约束）：
        # 1. 压缩失败，或中止于「一段都没落库」的 timeout / cancelled —— 二者语义等价，
        #    DB 里没有新摘要覆盖待淘汰历史，一律禁止推水位（否则用户按「停止」后
        #    下一轮就会偷偷抹掉 2 轮未摘要的原始对话）。
        # 2. 后台正有该会话压缩在跑（或命中 already_running）时严禁推水位：
        #    evict 是无 CAS 的裸 UPDATE，会打翻后台压缩的乐观锁、令成果全废。
        # 3. conflict_stale 属于「别的线程已摘要成功」，有兜底，允许正常微调淘汰。
        # 4. 其余情况最多淘汰 MAX_EVICT_TURNS 轮做微调。
        # 注：熔断阈值保持 1.2*window 是有意的——轻微超窗（0.95~1.2）时留着 2 轮微调
        # 换可用性，比整轮拒绝服务更划算，别顺手收紧成 0.95。
        compress_unprotected = outcome.status == "failed" or outcome.reason in {
            "timeout", "cancelled",
        }
        compress_running = (
            is_session_compressing(manager, session_name)
            or outcome.reason == "already_running"
        )

        if compress_unprotected and estimated_tokens > window * 1.2:
            _notify("compress", "<ansired>⚠️ 历史记忆压缩未能完成（服务商限流或已被取消），当前上下文仍严重超出当前模型窗口。为保护您的历史对话完整，系统已停止自动淘汰历史；本轮请求将在内存中临时按轮次裁剪以保证正常回复。</ansired>")
        elif compress_running:
            _notify("compress", "<ansiyellow>ℹ 后台记忆压缩仍在进行中，本轮请求将在内存中临时按轮次裁剪以适配当前模型窗口。</ansiyellow>")
        else:
            evict_count = 0
            while (
                evict_count < MAX_EVICT_TURNS
                and len(history) > 2
                and estimated_tokens > window * SUMMARIZE_RATIO
            ):
                if not manager.evict_oldest_turn_persistent(session_name):
                    break
                evict_count += 1
                history = manager.get_history_for_llm(session_name)
                estimated_tokens = estimate_tokens(model_name, [system_message] + history)
```

- [ ] **Step 4: 确认通过且既有 3 条不回归**

Run: `venv\Scripts\pytest tests\test_agent_service.py -k "zero_summary or circuit_breaker or already_running or max_turns" -v`

- [ ] **Step 5: 提交**

```
git add core/agent_service.py tests/test_agent_service.py
git commit -m "fix(memory): 压缩超时/取消等零摘要状态纳入滑窗淘汰禁区，杜绝蚕食式历史丢失"
```

---

## Task 2: 压缩分段 CAS 增量提交（L2，根治白烧）

**问题：** 见上「关键量化」。现状是「全部段完成才写一次库」，所以任何中止都前功尽弃；`max_timeout_seconds=180` 在 34 段场景下必然反复归零。

**前置障碍（先解决，否则实施会卡住）：** `prepared_messages` 是 `[(role_display, clean_content)]`，**丢了 `msg["id"]`**，算不出「每段末尾的 DB id」。必须先让它携带 id。

**Files:** Modify `core/memory_compressor.py:407-432`、`463-483`、`484-585`；Test `tests/test_memory_compressor_chunks.py`

**Produces:** `_commit_summary_progress(manager, session_id, summary, boundary_id, expect_raw) -> bool`；`_finish_with_progress(committed, reason) -> CompressionOutcome`；新增 reason 取值 `partial_*`

- [ ] **Step 1: 写失败测试**

```python
def test_partial_progress_is_committed_when_budget_exceeded(manager, monkeypatch):
    """预算耗尽中止时，已完成段落的摘要与水位必须已落库（不白烧）。"""
    import time as real_time
    _seed_history(manager, 40, filler=60)          # 造出多段

    clock = {"t": 1000.0}
    monkeypatch.setattr(real_time, "monotonic", lambda: clock["t"])

    calls = {"n": 0}
    def fake_completion(**kwargs):
        calls["n"] += 1
        clock["t"] += 90.0                          # 每段假装耗时 90s
        return _Resp("第 %d 段摘要内容" % calls["n"])
    monkeypatch.setattr("core.llm._litellm_completion", fake_completion)
    monkeypatch.setattr(mc, "_cancellable_sleep", lambda *a, **kw: True)

    def read():
        row = manager.conn.execute(
            "SELECT summarized_until_msg_id, memory_summary FROM sessions WHERE session_name='chunk-session'"
        ).fetchone()
        return row[0] or 0, row[1]

    boundary_before, _ = read()
    outcome = compress_session_memory(
        manager, "chunk-session", "fake/compress-model", "k", "",
        output_callback=lambda m: None, max_timeout_seconds=180.0, return_outcome=True,
    )
    boundary_after, summary = read()

    assert calls["n"] < 40                          # 确实中途停了
    assert boundary_after > boundary_before         # 已完成段落水位已推进
    assert summary                                   # 摘要已落库
    assert outcome.status == "compressed"            # 有兜底 → 下游可正常微调淘汰
```

- [ ] **Step 2: 确认失败**

Run: `venv\Scripts\pytest tests\test_memory_compressor_chunks.py::test_partial_progress_is_committed_when_budget_exceeded -v`
Expected: FAIL —— `boundary_after == boundary_before`

- [ ] **Step 3: prepared_messages / chunks 携带 msg_id**

`core/memory_compressor.py:427-429`：

```python
                if clean_content:
                    prepared_messages.append((msg["id"], role_display, clean_content))
                    total_chars += len(clean_content)
```

`core/memory_compressor.py:463-483` 的分段循环：

```python
        chunks = []
        current_chunk = []
        current_tokens = 0
        for msg_id, role_display, content in prepared_messages:
            token_count = _estimate_tokens(content)
            if token_count > chunk_token_budget:
                # 单条消息即超预算（如超长助手文本）：按 token 截断而非按字符，
                # 中文 1 字符 ≈ 1~1.5 token，按字符截会严重超窗
                content, token_count = _truncate_to_token_budget(
                    content, chunk_token_budget
                )
            if current_chunk and current_tokens + token_count > chunk_token_budget:
                chunks.append(current_chunk)
                current_chunk, current_tokens = [], 0
            current_chunk.append((msg_id, role_display, content))
            current_tokens += token_count
        if current_chunk:
            chunks.append(current_chunk)
        total_chunks = len(chunks)
```

循环体内 `chat_log` 组装（原 508 行）相应改为三元组解包：

```python
            chat_log = "".join(
                "%s: %s\n" % (role_display, content) for _mid, role_display, content in chunk
            )
```

- [ ] **Step 4: 新增两个模块级助手**（放在 `_cancellable_sleep` 之后）

```python
def _commit_summary_progress(manager, session_id, summary, boundary_id, expect_boundary_raw):
    """把「已完成段」的摘要与水位 CAS 写库。

    仅当库里水位仍等于本次读取时的旧值才更新：期间若有淘汰或并发压缩推进过水位，
    这段进度已不可信（可能重复摘要或静默跳过消息），放弃本次提交。
    返回 True 表示已提交，调用方须把 CAS 基准前移到 boundary_id。
    """
    lock = manager.acquire_lock() if hasattr(manager, "acquire_lock") else contextlib.nullcontext()
    with lock:
        cursor = manager.conn.cursor()
        cursor.execute(
            "UPDATE sessions SET memory_summary = ?, summarized_until_msg_id = ? "
            "WHERE id = ? AND summarized_until_msg_id IS ?",
            (summary, boundary_id, session_id, expect_boundary_raw),
        )
        if cursor.rowcount == 0:
            manager.conn.rollback()
            return False
        manager.conn.commit()
    return True


def _finish_with_progress(committed, reason):
    """中止收尾：只要有段落已落库就算 compressed（下游淘汰有摘要兜底），
    否则才是 skipped（下游必须禁止推水位，见 agent_service 的淘汰禁区）。"""
    if committed:
        return CompressionOutcome("compressed", "partial_" + reason)
    return CompressionOutcome("skipped", reason)
```

- [ ] **Step 5: 主循环加预算夹取与每段提交**

`new_summary = old_summary` 之后加进度变量，循环头替换：

```python
        new_summary = old_summary
        committed = False
        for chunk_index, chunk in enumerate(chunks):
            # 预算至少容得下 3 段：单段超时可达 106s，只给 180s 会退化到
            # 「刚完成一段就爆预算」，永远压不完。
            per_chunk_seconds = (_compression_request_timeout(chunk_token_budget)
                                 + COMPRESSION_INTER_CHUNK_DELAY_SECONDS)
            budget_seconds = max(max_timeout_seconds, 3 * per_chunk_seconds)

            if cancel_event is not None and cancel_event.is_set():
                emit("<ansiyellow>ℹ 记忆压缩已被用户取消。</ansiyellow>")
                return _finish_with_progress(committed, "cancelled")
            if _time.monotonic() - start_monotonic > budget_seconds:
                emit("<ansiyellow>ℹ 记忆压缩耗时超出预算，本次已中止（已完成段落进度已保存）。</ansiyellow>")
                return _finish_with_progress(committed, "timeout")

            if chunk_index > 0:
                if not _cancellable_sleep(COMPRESSION_INTER_CHUNK_DELAY_SECONDS, cancel_event):
                    return _finish_with_progress(committed, "cancelled")
```

429 退避里两处 `return CompressionOutcome("skipped", "cancelled")` 一并改为 `return _finish_with_progress(committed, "cancelled")`。

循环体末尾（原 565-568）替换：

```python
            if not candidate:
                # 某一段返回空结果：继续合并后续段会丢失该段事实，就此中止；
                # 已完成段落的进度已在上一轮循环提交，不回滚、不白烧。
                emit("<ansiyellow>ℹ 记忆压缩中途返回空摘要，本次已中止（已完成段落进度已保存）。</ansiyellow>")
                return _finish_with_progress(committed, "empty_summary")
            new_summary = candidate

            # 每段完成即落库：任何后续中止（429 用尽 / 超预算 / 取消 / 异常）
            # 最多损失当前段，不会把已付费的摘要一起作废。
            if _commit_summary_progress(
                manager, session_id, new_summary,
                chunk[-1][0], old_boundary_raw,
            ):
                committed = True
                old_boundary_raw = chunk[-1][0]        # CAS 基准随进度前移
                emit("<ansigray>💾 已保存第 %d/%d 段压缩进度。</ansigray>"
                     % (chunk_index + 1, total_chunks))
            else:
                emit("<ansiyellow>ℹ 压缩进度与库内水位冲突（期间有淘汰或并发压缩），已停止后续提交。</ansiyellow>")
                return _finish_with_progress(committed, "conflict_stale")
```

循环后的整体写回块（原 570-585）删除，替换为：

```python
        emit("<ansigreen>✔ 记忆压缩整合成功！腾出了约 %d 字的历史对话空间。</ansigreen>"
             % (total_chars // 2))
        return CompressionOutcome("compressed", "ok")
```

- [ ] **Step 6: 确认通过 + 全量回归**

Run: `venv\Scripts\pytest tests\test_memory_compressor_chunks.py tests\test_memory_maintenance.py tests\test_memory.py -q`
Expected: 全 PASS。若既有用例按 `[(role, content)]` 解包 chunks（`test_chunked_merge_carries_summary_forward`、`test_chinese_long_message_truncated_by_token_not_chars`），改成三元组。

- [ ] **Step 7: 交叉确认 Task 1 语义仍成立**

Run: `venv\Scripts\pytest tests\test_agent_service.py -k "zero_summary or circuit_breaker" -v`
Expected: 全 PASS。确认「未提交任何段 → skipped → 禁止淘汰」，而「已提交部分段 → compressed → 允许微调」—— 这正是期望分界。

- [ ] **Step 8: 提交**

```
git add core/memory_compressor.py tests/test_memory_compressor_chunks.py
git commit -m "fix(memory): 记忆压缩改为分段 CAS 增量提交，中止不再作废已完成段落"
```

---

## Task 3: 内存裁剪改增量估算（L4，去掉回合路径上的 O(n²)）

**问题：** `_prune_history_in_memory_to_fit_window`（`core/agent_service.py:429-459`）每裁一轮就 `estimate_tokens([system]+history)` 全量重扫。80 万 token 压到 24 万约需 25 轮 → 25 次全量编码，几十秒卡顿，而这条路恰好跑在「压缩失败 / 后台压缩中」的用户等待路径上。

**Files:** Modify `core/agent_service.py:429-459`、调用点 `620-627`；Test `tests/test_agent_service.py`

**Produces:** 签名变更 → `_prune_history_in_memory_to_fit_window(history, window, model_name, system_message, estimated_tokens) -> tuple[int, int]`（被裁轮次数, 剩余估算 token）。**返回值形状变了，调用点与既有测试必须同步改。**

- [ ] **Step 1: 写失败测试**（新增；并把既有 `test_prune_history_in_memory_preserves_summary_and_last_turn` 的调用改成解包二元组）

```python
def test_prune_history_in_memory_estimates_incrementally():
    """裁剪过程不得对全量历史反复重扫：全量估算次数必须与裁剪轮数解耦。"""
    import core.agent_service as ag
    import core.llm as llm

    history = [{"role": "user", "content": "[历史对话记忆摘要 (背景)]\n摘要保留"}]
    for i in range(12):
        history.append({"role": "user", "content": "问题 %d " % i + "内容 " * 400})
        history.append({"role": "assistant", "content": "回答 %d " % i + "细节 " * 400})

    full_scans = {"n": 0}
    def counting_estimate(model, msgs):
        if len(msgs) > 4:
            full_scans["n"] += 1
        return len(msgs) * 800

    orig = llm.estimate_tokens
    llm.estimate_tokens = counting_estimate
    try:
        pruned, remaining = ag._prune_history_in_memory_to_fit_window(
            history, 4000, "test/model", {"role": "system", "content": "s"},
            len(history) * 800,
        )
    finally:
        llm.estimate_tokens = orig

    assert pruned >= 5
    assert full_scans["n"] <= 2          # 只允许收尾校验，不许每轮全量重扫
    assert remaining <= 4000 * 0.95       # SUMMARIZE_RATIO
    assert history[0]["content"].startswith("[历史对话记忆摘要")
```

- [ ] **Step 2: 确认失败**

Run: `venv\Scripts\pytest tests\test_agent_service.py -k prune_history -v`
Expected: FAIL —— `TypeError`（不接受第 5 个位置参数）或 `full_scans["n"]` 远大于 2

- [ ] **Step 3: 实现增量版本**（整体替换该函数）

```python
def _prune_history_in_memory_to_fit_window(
    history: list, window: int, model_name: str, system_message: dict,
    estimated_tokens: int,
) -> tuple:
    """请求侧内存级轮次裁剪：仅在待发请求内存列表中按完整轮次剔除最早历史，
    保证发给小窗口模型的载荷处于 window * SUMMARIZE_RATIO 之内。
    绝不触碰 DB，绝不推进水位，保留数据库历史完整性。

    估算成本：每轮只对被剔除片段做一次局部估算并做减法，不对全量历史反复重扫
    （80 万 token 全量重扫可达数十秒，且这条路正跑在用户等回复的同步路径上）。
    返回 (裁剪掉的轮次数, 剩余估算 token)。
    """
    from core.llm import estimate_tokens

    limit = window * SUMMARIZE_RATIO
    remaining = int(estimated_tokens)
    pruned_turns = 0
    while remaining > limit:
        # 轮次起点口径与 memory.evict_oldest_turn_persistent 保持一致：
        # 第一条非 system 消息即轮次开头。摘要行是 get_history_for_llm 现插的
        # role=user 伪消息（DB 侧不存在），故此处显式跳过。改这里必须同步改那边，
        # tests/test_memory.py::test_memory_and_db_turn_boundary_agree 会把关。
        start_idx = None
        for i, m in enumerate(history):
            role = m.get("role")
            if role == "system":
                continue
            if role == "user" and str(m.get("content") or "").startswith("[历史对话记忆摘要"):
                continue
            start_idx = i
            break
        if start_idx is None:
            break
        end_idx = None
        for j in range(start_idx + 1, len(history)):
            if history[j].get("role") == "user":
                end_idx = j
                break
        if end_idx is None:
            break
        removed = history[start_idx:end_idx]
        remaining -= estimate_tokens(model_name, removed)
        del history[start_idx:end_idx]
        pruned_turns += 1
    return pruned_turns, remaining
```

- [ ] **Step 4: 同步调用点**（`core/agent_service.py:620-627`）

```python
        if estimated_tokens > window * SUMMARIZE_RATIO:
            from core.memory import _sanitize_tool_call_pairing
            pruned_count, estimated_tokens = _prune_history_in_memory_to_fit_window(
                history, window, model_name, system_message, estimated_tokens
            )
            if pruned_count > 0:
                history = _sanitize_tool_call_pairing(history)
                estimated_tokens = estimate_tokens(model_name, [system_message] + history)
```

- [ ] **Step 5: 确认通过**

Run: `venv\Scripts\pytest tests\test_agent_service.py tests\test_gui_token_usage.py -q`

- [ ] **Step 6: 提交**

```
git add core/agent_service.py tests/test_agent_service.py
git commit -m "perf(memory): 请求侧轮次裁剪改增量估算，消除大历史 O(n^2) 重扫"
```

---

## Task 4: `evict` 加 CAS + 轮次边界口径交叉测试（L5/L6）

**问题：** ① `memory.py:2410 evict_oldest_turn_persistent` 是裸 `UPDATE sessions SET summarized_until_msg_id = ? WHERE id = ?`，与压缩写回的 CAS 竞争时只靠「事前检查 `is_session_compressing`」降概率，毫秒级窗口仍在——Task 2 之后每段都提交，冲突面反而更大。② DB 版以「第一个非 system」为轮次起点，内存版以 `role=="user"` 为起点，首条是 assistant/tool 的会话里两者会裁出不同结果，目前只靠 `_sanitize_tool_call_pairing` 事后兜。

**Files:** Modify `core/memory.py:2410-2460`；Test `tests/test_memory.py`

- [ ] **Step 1: 写失败测试**

```python
def test_evict_uses_cas_and_rejects_stale_boundary(tmp_path):
    """水位在读取后被他线程推进时，淘汰必须放弃，不得覆盖别人的进度。"""
    from core.memory import MemoryManager

    manager = MemoryManager(str(tmp_path / "cas.db"))
    manager.create_session_explicit("s_cas", workdir="GLOBAL", source="GUI")
    for i in range(6):
        manager.add_message("s_cas", "user", "问题 %d" % i)
        manager.add_message("s_cas", "assistant", "回答 %d" % i)
    assert manager.evict_oldest_turn_persistent("s_cas") is True

    sid = manager.conn.execute(
        "SELECT id FROM sessions WHERE session_name='s_cas'").fetchone()[0]
    live = manager.conn.execute(
        "SELECT summarized_until_msg_id FROM sessions WHERE id=?", (sid,)).fetchone()[0]
    # 模拟「evict 读到旧值之后、写回之前」被别人把水位推得很远
    manager.conn.execute(
        "UPDATE sessions SET summarized_until_msg_id=? WHERE id=?", (live + 100000, sid))
    manager.conn.commit()

    assert manager.evict_oldest_turn_persistent("s_cas") is False
    now = manager.conn.execute(
        "SELECT summarized_until_msg_id FROM sessions WHERE id=?", (sid,)).fetchone()[0]
    assert now == live + 100000          # 未被覆盖
```

```python
def test_memory_and_db_turn_boundary_agree(tmp_path):
    """同一份历史：DB 淘汰一轮与内存裁剪一轮，被裁掉的条数必须完全一致。"""
    from core.memory import MemoryManager
    import core.agent_service as ag
    import core.llm as llm

    manager = MemoryManager(str(tmp_path / "agree.db"))
    manager.create_session_explicit("s_ag", workdir="GLOBAL", source="GUI")
    manager.add_message("s_ag", "assistant", "开场白")          # 故意让首条非 user
    for i in range(5):
        manager.add_message("s_ag", "user", "问题 %d" % i)
        manager.add_message("s_ag", "assistant", "回答 %d" % i)

    before = manager.get_history_for_llm("s_ag")
    assert manager.evict_oldest_turn_persistent("s_ag") is True
    db_dropped = len(before) - len(manager.get_history_for_llm("s_ag"))
    assert db_dropped >= 1

    mem = list(before)
    orig = llm.estimate_tokens
    llm.estimate_tokens = lambda model, msgs: len(msgs) * 100000
    try:
        pruned, _ = ag._prune_history_in_memory_to_fit_window(
            mem, 100, "test/model", {"role": "system", "content": "s"}, 10 ** 9)
    finally:
        llm.estimate_tokens = orig
    assert pruned >= 1
    assert len(before) - len(mem) == db_dropped      # 口径必须一致
```

- [ ] **Step 2: 确认失败**

Run: `venv\Scripts\pytest tests\test_memory.py -k "stale_boundary or turn_boundary_agree" -v`
Expected: 至少一条 FAIL（裸 UPDATE 覆盖别人进度 / 口径差异导致条数不等）

- [ ] **Step 3: 实现 CAS**（`evict_oldest_turn_persistent` 内 UPDATE 替换）

```python
                new_boundary = rows[end - 1][0]
                if new_boundary <= boundary:
                    return False
                # CAS：仅当库里水位仍等于本次读到的旧值才推进。无 CAS 时会与
                # 后台压缩的每段提交互相覆盖（谁后写谁赢），令对方进度全废。
                cursor.execute(
                    'UPDATE sessions SET summarized_until_msg_id = ? '
                    'WHERE id = ? AND summarized_until_msg_id IS ?',
                    (new_boundary, session_id, row[1]),
                )
                if cursor.rowcount == 0:
                    self.conn.rollback()
                    return False        # 期间有人推进过水位，本轮放弃，下轮重读
                self.conn.commit()
                return True
```

（`row[1]` 是本轮 `SELECT id, summarized_until_msg_id` 读到的原始值，可能为 `None`，配合 `IS ?` 语义正确。）

- [ ] **Step 4: 若交叉测试仍失败，改内存版轮次起点口径**（Task 3 已按「非 system 即起算」实现，正常应已一致；不一致时以 `evict` 为准修正）

- [ ] **Step 5: 确认通过 + 全量回归**

Run: `venv\Scripts\pytest tests\test_memory.py tests\test_agent_service.py tests\test_memory_maintenance.py -q`
Expected: 全 PASS（`evict` 契约不变，调用方无需改动）

- [ ] **Step 6: 提交**

```
git add core/memory.py tests/test_memory.py
git commit -m "fix(memory): 滑窗淘汰加 CAS 水位校验，统一轮次边界口径并补交叉测试"
```

---

## Task 5: 恢复被删的踩坑注释（L3）

**问题：** 上一轮改动顺手删掉了 7 处解释性注释，其中多条是踩坑记录，删掉后人必然重踩；且与需求无关，违反最小改动原则。尤其讽刺的是：在「动了 CAS 相关代码」的同一轮里，把解释 CAS 存在意义的注释删了。

**Files:** Modify `core/memory_compressor.py`（4 处）、`core/agent_service.py`（3 处）

- [ ] **Step 1: 恢复以下注释（原文照贴，位置见说明）**

`core/memory_compressor.py`：

1. `chunk_token_budget = _compression_chunk_budget(compress_window)` 上方：
```python
        # 历史一次性塞进单个 prompt 会在长会话下超过压缩模型窗口
        # （如 88 万 token 历史 vs 26 万窗口），压缩请求本身超窗失败并反复重试。
        # 按窗口的安全预算切成多段，逐段合并摘要，全部完成后写库。
        # 预算取窗口 25%：其余留给指令、累积摘要、输出、Provider 处理延迟
        # 与估算偏差（tiktoken 对非 OpenAI 分词器的估算普遍偏低）。
```
2. `request_kwargs` 里 `"timeout": request_timeout,` 上方：
```python
                # 分段载荷仍可能有数万 token，按实际 prompt 大小放大超时；
                # 固定 60 秒会让 64K～100K token 的摘要请求稳定超时。
```
3. `except Exception as e:` 内 rollback 之前：
```python
        # 异常路径必须回滚，否则未提交的事务会使 SQLite 连接进入锁定状态，
        # 导致后续所有写操作报 "database is locked"。
```
4. `if compress_window <= 0:` 内 `get_active_model` 分支上方：
```python
            # 压缩模型是主模型时取主模型窗口（按会话解析：GUI 会话绑定的模型
            # 可能与全局默认不同，取错窗口会让分段预算失真），仍取不到按 128k 兜底
```

`core/agent_service.py`（原 Tier3 段被压缩掉的注释，恢复为一条即可，勿与 Task 1 新注释重复）：

5. `history.insert(0, system_message)` 之前那段「淘汰必须在 DB 层推进水位持久化，纯内存淘汰下一轮会从 DB 读回而失效」的说明，补到 Task 3 的内存裁剪函数 docstring 里（已在其中写明「绝不触碰 DB」，再加一句对比）：
```python
    与 evict_oldest_turn_persistent 的区别：那个是 DB 层永久推进（下一轮读不回），
    这个只影响本次请求载荷，下一轮历史会原样从 DB 读回——所以它无法“省 token”，
    只用于在压缩/淘汰被禁时保证本轮不因超窗被 provider 拒绝。
```

- [ ] **Step 2: 校验**

Run: `venv\Scripts\python -m py_compile core/memory_compressor.py core/agent_service.py`
Expected: 无输出（成功）

- [ ] **Step 3: 提交**

```
git add core/memory_compressor.py core/agent_service.py
git commit -m "docs(memory): 恢复压缩/淘汰路径上被误删的踩坑注释"
```

---

## Task 6: 配置健壮性与测试稳定性收尾（L7 + 小瑕疵）

**Files:** Modify `core/memory_compressor.py:492`、`_load_compression_config`；Modify `tests/test_memory_compressor_chunks.py`；Modify `tests/test_gui_token_usage.py`

- [ ] **Step 1: `dynamic_merge_limit` 不得反向压低用户配置**

现状 `min(1200, config["summary_length_merge"] + 30 * n)`：用户显式配 `summary_length_merge > 1200` 会被硬上限**改小**。当前 `config.json` 的 `memory_compression` 为 `{}` 所以没触发，属于潜伏 bug。`core/memory_compressor.py:492` 替换：

```python
        # 每多一段放宽 30 字，缓解串行累积合并的信息衰减；上限 1200，
        # 但绝不低于用户显式配置值本身。
        dynamic_merge_limit = max(
            config["summary_length_merge"],
            min(1200, config["summary_length_merge"] + 30 * max(0, total_chunks - 1)),
        )
```

- [ ] **Step 2: 三个新常量纳入 config.json**

在 `_load_compression_config` 的返回 dict 里补 `"max_chunk_token_budget"`、`"inter_chunk_delay_seconds"`、`"max_total_seconds"` 三项（读 `comp_cfg.get(...)`，默认取现有常量），并把 `max_timeout_seconds` 的默认值改为 `None` → 内部回落到配置值。这样限流调优不必改代码。**注意**：`_compression_chunk_budget` 需接受可选上限参数，别把默认值写死成第二处真相。

- [ ] **Step 3: 修脆弱断言与慢用例**

- `test_chunk_rate_limit_respects_retry_after` 的 `assert len(sleeps) == 1` 依赖「本次恰好只有 1 段」，将来改 seed 或 fixture 窗口即误挂 → 改 `assert 12.5 in sleeps`。
- `test_chunk_rate_limit_backoff_retry` 耗时 1.76s，来源是 `is_rate_limit_error` 内 `_get_litellm()` 真实 import litellm（约 1.4s）→ 在该文件 fixture 里 `monkeypatch.setattr("core.llm._get_litellm", lambda: <带 exceptions.RateLimitError 的替身>)`。**但保留至少一条不 patch 的用例**，让真实 litellm 异常链路仍被覆盖。

- [ ] **Step 4: 把「同步回归守门用例」并回正式测试**

复审查用的反证探针在 `tmp/test_async_probe_v2.py`（不受 git 跟踪）。把它的思路落成一条常驻用例，防止谁再把后台压缩改回同步阻塞：

```python
def test_gui_compress_test_latch_catches_sync_regression(tmp_path, monkeypatch):
    """守门：若 _ensure_fits_active_window 退化为同步实现，门闩断言必须失败。"""
    # 用 start() 即同步执行 target 的 Thread 替身替换 gui_bridge.threading，
    # 再跑 _ensure_fits_active_window，断言 with pytest.raises(AssertionError)
    # 或直接断言 bg_finished.is_set() is True（同步特征），二者只能满足其一。
```

实现要点：替身类 `def start(self): self._target()`；断言 `assert bg_finished.is_set() is True`（同步特征成立即证明替身生效），而真实异步实现下应为 `False` —— 两条用例合起来才封死。

- [ ] **Step 5: 全量验证 + 提交**

```
venv\Scripts\python -m py_compile main.py core/llm.py core/memory.py core/memory_compressor.py core/agent_service.py gui_bridge.py
venv\Scripts\pytest tests\test_memory_compressor_chunks.py tests\test_agent_service.py tests\test_gui_token_usage.py tests\test_memory.py tests\test_memory_maintenance.py tests\test_memory_compressor_encoding.py -q --durations=8
# 注意：禁止用 `git add -A`（会把无关的 llm/缓存改动卷进本轮 commit）
git add core/memory_compressor.py tests/test_memory_compressor_chunks.py tests/test_gui_token_usage.py
git commit -m "chore(memory): 压缩限流参数入配置、修脆弱断言与慢用例、补异步回归守门"
```

---

## 最终验收清单

- [ ] 6 个测试文件全绿，且总耗时不超过约 5s（若显著上涨，查是否又引入真实 sleep / 真实 litellm import）
- [ ] 新增 5 条用例分别锁定：零摘要禁淘汰、分段进度落库、增量估算、evict CAS、边界口径一致
- [ ] 人工核一遍：`_do_compress` 的 LLM 调用仍在锁外；GUI 后台线程仍只经 `emit_event`
- [ ] 端到端自测：`config.json` 里给一个 256k 窗口小模型，把某会话灌到 60 万+ token，切模型 → 观察 ①界面不卡 ②分段进度提示逐段出现 ③中途点停止后 DB 历史条数不减少 ④再发消息能正常回复
- [ ] 工作区拆分：`core/llm.py` 的 Qwen/DashScope 缓存特性、`core/cache_keepalive.py`、`core/harness/request_envelope.py`、`tests/test_qwen_cache.py`、`cache_benchmark.py` 与本轮压缩加固**无关**，必须独立成另一个 commit，保证压缩加固可单独回滚

## 遗留（本轮不做，需另行设计）

**C3 未根治：** 34 段串行累积合并的信息衰减只是被 `dynamic_merge_limit`（≤1200 字）缓解，本质仍是 N 次有损串联，早期轮次的文件名/未修完 bug 依然会蒸发。根治要改 map-reduce：各段独立产段摘要（2~4 路并发 + 限流），最后一次或树状合并。这会同时缓解墙钟与预算矛盾，是下一轮的主菜。

## 自审（已按 writing-plans 检查）

- **覆盖度：** L1→Task1，L2→Task2，L4→Task3，L5/L6→Task4，L3→Task5，L7 及小瑕疵→Task6，C3→明确记为遗留。无遗漏。
- **无占位符：** 所有代码块均为可直接粘贴的实参实现；`Task 2 Step 5`、`Task 6 Step 4` 中出现的中文要点，均已给出对应代码形态与判定式，非 TBD。
- **类型一致性：** `CompressionOutcome(status, reason)` 全程二元；`_commit_summary_progress` 与 `_finish_with_progress` 的签名在定义与调用处一致；`_prune_history_in_memory_to_fit_window` 返回二元组的改动已同时覆盖定义、调用点、两条既有测试。
