"""管理运维类 Agent 工具：用量统计 / 会话导出 / 自动更新 / 系统设置 / 图表 / 自检。

设计约束：
- 只复用 core 层公开模块，禁止 import gui_bridge / main / pywebview；
- 所有工具统一返回 JSON 字符串（ensure_ascii=False）：
  成功 ``{"ok": true, ...}``，失败 ``{"ok": false, "error": "中文原因"}``；
- 所有异常在工具入口处兜底，任何情况下都不向调度层抛出；
- 不触碰安全项：permission_mode / allow_outside_workspace（沙箱）/ whitelist
  在本模块中显式拒绝，只暴露非安全的设置项。

工具清单（build_specs() 返回 6 个 ToolSpec）：
usage_stats / session_export / update_manage / system_settings /
diagram_manage / doctor_run
"""

from __future__ import annotations

import datetime
import functools
import json
import os
import threading
from pathlib import Path
from typing import Any

from core.tool_protocol import ToolSpec


# ---------------------------------------------------------------------------
# 通用返回与容错
# ---------------------------------------------------------------------------

# 统计范围（与 CLI /usage、GUI get_token_usage_statistics 口径一致）
USAGE_ALLOWED_DAYS = (1, 7, 30, 365)

# 严禁通过工具修改的安全项（命中即明确拒绝，不静默忽略）
FORBIDDEN_SETTING_KEYS = {
    "permission_mode": "权限模式",
    "permission": "权限模式",
    "allow_outside_workspace": "工作区外访问（沙箱）",
    "sandbox": "工作区外访问（沙箱）",
    "whitelist": "命令白名单",
    "command_whitelist": "命令白名单",
}

# 支持的设置项 -> 中文标签
SUPPORTED_SETTING_KEYS = {
    "command_shell": "默认命令解释器",
    "turn_step_limit": "单任务最大轮次",
    "search_exclude_dirs": "搜索排除目录",
    "subagent_enabled": "子代理开关",
    "ai_output_dir": "AI 输出目录",
}


def _dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _ok(**payload: Any) -> str:
    data: dict[str, Any] = {"ok": True}
    data.update(payload)
    return _dumps(data)


def _fail(error: Any) -> str:
    return _dumps({"ok": False, "error": str(error)})


def _guard(func):
    """工具入口兜底：任何异常都转成统一失败 JSON，不让异常冒到调度层。"""

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> str:
        try:
            return func(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - 工具边界必须吞掉全部异常
            return _fail(f"{type(exc).__name__}: {exc}")

    return wrapper


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "y", "on", "是"}:
        return True
    if text in {"0", "false", "no", "n", "off", "否", ""}:
        return False
    return default


def _as_int(value: Any, default: int = 0) -> int:
    try:
        if isinstance(value, bool):
            return int(value)
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# MemoryManager 获取（统计 / 导出共用）
# ---------------------------------------------------------------------------

_MANAGER_LOCK = threading.Lock()
_FALLBACK_MANAGER: Any = None


def _resolve_manager():
    """优先用运行时注册的 MemoryManager；headless 场景就地创建一个只读用途实例。

    运行时上下文（core.app_context）由 CLI / GUI 启动时注册，这里不依赖
    任何入口模块，拿不到时按当前 UI 形态回落到对应的会话库。
    """
    global _FALLBACK_MANAGER
    try:
        from core import app_context

        manager = app_context.get_manager()
        if manager is not None:
            return manager
        flavor = app_context.get_ui_flavor()
    except Exception:
        flavor = "headless"

    with _MANAGER_LOCK:
        if _FALLBACK_MANAGER is None:
            import config as _config

            from core.memory import MemoryManager

            db_path = _config.GUI_DB_FILE if flavor == "gui" else _config.DB_FILE
            _FALLBACK_MANAGER = MemoryManager(db_path=db_path)
        return _FALLBACK_MANAGER


# ---------------------------------------------------------------------------
# 1) usage_stats
# ---------------------------------------------------------------------------

_USAGE_METRICS = ("prompt", "completion", "cached", "cache_prompt", "total", "requests")


def _empty_metrics() -> dict[str, int]:
    # 成本账本（S1）：cost_micros 为整数微元，unpriced 是未计价条数。
    # 二者分开累计——未计价 ≠ 0 元，混在一起会让「省钱」与「没配价」看起来一样。
    metrics = {name: 0 for name in _USAGE_METRICS}
    metrics["cost_micros"] = 0
    metrics["unpriced"] = 0
    return metrics


def _add_metrics(target: dict[str, Any], source: dict[str, Any]) -> None:
    for name in _USAGE_METRICS:
        target[name] += _as_int(source.get(name, 0), 0)

    unpriced_delta = _as_int(source.get("unpriced", 0), 0)
    micros = source.get("cost_micros")
    if micros is None:
        # 整行未计价：老数据没有 unpriced 字段时按该行请求数计
        target["unpriced"] = target.get("unpriced", 0) + (
            unpriced_delta or _as_int(source.get("requests", 0), 0)
        )
        return
    target["cost_micros"] = target.get("cost_micros", 0) + _as_int(micros, 0)
    target["unpriced"] = target.get("unpriced", 0) + unpriced_delta
    currency = str(source.get("cost_currency") or "")
    if currency:
        target.setdefault("cost_currency", currency)


def _with_cache_rate(metrics: dict[str, Any]) -> dict[str, Any]:
    """补缓存命中率与成本展示。

    命中率分母只统计已报告缓存明细的 prompt（与 GUI 口径一致）。
    跨币种不求和：聚合层已按币种分开，这里只格式化。
    """
    result = dict(metrics)
    cache_prompt = _as_int(result.get("cache_prompt", 0), 0)
    result["cache_hit_rate"] = (
        round(_as_int(result.get("cached", 0), 0) / cache_prompt * 100, 1)
        if cache_prompt > 0
        else None
    )
    result["cost_text"] = _format_cost(result)
    return result


def _format_cost(metrics: dict[str, Any]) -> str | None:
    """微元 → 人类可读金额；无币种时返回 None（未计价，不显示 ¥0）。"""
    currency = str(metrics.get("cost_currency") or "")
    if not currency:
        return None
    from core.cost_model import CURRENCY_SYMBOLS

    micros = _as_int(metrics.get("cost_micros", 0), 0)
    symbol = CURRENCY_SYMBOLS.get(currency, currency + " ")
    return f"{symbol}{micros / 1_000_000:.4f}"


@_guard
def usage_stats(days: int = 30) -> str:
    """按天与按模型汇总 token 用量（含缓存命中率）。"""
    range_days = _as_int(days, 0)
    if range_days not in USAGE_ALLOWED_DAYS:
        return _fail(
            "统计范围仅支持 1、7、30、365 天（收到 "
            f"{days!r}）"
        )

    manager = _resolve_manager()
    query = getattr(manager, "query_token_usage_by_day_and_model", None)
    if not callable(query):
        return _fail("当前会话管理器不支持 Token 用量统计（缺少 query_token_usage_by_day_and_model）")

    end_date = datetime.date.today()
    start_date = end_date - datetime.timedelta(days=range_days - 1)
    rows = query(start_date.isoformat(), end_date.isoformat()) or []

    summary = _empty_metrics()
    model_totals: dict[str, dict[str, Any]] = {}
    day_lookup: dict[str, dict[str, Any]] = {}
    for offset in range(range_days):
        day = (start_date + datetime.timedelta(days=offset)).isoformat()
        day_lookup[day] = {"date": day, **_empty_metrics(), "models": []}

    for row in rows:
        if not isinstance(row, dict):
            continue
        provider = str(row.get("provider") or "")
        model = str(row.get("model") or "")
        key = f"{provider}/{model}" if provider else model
        model_item = model_totals.setdefault(
            key,
            {"key": key, "provider": provider, "model": model, **_empty_metrics()},
        )
        _add_metrics(model_item, row)
        _add_metrics(summary, row)

        day_item = day_lookup.get(str(row.get("date") or ""))
        if day_item is None:
            # 超出请求区间的脏数据不进日明细（SQL 已过滤，这里只是防御）
            continue
        _add_metrics(day_item, row)
        item = {
            "key": key,
            "provider": provider,
            "model": model,
            **_empty_metrics(),
        }
        _add_metrics(item, row)
        day_item["models"].append(_with_cache_rate(item))

    models = [_with_cache_rate(item) for item in model_totals.values()]
    models.sort(key=lambda item: (-item["total"], item["key"]))

    day_items: list[dict[str, Any]] = []
    for day_item in day_lookup.values():
        day_item["models"].sort(key=lambda item: (-item["total"], item["key"]))
        day_items.append(_with_cache_rate(day_item))

    return _ok(
        action="usage_stats",
        range={
            "days": range_days,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
        },
        summary=_with_cache_rate(summary),
        models=models,
        days=day_items,
    )


# ---------------------------------------------------------------------------
# 2) session_export
# ---------------------------------------------------------------------------

_EXPORT_FORMATS = {
    "md": "md",
    "markdown": "md",
    "html": "html",
    "htm": "html",
    "json": "json",
}


@_guard
def session_export(session: str = "", fmt: str = "md", output_path: str = "") -> str:
    """导出会话到本地文件（md / html / json），返回真实文件路径。"""
    from core import share

    fmt_key = str(fmt or "md").strip().lower()
    kind = _EXPORT_FORMATS.get(fmt_key)
    if kind is None:
        return _fail("导出格式仅支持 md / html / json")

    session_name = str(session or "").strip()
    if not session_name:
        session_name = _current_session_name()
    if not session_name:
        return _fail("无法确定要导出的会话：请用 session 参数指定会话名，或先进入某个会话")

    manager = _resolve_manager()

    target = str(output_path or "").strip()
    if target:
        target = os.path.expandvars(os.path.expanduser(target))
    else:
        export_dir = Path(share.default_export_dir())
        export_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in session_name)
        target = str(export_dir / f"{safe_name}_{stamp}.{kind}")

    exporter = {
        "md": share.export_session_markdown,
        "html": share.export_session_html,
        "json": share.export_session_json,
    }[kind]
    message = str(exporter(session_name, manager=manager, output_path=target) or "")

    absolute = str(Path(target).resolve())
    if message.lstrip().startswith("🚨") or not os.path.isfile(target):
        reason = message.replace("🚨", "").strip() or "导出失败（未生成文件）"
        return _fail(reason)

    try:
        size_bytes = os.path.getsize(target)
    except OSError:
        size_bytes = 0
    return _ok(
        action="session_export",
        session=session_name,
        format=kind,
        path=absolute,
        size_bytes=size_bytes,
        message=message,
    )


def _current_session_name() -> str:
    """当前会话名：优先本线程回合绑定的会话，其次 UI 当前查看的会话。"""
    try:
        from core import app_context

        return str(app_context.get_thread_session() or app_context.get_current_session() or "").strip()
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# 3) update_manage
# ---------------------------------------------------------------------------

_DOWNLOAD_LOCK = threading.Lock()
_DOWNLOAD_STATE: dict[str, Any] = {
    "status": "idle",  # idle / downloading / done / error
    "version": "",
    "percent": 0,
    "downloaded": 0,
    "total": 0,
    "path": "",
    "error": "",
}


def _update_download_state(**fields: Any) -> None:
    with _DOWNLOAD_LOCK:
        _DOWNLOAD_STATE.update(fields)


def _download_state_snapshot() -> dict[str, Any]:
    with _DOWNLOAD_LOCK:
        return dict(_DOWNLOAD_STATE)


@_guard
def update_manage(action: str, installer_path: str = "") -> str:
    """自动更新：检查 / 下载 / 取消 / 安装 / 清理本地安装包缓存。"""
    act = str(action or "").strip().lower()
    if act not in {"check", "download", "cancel", "install", "cleanup"}:
        return _fail("action 仅支持 check / download / cancel / install / cleanup")

    if act == "check":
        return _update_check()
    if act == "download":
        return _update_download()
    if act == "cancel":
        return _update_cancel()
    if act == "install":
        return _update_install(installer_path)
    return _update_cleanup()


def _update_check() -> str:
    from core.updater import updater_downloader

    info = _fetch_release()
    if not info.get("ok"):
        return _fail(info.get("msg") or "检查更新失败")

    state = _download_state_snapshot()
    return _ok(
        action="check",
        has_update=bool(info.get("has_update")),
        current_version=str(info.get("current_version") or ""),
        latest_version=str(info.get("latest_version") or ""),
        release_title=str(info.get("release_title") or ""),
        asset_name=str(info.get("asset_name") or ""),
        asset_size=_as_int(info.get("asset_size"), 0),
        is_split=bool(info.get("is_split")),
        part_count=_as_int(info.get("part_count"), 0),
        sha256=str(info.get("sha256") or ""),
        download_url=str(info.get("download_url") or ""),
        message=str(info.get("msg") or ""),
        download={
            "is_downloading": bool(getattr(updater_downloader, "is_downloading", False)),
            **state,
        },
    )


def _fetch_release() -> dict[str, Any]:
    from core.updater import fetch_latest_release

    info = fetch_latest_release()
    return info if isinstance(info, dict) else {"ok": False, "msg": "检查更新失败：返回格式异常"}


def _update_download() -> str:
    from core.updater import updater_downloader

    if getattr(updater_downloader, "is_downloading", False):
        return _fail("已有安装包正在下载中，请等待完成或先执行 cancel 取消")

    info = _fetch_release()
    if not info.get("ok"):
        return _fail(info.get("msg") or "检查更新失败，无法开始下载")
    if not info.get("has_update"):
        return _ok(
            action="download",
            started=False,
            message=str(info.get("msg") or "当前已是最新版本，无需下载"),
            current_version=str(info.get("current_version") or ""),
        )

    version = str(info.get("latest_version") or "").strip()
    if not version:
        return _fail("最新版本号缺失，无法下载安装包")

    def on_progress(payload: dict[str, Any]) -> None:
        _update_download_state(
            status="downloading",
            percent=_as_int((payload or {}).get("percent"), 0),
            downloaded=_as_int((payload or {}).get("downloaded"), 0),
            total=_as_int((payload or {}).get("total"), 0),
        )

    def on_complete(path: str) -> None:
        _update_download_state(status="done", percent=100, path=str(path or ""), error="")

    def on_error(message: str) -> None:
        _update_download_state(status="error", error=str(message or "下载失败"))

    _update_download_state(
        status="downloading",
        version=version,
        percent=0,
        downloaded=0,
        total=_as_int(info.get("asset_size"), 0),
        path="",
        error="",
    )

    started = updater_downloader.start_download(
        str(info.get("download_url") or ""),
        version,
        on_progress,
        on_complete,
        on_error,
        part_urls=list(info.get("part_urls") or []),
        expected_sha256=str(info.get("sha256") or ""),
        total_size_hint=_as_int(info.get("asset_size"), 0),
    )
    if not started:
        _update_download_state(status="idle")
        return _fail("下载未能启动（可能已有下载任务在进行，或下载地址无效）")

    return _ok(
        action="download",
        started=True,
        background=True,
        version=version,
        part_count=_as_int(info.get("part_count"), 0),
        is_split=bool(info.get("is_split")),
        message=f"已开始后台下载 v{version}，可用 action=check 查看进度，cancel 取消",
        download=_download_state_snapshot(),
    )


def _update_cancel() -> str:
    from core.updater import updater_downloader

    updater_downloader.cancel()
    _update_download_state(status="idle", percent=0)
    return _ok(
        action="cancel",
        cancelled=True,
        is_downloading=bool(getattr(updater_downloader, "is_downloading", False)),
        download=_download_state_snapshot(),
    )


def _update_install(installer_path: str) -> str:
    from core.updater import find_cached_installer, launch_installer, updater_downloader

    if getattr(updater_downloader, "is_downloading", False):
        return _fail("安装包仍在下载中，请等待下载完成后再安装")

    path = str(installer_path or "").strip()
    if path:
        path = os.path.expandvars(os.path.expanduser(path))
    else:
        info = _fetch_release()
        if not info.get("ok"):
            return _fail(info.get("msg") or "检查更新失败，无法定位本地安装包")
        path = find_cached_installer(
            str(info.get("latest_version") or ""),
            str(info.get("sha256") or ""),
            _as_int(info.get("asset_size"), 0),
        )
        if not path:
            return _fail("本地没有可用的安装包，请先执行 action=download 下载完成后再安装")

    if not os.path.isfile(path):
        return _fail(f"安装包不存在：{path}")
    if not launch_installer(path):
        return _fail("启动安装向导失败（安装包无效或权限不足）")

    return _ok(
        action="install",
        path=str(Path(path).resolve()),
        message="安装向导已启动，星瑶Code 即将退出完成更新",
    )


def _update_cleanup() -> str:
    from core.updater import cleanup_old_installers, get_update_cache_dir

    cleaned = _as_int(cleanup_old_installers(), 0)
    cache_dir = ""
    try:
        cache_dir = str(get_update_cache_dir())
    except Exception:
        cache_dir = ""
    return _ok(
        action="cleanup",
        cleaned=cleaned,
        cache_dir=cache_dir,
        message=f"已清理 {cleaned} 个更新缓存条目",
    )


# ---------------------------------------------------------------------------
# 4) system_settings
# ---------------------------------------------------------------------------

def _settings_snapshot() -> dict[str, Any]:
    """当前非安全设置项快照。"""
    return {key: _get_one_setting(key) for key in SUPPORTED_SETTING_KEYS}


def _get_one_setting(key: str) -> dict[str, Any]:
    if key == "command_shell":
        from core.command_shell import (
            describe_selected_command_shell,
            get_selected_command_shell,
            list_command_shell_options,
        )

        options = list_command_shell_options()
        return {
            "label": SUPPORTED_SETTING_KEYS[key],
            "value": get_selected_command_shell(),
            "description": describe_selected_command_shell(),
            "options": [
                {
                    "id": str(item.get("id") or ""),
                    "label": str(item.get("label") or ""),
                    "available": bool(item.get("available")),
                }
                for item in options
            ],
        }

    if key == "turn_step_limit":
        from core.harness.turn_limits import (
            DEFAULT_TURN_STEP_LIMIT,
            MAX_TURN_STEP_LIMIT,
            MIN_TURN_STEP_LIMIT,
            load_turn_step_limit,
        )

        return {
            "label": SUPPORTED_SETTING_KEYS[key],
            "value": load_turn_step_limit(),
            "min": MIN_TURN_STEP_LIMIT,
            "max": MAX_TURN_STEP_LIMIT,
            "default": DEFAULT_TURN_STEP_LIMIT,
        }

    if key == "search_exclude_dirs":
        import config as _config

        configured = _config._load().get("search_exclude_dirs")
        extra = (
            [str(item).strip() for item in configured if str(item).strip()]
            if isinstance(configured, (list, tuple, set))
            else []
        )
        return {
            "label": SUPPORTED_SETTING_KEYS[key],
            "value": extra,
            "note": "该列表在 search_code 的内置排除目录（.git/.venv/node_modules 等）之外追加",
        }

    if key == "subagent_enabled":
        from core.subagent_settings import subagent_enabled as _subagent_enabled

        return {
            "label": SUPPORTED_SETTING_KEYS[key],
            "value": _subagent_enabled(),
            "description": "关闭后不派发子代理，计划编排工具一并不可用；所有子任务由主 Agent 直接完成",
        }

    # ai_output_dir
    from core.ai_output_paths import (
        default_ai_output_dir,
        get_ai_output_dir_setting,
        resolve_ai_output_dir,
    )

    return {
        "label": SUPPORTED_SETTING_KEYS[key],
        "value": get_ai_output_dir_setting(),
        "resolved": str(resolve_ai_output_dir()),
        "default": str(default_ai_output_dir()),
    }


@_guard
def system_settings(action: str = "get", key: str = "", value: str = "") -> str:
    """读取 / 修改非安全设置项（命令解释器、单任务最大轮次、搜索排除目录、AI 输出目录、子代理开关）。"""
    act = str(action or "get").strip().lower()
    setting_key = str(key or "").strip()

    if act == "get":
        if not setting_key:
            return _ok(action="get", settings=_settings_snapshot(), supported=list(SUPPORTED_SETTING_KEYS))
        if setting_key in FORBIDDEN_SETTING_KEYS:
            return _fail(
                f"{FORBIDDEN_SETTING_KEYS[setting_key]}属于安全策略项，不提供工具修改或读取，请使用设置界面"
            )
        if setting_key not in SUPPORTED_SETTING_KEYS:
            return _fail(
                f"不支持的设置项：{setting_key}（支持：{'、'.join(SUPPORTED_SETTING_KEYS)}）"
            )
        return _ok(action="get", key=setting_key, setting=_get_one_setting(setting_key))

    if act != "set":
        return _fail("action 仅支持 get / set")

    if not setting_key:
        return _fail("set 需要指定 key（支持：" + "、".join(SUPPORTED_SETTING_KEYS) + "）")
    if setting_key in FORBIDDEN_SETTING_KEYS:
        return _fail(
            f"{FORBIDDEN_SETTING_KEYS[setting_key]}属于安全策略项，严禁通过工具修改；请在设置界面由用户本人操作"
        )
    if setting_key not in SUPPORTED_SETTING_KEYS:
        return _fail(
            f"不支持的设置项：{setting_key}（支持：{'、'.join(SUPPORTED_SETTING_KEYS)}）"
        )

    text = str(value if value is not None else "").strip()

    if setting_key == "command_shell":
        from core.command_shell import set_selected_command_shell

        if not text:
            return _fail("command_shell 需要提供解释器 id（如 auto / cmd / powershell / bash）")
        try:
            set_selected_command_shell(text)
        except Exception as exc:  # CommandShellError / 未安装 / 保存失败
            return _fail(str(exc) or "设置默认命令解释器失败")
        return _ok(action="set", key=setting_key, setting=_get_one_setting(setting_key))

    if setting_key == "turn_step_limit":
        number = _as_int(text, 0)
        if number <= 0:
            return _fail(f"单任务最大轮次必须是正整数，收到 {value!r}")
        from core.harness.turn_limits import save_turn_step_limit

        try:
            save_turn_step_limit(number)
        except Exception as exc:  # TurnStepLimitError（越界 / 保存失败）
            return _fail(str(exc) or "保存单任务最大轮次失败")
        return _ok(action="set", key=setting_key, setting=_get_one_setting(setting_key))

    if setting_key == "search_exclude_dirs":
        entries: list[str] = []
        for chunk in str(value or "").replace("；", ";").replace("，", ",").replace("\n", ",").split(","):
            item = chunk.strip()
            if item and item not in entries:
                entries.append(item)
        import config as _config

        if not _config.save_config_value("search_exclude_dirs", entries):
            return _fail("搜索排除目录保存失败（config.json 写入失败）")
        return _ok(action="set", key=setting_key, setting=_get_one_setting(setting_key))

    if setting_key == "subagent_enabled":
        from core.subagent_settings import SubagentSettingError, set_subagent_enabled

        lowered = text.strip().lower()
        if lowered in {"1", "true", "yes", "y", "on", "是", "开", "开启"}:
            enabled = True
        elif lowered in {"0", "false", "no", "n", "off", "否", "关", "关闭"}:
            enabled = False
        else:
            return _fail(f"子代理开关只接受布尔值，收到 {value!r}")
        try:
            set_subagent_enabled(enabled)
        except SubagentSettingError as exc:
            return _fail(str(exc) or "子代理开关保存失败")
        return _ok(action="set", key=setting_key, setting=_get_one_setting(setting_key))

    # ai_output_dir
    from core.ai_output_paths import set_ai_output_dir_setting

    try:
        resolved = set_ai_output_dir_setting(text)
    except Exception as exc:  # AiOutputDirError
        return _fail(str(exc) or "设置 AI 输出目录失败")
    return _ok(
        action="set",
        key=setting_key,
        resolved=str(resolved),
        setting=_get_one_setting(setting_key),
    )


# ---------------------------------------------------------------------------
# 5) diagram_manage
# ---------------------------------------------------------------------------

@_guard
def diagram_manage(action: str = "export", text: str = "", index: int = 0) -> str:
    """Mermaid 图表：导出为本地 HTML / 列出最近图表 / 用浏览器打开。"""
    from core.diagram_export import export_diagram_html, get_recent_diagrams, open_diagram

    act = str(action or "export").strip().lower()
    if act not in {"export", "list", "open"}:
        return _fail("action 仅支持 export / list / open")

    if act == "export":
        source = str(text or "")
        if not source.strip():
            return _fail("text 不能为空：请提供包含 ```mermaid 代码块的文本")
        exported = export_diagram_html(source)
        if not exported:
            return _fail("未在文本中找到闭合的 ```mermaid 代码块，无法生成图表")
        path, count = exported
        return _ok(
            action="export",
            path=str(Path(path).resolve()),
            count=_as_int(count, 0),
            message=f"已生成 {_as_int(count, 0)} 个 Mermaid 图表",
        )

    files = list(get_recent_diagrams())
    items = [
        {
            "index": position + 1,
            "path": str(item),
            "name": item.name,
            "size_bytes": item.stat().st_size if item.exists() else 0,
        }
        for position, item in enumerate(files)
    ]

    if act == "list":
        return _ok(action="list", count=len(items), diagrams=items)

    if not items:
        return _fail("还没有生成过图表：先用 action=export 导出 Mermaid 图表")

    # index 采用 1 起序号；0（或负数）表示最近一张
    position = _as_int(index, 0)
    if position <= 0:
        position = 1
    if position > len(items):
        return _fail(f"序号超出范围：只有 {len(items)} 个图表（1 为最近一张）")
    target = Path(items[position - 1]["path"])
    if not target.is_file():
        return _fail(f"图表文件已不存在：{target}")
    if not open_diagram(target):
        return _fail(f"打开图表失败（无法唤起默认浏览器）：{target}")
    return _ok(action="open", path=str(target), index=position)


# ---------------------------------------------------------------------------
# 6) doctor_run
# ---------------------------------------------------------------------------

@_guard
def doctor_run(json_output: bool = False) -> str:
    """运行聚合自检（core.doctor.run_checks），返回结果摘要。"""
    from core.doctor import run_checks

    checks = run_checks()
    items: list[dict[str, Any]] = []
    counts = {"ok": 0, "warn": 0, "fail": 0, "skip": 0}
    for check in checks:
        item = check.to_dict() if hasattr(check, "to_dict") else {
            "name": str(getattr(check, "name", "")),
            "status": str(getattr(check, "status", "")),
            "detail": str(getattr(check, "detail", "")),
            "hint": str(getattr(check, "hint", "")),
        }
        items.append(item)
        status = str(item.get("status") or "")
        if status in counts:
            counts[status] += 1

    healthy = counts["fail"] == 0
    lines = [
        f"[{item.get('status')}] {item.get('name')}: {item.get('detail')}"
        + (f"（建议：{item.get('hint')}）" if item.get("hint") else "")
        for item in items
    ]
    summary_text = (
        f"自检完成：通过 {counts['ok']} 项，警告 {counts['warn']} 项，"
        f"失败 {counts['fail']} 项，跳过 {counts['skip']} 项。\n" + "\n".join(lines)
    )

    payload: dict[str, Any] = {
        "action": "doctor_run",
        "healthy": healthy,
        "status_counts": counts,
        "checks": items,
        "summary": summary_text,
    }
    if not _as_bool(json_output, False):
        payload["text"] = summary_text
    return _ok(**payload)


# ---------------------------------------------------------------------------
# ToolSpec 注册
# ---------------------------------------------------------------------------

def _schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = required
    return schema


def build_specs() -> list[ToolSpec]:
    """返回 6 个管理运维工具契约（供工具注册表装配）。"""
    return [
        ToolSpec(
            name="usage_stats",
            description=(
                "统计 token 用量：可按 1/7/30/365 天汇总每日与各模型的 prompt、"
                "completion、缓存命中率等指标。只读查询，可用于回答用量与成本相关问题。"
            ),
            input_schema=_schema(
                {
                    "days": {
                        "type": "integer",
                        "enum": list(USAGE_ALLOWED_DAYS),
                        "description": "统计范围天数，仅支持 1、7、30、365，默认 30",
                    }
                }
            ),
            execute=usage_stats,
            read_only=True,
            mutates_files=False,
            requires_permission=True,
            cancellable=True,
            parallel_safe=False,
            activity_description="统计 Token 用量",
            side_effect_scope="none",  # 只读查询：scope 只能是 none/network
        ),
        ToolSpec(
            name="session_export",
            description=(
                "把指定会话（默认当前会话）导出为 md / html / json 文件，返回真实文件路径。"
                "用于分享、归档或备份对话记录。"
            ),
            input_schema=_schema(
                {
                    "session": {"type": "string", "description": "会话名；留空表示当前会话"},
                    "fmt": {
                        "type": "string",
                        "enum": ["md", "html", "json"],
                        "description": "导出格式，默认 md",
                    },
                    "output_path": {
                        "type": "string",
                        "description": "输出文件路径；留空写入默认导出目录",
                    },
                }
            ),
            execute=session_export,
            read_only=False,
            # 只写会话导出文件（返回值即真实产物路径），不属于受控源码编辑，
            # 因此不声明 mutates_files、不纳入按回合撤销记录器。
            mutates_files=False,
            requires_permission=True,
            cancellable=True,
            parallel_safe=False,
            activity_description="导出会话记录",
            side_effect_scope="system",
        ),
        ToolSpec(
            name="update_manage",
            description=(
                "管理星瑶Code 自身更新：check 检查最新版本、download 后台异步下载安装包、"
                "cancel 取消下载、install 启动安装向导、cleanup 清理更新缓存。"
            ),
            input_schema=_schema(
                {
                    "action": {
                        "type": "string",
                        "enum": ["check", "download", "cancel", "install", "cleanup"],
                        "description": "操作类型",
                    },
                    "installer_path": {
                        "type": "string",
                        "description": "install 时可指定安装包路径；留空自动查找已下载的缓存安装包",
                    },
                },
                required=["action"],
            ),
            execute=update_manage,
            read_only=False,
            # 只写更新安装包/缓存目录（installer_path 语义），不属于受控源码编辑，
            # 因此不声明 mutates_files、不纳入按回合撤销记录器。
            mutates_files=False,
            requires_permission=True,
            cancellable=False,
            parallel_safe=False,
            activity_description="管理程序更新",
            side_effect_scope="system",
        ),
        ToolSpec(
            name="system_settings",
            description=(
                "读取或修改非安全设置项：默认命令解释器（command_shell）、"
                "单任务最大轮次（turn_step_limit）、搜索排除目录（search_exclude_dirs）、"
                "AI 输出目录（ai_output_dir）。权限模式、沙箱与命令白名单不允许通过本工具修改。"
            ),
            input_schema=_schema(
                {
                    "action": {"type": "string", "enum": ["get", "set"], "description": "默认 get"},
                    "key": {
                        "type": "string",
                        "enum": list(SUPPORTED_SETTING_KEYS),
                        "description": "设置项键；get 时留空返回全部",
                    },
                    "value": {"type": "string", "description": "set 时的新值"},
                }
            ),
            execute=system_settings,
            read_only=False,
            mutates_files=False,
            requires_permission=True,
            cancellable=False,
            parallel_safe=False,
            activity_description="读写系统设置",
            side_effect_scope="system",
        ),
        ToolSpec(
            name="diagram_manage",
            description=(
                "Mermaid 图表管理：export 把文本中的 ```mermaid 代码块导出为本地 HTML，"
                "list 列出最近生成的图表，open 用默认浏览器打开指定图表（1 为最近一张）。"
            ),
            input_schema=_schema(
                {
                    "action": {"type": "string", "enum": ["export", "list", "open"], "description": "默认 export"},
                    "text": {"type": "string", "description": "包含 mermaid 代码块的文本（export 用）"},
                    "index": {
                        "type": "integer",
                        "description": "open 时的图表序号（1 为最近一张，0 等同 1）",
                    },
                }
            ),
            execute=diagram_manage,
            read_only=False,
            # 只写导出的图表产物（路径由 export 返回值给出），不属于受控源码编辑，
            # 因此不声明 mutates_files、不纳入按回合撤销记录器。
            mutates_files=False,
            requires_permission=True,
            cancellable=True,
            parallel_safe=False,
            activity_description="管理 Mermaid 图表",
            side_effect_scope="system",
        ),
        ToolSpec(
            name="doctor_run",
            description=(
                "运行聚合自检（环境、配置、凭据、数据目录、数据库、MCP、模型端点、GUI 运行时），"
                "返回各项状态与建议；json_output=True 时返回 JSON 结构。"
            ),
            input_schema=_schema(
                {
                    "json_output": {
                        "type": "boolean",
                        "description": "是否以 JSON 结构返回（默认 false 返回文字摘要）",
                    }
                }
            ),
            execute=doctor_run,
            read_only=True,
            mutates_files=False,
            requires_permission=True,
            cancellable=True,
            parallel_safe=False,
            activity_description="运行程序自检",
            side_effect_scope="none",  # 只读自检：scope 只能是 none/network
        ),
    ]
