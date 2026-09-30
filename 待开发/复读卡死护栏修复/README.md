# 模型复读卡死护栏修复

- **状态**：待开发（方案已定，未实施）
- **设计日期**：2026-09-30
- **触发来源**：GUI 会话「你帮我去找一下openc」长时间卡死。模型每步只输出填充词（"好。""嗯。""OK。"），既不调用工具也不给结论，气泡持续刷屏、状态栏长期停在"生成中"。
- **涉及模块**：`core/llm.py`（判据与流式装配层、退避重试决策）、`core/llm_error_mapper.py`（错误分类，只读复用）、新增测试 `tests/test_degenerate_output_guard.py`。
- **配套实施文档**：见 [01-实施方案与验收清单.md](./01-实施方案与验收清单.md)。

---

## 一、问题现象

- 会话「你帮我去找一下openc」中，模型连续多步只产出填充词（"好。""嗯。""OK。"），**不调用任何工具，也不给出最终结论**，前端气泡持续刷屏，状态栏长期停留在"生成中"，用户视角等于卡死。
- 同源会话「你帮我去找一下openc(2)」表现相同。
- 日志 `~/.ycode/logs/gui-runtime.log` 记录单步耗时极长：故障会话单步 **96 秒**，同源会话单步 **294 秒**。
- 截图中的 `输入 43,105,547 / 命中 42,917,632 (100%)` 是**会话累计口径**（每轮累加），命中率 100% 只能说明缓存全部命中；`Token 10%` 才是**当前上下文占窗口比例**，并不高。
- **结论：这不是"上下文爆了"。** 累计输入巨大只是长会话的累加结果，与本次卡死无因果。

## 二、根因

### 2.1 判据过宽：填充词被当作有效正文

- `core/llm.py:528` 的 `_is_meaningful_assistant_content` 只剥离空白/标点/Markdown 符号：

  ```python
  def _is_meaningful_assistant_content(text):
      return bool(re.sub(r"[\s`*_#>~\-\[\](){}|.,!?，。！？:：;；/\\]+", "", text or ""))
  ```

  对 `"好。"` 而言，剥掉句号后还剩 `"好"`，非空 ⇒ **判为有效正文**。
- 因此 `core/llm.py:2413` 得到的 `raw_content_meaningful = True`，把"只有填充词"这一步误判成"模型已给出正文"。

### 2.2 三道护栏同时短路

三道护栏全部以"本步无正文（`not raw_content_meaningful`）"为进入前提。由于 2.1 误判 `raw_content_meaningful = True`，它们被**同时短路**，等于对一个只输出填充词的回合集体失效：

| 护栏 | 位置 | 触发条件 | 为什么失效 |
| --- | --- | --- | --- |
| 慢步熔断 | `core/llm.py:2453-2457`（阈值 `core/llm_execution.py:270` `SLOW_STEP_ABORT_SECONDS = 60.0`，上限 `core/llm_execution.py:272` `SLOW_STEP_MAX_NUDGES = 1`） | `duration_s > SLOW_STEP_ABORT_SECONDS and not raw_content_meaningful and not tool_calls_list` | 条件含 `not raw_content_meaningful`；填充词被判为有正文 ⇒ 永不进入，96/294 秒慢步也不会熔断 |
| 最终总结催促 | `core/llm.py:2542-2546`（重试上限 `core/llm.py:1260` `FINAL_SUMMARY_MAX_RETRIES = 2`） | `not tool_calls_list and not raw_content_meaningful and (tool_activity_seen or promoted_reasoning)` | 同样含 `not raw_content_meaningful` ⇒ 永不触发，不会催它给结论 |
| 收尾兜底报错 | `core/llm.py:2757` | `if not raw_content_meaningful:` 才发 error/notice | 填充词被当作有正文 ⇒ 不报错，回合"安静地"结束在无意义填充词上 |

### 2.3 残留护栏不读正文

唯一还在生效的是 `core/harness/progress_guard.py`（`MAX_STALLED_STEPS = 12`、`MAX_UNIQUE_READS = 48`）。但它**只比较确定性状态**：任务/计划快照哈希、工具结果哈希、修改提交标记、只读工具读取签名。文件头注释即写明：

> "确定性 Turn 进展观察：只比较状态哈希与修改类提交标记，**不读模型自然语言**。"

填充词步骤不产生任何确定性状态变化，因此：

- 必须**连续 12 步无进展**才会触发 `should_abort()`；
- 期间只要模型偶发一次工具调用、或读取一个新文件（`MAX_UNIQUE_READS = 48` 预算内），停滞计数就被**重置**，熔断永远追不上"边刷填充词边偶尔摸一下工具"的模式。

### 2.4 无退化（复读）检测

在 `core/` 下搜索 `重复` / `repeat` / `repetition` / `degener` / `填充词`，**不存在任何**针对"连续相同或近似正文"的检测或熔断逻辑。系统对"模型复读"这一类退化没有专门的护栏，只能等 2.3 的通用停滞计数，而它恰恰对填充词不敏感。

### 2.5 环境诱因与历史同源问题

- **环境诱因（旁证）**：同期自建网关 `http://129.204.50.227` 密集返回 `402 Payment Required` 与 `429`（日志时间点 22:02:04、22:02:44、22:03:01、22:03:07、22:03:37、22:04:01 …）。当前额度/鉴权类错误会进入"指数退避重试 ×5"（`core/llm.py:1761` `max_api_retries = 5`），**属确定性失败却仍在重试**，徒增单步耗时。`core/llm_error_mapper.py:107` 已能把 402 归类为 `insufficient_quota`，但**该分类未传导到重试决策**，重试逻辑仍一视同仁。
- **历史同源**：`tests/test_gui_protocol_sanitise.py:3-13` 记载：上游网关会把空 assistant 正文替换为占位符 `[System: Empty message content sanitised to satisfy protocol]`，该标记进入历史后会**诱导模型复读**。现有修复只做"清理"——`core/turn_renderer.py:77-91` 的 `sanitize_protocol_text`（占位符常量定义于 `:72-74`）、`core/llm_dialects.py:1331-1333` 出站置空——**没有任何复读检测**。这与本次"填充词复读"是同一类失效的两种表现。

## 三、修复项

### 修复①（P0，最小改动）：强化"有效正文"判据

**要点**

- 在 `core/llm.py` 新增模块级 `_FILLER_TOKENS` 集合，首版内容：
  `好 / 好的 / 嗯 / 哦 / 噢 / ok / okay / 行 / 可以 / 收到 / 明白 / 了解 / 继续 / 输出 / 完成 / 完毕 / yes / no`，**比对前统一小写化**。
- 新增辅助函数 `_is_filler_only(text)`：先剥离空白与标点；若剩余部分**只由填充词构成**则返回真。**建议实现口径**：按标点切分后**所有片段均命中词表**，且整体剩余长度 **≤6 字符**。
- `_is_meaningful_assistant_content` 改为：**原判据为真 且 `not _is_filler_only(text)`**。

**边界**

- 只命中"剥完只剩填充词"的情形：`"好的，已完成"`、`"OK，我来处理"` 这类含实义短句的正文**仍算有效正文**，不得误判。
- **不改函数名与签名**（红线）。
- **词表宁可漏判，不可误判**：拿不准的词不进表。

### 修复②（P0）：流式装配层增加退化检测

**要点**

- 在流式装配层维护**最近 3 步**的正文指纹与思考指纹（各自去掉标点、空白后的字符串）。
- 判定退化：**连续 3 步相同**，且**单步长度 ≤20 字符**（或两两相似度 **≥0.9**）。
- **首次命中**：复用 `SLOW_STEP_NUDGE_TEXT`（`core/llm.py:1086`）**同一通道**追加强催促，并 `_notify_event` 发 `notice` 事件。
- **二次命中**：发 `error` 事件，并**走慢步熔断同一收尾路径**（`core/llm.py:2483-2496`：结束循环、返回已产出内容、补状态机迁移与 harness 收尾）。

**边界**

- **必须发事件让用户可见，不得静默收尾**（红线）。
- 计数**随回合生命周期重置**（与 `slow_step_nudge_count` 同生命周期）。

### 修复③（P1）：额度/鉴权类错误 fail-fast

**要点**

- 在退避重试决策处（`core/llm.py:2099` 分支入口）调用 `core.llm_error_mapper.map_llm_error(exc).category`。
- **直接失败、不重试**的类别：`insufficient_quota`、`auth_invalid`、`permission_denied`、`model_not_found`（四类均为确定性失败，重试无意义）。
- **保留现有退避行为**的瞬时类别：`rate_limit`、`service_overloaded`、`gateway_error`、`network_error` 等。

**边界**

- 复用 `core/llm_error_mapper`（`LLMErrorInfo.category` 字段已存在：`core/llm_error_mapper.py:19-23`；上表四类分别在 `:125` / `:148` / `:170` / `:191`，瞬时类分别在 `:237` / `:259` / `:279` / `:332`）。
- 不改动既有 429 退避时长口径（`core/llm.py:2102-2112`）。

## 四、红线与不做的事

- **不改** `_is_meaningful_assistant_content` 的函数名与签名。
- **不得改动 prompt 稳定字节**：`test_prompt_byte_stability.py` 的 sha256 golden 守着缓存命中率，任何拼接顺序/文案改动都不得触碰稳定前缀。
- **不引入第三方依赖**。
- **不做通用"内容质量"判断**：只针对"填充词 / 连续退化"，不做语义质量打分。
- **不改动 `progress_guard.py` 的判据**：不读正文是**有意设计**（确定性、可回放），不得因本次修复让它去读自然语言。
- **每项独立可验收、独立可回滚**（① / ② / ③ 互不依赖）。

## 五、文件说明

- 本文件：`待开发/复读卡死护栏修复/README.md`——问题、根因与修复总览。
- [01-实施方案与验收清单.md](./01-实施方案与验收清单.md)——改动锚点、任务拆解、测试用例设计、验收场景与回归红线，可直接交给实施者执行。

## 六、遗留待证项

- 故障会话的**消息级明细未取到**：数据在 `~/.ycode/ycode_gui.db`，`messages` 表仅 `id / session_id / role / content` 四列，正文与 `tool_calls` 混在 `content` 的 JSON 内。
- 该项**不影响修复 ① ② 的必要性**（判据过宽与无退化检测本身即可独立成立），但**实施前建议补一次查询**，确认诱因是否与 `[System: Empty message content sanitised to satisfy protocol]` 占位符残留相关（若相关，可进一步收窄词表与指纹口径）。
