# -*- coding: utf-8 -*-

import importlib.util
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from core.tool_protocol import ToolResult

_PLUGIN_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "plugins", "tokenrhythm_image",
)
_PLUGIN_ENTRY = os.path.join(_PLUGIN_DIR, "plugin.py")


def _load_plugin():
    spec = importlib.util.spec_from_file_location("_test_tokenrhythm_image_plugin", _PLUGIN_ENTRY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Response:

    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


def _request_header(request, name):
    return next(
        (value for key, value in request.headers.items() if key.lower() == name.lower()),
        None,
    )


class _FakeRegistry:
    def __init__(self):
        self.specs = []

    def register(self, spec):
        self.specs.append(spec)


class ResolveApiKeyTest(unittest.TestCase):

    def setUp(self):
        self.tr = _load_plugin()

    def test_resolve_key_found_and_decrypted(self):
        providers = [{
            "name": "基元律动",
            "base_url": "https://tokenrhythm.studio/v1",
            "api_key": "dpapi:FAKECIPHERTEXT",
        }]
        with mock.patch("config._load", return_value={"providers": providers}), \
             mock.patch("core.credentials.unprotect_secret", return_value="sk_tr_fake_test"):
            self.assertEqual(self.tr._resolve_api_key(), "sk_tr_fake_test")

    def test_resolve_key_missing_entry(self):
        providers = [{"name": "其他", "base_url": "https://api.example.com/v1", "api_key": "dpapi:X"}]
        with mock.patch("config._load", return_value={"providers": providers}):
            self.assertEqual(self.tr._resolve_api_key(), "")

    def test_resolve_key_decrypt_failure_returns_empty(self):
        providers = [{
            "name": "基元律动",
            "base_url": "https://tokenrhythm.studio/v1",
            "api_key": "dpapi:BROKEN",
        }]
        with mock.patch("config._load", return_value={"providers": providers}), \
             mock.patch("core.credentials.unprotect_secret", side_effect=OSError("blob 损坏")):
            self.assertEqual(self.tr._resolve_api_key(), "")

    def test_resolve_key_empty_api_key_returns_empty(self):
        providers = [{"name": "基元律动", "base_url": "https://tokenrhythm.studio/v1", "api_key": ""}]
        with mock.patch("config._load", return_value={"providers": providers}):
            self.assertEqual(self.tr._resolve_api_key(), "")


class GenerateImageTest(unittest.TestCase):
    def setUp(self):
        self.tr = _load_plugin()

    def test_no_key_returns_config_guidance(self):
        with mock.patch.object(self.tr, "_resolve_api_key", return_value=""), \
             mock.patch.object(self.tr.urllib.request, "urlopen") as fake_open:
            result = self.tr.generate_image("一只猫")
        self.assertTrue(result.is_error)
        self.assertIn("基元律动", result.content)
        self.assertIn("tokenrhythm.studio", result.content)
        fake_open.assert_not_called()

    def test_param_validation_rejects_without_network(self):
        cases = [
            dict(prompt=""),
            dict(prompt="猫", size="333x333"),
            dict(prompt="猫", n=5),
            dict(prompt="猫", n=0),
            dict(prompt="猫", model="gpt-image"),
        ]
        for kwargs in cases:
            with mock.patch.object(self.tr, "_resolve_api_key", return_value="sk_tr_fake_test"), \
                 mock.patch.object(self.tr.urllib.request, "urlopen") as fake_open:
                result = self.tr.generate_image(**kwargs)
            self.assertTrue(result.is_error, msg="应拒绝参数: %r" % kwargs)
            fake_open.assert_not_called()

    def test_generate_success_downloads_and_reports_cost(self):
        captured = {}

        def fake_urlopen(request, timeout):
            captured["url"] = request.full_url
            captured["auth"] = request.get_header("Authorization")
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            return _Response({"data": [{"url": "https://example.invalid/x.png"}], "cost_cny": "0.40000000"})

        def fake_urlretrieve(url, target):
            with open(target, "wb") as f:
                f.write(b"\xff\xd8\xff\xe0")

        with tempfile.TemporaryDirectory() as save_dir:
            with mock.patch.object(self.tr, "_resolve_api_key", return_value="sk_tr_fake_test"), \
                 mock.patch.object(self.tr.urllib.request, "urlopen", fake_urlopen), \
                 mock.patch.object(self.tr.urllib.request, "urlretrieve", fake_urlretrieve):
                result = self.tr.generate_image("一只戴头盔的橘猫", n=2, save_dir=save_dir)

        self.assertFalse(result.is_error)
        self.assertEqual(captured["url"], "https://tokenrhythm.studio/v1/images/generations")
        self.assertEqual(captured["auth"], "Bearer sk_tr_fake_test")
        self.assertEqual(captured["payload"]["response_format"], "url")
        self.assertEqual(captured["payload"]["n"], 2)
        self.assertEqual(captured["payload"]["model"], "qwen-image-2.0")
        self.assertIn("0.40", result.content)
        self.assertTrue(result.changed_files, "应记录生成的本地文件")
        self.assertTrue(result.changed_files[0].endswith(".jpg"))
        self.assertIn("![生成的图片](%s)" % result.changed_files[0], result.content)

    def test_generate_http_401_hints_key_invalid(self):
        error = __import__("urllib").error.HTTPError(
            "https://tokenrhythm.studio/v1/images/generations", 401, "UNAUTHORIZED",
            {}, io.BytesIO(b'{"error":"bad key"}'),
        )
        with mock.patch.object(self.tr, "_resolve_api_key", return_value="sk_tr_fake_test"), \
             mock.patch.object(self.tr.urllib.request, "urlopen", side_effect=error):
            result = self.tr.generate_image("一只猫")
        self.assertTrue(result.is_error)
        self.assertIn("401", result.content)
        self.assertIn("Key 无效", result.content)
    def test_wan_model_requests_b64_and_saves_decoded_bytes(self):
        import base64

        captured = {}
        raw = b"\x89PNG\r\n\x1a\n" + b"wan-image-bytes" * 4

        def fake_urlopen(request, timeout):
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            return _Response({"data": [{"b64_json": base64.b64encode(raw).decode("ascii")}]})

        with tempfile.TemporaryDirectory() as save_dir:
            with mock.patch.object(self.tr, "_resolve_api_key", return_value="sk_tr_fake_test"), \
                 mock.patch.object(self.tr.urllib.request, "urlopen", fake_urlopen), \
                 mock.patch.object(self.tr.urllib.request, "urlretrieve") as fake_retrieve:
                result = self.tr.generate_image("雪山日出", model="wan2.7-image", save_dir=save_dir)

            self.assertFalse(result.is_error)
            self.assertEqual(captured["payload"]["response_format"], "b64_json")
            fake_retrieve.assert_not_called()
            path = result.changed_files[0]
            self.assertTrue(path.endswith(".png"))
            with open(path, "rb") as f:
                self.assertEqual(f.read(), raw)

    def test_wan_model_falls_back_to_url_when_server_returns_url(self):
        captured = {}

        def fake_urlopen(request, timeout):
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            return _Response({"data": [{"url": "https://example.invalid/wan.png"}]})

        def fake_urlretrieve(url, target):
            with open(target, "wb") as f:
                f.write(b"\x89PNG\r\n\x1a\n")

        with tempfile.TemporaryDirectory() as save_dir:
            with mock.patch.object(self.tr, "_resolve_api_key", return_value="sk_tr_fake_test"), \
                 mock.patch.object(self.tr.urllib.request, "urlopen", fake_urlopen), \
                 mock.patch.object(self.tr.urllib.request, "urlretrieve", fake_urlretrieve):
                result = self.tr.generate_image("雪山日出", model="wan2.7-image", save_dir=save_dir)

        self.assertFalse(result.is_error)
        self.assertEqual(captured["payload"]["response_format"], "b64_json")
        self.assertTrue(result.changed_files[0].endswith(".png"))

    def test_broken_b64_returns_error_without_crash(self):

        def fake_urlopen(request, timeout):
            return _Response({"data": [{"b64_json": "!!!broken!!!"}]})

        with tempfile.TemporaryDirectory() as save_dir:
            with mock.patch.object(self.tr, "_resolve_api_key", return_value="sk_tr_fake_test"), \
                 mock.patch.object(self.tr.urllib.request, "urlopen", fake_urlopen):
                result = self.tr.generate_image("雪山日出", model="wan2.7-image", save_dir=save_dir)

        self.assertTrue(result.is_error)
        self.assertIn("base64", result.content)

    def test_generate_request_carries_tokenrhythm_agent_header(self):
        from core.llm_policy import TOKENRHYTHM_AGENT_NAME

        captured = {}

        def fake_urlopen(request, timeout):
            captured["agent"] = _request_header(request, "X-TokenRhythm-Agent")
            return _Response({"data": [{"url": "https://example.invalid/x.png"}]})

        def fake_urlretrieve(url, target):
            with open(target, "wb") as f:
                f.write(b"\xff\xd8\xff\xe0")

        with tempfile.TemporaryDirectory() as save_dir:
            with mock.patch.object(self.tr, "_resolve_api_key", return_value="sk_tr_fake_test"), \
                 mock.patch.object(self.tr.urllib.request, "urlopen", fake_urlopen), \
                 mock.patch.object(self.tr.urllib.request, "urlretrieve", fake_urlretrieve):
                result = self.tr.generate_image("橘猫", save_dir=save_dir)

        self.assertFalse(result.is_error)
        self.assertEqual(captured["agent"], TOKENRHYTHM_AGENT_NAME)

    def test_generate_request_carries_session_id_header(self):
        from core import app_context

        old_session = app_context.get_thread_session()
        self.addCleanup(app_context.set_thread_session, old_session)
        app_context.set_thread_session("session_tr42")

        captured = {}

        def fake_urlopen(request, timeout):
            captured["session_id"] = _request_header(request, "X-Session-Id")
            return _Response({"data": [{"url": "https://example.invalid/x.png"}]})

        def fake_urlretrieve(url, target):
            with open(target, "wb") as f:
                f.write(b"\xff\xd8\xff\xe0")

        with tempfile.TemporaryDirectory() as save_dir:
            with mock.patch.object(self.tr, "_resolve_api_key", return_value="sk_tr_fake_test"), \
                 mock.patch.object(self.tr.urllib.request, "urlopen", fake_urlopen), \
                 mock.patch.object(self.tr.urllib.request, "urlretrieve", fake_urlretrieve):
                result = self.tr.generate_image("橘猫", save_dir=save_dir)

        self.assertFalse(result.is_error)
        self.assertEqual(captured["session_id"], "session_tr42")

class EditImageTest(unittest.TestCase):
    def setUp(self):
        self.tr = _load_plugin()

    def _make_png(self, directory):
        path = os.path.join(directory, "cat.png")
        with open(path, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n" + b"fake-image-bytes" * 8)
        return path

    def test_validation_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = os.path.join(tmp, "nope.png")
            cases = [
                dict(prompt="", image=self._make_png(tmp)),
                dict(prompt="换背景", image=missing),
            ]
            for kwargs in cases:
                result = self.tr.edit_image(**kwargs)
                self.assertTrue(result.is_error, msg="应拒绝: %r" % kwargs)

            bad_ext = os.path.join(tmp, "doc.txt")
            with open(bad_ext, "wb") as f:
                f.write(b"text")
            result = self.tr.edit_image(prompt="换背景", image=bad_ext)
            self.assertTrue(result.is_error)

            png = self._make_png(tmp)
            with mock.patch("os.path.getsize", return_value=11 * 1024 * 1024):
                result = self.tr.edit_image(prompt="换背景", image=png)
            self.assertTrue(result.is_error)
            self.assertIn("10MB", result.content)

    def test_edit_success_multipart_without_size(self):
        captured = {}

        def fake_urlopen(request, timeout):
            captured["url"] = request.full_url
            captured["auth"] = request.get_header("Authorization")
            captured["content_type"] = request.get_header("Content-type")
            captured["body"] = request.data
            return _Response({"data": [{"url": "https://example.invalid/edited.png"}]})

        def fake_urlretrieve(url, target):
            with open(target, "wb") as f:
                f.write(b"\x89PNG\r\n\x1a\n")

        with tempfile.TemporaryDirectory() as tmp:
            png = self._make_png(tmp)
            with mock.patch.object(self.tr, "_resolve_api_key", return_value="sk_tr_fake_test"), \
                 mock.patch.object(self.tr.urllib.request, "urlopen", fake_urlopen), \
                 mock.patch.object(self.tr.urllib.request, "urlretrieve", fake_urlretrieve):
                result = self.tr.edit_image(prompt="把头盔换成粉色棒球帽", image=png, save_dir=tmp)

        self.assertFalse(result.is_error)
        self.assertEqual(captured["url"], "https://tokenrhythm.studio/v1/images/edits")
        self.assertEqual(captured["auth"], "Bearer sk_tr_fake_test")
        self.assertIn("multipart/form-data; boundary=", captured["content_type"])
        body = captured["body"]
        self.assertIn(b'name="image"', body)
        self.assertIn(b'filename="cat.png"', body)
        self.assertIn(b'name="prompt"', body)
        self.assertIn("把头盔换成粉色棒球帽".encode("utf-8"), body)
        self.assertNotIn(b'name="size"', body, "size 留空时不应发送该字段")
        self.assertTrue(result.changed_files)
        self.assertTrue(result.changed_files[0].endswith(".png"))

    def test_edit_with_size_includes_field(self):
        captured = {}

        def fake_urlopen(request, timeout):
            captured["body"] = request.data
            return _Response({"data": [{"url": "https://example.invalid/edited.png"}]})

        def fake_urlretrieve(url, target):
            with open(target, "wb") as f:
                f.write(b"\x89PNG\r\n\x1a\n")

        with tempfile.TemporaryDirectory() as tmp:
            png = self._make_png(tmp)
            with mock.patch.object(self.tr, "_resolve_api_key", return_value="sk_tr_fake_test"), \
                 mock.patch.object(self.tr.urllib.request, "urlopen", fake_urlopen), \
                 mock.patch.object(self.tr.urllib.request, "urlretrieve", fake_urlretrieve):
                result = self.tr.edit_image(prompt="换背景", image=png, size="1024x1024", save_dir=tmp)

        self.assertFalse(result.is_error)
        self.assertIn(b'name="size"', captured["body"])
        self.assertIn(b"1024x1024", captured["body"])

    def test_edit_with_wan_model_requests_b64_and_saves(self):
        import base64

        captured = {}
        raw = b"\x89PNG\r\n\x1a\n" + b"edited-bytes" * 3

        def fake_urlopen(request, timeout):
            captured["body"] = request.data
            return _Response({"data": [{"b64_json": base64.b64encode(raw).decode("ascii")}]})

        with tempfile.TemporaryDirectory() as tmp:
            png = self._make_png(tmp)
            with mock.patch.object(self.tr, "_resolve_api_key", return_value="sk_tr_fake_test"), \
                 mock.patch.object(self.tr.urllib.request, "urlopen", fake_urlopen), \
                 mock.patch.object(self.tr.urllib.request, "urlretrieve") as fake_retrieve:
                result = self.tr.edit_image(
                    prompt="换背景", image=png, model="wan2.7-image", save_dir=tmp
                )

            self.assertFalse(result.is_error)
            self.assertIn(b'name="response_format"', captured["body"])
            self.assertIn(b"b64_json", captured["body"])
            fake_retrieve.assert_not_called()
            with open(result.changed_files[0], "rb") as f:
                self.assertEqual(f.read(), raw)

    def test_edit_request_carries_tokenrhythm_agent_header(self):
        from core.llm_policy import TOKENRHYTHM_AGENT_NAME

        captured = {}

        def fake_urlopen(request, timeout):
            captured["agent"] = _request_header(request, "X-TokenRhythm-Agent")
            return _Response({"data": [{"url": "https://example.invalid/edited.png"}]})

        def fake_urlretrieve(url, target):
            with open(target, "wb") as f:
                f.write(b"\x89PNG\r\n\x1a\n")

        with tempfile.TemporaryDirectory() as tmp:
            png = self._make_png(tmp)
            with mock.patch.object(self.tr, "_resolve_api_key", return_value="sk_tr_fake_test"), \
                 mock.patch.object(self.tr.urllib.request, "urlopen", fake_urlopen), \
                 mock.patch.object(self.tr.urllib.request, "urlretrieve", fake_urlretrieve):
                result = self.tr.edit_image(prompt="把背景换成雪景", image=png, save_dir=tmp)

        self.assertFalse(result.is_error)
        self.assertEqual(captured["agent"], TOKENRHYTHM_AGENT_NAME)

    def test_edit_request_carries_session_id_header(self):
        from core import app_context

        old_session = app_context.get_thread_session()
        self.addCleanup(app_context.set_thread_session, old_session)
        app_context.set_thread_session("session_tr42")

        captured = {}

        def fake_urlopen(request, timeout):
            captured["session_id"] = _request_header(request, "X-Session-Id")
            return _Response({"data": [{"url": "https://example.invalid/edited.png"}]})

        def fake_urlretrieve(url, target):
            with open(target, "wb") as f:
                f.write(b"\x89PNG\r\n\x1a\n")

        with tempfile.TemporaryDirectory() as tmp:
            png = self._make_png(tmp)
            with mock.patch.object(self.tr, "_resolve_api_key", return_value="sk_tr_fake_test"), \
                 mock.patch.object(self.tr.urllib.request, "urlopen", fake_urlopen), \
                 mock.patch.object(self.tr.urllib.request, "urlretrieve", fake_urlretrieve):
                result = self.tr.edit_image(prompt="把背景换成雪景", image=png, save_dir=tmp)

        self.assertFalse(result.is_error)
        self.assertEqual(captured["session_id"], "session_tr42")


class PluginMetadataTest(unittest.TestCase):

    def test_plugin_json_enabled_by_deliberate_enable(self):
        with open(os.path.join(_PLUGIN_DIR, "plugin.json"), "r", encoding="utf-8") as f:
            meta = json.load(f)
        self.assertIs(meta.get("enabled"), True)
        self.assertEqual(meta.get("name"), "tokenrhythm_image")

    def test_register_two_tools_with_permission(self):
        tr = _load_plugin()
        registry = _FakeRegistry()
        tr.register(registry)
        names = [s.name for s in registry.specs]
        self.assertEqual(names, ["tokenrhythm_generate_image", "tokenrhythm_edit_image"])
        for spec in registry.specs:
            self.assertTrue(spec.requires_permission)
            self.assertTrue(spec.mutates_files)
            self.assertFalse(spec.read_only)


if __name__ == "__main__":
    unittest.main()
