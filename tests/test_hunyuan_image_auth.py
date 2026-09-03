"""混元生图客户端鉴权边界测试。"""

import json
import os
import tempfile
from unittest import TestCase, mock

from core import hunyuan_image


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


class GenerateImageAuthTest(TestCase):
    def test_generate_image_uses_local_server_key_from_environment(self):
        """恢复网关调用：管理员 Key 仅从本机环境变量读取，不进入源码。"""
        captured = {}

        def fake_urlopen(request, timeout):
            captured["authorization"] = request.get_header("Authorization")
            return _Response({"imageUrl": "https://example.invalid/generated.jpg"})

        def fake_urlretrieve(url, target):
            with open(target, "wb") as image_file:
                image_file.write(b"\xff\xd8\xff\xe0")

        with tempfile.TemporaryDirectory() as save_dir:
            with mock.patch.dict(
                os.environ,
                {"HUNYUAN_API_KEY": "legacy-server-key-must-not-be-sent"},
            ), mock.patch.object(
                hunyuan_image.urllib.request, "urlopen", fake_urlopen
            ), mock.patch.object(
                hunyuan_image.urllib.request, "urlretrieve", fake_urlretrieve
            ):
                result = hunyuan_image.generate_image("一只猫", save_dir=save_dir)

        self.assertFalse(result.is_error)
        self.assertEqual(captured["authorization"], "Bearer legacy-server-key-must-not-be-sent")

    def test_resolve_api_key_prefers_environment(self):
        """环境变量优先：已设置时直接返回环境变量值。"""
        with mock.patch.dict(os.environ, {"HUNYUAN_API_KEY": "  env-key  "}):
            self.assertEqual(hunyuan_image._resolve_api_key(), "env-key")

    def test_generate_image_errors_without_environment(self):
        """未配置环境变量时直接明确报错，不再回退内置 Key。"""
        env = {k: v for k, v in os.environ.items() if k != "HUNYUAN_API_KEY"}
        with mock.patch.dict(os.environ, env, clear=True):
            result = hunyuan_image.generate_image("一只猫")

        self.assertTrue(result.is_error)
        self.assertIn("HUNYUAN_API_KEY", result.content)
