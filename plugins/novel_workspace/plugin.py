"""novel_workspace：小说工作区插件入口（薄壳）。

业务实现位于 core.novel_service（随主程序编译进 PYZ 分发），本文件只负责
注册工具与转发调用，不含目录创建、Agent 转换等任何业务逻辑。
"""

from __future__ import annotations

import json

from core.novel_service import (
    init_novel_project,
    novel_audit,
    novel_outline,
    novel_status,
    novel_track,
    novel_wordcount,
)
from core.tool_protocol import ToolResult, ToolSpec


def _to_result(result: dict) -> ToolResult:
    """把核心实现返回的字典序列化为工具结果；失败时标记 is_error。"""
    return ToolResult(
        json.dumps(result, ensure_ascii=False, indent=2),
        is_error=not result.get("ok", False),
    )


def _run_novel_init(workdir, title, genre="", platform="", target_words=0):
    """工具执行体：仅把核心实现返回的字典序列化为工具结果文本。"""
    result = init_novel_project(
        workdir,
        title,
        genre=genre,
        platform=platform,
        target_words=target_words,
    )
    return ToolResult(json.dumps(result, ensure_ascii=False, indent=2))


def _run_novel_outline(workdir, unit_id=None, stage="outline", toc=False, path=None):
    """工具执行体：转发 core.novel_service.novel_outline，不做文本加工。"""
    return _to_result(novel_outline(
        workdir,
        unit_id=unit_id,
        stage=stage,
        toc=toc,
        path=path,
    ))


def _run_novel_track(workdir, mode, transaction):
    """工具执行体：转发 core.novel_service.novel_track，不重算追踪状态。"""
    return _to_result(novel_track(workdir, mode, transaction))


def _run_novel_status(workdir):
    """工具执行体：转发 novel_status；非小说项目返回 is_novel=False 仍属正常结果。"""
    result = novel_status(workdir)
    return ToolResult(json.dumps(result, ensure_ascii=False, indent=2), is_error=False)


def _run_novel_wordcount(text=None, workdir=None, chapter=None):
    """工具执行体：转发 novel_wordcount；参数/文件错误按 is_error 标记。"""
    return _to_result(novel_wordcount(text=text, workdir=workdir, chapter=chapter))


def _run_novel_audit(workdir, scope="chapter", chapter=None, checks="auto"):
    """工具执行体：转发 novel_audit，只读体检，不改任何项目文件。"""
    return _to_result(novel_audit(
        workdir,
        scope=scope,
        chapter=chapter,
        checks=checks,
    ))


def register(registry):
    registry.register(ToolSpec(
        name="novel_init",
        description=(
            "初始化小说项目：在 workdir 建目录树（设定/角色、大纲、正文、追踪/逐章记录、对标），"
            "写 .novel.json 项目标记，并把 7 个写作子 Agent（story-architect、narrative-writer、"
            "character-designer、story-researcher、consistency-checker、chapter-extractor、"
            "story-explorer）与写作参考资料部署到项目 .ycode/ 下。"
            "【主动使用时机】用户要开新书、新建小说项目，或小说目录里缺少 .novel.json 时。"
            "【不要主动使用】已有 .novel.json 的项目重复调用只会跳过，不覆盖任何已有文件；"
            "普通代码项目不要调用。返回 created / skipped 清单，说明实际创建了什么。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "workdir": {
                    "type": "string",
                    "description": "小说项目目录（绝对路径或相对当前工作目录）；不存在时自动创建",
                },
                "title": {
                    "type": "string",
                    "description": "书名",
                },
                "genre": {
                    "type": "string",
                    "description": "题材类型，如 玄幻 / 都市 / 悬疑（可选）",
                },
                "platform": {
                    "type": "string",
                    "description": "目标平台，如 起点 / 番茄（可选）",
                },
                "target_words": {
                    "type": "integer",
                    "description": "目标总字数，默认 0（不设目标）",
                },
            },
            "required": ["workdir", "title"],
        },
        execute=_run_novel_init,
        read_only=False,
        mutates_files=True,          # 建目录、写标记与 Agent 文件
        requires_permission=True,    # 写操作走用户审批
        parallel_safe=False,
        activity_description="初始化小说项目",
        side_effect_scope="workspace",
    ))

    registry.register(ToolSpec(
        name="novel_outline",
        description=(
            "读取小说项目卷纲的指定片段：调用 vendored outline_view.py，按段头的作用域声明"
            "（卷级常任 / 单元级 / 批次底稿）选择性输出，用来在写作期只取本单元需要的卷纲，"
            "不必整读卷纲文件。unit_id 指定单元（如 D1-03），stage=outline 给排纲档（含在用批次底稿），"
            "stage=write 给写作档（只给卷级常任＋单元级）；toc=true 返回段目录（段号、作用域、单元、字符数）。"
            "卷纲文件默认按 大纲/卷纲_*.md 自动发现，命中多个时必须用 path 指定。"
            "【主动使用时机】写某一单元前取该单元卷纲段、核对卷纲作用域、需要卷纲目录时。"
            "【不要主动使用】需要整读卷纲原文时直接用 read_file。"
            "返回 {ok: true, text} 为卷纲段文本；单元不存在或脚本非 0 退出返回 "
            "{ok: false, error, stderr}，stderr 为脚本原样输出；非小说项目返回 is_novel: false。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "workdir": {
                    "type": "string",
                    "description": "小说项目目录（含 .novel.json）",
                },
                "unit_id": {
                    "type": "string",
                    "description": "单元 ID，如 D1-03；省略时输出卷级常任与未声明段",
                },
                "stage": {
                    "type": "string",
                    "enum": ["outline", "write"],
                    "description": "outline＝排纲档（默认，带在用批次底稿）；write＝写作档（只给卷级常任＋单元级）",
                },
                "toc": {
                    "type": "boolean",
                    "description": "true 时输出段目录而非正文片段，默认 false",
                },
                "path": {
                    "type": "string",
                    "description": "卷纲文件路径（相对 workdir 或绝对路径）；大纲/ 下有多卷卷纲时用它指定",
                },
            },
            "required": ["workdir"],
        },
        execute=_run_novel_outline,
        read_only=True,
        mutates_files=False,
        requires_permission=False,
        parallel_safe=True,
        activity_description="读取卷纲片段",
        side_effect_scope="none",
    ))

    registry.register(ToolSpec(
        name="novel_track",
        description=(
            "提交小说追踪事务：调用 vendored tracking_commit.py，把模型给出的语义 JSON 校验合并进"
            "追踪/_tracking-state.json，并同步渲染 上下文.md、伏笔.md、时间线/、角色状态/、逐章记录/。"
            "mode=init 初始化追踪状态（project 内已有 _tracking-state.json 时脚本会拒绝）；"
            "mode=append 追加下一章（chapter 必须等于 last_committed_chapter+1）；"
            "mode=revision 修订已提交章（chapter 不得超出最后已提交章）。"
            "transaction 为事务文档对象，字段见 §5.2：schema_version、mode、chapter、chapter_title、"
            "expected_state_revision、delta、context、character_snapshots、wordcount。"
            "本工具只做转发：脚本拒绝的事务原样返回错误，不重算、不修补状态；"
            "同章重复 append 会返回错误（chapter delta already exists 系列），不会静默覆盖。"
            "【主动使用时机】写完一章并核对字数后提交追踪；修订已提交章后重新提交；首次建追踪状态。"
            "【不要主动使用】不要用它绕过脚本校验直接改 _tracking-state.json。"
            "成功返回脚本 JSON（last_committed_chapter、state_revision）；失败返回 "
            "{ok: false, error, stderr}，stderr 为脚本原样输出；非小说项目返回 is_novel: false。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "workdir": {
                    "type": "string",
                    "description": "小说项目目录（含 .novel.json 与 追踪/）",
                },
                "mode": {
                    "type": "string",
                    "enum": ["init", "append", "revision"],
                    "description": "init＝建追踪状态；append＝追加新章；revision＝修订已提交章",
                },
                "transaction": {
                    "type": "object",
                    "description": (
                        "事务文档：{schema_version, mode, chapter, chapter_title, "
                        "expected_state_revision, delta{result, character_changes, "
                        "foreshadow_changes, timeline_events, constraints, "
                        "next_chapter_commitments, retired_context_items, retired_characters}, "
                        "context{position, long_term_constraints, active_character_names, "
                        "continuity_risks}, character_snapshots, wordcount}"
                    ),
                    "additionalProperties": True,
                },
            },
            "required": ["workdir", "mode", "transaction"],
        },
        execute=_run_novel_track,
        read_only=False,
        mutates_files=True,          # 写 _tracking-state.json 与派生的追踪视图
        requires_permission=True,    # 写操作走用户审批
        parallel_safe=False,
        activity_description="提交追踪事务",
        side_effect_scope="workspace",
    ))

    registry.register(ToolSpec(
        name="novel_status",
        description=(
            "查看小说项目当前状态：只读扫描项目目录与追踪状态，返回 "
            "{is_novel, title, chapters_written, total_words, today_words, next_chapter, "
            "outline_ready, active_foreshadows, last_commit}。"
            "chapters_written 是 正文/ 下 第NNN章_*.md 的文件数；total_words 是各章可见字数"
            "（visible_chars_v1 口径，与 novel_wordcount 一致）之和；today_words 是 mtime 落在"
            "本地当天的章节字数之和；next_chapter = last_commit + 1；outline_ready 表示下一章"
            "细纲 大纲/细纲_第NNN章.md 是否已就绪；active_foreshadows 是状态为“已埋”的伏笔数；"
            "last_commit 是追踪状态里的最近提交章号。"
            "【主动使用时机】写稿前确认写到第几章、今日写了多少字、伏笔还剩几条、下一章细纲齐不齐。"
            "【不要主动使用】只想统计某一段文字字数时用 novel_wordcount(text=...)；"
            "非小说项目返回 {is_novel: false}。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "workdir": {
                    "type": "string",
                    "description": "小说项目目录（含 .novel.json）",
                },
            },
            "required": ["workdir"],
        },
        execute=_run_novel_status,
        read_only=True,
        mutates_files=False,
        requires_permission=False,
        parallel_safe=True,
        activity_description="读取小说项目状态",
        side_effect_scope="none",
    ))

    registry.register(ToolSpec(
        name="novel_wordcount",
        description=(
            "统计小说正文的可见字数（visible_chars_v1 口径：去 frontmatter 与首行标题后，"
            "不计空白字符的字符数）。传 text 直接统计这段文字；传 workdir + chapter 则读取 "
            "正文/ 下对应章节文件（第NNN章_*.md）后统计，chapter 可写 1、\"1\"、\"第001章\" "
            "或完整文件名。返回 {ok: true, visible_chars: N, chapter: ...}。"
            "【主动使用时机】交稿前核对单章字数是否达标、写完某段后估算篇幅。"
            "【不要主动使用】只想看项目整体进度时用 novel_status；不要用它统计含大量空白的"
            "排版稿并据此判断实际成稿篇幅。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "要统计的正文文本（直接给出文本时用这个）",
                },
                "workdir": {
                    "type": "string",
                    "description": "小说项目目录（含 .novel.json）；与 chapter 配合使用",
                },
                "chapter": {
                    "type": ["integer", "string"],
                    "description": "章号或章节文件名，如 1、\"1\"、\"第001章\"、\"第001章_夜行.md\"",
                },
            },
        },
        execute=_run_novel_wordcount,
        read_only=True,
        mutates_files=False,
        requires_permission=False,
        parallel_safe=True,
        activity_description="统计小说可见字数",
        side_effect_scope="none",
    ))

    registry.register(ToolSpec(
        name="novel_audit",
        description=(
            "对小说项目做确定性体检（只读，不写文件）：返回 {ok, scope, findings, summary, skipped}。"
            "五项检查各自独立，单项失败只进 skipped、不影响其余项："
            "tracking_state（_tracking-state.json 的最近提交章与 正文/ 最大章号是否一致，"
            "不一致记 S1，状态文件缺失记 S3）、"
            "foreshadow_overdue（状态为“已埋”且有计划回收章时，最近提交章超过"
            "「计划回收章 + 20」逐条记 S2；无计划回收章的长线伏笔不报）、"
            "chapter_wordcount（正文可见字数低于细纲“字数目标”的 80%，记 S3）、"
            "outline_gap（细纲存在但正文缺失，记 S3）、"
            "ai_patterns（vendor 下缺 check-ai-patterns.js 时固定进 skipped，"
            "reason=checker_missing，不冒充通过）。"
            "scope=chapter 只查指定章（chapter）或最新已写章节；scope=all 覆盖全项目（上限 500 章）。"
            "findings 每条为 {level: S1|S2|S3|S4, type, file, message}，summary 按等级计数。"
            "【主动使用时机】交稿/提交追踪前自查状态断链与伏笔超期，或用户问“项目哪里不一致”时。"
            "【不要主动使用】需要项目进度数字时用 novel_status；非小说项目返回 is_novel: false。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "workdir": {
                    "type": "string",
                    "description": "小说项目目录（含 .novel.json）",
                },
                "scope": {
                    "type": "string",
                    "enum": ["chapter", "all"],
                    "description": "chapter＝指定章或最新已写章节（默认）；all＝全项目（单次上限 500 章）",
                },
                "chapter": {
                    "type": ["integer", "string"],
                    "description": "scope=chapter 时的章号或正文文件名，如 1、\"第001章\"；省略则查最新章",
                },
                "checks": {
                    "type": "string",
                    "enum": ["auto"],
                    "description": "检查项开关，P1 只接受 auto（跑全部检查）",
                },
            },
            "required": ["workdir"],
        },
        execute=_run_novel_audit,
        read_only=True,
        mutates_files=False,
        requires_permission=False,
        parallel_safe=True,
        activity_description="审计小说项目一致性",
        side_effect_scope="none",
    ))
