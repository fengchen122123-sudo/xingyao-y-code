"""飞书出站 API 客户端（发消息 / 发卡片 / 原地更新卡片）。

只用标准库（urllib），不引入新依赖。HTTP 实现可注入，便于单测。
"""

import json
import logging
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from core.feishu._lark_ws.const import FEISHU_DOMAIN, UTF_8

logger = logging.getLogger("ycode.feishu.client")

TOKEN_PATH = "/open-apis/auth/v3/tenant_access_token/internal"
MESSAGE_PATH = "/open-apis/im/v1/messages"
# 令牌提前刷新余量（秒）
TOKEN_REFRESH_MARGIN = 60
RETRY_STATUS = (429, 500, 502, 503, 504)

# http(method, url, headers, body_bytes, timeout) -> (status_code, response_text)
HttpCallable = Callable[[str, str, Dict[str, str], Optional[bytes], float], Tuple[int, str]]


class FeishuApiError(Exception):
    def __init__(self, message: str, code: Optional[int] = None, status: Optional[int] = None):
        super().__init__(message)
        self.code = code
        self.status = status

    def __str__(self) -> str:
        parts = [super().__str__()]
        if self.code is not None:
            parts.append(f"code={self.code}")
        if self.status is not None:
            parts.append(f"http={self.status}")
        return " / ".join(parts)


def _default_http(method: str, url: str, headers: Dict[str, str], body: Optional[bytes], timeout: float) -> Tuple[int, str]:
    request = Request(url, data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as resp:
            return resp.status, resp.read().decode(UTF_8, errors="replace")
    except Exception as e:
        # HTTPError 携带响应体（飞书的错误说明在这里），必须取出而不是直接抛
        status = getattr(e, "code", 0)
        body_text = ""
        try:
            body_text = e.read().decode(UTF_8, errors="replace")  # type: ignore[attr-defined]
        except Exception:
            body_text = str(e)
        if status:
            return int(status), body_text
        raise


class FeishuApiClient:
    """飞书开放平台出站调用（app_id + app_secret 换取 tenant_access_token）。"""

    def __init__(self,
                 app_id: str,
                 app_secret: str,
                 domain: str = FEISHU_DOMAIN,
                 http: Optional[HttpCallable] = None,
                 timeout: float = 15.0,
                 max_attempts: int = 3,
                 backoff: float = 0.5,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.time) -> None:
        self._app_id = app_id
        self._app_secret = app_secret
        self._domain = domain.rstrip("/")
        self._http = http or _default_http
        self._timeout = timeout
        self._max_attempts = max(1, int(max_attempts))
        self._backoff = backoff
        self._sleep = sleep
        self._clock = clock
        self._token = ""
        self._token_expire_at = 0.0

    # ---- 令牌 ----

    def tenant_access_token(self, force: bool = False) -> str:
        if not force and self._token and self._clock() < self._token_expire_at:
            return self._token
        payload = self._request(
            "POST", TOKEN_PATH,
            body={"app_id": self._app_id, "app_secret": self._app_secret},
            with_token=False,
        )
        token = str(payload.get("tenant_access_token") or "")
        if not token:
            raise FeishuApiError("飞书未返回 tenant_access_token", status=200)
        expire = int(payload.get("expire") or 7200)
        self._token = token
        self._token_expire_at = self._clock() + max(60, expire - TOKEN_REFRESH_MARGIN)
        return token

    # ---- 消息 ----

    def send_text(self, chat_id: str, text: str, reply_to: Optional[str] = None) -> Dict[str, Any]:
        return self._send(chat_id, "text", {"text": text}, reply_to=reply_to)

    def send_card(self, chat_id: str, card: Dict[str, Any], reply_to: Optional[str] = None) -> Dict[str, Any]:
        return self._send(chat_id, "interactive", card, reply_to=reply_to)

    def update_card(self, message_id: str, card: Dict[str, Any]) -> Dict[str, Any]:
        """原地更新卡片（点按钮后的回执走这里，不再多发一条消息）。"""
        return self._request(
            "PATCH", f"{MESSAGE_PATH}/{message_id}",
            body={"content": json.dumps(card, ensure_ascii=False)},
        )

    def _send(self, chat_id: str, msg_type: str, content: Any, reply_to: Optional[str] = None) -> Dict[str, Any]:
        body = {"msg_type": msg_type, "content": json.dumps(content, ensure_ascii=False)}
        if reply_to:
            return self._request("POST", f"{MESSAGE_PATH}/{reply_to}/reply", body=body)
        body["receive_id"] = chat_id
        return self._request("POST", MESSAGE_PATH, query={"receive_id_type": "chat_id"}, body=body)

    # ---- 传输 ----

    def _request(self, method: str, path: str, *, query: Optional[Dict[str, str]] = None,
                 body: Optional[Dict[str, Any]] = None, with_token: bool = True) -> Dict[str, Any]:
        url = self._domain + path
        if query:
            url = f"{url}?{urlencode(query)}"
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if with_token:
            headers["Authorization"] = f"Bearer {self.tenant_access_token()}"
        data = json.dumps(body, ensure_ascii=False).encode(UTF_8) if body is not None else None

        last_error: Optional[Exception] = None
        for attempt in range(self._max_attempts):
            if attempt:
                self._sleep(self._backoff * (2 ** (attempt - 1)))
            try:
                status, text = self._http(method, url, headers, data, self._timeout)
            except Exception as e:
                last_error = e
                logger.warning("飞书请求失败（第 %d 次）: %s %s -> %s", attempt + 1, method, path, e)
                continue
            if status in RETRY_STATUS:
                last_error = FeishuApiError("飞书服务暂时不可用", status=status)
                logger.warning("飞书返回 %s（第 %d 次）: %s", status, attempt + 1, path)
                continue
            return self._parse(status, text, path)

        if isinstance(last_error, FeishuApiError):
            raise last_error
        raise FeishuApiError(f"飞书请求失败: {last_error}", status=0)

    @staticmethod
    def _parse(status: int, text: str, path: str) -> Dict[str, Any]:
        try:
            payload = json.loads(text or "{}")
        except Exception:
            raise FeishuApiError(f"飞书响应不是 JSON（HTTP {status}）: {text[:200]}", status=status)
        if not isinstance(payload, dict):
            raise FeishuApiError(f"飞书响应格式异常（HTTP {status}）", status=status)
        code = payload.get("code")
        if code not in (0, None):
            raise FeishuApiError(str(payload.get("msg") or "飞书接口返回错误"), code=int(code), status=status)
        if status >= 400:
            raise FeishuApiError(f"飞书接口返回 HTTP {status}", status=status)
        return payload
