import json
from pathlib import Path

from scripts.project_stats import collect_stats, write_report


def _make_py(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_counts_py_files_and_lines(tmp_path):
    _make_py(tmp_path / "main.py", ["import os", "", "# 注释行", "print('hi')"])
    _make_py(tmp_path / "core" / "app.py", ["def run():", "    return 1"])

    stats = collect_stats(tmp_path)

    assert stats["total_py_files"] == 2
    # 空行与 # 注释行不计入代码行：2 + 2 = 4
    assert stats["total_code_lines"] == 4


def test_counts_test_files(tmp_path):
    _make_py(tmp_path / "test_config.py", ["x = 1"])
    _make_py(tmp_path / "tests" / "test_stats.py", ["y = 2"])
    _make_py(tmp_path / "config.py", ["z = 3"])

    stats = collect_stats(tmp_path)

    assert stats["test_files"] == 2
    assert stats["total_py_files"] == 3


def test_writes_json_report(tmp_path):
    _make_py(tmp_path / "main.py", ["a = 1"])
    _make_py(tmp_path / "test_main.py", ["b = 2"])

    out = tmp_path / "output" / "project_stats.json"
    stats = collect_stats(tmp_path)
    write_report(stats, out)

    assert out.exists()
    data = json.loads(out.read_text(encoding="utf-8"))
    assert isinstance(data["total_py_files"], int)
    assert isinstance(data["total_code_lines"], int)
    assert isinstance(data["test_files"], int)
    assert data["total_py_files"] == 2
    assert data["total_code_lines"] == 2
    assert data["test_files"] == 1


def test_excludes_venv_and_node_modules(tmp_path):
    _make_py(tmp_path / "app.py", ["a = 1"])
    _make_py(tmp_path / "venv" / "lib" / "site_pkg.py", ["b = 2"])
    _make_py(tmp_path / "node_modules" / "pkg.py", ["c = 3"])
    _make_py(tmp_path / ".git" / "hook.py", ["d = 4"])
    _make_py(tmp_path / "dist" / "ycode" / "_internal" / "pkg.py", ["e = 5"])
    _make_py(tmp_path / "build" / "lib" / "core.py", ["f = 6"])
    _make_py(tmp_path / "mytemp" / "scratch.py", ["g = 7"])

    stats = collect_stats(tmp_path)

    assert stats["total_py_files"] == 1
    assert stats["total_code_lines"] == 1
