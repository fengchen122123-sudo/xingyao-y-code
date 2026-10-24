"""命令感知的工具输出精简器（Reducer）。

目的：在历史工具结果被 snip/elide 折叠之前，先按输出特征做一次结构化精简，
把测试/构建/版本控制类输出里的重复行与噪声去掉，只留关键行（失败、摘要、
diff 头等）。相比纯 head+tail 截断，结构化精简能在同一份预算里保留更多
真正有用的信息。

不变量：
1. 纯函数：只读入参、不写库、不碰全局状态；判断只看工具名与输出文本本身
   （调用方拿不到命令行参数，因此规则只能按输出特征识别）。
2. 宁可不动手：精简收益不足 10% 时返回 None，避免无意义的改写破坏前缀缓存。
3. 绝不抛错：任何异常一律返回 None，投影失败不能影响调用方主流程。
4. 可逆性由调用方负责：本模块只产出精简文本，原文归档由 core/memory.py
   的 maintain_tool_results 用 tool_result_archive 完成。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 小于该体积的输出不值得精简（省不了多少 token，还会白改一次历史字节）
MIN_REDUCE_CHARS = 2000

# 命中规则后保留行太多时再截断：前 HEAD_LINES 行 + 省略提示 + 后 TAIL_LINES 行
MAX_KEEP_LINES = 200
HEAD_LINES = 40
TAIL_LINES = 60

# 收益门槛：候选文本长度必须 < 原文的 90%（即至少省 10%）才认为值得改写
MIN_GAIN_RATIO = 0.9

# 连续重复行的默认保留条数
DEFAULT_REPEAT_KEEP = 2

# ANSI 转义序列（颜色/光标控制等）：PowerShell、pytest -v、docker 常带
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")

# 省略提示行（截断保留行时插入）
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
    """折叠连续重复行：保留前 max_repeat_keep 行，后接一行重复提示。

    提示行的 count 是「被折叠掉」的行数（run 长度 - max_repeat_keep）。
    """
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


# 规则表：(名称, 特征正则, 必留正则)。
# - 特征正则用于识别「这是哪一类命令的输出」，只看输出特征，不看命令行参数；
# - 必留正则在每个单行上 search，命中即保留（^ 因此表示行首）。
# 顺序即优先级：越靠前越先匹配。
_RULES: tuple[tuple[str, re.Pattern, re.Pattern], ...] = (
    (
        "pytest",
        re.compile(r"=+ test session starts =+|short test summary info", re.MULTILINE),
        re.compile(
            r"^(FAILED|ERROR|E\s|={2,}|-{2,}|collected|short test summary|\d+ (passed|failed))",
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


def _shrink_kept_lines(kept: list[str]) -> list[str]:
    """必留行过多时做头尾截断，中间插一行省略提示。"""
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

        # 没有规则命中：只做通用去重（连续重复行），收益不足同样返回 None
        candidate = "\n".join(dedupe_adjacent(cleaned.split("\n")))
        if len(candidate) >= original_chars * MIN_GAIN_RATIO:
            return None
        return ReducerResult(text=candidate, reducer="dedupe", original_chars=original_chars)
    except Exception:
        # 精简失败必须静默降级：调用方按原文本继续走 snip/elide
        return None
