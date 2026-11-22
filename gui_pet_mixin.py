from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_KEY_ENABLED = "pet_enabled"
_KEY_ELECTRON_PATH = "pet_electron_path"
_KEY_NOTIFY = "pet_notify_enabled"
_KEY_SKIN = "pet_skin_id"

_CHAT_IDLE_MIN = 60
_CHAT_IDLE_MAX = 86400

# ---------- 任务气泡提示（emit_event → 桌宠冒泡）----------
# 气泡默认停留时长（毫秒）。不同事件会按重要性给更长时长，提问/权限还会设为常驻。
_NOTIFY_BUBBLE_MS = 4500

# 同一文案的最小发送间隔（秒）。白名单里现在全是「值得打断用户」的事件，这条只在
# 将来加回高频事件时兜底用。
_NOTIFY_THROTTLE_SECONDS = 1.5

_ERROR_DEDUP_SECONDS = 2.0
_last_error_notify = (0.0, "")

_NOTIFY_EVENTS = frozenset({
    "permission_request",
    "permission_dismiss",
    "turn_finish",
    "turn_error",
    "error",
    "turn_cancelled",
})

_NOTIFY_ALWAYS_EVENTS = _NOTIFY_EVENTS

# 需要权限的常见工具 → 简短易懂的提示后缀（气泡最多两行，要一眼看懂）
_PERMISSION_TOOL_LABELS = {
    "execute_command": "执行命令",
    "sandbox_outside_access": "访问项目外路径",
    "write_file": "写入文件",
    "edit_file": "修改文件",
    "delete_file": "删除文件",
    "undo_turn": "撤销回合",
}

_notify_last_sent: Dict[str, float] = {}


def _clip_notify_snippet(raw: Any, limit: int = 12) -> str:
    text = " ".join(str(raw or "").split()).strip()
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit].rstrip("，。、：:,. ") + "…"


def _notify_bubble_spec(event_type: str, data: Any) -> Optional[Dict[str, Any]]:
    payload = data if isinstance(data, dict) else {}
    if event_type == "permission_request":
        name = str(payload.get("function_name") or "")
        if name == "ask_user":
            args = payload.get("arguments")
            q_raw = args.get("question") if isinstance(args, dict) else ""
            snippet = _clip_notify_snippet(q_raw, limit=12)
            text = f"等你选：{snippet}" if snippet else "有选项等你选一下～"
            return {
                "text": text,
                "ms": 15000,
                "kind": "question",
                "emote": "interact",
                "sticky": True,
            }
        action = _PERMISSION_TOOL_LABELS.get(name, "")
        if not action:
            summary = str(payload.get("summary") or "").splitlines()[0] if payload.get("summary") else ""
            action = _clip_notify_snippet(summary, limit=10)
        text = f"需要权限：{action}，等你点头～" if action else "需要你确认权限，等你点头～"
        return {
            "text": text,
            "ms": 15000,
            "kind": "permission",
            "emote": "look_around",
            "sticky": True,
        }
    if event_type == "turn_finish":
        return {
            "text": "任务完成啦！快回来看看～",
            "ms": 6000,
            "kind": "finish",
            "emote": "happy",
            "sticky": False,
        }
    if event_type in ("turn_error", "error"):
        snippet = _clip_notify_snippet(payload.get("message"), limit=12)
        text = f"出错啦：{snippet}" if snippet else "任务出错啦，快回来看看！"
        return {
            "text": text,
            "ms": 8000,
            "kind": "error",
            "emote": "look_around",
            "sticky": False,
        }
    if event_type == "turn_cancelled":
        return {
            "text": "任务已停手了",
            "ms": 3200,
            "kind": "cancel",
            "emote": "",
            "sticky": False,
        }
    return None


def _notify_bubble_text(event_type: str, data: Any) -> Optional[str]:
    """兼容入口：返回事件对应的桌宠气泡文案；不该冒泡时返回 None。"""
    spec = _notify_bubble_spec(event_type, data)
    return spec["text"] if spec else None


_EMPTY_STATUS = {
    "running": False,
    "pid": None,
    "electron_path": None,
    "pet_dir": "",
    "available": False,
}

# ---------- Electron 运行时状态（detect + status 合并）----------
# detect() 的 shape（core/pet_runtime.py 的探测结果）异常/不可用时的骨架。
_EMPTY_RUNTIME_DETECT = {
    "installed": False,
    "exe": None,
    "version": None,
    "size": 0,
}

_EMPTY_RUNTIME = {
    "installed": False,
    "exe": None,
    "version": None,
    "size": 0,
    "state": "idle",
    "downloaded": 0,
    "total": 0,
    "percent": 0,
    "msg": "",
}

_RUNTIME_DETECT_TTL = 600.0
_runtime_detect_cache = (0.0, None, None)


def _pet_manager():
    from core import pet_manager

    return pet_manager


def _pet_runtime_module():
    try:
        from core import pet_runtime

        return pet_runtime
    except Exception:
        pass
    import pet_runtime

    return pet_runtime


def _pet_skins_module():
    try:
        from core import pet_skins

        return pet_skins
    except Exception:
        pass
    import pet_skins

    return pet_skins


def _pet_chat_module():
    try:
        from core import pet_chat

        return pet_chat
    except Exception:
        pass
    import pet_chat

    return pet_chat


def _pet_chat_available_models() -> list:
    models: list = []
    try:
        from core import model_manager

        data = model_manager.load_config() or {}
    except Exception:
        logger.warning("读取模型列表失败（桌宠闲聊设置）", exc_info=True)
        return models
    try:
        for provider in data.get("providers") or []:
            if not isinstance(provider, dict):
                continue
            provider_name = str(provider.get("name") or "")
            for model in provider.get("models") or []:
                if not isinstance(model, dict):
                    continue
                model_id = str(model.get("id") or model.get("model_id") or "").strip()
                if not model_id:
                    continue
                models.append({
                    "id": model_id,
                    "name": str(model.get("name") or model_id),
                    "provider_name": provider_name,
                })
    except Exception:
        logger.warning("解析模型列表失败（桌宠闲聊设置）", exc_info=True)
    return models


def _chat_minutes_text(seconds: int) -> str:
    """把空闲秒数写成「给人看的分钟数」：整分钟不带小数，否则留一位。

    闲聊间隔用户是按分钟想的（「10 分钟」比「600 秒」直观），但合法区间下探到 60
    秒、上探到 86400 秒，一律换成分钟会出现 "0.5" / "1440" 这种不自然会话用大数，
    所以整分钟才取整，其余保留一位小数。
    """
    try:
        minutes = float(seconds) / 60.0
    except (TypeError, ValueError):
        return "若干"
    rounded = round(minutes)
    if abs(minutes - rounded) < 0.05:
        return str(int(rounded))
    return f"{minutes:.1f}"


def _runtime_detect_cached(module, state: str) -> Dict[str, Any]:
    global _runtime_detect_cache
    prev: Optional[Dict[str, Any]] = None
    cached_state = None
    try:
        ts, cached_state, raw = _runtime_detect_cache
        if isinstance(raw, dict):
            prev = raw
        fresh = (time.monotonic() - float(ts)) < _RUNTIME_DETECT_TTL
    except Exception:
        fresh = False
    if prev is not None and (cached_state == state or (fresh and prev.get("installed"))):
        return dict(prev)
    try:
        info = module.detect()
    except Exception:
        logger.debug("探测 Electron 运行时失败（沿用上次结果）", exc_info=True)
        return dict(prev) if prev is not None else dict(_EMPTY_RUNTIME_DETECT)
    if not isinstance(info, dict):
        return dict(prev) if prev is not None else dict(_EMPTY_RUNTIME_DETECT)
    snapshot = dict(info)
    _runtime_detect_cache = (time.monotonic(), state, snapshot)
    return dict(snapshot)


def _config_value(key: str, default=None):
    try:
        import config
    except Exception:
        return default
    try:
        val = getattr(config, key, None)
    except Exception:
        val = None
    if val not in (None, ""):
        return val
    try:
        return config._load().get(key, default)
    except Exception:
        return default


def _config_bool(key: str, default: bool = False) -> bool:
    try:
        return bool(_config_value(key, default))
    except Exception:
        return default


def _safe_int(value, default: int = 0) -> int:
    """把进度类字段转成 int：None/空串/脏值一律回落 default，绝不抛。"""
    try:
        return int(value)
    except Exception:
        return default


def _save_config(key: str, value) -> bool:
    try:
        from config import save_config_value

        return bool(save_config_value(key, value))
    except Exception:
        logger.warning("保存配置项 %s 失败", key, exc_info=True)
        return False


class PetBridgeMixin:
    """为 ApiBridge 提供 pet_* 系列桥方法（桌宠开关 / Electron 路径 / 重启）。"""

    def pet_get_status(self) -> Dict[str, Any]:
        enabled = _config_bool(_KEY_ENABLED, False)
        notify_enabled = _config_bool(_KEY_NOTIFY, True)
        runtime = self._pet_runtime_snapshot_safe()
        try:
            status = _pet_manager().get_status()
        except Exception as exc:
            logger.warning("查询桌宠状态失败: %s", exc, exc_info=True)
            payload = dict(_EMPTY_STATUS)
            payload.update({
                "ok": False,
                "msg": f"查询桌宠状态失败：{exc}",
                "enabled": enabled,
                "notify_enabled": notify_enabled,
                "runtime": runtime,
            })
            return payload
        if not isinstance(status, dict):
            status = dict(_EMPTY_STATUS)
            status["msg"] = "桌宠状态返回值异常（非字典）"
        payload = dict(status)
        payload["enabled"] = enabled
        payload["notify_enabled"] = notify_enabled
        payload["runtime"] = runtime
        return payload

    def _pet_runtime_snapshot(self) -> Dict[str, Any]:
        module = _pet_runtime_module()
        raw = module.status()
        if not isinstance(raw, dict):
            raw = {}
        state = str(raw.get("state") or "idle")
        info = _runtime_detect_cached(module, state)
        runtime = dict(_EMPTY_RUNTIME)
        runtime["installed"] = bool(info.get("installed"))
        runtime["exe"] = info.get("exe") if runtime["installed"] else None
        runtime["version"] = info.get("version") or raw.get("version") or None
        runtime["size"] = _safe_int(info.get("size"))
        runtime["state"] = state
        runtime["downloaded"] = _safe_int(raw.get("downloaded"))
        runtime["total"] = _safe_int(raw.get("total"))
        runtime["percent"] = _safe_int(raw.get("percent"))
        runtime["msg"] = str(raw.get("msg") or "")
        return runtime

    def _pet_runtime_snapshot_safe(self) -> Dict[str, Any]:
        """_pet_runtime_snapshot 的兜底版：失败时给形状相同的空骨架 + 原因。

        pet_get_status 被前端高频轮询，绝不能因为运行时模块出问题就整体失败。
        """
        try:
            return self._pet_runtime_snapshot()
        except Exception as exc:
            logger.debug("查询 Electron 运行时状态失败: %s", exc, exc_info=True)
            fallback = dict(_EMPTY_RUNTIME)
            fallback["state"] = "unknown"
            fallback["msg"] = f"查询运行时状态失败：{exc}"
            return fallback

    def pet_get_runtime_status(self) -> Dict[str, Any]:
        """只查 Electron 运行时状态：已装/装在哪/多大 + 下载进度。

        返回 {"ok": True, "runtime": {...}}；任何异常转 {"ok": False, "msg"}，
        绝不抛给 js_api 线程。
        """
        try:
            return {"ok": True, "runtime": self._pet_runtime_snapshot()}
        except Exception as exc:
            logger.warning("查询 Electron 运行时状态失败: %s", exc, exc_info=True)
            return {"ok": False, "msg": f"查询 Electron 运行时状态失败：{exc}"}

    def pet_install_runtime(self) -> Dict[str, Any]:
        try:
            module = _pet_runtime_module()
            module.install()
        except Exception as exc:
            logger.warning("启动 Electron 运行时下载失败: %s", exc, exc_info=True)
            return {"ok": False, "msg": f"启动 Electron 运行时安装失败：{exc}"}

        runtime = self._pet_runtime_snapshot_safe()
        state = str(runtime.get("state") or "")
        if runtime.get("installed") or state == "done":
            msg = "已安装"
        elif state == "failed":
            # 后台线程已经失败：如实把原因带出去（ok 指"请求已受理"，详情在 runtime）。
            msg = str(runtime.get("msg") or "下载失败，请再点一次重试")
        else:
            msg = "正在下载…"
        return {"ok": True, "runtime": runtime, "msg": msg}

    def pet_set_notify_enabled(self, enabled: bool) -> Dict[str, Any]:
        want = bool(enabled)
        saved = _save_config(_KEY_NOTIFY, want)
        if saved:
            msg = "任务气泡提示已开启" if want else "任务气泡提示已关闭"
        else:
            msg = (
                f"配置未能写入 config.json（{_KEY_NOTIFY}={want}），"
                "重启星瑶后该开关可能不会保持，请检查 config.json 是否可写"
            )
        return {"ok": saved, "msg": msg, "status": self.pet_get_status()}

    def _pet_notify_event(self, event_type: str, data: Any = None) -> None:
        global _last_error_notify
        try:
            if event_type not in _NOTIFY_EVENTS:
                return
            if not _config_bool(_KEY_NOTIFY, True):
                return
            if event_type == "permission_dismiss":
                _pet_manager().clear_bubble(("question", "permission"))
                return

            now = time.monotonic()
            if event_type in ("turn_error", "error"):
                raw_msg = str(data.get("message") or "") if isinstance(data, dict) else ""
                prev_ts, prev_msg = _last_error_notify
                if event_type == "error" and (now - prev_ts) < _ERROR_DEDUP_SECONDS and raw_msg == prev_msg:
                    return
                _last_error_notify = (now, raw_msg)

            spec = _notify_bubble_spec(event_type, data)
            if not spec or not spec.get("text"):
                return
            text = spec["text"]
            if event_type not in _NOTIFY_ALWAYS_EVENTS:
                last = _notify_last_sent.get(text)
                if last is not None and (now - last) < _NOTIFY_THROTTLE_SECONDS:
                    return
            _notify_last_sent[text] = now
            result = _pet_manager().push_bubble(
                text,
                int(spec.get("ms") or _NOTIFY_BUBBLE_MS),
                kind=str(spec.get("kind") or ""),
                emote=str(spec.get("emote") or ""),
                sticky=bool(spec.get("sticky")),
            )
            # 桌宠未运行是常态（用户没开桌宠），不值得刷日志；统一降到 debug。
            if isinstance(result, dict) and not result.get("ok"):
                logger.debug("桌宠气泡未推送（%s）：%s", text, result.get("msg"))
        except Exception:
            logger.debug("桌宠气泡提示失败 event=%s", event_type, exc_info=True)

    def _pet_failed(self, msg: str) -> Dict[str, Any]:
        return {"ok": False, "msg": msg, "status": self.pet_get_status()}

    def pet_set_enabled(self, enabled: bool) -> Dict[str, Any]:
        want = bool(enabled)
        notes = []

        try:
            manager = _pet_manager()
            result = manager.start() if want else manager.stop()
        except Exception as exc:
            logger.warning("切换桌宠开关失败 enabled=%s: %s", want, exc, exc_info=True)
            notes.append(f"桌宠{'启动' if want else '停止'}异常：{exc}")
            result = {"ok": False}

        if not isinstance(result, dict):
            result = {"ok": False}
        action_ok = bool(result.get("ok"))
        if result.get("msg"):
            notes.insert(0, str(result["msg"]))

        # 动作失败时落盘"与实际一致"的值：启动失败即视为关闭（开关回落），停止失败
        # 即视为仍在运行（开关保持开）。宁可界面显示失败，也不要让开关和桌面分叉。
        persisted = want if action_ok else (not want)
        saved = _save_config(_KEY_ENABLED, persisted)
        if not saved:
            notes.append(
                f"配置未能写入 config.json（{_KEY_ENABLED}={persisted}），"
                "重启星瑶后该开关可能不会保持，请检查 config.json 是否可写"
            )
        elif not action_ok and want:
            notes.append("开关已回落到关闭，避免与桌面实际状态不一致")

        ok = action_ok and saved
        if not notes:
            notes.append("桌宠已启用" if want else "桌宠已关闭")
        return {"ok": ok, "msg": "；".join(notes), "status": self.pet_get_status()}

    def pet_set_electron_path(self, path: str) -> Dict[str, Any]:
        raw = str(path or "").strip()
        saved = _save_config(_KEY_ELECTRON_PATH, raw)
        notes = []
        if not saved:
            notes.append(
                f"配置未能写入 config.json（{_KEY_ELECTRON_PATH}），"
                "新路径可能未生效，请检查 config.json 是否可写"
            )

        display = raw or "（使用桌宠内置的 Electron）"
        try:
            manager = _pet_manager()
            status = manager.get_status()
        except Exception as exc:
            logger.warning("保存 Electron 路径后查询状态失败: %s", exc, exc_info=True)
            notes.append(f"查询桌宠状态失败：{exc}")
            return {"ok": False, "msg": "；".join(notes), "status": self.pet_get_status()}

        running = bool(isinstance(status, dict) and status.get("running"))
        ok = saved
        if not running:
            notes.insert(0, f"Electron 路径已保存：{display}（桌宠未在运行，下次启动生效）")
        else:
            try:
                stop_result = manager.stop()
            except Exception as exc:
                logger.warning("重启桌宠前停止失败: %s", exc, exc_info=True)
                stop_result = {"ok": False, "msg": f"停止桌宠异常：{exc}"}
            if not isinstance(stop_result, dict) or not stop_result.get("ok"):
                stop_msg = (
                    stop_result.get("msg") if isinstance(stop_result, dict) else ""
                ) or "桌宠未能停止"
                notes.insert(
                    0,
                    f"Electron 路径已保存：{display}，但桌宠未能停止，新路径尚未生效（{stop_msg}）",
                )
                ok = False
            else:
                try:
                    start_result = manager.start()
                except Exception as exc:
                    logger.warning("用新路径启动桌宠失败: %s", exc, exc_info=True)
                    start_result = {"ok": False, "msg": f"启动桌宠异常：{exc}"}
                if not isinstance(start_result, dict):
                    start_result = {"ok": False}
                start_msg = str(start_result.get("msg") or "")
                if start_result.get("ok"):
                    notes.insert(0, f"Electron 路径已保存并重启生效：{display}")
                    if start_msg:
                        notes.insert(1, start_msg)
                else:
                    notes.insert(
                        0,
                        f"Electron 路径已保存：{display}，但用新路径重启桌宠失败"
                        + (f"（{start_msg}）" if start_msg else ""),
                    )
                    ok = False

        if not notes:
            notes.append(f"Electron 路径已保存：{display}")
        return {"ok": ok, "msg": "；".join(notes), "status": self.pet_get_status()}

    def pet_list_skins(self) -> Dict[str, Any]:
        try:
            pet_skins = _pet_skins_module()
            active = pet_skins.active_skin_id()
            return {
                "ok": True,
                "skins": pet_skins.list_skins(active),
                "active": active,
                "user_dir": str(pet_skins.user_skin_dir()),
                "msg": "",
            }
        except Exception as exc:
            logger.warning("读取宠物列表失败: %s", exc, exc_info=True)
            try:
                active = _pet_skins_module().BUILTIN_SKIN_ID
            except Exception:
                active = "xingyao"
            return {
                "ok": False,
                "skins": [],
                "active": active,
                "user_dir": "",
                "msg": f"读取宠物列表失败：{exc}",
            }

    def pet_set_skin(self, skin_id: str) -> Dict[str, Any]:
        want = str(skin_id or "").strip()
        try:
            pet_skins = _pet_skins_module()
            skin = pet_skins.get_skin(want)
            if skin is None:
                return {
                    "ok": False,
                    "msg": "找不到这只宠物（可能已被删除）",
                    "status": self.pet_get_status(),
                }
            name = str(skin.get("name") or want)
            if not _save_config(_KEY_SKIN, want):
                return {
                    "ok": False,
                    "msg": (
                        f"配置未能写入 config.json（{_KEY_SKIN}={want}），"
                        "重启星瑶后可能不保持，请检查 config.json 是否可写"
                    ),
                    "status": self.pet_get_status(),
                }

            notes = [f"已切换到「{name}」"]
            ok = True
            manager = None
            running = False
            try:
                manager = _pet_manager()
                running = bool(manager.get_status().get("running"))
            except Exception as exc:
                # 查不到就当作"没在运行"：宁可不重启（形象已写盘，下次启动即生效），
                # 也不要盲目 stop/start 把用户正在用的桌宠弄掉。
                logger.warning("切换宠物后查询桌宠状态失败: %s", exc, exc_info=True)
                notes.append(f"查询桌宠状态失败：{exc}")

            if not running:
                notes[0] = f"{notes[0]}，下次打开桌宠时生效"
            else:
                try:
                    stop_result = manager.stop()
                except Exception as exc:
                    logger.warning("切换宠物时停止桌宠失败: %s", exc, exc_info=True)
                    stop_result = {"ok": False, "msg": f"停止桌宠异常：{exc}"}
                if not isinstance(stop_result, dict) or not stop_result.get("ok"):
                    stop_msg = (
                        stop_result.get("msg") if isinstance(stop_result, dict) else ""
                    ) or "桌宠未能停止"
                    notes[0] = f"{notes[0]}，但新形象尚未生效"
                    notes.append(f"桌宠未能停止（{stop_msg}）")
                    ok = False
                else:
                    try:
                        start_result = manager.start()
                    except Exception as exc:
                        logger.warning("切换宠物后重启桌宠失败: %s", exc, exc_info=True)
                        start_result = {"ok": False, "msg": f"启动桌宠异常：{exc}"}
                    if not isinstance(start_result, dict):
                        start_result = {"ok": False, "msg": "启动返回值异常（非字典）"}
                    ok = bool(start_result.get("ok"))
                    start_msg = str(start_result.get("msg") or "")
                    if ok:
                        notes[0] = f"{notes[0]}，桌宠已重新打开"
                        if start_msg:
                            notes.append(start_msg)
                    else:
                        notes[0] = f"{notes[0]}，但重启桌宠失败，新形象尚未生效"
                        notes.append(start_msg or "未取得启动结果")

            return {"ok": ok, "msg": "；".join(notes), "status": self.pet_get_status()}
        except Exception as exc:
            logger.warning("切换宠物失败 skin_id=%r: %s", skin_id, exc, exc_info=True)
            return {
                "ok": False,
                "msg": f"切换宠物失败：{exc}",
                "status": self.pet_get_status(),
            }

    def pet_import_skin(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """导入用户自定义桌宠（支持图片/GIF 序列帧或 ZIP 宠物包），可选立即激活。"""
        import base64

        if not isinstance(payload, dict):
            return {"ok": False, "msg": "参数格式不正确"}
        mode = str(payload.get("mode") or "images").strip().lower()
        name = str(payload.get("name") or "").strip()
        persona = str(payload.get("persona") or "").strip()
        description = str(payload.get("description") or "").strip()
        activate = bool(payload.get("activate", True))
        files = payload.get("files") or []
        if not isinstance(files, list) or not files:
            return {"ok": False, "msg": "请先选择要导入的文件"}

        raw_items: list[bytes] = []
        for item in files:
            if not isinstance(item, dict):
                continue
            b64 = str(item.get("data") or "").strip()
            if not b64:
                continue
            if "," in b64 and b64.startswith("data:"):
                b64 = b64.split(",", 1)[1]
            try:
                raw_items.append(base64.b64decode(b64, validate=False))
            except Exception:
                continue

        if not raw_items:
            return {"ok": False, "msg": "文件解码失败，请重试"}

        try:
            pet_skins = _pet_skins_module()
            if mode == "zip":
                res = pet_skins.import_skin_from_zip(
                    raw_items[0],
                    name=name,
                    persona=persona,
                    description=description,
                )
            else:
                res = pet_skins.import_skin_from_images(
                    name,
                    raw_items,
                    persona=persona,
                    description=description,
                )
        except Exception as exc:
            logger.warning("导入桌宠失败: %s", exc, exc_info=True)
            return {"ok": False, "msg": f"导入失败：{exc}"}

        if not res.get("ok"):
            return res

        skin = res.get("skin") or {}
        skin_id = str(skin.get("id") or "")
        if activate and skin_id:
            switch_res = self.pet_set_skin(skin_id)
            return {
                "ok": True,
                "msg": res.get("msg") or "导入成功",
                "skin": skin,
                "status": switch_res.get("status"),
                "list": self.pet_list_skins(),
            }
        return {
            "ok": True,
            "msg": res.get("msg") or "导入成功",
            "skin": skin,
            "list": self.pet_list_skins(),
        }

    def pet_update_skin(self, skin_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """更新自定义桌宠的名称与人设（persona）。"""
        if not isinstance(payload, dict):
            return {"ok": False, "msg": "参数格式不正确"}
        try:
            pet_skins = _pet_skins_module()
            res = pet_skins.update_skin_meta(
                str(skin_id or "").strip(),
                name=payload.get("name"),
                persona=payload.get("persona"),
                description=payload.get("description"),
            )
            if res.get("ok"):
                res["list"] = self.pet_list_skins()
            return res
        except Exception as exc:
            logger.warning("更新桌宠人设失败 skin_id=%r: %s", skin_id, exc, exc_info=True)
            return {"ok": False, "msg": f"保存失败：{exc}"}

    def pet_delete_skin(self, skin_id: str) -> Dict[str, Any]:
        """删除自定义桌宠；若删掉的是当前激活形象则切回内置星瑶并按需重启。"""
        try:
            pet_skins = _pet_skins_module()
            res = pet_skins.delete_skin(str(skin_id or "").strip())
            if not res.get("ok"):
                return res
            if res.get("was_active"):
                try:
                    manager = _pet_manager()
                    if manager.get_status().get("running"):
                        manager.stop()
                        manager.start()
                except Exception as exc:
                    logger.debug("删除激活桌宠后重启失败: %s", exc)
            res["list"] = self.pet_list_skins()
            res["status"] = self.pet_get_status()
            return res
        except Exception as exc:
            logger.warning("删除桌宠失败 skin_id=%r: %s", skin_id, exc, exc_info=True)
            return {"ok": False, "msg": f"删除失败：{exc}"}

    def pet_test_bubble(self, text: str = "") -> Dict[str, Any]:
        """向运行中的桌宠发送一条测试气泡，用于验证与星瑶系统的集成效果。"""
        try:
            manager = _pet_manager()
            if not manager.get_status().get("running"):
                return {"ok": False, "msg": "请先开启「显示桌宠」再测试冒泡"}
            msg = str(text or "").strip()
            if not msg:
                try:
                    skin = _pet_skins_module().resolve_active(_pet_skins_module().active_skin_id())
                    skin_name = str(skin.get("name") or "星瑶")
                except Exception:
                    skin_name = "星瑶"
                msg = f"你好呀，我是{skin_name}，已接入星瑶系统！"
            res = manager.push_bubble(msg, 3600)
            if isinstance(res, dict) and res.get("ok"):
                return {"ok": True, "msg": "已在桌宠上显示测试气泡"}
            err = res.get("msg") if isinstance(res, dict) else ""
            return {"ok": False, "msg": err or "发送气泡失败"}
        except Exception as exc:
            logger.warning("测试桌宠气泡失败: %s", exc, exc_info=True)
            return {"ok": False, "msg": f"发送气泡失败：{exc}"}

    def pet_chat_get_settings(self) -> Dict[str, Any]:
        try:
            state = _pet_chat_module().status()
            return {
                "ok": True,
                "enabled": bool(state.get("enabled")),
                "idle_seconds": int(state.get("idle_seconds") or 0),
                "model_id": str(state.get("model_id") or ""),
                "models": _pet_chat_available_models(),
                "status": state,
                "msg": "",
            }
        except Exception as exc:
            logger.warning("读取桌宠闲聊设置失败: %s", exc, exc_info=True)
            return {
                "ok": False,
                "enabled": False,
                "idle_seconds": 600,
                "model_id": "",
                "models": [],
                "status": {},
                "msg": f"读取桌宠闲聊设置失败：{exc}",
            }

    def pet_chat_set_settings(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        data = payload if isinstance(payload, dict) else {}
        kwargs: Dict[str, Any] = {}
        notes = []
        try:
            pet_chat = _pet_chat_module()

            if "enabled" in data:
                kwargs["enabled"] = bool(data.get("enabled"))

            if "idle_seconds" in data:
                raw = data.get("idle_seconds")
                value = None
                if not isinstance(raw, bool):
                    try:
                        value = int(raw)
                    except (TypeError, ValueError):
                        value = None
                if value is None:
                    notes.append("空闲时长不是数字，已忽略")
                elif value < _CHAT_IDLE_MIN or value > _CHAT_IDLE_MAX:
                    clamped = max(_CHAT_IDLE_MIN, min(_CHAT_IDLE_MAX, value))
                    notes.append(
                        f"空闲时长 {value} 秒超出 {_CHAT_IDLE_MIN}~{_CHAT_IDLE_MAX} 秒范围，"
                        f"已按 {clamped} 秒处理"
                    )
                    kwargs["idle_seconds"] = clamped
                else:
                    kwargs["idle_seconds"] = value

            model_name = ""
            if "model_id" in data:
                model_id = str(data.get("model_id") or "").strip()
                if model_id:
                    models = _pet_chat_available_models()
                    match = next((m for m in models if m["id"] == model_id), None)
                    if match is None:
                        return {
                            "ok": False,
                            "msg": "找不到这个模型（可能已被删除）",
                            "status": pet_chat.status(),
                        }
                    model_name = match["name"]
                kwargs["model_id"] = model_id

            result = pet_chat.update_settings(**kwargs)
            status = result if isinstance(result, dict) else pet_chat.status()

            parts = []
            if "enabled" in kwargs:
                if kwargs["enabled"]:
                    minutes = _chat_minutes_text(status.get("idle_seconds"))
                    parts.append(f"已开启，空闲 {minutes} 分钟后会自己说一句")
                else:
                    parts.append("已关闭")
            if "model_id" in kwargs:
                if kwargs["model_id"]:
                    parts.append(f"已切换到模型「{model_name}」")
                else:
                    parts.append("已改为自动选模型")
            if "idle_seconds" in kwargs and "enabled" not in kwargs:
                minutes = _chat_minutes_text(status.get("idle_seconds"))
                if status.get("enabled"):
                    parts.append(f"空闲时长已改为 {minutes} 分钟")
                else:
                    parts.append(f"空闲时长已改为 {minutes} 分钟（开启后生效）")
            parts.extend(notes)
            if not parts:
                parts.append("未做修改")
            if status.get("saved") is False:
                parts.append("但配置未能写入 config.json，重启星瑶后可能不保持")
            return {"ok": True, "msg": "；".join(parts), "status": status}
        except Exception as exc:
            logger.warning("保存桌宠闲聊设置失败 payload=%r: %s", payload, exc, exc_info=True)
            return {"ok": False, "msg": f"保存桌宠闲聊设置失败：{exc}", "status": {}}

    def pet_restart(self) -> Dict[str, Any]:
        """重启桌宠：先 stop 再 start，用于"改了桌宠代码/配置后手动重载"。"""
        notes = []
        try:
            manager = _pet_manager()
            stop_result = manager.stop()
        except Exception as exc:
            logger.warning("重启桌宠时停止失败: %s", exc, exc_info=True)
            return self._pet_failed(f"重启桌宠失败：停止阶段异常（{exc}）")

        if not isinstance(stop_result, dict) or not stop_result.get("ok"):
            stop_msg = (stop_result.get("msg") if isinstance(stop_result, dict) else "") or "未知原因"
            return self._pet_failed(f"重启桌宠失败：桌宠未能停止（{stop_msg}）")

        try:
            start_result = manager.start()
        except Exception as exc:
            logger.warning("重启桌宠时启动失败: %s", exc, exc_info=True)
            return self._pet_failed(f"重启桌宠失败：启动阶段异常（{exc}）")

        if not isinstance(start_result, dict):
            start_result = {"ok": False, "msg": "启动返回值异常（非字典）"}
        if start_result.get("msg"):
            notes.append(str(start_result["msg"]))
        ok = bool(start_result.get("ok"))
        if not ok and not notes:
            notes.append("重启桌宠失败：未取得启动结果")
        return {"ok": ok, "msg": "；".join(notes), "status": self.pet_get_status()}

    def _pet_auto_restore(self) -> None:
        """星瑶启动时的延迟自动恢复入口（由 ApiBridge.__init__ 的 Timer 调用）。

        在后台线程执行：配置里 pet_enabled 为真才拉起桌宠。任何异常只记日志，
        绝不外抛——桌宠起不来不能影响已经启动的 GUI，更不能让定时器线程炸掉。
        """
        try:
            result = _pet_manager().ensure_started_if_enabled()
        except Exception:
            logger.warning("桌宠自动恢复失败（已忽略，不影响 GUI）", exc_info=True)
            return
        # 闲聊调度线程跟着启动一起拉起（start() 自身幂等，重复调用只留一条线程）。
        # 整个包 try：模块缺失、线程起不来都只是「没有闲聊」这种可选功能失效，
        # 绝不能让定时器线程里的异常冒出去影响 GUI 启动。
        try:
            _pet_chat_module().start()
        except Exception:
            logger.debug("桌宠闲聊调度线程启动失败（已忽略）", exc_info=True)
        try:
            if isinstance(result, dict) and not result.get("skipped"):
                level = logging.INFO if result.get("ok") else logging.WARNING
                logger.log(level, "桌宠自动恢复：%s", result.get("msg") or result)
            elif isinstance(result, dict):
                logger.debug("桌宠自动恢复跳过：%s", result.get("msg"))
        except Exception:
            logger.debug("记录桌宠自动恢复结果失败", exc_info=True)
