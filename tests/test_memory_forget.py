"""core.slash_commands.memory_forget 的单测：两阶段删除的纯文件操作语义。

全部用 tmp_path 造日志与索引文件，绝不触碰真实 ~/.ycode/memory：这些函数会真删文件行，
一旦测试写错路径就是真实的记忆数据损失。文件不存在、空关键词等边界也在这里守住。
"""

import pytest

from core.slash_commands import memory_forget


def _write(path, text):
    path.write_text(text, encoding="utf-8")
    return path


def test_scan_counts_matches_and_trims_samples(tmp_path):
    """统计要准确，样例要去首尾空白；命中三次时样例只给前三条。"""
    log_path = _write(
        tmp_path / "today.md",
        "\n".join(
            [
                "08:10  用户喜欢深色主题",
                "08:12  无关的一条记录",
                "08:20  skill: 用户喜欢深色主题，也喜欢紧凑布局",
                "",
                "08:30  用户喜欢深色主题（第三次）",
                "08:40  收尾无关行",
            ]
        )
        + "\n",
    )
    index_path = _write(
        tmp_path / "MEMORY.md",
        "- [深色主题](user/theme.md) — 用户喜欢深色主题\n"
        "- [无关](project/x.md) — 与关键词无关\n",
    )

    stats = memory_forget.scan_matches("深色主题", log_path=log_path, index_path=index_path)

    assert stats["log_lines"] == 3
    assert stats["index_lines"] == 1
    assert len(stats["samples"]) == 3
    assert stats["samples"][0] == "08:10  用户喜欢深色主题"
    # 扫描是纯读：文件内容不能有任何变化
    assert "08:40  收尾无关行" in log_path.read_text(encoding="utf-8")


def test_scan_truncates_long_sample(tmp_path):
    """超长命中行在样例里截断到 80 字以内，避免提示卡片被一行日志撑变形。"""
    long_line = "关键词" + "填充内容" * 60
    log_path = _write(tmp_path / "today.md", long_line + "\n")
    index_path = tmp_path / "MEMORY.md"  # 不存在：索引按 0 处理

    stats = memory_forget.scan_matches("关键词", log_path=log_path, index_path=index_path)

    assert stats["log_lines"] == 1
    assert stats["index_lines"] == 0
    assert len(stats["samples"]) == 1
    assert len(stats["samples"][0]) <= memory_forget.MAX_SAMPLE_CHARS
    assert stats["samples"][0].endswith("…")


def test_delete_removes_only_hit_lines_and_keeps_trailing_newline(tmp_path):
    """真删只删命中行：未命中行原样保留，文件末仍以换行结尾（原子写回的结果）。"""
    log_path = _write(
        tmp_path / "today.md",
        "08:00  保留第一条\n08:10  要删除的旧偏好\n08:20  保留第二条\n",
    )
    index_path = _write(
        tmp_path / "MEMORY.md",
        "- [旧偏好](user/old.md) — 要删除的旧偏好\n- [保留](project/k.md) — 保留索引\n",
    )

    stats = memory_forget.delete_matches(
        "旧偏好", log_path=log_path, index_path=index_path
    )

    assert stats == {"log_lines": 1, "index_lines": 1}
    log_text = log_path.read_text(encoding="utf-8")
    assert "旧偏好" not in log_text
    assert "保留第一条" in log_text and "保留第二条" in log_text
    assert log_text.endswith("\n")
    index_text = index_path.read_text(encoding="utf-8")
    assert "旧偏好" not in index_text
    assert "保留索引" in index_text
    assert index_text.endswith("\n")
    # 同目录不能残留临时文件（原子写回必须清理干净）
    assert sorted(p.name for p in tmp_path.iterdir()) == ["MEMORY.md", "today.md"]


def test_blank_keyword_raises_value_error(tmp_path):
    """空关键词必须抛 ValueError：空串是所有行的子串，放行等于整片删除。"""
    log_path = _write(tmp_path / "today.md", "一行内容\n")
    index_path = _write(tmp_path / "MEMORY.md", "- [x](y.md) — 一行内容\n")

    with pytest.raises(ValueError):
        memory_forget.scan_matches("   ", log_path=log_path, index_path=index_path)
    with pytest.raises(ValueError):
        memory_forget.delete_matches("", log_path=log_path, index_path=index_path)

    # 抛错时不能动文件
    assert log_path.read_text(encoding="utf-8") == "一行内容\n"


def test_missing_files_are_treated_as_zero(tmp_path):
    """文件不存在按 0 处理，不抛错（全新安装时当天日志可能还没生成）。"""
    log_path = tmp_path / "logs" / "missing.md"
    index_path = tmp_path / "MEMORY.md"

    assert memory_forget.scan_matches(
        "任意关键词", log_path=log_path, index_path=index_path
    ) == {"log_lines": 0, "index_lines": 0, "samples": []}
    assert memory_forget.delete_matches(
        "任意关键词", log_path=log_path, index_path=index_path
    ) == {"log_lines": 0, "index_lines": 0}
    # 没命中就不该顺手创建文件
    assert not log_path.exists()
    assert not index_path.exists()
