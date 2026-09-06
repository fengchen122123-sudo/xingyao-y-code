"""受管浏览器执行进程的 IPC 协议（R2 第二段）。

父进程与子进程之间只传结构化 JSON 行（stdin/stdout 各一条管道）：

- 请求：``{"id": int, "op": "observe"|"execute"|"release"|"close", ...}``；
  操作白名单固定，不传 Callable、eval 或 shell 字符串；
- 响应：``{"id": int, "ok": bool, "error_code": str, "error_message": str,
  ...}``；
- 事件（子进程 → 父进程，无 id）：``{"event": "ready"|"boot_failed", ...}``。

观察截图绝不跨管道传输：子进程把 PNG 写入自己创建的短期目录并在 ready
事件里报告目录路径，响应只携带 png_path/sha256 等元数据；PNG 永不进入
日志。动作 payload 只在本机管道内存中存在，协议层不写任何日志。

Playwright 对象全部留在子进程内；父进程不持有任何 Playwright 对象，
线程/进程归属由进程边界天然保证（设计 12.1 的进程版）。
"""

from __future__ import annotations

import json
from typing import Any

# 操作白名单：父进程能命令子进程做的全部事情。
OPERATIONS = ("observe", "execute", "release", "close")

# ComputerObservation 中子进程回传父进程的字段（不含 png 字节与内部对象）。
OBSERVATION_FIELDS = (
    "run_id",
    "target",
    "action",
    "ok",
    "screen_sha256",
    "png_path",
    "width",
    "height",
    "original_width",
    "original_height",
    "origin_x",
    "origin_y",
    "scale_x",
    "scale_y",
    "window_title",
    "page_origin",
    "page_title",
    "surface_id",
    "surface_generation",
)

# ComputerAction 中经 JSON 往返后需要还原为 tuple 的字段。
_TUPLE_FIELDS = {
    "keys": (str,),
    "path": ((int, float), (int, float)),
}


def encode_request(command_id: int, op: str, **payload: Any) -> str:
    """请求编码：op 必须在白名单内（父进程侧出口校验）。"""
    if op not in OPERATIONS:
        raise ValueError(f"未知的受管进程操作: {op!r}")
    request: dict[str, Any] = {"id": int(command_id), "op": op}
    request.update(payload)
    return json.dumps(request, ensure_ascii=False)


def decode_request(line: str) -> dict:
    """子进程侧严格解码：op 白名单外直接拒绝（fail-closed）。"""
    request = json.loads(line)
    if not isinstance(request, dict):
        raise ValueError("受管进程请求必须是 JSON 对象")
    op = request.get("op")
    if op not in OPERATIONS:
        raise ValueError(f"未知的受管进程操作: {op!r}")
    command_id = request.get("id")
    if not isinstance(command_id, int) or isinstance(command_id, bool):
        raise ValueError("受管进程请求缺少整数 id")
    return request


def encode_response(command_id: int, **payload: Any) -> str:
    response: dict[str, Any] = {"id": int(command_id), "ok": bool(payload.pop("ok", True))}
    response.update(payload)
    return json.dumps(response, ensure_ascii=False)


def decode_response(line: str) -> dict:
    """父进程侧解码一行响应/事件。"""
    message = json.loads(line)
    if not isinstance(message, dict):
        raise ValueError("受管进程响应必须是 JSON 对象")
    return message


def normalize_action_payload(payload: dict) -> dict:
    """把 asdict(ComputerAction) 的 JSON 形态还原为可构造 ComputerAction 的形态。

    JSON 往返会把 tuple 变成 list；这里把 path/keys 还原为 tuple（path 元素
    还原为 int——round-trip 中 int 不会变 float，float 只可能来自非法输入，
    由下游既有严格校验拒绝）。
    """
    normalized = dict(payload)
    keys = normalized.get("keys")
    if isinstance(keys, list):
        normalized["keys"] = tuple(str(key) for key in keys)
    path = normalized.get("path")
    if isinstance(path, list):
        normalized["path"] = tuple(
            (int(point[0]), int(point[1])) for point in path
        )
    return normalized


def observation_payload(observation) -> dict:
    """ComputerObservation → 协议字段字典（只含 OBSERVATION_FIELDS）。"""
    return {field: getattr(observation, field) for field in OBSERVATION_FIELDS}
