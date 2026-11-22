from __future__ import annotations

import re
from dataclasses import dataclass

MIN_REDUCE_CHARS = 2000

MAX_KEEP_LINES = 200
HEAD_LINES = 40
TAIL_LINES = 60

# 收益门槛：候选文本长度必须 < 原文的 90%（即至少省 10%）才认为值得改写
MIN_GAIN_RATIO = 0.9

DEFAULT_REPEAT_KEEP = 2

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")

_OMITTED_HINT = "... (已省略 {count} 行)"

# 精简提示行的固定片段：core/memory.py 用它识别「这条结果已被精简过」，
# 避免同一条历史结果在后续维护轮次里被反复精简/重复归档。
REDUCED_HINT_MARK = "规则精简，完整内容见 #"


@dataclass(frozen=True)
class ReducerResult:
    """精简结果：text 为新文本，reducer 为命中的规则名，original_chars 为原字符数。"""

    text: str
    reducer: str
    original_chars: int


def strip_ansi(text: str) -> str:
    """剥掉 ANSI 转义序列，返回纯文本。"""
    return _ANSI_RE.sub("", text)


def dedupe_adjacent(lines: list[str], *, max_repeat_keep: int = DEFAULT_REPEAT_KEEP) -> list[str]:
    keep = max(1, int(max_repeat_keep))
    result: list[str] = []
    run_line: str | None = None
    run_count = 0

    def flush() -> None:
        nonlocal run_line, run_count
        if run_line is None:
            return
        result.extend([run_line] * min(run_count, keep))
        dropped = run_count - keep
        if dropped > 0:
            result.append(f"... (上一行重复 {dropped} 次)")
        run_line, run_count = None, 0

    for line in lines:
        if run_line is not None and line == run_line:
            run_count += 1
            continue
        flush()
        run_line, run_count = line, 1
    flush()
    return result


_RULES: tuple[tuple[str, re.Pattern, re.Pattern], ...] = (
    (
        "pytest",
        re.compile(r"=+ test session starts =+|short test summary info", re.MULTILINE),
        re.compile(
            r"^(?:FAILED|ERROR|E\s|={2,}|-{2,}|_{2,}|>\s|collected|short test summary|\d+ (?:passed|failed)|Traceback \(most recent call last\):|\s+File \".+\", line \d+|[^\s:]+\.py:\d+:)",
            re.MULTILINE,
        ),
    ),
    (
        "npm",
        re.compile(r"npm (ERR!|WARN)|added \d+ packages", re.MULTILINE),
        re.compile(r"(ERR!|error|added \d+ packages|vulnerabilities|npm (ERR|WARN))", re.MULTILINE),
    ),
    (
        "go test",
        re.compile(r"^(---|===) (RUN|FAIL|PASS)|^ok\s+\S+", re.MULTILINE),
        re.compile(r"^(---|=== RUN|FAIL|ok |PASS|\s+\S+\.go:\d+)", re.MULTILINE),
    ),
    (
        "docker",
        re.compile(
            r"^Step \d+/|^#[0-9]+ (\[|DONE|ERROR)|Successfully (built|tagged)",
            re.MULTILINE,
        ),
        re.compile(r"(^Step \d+|^#[0-9]+ |ERROR|error:|Successfully|DONE)", re.MULTILINE),
    ),
    (
        "git diff",
        re.compile(r"^diff --git ", re.MULTILINE),
        re.compile(r"^(diff --git|@@|\+\+\+|---|index )", re.MULTILINE),
    ),
    (
        "git status",
        re.compile(r"^(On branch|Changes not staged|Untracked files)", re.MULTILINE),
        re.compile(r"^(On branch|Changes|Untracked|modified:|deleted:|new file:)", re.MULTILINE),
    ),
)


_GIT_DIFF_HEADER_RE = re.compile(
    r"^(?:diff --git |@@ |\+\+\+ |--- |index |new file mode |deleted file mode |rename (?:from|to) )"
)
_GIT_DIFF_MAX_CHANGES_PER_HUNK = 8


def _reduce_git_diff_lines(cleaned: str) -> list[str]:
    kept: list[str] = []
    hunk_changes = 0
    hunk_omitted = 0

    def _flush_omitted() -> None:
        nonlocal hunk_omitted
        if hunk_omitted > 0:
            kept.append(f"... (本 hunk 其余 {hunk_omitted} 行 +/- 变更已折叠)")
            hunk_omitted = 0

    for line in cleaned.split("\n"):
        if _GIT_DIFF_HEADER_RE.match(line):
            _flush_omitted()
            hunk_changes = 0
            kept.append(line)
        elif line.startswith(("+", "-")) and not line.startswith(("+++", "---")):
            if hunk_changes < _GIT_DIFF_MAX_CHANGES_PER_HUNK:
                kept.append(line)
                hunk_changes += 1
            else:
                hunk_omitted += 1
    _flush_omitted()
    return kept


def _shrink_kept_lines(kept: list[str]) -> list[str]:
    if len(kept) <= MAX_KEEP_LINES:
        return kept
    omitted = len(kept) - HEAD_LINES - TAIL_LINES
    return kept[:HEAD_LINES] + [_OMITTED_HINT.format(count=omitted)] + kept[-TAIL_LINES:]


def reduce_tool_output(tool_name: str, text: str) -> ReducerResult | None:
    """按输出特征精简工具结果；不值得精简或不识别时返回 None。

    tool_name 只用于日志/调试语义（规则完全由输出特征决定），因为调用方
    拿不到当时的命令行参数。整个函数被 try/except 包住：任何异常都返回
    None，绝不影响调用方的投影/改写流程。
    """
    try:
        if not isinstance(text, str) or len(text) < MIN_REDUCE_CHARS:
            return None
        original_chars = len(text)
        cleaned = strip_ansi(text)

        for reducer_name, feature_re, keep_re in _RULES:
            if not feature_re.search(cleaned):
                continue
            if reducer_name == "git diff":
                kept = _reduce_git_diff_lines(cleaned)
            else:
                kept = [line for line in cleaned.split("\n") if keep_re.search(line)]
            if not kept:
                continue
            kept = _shrink_kept_lines(kept)
            candidate = "\n".join(dedupe_adjacent(kept))
            if len(candidate) >= original_chars * MIN_GAIN_RATIO:
                return None
            return ReducerResult(
                text=candidate,
                reducer=reducer_name,
                original_chars=original_chars,
            )

        candidate = "\n".join(dedupe_adjacent(cleaned.split("\n")))
        if len(candidate) >= original_chars * MIN_GAIN_RATIO:
            return None
        return ReducerResult(text=candidate, reducer="dedupe", original_chars=original_chars)
    except Exception:
        # 精简失败必须静默降级：调用方按原文本继续走 snip/elide
        return None
