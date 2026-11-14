"""浏览器到工人的桥接。网关只转发帧，不解释 RPC。"""

from __future__ import annotations

import json


def upstream_hello(worker_token: str, last_seq, last_boot_id) -> dict:
    return {
        "token": worker_token,
        "last_seq": last_seq,
        "last_boot_id": last_boot_id,
    }


def open_agent(first: dict, dial) -> dict:
    """dial(account_id) 返回带 token 的上游，或 None。全新连接两项水位都是 null。"""
    last_seq = first.get("last_seq")
    last_boot_id = first.get("last_boot_id")
    link = dial(first["account_id"])
    if link is None or link.get("status") == "lost":
        boot_id = None if link is None else link.get("boot_id")
        return {
            "ok": False,
            "client_frames": [
                {"type": "worker_starting", "data": {"boot_id": None}},
                {"type": "worker_lost", "data": {"boot_id": boot_id}},
            ],
            "upstream_frame": None,
        }
    frame = upstream_hello(link["token"], last_seq, last_boot_id)
    return {"ok": True, "client_frames": [], "upstream_frame": frame, "link": link}


def preview_request(account_id: str, public_path: str, worker_token: str) -> dict:
    """浏览器只带账号 JWT。工人令牌由网关注入，并且只访问该账号自己的容器。"""
    name = f"xingyao-worker-{account_id}"
    return {
        "url": f"http://{name}:8766{public_path}",
        "headers": {"Authorization": f"Bearer {worker_token}"},
    }
