"""T7 架构度量与模块地图（scripts/architecture_report.py）单元测试。

覆盖点（对应《Codex与ZCode增量改进》T7）：
- scan：新增文件 >800 行告警；存量超基线 +10% 告警；存量只登记不前移；--check 只读
- deps：ast 解析相对/绝对导入并检出循环依赖；外部导入不误报、无环不误报
- map：docstring 首行生成模块地图；输出字节确定；竖线转义
- 真实仓库只读冒烟：scan --check / deps / map 不抛异常且不写仓库文件
"""

from __future__ import annotations

import json
from pathlib import Path

from scripts.architecture_report import (
    build_updated_baseline,
    collect_dependencies,
    find_alerts,
    find_cycles,
    main,
    scan_tree,
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


# --------------------------------------------------------------------- scan


def test_scan_tree_counts_lines_and_skips_pycache(tmp_path):
    root = tmp_path / "core"
    _write(root / "__init__.py", "")
    _write(root / "big.py", "x = 1\n" * 900)
    _write(root / "__pycache__" / "stale.py", "ignored = 1\n")

    assert scan_tree(root, prefix="core") == {"core/__init__.py": 0, "core/big.py": 900}


def test_find_alerts_flags_oversized_new_file():
    alerts = find_alerts({"core/big.py": 801}, {})
    assert [(a.kind, a.path, a.current) for a in alerts] == [("new_file", "core/big.py", 801)]
    assert find_alerts({"core/ok.py": 800}, {}) == []


def test_find_alerts_flags_growth_over_ten_percent():
    baseline = {"core/a.py": 100}
    assert find_alerts({"core/a.py": 110}, baseline) == []
    alerts = find_alerts({"core/a.py": 111}, baseline)
    assert [(a.kind, a.current, a.baseline) for a in alerts] == [("growth", 111, 100)]


def test_build_updated_baseline_never_advances_existing_entries():
    updated = build_updated_baseline(
        current={"core/a.py": 150, "core/new.py": 50},
        baseline={"core/a.py": 100, "core/removed.py": 10},
    )
    assert updated == {"core/a.py": 100, "core/new.py": 50}


def test_scan_cli_first_registration_idempotent_and_check_readonly(tmp_path, capsys):
    root = tmp_path / "core"
    _write(root / "a.py", "x = 1\n" * 900)
    baseline_path = tmp_path / "baseline.json"

    # 首次运行：全量登记，不产生告警
    assert main(["scan", "--root", str(root), "--baseline", str(baseline_path)]) == 0
    first_bytes = baseline_path.read_bytes()
    payload = json.loads(first_bytes.decode("utf-8"))
    assert payload["files"] == {"core/a.py": 900}
    assert payload["root"] == "core"
    assert b"\r\n" not in first_bytes

    # 重复运行：存量不前移，字节稳定
    assert main(["scan", "--root", str(root), "--baseline", str(baseline_path)]) == 0
    assert baseline_path.read_bytes() == first_bytes

    # 新增超限文件：--check 告警且不写入
    _write(root / "b.py", "y = 2\n" * 900)
    assert main(["scan", "--root", str(root), "--baseline", str(baseline_path), "--check"]) == 1
    assert baseline_path.read_bytes() == first_bytes
    assert "core/b.py" in capsys.readouterr().out


# --------------------------------------------------------------------- deps


def test_deps_detects_relative_and_absolute_import_cycle(tmp_path, capsys):
    root = tmp_path / "pkg"
    _write(root / "__init__.py", "")
    _write(root / "a.py", "from . import b\n")
    _write(root / "b.py", "from pkg import a\n")

    assert main(["deps", "--root", str(root), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["cycles"] == [["pkg.a", "pkg.b"]]


def test_deps_ignores_external_imports_and_reports_no_cycle(tmp_path):
    root = tmp_path / "pkg"
    _write(root / "__init__.py", "")
    _write(root / "a.py", "import os\nfrom . import b\n")
    _write(root / "b.py", "x = 1\n")
    _write(root / "c.py", "from pkg import a\nimport third_party_pkg\n")

    graph, failures = collect_dependencies(root, prefix="pkg")
    assert failures == []
    assert graph["pkg.a"] == {"pkg.b"}
    assert graph["pkg.b"] == set()
    # `from pkg import a`：alias 命中子模块 pkg.a，不再回退到包本身
    assert graph["pkg.c"] == {"pkg.a"}
    assert find_cycles(graph) == []


def test_find_cycles_sorted_groups_and_self_loop_ignored():
    graph = {"a": {"b"}, "b": {"a"}, "c": {"d"}, "d": {"c"}, "solo": {"solo"}}
    assert find_cycles(graph) == [["a", "b"], ["c", "d"]]


# ---------------------------------------------------------------------- map


def test_module_map_generation_and_determinism(tmp_path):
    root = tmp_path / "pkg"
    _write(root / "__init__.py", '"""Fixture 包。"""\n')
    _write(root / "alpha.py", '"""Alpha 模块：处理甲。\n\n更多细节。"""\n\nx = 1\n')
    _write(root / "beta.py", "y = 2\n")
    out = tmp_path / "module_map.md"

    assert main(["map", "--root", str(root), "--output", str(out)]) == 0
    text = out.read_text(encoding="utf-8")
    assert "`pkg/alpha.py`" in text
    assert "Alpha 模块：处理甲。" in text
    assert "`pkg/beta.py`" in text
    assert "更多细节" not in text  # 只取 docstring 首行
    assert "\r\n" not in text

    # 相同输入 → 相同输出字节
    assert main(["map", "--root", str(root), "--output", str(out)]) == 0
    assert out.read_bytes() == text.encode("utf-8")


def test_module_map_skips_title_line_in_docstring(tmp_path):
    root = tmp_path / "pkg"
    _write(root / "titled.py", '"""\ncore/titled.py\n========\n标题下方的真正描述。\n"""\n')
    out = tmp_path / "m.md"
    assert main(["map", "--root", str(root), "--output", str(out)]) == 0
    row = next(line for line in out.read_text(encoding="utf-8").splitlines() if "`pkg/titled.py`" in line)
    assert "标题下方的真正描述。" in row
    assert row.count("titled.py") == 1  # 标题行未被当作摘要重复输出


def test_module_map_escapes_pipe_in_docstring(tmp_path):
    root = tmp_path / "pkg"
    _write(root / "a.py", '"""A | B 简介。"""\n')
    out = tmp_path / "m.md"
    assert main(["map", "--root", str(root), "--output", str(out)]) == 0
    assert "A \\| B 简介。" in out.read_text(encoding="utf-8")


# ----------------------------------------------------------- 真实仓库只读冒烟


def test_real_repo_readonly_smoke(tmp_path, capsys):
    """真实仓库只读冒烟：全部输出走 tmp_path/stdout，不写仓库文件。"""
    missing = tmp_path / "no_such_baseline.json"
    assert main(["scan", "--check", "--baseline", str(missing)]) == 0
    assert not missing.exists()

    assert main(["deps"]) == 0
    assert "core" in capsys.readouterr().out

    map_out = tmp_path / "module_map.md"
    assert main(["map", "--output", str(map_out)]) == 0
    assert "`core/" in map_out.read_text(encoding="utf-8")