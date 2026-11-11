"""项目画像：跨项目结构化长期记忆（秘书升级设计 2026-11-04 第 5 节）。

一个项目（workdir）对应一个 Markdown 文件，落在
``~/.ycode/memory/projects/<sha1(规范化 workdir)[:12]>.md``。文件纯文本、
可读可手改，供秘书在两件事上复用：

1. **派活自动注入**：``dispatch_task`` 把 技术栈 / 架构决策 / 硬约束 三节
   截断后拼在任务正文前，目标会话不用用户再交代一遍背景；
2. **显式记录**：秘书调 ``remember_decision`` 把约束、踩坑、偏好落盘，
   跨会话跨项目可查。

职责边界（与 ``core.secretary_dispatches`` 同风格）：

- 只碰 ``~/.ycode/memory/projects/`` 下的文件，**绝不写项目目录**；每次写盘
  前都用 ``os.path.abspath`` 校验目标仍在 projects 目录内（防 ``../`` 越界）；
- 读失败一律返回空串、写失败只记 ``logger.debug`` 不抛，异常不得把派活或
  回合收尾带崩；
- 唯一的"抛"是**入参校验**（非法章节 / 空内容）：那是调用方的错误，必须让
  它看到可读原因，而不是静默丢弃用户的记录。

数据目录取值与 ``core.memory_kairos`` 同源：``YCODE_DATA_DIR`` 优先，否则
``~/.ycode``；两者都在**调用时**解析（而非 import 时），这样测试重定向
``HOME`` / ``USERPROFILE`` / ``YCODE_DATA_DIR`` 后立即生效。
"""

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

# --- 章节常量 ---------------------------------------------------------------
SECTION_STACK = "技术栈"
SECTION_ARCHITECTURE = "架构决策"
SECTION_CONSTRAINT = "硬约束"
SECTION_PITFALL = "踩坑记录"
SECTION_PREFERENCE = "用户偏好"
#: 任务流水由回合收尾自动追加（纯文本 append，不调模型），不在秘书可写枚举里
SECTION_TASK_LOG = "任务流水"

#: 秘书可通过 ``remember_decision`` 写入的章节（设计第 5.2 节）
SECTION_ALLOWED = (
    SECTION_STACK,
    SECTION_ARCHITECTURE,
    SECTION_CONSTRAINT,
    SECTION_PITFALL,
    SECTION_PREFERENCE,
)

#: 画像正文的章节顺序（模板与重写都按它排，任务流水固定最后）
SECTION_ORDER = SECTION_ALLOWED + (SECTION_TASK_LOG,)

#: 派活时自动带入的章节：只带"约束类"信息，踩坑与偏好留给秘书按需查
INJECT_SECTIONS = (SECTION_STACK, SECTION_ARCHITECTURE, SECTION_CONSTRAINT)

#: 注入正文的默认字符上限（设计第 5.3 节）
DEFAULT_INJECTION_LIMIT = 800

#: 会话级「项目状态」注入块的标题行（每轮现算，不进稳定快照）
STATE_HEADER = "【项目状态】以下为本工作目录已记录的项目背景（自动注入，无需向用户复述来源）："

#: 画像 frontmatter ``updated`` 超过该天数即标注「状态可能已过期」
STATE_STALE_DAYS = 30

#: 单条记录的内容上限，防止一轮把画像写成日志
MAX_SECTION_CONTENT_CHARS = 1200

_TIME_FMT = "%Y-%m-%d %H:%M:%S"
_HASH_CHARS = 12
_PROFILE_SUFFIX = ".md"
_HEADING_RE = re.compile(r"^##\s+(.+?)\s*$")
_DRIVE_RE = re.compile(r"^([A-Za-z]):")


# ---------------------------------------------------------------------------
# 路径解析
# ---------------------------------------------------------------------------
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
    """规范化 workdir 作为哈希键：统一分隔符、折叠 ``.``/``..``、去尾分隔符、
    Windows 盘符大写；空值 / ``GLOBAL``（以及 ``SECRETARY``）返回空串。

    只做词法处理，不碰磁盘——画像键必须在目录还被删掉时依然稳定。
    """
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
            # 相对路径开头的 .. 保留（越界只影响哈希值，落盘位置始终在 projects 下）
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
    """画像文件路径；workdir 为空 / ``GLOBAL`` 时返回 ``None``。

    用 ``None`` 而不是 ``Path("")`` 表示"没有画像"：后者等价于当前目录，
    极易被 ``if path:`` 误判成合法路径。
    """
    normalized = normalize_workdir(workdir)
    if not normalized:
        return None
    digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:_HASH_CHARS]
    return projects_root() / (digest + _PROFILE_SUFFIX)


def _project_name(normalized: str) -> str:
    """画像文件的展示名：workdir 的最后一段（``D:/work/demo`` → ``demo``）。"""
    text = str(normalized or "").rstrip("/")
    if not text:
        return normalized
    name = text.rsplit("/", 1)[-1]
    if len(name) == 2 and name[1] == ":":
        # 只有盘符（D:）时退回完整规范化路径，避免显示成一个字母冒号
        return normalized
    return name or normalized


# ---------------------------------------------------------------------------
# 解析与渲染
# ---------------------------------------------------------------------------
def _strip_frontmatter(text: str) -> str:
    """去掉开头 ``---`` ... ``---`` 的 frontmatter，返回正文。"""
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        for index in range(1, len(lines)):
            if lines[index].strip() == "---":
                return "\n".join(lines[index + 1:])
    return text


def _split_blocks(text: str) -> List[Tuple[str, str]]:
    """正文按 ``## 章节`` 切成 ``[(章节名, 正文)]``；首个 ## 之前的一段章节名为空串。"""
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
    """``{章节名: 正文}``；同名章节按出现顺序拼接（手改重复标题时不丢内容）。"""
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
    """按模板重写整份画像（frontmatter + 六章 + 用户手加的额外章节）。"""
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
    """新建画像时的模板（设计第 5.1 节：frontmatter + 六个空章节）。"""
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


# ---------------------------------------------------------------------------
# 读写
# ---------------------------------------------------------------------------
def _read_text(path: Path) -> str:
    """读文件全文；不存在或读失败返回空串（不抛）。"""
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except OSError as exc:
        logger.debug("读取项目画像失败：%s（%s）", path, exc)
        return ""
    except Exception as exc:  # 编码异常等脏数据同样不该冒泡
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
        # 不同盘符 / 非法路径：直接拒绝
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
    """把已渲染好的条目块追加到指定章节；成功返回全文，失败返回空串。"""
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
    """把 ``content`` 追加到 ``section`` 章节末尾（章节不存在则补建）。

    返回给调用方的可读确认（``已记入「项目名」的「章节」``）；workdir 为空 /
    ``GLOBAL`` 或写盘失败时返回空串（不抛）。

    章节非法或内容为空**抛 ValueError**：这是调用方入参错误，必须让它看到
    可读原因（工具层据此返回给模型），而不是静默丢弃用户要记的东西。
    """
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
    """往「任务流水」追加一行（回合收尾自动调用，失败静默）。

    调用方传完整一行文本（``2026-11-04 #12 「补 README」done：…``）；
    内部补 ``- `` 前缀并把换行压成空格，保证流水始终是一行一条。
    """
    try:
        text = " ".join(str(line or "").split())
        if not text:
            return
        block = text if text.startswith("- ") else "- " + text
        _append(workdir, SECTION_TASK_LOG, block)
    except Exception as exc:
        logger.debug("追加项目任务流水失败：%s", exc)


# ---------------------------------------------------------------------------
# 派活注入
# ---------------------------------------------------------------------------
def build_injection(workdir, limit: int = DEFAULT_INJECTION_LIMIT) -> str:
    """拼出派活用的项目背景块（只含 技术栈 / 架构决策 / 硬约束）。

    ``limit`` 是三节合计的字符上限，超出时截断并补省略号；无画像或三节全空
    返回空串（调用方据此原样投递）。
    """
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


# ---------------------------------------------------------------------------
# 结构化读写（GUI 项目状态面板：查看 + 每节编辑保存）
# ---------------------------------------------------------------------------
def read_state(workdir) -> dict:
    """结构化读画像三节（技术栈 / 架构决策 / 硬约束），给 GUI 面板用。

    返回 ``{"workdir", "exists", "updated", "sections"}``；``exists`` 表示画像
    文件已有内容，``updated`` 是 frontmatter 时间原文（缺失为空串）。任何异常
    都返回同结构的空值，不抛。
    """
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
    """**整节覆盖**写入（GUI 编辑框提交的就是该节新正文）；空内容 = 清空该节。

    与 ``append_section`` 的差别只在"替换 vs 追加"。章节非法抛 ``ValueError``
    （调用方入参错误）；workdir 为空 / 写盘失败（含越界被拒）返回 ``False``。
    """
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


# ---------------------------------------------------------------------------
# 会话级「项目状态」注入（特性 1）
# ---------------------------------------------------------------------------
def _frontmatter_updated(text: str) -> Optional[datetime]:
    """取 frontmatter 里的 ``updated:`` 时间；缺失或格式非法返回 ``None``。"""
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
    """拼出会话级「项目状态」注入块（技术栈 / 架构决策 / 硬约束）。

    与 ``build_injection``（派活用）同源同口径，差别只有三点：

    - 头部带固定标题行，让模型知道这段是自动注入的背景；
    - frontmatter ``updated`` 超过 ``STATE_STALE_DAYS`` 天时补「状态可能已过期」；
    - 总长（标题 + 提示 + 正文）不超过 ``limit``；正文预算扣掉标题占用后
      复用 ``build_injection`` 的截断逻辑。

    任何异常 / 无画像 / 三节全空都返回空串，调用方据此"不注入"。
    """
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
