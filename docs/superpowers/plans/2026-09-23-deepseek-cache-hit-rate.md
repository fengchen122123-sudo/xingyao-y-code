# DeepSeek 缓存命中率提升 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 TokenRhythm 等网关的 DeepSeek 流量纳入 Prompt Ledger 与 tools 字母序规范化，用会话级目录快照消除中途设置变更导致的账本换代，并让每一次换代都留下可归因的原因。

**Architecture:** 三项改造全部落在既有的「DeepSeek 缓存优化家族」判定链上，不新增子系统。①把 `_is_official_deepseek_route` 换成一个可配置的 `_is_cache_optimized_deepseek_route`（官方路由 ∨ config.json 显式声明的网关），账本与 tools 排序两个调用点随之切换；②在 `agent_service` 层对「注入点 2」做会话级内存快照，会话首请求定型字节，中途目录变更不再改 system 头；③让 `_prepare_deepseek_ledger_request` 的每个换代分支返回 `rotation_reason`，由 `core/llm.py` 覆写到 `cache_request_diagnostics.request_reason`。

**Tech Stack:** Python 3.12、SQLite、pytest、LiteLLM 兼容请求层、Vue 3 + Vitest（前端提示文案）

**上游设计文档：** `docs/superpowers/specs/2026-09-23-deepseek-cache-hit-rate-design.md`

## Global Constraints

- 最小改动，遵循现有代码模式，不做无关重构。
- 系统提示词字节稳定性受 `tests/test_prompt_byte_stability.py` 的 sha256 golden（`ef286e513e9a5275cb79ecd48dd81da343f086c54575c921550985309e83c1e5`）守护：**快照逻辑不得改变无快照场景（`session_name` 为空）的渲染字节**。
- 非目标（设计文档 4.4）一律不做：不切 Anthropic 协议、不给 OpenAI 协议路由加显式 `cache_control`、不改压缩算法、不动保活、不动 `token_usage` 统计口径与历史数据。
- `_is_deepseek_family`（模型名含 `deepseek`）保持不变；账本语义只对 DeepSeek 磁盘缓存上游成立，非 DeepSeek 上游网关不得写入 `deepseek_cache_routes`。
- 用户侧配置是运维动作，代码里不写 `"tokenrhythm.studio"` 之类的默认值。
- 每项先写失败测试（RED）→ 验证失败原因正确 → 最小实现（GREEN）→ 跑测试确认通过，再进入下一项。
- 所有命令在仓库根目录、用项目自带的 `venv\Scripts\python.exe` 执行。

---

## 摸底结论（实施前已核实，直接影响任务设计）

三条与设计文档不一致/需要补充的事实，已实测确认，本计划据此调整：

1. **账本已在 `core/llm_dialects.py` 单点定义，切换成本极低。** `is_deepseek_ledger_route`（`core/llm_dialects.py:377`）内部的 `_is_official_deepseek_route(api_base)` 是唯一判定点；调用方（`core/agent_service.py:301`、`core/agent_service.py:969`、`core/llm_dialects.py:395`）全部只调 `is_deepseek_ledger_route`，无一处直接调 `_is_official_deepseek_route` 做过账本判定。所以 ① 只需改 2 个函数的条件表达式。

2. **`cache_request_diagnostics` 的写入路径本来就无路由开关，且已在记录 TokenRhythm 的 DeepSeek 流量。** 实测 `~/.ycode/ycode.db`：该表共 1087 行，其中 `model like '%deepseek%'` 435 行，`api_base` 全部是 `https://tokenrhythm.studio/v1`。设计文档 3.1-8「当前 deepseek 家族零记录」是过期结论——**不存在需要打开的「发射开关」**。③ 的实际缺口只是「换代原因没写进 `request_reason`」，因此 ③ 的实现是：新增 `rotation_reason` 覆写，而不是新增一条路由门禁。`_classify_cache_request_details` / `_persist_cache_request_diagnostic` 两处均保持不动。

3. **`bench/adapt_bench.py` 不经过 Prompt Ledger。** 它 `from core.llm import completion` 并直接以 `stream=False` 调用，走的是裸 provider 请求，绕开了 `chat_with_model_events` 里的账本/清洗/断点逻辑。因此用它做「改造前后对比」在构造上必然得到相同数字——它测的是**上游网关自身的前缀缓存上限**（正好回答风险表第 1 行「tokenrhythm 上游未必是官方 DeepSeek 磁盘缓存」），而不是本次改造的收益。任务 4 按此定位使用它，并如实标注该局限；账本收益的证据由任务 1/3 的单元测试 + 线上 `token_usage` 观测承担。

另需记录的一处偏差：设计文档 4.3 列出的换代原因有 5 个（`first_generation` / `pending_rotation` / `header_tools_changed` / `header_system_changed` / `boundary_ambiguous`），但 `core/llm.py:1867` 还存在一个调用方主动 `force_new=True` 的分支（超窗压缩后的重试）。该分支需要一个第 6 个取值 `caller_force_new`，否则会被误标成 `pending_rotation`。本计划采用 6 个取值。

---

## 文件结构

| 文件 | 动作 | 职责 |
|---|---|---|
| `core/llm_dialects.py` | 改 | ① 路由可配置化 + ③ 换代原因 |
| `core/agent_service.py` | 改 | ② 快照存取 + 请求链路传 `session_name` |
| `core/context_injection.py` | 改 | ② 把 `session_name` 透传到注入点 2 |
| `core/llm.py` | 改 | ③ 把 `rotation_reason` 覆写进诊断 |
| `core/llm_diagnostics.py` | 改 | ② 会话删除/重命名时清/迁快照 |
| `core/memory.py` | 改 | ③ `explicit_categories` 补 6 个换代原因的归类 |
| `frontend/src/components/Subpage.vue` | 改 | ② 技能页提示文案 |
| `frontend/src/components/PersonaSettings.vue` | 改 | ② 人设保存提示文案 |
| `tests/test_deepseek_prompt_ledger.py` | 改 | ① 两个新测试 + ③ 单位测试 + ③ 接线测试 |
| `tests/test_session_stable_snapshot.py` | 建 | ② 快照相关测试 |
| `docs/adapt-report-deepseek-tokenrhythm-cache.md` | 建 | 4 实测报告 |
| `docs/superpowers/specs/2026-09-23-deepseek-cache-hit-rate-design.md` | 改 | 4 追加验证结论 |

---

### Task 1: 改造项 ① — 缓存优化路由可配置化

**Files:**
- Modify: `core/llm_dialects.py:174-178`（新增配置键常量与缓存槽）
- Modify: `core/llm_dialects.py:303-313`（解析函数抽共用辅助）
- Modify: `core/llm_dialects.py:316-348`（新增 `_remember_deepseek_cache_routes` / `_configured_deepseek_cache_routes`）
- Modify: `core/llm_dialects.py:377-384`（`is_deepseek_ledger_route`）
- Modify: `core/llm_dialects.py:399-419`（`_select_request_tools_for_route`）
- Modify: `core/llm_dialects.py:226-228`（`_resolve_model_config` 里顺带预热新缓存）
- Test: `tests/test_deepseek_prompt_ledger.py`

**Interfaces:**
- Produces:
  - `_DEEPSEEK_CACHE_ROUTES_CONFIG_KEY: str = "deepseek_cache_routes"`
  - `_deepseek_cache_routes_cfg_cache: dict`（形如 `{"entry": None}`，供测试清缓存）
  - `_parse_route_match_entries(data, key) -> tuple[str, ...]`
  - `_parse_deepseek_cache_routes(data) -> tuple[str, ...]`
  - `_remember_deepseek_cache_routes(data, stamp) -> None`
  - `_configured_deepseek_cache_routes() -> tuple[str, ...]`
  - `_is_cache_optimized_deepseek_route(api_base="") -> bool`
- Consumes: 既有 `_normalize_route_match_key`、`_qwen_cache_config_stamp`、`_qwen_cache_cfg_lock`、`_QWEN_CACHE_CFG_TTL_SECONDS`、`_is_official_deepseek_route`。

---

- [ ] **Step 1: 写两个失败测试**

追加到 `tests/test_deepseek_prompt_ledger.py` 末尾（文件已有 `_message` / `_manager` 两个 helper，直接复用）：

```python
def _use_deepseek_cache_routes(monkeypatch, routes):
    """把顶层 deepseek_cache_routes 指向给定条目，并清掉解析缓存。"""
    import core.llm_dialects as llm_dialects

    monkeypatch.setattr(
        "core.model_manager.load_config", lambda: {"deepseek_cache_routes": list(routes)}
    )
    llm_dialects._deepseek_cache_routes_cfg_cache["entry"] = None


def test_configured_deepseek_cache_route_enables_ledger(monkeypatch):
    """配置条目让网关路由进入 Prompt Ledger：大小写/端口/路径归一化后仍命中。"""
    from core.llm import is_deepseek_ledger_route

    _use_deepseek_cache_routes(monkeypatch, ["tokenrhythm.studio"])

    assert is_deepseek_ledger_route(
        "openai/deepseek-v4-flash-0731", "https://tokenrhythm.studio/v1"
    )
    assert is_deepseek_ledger_route(
        "openai/deepseek-v4-flash-0731", "https://TokenRhythm.STUDIO:443/v1/"
    )


def test_unconfigured_gateway_stays_out_of_ledger(monkeypatch):
    """未配置的网关不得进入账本：账本只对显式声明的 DeepSeek 上游成立。"""
    from core.llm import is_deepseek_ledger_route

    _use_deepseek_cache_routes(monkeypatch, ["tokenrhythm.studio"])

    assert not is_deepseek_ledger_route(
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


def test_configured_deepseek_cache_route_sorts_tools(monkeypatch):
    """配置网关路由的 tools 必须字母序：MCP 顺序抖动不得击穿缓存前缀。"""
    from core.llm import _select_request_tools_for_route

    _use_deepseek_cache_routes(monkeypatch, ["tokenrhythm.studio"])
    tools = [
        {"type": "function", "function": {"name": "zeta", "parameters": {}}},
        {"type": "function", "function": {"name": "alpha", "parameters": {}}},
    ]

    sorted_tools = _select_request_tools_for_route(
        "openai/deepseek-v4-flash-0731", "https://tokenrhythm.studio/v1", tools
    )

    assert [tool["function"]["name"] for tool in sorted_tools] == ["alpha", "zeta"]
    assert sorted_tools is not tools
```

- [ ] **Step 2: 跑测试确认失败**

Run:
```bash
venv\Scripts\python.exe -m pytest tests/test_deepseek_prompt_ledger.py -k "configured_deepseek_cache_route_enables_ledger or unconfigured_gateway_stays_out_of_ledger or configured_deepseek_cache_route_sorts_tools" -v
```
Expected: 3 个测试全部 FAIL/ERROR，其中前两个报 `AttributeError: module 'core.llm_dialects' has no attribute '_deepseek_cache_routes_cfg_cache'`，第三个报 `AssertionError`（tools 未排序，顺序仍是 `zeta, alpha`）。若三个里任意一个直接通过，说明测试没测到目标行为，先修测试。

- [ ] **Step 3: 新增配置键常量与缓存槽**

在 `core/llm_dialects.py` 的 `_keepalive_routes_cfg_cache = {"entry": None}`（第 178 行）之后插入：

```python
# 顶层 deepseek_cache_routes：显式声明可安全启用 DeepSeek Prompt Ledger 与
# tools 字母序的第三方网关（域名/子串列表）。官方路由之外默认不启用——
# 账本语义（服务端按 2048 token 块匹配的磁盘前缀缓存）只在 DeepSeek 上游成立，
# 挂载非 DeepSeek 模型的网关加进本配置只会白做规范化。判定逻辑与
# cache_keepalive_routes 同构：只新增显式通道，不改任何官方路由判定。
_DEEPSEEK_CACHE_ROUTES_CONFIG_KEY = "deepseek_cache_routes"

# 条目格式同 _keepalive_routes_cfg_cache：`{"entry": (过期时间, stamp, routes) | None}`。
# 测试可直接把 entry 置 None 强制重读配置。
_deepseek_cache_routes_cfg_cache = {"entry": None}
```

- [ ] **Step 4: 解析逻辑抽成共用辅助，并新增 deepseek 版三件套**

把现有 `_parse_keepalive_routes`（第 303-313 行）整体替换为下面三段（保持 `_parse_keepalive_routes` 签名与语义不变，`_remember_keepalive_routes` / `_configured_keepalive_routes` 无需改动）：

```python
def _parse_route_match_entries(data, key):
    """从一份已加载的 config 中提取指定顶层键的路由条目并逐条归一化。"""
    raw = data.get(key) if isinstance(data, dict) else None
    if not isinstance(raw, (list, tuple)):
        return ()
    routes = []
    for item in raw:
        entry = _normalize_route_match_key(item)
        if entry:
            routes.append(entry)
    return tuple(routes)


def _parse_keepalive_routes(data):
    """从一份已加载的 config 中提取 cache_keepalive_routes 并逐条归一化。"""
    return _parse_route_match_entries(data, _KEEPALIVE_ROUTES_CONFIG_KEY)


def _parse_deepseek_cache_routes(data):
    """从一份已加载的 config 中提取 deepseek_cache_routes 并逐条归一化。"""
    return _parse_route_match_entries(data, _DEEPSEEK_CACHE_ROUTES_CONFIG_KEY)
```

在 `_configured_keepalive_routes`（第 348 行 `return routes` 之后）后面追加：

```python
def _remember_deepseek_cache_routes(data, stamp):
    """把顶层 deepseek_cache_routes 的解析结果按配置指纹写入缓存（重复读盘去重）。"""
    if stamp is None:
        return
    routes = _parse_deepseek_cache_routes(data)
    with _qwen_cache_cfg_lock:
        _deepseek_cache_routes_cfg_cache["entry"] = (
            time.monotonic() + _QWEN_CACHE_CFG_TTL_SECONDS, stamp, routes,
        )


def _configured_deepseek_cache_routes():
    """读取顶层 deepseek_cache_routes（域名/子串列表），带配置指纹缓存。"""
    stamp = _qwen_cache_config_stamp()
    now = time.monotonic()
    if stamp is not None:
        with _qwen_cache_cfg_lock:
            cached = _deepseek_cache_routes_cfg_cache.get("entry")
            if cached and cached[0] > now and cached[1] == stamp:
                return cached[2]
    routes = ()
    try:
        from core.model_manager import load_config

        routes = _parse_deepseek_cache_routes(load_config())
    except Exception:
        pass
    if stamp is not None:
        with _qwen_cache_cfg_lock:
            _deepseek_cache_routes_cfg_cache["entry"] = (
                now + _QWEN_CACHE_CFG_TTL_SECONDS, stamp, routes,
            )
    return routes


def _is_cache_optimized_deepseek_route(api_base=""):
    """DeepSeek 缓存优化路由 = 官方端点 ∨ 显式配置声明的 DeepSeek 上游网关。

    归一化走 _normalize_route_match_key（去 scheme/端口/路径后的小写主机名），
    用户写完整 URL 或裸域名都能命中；官方判定分支保持不变。
    """
    if _is_official_deepseek_route(api_base):
        return True
    host = _normalize_route_match_key(api_base)
    if not host:
        return False
    return any(entry in host for entry in _configured_deepseek_cache_routes())
```

- [ ] **Step 5: 在 `_resolve_model_config` 里顺带预热新缓存**

`_resolve_model_config` 第 228 行 `_remember_keepalive_routes(data, stamp)` 之后补一行，避免同一轮里两条路由配置各读一次盘：

```python
        _remember_keepalive_routes(data, stamp)
        # 同一份配置也顺手缓存顶层 deepseek_cache_routes：随后 is_deepseek_ledger_route
        # / _select_request_tools_for_route 的判定即可命中缓存，不再重复读盘
        _remember_deepseek_cache_routes(data, stamp)
```

- [ ] **Step 6: 切换两个调用点**

`is_deepseek_ledger_route`（第 377-384 行）改成：

```python
def is_deepseek_ledger_route(model_name, api_base):
    """DeepSeek 家族缓存优化路由判定：主会话不可变 Prompt Ledger 的核心条件。

    官方端点（api.deepseek.com）之外，config.json 顶层 deepseek_cache_routes
    显式声明的网关（按域名归一化匹配）同样放行——不改变任何官方路由判定，
    仅新增显式通道。供 agent_service 的水位线基线与主动轮换判定复用
    （账本启用还需 persist / parent_session / cache_scope=main 等会话层条件，
    见 _uses_deepseek_prompt_ledger）。
    """
    return _is_deepseek_family(model_name, api_base) and _is_cache_optimized_deepseek_route(api_base)
```

`_select_request_tools_for_route`（第 399-419 行）的条件表达式改成：

```python
    canonicalize = (
        _is_deepseek_family(model_name, api_base)
        and _is_cache_optimized_deepseek_route(api_base)
    ) or _qwen_cache_enabled(model_name, api_base, model_cfg) or (
        _qwen_cache_control_configured(model_name, api_base, model_cfg)
    )
```

同步把该函数的 docstring 第一段「覆盖官方 DeepSeek 路由、Qwen session-cache 路由……」改成「覆盖 DeepSeek 缓存优化路由（官方端点或 `deepseek_cache_routes` 显式声明的网关）、Qwen session-cache 路由（官方百炼或 `enable_prompt_cache` 配置）以及仅配置 `enable_cache_control` 的 Qwen 第三方网关」，其余句子不动。

- [ ] **Step 7: 跑测试确认通过**

Run:
```bash
venv\Scripts\python.exe -m pytest tests/test_deepseek_prompt_ledger.py -q
```
Expected: 全文件 PASS（原 13 个 + 新增 3 个 = 16 passed）。

- [ ] **Step 8: 提交**

```bash
git add core/llm_dialects.py tests/test_deepseek_prompt_ledger.py
git commit -m "feat(cache): 网关路由可配置纳入 DeepSeek 账本与 tools 规范化"
```

---

### Task 2: 改造项 ② — 会话级目录快照

**Files:**
- Modify: `core/agent_service.py:179-253`（拆渲染函数 + 新增快照存取）
- Modify: `core/agent_service.py:838`（传 `session_name`）
- Modify: `core/context_injection.py:157-174, 292-327`（透传 `session_name`）
- Modify: `core/llm_diagnostics.py:374-398`（会话重命名/清理时同步快照）
- Modify: `frontend/src/components/Subpage.vue`（技能页提示）
- Modify: `frontend/src/components/PersonaSettings.vue`（人设保存提示）
- Test: `tests/test_session_stable_snapshot.py`（新建）

**Interfaces:**
- Produces:
  - `build_stable_system_extra_sections(session_name=None) -> list[str]`
  - `_render_stable_system_extra_sections() -> list[str]`（无快照的原渲染逻辑）
  - `clear_session_stable_sections(session_name) -> None`
  - `rename_session_stable_sections(old_name, new_name) -> None`
  - `build_stable_system_message(is_gui=False, session_name=None) -> dict`
  - `build_default_injection_registry(*, is_gui=False, runtime_provider=None, session_name=None) -> ContextInjectionRegistry`
- Consumes: 任务 1 无依赖（可独立交付）。

---

- [ ] **Step 1: 写两个失败测试**

新建 `tests/test_session_stable_snapshot.py`：

```python
"""会话级稳定段落快照：会话中途的目录变更不得改变 system 头字节。"""

import pytest


@pytest.fixture(autouse=True)
def _reset_snapshot():
    """模块级快照跨用例残留会串扰，前后各清一次。"""
    import core.agent_service as agent_service

    agent_service._SESSION_STABLE_SECTIONS.clear()
    yield
    agent_service._SESSION_STABLE_SECTIONS.clear()


def _stub_prompt_sources(monkeypatch, skills_text):
    import core.agent_service as agent_service

    monkeypatch.setattr(agent_service, "load_system_prompt", lambda is_gui=False: "base")
    monkeypatch.setattr(
        "core.memory_kairos.build_memory_system_section", lambda: "memory"
    )
    monkeypatch.setattr(
        "core.skills.get_active_skills_catalog", lambda: skills_text
    )
    monkeypatch.setattr(
        "core.subagent_registry.get_custom_subagents_catalog",
        lambda: "subagents",
    )


def test_session_snapshot_freezes_system_header_bytes(monkeypatch):
    """会话中途切换技能目录：system 头字节不变，账本稳定头因此不换代。"""
    import core.agent_service as agent_service

    _stub_prompt_sources(monkeypatch, "skills-v1")
    first = agent_service.build_stable_system_message(session_name="snapshot")["content"]

    _stub_prompt_sources(monkeypatch, "skills-v2")
    second = agent_service.build_stable_system_message(session_name="snapshot")["content"]

    assert second == first
    assert "skills-v1" in second
    assert "skills-v2" not in second


def test_no_snapshot_when_session_absent_rerenders(monkeypatch):
    """无会话（Token 估算路径）时不落快照，每次都按当前目录现渲染。"""
    import core.agent_service as agent_service

    _stub_prompt_sources(monkeypatch, "skills-v1")
    agent_service.build_stable_system_message()

    _stub_prompt_sources(monkeypatch, "skills-v2")
    fresh = agent_service.build_stable_system_message()["content"]

    assert "skills-v2" in fresh
    assert agent_service._SESSION_STABLE_SECTIONS == {}


def test_snapshot_follows_session_rename_and_clear(monkeypatch):
    """重命名迁移快照（前缀不炸代），删除/清空丢弃快照（同名新会话不继承旧字节）。"""
    import core.agent_service as agent_service

    _stub_prompt_sources(monkeypatch, "skills-v1")
    first = agent_service.build_stable_system_message(session_name="before")["content"]

    agent_service.rename_session_stable_sections("before", "after")
    after = agent_service.build_stable_system_message(session_name="after")["content"]
    assert after == first

    agent_service.clear_session_stable_sections("after")
    _stub_prompt_sources(monkeypatch, "skills-v2")
    fresh = agent_service.build_stable_system_message(session_name="after")["content"]
    assert "skills-v2" in fresh
```

- [ ] **Step 2: 跑测试确认失败**

Run:
```bash
venv\Scripts\python.exe -m pytest tests/test_session_stable_snapshot.py -v
```
Expected: 3 个全部 FAIL/ERROR —— 报 `AttributeError: module 'core.agent_service' has no attribute '_SESSION_STABLE_SECTIONS'` / `TypeError: build_stable_system_message() got an unexpected keyword argument 'session_name'`。

- [ ] **Step 3: 拆出渲染函数并加快照层**

把 `core/agent_service.py` 现有 `build_stable_system_extra_sections`（第 179-227 行）的函数名改为 `_render_stable_system_extra_sections`（docstring 与函数体一字不改），紧跟其后插入以下内容。注意 `threading` 已在本文件第 21 行导入：

```python
# 会话级稳定段落快照：会话首请求定型「注入点 2」（编码规范 + 记忆索引 + 技能目录
# + 子 Agent 清单）的渲染字节，会话中途改设置/切技能/改子 Agent 不再改动 system 头，
# 因此不再触发 DeepSeek 账本换代（一次换代 = 一整条大 prompt 全价 miss）。
#
# 只进内存、不落库：进程重启后首请求重新渲染，文件无变化则渲染结果逐字节相同、
# 天然不炸代；有变化则只换代一次，符合预期。无 session_name 时完全走原渲染路径，
# 保证 tests/test_prompt_byte_stability.py 的 sha256 golden 不受影响。
_SESSION_STABLE_SECTIONS = {}
_SESSION_STABLE_SECTIONS_LOCK = threading.Lock()


def build_stable_system_extra_sections(session_name=None) -> list[str]:
    """注入点 2 的段落列表；给定会话名时按会话首请求的字节定型（会话级快照）。

    返回 list 副本，调用方对返回值的任何改动都不会污染快照本身。
    """
    key = str(session_name or "").strip()
    if key:
        with _SESSION_STABLE_SECTIONS_LOCK:
            cached = _SESSION_STABLE_SECTIONS.get(key)
        if cached is not None:
            return list(cached)
    parts = _render_stable_system_extra_sections()
    if key:
        with _SESSION_STABLE_SECTIONS_LOCK:
            _SESSION_STABLE_SECTIONS[key] = tuple(parts)
    return parts


def clear_session_stable_sections(session_name) -> None:
    """会话删除/清空历史时丢弃快照，防止同名新会话继承旧段落字节。"""
    key = str(session_name or "").strip()
    if not key:
        return
    with _SESSION_STABLE_SECTIONS_LOCK:
        _SESSION_STABLE_SECTIONS.pop(key, None)


def rename_session_stable_sections(old_name, new_name) -> None:
    """会话重命名时迁移快照，保持重命名前后请求前缀连续（账本也随会话名迁移）。"""
    old_key = str(old_name or "").strip()
    new_key = str(new_name or "").strip()
    if not old_key or not new_key or old_key == new_key:
        return
    with _SESSION_STABLE_SECTIONS_LOCK:
        value = _SESSION_STABLE_SECTIONS.pop(old_key, None)
        if value is not None:
            _SESSION_STABLE_SECTIONS[new_key] = value
```

同时更新 `_render_stable_system_extra_sections` 的 docstring：在首句后补一句「本函数是无快照的原始渲染逻辑，会话级快照包装见 `build_stable_system_extra_sections`。」

- [ ] **Step 4: `build_stable_system_message` 接受并透传 `session_name`**

把第 230 行签名与第 249-253 行的组装改成（docstring 末尾追加一段说明快照，其余不动）：

```python
def build_stable_system_message(is_gui: bool = False, session_name=None) -> dict:
```
```python
    from core.context_injection import build_default_injection_registry

    registry = build_default_injection_registry(
        is_gui=is_gui, session_name=session_name
    )
    parts = [section for section in registry.build_prefix_sections() if section]
    return {"role": "system", "content": "\n\n".join(parts)}
```

在 docstring 末尾（「…双重守护。」之后）追加：

```
    会话级快照（2026-09-23）：传入 session_name 时，注入点 2 的段落按会话首请求
    的字节定型并缓存在进程内，会话中途的目录/设置变更不再改动 system 头、不再
    触发 DeepSeek 账本换代；不传 session_name（Token 估算路径）与改造前逐字节相同。
```

- [ ] **Step 5: 注入注册表透传 `session_name`**

`core/context_injection.py` 的 `_build_stable_system_sections`（第 157-173 行）改为带参数版本：

```python
def _build_stable_system_sections(session_name=None) -> str:
    """注入点 2：通用编码规范 / 长期记忆 / 技能目录 / 子 Agent 清单段（PREFIX/SYSTEM）。

    包装 ``agent_service.build_stable_system_extra_sections``（B4 从
    ``build_stable_system_message`` 里拆出的「build_stable_prompt 之外」部分），
    按原有条件与顺序以 "\\n\\n" 连接。

    为什么必须拆分（规格 02 §3.2 步骤 2）：注入点 1（base_prompt + 决策规则）与
    注入点 2 是两条 PREFIX/SYSTEM source，渲染管线按 source 取值再拼；若本段仍返回
    ``build_stable_system_message`` 的完整 content（内含注入点 1），前缀会重复拼接
    一整份 base_prompt。拆分后两段各自完整，且分段点落在原有 "\\n\\n" 连接符上，
    join 结合律保证 ``build_stable_system_message`` 的输出逐字节不变。
    本段与 GUI/CLI 无关，故不再接收 is_gui（改造前该参数只用于读取 base_prompt）。

    ``session_name`` 透传给 ``build_stable_system_extra_sections``，用于会话级
    快照（规格见 agent_service 同名字段）；为空时走无快照的原始渲染路径。
    """
    from core.agent_service import build_stable_system_extra_sections

    return "\n\n".join(build_stable_system_extra_sections(session_name))
```

`build_default_injection_registry`（第 292-327 行）的签名加 `session_name=None`：

```python
def build_default_injection_registry(
    *,
    is_gui: bool = False,
    runtime_provider: Callable[[], "RuntimeContext"] | None = None,
    session_name: str | None = None,
) -> ContextInjectionRegistry:
```

docstring 的 `:param` 区追加：

```
    :param session_name: 注入点 2 的会话级快照键；为 None 时不使用快照
        （Token 估算、测试等无会话上下文场景），渲染字节与改造前一致。
```

注入点 2 的注册改成闭包捕获：

```python
    registry.register(InjectionSpec(
        source=SOURCE_STABLE_SYSTEM_SECTIONS,
        persistence=Persistence.PREFIX,
        channel=Channel.SYSTEM,
        section_builder=lambda: _build_stable_system_sections(session_name),
    ))
```

- [ ] **Step 6: 请求链路传 `session_name`**

`core/agent_service.py:838`：

```python
    system_message = build_stable_system_message(is_gui, session_name)
```

`core/agent_service.py:263`（`estimate_history_tokens_with_system`）保持 `build_stable_system_message(is_gui)` 不变——估算口径允许偏差，不参与线上字节。

- [ ] **Step 7: 会话重命名/清理时同步快照**

`core/llm_diagnostics.py` 的 `rename_cache_usage_session` 在 `token_usage_lock` 块**之后**追加：

```python
    try:
        from core.agent_service import rename_session_stable_sections

        rename_session_stable_sections(old_key, new_key)
    except Exception:
        logger.debug("稳定段落快照迁移失败（已忽略）", exc_info=True)
```

`clear_cache_usage_session` 在 `token_usage_lock` 块之后追加：

```python
    try:
        from core.agent_service import clear_session_stable_sections

        clear_session_stable_sections(session_key)
    except Exception:
        logger.debug("稳定段落快照清理失败（已忽略）", exc_info=True)
```

（这两个函数已是 `core/memory.py` 里会话删除/按目录删除/清空历史/重命名四条路径的统一落点，无需新增调用点。）

- [ ] **Step 8: 跑后端测试确认通过**

Run:
```bash
venv\Scripts\python.exe -m pytest tests/test_session_stable_snapshot.py tests/test_prompt_byte_stability.py tests/test_context_injection.py tests/test_agent_service.py -q
```
Expected: 全 PASS。关键守卫是 `tests/test_prompt_byte_stability.py::test_stable_system_message_has_fixed_sha256` 仍为 golden `ef286e51…`。

- [ ] **Step 9: 设置页提示文案**

`frontend/src/components/Subpage.vue`：技能页（`subType === 'skills'`）的 `desc` 之后加一条提示。在 `<div v-if="subType === 'skills'">` 内、`<p class="sub-desc">`（即 `desc` 渲染处）之后插入：

```html
          <p class="sub-note" data-test="session-snapshot-note">
            已打开的会话将在新会话中生效：进行中的会话沿用其启动时的目录快照，以保证前缀缓存稳定。
          </p>
```

在样式区（`<style scoped>`）里补：

```css
.sub-note { margin: 6px 0 0; color: var(--text-faint); font-size: 12px; }
```

`frontend/src/components/PersonaSettings.vue` 第 172 行保存成功分支：

```js
      savedTip.value = res.msg || '已保存，已打开的会话将在新会话中生效';
```

（保存失败分支 `errorMsg` 不动。）

- [ ] **Step 10: 跑前端测试确认通过**

Run:
```bash
cd frontend && npx vitest run src/components/Subpage.skills.test.ts
```
Expected: PASS（新增提示为新元素，不改动既有断言目标）。

- [ ] **Step 11: 提交**

```bash
git add core/agent_service.py core/context_injection.py core/llm_diagnostics.py \
  frontend/src/components/Subpage.vue frontend/src/components/PersonaSettings.vue \
  tests/test_session_stable_snapshot.py
git commit -m "feat(cache): 会话级目录快照避免会话中途设置变更触发账本换代"
```

---

### Task 3: 改造项 ③ — 轮换与 miss 可观测

**Files:**
- Modify: `core/llm_dialects.py:486-549`（`_prepare_deepseek_ledger_request`）
- Modify: `core/llm.py:1624, 1641-1656, 1669-1681`（把原因接到诊断）
- Modify: `core/memory.py:1323-1328`（`explicit_categories` 补归类）
- Test: `tests/test_deepseek_prompt_ledger.py`

**Interfaces:**
- Produces:
  - `_prepare_deepseek_ledger_request(...)` 的返回 dict 新增键 `"rotation_reason": str`
  - `_deepseek_header_rotation_reason(generation, current_messages, current_tools) -> str`
  - 原因取值域：`""`（未换代）/ `first_generation` / `pending_rotation` / `caller_force_new` / `header_tools_changed` / `header_system_changed` / `boundary_ambiguous`
- Consumes: 任务 1 的 `is_deepseek_ledger_route`（仅影响哪些路由会走到这里，不改本任务逻辑）。

---

- [ ] **Step 1: 写三个失败测试**

追加到 `tests/test_deepseek_prompt_ledger.py`：

```python
def test_ledger_rotation_reasons_for_first_and_header_changes():
    """换代原因必须写明：首代、system 头变化、tools 契约变化各归其因。"""
    from core.llm import _prepare_deepseek_ledger_request

    manager = _manager("reason-header")
    base = "https://api.deepseek.com"
    tools_v1 = [{"type": "function", "function": {"name": "alpha", "parameters": {}}}]

    first = _prepare_deepseek_ledger_request(
        manager, "reason-header", "openai/deepseek-v4-pro", base, "main",
        [_message("system", "stable"), _message("user", "u1")], tools_v1,
    )
    assert first["rotation_reason"] == "first_generation"

    changed_system = [
        _message("system", "changed"), _message("user", "u1"),
    ]
    second = _prepare_deepseek_ledger_request(
        manager, "reason-header", "openai/deepseek-v4-pro", base, "main",
        changed_system, tools_v1,
    )
    assert second["rotation_reason"] == "header_system_changed"

    tools_v2 = tools_v1 + [
        {"type": "function", "function": {"name": "beta", "parameters": {}}}
    ]
    third = _prepare_deepseek_ledger_request(
        manager, "reason-header", "openai/deepseek-v4-pro", base, "main",
        changed_system, tools_v2,
    )
    assert third["rotation_reason"] == "header_tools_changed"

    appended = changed_system + [_message("user", "u2")]
    fourth = _prepare_deepseek_ledger_request(
        manager, "reason-header", "openai/deepseek-v4-pro", base, "main",
        appended, tools_v2,
    )
    assert fourth["rotation_reason"] == ""


def test_ledger_rotation_reasons_for_pending_and_ambiguous_boundary():
    """主动轮换与追加边界失败也必须归因，不能与头变化混为一谈。"""
    from core.llm import (
        _prepare_deepseek_ledger_request,
        mark_deepseek_generation_rotation,
    )

    base = "https://api.deepseek.com"
    manager = _manager("reason-pending")
    seed = [_message("user", "a"), _message("user", "b")]
    _prepare_deepseek_ledger_request(
        manager, "reason-pending", "openai/deepseek-v4-pro", base, "main", seed, [],
    )

    # 追加边界不唯一：账本尾条在当前历史里出现两次，且前缀不整体匹配。
    # 种子刻意不含 system 消息——否则 _deepseek_stable_header_matches 会先以
    # header_system_changed 短路，走不到边界判定分支。
    ambiguous = [
        _message("user", "a"), _message("user", "c"),
        _message("user", "b"), _message("user", "b"),
    ]
    rotated = _prepare_deepseek_ledger_request(
        manager, "reason-pending", "openai/deepseek-v4-pro", base, "main",
        ambiguous, [],
    )
    assert rotated["rotation_reason"] == "boundary_ambiguous"

    mark_deepseek_generation_rotation("reason-pending")
    forced = _prepare_deepseek_ledger_request(
        manager, "reason-pending", "openai/deepseek-v4-pro", base, "main",
        ambiguous, [],
    )
    assert forced["rotation_reason"] == "pending_rotation"

    caller = _prepare_deepseek_ledger_request(
        manager, "reason-pending", "openai/deepseek-v4-pro", base, "main",
        ambiguous, [], force_new=True,
    )
    assert caller["rotation_reason"] == "caller_force_new"


def test_ledger_rotation_reason_reaches_cache_diagnostics(monkeypatch):
    """换代原因必须落到 cache_request_diagnostics：否则线上 miss 无法归因。"""
    import core.app_context as app_context
    import core.llm as llm
    from types import SimpleNamespace
    from copy import deepcopy

    manager = _manager("diag-rotation")
    monkeypatch.setattr(app_context, "get_manager", lambda: manager)
    monkeypatch.setattr(llm, "get_tools_schema", lambda: [])
    monkeypatch.setattr(
        "core.model_manager.get_model_protocol", lambda model, api_base=None: "openai"
    )
    monkeypatch.setattr(
        "core.model_manager.get_model_config_by_litellm_model",
        lambda model, api_base=None: {"enable_thinking": False},
    )

    def completion(**kwargs):
        delta = SimpleNamespace(
            content="answer", reasoning_content=None, tool_calls=None,
            provider_specific_fields=None,
        )
        chunk = SimpleNamespace(
            choices=[SimpleNamespace(delta=delta, finish_reason="stop")],
            usage=None,
        )
        return iter([chunk])

    monkeypatch.setattr(llm, "completion", completion)

    def turn(messages, turn_id):
        list(llm.chat_with_model_events(
            "openai/deepseek-v4-pro", "key", deepcopy(messages),
            "https://api.deepseek.com", persist=True,
            parent_session="diag-rotation", turn_id=turn_id,
            cache_session_id="diag-rotation",
            consume_background_notifications=False,
        ))

    turn([_message("system", "stable"), _message("user", "u1")], "diag-turn-1")
    turn([_message("system", "changed"), _message("user", "u1")], "diag-turn-2")

    reasons = [
        row["request_reason"]
        for row in manager.query_cache_request_diagnostics(session_id="diag-rotation")
    ]
    # 首行是账本原因而非形状差分的「冷启动」：rotation_reason 非空即覆写，口径统一。
    # 两者在 query_cache_diagnostic_summary 的 explicit_categories 里同归 cold_start，
    # 汇总口径不受影响；_account_server_usage 侧也同属"非热启动"，统计语义不变。
    assert reasons == ["first_generation", "header_system_changed"]
```

- [ ] **Step 2: 跑测试确认失败**

Run:
```bash
venv\Scripts\python.exe -m pytest tests/test_deepseek_prompt_ledger.py -k "rotation_reason" -v
```
Expected: 3 个全部 FAIL —— 前两个报 `KeyError: 'rotation_reason'`；第三个报 `AssertionError`，实际 `reasons` 为 `['冷启动', '上下文压缩或历史重写' 或 '']`（首行尚未带账本原因、第二行还是笼统的形状差分）。

- [ ] **Step 3: 让 `_prepare_deepseek_ledger_request` 记录换代原因**

在 `core/llm_dialects.py` 的 `_deepseek_stable_header_matches`（第 433 行 `return old_system == current_system`）之后插入：

```python
def _deepseek_header_rotation_reason(generation, current_messages, current_tools):
    """稳定头失配的具体归因：工具契约先于 system 头判定，与比对顺序一致。"""
    if generation["tools"] != current_tools:
        return "header_tools_changed"
    return "header_system_changed"
```

把 `_prepare_deepseek_ledger_request`（第 486-549 行）整体替换为：

```python
def _prepare_deepseek_ledger_request(
    manager, session_name, model_name, api_base, cache_scope, messages, tools,
    *, force_new=False
):
    """Build and persist the immutable request view for one DeepSeek call.

    返回值额外带 `rotation_reason`：本次是否换代、为什么换代。空串表示
    沿用了既有账本代（严格追加）。取值域见模块顶部 _LEDGER_ROTATION_REASONS
    的注释；调用方（core/llm.py）把它覆写进 cache_request_diagnostics.request_reason，
    让线上低命中能被直接归因，而不是只看到一个笼统的"上下文压缩或历史重写"。
    """
    rotation_reason = "caller_force_new" if force_new else ""
    if not force_new and consume_deepseek_generation_rotation(session_name):
        force_new = True
        rotation_reason = "pending_rotation"
    generation = manager.get_or_create_deepseek_prompt_generation(
        session_name,
        model_name,
        api_base,
        cache_scope,
        deepcopy(messages),
        deepcopy(tools),
        force_new=force_new,
    )
    if generation.get("created"):
        return {
            "generation_id": generation["id"],
            "messages": generation["messages"],
            "tools": generation["tools"],
            "rotation_reason": rotation_reason or "first_generation",
        }
    if not _deepseek_stable_header_matches(generation, messages, tools):
        rotation_reason = _deepseek_header_rotation_reason(
            generation, messages, tools
        )
        generation = manager.get_or_create_deepseek_prompt_generation(
            session_name,
            model_name,
            api_base,
            cache_scope,
            deepcopy(messages),
            deepcopy(tools),
            force_new=True,
        )
        return {
            "generation_id": generation["id"],
            "messages": generation["messages"],
            "tools": generation["tools"],
            "rotation_reason": rotation_reason,
        }
    try:
        tail = _new_tail_after_ledger_prefix(generation["messages"], messages)
    except RuntimeError:
        generation = manager.get_or_create_deepseek_prompt_generation(
            session_name,
            model_name,
            api_base,
            cache_scope,
            deepcopy(messages),
            deepcopy(tools),
            force_new=True,
        )
        return {
            "generation_id": generation["id"],
            "messages": generation["messages"],
            "tools": generation["tools"],
            "rotation_reason": "boundary_ambiguous",
        }
    if tail:
        manager.append_deepseek_prompt_messages(
            generation["id"], tail, expected_count=len(generation["messages"])
        )
        generation["messages"].extend(tail)
    return {
        "generation_id": generation["id"],
        "messages": generation["messages"],
        "tools": generation["tools"],
        "rotation_reason": "",
    }
```

在 `_deepseek_header_rotation_reason` 上方补一段取值域说明注释：

```python
# 账本换代原因取值域（写进 cache_request_diagnostics.request_reason）：
#   ""                     未换代，本次为严格追加（热路径）
#   first_generation       该会话冷启动，创建首个账本代
#   pending_rotation       主动轮换登记被消费（水位线压缩成果落库后的计划内重建）
#   caller_force_new       调用方显式 force_new（超窗压缩成功后的重试）
#   header_tools_changed   工具契约变化导致稳定头失配
#   header_system_changed  system 头变化导致稳定头失配
#   boundary_ambiguous     账本前缀无法与当前历史建立唯一追加边界
```

- [ ] **Step 4: 在 `core/llm.py` 把原因覆写进诊断**

第 1624 行 `active_deepseek_generation_id = None` 之后补一行：

```python
            active_deepseek_rotation_reason = ""
```

第 1655-1656 行 `active_deepseek_message_count = len(cleaned_messages)` 之后补一行：

```python
                        active_deepseek_rotation_reason = (
                            ledger_request.get("rotation_reason") or ""
                        )
```

第 1680 行 `request_cache_reason = request_diagnostic["request_reason"]` 之后补：

```python
                        if active_deepseek_rotation_reason:
                            # 账本换代原因比形状差分更精确（形状差分只能笼统看出
                            # "上下文压缩或历史重写"）。非空即换代：request_cache_reason
                            # 同步覆写，语义保持一致——两者都算"非热启动"，
                            # 汇总侧 explicit_categories 也已把账本原因映射到既有分类。
                            request_diagnostic["request_reason"] = (
                                active_deepseek_rotation_reason
                            )
                            request_cache_reason = active_deepseek_rotation_reason
```

- [ ] **Step 5: `explicit_categories` 补齐新原因的归类**

`core/memory.py` 第 1323-1328 行改成：

```python
        explicit_categories = {
            "冷启动": "cold_start",
            "模型或接口变化": "identity_change",
            "工具契约变化": "tool_contract_change",
            "上下文压缩或历史重写": "client_history_rewrite",
            # 账本换代原因（core/llm_dialects._deepseek_header_rotation_reason 取值域）
            "first_generation": "cold_start",
            "pending_rotation": "client_history_rewrite",
            "caller_force_new": "client_history_rewrite",
            "header_tools_changed": "tool_contract_change",
            "header_system_changed": "client_history_rewrite",
            "boundary_ambiguous": "client_history_rewrite",
        }
```

- [ ] **Step 6: 跑测试确认通过**

Run:
```bash
venv\Scripts\python.exe -m pytest tests/test_deepseek_prompt_ledger.py tests/test_cache_diagnostics_storage.py -q
```
Expected: 全 PASS（含既有 `test_cache_diagnostic_summary_classifies_evidence_without_guessing_provider` 的精确 categories 断言不受影响）。

- [ ] **Step 7: 提交**

```bash
git add core/llm_dialects.py core/llm.py core/memory.py tests/test_deepseek_prompt_ledger.py
git commit -m "feat(cache): 账本换代原因写入诊断，miss 可直接归因"
```

---

### Task 4: 实测、留档与设计文档回填

**Files:**
- Create: `docs/adapt-report-deepseek-tokenrhythm-cache.md`
- Modify: `docs/superpowers/specs/2026-09-23-deepseek-cache-hit-rate-design.md`
- Read (不改): `bench/adapt_bench.py`、`~/.ycode/config.json`

**Interfaces:**
- Consumes: Task 1-3 的代码；`~/.ycode/config.json` 中「基元律动」provider 的 key（`https://tokenrhythm.studio/v1`，模型 `openai/deepseek-v4-flash-0731` / `openai/deepseek-v4-pro-0813` / `openai/deepseek-flash`）。

---

- [ ] **Step 1: 改造前基线（在 Task 1 提交之前跑；若已错过，先 `git stash` 回到 419f14e 再跑，跑完 `git stash pop`）**

Run（key 用命令替换从 config.json 就地读取，不落盘、不打印、不进命令历史）：

```bash
venv/Scripts/python.exe bench/adapt_bench.py --model openai/deepseek-v4-flash-0731 \
  --api-base https://tokenrhythm.studio/v1 \
  --api-key "$(venv/Scripts/python.exe -c "import json,os;d=json.load(open(os.path.expanduser('~/.ycode/config.json'),encoding='utf-8'));print([x for x in d['providers'] if x.get('base_url')=='https://tokenrhythm.studio/v1'][0]['api_key'])")" \
  --rounds 3 --tool-rounds 3 --context 3000 --out mytemp/bench_tr_before.json
```
Expected: 9 次调用全部成功，输出逐轮明细与总体命中率；`mytemp/bench_tr_before.json` 落盘。失败即停止并把错误原文报给用户（key 失效/网关不可达属外部因素，不由代码改动解决）。

- [ ] **Step 2: 改造后复测**

Task 1-3 全部提交后，重复 Step 1，输出到 `mytemp/bench_tr_after.json`。

Expected: 与 before 基本相同（差异只来自上游抖动）。**这是预期结果，不是失败**——该脚本绕开 Prompt Ledger，测的是网关自身的前缀缓存上限（见「摸底结论 3」）。

- [ ] **Step 3: 记录线上历史基线（改造前数据）**

Run（两个脚本只读 `~/.ycode/ycode.db`，输出可直接作为线上观测的对照基线）：

```bash
venv/Scripts/python.exe ~/.ycode/tmp/deepseek_hitrate.py
venv/Scripts/python.exe ~/.ycode/tmp/deepseek_routes.py
```
Expected: 前者输出按模型/按日命中率，后者输出按 `api_base` 拆分。把两份输出原文贴进实测报告的「§6 线上历史基线」小节，并标注采集时间与样本量。

- [ ] **Step 4: 写实测报告**

新建 `docs/adapt-report-deepseek-tokenrhythm-cache.md`，结构对齐 `docs/adapt-report-deepseek-v4-flash.md`：

```markdown
# TokenRhythm 网关 DeepSeek 缓存命中率实测报告

> 生成时间：2026-09-23 | 测试对象：TokenRhythm（基元律动）网关的 DeepSeek 系列 | 测试方：Y-code 编码 agent（星瑶）

## 1. 测试目的
1. 确认 TokenRhythm 网关确实返回服务端前缀缓存用量（回答设计文档 §5 风险表第 1 行）；
2. 给出该网关的**上游缓存上限**，作为线上命中率验收的参照天花板。

## 2. 方法与口径
| 项目 | 值 |
|---|---|
| 脚本 | bench/adapt_bench.py（真实 API，stream=False） |
| 端点 | https://tokenrhythm.studio/v1 |
| 模型 | openai/deepseek-v4-flash-0731 |
| 参数 | --rounds 3 --tool-rounds 3 --context 3000 |
| 数据 | mytemp/bench_tr_before.json、mytemp/bench_tr_after.json |

## 3. 结果
<!-- 先跑这条取数命令，再把输出逐行抄成下面两类表： -->
<!--
venv/Scripts/python.exe -c "
import json
for f in ('mytemp/bench_tr_before.json', 'mytemp/bench_tr_after.json'):
    d = json.load(open(f, encoding='utf-8'))
    print('==', f, 'overall', d['summary']['overall_cache_hit_rate'])
    print('   chat', d['summary']['chat'])
    print('   tool', d['summary']['tool'])
    for row in d['details']:
        print('   ', row['scenario'], row['round'], row['step'],
              row['prompt_tokens'], row['completion_tokens'],
              row['cached_tokens'], row['cache_hit_rate'])
"
-->
<!-- 表一：场景一/场景二逐轮明细（场景|轮次|步骤|prompt|completion|cached|命中率） -->
<!-- 表二：总体统计（调用次数|prompt|completion|cached|total|总体命中率），before/after 各一列 -->
<!-- 表格样式抄 docs/adapt-report-deepseek-v4-flash.md §3.1 -->

## 4. 关键局限（必读）
adapt_bench.py 直接调用 core.llm.completion，**不经过 core.llm.chat_with_model_events，
因此不经过 DeepSeek Prompt Ledger**。所以本报告的数字：
- 反映的是上游网关自身的前缀缓存能力（实测有效，见 §3）；
- 不能用来度量本次「路由可配置化 + 目录快照 + 换代可观测」改造的收益。
改造收益的证据来自：① `tests/test_deepseek_prompt_ledger.py` 中配置路由进账本与
tools 排序的测试；② `tests/test_session_stable_snapshot.py` 的快照不换代测试；
③ 线上 `token_usage` 表按 `api_base = https://tokenrhythm.studio/v1` 的按日命中率。

## 5. 结论
<!-- 必须回答以下三问，每问一句、带 §3 的数字或命令出处： -->
<!-- 1. TokenRhythm 网关是否确认返回 cached_tokens（是/否 + before 首个热轮命中率）？ -->
<!-- 2. before 与 after 是否一致（一致/不一致 + 两者 overall_cache_hit_rate）？ -->
<!--    若一致，必须写明这与 §4 的局限一致，属预期，不是改造无效的证据。 -->
<!-- 3. 本次改造的收益要用什么口径验收（线上的哪个表/哪个字段/观察几天）？ -->
```

- [ ] **Step 4: 回填设计文档**

在 `docs/superpowers/specs/2026-09-23-deepseek-cache-hit-rate-design.md` 末尾追加：

```markdown
---

## 8. 实施结论（2026-09-23 回填）

### 8.1 三项改造落地情况
| 改造项 | 落点 | 状态 |
|---|---|---|
| ① 缓存优化路由可配置化 | `core/llm_dialects.py` `_is_cache_optimized_deepseek_route` | 已实施 |
| ② 会话级目录快照 | `core/agent_service.py` `build_stable_system_extra_sections(session_name)` | 已实施 |
| ③ 轮换可观测 | `_prepare_deepseek_ledger_request` → `rotation_reason` → `cache_request_diagnostics` | 已实施 |

### 8.2 与设计稿的事实修正
1. §3.1-8「cache_request_diagnostics 当前 deepseek 家族零记录」不成立：实测该表
   1087 行中 deepseek 435 行，`api_base` 全部为 `https://tokenrhythm.studio/v1`。
   写入路径本就是无路由门禁的，③ 无需新增发射开关，实际缺口只是换代原因未落到
   `request_reason`。
2. §4.3 的换代原因取值域需补第 6 个 `caller_force_new`（`core/llm.py` 超窗压缩
   重试分支主动 `force_new=True`），否则该分支会被误标。
3. `bench/adapt_bench.py` 绕开 Prompt Ledger（直接调 `core.llm.completion`），不能
   度量本次改造收益，只能测上游网关上限；详见 `docs/adapt-report-deepseek-tokenrhythm-cache.md` §4。

### 8.3 验证证据

| 验收项 | 命令 | 结果 |
|---|---|---|
| 配置路由进账本 | `pytest tests/test_deepseek_prompt_ledger.py -k configured_deepseek_cache_route -q` | 抄录实际通过数 |
| 目录快照不换代 | `pytest tests/test_session_stable_snapshot.py -q` | 抄录实际通过数 |
| 换代原因诊断 | `pytest tests/test_deepseek_prompt_ledger.py -k rotation_reason -q` | 抄录实际通过数 |
| 相关测试全量 | `pytest tests/test_deepseek_prompt_ledger.py tests/test_session_stable_snapshot.py tests/test_prompt_byte_stability.py tests/test_context_injection.py tests/test_qwen_cache.py tests/test_cache_diagnostics_storage.py tests/test_agent_service.py -q` | 抄录 `N passed` 与耗时 |
| 全量回归 | `pytest -q` | 抄录 `N passed`，注明与改动前基线的差异 |
| 字节日志未破坏 | `pytest tests/test_prompt_byte_stability.py -q` | golden 值应为 `ef286e51…` 未变 |
| 网关实测 | `bench/adapt_bench.py`（Task 4 Step 1/2） | 抄录 before/after 的总体命中率，并指向报告 §4 的局限说明 |

上表每一行都必须填命令的**实际输出数字**，不得写「通过」「正常」这类无数字结论。

### 8.4 上线动作（运维）
config.json 增加：`"deepseek_cache_routes": ["tokenrhythm.studio"]`。
生效后 3~7 天用 `~/.ycode/tmp/deepseek_hitrate.py`（按模型/按日命中率）与
`~/.ycode/tmp/deepseek_routes.py`（按 api_base 拆分）对照 §1.2 目标验收；
低命中事件按 `cache_request_diagnostics.request_reason` 的换代原因分布归因
（`first_generation` / `pending_rotation` / `caller_force_new` /
`header_tools_changed` / `header_system_changed` / `boundary_ambiguous`）。

- [ ] **Step 5: 提交**

`mytemp/` 已被 `.gitignore` 第 31 行忽略（scratch 目录，不入库），因此 before/after 原始
JSON 只作为本地证据留在 `mytemp/`，**实测数字逐行抄进报告**后随报告一起入库：

```bash
git add docs/adapt-report-deepseek-tokenrhythm-cache.md \
  docs/superpowers/specs/2026-09-23-deepseek-cache-hit-rate-design.md
git commit -m "docs(cache): TokenRhythm 实测报告与设计文档验证结论"
```

---

### Task 5: 完成前验证

**Files:** 无改动，只跑命令。

- [ ] **Step 1: 相关测试全量**

Run:
```bash
venv\Scripts\python.exe -m pytest tests/test_deepseek_prompt_ledger.py tests/test_session_stable_snapshot.py tests/test_prompt_byte_stability.py tests/test_context_injection.py tests/test_qwen_cache.py tests/test_cache_diagnostics_storage.py tests/test_agent_service.py -q
```
Expected: 全 PASS，0 failed。若 `test_prompt_byte_stability` 的 golden 变了，说明快照路径泄漏进了无快照场景，回到 Task 2 Step 3 检查。

- [ ] **Step 2: 全量回归**

Run:
```bash
venv\Scripts\python.exe -m pytest -q
```
Expected: 0 failed（与改动前基线对比，不得新增失败）。

- [ ] **Step 3: 语法与前端**

Run:
```bash
venv\Scripts\python.exe -m py_compile core/llm_dialects.py core/agent_service.py core/context_injection.py core/llm.py core/llm_diagnostics.py core/memory.py
cd frontend && npx vitest run src/components/Subpage.skills.test.ts
```
Expected: py_compile 退出码 0；前端测试 PASS。

- [ ] **Step 4: 验收清单逐条核对**

| 验收项 | 证据 |
|---|---|
| 配置路由进账本 | `test_configured_deepseek_cache_route_enables_ledger` PASS |
| 配置路由 tools 排序 | `test_configured_deepseek_cache_route_sorts_tools` PASS |
| 目录快照不换代 | `test_session_snapshot_freezes_system_header_bytes` PASS |
| 换代原因诊断 | `test_ledger_rotation_reasons_*` + `test_ledger_rotation_reason_reaches_cache_diagnostics` PASS |
| 字节稳定性未破坏 | `test_stable_system_message_has_fixed_sha256` golden 未变 |
| 实测报告留档 | `docs/adapt-report-deepseek-tokenrhythm-cache.md` 存在且含 before/after |

- [ ] **Step 5: 报告交付**

向用户汇报：改动的文件清单、每项验证命令的实际输出、实测数字、已知局限（adapt_bench 绕开账本；`deepseek_cache_routes` 需用户自己写进 config.json）、以及线上观测建议（3~7 天后看 `token_usage` 按日命中率与 `cache_request_diagnostics` 归因分布）。

---

## 风险与回滚

| 风险 | 表现 | 处置 |
|---|---|---|
| 快照泄漏进无快照路径 | `test_prompt_byte_stability` golden 失败 | Task 2 Step 3 的自查点；`session_name` 为空时必须完全走 `_render_stable_system_extra_sections` |
| 快照与账本不同步 | 会话改名后 system 头突变 | `rename_session_stable_sections` 与 `rename_cache_usage_session` 同点调用；`test_snapshot_follows_session_rename_and_clear` 守护 |
| `deepseek_cache_routes` 误配非 DeepSeek 上游 | 无谓改变请求形状 | 配置由用户手工写入；文档与注释双重写明「仅 DeepSeek 磁盘缓存上游」 |
| 换代原因覆盖掉形状差分信息 | 归因变粗 | 只在换代时覆盖（`rotation_reason` 非空），未换代的严格追加仍保留原差分原因 |
| 三项改造耦合 | 回滚困难 | 每项独立提交、独立可回滚；② 不依赖 ①③，③ 不依赖 ①② |
