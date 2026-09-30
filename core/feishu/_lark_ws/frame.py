"""飞书长连接帧编解码（本仓库自写，替代上游的 protobuf 生成代码）。

为什么自己写：上游 lark-oapi 用 protoc 3.19 之前的老式生成代码（手工构造
Descriptor），并为此在包内夹带了一整份 protobuf 运行时副本（约 50 个文件）。
官方 protobuf ≥4.21 明确拒绝这种老式生成代码（实测报 "Descriptors cannot be
created directly"），因此要么夹带旧 protobuf、要么把 protobuf 钉死在 3.20.x。
本协议只有两个消息、九个字段，且只用 varint 与长度前缀两种线格式，手写更小、
零依赖、可完整单测。

上游 schema（pbbp2.proto，proto2）：
    message Header { required string key = 1; required string value = 2; }
    message Frame {
      required uint64 SeqID            = 1;
      required uint64 LogID            = 2;
      required int32  service          = 3;
      required int32  method           = 4;
      repeated Header headers          = 5;
      optional string payload_encoding = 6;
      optional string payload_type     = 7;
      optional bytes  payload          = 8;
      optional string LogIDNew         = 9;
    }

对外接口与上游生成代码保持一致（Frame()/SerializeToString()/ParseFromString()/
headers.add()），便于将来对照上游升级。
"""

from typing import List

# 线格式
_WIRE_VARINT = 0
_WIRE_LEN = 2

# 字段号
_F_SEQ_ID = 1
_F_LOG_ID = 2
_F_SERVICE = 3
_F_METHOD = 4
_F_HEADERS = 5
_F_PAYLOAD_ENCODING = 6
_F_PAYLOAD_TYPE = 7
_F_PAYLOAD = 8
_F_LOG_ID_NEW = 9

_F_HEADER_KEY = 1
_F_HEADER_VALUE = 2

_UINT64_MASK = 0xFFFFFFFFFFFFFFFF


class Header:
    """帧头键值对（对应 pbbp2.Header）。"""

    __slots__ = ("key", "value")

    def __init__(self, key: str = "", value: str = ""):
        self.key = key
        self.value = value

    def __repr__(self):  # pragma: no cover - 调试用
        return f"Header(key={self.key!r}, value={self.value!r})"


class HeaderList(list):
    """帧头列表：提供上游生成代码的 .add() 用法。"""

    def add(self) -> Header:
        header = Header()
        self.append(header)
        return header


def _encode_varint(value: int) -> bytes:
    if value < 0:
        # int32 负数按 protobuf 语义以 64 位补码编码
        value &= _UINT64_MASK
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _decode_varint(buf: bytes, pos: int):
    result = 0
    shift = 0
    while True:
        if pos >= len(buf):
            raise ValueError("帧数据在 varint 中途结束")
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            raise ValueError("varint 超过 64 位")


def _encode_tag(field_number: int, wire_type: int) -> bytes:
    return _encode_varint((field_number << 3) | wire_type)


def _encode_bytes_field(field_number: int, value: bytes) -> bytes:
    return _encode_tag(field_number, _WIRE_LEN) + _encode_varint(len(value)) + value


def _encode_string_field(field_number: int, value: str) -> bytes:
    return _encode_bytes_field(field_number, value.encode("utf-8"))


def _encode_varint_field(field_number: int, value: int) -> bytes:
    return _encode_tag(field_number, _WIRE_VARINT) + _encode_varint(value)


def _as_bytes(value) -> bytes:
    if value is None:
        return b""
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    if isinstance(value, str):
        return value.encode("utf-8")
    raise TypeError(f"payload 需要 bytes 或 str，收到 {type(value).__name__}")


def _as_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


class Frame:
    """长连接帧（对应 pbbp2.Frame）。字段名与上游生成代码一致。"""

    def __init__(self):
        self.SeqID: int = 0
        self.LogID: int = 0
        self.service: int = 0
        self.method: int = 0
        self.headers: HeaderList = HeaderList()
        self.payload_encoding: str = ""
        self.payload_type: str = ""
        self.payload: bytes = b""
        self.LogIDNew: str = ""

    # ---- 序列化 ----

    def SerializeToString(self) -> bytes:
        out = bytearray()
        out += _encode_varint_field(_F_SEQ_ID, int(self.SeqID or 0))
        out += _encode_varint_field(_F_LOG_ID, int(self.LogID or 0))
        out += _encode_varint_field(_F_SERVICE, int(self.service or 0))
        out += _encode_varint_field(_F_METHOD, int(self.method or 0))
        for header in self.headers:
            body = _encode_string_field(_F_HEADER_KEY, _as_text(header.key))
            body += _encode_string_field(_F_HEADER_VALUE, _as_text(header.value))
            out += _encode_bytes_field(_F_HEADERS, body)
        if self.payload_encoding:
            out += _encode_string_field(_F_PAYLOAD_ENCODING, self.payload_encoding)
        if self.payload_type:
            out += _encode_string_field(_F_PAYLOAD_TYPE, self.payload_type)
        payload = _as_bytes(self.payload)
        if payload:
            out += _encode_bytes_field(_F_PAYLOAD, payload)
        if self.LogIDNew:
            out += _encode_string_field(_F_LOG_ID_NEW, self.LogIDNew)
        return bytes(out)

    # ---- 反序列化 ----

    def ParseFromString(self, raw: bytes) -> None:
        self.SeqID = 0
        self.LogID = 0
        self.service = 0
        self.method = 0
        self.headers = HeaderList()
        self.payload_encoding = ""
        self.payload_type = ""
        self.payload = b""
        self.LogIDNew = ""

        buf = bytes(raw or b"")
        pos = 0
        while pos < len(buf):
            tag, pos = _decode_varint(buf, pos)
            field_number = tag >> 3
            wire_type = tag & 0x07

            if wire_type == _WIRE_VARINT:
                value, pos = _decode_varint(buf, pos)
                if field_number == _F_SEQ_ID:
                    self.SeqID = value
                elif field_number == _F_LOG_ID:
                    self.LogID = value
                elif field_number == _F_SERVICE:
                    self.service = value
                elif field_number == _F_METHOD:
                    self.method = value
                continue

            if wire_type == _WIRE_LEN:
                length, pos = _decode_varint(buf, pos)
                if pos + length > len(buf):
                    raise ValueError("帧数据在长度前缀字段中途结束")
                chunk = buf[pos:pos + length]
                pos += length
                if field_number == _F_HEADERS:
                    self.headers.append(_parse_header(chunk))
                elif field_number == _F_PAYLOAD_ENCODING:
                    self.payload_encoding = chunk.decode("utf-8", errors="replace")
                elif field_number == _F_PAYLOAD_TYPE:
                    self.payload_type = chunk.decode("utf-8", errors="replace")
                elif field_number == _F_PAYLOAD:
                    self.payload = chunk
                elif field_number == _F_LOG_ID_NEW:
                    self.LogIDNew = chunk.decode("utf-8", errors="replace")
                continue

            if wire_type == 1:  # 64 位定长
                pos += 8
                continue
            if wire_type == 5:  # 32 位定长
                pos += 4
                continue
            raise ValueError(f"不支持的线格式 {wire_type}（字段 {field_number}）")


def _parse_header(chunk: bytes) -> Header:
    header = Header()
    pos = 0
    while pos < len(chunk):
        tag, pos = _decode_varint(chunk, pos)
        field_number = tag >> 3
        wire_type = tag & 0x07
        if wire_type == _WIRE_LEN:
            length, pos = _decode_varint(chunk, pos)
            value = chunk[pos:pos + length]
            pos += length
            if field_number == _F_HEADER_KEY:
                header.key = value.decode("utf-8", errors="replace")
            elif field_number == _F_HEADER_VALUE:
                header.value = value.decode("utf-8", errors="replace")
            continue
        if wire_type == _WIRE_VARINT:
            _, pos = _decode_varint(chunk, pos)
            continue
        if wire_type == 1:
            pos += 8
            continue
        if wire_type == 5:
            pos += 4
            continue
        raise ValueError(f"Header 中不支持的线格式 {wire_type}")
    return header


def get_header(headers: List[Header], key: str) -> str:
    """按 key 取帧头值；缺失抛 HeaderNotFoundException（与上游语义一致）。"""
    from .exception import HeaderNotFoundException

    for header in headers:
        if header.key == key:
            return header.value
    raise HeaderNotFoundException(key)
