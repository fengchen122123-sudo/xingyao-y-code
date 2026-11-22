from __future__ import annotations

from dataclasses import dataclass, field

import yaml

START_MARKER = "/* zcode-workflow"
END_MARKER = "*/"

ARG_TYPES = ("string", "number", "boolean", "json")

_METADATA_KEYS = frozenset({"description", "whenToUse", "args"})
_ARG_DECL_KEYS = frozenset({"type", "description", "required", "default"})


class WorkflowFrontmatterError(ValueError):
    """frontmatter 解析失败。kind 与 ZCode 的四类专名一一对应。"""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


@dataclass(frozen=True)
class ParsedWorkflow:
    """解析结果：元数据 + 逐字节脚本原文 + 正文行号偏移。"""

    description: str
    when_to_use: str = ""
    args: dict = field(default_factory=dict)
    body: str = ""
    body_line_offset: int = 0


def _invalid_metadata(message: str) -> WorkflowFrontmatterError:
    return WorkflowFrontmatterError("invalid_metadata", message)


def _normalize_args(raw_args) -> dict:
    if raw_args is None:
        return {}
    if not isinstance(raw_args, dict):
        raise _invalid_metadata("args 必须是「参数名 → 声明」的映射")
    args: dict = {}
    for name, decl in raw_args.items():
        if not isinstance(name, str) or not name.strip():
            raise _invalid_metadata("args 的参数名必须是非空字符串")
        if not isinstance(decl, dict):
            raise _invalid_metadata(f"参数 {name} 的声明必须是映射")
        unknown = set(decl) - _ARG_DECL_KEYS
        if unknown:
            raise _invalid_metadata(f"参数 {name} 含未知键: {sorted(unknown)}")
        arg_type = decl.get("type")
        if arg_type not in ARG_TYPES:
            raise _invalid_metadata(
                f"参数 {name} 的 type 必须是 {'/'.join(ARG_TYPES)} 之一， got {arg_type!r}"
            )
        normalized: dict = {"type": arg_type}
        if "description" in decl:
            if not isinstance(decl["description"], str):
                raise _invalid_metadata(f"参数 {name} 的 description 必须是字符串")
            normalized["description"] = decl["description"]
        if "required" in decl:
            if not isinstance(decl["required"], bool):
                raise _invalid_metadata(f"参数 {name} 的 required 必须是布尔值")
            normalized["required"] = decl["required"]
        if "default" in decl:
            normalized["default"] = decl["default"]
        args[name] = normalized
    return args


def parse_frontmatter(text: str) -> ParsedWorkflow:
    """解析工作流文件全文，失败抛 :class:`WorkflowFrontmatterError`。"""
    lines = text.splitlines()
    idx = 0
    while idx < len(lines) and not lines[idx].strip():
        idx += 1
    if idx >= len(lines) or lines[idx].strip() != START_MARKER:
        raise WorkflowFrontmatterError(
            "missing_frontmatter",
            f"文件必须以 {START_MARKER} 行开头（第一段非空白内容）",
        )
    yaml_lines: list[str] = []
    end_idx = None
    scan = idx + 1
    while scan < len(lines):
        if lines[scan].strip() == END_MARKER:
            end_idx = scan
            break
        yaml_lines.append(lines[scan])
        scan += 1
    if end_idx is None:
        raise WorkflowFrontmatterError(
            "unterminated_frontmatter", "frontmatter 块缺少单独一行 */ 终止符"
        )
    yaml_text = "\n".join(yaml_lines)
    try:
        meta = yaml.safe_load(yaml_text) if yaml_text.strip() else {}
    except yaml.YAMLError as exc:
        raise WorkflowFrontmatterError(
            "invalid_yaml", f"frontmatter YAML 非法: {exc}"
        ) from exc
    if meta is None:
        meta = {}
    if not isinstance(meta, dict):
        raise _invalid_metadata("frontmatter YAML 顶层必须是映射")
    unknown_keys = set(meta) - _METADATA_KEYS
    if unknown_keys:
        raise _invalid_metadata(f"未知的元数据键: {sorted(unknown_keys)}")
    description = meta.get("description")
    if not isinstance(description, str) or not description.strip():
        raise _invalid_metadata("description 必填且不能为空")
    when_to_use = meta.get("whenToUse", "")
    if when_to_use is None:
        when_to_use = ""
    if not isinstance(when_to_use, str):
        raise _invalid_metadata("whenToUse 必须是字符串")

    body = _extract_body(text, lines, end_idx)
    return ParsedWorkflow(
        description=description,
        when_to_use=when_to_use,
        args=_normalize_args(meta.get("args")),
        body=body,
        body_line_offset=end_idx + 1,
    )


def _extract_body(text: str, lines: list[str], end_idx: int) -> str:
    position = 0
    for line in lines[: end_idx + 1]:
        found = text.find(line, position)
        if found < 0:
            return ""  # 理论不可达（lines 即由 text 切出），防御性兜底
        position = found + len(line)
        if text.startswith("\r\n", position):
            position += 2
        elif text.startswith("\n", position):
            position += 1
    return text[position:]


def serialize_workflow(description: str, when_to_use: str = "", args=None, body: str = "") -> str:
    meta: dict = {"description": str(description)}
    if str(when_to_use or "").strip():
        meta["whenToUse"] = str(when_to_use)
    if args:
        ordered: dict = {}
        for arg_name, decl in args.items():
            item: dict = {"type": decl.get("type", "json")}
            if "description" in decl:
                item["description"] = decl["description"]
            if "required" in decl:
                item["required"] = decl["required"]
            if "default" in decl:
                item["default"] = decl["default"]
            ordered[arg_name] = item
        meta["args"] = ordered
    yaml_text = yaml.safe_dump(
        meta, allow_unicode=True, sort_keys=False, default_flow_style=False
    )
    script = str(body or "")
    if script and not script.endswith("\n"):
        script += "\n"
    return f"{START_MARKER}\n{yaml_text}{END_MARKER}\n{script}"
