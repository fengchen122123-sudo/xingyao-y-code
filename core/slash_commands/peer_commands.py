"""动态局域网节点斜杠命令（/<节点名>目录、/<节点名>对话、/<节点名>选模型 等）。"""

from __future__ import annotations

import re
import threading
from typing import Any, Optional

from core import lan_peers

_CACHE_LOCK = threading.Lock()
# {peer_id: {"workspaces": [...], "sessions": [...], "providers": [...], "models": [...]}}
_PEER_LIST_CACHE: dict[str, dict[str, list[dict[str, Any]]]] = {}

_SPEC_TEMPLATES: tuple[tuple[str, str, str], ...] = (
    ("目录", "/{peer}目录", "查看对端星瑶的所有工作目录"),
    ("对话", "/{peer}对话 [目录/序号]", "查看对端星瑶的历史对话与权限状态"),
    ("厂商", "/{peer}厂商", "查看对端星瑶已配置的模型厂商"),
    ("模型", "/{peer}模型 [厂商/序号]", "查看对端星瑶可用的模型列表"),
    ("选对话", "/{peer}选对话 <序号或会话名>", "绑定对端复用对话（默认完全访问+解除沙盒）"),
    ("新建", "/{peer}新建 [目录/序号] [会话名]", "在指定工作目录为对端新建并绑定对话"),
    ("选模型", "/{peer}选模型 <序号或模型名>", "切换对端当前绑定对话的模型"),
    ("状态", "/{peer}状态", "查看对端在线状态、当前绑定对话与模型"),
    ("帮助", "/{peer}帮助", "显示节点全部快捷指令与用法"),
)

_ACTION_ALIASES: dict[str, str] = {
    # 目录
    "目录": "workspaces",
    "工作目录": "workspaces",
    "查看目录": "workspaces",
    "看目录": "workspaces",
    "dir": "workspaces",
    "dirs": "workspaces",
    "workdir": "workspaces",
    "workdirs": "workspaces",
    "workspace": "workspaces",
    "workspaces": "workspaces",
    # 对话
    "对话": "sessions",
    "会话": "sessions",
    "查看对话": "sessions",
    "看对话": "sessions",
    "列对话": "sessions",
    "查看会话": "sessions",
    "session": "sessions",
    "sessions": "sessions",
    "chat": "sessions",
    "chats": "sessions",
    # 厂商
    "厂商": "providers",
    "供应商": "providers",
    "查看厂商": "providers",
    "看厂商": "providers",
    "provider": "providers",
    "providers": "providers",
    # 模型
    "模型": "models",
    "查看模型": "models",
    "看模型": "models",
    "model": "models",
    "models": "models",
    # 选对话
    "选对话": "select_session",
    "选择对话": "select_session",
    "切对话": "select_session",
    "切换对话": "select_session",
    "选会话": "select_session",
    "选择会话": "select_session",
    "切会话": "select_session",
    "切换会话": "select_session",
    "用对话": "select_session",
    "use": "select_session",
    "select": "select_session",
    # 新建对话
    "新建": "create_session",
    "新建对话": "create_session",
    "新对话": "create_session",
    "开对话": "create_session",
    "开新对话": "create_session",
    "建对话": "create_session",
    "新建会话": "create_session",
    "新会话": "create_session",
    "new": "create_session",
    "create": "create_session",
    # 选模型
    "选模型": "select_model",
    "选择模型": "select_model",
    "切模型": "select_model",
    "切换模型": "select_model",
    "换模型": "select_model",
    "setmodel": "select_model",
    # 状态与帮助
    "状态": "status",
    "详情": "status",
    "信息": "status",
    "status": "status",
    "info": "status",
    "帮助": "help",
    "help": "help",
}

_INDEX_RE = re.compile(r"^#?(\d+)$")


def _cache_set(peer_id: str, category: str, items: list[dict[str, Any]]) -> None:
    with _CACHE_LOCK:
        bucket = _PEER_LIST_CACHE.setdefault(peer_id, {})
        bucket[category] = list(items)


def _cache_get(peer_id: str, category: str) -> list[dict[str, Any]]:
    with _CACHE_LOCK:
        return list(_PEER_LIST_CACHE.get(peer_id, {}).get(category) or [])


def clear_peer_command_cache() -> None:
    with _CACHE_LOCK:
        _PEER_LIST_CACHE.clear()


def _parse_index(token: str) -> Optional[int]:
    m = _INDEX_RE.match(str(token or "").strip())
    if not m:
        return None
    val = int(m.group(1))
    return val if val >= 1 else None


def _split_first_arg(raw: str) -> tuple[str, str]:
    """按空白或成对引号拆分第一个参数与剩余文本（保留 Windows 反斜杠）。"""
    text = str(raw or "").strip()
    if not text:
        return "", ""
    quote = text[0] if text[0] in ('"', "'") else ""
    if quote:
        end = text.find(quote, 1)
        if end != -1:
            return text[1:end].strip(), text[end + 1 :].strip()
    parts = text.split(None, 1)
    first = parts[0].strip()
    rest = parts[1].strip() if len(parts) > 1 else ""
    return first, rest


def _resolve_peer_prefix(stripped_cmd: str) -> Optional[tuple[dict[str, Any], str]]:
    """从 `/<节点名>...` 解析命中的已启用节点与前缀之后的原文。"""
    if not stripped_cmd.startswith("/") or stripped_cmd.startswith("//"):
        return None
    body = stripped_cmd[1:]
    if not body:
        return None

    try:
        peers = lan_peers.list_peers()
    except Exception:
        return None

    candidates: list[tuple[str, dict[str, Any]]] = []
    seen_pairs: set[tuple[str, str]] = set()
    for peer in peers:
        if not isinstance(peer, dict) or not peer.get("enabled", True):
            continue
        pid = str(peer.get("id") or "").strip()
        pname = str(peer.get("name") or "").strip()
        for prefix in (pname, pid):
            if not prefix:
                continue
            key = (prefix.lower(), pid)
            if key not in seen_pairs:
                seen_pairs.add(key)
                candidates.append((prefix, peer))

    candidates.sort(key=lambda item: len(item[0]), reverse=True)
    body_lower = body.lower()
    for prefix, peer in candidates:
        if body_lower.startswith(prefix.lower()):
            return peer, body[len(prefix) :]
    return None


def _match_action_and_args(after_prefix: str) -> tuple[Optional[str], str]:
    """解析前缀之后的子命令与参数。未识别但形如目录路径则返回 (None, '') 放行。"""
    if not after_prefix:
        return "help", ""
    # 如 `/peer-id/src/file.py` 这类路径，不拦
    if after_prefix.startswith("/"):
        return None, ""

    if after_prefix[0].isspace():
        trimmed = after_prefix.strip()
        if not trimmed:
            return "help", ""
        first, rest = _split_first_arg(trimmed)
        action = _ACTION_ALIASES.get(first.lower())
        if action:
            return action, rest
        return "unknown", trimmed

    # 连写形式：如 `/笔记本目录` 或 `/笔记本选对话 1`
    parts = after_prefix.split(None, 1)
    verb_token = parts[0].strip()
    rest = parts[1].strip() if len(parts) > 1 else ""
    action = _ACTION_ALIASES.get(verb_token.lower())
    if action:
        return action, rest
    # 连写带 `/` 可能是文件路径，直接放行
    if "/" in verb_token or "\\" in verb_token:
        return None, ""
    return "unknown", after_prefix.strip()


def build_peer_slash_specs() -> list[dict[str, str]]:
    """为前端 `/` 补全生成动态节点命令规格列表。"""
    try:
        peers = lan_peers.list_peers()
    except Exception:
        return []
    specs: list[dict[str, str]] = []
    seen_names: set[str] = set()
    for peer in peers:
        if not isinstance(peer, dict) or not peer.get("enabled", True):
            continue
        pname = str(peer.get("name") or peer.get("id") or "").strip()
        if not pname or any(ch.isspace() for ch in pname):
            continue
        for suffix, usage_tpl, summary in _SPEC_TEMPLATES:
            cmd_name = f"/{pname}{suffix}"
            low = cmd_name.lower()
            if low in seen_names:
                continue
            seen_names.add(low)
            specs.append({
                "name": cmd_name,
                "usage": usage_tpl.format(peer=pname),
                "summary": f"[{pname}] {summary}",
                "kind": "action",
            })
    return specs


def _format_help(peer: dict[str, Any]) -> str:
    pname = str(peer.get("name") or peer.get("id") or "")
    bound_sess = str(peer.get("default_session") or "").strip() or "未指定（按主机名隔离）"
    bound_dir = str(peer.get("default_workdir") or "").strip() or "未指定"
    lines = [
        f"节点【{pname}】快捷命令（支持连写或空格分隔）：",
        f"• /{pname}目录 — 查看工作目录列表",
        f"• /{pname}对话 [目录/序号] — 查看对话列表",
        f"• /{pname}厂商 — 查看已配置模型厂商",
        f"• /{pname}模型 [厂商/序号] — 查看可用模型",
        f"• /{pname}选对话 <序号或会话名> — 绑定后续发消息复用的对话",
        f"• /{pname}新建 [目录/序号] [会话名] — 在指定目录新建并绑定对话",
        f"• /{pname}选模型 <序号或模型名> — 切换当前绑定对话的模型",
        f"• /{pname}状态 — 查看节点在线与绑定状态",
        "",
        f"当前绑定：对话={bound_sess} | 目录={bound_dir} | 默认策略=完全访问 + 沙盒已解除",
    ]
    return "\n".join(lines)


def _resolve_from_cache_or_fetch(
    peer_id: str,
    category: str,
    index: int,
    fetcher,
    list_key: str,
) -> Optional[dict[str, Any]]:
    items = _cache_get(peer_id, category)
    if not items or index > len(items):
        res = fetcher()
        items = list(res.get(list_key) or [])
        _cache_set(peer_id, category, items)
    if 1 <= index <= len(items):
        return items[index - 1]
    return None


def _execute_peer_action(peer: dict[str, Any], action: str, args: str) -> tuple[bool, str]:
    pid = str(peer.get("id") or "")
    pname = str(peer.get("name") or pid)

    if action == "help":
        return True, _format_help(peer)

    if action == "unknown":
        return False, f"未识别的节点指令：`{args}`。\n\n{_format_help(peer)}"

    if action == "status":
        res = lan_peers.fetch_peer_status(pid)
        p = res.get("peer") or peer
        bound_sess = str(p.get("default_session") or "").strip() or "未指定（按主机名隔离）"
        bound_dir = str(p.get("default_workdir") or "").strip() or "未指定"
        text = (
            f"节点【{pname}】状态\n"
            f"• 在线状态：{'在线' if res.get('online') else '离线'}（{res.get('agent') or '星瑶'}）\n"
            f"• 当前绑定对话：{bound_sess}\n"
            f"• 当前绑定目录：{bound_dir}\n"
            f"• 对端当前模型：{res.get('active_model_name') or res.get('active_model_id') or '-'}\n"
            f"• 对端前台对话：{res.get('current_session') or '-'}\n"
            f"• 工作目录数：{res.get('workspace_count', 0)} | 对话数：{res.get('session_count', 0)}\n"
            f"• 运行中任务：{res.get('active_tasks', 0)}"
        )
        return True, text

    if action == "workspaces":
        res = lan_peers.fetch_peer_workspaces(pid)
        p = res.get("peer") or peer
        items = list(res.get("workspaces") or [])
        _cache_set(pid, "workspaces", items)
        bound_dir = str(p.get("default_workdir") or "").strip()
        lines = [f"节点【{pname}】工作目录（共 {len(items)} 个）："]
        for idx, ws in enumerate(items, 1):
            mark = " ← 当前绑定" if bound_dir and ws.get("path") == bound_dir else ""
            lines.append(
                f"{idx}. {ws.get('path')}  ({ws.get('label')} · {ws.get('session_count', 0)} 个对话){mark}"
            )
        lines.append(f"\n提示：可用 `/{pname}对话 <序号>` 查看目录内对话，或 `/{pname}新建 <序号> [会话名]` 在对应目录建新对话。")
        return True, "\n".join(lines)

    if action == "sessions":
        workdir_filter = args.strip()
        if workdir_filter:
            idx = _parse_index(workdir_filter)
            if idx is not None:
                ws_item = _resolve_from_cache_or_fetch(
                    pid,
                    "workspaces",
                    idx,
                    lambda: lan_peers.fetch_peer_workspaces(pid),
                    "workspaces",
                )
                if ws_item is None:
                    return False, f"目录序号 #{idx} 超出范围，请先执行 `/{pname}目录` 查看。"
                workdir_filter = str(ws_item.get("path") or "")
        res = lan_peers.fetch_peer_sessions(pid, workdir=workdir_filter)
        p = res.get("peer") or peer
        items = list(res.get("sessions") or [])
        _cache_set(pid, "sessions", items)
        bound_sess = str(p.get("default_session") or "").strip()
        header = f"节点【{pname}】对话列表"
        if workdir_filter:
            header += f"（目录：{workdir_filter}）"
        lines = [f"{header}（共 {len(items)} 个）："]
        if not items:
            lines.append("（暂无对话）")
        for idx, s in enumerate(items, 1):
            mark = " ← 当前绑定" if bound_sess and s.get("name") == bound_sess else ""
            perm = s.get("permission_mode") or "confirm"
            sb = "沙盒已解除" if s.get("sandbox_bypass") else "沙盒开启"
            lines.append(
                f"{idx}. {s.get('name')} | 目录={s.get('workdir')} | 消息={s.get('count', 0)} | "
                f"模型={s.get('model_name') or '-'} | {perm}/{sb}{mark}"
            )
        lines.append(f"\n提示：输入 `/{pname}选对话 <序号或会话名>` 即可绑定并继续在该对话上下文内交流。")
        return True, "\n".join(lines)

    if action == "providers":
        res = lan_peers.fetch_peer_providers(pid)
        items = list(res.get("providers") or [])
        _cache_set(pid, "providers", items)
        lines = [f"节点【{pname}】模型厂商（共 {len(items)} 个）："]
        if not items:
            lines.append("（未配置厂商）")
        for idx, prov in enumerate(items, 1):
            preview = ", ".join(prov.get("models_preview") or [])
            lines.append(
                f"{idx}. {prov.get('name')} (id={prov.get('id')} · {prov.get('model_count', 0)} 个模型 · "
                f"{prov.get('base_url_host') or '-'})" + (f" | {preview}" if preview else "")
            )
        lines.append(f"\n提示：输入 `/{pname}模型 <厂商名或序号>` 查看该厂商下的模型。")
        return True, "\n".join(lines)

    if action == "models":
        prov_filter = args.strip()
        if prov_filter:
            idx = _parse_index(prov_filter)
            if idx is not None:
                prov_item = _resolve_from_cache_or_fetch(
                    pid,
                    "providers",
                    idx,
                    lambda: lan_peers.fetch_peer_providers(pid),
                    "providers",
                )
                if prov_item is None:
                    return False, f"厂商序号 #{idx} 超出范围，请先执行 `/{pname}厂商` 查看。"
                prov_filter = str(prov_item.get("id") or prov_item.get("name") or "")
        res = lan_peers.fetch_peer_models(pid, provider=prov_filter)
        items = list(res.get("models") or [])
        _cache_set(pid, "models", items)
        active_id = str(res.get("active_model_id") or "")
        sess_label = str(res.get("session_name") or "").strip() or "对端全局默认"
        header = f"节点【{pname}】可用模型"
        if prov_filter:
            header += f"（厂商：{prov_filter}）"
        lines = [f"{header}（共 {len(items)} 个 · 作用对话={sess_label}）："]
        if not items:
            lines.append("（未找到匹配模型）")
        for idx, m in enumerate(items, 1):
            mark = " ← 当前使用" if active_id and m.get("id") == active_id else ""
            lines.append(
                f"{idx}. {m.get('name')} (id={m.get('id')} · {m.get('provider_name')} · "
                f"ctx={m.get('context_window', 0)}){mark}"
            )
        lines.append(f"\n提示：输入 `/{pname}选模型 <序号或模型名>` 为当前绑定对话切换模型。")
        return True, "\n".join(lines)

    if action == "select_session":
        raw_target = args.strip()
        if not raw_target:
            return False, f"用法：`/{pname}选对话 <序号或会话名>`（先用 `/{pname}对话` 查看列表）"
        target_name = raw_target
        idx = _parse_index(raw_target)
        if idx is not None:
            sess_item = _resolve_from_cache_or_fetch(
                pid,
                "sessions",
                idx,
                lambda: lan_peers.fetch_peer_sessions(pid),
                "sessions",
            )
            if sess_item is None:
                return False, f"对话序号 #{idx} 超出范围，请先执行 `/{pname}对话` 查看。"
            target_name = str(sess_item.get("name") or "")
        res = lan_peers.select_peer_session(
            pid,
            target_name,
            permission_mode="full",
            sandbox_bypass=True,
        )
        if not res.get("ok"):
            return False, str(res.get("msg") or "选择对话失败")
        sess = res.get("session") or {}
        return True, (
            f"✅ 节点【{pname}】已选择并绑定对话：{sess.get('name')}\n"
            f"• 工作目录：{sess.get('workdir')}\n"
            f"• 当前模型：{sess.get('model_name') or '-'}\n"
            f"• 权限与沙盒：{sess.get('permission_mode')} / {'沙盒已解除' if sess.get('sandbox_bypass') else '沙盒开启'}\n"
            f"后续在本对话中向【{pname}】发消息将直接复用该对话上下文，不再新建随机对话。"
        )

    if action == "create_session":
        raw = args.strip()
        target_workdir = ""
        target_name = ""
        if raw:
            first, rest = _split_first_arg(raw)
            idx = _parse_index(first)
            if idx is not None:
                ws_item = _resolve_from_cache_or_fetch(
                    pid,
                    "workspaces",
                    idx,
                    lambda: lan_peers.fetch_peer_workspaces(pid),
                    "workspaces",
                )
                if ws_item is None:
                    return False, f"目录序号 #{idx} 超出范围，请先执行 `/{pname}目录` 查看。"
                target_workdir = str(ws_item.get("path") or "")
                target_name = rest
            elif first.upper() == "GLOBAL" or first == "全局":
                target_workdir = "GLOBAL"
                target_name = rest
            else:
                # 若未加引号且目录本身含空格，尝试先匹配已知工作目录前缀
                try:
                    ws_res = lan_peers.fetch_peer_workspaces(pid)
                    ws_paths = [
                        str(w.get("path") or "")
                        for w in (ws_res.get("workspaces") or [])
                        if str(w.get("path") or "")
                    ]
                except Exception:
                    ws_paths = []
                matched_ws = ""
                raw_low = raw.lower()
                for wp in sorted(ws_paths, key=len, reverse=True):
                    wp_low = wp.lower()
                    if raw_low == wp_low or (raw_low.startswith(wp_low) and raw[len(wp) : len(wp) + 1].isspace()):
                        matched_ws = wp
                        break
                if matched_ws:
                    target_workdir = matched_ws
                    target_name = raw[len(matched_ws) :].strip()
                else:
                    target_workdir = first
                    target_name = rest
        res = lan_peers.create_peer_session(
            pid,
            workdir=target_workdir,
            session_name=target_name,
            permission_mode="full",
            sandbox_bypass=True,
        )
        if not res.get("ok"):
            return False, str(res.get("msg") or "新建对话失败")
        sess = res.get("session") or {}
        return True, (
            f"✅ 节点【{pname}】已新建并绑定对话：{sess.get('name')}\n"
            f"• 工作目录：{sess.get('workdir')}\n"
            f"• 当前模型：{sess.get('model_name') or '-'}\n"
            f"• 权限与沙盒：{sess.get('permission_mode')} / {'沙盒已解除' if sess.get('sandbox_bypass') else '沙盒开启'}\n"
            f"后续向【{pname}】发消息将直接落在这个新对话内。"
        )

    if action == "select_model":
        raw_model = args.strip()
        if not raw_model:
            return False, f"用法：`/{pname}选模型 <序号或模型名>`（先用 `/{pname}模型` 查看列表）"
        target_model = raw_model
        idx = _parse_index(raw_model)
        if idx is not None:
            model_item = _resolve_from_cache_or_fetch(
                pid,
                "models",
                idx,
                lambda: lan_peers.fetch_peer_models(pid),
                "models",
            )
            if model_item is None:
                return False, f"模型序号 #{idx} 超出范围，请先执行 `/{pname}模型` 查看。"
            target_model = str(model_item.get("id") or model_item.get("name") or "")
        res = lan_peers.select_peer_model(pid, target_model)
        if not res.get("ok"):
            return False, str(res.get("msg") or "切换模型失败")
        sess_label = str(res.get("session_name") or "").strip() or "对端全局默认"
        return True, (
            f"✅ 节点【{pname}】已切换模型\n"
            f"• 作用对象：{sess_label}\n"
            f"• 当前模型：{res.get('active_model_name')}（{res.get('provider_name')} · id={res.get('active_model_id')}）"
        )

    return False, _format_help(peer)


def try_dispatch_peer_command(text: str):
    """若输入命中 `/<节点名>xxxx` 或 `/<节点名> xxxx`，直接走 HTTP 查询/操作并返回 DispatchResult。"""
    from core.slash_commands.dispatch import DispatchResult

    raw = "" if text is None else str(text)
    stripped = raw.lstrip()
    matched = _resolve_peer_prefix(stripped)
    if matched is None:
        return None
    peer, after_prefix = matched
    action, args = _match_action_and_args(after_prefix)
    if action is None:
        return None

    try:
        ok, message = _execute_peer_action(peer, action, args)
    except Exception as exc:
        pname = str(peer.get("name") or peer.get("id") or "")
        return DispatchResult(
            handled=True,
            kind="error",
            error=f"节点【{pname}】请求失败：{exc}",
        )

    if ok:
        return DispatchResult(handled=True, kind="notice", notice=message)
    return DispatchResult(handled=True, kind="error", error=message)
