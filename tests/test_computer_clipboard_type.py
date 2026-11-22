from __future__ import annotations

import sys
import threading

import pytest

from core.computer.models import ComputerAction, ComputerControlError
from core.computer.win32_adapter import Win32InputAdapter
from core.computer.windows_backend import WindowsComputerBackend
from tests.computer_fakes import FakeImage, FakeWin32Adapter


class ClipboardRecordingAdapter(FakeWin32Adapter):

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


def test_stop_during_clipboard_set_does_not_send_paste():
    clipboard_written = threading.Event()
    allow_clipboard_call_to_finish = threading.Event()
    adapter = ClipboardRecordingAdapter()
    original_set_clipboard_text = adapter.set_clipboard_text

    def record_clipboard_text(text):
        original_set_clipboard_text(text)
        clipboard_written.set()
        assert allow_clipboard_call_to_finish.wait(timeout=1)

    adapter.set_clipboard_text = record_clipboard_text
    backend = _backend_with(adapter)
    backend.start()
    backend.observe("run-clip-stop")
    errors = []

    def type_text():
        try:
            backend.execute(
                "run-clip-stop",
                ComputerAction(kind="type_text", target="desktop", text="中文"),
            )
        except ComputerControlError as error:
            errors.append(error)

    worker = threading.Thread(target=type_text)
    worker.start()
    try:
        assert clipboard_written.wait(timeout=1)
        backend.request_stop()
    finally:
        allow_clipboard_call_to_finish.set()
        worker.join(timeout=2)
        backend.close()

    assert not worker.is_alive()
    assert len(errors) == 1 and errors[0].code == "computer_cancelled"
    assert adapter.clipboard_writes == ["中文"]
    assert adapter.keys_sent == []


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


def test_adapter_set_clipboard_text_rejects_empty():
    adapter = Win32InputAdapter()
    with pytest.raises(ComputerControlError) as raised:
        adapter.set_clipboard_text("")
    assert raised.value.code == "computer_invalid_action"


def test_adapter_set_clipboard_text_seam_injection():
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


def test_real_clipboard_write_roundtrip():
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
