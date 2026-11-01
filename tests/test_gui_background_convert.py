"""背景图转换（视频 → 动画 WebP）桥接层测试。

覆盖 gui_bridge 中随包内置 ffmpeg 的移植实现：
- _find_ffmpeg 的查找优先级（内置副本 → PATH → 常见安装位置）
- _probe_video_meta 的 stderr 解析（不依赖 ffprobe）
- describe_background_video 的格式/体积/ffmpeg 缺失判定
- start_background_convert 的预检与 _background_convert_worker 的失败清理
- install_converted_background 在无激活主题时的明确提示

所有用例都不真的调用 ffmpeg（CI 上子进程很慢），一律 monkeypatch。
"""

import sys

import pytest

import core.gui_theme as gui_theme_module
import gui_bridge
from core.gui_theme import ThemeError
from gui_bridge import ApiBridge

FFMPEG_EXE = "ffmpeg.exe"


def _bare_bridge():
    """不经 __init__ 构造裸实例，避免启动期的重初始化副作用。"""
    bridge = ApiBridge.__new__(ApiBridge)
    bridge._hwnd = 0
    bridge._window = None
    return bridge


# ---------------------------------------------------------------------------
# _find_ffmpeg：内置副本优先
# ---------------------------------------------------------------------------


def _make_bundled_ffmpeg(root):
    target = root / "vendor" / "ffmpeg" / FFMPEG_EXE
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"stub-ffmpeg")
    return target


class _NoPath:
    """把 Path(...) 换成永不存在的假路径，用于屏蔽本机真实安装位置。"""

    def __init__(self, *args, **kwargs):
        pass

    def resolve(self):
        return self

    @property
    def parent(self):
        return self

    def __truediv__(self, other):
        return self

    def is_file(self):
        return False


def test_find_ffmpeg_prefers_meipass_bundled_copy(tmp_path, monkeypatch):
    """打包运行：_MEIPASS/vendor/ffmpeg/ffmpeg.exe 优先于系统 PATH。"""
    bundled = _make_bundled_ffmpeg(tmp_path / "meipass")
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "meipass"), raising=False)
    monkeypatch.setattr(gui_bridge.shutil, "which", lambda name: r"C:\system\ffmpeg.exe")

    assert _bare_bridge()._find_ffmpeg() == str(bundled)


def test_find_ffmpeg_prefers_source_tree_copy(tmp_path, monkeypatch):
    """源码运行：项目根 vendor/ffmpeg/ffmpeg.exe 优先，且不被 cwd 抢走。"""
    project = tmp_path / "project"
    project.mkdir()
    bundled = _make_bundled_ffmpeg(project)
    other_cwd = tmp_path / "other"
    other_cwd.mkdir()

    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    monkeypatch.setattr(gui_bridge, "__file__", str(project / "gui_bridge.py"))
    monkeypatch.setattr(gui_bridge.os, "getcwd", lambda: str(other_cwd))
    monkeypatch.setattr(gui_bridge.shutil, "which", lambda name: r"C:\system\ffmpeg.exe")

    assert _bare_bridge()._find_ffmpeg() == str(bundled)


def test_find_ffmpeg_falls_back_to_path(tmp_path, monkeypatch):
    """内置副本缺失 → 回退 shutil.which("ffmpeg")。"""
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    monkeypatch.setattr(gui_bridge, "__file__", str(project / "gui_bridge.py"))
    monkeypatch.setattr(gui_bridge.os, "getcwd", lambda: str(tmp_path))
    monkeypatch.setattr(gui_bridge.shutil, "which", lambda name: r"C:\tools\ffmpeg.exe")

    assert _bare_bridge()._find_ffmpeg() == r"C:\tools\ffmpeg.exe"



def test_find_ffmpeg_prefers_component_dir(tmp_path, monkeypatch):
    """组件安装目录（<COMPONENT_ROOT>/ffmpeg/ffmpeg.exe）优先于系统 PATH。

    按需组件下载后 ffmpeg 落在用户目录，必须能被 _find_ffmpeg 找到；COMPONENT_ROOT
    用 monkeypatch 改到 tmp_path，绝不往真实用户目录写文件。
    """
    from core import components

    root = tmp_path / "components"
    installed = root / "ffmpeg" / FFMPEG_EXE
    installed.parent.mkdir(parents=True, exist_ok=True)
    installed.write_bytes(b"stub-ffmpeg")

    monkeypatch.delenv("YCODE_COMPONENT_DIR", raising=False)
    monkeypatch.setattr(components, "COMPONENT_ROOT", str(root))
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(gui_bridge, "__file__", str(project / "gui_bridge.py"))
    # cwd 下没有内置副本，保证命中的只能是组件目录
    other_cwd = tmp_path / "other"
    other_cwd.mkdir()
    monkeypatch.setattr(gui_bridge.os, "getcwd", lambda: str(other_cwd))
    monkeypatch.setattr(gui_bridge.shutil, "which", lambda name: r"C:\tools\ffmpeg.exe")

    assert _bare_bridge()._find_ffmpeg() == str(installed)

def test_find_ffmpeg_returns_none_when_absent(tmp_path, monkeypatch):
    """内置副本、PATH、常见安装位置全缺失 → None（不静默回退到假路径）。"""
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    monkeypatch.setattr(gui_bridge, "Path", _NoPath)
    monkeypatch.setattr(gui_bridge.shutil, "which", lambda name: None)

    assert _bare_bridge()._find_ffmpeg() is None


# ---------------------------------------------------------------------------
# _probe_video_meta：解析 ffmpeg -i 的 stderr
# ---------------------------------------------------------------------------

_PROBE_STDERR = """ffmpeg version 8.1-essentials_build-www.gyan.dev
Input #0, mov,mp4,m4a,3gp,3g2,mj2, from 'loop.mp4':
  Metadata:
    encoder         : Lavf62.12.100
  Duration: 00:00:04.20, start: 0.000000, bitrate: 1200 kb/s
  Stream #0:0[0x1](und): Video: h264 (High) (avc1 / 0x31637661), yuv420p(tv, progressive), 1080x1920 [SAR 1:1 DAR 9:16], 30 fps, 30 tbr, 15360 tbn
At least one output file must be specified
"""


class _FakeCompleted:
    def __init__(self, stderr=""):
        self.stderr = stderr
        self.stdout = ""
        self.returncode = 1


def test_probe_video_meta_parses_duration_and_size(monkeypatch):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        return _FakeCompleted(_PROBE_STDERR)

    monkeypatch.setattr(gui_bridge.subprocess, "run", fake_run)

    meta = ApiBridge._probe_video_meta(r"C:\ffmpeg.exe", r"D:\clip\loop.mp4")

    assert meta == {"duration": pytest.approx(4.2), "width": 1080, "height": 1920}
    # 只用 `ffmpeg -i`，不依赖 ffprobe
    assert seen["cmd"][1:3] == ["-hide_banner", "-i"]


def test_probe_video_meta_tolerates_unparsable_output(monkeypatch):
    monkeypatch.setattr(
        gui_bridge.subprocess, "run",
        lambda cmd, **kwargs: _FakeCompleted("no metadata here"),
    )

    assert ApiBridge._probe_video_meta("ffmpeg", "x.mp4") == {
        "duration": None, "width": None, "height": None,
    }


def test_probe_video_meta_swallows_subprocess_errors(monkeypatch):
    def boom(cmd, **kwargs):
        raise OSError("ffmpeg exploded")

    monkeypatch.setattr(gui_bridge.subprocess, "run", boom)

    assert ApiBridge._probe_video_meta("ffmpeg", "x.mp4")["duration"] is None


# ---------------------------------------------------------------------------
# describe_background_video：格式 / 体积上限 / ffmpeg 缺失
# ---------------------------------------------------------------------------


def test_describe_rejects_missing_file(tmp_path):
    with pytest.raises(ThemeError) as excinfo:
        _bare_bridge().describe_background_video(str(tmp_path / "nope.mp4"))

    assert excinfo.value.code == "invalid_video"


def test_describe_rejects_unsupported_extension(tmp_path):
    source = tmp_path / "clip.txt"
    source.write_bytes(b"not a video")

    with pytest.raises(ThemeError) as excinfo:
        _bare_bridge().describe_background_video(str(source))

    assert excinfo.value.code == "unsupported_video"
    assert "mp4" in excinfo.value.message


def test_describe_reports_blocker_when_ffmpeg_missing(tmp_path, monkeypatch):
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"0" * 4096)
    bridge = _bare_bridge()
    monkeypatch.setattr(bridge, "_find_ffmpeg", lambda: None)

    info = bridge.describe_background_video(str(source))

    assert info["can_convert"] is False
    assert info["ffmpeg_available"] is False
    assert info["blockers"] and "ffmpeg" in info["blockers"][0]
    # 上限与 core/gui_theme 的图片校验同源
    assert info["max_bytes"] == gui_theme_module._MAX_IMAGE_BYTES
    assert info["max_dimension"] == gui_theme_module._MAX_IMAGE_DIMENSION
    assert info["bytes"] == 4096


def test_describe_flags_oversized_source_bytes(tmp_path, monkeypatch):
    """源视频超过上限时仍可尝试转换，但信息里带着同一套上限供前端提示。"""
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"0" * 128)
    bridge = _bare_bridge()
    monkeypatch.setattr(bridge, "_find_ffmpeg", lambda: r"C:\ffmpeg.exe")
    monkeypatch.setattr(
        bridge, "_background_convert_limits",
        lambda: {"max_bytes": 64, "max_dimension": 8192},
    )
    monkeypatch.setattr(
        ApiBridge, "_probe_video_meta",
        staticmethod(lambda ffmpeg, path: {"duration": 3.0, "width": 640, "height": 360}),
    )

    info = bridge.describe_background_video(str(source))

    assert info["can_convert"] is True
    assert info["bytes"] > info["max_bytes"]
    assert info["duration"] == 3.0
    assert info["width"] == 640


# ---------------------------------------------------------------------------
# start_background_convert 预检 + worker 失败清理
# ---------------------------------------------------------------------------


def test_start_convert_refuses_without_ffmpeg(monkeypatch, tmp_path):
    bridge = _bare_bridge()
    monkeypatch.setattr(bridge, "_find_ffmpeg", lambda: None)

    result = bridge.start_background_convert(str(tmp_path / "a.mp4"), str(tmp_path))

    assert result["ok"] is False
    assert "ffmpeg" in result["msg"]


def test_start_convert_refuses_second_job(tmp_path, monkeypatch):
    bridge = _bare_bridge()
    bridge._bg_convert_running = True
    monkeypatch.setattr(bridge, "_find_ffmpeg", lambda: r"C:\ffmpeg.exe")

    result = bridge.start_background_convert(str(tmp_path / "a.mp4"), str(tmp_path))

    assert result["ok"] is False
    assert "进行中" in result["msg"]


class _FakeProc:
    """假 ffmpeg 子进程：stdout 逐行喂给 worker，wait() 返回指定退出码。"""

    def __init__(self, lines=(), returncode=0, on_wait=None):
        self.stdout = list(lines)
        self.returncode = returncode
        self._on_wait = on_wait
        self.terminated = False

    def wait(self):
        if self._on_wait is not None:
            self._on_wait()
        return self.returncode

    def terminate(self):
        self.terminated = True


def _prepare_worker(bridge, monkeypatch, events, temp_dir, max_bytes):
    monkeypatch.setattr(gui_bridge.tempfile, "mkdtemp", lambda prefix="": str(temp_dir))
    monkeypatch.setattr(
        bridge, "_background_convert_limits",
        lambda: {"max_bytes": max_bytes, "max_dimension": 8192},
    )
    monkeypatch.setattr(bridge, "emit_event", lambda event_type, data: events.append((event_type, data)))
    for index in range(1, 4):
        (temp_dir / f"f-{index:05d}.png").write_bytes(b"frame")


def test_worker_deletes_oversized_output(tmp_path, monkeypatch):
    """转换结果超上限：删除半成品 + 提示降低帧率/质量。"""
    temp_dir = tmp_path / "frames"
    temp_dir.mkdir()
    output = tmp_path / "out.webp"
    bridge = _bare_bridge()
    events = []
    _prepare_worker(bridge, monkeypatch, events, temp_dir, max_bytes=100)

    def fake_popen(cmd, **kwargs):
        if "libwebp_anim" in cmd:
            output.write_bytes(b"W" * 4096)  # 远超上限
            return _FakeProc(lines=["writing..."], returncode=0)
        return _FakeProc(lines=["frame=1", "frame=2", "frame=3"], returncode=0)

    monkeypatch.setattr(gui_bridge.subprocess, "Popen", fake_popen)

    bridge._background_convert_worker(
        r"C:\ffmpeg.exe", str(tmp_path / "clip.mp4"), str(output),
        12, 78, True, {"duration": 1.0, "width": 640, "height": 360},
    )

    assert output.exists() is False
    kinds = [kind for kind, _ in events]
    assert kinds[-1] == "bg_convert_error"
    message = events[-1][1]["message"]
    assert "超过背景图上限" in message
    assert "帧率" in message and "质量" in message
    # 临时帧目录必须被清理
    assert not temp_dir.exists()


def test_worker_deletes_partial_output_when_assembly_fails(tmp_path, monkeypatch):
    """合成阶段 ffmpeg 失败：写出的半个容器要被删掉，用户不会拿到坏文件。"""
    temp_dir = tmp_path / "frames"
    temp_dir.mkdir()
    output = tmp_path / "out.webp"
    bridge = _bare_bridge()
    events = []
    _prepare_worker(bridge, monkeypatch, events, temp_dir, max_bytes=10 * 1024 * 1024)

    def fake_popen(cmd, **kwargs):
        if "libwebp_anim" in cmd:
            output.write_bytes(b"half-written")
            return _FakeProc(lines=["libwebp_anim: invalid argument"], returncode=1)
        return _FakeProc(lines=["frame=1"], returncode=0)

    monkeypatch.setattr(gui_bridge.subprocess, "Popen", fake_popen)

    bridge._background_convert_worker(
        r"C:\ffmpeg.exe", str(tmp_path / "clip.mp4"), str(output),
        12, 78, False, {"duration": 1.0, "width": 640, "height": 360},
    )

    assert output.exists() is False
    assert events[-1][0] == "bg_convert_error"
    assert "合成动画失败" in events[-1][1]["message"]


def test_worker_reports_done_and_progress_pipeline(tmp_path, monkeypatch):
    """正常路径：进度事件 0→82→88→100，最后 done 带体积与帧数。"""
    temp_dir = tmp_path / "frames"
    temp_dir.mkdir()
    output = tmp_path / "out.webp"
    bridge = _bare_bridge()
    events = []
    _prepare_worker(bridge, monkeypatch, events, temp_dir, max_bytes=10 * 1024 * 1024)

    def fake_popen(cmd, **kwargs):
        if "libwebp_anim" in cmd:
            output.write_bytes(b"W" * 512)
            return _FakeProc(returncode=0)
        return _FakeProc(lines=["frame=1", "frame=2", "frame=3"], returncode=0)

    monkeypatch.setattr(gui_bridge.subprocess, "Popen", fake_popen)

    bridge._background_convert_worker(
        r"C:\ffmpeg.exe", str(tmp_path / "clip.mp4"), str(output),
        12, 78, True, {"duration": 1.0, "width": 640, "height": 360},
    )

    stages = [data.get("stage") for kind, data in events if kind == "bg_convert_progress"]
    assert stages[0] == "extracting"
    assert "assembling" in stages and "encoding" in stages
    assert stages[-1] == "done"
    assert events[-1][0] == "bg_convert_done"
    assert events[-1][1]["bytes"] == 512
    # 乒乓：3 帧正放 + 倒放去掉两个折点重复帧 = 4 帧
    assert events[-1][1]["frames"] == 4


# ---------------------------------------------------------------------------
# install_converted_background：无激活主题与正常安装
# ---------------------------------------------------------------------------


class _RepoWithoutActive:
    def get_active(self):
        return {}

    def import_image(self, theme_id, region, source):  # pragma: no cover - 不应被调用
        raise AssertionError("无激活主题时不得写入资源")


class _RepoWithActive:
    def __init__(self, theme_id, asset_path="assets/abc.webp"):
        self.theme_id = theme_id
        self.asset_path = asset_path
        self.calls = []

    def get_active(self):
        return {"id": self.theme_id}

    def import_image(self, theme_id, region, source):
        self.calls.append((theme_id, region, str(source)))
        return self.asset_path


def test_install_without_active_theme_reports_clear_message(monkeypatch):
    monkeypatch.setattr(gui_bridge, "get_gui_theme_repository", lambda: _RepoWithoutActive())

    result = _bare_bridge().install_converted_background(r"D:\out\bg.webp")

    assert result["ok"] is False
    assert result["error"]["code"] == "no_active_theme"
    assert result["error"]["message"] == "当前没有激活的主题"


def test_install_uses_get_active_id_and_app_region(tmp_path, monkeypatch):
    webp = tmp_path / "bg.webp"
    webp.write_bytes(b"RIFF0000WEBPVP8X")
    repo = _RepoWithActive("rose")
    monkeypatch.setattr(gui_bridge, "get_gui_theme_repository", lambda: repo)

    result = _bare_bridge().install_converted_background(str(webp))

    assert result == {"ok": True, "theme_id": "rose", "asset_path": "assets/abc.webp"}
    assert repo.calls == [("rose", "app", str(webp))]


def test_install_rejects_path_when_repository_raises(monkeypatch):
    """仓库抛 ThemeError（例如 webp 损坏）时被结构化信封兜住，不穿出桥接层。"""
    class _RepoBroken(_RepoWithActive):
        def import_image(self, theme_id, region, source):
            raise ThemeError("invalid_image", "source", "图片内容无法解析")

    monkeypatch.setattr(gui_bridge, "get_gui_theme_repository", lambda: _RepoBroken("rose"))

    result = _bare_bridge().install_converted_background(r"D:\out\bg.webp")

    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_image"
