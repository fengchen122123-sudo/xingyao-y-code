"""按关键词删除记忆的两阶段落地：先 scan_matches 预览，再 delete_matches 真删。

删除范围被死限定为「当日记忆日志 + MEMORY.md 索引」这两处的命中行，理由：

- 分类记忆文件（user/ feedback/ project/ reference/ 下的 *.md）是结构化的事实与偏好。
  关键词出现在正文里往往只是"提到"，并不等于用户要抹掉整条记忆；按关键词删整文件
  会连带销毁同一条记忆里的其他事实，属于不可逆的信息损失。
- 历史日志（logs/<年>/<月>/<其他日期>.md）是时间线本身。跨天删除会让"某天发生了什么"
  永久缺失，事后既难核对是哪次操作造成的，也无法从别处还原。
- 所以 /forget 只清理"刚写进今天日记账与索引的那几条"，是最小集合操作。
- 匹配也限定为「单行内子串命中」：模糊/跨行匹配会让一处命中波及相邻上下文，
  删除边界不可预期；整行删除的边界是用户在预览样例里能肉眼核对的。

两个函数都是纯文件操作（只依赖传入路径），因此可以完全用 tmp_path 单测，
不需要碰真实的 ~/.ycode/memory 目录。
"""

import os
import tempfile
from pathlib import Path

__all__ = ["scan_matches", "delete_matches"]

# 预览样例上限：确认阶段只回显少量原文，够用户判断"命中的是不是自己想删的东西"，
# 又不会把整篇日志灌进一张提示卡片。
MAX_SAMPLES = 3
# 样例单行最大字符数（超长行截断，避免预览卡片被一行几百字的日志撑变形）。
MAX_SAMPLE_CHARS = 80


def _normalize_keyword(keyword) -> str:
    """校验并规范化关键词：去首尾空白后必须非空，否则抛 ValueError。

    空关键词必须在这里就拦住：行匹配是"关键词子串命中"，空串是所有行的子串，
    放行等于把当天日志与整个索引整片删掉。
    """
    value = str(keyword or "").strip()
    if not value:
        raise ValueError("关键词不能为空")
    return value


def _read_lines(path: Path) -> list:
    """读取文件的逻辑行；文件不存在时返回空列表（视为 0 命中，不抛错）。

    splitlines() 会把 \r\n / \n / \r 统一拆成行内容，写回时我们统一用 \n，
    因此行内容本身不受换行风格影响。
    """
    try:
        if not path.exists():
            return []
        return path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        # 与 exists() 检查之间的竞态：文件刚被别处删掉，同样按 0 处理。
        return []


def _is_hit(line: str, keyword: str) -> bool:
    """单行命中判定：跳过空行，其余按关键词子串命中原样比较（不做模糊匹配）。"""
    if not line.strip():
        return False
    return keyword in line


def _sample_of(line: str) -> str:
    """生成预览样例：去首尾空白，超长截断到 MAX_SAMPLE_CHARS 以内。"""
    text = line.strip()
    if len(text) > MAX_SAMPLE_CHARS:
        # 留一个省略号让用户知道后面还有内容（总长仍不超过上限）。
        text = text[: MAX_SAMPLE_CHARS - 1] + "…"
    return text


def _atomic_write_lines(path: Path, lines: list) -> None:
    """原子写回：先写同目录临时文件再 os.replace，避免中途失败留下半截文件。

    同目录是硬要求——os.replace 只有跨同分区才是原子的，临时文件放到系统临时目录
    会在跨卷时退化成"复制 + 删除"。
    """
    text = "\n".join(lines) + "\n" if lines else ""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        # 任何失败都要清掉临时文件，否则记忆目录里会残留 .tmp 垃圾。
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _count_hits(lines: list, keyword: str) -> int:
    return sum(1 for line in lines if _is_hit(line, keyword))


def scan_matches(keyword, *, log_path: Path, index_path: Path) -> dict:
    """只读扫描：统计两处文件里的命中行数，并给出最多 3 条样例。

    返回值 {"log_lines": n, "index_lines": m, "samples": [...]}；
    不修改任何文件，确认阶段用它生成"命中 N 行"的提示。
    """
    kw = _normalize_keyword(keyword)
    log_lines = _read_lines(Path(log_path))
    index_lines = _read_lines(Path(index_path))

    samples = []
    # 先日志后索引：日志是"今天新增的原始记录"，用户更容易凭原文辨认。
    for line in log_lines:
        if len(samples) >= MAX_SAMPLES:
            break
        if _is_hit(line, kw):
            samples.append(_sample_of(line))
    for line in index_lines:
        if len(samples) >= MAX_SAMPLES:
            break
        if _is_hit(line, kw):
            samples.append(_sample_of(line))

    return {
        "log_lines": _count_hits(log_lines, kw),
        "index_lines": _count_hits(index_lines, kw),
        "samples": samples,
    }


def delete_matches(keyword, *, log_path: Path, index_path: Path) -> dict:
    """真实删除命中行并原子写回，返回两处各删了多少行。

    只删这两处文件里的命中行：分类记忆文件与历史日志不在参数范围内，调用方拿不到
    也就不可能误删（范围约束见模块注释）。
    文件不存在、或一条都没命中时不写文件，保证"没命中"不会改动原文件（含换行风格）。
    """
    kw = _normalize_keyword(keyword)
    log_file = Path(log_path)
    index_file = Path(index_path)

    stats = {"log_lines": 0, "index_lines": 0}
    for key, target in (("log_lines", log_file), ("index_lines", index_file)):
        lines = _read_lines(target)
        if not lines:
            continue
        kept = [line for line in lines if not _is_hit(line, kw)]
        removed = len(lines) - len(kept)
        if not removed:
            continue
        _atomic_write_lines(target, kept)
        stats[key] = removed
    return stats
