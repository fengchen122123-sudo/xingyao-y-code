"""三处自动落盘的默认落点断言（规格 5）。

覆盖两条硬约束：
1. 未传 save_dir → 落到 AI 生成内容目录（由配置决定，不再跟进程 cwd 走）；
2. 显式传 save_dir（含 "."）→ 尊重原参数，不被隐式改道。

混元 / 基元生图通过 monkeypatch 假造 HTTP 响应与图片下载做真实落盘断言，
测试不发起任何真实网络请求，也不触碰真实 ~/.ycode。
"""

import inspect
import json
import re
from pathlib import Path

import pytest

import plugins.tokenrhythm_image.plugin as tokenrhythm_plugin
from core import ai_output_paths as aop
from core import hunyuan_image
from core.tools import fetch_image
from tests.test_fetch_image import _patch_http


@pytest.fixture()
def ai_dir(tmp_path, monkeypatch):
    """把 AI 生成内容目录指向 tmp_path/ai_out（隔离真实配置读取）。"""
    target = tmp_path / "ai_out"
    monkeypatch.setattr(aop, "_read_setting", lambda: str(target))
    return target


def _patch_hunyuan_http(monkeypatch, payload: bytes) -> None:
    """假造混元生图响应 + 图片下载：urlopen 返回 imageUrl，urlretrieve 写真实字节。"""

    class _FakeResponse:
        def __init__(self, body: bytes):
            self._body = body

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

        def read(self) -> bytes:
            return self._body

    body = json.dumps({"imageUrl": "https://example.com/gen.png"}).encode("utf-8")
    monkeypatch.setattr(hunyuan_image, "_resolve_api_key", lambda: "test-key")
    monkeypatch.setattr(
        hunyuan_image.urllib.request,
        "urlopen",
        lambda request, timeout=300: _FakeResponse(body),
    )

    def fake_urlretrieve(url, path):
        Path(path).write_bytes(payload)

    monkeypatch.setattr(hunyuan_image.urllib.request, "urlretrieve", fake_urlretrieve)


def _forbid_default_resolution(monkeypatch) -> None:
    """显式传值时若仍走默认落点解析，直接失败（防止隐式改道）。"""

    def _boom():
        raise AssertionError("显式传 save_dir 时不应解析默认落点")

    monkeypatch.setattr(aop, "resolve_ai_output_dir", _boom)


def test_fetch_image_lands_in_ai_dir_not_cwd(ai_dir, tmp_path, monkeypatch):
    """进程 cwd 故意设为别处：落点必须由配置决定，不再跟 cwd 走。"""
    payload = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    _patch_http(monkeypatch, payload, "image/png")
    monkeypatch.chdir(tmp_path)

    result = fetch_image("https://example.com/pic.png")

    assert not result.startswith("🚨"), result
    files = list(ai_dir.glob("download_*.png"))
    assert len(files) == 1, f"应落到 AI 目录，实际: {files}"
    assert not list(tmp_path.glob("download_*.png")), "不得再落到进程 cwd"
    assert files[0].read_bytes() == payload
    assert str(files[0]) in result


def test_hunyuan_default_save_dir_lands_in_ai_dir(ai_dir, monkeypatch):
    """未传 save_dir：混元生图真实落盘到 AI 生成内容目录。"""
    payload = b"\xff\xd8\xff\xe0" + b"\x00" * 64
    _patch_hunyuan_http(monkeypatch, payload)

    result = hunyuan_image.generate_image("一只在窗台上的猫")

    assert not result.is_error, result.content
    files = list(ai_dir.glob("hunyuan_*.jpg"))
    assert len(files) == 1, f"应落到 AI 目录，实际: {files}"
    assert files[0].read_bytes() == payload
    assert str(files[0]) in result.content
    assert result.changed_files == [str(files[0])]


def test_hunyuan_explicit_save_dir_is_respected(tmp_path, monkeypatch):
    """显式传 save_dir：原样使用，不解析默认落点、不被改道。"""
    explicit = tmp_path / "explicit"
    payload = b"\xff\xd8\xff\xe0" + b"\x00" * 64
    _patch_hunyuan_http(monkeypatch, payload)
    _forbid_default_resolution(monkeypatch)

    result = hunyuan_image.generate_image("一只在窗台上的猫", save_dir=str(explicit))

    assert not result.is_error, result.content
    files = list(explicit.glob("hunyuan_*.jpg"))
    assert len(files) == 1, f"应落到显式目录，实际: {files}"
    assert files[0].read_bytes() == payload


def test_tokenrhythm_default_save_dir_lands_in_ai_dir(ai_dir):
    """未传 save_dir：基元落盘走 _resolve_save_dir → AI 生成内容目录。"""
    payload = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32

    path = tokenrhythm_plugin._save_image_bytes(payload, None, "tokenrhythm")

    saved = Path(path)
    assert saved.parent == ai_dir.resolve(), saved
    assert saved.read_bytes() == payload
    assert re.fullmatch(r"tokenrhythm_\d{8}_\d{6}\.png", saved.name), saved.name


def test_tokenrhythm_download_lands_in_ai_dir(ai_dir, monkeypatch):
    """_download_image 未传 save_dir：先落 .part 再落 AI 目录，不留临时文件。"""
    payload = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    monkeypatch.setattr(
        tokenrhythm_plugin.urllib.request,
        "urlretrieve",
        lambda url, path: Path(path).write_bytes(payload),
    )

    path = tokenrhythm_plugin._download_image(
        "https://example.com/gen.png", None, "tokenrhythm"
    )

    saved = Path(path)
    assert saved.parent == ai_dir.resolve(), saved
    assert saved.read_bytes() == payload
    assert not list(ai_dir.glob(".tokenrhythm_*.part")), "临时文件应清理干净"


def test_tokenrhythm_explicit_save_dir_is_respected(tmp_path, monkeypatch):
    """显式传 save_dir：原样使用，不解析默认落点、不被改道。"""
    explicit = tmp_path / "explicit_tokenrhythm"
    payload = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    _forbid_default_resolution(monkeypatch)

    path = tokenrhythm_plugin._save_image_bytes(payload, str(explicit), "tokenrhythm")

    saved = Path(path)
    assert saved.parent == explicit
    assert saved.read_bytes() == payload


def test_execute_entry_defaults_are_none():
    """三个 execute 入口的 save_dir 默认值必须为 None（由落点解析接管）。"""
    entries = (
        hunyuan_image.generate_image,
        tokenrhythm_plugin.generate_image,
        tokenrhythm_plugin.edit_image,
    )
    for entry in entries:
        default = inspect.signature(entry).parameters["save_dir"].default
        assert default is None, f"{entry.__module__}.{entry.__name__} 默认值应为 None"


# --------------------------------------------------------------------------
# Task 3：AI 生成内容目录纳入沙盒写白名单（规格 6.1）
# --------------------------------------------------------------------------


def _lock_sandbox(monkeypatch) -> None:
    """锁定沙盒未开放：避免开发机真实配置（/sandbox on）让越界用例失效。"""
    monkeypatch.setattr(
        "core.model_manager.load_config", lambda: {"allow_outside_workspace": False}
    )


def test_write_file_inside_ai_dir_bypasses_sandbox(ai_dir, tmp_path, monkeypatch):
    """AI 写临时脚本到 AI 目录：属于白名单，不触发越界拒绝且真实落盘。"""
    from core import tools as tools_mod
    from core.workspace_context import workspace_scope

    _lock_sandbox(monkeypatch)
    project = tmp_path / "proj"
    project.mkdir()
    ai_dir.mkdir(parents=True, exist_ok=True)

    with workspace_scope(str(project)):
        result = tools_mod.write_file(str(ai_dir / "probe.py"), "print(1)")

    assert not str(result).startswith("🚨"), result
    assert (ai_dir / "probe.py").read_text(encoding="utf-8") == "print(1)"


def test_write_file_outside_workspace_still_blocked(ai_dir, tmp_path, monkeypatch):
    """回归保护：非白名单的越界写入仍必须被拒绝，且不得落盘。

    说明：write_file 的越界拒绝沿用项目既有契约——抛 ValueError（与
    tests/test_tools.py 的 write_file 越界断言一致），故此处断言异常消息含"越界"。
    """
    from core import tools as tools_mod
    from core.workspace_context import workspace_scope

    _lock_sandbox(monkeypatch)
    project = tmp_path / "proj"
    project.mkdir()
    outside = tmp_path / "outside.txt"

    with workspace_scope(str(project)):
        with pytest.raises(ValueError) as error:
            tools_mod.write_file(str(outside), "x")

    assert "越界" in str(error.value), str(error.value)
    assert not outside.exists(), "越界写入不得落盘"


# --------------------------------------------------------------------------
# harness 守卫层豁免（规格 6.1）：写工具访问 AI 目录不再弹越界确认
# --------------------------------------------------------------------------


def _guard_calls(monkeypatch, ai_dir, tmp_path):
    """在 workspace=proj 下调用 harness 守卫，返回 (询问记录, 结果)。

    询问记录为空表示"完全没有弹越界确认"（豁免生效）。
    """
    from core.harness.tool_adapters import _sandbox_outside_guard
    from core.workspace_context import workspace_scope

    _lock_sandbox(monkeypatch)
    project = tmp_path / "proj"
    project.mkdir()
    ai_dir.mkdir(parents=True, exist_ok=True)
    calls = []

    def checker(permission_name, payload, **kwargs):
        calls.append((permission_name, payload))
        return True

    def _run(function_name, target):
        with workspace_scope(str(project)):
            return _sandbox_outside_guard(
                function_name,
                str(target),
                None,
                "sess",
                "",
                "local",
                None,
                permission_checker=checker,
            )

    return calls, _run


def test_guard_exempts_write_tool_inside_ai_dir(ai_dir, tmp_path, monkeypatch):
    """核心缺口：写工具访问 AI 目录内的越界路径，守卫层必须直接放行、不询问。"""
    calls, run = _guard_calls(monkeypatch, ai_dir, tmp_path)

    for tool_name in ("write_file", "edit_file"):
        bypass_ctx, error = run(tool_name, ai_dir / "probe.py")
        assert error is None, f"{tool_name} 访问 AI 目录不应报错: {error}"
        assert bypass_ctx is not None, f"{tool_name} 访问 AI 目录应免确认放行"
    assert calls == [], f"AI 目录不应触发越界询问，实际询问: {calls}"


def test_guard_exempts_paths_classified_consistently(ai_dir, tmp_path, monkeypatch):
    """两层门禁一致：classify_path_access 也不得把 AI 目录判成越界（否则权限层先弹一次）。"""
    from core.harness.tool_adapters import classify_path_access
    from core.workspace_context import workspace_scope

    _lock_sandbox(monkeypatch)
    project = tmp_path / "proj"
    project.mkdir()
    ai_dir.mkdir(parents=True, exist_ok=True)

    with workspace_scope(str(project)):
        for tool_name in ("write_file", "edit_file", "read_file"):
            assert classify_path_access(tool_name, str(ai_dir / "probe.py")) == "trusted_read"


def test_guard_still_asks_for_write_outside_ai_dir(ai_dir, tmp_path, monkeypatch):
    """回归保护：非 AI 目录的越界写入（含名称相近的兄弟目录）仍必须询问。"""
    calls, run = _guard_calls(monkeypatch, ai_dir, tmp_path)
    decoy = tmp_path / "ai_out_decoy"
    decoy.mkdir()

    for tool_name, target in (
        ("write_file", tmp_path / "outside.txt"),
        ("write_file", decoy / "probe.py"),
    ):
        bypass_ctx, error = run(tool_name, target)
        assert error is None, error
        assert bypass_ctx is not None, "用户批准后应返回 bypass 上下文"
    assert len(calls) == 2, f"非白名单越界写入必须逐次询问，实际询问: {calls}"
    assert all(name == "sandbox_outside_access" for name, _ in calls)


def test_guard_denies_write_outside_ai_dir_when_rejected(ai_dir, tmp_path, monkeypatch):
    """回归保护：用户拒绝后越界写入必须被拦截（写保护不因本次改动放松）。"""
    from core.harness.tool_adapters import _sandbox_outside_guard
    from core.workspace_context import workspace_scope

    _lock_sandbox(monkeypatch)
    project = tmp_path / "proj"
    project.mkdir()
    ai_dir.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside.txt"

    with workspace_scope(str(project)):
        bypass_ctx, error = _sandbox_outside_guard(
            "write_file",
            str(outside),
            None,
            "sess",
            "",
            "local",
            None,
            permission_checker=lambda *args, **kwargs: False,
        )

    assert bypass_ctx is None
    assert error and "拒绝" in error, error
    assert not outside.exists()
