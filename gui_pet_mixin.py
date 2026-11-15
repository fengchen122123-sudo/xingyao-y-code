"""GUI 桌面宠物桥接 Mixin。

把设置页「桌面宠物」的开关/路径/重启指令转发给 core.pet_manager 的进程管理，
并把最终状态回推前端：每个方法都返回 dict，方法名即前端 JS 侧 API 名
（pet_get_status / pet_set_enabled / pet_set_electron_path / pet_restart）。

另有一条「Electron 运行时按需安装」的支路（pet_get_runtime_status /
pet_install_runtime，运行时细节在 core/pet_runtime.py）：Electron 解压后约
368MB，不随安装包分发，改为用户首次启用桌宠时后台下载。这里只负责把
detect() + status() 合并成前端好用的结构、并给高频轮询加一层探测缓存。

分工：core/pet_manager 只管 Electron 进程生命周期（拉起、探活、优雅退出、
单实例与 pid 记录），这里只做「配置持久化 + 参数校验 + 异常兜底」，不重复
实现进程细节。

顶层只 import 轻量模块；pet_manager / pet_runtime 与 config 都在方法内惰性导入
——设置页
点开才碰进程管理，桌面宠物未使用时不给启动路径添任何负担。所有异常一律转成
{'ok': False, 'msg': ...}，绝不穿透 pywebview 的 js_api 线程（那会让界面失去
响应）。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# 配置键名：与 config.py 已登记的键名保持一致
# （pet_enabled / pet_electron_path / pet_notify_enabled）
_KEY_ENABLED = "pet_enabled"
_KEY_ELECTRON_PATH = "pet_electron_path"
_KEY_NOTIFY = "pet_notify_enabled"

# ---------- 任务气泡提示（emit_event → 桌宠冒泡）----------
# 气泡停留时长（毫秒）。桌宠侧会再钳一次范围，这里给常用值即可。
_NOTIFY_BUBBLE_MS = 2600

# 同一文案的最小发送间隔（秒）：工具连读十个文件不该刷十次"翻文件…"。
_NOTIFY_THROTTLE_SECONDS = 1.5

# 收尾信号不受节流限制——用户最关心的就是"结束了没有"，重复也要让气泡更新。
_NOTIFY_ALWAYS_EVENTS = frozenset({"turn_finish", "turn_error", "turn_cancelled"})

# 白名单：先把高频事件的绝大多数（token_delta / round_started / pty_data …）挡在
# 外面，再去做读配置、算文案这些稍重的动作。
_NOTIFY_EVENTS = frozenset({"turn_start", "tool_start"}) | _NOTIFY_ALWAYS_EVENTS

# 事件 → 固定文案（工具类事件另走 _NOTIFY_TOOL_RULES）
_NOTIFY_EVENT_TEXTS = {
    "turn_start": "开工～",
    "turn_finish": "搞定！",
    "turn_error": "欸，出错了…",
    "turn_cancelled": "停手了",
}

_NOTIFY_TOOL_FALLBACK = "干活中…"

# 工具名 → 拟人化文案；按顺序匹配，命中即返回。
_NOTIFY_TOOL_RULES = (
    (("read_file", "search_code", "glob_files", "find_definition",
      "list_directory", "tool_result_read", "grep"), "翻文件…"),
    (("write_file", "edit_file"), "改代码…"),
    (("execute_command", "pty_manage"), "敲命令…"),
    (("fetch_url", "search_news", "fetch_image"), "上网查…"),
    (("run_subagent", "task"), "派个小助手…"),
)

# 文案 → 上次发送时刻（time.monotonic）。模块级：星瑶单进程，多会话共用一个节流表。
_notify_last_sent: Dict[str, float] = {}


def _notify_tool_text(name: Any) -> str:
    """把工具名映射成一句拟人化文案；未知/缺失工具名统一"干活中…"。"""
    try:
        tool = str(name or "").strip()
    except Exception:
        tool = ""
    if not tool:
        return _NOTIFY_TOOL_FALLBACK
    if tool.startswith("firecrawl"):
        return "上网查…"
    for names, text in _NOTIFY_TOOL_RULES:
        if tool in names:
            return text
    return _NOTIFY_TOOL_FALLBACK


def _notify_bubble_text(event_type: str, data: Any) -> Optional[str]:
    """事件 → 气泡文案；不该冒泡的事件返回 None。"""
    if event_type == "tool_start":
        name = ""
        if isinstance(data, dict):
            name = data.get("name")
        return _notify_tool_text(name)
    return _NOTIFY_EVENT_TEXTS.get(event_type)


# get_status() 异常时的最小骨架：形状与 pet_manager.get_status() 对齐，
# 前端不必为"后端炸了"另写一套字段判断。
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

# 合并后的结构：detect 的四个字段 + status() 的进度字段，前端一个对象画完整个卡片。
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

# detect() 会递归统计 dist 目录大小（~368MB / 几十万文件），而 pet_get_status 被前端
# 高频轮询，每次轮询都扫一遍盘会明显拖慢界面。缓存 = 模块级单变量元组
# (检测时刻 time.monotonic(), 当时的运行时 state, detect 结果)：
#   - state 未变（轮询常态）→ 复用，不碰磁盘；
#   - state 变了（idle→downloading→verifying→done/failed）→ 立即重测，
#     装完/装失败后前端下一次轮询就能看到新结果，不会被旧缓存骗住；
#   - 已装好且刚测过 → 也复用（dist 大小不会自己变，没必要重扫）。
# 线程安全：整体只做「整个元组」的读取与赋值，靠 GIL 的原子性，绝不原地改；
# 两个线程同时刷新最多多测一次，不会读到半截数据。
_RUNTIME_DETECT_TTL = 600.0
_runtime_detect_cache = (0.0, None, None)


def _pet_manager():
    """惰性导入 core.pet_manager。

    导入失败（打包缺文件等）时抛给调用方，由各桥方法统一转成 ok=False；
    不在模块级导入是为了让未使用桌宠的启动路径完全不碰进程管理依赖。
    """
    from core import pet_manager

    return pet_manager


def _pet_runtime_module():
    """惰性导入 core.pet_runtime（Electron 运行时的下载/安装）。

    与 _pet_manager 同样的降级策略：导入失败抛给调用方，由桥方法统一转
    ok=False；模块级导入会让没用桌宠的启动路径白拉 urllib/zipfile 等依赖。
    兼容以 core 目录为工作目录的调用（此时只有顶层 pet_runtime）。
    """
    try:
        from core import pet_runtime

        return pet_runtime
    except Exception:
        pass
    import pet_runtime

    return pet_runtime


def _runtime_detect_cached(module, state: str) -> Dict[str, Any]:
    """带缓存的 module.detect()：理由见 _runtime_detect_cache 上方的说明。

    任何异常都不外抛——探测失败退化成「上一次结果」或空骨架，
    不能让一次磁盘错误把整个 pet_get_status 打成 ok=False。
    """
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
    # 复用条件：状态没变（轮询常态），或「已装 + 刚测过」（dist 大小不会自己变）。
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
    """读单个配置项：先看模块级属性，再回落 config.json 的载荷。

    config 模块的 __getattr__ 对未登记名字会抛 AttributeError（pet_enabled 就是
    这种"只登记键名、不设模块属性"的键），所以两级都要吞异常：读不到配置只影响
    "开关显示"，不该让整个状态查询失败。
    """
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
    """把配置项读成布尔值：未设置/读失败/异常值一律回落 default。"""
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
    """写配置项；失败返回 False（save_config_value 内部已留告警日志）。"""
    try:
        from config import save_config_value

        return bool(save_config_value(key, value))
    except Exception:
        logger.warning("保存配置项 %s 失败", key, exc_info=True)
        return False


class PetBridgeMixin:
    """为 ApiBridge 提供 pet_* 系列桥方法（桌宠开关 / Electron 路径 / 重启）。"""

    # ---------- 公共小工具 ----------
    def pet_get_status(self) -> Dict[str, Any]:
        """查询桌宠状态，并附带配置里的开关值 enabled。

        status 里 running/available/pid/electron_path/pet_dir 全部来自
        pet_manager.get_status()（只读，最多跑一次 tasklist 探活）；enabled 是
        config.toml/config.json 里 pet_enabled 的当前值——"开关打开但进程没起来"
        和"开关关闭"在前端是两种提示，必须能分辨。notify_enabled 是任务气泡提示
        开关（pet_notify_enabled，缺省视为开启）。
        """
        enabled = _config_bool(_KEY_ENABLED, False)
        notify_enabled = _config_bool(_KEY_NOTIFY, True)
        # 运行时快照先算好（内部已兜住所有异常），失败路径也要带上这个字段，
        # 前端不必为"后端炸了"另写一套字段判断。
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

    # ---------- 内部：Electron 运行时快照 ----------
    def _pet_runtime_snapshot(self) -> Dict[str, Any]:
        """把 pet_runtime.detect() 与 status() 合并成一个扁平结构（异常外抛）。

        detect 给「装没装、装在哪、多大」，status 给「下载进度、当前状态机状态」，
        前端只想要一个对象画整个卡片。detect 走缓存（见 _runtime_detect_cached）。
        """
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
        """点「下载运行时」时调用：起后台下载线程并立刻返回当前进度。

        不阻塞：core/pet_runtime.install() 本身只置状态 + 起 daemon 线程就返回，
        151MB 的下载在后台跑，前端轮询 pet_get_runtime_status 画进度条。
        幂等：已装好直接 done、正在装返回当前状态，所以下载失败后用户再点一次
        就是重试，这里不做任何"只能点一次"的限制。
        """
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
        """打开/关闭「执行任务时桌宠冒泡提示」，并持久化开关。

        与 pet_set_enabled 的区别：这个开关只管气泡（emit_event 的轻量转发），
        不碰 Electron 进程；写盘失败时 ok=False 并在 msg 里说明配置没写进去，
        避免用户重启后发现开关自己回弹。
        """
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

    # ---------- 内部：事件 → 气泡提示 ----------
    def _pet_notify_event(self, event_type: str, data: Any = None) -> None:
        """把一次前端事件转成桌宠气泡（"开工～/翻文件…/搞定！"）。

        由 ApiBridge.emit_event 在事件派发后顺带调用，属于高频路径，所以：
        1) 先按白名单挡掉绝大多数事件（token_delta / pty_data / round_started…），
           再做读配置、算文案这些稍重的动作；
        2) 同一文案 1.5 秒内只发一次（工具连读十个文件不该刷十次"翻文件…"），
           但 turn_finish / turn_error / turn_cancelled 是用户最关心的收尾信号，
           不受节流限制；
        3) 整个方法体包 try/except，任何异常只 logger.debug——桌宠出任何问题都
           不允许影响事件派发链路。
        """
        try:
            if event_type not in _NOTIFY_EVENTS:
                return
            if not _config_bool(_KEY_NOTIFY, True):
                return
            text = _notify_bubble_text(event_type, data)
            if not text:
                return
            now = time.monotonic()
            if event_type not in _NOTIFY_ALWAYS_EVENTS:
                last = _notify_last_sent.get(text)
                if last is not None and (now - last) < _NOTIFY_THROTTLE_SECONDS:
                    return
            _notify_last_sent[text] = now
            result = _pet_manager().push_bubble(text, _NOTIFY_BUBBLE_MS)
            # 桌宠未运行是常态（用户没开桌宠），不值得刷日志；统一降到 debug。
            if isinstance(result, dict) and not result.get("ok"):
                logger.debug("桌宠气泡未推送（%s）：%s", text, result.get("msg"))
        except Exception:
            logger.debug("桌宠气泡提示失败 event=%s", event_type, exc_info=True)

    def _pet_failed(self, msg: str) -> Dict[str, Any]:
        """统一失败返回：带上最新状态，前端不必二次拉取。"""
        return {"ok": False, "msg": msg, "status": self.pet_get_status()}

    # ---------- 前端 API ----------
    def pet_set_enabled(self, enabled: bool) -> Dict[str, Any]:
        """打开/关闭桌面宠物，并按"实际结果"持久化开关。

        enabled 为真拉起桌宠、为假关闭它。落盘顺序是**先动作、再按实际结果写配置**：
        早期版本先写盘再启动，于是"没有运行时 → 启动失败 → 配置却已是 true"，用户
        看到开关亮着、桌面却没有宠物，重启后依旧，是最难自查的坑。现在启动失败会把
        配置回归 false，与桌面实际状态保持一致。
        """
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
        """保存 Electron 可执行文件路径（空字符串表示"用桌宠内置的那份"）。

        路径只在启动时被 pet_manager._resolve_electron 读取，所以正在运行时必须
        stop→start 才能让新路径生效；重启失败如实回报 ok=False（路径已存但没跑起来
        是真实状态，不能因为"配置保存成功"就报成功）。
        """
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

    # ---------- 内部：启动时自动恢复 ----------
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
        try:
            if isinstance(result, dict) and not result.get("skipped"):
                level = logging.INFO if result.get("ok") else logging.WARNING
                logger.log(level, "桌宠自动恢复：%s", result.get("msg") or result)
            elif isinstance(result, dict):
                logger.debug("桌宠自动恢复跳过：%s", result.get("msg"))
        except Exception:
            logger.debug("记录桌宠自动恢复结果失败", exc_info=True)
