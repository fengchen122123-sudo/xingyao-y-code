"""飞书长连接常量（搬运自 lark-oapi 1.7.3，MIT，见同目录 LICENSE 与 README.md）。

上游来源：lark_oapi/ws/const.py 与 lark_oapi/core/const.py 中本传输层用到的常量。
"""

# ---- 端点与查询参数 ----
GEN_ENDPOINT_URI = "/callback/ws/endpoint"
DEVICE_ID = "device_id"
SERVICE_ID = "service_id"

# ---- 域名与协议常量（上游 lark_oapi/core/const.py）----
FEISHU_DOMAIN = "https://open.feishu.cn"
UTF_8 = "UTF-8"
USER_AGENT = "User-Agent"

# ---- 错误码 ----
OK = 0
SYSTEM_BUSY = 1
FORBIDDEN = 403
AUTH_FAILED = 514
INTERNAL_ERROR = 1000040343
NO_CREDENTIAL = 1000040344
EXCEED_CONN_LIMIT = 1000040350

# ---- 帧头键 ----
HEADER_TYPE = "type"
HEADER_MESSAGE_ID = "message_id"
HEADER_SUM = "sum"
HEADER_SEQ = "seq"
HEADER_TRACE_ID = "trace_id"
HEADER_BIZ_RT = "biz_rt"
HEADER_HANDSHAKE_STATUS = "handshake-status"
HEADER_HANDSHAKE_MSG = "handshake-msg"
HEADER_HANDSHAKE_AUTH_ERRCODE = "handshake-autherrcode"
