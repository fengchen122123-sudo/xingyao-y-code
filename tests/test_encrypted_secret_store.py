# -*- coding: utf-8 -*-
"""core.encrypted_secret_store 的单元测试。

验证基于口令派生密钥（PBKDF2-HMAC-SHA256）的 AES-GCM 加解密：
- 相同口令可往返还原，不同口令/篡改必须失败
- 每次加密产生不同密文（随机盐 + 随机 nonce）
- 边界情况：空口令、空明文、Unicode、超长明文
"""
import base64

import pytest

from core.encrypted_secret_store import (
    DEFAULT_KDF_ITERATIONS,
    ENCRYPTED_PREFIX,
    EncryptedSecretStore,
    derive_key,
)

TEST_PASSPHRASE = "qa-encryption-key"


class TestDeriveKey:
    """密钥派生：确定性 + 雪崩效应。"""

    def test_same_passphrase_and_salt_derive_same_key(self):
        assert derive_key(TEST_PASSPHRASE) == derive_key(TEST_PASSPHRASE)

    def test_different_passphrase_derives_different_key(self):
        assert derive_key("key-a") != derive_key("key-b")

    def test_different_salt_derives_different_key(self):
        assert derive_key(TEST_PASSPHRASE, salt=b"0" * 16) != derive_key(
            TEST_PASSPHRASE, salt=b"1" * 16
        )

    def test_output_is_32_bytes_for_aes256(self):
        assert len(derive_key(TEST_PASSPHRASE)) == 32

    def test_empty_passphrase_rejected(self):
        with pytest.raises(ValueError):
            derive_key("")


class TestEncryptedSecretStoreRoundTrip:
    """核心行为：带密钥的对称加解密。"""

    def setup_method(self):
        self.store = EncryptedSecretStore(TEST_PASSPHRASE)

    def test_encrypt_decrypt_round_trip(self):
        token = self.store.encrypt("sk-my-secret-api-key-123")
        assert self.store.decrypt(token) == "sk-my-secret-api-key-123"

    def test_unicode_round_trip(self):
        secret = "密钥测试-中文与emoji🔑及换行\n第二行"
        assert self.store.decrypt(self.store.encrypt(secret)) == secret

    def test_long_plaintext_round_trip(self):
        secret = "x" * 10000
        assert self.store.decrypt(self.store.encrypt(secret)) == secret

    def test_token_format_is_prefix_plus_base64(self):
        token = self.store.encrypt("hello")
        assert token.startswith(ENCRYPTED_PREFIX)
        payload = token[len(ENCRYPTED_PREFIX):]
        # 必须是合法 ASCII Base64
        raw = base64.b64decode(payload, validate=True)
        # 内部布局：salt(16) + nonce(12) + ciphertext+tag(至少 16)
        assert len(raw) >= 16 + 12 + 16

    def test_each_encryption_produces_different_ciphertext(self):
        token_a = self.store.encrypt("same-plaintext")
        token_b = self.store.encrypt("same-plaintext")
        assert token_a != token_b


class TestDecryptionFailures:
    """解密必须能识别错误密钥与被篡改的数据。"""

    def setup_method(self):
        self.store = EncryptedSecretStore(TEST_PASSPHRASE)

    def test_wrong_key_fails(self):
        token = self.store.encrypt("secret")
        other = EncryptedSecretStore("wrong-passphrase")
        with pytest.raises(ValueError):
            other.decrypt(token)

    def test_tampered_ciphertext_fails(self):
        token = self.store.encrypt("secret")
        raw = bytearray(base64.b64decode(token[len(ENCRYPTED_PREFIX):]))
        # 破坏密文区（salt 16 + nonce 12 之后），触发 GCM 认证标签校验失败
        raw[-1] ^= 0xFF
        tampered = ENCRYPTED_PREFIX + base64.b64encode(bytes(raw)).decode("ascii")
        with pytest.raises(ValueError):
            self.store.decrypt(tampered)

    def test_garbage_input_fails(self):
        with pytest.raises(ValueError):
            self.store.decrypt("not-a-valid-token")

    def test_plain_text_without_prefix_rejected(self):
        with pytest.raises(ValueError):
            self.store.decrypt("sk-raw-unencrypted-value")

    def test_truncated_payload_fails(self):
        with pytest.raises(ValueError):
            self.store.decrypt(ENCRYPTED_PREFIX + base64.b64encode(b"short").decode())

    def test_empty_plaintext_rejected_on_encrypt(self):
        with pytest.raises(ValueError):
            self.store.encrypt("")

    def test_wrong_tamper_does_not_leak_plaintext_in_error(self):
        token = self.store.encrypt("super-secret-value")
        raw = bytearray(base64.b64decode(token[len(ENCRYPTED_PREFIX):]))
        raw[-5] ^= 0x55
        tampered = ENCRYPTED_PREFIX + base64.b64encode(bytes(raw)).decode("ascii")
        with pytest.raises(ValueError) as exc_info:
            self.store.decrypt(tampered)
        assert "super-secret-value" not in str(exc_info.value)


class TestHelpers:
    def test_is_encrypted_true_for_prefixed(self):
        store = EncryptedSecretStore(TEST_PASSPHRASE)
        assert EncryptedSecretStore.is_encrypted(store.encrypt("abc")) is True

    def test_is_encrypted_false_for_raw_and_empty(self):
        assert EncryptedSecretStore.is_encrypted("raw-key") is False
        assert EncryptedSecretStore.is_encrypted("") is False

    def test_kdf_iterations_constant_is_hardened(self):
        # 行业基线为 OWASP 推荐的 600000 次，防止有人手滑调回弱值
        assert DEFAULT_KDF_ITERATIONS >= 600_000
