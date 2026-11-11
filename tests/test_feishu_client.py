"""T4：飞书出站 API 客户端单测（令牌缓存、请求构造、退避、错误处理）。不触网。"""

import json

import pytest

from core.feishu.client import FeishuApiClient, FeishuApiError


class _Recorder:
    """记录请求并按脚本返回响应。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append({
            "method": method,
            "url": url,
            "headers": dict(headers),
            "body": json.loads(body.decode("utf-8")) if body else None,
        })
        if not self.responses:
            raise AssertionError("没有预设响应了")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _token_ok(token="t-abc", expire=7200):
    return 200, json.dumps({"code": 0, "tenant_access_token": token, "expire": expire})


def _ok(payload=None):
    return 200, json.dumps(payload or {"code": 0, "data": {"message_id": "om_1"}})


def test_token_is_cached_across_calls():
    http = _Recorder([_token_ok(), _ok(), _ok()])
    client = FeishuApiClient("cli_a", "sec", http=http)

    client.send_text("oc_1", "第一条")
    client.send_text("oc_1", "第二条")

    assert len(http.calls) == 3                      # 令牌只取一次
    assert http.calls[0]["url"].endswith("/open-apis/auth/v3/tenant_access_token/internal")
    assert http.calls[0]["body"] == {"app_id": "cli_a", "app_secret": "sec"}
    assert http.calls[1]["headers"]["Authorization"] == "Bearer t-abc"


def test_token_refreshed_after_expiry():
    now = [1000.0]
    http = _Recorder([_token_ok(expire=120), _ok(), _token_ok(token="t-new"), _ok()])
    client = FeishuApiClient("cli_a", "sec", http=http, clock=lambda: now[0])

    client.send_text("oc_1", "第一次")
    now[0] += 120                                    # 越过过期点
    client.send_text("oc_1", "第二次")

    assert len(http.calls) == 4
    assert http.calls[3]["headers"]["Authorization"] == "Bearer t-new"


def test_send_text_request_shape():
    http = _Recorder([_token_ok(), _ok()])
    FeishuApiClient("cli_a", "sec", http=http).send_text("oc_1", "你好")

    call = http.calls[1]
    assert call["method"] == "POST"
    assert "receive_id_type=chat_id" in call["url"]
    assert call["body"]["receive_id"] == "oc_1"
    assert call["body"]["msg_type"] == "text"
    assert json.loads(call["body"]["content"]) == {"text": "你好"}


def test_send_card_uses_interactive_type():
    http = _Recorder([_token_ok(), _ok()])
    card = {"schema": "2.0", "header": {"title": {"tag": "plain_text", "content": "标题"}}}
    FeishuApiClient("cli_a", "sec", http=http).send_card("oc_1", card)

    call = http.calls[1]
    assert call["body"]["msg_type"] == "interactive"
    assert json.loads(call["body"]["content"]) == card


def test_reply_uses_reply_endpoint():
    http = _Recorder([_token_ok(), _ok()])
    FeishuApiClient("cli_a", "sec", http=http).send_text("oc_1", "回复", reply_to="om_9")

    call = http.calls[1]
    assert call["url"].endswith("/open-apis/im/v1/messages/om_9/reply")
    assert "receive_id" not in call["body"]


def test_update_card_uses_patch():
    http = _Recorder([_token_ok(), _ok()])
    card = {"schema": "2.0", "header": {"title": {"tag": "plain_text", "content": "已批准"}}}
    FeishuApiClient("cli_a", "sec", http=http).update_card("om_9", card)

    call = http.calls[1]
    assert call["method"] == "PATCH"
    assert call["url"].endswith("/open-apis/im/v1/messages/om_9")
    assert json.loads(call["body"]["content"]) == card


def test_retries_on_429_then_succeeds():
    slept = []
    http = _Recorder([_token_ok(), (429, "{}"), _ok()])
    client = FeishuApiClient("cli_a", "sec", http=http, sleep=slept.append, backoff=0.5)

    client.send_text("oc_1", "重试")

    assert len(http.calls) == 3
    assert slept == [0.5]


def test_retries_on_network_error_then_raises():
    slept = []
    http = _Recorder([_token_ok(), ConnectionError("boom"), ConnectionError("boom2")])
    client = FeishuApiClient("cli_a", "sec", http=http, sleep=slept.append, max_attempts=2)

    with pytest.raises(FeishuApiError):
        client.send_text("oc_1", "失败")
    assert slept == [0.5]


def test_business_error_code_raises():
    http = _Recorder([_token_ok(), (200, json.dumps({"code": 230002, "msg": "Bot is not in the chat"}))])
    client = FeishuApiClient("cli_a", "sec", http=http)

    with pytest.raises(FeishuApiError) as exc:
        client.send_text("oc_1", "x")
    assert exc.value.code == 230002
    assert "not in the chat" in str(exc.value)


def test_non_json_response_raises():
    http = _Recorder([_token_ok(), (200, "<html>bad gateway</html>")])
    with pytest.raises(FeishuApiError):
        FeishuApiClient("cli_a", "sec", http=http).send_text("oc_1", "x")


def test_missing_token_in_response_raises():
    http = _Recorder([(200, json.dumps({"code": 0, "msg": "no token"}))])
    with pytest.raises(FeishuApiError):
        FeishuApiClient("cli_a", "sec", http=http).tenant_access_token()
