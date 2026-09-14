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

# 应用级额外熵（pOptionalEntropy）：把密文绑定到 Y-CODE 自身，同一 Windows 用户
# 下的其它进程仅用裸 DPAPI 无法解密本应用保存的密钥。
# 迁移策略：写入一律带 entropy（新格式）；读取先尝试新格式，失败后回退无
# entropy 的旧格式，保证升级前保存的密钥仍可解密（protect/unprotect 接口签名不变）。
_DPAPI_ENTROPY = b"Y-CODE-DPAPI-Entropy-v1"


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
    entropy, entropy_buffer = _blob(_DPAPI_ENTROPY)
    output = _DataBlob()
    if not _crypt32().CryptProtectData(
        ctypes.byref(source), "Y-CODE API Key", ctypes.byref(entropy), None, None,
        CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(output),
    ):
        raise ctypes.WinError()
    try:
        encrypted = ctypes.string_at(output.pbData, output.cbData)
        return DPAPI_PREFIX + base64.b64encode(encrypted).decode("ascii")
    finally:
        ctypes.windll.kernel32.LocalFree(output.pbData)
        del source_buffer, entropy_buffer


def _unprotect_dpapi(encrypted: bytes, use_entropy: bool) -> str:
    source, source_buffer = _blob(encrypted)
    entropy, entropy_buffer = (None, None)
    if use_entropy:
        entropy, entropy_buffer = _blob(_DPAPI_ENTROPY)
    output = _DataBlob()
    try:
        if not _crypt32().CryptUnprotectData(
            ctypes.byref(source), None,
            ctypes.byref(entropy) if entropy is not None else None,
            None, None,
            CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(output),
        ):
            raise ctypes.WinError()
        try:
            return ctypes.string_at(output.pbData, output.cbData).decode("utf-8")
        finally:
            ctypes.windll.kernel32.LocalFree(output.pbData)
    finally:
        del source_buffer
        if entropy_buffer is not None:
            del entropy_buffer


def unprotect_secret(value: str) -> str:
    text = str(value or "")
    if text.startswith(KEYRING_PREFIX):
        import keyring

        account = text[len(KEYRING_PREFIX):]
        secret = keyring.get_password(KEYRING_SERVICE, account)
        if secret is None:
            raise RuntimeError("系统凭据存储中缺少对应的 星瑶Code API Key")
        return secret
    if not text or not text.startswith(DPAPI_PREFIX):
        return text
    encrypted = base64.b64decode(text[len(DPAPI_PREFIX):], validate=True)
    try:
        return _unprotect_dpapi(encrypted, use_entropy=True)
    except OSError:
        # 旧格式（无额外熵）密文回退：保证升级前保存的密钥仍可解密
        return _unprotect_dpapi(encrypted, use_entropy=False)
