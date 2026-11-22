from pathlib import Path
from unittest.mock import patch

import pytest

from core import ai_output_paths as aop
from core.app_context import permission_mode_scope
from core.gui_path_policy import gui_path_access_allowed
from core.harness.tool_adapters import classify_path_access
from core.permission_policy import (
    PermissionAction,
    PermissionFacts,
    PermissionRisk,
    decide_permission,
)
from core import permission_terminal as pt
from core.tool_registry import build_default_registry
from core.workspace_context import workspace_scope


def test_strip_quotes_and_relative_set_setting(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_CUSTOM_AI_VAR", str(tmp_path / "env_ai_out"))
    with workspace_scope(tmp_path):
        quoted = f'  "{tmp_path / "quoted_out"}"  '
        target = aop.set_ai_output_dir_setting(quoted)
        assert target == (tmp_path / "quoted_out").resolve()
        assert aop.get_ai_output_dir_setting() == str(tmp_path / "quoted_out")

        rel_target = aop.set_ai_output_dir_setting("rel_ai_out")
        assert rel_target == (tmp_path / "rel_ai_out").resolve()
        assert aop.get_ai_output_dir_setting() == str(rel_target)

        env_target = aop.set_ai_output_dir_setting("${MY_CUSTOM_AI_VAR}")
        assert env_target == (tmp_path / "env_ai_out").resolve()
        assert aop.resolve_ai_output_dir() == env_target

    # Cross-workspace: relative-saved setting stays anchored to its resolved path
    other_ws = tmp_path / "other_ws"
    other_ws.mkdir()
    aop.set_ai_output_dir_setting(str(tmp_path / "rel_ai_out"))
    with workspace_scope(other_ws):
        assert aop.resolve_ai_output_dir() == (tmp_path / "rel_ai_out").resolve()


def test_is_ai_output_path_handles_quotes_env_and_relative(tmp_path, monkeypatch):
    custom_dir = tmp_path / "custom_ai"
    aop.set_ai_output_dir_setting(str(custom_dir))
    monkeypatch.setenv("TEMP_AI_TARGET", str(custom_dir / "chart.png"))

    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    with workspace_scope(project_dir):
        assert not aop.is_ai_output_path("")
        assert not aop.is_ai_output_path("   ")
        assert aop.is_ai_output_path(f'"{custom_dir / "note.txt"}"')
        assert aop.is_ai_output_path("${TEMP_AI_TARGET}")

    with workspace_scope(custom_dir):
        assert aop.is_ai_output_path("script.py")
        assert aop.is_ai_output_path("nested/data.json")


def test_resolve_ai_output_dir_respect_workdir_false_for_nested_project(tmp_path):
    configured = tmp_path / "ai_root"
    subproject = configured / "user_project"
    subproject.mkdir(parents=True)
    aop.set_ai_output_dir_setting(str(configured))

    with workspace_scope(subproject):
        assert aop.resolve_ai_output_dir(respect_workdir=True) == subproject.resolve()
        assert aop.resolve_ai_output_dir(respect_workdir=False) == configured.resolve()

        # Distinct subproject nested inside ai_output_dir is NOT exempted as temp output
        assert not aop.is_ai_output_write_target(str(subproject / "app.py"))

    # GLOBAL session (workdir == ai_output_dir) IS a temp output write target
    with workspace_scope(configured):
        assert aop.is_ai_output_write_target("temp_script.py")
        assert aop.is_ai_output_write_target(str(configured / "sub" / "out.md"))


def test_global_workspace_root_tracks_default_and_custom_ai_output_dir(tmp_path):
    import gui_bridge

    aop.set_ai_output_dir_setting("")
    assert gui_bridge._global_workspace_root() == str(aop.default_ai_output_dir().resolve())

    custom_dir = tmp_path / "user_global_ai"
    aop.set_ai_output_dir_setting(str(custom_dir))
    assert gui_bridge._global_workspace_root() == str(custom_dir.resolve())

    aop.set_ai_output_dir_setting("")
    assert gui_bridge._global_workspace_root() == str(aop.default_ai_output_dir().resolve())


def test_media_save_dir_handles_blank_and_relative_against_workdir(tmp_path):
    from core import hunyuan_image
    import importlib.util
    import json

    img_plugin_path = Path(__file__).resolve().parents[1] / "plugins" / "tokenrhythm_image" / "plugin.py"
    vid_plugin_path = Path(__file__).resolve().parents[1] / "plugins" / "tokenrhythm_video" / "plugin.py"

    def _load(name, path):
        spec = importlib.util.spec_from_file_location(name, str(path))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    tr_img = _load("_test_tr_img_save_dir", img_plugin_path)
    tr_vid = _load("_test_tr_vid_save_dir", vid_plugin_path)

    custom_ai = tmp_path / "ai_media_out"
    project_ws = tmp_path / "project_ws"
    project_ws.mkdir()
    aop.set_ai_output_dir_setting(str(custom_ai))

    with workspace_scope(project_ws):
        assert aop.resolve_ai_output_save_dir(None) == str(custom_ai.resolve())
        assert aop.resolve_ai_output_save_dir("") == str(custom_ai.resolve())
        assert aop.resolve_ai_output_save_dir("   ") == str(custom_ai.resolve())
        assert tr_img._resolve_save_dir("") == str(custom_ai.resolve())
        assert tr_vid._resolve_save_dir("   ") == str(custom_ai.resolve())

        rel_expected = str((project_ws / "assets").resolve())
        assert tr_img._resolve_save_dir("assets") == rel_expected
        assert tr_vid._resolve_save_dir("assets") == rel_expected

        import io

        class _FakeUrlResp:
            headers = {"Content-Type": "image/png"}

            def __init__(self, body: bytes):
                self._buf = io.BytesIO(body)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def close(self):
                self._buf.close()

            def info(self):
                return self.headers

            def read(self, n: int = -1):
                return self._buf.read(n)

        responses = iter([
            _FakeUrlResp(json.dumps({"imageUrl": "https://example.com/a.png"}).encode("utf-8")),
            _FakeUrlResp(b"\x89PNG\r\n\x1a\n" + b"0" * 16),
        ])

        with patch.object(hunyuan_image, "_resolve_api_key", return_value="k"), \
             patch.object(hunyuan_image, "_resolve_api_url", return_value="https://example.com/gen"), \
             patch.object(hunyuan_image.urllib.request, "urlopen", side_effect=lambda *_a, **_kw: next(responses)):
            res = hunyuan_image.generate_image("cat", save_dir="")
            assert not res.is_error, res.content
            files = list(custom_ai.glob("*.png"))
            assert len(files) == 1
            assert files[0].parent == custom_ai.resolve()


def test_gui_path_policy_allows_ai_output_dir_and_blocks_credentials(tmp_path):
    project_ws = tmp_path / "project_ws"
    project_ws.mkdir()
    custom_ai = tmp_path / "ai_out"
    custom_ai.mkdir()
    outside_other = tmp_path / "outside_other" / "secret.txt"
    outside_other.parent.mkdir()
    outside_other.write_text("x", encoding="utf-8")

    aop.set_ai_output_dir_setting(str(custom_ai))
    ai_file = custom_ai / "preview.png"
    ai_file.write_bytes(b"png")

    with patch("core.model_manager.load_config", return_value={"allow_outside_workspace": False}):
        allowed, reason = gui_path_access_allowed(str(ai_file), str(project_ws), action="preview")
        assert allowed, reason

        denied, _ = gui_path_access_allowed(str(outside_other), str(project_ws), action="preview")
        assert not denied

        from config import _CONFIG_FILE
        cred_allowed, _ = gui_path_access_allowed(str(_CONFIG_FILE), str(project_ws), action="open")
        assert not cred_allowed


@pytest.mark.parametrize("mode", ["confirm", "accept_edits", "auto_decision", "full"])
def test_permission_policy_allows_ai_output_write_without_bypassing_sandbox(mode):
    decision = decide_permission(
        PermissionFacts(
            mode=mode,
            risk=PermissionRisk.NORMAL_MUTATION,
            outside_workspace=False,
            sandbox_restricted=True,
            ai_output_write=True,
        )
    )
    assert decision.action is PermissionAction.ALLOW
    assert decision.reason == "ai_output_dir_allow"

    outside_decision = decide_permission(
        PermissionFacts(
            mode=mode,
            risk=PermissionRisk.NORMAL_MUTATION,
            outside_workspace=True,
            sandbox_restricted=True,
            ai_output_write=True,
        )
    )
    assert outside_decision.action is PermissionAction.ASK
    assert outside_decision.reason == "sandbox_boundary_requires_confirmation"


def test_permission_policy_plan_mode_still_denies_ai_output_write():
    decision = decide_permission(
        PermissionFacts(
            mode="plan",
            risk=PermissionRisk.NORMAL_MUTATION,
            outside_workspace=False,
            sandbox_restricted=True,
            ai_output_write=True,
        )
    )
    assert decision.action is PermissionAction.DENY
    assert decision.reason == "plan_mutation_denied"


@pytest.mark.parametrize("use_custom_dir", [False, True])
@pytest.mark.parametrize("mode", ["confirm", "accept_edits", "auto_decision", "full"])
def test_authorize_tool_call_allows_ai_output_dir_without_prompts(tmp_path, monkeypatch, use_custom_dir, mode):
    if use_custom_dir:
        custom_dir = tmp_path / "custom_ai_dir"
        aop.set_ai_output_dir_setting(str(custom_dir))
    else:
        aop.set_ai_output_dir_setting("")

    project_ws = tmp_path / "proj_ws"
    project_ws.mkdir()
    reg = build_default_registry()

    with patch("core.model_manager.load_config", return_value={"allow_outside_workspace": False}), \
         patch("builtins.input", side_effect=AssertionError("不应触发终端 input 询问")), \
         patch.object(pt, "_wait_permission_option", side_effect=AssertionError("不应弹窗询问权限")):
        # 1) Project session writing/reading/fetching into default or custom ai_output_dir
        with workspace_scope(project_ws), permission_mode_scope(mode):
            ai_dir = aop.resolve_ai_output_dir()
            assert classify_path_access("write_file", str(ai_dir / "temp.py")) == "trusted_read"

            for tool_name, args in [
                ("write_file", {"file_path": str(ai_dir / "temp.py"), "content": "x"}),
                ("edit_file", {"file_path": str(ai_dir / "temp.py"), "target_content": "a", "replacement_content": "b"}),
                ("multi_edit", {"file_path": str(ai_dir / "temp.py"), "edits": [{"target_content": "a", "replacement_content": "b"}]}),
                ("office_write", {"path": str(ai_dir / "draft.xlsx"), "sheets": [{"rows": [["k", "v"], ["a", 1]]}]}),
                ("read_file", {"file_path": str(ai_dir / "temp.py")}),
                ("list_directory", {"directory_path": str(ai_dir)}),
                ("fetch_image", {"url": "https://example.com/demo.png"}),
            ]:
                result = pt.authorize_tool_call(tool_name, args, tool_spec=reg.get(tool_name))
                assert result.allowed, result

        # 2) Non-project GLOBAL session (workdir == resolve_ai_output_dir(respect_workdir=False))
        global_root = aop.resolve_ai_output_dir(respect_workdir=False)
        with workspace_scope(global_root), permission_mode_scope(mode):
            for tool_name, args in [
                ("write_file", {"file_path": "rel_note.md", "content": "hi"}),
                ("edit_file", {"file_path": "rel_note.md", "target_content": "hi", "replacement_content": "hello"}),
                ("multi_edit", {"file_path": "sub/rel_note.md", "edits": [{"target_content": "a", "replacement_content": "b"}]}),
                ("office_write", {"path": "out.xlsx", "sheets": [{"rows": [["k"], ["v"]]}]}),
                ("fetch_image", {"url": "https://example.com/demo.png"}),
            ]:
                result = pt.authorize_tool_call(tool_name, args, tool_spec=reg.get(tool_name))
                assert result.allowed, result


def test_confirm_mode_still_prompts_for_project_and_credentials_and_outside(tmp_path):
    project_ws = tmp_path / "proj_ws"
    project_ws.mkdir()
    outside_ws = tmp_path / "outside_ws"
    outside_ws.mkdir()
    aop.set_ai_output_dir_setting("")
    reg = build_default_registry()

    prompts = []

    def _reject_input(_prompt=""):
        prompts.append("asked")
        return "n"

    with workspace_scope(project_ws), permission_mode_scope("confirm"), \
         patch("core.model_manager.load_config", return_value={"allow_outside_workspace": False}), \
         patch("builtins.input", side_effect=_reject_input):
        for tool_name, args in [
            ("write_file", {"file_path": str(project_ws / "src.py"), "content": "x"}),
            ("write_file", {"file_path": str(outside_ws / "other.py"), "content": "x"}),
            ("write_file", {"file_path": str(aop.default_ai_output_dir().parent / "config.json"), "content": "{}"}),
        ]:
            prompts.clear()
            auth_res = pt.authorize_tool_call(tool_name, args, tool_spec=reg.get(tool_name))
            assert not auth_res.allowed
            assert len(prompts) == 1


def test_tool_pipeline_write_edit_read_inside_ai_output_dir_in_confirm_mode(tmp_path):
    import json
    from core.harness.tool_pipeline import (
        DecodedToolCall,
        ToolExecutionContext,
        ToolPipeline,
    )

    custom_dir = tmp_path / "pipeline_ai_out"
    aop.set_ai_output_dir_setting(str(custom_dir))
    project_ws = tmp_path / "proj_ws"
    project_ws.mkdir()
    reg = build_default_registry()
    pipeline = ToolPipeline()
    ctx = ToolExecutionContext(
        event_callback=None,
        parent_session=None,
        parent_turn_id="turn_test",
        cancel_event=None,
        allow_subagents=False,
        permission_owner=None,
        permission_source="主代理",
        permission_checker=pt.authorize_tool_call,
        registry=reg,
        mcp_manager=None,
    )

    with workspace_scope(project_ws), permission_mode_scope("confirm"), \
         patch("core.model_manager.load_config", return_value={"allow_outside_workspace": False}), \
         patch("builtins.input", side_effect=AssertionError("不应弹窗询问")):
        target = custom_dir / "note.txt"
        write_args = {"file_path": str(target), "content": "hello v1\n"}
        write_rec = pipeline.execute_decoded(
            DecodedToolCall("c1", "write_file", write_args, json.dumps(write_args)),
            ctx,
        )
        assert not write_rec["content"].startswith("🚨"), write_rec["content"]
        assert target.exists()

        edit_args = {"file_path": str(target), "target_content": "v1", "replacement_content": "v2"}
        edit_rec = pipeline.execute_decoded(
            DecodedToolCall("c2", "edit_file", edit_args, json.dumps(edit_args)),
            ctx,
        )
        assert not edit_rec["content"].startswith("🚨"), edit_rec["content"]

        read_args = {"file_path": str(target)}
        read_rec = pipeline.execute_decoded(
            DecodedToolCall("c3", "read_file", read_args, json.dumps(read_args)),
            ctx,
        )
        assert not read_rec["content"].startswith("🚨"), read_rec["content"]
        assert "hello v2" in read_rec["content"]

