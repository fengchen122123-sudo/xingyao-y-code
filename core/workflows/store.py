"""星瑶工作流存取：目录布局、同名遮蔽、命名校验与 ZCode 导入（设计 §3.1）。

目录布局（与 ZCode 的作用域语义一致，只是根从 ``.zcode`` 换成 ``.ycode``）：

::

    ~/.ycode/workflows/          全局档（本机所有项目）
    <project>/.ycode/workflows/  项目档（同名遮蔽全局档，first-wins）

名字即文件名（``<name>.dwf.ts``），命名正则同时是路径穿越防线
（显式拒绝 ``.`` / ``..``）。文件逐字节原样保留——星瑶不执行 TS，
脚本文本是转译执行与将来对照升级的基准。
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
from pathlib import Path

from core.workflows.frontmatter import (
    WorkflowFrontmatterError,
    parse_frontmatter,
)
from core.workflows.plan import validate_plan

logger = logging.getLogger("ycode.workflows")

WORKFLOW_SUFFIX = ".dwf.ts"

#: 与 ZCode 同款命名约束；这条正则就是路径穿越防线。
NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")

_SCOPES = ("project", "user")


def user_data_dir() -> Path:
    """用户数据根：YCODE_DATA_DIR 优先，否则 ~/.ycode。"""
    env_dir = os.environ.get("YCODE_DATA_DIR")
    if env_dir and str(env_dir).strip():
        return Path(env_dir)
    return Path.home() / ".ycode"


def workflows_dir(scope: str, workspace_root=None, *, home=None) -> Path:
    """星瑶侧工作流目录（不主动创建，列出/导入时按需建）。"""
    if scope == "project":
        return Path(workspace_root or os.getcwd()) / ".ycode" / "workflows"
    if home is not None:
        return Path(home) / ".ycode" / "workflows"
    return user_data_dir() / "workflows"


def zcode_workflows_dir(scope: str, workspace_root=None, *, home=None) -> Path:
    """ZCode 侧来源目录。"""
    if scope == "project":
        return Path(workspace_root or os.getcwd()) / ".zcode" / "workflows"
    base = Path(home) if home is not None else Path.home()
    return base / ".zcode" / "workflows"


def validate_name(name: str) -> str:
    """名字合法返回空串，否则返回错误文案。"""
    if name in (".", ".."):
        return "工作流名字不能是 . 或 .."
    if not NAME_PATTERN.match(name or ""):
        return "工作流名字只允许字母/数字/点/横线/下划线，长度 1..64"
    return ""


def _read_entry(path: Path, name: str, scope: str) -> tuple[dict | None, dict | None]:
    """读单个工作流文件 → (正常条目, invalid 条目)。二者互斥。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, {"name": name, "scope": scope, "path": str(path), "error": f"读取失败: {exc}"}
    try:
        parsed = parse_frontmatter(text)
    except WorkflowFrontmatterError as exc:
        return None, {"name": name, "scope": scope, "path": str(path), "error": f"{exc.kind}: {exc.message}"}
    return (
        {
            "name": name,
            "scope": scope,
            "path": str(path),
            "description": parsed.description,
            "when_to_use": parsed.when_to_use,
            "args": parsed.args,
            "body": parsed.body,
        },
        None,
    )


def list_workflows(workspace_root=None, *, home=None) -> dict:
    """列出全部工作流（项目档遮蔽全局档）与坏文件清单。

    被遮蔽的全局档不进列表——列表要说的是「调用这个名字会跑到什么」。
    坏文件是用户手改的一等公民：逐个指名，绝不让一个坏文件炸掉清单。
    """
    workflows: list[dict] = []
    invalid: list[dict] = []
    seen: set[str] = set()
    for scope in _SCOPES:
        directory = workflows_dir(scope, workspace_root, home=home)
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob(f"*{WORKFLOW_SUFFIX}")):
            name = path.name[: -len(WORKFLOW_SUFFIX)]
            name_error = validate_name(name)
            if name_error:
                invalid.append({"name": path.name, "scope": scope, "path": str(path), "error": name_error})
                continue
            if name in seen:
                continue
            entry, bad = _read_entry(path, name, scope)
            if bad is not None:
                invalid.append(bad)
                continue
            seen.add(name)
            workflows.append({key: entry[key] for key in ("name", "scope", "path", "description", "when_to_use", "args")})
    return {"workflows": workflows, "invalid": invalid}


def get_workflow(name: str, workspace_root=None, *, home=None) -> dict:
    """按名取工作流（项目档优先）；plan 边车存在且合法时附带 plan 字段。"""
    name_error = validate_name(name)
    if name_error:
        raise LookupError(name_error)
    for scope in _SCOPES:
        directory = workflows_dir(scope, workspace_root, home=home)
        path = directory / f"{name}{WORKFLOW_SUFFIX}"
        if not path.is_file():
            continue
        entry, bad = _read_entry(path, name, scope)
        if bad is not None:
            raise LookupError(f"工作流 {name} 文件损坏: {bad['error']}")
        entry["plan"] = _read_plan_sidecar(directory, name)
        return entry
    raise LookupError(f"找不到工作流 {name!r}（项目档与全局档均不存在）")


def import_from_zcode(
    workspace_root=None,
    *,
    home=None,
    scopes=("project", "user"),
    overwrite: bool = False,
    generate_skills: bool = True,
) -> dict:
    """从兼容目录逐字节复制工作流文件到星瑶目录，并生成同名技能。

    返回报告：imported / skipped（已存在未覆盖 / 名字非法 / 解析失败）/
    各自点名。解析失败的文件**仍然复制**（原文保留供修复），但技能不生成、
    列表里以 invalid 呈现。
    """
    report = {"imported": [], "skipped": [], "invalid": []}
    for scope in scopes:
        source_dir = zcode_workflows_dir(scope, workspace_root, home=home)
        if not source_dir.is_dir():
            continue
        target_dir = workflows_dir(scope, workspace_root, home=home)
        for source in sorted(source_dir.glob(f"*{WORKFLOW_SUFFIX}")):
            name = source.name[: -len(WORKFLOW_SUFFIX)]
            name_error = validate_name(name)
            if name_error:
                report["invalid"].append({"name": source.name, "scope": scope, "error": name_error})
                continue
            target = target_dir / source.name
            if target.exists() and not overwrite:
                report["skipped"].append(
                    {"name": name, "scope": scope, "reason": "目标已存在（未加 overwrite 跳过）"}
                )
                continue
            parsed: object = None
            parse_error = ""
            try:
                parsed = parse_frontmatter(source.read_text(encoding="utf-8"))
            except WorkflowFrontmatterError as exc:
                parse_error = f"{exc.kind}: {exc.message}"
            except OSError as exc:
                parse_error = f"读取失败: {exc}"
            try:
                target_dir.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)  # 逐字节复制，不改一个字符
            except OSError as exc:
                report["invalid"].append({"name": name, "scope": scope, "error": f"复制失败: {exc}"})
                continue
            entry = {"name": name, "scope": scope, "path": str(target)}
            if parse_error:
                entry["frontmatter_error"] = parse_error
                report["invalid"].append(entry)
                continue
            if generate_skills and parsed is not None:
                skill_path = _sync_skill_quietly(name, parsed, home=home)
                if skill_path:
                    entry["skill"] = skill_path
            report["imported"].append(entry)
    return report


def _sync_skill_quietly(name: str, parsed, *, home=None) -> str:
    """生成技能；失败只记日志不阻断导入（技能是通道二，不是主路径）。"""
    try:
        from core.workflows.skill_sync import generate_skill

        return generate_skill(name, parsed, home=home)
    except Exception:
        logger.warning("工作流 %s 的技能生成失败（导入不受影响）", name, exc_info=True)
        return ""


def _plan_sidecar_path(directory: Path, name: str) -> Path:
    return directory / f"{name}.dwf.plan.json"


def _read_plan_sidecar(directory: Path, name: str):
    """读 plan 边车；存在且合法返回 dict，不存在/非法返回 None（回退转译执行）。"""
    sidecar = _plan_sidecar_path(directory, name)
    if not sidecar.is_file():
        return None
    try:
        plan = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return plan if isinstance(plan, dict) and not validate_plan(plan) else None


def save_workflow(
    name: str,
    *,
    description: str,
    when_to_use: str = "",
    args=None,
    body: str = "",
    scope: str = "project",
    workspace_root=None,
    home=None,
    overwrite: bool = False,
    plan=None,
) -> str:
    """新建 / 更新一个工作流文件（序列化 + 回读校验 + 技能同步）。

    ``plan`` 非空时为确定性解释执行的可执行步骤图：先严格校验，通过后作为
    ``<name>.dwf.plan.json`` 边车与脚本并存；校验失败整次保存拒绝（返回全部
    错误）。plan 为空且同名旧边车存在 → 删除旧边车（脚本更新后 plan 可能失配）。

    返回空串 = 成功；否则为可直接展示的错误文案。同名已存在且未传
    overwrite 时拒绝（调用方先与用户确认再覆盖）。
    """
    from core.workflows.frontmatter import serialize_workflow
    from core.workflows.plan import validate_plan

    name_error = validate_name(name)
    if name_error:
        return name_error
    if not str(description or "").strip():
        return "description 不能为空"
    if not str(body or "").strip():
        return "脚本正文不能为空"
    if scope not in _SCOPES:
        return f"scope 必须是 {'/'.join(_SCOPES)} 之一"
    plan_errors = validate_plan(plan) if plan is not None else []
    if plan_errors:
        return "plan 校验未通过：" + "；".join(plan_errors)
    text = serialize_workflow(description, when_to_use, args or {}, body)
    try:
        parsed = parse_frontmatter(text)
    except WorkflowFrontmatterError as exc:
        return f"内部序列化结果不可解析（{exc.kind}: {exc.message}）"
    if parsed.body != (body if body.endswith("\n") or not body else body + "\n"):
        return "内部校验失败：脚本正文往返不一致"
    target = workflows_dir(scope, workspace_root, home=home) / f"{name}{WORKFLOW_SUFFIX}"
    if target.exists() and not overwrite:
        return f"已存在同名工作流「{name}」（{scope} 档）；确认覆盖请显式允许"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="\n")
        sidecar = _plan_sidecar_path(target.parent, name)
        if plan is not None:
            sidecar.write_text(
                json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        elif sidecar.exists():
            sidecar.unlink()  # 脚本已更新且未带新 plan：旧计划失配，宁可回退转译
    except OSError as exc:
        return f"写入失败: {exc}"
    _sync_skill_quietly(name, parsed, home=home)
    return ""


def delete_workflow(name: str, scope: str, workspace_root=None, *, home=None) -> str:
    """删除指定档位的工作流文件，并同步移除生成的同名技能。"""
    from core.workflows.skill_sync import skill_dir

    name_error = validate_name(name)
    if name_error:
        return name_error
    if scope not in _SCOPES:
        return f"scope 必须是 {'/'.join(_SCOPES)} 之一"
    target = workflows_dir(scope, workspace_root, home=home) / f"{name}{WORKFLOW_SUFFIX}"
    if not target.exists():
        return f"{scope} 档没有工作流「{name}」"
    try:
        target.unlink()
        sidecar = _plan_sidecar_path(target.parent, name)
        if sidecar.exists():
            sidecar.unlink()
    except OSError as exc:
        return f"删除失败: {exc}"
    skill = skill_dir(name, home=home)
    try:
        if skill.is_dir():
            shutil.rmtree(skill)
    except OSError:
        logger.warning("工作流 %s 的技能目录清理失败（不影响删除）", name, exc_info=True)
    return ""


def import_workflow_file(
    source_path,
    *,
    scope: str,
    workspace_root=None,
    home=None,
    overwrite: bool = False,
) -> dict:
    """导入用户选择的工作流文件到指定档位。

    与批量目录扫描不同：显式导入要求 frontmatter 可解析（坏文件不落盘、
    直接报错），复制仍逐字节。返回 ``{"ok", "name", "path"}`` 或
    ``{"ok": False, "error"}``。
    """
    source = Path(str(source_path))
    if scope not in _SCOPES:
        return {"ok": False, "error": f"scope 必须是 {'/'.join(_SCOPES)} 之一"}
    if not source.name.endswith(WORKFLOW_SUFFIX):
        return {"ok": False, "error": f"仅支持 {WORKFLOW_SUFFIX} 工作流文件: {source.name}"}
    if not source.is_file():
        return {"ok": False, "error": f"文件不存在: {source}"}
    name = source.name[: -len(WORKFLOW_SUFFIX)]
    name_error = validate_name(name)
    if name_error:
        return {"ok": False, "error": f"{source.name}: {name_error}"}
    try:
        text = source.read_text(encoding="utf-8")
        parsed = parse_frontmatter(text)
    except WorkflowFrontmatterError as exc:
        return {"ok": False, "error": f"{source.name}: {exc.kind}: {exc.message}"}
    except OSError as exc:
        return {"ok": False, "error": f"{source.name}: 读取失败: {exc}"}
    target = workflows_dir(scope, workspace_root, home=home) / source.name
    if target.exists() and not overwrite:
        return {"ok": False, "error": f"已存在同名工作流「{name}」", "exists": True}
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)  # 逐字节复制，不改一个字符
    except OSError as exc:
        return {"ok": False, "error": f"{source.name}: 复制失败: {exc}"}
    _sync_skill_quietly(name, parsed, home=home)
    return {"ok": True, "name": name, "path": str(target)}
