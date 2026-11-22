#!/usr/bin/env python3

from __future__ import annotations

import argparse
import ast
import json
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ROOT = "core"
DEFAULT_BASELINE_PATH = "docs/architecture_baseline.json"
DEFAULT_MAP_PATH = "docs/module_map.md"
NEW_FILE_LINE_THRESHOLD = 800
GROWTH_ALERT_RATIO = 1.1
BASELINE_SCHEMA = 1
SUMMARY_MAX_CHARS = 120


@dataclass(frozen=True)
class Alert:
    """一条基线告警。kind: "new_file"（新增超限）| "growth"（存量增长超阈值）。"""

    kind: str
    path: str
    current: int
    baseline: int | None = None


@dataclass(frozen=True)
class ModuleInfo:
    module: str
    path: str
    lines: int
    summary: str | None


def iter_python_files(root: Path) -> list[Path]:

    def sort_key(path: Path) -> str:
        return path.relative_to(root).as_posix()

    files = [p for p in root.rglob("*.py") if "__pycache__" not in p.relative_to(root).parts]
    return sorted(files, key=sort_key)


def count_lines(path: Path) -> int:
    """行数定义：splitlines() 长度（空文件 0 行；末行无换行符也计入）。"""
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    return len(text.splitlines())


def scan_tree(root: Path, *, prefix: str | None = None) -> dict[str, int]:
    """扫描目录，返回 {"<prefix>/<相对路径>": 行数}（键有序）。"""
    name = root.name if prefix is None else prefix
    counts = {
        f"{name}/{path.relative_to(root).as_posix()}": count_lines(path)
        for path in iter_python_files(root)
    }
    return dict(sorted(counts.items()))


def find_alerts(current: dict[str, int], baseline: dict[str, int]) -> list[Alert]:
    """对比基线给出告警；baseline 为空视为首次登记（全部为新文件）。"""
    alerts: list[Alert] = []
    for path in sorted(current):
        cur = current[path]
        if path in baseline:
            base = baseline[path]
            # 基线为 0 行的文件（如空 __init__.py）不参与比例告警，避免噪报。
            if base > 0 and cur * 10 > base * 11:
                alerts.append(Alert("growth", path, cur, base))
        elif cur > NEW_FILE_LINE_THRESHOLD:
            alerts.append(Alert("new_file", path, cur))
    return alerts


def build_updated_baseline(current: dict[str, int], baseline: dict[str, int]) -> dict[str, int]:
    """生成新基线：存量保留旧登记值（不前移），新文件按当前值登记。"""
    updated = {
        path: baseline[path] if path in baseline else current[path] for path in sorted(current)
    }
    return updated


def load_baseline(path: Path) -> dict[str, int]:
    """读取基线 files 映射；文件缺失/损坏返回 {}（按首次登记处理）。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    files = data.get("files") if isinstance(data, dict) else None
    if not isinstance(files, dict):
        return {}
    result = {
        key: value
        for key, value in files.items()
        if isinstance(key, str) and isinstance(value, int) and value >= 0
    }
    return dict(sorted(result.items()))


def save_baseline(path: Path, files: dict[str, int], *, root_name: str) -> None:
    """确定性写入基线 JSON（排序键、LF 行尾、无时间戳）。"""
    payload = {
        "schema": BASELINE_SCHEMA,
        "root": root_name,
        "new_file_line_threshold": NEW_FILE_LINE_THRESHOLD,
        "growth_alert_ratio": GROWTH_ALERT_RATIO,
        "note": "由 scripts/architecture_report.py scan 生成；存量行数只登记不前移。",
        "files": dict(sorted(files.items())),
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _module_name(root: Path, prefix: str, path: Path) -> str:
    parts = list(path.relative_to(root).parts)
    if parts[-1] == "__init__.py":
        parts = parts[:-1]
    else:
        parts[-1] = path.stem
    return ".".join([prefix, *parts])


def _longest_module(candidate: str, modules: set[str]) -> str | None:
    """取 candidate 在模块集合中存在的最长前缀（import core.a.b 命中 core.a.b，否则退 core.a…）。"""
    while candidate:
        if candidate in modules:
            return candidate
        candidate = candidate.rpartition(".")[0]
    return None


def _resolve_relative(package: str, level: int) -> str | None:
    parts = package.split(".")
    if level < 1 or level > len(parts):
        return None
    base = parts[: len(parts) - (level - 1)]
    return ".".join(base) if base else None


def _dependency_targets(stem: str, aliases: list[str], modules: set[str]) -> set[str]:
    targets = set()
    for name in aliases:
        if name == "*":
            continue
        target = _longest_module(f"{stem}.{name}", modules)
        if target:
            targets.add(target)
    if not targets:
        fallback = _longest_module(stem, modules)
        if fallback:
            targets.add(fallback)
    return targets


def collect_dependencies(
    root: Path, *, prefix: str | None = None
) -> tuple[dict[str, set[str]], list[str]]:
    name = root.name if prefix is None else prefix
    files = iter_python_files(root)
    modules = {_module_name(root, name, path): path for path in files}
    module_set = set(modules)
    graph: dict[str, set[str]] = {module: set() for module in sorted(modules)}
    failures: list[str] = []

    for module in sorted(modules):
        path = modules[module]
        try:
            tree = ast.parse(
                path.read_text(encoding="utf-8-sig", errors="replace"), filename=str(path)
            )
        except SyntaxError:
            failures.append(f"{name}/{path.relative_to(root).as_posix()}")
            continue
        is_package = path.name == "__init__.py"
        package = module if is_package else module.rpartition(".")[0]
        edges: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    target = _longest_module(alias.name, module_set)
                    if target:
                        edges.add(target)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    if not package:
                        continue
                    base = _resolve_relative(package, node.level)
                    if base is None:
                        continue
                    stem = f"{base}.{node.module}" if node.module else base
                else:
                    if not node.module:
                        continue
                    stem = node.module
                edges.update(_dependency_targets(stem, [a.name for a in node.names], module_set))
        graph[module] = {edge for edge in edges if edge != module}
    return graph, failures


def find_cycles(graph: dict[str, set[str]]) -> list[list[str]]:
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    comps: list[list[str]] = []
    counter = 0

    for root in sorted(graph):
        if root in index:
            continue
        work: list[tuple[str, object]] = [(root, iter(sorted(graph[root])))]
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        while work:
            node, it = work[-1]
            pushed = False
            for nxt in it:  # type: ignore[union-attr]
                if nxt not in graph:
                    continue
                if nxt not in index:
                    index[nxt] = low[nxt] = counter
                    counter += 1
                    stack.append(nxt)
                    on_stack.add(nxt)
                    work.append((nxt, iter(sorted(graph[nxt]))))
                    pushed = True
                    break
                if nxt in on_stack:
                    low[node] = min(low[node], index[nxt])
            if pushed:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index[node]:
                comp: list[str] = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    comp.append(member)
                    if member == node:
                        break
                if len(comp) > 1:
                    comps.append(sorted(comp))
    return sorted(comps, key=lambda comp: (comp[0], len(comp), comp))


def _is_title_line(line: str, path: Path) -> bool:
    if set(line) <= set("=-_*#"):
        return True
    return " " not in line and line.endswith(path.name)


def extract_docstring_first_line(path: Path) -> str | None:
    """取 docstring 中首个非空、非标题样式行的摘要；无 docstring 或解析失败返回 None。"""
    try:
        tree = ast.parse(
            path.read_text(encoding="utf-8-sig", errors="replace"), filename=str(path)
        )
    except SyntaxError:
        return None
    doc = ast.get_docstring(tree, clean=True)
    if not doc:
        return None
    for line in doc.splitlines():
        line = line.strip()
        if line and not _is_title_line(line, path):
            return line
    return None


def collect_modules(root: Path, *, prefix: str | None = None) -> dict[str, ModuleInfo]:
    """按模块名排序返回 {模块名: ModuleInfo}。"""
    name = root.name if prefix is None else prefix
    infos = {
        _module_name(root, name, path): ModuleInfo(
            module=_module_name(root, name, path),
            path=f"{name}/{path.relative_to(root).as_posix()}",
            lines=count_lines(path),
            summary=extract_docstring_first_line(path),
        )
        for path in iter_python_files(root)
    }
    return dict(sorted(infos.items()))


def _escape_summary(summary: str | None) -> str:
    if not summary:
        return "—"
    text = summary.replace("|", "\\|")
    if len(text) > SUMMARY_MAX_CHARS:
        text = text[: SUMMARY_MAX_CHARS - 1] + "…"
    return text


def render_module_map(root_name: str, modules: dict[str, ModuleInfo]) -> str:
    """渲染模块地图 Markdown（确定性：排序 + LF）。"""
    total_lines = sum(info.lines for info in modules.values())
    lines = [
        f"# 模块地图（{root_name}）",
        "",
        "> 本文件由 `scripts/architecture_report.py map` 自动生成，请勿手工编辑。",
        "> 重新生成：`python scripts/architecture_report.py map`",
        "",
        f"- 模块数：{len(modules)}",
        f"- 行数合计：{total_lines}",
        "",
        "| 模块 | 行数 | 摘要（docstring 首行） |",
        "| --- | ---: | --- |",
    ]
    for info in modules.values():
        lines.append(f"| `{info.path}` | {info.lines} | {_escape_summary(info.summary)} |")
    lines.append("")
    return "\n".join(lines)


def _configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass


def _resolve(path_str: str) -> Path:
    path = Path(path_str)
    return path if path.is_absolute() else REPO_ROOT / path


def cmd_scan(args: argparse.Namespace) -> int:
    root = _resolve(args.root)
    baseline_path = _resolve(args.baseline)
    if not root.is_dir():
        print(f"错误：根目录不存在：{root}")
        return 2
    current = scan_tree(root, prefix=root.name)
    baseline = load_baseline(baseline_path)
    alerts = find_alerts(current, baseline) if baseline else []

    print(f"扫描 {root.name}：{len(current)} 个 Python 文件，共 {sum(current.values())} 行")
    if not baseline:
        print(f"未发现可用基线（{baseline_path}），本次按首次全量登记处理，不产生告警。")
    else:
        new_files = [p for p in current if p not in baseline]
        if new_files:
            print(f"新增文件 {len(new_files)} 个。")
    for alert in alerts:
        if alert.kind == "new_file":
            print(f"[告警] 新增文件超 {NEW_FILE_LINE_THRESHOLD} 行：{alert.path}（{alert.current} 行）")
        else:
            assert alert.baseline is not None
            print(
                f"[告警] 存量文件超基线 +{int((GROWTH_ALERT_RATIO - 1) * 100)}%："
                f"{alert.path}（{alert.current} 行 > 基线 {alert.baseline} 行）"
            )

    if args.check:
        print(f"--check 模式：未写入基线；告警 {len(alerts)} 条。")
        return 1 if alerts else 0

    updated = build_updated_baseline(current, baseline)
    save_baseline(baseline_path, updated, root_name=root.name)
    added = sum(1 for p in updated if p not in baseline)
    print(
        f"基线已写入 {baseline_path}（登记 {len(updated)} 个文件，其中新登记 {added} 个）；"
        f"告警 {len(alerts)} 条。"
    )
    return 1 if alerts else 0


def cmd_deps(args: argparse.Namespace) -> int:
    root = _resolve(args.root)
    if not root.is_dir():
        print(f"错误：根目录不存在：{root}")
        return 2
    graph, failures = collect_dependencies(root, prefix=root.name)
    cycles = find_cycles(graph)
    if args.json:
        payload = {
            "root": root.name,
            "module_count": len(graph),
            "edge_count": sum(len(v) for v in graph.values()),
            "unparsed": sorted(failures),
            "cycles": cycles,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    print(
        f"扫描 {root.name}：{len(graph)} 个模块，"
        f"{sum(len(v) for v in graph.values())} 条包内依赖边，"
        f"检出 {len(cycles)} 个循环依赖组（涉及 {sum(len(c) for c in cycles)} 个模块）。"
    )
    for order, comp in enumerate(cycles, 1):
        print(f"环 {order}（{len(comp)} 个模块）：{', '.join(comp)}")
    if failures:
        print(f"注意：{len(failures)} 个文件无法解析（SyntaxError），已跳过：{', '.join(sorted(failures))}")
    if not cycles:
        print("未检出循环依赖。")
    return 0


def cmd_map(args: argparse.Namespace) -> int:
    root = _resolve(args.root)
    output = _resolve(args.output)
    if not root.is_dir():
        print(f"错误：根目录不存在：{root}")
        return 2
    modules = collect_modules(root, prefix=root.name)
    text = render_module_map(root.name, modules)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text, encoding="utf-8", newline="\n")
    with_doc = sum(1 for info in modules.values() if info.summary)
    print(f"模块地图已写入 {output}（{len(modules)} 个模块，{with_doc} 个含 docstring 摘要）。")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="architecture_report",
        description="架构度量与模块地图（T7）：行数基线 / 循环依赖检测 / 模块地图。",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_scan = sub.add_parser("scan", help="生成/更新行数基线 JSON")
    p_scan.add_argument("--root", default=DEFAULT_ROOT, help="扫描根目录（相对仓库根，默认 core）")
    p_scan.add_argument("--baseline", default=DEFAULT_BASELINE_PATH, help="基线 JSON 路径")
    p_scan.add_argument("--check", action="store_true", help="只检查不写入；有告警返回退出码 1")
    p_scan.set_defaults(func=cmd_scan)

    p_deps = sub.add_parser("deps", help="ast 解析导入并检测循环依赖")
    p_deps.add_argument("--root", default=DEFAULT_ROOT, help="扫描根目录（相对仓库根，默认 core）")
    p_deps.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    p_deps.set_defaults(func=cmd_deps)

    p_map = sub.add_parser("map", help="生成模块地图 Markdown")
    p_map.add_argument("--root", default=DEFAULT_ROOT, help="扫描根目录（相对仓库根，默认 core）")
    p_map.add_argument("--output", default=DEFAULT_MAP_PATH, help="输出 Markdown 路径")
    p_map.set_defaults(func=cmd_map)
    return parser


def main(argv: list[str] | None = None) -> int:
    _configure_stdio()
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())