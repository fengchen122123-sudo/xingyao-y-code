import core.model_discovery as discovery


def test_models_endpoint_keeps_existing_version_segment():
    assert (
        discovery.models_endpoint("https://api.example.com/v1")
        == "https://api.example.com/v1/models"
    )
    assert (
        discovery.models_endpoint("https://api.kimi.com/coding/v1/")
        == "https://api.kimi.com/coding/v1/models"
    )
    assert (
        discovery.models_endpoint("https://open.bigmodel.cn/api/paas/v4")
        == "https://open.bigmodel.cn/api/paas/v4/models"
    )


def test_models_endpoint_adds_v1_for_bare_base():
    assert (
        discovery.models_endpoint("https://api.stepfun.com/step_plan")
        == "https://api.stepfun.com/step_plan/v1/models"
    )
    assert (
        discovery.models_endpoint("https://api.deepseek.com")
        == "https://api.deepseek.com/v1/models"
    )


def test_normalize_models_extracts_metadata_and_sorts_non_chat_last():
    models = discovery.normalize_models([
        {"id": "zeta-tts", "model_type": "语音合成"},
        {
            "id": "or-y",
            "context_length": 131072,
            "max_completion_tokens": 8192,
            "architecture": {"input_modalities": ["text", "image"]},
        },
        {"id": "step-x", "max_input_tokens": 1024000, "enable_vision_input": True},
        "plain-z",
    ])

    assert [m["id"] for m in models] == ["or-y", "plain-z", "step-x", "zeta-tts"]
    by_id = {m["id"]: m for m in models}
    assert by_id["step-x"]["context_window"] == 1024000
    assert by_id["step-x"]["supports_vision"] is True
    assert by_id["or-y"]["supports_vision"] is True
    assert by_id["or-y"]["max_tokens"] == 8192
    assert by_id["plain-z"]["supports_vision"] is None


class _FakeResponse:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


def test_fetch_model_list_retries_alternate_endpoint_and_next_key(monkeypatch):
    import httpx

    calls = []

    def fake_get(url, headers=None, timeout=None):
        calls.append((url, (headers or {}).get("Authorization")))
        if url.endswith("/v1/models") and len(calls) == 1:
            return _FakeResponse(404, text="not found")
        if (headers or {}).get("Authorization") == "Bearer sk-bad":
            return _FakeResponse(401, text='{"error":"bad key"}')
        return _FakeResponse(200, {"data": [{"id": "alpha"}]})

    monkeypatch.setattr(httpx, "get", fake_get)

    result = discovery.fetch_model_list("https://api.example.com", "sk-bad\nsk-good")

    assert result["ok"] is True
    assert [m["id"] for m in result["models"]] == ["alpha"]
    assert calls[0] == ("https://api.example.com/v1/models", "Bearer sk-bad")
    assert calls[1] == ("https://api.example.com/models", "Bearer sk-bad")
    assert calls[2][1] == "Bearer sk-good"


def test_fetch_model_list_reports_http_error_for_single_key(monkeypatch):
    import httpx

    monkeypatch.setattr(
        httpx, "get",
        lambda url, headers=None, timeout=None: _FakeResponse(
            401, text='{"error":"invalid key"}'
        ),
    )

    result = discovery.fetch_model_list("https://api.example.com", "sk-bad")

    assert result["ok"] is False
    assert "401" in result["msg"]
    assert "invalid key" in result["msg"]


def test_fetch_model_list_mentions_all_keys_failed(monkeypatch):
    import httpx

    monkeypatch.setattr(
        httpx, "get",
        lambda url, headers=None, timeout=None: _FakeResponse(401, text="denied"),
    )

    result = discovery.fetch_model_list("https://api.example.com", "sk-1,sk-2")

    assert result["ok"] is False
    assert "2 个 Key 均获取失败" in result["msg"]


def test_fetch_model_list_requires_base_url():
    result = discovery.fetch_model_list("", "sk-any")

    assert result["ok"] is False
    assert "Base URL" in result["msg"]


def test_fetch_model_list_has_no_key_sends_no_auth_header(monkeypatch):
    import httpx

    captured = {}

    def fake_get(url, headers=None, timeout=None):
        captured["headers"] = headers
        return _FakeResponse(200, {"data": [{"id": "local-model"}]})

    monkeypatch.setattr(httpx, "get", fake_get)

    result = discovery.fetch_model_list("http://127.0.0.1:8000/v1", "")

    assert result["ok"] is True
    assert "Authorization" not in captured["headers"]
    assert "x-api-key" not in captured["headers"]