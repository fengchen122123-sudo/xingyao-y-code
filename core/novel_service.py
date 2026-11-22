from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

NOVEL_MARKER = ".novel.json"

NOVEL_SCHEMA = 1
ENGINE_NAME = "story-ycode"
ENGINE_VERSION = "1.0.0"

NOVEL_DIRS = (
    "设定",
    "设定/角色",
    "大纲",
    "正文",
    "追踪",
    "追踪/逐章记录",
    "对标",
)

AGENTS_SUBDIR = Path(".ycode") / "agents"
REFERENCES_SUBDIR = (
    Path(".ycode") / "skills" / "story-ycode" / "references" / "agent-references"
)

STORY_SETUP_ENV = "YCODE_STORY_SETUP_ROOT"

TOOL_NAME_MAP = {
    "read": "read_file",
    "glob": "glob_files",
    "grep": "search_code",
    "write": "write_file",
    "edit": "edit_file",
    "bash": "execute_command",
    "websearch": "search_news",
    "webfetch": "fetch_url",
}

AGENT_SPECS = (
    {
        "name": "story-architect",
        "when_to_use": "需要题材定位、世界观构建、大纲排布、钩子与反转设计、范围控制审查时",
        "base_type": "general",
        "model": "inherit",
        "upstream_tools": ("Read", "Glob", "Grep", "Write", "Edit"),
    },
    {
        "name": "narrative-writer",
        "when_to_use": "需要写正文、执行情绪弧线、终稿去 AI 味与格式检查时",
        "base_type": "general",
        "model": "inherit",
        "upstream_tools": ("Read", "Glob", "Grep", "Write", "Edit", "Bash"),
    },
    {
        "name": "character-designer",
        "when_to_use": "需要设计角色设定、语言风格档案、动机链、人物弧线与对话时",
        "base_type": "general",
        "model": "inherit",
        "upstream_tools": ("Read", "Glob", "Grep", "Write", "Edit"),
    },
    {
        "name": "story-researcher",
        "when_to_use": "需要外部事实、行业细节、现实素材与来源引用时",
        "base_type": "general",
        "model": "inherit",
        "upstream_tools": (
            "Read", "Glob", "Grep", "Write", "Bash", "WebSearch", "WebFetch",
        ),
    },
    {
        "name": "consistency-checker",
        "when_to_use": "需要事实一致性、伏笔断线、时间线与设定矛盾检查时",
        "base_type": "review",
        "model": "secondary",
        "upstream_tools": ("Read", "Glob", "Grep"),
    },
    {
        "name": "chapter-extractor",
        "when_to_use": "需要把单章正文拆成结构化摘要、情节点与角色提及时",
        "base_type": "explore",
        "model": "secondary",
        "upstream_tools": ("Read", "Glob", "Grep"),
    },
    {
        "name": "story-explorer",
        "when_to_use": "需要查询角色状态、伏笔进度、设定出处与写作进度时",
        "base_type": "explore",
        "model": "secondary",
        "upstream_tools": ("Read", "Glob", "Grep"),
    },
)

_UPSTREAM_REF_PREFIX = ".claude/skills/story-setup/references/agent-references/"
_PROJECT_REF_PREFIX = ".ycode/skills/story-ycode/references/agent-references/"
_BARE_REF_PREFIX = "story-setup/references/agent-references/"

_FALLBACK_NOTE = """## 参考文件路径回退（星瑶环境）

本项目资料已由 novel_init 部署到项目内，读取参考文件按两级回退，禁止跨级跳读：

1. 第一级（项目内，优先）：`{项目根}/.ycode/skills/story-ycode/references/agent-references/{文件名}`
2. 第二级（用户全局兜底）：`~/.agents/skills/story-setup/references/agent-references/{文件名}`

第一级缺失时再读第二级；两级都缺失时返回缺失事实（`missing_reference: {文件名}`），不要用 Glob/Grep 全盘搜索替代，也不要改读裸文件名。
"""


def story_setup_root() -> Path:
    """返回上游 story-setup 技能包根目录（可用环境变量覆盖）。"""
    override = os.environ.get(STORY_SETUP_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".agents" / "skills" / "story-setup"


def bundled_novel_root() -> Path:
    return Path(__file__).resolve().parent.parent / "plugins" / "novel_workspace"


def upstream_agents_dir() -> Path:
    """Agent 模板目录：优先插件自带，缺失时才回退上游技能包。"""
    bundled = bundled_novel_root() / "agents"
    if bundled.is_dir():
        return bundled
    return story_setup_root() / "references" / "templates" / "agents"


def upstream_references_dir() -> Path:
    """资料库目录：优先插件自带，缺失时才回退上游技能包。"""
    bundled = bundled_novel_root() / "references" / "agent-references"
    if bundled.is_dir():
        return bundled
    return story_setup_root() / "references" / "agent-references"


def init_novel_project(workdir, title, genre="", platform="", target_words=0) -> dict:
    root = Path(workdir).expanduser()
    root.mkdir(parents=True, exist_ok=True)

    created: list[str] = []
    skipped: list[str] = []

    for rel in NOVEL_DIRS:
        path = root / rel
        entry = rel + "/"
        if path.is_dir():
            skipped.append(entry)
        else:
            path.mkdir(parents=True, exist_ok=True)
            created.append(entry)

    marker = root / NOVEL_MARKER
    already_initialized = marker.is_file()
    if already_initialized:
        skipped.append(NOVEL_MARKER)
    else:
        _write_json(marker, {
            "schema": NOVEL_SCHEMA,
            "title": (title or "").strip() or root.name,
            "genre": (genre or "").strip(),
            "platform": (platform or "").strip(),
            "target_words": _normalize_target_words(target_words),
            "engine": ENGINE_NAME,
            "engine_version": ENGINE_VERSION,
            "created_at": _utc_now(),
        })
        created.append(NOVEL_MARKER)

    created.extend(_deploy_agents(root, skipped))
    created.extend(_deploy_references(root, skipped))

    return {
        "ok": True,
        "created": created,
        "skipped": skipped,
        "already_initialized": already_initialized,
    }


def convert_agent_md(source_path, spec) -> str:
    raw = Path(source_path).read_text(encoding="utf-8")
    header, body = _split_frontmatter(raw)
    description = _one_line(header.get("description", ""), 200) or spec["name"]
    body = rewrite_reference_paths(body)
    allowed_tools = ", ".join(_map_upstream_tools(spec["upstream_tools"]))

    lines = [
        "---",
        f"name: {spec['name']}",
        f"description: {description}",
        f"when_to_use: {spec['when_to_use']}",
        f"base_type: {spec['base_type']}",
        f"model: {spec['model']}",
        f"allowed_tools: {allowed_tools}",
        "---",
        "",
        body,
        "",
    ]
    return "\n".join(lines)


def rewrite_reference_paths(body: str) -> str:
    """改写上游正文里的资料路径，并在首个标题后注入两级回退说明。

    改写顺序不能颠倒：先把带 ``.claude/skills`` 的完整前缀换成项目内前缀，
    再处理省略项目根的裸前缀，否则第二步会把第一步的结果重复改写。
    """
    rewritten = body.replace(_UPSTREAM_REF_PREFIX, _PROJECT_REF_PREFIX)
    rewritten = rewritten.replace(_BARE_REF_PREFIX, "{项目根}/" + _PROJECT_REF_PREFIX)
    return _insert_fallback_note(rewritten)


def _normalize_target_words(value) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _write_text(path: Path, text: str, *, tool_name: str = "novel_init") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = text.encode("utf-8")

    from core.turn_undo import coordinator as _tu_coordinator
    from core.turn_undo import recorder as _tu_recorder
    from core.turn_undo.models import NodeKind, TurnUndoError

    with _tu_coordinator.get_coordinator().workspace_lock(
        _tu_coordinator.thread_workspace_root()
    ):
        try:
            handle = _tu_recorder.prepare_mutation(
                display_path=str(path),
                after_kind=NodeKind.REGULAR_FILE,
                after_content=payload,
                tool_name=tool_name,
                tool_call_id="",
            )
        except TurnUndoError as error:
            if error.code != "no_active_checkpoint":
                raise
            handle = None
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        if handle is not None:
            _tu_recorder.verify_after_mutation(handle)


def _write_json(path: Path, data: dict) -> None:
    """写 UTF-8 JSON（中文不转义，2 空格缩进，末尾换行）。"""
    _write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def _deploy_agents(root: Path, skipped: list[str]) -> list[str]:
    upstream = upstream_agents_dir()
    if not upstream.is_dir():
        skipped.append(f"{AGENTS_SUBDIR.as_posix()}/（上游模板目录缺失）")
        return []

    created: list[str] = []
    for spec in AGENT_SPECS:
        source = upstream / f"{spec['name']}.md"
        target = root / AGENTS_SUBDIR / f"{spec['name']}.md"
        entry = (AGENTS_SUBDIR / f"{spec['name']}.md").as_posix()
        if target.is_file():
            skipped.append(entry)
            continue
        if not source.is_file():
            skipped.append(f"{entry}（上游模板缺失）")
            continue
        _write_text(target, convert_agent_md(source, spec))
        created.append(entry)
    return created


def _deploy_references(root: Path, skipped: list[str]) -> list[str]:
    upstream = upstream_references_dir()
    if not upstream.is_dir():
        skipped.append(f"{REFERENCES_SUBDIR.as_posix()}/（上游资料目录缺失）")
        return []

    created: list[str] = []
    for source in sorted(upstream.rglob("*.md")):
        rel_inside = source.relative_to(upstream)
        target = root / REFERENCES_SUBDIR / rel_inside
        entry = (REFERENCES_SUBDIR / rel_inside).as_posix()
        if target.is_file():
            skipped.append(entry)
            continue
        _write_text(target, source.read_text(encoding="utf-8"))
        created.append(entry)
    return created


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)

_BLOCK_SCALARS = ("|", ">", "|-", ">-", "|+", ">+")


def _split_frontmatter(text: str):
    match = _FRONTMATTER_RE.match(text.lstrip("\ufeff"))
    if not match:
        return {}, text.strip()
    return _parse_upstream_header(match.group(1)), match.group(2).strip()


def _parse_upstream_header(raw: str) -> dict:
    data: dict[str, str] = {}
    key: str | None = None
    buffer: list[str] = []

    def flush() -> None:
        nonlocal key, buffer
        if key is not None and buffer:
            data[key] = "".join(part.strip() for part in buffer)
        key, buffer = None, []

    for line in raw.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[0] in " \t":
            if key is not None:
                buffer.append(line)
            continue
        flush()
        name, sep, value = line.partition(":")
        if not sep:
            continue
        name = name.strip().lower()
        value = value.strip()
        if value in _BLOCK_SCALARS:
            key = name
            buffer = []
            continue
        data[name] = value.strip("\"'")
    flush()
    return data


def _one_line(text: str, limit: int = 200) -> str:
    merged = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(merged) > limit:
        merged = merged[: limit - 1].rstrip() + "…"
    return merged


def _map_upstream_tools(tools) -> tuple[str, ...]:
    mapped: list[str] = []
    for name in tools:
        target = TOOL_NAME_MAP.get(str(name).strip().lower())
        if target and target not in mapped:
            mapped.append(target)
    return tuple(mapped)


def _insert_fallback_note(body: str) -> str:
    """在正文首个一级标题后插入路径回退说明；无标题时放在正文最前。"""
    lines = body.splitlines()
    for index, line in enumerate(lines):
        if line.startswith("# "):
            rest = lines[index + 1:]
            while rest and not rest[0].strip():
                rest.pop(0)
            return "\n".join(
                lines[: index + 1] + ["", _FALLBACK_NOTE.strip(), ""] + rest
            )
    return _FALLBACK_NOTE.strip() + "\n\n" + body


VENDOR_SUBDIR = Path("plugins") / "novel_workspace" / "vendor"
VENDOR_ENV = "YCODE_NOVEL_VENDOR_DIR"
# 需要一起 vendor 的脚本；tracking_commit.py 运行期按 __file__ 同目录加载
# wordcount_core.py，两者必须同时存在
VENDOR_SCRIPTS = ("outline_view.py", "tracking_commit.py", "wordcount_core.py")
VERSION_LOCK_NAME = "VERSION.lock"
SCRIPT_TIMEOUT_SECONDS = 30

TRACK_MODES = ("init", "append", "revision")
TRACK_SUBCOMMANDS = {"init": "init", "append": "commit", "revision": "commit"}
OUTLINE_STAGES = ("outline", "write")
OUTLINE_SUBDIR = Path("大纲")
OUTLINE_GLOB = "卷纲_*.md"
TRACKING_SUBDIR = Path("追踪")
TRACKING_STATE_NAME = "_tracking-state.json"
BODY_SUBDIR = Path("正文")
CHAPTER_FILE_RE = re.compile(r"^第(\d{3,})章_.*\.md$")
OUTLINE_DETAIL_NAME = "细纲_第{chapter:03d}章.md"
CHAPTER_SCAN_LIMIT = 500
FORESHADOW_STATUS_ACTIVE = "已埋"

AUDIT_SCOPES = ("chapter", "all")
AUDIT_CHECKS_DEFAULT = "auto"
AUDIT_LEVELS = ("S1", "S2", "S3", "S4")
FORESHADOW_OVERDUE_CHAPTERS = 20
CHAPTER_WORDCOUNT_FLOOR = 0.8
OUTLINE_WORD_TARGET_RE = re.compile(r"字数目标\s*[:：]\s*(\d+)")
OUTLINE_DETAIL_RE = re.compile(r"^细纲_第(\d{3,})章\.md$")
VOLUME_OUTLINE_RE = re.compile(r"^卷纲_第(\d+)卷\.md$")
OUTLINE_CHAPTER_RANGE_RE = re.compile(
    r"章节范围\s*[:：]\s*第\s*(\d+)\s*[-–—~至]\s*(\d+)\s*章"
)
AI_PATTERN_CHECKER = "check-ai-patterns.js"

AUDIT_FIX_TYPES = ("foreshadow_overdue",)
AUDIT_FIX_ACTIONS = ("defer", "reschedule", "resolve")
FORESHADOW_TABLE_NAME = "伏笔.md"
FORESHADOW_TABLE_PLANNED_COL = 4
FORESHADOW_PLANNED_PLACEHOLDER = "—"
FORESHADOW_ID_IN_MESSAGE_RE = re.compile(r"伏笔\s*([A-Za-z]{1,4}\d{1,6})")


def vendor_dir() -> Path:
    override = os.environ.get(VENDOR_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass) / VENDOR_SUBDIR
    return Path(__file__).resolve().parent.parent / VENDOR_SUBDIR


def vendor_script(name: str) -> Path | None:
    """返回脚本路径；文件缺失时返回 None（由调用方转成结构化错误）。"""
    path = vendor_dir() / name
    return path if path.is_file() else None


def novel_outline(workdir, unit_id=None, stage="outline", toc=False, path=None) -> dict:
    root = Path(workdir).expanduser()
    missing = _require_novel_project(root)
    if missing is not None:
        return missing

    stage_value = str(stage or "outline").strip().lower()
    if stage_value not in OUTLINE_STAGES:
        return {
            "ok": False,
            "error": f"stage 只接受 outline / write，收到：{stage}",
            "stderr": "",
        }

    outline_path, error = _resolve_outline_file(root, path)
    if error is not None:
        return {"ok": False, "error": error, "stderr": ""}

    script = vendor_script("outline_view.py")
    if script is None:
        return _missing_script_error("outline_view.py")

    args = [str(outline_path), "--stage", stage_value]
    if toc:
        args.append("--toc")
    if unit_id:
        args.extend(["--unit", str(unit_id).strip()])

    result = _run_vendor_script(script, args, root)
    if not result["ok"]:
        return _script_failure(result)
    payload = {"ok": True, "text": result["stdout"]}
    if result["stderr"].strip():
        payload["stderr"] = result["stderr"]
    return payload


def novel_track(workdir, mode, transaction) -> dict:
    root = Path(workdir).expanduser()
    missing = _require_novel_project(root)
    if missing is not None:
        return missing

    mode_value = str(mode or "").strip().lower()
    if mode_value not in TRACK_MODES:
        return {
            "ok": False,
            "error": f"mode 只接受 {' / '.join(TRACK_MODES)}，收到：{mode}",
            "stderr": "",
        }

    document, error = _coerce_transaction(transaction, mode_value)
    if error is not None:
        return {"ok": False, "error": error, "stderr": ""}

    script = vendor_script("tracking_commit.py")
    if script is None:
        return _missing_script_error("tracking_commit.py")

    input_path, error = _write_temp_json(document)
    if error is not None:
        return {"ok": False, "error": error, "stderr": ""}

    tracking_before = _snapshot_tracking_files(root)
    try:
        result = _run_vendor_script(
            script,
            [TRACK_SUBCOMMANDS[mode_value], "--project", str(root), "--input", str(input_path)],
            root,
        )
    finally:
        try:
            input_path.unlink()
        except OSError:
            # 临时文件清理失败不影响提交结果，交给系统临时目录回收
            pass

    if not result["ok"]:
        return _script_failure(result)

    if tracking_before is not None:
        _record_tracking_mutations(root, tracking_before)

    payload = _parse_script_json(result["stdout"])
    if payload is None:
        return {
            "ok": True,
            "stdout": result["stdout"],
            "stderr": result["stderr"],
            "error": "脚本输出不是 JSON，未做解析",
        }
    payload["ok"] = True
    if result["stderr"].strip():
        payload["stderr"] = result["stderr"]
    return payload


# ---------------------------------------------------------------------------
# 只读工具：novel_status / novel_wordcount
# ---------------------------------------------------------------------------

# wordcount_core 按文件路径动态加载（vendor 目录不可作为包导入，且不能改 sys.path）
_WORDCOUNT_MODULE_CACHE: dict[str, object] = {}


def _load_wordcount_module():
    """按文件路径加载 vendored ``wordcount_core.py``；缺失时返回 None。

    每个 vendor 路径独立缓存，环境变量切换 vendor 目录后仍能拿到对应副本，
    且不修改全局 ``sys.path``（避免污染主程序导入面）。
    """
    script = vendor_script("wordcount_core.py")
    if script is None:
        return None
    cache_key = str(script.resolve())
    cached = _WORDCOUNT_MODULE_CACHE.get(cache_key)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location("ycode_novel_wordcount_core", script)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _WORDCOUNT_MODULE_CACHE[cache_key] = module
    return module


def _visible_chars(module, text: str) -> int:
    body = module.visible_body(text)
    whitespace = getattr(module, "_WHITE_SPACE_CODEPOINTS", None)
    if whitespace is None:
        return sum(1 for character in body if not character.isspace())
    return sum(1 for character in body if ord(character) not in whitespace)


def _read_body_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _chapter_number_from_name(name: str) -> int | None:
    match = CHAPTER_FILE_RE.match(name)
    return int(match.group(1)) if match else None


def _list_chapter_files(root: Path) -> list[Path]:
    body_dir = root / BODY_SUBDIR
    if not body_dir.is_dir():
        return []
    chapters: list[tuple[int, str, Path]] = []
    try:
        entries = list(body_dir.iterdir())
    except OSError:
        return []
    for path in entries:
        if not path.is_file():
            continue
        number = _chapter_number_from_name(path.name)
        if number is None:
            continue
        chapters.append((number, path.name, path))
    chapters.sort(key=lambda item: (item[0], item[1]))
    return [item[2] for item in chapters[:CHAPTER_SCAN_LIMIT]]


def _resolve_chapter_file(root: Path, chapter) -> tuple[Path | None, str | None]:
    if isinstance(chapter, bool):
        return None, f"chapter 不是合法章号：{chapter!r}"
    if isinstance(chapter, int):
        number = chapter
        explicit_name = None
    else:
        raw = str(chapter or "").strip()
        if not raw:
            return None, "chapter 为空，需要章号或正文文件名"
        explicit_name = raw if raw.lower().endswith(".md") else None
        match = re.search(r"\d+", raw)
        if match is None:
            return None, f"无法从 chapter 识别章号：{raw}"
        number = int(match.group())

    body_dir = root / BODY_SUBDIR
    if explicit_name:
        candidate = body_dir / Path(explicit_name).name
        if candidate.is_file():
            return candidate, None
    if number <= 0:
        return None, f"chapter 必须是正整数，收到：{chapter}"
    matches: list[Path] = []
    if body_dir.is_dir():
        try:
            entries = list(body_dir.iterdir())
        except OSError:
            entries = []
        for path in entries:
            if path.is_file() and _chapter_number_from_name(path.name) == number:
                matches.append(path)
    if not matches:
        return None, f"正文/ 下找不到第 {number} 章（期望第{number:03d}章_*.md）"
    matches.sort(key=lambda item: item.name)
    return matches[0], None


def _load_tracking_state(root: Path) -> dict | None:
    path = root / TRACKING_SUBDIR / TRACKING_STATE_NAME
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _as_non_negative_int(value, default: int = 0) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def _count_active_foreshadows(state: dict | None) -> int:
    if not isinstance(state, dict):
        return 0
    rows = state.get("foreshadow")
    if isinstance(rows, dict):
        values = rows.values()
    elif isinstance(rows, list):
        values = rows
    else:
        return 0
    return sum(
        1
        for row in values
        if isinstance(row, dict) and row.get("status") == FORESHADOW_STATUS_ACTIVE
    )


def _tracking_actual_words(state: dict | None, number: int) -> int | None:
    if not isinstance(state, dict):
        return None
    records = state.get("wordcount_records")
    if not isinstance(records, dict):
        return None
    row = records.get(str(number))
    if not isinstance(row, dict):
        return None
    actual = row.get("actual")
    # bool 是 int 子类，必须单独排除，避免 true 被当成 1 字
    if isinstance(actual, bool) or not isinstance(actual, int):
        return None
    return actual if actual > 0 else None


def _volume_chapter_ranges(root: Path) -> list[tuple[int, int, int]]:
    """从 大纲/卷纲_第N卷.md 收集「章节范围：第X-Y章」→ (起, 止, 卷号)。

    卷纲没有该声明、文件不可读、范围倒挂时一律跳过；解析不到就留空，
    由调用方省略 volume 字段，绝不猜归属。
    """
    outline_dir = root / OUTLINE_SUBDIR
    if not outline_dir.is_dir():
        return []
    try:
        entries = list(outline_dir.iterdir())
    except OSError:
        return []
    ranges: list[tuple[int, int, int]] = []
    for path in entries:
        matched = VOLUME_OUTLINE_RE.match(path.name)
        if matched is None or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        volume = int(matched.group(1))
        for low, high in OUTLINE_CHAPTER_RANGE_RE.findall(text):
            start, end = int(low), int(high)
            if start <= 0 or end < start:
                continue
            ranges.append((start, end, volume))
    return ranges


def _chapter_volume(ranges: list[tuple[int, int, int]], number: int) -> int | None:
    """按章号范围判定唯一卷号；无声明或多卷重叠（归属不可靠）时返回 None。"""
    volumes = {volume for start, end, volume in ranges if start <= number <= end}
    return volumes.pop() if len(volumes) == 1 else None


def novel_status(workdir) -> dict:
    root = Path(workdir).expanduser()
    if _require_novel_project(root) is not None:
        return {"is_novel": False}

    title = ""
    target_words = 0
    marker_path = root / NOVEL_MARKER
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        if isinstance(marker, dict):
            title = str(marker.get("title") or "")
            target_words = _normalize_target_words(marker.get("target_words"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        title = ""
        target_words = 0

    state = _load_tracking_state(root)
    last_commit = _as_non_negative_int(
        state.get("last_committed_chapter") if isinstance(state, dict) else None
    )
    next_chapter = last_commit + 1
    outline_ready = (
        root / OUTLINE_SUBDIR / OUTLINE_DETAIL_NAME.format(chapter=next_chapter)
    ).is_file()

    chapter_files = _list_chapter_files(root)
    module = _load_wordcount_module()
    volume_ranges = _volume_chapter_ranges(root)
    chapters: list[dict] = []
    total_words = 0
    today_words = 0
    today = date.today()
    for path in chapter_files:
        number = _chapter_number_from_name(path.name)
        if number is None:
            continue
        visible_chars = 0
        text = _read_body_text(path)
        if text is not None and module is not None:
            visible_chars = _visible_chars(module, text)
            total_words += visible_chars
            try:
                modified = datetime.fromtimestamp(path.stat().st_mtime).date()
            except OSError:
                modified = None
            if modified == today:
                today_words += visible_chars
        actual_words = _tracking_actual_words(state, number)
        entry = {
            "number": number,
            "name": path.stem or f"第{number:03d}章",
            "words": actual_words if actual_words is not None else visible_chars,
            "submitted": number <= last_commit,
            "outline_ready": (
                root / OUTLINE_SUBDIR / OUTLINE_DETAIL_NAME.format(chapter=number)
            ).is_file(),
        }
        volume = _chapter_volume(volume_ranges, number)
        if volume is not None:
            entry["volume"] = volume
        chapters.append(entry)

    return {
        "is_novel": True,
        "title": title,
        "target_words": target_words,
        "chapters_written": len(chapter_files),
        "chapters": chapters,
        "total_words": total_words,
        "today_words": today_words,
        "next_chapter": next_chapter,
        "outline_ready": outline_ready,
        "active_foreshadows": _count_active_foreshadows(state),
        "last_commit": last_commit,
    }


def novel_wordcount(text=None, workdir=None, chapter=None) -> dict:
    if text is not None:
        if not isinstance(text, str):
            return {"ok": False, "error": f"text 必须是字符串，收到：{type(text).__name__}"}
        module = _load_wordcount_module()
        if module is None:
            return _missing_script_error("wordcount_core.py")
        return {"ok": True, "visible_chars": _visible_chars(module, text), "chapter": chapter}

    if workdir is None or chapter is None:
        return {
            "ok": False,
            "error": "参数错误：传 text 直接统计，或同时传 workdir 与 chapter 读正文文件统计",
        }

    root = Path(workdir).expanduser()
    missing = _require_novel_project(root)
    if missing is not None:
        return missing
    module = _load_wordcount_module()
    if module is None:
        return _missing_script_error("wordcount_core.py")
    path, error = _resolve_chapter_file(root, chapter)
    if error is not None:
        return {"ok": False, "error": error}
    text_value = _read_body_text(path)
    if text_value is None:
        return {"ok": False, "error": f"无法读取正文文件：{path}"}
    return {
        "ok": True,
        "visible_chars": _visible_chars(module, text_value),
        "chapter": chapter,
    }


def _body_path_entry(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _chapter_number_for_result(path: Path, chapter) -> int:
    number = _chapter_number_from_name(path.name)
    if number is not None:
        return number
    match = re.search(r"\d+", str(chapter or ""))
    return int(match.group()) if match else 0


def read_chapter(workdir, chapter) -> dict:
    root = Path(workdir).expanduser()
    missing = _require_novel_project(root)
    if missing is not None:
        return missing
    path, error = _resolve_chapter_file(root, chapter)
    if error is not None:
        return {"ok": False, "error": error}
    text_value = _read_body_text(path)
    if text_value is None:
        return {"ok": False, "error": f"无法读取正文文件：{path}"}
    module = _load_wordcount_module()
    if module is None:
        return _missing_script_error("wordcount_core.py")
    return {
        "ok": True,
        "chapter": _chapter_number_for_result(path, chapter),
        "path": _body_path_entry(root, path),
        "content": text_value,
        "visible_chars": _visible_chars(module, text_value),
    }


def save_chapter(workdir, chapter, content) -> dict:
    root = Path(workdir).expanduser()
    missing = _require_novel_project(root)
    if missing is not None:
        return missing
    if not isinstance(content, str):
        return {"ok": False, "error": f"content 必须是字符串，收到：{type(content).__name__}"}
    path, error = _resolve_chapter_file(root, chapter)
    if error is not None:
        return {"ok": False, "error": error}
    module = _load_wordcount_module()
    if module is None:
        return _missing_script_error("wordcount_core.py")
    _write_text(path, content, tool_name="novel_edit_chapter")
    return {
        "ok": True,
        "chapter": _chapter_number_for_result(path, chapter),
        "path": _body_path_entry(root, path),
        "visible_chars": _visible_chars(module, content),
    }

# ---------------------------------------------------------------------------
# GUI 工作台聚合：novel_workbench / read_novel_doc（P3-C，全部只读）
# ---------------------------------------------------------------------------

DOC_PREVIEW_SUBDIRS = (OUTLINE_SUBDIR.as_posix(), "设定", TRACKING_SUBDIR.as_posix())
DOC_PREVIEW_MAX_BYTES = 2 * 1024 * 1024
DAILY_WINDOW_DAYS = 7


def _as_text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def _as_str_list(value) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _tracking_rows(state: dict | None, key: str) -> list[tuple[str, dict]]:
    if not isinstance(state, dict):
        return []
    rows = state.get(key)
    if isinstance(rows, dict):
        pairs = list(rows.items())
    elif isinstance(rows, list):
        pairs = []
        for index, row in enumerate(rows):
            row_id = row.get("id") or row.get("name") if isinstance(row, dict) else None
            pairs.append((str(row_id or index), row))
    else:
        return []
    return [(str(name), row) for name, row in pairs if isinstance(row, dict)]


def _workbench_foreshadows(state: dict | None) -> list[dict]:
    last_commit = _as_non_negative_int(
        state.get("last_committed_chapter") if isinstance(state, dict) else None
    )
    next_chapter = last_commit + 1
    entries: list[dict] = []
    for name, row in _tracking_rows(state, "foreshadow"):
        identifier = _as_text(row.get("id")) or name
        status = _as_text(row.get("status"))
        planted = _as_non_negative_int(row.get("planted_chapter"))
        planned = _as_non_negative_int(row.get("planned_resolution_chapter"))
        active = status == FORESHADOW_STATUS_ACTIVE
        entries.append({
            "id": identifier,
            "summary": _as_text(row.get("summary")),
            "status": status,
            "importance": _as_text(row.get("importance")),
            "planted_chapter": planted,
            "planned_resolution_chapter": planned,
            "updated_chapter": _as_non_negative_int(row.get("updated_chapter")),
            "overdue": (
                active
                and planted > 0
                and (last_commit - planted) > FORESHADOW_OVERDUE_CHAPTERS
            ),
            "planned_overdue": active and planned > 0 and planned < next_chapter,
            "stale_chapters": max(0, last_commit - planted),
        })
    entries.sort(
        key=lambda item: (
            not item["overdue"],
            not item["planned_overdue"],
            -item["stale_chapters"],
            item["planted_chapter"],
            item["id"],
        )
    )
    return entries


def _workbench_characters(state: dict | None) -> list[dict]:
    entries: list[dict] = []
    for name, row in _tracking_rows(state, "characters"):
        entries.append({
            "name": name,
            "identity": _as_text(row.get("identity")),
            "state": _as_text(row.get("state")),
            "location": _as_text(row.get("location")),
            "goal": _as_text(row.get("goal")),
            "knowledge": _as_str_list(row.get("knowledge")),
            "abilities_resources": _as_str_list(row.get("abilities_resources")),
            "open_threads": _as_str_list(row.get("open_threads")),
            "relationships": _as_str_list(row.get("relationships")),
        })
    entries.sort(key=lambda item: (not item["open_threads"], item["name"]))
    return entries


def _workbench_volumes(root: Path, next_chapter: int) -> list[dict]:
    outline_dir = root / OUTLINE_SUBDIR
    if not outline_dir.is_dir():
        return []
    try:
        entries = list(outline_dir.iterdir())
    except OSError:
        return []
    module = _load_wordcount_module()
    current = _chapter_volume(_volume_chapter_ranges(root), next_chapter)
    volumes: list[dict] = []
    for path in entries:
        matched = VOLUME_OUTLINE_RE.match(path.name)
        if matched is None or not path.is_file():
            continue
        number = int(matched.group(1))
        chars = 0
        text = _read_body_text(path)
        if text is not None and module is not None:
            chars = _visible_chars(module, text)
        volumes.append({
            "no": number,
            "name": path.stem,
            "chars": chars,
            "current": current is not None and current == number,
        })
    volumes.sort(key=lambda item: item["no"])
    return volumes


def _workbench_recent_daily(root: Path, days: int = DAILY_WINDOW_DAYS) -> list[dict]:
    span = max(1, int(days))
    today = date.today()
    window = [today - timedelta(days=offset) for offset in range(span - 1, -1, -1)]
    totals = {value: 0 for value in window}
    module = _load_wordcount_module()
    if module is not None:
        for path in _list_chapter_files(root):
            text = _read_body_text(path)
            if text is None:
                continue
            try:
                modified = datetime.fromtimestamp(path.stat().st_mtime).date()
            except OSError:
                continue
            if modified not in totals:
                continue
            totals[modified] += _visible_chars(module, text)
    return [{"date": value.isoformat(), "words": totals[value]} for value in window]


def novel_workbench(workdir) -> dict:
    root = Path(workdir).expanduser()
    if _require_novel_project(root) is not None:
        return {"is_novel": False}

    state = _load_tracking_state(root)
    last_commit = _as_non_negative_int(
        state.get("last_committed_chapter") if isinstance(state, dict) else None
    )
    return {
        "is_novel": True,
        "foreshadows": _workbench_foreshadows(state),
        "characters": _workbench_characters(state),
        "volumes": _workbench_volumes(root, last_commit + 1),
        "recent_daily": _workbench_recent_daily(root),
    }


def read_novel_doc(workdir, rel_path) -> dict:
    root = Path(workdir).expanduser()
    missing = _require_novel_project(root)
    if missing is not None:
        return missing

    raw = str(rel_path or "").strip()
    if not raw:
        return {"ok": False, "error": "rel_path 不能为空，需要 大纲/ 设定/ 追踪/ 下的 .md 相对路径"}
    candidate = Path(raw)
    if candidate.is_absolute() or candidate.drive:
        return {"ok": False, "error": f"只接受项目内相对路径，拒绝绝对路径：{raw}"}

    try:
        root_resolved = root.resolve()
        target = (root_resolved / candidate).resolve()
    except OSError as error:
        return {"ok": False, "error": f"无法解析路径 {raw}：{error}"}

    if not target.is_relative_to(root_resolved):
        return {
            "ok": False,
            "error": f"路径越界：{raw} 不在项目目录内，只允许 大纲/ 设定/ 追踪/ 下的文档",
        }
    relative = target.relative_to(root_resolved)
    parts = relative.parts
    if not parts or parts[0] not in DOC_PREVIEW_SUBDIRS:
        return {
            "ok": False,
            "error": (
                f"目录不在白名单：{' / '.join(DOC_PREVIEW_SUBDIRS)} 之外不开放预览，"
                f"收到：{relative.as_posix()}"
            ),
        }
    if relative.suffix.lower() != ".md":
        return {
            "ok": False,
            "error": f"只支持预览 .md 文档，收到：{relative.name}",
        }
    if not target.is_file():
        return {"ok": False, "error": f"文档不存在：{relative.as_posix()}"}

    try:
        size = target.stat().st_size
    except OSError as error:
        return {"ok": False, "error": f"无法读取文档 {relative.as_posix()}：{error}"}
    if size > DOC_PREVIEW_MAX_BYTES:
        return {"ok": False, "error": "文件过大，不在面板内预览"}

    text = _read_body_text(target)
    if text is None:
        return {"ok": False, "error": f"无法读取文档：{relative.as_posix()}"}
    module = _load_wordcount_module()
    if module is not None:
        chars = _visible_chars(module, text)
    else:
        # 统计脚本缺失时按非空白字符数兜底：预览本身不该因此不可用
        chars = sum(1 for character in text if not character.isspace())
    return {
        "ok": True,
        "text": text,
        "chars": chars,
        "path": relative.as_posix(),
    }


DISCOVER_SKIP_DIRS = {
    ".git", ".ycode", ".idea", ".vscode", "node_modules",
    "__pycache__", "site-packages", "导出",
}
DISCOVER_MAX_DEPTH = 2
DISCOVER_MAX_BOOKS = 50
EXPORT_SUBDIR = "导出"
EXPORT_FORMATS = ("md", "txt")


def _novel_marker_title(root: Path) -> str:
    try:
        data = json.loads((root / NOVEL_MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return root.name
    title = str(data.get("title") or "").strip() if isinstance(data, dict) else ""
    return title or root.name


def _count_chapter_files(root: Path) -> int:
    body_dir = root / BODY_SUBDIR
    if not body_dir.is_dir():
        return 0
    return sum(
        1
        for path in body_dir.glob("*.md")
        if _chapter_number_from_name(path.name) is not None
    )


def discover_novel_projects(
    workdir, max_depth=DISCOVER_MAX_DEPTH, limit=DISCOVER_MAX_BOOKS
) -> dict:
    root = Path(workdir).expanduser()
    if not str(workdir or "").strip():
        return {"ok": False, "error": "workdir 不能为空"}
    if not root.is_dir():
        return {"ok": False, "error": f"工作目录不存在：{root}"}

    depth_limit = max(0, int(max_depth))
    limit_value = max(1, int(limit))
    projects: list[dict] = []

    def scan(directory: Path, depth: int) -> None:
        if len(projects) >= limit_value:
            return
        if (directory / NOVEL_MARKER).is_file():
            projects.append({
                "path": "." if directory == root else directory.relative_to(root).as_posix(),
                "title": _novel_marker_title(directory),
                "chapters_written": _count_chapter_files(directory),
            })
            return
        if depth >= depth_limit:
            return
        try:
            children = sorted(directory.iterdir(), key=lambda item: item.name)
        except OSError:
            return
        for child in children:
            if not child.is_dir():
                continue
            if child.name.startswith(".") or child.name in DISCOVER_SKIP_DIRS:
                continue
            scan(child, depth + 1)

    scan(root, 0)
    return {"ok": True, "root": str(root), "projects": projects}


def _strip_markdown(text: str) -> str:
    body = re.sub(r"\A---\s*\n.*?\n---\s*\n", "", text, flags=re.DOTALL)
    return "\n".join(re.sub(r"^#{1,6}\s*", "", line) for line in body.splitlines()).strip()


def export_novel(workdir, fmt="md") -> dict:
    root = Path(workdir).expanduser()
    missing = _require_novel_project(root)
    if missing is not None:
        return missing
    format_value = str(fmt or "md").strip().lower()
    if format_value not in EXPORT_FORMATS:
        return {
            "ok": False,
            "error": f"fmt 只接受 {' / '.join(EXPORT_FORMATS)}，收到：{fmt}",
        }
    module = _load_wordcount_module()
    if module is None:
        return _missing_script_error("wordcount_core.py")

    chapters: list[str] = []
    visible_total = 0
    for path in _list_chapter_files(root):
        text_value = _read_body_text(path)
        if text_value is None:
            continue
        visible_total += _visible_chars(module, text_value)
        chapters.append(
            _strip_markdown(text_value) if format_value == "txt" else text_value.strip()
        )
    if not chapters:
        return {"ok": False, "error": f"正文目录没有可导出的章节：{root / BODY_SUBDIR}"}

    separator = "\n\n\n" if format_value == "txt" else "\n\n---\n\n"
    body = separator.join(chapters)
    title = _novel_marker_title(root)
    if format_value == "md":
        body = f"# {title}\n\n{body}"
    target_dir = root / EXPORT_SUBDIR
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{title}.{format_value}"
    _write_text(target, body.rstrip() + "\n", tool_name="novel_export")
    return {
        "ok": True,
        "path": target.relative_to(root).as_posix(),
        "chapters": len(chapters),
        "visible_chars": visible_total,
    }


def _recording_active() -> bool:
    try:
        from core.turn_undo import recorder as _tu_recorder
        return _tu_recorder.is_context_active(_tu_recorder.current_context())
    except Exception:
        # 记录器不可用（测试替身/嵌入方）时按"无检查点"处理，不阻塞提交
        return False


def _snapshot_tracking_files(root: Path) -> dict[str, bytes] | None:
    if not _recording_active():
        return None
    base = root / TRACKING_SUBDIR
    if not base.is_dir():
        return {}
    snapshot: dict[str, bytes] = {}
    for path in sorted(base.rglob("*")):
        if not path.is_file():
            continue
        try:
            snapshot[path.relative_to(root).as_posix()] = path.read_bytes()
        except OSError:
            # 读不到的文件不参与重放，最多退回"未记录"，不影响提交结果
            continue
    return snapshot


def _record_tracking_mutations(root: Path, before: dict[str, bytes]) -> None:
    after = _snapshot_tracking_files(root) or {}
    for rel, after_bytes in after.items():
        if before.get(rel) == after_bytes:
            continue
        try:
            text = after_bytes.decode("utf-8")
        except UnicodeDecodeError:
            continue
        path = root / rel
        prior = before.get(rel)
        try:
            if prior is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(prior)
        except OSError:
            continue
        _write_text(path, text, tool_name="novel_track")


def _require_novel_project(root: Path) -> dict | None:
    if (root / NOVEL_MARKER).is_file():
        return None
    return {
        "ok": False,
        "is_novel": False,
        "error": f"不是小说项目：{root} 下缺少 {NOVEL_MARKER}（先运行 novel_init）",
    }


def _resolve_outline_file(root: Path, path) -> tuple[Path | None, str | None]:
    if path:
        candidate = Path(str(path)).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        if not candidate.is_file():
            return None, f"卷纲文件不存在：{candidate}"
        return candidate, None

    candidates = sorted(p for p in (root / OUTLINE_SUBDIR).glob(OUTLINE_GLOB) if p.is_file())
    if not candidates:
        return None, f"缺少卷纲：{OUTLINE_SUBDIR.as_posix()}/{OUTLINE_GLOB} 没有匹配文件"
    if len(candidates) > 1:
        names = "、".join(p.name for p in candidates)
        return None, f"卷纲文件不唯一（{names}），请用 path 指定要取的那一卷"
    return candidates[0], None


def _coerce_transaction(transaction, mode_value: str) -> tuple[dict | None, str | None]:
    if isinstance(transaction, str):
        try:
            transaction = json.loads(transaction)
        except json.JSONDecodeError as error:
            return None, f"transaction 不是合法 JSON：{error}"
    if not isinstance(transaction, dict):
        return None, f"transaction 必须是 JSON 对象，收到：{type(transaction).__name__}"

    document = dict(transaction)
    if mode_value == "init":
        return document, None

    document_mode = document.get("mode")
    if document_mode is None:
        document["mode"] = mode_value
        return document, None
    if str(document_mode).strip().lower() != mode_value:
        return None, (
            f"mode 与 transaction.mode 不一致：工具入参 {mode_value}，"
            f"事务文档 {document_mode}"
        )
    document["mode"] = mode_value
    return document, None


def _write_temp_json(document: dict) -> tuple[Path | None, str | None]:
    """把事务文档写成临时 UTF-8 JSON 文件（无 BOM），返回 (路径, 错误说明)。"""
    try:
        handle, name = tempfile.mkstemp(prefix="novel-track-", suffix=".json")
    except OSError as error:
        return None, f"无法创建临时事务文件：{error}"
    path = Path(name)
    try:
        payload = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(payload)
    except OSError as error:
        try:
            path.unlink()
        except OSError:
            pass
        return None, f"无法写入临时事务文件：{error}"
    return path, None


def _run_vendor_script(script: Path, args: list[str], cwd: Path) -> dict:
    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    try:
        completed = subprocess.run(
            [sys.executable, str(script), *args],
            cwd=str(cwd),
            capture_output=True,
            timeout=SCRIPT_TIMEOUT_SECONDS,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "stdout": "",
            "stderr": "",
            "returncode": None,
            "error": f"{script.name} 执行超时（{SCRIPT_TIMEOUT_SECONDS} 秒）",
        }
    except OSError as error:
        return {
            "ok": False,
            "stdout": "",
            "stderr": "",
            "returncode": None,
            "error": f"无法启动 {script.name}：{error}",
        }
    return {
        "ok": completed.returncode == 0,
        "stdout": completed.stdout.decode("utf-8", errors="replace"),
        "stderr": completed.stderr.decode("utf-8", errors="replace"),
        "returncode": completed.returncode,
        "error": None,
    }


def _script_failure(result: dict) -> dict:
    stderr = result.get("stderr") or ""
    message = result.get("error") or _first_line(stderr)
    if not message:
        message = f"脚本以退出码 {result.get('returncode')} 结束"
    payload = {"ok": False, "error": message, "stderr": stderr}
    if result.get("returncode") is not None:
        payload["returncode"] = result["returncode"]
    return payload


def _missing_script_error(name: str) -> dict:
    """vendored 脚本缺失：返回结构化错误，不回退到用户全局技能包。"""
    return {
        "ok": False,
        "error": (
            f"vendored 脚本缺失：{name}（期望位于 {vendor_dir()}）。"
            "请检查插件完整性后重装 novel_workspace；本工具不静默降级。"
        ),
        "stderr": "",
    }


def _first_line(text: str) -> str:
    for line in str(text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def _parse_script_json(text: str) -> dict | None:
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


# ---------------------------------------------------------------------------
# 确定性审计：novel_audit（只读体检，按 S1~S4 分级输出 findings）
# ---------------------------------------------------------------------------


def novel_audit(workdir, scope="chapter", chapter=None, checks="auto") -> dict:
    root = Path(workdir).expanduser()
    missing = _require_novel_project(root)
    if missing is not None:
        return missing

    checks_value = str(checks or AUDIT_CHECKS_DEFAULT).strip().lower()
    if checks_value != AUDIT_CHECKS_DEFAULT:
        return {
            "ok": False,
            "error": f"checks 只接受 {AUDIT_CHECKS_DEFAULT}（P1 不支持单项筛选），收到：{checks}",
        }
    scope_value = str(scope or "chapter").strip().lower()
    if scope_value not in AUDIT_SCOPES:
        return {
            "ok": False,
            "error": f"scope 只接受 {' / '.join(AUDIT_SCOPES)}，收到：{scope}",
        }

    chapter_files = _list_chapter_files(root)
    body_by_number: dict[int, Path] = {}
    for path in chapter_files:
        number = _chapter_number_from_name(path.name)
        if number is None or number in body_by_number:
            continue
        body_by_number[number] = path

    context, error = _audit_context(root, body_by_number, scope_value, chapter)
    if error is not None:
        return {"ok": False, "error": error}

    findings: list[dict] = []
    skipped: list[dict] = []
    for name, runner in (
        ("tracking_state", _audit_tracking_state),
        ("foreshadow_overdue", _audit_foreshadow_overdue),
        ("chapter_wordcount", _audit_chapter_wordcount),
        ("outline_gap", _audit_outline_gap),
        ("ai_patterns", _audit_ai_patterns),
    ):
        try:
            produced, reason = runner(context)
        except Exception as exc:
            skipped.append({"check": name, "reason": f"checker_error: {exc}"})
            continue
        if reason:
            skipped.append({"check": name, "reason": reason})
        findings.extend(produced)

    summary = {level: 0 for level in AUDIT_LEVELS}
    for finding in findings:
        level = finding.get("level")
        if level in summary:
            summary[level] += 1

    return {
        "ok": True,
        "scope": scope_value,
        "findings": findings,
        "summary": summary,
        "skipped": skipped,
    }


def _audit_context(root: Path, body_by_number: dict[int, Path], scope_value: str, chapter):
    state = _load_tracking_state(root)
    last_commit = _as_non_negative_int(
        state.get("last_committed_chapter") if isinstance(state, dict) else None
    )
    body_max = max(body_by_number) if body_by_number else 0

    if scope_value == "all":
        numbers = _audit_all_numbers(root, body_by_number, body_max)
    elif chapter is None or not str(chapter).strip():
        numbers = [body_max, body_max + 1] if body_max else []
    else:
        path, error = _resolve_chapter_file(root, chapter)
        if error is not None:
            return None, error
        number = _chapter_number_from_name(path.name)
        if number is None:
            return None, f"无法从正文文件名识别章号：{path.name}"
        numbers = [number]
        if number + 1 not in body_by_number:
            numbers.append(number + 1)

    return {
        "root": root,
        "state": state,
        "last_commit": last_commit,
        "body_by_number": body_by_number,
        "numbers": numbers,
        "scope": scope_value,
    }, None


def _audit_all_numbers(root: Path, body_by_number: dict[int, Path], body_max: int) -> list[int]:
    """scope=all 的章号集合：正文章号 + 不超过"正文最大章 + 1"的细纲章号。

    远端还没写到的大批细纲不算缺口（否则一部长篇会刷出上百条噪声）；
    集合按章号升序截断到 CHAPTER_SCAN_LIMIT，与 _list_chapter_files 同一口径。
    """
    numbers = set(body_by_number)
    horizon = body_max + 1 if body_max else 1
    for number in _outline_detail_numbers(root):
        if number <= horizon:
            numbers.add(number)
    return sorted(numbers)[:CHAPTER_SCAN_LIMIT]


def _outline_detail_numbers(root: Path) -> list[int]:
    base = root / OUTLINE_SUBDIR
    if not base.is_dir():
        return []
    try:
        entries = list(base.iterdir())
    except OSError:
        return []
    numbers = []
    for path in entries:
        if not path.is_file():
            continue
        match = OUTLINE_DETAIL_RE.match(path.name)
        if match is not None:
            numbers.append(int(match.group(1)))
    return sorted(numbers)


def _outline_detail_path(root: Path, number: int) -> Path:
    return root / OUTLINE_SUBDIR / OUTLINE_DETAIL_NAME.format(chapter=number)


def _outline_word_target(path: Path) -> int | None:
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = OUTLINE_WORD_TARGET_RE.search(text)
    if match is None:
        return None
    return _as_non_negative_int(match.group(1), default=0) or None


def _audit_finding(level: str, type_name: str, file_path: str, message: str) -> dict:
    return {"level": level, "type": type_name, "file": file_path, "message": message}


def _audit_relpath(root: Path, path) -> str:
    try:
        return Path(path).relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _audit_tracking_state(context: dict) -> tuple[list[dict], str | None]:
    root = context["root"]
    state_path = root / TRACKING_SUBDIR / TRACKING_STATE_NAME
    rel = _audit_relpath(root, state_path)
    if context["state"] is None:
        if state_path.is_file():
            message = f"{TRACKING_STATE_NAME} 无法解析为 JSON 对象，追踪基线不可用，需重新初始化"
        else:
            message = f"缺少 {TRACKING_STATE_NAME}，追踪基线未建立（先运行 novel_track init）"
        return [_audit_finding("S3", "tracking_state", rel, message)], None

    last_commit = context["last_commit"]
    body_by_number = context["body_by_number"]
    body_max = max(body_by_number) if body_by_number else 0
    if last_commit == body_max:
        return [], None
    message = (
        f"追踪最近提交章为第{last_commit}章，正文最大章号为第{body_max}章，"
        "两者不一致（写完正文未提交追踪，或追踪提交后正文被改动）"
    )
    return [_audit_finding("S1", "tracking_state", rel, message)], None


def _audit_foreshadow_overdue(context: dict) -> tuple[list[dict], str | None]:
    state = context["state"]
    if not isinstance(state, dict):
        return [], "tracking_state_missing"

    rows = state.get("foreshadow")
    if isinstance(rows, dict):
        values = list(rows.values())
    elif isinstance(rows, list):
        values = list(rows)
    else:
        values = []

    last_commit = context["last_commit"]
    rel = _audit_relpath(
        context["root"], context["root"] / TRACKING_SUBDIR / TRACKING_STATE_NAME
    )
    findings = []
    for row in values:
        if not isinstance(row, dict):
            continue
        if row.get("status") != FORESHADOW_STATUS_ACTIVE:
            continue
        planted = _as_non_negative_int(row.get("planted_chapter"), default=0)
        if planted <= 0:
            continue
        planned = _as_non_negative_int(
            row.get("planned_resolution_chapter"), default=0
        )
        if planned <= 0:
            continue
        if last_commit <= planned + FORESHADOW_OVERDUE_CHAPTERS:
            continue
        identifier = str(row.get("id") or "?").strip() or "?"
        message = (
            f"伏笔 {identifier} 计划第{planned}章回收，已逾期 {last_commit - planned} 章"
            f"（超过宽限 {FORESHADOW_OVERDUE_CHAPTERS} 章）"
        )
        summary = str(row.get("summary") or "").strip()
        if summary:
            message += f"：{summary}"
        findings.append(_audit_finding("S2", "foreshadow_overdue", rel, message))
    return findings, None


def _audit_chapter_wordcount(context: dict) -> tuple[list[dict], str | None]:
    module = _load_wordcount_module()
    if module is None:
        return [], "wordcount_core_missing"

    root = context["root"]
    findings = []
    for number in context["numbers"]:
        body_path = context["body_by_number"].get(number)
        if body_path is None:
            continue
        target = _outline_word_target(_outline_detail_path(root, number))
        if target is None:
            continue
        text = _read_body_text(body_path)
        if text is None:
            continue
        visible = _visible_chars(module, text)
        floor = int(target * CHAPTER_WORDCOUNT_FLOOR)
        if visible >= floor:
            continue
        findings.append(_audit_finding(
            "S3",
            "chapter_wordcount",
            _audit_relpath(root, body_path),
            f"第{number}章可见字数 {visible} 低于细纲目标 {target} 的 "
            f"{int(CHAPTER_WORDCOUNT_FLOOR * 100)}%（下限 {floor}）",
        ))
    return findings, None


def _audit_outline_gap(context: dict) -> tuple[list[dict], str | None]:
    root = context["root"]
    body_by_number = context["body_by_number"]
    findings = []
    for number in context["numbers"]:
        if number in body_by_number:
            continue
        outline_path = _outline_detail_path(root, number)
        if not outline_path.is_file():
            continue
        findings.append(_audit_finding(
            "S3",
            "outline_gap",
            _audit_relpath(root, outline_path),
            f"第{number}章已有细纲但正文缺失，细纲先行的这一章还没写成正文",
        ))
    return findings, None


AI_BANNED_WORDS_FILE = "banned-words.md"
AI_BANNED_LEVEL_HEADING = "## 一级禁用词"
# 资料库缺失时的最小兜底词表：取一级禁用词里最具 AI 特征的高频词，
# 保证没装插件资料的本机也能给出确定性结论，而不是整体 skipped
AI_BANNED_WORDS_FALLBACK = (
    "仿佛", "犹如", "宛若", "一丝", "一抹", "些许", "隐约",
    "眼中闪过", "嘴角勾起", "心中一动", "心头一震",
    "不由得", "不容置疑", "显而易见", "毫无疑问",
    "不由自主", "情不自禁", "深吸一口气", "不禁",
)
# 每章最多报几条禁用词：按命中次数降序，避免一个高频词淹没其它检查
AI_BANNED_FINDING_LIMIT = 6
AI_FORMULAIC_RE = re.compile(r"不是[^，。！？\n]{1,20}[，,]\s*(?:而)?是")
AI_FORMULAIC_LIMIT = 2
AI_ENDING_RE = re.compile(r"他不知道的是|这一刻[，,]?(?:他|她)?终于明白|才刚刚开始")


def _markdown_section(text: str, heading: str) -> str:
    start = text.find(heading)
    if start < 0:
        return ""
    body = text[start + len(heading):]
    match = re.search(r"^## ", body, re.MULTILINE)
    return body[: match.start()] if match else body


def _ai_banned_words() -> tuple[str, ...]:
    path = bundled_novel_root() / "references" / "agent-references" / AI_BANNED_WORDS_FILE
    words: list[str] = []
    if path.is_file():
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            text = ""
        for line in _markdown_section(text, AI_BANNED_LEVEL_HEADING).splitlines():
            stripped = line.strip()
            if not stripped or stripped[0] in "#>|-":
                continue
            for token in stripped.split("、"):
                token = token.strip().strip("。；;")
                if token:
                    words.append(token)
    deduped = tuple(dict.fromkeys(words))
    return deduped or AI_BANNED_WORDS_FALLBACK


def _scan_ai_patterns(
    root: Path, body_path: Path, number: int, text: str, banned: tuple[str, ...]
) -> list[dict]:
    rel = _audit_relpath(root, body_path)
    findings: list[dict] = []

    hits = sorted(
        ((text.count(word), word) for word in banned if word in text),
        key=lambda item: (-item[0], item[1]),
    )
    for count, word in hits[:AI_BANNED_FINDING_LIMIT]:
        findings.append(_audit_finding(
            "S4",
            "ai_patterns",
            rel,
            f"第{number}章命中一级禁用词「{word}」{count} 处，按 banned-words.md 应替换",
        ))

    formulaic = len(AI_FORMULAIC_RE.findall(text))
    if formulaic >= AI_FORMULAIC_LIMIT:
        findings.append(_audit_finding(
            "S4",
            "ai_patterns",
            rel,
            f"第{number}章出现「不是A，而是B」句式 {formulaic} 处"
            f"（≥{AI_FORMULAIC_LIMIT} 处属 AI 味强证据）",
        ))

    ending = AI_ENDING_RE.search(text)
    if ending is not None:
        findings.append(_audit_finding(
            "S4",
            "ai_patterns",
            rel,
            f"第{number}章出现章末预告/总结句式「{ending.group(0)}」，改用具体钩子收束",
        ))
    return findings


def _audit_ai_patterns(context: dict) -> tuple[list[dict], str | None]:
    banned = _ai_banned_words()
    findings: list[dict] = []
    for number in context["numbers"]:
        body_path = context["body_by_number"].get(number)
        if body_path is None:
            continue
        text = _read_body_text(body_path)
        if text is None:
            continue
        findings.extend(
            _scan_ai_patterns(context["root"], body_path, number, text, banned)
        )
    return findings, None


def novel_audit_fix(
    workdir, finding, action, planned_resolution_chapter=None, dry_run=False
) -> dict:
    root = Path(workdir).expanduser()
    missing = _require_novel_project(root)
    if missing is not None:
        return missing

    if not isinstance(finding, dict):
        return {
            "ok": False,
            "error": f"finding 必须是审计结论对象，收到：{type(finding).__name__}",
        }
    type_name = str(finding.get("type") or "").strip()
    if not type_name:
        return {"ok": False, "error": "finding 缺少 type 字段，无法判断问题类型"}
    if type_name not in AUDIT_FIX_TYPES:
        return {
            "ok": False,
            "supported": False,
            "error": "该类型不支持自动修复，请交给星瑶按证据处理",
        }

    action_value = str(action or "").strip().lower()
    if action_value not in AUDIT_FIX_ACTIONS:
        return {
            "ok": False,
            "error": f"action 只接受 {' / '.join(AUDIT_FIX_ACTIONS)}，收到：{action}",
        }
    if action_value == "resolve":
        return {
            "ok": False,
            "supported": False,
            "error": "不提供自动标已回收：回收必须有正文证据，请交给星瑶",
        }

    matched = FORESHADOW_ID_IN_MESSAGE_RE.search(str(finding.get("message") or ""))
    if matched is None:
        return {"ok": False, "error": "无法从问题描述中定位伏笔编号"}
    identifier = matched.group(1)

    state = _load_tracking_state(root)
    if not isinstance(state, dict):
        return {
            "ok": False,
            "error": (
                f"追踪状态不可用：{TRACKING_SUBDIR.as_posix()}/{TRACKING_STATE_NAME} "
                "缺失或无法解析（先运行 novel_track init）"
            ),
        }
    rows = state.get("foreshadow")
    row = rows.get(identifier) if isinstance(rows, dict) else None
    if not isinstance(row, dict):
        return {"ok": False, "error": f"追踪状态里没有伏笔 {identifier}，无法修复"}

    last_commit = _as_non_negative_int(state.get("last_committed_chapter"))
    before = {
        "planned_resolution_chapter": row.get("planned_resolution_chapter"),
        "updated_chapter": row.get("updated_chapter"),
    }

    after_planned = None
    if action_value == "reschedule":
        raw_planned = planned_resolution_chapter
        if isinstance(raw_planned, bool):
            return {"ok": False, "error": "planned_resolution_chapter 必须是正整数"}
        try:
            planned = int(raw_planned) if raw_planned is not None else None
        except (TypeError, ValueError):
            planned = None
        if planned is None or planned <= 0:
            return {
                "ok": False,
                "error": (
                    "reschedule 需要正整数 planned_resolution_chapter，"
                    f"收到：{raw_planned!r}"
                ),
            }
        if planned <= last_commit:
            return {
                "ok": False,
                "error": (
                    "计划回收章不能早于或等于当前已提交章"
                    f"（第{last_commit}章），收到：第{planned}章"
                ),
            }
        after_planned = planned

    after = {"planned_resolution_chapter": after_planned, "updated_chapter": last_commit}
    would_change = (
        before["planned_resolution_chapter"] != after_planned
        or before["updated_chapter"] != last_commit
    )
    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "foreshadow_id": identifier,
            "before": before,
            "after": after,
            "would_change": would_change,
        }

    state_path = root / TRACKING_SUBDIR / TRACKING_STATE_NAME
    table_path = root / TRACKING_SUBDIR / FORESHADOW_TABLE_NAME

    row["planned_resolution_chapter"] = after_planned
    row["updated_chapter"] = last_commit
    _write_text(
        state_path,
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        tool_name="novel_audit_fix",
    )

    planned_cell = (
        FORESHADOW_PLANNED_PLACEHOLDER
        if after_planned is None
        else f"第{after_planned}章"
    )
    table_updated = False
    if table_path.is_file():
        try:
            table_text = table_path.read_text(encoding="utf-8")
        except OSError as error:
            return {
                "ok": False,
                "error": f"伏笔表无法读取：{table_path}（{error}）",
                "state_path": str(state_path),
            }
        patched, table_updated = _patch_foreshadow_table_text(
            table_text, identifier, planned_cell
        )
        if table_updated:
            _write_text(table_path, patched, tool_name="novel_audit_fix")

    verified_state = _load_tracking_state(root)
    verified_rows = (
        verified_state.get("foreshadow") if isinstance(verified_state, dict) else None
    )
    verified_row = (
        verified_rows.get(identifier) if isinstance(verified_rows, dict) else None
    )
    if not isinstance(verified_row, dict) or (
        verified_row.get("planned_resolution_chapter") != after_planned
    ):
        return {
            "ok": False,
            "error": (
                f"写入后自检失败：{_audit_relpath(root, state_path)} 中伏笔 "
                f"{identifier} 的计划回收章未生效"
            ),
            "foreshadow_id": identifier,
            "before": before,
            "after": after,
            "state_path": str(state_path),
            "table_path": str(table_path),
        }

    result = {
        "ok": True,
        "dry_run": False,
        "foreshadow_id": identifier,
        "before": before,
        "after": after,
        "state_path": str(state_path),
        "table_path": str(table_path),
        "table_updated": table_updated,
    }
    result["still_reported"] = _audit_fix_still_reported(root, type_name, identifier)
    return result


def _patch_foreshadow_table_text(
    text: str, identifier: str, planned_cell: str
) -> tuple[str, bool]:
    lines = text.splitlines(keepends=True)
    for index, line in enumerate(lines):
        body = line.rstrip("\r\n")
        ending = line[len(body):]
        parts = body.split("|")
        if len(parts) <= FORESHADOW_TABLE_PLANNED_COL:
            continue
        if parts[1].strip() != identifier:
            continue
        parts[FORESHADOW_TABLE_PLANNED_COL] = f" {planned_cell} "
        lines[index] = "|".join(parts) + ending
        return "".join(lines), True
    return text, False


def _audit_fix_still_reported(root: Path, type_name: str, identifier: str) -> bool:
    """修复后再跑一次全项目审计，判断该伏笔是否仍被报出（只读）。"""
    audit = novel_audit(str(root), scope="all")
    if not isinstance(audit, dict) or not audit.get("ok"):
        return False
    for finding in audit.get("findings") or []:
        if not isinstance(finding, dict) or finding.get("type") != type_name:
            continue
        matched = FORESHADOW_ID_IN_MESSAGE_RE.search(
            str(finding.get("message") or "")
        )
        if matched is not None and matched.group(1) == identifier:
            return True
    return False
