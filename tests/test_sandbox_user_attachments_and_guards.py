# -*- coding: utf-8 -*-
"""测试沙盒限制下用户粘贴图片、文件附件及工作区外工具访问交互门禁策略。"""
import os
import tempfile
from pathlib import Path
import pytest

from core.tools import (
    is_trusted_paste_path,
    register_paste_file,
    register_session_user_attached_paths,
    is_session_user_attached_path,
    _resolve_workspace_path,
)
from core.workspace_context import workspace_scope
from core.input_parser import parse_user_input
from core.gui_path_policy import gui_path_access_allowed
from core.harness.tool_adapters import _sandbox_outside_guard


def test_is_trusted_paste_path(tmp_path):
    # 模拟系统临时目录下的 ycode_paste 路径
    temp_dir = tempfile.gettempdir()
    paste_dir = os.path.join(temp_dir, "ycode_paste")
    paste_file = os.path.join(paste_dir, "paste_img.png")

    # 未登记的文件：即使位于 paste 目录内也不受信（%TEMP% 全用户可写，
    # 其他进程投放的文件不得免沙盒确认）
    assert is_trusted_paste_path(paste_file) is False
    assert is_trusted_paste_path(paste_dir) is False

    # 本进程落盘登记后：受信
    register_paste_file(paste_file)
    assert is_trusted_paste_path(paste_file) is True

    # 普通外部目录不能算作 paste 目录
    assert is_trusted_paste_path(str(tmp_path / "normal.png")) is False


def test_session_user_attached_paths(tmp_path):
    session_id = "test_session_attach"
    p1 = str(tmp_path / "outside1.txt")
    p2 = str(tmp_path / "outside2.txt")
    Path(p1).write_text("outside 1", encoding="utf-8")
    Path(p2).write_text("outside 2", encoding="utf-8")

    # 初始未注册
    assert is_session_user_attached_path(p1, session_id) is False

    # 注册后
    register_session_user_attached_paths([p1], session_id=session_id)
    assert is_session_user_attached_path(p1, session_id) is True
    assert is_session_user_attached_path(p1, "other_session") is False
    # 无会话绑定（session_id=None 且线程未绑定会话）时不得兜底放行：
    # 其他会话挂载过的路径不代表本会话用户已授权
    assert is_session_user_attached_path(p1, None) is False
    assert is_session_user_attached_path(p2, session_id) is False


def test_attached_paths_no_cross_session_leak(tmp_path):
    """跨会话防泄漏回归：会话 A 挂载的附件不得被会话 B 免确认读取。"""
    p = str(tmp_path / "secret.txt")
    Path(p).write_text("secret", encoding="utf-8")
    register_session_user_attached_paths([p], session_id="session_a")
    assert is_session_user_attached_path(p, "session_a") is True
    assert is_session_user_attached_path(p, "session_b") is False
    # 显式传 None 也不得遍历所有会话的登记集合兜底放行
    assert is_session_user_attached_path(p, None) is False


def test_resolve_workspace_path_with_trusted_read(tmp_path, monkeypatch):
    # 锁定沙盒：不允许外部路径
    monkeypatch.setattr("core.model_manager.load_config", lambda: {"allow_outside_workspace": False})
    
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    
    outside_file = str(outside / "test.txt")
    Path(outside_file).write_text("hello", encoding="utf-8")
    
    temp_dir = tempfile.gettempdir()
    paste_file = os.path.join(temp_dir, "ycode_paste", "p.txt")
    os.makedirs(os.path.dirname(paste_file), exist_ok=True)
    Path(paste_file).write_text("paste", encoding="utf-8")
    register_paste_file(paste_file)

    import core.app_context as app_context
    old_thread = app_context.get_thread_session()
    app_context.set_thread_session("s1")
    try:
        with workspace_scope(str(workspace)):
            # 1. 默认情况下，外部路径越界抛异常
            with pytest.raises(ValueError, match="路径越界"):
                _resolve_workspace_path(outside_file, allow_trusted_read=False)

            # 2. 带有 allow_trusted_read=True，但未在受托集合中：依然抛异常
            with pytest.raises(ValueError, match="路径越界"):
                _resolve_workspace_path(outside_file, allow_trusted_read=True)

            # 3. 注册到当前 session 附件后，allow_trusted_read=True 放行
            register_session_user_attached_paths([outside_file], session_id="s1")
            resolved = _resolve_workspace_path(outside_file, allow_trusted_read=True)
            assert os.path.normcase(resolved) == os.path.normcase(outside_file)

            # 4. 受托粘贴路径，直接放行
            resolved_paste = _resolve_workspace_path(paste_file, allow_trusted_read=True)
            assert os.path.normcase(resolved_paste) == os.path.normcase(paste_file)
    finally:
        app_context.set_thread_session(old_thread)


def test_parse_user_input_explicit_attachments_bypass_sandbox(tmp_path, monkeypatch):
    # 模拟沙盒未开放
    monkeypatch.setattr("core.model_manager.load_config", lambda: {"allow_outside_workspace": False})
    # 工作目录设置为 workspace
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    
    outside_txt = tmp_path / "outside_note.txt"
    outside_txt.write_text("重要用户说明内容", encoding="utf-8")
    
    session_id = "test_gui_session"
    
    with workspace_scope(str(workspace)):
        # 传入 explicit_paths
        content, msgs = parse_user_input(
            "请看这个文件",
            explicit_paths=[str(outside_txt)],
            session_id=session_id
        )
        
        # 确认没有产生“已跳过工作目录外的文件”提示
        for msg in msgs:
            assert "已跳过工作目录外的文件" not in msg
            
        # 确认内容已被成功附带
        assert isinstance(content, str)
        assert "重要用户说明内容" in content
        
        # 确认注册生效
        assert is_session_user_attached_path(str(outside_txt), session_id) is True


def test_sandbox_outside_guard_bypass_trusted_read(tmp_path, monkeypatch):
    monkeypatch.setattr("core.model_manager.load_config", lambda: {"allow_outside_workspace": False})

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    outside_file = str(tmp_path / "doc.txt")
    Path(outside_file).write_text("doc content", encoding="utf-8")
    session_id = "guard_session"
    register_session_user_attached_paths([outside_file], session_id=session_id)

    checker_calls = []
    def mock_checker(perm_type, payload, **kwargs):
        checker_calls.append((perm_type, payload))
        return True

    # 模拟回合线程的会话绑定（gui_bridge 在回合入口 set_thread_session）：
    # guard 内部按线程会话查附件登记，不再跨会话兜底
    import core.app_context as app_context
    old_thread = app_context.get_thread_session()
    app_context.set_thread_session(session_id)
    try:
        with workspace_scope(str(workspace)):
            # 对于 read_file：受托文件自动免打扰 bypass，不触发 checker
            auth_ctx, auth_err = _sandbox_outside_guard(
                "read_file",
                outside_file,
                None,
                session_id,
                "",
                "local",
                None,
                permission_checker=mock_checker,
            )
            assert auth_err is None
            assert auth_ctx is not None  # sandbox_bypass() context manager
            assert len(checker_calls) == 0  # 免打扰放行，未调 checker

            # 对于 write_file：受托文件仍必须弹门禁（写保护）
            auth_ctx_w, auth_err_w = _sandbox_outside_guard(
                "write_file",
                outside_file,
                None,
                session_id,
                "",
                "local",
                None,
                permission_checker=mock_checker,
            )
            assert auth_err_w is None
            assert auth_ctx_w is not None
            assert len(checker_calls) == 1
            assert checker_calls[0][0] == "sandbox_outside_access"
            assert checker_calls[0][1]["operation"] == "write_file"
    finally:
        app_context.set_thread_session(old_thread)


def test_sandbox_outside_guard_for_directory_and_outline_tools(tmp_path, monkeypatch):
    """测试 directory_tree, view_file_outline 等工具越界时触发交互门禁，而不是未捕获崩溃。"""
    monkeypatch.setattr("core.model_manager.load_config", lambda: {"allow_outside_workspace": False})

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside_dir = tmp_path / "outside_dir"
    outside_dir.mkdir()
    outside_code = outside_dir / "code.py"
    outside_code.write_text("def hello(): pass\n", encoding="utf-8")

    checker_calls = []
    def mock_checker(perm_type, payload, **kwargs):
        checker_calls.append((perm_type, payload))
        return True

    with workspace_scope(str(workspace)):
        # 1. directory_tree
        auth_ctx, auth_err = _sandbox_outside_guard(
            "directory_tree",
            str(outside_dir),
            None,
            "sess",
            "",
            "local",
            None,
            permission_checker=mock_checker,
        )
        assert auth_err is None
        assert auth_ctx is not None
        assert len(checker_calls) == 1
        assert checker_calls[0][0] == "sandbox_outside_access"
        assert checker_calls[0][1]["operation"] == "directory_tree"

        # 2. view_file_outline
        auth_ctx2, auth_err2 = _sandbox_outside_guard(
            "view_file_outline",
            str(outside_code),
            None,
            "sess",
            "",
            "local",
            None,
            permission_checker=mock_checker,
        )
        assert auth_err2 is None
        assert auth_ctx2 is not None
        assert len(checker_calls) == 2
        assert checker_calls[1][0] == "sandbox_outside_access"
        assert checker_calls[1][1]["operation"] == "view_file_outline"


def test_sandbox_outside_guard_trusted_readonly_tools_exempt(tmp_path, monkeypatch):
    """受托路径豁免必须覆盖全部只读工具，而不只 read_file。

    回归：classify_path_access 豁免 7 个只读工具而 guard 只豁免 read_file 时，
    权限层免问、执行层又弹沙盒确认，两层门禁不一致。"""
    monkeypatch.setattr("core.model_manager.load_config", lambda: {"allow_outside_workspace": False})

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside_dir = tmp_path / "outside_dir"
    outside_dir.mkdir()
    outside_file = outside_dir / "code.py"
    outside_file.write_text("def hello(): pass\n", encoding="utf-8")

    # 登记为会话附件（用户显式挂载）
    register_session_user_attached_paths([str(outside_dir), str(outside_file)], session_id="sess2")

    checker_calls = []
    def mock_checker(perm_type, payload, **kwargs):
        checker_calls.append((perm_type, payload))
        return True

    import core.app_context as app_context
    old_thread = app_context.get_thread_session()
    app_context.set_thread_session("sess2")
    try:
        with workspace_scope(str(workspace)):
            for tool_name, path in (
                ("read_file", str(outside_file)),
                ("view_file_outline", str(outside_file)),
                ("directory_tree", str(outside_dir)),
                ("list_directory", str(outside_dir)),
                ("search_code", str(outside_dir)),
                ("glob_files", str(outside_dir)),
                ("find_definition", str(outside_dir)),
            ):
                auth_ctx, auth_err = _sandbox_outside_guard(
                    tool_name, path, None, "sess2", "", "local", None,
                    permission_checker=mock_checker,
                )
                assert auth_err is None, f"{tool_name} 受托路径不应报错: {auth_err}"
                assert auth_ctx is not None, f"{tool_name} 受托路径应免确认 bypass"
            # 全部免问：checker 一次都不该被调用
            assert len(checker_calls) == 0

            # 对照：写工具访问受托路径仍必须弹门禁（写保护不放松）
            auth_ctx_w, auth_err_w = _sandbox_outside_guard(
                "write_file", str(outside_file), None, "sess2", "", "local", None,
                permission_checker=mock_checker,
            )
            assert auth_err_w is None
            assert auth_ctx_w is not None
            assert len(checker_calls) == 1
            assert checker_calls[0][1]["operation"] == "write_file"
    finally:
        app_context.set_thread_session(old_thread)


def test_gui_path_policy_trusted_paths(tmp_path, monkeypatch):
    monkeypatch.setattr("core.gui_path_policy._allow_outside_workspace", lambda: False)
    
    workspace = str(tmp_path / "workspace")
    os.makedirs(workspace, exist_ok=True)
    
    outside_file = str(tmp_path / "attach.png")
    Path(outside_file).write_bytes(b"png-data")
    session_id = "gui_test_sess"
    
    # 未注册前：拒绝
    allowed, msg = gui_path_access_allowed(outside_file, workspace, action="preview", session_id=session_id)
    assert allowed is False
    assert "文件不在当前工作区内" in msg
    
    # 注册为该会话附件后：放行
    register_session_user_attached_paths([outside_file], session_id=session_id)
    allowed, msg = gui_path_access_allowed(outside_file, workspace, action="preview", session_id=session_id)
    assert allowed is True
    assert msg == ""


def test_authorize_tool_call_for_outside_directory(monkeypatch):
    """验证查看外部目录（如 list_directory）不会因为非 read/write/edit 操作而报权限系统异常。"""
    from core.llm import authorize_tool_call
    from core.permission_broker import PermissionOptionKey

    monkeypatch.setattr("core.model_manager.load_config", lambda: {"allow_outside_workspace": False})
    # 模拟用户点击“允许本次”
    monkeypatch.setattr("core.llm._wait_permission_option", lambda *args, **kwargs: PermissionOptionKey.ALLOW_ONCE.value)

    # 之前这里会直接 raise PermissionSystemError("沙箱外访问缺少有效的原始文件操作类型")
    auth_res = authorize_tool_call(
        "sandbox_outside_access",
        {"path": "D:\\Users", "operation": "list_directory"},
        tool_spec=None,
    )
    assert auth_res.allowed is True
    assert auth_res.sandbox_bypass is True

