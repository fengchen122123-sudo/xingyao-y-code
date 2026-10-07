"""一键运行的命令解析：按语言猜命令，优先读项目配置，未知语言给明确提示。

输出是「可以直接写进常驻 shell 的一行命令行」。常驻 shell 由
core/gui_terminal.py:62 resolve_shell_command 决定：Windows 上是 PowerShell，
因此统一用 PowerShell 语法（& 调用 + 单引号引用），不拼 cmd 风格字符串。

项目配置读取顺序：.vscode/launch.json 的 program（编译型语言唯一可运行入口）
→ package.json 的 scripts.start / scripts.dev → 语言默认命令。

配置读不出来或读不懂时**不失败**：回退到语言默认命令，并把原因写进 `RunPlan.note`
（中文，可直接展示），用户据此知道下一步该改哪个文件。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

# 扩展名 -> 语言 id（与 S2 language_registry 的 id 口径一致）
LANGUAGE_BY_EXTENSION: Dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".go": "go",
    ".rs": "rust",
    ".ps1": "powershell",
    ".bat": "batch",
    ".cmd": "batch",
    ".sh": "shell",
    ".c": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
}

# 明确"不可运行"的扩展名：给出比"未识别语言"更准确的提示
NOT_RUNNABLE_EXTENSIONS = {
    ".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".ini", ".csv", ".log",
    ".html", ".css", ".svg", ".png", ".jpg", ".jpeg", ".gif", ".pdf",
}

# 编译型语言：没有「直接跑源码」这回事，可运行入口是编译产物或语言自己的工具链
COMPILED_LANGUAGES = {"c", "cpp"}


@dataclass(frozen=True)
class RunPlan:
    """一条可运行的命令：command 未含末尾回车，由桥层补 \r 写进 PTY。

    note 只在「没能按项目配置解析、退回到语言默认命令」时非空：它是可直接展示给用户的
    中文说明（哪个配置文件、什么问题、退成了什么），正常解析出来时是空串。
    """

    command: str
    source: str        # "explicit" / "launch.json" / "package.json" / "language"
    language_id: str
    note: str = ""


class RunPlanError(Exception):
    """无法解析出可运行命令（携带可直接展示给用户的中文消息）。"""


def _quote(path: str) -> str:
    """PowerShell 单引号引用：路径含空格/中文都不用转义（'' 表示一个单引号）。"""
    return "'" + str(path).replace("'", "''") + "'"


def _read_json(path: str) -> Tuple[Optional[Dict[str, Any]], str]:
    """读 JSON 配置：UTF-8 strict 优先，失败回退 GBK（与 read_file_preview 口径一致）。

    返回 `(数据, 问题说明)`：问题说明为空串表示读取正常；非空时是可直接展示的中文原因，
    调用方据此回退到语言默认命令并写进 `RunPlan.note`。任何畸形内容都不抛异常。
    """
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError as e:
        return None, f"读取失败（{e.__class__.__name__}）"
    for encoding in ("utf-8-sig", "gbk"):
        try:
            text = raw.decode(encoding)
        except UnicodeDecodeError:
            continue
        try:
            data = json.loads(text)
        except ValueError:
            return None, "内容不是合法 JSON"
        if not isinstance(data, dict):
            return None, "顶层不是 JSON 对象"
        return data, ""
    return None, "编码既不是 UTF-8 也不是 GBK，无法读取"


def _nearest_config(name: str, start_dir: str, workspace: str) -> str:
    """从文件目录向上找最近的配置文件，最远到工作区根（不在工作区则只找文件目录）。"""
    current = os.path.abspath(start_dir)
    stop = os.path.abspath(workspace) if workspace and workspace != "GLOBAL" else current
    while True:
        candidate = os.path.join(current, name)
        if os.path.isfile(candidate):
            return candidate
        parent = os.path.dirname(current)
        if current == stop or parent == current:
            return ""
        current = parent


def _match_launch_config(path: str, configs: List[Any]) -> Optional[Dict[str, Any]]:
    """严格匹配：program 与当前文件同路径，或 basename 同名（launch.json 常写相对路径）。"""
    base = os.path.normcase(os.path.basename(path))
    target = os.path.normcase(os.path.abspath(path))
    for item in configs:
        if not isinstance(item, dict):
            continue
        program = str(item.get("program") or "")
        if not program:
            continue
        if os.path.isabs(program) and os.path.normcase(os.path.abspath(program)) == target:
            return item
        if os.path.normcase(os.path.basename(program)) == base:
            return item
    return None


def _build_config_command(item: Dict[str, Any]) -> str:
    """把一条 configuration 变成命令行：runtimeExecutable（可选）+ program + args。"""
    program = str(item.get("program") or "")
    runtime = str(item.get("runtimeExecutable") or "")
    raw_args = item.get("args")
    extra = [str(arg) for arg in raw_args] if isinstance(raw_args, (list, tuple)) else []
    return " ".join(
        [*(_quote(runtime) if runtime else "",), _quote(program), *[_quote(arg) for arg in extra]]
    ).strip()


def _launch_json_plan(
    path: str, workspace: str, language_id: str, notes: List[str]
) -> Optional[RunPlan]:
    """.vscode/launch.json 里匹配当前文件的 configuration -> 运行计划。

    读不懂就返回 None 并在 notes 里留下原因（调用方会退回到语言默认命令）。
    """
    config_path = _nearest_config(
        os.path.join(".vscode", "launch.json"), os.path.dirname(path), workspace
    )
    if not config_path:
        return None
    data, problem = _read_json(config_path)
    if data is None:
        notes.append(f"`.vscode/launch.json` {problem or '无法读取'}，已改用语言默认命令运行")
        return None
    configs = data.get("configurations")
    if not isinstance(configs, list):
        notes.append(
            "`.vscode/launch.json` 里的 configurations 字段缺失或不是数组，已改用语言默认命令运行"
        )
        return None
    matched = _match_launch_config(path, configs)
    if matched is None and language_id in COMPILED_LANGUAGES:
        # 编译型语言的可运行入口是编译产物（build/app.exe），文件名天然与源码不同名：
        # 没有严格匹配时取第一个配了 program 的 configuration（用户机器上通常只有一条）。
        matched = next(
            (
                item for item in configs
                if isinstance(item, dict) and str(item.get("program") or "")
            ),
            None,
        )
        if matched is not None:
            notes.append(
                f"`.vscode/launch.json` 里没有与 {os.path.basename(path)} 同名的 program，"
                f"已改用第一个配置「{str(matched.get('name') or '未命名')}」里的 program"
            )
    if matched is None:
        notes.append(
            "`.vscode/launch.json` 里没有配置匹配当前文件的 program，已改用语言默认命令运行"
        )
        return None
    return RunPlan(
        command=_build_config_command(matched),
        source="launch.json",
        language_id=language_id,
    )


def _package_json_script(path: str, workspace: str, notes: List[str]) -> str:
    """package.json 的 scripts.start / scripts.dev（找不到返回空串并说明原因）。"""
    config_path = _nearest_config("package.json", os.path.dirname(path), workspace)
    if not config_path:
        return ""
    data, problem = _read_json(config_path)
    if data is None:
        notes.append(f"`package.json` {problem or '无法读取'}，已改用语言默认命令运行")
        return ""
    scripts = data.get("scripts")
    if not isinstance(scripts, dict):
        notes.append("`package.json` 里缺少 scripts 字段（或它不是对象），已改用语言默认命令运行")
        return ""
    for name in ("start", "dev"):
        value = scripts.get(name)
        if isinstance(value, str) and value.strip():
            return name
    notes.append(
        "`package.json` 里没有可用的 scripts.start / scripts.dev，已改用语言默认命令运行"
    )
    return ""


def build_run_plan(
    path: str,
    workspace: str,
    explicit_command: str = "",
    extra_args: str = "",
) -> RunPlan:
    """解析运行计划；解析不出来时抛 RunPlanError（不返回"半截命令"）。"""
    explicit = str(explicit_command or "").strip()
    if explicit:
        return RunPlan(command=_append_args(explicit, extra_args), source="explicit", language_id="")

    real = os.path.abspath(str(path or ""))
    if not os.path.isfile(real):
        raise RunPlanError("文件不存在或不是普通文件，无法运行")

    extension = os.path.splitext(real)[1].lower()
    if extension in NOT_RUNNABLE_EXTENSIONS:
        raise RunPlanError(f"该文件类型不可运行：{extension or '无扩展名'}")
    language_id = LANGUAGE_BY_EXTENSION.get(extension, "")
    if not language_id:
        raise RunPlanError(
            f"未识别的语言：{extension or '无扩展名'}；请在底部终端手动运行，"
            "或在 .vscode/launch.json 里为本文件配置 program"
        )

    notes: List[str] = []
    launch_plan = _launch_json_plan(real, workspace, language_id, notes)
    if launch_plan is not None:
        return RunPlan(
            command=_append_args(launch_plan.command, extra_args),
            source="launch.json",
            language_id=language_id,
            note="；".join(notes),
        )

    if language_id in ("javascript", "typescript"):
        script = _package_json_script(real, workspace, notes)
        if script:
            return RunPlan(
                command=_append_args(f"& npm run {script}", extra_args),
                source="package.json",
                language_id=language_id,
                note="；".join(notes),
            )

    note = "；".join(notes)
    if language_id == "python":
        interpreter = sys.executable or "python"
        return RunPlan(command=_append_args(f"& {_quote(interpreter)} -u {_quote(real)}", extra_args), source="language", language_id=language_id, note=note)
    if language_id == "javascript":
        node = _require_executable("node", "Node.js")
        return RunPlan(command=_append_args(f"& {_quote(node)} {_quote(real)}", extra_args), source="language", language_id=language_id, note=note)
    if language_id == "typescript":
        raise RunPlanError(
            "TypeScript 需要 package.json 的 scripts.start / scripts.dev 才能在终端运行；"
            "请先补齐 package.json，或在底部终端手动运行"
        )
    if language_id == "go":
        go = _require_executable("go", "Go")
        return RunPlan(command=_append_args(f"& {_quote(go)} run {_quote(real)}", extra_args), source="language", language_id=language_id, note=note)
    if language_id == "rust":
        cargo = _require_executable("cargo", "Rust")
        # 用 Cargo.toml 所在目录作为 cargo 的运行目录不可行（终端 cwd 不能逐条改），
        # 因此显式指定 manifest-path：文件向上最近的 Cargo.toml
        manifest = _nearest_config("Cargo.toml", os.path.dirname(real), workspace)
        if not manifest:
            raise RunPlanError("未找到 Cargo.toml，无法用 cargo run 运行；请在终端手动运行")
        return RunPlan(command=_append_args(f"& {_quote(cargo)} run --manifest-path {_quote(manifest)}", extra_args), source="language", language_id=language_id, note=note)
    if language_id in ("powershell", "batch"):
        return RunPlan(command=_append_args(f"& {_quote(real)}", extra_args), source="language", language_id=language_id, note=note)
    if language_id == "shell":
        bash = _require_executable("bash", "bash")
        return RunPlan(command=_append_args(f"& {_quote(bash)} {_quote(real)}", extra_args), source="language", language_id=language_id, note=note)

    raise RunPlanError(
        f"{extension} 是编译型语言，需要先编译出可执行文件，"
        "并在 .vscode/launch.json 里配置 program（或手动在终端运行）"
    )


def _append_args(command: str, extra_args: str) -> str:
    extra = str(extra_args or "").strip()
    return f"{command} {extra}" if extra else command


def _require_executable(name: str, display: str) -> str:
    path = shutil.which(name)
    if not path:
        raise RunPlanError(f"未找到 {display}（{name}），请先安装并确保它在 PATH 中")
    return path