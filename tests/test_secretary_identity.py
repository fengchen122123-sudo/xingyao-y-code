"""秘书身份（核心数据层 + 身份解析）单元测试。

覆盖：名字落库 / 读取往返、非法名字拒绝、会话来源读取、秘书会话判定、
当前生效自称在秘书会话与普通会话间切换、缓存失效、异常回落。
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.app_context as app_context
import core.secretary as secretary
from core.memory import MemoryManager


@pytest.fixture
def manager(tmp_path):
    """独立临时 DB 的 MemoryManager，并注册为 app_context 当前管理器。

    `import main`（任何更早的测试文件触发）会注册 CLI ctx，其只读 manager/
    current_session 视图优先于 set_manager 的 fallback——按 app_context 的
    文档契约，需覆盖必须先注销 ctx，否则本 fixture 的管理器被 main 模块
    全局影子化（全量套件下身份解析读到 main.manager 的空库、当前会话读到
    main 导入期生成的 session_xxx）。
    """
    m = MemoryManager(str(tmp_path / "s.db"))
    prev_ctx = app_context.get()
    prev_manager = app_context.get_manager()
    prev_session = app_context.get_current_session()
    app_context.register(None)
    app_context.set_manager(m)
    app_context.set_current_session("")
    secretary.invalidate_secretary_cache()
    try:
        yield m
    finally:
        secretary.invalidate_secretary_cache()
        app_context.set_current_session(prev_session)
        app_context.set_manager(prev_manager)
        app_context.register(prev_ctx)


# ---------------------------------------------------------------------------
# 数据层：名字落库 / 读取 / 写入
# ---------------------------------------------------------------------------

def test_secretary_name_roundtrip(manager):
    manager.create_session_explicit(
        "s_sec", workdir="GLOBAL", source="SECRETARY", secretary_name="星河"
    )
    assert manager.get_session_secretary_name("s_sec") == "星河"
    assert manager.get_session_source("s_sec") == "SECRETARY"

    manager.create_session_explicit("s_plain", workdir="GLOBAL", source="GUI")
    # 非秘书会话与不存在的会话都返回空串，不抛错
    assert manager.get_session_secretary_name("s_plain") == ""
    assert manager.get_session_source("s_plain") == "GUI"
    assert manager.get_session_secretary_name("s_missing") == ""
    assert manager.get_session_source("s_missing") == ""


def test_set_session_secretary_name_updates_and_reports_rowcount(manager):
    manager.create_session_explicit(
        "s_rename", workdir="GLOBAL", source="SECRETARY", secretary_name="星河"
    )
    assert manager.set_session_secretary_name("s_rename", "小满") is True
    assert manager.get_session_secretary_name("s_rename") == "小满"

    # 清空名字（落 NULL）后读取回落到空串
    assert manager.set_session_secretary_name("s_rename", "  ") is True
    assert manager.get_session_secretary_name("s_rename") == ""

    # 写入不存在的会话：无匹配行，返回 False 且不抛错
    assert manager.set_session_secretary_name("s_nope", "小满") is False


# ---------------------------------------------------------------------------
# 名字校验：拒绝非法值，且不静默截断
# ---------------------------------------------------------------------------

def test_validate_secretary_name_rejects_blank_and_long():
    # 首尾空白被去掉，合法名原样返回
    assert secretary.validate_secretary_name("  星河  ") == "星河"
    # 恰好 20 字符合法（边界内）
    assert secretary.validate_secretary_name("星" * 20) == "星" * 20
    # 空白 / 超长一律抛错，不做静默截断
    with pytest.raises(ValueError):
        secretary.validate_secretary_name("   ")
    with pytest.raises(ValueError):
        secretary.validate_secretary_name("星" * 21)
    with pytest.raises(ValueError):
        secretary.validate_secretary_name(None)


# ---------------------------------------------------------------------------
# 身份解析：秘书会话判定 / 名字读取
# ---------------------------------------------------------------------------

def test_is_secretary_session_and_get_secretary_name(manager):
    manager.create_session_explicit(
        "s_sec", workdir="GLOBAL", source="SECRETARY", secretary_name="星河"
    )
    manager.create_session_explicit("s_plain", workdir="GLOBAL", source="GUI")

    assert secretary.is_secretary_session("s_sec") is True
    assert secretary.is_secretary_session("s_plain") is False
    # 查不到的会话按普通会话处理
    assert secretary.is_secretary_session("s_missing") is False

    assert secretary.get_secretary_name("s_sec") == "星河"
    assert secretary.get_secretary_name("s_plain") == ""


# ---------------------------------------------------------------------------
# 当前生效自称：秘书会话 / 普通会话切换
# ---------------------------------------------------------------------------

def test_active_agent_name_switches_by_session(manager, monkeypatch):
    manager.create_session_explicit(
        "s_sec", workdir="GLOBAL", source="SECRETARY", secretary_name="星河"
    )
    manager.create_session_explicit("s_plain", workdir="GLOBAL", source="GUI")
    monkeypatch.setattr(
        "core.persona_settings.get_agent_display_name", lambda: "设置页名字"
    )

    # 缺省取当前会话：切到秘书会话 → 秘书名
    app_context.set_current_session("s_sec")
    assert secretary.get_active_agent_name() == "星河"

    # 切到普通会话 → 设置页名字
    app_context.set_current_session("s_plain")
    assert secretary.get_active_agent_name() == "设置页名字"


def test_active_agent_name_falls_back_for_unnamed_secretary(manager):
    manager.create_session_explicit("s_noname", workdir="GLOBAL", source="SECRETARY")
    app_context.set_current_session("s_noname")
    assert secretary.get_active_agent_name() == secretary.FALLBACK_SECRETARY_NAME


def test_active_agent_name_never_raises_on_backend_error(monkeypatch):
    class Boom:
        def get_session_source(self, *a, **k):
            raise RuntimeError("boom")

        def get_session_secretary_name(self, *a, **k):
            raise RuntimeError("boom")

    secretary.invalidate_secretary_cache()
    app_context.set_current_session("s_broken")
    monkeypatch.setattr(app_context, "get_manager", lambda: Boom())
    monkeypatch.setattr(
        "core.persona_settings.get_agent_display_name", lambda: "设置页名字"
    )
    # 数据层抛错时回落 persona 名，绝不冒泡
    assert secretary.get_active_agent_name() == "设置页名字"
    secretary.invalidate_secretary_cache()


# ---------------------------------------------------------------------------
# 名字块与缓存失效
# ---------------------------------------------------------------------------

def test_build_secretary_name_block():
    block = secretary.build_secretary_name_block("星河")
    assert "星河" in block and "秘书" in block
    # 空名字不注入任何块
    assert secretary.build_secretary_name_block("") == ""


def test_invalidate_secretary_cache_refreshes_name(manager):
    manager.create_session_explicit(
        "s_sec", workdir="GLOBAL", source="SECRETARY", secretary_name="星河"
    )
    assert secretary.get_secretary_name("s_sec") == "星河"

    # 改了库但缓存未失效 → 仍读到旧值（证明缓存生效）
    manager.set_session_secretary_name("s_sec", "小满")
    assert secretary.get_secretary_name("s_sec") == "星河"

    # 显式失效后读到新值
    secretary.invalidate_secretary_cache("s_sec")
    assert secretary.get_secretary_name("s_sec") == "小满"

    # 不传参清空全部缓存
    manager.set_session_secretary_name("s_sec", "云疏")
    secretary.invalidate_secretary_cache()
    assert secretary.get_secretary_name("s_sec") == "云疏"


# ---------------------------------------------------------------------------
# 系统提示词按会话分流（Task 3：稳定系统提示词 + 人设块）
# ---------------------------------------------------------------------------

# 秘书随包提示词独有的表述（普通提示词里没有），用于判定"确实读了秘书文件"
_SECRETARY_PROMPT_MARKER = "用户的私人秘书"
# 设置页人设块的固定表头（core/persona_settings.build_persona_prompt_block）
_PERSONA_BLOCK_HEADER = "# 人设设置（来自设置页，用户自定义）"


def test_secretary_session_stable_prompt_carries_secretary_name(manager, monkeypatch):
    """秘书会话：稳定提示词用秘书名，且**不注入**设置页人设块。"""
    import core.agent_service as agent_service
    import core.persona_settings as persona_settings

    manager.create_session_explicit(
        "s_sec_prompt",
        workdir="GLOBAL",
        source="SECRETARY",
        secretary_name="星河",
    )
    secretary.invalidate_secretary_cache()
    # 人设块故意设为非空：秘书会话必须跳过它，否则星瑶人设会串进秘书身份
    monkeypatch.setattr(
        persona_settings,
        "build_persona_prompt_block",
        lambda: _PERSONA_BLOCK_HEADER + "\n\n名字覆盖：你是「星瑶」。",
    )

    content = agent_service.build_stable_system_message(
        is_gui=True, session_name="s_sec_prompt"
    )["content"]

    assert "星河" in content
    # 来自秘书随包文件的标记：证明确实走了分流文件而不是普通文件
    assert _SECRETARY_PROMPT_MARKER in content
    # 占位符已替换，不残留
    assert "{{secretary_name}}" not in content
    # 设置页人设块（名称覆盖 / 称呼 / 自定义人设）一律不进秘书会话
    assert _PERSONA_BLOCK_HEADER not in content


def test_plain_session_stable_prompt_bytes_unchanged(manager):
    """普通会话：带 session_name 与不带 session_name 逐字节相同，且不含秘书文件文本。"""
    import core.agent_service as agent_service

    manager.create_session_explicit("s_plain_prompt", workdir="GLOBAL", source="GUI")
    secretary.invalidate_secretary_cache()
    app_context.set_current_session("s_plain_prompt")

    with_name = agent_service.build_stable_system_message(
        is_gui=True, session_name="s_plain_prompt"
    )["content"]
    without_name = agent_service.build_stable_system_message(is_gui=True)["content"]

    assert with_name == without_name
    assert _SECRETARY_PROMPT_MARKER not in with_name
    assert "{{secretary_name}}" not in with_name


def test_secretary_prompt_falls_back_when_file_missing(tmp_path, manager, monkeypatch):
    """秘书提示词文件读取失败：回落普通提示词（默认名换成秘书名），绝不抛错。"""
    import core.agent_service as agent_service

    manager.create_session_explicit(
        "s_sec_fallback",
        workdir="GLOBAL",
        source="SECRETARY",
        secretary_name="望舒",
    )
    secretary.invalidate_secretary_cache()

    # ① 文件不存在：回落普通提示词，身份仍是秘书名（默认名被替换，不出现星瑶）
    monkeypatch.setattr(
        agent_service, "SECRETARY_PROMPT_FILENAME", "system_prompt_secretary_gui.none.md"
    )
    content = agent_service.build_stable_system_message(
        is_gui=True, session_name="s_sec_fallback"
    )["content"]
    assert _SECRETARY_PROMPT_MARKER not in content
    assert "{{secretary_name}}" not in content
    assert "望舒" in content
    # 名字替换只对"提示词正文"负责：整条 system message 还挂着技能目录、子 Agent
    # 清单、记忆索引等外部文本，而内置技能描述里本来就可能出现默认名
    # （skills/plugin-creator 的 description 就写着"为星瑶开发插件"）。
    # 断言必须收窄到正文，否则技能目录一改就误报。
    prompt_text = agent_service.load_system_prompt(is_gui=True, session_name="s_sec_fallback")
    assert "望舒" in prompt_text
    assert "星瑶" not in prompt_text

    # ② 连普通提示词文件也读不到（极端情况）：仍返回兜底文本，名字换成秘书名
    monkeypatch.setattr(agent_service, "_repo_root", lambda: str(tmp_path))
    fallback = agent_service.load_system_prompt(is_gui=True, session_name="s_sec_fallback")
    assert "望舒" in fallback and "星瑶" not in fallback


def test_plain_session_unaffected_when_prompt_files_missing(tmp_path, monkeypatch):
    """非秘书路径在提示词文件缺失时仍逐字节等于原兜底文本（不带秘书名替换）。"""
    import core.agent_service as agent_service
    import core.persona_settings as persona_settings

    monkeypatch.setattr(agent_service, "_repo_root", lambda: str(tmp_path))
    monkeypatch.setattr(
        agent_service, "SECRETARY_PROMPT_FILENAME", "system_prompt_secretary_gui.none.md"
    )
    app_context.set_current_session("")

    content = agent_service.load_system_prompt(is_gui=True, session_name="s_not_secretary")
    expected = agent_service.FALLBACK_SYSTEM_PROMPT.format(
        agent_name=persona_settings.get_agent_display_name()
    )
    assert content == expected


# ---------------------------------------------------------------------------
# 秘书专属人设（每个秘书是一个独立 agent，人设只属于它自己的会话）
# ---------------------------------------------------------------------------

# core.secretary.build_secretary_persona_block 的固定表头
_SECRETARY_PERSONA_HEADER = "# 秘书人设（本会话专属，由用户设定）"


def test_secretary_persona_roundtrip(manager):
    manager.create_session_explicit(
        "s_persona",
        workdir="GLOBAL",
        source="SECRETARY",
        secretary_name="星河",
        secretary_persona="你是一位做事讲究先后的行政秘书。",
    )
    assert manager.get_session_secretary_persona("s_persona") == "你是一位做事讲究先后的行政秘书。"

    # 非秘书会话与不存在的会话都返回空串（= 无专属人设），不抛错
    manager.create_session_explicit("s_plain_persona", workdir="GLOBAL", source="GUI")
    assert manager.get_session_secretary_persona("s_plain_persona") == ""
    assert manager.get_session_secretary_persona("s_missing_persona") == ""

    # 写人设：命中与会话不存在分别返回 True / False
    assert manager.set_session_secretary_persona("s_persona", "新人设") is True
    assert manager.get_session_secretary_persona("s_persona") == "新人设"
    # 清空 = 恢复内置默认秘书人设
    assert manager.set_session_secretary_persona("s_persona", "  ") is True
    assert manager.get_session_secretary_persona("s_persona") == ""
    assert manager.set_session_secretary_persona("s_nope_persona", "x") is False


def test_validate_secretary_persona_allows_empty_and_rejects_too_long():
    # 没写人设是合法输入（= 用默认人设），首尾空白去掉后原样返回
    assert secretary.validate_secretary_persona("") == ""
    assert secretary.validate_secretary_persona("   ") == ""
    assert secretary.validate_secretary_persona(None) == ""
    assert secretary.validate_secretary_persona("  人设  ") == "人设"
    # 边界内合法、超出报错（不静默截断）
    assert secretary.validate_secretary_persona("设" * secretary.MAX_PERSONA_LEN)
    with pytest.raises(ValueError):
        secretary.validate_secretary_persona("设" * (secretary.MAX_PERSONA_LEN + 1))
    with pytest.raises(ValueError):
        secretary.validate_secretary_persona(123)


def test_secretary_persona_read_and_cache_invalidation(manager):
    manager.create_session_explicit(
        "s_persona_cache",
        workdir="GLOBAL",
        source="SECRETARY",
        secretary_name="星河",
        secretary_persona="旧人设",
    )
    secretary.invalidate_secretary_cache()
    assert secretary.get_secretary_persona("s_persona_cache") == "旧人设"

    # 改库但缓存未失效 → 仍读到旧值（证明缓存生效）
    manager.set_session_secretary_persona("s_persona_cache", "新人设")
    assert secretary.get_secretary_persona("s_persona_cache") == "旧人设"

    secretary.invalidate_secretary_cache("s_persona_cache")
    assert secretary.get_secretary_persona("s_persona_cache") == "新人设"
    # 非秘书会话永远读不到人设
    manager.create_session_explicit("s_plain_cache", workdir="GLOBAL", source="GUI")
    manager.set_session_secretary_persona("s_plain_cache", "不该出现")
    secretary.invalidate_secretary_cache()
    assert secretary.get_secretary_persona("s_plain_cache") == ""


def test_build_secretary_persona_block():
    block = secretary.build_secretary_persona_block("你是一位说话简短直接的秘书。")
    assert _SECRETARY_PERSONA_HEADER in block
    assert "你是一位说话简短直接的秘书。" in block
    # 明确声明"取代默认人设"，但红线和调度硬约束不受影响
    assert "取代" in block and "绝对红线" in block
    # 空人设不注入任何块
    assert secretary.build_secretary_persona_block("") == ""
    assert secretary.build_secretary_persona_block(None) == ""


def test_secretary_prompt_injects_custom_persona(manager, monkeypatch):
    """专属人设非空：前置注入秘书提示词，且设置页星瑶人设仍不串入。"""
    import core.agent_service as agent_service
    import core.persona_settings as persona_settings

    manager.create_session_explicit(
        "s_prompt_persona",
        workdir="GLOBAL",
        source="SECRETARY",
        secretary_name="星河",
        secretary_persona="你是一位说话像老朋友的秘书，偶尔会用一个语气词。",
    )
    secretary.invalidate_secretary_cache()
    # 设置页人设块故意设为非空：秘书会话必须跳过它，不能把星瑶人设串进来
    monkeypatch.setattr(
        persona_settings,
        "build_persona_prompt_block",
        lambda: _PERSONA_BLOCK_HEADER + "\n\n名字覆盖：你是「星瑶」。",
    )
    prompt = agent_service.load_system_prompt(is_gui=True, session_name="s_prompt_persona")

    # 人设块在最前面，且写明覆盖默认人设
    assert prompt.startswith(_SECRETARY_PERSONA_HEADER)
    assert "你是一位说话像老朋友的秘书" in prompt
    # 秘书文件照常加载、名字占位符已替换
    assert _SECRETARY_PROMPT_MARKER in prompt
    assert "{{secretary_name}}" not in prompt
    assert "星河" in prompt
    # 设置页人设块一律不进秘书会话
    assert _PERSONA_BLOCK_HEADER not in prompt


def test_secretary_prompt_without_persona_has_no_persona_block(manager):
    """没填人设：提示词与改造前一致，不出现人设块表头。"""
    import core.agent_service as agent_service

    manager.create_session_explicit(
        "s_prompt_default", workdir="GLOBAL", source="SECRETARY", secretary_name="星河"
    )
    secretary.invalidate_secretary_cache()
    prompt = agent_service.load_system_prompt(is_gui=True, session_name="s_prompt_default")
    assert _SECRETARY_PERSONA_HEADER not in prompt
    assert prompt.startswith("你是「星河」，用户的私人秘书。")


# ---------------------------------------------------------------------------
# 桥层：创建带人设的秘书 / 事后改人设 / 非秘书会话拒绝
# ---------------------------------------------------------------------------

def _lightweight_bridge(manager):
    """只注入 _memory 并桩掉 get_sessions 的桥对象：避免起整套 ApiBridge。"""
    import gui_bridge

    bridge = object.__new__(gui_bridge.ApiBridge)
    bridge._memory = manager
    bridge.get_sessions = lambda: []
    return bridge


def test_bridge_create_secretary_with_persona_and_update_it(manager):
    bridge = _lightweight_bridge(manager)
    created = bridge.create_secretary_session("新会话", "星河", "你是说话干脆的行政秘书。")
    assert created["ok"] is True
    session = created["session_name"]
    secretary.invalidate_secretary_cache()
    assert secretary.get_secretary_persona(session) == "你是说话干脆的行政秘书。"

    # 事后改人设：只动 secretary_persona，会话名不变
    updated = bridge.set_secretary_persona(session, "改成一个爱开玩笑的秘书。")
    assert updated["ok"] is True and updated["session_name"] == session
    secretary.invalidate_secretary_cache()
    assert secretary.get_secretary_persona(session) == "改成一个爱开玩笑的秘书。"

    # 传空白 = 恢复内置默认秘书人设（合法操作，不报错）
    assert bridge.set_secretary_persona(session, "  ")["ok"] is True
    secretary.invalidate_secretary_cache()
    assert secretary.get_secretary_persona(session) == ""


def test_bridge_rejects_bad_persona_and_non_secretary_session(manager):
    bridge = _lightweight_bridge(manager)
    # 超长人设拒绝，且不产生会话记录（否则会留下一个名字合法但人设非法的孤儿会话）
    too_long = "设" * (secretary.MAX_PERSONA_LEN + 1)
    created = bridge.create_secretary_session("新会话", "星河", too_long)
    assert created["ok"] is False
    secretary.invalidate_secretary_cache()
    assert [s for s in manager.get_all_sessions() if s["source"] == "SECRETARY"] == []

    # 非秘书会话不允许设置秘书人设
    manager.create_session_explicit("s_plain_bridge", workdir="GLOBAL", source="GUI")
    secretary.invalidate_secretary_cache()
    assert bridge.set_secretary_persona("s_plain_bridge", "人设")["ok"] is False


# ---------------------------------------------------------------------------
# 回合身份优先（2026-11-12 修复）：后台回合按线程会话判身份，不看 UI 当前会话
# ---------------------------------------------------------------------------

def test_thread_session_wins_over_ui_current_session(manager):
    """复现秘书派活误判：UI 停在秘书会话，被派的普通会话仍须按普通会话处理。

    修复前 ``is_secretary_session()`` 读 get_current_session（= UI 当前查看的
    会话），派活那一轮把目标会话判成秘书会话：工具面裁成秘书只读集
    （实测 tools_bytes=25206，与秘书会话逐字节相同）、系统提示词换成秘书契约，
    表现为"派出去的工作说本会话是秘书会话、没有命令执行和写文件工具"。
    """
    manager.create_session_explicit(
        "s_ui_secretary", workdir="GLOBAL", source="SECRETARY", secretary_name="星河"
    )
    manager.create_session_explicit("s_dispatched", workdir="GLOBAL", source="GUI")
    secretary.invalidate_secretary_cache()

    # UI 仍停在秘书会话上，但本回合跑的是被派的目标会话 → 必须按普通会话处理
    app_context.set_current_session("s_ui_secretary")
    app_context.set_thread_session("s_dispatched")
    try:
        assert secretary.is_secretary_session() is False
        assert secretary.get_active_agent_name() != "星河"
        # 显式传名依旧压倒线程绑定（回合门控传 parent_session 的既有用法不受影响）
        assert secretary.is_secretary_session("s_ui_secretary") is True
    finally:
        app_context.set_thread_session(None)

    # 反向：线程绑的确实是秘书会话时，必须按秘书处理
    app_context.set_current_session("s_dispatched")
    app_context.set_thread_session("s_ui_secretary")
    try:
        assert secretary.is_secretary_session() is True
        assert secretary.get_active_agent_name() == "星河"
    finally:
        app_context.set_thread_session(None)

    # 无线程绑定（UI 交互路径）回落 UI 当前会话，行为与改动前一致
    app_context.set_current_session("s_ui_secretary")
    assert secretary.is_secretary_session() is True
    app_context.set_current_session("s_dispatched")
    assert secretary.is_secretary_session() is False
    app_context.set_current_session("")


def test_dispatched_turn_gets_full_toolset(manager):
    """派活回合的工具面必须是完整工具集：含命令执行与写文件。

    这是"秘书派出去的活干不了"的直接判据：修复前 UI 停在秘书会话时，
    被派会话的 get_tools_schema() 返回秘书只读集（实测 tools_bytes=25206，
    与秘书会话逐字节相同），目标只能回"我没有命令执行和写文件工具"。
    同时反向校验秘书护栏没被削弱——秘书回合仍然拿不到写类工具。
    """
    manager.create_session_explicit(
        "s_ui_secretary", workdir="GLOBAL", source="SECRETARY", secretary_name="星河"
    )
    manager.create_session_explicit("s_dispatched", workdir="GLOBAL", source="GUI")
    secretary.invalidate_secretary_cache()

    import core.llm as llm

    def _schema_names():
        return {
            str(schema.get("function", {}).get("name", ""))
            for schema in llm.get_tools_schema()
        }

    # UI 停在秘书会话，但本回合跑的是被派的目标会话 → 完整工具面
    app_context.set_current_session("s_ui_secretary")
    app_context.set_thread_session("s_dispatched")
    try:
        names = _schema_names()
        assert {"execute_command", "write_file", "edit_file"} <= names
        # 派活的执行会话不该看见秘书专属调度工具
        assert "dispatch_task" not in names
    finally:
        app_context.set_thread_session(None)

    # 反向：线程确实是秘书会话 → 只读集，护栏未削弱
    app_context.set_current_session("s_dispatched")
    app_context.set_thread_session("s_ui_secretary")
    try:
        names = _schema_names()
        assert "dispatch_task" in names
        assert "execute_command" not in names
        assert "write_file" not in names
    finally:
        app_context.set_thread_session(None)
        app_context.set_current_session("")
