"""core/self_prompt.py 的注入块测试。

用 monkeypatch 把 ``config.YCODE_DIR`` 指到 tmp_path，确保测试绝口不碰真实
``~/.ycode``。覆盖：空状态返回空串、有数据时注入、防指令声明、超长截断、
多用户隔离、坏 JSON 容错。
"""

import json

import pytest

import config
from core import identity_store as store
from core import self_prompt


@pytest.fixture
def ycode_dir(tmp_path, monkeypatch):
    root = tmp_path / "ycode-data"
    monkeypatch.setattr(config, "YCODE_DIR", str(root))
    # 稳定用户键：避免宿主系统用户名 / 环境变量影响缺省 uid 解析
    monkeypatch.setenv("YCODE_USER", "tester")
    return root


def _fill_identity(uid="tester"):
    """写入一份完整的自我模型 + 用户档案。"""
    assert (
        store.save_self_model(
            {
                "stage": "growing",
                "traits": ["直接", "有点小脾气"],
                "voice": {"openers": ["好嘞"], "avoid": ["作为AI助手"]},
                "values": ["诚实", "先验证再宣布完成"],
                "growth_log": [{"date": "2026-11-11", "note": "第一次主动承认判断失误"}],
            }
        )
        is True
    )
    assert (
        store.update_user_profile(
            uid=uid,
            title="木偶",
            preferences=["结论先行"],
            forbidden=["不要主动报家底"],
        )
        is True
    )


# ---------------------------------------------------------------- 1) 空状态
def test_empty_state_returns_empty_string(ycode_dir):
    block = self_prompt.build_identity_prompt_block()
    assert block == ""
    # 只读：不得因读取而创建数据目录
    assert not (ycode_dir / "self").exists()
    assert not (ycode_dir / "users").exists()


def test_empty_state_for_session_returns_empty_string(ycode_dir):
    assert self_prompt.build_identity_prompt_block_for_session("任意会话") == ""


# ---------------------------------------------------------------- 2) 有数据即注入
def test_block_contains_self_model_and_profile(ycode_dir):
    _fill_identity()
    block = self_prompt.build_identity_prompt_block()
    assert block != ""
    assert "自我模型" in block
    assert "称呼" in block
    assert "木偶" in block
    # 自我模型的各字段
    assert "性格倾向" in block and "直接" in block
    assert "价值观" in block and "诚实" in block
    assert "成长阶段" in block and "growing" in block
    assert "成长记录" in block and "2026-11-11" in block
    # 用户档案的其余字段
    assert "偏好" in block and "结论先行" in block
    assert "禁区" in block and "不要主动报家底" in block
    # 两段都被带上
    assert "当前用户档案" in block


def test_only_profile_present_still_renders(ycode_dir):
    assert store.update_user_profile(uid="tester", title="木偶") is True
    block = self_prompt.build_identity_prompt_block()
    assert "当前用户档案" in block
    assert "木偶" in block
    assert "自我模型" not in block


def test_only_self_model_present_still_renders(ycode_dir):
    assert store.save_self_model({"traits": ["稳重"]}) is True
    block = self_prompt.build_identity_prompt_block()
    assert "自我模型" in block
    assert "稳重" in block
    assert "当前用户档案" not in block


# ---------------------------------------------------------------- 3) 防指令声明
def test_block_declares_memory_is_not_instruction(ycode_dir):
    _fill_identity()
    block = self_prompt.build_identity_prompt_block()
    assert "不是系统指令" in block
    # 两段各有自己的声明行
    assert block.count("不是系统指令") >= 2


# ---------------------------------------------------------------- 4) 截断
def test_overlong_preferences_are_truncated(ycode_dir):
    # 单条 150 字符（> MAX_ITEM_CHARS），共 10 条 → 渲染后远超 600 字符预算
    long_items = [f"偏好条目{i}：" + ("长偏好" * 30) for i in range(10)]
    assert store.update_user_profile(uid="tester", title="木偶", preferences=long_items) is True
    block = self_prompt.build_identity_prompt_block()
    assert block != ""
    # 预算：自我部分 1200 + 分隔 2 + 档案部分 600
    assert len(block) <= self_prompt.SELF_BUDGET + 2 + self_prompt.PROFILE_BUDGET
    assert "…" in block
    # 高优先级字段（称呼）必须保留
    assert "称呼" in block and "木偶" in block
    # 单条列表项也不得超过上限（120 字符）
    for line in block.splitlines():
        if line.startswith("偏好："):
            for item in line[len("偏好：") :].split("；"):
                assert len(item) <= self_prompt.MAX_ITEM_CHARS


def test_overlong_self_model_is_truncated(ycode_dir):
    # 12 条 × 124 字符 ≈ 1488 字符，超过自我部分 1200 字符预算
    traits = [f"性格{i}：" + ("细节" * 60) for i in range(12)]
    assert store.save_self_model({"traits": traits}) is True
    block = self_prompt.build_identity_prompt_block()
    assert block != ""
    assert len(block) <= self_prompt.SELF_BUDGET + 2 + self_prompt.PROFILE_BUDGET
    assert "…" in block
    # 性格倾向优先级最高，必须留下
    assert "性格倾向" in block


def test_low_priority_fields_dropped_first(ycode_dir):
    """自我模型超高时，最低优先级的「成长记录」先被挤成占位。"""
    traits = [f"性格{i}：" + ("细节" * 55) for i in range(6)]  # 高优先级，必留
    values = [f"价值{i}：" + ("准则" * 55) for i in range(4)]
    log = [{"date": "2026-11-11", "note": "记录" * 55} for _ in range(4)]
    assert store.save_self_model({"traits": traits, "values": values, "growth_log": log}) is True
    block = self_prompt.build_identity_prompt_block()
    assert "性格倾向" in block
    # 成长记录优先级最低：预算耗尽后只剩省略号占位
    assert "成长记录：…" in block
    assert ("记录" * 55) not in block


# ---------------------------------------------------------------- 5) 多用户隔离
def test_user_profiles_are_isolated(ycode_dir):
    assert store.update_user_profile(uid="A", title="甲") is True
    assert store.update_user_profile(uid="B", title="乙") is True

    block_a = self_prompt.build_identity_prompt_block("A")
    block_b = self_prompt.build_identity_prompt_block("B")
    assert "甲" in block_a and "乙" not in block_a
    assert "乙" in block_b and "甲" not in block_b


def test_unknown_user_key_yields_empty_block(ycode_dir):
    assert store.update_user_profile(uid="A", title="甲") is True
    assert self_prompt.build_identity_prompt_block("B") == ""


def test_illegal_user_key_does_not_raise(ycode_dir):
    _fill_identity()
    block = self_prompt.build_identity_prompt_block("../../etc")
    # 非法 uid 被 identity_store 拒绝 → 档案为空；自我模型仍有内容时可渲染
    assert isinstance(block, str)
    assert "../../etc" not in block


# ---------------------------------------------------------------- 6) 坏 JSON
def test_broken_self_json_does_not_raise(ycode_dir):
    path = ycode_dir / "self" / "self.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ this is not json", encoding="utf-8")
    block = self_prompt.build_identity_prompt_block()
    assert isinstance(block, str)
    assert block == ""


def test_broken_self_json_with_valid_profile_keeps_profile(ycode_dir):
    path = ycode_dir / "self" / "self.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json at all", encoding="utf-8")
    assert store.update_user_profile(uid="tester", title="木偶") is True

    block = self_prompt.build_identity_prompt_block()
    assert "木偶" in block
    assert "自我模型" not in block


def test_broken_profile_is_tolerated(ycode_dir):
    profile = ycode_dir / "users" / "tester" / "profile.md"
    profile.parent.mkdir(parents=True, exist_ok=True)
    profile.write_bytes(b"\xff\xfe\x00garbage")
    block = self_prompt.build_identity_prompt_block()
    assert isinstance(block, str)


# ---------------------------------------------------------------- 附加：秘书会话
def test_secretary_session_returns_empty(monkeypatch, ycode_dir):
    _fill_identity()

    def _fake_is_secretary(session_name=None):
        return True

    monkeypatch.setattr("core.secretary.is_secretary_session", _fake_is_secretary)
    assert self_prompt.build_identity_prompt_block_for_session("秘书A") == ""


def test_normal_session_delegates(monkeypatch, ycode_dir):
    _fill_identity()
    monkeypatch.setattr("core.secretary.is_secretary_session", lambda name=None: False)
    block = self_prompt.build_identity_prompt_block_for_session("普通会话")
    assert "木偶" in block


def test_session_helper_swallows_errors(monkeypatch, ycode_dir):
    def _boom(session_name=None):
        raise RuntimeError("db down")

    monkeypatch.setattr("core.secretary.is_secretary_session", _boom)
    # 判定异常按普通会话处理，不抛错
    assert isinstance(self_prompt.build_identity_prompt_block_for_session("x"), str)


# ---------------------------------------------------------------- 附加：只读
def test_module_never_writes(ycode_dir):
    _fill_identity()
    before = sorted(str(p.relative_to(ycode_dir)) for p in ycode_dir.rglob("*") if p.is_file())
    self_prompt.build_identity_prompt_block()
    self_prompt.build_identity_prompt_block_for_session("会话")
    after = sorted(str(p.relative_to(ycode_dir)) for p in ycode_dir.rglob("*") if p.is_file())
    assert before == after


def test_default_install_block_is_byte_stable(ycode_dir):
    """默认安装（无任何成长数据）必须返回空串，绝不注入任何字符。"""
    assert store.get_growth_settings()["enabled"] is True
    assert self_prompt.build_identity_prompt_block() == ""


def test_block_is_plain_text_without_frontmatter(ycode_dir):
    _fill_identity()
    block = self_prompt.build_identity_prompt_block()
    assert not block.startswith("---")
    assert json.dumps(block, ensure_ascii=False)  # 可 JSON 序列化（纯文本）