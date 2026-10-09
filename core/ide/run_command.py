"""一键运行的命令解析：按语言猜命令，优先读项目配置，未知语言给明确提示。

输出是「可以直接写进常驻 shell 的一行命令行」。常驻 shell 由
core/gui_terminal.py:62 resolve_shell_command 决定：Windows 上是 PowerShell，
因此统一用 PowerShell 语法（& 调用 + 单引号引用），不拼 cmd 风格字符串。

项目配置读取顺序：.vscode/launch.json 的 program（编译型语言唯一可运行入口）
→ package.json 的 scripts.start / scripts.dev → 语言默认命令。

launch.json 里的 program / runtimeExecutable / args 会先展开 VS Code 变量再使用：
VS Code 生成的默认配置几乎都写 `${workspaceFolder}/...`，而命令最终进入 PowerShell，
单引号内的 `${...}` 不会展开——不展开就等于把字面量 `${workspaceFolder}/app.py`
当路径用，必然"文件不存在"。无法识别的变量（如 `${env:PATH}`）不原样透传，而是
给出中文原因并回退到语言默认命令。

配置读不出来或读不懂时**不失败**：回退到语言默认命令，并把原因写进 `RunPlan.note`
（中文，可直接展示），用户据此知道下一步该改哪个文件。
"""

from __future__ import annotations

import json
import os
import re
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


# launch.json 里支持的 VS Code 变量（以工作区与"当前文件"为基准展开）。
# 前三个是 VS Code 默认生成的配置最常用的写法；file 系列让
# "${workspaceFolder}/build/${fileBasenameNoExtension}.exe" 这类 C/C++ 默认
# 模板也能解析出真实路径。
_LAUNCH_VARIABLE_NAMES = (
    "workspaceFolder",
    "workspaceFolderBasename",
    "file",
    "fileBasename",
    "fileBasenameNoExtension",
    "fileDirname",
    "fileExtname",
)

_VARIABLE_PATTERN = re.compile(r"\$\{([^}]*)\}")


def _expand_launch_variables(text: str, path: str, workspace: str) -> Tuple[str, str]:
    """展开 launch.json 字段里的 VS Code 变量。

    返回 `(展开结果, 问题说明)`：问题说明为空串表示全部变量都认得；非空时是可直接
    展示的中文原因（列出了认不出来的变量名），调用方必须放弃这条配置而不是把
    `${...}` 透传进终端——PowerShell 单引号里不会展开变量，透传的结果是"路径不存在"。

    `${workspaceFolder}` 以会话工作区为基准；GLOBAL 自由对话没有工作区概念，退化为
    当前文件所在目录（用户至少能跑到自己点开的那个文件）。
    """
    raw = str(text or "")
    if "${" not in raw:
        return raw, ""
    real_file = os.path.abspath(str(path or ""))
    base = str(workspace or "")
    if not base or base == "GLOBAL" or not os.path.isdir(base):
        base = os.path.dirname(real_file)
    workspace_dir = os.path.abspath(base)
    basename = os.path.basename(real_file)
    stem, extension = os.path.splitext(basename)
    values = {
        "workspaceFolder": workspace_dir,
        "workspaceFolderBasename": os.path.basename(workspace_dir),
        "file": real_file,
        "fileBasename": basename,
        "fileBasenameNoExtension": stem,
        "fileDirname": os.path.dirname(real_file),
        "fileExtname": extension,
    }
    unknown: List[str] = []

    def _replace(match: "re.Match[str]") -> str:
        name = match.group(1).strip()
        if name in values:
            return values[name]
        unknown.append("${" + name + "}" if name else "空变量 `${}`")
        return match.group(0)

    expanded = _VARIABLE_PATTERN.sub(_replace, raw)
    if unknown:
        supported = "、".join("${" + name + "}" for name in _LAUNCH_VARIABLE_NAMES)
        return expanded, f"变量 {'、'.join(sorted(set(unknown)))} 无法识别（只支持 {supported}）"
    return expanded, ""


def _expand_launch_config(
    item: Dict[str, Any], path: str, workspace: str
) -> Tuple[Optional[Dict[str, Any]], str, str]:
    """展开一条 configuration 的 program / runtimeExecutable / args。

    返回 `(展开后的字段, 问题说明, 部分展开的 program)`；问题说明非空时第一个元素为
    None，表示这条配置不可用（含无法识别的变量），调用方据此回退并展示中文原因；
    第三个元素用于"这条配置本来匹配当前文件、只是变量解析不了"的精确诊断。
    """
    raw_args = item.get("args")
    args_raw = [str(arg) for arg in raw_args] if isinstance(raw_args, (list, tuple)) else []
    problems: List[str] = []

    def _expand(value: str) -> str:
        expanded, problem = _expand_launch_variables(value, path, workspace)
        if problem:
            problems.append(problem)
        return expanded

    program = _expand(str(item.get("program") or ""))
    runtime = _expand(str(item.get("runtimeExecutable") or ""))
    args = [_expand(arg) for arg in args_raw]
    # program / runtimeExecutable 是路径：${workspaceFolder}/build/app.exe 展开后
    # 混着正斜杠，normpath 统一成 Windows 分隔符（args 是参数，不能动）。
    program_path = os.path.normpath(program)
    if problems:
        return None, "；".join(sorted(set(problems))), program_path
    return {
        "program": program_path,
        "runtimeExecutable": os.path.normpath(runtime) if runtime else "",
        "args": args,
    }, "", program_path


def _program_matches(path: str, program: str) -> bool:
    """展开后的 program 是否指向当前文件：绝对路径同一，或 basename 同名。

    必须先展开再比对：VS Code 会把 program 写成 `${file}` / `${fileBasename}`，
    字面量 basename 永远与文件名不同名，展开后比对才能匹配上。
    """
    if not program:
        return False
    if os.path.isabs(program) and os.path.normcase(os.path.abspath(program)) == os.path.normcase(
        os.path.abspath(path)
    ):
        return True
    return os.path.normcase(os.path.basename(program)) == os.path.normcase(os.path.basename(path))


def _build_config_command(values: Dict[str, Any]) -> str:
    """把展开好的一条 configuration 变成命令行：runtimeExecutable（可选）+ program + args。

    统一 PowerShell 语法：`& ` 显式调用 + 单引号引用。**不能省略 `& `**：PowerShell
    把「以引号字符串开头的语句」当字符串表达式求值并回显，用户点运行后终端只会打印
    一行路径、程序不会启动（launch.json 是 C/C++ 唯一可运行入口，必现）。
    """
    program = str(values.get("program") or "")
    runtime = str(values.get("runtimeExecutable") or "")
    args = [str(arg) for arg in (values.get("args") or [])]
    return " ".join(
        ["&", *([_quote(runtime)] if runtime else []), _quote(program), *[_quote(arg) for arg in args]]
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
    # 先展开所有「配了 program」的 configuration：匹配与命令拼装都基于展开后的值
    entries: List[Tuple[Dict[str, Any], Optional[Dict[str, Any]], str, str]] = []
    for item in configs:
        if not isinstance(item, dict) or not str(item.get("program") or ""):
            continue
        values, problem, partial = _expand_launch_config(item, path, workspace)
        entries.append((item, values, problem, partial))
    matched = next(
        (
            entry for entry in entries
            if not entry[2] and _program_matches(path, (entry[1] or {}).get("program") or "")
        ),
        None,
    )
    if matched is None and language_id in COMPILED_LANGUAGES:
        # 编译型语言的可运行入口是编译产物（build/app.exe），文件名天然与源码不同名：
        # 没有严格匹配时取第一个配了 program 的 configuration（用户机器上通常只有一条）。
        matched = entries[0] if entries else None
        if matched is not None:
            notes.append(
                f"`.vscode/launch.json` 里没有与 {os.path.basename(path)} 同名的 program，"
                f"已改用第一个配置「{str(matched[0].get('name') or '未命名')}」里的 program"
            )
    if matched is None:
        # 有一条配置本来就指的是当前文件、只是变量解析不了：点名这条配置，用户才知道改哪个
        broken = next(
            (entry for entry in entries if entry[2] and _program_matches(path, entry[3])), None
        )
        if broken is not None:
            notes.append(
                f"`.vscode/launch.json` 配置「{str(broken[0].get('name') or '未命名')}」的 program "
                f"无法解析：{broken[2]}，已改用语言默认命令运行"
            )
            return None
        problems = sorted({entry[2] for entry in entries if entry[2]})
        if problems:
            notes.append(
                f"`.vscode/launch.json` 的 program 无法解析：{'；'.join(problems)}，"
                "已改用语言默认命令运行"
            )
        else:
            notes.append(
                "`.vscode/launch.json` 里没有配置匹配当前文件的 program，已改用语言默认命令运行"
            )
        return None
    item, values, problem, _partial = matched
    if problem or not values:
        notes.append(
            f"`.vscode/launch.json` 配置「{str(item.get('name') or '未命名')}」的 program 无法解析："
            f"{problem or 'program 展开后为空'}，已改用语言默认命令运行"
        )
        return None
    return RunPlan(
        command=_build_config_command(values),
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

    hint = f"（{'；'.join(notes)}）" if notes else ""
    raise RunPlanError(
        f"{extension} 是编译型语言，需要先编译出可执行文件，"
        f"并在 .vscode/launch.json 里配置 program（或手动在终端运行）{hint}"
    )


def _append_args(command: str, extra_args: str) -> str:
    extra = str(extra_args or "").strip()
    return f"{command} {extra}" if extra else command


def _require_executable(name: str, display: str) -> str:
    path = shutil.which(name)
    if not path:
        raise RunPlanError(f"未找到 {display}（{name}），请先安装并确保它在 PATH 中")
    return path