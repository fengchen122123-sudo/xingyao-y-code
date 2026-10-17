"""飞书长连接客户端（搬运自 lark-oapi 1.7.3，MIT，见同目录 LICENSE 与 README.md）。

上游：lark_oapi/ws/client.py（v2_main / 1.7.3）。本文件保留上游的连接建立、
心跳、收发、合包、重连与 ACK 逻辑，仅做以下改造（均以 `# [星瑶改造]` 标注）：

1. 事件回调：上游用 `EventDispatcherHandler._do_without_validation(payload)`；
   本层改成注入回调 `on_event(payload: bytes) -> Any | None`，语义一致（返回对象
   会被 JSON 序列化后回给服务端作为 ACK，返回 None 表示无内容）。
2. 去掉 client_assertion 分支：星瑶固定用 app_id + app_secret 鉴权。
3. HTTP 客户端 `requests` → 标准库 `urllib.request`，避免依赖未在
   requirements.txt 声明的包。
4. `JSON` / 日志 / 过期缓存 / User-Agent 改为本地内联实现（上游来自
   lark_oapi.core.*，本层不引入上游包）。
5. 帧编解码由本地 `frame.py` 提供（替代上游 protobuf 生成代码 + 夹带的
   protobuf 运行时副本，原因见 frame.py 顶部说明）。
6. 新增 `stop()`：上游没有停止入口，星瑶需要在应用退出时优雅关闭长连接。
"""

import asyncio
import base64
import copy
import http
import inspect
import json
import logging
import random
import threading
import time
from typing import Any, Callable, Dict, List, Mapping, Optional
from urllib.parse import urlparse, parse_qs
from urllib.request import Request, urlopen

import websockets
from websockets.exceptions import InvalidHandshake

from .const import (
    AUTH_FAILED,
    DEVICE_ID,
    EXCEED_CONN_LIMIT,
    FEISHU_DOMAIN,
    FORBIDDEN,
    GEN_ENDPOINT_URI,
    HEADER_BIZ_RT,
    HEADER_HANDSHAKE_AUTH_ERRCODE,
    HEADER_HANDSHAKE_MSG,
    HEADER_HANDSHAKE_STATUS,
    HEADER_MESSAGE_ID,
    HEADER_SEQ,
    HEADER_SUM,
    HEADER_TRACE_ID,
    HEADER_TYPE,
    INTERNAL_ERROR,
    NO_CREDENTIAL,
    OK,
    SERVICE_ID,
    SYSTEM_BUSY,
    USER_AGENT,
    UTF_8,
)
from .enum import FrameType, LogLevel, MessageType
from .exception import (
    ClientException,
    ConnectionClosedException,
    HeaderNotFoundException,
    ServerException,
    ServerUnreachableException,
)
from .frame import Frame, HeaderList
from .model import ClientConfig, EndpointResp, Response

logger = logging.getLogger("ycode.feishu.ws")

_SDK_PROJECT = "oapi-sdk-python"
_SDK_VERSION = "1.7.3"


def _build_user_agent(source: Optional[str] = None) -> str:
    """UA 格式对齐上游：<project>/v<version>[ source/<sanitized>]"""
    ua = f"{_SDK_PROJECT}/v{_SDK_VERSION}"
    if source:
        sanitized = "".join(ch if (ch.isalnum() or ch in "._-") else "_" for ch in source)[:64]
        if sanitized:
            ua += f" source/{sanitized}"
    return ua


class _ExpiringCache:
    """带过期时间的键值缓存（替代上游 lark_oapi.core.cache.ExpiringCache）。

    上游的 clear_interval 语义是惰性清理，这里按需清理，行为等价。
    """

    def __init__(self, clear_interval: int = 30):
        self._clear_interval = max(1, int(clear_interval))
        self._data: Dict[str, Any] = {}
        self._expire_at: Dict[str, float] = {}

    def get(self, key: str) -> Any:
        if key in self._data and self._expire_at.get(key, 0) > time.time():
            return self._data[key]
        return None

    def set(self, key: str, value: Any, ttl: int) -> None:
        self._data[key] = value
        self._expire_at[key] = time.time() + ttl
        if len(self._data) > 256:
            self._purge()

    def _purge(self) -> None:
        now = time.time()
        for key in [k for k, exp in self._expire_at.items() if exp <= now]:
            self._data.pop(key, None)
            self._expire_at.pop(key, None)


def _filter_null(d: Dict) -> Dict:
    """与上游 JSON.filter_null 一致：递归删除值为 None 的键。"""
    if isinstance(d, dict):
        for k, v in list(d.items()):
            if isinstance(v, dict):
                _filter_null(v)
            elif v is None:
                del d[k]
    return d


def _json_default(o: Any) -> Any:
    """与上游 JSON.Encoder.default 一致的对象/字节处理。"""
    if hasattr(o, "__dict__"):
        return _filter_null(copy.deepcopy(vars(o)))
    if isinstance(o, bytes):
        try:
            return str(o, encoding=UTF_8)
        except UnicodeDecodeError:
            return base64.b64encode(o).decode("ascii")
    if isinstance(o, set):
        return list(o)
    raise TypeError(f"无法序列化 {type(o).__name__}")


def _json_marshal(obj: Any) -> Optional[str]:
    if obj is None:
        return None
    return json.dumps(obj, default=_json_default, ensure_ascii=False)


def _json_unmarshal(text: str, clazz):
    return clazz(json.loads(text))


def _is_empty(value: Optional[str]) -> bool:
    return value is None or value == ""


def _get_by_key(headers: HeaderList, key: str) -> str:
    for header in headers:
        if header.key == key:
            return header.value
    raise HeaderNotFoundException(key)


def _new_ping_frame(service_id: int) -> Frame:
    frame = Frame()
    header = frame.headers.add()
    header.key = HEADER_TYPE
    header.value = MessageType.PING.value
    frame.service = service_id
    frame.method = FrameType.CONTROL.value
    frame.SeqID = 0
    frame.LogID = 0
    return frame


def _ordinal(n: int) -> str:
    suffixes = {1: 'st', 2: 'nd', 3: 'rd'}
    if 10 <= n <= 20:
        suffix = 'th'
    else:
        suffix = suffixes.get(n % 10, 'th')
    return str(n) + suffix


def _ws_connect_kwargs() -> Dict[str, Any]:
    params = inspect.signature(websockets.connect).parameters
    if "proxy" in params:
        # websockets 15 起默认走环境代理发现；上游历史上是直连，这里保持直连。
        return {"proxy": None}
    return {}


def _get_ws_conn_exception_headers(e):
    headers = getattr(e, "headers", None)
    if headers is not None:
        return headers
    response = getattr(e, "response", None)
    if response is None:
        return None
    return getattr(response, "headers", None)


def _parse_ws_conn_exception(e):
    headers = _get_ws_conn_exception_headers(e)
    if headers is None:
        raise e

    code = headers.get(HEADER_HANDSHAKE_STATUS)
    msg = headers.get(HEADER_HANDSHAKE_MSG)
    if code is None or msg is None:
        raise e

    code = int(code)
    if code == AUTH_FAILED:
        auth_code = headers.get(HEADER_HANDSHAKE_AUTH_ERRCODE)
        if int(auth_code) == EXCEED_CONN_LIMIT:
            raise ClientException(code, msg)
        else:
            raise ServerException(code, msg)
    elif code == FORBIDDEN:
        raise ClientException(code, msg)
    else:
        raise ServerException(code, msg)


class Client(object):
    """飞书长连接客户端：start() 阻塞在专用线程里维持连接，stop() 优雅关闭。"""

    def __init__(self,
                 app_id: str,
                 app_secret: str,
                 on_event: Optional[Callable[[bytes], Any]] = None,
                 log_level: LogLevel = LogLevel.INFO,
                 domain: str = FEISHU_DOMAIN,
                 auto_reconnect: bool = True,
                 source: Optional[str] = None,
                 headers: Optional[Mapping[str, str]] = None) -> None:
        self._app_id: str = app_id
        self._app_secret: str = app_secret
        self._on_event = on_event  # [星瑶改造] 替代上游 EventDispatcherHandler
        self._log_level: LogLevel = log_level
        self._auto_reconnect: bool = auto_reconnect
        self._domain: str = domain
        self._headers: Dict[str, str] = dict(headers or {})
        self._user_agent: str = _build_user_agent(source=source)
        self._conn: Optional[Any] = None
        self._conn_url: str = ""
        self._service_id: str = ""
        self._conn_id: str = ""
        # 本地默认值；飞书接入点会在每次握手时用 _configure() 覆盖（也可能
        # 通过 CONTROL 帧在会话中途下发更新）。
        self._reconnect_nonce: int = 30
        self._reconnect_count: int = -1
        self._reconnect_interval: int = 120
        self._ping_interval: int = 120
        self._cache: _ExpiringCache = _ExpiringCache(clear_interval=30)
        self._lock: Optional[asyncio.Lock] = None
        # [星瑶改造] 供外部观察重连生命周期
        self.on_reconnecting: Callable[[], None] = lambda: None
        self.on_reconnected: Callable[[], None] = lambda: None
        self.on_connected: Callable[[], None] = lambda: None
        # [星瑶改造] 停止支持：start() 阻塞在专用线程，stop() 从其它线程请求退出
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stop_waiter: Optional[asyncio.Event] = None
        self._stopping = False
        self._stop_lock = threading.Lock()
        logger.setLevel(log_level.value)

    # ---- 生命周期 ----

    def start(self) -> None:
        """阻塞：建立长连接并维持（调用方负责放进专用线程）。"""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._lock = asyncio.Lock()
        try:
            self._loop.run_until_complete(self._connect())
        except ClientException as e:
            logger.error(self._fmt_log("connect failed, err: {}", e))
            raise e
        except Exception as e:
            logger.error(self._fmt_log("connect failed, err: {}", e))
            self._loop.run_until_complete(self._disconnect())
            if self._auto_reconnect:
                self._loop.run_until_complete(self._reconnect())
            else:
                raise e

        if self._stopping:
            self._loop.run_until_complete(self._disconnect())
            return

        self._loop.create_task(self._ping_loop())
        self._loop.run_until_complete(self._select())

    def stop(self) -> None:
        """线程安全：请求停止（关闭连接并让 start() 返回）。"""
        with self._stop_lock:
            if self._stopping:
                return
            self._stopping = True
        loop = self._loop
        if loop is None or not loop.is_running():
            return
        try:
            loop.call_soon_threadsafe(self._signal_stop)
        except Exception:  # pragma: no cover - 关闭竞态
            logger.debug(self._fmt_log("投递停止信号失败"), exc_info=True)

    def _signal_stop(self) -> None:
        waiter = self._stop_waiter
        if waiter is not None and not waiter.is_set():
            waiter.set()
        self._loop.create_task(self._shutdown())

    async def _shutdown(self) -> None:
        try:
            await self._disconnect()
        except Exception:  # pragma: no cover - 关闭时尽力而为
            logger.debug(self._fmt_log("断开长连接失败"), exc_info=True)

    async def _select(self) -> None:
        """[星瑶改造] 上游是 `while True: sleep(3600)`；这里改成可被 stop() 唤醒。"""
        self._stop_waiter = asyncio.Event()
        await self._stop_waiter.wait()

    # ---- 连接 ----

    async def _ping_loop(self) -> None:
        while not self._stopping:
            try:
                if self._conn is not None:
                    frame = _new_ping_frame(int(self._service_id))
                    await self._write_message(frame.SerializeToString())
                    logger.debug(self._fmt_log("ping success"))
            except Exception as e:
                logger.warning(self._fmt_log("ping failed, err: {}", e))
            finally:
                await asyncio.sleep(self._ping_interval)

    async def _connect(self) -> None:
        await self._lock.acquire()
        if self._conn is not None:
            return
        try:
            conn_url = self._get_conn_url()
            u = urlparse(conn_url)
            q = parse_qs(u.query)
            conn_id = q[DEVICE_ID][0]
            service_id = q[SERVICE_ID][0]

            conn = await websockets.connect(conn_url, **_ws_connect_kwargs())
            self._conn = conn
            self._conn_url = conn_url
            self._conn_id = conn_id
            self._service_id = service_id

            logger.info(self._fmt_log("connected to {}", conn_url))
            self._loop.create_task(self._receive_message_loop())
            try:
                self.on_connected()
            except Exception:  # pragma: no cover - 观察者异常不影响连接
                logger.debug(self._fmt_log("on_connected callback raised"), exc_info=True)
        except InvalidHandshake as e:
            _parse_ws_conn_exception(e)
        finally:
            self._lock.release()

    async def _receive_message_loop(self) -> None:
        try:
            while True:
                if self._conn is None:
                    raise ConnectionClosedException("connection is closed")
                msg = await self._conn.recv()
                self._loop.create_task(self._handle_message(msg))
        except Exception as e:
            logger.error(self._fmt_log("receive message loop exit, err: {}", e))
            await self._disconnect()
            if self._stopping:
                return
            if self._auto_reconnect:
                await self._reconnect()
            else:
                raise e

    def _get_conn_url(self) -> str:
        if _is_empty(self._app_id) or _is_empty(self._app_secret):
            raise ClientException(
                NO_CREDENTIAL,
                "app_id and app_secret are required",
            )

        headers = dict(self._headers)
        headers.update({
            "locale": "zh",
            USER_AGENT: self._user_agent,
            "Content-Type": "application/json",
        })
        url = self._domain + GEN_ENDPOINT_URI
        body = {"AppID": self._app_id, "AppSecret": self._app_secret}

        # [星瑶改造] 上游用 requests.post；这里用标准库，避免依赖未声明的包
        request = Request(
            url,
            data=json.dumps(body).encode(UTF_8),
            headers=headers,
            method="POST",
        )
        try:
            with urlopen(request, timeout=30) as resp:
                status_code = resp.status
                content = resp.read()
        except Exception as e:
            raise ServerException(0, f"请求飞书长连接接入点失败: {e}")

        if status_code != http.HTTPStatus.OK:
            msg = "system busy"
            try:
                payload = json.loads(str(content, UTF_8))
                msg = payload.get("msg") or msg
            except Exception:
                pass
            raise ServerException(status_code, msg)

        resp_obj = _json_unmarshal(str(content, UTF_8), EndpointResp)
        if resp_obj.code == OK:
            pass
        elif resp_obj.code == SYSTEM_BUSY:
            raise ServerException(resp_obj.code, "system busy")
        elif resp_obj.code == INTERNAL_ERROR:
            raise ServerException(resp_obj.code, resp_obj.msg)
        else:
            raise ClientException(resp_obj.code, resp_obj.msg or "接入点返回错误")

        data = resp_obj.data
        if data is None or _is_empty(data.URL):
            raise ClientException(resp_obj.code, "接入点未返回连接地址")
        if data.ClientConfig is not None:
            self._configure(data.ClientConfig)

        return data.URL

    async def _handle_message(self, msg: bytes) -> None:
        try:
            frame = Frame()
            frame.ParseFromString(msg)
            ft = FrameType(frame.method)

            if ft == FrameType.CONTROL:
                await self._handle_control_frame(frame)
            elif ft == FrameType.DATA:
                await self._handle_data_frame(frame)
        except Exception as e:
            logger.error(self._fmt_log("handle message failed, err: {}", e))

    async def _handle_control_frame(self, frame: Frame) -> None:
        hs = frame.headers
        type_ = _get_by_key(hs, HEADER_TYPE)
        message_type = MessageType(type_)

        if message_type == MessageType.PING:
            return
        elif message_type == MessageType.PONG:
            logger.debug(self._fmt_log("receive pong"))
            if not frame.payload:
                return
            conf = _json_unmarshal(str(frame.payload, UTF_8), ClientConfig)
            self._configure(conf)

    async def _handle_data_frame(self, frame: Frame) -> None:
        hs = frame.headers
        msg_id = _get_by_key(hs, HEADER_MESSAGE_ID)
        trace_id = _get_by_key(hs, HEADER_TRACE_ID)
        sum_ = _get_by_key(hs, HEADER_SUM)
        seq = _get_by_key(hs, HEADER_SEQ)
        type_ = _get_by_key(hs, HEADER_TYPE)

        pl = frame.payload
        if int(sum_) > 1:
            # 合包
            pl = self._combine(msg_id, int(sum_), int(seq), pl)
            if pl is None:
                return

        message_type = MessageType(type_)
        logger.debug(self._fmt_log("receive message, message_type: {}, message_id: {}, trace_id: {}",
                                   message_type.value, msg_id, trace_id))

        resp = Response(code=http.HTTPStatus.OK)
        try:
            start = int(round(time.time() * 1000))
            if message_type == MessageType.EVENT:
                # [星瑶改造] 上游调 EventDispatcherHandler._do_without_validation
                result = self._on_event(pl) if self._on_event is not None else None
            elif message_type == MessageType.CARD:
                return
            else:
                return
            end = int(round(time.time() * 1000))
            header = hs.add()
            header.key = HEADER_BIZ_RT
            header.value = str(end - start)
            if result is not None:
                resp.data = base64.b64encode(_json_marshal(result).encode(UTF_8))
        except Exception as e:
            logger.error(
                self._fmt_log("handle message failed, message_type: {}, message_id: {}, trace_id: {}, err: {}",
                              message_type.value, msg_id, trace_id, e))
            resp = Response(code=http.HTTPStatus.INTERNAL_SERVER_ERROR)

        frame.payload = _json_marshal(resp).encode(UTF_8)
        await self._write_message(frame.SerializeToString())

    async def _reconnect(self) -> None:
        try:
            self.on_reconnecting()
        except Exception as e:  # pragma: no cover - 防御性
            logger.warning(self._fmt_log("on_reconnecting callback raised: {}", e))

        # 首次重连随机抖动
        if self._reconnect_nonce > 0:
            nonce = random.random() * self._reconnect_nonce
            await asyncio.sleep(nonce)

        if self._reconnect_count >= 0:
            for i in range(self._reconnect_count):
                if self._stopping:
                    return
                if await self._try_connect(i):
                    self._fire_on_reconnected()
                    return
                await asyncio.sleep(self._reconnect_interval)
            raise ServerUnreachableException(
                f"unable to connect to the server after trying {self._reconnect_count} times")
        else:
            i = 0
            while not self._stopping:
                if await self._try_connect(i):
                    self._fire_on_reconnected()
                    return
                await asyncio.sleep(self._reconnect_interval)
                i += 1

    def _fire_on_reconnected(self) -> None:
        try:
            self.on_reconnected()
        except Exception as e:  # pragma: no cover - 防御性
            logger.warning(self._fmt_log("on_reconnected callback raised: {}", e))

    async def _try_connect(self, cnt: int) -> bool:
        logger.info(self._fmt_log("trying to reconnect for the {} time", _ordinal(cnt + 1)))
        try:
            await self._connect()
            return True
        except ClientException as e:
            logger.error(self._fmt_log("connect failed, err: {}", e))
            raise e
        except Exception as e:
            logger.error(self._fmt_log("connect failed, err: {}", e))
            return False

    async def _disconnect(self) -> None:
        try:
            await self._lock.acquire()
            if self._conn is None:
                return
            await self._conn.close()
            logger.info(self._fmt_log("disconnected to {}", self._conn_url))
        finally:
            self._conn = None
            self._conn_url = ""
            self._conn_id = ""
            self._service_id = ""
            self._lock.release()

    async def _write_message(self, data: bytes) -> None:
        async with self._lock:
            if self._conn is None:
                raise ConnectionClosedException("connection is closed, write message failed")
            await self._conn.send(data)

    def _combine(self, msg_id: str, sum_: int, seq: int, bs: bytes) -> Optional[bytes]:
        val = self._cache.get(msg_id)
        if val is None:
            buf: List[bytes] = [b''] * sum_
            buf[seq] = bs
            self._cache.set(msg_id, buf, 5)
            return None

        val[seq] = bs
        pl = b''
        for v in val:
            if not v:
                self._cache.set(msg_id, val, 5)
                return None
            pl += v

        return pl

    def _configure(self, conf: ClientConfig) -> None:
        if conf.ReconnectCount is not None:
            self._reconnect_count = conf.ReconnectCount
        if conf.ReconnectInterval is not None:
            self._reconnect_interval = conf.ReconnectInterval
        if conf.ReconnectNonce is not None:
            self._reconnect_nonce = conf.ReconnectNonce
        if conf.PingInterval is not None:
            self._ping_interval = conf.PingInterval

    def _fmt_log(self, fmt: str, *args) -> str:
        text = fmt.format(*args)
        if self._conn_id != "":
            text += f' [conn_id={self._conn_id}]'
        return text
