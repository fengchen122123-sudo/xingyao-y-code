"""一键运行命令解析测试（规格 6.2：按语言猜命令或读取项目配置；未知语言要有明确处理）。"""

import json
import os
import sys

import pytest

from core.ide import run_command as rc


@pytest.fixture()
def workspace(tmp_path):
    return str(tmp_path)


def _write(path, text, encoding="utf-8"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding=encoding)


def test_python_plan_uses_current_interpreter(workspace, tmp_path):
    target = tmp_path / "app.py"
    _write(target, "print('hi')\n")

    plan = rc.build_run_plan(str(target), workspace)

    assert plan.language_id == "python" and plan.source == "language"
    assert plan.command.startswith("& ")
    assert sys.executable in plan.command
    assert "-u" in plan.command
    assert plan.command.endswith("'" + str(target) + "'")


def test_extra_args_are_appended(workspace, tmp_path):
    target = tmp_path / "app.py"
    _write(target, "print('hi')\n")

    plan = rc.build_run_plan(str(target), workspace, extra_args="--name 星瑶")

    assert plan.command.endswith("--name 星瑶")


def test_explicit_command_wins(workspace, tmp_path):
    target = tmp_path / "app.py"
    _write(target, "print('hi')\n")

    plan = rc.build_run_plan(str(target), workspace, explicit_command="echo 自定义")

    assert plan.command == "echo 自定义" and plan.source == "explicit"


def test_javascript_plan_uses_node(workspace, tmp_path, monkeypatch):
    monkeypatch.setattr(rc.shutil, "which", lambda name: r"C:\node\node.exe" if name == "node" else None)
    target = tmp_path / "index.js"
    _write(target, "console.log(1)\n")

    plan = rc.build_run_plan(str(target), workspace)

    assert plan.language_id == "javascript"
    assert plan.command == f"& 'C:\\node\\node.exe' '{target}'"


def test_javascript_without_node_raises_hint(workspace, tmp_path, monkeypatch):
    monkeypatch.setattr(rc.shutil, "which", lambda name: None)
    target = tmp_path / "index.js"
    _write(target, "console.log(1)\n")

    with pytest.raises(rc.RunPlanError, match="Node"):
        rc.build_run_plan(str(target), workspace)


def test_go_plan_uses_go_run(workspace, tmp_path, monkeypatch):
    monkeypatch.setattr(rc.shutil, "which", lambda name: r"C:\Go\bin\go.exe" if name == "go" else None)
    target = tmp_path / "main.go"
    _write(target, "package main\n")

    plan = rc.build_run_plan(str(target), workspace)

    assert plan.command == f"& 'C:\\Go\\bin\\go.exe' run '{target}'"
    assert plan.language_id == "go"


def test_package_json_start_script_preferred(workspace, tmp_path):
    _write(tmp_path / "package.json", json.dumps({"scripts": {"start": "node index.js", "dev": "vite"}}))
    target = tmp_path / "index.js"
    _write(target, "console.log(1)\n")

    plan = rc.build_run_plan(str(target), workspace)

    assert plan.source == "package.json" and "run start" in plan.command


def test_package_json_dev_script_fallback(workspace, tmp_path):
    _write(tmp_path / "package.json", json.dumps({"scripts": {"dev": "vite"}}))
    target = tmp_path / "index.js"
    _write(target, "console.log(1)\n")

    plan = rc.build_run_plan(str(target), workspace)

    assert "run dev" in plan.command and plan.source == "package.json"


def test_launch_json_program_wins_for_compiled_output(workspace, tmp_path):
    exe = tmp_path / "build" / "app.exe"
    _write(tmp_path / ".vscode" / "launch.json", json.dumps({
        "version": "0.2.0",
        "configurations": [{"name": "dbg", "program": str(exe), "args": ["--verbose"]}],
    }))
    source_file = tmp_path / "src" / "main.cpp"
    _write(source_file, "int main(){return 0;}\n")

    plan = rc.build_run_plan(str(source_file), workspace)

    assert plan.source == "launch.json"
    assert str(exe) in plan.command and "--verbose" in plan.command


def test_typescript_without_package_json_raises(workspace, tmp_path):
    target = tmp_path / "app.ts"
    _write(target, "export const x = 1\n")

    with pytest.raises(rc.RunPlanError, match="package.json"):
        rc.build_run_plan(str(target), workspace)


def test_markdown_file_is_not_runnable(workspace, tmp_path):
    target = tmp_path / "notes.md"
    _write(target, "# hi\n")

    with pytest.raises(rc.RunPlanError, match="不可运行"):
        rc.build_run_plan(str(target), workspace)


def test_unknown_extension_raises_with_hint(workspace, tmp_path):
    target = tmp_path / "data.xyz"
    _write(target, "x\n")

    with pytest.raises(rc.RunPlanError) as excinfo:
        rc.build_run_plan(str(target), workspace)

    assert ".xyz" in str(excinfo.value) and "launch.json" in str(excinfo.value)


def test_missing_file_raises(workspace, tmp_path):
    with pytest.raises(rc.RunPlanError, match="不存在"):
        rc.build_run_plan(str(tmp_path / "nope.py"), workspace)


def test_gbk_package_json_is_still_readable(workspace, tmp_path):
    """项目里 GBK 编码的 package.json 不得让一键运行直接失败。"""
    payload = {"scripts": {"start": "node 主程序.js"}, "name": "中文项目"}
    (tmp_path / "package.json").write_bytes(json.dumps(payload, ensure_ascii=False).encode("gbk"))
    target = tmp_path / "index.js"
    _write(target, "console.log(1)\n")

    plan = rc.build_run_plan(str(target), workspace)

    assert plan.source == "package.json" and "run start" in plan.command


# ------------------------------------------- 畸形 / 不完整项目配置（回退且说明原因）


def test_malformed_launch_json_falls_back_with_reason(workspace, tmp_path):
    _write(tmp_path / ".vscode" / "launch.json", "{ 这不是 JSON")
    target = tmp_path / "app.py"
    _write(target, "print('hi')\n")

    plan = rc.build_run_plan(str(target), workspace)

    assert plan.source == "language" and "-u" in plan.command
    assert "launch.json" in plan.note and "默认" in plan.note


def test_malformed_package_json_falls_back_with_reason(workspace, tmp_path, monkeypatch):
    monkeypatch.setattr(rc.shutil, "which", lambda name: r"C:\node\node.exe" if name == "node" else None)
    _write(tmp_path / "package.json", "not json at all")
    target = tmp_path / "index.js"
    _write(target, "console.log(1)\n")

    plan = rc.build_run_plan(str(target), workspace)

    assert plan.source == "language" and plan.command == f"& 'C:\\node\\node.exe' '{target}'"
    assert "package.json" in plan.note and "默认" in plan.note


def test_package_json_without_scripts_falls_back_with_reason(workspace, tmp_path, monkeypatch):
    monkeypatch.setattr(rc.shutil, "which", lambda name: r"C:\node\node.exe" if name == "node" else None)
    _write(tmp_path / "package.json", json.dumps({"name": "只有名字"}))
    target = tmp_path / "index.js"
    _write(target, "console.log(1)\n")

    plan = rc.build_run_plan(str(target), workspace)

    assert plan.source == "language"
    assert "scripts" in plan.note and "package.json" in plan.note


def test_package_json_non_object_falls_back(workspace, tmp_path, monkeypatch):
    """package.json 里是合法 JSON 但不是对象（[1,2,3]）：同样不许崩。"""
    monkeypatch.setattr(rc.shutil, "which", lambda name: r"C:\node\node.exe" if name == "node" else None)
    _write(tmp_path / "package.json", json.dumps([1, 2, 3]))
    target = tmp_path / "index.js"
    _write(target, "console.log(1)\n")

    plan = rc.build_run_plan(str(target), workspace)

    assert plan.source == "language" and "package.json" in plan.note


def test_launch_json_gbk_encoding_is_readable(workspace, tmp_path):
    """GBK 编码的 launch.json 也必须能读出来（和 package.json 同一口径）。"""
    target = tmp_path / "src" / "主程序.py"
    (tmp_path / ".vscode").mkdir(parents=True, exist_ok=True)
    payload = {"configurations": [{"name": "调试我的程序", "program": str(target)}]}
    (tmp_path / ".vscode" / "launch.json").write_bytes(
        json.dumps(payload, ensure_ascii=False).encode("gbk")
    )
    _write(target, "print('hi')\n")

    plan = rc.build_run_plan(str(target), workspace)

    assert plan.source == "launch.json" and plan.note == ""
    assert str(target) in plan.command


def test_launch_json_without_matching_program_falls_back(workspace, tmp_path):
    _write(tmp_path / ".vscode" / "launch.json", json.dumps({
        "version": "0.2.0",
        "configurations": [{"name": "别的东西", "program": str(tmp_path / "other.py")}],
    }))
    target = tmp_path / "app.py"
    _write(target, "print('hi')\n")

    plan = rc.build_run_plan(str(target), workspace)

    assert plan.source == "language" and "-u" in plan.command
    assert "launch.json" in plan.note


def test_launch_json_configurations_not_a_list(workspace, tmp_path):
    _write(tmp_path / ".vscode" / "launch.json", json.dumps({"configurations": "oops"}))
    target = tmp_path / "app.py"
    _write(target, "print('hi')\n")

    plan = rc.build_run_plan(str(target), workspace)

    assert plan.source == "language" and "configurations" in plan.note


def test_launch_json_config_without_program_is_ignored(workspace, tmp_path):
    _write(tmp_path / ".vscode" / "launch.json", json.dumps({
        "configurations": [{"name": "没有 program"}, "一个字符串", 42],
    }))
    target = tmp_path / "app.py"
    _write(target, "print('hi')\n")

    plan = rc.build_run_plan(str(target), workspace)

    assert plan.source == "language" and "-u" in plan.command
    assert "program" in plan.note


def test_compiled_language_without_launch_json_gives_actionable_hint(workspace, tmp_path):
    target = tmp_path / "main.cpp"
    _write(target, "int main(){return 0;}\n")

    with pytest.raises(rc.RunPlanError) as excinfo:
        rc.build_run_plan(str(target), workspace)

    message = str(excinfo.value)
    assert "launch.json" in message and "编译" in message


def test_package_json_search_stops_at_workspace_root(workspace, tmp_path, monkeypatch):
    """工作区之外（父目录）的 package.json 不得被捡来用。"""
    monkeypatch.setattr(rc.shutil, "which", lambda name: r"C:\node\node.exe" if name == "node" else None)
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"start": "evil"}}), encoding="utf-8")
    nested = tmp_path / "proj"
    target = nested / "index.js"
    _write(target, "console.log(1)\n")

    plan = rc.build_run_plan(str(target), str(nested))

    assert plan.source != "package.json", "不能越出工作区去捡父目录的 package.json"