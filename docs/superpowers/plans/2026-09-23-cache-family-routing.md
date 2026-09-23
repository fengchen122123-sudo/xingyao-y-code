# 缓存优化家族化判定（去渠道依赖）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把缓存优化适用判定从「模型家族 ∧ 渠道白名单」改为「只看模型家族」——deepseek → 账本+工具排序+保活；kimi → 亲和键+保活；glm → 保活；qwen → 断点+会话缓存头+工具排序+保活；并补齐 kimi 亲和键 400 拒绝兜底。

**Architecture:** 只改「要不要发」的条件表达式（统一为对应 `_is_*_family(model_name, api_base)`），机制实现路径零改动。官方路由行为逐字节不变（字节 golden 守护）；非官方渠道从「未优化」变「优化」是预期变更。新增与 cache_control 降级完全同构的亲和键拉黑/重试链（`_affinity_disabled_routes` + `_disable_affinity_route` + `_is_kimi_affinity_rejection` + llm.py 重试分支）。

**Tech Stack:** Python（仓库 venv）、pytest、自研 LLM 传输层（`core/llm_dialects.py` + `core/llm.py`）。

**上游设计文档（唯一事实源）：** `docs/superpowers/specs/2026-09-23-cache-family-routing-design.md`

## Global Constraints

- 只动「缓存优化」判定链路；thinking 字段（`_deepseek_thinking_enabled` 官方守卫）、`_supports_reasoning_fields`、max_tokens 预算、推理档位等非缓存逻辑一概不碰（设计 §1.3）。
- 机制行为本身不变：账本结构、保活请求构造、`_apply_cache_control` 协议逻辑零改动。
- 字节稳定性硬门槛：`tests/test_prompt_byte_stability.py`（sha256 golden `ef286e51…`）必须全绿；`tests/test_provider_payload_golden.py` 必须全绿——官方路由用例逐字节不变，非官方路由用例按设计 §3.2 预期精确更新。
- 配置键全部保留兼容：`deepseek_cache_routes`、`cache_keepalive_routes`、`enable_prompt_cache`、`enable_cache_control` 不删、不告警、用户配置零改动（设计 §3.5）。
- 回归基线（2026-09-23 实跑）：7 文件验收集 **123 passed**；另加 `test_llm.py`、`test_cache_keepalive.py`、`test_provider_payload_golden.py` 后合计 **245 passed**。
- 每任务：先 RED（跑出失败且失败原因正确）→ 最小实现（GREEN）→ 相关测试全绿 → 独立 commit（风格 `feat(cache): ...`）。
- 所有命令在仓库根目录、用 `venv\Scripts\python.exe`（本机 Python 3.12.10）执行；文件保持 UTF-8。
- 多会话共享工作区：禁止 `git add -A` / `git commit -a`，只 add 本任务改动的文件。

## 摸底结论（实施前已核实，直接影响任务设计）

1. **判定点集中且单点**：六个判定函数全在 `core/llm_dialects.py`（`_qwen_cache_enabled:275` / `_qwen_cache_control_configured:285` / `is_deepseek_ledger_route:450` / `_select_request_tools_for_route:474` / `_supports_cache_keepalive:670` / `_attach_kimi_request_extras:1212`），`core/agent_service.py` 只经 `is_deepseek_ledger_route` 消费（:365、:1046，均有 `if api_base:` 守卫，测试不传 api_base 不受影响），自动跟随。
2. **配置解析设施保留**：改后 qwen 判定不再读配置（`_resolve_model_config` 成为纯兼容保留）；`cache_keepalive_routes` 显式通道继续服务「非四族模型」（`_keepalive_route_explicitly_enabled` 判定链不动）。
3. **§3.4 保活服务复核已完成**（实读 `core/cache_keepalive.py`）：`_run` 循环按会话挑最早到期执行，单会话连续失败只 `self._entries.pop(session_key)` 停自己，其余会话不受影响（revision 让位机制）；**无需补充级联保护代码**，仅在本计划记录结论。
4. **既有测试语义反转点已实测枚举**（见各任务 RED 清单）。额外发现（超出设计 §2.3 清单，RED 实测为准）：`test_deepseek_generation_identity_isolated_from_kimi_and_endpoints` 的 relay 断言、`test_qwen_config_driven_cache_enablement` 三连断言、`test_qwen_cache_config_lookup_*` 两个缓存用例、`test_keepalive_routes_config_enables_explicit_gateway_routes`（需改用非四族模型）。
5. **金样影响面**：`tests/test_provider_payload_golden.py` 中 5 个非官方路由用例按预期更新——`test_model_reasoning_choice_provider_kwargs_are_byte_golden` 的 deepseek/qwen×2/kimi 三条 + `test_exact_model_id_reaches_final_provider_payload`；官方路由用例（deepseek 官方、kimi 官方 openai/anthropic、stepfun、responses）逐字节不变。金样键序敏感（`snapshot_provider_kwargs` 用 `sort_keys=False`），更新时按失败输出中的实际键序写入，并逐字段核对为预期变更。
6. **任务顺序设计**：先 qwen（其 `_qwen_cache_enabled` 变家族判定后会顺带激活 qwen 的保活/工具排序短路径），再 deepseek 账本、工具排序、kimi 亲和键、保活四族全放、亲和键兜底——使每个任务的测试改动最小且每步全绿。

## 文件结构

| 文件 | 动作 | 职责 |
|---|---|---|
| `core/llm_dialects.py` | 改 | 六个判定点去渠道化 + 亲和键兜底状态/判定/剥离 |
| `core/llm.py` | 改 | 亲和键 400 降级重试分支 + 导入（Task 6） |
| `tests/test_cache_family_routing.py` | 建 | 判定矩阵（四族 × 四渠道）+ 非族不误放 + kimi 亲和键拒绝兜底 |
| `tests/test_qwen_cache.py` | 改 | qwen 放行/工具排序/会话头/配置缓存用例反转与重写 |
| `tests/test_deepseek_prompt_ledger.py` | 改 | 账本/工具排序断言反转 |
| `tests/test_llm.py` | 改 | kimi 亲和键第三方断言反转 |
| `tests/test_cache_keepalive.py` | 改 | 保活「仅官方」断言反转 + 显式通道用例重写 |
| `tests/test_provider_payload_golden.py` | 改 | 非官方路由金样按预期更新 |

---

### Task 1: qwen 缓存判定去渠道化（断点 + 会话缓存头）

**Files:**
- Modify: `core/llm_dialects.py:275-296`（`_qwen_cache_enabled` / `_qwen_cache_control_configured`）、`:714-720`（`_CACHE_CONTROL_PROVIDERS` 注释）、`:781-796`（`_supports_cache_control` docstring）
- Modify: `tests/test_qwen_cache.py`（8 处反转/重写）
- Modify: `tests/test_provider_payload_golden.py`（qwen×2 金样用例）
- Create: `tests/test_cache_family_routing.py`

**Interfaces:**
- Consumes: `_is_qwen_family(model_name, api_base)`（既有）、`_is_official_dashscope_route`（保留不动）。
- Produces: `_qwen_cache_enabled(model_name, api_base="", model_cfg=None) -> bool`（新语义：纯家族判定；签名不变）、`_qwen_cache_control_configured(...) -> bool`（同上）。两者自动带动 `_supports_cache_control`、`_attach_qwen_request_extras`、`_select_request_tools_for_route`（qwen 侧）、`_supports_cache_keepalive`（qwen 侧）。

- [ ] **Step 1: 新建矩阵测试文件（qwen 部分）+ 反转 qwen 既有断言（RED）**

新建 `tests/test_cache_family_routing.py`：

```python
"""缓存优化家族化判定：四族 × 渠道矩阵与 kimi 亲和键拒绝兜底。

判定只看模型家族，不看渠道：deepseek → 账本+工具排序+保活；
kimi → 亲和键+保活；glm → 保活；qwen → 断点+会话缓存头+工具排序+保活。
"""

import time
from copy import deepcopy

import pytest

# 四族代表模型与各自官方渠道
_FAMILIES = [
    ("openai/deepseek-v4.1-flash", "https://api.deepseek.com"),
    ("openai/kimi-k2.6", "https://api.moonshot.cn/v1"),
    ("openai/glm-5.3", "https://open.bigmodel.cn/api/paas/v4"),
    ("openai/qwen3.8-flash", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
]

# 非官方渠道：tokenrhythm / 阿里云 token-plan（maas.aliyuncs.com）/ 任意中转
_OTHER_CHANNELS = [
    "https://tokenrhythm.studio/v1",
    "https://workspace-123.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
    "https://relay.example/v1",
]


@pytest.fixture(autouse=True)
def _isolate_route_state(monkeypatch):
    """隔离路由判定全局态：配置替身、配置解析缓存与拉黑表不跨测试残留。"""
    import core.llm_dialects as dialects

    monkeypatch.setattr("core.model_manager.load_config", lambda: {})
    with dialects._qwen_cache_cfg_lock:
        dialects._keepalive_routes_cfg_cache["entry"] = None
        dialects._deepseek_cache_routes_cfg_cache["entry"] = None
        dialects._qwen_cache_cfg_cache.clear()
    with dialects._affinity_disabled_lock:
        dialects._affinity_disabled_routes.clear()
    with dialects._cache_control_disabled_lock:
        dialects._cache_control_disabled_routes.clear()
    yield
    with dialects._affinity_disabled_lock:
        dialects._affinity_disabled_routes.clear()
    with dialects._cache_control_disabled_lock:
        dialects._cache_control_disabled_routes.clear()


# ---------------------------------------------------------------------------
# qwen：断点 + 会话缓存头 + 工具排序，任意渠道自动启用
# ---------------------------------------------------------------------------


def test_qwen_cache_extras_enabled_across_channels():
    """qwen 家族任意渠道：缓存开启判定、断点支持、会话缓存头、工具排序全部生效。"""
    from core.llm import (
        _provider_request_options,
        _qwen_cache_enabled,
        _select_request_tools_for_route,
        _supports_cache_control,
    )

    model = "openai/qwen3.8-flash"
    tools = [
        {"type": "function", "function": {"name": "zeta", "parameters": {}}},
        {"type": "function", "function": {"name": "alpha", "parameters": {}}},
    ]
    for api_base in [*_OTHER_CHANNELS]:
        assert _qwen_cache_enabled(model, api_base) is True, api_base
        assert _supports_cache_control(model, api_base) is True, api_base
        opts = _provider_request_options(model, api_base, cache_scope="main")
        assert (
            opts.get("extra_headers", {}).get("x-dashscope-session-cache") == "enable"
        ), api_base
        sorted_tools = _select_request_tools_for_route(model, api_base, tools)
        assert [t["function"]["name"] for t in sorted_tools] == ["alpha", "zeta"]
```

修改 `tests/test_qwen_cache.py` 的 8 处（逐一）：

1. `test_qwen_tools_canonicalization`（:97-103）第三方段反转为排序生效：

```python
    # 第三方路由（任意中转）：同样排序（判定只看模型家族）
    sorted_gateway_tools = _select_request_tools_for_route(
        "openai/qwen3.8-max",
        "https://gateway.example/v1",
        tools,
    )
    assert [t["function"]["name"] for t in sorted_gateway_tools] == ["alpha_tool", "zeta_tool"]
```

2. `test_qwen_session_cache_header_injection`（:129-135）第三方段反转为注入：

```python
    # 第三方网关同样注入（判定只看模型家族）
    opts_gateway = _provider_request_options(
        "openai/qwen3.8-max",
        gateway_base,
        cache_scope="main",
    )
    assert opts_gateway.get("extra_headers", {}).get("x-dashscope-session-cache") == "enable"
```

3. `test_qwen_dashscope_explicit_cache_control`（:146-174）：函数改名 `test_qwen_cache_control_enabled_across_channels`；:151 反转为 `assert _supports_cache_control("openai/qwen3.8-max", gateway_base)`；第三方段反转为标记生效：

```python
    # 第三方网关同样打 cache_control 断点
    marked_gateway = _apply_cache_control(deepcopy(messages), "openai/qwen3.8-max", gateway_base)
    assert isinstance(marked_gateway[0]["content"], list)
    assert marked_gateway[0]["content"][0]["cache_control"] == {"type": "ephemeral"}
```

4. `test_qwen_keepalive_support_and_request_building`（:181-183）注释与断言反转：

```python
    # 家族判定：官方与第三方路由都放行保活
    assert _supports_cache_keepalive("openai/qwen3.8-max", official_base)
    assert _supports_cache_keepalive("openai/qwen3.8-max", gateway_base)
```

5. `test_qwen_config_driven_cache_enablement`（:307-323）改名 `test_qwen_cache_enabled_regardless_of_config`，第 1 段三连断言反转为全开（配置不再是必要条件）：

```python
def test_qwen_cache_enabled_regardless_of_config(monkeypatch):
    """Qwen 家族任意渠道默认全开；配置键保留兼容，存在与否都不影响判定。"""
    gateway_base = "https://tokenrhythm.studio/v1"
    model = "openai/qwen3.8-flash"

    # 1. 未配置任何缓存键：家族判定下默认全开
    monkeypatch.setattr("core.model_manager.load_config", lambda: {
        "providers": [{
            "name": "TokenRhythm",
            "base_url": gateway_base,
            "models": [{"litellm_model": model, "name": "qwen3.8-flash"}],
        }]
    })
    from core.llm import _qwen_cache_enabled, _supports_cache_keepalive, _supports_cache_control
    assert _qwen_cache_enabled(model, gateway_base)
    assert _supports_cache_keepalive(model, gateway_base)
    assert _supports_cache_control(model, gateway_base)
    # session-cache header 同样注入
    opts = _provider_request_options(model, gateway_base, cache_scope="main")
    assert opts.get("extra_headers", {}).get("x-dashscope-session-cache") == "enable"
```

（第 2、3 段（provider 级 `enable_prompt_cache`、model 级 `enable_cache_control`）保留为配置兼容回归，断言不变。）

6. `test_qwen_tools_canonicalized_when_only_cache_control_configured`（:430-459）改名 `test_qwen_tools_canonicalized_for_third_party_route`，末尾断言反转 + 注释更新：

```python
    # 缓存开关判定同样只看家族：配置不再是必要条件
    assert _qwen_cache_enabled(model, gateway_base)
```

7. `test_qwen_cache_config_lookup_is_cached`（:609-628）重写为「显式保活通道仍走配置缓存」（非四族模型）：

```python
def test_keepalive_route_config_lookup_is_cached(monkeypatch):
    """非四族模型的显式保活通道判定：同一路由一轮内多次判定只读一次 config.json。"""
    reads = []

    def fake_load_config():
        reads.append(1)
        return {"cache_keepalive_routes": ["tokenrhythm.studio"]}

    monkeypatch.setattr("core.model_manager.load_config", fake_load_config)
    for _ in range(6):
        assert _supports_cache_keepalive("openai/some-model", _GATEWAY)
    assert len(reads) == 1
```

8. `test_qwen_cache_config_lookup_refreshes_when_config_changes`（:631-648）重写：

```python
def test_keepalive_route_config_lookup_refreshes_when_config_changes(monkeypatch):
    """配置被改写后显式保活通道判定必须跟上，不能永远停留在旧结论。"""
    monkeypatch.setattr("core.model_manager.load_config", lambda: {
        "cache_keepalive_routes": []
    })
    assert not _supports_cache_keepalive("openai/some-model", _GATEWAY)

    monkeypatch.setattr("core.model_manager.load_config", lambda: {
        "cache_keepalive_routes": ["tokenrhythm.studio"]
    })
    assert _supports_cache_keepalive("openai/some-model", _GATEWAY)
```

- [ ] **Step 2: 运行验证 RED**

Run: `venv\Scripts\python.exe -m pytest tests/test_qwen_cache.py tests/test_cache_family_routing.py -q`

Expected: FAIL——`test_qwen_cache.py` 8 个用例失败（tools 不排序、header 不注入、cache_control 不支持、keepalive 不放行、config lookup 读盘数不符）；`test_cache_family_routing.py::test_qwen_cache_extras_enabled_across_channels` 失败。失败原因必须是「旧判定不放行」，不是笔误。

- [ ] **Step 3: 最小实现**

修改 `core/llm_dialects.py`：

```python
def _qwen_cache_enabled(model_name, api_base="", model_cfg=None):
    """判断当前 Qwen 模型路由是否开启缓存加速：只看模型家族，不看渠道。

    （model_cfg 参数仅为调用方签名兼容保留；enable_prompt_cache /
    prompt_cache 配置键仍被解析但不再是必要条件。）
    """
    return _is_qwen_family(model_name, api_base)


def _qwen_cache_control_configured(model_name, api_base="", model_cfg=None):
    """Qwen 路由是否启用显式 cache_control 断点：只看模型家族，不看渠道。

    不含运行期 400 黑名单降级：黑名单只关掉标记本身，tools 规范化等请求
    形状稳定措施不随 TTL 黑名单来回翻转（形状抖动比标记更伤缓存前缀）。
    """
    return _is_qwen_family(model_name, api_base)
```

同步更新两处注释/docstring（`core/llm_dialects.py:714-720` 的 `_CACHE_CONTROL_PROVIDERS` 注释、`:781-796` 的 `_supports_cache_control` docstring）：

```python
# Qwen 不在此白名单里，但 Qwen 家族任意渠道默认启用（见 _supports_cache_control）；
# 其余 OpenAI 兼容网关默认不标记——隐式缓存本就命中，而显式标记可能被严格校验的
# 网关拒绝为 400（真被拒绝时由 _disable_cache_control_route 临时降级兜底）。
```

```python
    """判断 provider 是否支持 cache_control 字段。

    保守策略：
    - Anthropic：始终支持
    - Qwen 家族：任意渠道默认启用（家族判定，不再依赖渠道或配置）
    - 若先前已被上游 400 拒绝，降级一段时间（TTL 到期后自动恢复）
    """
```

- [ ] **Step 4: 运行验证 GREEN**

Run: `venv\Scripts\python.exe -m pytest tests/test_qwen_cache.py tests/test_cache_family_routing.py -q`
Expected: PASS。

- [ ] **Step 5: 更新 qwen 金样用例（按预期变更）**

Run: `venv\Scripts\python.exe -m pytest tests/test_provider_payload_golden.py -q`
Expected: FAIL——`test_model_reasoning_choice_provider_kwargs_are_byte_golden[openai/qwen3.7-max-…]` 与 `[…qwen3.8-max…]` 两条失败。从失败输出读实际快照（键序敏感，用 `sort_keys=False` 序列化），逐字段核对后更新 `provider_tail`，预期仅以下变化：

- `tools`：`[ZETA_TOOL, ALPHA_TOOL]` → `[ALPHA_TOOL, ZETA_TOOL]`（字母序）
- `messages`：system 与尾部 user 变为 content 块并携带 `cache_control: {"type": "ephemeral"}`：

```python
    "messages": [
        {"role": "system", "content": [
            {"type": "text", "text": "stable", "cache_control": {"type": "ephemeral"}},
        ]},
        {"role": "user", "content": [
            {"type": "text", "text": "inspect", "cache_control": {"type": "ephemeral"}},
        ]},
    ],
```

- 新增 `extra_headers: {"x-dashscope-session-cache": "enable"}`（实际键序以失败输出为准；`extra_headers` 在 `extra_body` 之前——`_attach_qwen_request_extras` 先于 `_apply_reasoning_choice` 执行）。
- 其余字段（max_tokens / extra_body 内容 / tool_choice / api_base / timeout）不变。

重新运行至 PASS。**任何超出上表的字节变化都要停下来核查**（防止误放或误改）。

- [ ] **Step 6: 相关回归 + 提交**

Run: `venv\Scripts\python.exe -m pytest tests/test_deepseek_prompt_ledger.py tests/test_llm.py tests/test_cache_keepalive.py tests/test_session_stable_snapshot.py tests/test_prompt_byte_stability.py tests/test_context_injection.py tests/test_cache_diagnostics_storage.py tests/test_agent_service.py tests/test_llm_events.py -q`
Expected: 全绿（本任务尚未触碰这些文件的行为；若出现失败，先判断是否属预期反转清单，否则停下核查）。

```bash
git add core/llm_dialects.py tests/test_qwen_cache.py tests/test_provider_payload_golden.py tests/test_cache_family_routing.py
git commit -m "feat(cache): qwen 缓存判定去渠道化——断点与会话缓存头随家族自动启用"
```

---

### Task 2: deepseek 账本判定去渠道化

**Files:**
- Modify: `core/llm_dialects.py:450-459`（`is_deepseek_ledger_route`）
- Modify: `tests/test_deepseek_prompt_ledger.py:80-95`、`:372-388`
- Modify: `tests/test_cache_family_routing.py`（追加账本矩阵）

**Interfaces:**
- Consumes: `_is_deepseek_family(model_name, api_base)`（既有）。
- Produces: `is_deepseek_ledger_route(model_name, api_base) -> bool`（新语义：纯家族判定）。`_uses_deepseek_prompt_ledger`、`agent_service` 水位线基线/主动轮换自动跟随。

- [ ] **Step 1: 反转账本测试 + 追加矩阵（RED）**

修改 `tests/test_deepseek_prompt_ledger.py`：

`test_unconfigured_gateway_stays_out_of_ledger`（:372-388）改名并反转：

```python
def test_any_gateway_enters_ledger_for_deepseek_family(monkeypatch):
    """任意网关进账本（配置不再必要）：非本族不误放，官方不受配置影响。"""
    from core.llm import is_deepseek_ledger_route

    _use_deepseek_cache_routes(monkeypatch, ["tokenrhythm.studio"])

    assert is_deepseek_ledger_route(
        "openai/deepseek-v4-flash-0731", "https://relay.example/v1"
    )
    assert not is_deepseek_ledger_route(
        "openai/kimi-for-coding", "https://tokenrhythm.studio/v1"
    )
    # 官方路由判定不受配置影响：配置为空时官方端点照样进账本
    _use_deepseek_cache_routes(monkeypatch, [])
    assert is_deepseek_ledger_route(
        "openai/deepseek-v4-pro", "https://api.deepseek.com/v1"
    )
```

`test_deepseek_generation_identity_isolated_from_kimi_and_endpoints`（:80-95）改名 `test_deepseek_generation_identity_isolated_from_kimi_and_scopes`，relay 断言反转为 True：

```python
    assert _uses_deepseek_prompt_ledger(
        "openai/deepseek-v4-pro", "https://relay.example/v1", "main", True, "s1"
    )
```

（kimi 与 subagent scope 两条 False 断言保留——非本族/非主会话仍不放行。）

追加到 `tests/test_cache_family_routing.py`：

```python
# ---------------------------------------------------------------------------
# deepseek：账本判定，任意渠道自动放行；非本族不误放
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("api_base", ["https://api.deepseek.com", *_OTHER_CHANNELS])
def test_deepseek_ledger_route_covers_all_channels(api_base):
    from core.llm import is_deepseek_ledger_route

    assert is_deepseek_ledger_route("openai/deepseek-v4.1-flash", api_base) is True


def test_deepseek_ledger_route_not_released_for_other_families():
    from core.llm import is_deepseek_ledger_route

    assert is_deepseek_ledger_route("openai/kimi-for-coding", "https://tokenrhythm.studio/v1") is False
    assert is_deepseek_ledger_route("openai/qwen3.8-flash", "https://relay.example/v1") is False
    assert is_deepseek_ledger_route("openai/glm-5.3", "https://relay.example/v1") is False
```

- [ ] **Step 2: 运行验证 RED**

Run: `venv\Scripts\python.exe -m pytest tests/test_deepseek_prompt_ledger.py tests/test_cache_family_routing.py -q`
Expected: FAIL——改名后的两个用例失败（relay 网关未进账本）、`test_deepseek_ledger_route_covers_all_channels[relay/maas/tokenrhythm]` 失败；`[api.deepseek.com]` 通过。

- [ ] **Step 3: 最小实现**

```python
def is_deepseek_ledger_route(model_name, api_base):
    """DeepSeek 家族缓存优化路由判定：主会话不可变 Prompt Ledger 的核心条件。

    只看模型家族，不看渠道：任意网关（tokenrhythm、阿里云 token-plan、
    其它中转）上的 deepseek 模型一律放行。供 agent_service 的水位线基线与
    主动轮换判定复用（账本启用还需 persist / parent_session / cache_scope=main
    等会话层条件，见 _uses_deepseek_prompt_ledger）。
    """
    return _is_deepseek_family(model_name, api_base)
```

- [ ] **Step 4: 运行验证 GREEN**

Run: `venv\Scripts\python.exe -m pytest tests/test_deepseek_prompt_ledger.py tests/test_cache_family_routing.py tests/test_agent_service.py -q`
Expected: PASS。

- [ ] **Step 5: 相关回归 + 提交**

Run: `venv\Scripts\python.exe -m pytest tests/test_qwen_cache.py tests/test_provider_payload_golden.py tests/test_prompt_byte_stability.py tests/test_session_stable_snapshot.py tests/test_context_injection.py tests/test_cache_diagnostics_storage.py -q`
Expected: 全绿。

```bash
git add core/llm_dialects.py tests/test_deepseek_prompt_ledger.py tests/test_cache_family_routing.py
git commit -m "feat(cache): deepseek 账本判定去渠道化——按模型家族自动放行"
```

---

### Task 3: 工具排序去渠道化（deepseek + qwen）

**Files:**
- Modify: `core/llm_dialects.py:474-495`（`_select_request_tools_for_route`）
- Modify: `tests/test_deepseek_prompt_ledger.py`（追加无配置排序用例）
- Modify: `tests/test_provider_payload_golden.py`（deepseek×2 金样用例）
- Modify: `tests/test_cache_family_routing.py`（追加工具排序矩阵）

**Interfaces:**
- Consumes: `_is_deepseek_family` / `_is_qwen_family`（既有）。Task 1 完成后 qwen 侧行为已等价（本任务把表达式统一为直接家族判定）。
- Produces: `_select_request_tools_for_route(model_name, api_base, tools, model_cfg=None)`（新语义：deepseek ∨ qwen 家族排序；kimi/glm/其它原样返回）。

- [ ] **Step 1: 追加排序用例（RED）**

追加到 `tests/test_deepseek_prompt_ledger.py`（复用 `_use_deepseek_cache_routes` helper）：

```python
def test_deepseek_family_route_sorts_tools_without_config(monkeypatch):
    """家族判定下任意渠道排序 tools：配置不再必要（MCP 顺序抖动不得击穿缓存前缀）。"""
    from core.llm import _select_request_tools_for_route

    _use_deepseek_cache_routes(monkeypatch, [])
    tools = [
        {"type": "function", "function": {"name": "zeta", "parameters": {}}},
        {"type": "function", "function": {"name": "alpha", "parameters": {}}},
    ]

    sorted_tools = _select_request_tools_for_route(
        "openai/deepseek-v4-flash-0731", "https://relay.example/v1", tools
    )

    assert [tool["function"]["name"] for tool in sorted_tools] == ["alpha", "zeta"]
    assert sorted_tools is not tools
```

追加到 `tests/test_cache_family_routing.py`：

```python
# ---------------------------------------------------------------------------
# 工具排序：deepseek / qwen 任意渠道字母序；kimi / glm / 其它原样
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("model,api_base", [
    ("openai/deepseek-v4.1-flash", "https://relay.example/v1"),
    ("openai/qwen3.8-flash", "https://relay.example/v1"),
])
def test_tools_sorted_for_deepseek_and_qwen_across_channels(model, api_base):
    from core.llm import _select_request_tools_for_route

    tools = [
        {"type": "function", "function": {"name": "zeta", "parameters": {}}},
        {"type": "function", "function": {"name": "alpha", "parameters": {}}},
    ]
    sorted_tools = _select_request_tools_for_route(model, api_base, tools)
    assert [t["function"]["name"] for t in sorted_tools] == ["alpha", "zeta"]
    assert sorted_tools is not tools


def test_tools_order_untouched_for_other_models():
    from core.llm import _select_request_tools_for_route

    tools = [
        {"type": "function", "function": {"name": "zeta", "parameters": {}}},
        {"type": "function", "function": {"name": "alpha", "parameters": {}}},
    ]
    for model in ("openai/kimi-k2.6", "openai/glm-5.3", "openai/gpt-4o"):
        assert _select_request_tools_for_route(model, "https://relay.example/v1", tools) is tools
```

- [ ] **Step 2: 运行验证 RED**

Run: `venv\Scripts\python.exe -m pytest tests/test_deepseek_prompt_ledger.py::test_deepseek_family_route_sorts_tools_without_config tests/test_cache_family_routing.py -q`
Expected: FAIL——deepseek 无配置不排序、矩阵 deepseek 两条失败；qwen 两条与「其它模型」用例通过（Task 1 已生效/本就如此）。

- [ ] **Step 3: 最小实现**

```python
def _select_request_tools_for_route(model_name, api_base, tools, model_cfg=None):
    """Canonicalize tools for cache-optimized routes; preserve every other provider payload.

    覆盖 DeepSeek 家族（账本 + 工具字母序）与 Qwen 家族（断点 + 会话缓存头 +
    工具字母序）的全部渠道——tools 字母序是缓存前缀字节稳定的前提，任何开启
    缓存优化的路由都必须排序，否则 MCP 工具顺序一抖动前缀整体失效。
    """
    canonicalize = (
        _is_deepseek_family(model_name, api_base)
        or _is_qwen_family(model_name, api_base)
    )
    if not canonicalize:
        return tools
    selected = deepcopy(list(tools or []))
    return sorted(
        selected,
        key=lambda tool: str(tool.get("function", {}).get("name") or ""),
    )
```

- [ ] **Step 4: 运行验证 GREEN**

Run: `venv\Scripts\python.exe -m pytest tests/test_deepseek_prompt_ledger.py tests/test_cache_family_routing.py tests/test_qwen_cache.py -q`
Expected: PASS。

- [ ] **Step 5: 更新 deepseek 金样用例（按预期变更）**

Run: `venv\Scripts\python.exe -m pytest tests/test_provider_payload_golden.py -q`
Expected: FAIL——`test_model_reasoning_choice_provider_kwargs_are_byte_golden[openai/deepseek-v4-flash…]` 与 `test_exact_model_id_reaches_final_provider_payload`。预期仅 `tools` 变化：`[ZETA_TOOL, ALPHA_TOOL]` → `[ALPHA_TOOL, ZETA_TOOL]`；其余字段不变（deepseek 不打 cache_control、无 extra_headers、persist=False 无账本）。按失败输出更新后重跑至 PASS；超出该变化范围的字节差异必须停下核查。

- [ ] **Step 6: 相关回归 + 提交**

Run: `venv\Scripts\python.exe -m pytest tests/test_llm.py tests/test_cache_keepalive.py tests/test_prompt_byte_stability.py tests/test_llm_events.py -q`
Expected: 全绿。

```bash
git add core/llm_dialects.py tests/test_deepseek_prompt_ledger.py tests/test_provider_payload_golden.py tests/test_cache_family_routing.py
git commit -m "feat(cache): 工具排序去渠道化——deepseek/qwen 任意渠道字母序"
```

---

### Task 4: kimi 亲和键去渠道化（任意渠道发送 prompt_cache_key）

**Files:**
- Modify: `core/llm_dialects.py:1212-1241`（`_attach_kimi_request_extras`）
- Modify: `tests/test_llm.py:138-171`（两个用例反转+改名）
- Modify: `tests/test_provider_payload_golden.py`（kimi 金样用例）
- Modify: `tests/test_cache_family_routing.py`（追加亲和键矩阵）

**Interfaces:**
- Consumes: `_is_kimi_family(model_name, api_base)`（既有）、`_cache_affinity_session_id`（既有）。
- Produces: `_attach_kimi_request_extras(...)`（新语义：kimi 家族任意渠道附加亲和键；`_provider_cache_affinity_options` 全部调用方自动跟随）。Task 6 将在此追加拉黑检查。

- [ ] **Step 1: 反转 kimi 亲和键测试 + 追加矩阵（RED）**

修改 `tests/test_llm.py`：

`test_kimi_third_party_openai_route_omits_official_cache_key`（:138-150）改名 `test_kimi_third_party_openai_route_sends_cache_key`：

```python
def test_kimi_third_party_openai_route_sends_cache_key(monkeypatch):
    """第三方 OpenAI 网关同样发送缓存亲和键：判定只看模型家族。"""
    from core.llm import _provider_request_options

    _patch_session(monkeypatch, "session_abc123")
    _patch_model_cfg(monkeypatch, None)

    opts = _provider_request_options(
        "openai/kimi-k2.6", "https://tokenrhythm.studio/v1"
    )

    assert opts["max_tokens"] == 128000
    assert opts["extra_body"]["prompt_cache_key"].startswith("ycode-")
```

`test_kimi_cache_affinity_rejects_noncanonical_official_urls`（:153-171）改名 `test_kimi_cache_affinity_covers_noncanonical_api_bases`，亲和断言反转：

```python
@pytest.mark.parametrize("api_base", [
    "http://api.moonshot.cn/v1",
    "ftp://api.moonshot.ai/v1",
    "https://user:pass@api.kimi.com/coding/v1",
    "https://api.kimi.com:8443/coding/v1",
    "https://api.kimi.com/coding-evil/v1",
])
def test_kimi_cache_affinity_covers_noncanonical_api_bases(monkeypatch, api_base):
    """任意 api_base（含非规范官方 URL）都发送缓存亲和键：判定只看模型家族。"""
    from core.llm import _provider_request_options, _supports_cache_keepalive

    _patch_session(monkeypatch, "session_abc123")
    _patch_model_cfg(monkeypatch, None)

    opts = _provider_request_options("openai/kimi-k3", api_base)

    assert opts["extra_body"]["prompt_cache_key"].startswith("ycode-")
    assert _supports_cache_keepalive("openai/kimi-k3", api_base) is False
```

（末行保活断言暂保持 False——Task 5 反转。Task 4 提交时该断言仍为真。）

追加到 `tests/test_cache_family_routing.py`：

```python
# ---------------------------------------------------------------------------
# kimi：缓存亲和键，任意渠道发送
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("api_base", ["https://api.moonshot.cn/v1", *_OTHER_CHANNELS])
def test_kimi_affinity_sent_across_channels(api_base):
    from core.llm import _provider_request_options

    opts = _provider_request_options(
        "openai/kimi-k2.6", api_base, cache_session_id="family-matrix"
    )
    assert opts["extra_body"]["prompt_cache_key"].startswith("ycode-")
```

- [ ] **Step 2: 运行验证 RED**

Run: `venv\Scripts\python.exe -m pytest tests/test_llm.py tests/test_cache_family_routing.py -q`
Expected: FAIL——改名的两个 kimi 用例失败（第三方/非规范 URL 未发亲和键）；矩阵非官方三条失败、官方条通过。

- [ ] **Step 3: 最小实现**

```python
def _attach_kimi_request_extras(
    result,
    model_name,
    model_cfg,
    *,
    api_base="",
    cache_scope="main",
    cache_session_id=None,
    tool_contract="",
):
    """给 Kimi 系模型的请求参数附加缓存亲和配置（原地修改 result）。

    - 缓存亲和：OpenAI 兼容路由传 prompt_cache_key，Anthropic 路由传 metadata.user_id；
      判定只看模型家族，任意渠道都发送（被上游 400 拒绝的路由由后续拉黑机制兜底）。
    """
    anthropic_route = str(model_name or "").split("/")[0].lower() == "anthropic"

    session_id = None
    if _is_kimi_family(model_name, api_base):
        session_id = _cache_affinity_session_id(
            cache_scope=cache_scope,
            model_name=model_name,
            api_base=api_base,
            tool_contract=tool_contract,
            cache_session_id=cache_session_id,
        )
    if session_id:
        if anthropic_route:
            result.setdefault("metadata", {"user_id": session_id})
        else:
            result.setdefault("extra_body", {})["prompt_cache_key"] = session_id
```

- [ ] **Step 4: 运行验证 GREEN**

Run: `venv\Scripts\python.exe -m pytest tests/test_llm.py tests/test_cache_family_routing.py -q`
Expected: PASS。

- [ ] **Step 5: 更新 kimi 金样用例（按预期变更）**

`tests/test_provider_payload_golden.py` 的 `test_model_reasoning_choice_provider_kwargs_are_byte_golden`：给 `_capture(...)` 增加 `cache_session_id="golden-reasoning-choice"`（使亲和键确定化；其余四条用例字节不受影响），kimi 用例的 `provider_tail` 中 `extra_body` 追加 `prompt_cache_key`（实际键序以失败输出为准——`_attach_kimi_request_extras` 先写入 key、`_apply_reasoning_choice` 后追加 `reasoning_effort`，预期 key 在 `reasoning_effort` 之前）：

Run: `venv\Scripts\python.exe -m pytest tests/test_provider_payload_golden.py -q`
Expected: 首次 FAIL——从失败输出读取确定性 key（`ycode-…`），核对 lane 组成（session/scope/model/api_base/tool_contract）后写入 expected；重跑至 PASS。其余四条用例（deepseek/LongCat/qwen×2）除 qwen 已在 Task 1 更新外不得有任何字节变化。

- [ ] **Step 6: 相关回归 + 提交**

Run: `venv\Scripts\python.exe -m pytest tests/test_llm_events.py tests/test_qwen_cache.py tests/test_prompt_byte_stability.py -q`
Expected: 全绿（`test_llm_events.py` 的 kimi 用例走官方路由，字节不变）。

```bash
git add core/llm_dialects.py tests/test_llm.py tests/test_provider_payload_golden.py tests/test_cache_family_routing.py
git commit -m "feat(cache): kimi 亲和键去渠道化——任意渠道发送 prompt_cache_key"
```

---

### Task 5: 保活判定去渠道化（四族全放，显式通道保留）

**Files:**
- Modify: `core/llm_dialects.py:670-688`（`_supports_cache_keepalive`）
- Modify: `tests/test_cache_keepalive.py:128-150`、`:153-185`、`:253-281`
- Modify: `tests/test_llm.py:171`（保活断言反转）
- Modify: `tests/test_cache_family_routing.py`（追加保活矩阵）

**Interfaces:**
- Consumes: 四个 `_is_*_family`（既有）、`_keepalive_route_explicitly_enabled`（既有，判定链不动）。
- Produces: `_supports_cache_keepalive(model_name, api_base="", model_cfg=None) -> bool`（新语义：四族任一 ∨ 显式白名单）。`core/llm.py:1085` 的保活注册自动跟随。

- [ ] **Step 1: 反转保活测试 + 追加矩阵（RED）**

修改 `tests/test_cache_keepalive.py`：

1. `test_deepseek_keepalive_is_limited_to_official_routes`（:128-136）改名 `test_deepseek_keepalive_covers_third_party_routes`：

```python
def test_deepseek_keepalive_covers_third_party_routes():
    from core.llm import _supports_cache_keepalive

    assert _supports_cache_keepalive(
        "openai/deepseek-v4-pro", "https://api.deepseek.com"
    ) is True
    assert _supports_cache_keepalive(
        "openai/deepseek-v4-pro", "https://aggregator.example/v1"
    ) is True
```

2. `test_kimi_keepalive_is_limited_to_official_routes`（:139-150）改名 `test_kimi_keepalive_covers_third_party_routes`，第三方断言反转为 True。

3. `test_keepalive_routes_config_enables_explicit_gateway_routes`（:153-185）改用非四族模型（显式通道语义保留，docstring 更新）：

```python
def test_keepalive_routes_config_enables_explicit_gateway_routes(monkeypatch):
    """顶层 cache_keepalive_routes 对非四族模型的显式通道：声明后该域名放行。"""
    from core.llm import _supports_cache_keepalive

    monkeypatch.setattr(
        "core.model_manager.load_config",
        lambda: {"cache_keepalive_routes": ["tokenrhythm.studio"]},
    )

    # 裸域名条目命中带路径的 api_base（/v1 后缀不影响域名归一化）
    assert _supports_cache_keepalive(
        "openai/some-model", "https://tokenrhythm.studio/v1"
    ) is True
    # 无路径、大小写与端口变体同样命中
    assert _supports_cache_keepalive(
        "openai/some-model", "https://TokenRhythm.Studio"
    ) is True
    assert _supports_cache_keepalive(
        "openai/some-model", "https://tokenrhythm.studio:8443/v1"
    ) is True
    # 未在配置中声明的其他网关路由不受影响
    assert _supports_cache_keepalive(
        "openai/some-model", "https://aggregator.example/v1"
    ) is False

    # 条目写成完整 URL（带路径）走同一归一化，同样命中
    monkeypatch.setattr(
        "core.model_manager.load_config",
        lambda: {"cache_keepalive_routes": ["https://tokenrhythm.studio/v1"]},
    )
    assert _supports_cache_keepalive(
        "openai/some-model", "https://tokenrhythm.studio/v1"
    ) is True
```

4. `test_zhipu_keepalive_is_limited_to_official_routes`（:253-281）改名 `test_zhipu_keepalive_covers_third_party_routes`；「官方域名下的未知路径不放行」「第三方中转的 GLM 模型不放行」两段反转为 True（glm 家族判定不依赖 URL 形状）。

修改 `tests/test_llm.py` 的 `test_kimi_cache_affinity_covers_noncanonical_api_bases` 末行（:171 对应位置）反转为：

```python
    assert _supports_cache_keepalive("openai/kimi-k3", api_base) is True
```

追加到 `tests/test_cache_family_routing.py`：

```python
# ---------------------------------------------------------------------------
# 保活：四族任意渠道自动放行；非四族仍走显式白名单
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("model,official_base", _FAMILIES)
def test_keepalive_enabled_for_family_across_channels(model, official_base):
    from core.llm import _supports_cache_keepalive

    for api_base in [official_base, *_OTHER_CHANNELS]:
        assert _supports_cache_keepalive(model, api_base) is True, (model, api_base)


def test_keepalive_explicit_routes_still_gate_non_family_models(monkeypatch):
    from core.llm import _supports_cache_keepalive
    import core.llm_dialects as dialects

    assert _supports_cache_keepalive("openai/some-model", "https://relay.example/v1") is False

    monkeypatch.setattr(
        "core.model_manager.load_config",
        lambda: {"cache_keepalive_routes": ["tokenrhythm.studio"]},
    )
    with dialects._qwen_cache_cfg_lock:
        dialects._keepalive_routes_cfg_cache["entry"] = None
    assert _supports_cache_keepalive("openai/some-model", "https://tokenrhythm.studio/v1") is True
```

- [ ] **Step 2: 运行验证 RED**

Run: `venv\Scripts\python.exe -m pytest tests/test_cache_keepalive.py tests/test_llm.py tests/test_cache_family_routing.py -q`
Expected: FAIL——`test_cache_keepalive.py` 的 deepseek/kimi/zhipu 三个改名用例失败（第三方未放行）、`test_keepalive_routes_config_enables_explicit_gateway_routes` 失败（aggregator 被家族判定放行）；`test_llm.py` 保活断言失败；矩阵非官方条目失败。

- [ ] **Step 3: 最小实现**

```python
def _supports_cache_keepalive(model_name, api_base="", model_cfg=None):
    """保活适用于四族模型（DeepSeek/Kimi/智谱/Qwen）的任意渠道路由。

    家族判定之外，config.json 顶层 cache_keepalive_routes 显式声明的网关路由
    （按域名归一化匹配）继续为「非四族模型」提供显式保活通道。
    """
    return (
        _is_kimi_family(model_name, api_base)
        or _is_deepseek_family(model_name, api_base)
        or _is_glm_family(model_name, api_base)
        or _is_qwen_family(model_name, api_base)
        or _keepalive_route_explicitly_enabled(api_base)
    )
```

- [ ] **Step 4: 运行验证 GREEN**

Run: `venv\Scripts\python.exe -m pytest tests/test_cache_keepalive.py tests/test_llm.py tests/test_cache_family_routing.py -q`
Expected: PASS。

- [ ] **Step 5: 相关回归 + 提交**

Run: `venv\Scripts\python.exe -m pytest tests/test_qwen_cache.py tests/test_deepseek_prompt_ledger.py tests/test_provider_payload_golden.py tests/test_agent_service.py tests/test_prompt_byte_stability.py -q`
Expected: 全绿。

```bash
git add core/llm_dialects.py tests/test_cache_keepalive.py tests/test_llm.py tests/test_cache_family_routing.py
git commit -m "feat(cache): 保活判定去渠道化——四族模型任意渠道自动保活"
```

---

### Task 6: kimi 亲和键 400 拒绝兜底（拉黑 + 降级重试）

**Files:**
- Modify: `core/llm_dialects.py:778-781` 之间插入亲和键兜底块（状态/锁/拉黑/查询/判定/剥离），`:1212` 起 `_attach_kimi_request_extras` 追加拉黑检查
- Modify: `core/llm.py`（导入 3 个新符号；`:1627` 旁初始化 `affinity_downgraded`；`:1913` 后追加降级分支）
- Modify: `tests/test_cache_family_routing.py`（追加兜底测试）

**Interfaces:**
- Produces:
  - `_affinity_disabled_routes: dict` + `_affinity_disabled_lock: threading.RLock`
  - `_disable_affinity_route(model_name, api_base, ttl_seconds=None) -> tuple`
  - `_affinity_route_disabled(model_name, api_base) -> bool`
  - `_is_kimi_affinity_rejection(api_err) -> bool`
  - `_strip_cache_affinity_options(result) -> None`
- Consumes: `_cache_control_route_key`（复用 key 归一化）、`_CACHE_CONTROL_REJECTION_TTL_SECONDS`（TTL 复用）。

- [ ] **Step 1: 写兜底失败测试（RED）**

追加到 `tests/test_cache_family_routing.py`：

```python
# ---------------------------------------------------------------------------
# kimi 亲和键 400 拒绝兜底：判定从严、临时拉黑、TTL 自愈、重试剥离
# ---------------------------------------------------------------------------


class _FakeBadRequest(Exception):
    status_code = 400


class _FakeServerError(Exception):
    status_code = 500


def test_affinity_rejection_requires_bad_request_status_and_named_field():
    """只有 400 且文案点名 prompt_cache_key / user_id 才拉黑；无关故障不误伤。"""
    from core.llm import _is_kimi_affinity_rejection

    assert _is_kimi_affinity_rejection(
        _FakeBadRequest("Error code: 400 - unknown field 'prompt_cache_key'")
    )
    assert _is_kimi_affinity_rejection(
        _FakeBadRequest("Error code: 400 - invalid metadata.user_id")
    )
    assert not _is_kimi_affinity_rejection(
        RuntimeError("connection reset while uploading prompt_cache_key (4000 bytes)")
    )
    assert not _is_kimi_affinity_rejection(
        _FakeServerError("500 internal server error mentioning user_id")
    )
    assert not _is_kimi_affinity_rejection(
        _FakeBadRequest("Error code: 400 - context length exceeded, invalid prompt")
    )


def test_affinity_blacklist_skips_key_and_expires_after_ttl():
    """拉黑期间请求不再携带亲和键；TTL 到期自动恢复，一次误判不永久关优化。"""
    from core.llm import (
        _disable_affinity_route,
        _provider_request_options,
    )

    base = "https://relay.example/v1"
    model = "openai/kimi-k2.6"

    opts = _provider_request_options(model, base, cache_session_id="affinity-ttl")
    assert opts["extra_body"]["prompt_cache_key"].startswith("ycode-")

    _disable_affinity_route(model, base, ttl_seconds=0.05)
    blocked = _provider_request_options(model, base, cache_session_id="affinity-ttl")
    assert "prompt_cache_key" not in blocked.get("extra_body", {})

    time.sleep(0.08)
    recovered = _provider_request_options(model, base, cache_session_id="affinity-ttl")
    assert recovered["extra_body"]["prompt_cache_key"].startswith("ycode-")


def _chunk(text="完成"):
    from types import SimpleNamespace

    delta = SimpleNamespace(content=text, reasoning_content=None, tool_calls=None)
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=delta, finish_reason="stop")], usage=None
    )


def test_affinity_rejection_downgrades_retry_without_key(mocker):
    """模拟 400：拉黑该路由 → 本轮重试剥离亲和键 → 后续请求也不再携带。"""
    from core.llm import (
        _affinity_route_disabled,
        _provider_request_options,
        chat_with_model_events,
    )

    captured = []

    def fake_completion(**kwargs):
        captured.append(deepcopy(kwargs))
        if len(captured) == 1:
            raise _FakeBadRequest("Error code: 400 - unknown field 'prompt_cache_key'")
        return iter([_chunk("完成")])

    mocker.patch("core.llm.completion", side_effect=fake_completion)

    list(chat_with_model_events(
        "openai/kimi-k2.6",
        "fake-key",
        [{"role": "user", "content": "提问"}],
        "https://relay.example/v1",
        persist=False,
        turn_id="turn-affinity-downgrade",
        consume_background_notifications=False,
        cache_session_id="affinity-downgrade",
    ))

    assert len(captured) >= 2
    assert captured[0]["extra_body"]["prompt_cache_key"].startswith("ycode-")
    assert "prompt_cache_key" not in captured[1].get("extra_body", {})
    assert _affinity_route_disabled("openai/kimi-k2.6", "https://relay.example/v1") is True
    follow_up = _provider_request_options(
        "openai/kimi-k2.6", "https://relay.example/v1", cache_session_id="affinity-downgrade"
    )
    assert "prompt_cache_key" not in follow_up.get("extra_body", {})
```

- [ ] **Step 2: 运行验证 RED**

Run: `venv\Scripts\python.exe -m pytest tests/test_cache_family_routing.py -q -k affinity`
Expected: FAIL——`_is_kimi_affinity_rejection` / `_disable_affinity_route` / `_affinity_route_disabled` 不存在（ImportError / AttributeError），重试用例拿不到剥离后的第二次请求。

- [ ] **Step 3: 最小实现（llm_dialects.py）**

在 `_is_cache_control_rejection`（:760-778）之后、`_supports_cache_control` 之前插入：

```python
# 被上游以 400 明确拒绝过 Kimi 缓存亲和键（prompt_cache_key / metadata.user_id）
# 的路由 -> 恢复时间戳（monotonic）。与 cache_control 降级完全同构：一次误判
# 不该把该路由的亲和优化永久关掉，TTL 到期后自动再给一次机会。
_affinity_disabled_routes = {}
_affinity_disabled_lock = threading.RLock()


def _disable_affinity_route(model_name, api_base, ttl_seconds=None):
    """临时禁用某路由的 Kimi 缓存亲和键，返回被拉黑的路由 key。"""
    ttl = (
        _CACHE_CONTROL_REJECTION_TTL_SECONDS
        if ttl_seconds is None else float(ttl_seconds)
    )
    key = _cache_control_route_key(model_name, api_base)
    with _affinity_disabled_lock:
        _affinity_disabled_routes[key] = time.monotonic() + ttl
    return key


def _affinity_route_disabled(model_name, api_base):
    """该路由是否仍在亲和键拉黑期内（过期条目顺手清理）。"""
    key = _cache_control_route_key(model_name, api_base)
    now = time.monotonic()
    with _affinity_disabled_lock:
        expire_at = _affinity_disabled_routes.get(key)
        if expire_at is None:
            return False
        if expire_at <= now:
            _affinity_disabled_routes.pop(key, None)
            return False
        return True


def _is_kimi_affinity_rejection(api_err):
    """判定上游是否以 400/bad request 明确拒绝了 Kimi 缓存亲和键字段。

    必须同时满足两个条件，缺一不判：
    1. 状态码确为 400（或异常类型名为 BadRequestError）；
    2. 错误文本点名 prompt_cache_key / user_id 这类亲和字段。
    只按文案关键词宽泛匹配的话，一次无关故障（网络抖动、上游 5xx）就会把
    该路由的亲和优化整体关掉。
    """
    if api_err is None:
        return False
    is_bad_request = (
        getattr(api_err, "status_code", None) == 400
        or type(api_err).__name__ == "BadRequestError"
    )
    if not is_bad_request:
        return False
    text = str(api_err).lower()
    return "prompt_cache_key" in text or "user_id" in text


def _strip_cache_affinity_options(result):
    """剥离请求参数中的 Kimi 缓存亲和字段（400 降级重试用）。

    只动 prompt_cache_key 与 metadata.user_id 两个亲和字段本身；
    extra_body / metadata 剥离后若为空容器则整体移除，保证重试请求
    与「从未附加亲和」的请求形状一致。
    """
    extra_body = result.get("extra_body")
    if isinstance(extra_body, dict):
        extra_body.pop("prompt_cache_key", None)
        if not extra_body:
            result.pop("extra_body", None)
    metadata = result.get("metadata")
    if isinstance(metadata, dict):
        metadata.pop("user_id", None)
        if not metadata:
            result.pop("metadata", None)
```

`_attach_kimi_request_extras` 的附加条件追加拉黑检查：

```python
    if _is_kimi_family(model_name, api_base) and not _affinity_route_disabled(
        model_name, api_base
    ):
```

- [ ] **Step 4: 最小实现（llm.py 重试链）**

导入块（`core/llm.py:827` 起）追加（保持既有排序风格）：

```python
    _disable_affinity_route,
    ...
    _is_kimi_affinity_rejection,
    ...
    _strip_cache_affinity_options,
```

`:1627` 旁初始化：

```python
            cache_control_downgraded = False
            affinity_downgraded = False
```

在 cache_control 降级分支（:1899-1913）之后、通用重试块（:1915）之前插入：

```python
                    # 上游以 400 明确拒绝 Kimi 缓存亲和键（prompt_cache_key /
                    # metadata.user_id）：本轮剥离亲和字段立即重发，并按路由
                    # 临时拉黑（TTL 到期自愈），与 cache_control 降级完全同构。
                    if not affinity_downgraded and _is_kimi_affinity_rejection(api_err):
                        affinity_downgraded = True
                        _disable_affinity_route(model_name, api_base)
                        _strip_cache_affinity_options(kwargs)
                        warn_msg = (
                            "上游拒绝缓存亲和参数 (400)，已剥离并降级重试；"
                            f"该路由的缓存亲和键在 {int(_CACHE_CONTROL_REJECTION_TTL_SECONDS // 60)} 分钟内不再启用。"
                        )
                        if event_callback is None:
                            print(f"\n[⚠️ {warn_msg}]")
                        else:
                            _notify_event(event_callback, "status", {"message": warn_msg})
                        if attempt < max_api_retries - 1:
                            continue
```

- [ ] **Step 5: 运行验证 GREEN**

Run: `venv\Scripts\python.exe -m pytest tests/test_cache_family_routing.py -q`
Expected: PASS（含兜底三用例）。

- [ ] **Step 6: 相关回归 + 提交**

Run: `venv\Scripts\python.exe -m pytest tests/test_llm.py tests/test_llm_events.py tests/test_qwen_cache.py tests/test_cache_keepalive.py tests/test_provider_payload_golden.py tests/test_prompt_byte_stability.py -q`
Expected: 全绿（拉黑表为进程级全局，测试文件已有 autouse fixture 清理；正常路径不受影响）。

```bash
git add core/llm_dialects.py core/llm.py tests/test_cache_family_routing.py
git commit -m "feat(cache): kimi 亲和键 400 拒绝兜底——临时拉黑与降级重试"
```

---

### Task 7: 全量回归与验收

**Files:**
- 不改仓库文件；探针脚本写入 AI 生成内容目录（`D:\ycodesso\cache_matrix_probe.py`，一次性，不入库）

- [ ] **Step 1: 验收套件全绿（实跑）**

Run:
```
venv\Scripts\python.exe -m pytest tests/test_cache_family_routing.py tests/test_deepseek_prompt_ledger.py tests/test_session_stable_snapshot.py tests/test_prompt_byte_stability.py tests/test_context_injection.py tests/test_qwen_cache.py tests/test_cache_diagnostics_storage.py tests/test_agent_service.py tests/test_llm.py tests/test_cache_keepalive.py tests/test_provider_payload_golden.py tests/test_llm_events.py -q
```
Expected: 全绿（数字以实跑为准；基线为 245 passed + 新增矩阵/兜底用例）。

- [ ] **Step 2: 判定矩阵探针（证据留存）**

写 `D:\ycodesso\cache_matrix_probe.py` 并运行，打印「四族 × {官方, tokenrhythm, 阿里云 token-plan, 任意中转}」判定矩阵（ledger / keepalive / qwen_cache / cache_control），核对与设计 §3.2 矩阵一致：

```python
"""判定摸底对照：打印四族 × 渠道判定矩阵（一次性探针，不属于仓库）。"""
import os

os.environ.setdefault("YCODE_DATA_DIR", r"D:\ycodesso\probe-data")

from core.llm import (  # noqa: E402
    _qwen_cache_enabled,
    _supports_cache_control,
    _supports_cache_keepalive,
    is_deepseek_ledger_route,
)

FAMILIES = [
    ("openai/deepseek-v4.1-flash", "https://api.deepseek.com"),
    ("openai/kimi-k2.6", "https://api.moonshot.cn/v1"),
    ("openai/glm-5.3", "https://open.bigmodel.cn/api/paas/v4"),
    ("openai/qwen3.8-flash", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
]
CHANNELS = [
    "https://tokenrhythm.studio/v1",
    "https://workspace-123.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
    "https://relay.example/v1",
]

for model, official in FAMILIES:
    print(f"\n== {model} ==")
    for base in [official, *CHANNELS]:
        print(
            f"  {base}\n"
            f"    ledger={is_deepseek_ledger_route(model, base)}"
            f" keepalive={_supports_cache_keepalive(model, base)}"
            f" qwen_cache={_qwen_cache_enabled(model, base)}"
            f" cache_control={_supports_cache_control(model, base)}"
        )
```

Run: `cd /d E:\fch\Y-code_tui && venv\Scripts\python.exe D:\ycodesso\cache_matrix_probe.py`

- [ ] **Step 3: 收尾核对**

- `git log --oneline -8`：确认 6 个独立 commit 存在且工作区只含自己的改动（`git status` 中不误含其它会话文件）。
- 向用户汇报：改动文件清单、每个 commit、验收实跑数字、探针矩阵输出。
- 说明后续人工验收（超出本会话范围）：换渠道会话（如阿里云 token-plan 的 deepseek-v4.1-flash）在 `cache_request_diagnostics.request_reason` 出现账本归因（`first_generation` → 热路径空串）；`token_usage` 按日命中率对照观察 3~7 天（统计口径：GUI 活动库 `~/.ycode/ycode_gui.db`；新机器以新产生流量为准）。

---

## 自检记录（计划完成后自查）

- 设计 §3.2 判定矩阵：四族 × 四渠道全部有对应任务（Task 1/2/3/4/5 的矩阵用例 + Task 7 探针）。
- 设计 §3.3 亲和键兜底：Task 6 全量覆盖（状态/锁/TTL/判定/消费点/重试链 + 测试）。
- 设计 §3.4 复核项：摸底结论 3 已记录（无需代码）。
- 设计 §3.5 兼容性：配置键与函数本体均保留（无删除动作）；官方路由字节由金样守护。
- 设计 §5 验证方案：矩阵、兜底、回归、实测验收（Task 7）逐项落位。
- 占位符扫描：无 TBD/TODO；各步骤均含具体代码与命令。
- 类型一致性：新符号命名在 Task 6 各步骤间一致（`_affinity_disabled_routes` / `_disable_affinity_route` / `_affinity_route_disabled` / `_is_kimi_affinity_rejection` / `_strip_cache_affinity_options`）。
