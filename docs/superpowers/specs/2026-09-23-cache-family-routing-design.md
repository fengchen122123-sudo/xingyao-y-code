# 缓存优化家族化判定设计（按模型家族自动放行，去除渠道依赖）

- 日期：2026-09-23
- 状态：设计已评审（用户批准方向），待实施
- 目标读者：接手实施的 agent（无需查阅前置对话）
- 前置文档：`docs/superpowers/specs/2026-09-23-deepseek-cache-hit-rate-design.md`（渠道可配置化）。
  **本设计将其「渠道白名单」整层去掉**：该文档 §8.4 的手工配置动作（config.json 增加
  `deepseek_cache_routes`）自本设计落地后不再需要，请勿继续执行。

---

## 1. 背景与目标

### 1.1 问题

缓存优化体系现有适用判定为「模型家族 ∧ 渠道白名单」双条件：官方路由天然放行；第三方渠道
（tokenrhythm、阿里云 token-plan、其它中转）需显式配置（`deepseek_cache_routes` 等）才放行。

用户在多渠道（同日实测：tokenrhythm 与阿里云 token-plan 并存）使用同一模型时，切换渠道后
缓存优化自动断档：

| 渠道（deepseek 流量） | 记录数 | 命中率 | 现有保护 / 缺口（2026-09-23 实测判定） |
|---|---|---|---|
| tokenrhythm.studio | 12571 | 96.82% | 保活已开（白名单显式）；缺：账本、工具排序 |
| api.deepseek.com | 1389 | 97.92% | 全量（官方）；无缺口 |
| 阿里云 token-plan（当日启用） | 157 | 80.59% | 工具排序/保活/断点/会话缓存头已开（Qwen 通道的官方域名兜底）；缺：账本 |

> 注：阿里云行的「已开」是 API 域名（maas.aliyuncs.com）被识别为百炼官方端点所致的偶然兜底，
> 对任意中转不成立；各家族的核心机制（如 deepseek 账本）仍受渠道门禁——这正是本设计要统一抹平的差异。

同模型换渠道不应改变缓存优化待遇。用户决策（2026-09-23）：**判定只看模型家族，不看渠道**——
deepseek 模型到哪个渠道都走 deepseek 缓存渠道，kimi / glm / qwen 同理。

### 1.2 目标

- 四族判定去渠道化：官方判定结果保持完全等价（改动前后官方一律放行），非官方渠道从
  「需配置」变为「自动放行」：
  - **deepseek** → 账本（Prompt Ledger）+ 工具字母序 + 保活
  - **kimi** → 会话亲和键 + 保活
  - **glm** → 保活
  - **qwen** → 缓存断点（cache_control）+ 会话缓存头 + 工具字母序 + 保活
- **机制行为本身不变**：怎么发（字段、标记、心跳构造）不改，只改「要不要发」的条件。
- 保活心跳一并全自动（用户选定）。对无缓存渠道的持续小额成本，由下述兜底与既有可关通道控制。

### 1.3 非目标（明确不做）

- 不动非缓存类判定：thinking 字段（`_deepseek_thinking_enabled` 的官方守卫）、
  `_supports_reasoning_fields`、max_tokens 预算（`_builtin_default_max_tokens`）、
  推理档位（`_apply_reasoning_choice`）等全部保持现状。
- 不改机制实现：账本结构、保活请求构造（max_tokens=1 等）、cache_control 标记策略、
  `_apply_cache_control` 协议逻辑。
- 不新增开关；不删配置键（保留解析兼容，见 3.5）。
- 不改压缩算法、不动 token 统计口径与历史数据。

---

## 2. 现状盘点（函数级，2026-09-23 核实的代码事实）

### 2.1 判定点清单（本设计仅动「缓存优化」链路）

| # | 位置（`core/llm_dialects.py`，行号为 2026-09-23 时点） | 现判定 | 改后 |
|---|---|---|---|
| 1 | `is_deepseek_ledger_route`（:450） | deepseek ∧ (官方 ∨ `deepseek_cache_routes`) | `_is_deepseek_family` |
| 2 | `_select_request_tools_for_route`（约 :505）deepseek 分支 | 同 #1 | `_is_deepseek_family` |
| 3 | `_select_request_tools_for_route` qwen 分支 | `_qwen_cache_enabled` ∨ 断点配置 | `_is_qwen_family` |
| 4 | `_supports_cache_keepalive`（:670） | kimi/deepseek/glm ∧ 各自官方；qwen ∧ (enabled ∨ 白名单) | 四族任一 ∨ 白名单（见 3.2 代码示意） |
| 5 | `_attach_kimi_request_extras`（约 :1218） | kimi ∧ `_is_official_kimi_route` | `_is_kimi_family` |
| 6 | `_qwen_cache_enabled`（约 :270） | qwen ∧ (官方 ∨ `enable_prompt_cache`) | `_is_qwen_family` |
| 7 | `_qwen_cache_control_configured`（约 :293） | qwen ∧ (官方 ∨ `enable_cache_control`) | `_is_qwen_family` |
| 8 | `_attach_qwen_request_extras`（约 :1248） | 依赖 #6 | 自动跟随 |
| 9 | `_uses_deepseek_prompt_ledger`（约 :462） | 依赖 #1 | 自动跟随 |

`_supports_cache_control`（约 :781）经 #7 间接跟随；`_apply_cache_control` 协议级逻辑不动。

### 2.2 保留不动的相关设施

- `_is_official_kimi_route` / `_is_official_deepseek_route` / `_is_official_zhipu_route` /
  `_is_official_dashscope_route`：**保留函数本体**（非缓存用途仍引用：thinking 判定、
  各 `_is_*_family` 的 api_base 兜底等）。
- `_keepalive_route_explicitly_enabled` + `cache_keepalive_routes`：**保留**——改后仍作为
  「对非四族模型」的显式保活通道（该分支判定链不动）。
- `deepseek_cache_routes`、`enable_prompt_cache`、`enable_cache_control` 的解析：保留
  （纯兼容，不再必需）。

### 2.3 受影响测试（语义反转清单，以 RED 实测为准）

| 测试 | 现状断言 | 处置 |
|---|---|---|
| `tests/test_deepseek_prompt_ledger.py::test_unconfigured_gateway_stays_out_of_ledger` | 未配置网关不进账本 | **反转**：任意网关进账本 |
| `tests/test_deepseek_prompt_ledger.py::test_configured_deepseek_cache_route_enables_ledger` / `..._sorts_tools` | 配置后进账本 / 排序 | 保留，可改名以描述新语义（配置不再必要） |
| `tests/test_llm.py::test_kimi_third_party_openai_route_omits_official_cache_key` | 第三方网关不发亲和键 | **反转**：第三方也发 |
| `tests/test_llm.py::test_kimi_cache_affinity_rejects_noncanonical_official_urls` | 非规范 URL 不发亲和键且不保活 | **反转**：任意 api_base 都发、都保活 |
| `tests/test_llm.py::test_non_kimi_model_gets_no_cache_affinity` | 非 kimi 不发 | 保留（防误放） |
| `tests/test_qwen_cache.py`、`tests/test_cache_keepalive.py` 中「第三方不放行」类断言 | 各家族非官方不放行 | 实施时以 RED 清单逐条反转为「第三方放行」 |

---

## 3. 设计

### 3.1 核心原则

只改「要不要发」的条件表达式；所有机制实现路径零改动。新条件统一为对应
`_is_*_family(model_name, api_base)`（保留 api_base 参数，兜底语义不变）。

### 3.2 判定矩阵（改后）与关键代码形态

| 模型家族 | 账本 | 工具排序 | 亲和键 | 断点 | 会话缓存头 | 保活 |
|---|---|---|---|---|---|---|
| deepseek | ✓ 任意渠道 | ✓ | — | — | — | ✓ |
| kimi | — | — | ✓ 任意渠道 | （协议级不变） | — | ✓ |
| glm | — | — | — | — | — | ✓ |
| qwen | — | ✓ | — | ✓ 任意渠道 | ✓ | ✓ |
| 其它模型 | — | — | — | — | — | 白名单显式放行（保留） |

`_supports_cache_keepalive` 改后形态示意：

```python
return (
    _is_kimi_family(model_name, api_base)
    or _is_deepseek_family(model_name, api_base)
    or _is_glm_family(model_name, api_base)
    or _is_qwen_family(model_name, api_base)
    or _keepalive_route_explicitly_enabled(api_base)
)
```

### 3.3 新增：亲和键拒绝兜底（仿 cache_control 降级链）

全放后 kimi 亲和键（`prompt_cache_key` / `metadata.user_id`）会发往未声明支持该字段的渠道，
个别严格网关可能以 400 拒绝。为此补齐与 cache_control 完全同构的降级机制：

1. **状态**：`_affinity_disabled_routes`（route key → 恢复时间戳）+ 同款锁；TTL 复用
   `_CACHE_CONTROL_REJECTION_TTL_SECONDS`（900s，到期自动再试）。
2. **函数**：`_disable_affinity_route(model, api_base)` / `_affinity_route_disabled(model, api_base)`
   / `_is_kimi_affinity_rejection(api_err)`——判定从严：仅当 400（或 `BadRequestError`）且
   错误文案点名 `prompt_cache_key` 或 `user_id` 才拉黑（防无关故障误伤）。
3. **消费点**：`_attach_kimi_request_extras`（及其经 `_provider_cache_affinity_options` 的
   所有调用方）在附加前先查 `_affinity_route_disabled`。
4. **重试链**：`core/llm.py` 现有 `cache_control_downgraded` 分支（约 :1902，`_disable_cache_control_route`
   调用处）旁并列一个 `affinity_downgraded` 分支：命中判定 → 拉黑 → 本轮剥离重发
   （沿用与 cache_control 相同的「重建请求重试」路径）。

### 3.4 保活全放的保护（复核项）

- 沿用现状：`account_keepalive_response` 的「provider 未返回 usage / 校验失败 → 停止后续
  保活」自保护；全局关闭通道 `prompt_cache_keepalive_seconds=0`；`token_usage` 的
  `cache_scope=keepalive` 记账可观测（均已存在）。
- **实施时复核**：读 `core/cache_keepalive.py` 服务循环，确认单会话（渠道）连续失败只停
  自身、不拖垮其它会话；如发现级联缺口，按「失败计数上限即停用」补充并记入实施计划。

### 3.5 兼容性

- 配置零改动：`deepseek_cache_routes`、`cache_keepalive_routes`、`enable_prompt_cache`、
  `enable_cache_control` 全部保留解析（不产生未知键告警、不阻断 strict 启动）。语义降级：
  - `cache_keepalive_routes`：保留「对非四族模型显式放行保活」能力（见 3.2）；
  - `deepseek_cache_routes` / `enable_prompt_cache` / `enable_cache_control`：纯兼容项。
- 官方路由行为逐字节不变（由字节 golden 回归守护）；非官方渠道从「未优化」变「优化」
  属本设计预期变更。

---

## 4. 风险与缓解

| 风险 | 等级 | 缓解 |
|---|---|---|
| 无缓存渠道保活持续烧请求 | 中 | usage 校验失败自停用；`prompt_cache_keepalive_seconds=0` 可全局关；keepalive scope 记账可观测 |
| kimi 亲和键被渠道拒绝 | 低 | 3.3 兜底拉黑，TTL 自愈 |
| qwen 断点被渠道拒绝 | 低 | 现有兜底（已存在，机制不变） |
| 非 DeepSeek 上游的账本「白做」 | 低 | append-only 对任意上游只增不减；成本为 DB 行数，可忽略 |
| 字节稳定性回归 | 低 | `test_provider_payload_golden` + `test_prompt_byte_stability`（sha256 golden `ef286e51…`）必须全绿 |
| 判定放宽误放非本族模型 | 低 | `_is_*_family` 判定本体不变，仅去路由条件；新测试矩阵覆盖「非本族不误放」 |

---

## 5. 验证方案

1. **新增/更新单测**（TDD，先 RED 后 GREEN）：
   - 判定矩阵：四族 × {官方, tokenrhythm, 阿里云 token-plan, 任意中转} 全放行；非族模型不误放；
   - kimi 兜底：模拟 400（文案含 `prompt_cache_key`）→ 拉黑 → 重试不带键 → TTL 过期恢复；
   - 按 2.3 清单反转既有断言。
2. **回归（必须全绿）**：
   - 字节 golden：`tests/test_provider_payload_golden.py`、`tests/test_prompt_byte_stability.py`；
   - 7 文件验收集（基线 123 passed）：`test_deepseek_prompt_ledger` / `test_session_stable_snapshot`
     / `test_prompt_byte_stability` / `test_context_injection` / `test_qwen_cache`
     / `test_cache_diagnostics_storage` / `test_agent_service`；
   - 追加：`tests/test_llm.py`、`tests/test_cache_keepalive.py`。
3. **实测验收（落地后人工）**：
   - 换渠道会话（如阿里云 token-plan 的 deepseek-v4.1-flash）→ `cache_request_diagnostics.request_reason`
     出现账本归因（`first_generation` → 热路径 `""`）；
   - `token_usage` 按日命中率对比，建议观察 3~7 天。

---

## 6. 实施备注

- 运行环境：仓库根目录，`venv\Scripts\python.exe`（Python 3.14；`.venv` 不存在）。
- 遵守仓库 Global Constraints：最小改动、每步 TDD（先 RED）、字节 golden 守护、独立 commit。
- 变更文件预估：`core/llm_dialects.py`（主）、`core/llm.py`（兜底接入与重试链）、
  `tests/test_llm.py`、`tests/test_deepseek_prompt_ledger.py`、`tests/test_qwen_cache.py`
  （+ 视 RED 情况 `tests/test_cache_keepalive.py`）。
- 后续流程：本设计 → writing-plans 出实施计划 → 按计划实施。