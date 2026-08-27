# -*- coding: utf-8 -*-
"""带密钥的对称加解密模块。

与 core.credentials.py 的 DPAPI/keyring 方案互补：DPAPI 把密钥托管给操作系统，
本模块允许调用方显式提供口令（passphrase），由 PBKDF2-HMAC-SHA256 派生出
AES-256-GCM 密钥，适合跨机器迁移、需要口令掌控权的场景。

存储格式（全部为 ASCII，可直接写入 JSON）：
    enc-v1:<base64(salt[16] || nonce[12] || ciphertext+tag)>
- salt    ：每次加密随机生成（16 字节），保证相同口令+明文每次密文都不同
- nonce   ：每次加密随机生成（12 字节），GCM 标准要求同一密钥下 nonce 不复用
- tag     ：GCM 认证标签（16 字节），密文或密钥错误时解密必然失败
"""
from __future__ import annotations

import base64
import hashlib
import os
from typing import Optional

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ENCRYPTED_PREFIX = "enc-v1:"
SALT_SIZE = 16          # 字节
NONCE_SIZE = 12         # 字节（GCM 推荐）
KEY_SIZE = 32           # 字节，AES-256
DEFAULT_KDF_ITERATIONS = 600_000  # OWASP 2023 对 PBKDF2-HMAC-SHA256 的推荐基线
# derive_key() 省略 salt 时的常量盐（仅限便捷派生场景；正式加密一律随机盐）
_DEFAULT_KDF_SALT = b"Y-CODE-kdf-salt1"


def derive_key(
    passphrase: str,
    salt: Optional[bytes] = None,
    iterations: int = DEFAULT_KDF_ITERATIONS,
) -> bytes:
    """从口令 + 盐派生 32 字节 AES 密钥（确定性：同口令同盐必得同密钥）。

    salt 未提供时使用固定应用常量盐：密钥派生仍有确定性，但安全性弱于
    随机盐（无法抵抗彩虹表预计算），生产加密路径（encrypt/decrypt）始终显式传随机盐。
    """
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
        payload = salt + nonce + ciphertext  # ciphertext 末尾已含 GCM tag
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
            # 密钥错误或密文被篡改；不回显任何明文/密文内容
            raise ValueError("解密失败：密钥错误或数据已被篡改") from error
        return plaintext.decode("utf-8")
