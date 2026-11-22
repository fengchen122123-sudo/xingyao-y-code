import pytest

from core.slash_commands import memory_forget


def _write(path, text):
    path.write_text(text, encoding="utf-8")
    return path


def test_scan_counts_matches_and_trims_samples(tmp_path):
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
    assert "08:40  收尾无关行" in log_path.read_text(encoding="utf-8")


def test_scan_truncates_long_sample(tmp_path):
    long_line = "关键词" + "填充内容" * 60
    log_path = _write(tmp_path / "today.md", long_line + "\n")
    index_path = tmp_path / "MEMORY.md"

    stats = memory_forget.scan_matches("关键词", log_path=log_path, index_path=index_path)

    assert stats["log_lines"] == 1
    assert stats["index_lines"] == 0
    assert len(stats["samples"]) == 1
    assert len(stats["samples"][0]) <= memory_forget.MAX_SAMPLE_CHARS
    assert stats["samples"][0].endswith("…")


def test_delete_removes_only_hit_lines_and_keeps_trailing_newline(tmp_path):
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
    assert sorted(p.name for p in tmp_path.iterdir()) == ["MEMORY.md", "today.md"]


def test_blank_keyword_raises_value_error(tmp_path):
    log_path = _write(tmp_path / "today.md", "一行内容\n")
    index_path = _write(tmp_path / "MEMORY.md", "- [x](y.md) — 一行内容\n")

    with pytest.raises(ValueError):
        memory_forget.scan_matches("   ", log_path=log_path, index_path=index_path)
    with pytest.raises(ValueError):
        memory_forget.delete_matches("", log_path=log_path, index_path=index_path)

    assert log_path.read_text(encoding="utf-8") == "一行内容\n"


def test_missing_files_are_treated_as_zero(tmp_path):
    log_path = tmp_path / "logs" / "missing.md"
    index_path = tmp_path / "MEMORY.md"

    assert memory_forget.scan_matches(
        "任意关键词", log_path=log_path, index_path=index_path
    ) == {"log_lines": 0, "index_lines": 0, "samples": []}
    assert memory_forget.delete_matches(
        "任意关键词", log_path=log_path, index_path=index_path
    ) == {"log_lines": 0, "index_lines": 0}
    assert not log_path.exists()
    assert not index_path.exists()
