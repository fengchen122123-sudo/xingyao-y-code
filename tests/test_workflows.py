"""星瑶工作流模块测试：frontmatter 解析 / args 校验 / store 存取 / 技能生成。

全部用 tmp 目录构造（store 的函数接受 ``home=`` 参数，绝不触碰真实用户目录）。
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from core.workflows.args import validate_workflow_args
from core.workflows.frontmatter import (
    WorkflowFrontmatterError,
    parse_frontmatter,
)
from core.workflows.skill_sync import generate_skill
from core.workflows.store import (
    delete_workflow,
    get_workflow,
    import_from_zcode,
    import_workflow_file,
    list_workflows,
    save_workflow,
    validate_name,
    workflows_dir,
    zcode_workflows_dir,
)

_SAMPLE = """/* zcode-workflow
description: Review changed files and report findings.
whenToUse: Use when the user asks to review changes.
args:
  base:
    type: string
    description: Git ref to diff against.
    required: true
  maxRounds:
    type: number
    default: 3
*/
phase("Collect");
return 1;
"""


# ── frontmatter ────────────────────────────────────────────────


def test_parse_ok_with_args_and_body_offset():
    parsed = parse_frontmatter(_SAMPLE)
    assert parsed.description == "Review changed files and report findings."
    assert parsed.when_to_use == "Use when the user asks to review changes."
    assert parsed.args["base"] == {
        "type": "string",
        "description": "Git ref to diff against.",
        "required": True,
    }
    assert parsed.args["maxRounds"] == {"type": "number", "default": 3}
    # 脚本逐字节跟在 */ 行之后（含尾换行；终止行自己的 \n 已被吃掉）
    assert parsed.body == 'phase("Collect");\nreturn 1;\n'
    # 正文第一行 = 文件第 13 行（1-based）→ offset = 12
    assert parsed.body_line_offset == 12


def test_parse_body_preserves_crlf_and_leading_blank_lines():
    text = "\n\n/* zcode-workflow\ndescription: d\r\n*/\r\nbody;line\r\n"
    parsed = parse_frontmatter(text)
    assert parsed.body == "body;line\r\n"


@pytest.mark.parametrize(
    "text,kind",
    [
        ("description: d\n*/\nbody", "missing_frontmatter"),
        ("/* zcode-workflow\ndescription: d\nbody", "unterminated_frontmatter"),
        ("/* zcode-workflow\nfoo: [unclosed\ndescription: d\n*/\n", "invalid_yaml"),
        ("/* zcode-workflow\nwhenToUse: x\n*/\n", "invalid_metadata"),
        ("/* zcode-workflow\ndescription: d\nunknownKey: 1\n*/\n", "invalid_metadata"),
        ("/* zcode-workflow\ndescription: d\nargs:\n  a:\n    type: blob\n*/\n", "invalid_metadata"),
        ("/* zcode-workflow\ndescription: d\nargs:\n  a:\n    type: string\n    bogus: 1\n*/\n", "invalid_metadata"),
        ("just a typescript file", "missing_frontmatter"),
    ],
)
def test_parse_errors_carry_zcode_kind_names(text, kind):
    with pytest.raises(WorkflowFrontmatterError) as exc_info:
        parse_frontmatter(text)
    assert exc_info.value.kind == kind


# ── args 校验（ZCode 语义）────────────────────────────────────


def test_args_unknown_parameter_rejected():
    declarations = {"base": {"type": "string", "required": True}}
    errors, _ = validate_workflow_args(declarations, {"base": "main", "extra": 1})
    assert any("未声明" in error for error in errors)


def test_args_collects_all_errors_at_once():
    declarations = {
        "base": {"type": "string", "required": True},
        "n": {"type": "number"},
        "flag": {"type": "boolean"},
    }
    errors, effective = validate_workflow_args(
        declarations, {"n": "not-a-number", "flag": 0, "oops": True}
    )
    assert len(errors) == 4  # 缺 base + n 类型 + flag 类型 + 未声明 oops
    assert effective == {}


def test_args_default_filled_and_validated_same_path():
    declarations = {"maxRounds": {"type": "number", "default": 3}}
    errors, effective = validate_workflow_args(declarations, {})
    assert errors == []
    assert effective == {"maxRounds": 3}
    # default 本身类型错 → 生效那一刻报错（保存时不判别）
    bad = {"maxRounds": {"type": "number", "default": "three"}}
    errors, _ = validate_workflow_args(bad, {})
    assert errors


def test_args_type_rules_number_boolean_json():
    declarations = {
        "n": {"type": "number"},
        "flag": {"type": "boolean"},
        "payload": {"type": "json"},
    }
    errors, effective = validate_workflow_args(
        declarations,
        {"n": math.nan, "flag": True, "payload": {"any": [1, None]}},
    )
    assert len(errors) == 1 and "NaN" in errors[0]
    assert effective == {"flag": True, "payload": {"any": [1, None]}}
    errors, _ = validate_workflow_args(declarations, {"flag": 1})
    assert errors  # boolean 拒 0/1


def test_args_optional_missing_not_injected():
    declarations = {"opt": {"type": "string"}}
    errors, effective = validate_workflow_args(declarations, {})
    assert errors == [] and effective == {}


# ── store：目录布局 / 遮蔽 / 命名 ─────────────────────────────


def _write_workflow(directory: Path, name: str, text: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.dwf.ts").write_text(text, encoding="utf-8")


def test_validate_name_rejects_traversal_and_bad_chars():
    assert validate_name("pr-review") == ""
    assert validate_name(".") != ""
    assert validate_name("..") != ""
    assert validate_name("../escape") != ""
    assert validate_name("a" * 65) != ""
    assert validate_name("中文") != ""


def test_list_project_shadows_user_and_invalid_named(tmp_path):
    home, root = tmp_path / "home", tmp_path / "proj"
    _write_workflow(workflows_dir("user", str(root), home=home), "shared", "/* zcode-workflow\ndescription: global\n*/\nglobal-body")
    _write_workflow(workflows_dir("project", str(root), home=home), "shared", "/* zcode-workflow\ndescription: project\n*/\nproject-body")
    _write_workflow(workflows_dir("user", str(root), home=home), "broken", "no frontmatter here")

    data = list_workflows(str(root), home=home)
    names = [item["name"] for item in data["workflows"]]
    assert names == ["shared"]
    assert data["workflows"][0]["scope"] == "project"
    assert data["workflows"][0]["description"] == "project"
    assert [item["name"] for item in data["invalid"]] == ["broken"]

    entry = get_workflow("shared", str(root), home=home)
    assert entry["body"] == "project-body"
    with pytest.raises(LookupError):
        get_workflow("missing", str(root), home=home)


def test_import_from_zcode_copies_bytes_and_generates_skill(tmp_path):
    home, root = tmp_path / "home", tmp_path / "proj"
    source_dir = zcode_workflows_dir("user", str(root), home=home)
    original = _SAMPLE
    _write_workflow(source_dir, "pr-review", original)
    _write_workflow(source_dir, "broken", "not a workflow")

    report = import_from_zcode(str(root), home=home, scopes=("user",))
    assert [item["name"] for item in report["imported"]] == ["pr-review"]
    assert report["imported"][0]["skill"].endswith("SKILL.md")
    assert [item["name"] for item in report["invalid"]] == ["broken"]

    copied = workflows_dir("user", str(root), home=home) / "pr-review.dwf.ts"
    assert copied.read_text(encoding="utf-8") == original  # 逐字节一致

    # 技能生成：简单 frontmatter + 执行指引
    skill_text = Path(report["imported"][0]["skill"]).read_text(encoding="utf-8")
    assert skill_text.startswith("---")
    assert "name: pr-review" in skill_text
    assert "workflow_run" in skill_text

    # 二次导入默认跳过；overwrite=True 覆盖
    report_again = import_from_zcode(str(root), home=home, scopes=("user",))
    assert report_again["skipped"] and not report_again["imported"]
    report_overwrite = import_from_zcode(str(root), home=home, scopes=("user",), overwrite=True)
    assert [item["name"] for item in report_overwrite["imported"]] == ["pr-review"]


def test_generate_skill_rejects_bad_name(tmp_path):
    parsed = parse_frontmatter(_SAMPLE)
    with pytest.raises(ValueError):
        generate_skill("../escape", parsed, home=tmp_path)


def test_import_report_is_json_serializable(tmp_path):
    home, root = tmp_path / "home", tmp_path / "proj"
    _write_workflow(zcode_workflows_dir("user", str(root), home=home), "x", _SAMPLE)
    report = import_from_zcode(str(root), home=home, scopes=("user",))
    json.dumps(report)  # 不抛即可


# ── save / delete / 单文件导入（创建能力）─────────────────────


def test_save_workflow_creates_editable_and_regenerates_skill(tmp_path):
    home, root = tmp_path / "home", tmp_path / "proj"
    error = save_workflow(
        "my-flow",
        description="My flow.",
        when_to_use="When asked.",
        args={"base": {"type": "string", "required": True}, "n": {"type": "number", "default": 3}},
        body='phase("Run");\nreturn String(args.base);\n',
        scope="project",
        workspace_root=str(root),
        home=home,
    )
    assert error == ""
    target = workflows_dir("project", str(root), home=home) / "my-flow.dwf.ts"
    text = target.read_text(encoding="utf-8")
    # 键序 description → whenToUse → args；正文逐字节在 */ 之后
    assert text.index("description:") < text.index("whenToUse:") < text.index("args:")
    parsed = parse_frontmatter(text)
    assert parsed.args["base"]["required"] is True
    assert parsed.body == 'phase("Run");\nreturn String(args.base);\n'
    # 技能同步生成
    assert (skill_dir_of(home, "my-flow") / "SKILL.md").is_file()

    # 更新（overwrite）后技能内容跟随刷新
    error = save_workflow(
        "my-flow",
        description="My flow v2.",
        body='phase("Go");\n',
        scope="project",
        workspace_root=str(root),
        home=home,
        overwrite=True,
    )
    assert error == ""
    skill_text = (skill_dir_of(home, "my-flow") / "SKILL.md").read_text(encoding="utf-8")
    assert "My flow v2." in skill_text


def skill_dir_of(home, name):
    from core.workflows.skill_sync import skill_dir

    return skill_dir(name, home=home)


def test_save_workflow_duplicate_gate_and_validation(tmp_path):
    home, root = tmp_path / "home", tmp_path / "proj"
    kwargs = dict(
        description="d",
        body="phase(\"X\");\n",
        scope="user",
        home=home,
    )
    assert save_workflow("dup", **kwargs) == ""
    error = save_workflow("dup", **kwargs)
    assert "已存在同名" in error
    assert save_workflow("dup", **kwargs, overwrite=True) == ""
    # 校验：空描述 / 空脚本 / 坏名字 / 坏 scope
    assert "description" in save_workflow("x", description="  ", body="b", scope="user", home=home)
    assert "脚本" in save_workflow("x", description="d", body="  ", scope="user", home=home)
    assert "名字" in save_workflow("..", description="d", body="b", scope="user", home=home)
    assert "scope" in save_workflow("x", description="d", body="b", scope="bogus", home=home)


def test_delete_workflow_removes_file_and_skill(tmp_path):
    home, root = tmp_path / "home", tmp_path / "proj"
    save_workflow(
        "gone",
        description="d",
        body="phase(\"X\");\n",
        scope="user",
        home=home,
    )
    assert (skill_dir_of(home, "gone") / "SKILL.md").is_file()
    assert delete_workflow("gone", "user", home=home) == ""
    assert not (home / ".ycode" / "workflows" / "gone.dwf.ts").exists()
    assert not skill_dir_of(home, "gone").exists()
    # 再删报「没有」；坏 scope 报错
    assert "没有工作流" in delete_workflow("gone", "user", home=home)
    assert "scope" in delete_workflow("gone", "bogus", home=home)


def test_import_workflow_file_validates_and_copies(tmp_path):
    home, root = tmp_path / "home", tmp_path / "proj"
    picked = tmp_path / "picked"
    (picked).mkdir()
    good = picked / "ok.dwf.ts"
    good.write_text(_SAMPLE, encoding="utf-8")
    (picked / "nope.txt").write_text("text", encoding="utf-8")
    (picked / "broken.dwf.ts").write_text("garbage", encoding="utf-8")

    result = import_workflow_file(good, scope="project", workspace_root=str(root), home=home)
    assert result["ok"] is True
    copied = workflows_dir("project", str(root), home=home) / "ok.dwf.ts"
    assert copied.read_text(encoding="utf-8") == _SAMPLE
    assert (skill_dir_of(home, "ok") / "SKILL.md").is_file()

    assert not import_workflow_file(picked / "nope.txt", scope="user", home=home)["ok"]
    broken = import_workflow_file(picked / "broken.dwf.ts", scope="user", home=home)
    assert not broken["ok"] and "missing_frontmatter" in broken["error"]
    # 显式导入坏文件不落盘
    assert not (workflows_dir("user", home=home) / "broken.dwf.ts").exists()
    # 同名 exists 标记；overwrite=True 放行
    dup = import_workflow_file(good, scope="project", workspace_root=str(root), home=home)
    assert not dup["ok"] and dup.get("exists") is True
    assert import_workflow_file(
        good, scope="project", workspace_root=str(root), home=home, overwrite=True
    )["ok"]
