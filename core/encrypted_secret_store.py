# -*- coding: utf-8 -*-
from __future__ import annotations

import base64
import hashlib
import os
from typing import Optional

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ENCRYPTED_PREFIX = "enc-v1:"
SALT_SIZE = 16
NONCE_SIZE = 12
KEY_SIZE = 32
DEFAULT_KDF_ITERATIONS = 600_000
_DEFAULT_KDF_SALT = b"Y-CODE-kdf-salt1"


def derive_key(
    passphrase: str,
    salt: Optional[bytes] = None,
    iterations: int = DEFAULT_KDF_ITERATIONS,
) -> bytes:
    if salt is None:
        salt = _DEFAULT_KDF_SALT
    if not isinstance(salt, (bytes, bytearray)) or len(salt) != SALT_SIZE:
        raise ValueError(f"salt 必须为 {SALT_SIZE} 字节")
    if not passphrase:
        raise ValueError("passphrase 不能为空")
    return hashlib.pbkdf2_hmac(
        "sha256", passphrase.encode("utf-8"), bytes(salt), iterations, dklen=KEY_SIZE
    )


class EncryptedSecretStore:
    """口令加密存储：encrypt 产出带前缀的 Base64 密文，decrypt 还原明文。"""

    def __init__(self, passphrase: str, iterations: int = DEFAULT_KDF_ITERATIONS):
        if not passphrase:
            raise ValueError("passphrase 不能为空")
        self._passphrase = passphrase
        self._iterations = iterations

    @staticmethod
    def is_encrypted(value: str) -> bool:
        """判断字符串是否为本模块产生的密文格式。"""
        return isinstance(value, str) and value.startswith(ENCRYPTED_PREFIX)

    def encrypt(self, plaintext: str) -> str:
        """加密明文，返回 enc-v1: 前缀的 Base64 密文。每次调用密文都不同。"""
        if not plaintext:
            raise ValueError("待加密内容不能为空")
        salt = os.urandom(SALT_SIZE)
        nonce = os.urandom(NONCE_SIZE)
        key = derive_key(self._passphrase, salt, self._iterations)
        ciphertext = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), None)
        payload = salt + nonce + ciphertext
        return ENCRYPTED_PREFIX + base64.b64encode(payload).decode("ascii")

    def decrypt(self, token: str) -> str:
        """解密 enc-v1: 密文；口令错误、密文被篡改或格式非法时抛 ValueError。"""
        if not self.is_encrypted(token):
            raise ValueError("不是有效的 enc-v1 加密格式（缺少前缀）")
        try:
            payload = base64.b64decode(token[len(ENCRYPTED_PREFIX):], validate=True)
        except Exception as error:
            raise ValueError("密文不是合法的 Base64 数据") from error
        if len(payload) < SALT_SIZE + NONCE_SIZE + 16:
            raise ValueError("密文长度不足，数据可能被截断")
        salt = payload[:SALT_SIZE]
        nonce = payload[SALT_SIZE:SALT_SIZE + NONCE_SIZE]
        ciphertext = payload[SALT_SIZE + NONCE_SIZE:]
        key = derive_key(self._passphrase, salt, self._iterations)
        try:
            plaintext = AESGCM(key).decrypt(nonce, ciphertext, None)
        except InvalidTag as error:
            raise ValueError("解密失败：密钥错误或数据已被篡改") from error
        return plaintext.decode("utf-8")
