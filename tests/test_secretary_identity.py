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
    """独立临时 DB 的 MemoryManager，并注册为 app_context 当前管理器。"""
    m = MemoryManager(str(tmp_path / "s.db"))
    app_context.set_manager(m)
    secretary.invalidate_secretary_cache()
    yield m
    secretary.invalidate_secretary_cache()
    app_context.set_current_session("")


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
