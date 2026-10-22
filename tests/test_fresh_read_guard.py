"""写前新鲜度守卫（core.fresh_read_guard）单元测试。"""

import pytest

from core import fresh_read_guard
from core.fresh_read_guard import (
    StaleReadError,
    is_guard_enabled,
    record_read,
    require_fresh_read,
)


@pytest.fixture(autouse=True)
def _clear_read_state():
    """模块级状态是全局的，每个用例前后清空，避免相互污染。"""
    with fresh_read_guard._LOCK:
        fresh_read_guard._READ_STATE.clear()
    yield
    with fresh_read_guard._LOCK:
        fresh_read_guard._READ_STATE.clear()


def test_write_without_read_passes_in_lenient_mode(tmp_path):
    """默认宽松模式：未读即写放行——用户直接给内容让模型覆盖是常见合法用法。"""
    target = tmp_path / "a.txt"
    target.write_text("hello\n", encoding="utf-8")

    # 不抛异常即通过
    require_fresh_read("s1", str(target), tool_name="write_file")


def test_strict_mode_without_read_raises(tmp_path):
    """严格模式（require_read=True）：未读即改 → 抛 StaleReadError，消息带工具名与路径。"""
    target = tmp_path / "a.txt"
    target.write_text("hello\n", encoding="utf-8")

    with pytest.raises(StaleReadError) as excinfo:
        require_fresh_read(
            "s1", str(target), tool_name="write_file", require_read=True
        )

    message = str(excinfo.value)
    assert "write_file" in message
    assert str(target) in message


def test_read_then_write_passes(tmp_path):
    """读过且文件未变 → 不抛。"""
    target = tmp_path / "a.txt"
    target.write_text("hello\n", encoding="utf-8")

    record_read("s2", str(target), full_read=True)
    # 不抛异常即通过
    require_fresh_read("s2", str(target), tool_name="edit_file")


def test_file_changed_after_read_raises(tmp_path):
    """读后文件内容被改 → 抛 StaleReadError。"""
    target = tmp_path / "a.txt"
    target.write_text("hello\n", encoding="utf-8")
    record_read("s3", str(target), full_read=True)

    target.write_text("hello world\n", encoding="utf-8")

    with pytest.raises(StaleReadError):
        require_fresh_read("s3", str(target), tool_name="write_file")


def test_partial_read_is_not_registered(tmp_path):
    """full_read=False 的登记不生效 → 严格模式下仍会拒绝。"""
    target = tmp_path / "a.txt"
    target.write_text("hello\n", encoding="utf-8")

    record_read("s4", str(target), full_read=False)

    with pytest.raises(StaleReadError):
        require_fresh_read(
            "s4", str(target), tool_name="edit_file", require_read=True
        )


def test_skip_bypasses_guard(tmp_path):
    """skip=True（守卫关闭）→ 未读也不抛。"""
    target = tmp_path / "a.txt"
    target.write_text("hello\n", encoding="utf-8")

    require_fresh_read("s5", str(target), tool_name="write_file", skip=True)


def test_missing_target_passes(tmp_path):
    """目标文件不存在（新建场景）→ 不抛。"""
    target = tmp_path / "not_exists.txt"

    require_fresh_read("s6", str(target), tool_name="write_file")


def test_guard_enabled_switch(monkeypatch):
    """环境变量为 0/off/false（不区分大小写）时关闭，其余情况开启。"""
    monkeypatch.setenv("XINGYAO_FRESH_READ_GUARD", "0")
    assert is_guard_enabled() is False

    for value in ("off", "FALSE", "False", " OFF "):
        monkeypatch.setenv("XINGYAO_FRESH_READ_GUARD", value)
        assert is_guard_enabled() is False

    monkeypatch.setenv("XINGYAO_FRESH_READ_GUARD", "1")
    assert is_guard_enabled() is True

    monkeypatch.delenv("XINGYAO_FRESH_READ_GUARD", raising=False)
    assert is_guard_enabled() is True
