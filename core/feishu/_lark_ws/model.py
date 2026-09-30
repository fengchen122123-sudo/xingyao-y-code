"""端点响应模型（搬运自 lark-oapi 1.7.3，MIT）。

上游：lark_oapi/ws/model.py。
改造：内联上游 core/construct.py 的 init()，去掉对 lark_oapi.core 的依赖
（本传输层不引入上游包，见 README.md）。
"""

from typing import Dict, Optional


def _init(obj, d, types: Dict) -> None:
    """把 dict 字段灌进模型对象；嵌套字段按 types 声明的类继续构造。"""
    if d is None:
        return
    for k, v in d.items():
        if not hasattr(obj, k):
            continue
        t = types.get(k)
        if t is not None and isinstance(v, dict):
            setattr(obj, k, t(v))
        else:
            setattr(obj, k, v)


class ClientConfig(object):
    _types = {}

    def __init__(self, d=None):
        self.ReconnectCount: Optional[int] = None
        self.ReconnectInterval: Optional[int] = None
        self.ReconnectNonce: Optional[int] = None
        self.PingInterval: Optional[int] = None
        _init(self, d, self._types)


class Endpoint(object):
    _types = {
        "ClientConfig": ClientConfig
    }

    def __init__(self, d=None):
        self.URL: Optional[str] = None
        self.ClientConfig: Optional[ClientConfig] = None
        _init(self, d, self._types)


class EndpointResp(object):
    _types = {
        "data": Endpoint
    }

    def __init__(self, d=None):
        self.code: Optional[int] = None
        self.msg: Optional[str] = None
        self.data: Optional[Endpoint] = None
        _init(self, d, self._types)


class Response(object):
    def __init__(self, code: int = None, headers: Dict[str, str] = None, data: bytes = None):
        self.code = code
        self.headers = headers
        self.data = data
