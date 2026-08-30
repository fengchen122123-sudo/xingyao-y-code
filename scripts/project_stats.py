# -*- coding: utf-8 -*-
"""项目 Python 代码统计脚本。

递归统计 .py 文件指标并输出 JSON 报告：
- total_py_files: .py 文件总数（跳过 venv / node_modules / .git / dist / build / mytemp）
- total_code_lines: 非空、非纯注释（strip 后以 # 开头）的代码行总数
- test_files: 文件名以 test_ 开头的 .py 文件数量

用法：
    venv\\Scripts\\python.exe scripts\\project_stats.py
"""

import json
import sys
from pathlib import Path

EXCLUDED_DIRS = {"venv", "node_modules", ".git", "dist", "build", "mytemp"}


def _count_code_lines(path: Path) -> int:
    """统计单个文件的代码行数：strip 后为空或以 # 开头的行不计。"""
    count = 0
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            count += 1
    return count


def collect_stats(root_dir: Path) -> dict:
    """递归统计 root_dir 下的 Python 代码指标，返回统计字典。"""
    root = Path(root_dir)
    total_py_files = 0
    total_code_lines = 0
    test_files = 0

    for path in root.rglob("*.py"):
        if any(part in EXCLUDED_DIRS for part in path.parts):
            continue
        total_py_files += 1
        total_code_lines += _count_code_lines(path)
        if path.name.startswith("test_"):
            test_files += 1

    return {
        "total_py_files": total_py_files,
        "total_code_lines": total_code_lines,
        "test_files": test_files,
    }


def write_report(stats: dict, output_path: Path) -> None:
    """将统计结果写入 JSON 文件（UTF-8，indent=2）。"""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    output = root / "output" / "project_stats.json"

    stats = collect_stats(root)
    write_report(stats, output)

    print(
        f"统计完成: {stats['total_py_files']} 个 .py 文件, "
        f"{stats['total_code_lines']} 行代码, "
        f"{stats['test_files']} 个测试文件 -> {output}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
