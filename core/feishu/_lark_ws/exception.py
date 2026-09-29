"""飞书长连接异常类型（搬运自 lark-oapi 1.7.3，MIT）。

上游：lark_oapi/ws/exception.py。
"""


class ClientException(Exception):
    def __init__(self, code: int, msg: str):
        super().__init__(msg)
        self.code = code

    def __str__(self):
        return f"{self.code}: {super().__str__()}"


class ServerException(Exception):
    def __init__(self, code: int, msg: str):
        super().__init__(msg)
        self.code = code

    def __str__(self):
        return f"{self.code}: {super().__str__()}"


class HeaderNotFoundException(Exception):
    def __init__(self, key: str):
        self.key = key

    def __str__(self):
        return f"{self.key} not found"


class ConnectionClosedException(Exception):
    def __init__(self, msg: str):
        super().__init__(msg)


class ServerUnreachableException(Exception):
    def __init__(self, msg: str):
        super().__init__(msg)
