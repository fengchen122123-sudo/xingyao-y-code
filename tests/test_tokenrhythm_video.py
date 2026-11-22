# -*- coding: utf-8 -*-

import importlib.util
import json
import os
import tempfile
import unittest
from unittest import mock

from core.tool_protocol import ToolResult

_PLUGIN_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "plugins", "tokenrhythm_video",
)
_PLUGIN_ENTRY = os.path.join(_PLUGIN_DIR, "plugin.py")

_PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)

_MP4_BYTES = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 32


def _load_plugin():
    spec = importlib.util.spec_from_file_location("_test_tokenrhythm_video_plugin", _PLUGIN_ENTRY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _JSONResponse:

    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


class _BytesResponse:

    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


class _FakeRegistry:
    def __init__(self):
        self.specs = []

    def register(self, spec):
        self.specs.append(spec)


def _request_body(request):
    return json.loads(request.data.decode("utf-8"))


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

    def test_resolve_key_missing_entry_returns_empty(self):
        providers = [{"name": "其他", "base_url": "https://api.example.com/v1", "api_key": "dpapi:X"}]
        with mock.patch("config._load", return_value={"providers": providers}):
            self.assertEqual(self.tr._resolve_api_key(), "")


class ParamValidationTest(unittest.TestCase):

    def setUp(self):
        self.tr = _load_plugin()

    def _assert_rejected(self, result, fragment):
        self.assertTrue(result.is_error)
        self.assertIn(fragment, result.content)

    def test_no_key_returns_config_guidance(self):
        with mock.patch.object(self.tr, "_resolve_api_key", return_value=""), \
             mock.patch.object(self.tr.urllib.request, "urlopen") as fake_open:
            result = self.tr.generate_video("一只猫在窗台跳舞")
        self.assertTrue(result.is_error)
        self.assertIn("tokenrhythm.studio", result.content)
        fake_open.assert_not_called()

    def test_empty_prompt_rejected(self):
        with mock.patch.object(self.tr, "_resolve_api_key", return_value="sk_tr_fake_test"):
            result = self.tr.generate_video("")
        self._assert_rejected(result, "prompt")

    def test_bad_duration_rejected(self):
        with mock.patch.object(self.tr, "_resolve_api_key", return_value="sk_tr_fake_test"):
            for bad in (1, 31, "abc", True):
                result = self.tr.generate_video("跳舞", duration=bad)
                self.assertTrue(result.is_error, "duration=%r 应被拒绝" % (bad,))

    def test_smart_duration_minus_one_allowed(self):
        with mock.patch.object(self.tr, "_resolve_api_key", return_value="sk_tr_fake_test"), \
             mock.patch.object(self.tr.urllib.request, "urlopen") as fake_open:
            fake_open.side_effect = Exception("stop here")
            self.tr.generate_video("跳舞", duration=-1)
        self.assertEqual(fake_open.call_count, 1)

    def test_bad_resolution_rejected(self):
        with mock.patch.object(self.tr, "_resolve_api_key", return_value="sk_tr_fake_test"):
            result = self.tr.generate_video("跳舞", resolution="4K")
        self._assert_rejected(result, "resolution")

    def test_bad_model_rejected(self):
        with mock.patch.object(self.tr, "_resolve_api_key", return_value="sk_tr_fake_test"):
            result = self.tr.generate_video("跳舞", model="sora-2")
        self._assert_rejected(result, "model")

    def test_missing_image_file_rejected(self):
        with mock.patch.object(self.tr, "_resolve_api_key", return_value="sk_tr_fake_test"):
            result = self.tr.generate_video("跳舞", image=os.path.join(tempfile.gettempdir(), "no_such_frame.png"))
        self._assert_rejected(result, "不存在")

    def test_oversize_image_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "big.png")
            with open(path, "wb") as handle:
                handle.write(_PNG_BYTES)
            with mock.patch.object(self.tr, "_resolve_api_key", return_value="sk_tr_fake_test"), \
                 mock.patch.object(self.tr.os.path, "getsize", return_value=11 * 1024 * 1024):
                result = self.tr.generate_video("跳舞", image=path)
        self._assert_rejected(result, "10MB")

    def test_bad_image_ext_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "frame.gif")
            with open(path, "wb") as handle:
                handle.write(b"GIF89a")
            with mock.patch.object(self.tr, "_resolve_api_key", return_value="sk_tr_fake_test"):
                result = self.tr.generate_video("跳舞", image=path)
        self._assert_rejected(result, "格式")

    def test_refit_size_follows_aspect(self):
        self.assertEqual(self.tr._refit_size(1280, 720), "1280x720")
        self.assertEqual(self.tr._refit_size(1792, 1024), "1280x720")
        self.assertEqual(self.tr._refit_size(720, 1280), "720x1280")
        self.assertEqual(self.tr._refit_size(1024, 1792), "720x1280")
        self.assertEqual(self.tr._refit_size(1024, 1024), "1024x1024")
        self.assertEqual(self.tr._refit_size(0, 0), "1024x1024")


class GenerateVideoFlowTest(unittest.TestCase):

    def setUp(self):
        self.tr = _load_plugin()
        patcher = mock.patch.object(self.tr, "_POLL_INTERVAL", 0.0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _key(self):
        patcher = mock.patch.object(self.tr, "_resolve_api_key", return_value="sk_tr_fake_test")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_text_to_video_saves_mp4(self):
        self._key()
        responses = [
            _JSONResponse({"id": "vid-1", "status": "queued", "estimated_output_cost_cny": 1.0}),
            _JSONResponse({"id": "vid-1", "status": "running"}),
            _JSONResponse({
                "id": "vid-1", "status": "success",
                "result": {"url": "https://cdn.example.com/vid-1.mp4"},
                "billing": {"total_cost_cny": 0.96},
            }),
            _BytesResponse(_MP4_BYTES),
        ]
        with tempfile.TemporaryDirectory() as save_dir, \
             mock.patch.object(self.tr.urllib.request, "urlopen") as fake_open:
            fake_open.side_effect = responses
            result = self.tr.generate_video("橘猫在窗台伸懒腰", duration=5, save_dir=save_dir)

            self.assertFalse(result.is_error, result.content)
            self.assertEqual(fake_open.call_count, 4)
            create_req = fake_open.call_args_list[0][0][0]
            self.assertEqual(create_req.full_url, "https://tokenrhythm.studio/v1/videos/generations")
            self.assertEqual(create_req.get_method(), "POST")
            body = _request_body(create_req)
            self.assertEqual(body["model"], "wan3.0-video")
            self.assertEqual(body["resolution"], "720P")
            self.assertEqual(body["duration"], 5)
            self.assertEqual(body["ratio"], "16:9")
            self.assertEqual(body["content"][0]["type"], "text")
            self.assertEqual(body["content"][0]["text"], "橘猫在窗台伸懒腰")
            poll_req = fake_open.call_args_list[1][0][0]
            self.assertEqual(poll_req.full_url, "https://tokenrhythm.studio/v1/videos/vid-1")
            self.assertEqual(poll_req.get_method(), "GET")
            self.assertEqual(len(result.changed_files), 1)
            path = result.changed_files[0]
            self.assertTrue(path.endswith(".mp4"), path)
            self.assertTrue(os.path.isfile(path))
            with open(path, "rb") as handle:
                self.assertEqual(handle.read(), _MP4_BYTES)
            self.assertIn(path, result.content)
            self.assertIn("0.96", result.content)

    def test_image_to_video_refits_local_frame_to_oss_url(self):
        self._key()
        with tempfile.TemporaryDirectory() as tmp:
            frame = os.path.join(tmp, "frame.png")
            with open(frame, "wb") as handle:
                handle.write(_PNG_BYTES)
            responses = [
                _JSONResponse({"data": [{"url": "https://oss.example.com/frame.png"}]}),
                _JSONResponse({"id": "vid-2", "status": "queued"}),
                _JSONResponse({
                    "id": "vid-2", "status": "success",
                    "result": {"url": "https://cdn.example.com/vid-2.mp4"},
                }),
                _BytesResponse(_MP4_BYTES),
            ]
            with mock.patch.object(self.tr.urllib.request, "urlopen") as fake_open:
                fake_open.side_effect = responses
                result = self.tr.generate_video(
                    "镜头缓缓推近", image=frame, duration=5, save_dir=tmp
                )

            self.assertFalse(result.is_error, result.content)
            self.assertEqual(fake_open.call_count, 4)
            refit_req = fake_open.call_args_list[0][0][0]
            self.assertEqual(refit_req.full_url, "https://tokenrhythm.studio/v1/images/edits")
            self.assertTrue(refit_req.headers.get("Content-type", "").startswith("multipart/form-data"))
            refit_body = refit_req.data.decode("utf-8", errors="replace")
            self.assertIn("qwen-image-2.0", refit_body)
            self.assertIn("原样重绘", refit_body)
            self.assertIn('name="size"', refit_body)
            self.assertIn("1024x1024", refit_body)
            create_req = fake_open.call_args_list[1][0][0]
            body = _request_body(create_req)
            image_item = body["content"][0]
            self.assertEqual(image_item["type"], "image_url")
            self.assertEqual(image_item["role"], "first_frame")
            self.assertEqual(image_item["image_url"]["url"], "https://oss.example.com/frame.png")
            self.assertEqual(body["ratio"], "16:9")
            self.assertEqual(body["content"][1]["text"], "镜头缓缓推近")

    def test_image_url_passthrough_skips_refit(self):
        self._key()
        responses = [
            _JSONResponse({"id": "vid-5", "status": "queued"}),
            _JSONResponse({
                "id": "vid-5", "status": "success",
                "result": {"url": "https://cdn.example.com/vid-5.mp4"},
            }),
            _BytesResponse(_MP4_BYTES),
        ]
        with tempfile.TemporaryDirectory() as save_dir, \
             mock.patch.object(self.tr.urllib.request, "urlopen") as fake_open:
            fake_open.side_effect = responses
            result = self.tr.generate_video(
                "推近", image="https://oss.example.com/ready.png", save_dir=save_dir
            )
            self.assertFalse(result.is_error, result.content)
            self.assertEqual(fake_open.call_count, 3)
            create_req = fake_open.call_args_list[0][0][0]
            self.assertEqual(create_req.full_url, "https://tokenrhythm.studio/v1/videos/generations")
            body = _request_body(create_req)
            self.assertEqual(body["content"][0]["image_url"]["url"], "https://oss.example.com/ready.png")

    def test_refit_failure_returns_error(self):
        self._key()
        with tempfile.TemporaryDirectory() as tmp:
            frame = os.path.join(tmp, "frame.png")
            with open(frame, "wb") as handle:
                handle.write(_PNG_BYTES)
            with mock.patch.object(self.tr.urllib.request, "urlopen") as fake_open:
                fake_open.side_effect = [_JSONResponse({"data": []})]
                result = self.tr.generate_video("推近", image=frame, save_dir=tmp)
        self.assertTrue(result.is_error)
        self.assertIn("data[0].url", result.content)
        self.assertEqual(result.changed_files, [])

    def test_failed_task_status_returns_error(self):
        self._key()
        responses = [
            _JSONResponse({"id": "vid-3", "status": "queued"}),
            _JSONResponse({"id": "vid-3", "status": "failed", "error": "内容审核未通过"}),
        ]
        with tempfile.TemporaryDirectory() as save_dir, \
             mock.patch.object(self.tr.urllib.request, "urlopen") as fake_open:
            fake_open.side_effect = responses
            result = self.tr.generate_video("跳舞", save_dir=save_dir)
        self.assertTrue(result.is_error)
        self.assertIn("vid-3", result.content)
        self.assertIn("内容审核未通过", result.content)
        self.assertEqual(result.changed_files, [])

    def test_poll_timeout_returns_error(self):
        self._key()
        forever = _JSONResponse({"id": "vid-4", "status": "running"})
        with tempfile.TemporaryDirectory() as save_dir, \
             mock.patch.object(self.tr, "_POLL_TIMEOUT", 0.05), \
             mock.patch.object(self.tr.urllib.request, "urlopen") as fake_open:
            fake_open.side_effect = lambda *a, **k: forever
            result = self.tr.generate_video("跳舞", save_dir=save_dir)
        self.assertTrue(result.is_error)
        self.assertIn("超时", result.content)
        self.assertIn("vid-4", result.content)
        self.assertEqual(result.changed_files, [])

    def test_create_http_error_returns_error(self):
        self._key()
        import urllib.error
        err = urllib.error.HTTPError(
            "https://tokenrhythm.studio/v1/videos/generations", 401, "Unauthorized",
            {}, None,
        )
        with tempfile.TemporaryDirectory() as save_dir, \
             mock.patch.object(self.tr.urllib.request, "urlopen") as fake_open:
            fake_open.side_effect = err
            result = self.tr.generate_video("跳舞", save_dir=save_dir)
        self.assertTrue(result.is_error)
        self.assertIn("401", result.content)


class RegisterTest(unittest.TestCase):

    def setUp(self):
        self.tr = _load_plugin()

    def test_register_exposes_generate_video_tool(self):
        registry = _FakeRegistry()
        self.tr.register(registry)
        self.assertEqual(len(registry.specs), 1)
        spec = registry.specs[0]
        self.assertEqual(spec.name, "tokenrhythm_generate_video")
        self.assertEqual(spec.input_schema.get("required"), ["prompt"])
        props = spec.input_schema.get("properties", {})
        self.assertIn("prompt", props)
        self.assertIn("image", props)
        self.assertIn("duration", props)
        self.assertIn("resolution", props)
        self.assertIn("save_dir", props)
        self.assertFalse(spec.read_only)
        self.assertFalse(spec.mutates_files)
        self.assertTrue(spec.requires_permission)
        self.assertTrue(spec.cancellable)
        self.assertEqual(spec.side_effect_scope, "network")


if __name__ == "__main__":
    unittest.main()
