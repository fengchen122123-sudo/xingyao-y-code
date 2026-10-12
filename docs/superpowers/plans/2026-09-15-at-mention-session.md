# @ 提及对话（Session Mention）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 输入框中输入 `@` 时，补全面板在「文件」之外新增「对话」候选；选中后插入 `@{会话名}` 标记，发送时把被引用对话的内容（记忆摘要优先 + 近期消息，限额截断）注入本轮 LLM 上下文。

**Architecture:** 前端复用现有 useSuggest/InputSuggest 补全体系新增会话候选组；后端在 `parse_user_input` 之前用独立函数 `extract_session_refs` 摘出 `@{...}` 标记（不改变 parse_user_input 返回契约）；引用内容在 `prepare_turn_messages` 中组装为上下文后缀块，复用现有「动态上下文/后台汇总/时间注记」的内联 + 剥离机制（CAS 持久化、展示层剥离）。

**Tech Stack:** Python 3 后端（pytest）、Vue 3 + TypeScript 前端（vitest + vue-tsc）、pywebview JS 桥。

## Global Constraints

- 引用标记格式固定为 `@{会话名}`（`}` 定界，会话名内禁止出现 `{`/`}`，插入时剥离）。
- `parse_user_input(text, explicit_paths, session_id)` 返回契约保持 2 元组 `(content, attached_msgs)` 不变。
- 引用内容硬上限：单会话 6000 字符，总计 15000 字符（后缀注入发生在 token 估算之后，必须自行限额）。
- 跳过对当前会话的自引用；引用不存在的会话时在块内如实标注「已跳过」，不报错。
- v1 范围：GUI + CLI 本地入口；LAN 消息中的 `@{...}` 原样保留不解析。
- @ 补全查询词不支持空格（沿用现有 `[^\s@]*` 语义，会话标题靠子串匹配命中）。
- 所有面向用户的文案使用中文。
- 工作目录：`D:\fengchenhao\ycode\xingyao-y-code`；命令解释器 PowerShell 7+。

## 现状关键事实（探索结论）

- 前端补全：`frontend/src/composables/useSuggest.ts`（`detectToken` 识别 `@`，`fetchFileItems` 走 `api.search_workspace_files` 防抖 150ms）；面板组件 `frontend/src/components/InputSuggest.vue`（`SuggestItem {key, icon, label, desc, apply}`）；`InputArea.vue` 与 `KimiEmptyState.vue` 共用 useSuggest，改一处两处生效。
- 后端 @文件 解析：`core/input_parser.py:191 parse_user_input`，@ 文件正则 `@([a-zA-Z0-9_\-\.\/\\]+)`——`{` 不在字符集内，`@{...}` 与之天然不冲突。
- 会话存储：`core/memory.py MemoryManager`；`get_all_sessions(workdir=None, source=None, include_empty=False)` 返回 `[{id, name, count, workdir, created_at, updated_at, source}]`；`get_history(session_name)` 返回消息列表（content 可能为 JSON 字符串/多媒体 list）；`sessions.memory_summary` 字段存压缩摘要。
- 上下文注入点：`core/agent_service.py:604 prepare_turn_messages`，末尾 `:848 _append_context_suffix_to_last_user(history, dynamic_prompt, summary_msg, time_note, manager, session_name)` 内联到末条 user 消息并 CAS 持久化；`:335 strip_context_suffix` 按 `TIME_NOTE_PREFIX / _BG_SUMMARY_PREFIX / DYNAMIC_CONTEXT_PREFIX` 从末尾反向剥离（GUI 气泡 `gui_bridge.py:2360` 与 CLI 回放 `core/history_renderer.py:76` 均已复用）。
- 发送链路：GUI worker `gui_bridge.py:5039 parse_user_input` → `:5072 add_message` → `:5109 prepare_turn_messages`（内部回合 `internal_kind` 非空时跳过解析，refs 天然为空）；CLI `main.py:4786 / :4801 parse_user_input`、`:4853 prepare_turn_messages`。
- 测试基建：后端 `MemoryManager(":memory:")` 可直接内存构造（见 `tests/test_agent_service.py:70 _make_manager`）；`tests/test_input_parser.py` 已存在；前端 `frontend/src/composables/useSuggest.test.ts` 已存在，`npm run test` = vitest，`npm run build` 含 vue-tsc 类型检查。

---

### Task 1: 后端 — 引用标记提取 `extract_session_refs`

**Files:**
- Modify: `core/input_parser.py`（文件头部常量区 + 文件末尾新增函数）
- Test: `tests/test_input_parser.py`（追加用例）

**Interfaces:**
- Produces: `extract_session_refs(text: str) -> tuple[str, list[str]]`，返回（清理后的文本, 去重保序的会话名列表）。Task 4 的 GUI/CLI 发送链路消费它。

- [x] **Step 1: 写失败测试**

在 `tests/test_input_parser.py` 末尾追加：

```python
def test_extract_session_refs_basic():
    from core.input_parser import extract_session_refs
    cleaned, refs = extract_session_refs("参考 @{登录bug排查} 的结论继续")
    assert refs == ["登录bug排查"]
    assert "@{" not in cleaned
    assert "参考" in cleaned and "的结论继续" in cleaned


def test_extract_session_refs_none_and_unclosed():
    from core.input_parser import extract_session_refs
    assert extract_session_refs("没有引用") == ("没有引用", [])
    # 未闭合 / 空内容 / 跨行都不提取
    assert extract_session_refs("@{没闭合") == ("@{没闭合", [])
    assert extract_session_refs("@{}") == ("@{}", [])
    assert extract_session_refs("@{跨\n行}") == ("@{跨\n行}", [])


def test_extract_session_refs_dedup_and_multi():
    from core.input_parser import extract_session_refs
    cleaned, refs = extract_session_refs("@{甲} 和 @{乙}，再说 @{甲}")
    assert refs == ["甲", "乙"]


def test_extract_session_refs_coexists_with_at_file(tmp_path):
    from core.input_parser import extract_session_refs
    f = tmp_path / "config.py"
    f.write_text("x=1", encoding="utf-8")
    text = f"@{{我的对话}} 看一下 @{f.name}"
    cleaned, refs = extract_session_refs(text)
    assert refs == ["我的对话"]
    assert f"@{f.name}" in cleaned  # @文件 标记原样保留给 parse_user_input
```

- [x] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_input_parser.py -q -k session_refs`
Expected: FAIL（`ImportError: cannot import name 'extract_session_refs'`）

- [x] **Step 3: 实现**

`core/input_parser.py` 头部常量区追加：

```python
# @{会话名} 引用标记：} 定界，不允许跨行，标题上限 80 字符
SESSION_REF_PATTERN = re.compile(r"@\{([^}\n]{1,80})\}")
```

文件末尾追加：

```python
def extract_session_refs(text):
    """提取文本中的 @{会话名} 引用标记，返回 (清理后的文本, 会话名列表)。

    会话名去重保序；所有 @{...} 标记均从文本中移除。
    与 @文件 解析互不干扰：文件正则字符集不含 { }。
    须在 parse_user_input 之前调用。
    """
    if not text:
        return text, []
    refs = []

    def _sub(m):
        name = m.group(1).strip()
        if name and name not in refs:
            refs.append(name)
        return ""

    cleaned = SESSION_REF_PATTERN.sub(_sub, text)
    return cleaned.strip(), refs
```

- [x] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_input_parser.py -q`
Expected: PASS（含既有用例不回归）

- [x] **Step 5: Commit**

```powershell
git add core/input_parser.py tests/test_input_parser.py
git commit -m "feat: add extract_session_refs for @-mention session tokens"
```

---

### Task 2: 后端 — 引用内容组装 `build_referenced_sessions_block`

**Files:**
- Modify: `core/memory.py`（新增 `get_session_memory_summary` 小方法）
- Modify: `core/agent_service.py`（常量区 + 模块级新函数，放在 `build_background_task_summary` 之后）
- Test: `tests/test_agent_service.py`（追加用例）

**Interfaces:**
- Consumes: Task 1 的 `extract_session_refs` 产出的会话名列表。
- Produces:
  - `MemoryManager.get_session_memory_summary(session_name: str) -> str`（无行返回 `""`）
  - `core.agent_service.REF_SESSIONS_PREFIX`（常量，Task 3 剥离逻辑引用）
  - `build_referenced_sessions_block(manager, ref_names, current_session=None) -> str`（无引用返回 `""`）

- [x] **Step 1: 写失败测试**

在 `tests/test_agent_service.py` 末尾追加（复用文件内 `_make_manager()`）：

```python
def test_build_referenced_sessions_block_content():
    from core.agent_service import build_referenced_sessions_block, REF_SESSIONS_PREFIX
    mgr = _make_manager()
    mgr.ensure_session("目标对话")
    mgr.add_message("目标对话", "user", "登录接口报 500 怎么查")
    mgr.add_message("目标对话", "assistant", "先看 gateway 日志，再查鉴权中间件")
    block = build_referenced_sessions_block(mgr, ["目标对话"], current_session="当前对话")
    assert block.startswith(REF_SESSIONS_PREFIX)
    assert "目标对话" in block
    assert "登录接口报 500" in block
    assert "先看 gateway 日志" in block


def test_build_referenced_sessions_block_skip_and_missing():
    from core.agent_service import build_referenced_sessions_block
    mgr = _make_manager()
    mgr.ensure_session("当前对话")
    block = build_referenced_sessions_block(
        mgr, ["当前对话", "不存在的对话"], current_session="当前对话"
    )
    assert "已跳过" in block  # 自引用与不存在的会话都如实标注，不抛错


def test_build_referenced_sessions_block_empty_refs():
    from core.agent_service import build_referenced_sessions_block
    assert build_referenced_sessions_block(_make_manager(), []) == ""


def test_get_session_memory_summary(tmp_path):
    from core.memory import MemoryManager
    mgr = MemoryManager(str(tmp_path / "t.db"))
    mgr.ensure_session("s1")
    assert mgr.get_session_memory_summary("s1") == ""
    assert mgr.get_session_memory_summary("不存在") == ""
```

- [x] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_agent_service.py -q -k "referenced_sessions_block or session_memory_summary"`
Expected: FAIL（`ImportError` / `AttributeError: 'MemoryManager' object has no attribute 'get_session_memory_summary'`）

- [x] **Step 3: 实现**

`core/memory.py` 在 `get_all_sessions` 附近新增：

```python
def get_session_memory_summary(self, session_name):
    """读取会话的记忆压缩摘要；会话不存在或无摘要返回空串。"""
    with self._lock:
        row = self._conn.execute(
            "SELECT memory_summary FROM sessions WHERE session_name = ?",
            (session_name,),
        ).fetchone()
    if not row:
        return ""
    return str(row[0] or "").strip()
```

（注意：实际字段名/连接属性名以 `core/memory.py` 内现有 `get_all_sessions` 的实现为准，照其写法访问。）

`core/agent_service.py` 常量区（`TIME_NOTE_PREFIX` 等常量附近）追加：

```python
# @ 提及对话：注入到末条 user 消息末尾的引用块前缀（strip_context_suffix 按它剥离）
REF_SESSIONS_PREFIX = "【引用的对话记录】"
_REF_SESSION_MAX_CHARS = 6000    # 单会话内容上限
_REF_TOTAL_MAX_CHARS = 15000     # 全部引用总上限
_REF_RECENT_MSG_LIMIT = 20       # 无摘要时回溯的近期消息条数
_REF_MSG_MAX_CHARS = 800         # 单条消息上限
```

`build_background_task_summary` 之后新增：

```python
def _digest_one_session(manager, name, info):
    """组装单个被引用会话的紧凑摘要：记忆摘要优先，再补近期 user/assistant 文本消息。

    跳过 tool 角色与多媒体 content；user 文本先 strip_context_suffix 去掉当轮
    运行上下文，避免把别个会话的模型/时间快照带进来。总长硬限 _REF_SESSION_MAX_CHARS。
    """
    lines = []
    updated = str(info.get("updated_at") or "")[:16]
    header = f"── 对话「{name}」（{info.get('count', 0)} 条消息，更新于 {updated or '未知'}）──"
    summary = ""
    try:
        summary = manager.get_session_memory_summary(name)
    except Exception:
        summary = ""
    if summary:
        lines.append("[记忆摘要] " + summary[:2000])
    try:
        history = manager.get_history(name) or []
    except Exception:
        history = []
    recent = []
    for msg in reversed(history):
        if len(recent) >= _REF_RECENT_MSG_LIMIT:
            break
        role = msg.get("role")
        if role not in ("user", "assistant"):
            continue
        content = msg.get("content")
        if not isinstance(content, str):
            continue  # 多媒体 content（list）跳过
        if role == "user":
            content = strip_context_suffix(content)
        content = content.strip()
        if not content:
            continue
        label = "用户" if role == "user" else "助手"
        recent.append(f"{label}: {content[:_REF_MSG_MAX_CHARS]}")
    if recent:
        lines.append("[近期对话]")
        lines.extend(reversed(recent))
    body = header + "\n" + "\n".join(lines) if lines else header + "\n（无可展示的文本消息）"
    if len(body) > _REF_SESSION_MAX_CHARS:
        body = body[:_REF_SESSION_MAX_CHARS] + "\n...（已截断）"
    return body


def build_referenced_sessions_block(manager, ref_names, current_session=None):
    """把 @{会话名} 引用的对话内容组装为上下文后缀块文本。

    注入发生在 token 估算之后，必须自行限额（_REF_TOTAL_MAX_CHARS）。
    自引用与不存在的会话如实标注「已跳过」，绝不抛错影响主流程。
    """
    if not ref_names:
        return ""
    try:
        existing = {
            str(s.get("name") or ""): s
            for s in manager.get_all_sessions(workdir=None, include_empty=True)
        }
    except Exception:
        existing = {}
    intro = (
        REF_SESSIONS_PREFIX
        + "用户通过 @ 引用了以下历史对话供你参考，可将其作为上下文依据，"
          "但不要声称自己亲历过这些对话："
    )
    parts = [intro]
    total = len(intro)
    skipped = []
    for name in ref_names:
        if current_session and name == current_session:
            skipped.append(f"「{name}」是当前会话，已跳过")
            continue
        info = existing.get(name)
        if info is None:
            skipped.append(f"「{name}」不存在或已删除，已跳过")
            continue
        digest = _digest_one_session(manager, name, info)
        if total + len(digest) > _REF_TOTAL_MAX_CHARS:
            skipped.append(f"「{name}」因总长度超限已省略")
            continue
        parts.append(digest)
        total += len(digest)
    parts.extend(skipped)
    return "\n\n".join(parts)
```

- [x] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_agent_service.py -q -k "referenced_sessions_block or session_memory_summary"`
Expected: PASS

- [x] **Step 5: Commit**

```powershell
git add core/memory.py core/agent_service.py tests/test_agent_service.py
git commit -m "feat: build referenced-session digest block with hard caps"
```

---

### Task 3: 后端 — 注入链路（prepare_turn_messages / 后缀内联 / 展示剥离）

**Files:**
- Modify: `core/agent_service.py`（`strip_context_suffix`、`_append_context_suffix_to_last_user`、`prepare_turn_messages` 三处）
- Test: `tests/test_agent_service.py`（追加用例）

**Interfaces:**
- Consumes: Task 2 的 `build_referenced_sessions_block`、`REF_SESSIONS_PREFIX`。
- Produces: `prepare_turn_messages(..., referenced_sessions=None)` 新关键字参数（Task 4 的 GUI/CLI 调用方传入）；后缀顺序固定为 `动态块 → 引用块 → 后台汇总 → 时间注记`。

- [x] **Step 1: 写失败测试**

在 `tests/test_agent_service.py` 末尾追加：

```python
def test_strip_context_suffix_strips_ref_block():
    from core.agent_service import strip_context_suffix, REF_SESSIONS_PREFIX
    content = "原始问题" + "\n\n" + REF_SESSIONS_PREFIX + "（引用内容……）"
    assert strip_context_suffix(content) == "原始问题"


def test_prepare_turn_messages_injects_ref_block():
    from core.agent_service import (
        prepare_turn_messages, strip_context_suffix, REF_SESSIONS_PREFIX,
    )
    mgr = _make_manager()
    mgr.ensure_session("被引用")
    mgr.add_message("被引用", "user", "旧结论：根因是鉴权中间件")
    mgr.ensure_session("当前会话")
    mgr.add_message("当前会话", "user", "参考 @{被引用} 继续排查")
    history = prepare_turn_messages(
        mgr, "当前会话", "test-model", 128000, permission_mode="full",
    )
    last_user = [m for m in history if m.get("role") == "user"][-1]
    assert REF_SESSIONS_PREFIX in last_user["content"]
    assert "旧结论：根因是鉴权中间件" in last_user["content"]
    # 展示层剥离后只剩用户原文
    assert strip_context_suffix(last_user["content"]) == "参考 @{被引用} 继续排查"
```

（注：若 `prepare_turn_messages` 在测试环境因读取配置/模型信息报错，参照本文件既有用例的 monkeypatch 方式补齐最小桩，不改生产代码签名。）

- [x] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_agent_service.py -q -k "ref_block"`
Expected: FAIL（`TypeError: prepare_turn_messages() got an unexpected keyword argument 'referenced_sessions'`；strip 用例因前缀未登记而失败）

- [x] **Step 3: 实现**

`strip_context_suffix`（agent_service.py:335）的 while 循环中，在「后台汇总块」分支与「动态运行上下文块」分支之间插入：

```python
        # @ 提及对话引用块（追加顺序在后台汇总之前，剥离从末尾反向进行）
        marker = "\n\n" + REF_SESSIONS_PREFIX
        idx = text.rfind(marker)
        if idx != -1:
            text = text[:idx]
            changed = True
            continue
```

`_append_context_suffix_to_last_user`（agent_service.py:379）签名与组装改为：

```python
def _append_context_suffix_to_last_user(
    history, dynamic_prompt, summary_msg, time_note, manager, session_name,
    ref_block="",
):
```

函数体中 `suffix_parts` 一行改为（顺序即持久化顺序，与剥离顺序镜像）：

```python
    suffix_parts = [p for p in (dynamic_prompt, ref_block, summary_msg, time_note) if p]
```

`prepare_turn_messages`（agent_service.py:604）签名追加关键字参数：

```python
    turn_source: str = "local",
    referenced_sessions: Optional[list] = None,
```

在 `summary_msg = ...` 赋值块（agent_service.py:843-847）之后追加：

```python
    try:
        ref_block = build_referenced_sessions_block(
            manager, referenced_sessions or [], session_name
        )
    except Exception:
        logger.warning("引用对话块构建失败，已跳过", exc_info=True)
        ref_block = ""
```

`_append_context_suffix_to_last_user(...)` 调用（agent_service.py:848-850）改为：

```python
    _append_context_suffix_to_last_user(
        history, dynamic_prompt, summary_msg, time_note, manager, session_name,
        ref_block=ref_block,
    )
```

- [x] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_agent_service.py -q`
Expected: PASS（含既有用例不回归）

- [x] **Step 5: Commit**

```powershell
git add core/agent_service.py tests/test_agent_service.py
git commit -m "feat: inject referenced-session block into turn context suffix"
```

---

### Task 4: 后端 — 桥接 `search_sessions` + GUI/CLI 发送链路透传

**Files:**
- Modify: `gui_bridge.py`（`search_workspace_files` 后新增 `search_sessions`；worker 内 5031-5044 解析段与 5109 `prepare_turn_messages` 调用）
- Modify: `main.py`（4775-4801 两处解析点与 4853 附近 `prepare_turn_messages` 调用）
- Test: `tests/test_agent_service.py` 或新增 `tests/test_session_mention_bridge.py`

**Interfaces:**
- Consumes: Task 1 `extract_session_refs`；Task 3 `prepare_turn_messages(referenced_sessions=...)`。
- Produces: `GuiBridge.search_sessions(query: str = "", limit: int = 8) -> {"ok": bool, "sessions": [{"name", "count", "updated_at", "workdir"}]}`（Task 5 前端桥声明与之对应）。

- [x] **Step 1: 写失败测试**

新增 `tests/test_session_mention_bridge.py`：

```python
def test_search_sessions_filters_and_excludes_current(tmp_path):
    from core.memory import MemoryManager
    from gui_bridge import GuiBridge
    bridge = GuiBridge.__new__(GuiBridge)  # 裸构造，只挂本测试用到的属性
    bridge._memory = MemoryManager(str(tmp_path / "t.db"))
    bridge._current_session = "当前会话"
    bridge._memory.ensure_session("当前会话")
    bridge._memory.ensure_session("登录bug排查")
    bridge._memory.add_message("登录bug排查", "user", "hi")
    bridge._memory.ensure_session("发布流程梳理")
    bridge._memory.add_message("发布流程梳理", "user", "hi")

    res = bridge.search_sessions("登录")
    names = [s["name"] for s in res["sessions"]]
    assert names == ["登录bug排查"]

    res_all = bridge.search_sessions("")
    names_all = [s["name"] for s in res_all["sessions"]]
    assert "当前会话" not in names_all  # 排除当前会话
    assert set(names_all) == {"登录bug排查", "发布流程梳理"}


def test_search_sessions_error_path_returns_empty():
    from gui_bridge import GuiBridge
    bridge = GuiBridge.__new__(GuiBridge)
    bridge._memory = None
    bridge._current_session = ""
    res = bridge.search_sessions("x")
    assert res["ok"] is False and res["sessions"] == []
```

（注：`GuiBridge.__new__` 裸构造的可用属性以 `search_workspace_files` 既有测试的写法为准——参考 `tests/test_workspace_suggest_root.py` 的构造模式，若该文件已有可复用的桥夹具则直接复用。）

- [x] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_session_mention_bridge.py -q`
Expected: FAIL（`AttributeError: 'GuiBridge' object has no attribute 'search_sessions'`）

- [x] **Step 3: 实现**

`gui_bridge.py` 在 `search_workspace_files`（:2688）之后新增：

```python
    def search_sessions(self, query: str = "", limit: int = 8) -> Dict[str, Any]:
        """@ 提及对话自动补全：会话名大小写不敏感子串匹配（排除当前会话）。"""
        try:
            if self._memory is None:
                return {"ok": False, "msg": "后端未就绪", "sessions": []}
            q = str(query or "").strip().lower()
            raw = self._memory.get_all_sessions(workdir=None, include_empty=False)
            items = []
            for s in raw:
                name = str(s.get("name") or "")
                if not name or name == self._current_session:
                    continue
                if q and q not in name.lower():
                    continue
                items.append({
                    "name": name,
                    "count": int(s.get("count") or 0),
                    "updated_at": str(s.get("updated_at") or ""),
                    "workdir": str(s.get("workdir") or ""),
                })
            items.sort(key=lambda it: it["updated_at"], reverse=True)
            return {"ok": True, "sessions": items[:max(1, int(limit or 8))]}
        except Exception as e:
            logger.exception("search_sessions 失败")
            return {"ok": False, "msg": str(e), "sessions": []}
```

`gui_bridge.py` worker 解析段（:5031-5044）改为：

```python
            try:
                from core.input_parser import parse_user_input, extract_session_refs
                if internal_kind:
                    # 内部回合（后台汇总/命令总结）：跳过 @文件/@对话 解析
                    parsed_content, attached_msgs = user_text, []
                    session_refs = []
                else:
                    user_text, session_refs = extract_session_refs(user_text)
                    parsed_content, attached_msgs = parse_user_input(
                        user_text, explicit_paths=attachments, session_id=session_name
                    )
                    if session_refs:
                        attached_msgs = list(attached_msgs) + [
                            f"💬 已引用对话: {name}" for name in session_refs
                        ]
            except Exception:
                logger.debug("解析用户输入失败，退回纯文本", exc_info=True)
                parsed_content, attached_msgs = user_text, []
                session_refs = []
```

`prepare_turn_messages(...)` 调用（:5109）追加参数：

```python
                turn_source=turn_source,
                referenced_sessions=session_refs,
            )
```

`main.py` CLI：两处 `parse_user_input` 调用点（:4786、:4801）之前先 `actual_query, session_refs = extract_session_refs(actual_query)`（对应变量名以现场为准），并打印 `💬 已引用对话: ...`（仿 :4802 的 attached_msgs 打印）；`prepare_turn_messages` 调用（:4853 附近）追加 `referenced_sessions=session_refs`。注意 :4776 的 `is_skill_call` 分支与内部回合一样跳过提取（`session_refs = []`）。

- [x] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_session_mention_bridge.py -q; python -m py_compile gui_bridge.py main.py core/agent_service.py core/input_parser.py`
Expected: 测试 PASS，编译无输出（退出码 0）

- [x] **Step 5: Commit**

```powershell
git add gui_bridge.py main.py tests/test_session_mention_bridge.py
git commit -m "feat: bridge search_sessions and thread refs through GUI/CLI send paths"
```

---

### Task 5: 前端 — @ 面板新增「对话」候选组

**Files:**
- Modify: `frontend/src/types/pywebview.d.ts:881`（`search_workspace_files` 声明后追加）
- Modify: `frontend/src/composables/useSuggest.ts`
- Modify: `frontend/src/components/InputSuggest.vue`（分组小标题渲染）
- Test: `frontend/src/composables/useSuggest.test.ts`（追加用例）

**Interfaces:**
- Consumes: Task 4 的 `GuiBridge.search_sessions`。
- Produces: `SuggestItem` 新增可选 `group?: 'session' | 'file'`；会话项 `apply` 产出 `@{会话名}`（名内 `{`/`}` 被剥离）。

- [x] **Step 1: 写失败测试**

`useSuggest.test.ts` 既有用例的 `window.pywebview` mock 基础上追加（具体 mock 写法照该文件既有模式）：

```ts
it('@ 输入时合并会话与文件候选，会话在前', async () => {
  // mock api.search_sessions 返回 [{name:'登录bug排查', count:3, updated_at:'', workdir:''}]
  // mock api.search_workspace_files 返回 {files:['core/login.py']}
  // 输入 '@登录' 触发 detectToken，等待防抖后断言：
  // items[0].group === 'session'，label 为会话名；文件项排在会话项之后
});

it('选中会话项插入 @{会话名} 并剥离花括号', async () => {
  // 会话名 '我的{怪}对话' → apply 应为 '@{我的怪对话}'
});

it('search_sessions 接口缺失时退化为纯文件补全', async () => {
  // api 只实现 search_workspace_files，不抛错、仍返回文件项
});
```

- [x] **Step 2: 运行确认失败**

Run: `cd frontend; npm run test -- useSuggest`
Expected: FAIL（`group` 属性不存在 / 会话项未出现）

- [x] **Step 3: 实现**

`pywebview.d.ts` 在 `search_workspace_files` 声明（:882）后追加：

```ts
  // @ 提及对话自动补全：历史会话子串匹配（排除当前会话）
  search_sessions(query: string, limit?: number): Promise<{
    ok: boolean;
    sessions?: { name: string; count: number; updated_at: string; workdir: string }[];
    msg?: string;
  }>;
```

`useSuggest.ts`：
1. `suggest` 的 mode 联合类型保持 `'command' | 'file'` 不变（会话并入 file 模式的 @ 分支）。
2. `fetchFileItems(query)` 改名为 `fetchAtItems(query)`（同步改 `detectToken` 调用点），实现改为并发拉取后合并：

```ts
  function fetchAtItems(query: string) {
    window.clearTimeout(fileSuggestTimer);
    fileSuggestTimer = window.setTimeout(async () => {
      const api = window.pywebview && window.pywebview.api;
      if (!api) {
        suggestItems.value = [];
        return;
      }
      try {
        const sessionPromise =
          typeof api.search_sessions === 'function'
            ? api.search_sessions(query, 4)
            : Promise.resolve(null);
        const [sessionRes, fileRes] = await Promise.all([
          sessionPromise,
          api.search_workspace_files(query, 8),
        ]);
        // M26 同款守卫：异步返回时校验模式与 query 未变
        if (!suggest.value || suggest.value.mode !== 'file') return;
        if (suggest.value.query !== query) return;
        const sessionItems: SuggestItem[] = (
          (sessionRes && sessionRes.sessions) || []
        ).map(s => ({
          key: 'session:' + s.name,
          icon: '💬',
          label: s.name,
          desc: `${s.count} 条消息`,
          apply: '@{' + s.name.replace(/[{}]/g, '') + '}',
          group: 'session',
        }));
        const fileItems: SuggestItem[] = (((fileRes && fileRes.files) || []) as string[]).map(f => ({
          key: 'file:' + f,
          icon: '📄',
          label: f,
          desc: '',
          apply: '@' + f,
          group: 'file',
        }));
        suggestItems.value = [...sessionItems, ...fileItems].slice(0, 8);
        suggestIndex.value = 0;
      } catch {
        suggestItems.value = [];
      }
    }, 150);
  }
```

3. 文件项 key 由纯路径改为 `'file:' + f`（避免与会话项撞 key）。

`InputSuggest.vue`：
1. `SuggestItem` 接口追加 `group?: 'session' | 'file'`。
2. 模板 `v-for` 项前插分组小标题（相邻项 group 变化时渲染）：

```vue
      <template v-for="(it, i) in items" :key="it.key">
        <div v-if="i === 0 || items[i - 1].group !== it.group" class="sg-group">
          {{ it.group === 'session' ? '对话' : '文件' }}
        </div>
        <div
          class="sg-item"
          :class="{ active: i === activeIndex }"
          @mousedown.prevent="emit('select', it)"
          @mouseenter="emit('hover', i)"
        >
          <span class="sg-icon">{{ it.icon }}</span>
          <span class="sg-label">{{ it.label }}</span>
          <span class="sg-desc">{{ it.desc }}</span>
        </div>
      </template>
```

3. 样式追加：

```css
.sg-group {
  padding: 4px 10px 2px;
  font-size: 10.5px;
  color: var(--text-faint);
}
```

（斜杠命令项无 `group` 字段，`i === 0` 时会渲染一个「文件」标题——为避免误标，渲染条件加 `it.group` 非空判断：`<div v-if="it.group && (i === 0 || items[i - 1].group !== it.group)" ...>`。）

- [x] **Step 4: 运行确认通过**

Run: `cd frontend; npm run test; npm run build`
Expected: vitest 全绿（含既有用例），vue-tsc 类型检查无错、构建成功

- [x] **Step 5: Commit**

```powershell
git add frontend/src/types/pywebview.d.ts frontend/src/composables/useSuggest.ts frontend/src/components/InputSuggest.vue frontend/src/composables/useSuggest.test.ts
git commit -m "feat(frontend): session candidates in @ suggest panel"
```

---

### Task 6: 全量验证与手动冒烟

**Files:**
- 无新增/修改（如冒烟发现问题，回到对应 Task 修复）

- [x] **Step 1: 后端全量测试**

Run: `python -m pytest tests/ -q -x`
Expected: PASS（重点关注 test_input_parser / test_agent_service / test_session_mention_bridge 及 memory 相关用例无回归）

- [x] **Step 2: 前端全量验证**

Run: `cd frontend; npm run test; npm run build`
Expected: 全绿

- [ ] **Step 3: GUI 手动冒烟清单**

启动 GUI（`python gui_main.py`）后逐项确认：

1. 输入 `@` → 面板出现「对话」分组（当前会话不在列表中）+「文件」分组
2. 输入 `@部分标题` → 会话列表按子串过滤；↑↓/Tab/Enter/Esc 行为与 @文件 一致
3. 选中会话 → 输入框插入 `@{标题}`；发送后气泡不显示引用块正文，附件提示区出现「💬 已引用对话: 标题」
4. 模型回复能引用被 @ 对话的内容；数据库中该 user 消息带 `【引用的对话记录】` 块，重新加载会话后气泡被 `strip_context_suffix` 剥离干净
5. `@{不存在的会话}` 发送不报错，模型收到「已跳过」标注
6. @ 当前会话自身 → 块内标注「是当前会话，已跳过」
7. CLI（`python main.py`）手动输入 `@{标题}` 同样生效

- [x] **Step 4: Commit（如有修复）**

```powershell
git add -A
git commit -m "test: verify @-mention session end to end"
```

---

## 设计取舍说明（评审用）

1. **为什么是 `@{会话名}` 而不是 `@会话名`**：现有 @文件 正则字符集 `[a-zA-Z0-9_\-\.\/\\]` 不支持中文/空格，会话标题两者都有；`{...}` 定界清晰、与文件正则零冲突，后端一个 `SESSION_REF_PATTERN` 搞定。
2. **为什么走上下文后缀注入而不是把对话内容拼进 user 消息正文**（@文件的做法）：对话内容动辄数千字，拼进正文会永久留在气泡与历史里；后缀方案复用现有 CAS 持久化 + `strip_context_suffix` 剥离机制，气泡展示零污染，且三条入口（GUI/CLI/LAN）唯一必经 `prepare_turn_messages`。
3. **为什么不改 `parse_user_input` 返回契约**：现契约 `(content, attached_msgs)` 有 4 个调用方（gui_bridge/main.py×2/parity 脚本），新增独立函数 `extract_session_refs` 零涟漪。
4. **已知限制（v1 接受）**：@ 查询词不能含空格（沿用现有语义）；LAN 消息不解析 `@{...}`；会话被重命名后历史消息里的旧 `@{旧名}` 无害（内容已在当轮内联固化）。
5. **后续可选增强（不在本计划）**：气泡中把 `@{标题}` 渲染为可点击芯片（点击跳转该会话）；引用块按 token 数而非字符数限额。
