# DeepSeek 缓存命中率提升设计

- 日期：2026-09-23
- 状态：设计已评审（用户选定方案 B），待实施
- 目标读者：接手实施的 agent（无需查阅前置对话）

---

## 1. 背景与目标

### 1.1 问题

用户反馈 DeepSeek 家族模型的 prompt 缓存命中率「最近到不了 99%」。对 `~/.ycode/ycode.db`
的 `token_usage` 表统计（口径：model 以 `deepseek` 开头、不分大小写，命中率 =
Σcached_tokens / Σprompt_tokens）：

| 范围 | 命中率 | 样本 |
|---|---|---|
| 总体（2026-08-05 ~ 09-17） | **88.44%** | 661 条 |
| 近 30 天 | 93.93% | 83 条 |
| 按模型：deepseek-v4-flash-0731 | 88.12% | 578 条（主力） |
| 按模型：deepseek-v4-pro-0813 | 94.28% | 67 条 |
| 按模型：deepseek-flash | 91.73% | 16 条 |

单条记录分布（prompt>0）：57.0% ≥99%，28.7% 在 90~99%，5.3% 在 50~90%，
7.7% <50%，1.2% 冷启动（cached=0）。**低命中记录都是大 prompt**——它们把加权
均值从 99% 拽到 88%。

**按 api_base 拆分（决定性发现）：**

| api_base | 记录 | prompt | 命中率 |
|---|---|---|---|
| `https://tokenrhythm.studio/v1` | 435 | 44.1M（71%） | 88.35% |
| （空，旧记录未写 api_base） | 226 | 18.3M | 88.68% |

用户的 DeepSeek 流量约 7 成走 **TokenRhythm（基元律动）网关**，而非官方
`api.deepseek.com`。

### 1.2 目标

- 热路径（剔除冷启动/窗口轮换/工具契约变更轮）命中率：**≥97~99%**
- 含一切的总体命中率：**≥93~96%**（物理上限，见 1.3）
- 统计口径不变：`token_usage` 表 deepseek 开头模型（不分大小写）的
  Σcached/Σprompt

### 1.3 物理约束（诚实预期）

以下场景必然全量 miss，任何实现都消不掉：

1. 新会话/新账本代的首个请求（冷启动）；
2. 1M 上下文窗口超限后的账本轮换（主动轮换已有，仍是一次全价重建）；
3. 服务端缓存 TTL 驱逐（App 关闭过久后恢复，客户端只能保活无法杜绝）；
4. 每轮真实新增内容（用户消息、工具结果）——这是 90~99% 区间带的主因，属正常。

因此「总体恒 99%」不可达，本设计以 1.2 的分档口径验收。

---

## 2. 调研结论（ZCode / DeepSeek Harness 借鉴点）

调研对象已浅克隆到本机：`~/.ycode/tmp/ZCode`、`~/.ycode/tmp/deepseek-harness`。
结论提炼（细节见当回合对话，此处只留可执行要点）：

| 手段 | ZCode | DeepSeek Harness | 我们的现状 |
|---|---|---|---|
| 稳定/动态内容分离 | ✅ stable/dynamic 分块 | ✅ 一切易变内容追加到历史尾部 | ✅ 已有（动态块尾部内联、append-only） |
| 显式 cache_control 断点 | ✅ Anthropic 协议 4 断点 | ❌ 刻意不用（词汇表剪除） | ✅ 仅 anthropic/ 前缀模型 |
| append-only 不可变请求账本 | 部分（消息侧滚动断点） | ✅ session log 即唯一事实源 | ✅ **已有 Prompt Ledger，但仅官方路由** |
| 系统提示词变化处理 | 失效尾部小块 | in-history 追加（前缀不动） | system 头变化 → 账本强制轮换 |
| 工具列表字节稳定 | 注册表生成 + hash | 确定性投影 | ✅ 官方/Qwen 路由字母序规范化 |
| 缓存意识制度化 | — | 每个插件 README 必写 KV Cache effect | 无 |

核心判断：**我们的架构已具备 DSH 同款骨架（Prompt Ledger），差距只在覆盖范围
与少数前缀破坏点**，不需要引入 Anthropic 协议切换或显式断点扩展。

---

## 3. 现状盘点（代码事实，含位置）

### 3.1 已有机制（不要重复造）

1. **DeepSeek Prompt Ledger**（不可变账本）
   - 表：`deepseek_prompt_generations` / `deepseek_prompt_messages`
     （`core/memory.py:442-464`）；CRUD：`get_or_create_deepseek_prompt_generation`
     （约 `core/memory.py:990-1030`）、`append_deepseek_prompt_messages`（约 1033 行）。
   - 请求构建：`_prepare_deepseek_ledger_request`（`core/llm_dialects.py:477` 附近）：
     稳定头（tools + 首条 system）比对失败或追加边界不唯一 → `force_new` 换代。
   - 启用条件：`_uses_deepseek_prompt_ledger`（`core/llm_dialects.py:387`）=
     persist ∧ parent_session ∧ cache_scope==main ∧ `is_deepseek_ledger_route`。
   - 路由判定：`is_deepseek_ledger_route`（`core/llm_dialects.py:377`）=
     `_is_deepseek_family` ∧ `_is_official_deepseek_route`；
     **`_is_official_deepseek_route`（`core/llm_dialects.py:77`）只认
     `https://api.deepseek.com` 三个拼法 → tokenrhythm.studio 被排除**。
   - 压缩免疫：压缩只改写 DB 工作副本，账本字节不变；窗口仍超限则由
     `mark_deepseek_generation_rotation`（`core/agent_service.py:969` 附近调用）
     登记主动轮换。
2. **tools 字母序规范化**：`_select_request_tools_for_route`
   （`core/llm_dialects.py:397` 附近），仅覆盖官方 DeepSeek / Qwen 缓存路由。
3. **稳定/动态提示词分离**：`core/context_builder.py`
   （`build_stable_prompt` / `build_dynamic_prompt` / `build_time_note`）；
   动态块与时间注记内联到最后一条 user 消息尾部，append-only。
4. **记忆索引恒定化**：`core/memory_kairos.py:192` `build_memory_system_section`
   的索引段为恒定占位文案，写记忆已不影响前缀。
5. **MCP 首等**：`core/llm.py:265 get_tools_schema` 进程内首次调用同步等 MCP
   就绪，避免 tools 第 N 轮突然出现。
6. **plan 模式不裁剪 tools**：`core/llm.py:1367` 附近注释明确（约束由执行层
   plan_enforced 保证）。
7. **显式断点**：`_apply_cache_control`（`core/llm_dialects.py:704`），
   `_CACHE_CONTROL_PROVIDERS = ("anthropic",)`（`core/llm_dialects.py:614`）。
8. **诊断表（空转）**：`cache_request_diagnostics`（`core/memory.py:511` 附近建表，
   含 request_reason / messages_sha256 / system_sha256 / tools_sha256 /
   common_prefix_messages 等），**当前 deepseek 家族零记录**。

### 3.2 剩余失分点（按预期影响排序）

1. **tokenrhythm 网关流量无账本、无 tools 规范化**（3.1-1/2 的判定排除）：
   压缩 → 历史重写 → 前缀全灭；MCP 重连/增减 → tools 顺序/集合抖动 → 全灭。
   这是 88% 与 99% 之间的最大差额来源。
2. **system 头中途变化 → 账本强制轮换**：稳定头由
   `build_stable_system_message`（`core/agent_service.py:230`）每轮重建，内含
   人设块 / 技能目录 / 子 Agent 清单（`build_stable_system_extra_sections`，
   `core/agent_service.py:179`）。会话中途用户改设置/切技能/改子 Agent →
   `_deepseek_stable_header_matches` 失败 → `force_new` → 全量 miss 一次。
3. **轮换与 miss 原因不可观测**：换代分支不记录原因，无法归因验证。

---

## 4. 设计方案（用户已选定：方案 B 直接结构改造）

### 4.1 改造项 ①：缓存优化路由可配置化（核心，约八成收益）

**做法**：新增 config.json 顶层键 `deepseek_cache_routes`（字符串数组，域名/子串
条目），把网关路由纳入 DeepSeek 缓存优化家族。

- 在 `core/llm_dialects.py` 新增：
  - `_DEEPSEEK_CACHE_ROUTES_CONFIG_KEY = "deepseek_cache_routes"`；
  - `_configured_deepseek_cache_routes()`：**直接复用
    `_configured_keepalive_routes` 的既有模式**（`_parse_keepalive_routes` 解析 +
    `_qwen_cache_config_stamp` 指纹缓存，见 `core/llm_dialects.py:297-340` 附近），
    解析逻辑抽到共用辅助函数或按同模式仿写；
  - `_is_cache_optimized_deepseek_route(api_base)` =
    `_is_official_deepseek_route(api_base)` ∨ 主机名命中任一配置条目
    （归一化用现有 `_normalize_route_match_key`，`core/llm_dialects.py:283`）。
- 切换两个调用点（其余判定不动）：
  1. `is_deepseek_ledger_route`（`core/llm_dialects.py:377`）的官方路由条件 →
     换 `_is_cache_optimized_deepseek_route`；
  2. `_select_request_tools_for_route`（`core/llm_dialects.py:397` 附近）的
     canonicalize 条件中 `_is_official_deepseek_route(api_base)` →
     换 `_is_cache_optimized_deepseek_route(api_base)`。
- 用户侧配置（运维动作，不写代码默认值）：config.json 增加
  `"deepseek_cache_routes": ["tokenrhythm.studio"]`。

**效果**：tokenrhythm 流量获得 append-only 账本（压缩不再炸缓存——方案 B 中
「compaction 保前缀」由本项直接覆盖，无需另改压缩器）+ tools 字母序
（MCP 抖动免疫）。

**注意**：`_is_deepseek_family`（模型名含 deepseek）保持不变；非 DeepSeek 上游的
网关不要加进配置（账本语义只对 DeepSeek 磁盘缓存上游有意义）。

### 4.2 改造项 ②：目录变更不炸代（会话级目录快照）

**问题**：`build_stable_system_extra_sections`（`core/agent_service.py:179`）每轮
现取人设/技能目录/子 Agent 清单，会话中途变化 → 账本稳定头失配 → 强制换代。

**做法**：对「注入点 2」的输出做**会话级快照**：

- 会话首个请求时渲染一次人设块 + 技能目录 + 子 Agent 清单（记忆段已是恒定
  索引，包含在内无妨），按 `session_name` 缓存到进程内字典；
- 该会话后续请求一律复用快照字节；会话删除/重命名时清快照；
- 进程重启后恢复快照不存在 → 首请求重新渲染（若文件无变化，渲染结果逐字节
  相同，天然不炸代；有变化则只换代一次，符合预期）。
- 设置页（技能/子 Agent/人设的保存入口）加提示文案：「已打开的会话将在
  新会话中生效」。

**实现锚点**：`build_stable_system_message`（`core/agent_service.py:230`）目前
无 session 参数，需在请求组装链路上传入 session_name（chat_with_model_events
方向），快照存取放在 agent_service 层；Token 估算路径
（`estimate_history_tokens_with_system`）可继续用无快照的现值（估算口径允许偏差，
不影响线上字节）。

**风险**：会话内改技能/子 Agent 不立即生效，用户可能困惑 → 用提示文案缓解；
快照只进内存，不落库。

### 4.3 改造项 ③：轮换与 miss 可观测（验收的必要手段）

- 在 `_prepare_deepseek_ledger_request` 的各 `force_new` 分支记录原因：
  `first_generation` / `pending_rotation` / `header_tools_changed` /
  `header_system_changed` / `boundary_ambiguous`；
  向 `cache_request_diagnostics` 写一行（request_reason 用上述值，
  tools_sha256 / system_sha256 能算则算，其余字段按现有 schema 可空）。
- 排查 `cache_request_diagnostics` 现有的写入点（实现时先定位其发射开关），
  把 deepseek 家族路由纳入——当前该表 deepseek 零记录，改完要能直接看到
  miss 归因。
- 复用统计脚本 `~/.ycode/tmp/deepseek_hitrate.py`（按模型/按日命中率）与
  `~/.ycode/tmp/deepseek_routes.py`（按 api_base 拆分）做改造前后对比。

### 4.4 非目标（明确不做）

- 不切 Anthropic 协议、不给 OpenAI 协议路由加显式 cache_control
  （DeepSeek 服务端自动前缀缓存已生效，DSH 同样刻意不用显式断点）；
- 不改压缩算法（账本已对压缩免疫）；
- 不改保活机制（per-session 保活隔离已修过）；
- 不动 `token_usage` 统计口径与历史数据。

---

## 5. 风险与缓解

| 风险 | 缓解 |
|---|---|
| tokenrhythm 上游未必是官方 DeepSeek 磁盘缓存，账本收益不确定 | 现状 88% 证明服务端确有前缀缓存行为；append-only 对任何前缀缓存上游只增不减；改造前后用 4.3 的脚本实测验证 |
| 目录快照导致会话内设置变更不生效，用户困惑 | 设置页提示文案；快照仅内存，重启即新值 |
| 配置条目误加非 DeepSeek 上游网关 | 文档（本文件 + config 注释）写明「仅 DeepSeek 磁盘缓存上游」 |
| 空 api_base 的 legacy 记录（约 1/3）无法回溯归因 | 不管，只看改造后新数据 |

---

## 6. 验证方案

1. **单元测试**（新增/更新）：
   - 配置条目使 `is_deepseek_ledger_route` 对网关怀里通过（含大小写/端口/路径归一化）；
   - `_select_request_tools_for_route` 对配置路由输出字母序 tools；
   - 目录快照：会话中途变更技能目录 → system 头字节不变 → 不触发换代；
   - 换代分支写出 rotation 原因诊断行。
2. **实测基准**：`bench/adapt_bench.py` 以 tokenrhythm 的 deepseek 模型跑
   缓存命中率场景，改造前后各一遍对比 hot_rate（参考既有报告
   `docs/adapt-report-deepseek-v4-flash.md` 的格式留档）。
3. **线上观测**：改造生效后 3~7 天，用 4.3 脚本盯 `token_usage` 按日命中率与
   `cache_request_diagnostics` 的 miss 归因，对照 1.2 目标验收。

## 7. 接手指引（给实施 agent）

1. 先读 `core/llm_dialects.py` 的 ① 路由判定区（60-110 行）与账本地区
   （370-560 行）、`core/agent_service.py` 的稳定头组装（179-260 行）；
2. 按 4.1 → 4.2 → 4.3 顺序实施，每项配单测；
3. 写实施计划请使用 writing-plans 技能；TDD 请使用 test-driven-development 技能；
4. 完成后用 `bench/adapt_bench.py` 实测并更新本文件的验证结论。

---

## 8. 实施结论（2026-09-23 回填）

### 8.1 三项改造落地情况

| 改造项 | 落点 | 状态 |
|---|---|---|
| ① 缓存优化路由可配置化 | `core/llm_dialects.py` `_is_cache_optimized_deepseek_route` + `_configured_deepseek_cache_routes` | 已实施 |
| ② 会话级目录快照 | `core/agent_service.py` `build_stable_system_extra_sections(session_name)` / `_SESSION_STABLE_SECTIONS` | 已实施 |
| ③ 轮换可观测 | `_prepare_deepseek_ledger_request` → `rotation_reason` → `core/llm.py` 覆写 `cache_request_diagnostics.request_reason` | 已实施 |

提交：`5dd359d`（实施计划）、`c752110`（三项改造 + 测试），本文件与实测报告的回填随其后的
文档提交入库。实施计划见 `docs/superpowers/plans/2026-09-23-deepseek-cache-hit-rate.md`。

### 8.2 与设计稿的事实修正

1. §3.1-8「`cache_request_diagnostics` 当前 deepseek 家族零记录」不成立：实测该表共
   1087 行，其中 deepseek 家族 435 行、`api_base` 全部为 `https://tokenrhythm.studio/v1`。
   该表的写入路径本就**没有路由门禁**，③ 无需新增「发射开关」；实际缺口只是换代原因
   没有落到 `request_reason`。
2. §4.3 的换代原因取值域需补第 6 个 `caller_force_new`（`core/llm.py` 超窗压缩成功后的
   重试分支主动传 `force_new=True`），否则该分支会被误标为 `pending_rotation`。
3. §4.2 未说明的一处语义细节：③ 的原因覆写对**所有**换代都生效（含首代
   `first_generation`），因此账本路由的首个请求在 `request_reason` 上由「冷启动」变为
   `first_generation`。二者在 `query_cache_diagnostic_summary` 的 `explicit_categories`
   中同归 `cold_start`，且都属「非热启动」，统计口径不变。
4. `bench/adapt_bench.py` 绕开 Prompt Ledger（第 41 行直接 `from core.llm import completion`
   并以 `stream=False` 调用），**不能度量本次改造收益**，只能测上游网关自身上限；
   实测其运行间抖动达 ±12 个百分点。详见
   `docs/adapt-report-deepseek-tokenrhythm-cache.md` §3.3/§4。

### 8.3 验证证据

| 验收项 | 命令 | 结果 |
|---|---|---|
| 配置路由进账本 / tools 排序 | `pytest tests/test_deepseek_prompt_ledger.py -k configured_deepseek_cache_route -q` | `2 passed, 17 deselected in 1.78s`（第三个用例名不含该子串，见下行全文件） |
| 账本测试全文件 | `pytest tests/test_deepseek_prompt_ledger.py -q` | `19 passed`（原 16 + 新增 3） |
| 目录快照不换代 | `pytest tests/test_session_stable_snapshot.py -q` | `3 passed in 1.95s` |
| 换代原因诊断 | `pytest tests/test_deepseek_prompt_ledger.py -k rotation_reason -q` | `3 passed, 16 deselected in 2.41s` |
| 相关测试全量（7 文件） | `pytest tests/test_deepseek_prompt_ledger.py tests/test_session_stable_snapshot.py tests/test_prompt_byte_stability.py tests/test_context_injection.py tests/test_qwen_cache.py tests/test_cache_diagnostics_storage.py tests/test_agent_service.py -q` | `123 passed in 25.45s` |
| 受改动影响测试集 | 69 个测试文件（正则圈出所有引用 `agent_service` / `llm(_dialects)` / `context_injection` / `llm_diagnostics` / `memory` / `deepseek` / cache 诊断的测试），`pytest -q --junitxml=…` | `tests=1547 failures=1 errors=0`（耗时 617s）；唯一失败为既有性能型用例，见下条 |
| 该唯一失败的归属 | `pytest tests/test_memory.py::test_delete_session_stays_fast_with_many_harness_events`，并用基线版 `core/memory.py`（`git show 419f14e:core/memory.py`）交叉复跑 | 改动后 356 ms / 基线版 553 ms，**两侧都超 350 ms 阈值** → 机器性能型既有失败，非本次回归（该用例断言的是会话删除耗时，与本次三处改动无交集） |
| 字节 golden 未破坏 | `pytest tests/test_prompt_byte_stability.py -q` | `5 passed in 1.77s`，golden `ef286e51…` 未变 |
| 前端技能页 | `npx vitest run src/components/Subpage.skills.test.ts` | `Tests 9 passed (9)` |
| 语法检查 | `py_compile.compile(..., doraise=True)` 覆盖 6 个 core 文件 + 2 个测试文件（进程内执行，见备注） | `compiled 8 / 8 OK`，零 SyntaxError |
| 网关实测 | `bench/adapt_bench.py`（before=419f14e / after=c752110） | 总体命中率 before `76.66%` / after `64.91%`；差异为上游抖动，见 `docs/adapt-report-deepseek-tokenrhythm-cache.md` §3.3 |
| 线上历史基线 | `~/.ycode/tmp/deepseek_hitrate.py` / `deepseek_routes.py` | 661 条 88.44%；按 api_base 拆 `tokenrhythm.studio/v1` 435 条 88.35%（2026-08-05~09-17） |

> 备注（本机环境）：本沙箱会拦截子进程的批量删除并改写退出码（`3221226505` / `0xC0000409`），
> 因此 `python -m py_compile` 与 `pytest` 的**进程退出码不可信**——验收一律以进程内编译结果
> 或 `--junitxml` 产物为准。两点实操经验：
> 1. `--basetemp` **不要**指向工作区内的 `mytemp/`：部分用例会在 tmp 目录内写配置再改名，
>    沙箱会以 `PermissionError: [WinError 5]` 拒绝，制造约 45 个**假失败**（换成默认临时目录后
>    `test_config.py` + `test_gui_model_settings.py` 复跑 `56 passed` 即证）；
> 2. pytest 收尾清理临时目录会撞「safe-delete 批量删除守卫」，终端汇总行可能丢失，故用
>    `--junitxml` 落盘再解析，比读 stdout 可靠。
>
> 同一原因导致沙箱内 `ripgrep` 不可用，`tests/test_agent_turn_e2e.py` 中以 `search_code` /
> `glob_files` 为前置的用例会因 `rg 错误 (rc=3221226505)` 而失败，属环境限制、与本改造无关。
>
> **全量回归（`pytest -q` 全仓）在本机未能完成**：受浏览器/电脑操控类用例拖累，实测 30 分钟
> 仍未跑完且中途被沙箱删除守卫终止。上表的「受改动影响测试集」（69 文件 / 1547 用例）是本次
> 可交付的最强回归证据；仓库全量回归的完整数字需在无沙箱干扰的环境补跑。
>
> **合并后复验**：本改造落地期间，仓库内另有会话并行提交了「提示词工程与信源防护升级」
> T1~T5（触及 `core/agent_service.py`、`core/memory.py`、`core/context_builder.py`、
> 新增 `core/injection_markers.py`）。在其提交入库后的当前工作区上重跑上表「相关测试全量
> （7 文件）」→ `123 passed, 0 failed`（13.84s），字节 golden 仍为 `ef286e51…`，两侧改动共存无冲突。

### 8.4 上线动作（运维）

config.json 增加：`"deepseek_cache_routes": ["tokenrhythm.studio"]`（本次交付未写入用户配置，
由使用方手工添加）。生效后 3~7 天用 `~/.ycode/tmp/deepseek_hitrate.py`（按模型/按日命中率）
与 `~/.ycode/tmp/deepseek_routes.py`（按 api_base 拆分）对照 §1.2 目标验收；低命中事件按
`cache_request_diagnostics.request_reason` 的换代原因分布归因（`first_generation` /
`pending_rotation` / `caller_force_new` / `header_tools_changed` / `header_system_changed` /
`boundary_ambiguous`）。
