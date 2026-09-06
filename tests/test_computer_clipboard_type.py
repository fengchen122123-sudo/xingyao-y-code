"""type_text 非 ASCII 剪贴板路径（2026-09-06，提速计划 Task 4）。"""

from __future__ import annotations

import sys

import pytest

from core.computer.models import ComputerAction, ComputerControlError
from core.computer.win32_adapter import Win32InputAdapter
from core.computer.windows_backend import WindowsComputerBackend
from tests.computer_fakes import FakeImage, FakeWin32Adapter


class ClipboardRecordingAdapter(FakeWin32Adapter):
    """在 FakeWin32Adapter 上追加剪贴板写入记录。"""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.clipboard_writes = []
        self.clipboard_error = None

    def set_clipboard_text(self, text):
        if self.clipboard_error is not None:
            raise self.clipboard_error
        self.clipboard_writes.append(text)


def _backend_with(adapter):
    return WindowsComputerBackend(
        image_grab=lambda: FakeImage(width=3840, height=2160, origin_x=-1920, origin_y=0),
        win32_adapter=adapter,
    )


def test_ascii_text_keeps_send_text():
    adapter = ClipboardRecordingAdapter()
    backend = _backend_with(adapter)
    backend.start()
    run_id = "run-clip1"
    backend.observe(run_id)
    backend.execute(run_id, ComputerAction(kind="type_text", target="desktop", text="hello"))
    assert adapter.texts == ["hello"]
    assert adapter.clipboard_writes == []
    backend.close()


def test_chinese_text_goes_through_clipboard_paste():
    adapter = ClipboardRecordingAdapter()
    backend = _backend_with(adapter)
    backend.start()
    run_id = "run-clip2"
    backend.observe(run_id)
    message = "大家好，我是星瑶（Y-code）我在测试我的电脑操控功能"
    backend.execute(run_id, ComputerAction(kind="type_text", target="desktop", text=message))
    assert adapter.clipboard_writes == [message]
    assert ("CTRL", "V") in [tuple(k) for k in adapter.keys_sent]
    assert adapter.texts == []
    backend.close()


def test_clipboard_failure_raises_stable_error():
    adapter = ClipboardRecordingAdapter()
    adapter.clipboard_error = ComputerControlError("computer_backend_error", "剪贴板被占用")
    backend = _backend_with(adapter)
    backend.start()
    run_id = "run-clip3"
    backend.observe(run_id)
    with pytest.raises(ComputerControlError) as raised:
        backend.execute(
            run_id, ComputerAction(kind="type_text", target="desktop", text="中文")
        )
    assert raised.value.code == "computer_backend_error"
    backend.close()


# ---------------- Win32InputAdapter.set_clipboard_text 真实 seam ----------------


def test_adapter_set_clipboard_text_rejects_empty():
    adapter = Win32InputAdapter()
    with pytest.raises(ComputerControlError) as raised:
        adapter.set_clipboard_text("")
    assert raised.value.code == "computer_invalid_action"


def test_adapter_set_clipboard_text_seam_injection():
    """seam 注入路径：不触碰真实剪贴板，验证成功与失败两条分支。"""
    calls = []

    def _ok_set(text):
        calls.append(text)

    adapter = Win32InputAdapter(set_clipboard_text=_ok_set)
    adapter.set_clipboard_text("你好")
    assert calls == ["你好"]

    def _fail_set(text):
        raise OSError("剪贴板锁死")

    adapter2 = Win32InputAdapter(set_clipboard_text=_fail_set)
    with pytest.raises(ComputerControlError) as raised:
        adapter2.set_clipboard_text("你好")
    assert raised.value.code == "computer_backend_error"


# ---------------- 真实剪贴板功能回归（会话 156 缺陷防护） ----------------


def test_real_clipboard_write_roundtrip():
    """真机功能回归：ctypes 函数归属错误（EmptyClipboard/SetClipboardData 在
    user32 不在 kernel32）与 64 位句柄截断（缺 restype 声明）都会在此暴露。

    写入哨兵文本后读回校验，并把剪贴板复位为哨兵确认文本（不尝试恢复
    原内容的读取——读取本身依赖同一段签名代码，读不到原内容时不可靠）。
    """
    import ctypes
    from ctypes import wintypes

    if sys.platform != "win32":
        pytest.skip("仅 Windows")

    from core.computer.win32_adapter import _ctypes_set_clipboard_text

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    CF_UNICODETEXT = 13
    user32.OpenClipboard.argtypes = (wintypes.HWND,)
    user32.GetClipboardData.restype = ctypes.c_void_p
    user32.GetClipboardData.argtypes = (wintypes.UINT,)
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = (ctypes.c_void_p,)
    kernel32.GlobalUnlock.argtypes = (ctypes.c_void_p,)

    def read_clip():
        value = None
        if user32.OpenClipboard(0):
            try:
                handle = user32.GetClipboardData(CF_UNICODETEXT)
                if handle:
                    pointer = kernel32.GlobalLock(handle)
                    if pointer:
                        value = ctypes.c_wchar_p(pointer).value
                        kernel32.GlobalUnlock(handle)
            finally:
                user32.CloseClipboard()
        return value

    sentinel = "ycode-clipboard-regression-星瑶"
    _ctypes_set_clipboard_text(sentinel)
    assert read_clip() == sentinel
