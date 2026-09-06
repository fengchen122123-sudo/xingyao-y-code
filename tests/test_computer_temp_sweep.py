"""启动清扫崩溃残留的电脑操控临时目录（提速计划 Task 7）。"""

from __future__ import annotations

import os
import time

from core.computer import runtime


def test_sweep_removes_only_stale_matching_dirs(tmp_path):
    stale = tmp_path / "ycode-computer-desktop-abc"
    fresh = tmp_path / "ycode-computer-desktop-xyz"
    unrelated = tmp_path / "ycode-computer-unrelated"
    preview = tmp_path / "ycode-computer-preview"
    for d in (stale, fresh, unrelated, preview):
        d.mkdir()
    old = time.time() - 25 * 3600
    os.utime(stale, (old, old))
    os.utime(preview, (old, old))

    removed = runtime.sweep_stale_computer_temp_dirs(root=tmp_path)

    assert removed == 2
    assert not stale.exists()
    assert not preview.exists()
    assert fresh.exists()      # 24h 内：在用，不删
    assert unrelated.exists()  # 前缀不匹配，不删


def test_sweep_never_raises_and_counts(tmp_path):
    (tmp_path / "ycode-computer-preview").mkdir()
    assert runtime.sweep_stale_computer_temp_dirs(root=tmp_path) == 0
    # root 不存在也不崩
    assert runtime.sweep_stale_computer_temp_dirs(root=tmp_path / "nope") == 0
