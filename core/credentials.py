"""Windows DPAPI helpers for user-bound credential encryption at rest."""

from __future__ import annotations

import base64
import ctypes
import hashlib
import sys
from ctypes import wintypes

DPAPI_PREFIX = "dpapi:"
KEYRING_PREFIX = "keyring:"
KEYRING_SERVICE = "Y-CODE API Keys"
CRYPTPROTECT_UI_FORBIDDEN = 0x1


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _crypt32():
    if sys.platform != "win32":
        raise RuntimeError("API Key 安全存储需要 Windows DPAPI")
    library = ctypes.windll.crypt32
    library.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob), wintypes.LPCWSTR, ctypes.POINTER(_DataBlob),
        wintypes.LPVOID, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(_DataBlob),
    ]
    library.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob), ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(_DataBlob),
        wintypes.LPVOID, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(_DataBlob),
    ]
    return library


def _blob(data: bytes):
    buffer = ctypes.create_string_buffer(data)
    return _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))), buffer


def protect_secret(value: str) -> str:
    text = str(value or "")
    if not text or text.startswith((DPAPI_PREFIX, KEYRING_PREFIX)):
        return text
    if sys.platform != "win32":
        import keyring

        account = hashlib.sha256(text.encode("utf-8")).hexdigest()
        keyring.set_password(KEYRING_SERVICE, account, text)
        return KEYRING_PREFIX + account
    source, source_buffer = _blob(text.encode("utf-8"))
    output = _DataBlob()
    if not _crypt32().CryptProtectData(
        ctypes.byref(source), "Y-CODE API Key", None, None, None,
        CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(output),
    ):
        raise ctypes.WinError()
    try:
        encrypted = ctypes.string_at(output.pbData, output.cbData)
        return DPAPI_PREFIX + base64.b64encode(encrypted).decode("ascii")
    finally:
        ctypes.windll.kernel32.LocalFree(output.pbData)
        del source_buffer


def unprotect_secret(value: str) -> str:
    text = str(value or "")
    if text.startswith(KEYRING_PREFIX):
        import keyring

        account = text[len(KEYRING_PREFIX):]
        secret = keyring.get_password(KEYRING_SERVICE, account)
        if secret is None:
            raise RuntimeError("系统凭据存储中缺少对应的 Y-CODE API Key")
        return secret
    if not text or not text.startswith(DPAPI_PREFIX):
        return text
    encrypted = base64.b64decode(text[len(DPAPI_PREFIX):], validate=True)
    source, source_buffer = _blob(encrypted)
    output = _DataBlob()
    if not _crypt32().CryptUnprotectData(
        ctypes.byref(source), None, None, None, None,
        CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(output),
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(output.pbData, output.cbData).decode("utf-8")
    finally:
        ctypes.windll.kernel32.LocalFree(output.pbData)
        del source_buffer
