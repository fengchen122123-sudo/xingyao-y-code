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
    stale_child = preview / "preview-111-abcd"
    fresh_child = preview / "preview-222-ef01"
    stale_child.mkdir()
    fresh_child.mkdir()
    old = time.time() - 25 * 3600
    os.utime(stale, (old, old))
    os.utime(stale_child, (old, old))
    os.utime(preview, (old, old))

    removed = runtime.sweep_stale_computer_temp_dirs(root=tmp_path)

    assert removed == 2
    assert not stale.exists()
    assert preview.exists()
    assert not stale_child.exists()
    assert fresh_child.exists()
    assert fresh.exists()
    assert unrelated.exists()


def test_sweep_never_raises_and_counts(tmp_path):
    (tmp_path / "ycode-computer-preview").mkdir()
    assert runtime.sweep_stale_computer_temp_dirs(root=tmp_path) == 0
    assert runtime.sweep_stale_computer_temp_dirs(root=tmp_path / "nope") == 0
