from __future__ import annotations

import hashlib
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

__all__ = [
    "SECTION_STACK",
    "SECTION_ARCHITECTURE",
    "SECTION_CONSTRAINT",
    "SECTION_PITFALL",
    "SECTION_PREFERENCE",
    "SECTION_TASK_LOG",
    "SECTION_ALLOWED",
    "SECTION_ORDER",
    "INJECT_SECTIONS",
    "DEFAULT_INJECTION_LIMIT",
    "STATE_HEADER",
    "STATE_STALE_DAYS",
    "MAX_SECTION_CONTENT_CHARS",
    "projects_root",
    "normalize_workdir",
    "profile_path",
    "read_profile",
    "append_section",
    "append_task_log",
    "build_injection",
    "build_state_section",
    "read_state",
    "set_section",
]

logger = logging.getLogger(__name__)

SECTION_STACK = "技术栈"
SECTION_ARCHITECTURE = "架构决策"
SECTION_CONSTRAINT = "硬约束"
SECTION_PITFALL = "踩坑记录"
SECTION_PREFERENCE = "用户偏好"
SECTION_TASK_LOG = "任务流水"

SECTION_ALLOWED = (
    SECTION_STACK,
    SECTION_ARCHITECTURE,
    SECTION_CONSTRAINT,
    SECTION_PITFALL,
    SECTION_PREFERENCE,
)

SECTION_ORDER = SECTION_ALLOWED + (SECTION_TASK_LOG,)

#: 派活时自动带入的章节：只带"约束类"信息，踩坑与偏好留给秘书按需查
INJECT_SECTIONS = (SECTION_STACK, SECTION_ARCHITECTURE, SECTION_CONSTRAINT)

DEFAULT_INJECTION_LIMIT = 800

STATE_HEADER = "【项目状态】以下为本工作目录已记录的项目背景（自动注入，无需向用户复述来源）："

STATE_STALE_DAYS = 30

#: 单条记录的内容上限，防止一轮把画像写成日志
MAX_SECTION_CONTENT_CHARS = 1200

_TIME_FMT = "%Y-%m-%d %H:%M:%S"
_HASH_CHARS = 12
_PROFILE_SUFFIX = ".md"
_HEADING_RE = re.compile(r"^##\s+(.+?)\s*$")
_DRIVE_RE = re.compile(r"^([A-Za-z]):")


def _data_dir() -> Path:
    """数据根目录：``YCODE_DATA_DIR`` 优先，否则 ``~/.ycode``（调用时解析）。"""
    override = str(os.environ.get("YCODE_DATA_DIR") or "").strip()
    if override:
        return Path(override)
    return Path(os.path.expanduser("~")) / ".ycode"


def projects_root() -> Path:
    """画像目录 ``<数据根>/memory/projects``（不保证已存在；写盘时再创建）。"""
    return _data_dir() / "memory" / "projects"


def normalize_workdir(workdir) -> str:
    raw = str(workdir or "").strip().strip('"').strip("'").strip()
    if not raw:
        return ""
    if raw.upper() in {"GLOBAL", "SECRETARY"}:
        return ""
    text = raw.replace("\\", "/")
    is_abs = text.startswith("/")
    folded: List[str] = []
    for part in text.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if folded and folded[-1] != "..":
                folded.pop()
                continue
            folded.append(part)
            continue
        folded.append(part)
    normalized = "/".join(folded)
    if is_abs:
        normalized = "/" + normalized
    match = _DRIVE_RE.match(normalized)
    if match:
        normalized = match.group(1).upper() + normalized[1:]
    return normalized


def profile_path(workdir) -> Optional[Path]:
    normalized = normalize_workdir(workdir)
    if not normalized:
        return None
    digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:_HASH_CHARS]
    return projects_root() / (digest + _PROFILE_SUFFIX)


def _project_name(normalized: str) -> str:
    text = str(normalized or "").rstrip("/")
    if not text:
        return normalized
    name = text.rsplit("/", 1)[-1]
    if len(name) == 2 and name[1] == ":":
        # 只有盘符（D:）时退回完整规范化路径，避免显示成一个字母冒号
        return normalized
    return name or normalized


def _strip_frontmatter(text: str) -> str:
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        for index in range(1, len(lines)):
            if lines[index].strip() == "---":
                return "\n".join(lines[index + 1:])
    return text


def _split_blocks(text: str) -> List[Tuple[str, str]]:
    blocks: List[Tuple[str, List[str]]] = [(("", []))]
    current_name = ""
    current_lines: List[str] = []
    for line in text.splitlines():
        match = _HEADING_RE.match(line.strip())
        if match:
            blocks.append((current_name, current_lines))
            current_name = match.group(1).strip()
            current_lines = []
            continue
        current_lines.append(line)
    blocks.append((current_name, current_lines))
    out: List[Tuple[str, str]] = []
    for name, lines in blocks:
        body = "\n".join(lines).strip()
        if name or body:
            out.append((name, body))
    return out


def _section_map(text: str) -> dict:
    merged: dict = {}
    for name, body in _split_blocks(_strip_frontmatter(text)):
        if not name:
            continue
        if name in merged and merged[name]:
            merged[name] = (merged[name] + "\n" + body).strip() if body else merged[name]
        else:
            merged[name] = body
    return merged


def _render(
    normalized: str,
    known: dict,
    extras: List[Tuple[str, str]],
    preamble: str,
    stamp: str,
) -> str:
    lines = ["---", "workdir: %s" % normalized, "updated: %s" % stamp, "---"]
    if preamble.strip():
        lines += ["", preamble.strip()]
    for name in SECTION_ORDER:
        lines += ["", "## %s" % name]
        body = str(known.get(name) or "").strip()
        if body:
            lines += ["", body]
    for name, body in extras:
        lines += ["", "## %s" % name]
        if body.strip():
            lines += ["", body.strip()]
    return "\n".join(lines).rstrip() + "\n"


def _template(normalized: str, stamp: str) -> str:
    return _render(normalized, {}, [], "", stamp)


def _bullet_block(content: str) -> str:
    """把一段内容变成条目块：首行 ``- xxx``，后续行缩进两格保持同一条目。"""
    lines = [line.rstrip() for line in str(content or "").strip().splitlines()]
    lines = [line for line in lines if line.strip()]
    if not lines:
        return ""
    out = ["- " + lines[0].strip()]
    for line in lines[1:]:
        out.append("  " + line.strip())
    return "\n".join(out)


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except OSError as exc:
        logger.debug("读取项目画像失败：%s（%s）", path, exc)
        return ""
    except Exception as exc:
        logger.debug("读取项目画像异常：%s（%s）", path, exc)
        return ""


def read_profile(workdir) -> str:
    """读某个项目的画像全文；不存在 / workdir 为空 / 读失败都返回空串。"""
    path = profile_path(workdir)
    if path is None:
        return ""
    return _read_text(path)


def _within_projects(path: Path) -> bool:
    """写盘前的越界校验：目标必须落在 ``projects_root()`` 之内。"""
    try:
        root_abs = os.path.abspath(str(projects_root()))
        path_abs = os.path.abspath(str(path))
    except Exception:
        return False
    try:
        common = os.path.commonpath([os.path.normcase(path_abs), os.path.normcase(root_abs)])
    except ValueError:
        return False
    return common == os.path.normcase(root_abs)


def _write_text(path: Path, text: str) -> bool:
    """写文件；越界、目录创建失败、写入失败都只记 debug 并返回 False。"""
    if not _within_projects(path):
        logger.debug("项目画像写入被拒绝（目标不在 projects 目录内）：%s", path)
        return False
    try:
        os.makedirs(str(path.parent), exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return True
    except Exception as exc:
        logger.debug("写入项目画像失败：%s（%s）", path, exc)
        return False


def _append(workdir, section_name: str, block: str) -> str:
    path = profile_path(workdir)
    if path is None or not block:
        return ""
    normalized = normalize_workdir(workdir)
    stamp = datetime.now().strftime(_TIME_FMT)
    existing = _read_text(path)
    known: dict = {}
    extras: List[Tuple[str, str]] = []
    preamble = ""
    for name, body in _split_blocks(_strip_frontmatter(existing)):
        if not name:
            preamble = body
            continue
        if name in SECTION_ORDER:
            if name in known and known[name]:
                known[name] = (known[name] + "\n" + body).strip() if body else known[name]
            else:
                known[name] = body
            continue
        extras.append((name, body))
    previous = str(known.get(section_name) or "").strip()
    known[section_name] = (previous + "\n" + block).strip() if previous else block
    text = _render(normalized, known, extras, preamble, stamp)
    if not _write_text(path, text):
        return ""
    return text


def append_section(workdir, section, content) -> str:
    section_name = str(section or "").strip()
    if section_name not in SECTION_ALLOWED:
        raise ValueError(
            "不支持的章节：%s；可选：%s"
            % (section_name or "(空)", "、".join(SECTION_ALLOWED))
        )
    body = str(content or "").strip()
    if not body:
        raise ValueError("内容不能为空")
    block = _bullet_block(body)
    if not block:
        raise ValueError("内容不能为空")
    if not _append(workdir, section_name, block):
        return ""
    return "已记入「%s」的「%s」" % (
        _project_name(normalize_workdir(workdir)),
        section_name,
    )


def append_task_log(workdir, line) -> None:
    try:
        text = " ".join(str(line or "").split())
        if not text:
            return
        block = text if text.startswith("- ") else "- " + text
        _append(workdir, SECTION_TASK_LOG, block)
    except Exception as exc:
        logger.debug("追加项目任务流水失败：%s", exc)


def build_injection(workdir, limit: int = DEFAULT_INJECTION_LIMIT) -> str:
    try:
        parsed_limit = int(limit)
    except Exception:
        parsed_limit = DEFAULT_INJECTION_LIMIT
    if parsed_limit <= 0:
        parsed_limit = DEFAULT_INJECTION_LIMIT
    text = read_profile(workdir)
    if not text.strip():
        return ""
    sections = _section_map(text)
    parts: List[str] = []
    for name in INJECT_SECTIONS:
        body = str(sections.get(name) or "").strip()
        if not body:
            continue
        parts.append("## %s\n%s" % (name, body))
    if not parts:
        return ""
    injection = "\n\n".join(parts).strip()
    if len(injection) > parsed_limit:
        injection = injection[: max(0, parsed_limit - 1)].rstrip() + "…"
    return injection


def read_state(workdir) -> dict:
    empty = {
        "workdir": normalize_workdir(workdir),
        "exists": False,
        "updated": "",
        "sections": {name: "" for name in INJECT_SECTIONS},
    }
    try:
        text = read_profile(workdir)
        if not text.strip():
            return empty
        sections = _section_map(text)
        updated = _frontmatter_updated(text)
        return {
            "workdir": normalize_workdir(workdir),
            "exists": True,
            "updated": updated.strftime(_TIME_FMT) if updated else "",
            "sections": {
                name: str(sections.get(name) or "").strip() for name in INJECT_SECTIONS
            },
        }
    except Exception as exc:
        logger.debug("读取项目状态失败：%s", exc)
        return empty


def set_section(workdir, section, content) -> bool:
    section_name = str(section or "").strip()
    if section_name not in SECTION_ALLOWED:
        raise ValueError(
            "不支持的章节：%s；可选：%s"
            % (section_name or "(空)", "、".join(SECTION_ALLOWED))
        )
    path = profile_path(workdir)
    if path is None:
        return False
    normalized = normalize_workdir(workdir)
    stamp = datetime.now().strftime(_TIME_FMT)
    existing = _read_text(path)
    known: dict = {}
    extras: List[Tuple[str, str]] = []
    preamble = ""
    for name, body in _split_blocks(_strip_frontmatter(existing)):
        if not name:
            preamble = body
            continue
        if name in SECTION_ORDER:
            if name in known and known[name]:
                known[name] = (known[name] + "\n" + body).strip() if body else known[name]
            else:
                known[name] = body
            continue
        extras.append((name, body))
    known[section_name] = str(content or "").strip()
    text = _render(normalized, known, extras, preamble, stamp)
    return _write_text(path, text)


def _frontmatter_updated(text: str) -> Optional[datetime]:
    lines = str(text or "").splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for line in lines[1:]:
        stripped = line.strip()
        if stripped == "---":
            break
        if not stripped.startswith("updated:"):
            continue
        raw = stripped[len("updated:"):].strip()
        for fmt in (_TIME_FMT, "%Y-%m-%d"):
            try:
                return datetime.strptime(raw, fmt)
            except ValueError:
                continue
        return None
    return None


def build_state_section(workdir, limit: int = DEFAULT_INJECTION_LIMIT) -> str:
    try:
        try:
            parsed_limit = int(limit)
        except Exception:
            parsed_limit = DEFAULT_INJECTION_LIMIT
        if parsed_limit <= 0:
            parsed_limit = DEFAULT_INJECTION_LIMIT
        text = read_profile(workdir)
        if not text.strip():
            return ""
        sections = _section_map(text)
        if not any(str(sections.get(name) or "").strip() for name in INJECT_SECTIONS):
            return ""
        prefix: List[str] = [STATE_HEADER]
        updated = _frontmatter_updated(text)
        if updated is not None:
            try:
                age_days = (datetime.now() - updated).days
            except Exception:
                age_days = 0
            if age_days > STATE_STALE_DAYS:
                prefix.append(
                    "（注意：本画像最近更新于 %s，已超过 %d 天，状态可能已过期，请与用户确认。）"
                    % (updated.strftime("%Y-%m-%d"), STATE_STALE_DAYS)
                )
        occupied = sum(len(item) for item in prefix) + len(prefix)
        budget = parsed_limit - occupied
        if budget <= 0:
            return ""
        body = build_injection(workdir, budget)
        if not body:
            return ""
        return "\n".join(prefix + [body])
    except Exception as exc:
        logger.debug("构建项目状态注入块失败：%s", exc)
        return ""
