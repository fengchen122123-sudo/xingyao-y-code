"""T2：搬运的飞书长连接传输层单测。

覆盖：帧编解码与 protobuf 线格式一致性（黄金字节）、ACK 回写路径、
接入点请求构造与错误分支、websockets 版本红线。
"""

import asyncio
import base64
import json

import pytest

import websockets

from core.feishu._lark_ws import client as ws_client
from core.feishu._lark_ws.const import NO_CREDENTIAL
from core.feishu._lark_ws.enum import FrameType, MessageType
from core.feishu._lark_ws.exception import ClientException, HeaderNotFoundException, ServerException
from core.feishu._lark_ws.frame import Frame, HeaderList, get_header


# ---------------------------------------------------------------- 帧编解码

def test_frame_encoding_matches_protobuf_wire_format():
    """黄金字节：与 protobuf 对同一 Frame 的编码逐字节一致。

    pbbp2.proto: SeqID=1(uint64) LogID=2(uint64) service=3(int32) method=4(int32)
                 headers=5(repeated Header) payload=8(bytes)
    Header: key=1(string) value=2(string)
    """
    frame = Frame()
    frame.SeqID = 1
    frame.LogID = 2
    frame.service = 3
    frame.method = 1
    header = frame.headers.add()
    header.key = "type"
    header.value = "event"
    frame.payload = b"{}"

    expected = bytes.fromhex(
        "0801"          # SeqID = 1
        "1002"          # LogID = 2
        "1803"          # service = 3
        "2001"          # method = 1
        "2a0d"          # headers[0] (len 13)
        "0a04" + "74797065"      # key = "type"
        "1205" + "6576656e74"    # value = "event"
        "4202" + "7b7d"          # payload = b"{}"
    )
    assert frame.SerializeToString() == expected


def test_frame_roundtrip_with_multibyte_varint_and_many_headers():
    frame = Frame()
    frame.SeqID = 300           # 需要两字节 varint
    frame.LogID = 0
    frame.service = 9
    frame.method = FrameType.DATA.value
    for key, value in (("message_id", "m-1"), ("trace_id", "t-2"), ("sum", "1"), ("seq", "0"), ("type", "event")):
        item = frame.headers.add()
        item.key = key
        item.value = value
    frame.payload = "中文载荷".encode("utf-8")

    parsed = Frame()
    parsed.ParseFromString(frame.SerializeToString())

    assert (parsed.SeqID, parsed.LogID, parsed.service, parsed.method) == (300, 0, 9, 1)
    assert [(h.key, h.value) for h in parsed.headers] == [
        ("message_id", "m-1"), ("trace_id", "t-2"), ("sum", "1"), ("seq", "0"), ("type", "event"),
    ]
    assert parsed.payload.decode("utf-8") == "中文载荷"
    # varint 300 应为两字节 AC 02
    assert frame.SerializeToString()[1:3] == bytes.fromhex("ac02")


def test_frame_empty_payload_and_optional_fields_omitted():
    frame = Frame()
    frame.service = 1
    frame.method = 0
    raw = frame.SerializeToString()
    # 可选字段（payload/payload_type/LogIDNew 等）为空时不写入
    assert raw == bytes.fromhex("0800100018012000")
    parsed = Frame()
    parsed.ParseFromString(raw)
    assert parsed.payload == b"" and parsed.payload_type == ""


def test_parse_rejects_truncated_buffer():
    frame = Frame()
    frame.service = 1
    raw = frame.SerializeToString()
    with pytest.raises(ValueError):
        Frame().ParseFromString(raw[:-1])


def test_get_header_found_and_missing():
    headers = HeaderList()
    item = headers.add()
    item.key = "type"
    item.value = "event"
    assert get_header(headers, "type") == "event"
    with pytest.raises(HeaderNotFoundException):
        get_header(headers, "absent")


# ---------------------------------------------------------------- ACK 回写路径

def test_event_frame_ack_uses_injected_callback():
    """EVENT 数据帧 → 调用注入回调 → ACK 帧带回结果与 biz_rt 头。"""
    received = []

    def on_event(payload: bytes):
        received.append(payload)
        return {"ok": True}

    client = ws_client.Client("cli_x", "secret", on_event=on_event)

    frame = Frame()
    frame.SeqID = 1
    frame.LogID = 2
    frame.service = 9
    frame.method = FrameType.DATA.value
    for key, value in (
        ("message_id", "m-1"), ("trace_id", "t-2"), ("sum", "1"), ("seq", "0"),
        ("type", MessageType.EVENT.value),
    ):
        item = frame.headers.add()
        item.key = key
        item.value = value
    frame.payload = b'{"header":{"event_type":"im.message.receive_v1"}}'

    written = []

    class _FakeConn:
        async def send(self, data: bytes):
            written.append(data)

    client._conn = _FakeConn()
    client._lock = asyncio.Lock()
    asyncio.run(client._handle_message(frame.SerializeToString()))

    assert received == [b'{"header":{"event_type":"im.message.receive_v1"}}']
    assert len(written) == 1

    ack = Frame()
    ack.ParseFromString(written[0])
    assert "biz_rt" in [h.key for h in ack.headers]
    body = json.loads(ack.payload.decode("utf-8"))
    assert body["code"] == 200
    assert json.loads(base64.b64decode(body["data"]).decode("utf-8")) == {"ok": True}


def test_card_frame_is_ignored_without_ack():
    """CARD 帧上游直接 return，不写 ACK。"""
    client = ws_client.Client("cli_x", "secret", on_event=lambda _p: {"ok": True})

    frame = Frame()
    frame.method = FrameType.DATA.value
    for key, value in (
        ("message_id", "m-1"), ("trace_id", "t-2"), ("sum", "1"), ("seq", "0"),
        ("type", MessageType.CARD.value),
    ):
        item = frame.headers.add()
        item.key = key
        item.value = value

    written = []

    class _FakeConn:
        async def send(self, data: bytes):
            written.append(data)

    client._conn = _FakeConn()
    client._lock = asyncio.Lock()
    asyncio.run(client._handle_message(frame.SerializeToString()))
    assert written == []


def test_combine_multi_part_payload():
    """合包：sum>1 时按 seq 拼装，未集齐返回 None，集齐返回完整载荷。"""
    client = ws_client.Client("cli_x", "secret")
    assert client._combine("m-1", 2, 0, b"AA") is None
    assert client._combine("m-1", 2, 1, b"BB") == b"AABB"


# ---------------------------------------------------------------- 接入点请求

class _FakeResponse:
    def __init__(self, payload: dict, status: int = 200):
        self.status = status
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _patch_urlopen(monkeypatch, response, captured):
    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["headers"] = {k.lower(): v for k, v in request.headers.items()}
        return response

    monkeypatch.setattr(ws_client, "urlopen", fake_urlopen)


def test_get_conn_url_posts_credentials_and_applies_client_config(monkeypatch):
    captured = {}
    _patch_urlopen(
        monkeypatch,
        _FakeResponse({
            "code": 0,
            "data": {
                "URL": "wss://example.invalid/ws?device_id=d1&service_id=s1",
                "ClientConfig": {"PingInterval": 42, "ReconnectNonce": 7},
            },
        }),
        captured,
    )

    client = ws_client.Client("cli_app", "app_secret_value")
    url = client._get_conn_url()

    assert url.endswith("device_id=d1&service_id=s1")
    assert captured["url"] == "https://open.feishu.cn/callback/ws/endpoint"
    assert captured["body"] == {"AppID": "cli_app", "AppSecret": "app_secret_value"}
    assert "user-agent" in captured["headers"]
    assert captured["headers"]["user-agent"].startswith("oapi-sdk-python/v")
    # ClientConfig 生效
    assert client._ping_interval == 42
    assert client._reconnect_nonce == 7


def test_get_conn_url_rejects_error_code(monkeypatch):
    captured = {}
    _patch_urlopen(monkeypatch, _FakeResponse({"code": 1000040346, "msg": "invalid app"}), captured)

    client = ws_client.Client("cli_bad", "bad")
    with pytest.raises(ClientException) as exc:
        client._get_conn_url()
    assert exc.value.code == 1000040346


def test_get_conn_url_requires_credentials():
    with pytest.raises(ClientException) as exc:
        ws_client.Client("", "")._get_conn_url()
    assert exc.value.code == NO_CREDENTIAL


def test_get_conn_url_rejects_non_200(monkeypatch):
    captured = {}
    _patch_urlopen(monkeypatch, _FakeResponse({"msg": "system busy"}, status=503), captured)
    with pytest.raises(ServerException) as exc:
        ws_client.Client("cli_x", "y")._get_conn_url()
    assert exc.value.code == 503


# ---------------------------------------------------------------- 版本红线

def test_websockets_version_untouched():
    """红线：飞书通道不得改动 websockets 版本（必须仍是 17.x）。"""
    assert websockets.__version__.split(".")[0] == "17", (
        f"websockets 版本被改动：{websockets.__version__}（飞书集成红线要求保持 17.x）"
    )
