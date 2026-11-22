import os
import tempfile
from pathlib import Path

__all__ = ["scan_matches", "delete_matches"]

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
    try:
        if not path.exists():
            return []
        return path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        # 与 exists() 检查之间的竞态：文件刚被别处删掉，同样按 0 处理。
        return []


def _is_hit(line: str, keyword: str) -> bool:
    if not line.strip():
        return False
    return keyword in line


def _sample_of(line: str) -> str:
    text = line.strip()
    if len(text) > MAX_SAMPLE_CHARS:
        text = text[: MAX_SAMPLE_CHARS - 1] + "…"
    return text


def _atomic_write_lines(path: Path, lines: list) -> None:
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
    kw = _normalize_keyword(keyword)
    log_lines = _read_lines(Path(log_path))
    index_lines = _read_lines(Path(index_path))

    samples = []
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
