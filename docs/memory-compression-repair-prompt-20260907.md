# 明日开工提示词（直接整段复制给 AI 搭档）

> 用法：把下面「提示词正文」整段粘进新会话即可。文档路径已写死在本仓库，无需再解释背景。

---

## 提示词正文

请按 `docs/memory-compression-repair-20260907.md` 逐条执行修复。这是上一轮「大窗口切小窗口时记忆压缩 429 / 界面卡死 / 滑窗淘汰丢历史」加固之后复查发现的新问题，编号 L1–L7，文档里每个 Task 都已写好精确定位、失败测试代码、最小实现代码和验收命令。

> ⏭️ **基线已固化**：2026-09-07 已切成 3 个 commit 推送 `origin/dev`（HEAD `f5aa0a4`），工作区干净。
> 文档里的 **Step 0 已完成，直接跳过，从 Task 1 开始**。

**执行要求：**

1. 严格按 Task 1 → 6 顺序做，依赖关系是真实的（Task 2 依赖 Task 1 的 `compress_unprotected` 语义，Task 4 的交叉测试依赖 Task 3 的口径改动）。
2. 每个 Task 走完整 TDD 循环：先照抄文档里的失败测试 → 跑一遍确认它以预期的方式失败 → 再写最小实现 → 跑到通过 → 单独 commit。测试没失败过就不要写实现。
3. 行号可能有漂移，以文档里的**代码片段和函数名**为准定位；定位不到先用 `read_file(with_line_numbers=True)` 重新确认，不要凭记忆改。
4. 任何一步测试未按预期失败或通过，先分析根因再说下一步，禁止「改断言迁就实现」。如果确认是文档里的方案有问题，停下来告诉我，不要自行绕。

**硬约束（违反即算失败）：**

- 请求路径绝不改写历史中间消息的字节；改写与水位推进只在 DB 层一次性完成。内存裁剪只能整轮剔除、只作用于本次待发副本。
- `_do_compress` 的 LLM 调用必须留在 `manager.acquire_lock()` 之外，只有读写阶段持锁。
- `compress_session_memory` 的默认返回值仍是 bool，三态只在 `return_outcome=True` 时启用 —— CLI 和既有测试都依赖 bool。
- GUI 后台线程只能走 `emit_event`，绝不直接碰 JS 桥（AGENTS.md:170-172）。
- 测试里禁止真实网络、真实长 sleep、真实 litellm import。
- 内存裁剪与 `evict_oldest_turn_persistent` 的轮次边界口径必须一致，Task 4 的交叉测试就是用来锁死这一点的。

**明确禁止扩大范围：**

- 不要碰 `core/llm.py` 里 Qwen/DashScope 缓存那批改动（`is_rate_limit_error` 除外，它是本轮依赖），不要碰 `core/cache_keepalive.py`、`core/harness/request_envelope.py`、`tests/test_qwen_cache.py`、`cache_benchmark.py`。它们与本任务无关，要留在独立 commit 里。
- 不要顺手重构、不要改命名风格、不要动无关的格式。文档 Task 5 只要求恢复注释，别捎带别的。

**每个 Task 结束后跑：**

```
venv\Scripts\python -m py_compile core/memory_compressor.py core/agent_service.py core/memory.py
venv\Scripts\pytest tests\test_memory_compressor_chunks.py tests\test_agent_service.py tests\test_gui_token_usage.py tests\test_memory.py tests\test_memory_maintenance.py tests\test_memory_compressor_encoding.py -q --durations=8
```

**全部做完后，向我汇报：**

- 每个 Task 的实际改动文件与 commit message；
- 最终测试通过数与总耗时（开工基线是 88 passed / 3.76s，请给出对比）；
- Task 2 的量化验证：按 `_compression_request_timeout(24000)=106s` 与 34 段的算术，说明分段提交后「中止时最多损失几段」；
- 哪些地方和文档方案做了偏离，以及偏离原因；
- C3（34 段串行合并的摘要语义衰减）仍属遗留，不要在这一轮顺手改 —— 但可以给出你对 map-reduce 分段方案的具体设计（并发度、合并树深度、限流如何配合 TPM）。

---

## 附：一句话版（赶时间时用）

按 `docs/memory-compression-repair-20260907.md` 的 Task 1-6 逐条 TDD 修复记忆压缩的 L1-L7，硬约束是「DB 层一次性改写、LLM 调用在锁外、`compress_session_memory` 默认仍返回 bool、测试不碰真实网络与真实 sleep」，不要碰工作区里 Qwen 缓存那批无关改动，做完给我 88→? 的测试对比和偏离说明。
