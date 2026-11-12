"""配对评测的最小运行器（S2）。

设计见 docs/2026-11-12-pi-inspired-optimization-design.md 第 5 节。

本版只实现**可确定性验证的部分**：
- 用例加载与校验（evals/cases/<suite>/<id>.json）
- 5 种确定性判据（file_exists / file_not_exists / file_contains /
  file_not_contains / tool_called）
- dry-run 执行：不调用任何模型，用 `--simulate A=1,0` 注入每条 arm 的结果；
  没有 simulate 时直接把判据对着 fixture 跑一遍
- 配对统计与门禁：复用 core/eval_stats，退出码 0=结论成立 / 2=结论不成立

真实 provider 调用**刻意未实现**：函数体显式抛 NotImplementedError，
避免留下一个看起来能跑、实际上会烧钱或产生假数据的半成品。
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable, Mapping

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CASES_DIR = "evals/cases"
DEFAULT_OUT_DIR = "evals/reports"

ASSERTION_TYPES = (
    "file_exists",
    "file_not_exists",
    "file_contains",
    "file_not_contains",
    "tool_called",
)


class EvalCaseError(ValueError):
    """用例本身不合法（加载期就该失败，不要静默跳过）。"""


def _resolve_inside(workdir: Path, relative: str) -> Path | None:
    """把用例里的相对路径限制在 workdir 内；越界返回 None（判失败，不抛异常）。"""
    try:
        candidate = (workdir / str(relative)).resolve()
        root = workdir.resolve()
    except OSError:
        return None
    return candidate if candidate == root or root in candidate.parents else None


def load_cases(cases_dir: Path) -> list[dict[str, Any]]:
    """读 cases_dir/*/*.json，校验并返回；结构不合法直接抛 EvalCaseError。"""
    cases: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path in sorted(cases_dir.glob("*/*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise EvalCaseError(f"{path}: 无法解析 JSON（{exc}）") from exc
        if not isinstance(data, dict):
            raise EvalCaseError(f"{path}: 顶层必须是对象")
        case_id = data.get("id")
        if not isinstance(case_id, str) or not case_id.strip():
            raise EvalCaseError(f"{path}: 缺少 id")
        if case_id in seen:
            raise EvalCaseError(f"{path}: id 重复（{case_id}）")
        seen.add(case_id)
        assertions = data.get("assertions")
        if not isinstance(assertions, list) or not assertions:
            raise EvalCaseError(f"{path}: assertions 不能为空")
        for item in assertions:
            kind = item.get("type") if isinstance(item, dict) else None
            if kind not in ASSERTION_TYPES:
                raise EvalCaseError(f"{path}: 未知 assertion type（{kind!r}）")
            if kind == "file_not_contains":
                try:
                    re.compile(str(item.get("pattern", "")))
                except re.error as exc:
                    raise EvalCaseError(f"{path}: file_not_contains 正则非法（{exc}）") from exc
        data["_path"] = str(path)
        cases.append(data)
    if not cases:
        raise EvalCaseError(f"{cases_dir}: 没有找到任何用例")
    return cases


def check_assertions(
    workdir: Path, assertions: Iterable[Mapping[str, Any]], tool_calls: Iterable[str] = ()
) -> tuple[bool, str]:
    """逐条判定；全部通过才通过，返回 (passed, 首个失败原因)。"""
    called = {str(name) for name in tool_calls}
    for item in assertions:
        kind = item.get("type")
        if kind == "tool_called":
            name = str(item.get("name", ""))
            if name not in called:
                return False, f"tool_called 未命中：{name}"
            continue

        target = _resolve_inside(workdir, str(item.get("path", "")))
        if target is None:
            return False, f"{kind}: 路径越界（{item.get('path')}）"

        if kind == "file_exists":
            if not target.is_file():
                return False, f"file_exists 失败：{item.get('path')}"
        elif kind == "file_not_exists":
            if target.exists():
                return False, f"file_not_exists 失败：{item.get('path')}"
        elif kind == "file_contains":
            # 用 utf-8（非 utf-8-sig）读，BOM 才能以 \ufeff 留在文本里被判据看见
            text = target.read_text(encoding="utf-8", errors="replace") if target.is_file() else ""
            if str(item.get("value", "")) not in text:
                return False, f"file_contains 失败：{item.get('path')}"
        elif kind == "file_not_contains":
            text = target.read_text(encoding="utf-8", errors="replace") if target.is_file() else ""
            if re.search(str(item.get("pattern", "")), text, re.MULTILINE):
                return False, f"file_not_contains 失败：{item.get('path')}"
        else:  # pragma: no cover - 加载期已拦
            return False, f"未知判据：{kind}"
    return True, ""


def run_case(case: Mapping[str, Any], profile: str, *, dry_run: bool) -> bool:
    """执行一次 arm；真实 provider 调用未实现，dry-run 只跑判据。"""
    if not dry_run:
        raise NotImplementedError("real provider runner not implemented; use --dry-run")
    fixture = case.get("fixture")
    workdir = Path(tempfile.mkdtemp(prefix="eval-"))
    try:
        if fixture:
            source = (Path(str(case["_path"])).parent / str(fixture)).resolve()
            if not source.exists():
                source = REPO_ROOT / str(fixture)
            if source.exists():
                if source.is_dir():
                    shutil.copytree(source, workdir, dirs_exist_ok=True)
                else:
                    shutil.copy2(source, workdir / source.name)
        passed, _detail = check_assertions(workdir, case.get("assertions", []))
        return passed
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def parse_simulate(items: Iterable[str]) -> dict[str, list[bool]]:
    """--simulate A=1,0,1 → {"A": [True, False, True]}。"""
    result: dict[str, list[bool]] = {}
    for item in items:
        profile, _, raw = str(item).partition("=")
        if not profile or not raw:
            raise SystemExit(f"--simulate 格式应为 A=1,0（收到 {item!r}）")
        try:
            result[profile] = [bool(int(part)) for part in raw.split(",") if part != ""]
        except ValueError as exc:
            raise SystemExit(f"--simulate 只能包含 0/1（收到 {raw!r}）") from exc
    return result


def render_report(summary: Mapping[str, Any], profiles: tuple[str, str]) -> str:
    lines = [
        "# 配对评测报告",
        "",
        f"- 对比：{profiles[0]} vs {profiles[1]}",
        f"- 配对 case：{summary['paired']}（informative {summary['informative']} / "
        f"saturated {summary['saturated']} / flaky {summary['flaky']} / blocked {summary['blocked']}）",
        f"- delta：{summary['delta']:.3f}",
        f"- 置信区间：{summary['ci'] if summary['ci'] else '样本不足'}",
        f"- 结论：{summary['verdict']} {summary.get('reason') or ''}".strip(),
        "",
        "| case | A 通过率 | B 通过率 | delta | 状态 |",
        "| --- | ---: | ---: | ---: | --- |",
    ]
    for case_id, item in summary["cases"].items():
        lines.append(
            f"| {case_id} | {item['a_rate']} | {item['b_rate']} | {item['delta']} | {item['status']} |"
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="eval_run", description="配对评测运行器（dry-run 版）")
    parser.add_argument("--cases", default=DEFAULT_CASES_DIR)
    parser.add_argument("--profile", action="append", default=[], help="形如 A=config-a.json，需两个")
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--simulate", action="append", nargs="+", default=[], help="形如 A=1,0,1（可一次给多个）")
    parser.add_argument("--out", default=DEFAULT_OUT_DIR)
    args = parser.parse_args(argv)

    from core.eval_stats import ArmRun, gate_exit_code, group_runs, summarize

    if not args.dry_run:
        print("real provider runner not implemented; use --dry-run")
        return 1
    profiles = [item.partition("=")[0] for item in args.profile] or ["A", "B"]
    if len(profiles) < 2:
        print("need two --profile arguments")
        return 1
    profile_a, profile_b = profiles[0], profiles[1]

    cases = load_cases(Path(args.cases))
    simulate = parse_simulate([item for group in args.simulate for item in group])

    runs: list[ArmRun] = []
    for case in cases:
        for profile in (profile_a, profile_b):
            sequence = simulate.get(profile)
            if sequence is not None and len(sequence) < args.runs:
                print(f"--simulate {profile} 的序列短于 --runs {args.runs}")
                return 1
            for index in range(args.runs):
                passed = (
                    sequence[index]
                    if sequence is not None
                    else run_case(case, profile, dry_run=True)
                )
                runs.append(ArmRun(str(case["id"]), profile, index, passed))

    summary = summarize(group_runs(runs, profile_a, profile_b))
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    (out_dir / f"{stamp}.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (out_dir / f"{stamp}.md").write_text(
        render_report(summary, (profile_a, profile_b)), encoding="utf-8"
    )

    print(f"paired={summary['paired']} informative={summary['informative']} delta={summary['delta']:.3f}")
    print(f"verdict={summary['verdict']} {summary.get('reason') or ''}".strip())
    print(f"report: {out_dir / stamp}.md")
    return gate_exit_code(summary)


if __name__ == "__main__":
    sys.exit(main())