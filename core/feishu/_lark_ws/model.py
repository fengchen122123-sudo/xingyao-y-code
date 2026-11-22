from typing import Dict, Optional


def _init(obj, d, types: Dict) -> None:
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
