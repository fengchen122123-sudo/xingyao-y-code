"""小说工作区核心实现（建书、Agent 转换、资料部署、大纲取段、追踪提交）。

业务逻辑集中在 core 包内，插件 ``plugins/novel_workspace`` 只做工具注册与转发，
GUI 侧可直接调用本模块，不依赖插件是否加载。

数据契约见 ``docs/2026-10-28-novel-workspace-design.md``：
- §4.1 项目目录：设定/大纲/正文/追踪/对标（复用 oh-story 契约，vendored 脚本可直接跑）；
- §4.2 项目标记：``.novel.json``，存在即视为小说项目；
- §5.3 工具行为约束：先校验项目标记；脚本统一用 ``sys.executable`` 子进程调用，
  超时 30 秒，stderr 原样带回，不吞错、不修补脚本状态；
- §6.1 Agent 转换规则：上游 frontmatter 的多行块与数组语法星瑶解析器不支持，
  因此程序化转换为单行 key: value，并按下表固定 base_type / model / allowed_tools；
- §6.2 Agent 正文路径改写：``.claude/skills/story-setup/references/agent-references/``
  改写为项目内 `.ycode` 路径，并注入两级回退说明。

确定性脚本（outline_view.py / tracking_commit.py / wordcount_core.py）以源码副本
形式放在 ``plugins/novel_workspace/vendor/``，由 ``vendor_dir()`` 定位，来源与哈希
记在 ``VERSION.lock``；升级只替换脚本，不改调用方。
"""

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

# 项目标记文件名：存在即视为小说项目
NOVEL_MARKER = ".novel.json"

# 项目标记 schema 与引擎标识（固定值，供后续 novel_status 等工具校验）
NOVEL_SCHEMA = 1
ENGINE_NAME = "story-ycode"
ENGINE_VERSION = "1.0.0"

# 项目目录契约（§4.1）：顺序即创建顺序，父目录先于子目录
NOVEL_DIRS = (
    "设定",
    "设定/角色",
    "大纲",
    "正文",
    "追踪",
    "追踪/逐章记录",
    "对标",
)

# 星瑶侧落点（相对项目根）
AGENTS_SUBDIR = Path(".ycode") / "agents"
REFERENCES_SUBDIR = (
    Path(".ycode") / "skills" / "story-ycode" / "references" / "agent-references"
)

# 上游技能包位置：默认用户全局安装；环境变量覆盖便于离线安装与测试
STORY_SETUP_ENV = "YCODE_STORY_SETUP_ROOT"

# 上游工具名 → 星瑶工具名（§6.1 映射表）
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

# 7 个写作子 Agent 的最终配置（§6.1 表格）。
# upstream_tools 用上游工具名声明、经 TOOL_NAME_MAP 转换后即得到设计表里的
# allowed_tools（顺序也一致）；base_type / model 按表格固定，不从上游推导，
# 避免上游换模型时星瑶侧静默漂移。
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

# 上游正文里写死的资料路径前缀 → 星瑶项目内路径前缀
_UPSTREAM_REF_PREFIX = ".claude/skills/story-setup/references/agent-references/"
_PROJECT_REF_PREFIX = ".ycode/skills/story-ycode/references/agent-references/"
# 上游正文里省略项目根的相对写法（如表格中的 `story-setup/references/agent-references/x.md`）
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
    """插件自带资源根目录：随包分发，不依赖用户是否安装上游技能包。

    开发态为 <repo>/plugins/novel_workspace；打包后为 <_MEIPASS>/plugins/novel_workspace。
    """
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
    """在 workdir 初始化小说项目，返回创建/跳过清单。

    行为（§5.2 novel_init）：
    1. 建目录树：设定/角色、大纲、正文、追踪/逐章记录、对标；
    2. 写 ``.novel.json`` 项目标记（已存在则不覆盖）；
    3. 转换并部署 7 个上游 Agent 到 ``.ycode/agents/``（已存在则不覆盖）；
    4. 复制上游 agent-references 全部 .md 到
       ``.ycode/skills/story-ycode/references/agent-references/``（已存在则不覆盖）。

    幂等：第二次调用 created 为空，所有条目进 skipped，已有文件内容不变。
    上游技能包缺失时不抛异常，缺失项进 skipped，目录与标记照常创建。
    """
    root = Path(workdir).expanduser()
    root.mkdir(parents=True, exist_ok=True)

    created: list[str] = []
    skipped: list[str] = []

    # 1. 目录树
    for rel in NOVEL_DIRS:
        path = root / rel
        entry = rel + "/"
        if path.is_dir():
            skipped.append(entry)
        else:
            path.mkdir(parents=True, exist_ok=True)
            created.append(entry)

    # 2. 项目标记
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

    # 3 + 4. Agent 与参考资料
    created.extend(_deploy_agents(root, skipped))
    created.extend(_deploy_references(root, skipped))

    return {
        "ok": True,
        "created": created,
        "skipped": skipped,
        "already_initialized": already_initialized,
    }


def convert_agent_md(source_path, spec) -> str:
    """把上游 Agent 模板转换为星瑶格式文本。

    - frontmatter 只保留 name/description/when_to_use/base_type/model/allowed_tools，
      全部单行；
    - description 由上游多行块合并为单行且不超过 200 字；
    - 正文保留上游原文，仅改写资料路径并注入两级回退说明。
    """
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
    """目标字数归一到非负整数；非法输入按 0 处理，不让建书失败。"""
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _utc_now() -> str:
    """ISO8601 UTC 时间戳，形如 2026-10-28T00:00:00Z。"""
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _write_text(path: Path, text: str, *, tool_name: str = "novel_init") -> None:
    """统一 UTF-8 + LF 写文件（Windows 下避免 CRLF 污染 Agent 定义）。

    按回合撤销（规格 9.5）：与 hunyuan_image / tokenrhythm 落盘同一模式，
    先由 FileMutationRecorder 持久化计划 after 快照，再落盘、再校验，三段
    在同一把工作区锁内完成。回合外调用（无活动检查点）降级为不可撤销写入，
    不阻塞建书；检查点已封闭时按规格 9.2 拒绝触盘。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = text.encode("utf-8")

    # 惰性 import：turn_undo 会反向加载默认注册表 → 插件 → 本模块，顶层导入易成环
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
    """部署 7 个星瑶格式 Agent，返回新建文件清单。"""
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
    """复制上游 agent-references 全部 .md（含子目录），返回新建文件清单。"""
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


# frontmatter 分隔：与 core.subagent_registry 保持同一形状（--- 包夹）
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)

# 上游 description 使用 YAML 块标量（| / > 及其变体）
_BLOCK_SCALARS = ("|", ">", "|-", ">-", "|+", ">+")


def _split_frontmatter(text: str):
    """拆出 (frontmatter_dict, body)。

    上游模板含 ``description: |`` 多行块与 ``tools: [..]`` 数组、注释行，
    core.subagent_registry._parse_frontmatter 只能读单行 key: value，
    因此这里单独解析上游格式，不复用注册中心解析器。
    """
    match = _FRONTMATTER_RE.match(text.lstrip("\ufeff"))
    if not match:
        return {}, text.strip()
    return _parse_upstream_header(match.group(1)), match.group(2).strip()


def _parse_upstream_header(raw: str) -> dict:
    """解析上游 frontmatter：支持单行值、块标量续行，忽略注释与嵌套缩进。"""
    data: dict[str, str] = {}
    key: str | None = None
    buffer: list[str] = []

    def flush() -> None:
        nonlocal key, buffer
        if key is not None and buffer:
            # 中文折行处不需要空格，直接拼接后再归并空白
            data[key] = "".join(part.strip() for part in buffer)
        key, buffer = None, []

    for line in raw.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[0] in " \t":
            # 块标量续行；无归属时忽略
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
    """把多行文本合并为单行并截断到 limit 字（超长以省略号收尾）。"""
    merged = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(merged) > limit:
        merged = merged[: limit - 1].rstrip() + "…"
    return merged


def _map_upstream_tools(tools) -> tuple[str, ...]:
    """上游工具名映射为星瑶工具名；未知工具丢弃，结果去重保序。"""
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


# ---------------------------------------------------------------------------
# vendored 确定性脚本接入（§5.1 vendor/、§5.2 novel_outline/novel_track）
# ---------------------------------------------------------------------------

# vendor 目录：随插件发布，插件目录与 core 目录同属仓库根下的兄弟目录
VENDOR_SUBDIR = Path("plugins") / "novel_workspace" / "vendor"
# 环境变量覆盖 vendor 目录：便于离线安装、打包态与测试注入
VENDOR_ENV = "YCODE_NOVEL_VENDOR_DIR"
# 需要一起 vendor 的脚本；tracking_commit.py 运行期按 __file__ 同目录加载
# wordcount_core.py，两者必须同时存在
VENDOR_SCRIPTS = ("outline_view.py", "tracking_commit.py", "wordcount_core.py")
# 版本锁文件名（§10：锁 commit，升级需人工比对差异）
VERSION_LOCK_NAME = "VERSION.lock"
# 子进程硬超时（§5.3）：脚本卡死时按超时失败返回，不无限等待
SCRIPT_TIMEOUT_SECONDS = 30

# novel_track 三种模式：init 建状态；append 追加新章；revision 修订已提交章
TRACK_MODES = ("init", "append", "revision")
# 模式 → tracking_commit.py 子命令（脚本只有 init / commit / check 三个子命令）
TRACK_SUBCOMMANDS = {"init": "init", "append": "commit", "revision": "commit"}
# novel_outline 的 stage：outline＝排纲档（带批次底稿），write＝写作档
OUTLINE_STAGES = ("outline", "write")
# 卷纲落点（§4.1）：大纲/卷纲_第001卷.md
OUTLINE_SUBDIR = Path("大纲")
OUTLINE_GLOB = "卷纲_*.md"
# 追踪目录（§4.1）：提交后由脚本写出 _tracking-state.json 与派生视图
TRACKING_SUBDIR = Path("追踪")
# 追踪状态文件名（tracking_commit.py 写出的唯一状态源）
TRACKING_STATE_NAME = "_tracking-state.json"
# 正文目录（§4.1）：正文文件落点，文件名为 第NNN章_章名.md
BODY_SUBDIR = Path("正文")
# 正文文件命名契约：第 + 三位以上章号 + 下划线 + 章名 + .md
CHAPTER_FILE_RE = re.compile(r"^第(\d{3,})章_.*\.md$")
# 细纲文件命名契约：第NNN章.md（三位零填充）
OUTLINE_DETAIL_NAME = "细纲_第{chapter:03d}章.md"
# novel_status 单次扫描的章节上限（§5.3：不得整读正文，扫描有界）
CHAPTER_SCAN_LIMIT = 500
# 伏笔状态枚举（与 tracking_commit.py 一致）：未回收即视为活跃
FORESHADOW_STATUS_ACTIVE = "已埋"

# novel_audit 审计范围：chapter＝指定章或最新章；all＝全项目（单次上限 CHAPTER_SCAN_LIMIT）
AUDIT_SCOPES = ("chapter", "all")
# 审计项开关：P1 只支持 auto（全部检查项跑一遍，缺 checker 的项进 skipped）
AUDIT_CHECKS_DEFAULT = "auto"
# 审计结论分级（S1 最高）：S1 状态断链、S2 伏笔超期、S3 进度/字数风险、S4 风格提示
AUDIT_LEVELS = ("S1", "S2", "S3", "S4")
# 伏笔超期阈值：已埋且距最近提交章超过 20 章仍未回收 → S2
FORESHADOW_OVERDUE_CHAPTERS = 20
# 单章字数下限系数：正文可见字数低于细纲字数目标的 80% → S3
CHAPTER_WORDCOUNT_FLOOR = 0.8
# 细纲里的字数目标字段（形如"字数目标：3000"）
OUTLINE_WORD_TARGET_RE = re.compile(r"字数目标\s*[:：]\s*(\d+)")
# 细纲文件名匹配（与 OUTLINE_DETAIL_NAME 同构，用于反解章号）
OUTLINE_DETAIL_RE = re.compile(r"^细纲_第(\d{3,})章\.md$")
# 卷纲文件名匹配（卷纲_第NNN卷.md → 卷号）；仅用于章节卷归属
VOLUME_OUTLINE_RE = re.compile(r"^卷纲_第(\d+)卷\.md$")
# 卷纲里声明的章号范围（与 vendored outline_view.py 的 CH_RANGE 同构）
OUTLINE_CHAPTER_RANGE_RE = re.compile(
    r"章节范围\s*[:：]\s*第\s*(\d+)\s*[-–—~至]\s*(\d+)\s*章"
)
# AI 味检查脚本名（上游未随包分发，缺失时固定进 skipped）
AI_PATTERN_CHECKER = "check-ai-patterns.js"


def vendor_dir() -> Path:
    """返回 vendored 脚本目录。

    优先级：环境变量 ``YCODE_NOVEL_VENDOR_DIR`` > 打包态 ``sys._MEIPASS/plugins``
    > 开发态仓库根 ``plugins/``。与 core.plugin_loader._builtin_plugin_dir
    保持同一套解析规则，避免开发态与打包态找错目录。
    """
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
    """取卷纲段文本（§5.2 novel_outline）：转发 vendored ``outline_view.py``。

    参数映射：``unit_id → --unit``、``stage → --stage``、``toc → --toc``。
    卷纲文件按 ``大纲/卷纲_*.md`` 自动发现；命中多个时要求用 ``path`` 指定，
    不猜、不合并，避免取错卷的文本。

    返回：
    - 成功：``{"ok": True, "text": stdout}``（stderr 非空时附 ``stderr``，供告警透传）；
    - 失败：``{"ok": False, "error": ..., "stderr": ...}``；
    - 非小说项目：``{"ok": False, "is_novel": False, "error": ...}``。
    """
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
        # 未声明作用域等告警走 stderr，原样带回但不算失败
        payload["stderr"] = result["stderr"]
    return payload


def novel_track(workdir, mode, transaction) -> dict:
    """提交追踪事务（§5.2 novel_track）：转发 vendored ``tracking_commit.py``。

    接口事实（读脚本确认，不靠猜）：脚本只从 ``--input <文件>`` 读 JSON，
    不读 stdin；因此事务文档先写临时文件再以参数传入，读完即删。
    只做转发：脚本拒绝的事务原样返回错误，本函数不重算、不修补状态（§5.3）。
    """
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

    # 脚本在自己的子进程里落盘，无法在写前调用记录器；先拍前后快照，
    # 成功后把变更文件重放一遍记录器写法，保证按回合撤销有正确的 before/after。
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
    """用 wordcount_core 的 visible_body 取正文，再按 visible_chars_v1 口径计非空白字符。

    visible_chars_v1 的空白集合是 vendor 模块内的固定常量；这里直接复用该常量，
    保证与脚本口径逐字符一致（若上游调整，随 VERSION.lock 一起升级）。
    """
    body = module.visible_body(text)
    whitespace = getattr(module, "_WHITE_SPACE_CODEPOINTS", None)
    if whitespace is None:
        # 常量改名时退回 Unicode 空白判断，仍在同一量级，不因上游改动直接崩
        return sum(1 for character in body if not character.isspace())
    return sum(1 for character in body if ord(character) not in whitespace)


def _read_body_text(path: Path) -> str | None:
    """读正文文件为字符串；无法解码/读取时返回 None（不打断整次扫描）。"""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _chapter_number_from_name(name: str) -> int | None:
    """从正文文件名提取章号（第NNN章_...）；不合规文件名返回 None。"""
    match = CHAPTER_FILE_RE.match(name)
    return int(match.group(1)) if match else None


def _list_chapter_files(root: Path) -> list[Path]:
    """列出 正文/ 下符合命名契约的章节文件，按章号升序、单次最多 500 章。"""
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
    """按章号定位正文文件，返回 (路径, 错误说明)，二者恰有一个为 None。

    支持 int、纯数字字符串、"第001章" 或带 .md 的文件名；统一按提取出的章号匹配，
    因此 1 / "1" / "第001章" / "第001章_夜行.md" 都能命中同一章。
    """
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
    """读 追踪/_tracking-state.json；缺失或损坏时返回 None（状态工具按 0 处理）。"""
    path = root / TRACKING_SUBDIR / TRACKING_STATE_NAME
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _as_non_negative_int(value, default: int = 0) -> int:
    """把 JSON 里的数字安全转成非负整数；非法值回落默认值。"""
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def _count_active_foreshadows(state: dict | None) -> int:
    """统计状态为"已埋"（未回收）的伏笔数；状态结构按 dict/id→row 读取。"""
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
    """取追踪状态里的权威逐章字数；记录缺失或 actual 非正整数时返回 None。"""
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
    """返回小说项目状态（§5.2 novel_status）；非小说项目只返回 ``{"is_novel": False}``。

    只读扫描：不写文件、不调子进程；正文按章文件名枚举（单次上限 500 章），
    逐章用 vendored wordcount_core 的 visible_body 口径统计可见字符；
    ``today_words`` 以章节文件 mtime 落在本地当天判定（追踪记录无日期字段）。

    除原有 9 个字段外，额外透出 ``target_words``（来自 .novel.json）与
    ``chapters``（逐章 number/name/words/submitted/outline_ready，卷归属
    可解析时附 volume）：``words`` 优先取追踪记录 ``wordcount_records`` 的
    ``actual``，缺失时回落到本次扫描的可见字数；读不出正文的章仍会列出，
    ``words`` 为 0，不因缺正文而消失。
    """
    root = Path(workdir).expanduser()
    # 项目标记校验：非小说项目按契约只返回 is_novel=False，不附带错误细节
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
        # 标记损坏不阻塞状态展示：书名/目标字数留空，其余字段照常统计
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
    """统计可见字数（§5.2 novel_wordcount）。

    两种用法互斥：
    - 传 ``text``：直接按 visible_chars_v1 口径计算（chapter 仅作回显）；
    - 传 ``workdir`` + ``chapter``：校验 .novel.json 后读 正文/ 对应章节再计算。

    返回 ``{"ok": True, "visible_chars": N, "chapter": ...}``；
    参数缺失、非小说项目、章节文件不存在返回 ``{"ok": False, "error": ...}``。
    """
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


# ---------------------------------------------------------------------------
# GUI 正文读写：read_chapter / save_chapter（小说工作台章节编辑器）
# ---------------------------------------------------------------------------


def _body_path_entry(root: Path, path: Path) -> str:
    """正文文件的对外路径：相对项目根的 posix 形式（前端可直接展示/比对）。"""
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        # 理论上不会发生（路径由 _resolve_chapter_file 在项目内拼出）；保底回绝对路径
        return path.as_posix()


def _chapter_number_for_result(path: Path, chapter) -> int:
    """回显用章号：优先按文件名提取（第NNN章），不合规文件名退回入参里的数字。"""
    number = _chapter_number_from_name(path.name)
    if number is not None:
        return number
    match = re.search(r"\d+", str(chapter or ""))
    return int(match.group()) if match else 0


def read_chapter(workdir, chapter) -> dict:
    """读取单章正文原文，供 GUI 章节编辑器展示。

    校验 ``.novel.json`` 后用 ``_resolve_chapter_file`` 定位正文（章号或文件名
    均可），返回 ``{"ok": True, "chapter": N, "path": "正文/第001章_x.md",
    "content": 原文, "visible_chars": N}``；非小说项目、章节不存在、文件不可读
    时返回 ``{"ok": False, "error": ...}``。只读：不改动任何文件。
    """
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
    """覆盖保存单章正文，返回保存后的可见字数。

    定位规则与 ``read_chapter`` 一致：章节必须已存在，本函数**不新建**章节
    （新章走 Agent 写作流程）。内容按 UTF-8 + LF 经 ``_write_text`` 落盘，
    与其它写文件工具共用同一套按回合撤销记录，不直接用 ``open`` 写。
    返回 ``{"ok": True, "chapter": N, "path": ..., "visible_chars": N}``，
    其中 ``visible_chars`` 按保存后的内容重算（与 novel_status 同口径）。
    """
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

# 「大纲/设定」预览只开放这三个子目录（与 §4.1 目录契约一致）+ 仅 .md
DOC_PREVIEW_SUBDIRS = (OUTLINE_SUBDIR.as_posix(), "设定", TRACKING_SUBDIR.as_posix())
# 单文件预览上限：超过 2MB 不送前端（大文件走编辑器，不在面板里渲染）
DOC_PREVIEW_MAX_BYTES = 2 * 1024 * 1024
# recent_daily 的天数窗口（含今天，固定 7 项，无数据的天补 0）
DAILY_WINDOW_DAYS = 7


def _as_text(value) -> str:
    """JSON 里的文本字段：非字符串与缺失一律给空串，不让 None 混进返回契约。"""
    return value.strip() if isinstance(value, str) else ""


def _as_str_list(value) -> list[str]:
    """JSON 里的数组字段：只留字符串项；非数组或缺失一律给空列表（不用 null）。"""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _tracking_rows(state: dict | None, key: str) -> list[tuple[str, dict]]:
    """取追踪状态里的某张表（foreshadow / characters）：兼容 dict 与 list 两种形态。

    dict 形态的键是 id / 角色名，list 形态（历史数据）用行内 id 或下标兜底。
    """
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
    """把伏笔表摊平成前端可直接渲染的行，两个「超期」维度分开给。

    ``overdue`` 与 ``_audit_foreshadow_overdue`` 逐条对齐：状态为「已埋」、埋设章
    有效（>0），且最近提交章 − 埋设章 **超过** ``FORESHADOW_OVERDUE_CHAPTERS``
    （严格大于，正好等于阈值不算超期）。埋设章非法时无法判定，audit 会跳过，
    这里同样不给 overdue，避免面板与体检结论两套标准。

    ``planned_overdue`` 是写作视角的另一维度：计划回收章已过（``< next_chapter``）
    但仍标记「已埋」。它不等同于 audit 的 overdue——audit 看埋设章搁置了多久，
    这里看「承诺在第 N 章回收却没回收」，两者可能同时成立也可能只有一个成立，
    所以分开给，由界面分别提示。
    """
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
    # 先体检超期、再计划超期、再搁置最久、再埋设最早；同序按 id 保证输出稳定
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
    """把角色表摊平成前端卡片：字符串字段给空串、数组字段给 []，不用 null 混用。"""
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
    # 有未结线索的角色排前面，其余按名字排序（名字全中文，排序稳定）
    entries.sort(key=lambda item: (not item["open_threads"], item["name"]))
    return entries


def _workbench_volumes(root: Path, next_chapter: int) -> list[dict]:
    """扫 大纲/卷纲_第N卷.md，给出卷号、卷名、可见字数与「当前卷」标记。

    ``chars`` 用与 novel_status 同一套 visible_chars 口径；wordcount_core 不可用时
    给 0（不谎报大小）。``current`` 依据 ``_volume_chapter_ranges`` 判定，只有
    恰好唯一命中下一章所在卷才为 True，范围重叠或未声明时全为 False。
    """
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
            # 唯一命中才算当前卷：current 为 None（未声明/重叠）时全为 False
            "current": current is not None and current == number,
        })
    volumes.sort(key=lambda item: item["no"])
    return volumes


def _workbench_recent_daily(root: Path, days: int = DAILY_WINDOW_DAYS) -> list[dict]:
    """近 N 天（含今天）的正文新增字数，固定 N 项、按日期升序、无数据补 0。

    口径与 novel_status 的 ``today_words`` 完全一致：章节文件的 mtime 落在哪一天，
    就把该章整章可见字数计入哪一天（追踪记录没有日期字段，只能按文件写入时间）。
    这里是独立函数，因此自己重新枚举一次章节文件；wordcount_core 缺失时全为 0。
    """
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
    """返回小说工作台的聚合数据（只读；不写文件、不调子进程）。

    非小说项目与 ``novel_status`` 同口径判断（复用 ``_require_novel_project``），
    只返回 ``{"is_novel": False}``；小说项目返回四张表：

    - ``foreshadows``：伏笔扁平行（含 overdue / stale_chapters，overdue 与
      novel_audit 的超期口径一致），按超期 → 搁置章数 → 埋设章排序；
    - ``characters``：角色卡片（有未结线索的排前面）；
    - ``volumes``：卷纲清单（卷号 / 卷名 / 可见字数 / 是否当前卷）；
    - ``recent_daily``：近 7 天逐日正文新增字数（固定 7 项）。
    """
    root = Path(workdir).expanduser()
    # 项目标记校验：与 novel_status 一致，非小说项目只回 is_novel=False
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
    """读取 大纲/设定/追踪 下的 .md 文档，供前端只读预览。

    白名单是安全边界，按下列顺序校验，任一不通过都返回结构化错误而不抛异常：

    1. ``rel_path`` 必须是相对路径（绝对路径直接拒）；
    2. ``(项目根 / rel_path).resolve()`` 必须仍在项目根内（``..`` 穿越、符号链接
       逃逸都在这一步被挡，因为 resolve 会把链接解到真实路径）；
    3. 相对路径第一段必须属于 ``DOC_PREVIEW_SUBDIRS``（大纲 / 设定 / 追踪）；
    4. 扩展名只允许 ``.md``；
    5. 文件必须存在、可读，且不超过 ``DOC_PREVIEW_MAX_BYTES``（2MB）。

    成功返回 ``{"ok": True, "text": 内容, "chars": 可见字符数, "path": 相对 posix}``；
    ``chars`` 与 novel_status 同口径（wordcount_core 缺失时按非空白字符数兜底）。
    """
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


# ---- 多书发现与导出（P3-B） ----

# 扫描多书时跳过的目录名：VCS、星瑶自身数据、依赖与导出产物
DISCOVER_SKIP_DIRS = {
    ".git", ".ycode", ".idea", ".vscode", "node_modules",
    "__pycache__", "site-packages", "导出",
}
DISCOVER_MAX_DEPTH = 2
DISCOVER_MAX_BOOKS = 50
EXPORT_SUBDIR = "导出"
EXPORT_FORMATS = ("md", "txt")


def _novel_marker_title(root: Path) -> str:
    """读 .novel.json 的标题，读不到时退回目录名（坏标记不让多书列表整体失败）。"""
    try:
        data = json.loads((root / NOVEL_MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return root.name
    title = str(data.get("title") or "").strip() if isinstance(data, dict) else ""
    return title or root.name


def _count_chapter_files(root: Path) -> int:
    """只数正文文件、不读内容：多书列表要快，精确字数留给 novel_status。"""
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
    """列出工作区内的小说项目（自身 + 有限深度子目录），供 GUI 多书切换。

    - 含 ``.novel.json`` 的目录即为一本书，命中后不再向下钻，避免把书的子目录
      误判成另一本书；
    - 跳过隐藏目录、依赖目录与导出目录，默认向下 2 层；
    - 只读目录结构与项目标记，不逐章读正文（字数交给 novel_status 按需统计）。

    返回 ``{"ok": True, "root": 绝对路径, "projects": [{path, title,
    chapters_written}]}``，其中 ``path`` 是相对 root 的 posix 路径，root 自身为 "."。
    """
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
    """txt 导出：去掉 frontmatter 与 ATX 标题记号，只留正文文字。"""
    body = re.sub(r"\A---\s*\n.*?\n---\s*\n", "", text, flags=re.DOTALL)
    return "\n".join(re.sub(r"^#{1,6}\s*", "", line) for line in body.splitlines()).strip()


def export_novel(workdir, fmt="md") -> dict:
    """把全书正文按章号顺序导出为单个 md / txt 文件。

    产物写到 ``{项目}/导出/{书名}.{fmt}``，经 ``_write_text`` 落盘，与其它写文件
    工具共用按回合撤销记录。章序由 ``_list_chapter_files`` 决定（章号升序），
    同章多文件只取第一个；md 原样保留正文，txt 额外去掉 frontmatter 与标题记号。
    没有正文时返回 ``ok=False``，不生成空文件。
    """
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
    """当前线程是否有活动的按回合撤销检查点（无则不必重放记录）。"""
    try:
        # 惰性 import：turn_undo 会反向加载默认注册表 → 插件 → 本模块
        from core.turn_undo import recorder as _tu_recorder
        return _tu_recorder.is_context_active(_tu_recorder.current_context())
    except Exception:
        # 记录器不可用（测试替身/嵌入方）时按"无检查点"处理，不阻塞提交
        return False


def _snapshot_tracking_files(root: Path) -> dict[str, bytes] | None:
    """快照 追踪/ 下全部文件；无活动检查点时返回 None（省一次全量读盘）。"""
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
    """把子进程写入的追踪文件转成可撤销记录。

    做法：比对前后快照，对每个变更文件先把磁盘回退到 before，再经
    ``_write_text``（内部 prepare_mutation → 落盘 → verify_after_mutation）
    写回 after，从而留下正确的 before/after 记录。只处理 UTF-8 文本文件；
    子进程删掉的文件不在本函数范围内（tracking_commit 只归档不删除）。
    """
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
    """项目标记校验：缺失时返回 is_novel=False 的失败载荷，存在时返回 None。"""
    if (root / NOVEL_MARKER).is_file():
        return None
    return {
        "ok": False,
        "is_novel": False,
        "error": f"不是小说项目：{root} 下缺少 {NOVEL_MARKER}（先运行 novel_init）",
    }


def _resolve_outline_file(root: Path, path) -> tuple[Path | None, str | None]:
    """定位卷纲文件，返回 (路径, 错误说明)，二者恰有一个为 None。"""
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
    """把事务参数规范成待写入的 dict。

    接受 dict 或 JSON 字符串（模型可能传字符串）；不修改调用方对象。
    commit 类模式要求文档里的 ``mode`` 与入参一致：缺失时按入参补齐，
    冲突时直接报错，避免「传 append 却提交 revision」这类静默错位。
    """
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
    """用 ``sys.executable`` 跑 vendored 脚本（§5.3）。

    返回 ``{"ok", "stdout", "stderr", "returncode"}``；超时/启动失败也走
    同一结构（``ok=False`` 且带 ``error``），让调用方不必处理异常。
    stdout / stderr 按 UTF-8 解码：脚本自身写 buffer 输出 UTF-8 字节，
    Windows 默认 cp936 会解码失败，因此显式声明并不交给 text=True。
    """
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
    """把子进程失败结果转成工具错误载荷，stderr 原样带回。"""
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
    """取首个非空行作为简短错误消息（完整内容仍放在 stderr 字段）。"""
    for line in str(text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def _parse_script_json(text: str) -> dict | None:
    """解析脚本 stdout 的 JSON 对象；非对象或解析失败返回 None。"""
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


# ---------------------------------------------------------------------------
# 确定性审计：novel_audit（只读体检，按 S1~S4 分级输出 findings）
# ---------------------------------------------------------------------------


def novel_audit(workdir, scope="chapter", chapter=None, checks="auto") -> dict:
    """对小说项目做确定性体检（只读，不写文件、不跑子进程）。

    五项检查各自独立 try：单项异常或前置缺失只进 ``skipped``，不影响其余项，
    因此部分素材缺失时仍能拿到可用结论。

    - ``tracking_state``：``_tracking-state.json`` 的最近提交章与 正文/ 最大章号比对，
      不一致记 S1，状态文件缺失/损坏记 S3；
    - ``foreshadow_overdue``：状态为"已埋"且埋设章距最近提交章超过 20 章，逐条记 S2；
    - ``chapter_wordcount``：有细纲且正文存在时，正文可见字数低于细纲字数目标的
      80% 记 S3；
    - ``outline_gap``：审计范围内细纲存在但正文缺失记 S3；
    - ``ai_patterns``：本地词表与句式扫描（不依赖 Node），命中一级禁用词、
      「不是A，而是B」套句式、章末预告时逐条记 S4；词表取自插件资料库
      ``banned-words.md``，读不到时用内置兜底词表，不整体跳过。

    ``scope=chapter`` 只查指定章（``chapter``）或最新已写章节；``scope=all`` 覆盖
    全项目（单次上限 500 章）。``checks`` 在 P1 只接受 auto。
    """
    root = Path(workdir).expanduser()
    missing = _require_novel_project(root)
    if missing is not None:
        return missing

    # 参数校验放在项目校验之后：非小说项目优先返回 is_novel=False
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
    # 同章多个文件时只认排序后的第一个（与 _resolve_chapter_file 一致），不重复计数
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
        except Exception as exc:  # 单项异常不拖垮整次审计，记录后继续
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
    """构造审计上下文，返回 (context, 错误说明)，二者恰有一个为 None。

    context 字段：root / state / last_commit / body_by_number / numbers（本次审计
    覆盖的章号）/ scope。``chapter`` 指向的正文不存在时报参数错误，不做静默降级。
    """
    state = _load_tracking_state(root)
    last_commit = _as_non_negative_int(
        state.get("last_committed_chapter") if isinstance(state, dict) else None
    )
    body_max = max(body_by_number) if body_by_number else 0

    if scope_value == "all":
        numbers = _audit_all_numbers(root, body_by_number, body_max)
    elif chapter is None or not str(chapter).strip():
        # 未指定章：最新已写章节；顺带查它的下一章（细纲先行是常态，缺正文才算缺口）。
        # 正文为空时没有"最新章"，章级检查整体跳过。
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
    """列出 大纲/ 下 细纲_第NNN章.md 的章号（升序）；目录缺失时返回空列表。"""
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
    """细纲文件路径：大纲/细纲_第NNN章.md（不保证存在）。"""
    return root / OUTLINE_SUBDIR / OUTLINE_DETAIL_NAME.format(chapter=number)


def _outline_word_target(path: Path) -> int | None:
    """读细纲的"字数目标"；文件缺失、无该字段或值为 0 时返回 None。"""
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
    """构造一条审计结论：level/type/file/message 四字段固定。"""
    return {"level": level, "type": type_name, "file": file_path, "message": message}


def _audit_relpath(root: Path, path) -> str:
    """把项目内路径转成相对 POSIX 路径；项目外路径原样返回。"""
    try:
        return Path(path).relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _audit_tracking_state(context: dict) -> tuple[list[dict], str | None]:
    """检查 1：追踪最近提交章与正文最大章号是否一致（S1）／状态文件是否可用（S3）。"""
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
    """检查 2：已埋伏笔距最近提交章超过 20 章未回收，逐条记 S2。"""
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
            # 埋设章缺失/非法时无法判定超期，跳过该条而不误报
            continue
        distance = last_commit - planted
        if distance <= FORESHADOW_OVERDUE_CHAPTERS:
            continue
        identifier = str(row.get("id") or "?").strip() or "?"
        message = (
            f"伏笔 {identifier} 于第{planted}章埋设，已过 {distance} 章仍未回收，"
            f"超过阈值 {FORESHADOW_OVERDUE_CHAPTERS} 章"
        )
        summary = str(row.get("summary") or "").strip()
        if summary:
            message += f"：{summary}"
        findings.append(_audit_finding("S2", "foreshadow_overdue", rel, message))
    return findings, None


def _audit_chapter_wordcount(context: dict) -> tuple[list[dict], str | None]:
    """检查 3：有细纲且正文存在时，正文可见字数低于细纲字数目标的 80% 记 S3。"""
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
    """检查 4：审计范围内细纲存在但正文缺失记 S3（细纲先行，正文未落盘）。"""
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


# ---- 检查 5：AI 味确定性扫描（纯本地词表与句式，不依赖 Node） ----

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
# 「不是A，而是B」句式：同章出现 ≥2 次属 AI 味强证据（banned-words.md 的 ★★★★★）
AI_FORMULAIC_RE = re.compile(r"不是[^，。！？\n]{1,20}[，,]\s*(?:而)?是")
AI_FORMULAIC_LIMIT = 2
# 章末预告与总结句式：用具体钩子收束，别做空泛预告
AI_ENDING_RE = re.compile(r"他不知道的是|这一刻[，,]?(?:他|她)?终于明白|才刚刚开始")


def _markdown_section(text: str, heading: str) -> str:
    """取 markdown 里某个二级标题到下一个二级标题之间的正文。"""
    start = text.find(heading)
    if start < 0:
        return ""
    body = text[start + len(heading):]
    match = re.search(r"^## ", body, re.MULTILINE)
    return body[: match.start()] if match else body


def _ai_banned_words() -> tuple[str, ...]:
    """解析插件资料库 banned-words.md 的「一级禁用词」段落，缺失时用内置兜底。

    只取一级（出现即替换）词；二级是密度控制词，逐条报警会淹没真正的 AI 味证据。
    资料库随插件分发，读不到时退回内置词表仍能给出结论，因此不进 skipped。
    """
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
    """单章扫描：一级禁用词命中、套句式密度、章末预告，全部记 S4。"""
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
    """检查 5：AI 味确定性扫描（纯本地词表与句式，不依赖 Node）。

    词表优先取插件资料库 ``banned-words.md`` 的「一级禁用词」，缺席时用内置兜底
    词表，因此该检查不再因外部工具缺失整体跳过；命中一律记 S4（提示级），是否
    改写交给技能编排的 narrative-writer 复核，工具只提供确定性线索。
    """
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
