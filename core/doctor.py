from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass

import config
from core import credentials
from core.version import __version__ as YCODE_VERSION

STATUS_OK = "ok"
STATUS_WARN = "warn"
STATUS_FAIL = "fail"
STATUS_SKIP = "skip"

# 模型端点连通探测的默认超时（秒）：体检必须快速返回，不长时间挂起
NETWORK_TIMEOUT_SECONDS = 3.0


@dataclass(frozen=True)
class CheckResult:
    """单项检查结果；字段即 --json 的稳定 schema。"""

    name: str
    status: str
    detail: str = ""
    hint: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "hint": self.hint,
        }


def _load_config() -> dict:
    return config._load()


def _active_base_url() -> str:
    """当前激活模型的 API 地址；兼容旧版单模型配置；无则返回空串。"""
    try:
        from core.model_manager import get_active_model

        model = get_active_model()
        if isinstance(model, dict) and str(model.get("base_url") or "").strip():
            return str(model["base_url"]).strip()
    except Exception:
        pass
    data = _load_config()
    if not data.get("providers"):
        return str(data.get("base_url") or "").strip()
    return ""


def _probe_endpoint(url: str, timeout: float) -> tuple[bool, str]:
    import urllib.error
    import urllib.request

    from core.llm_policy import tokenrhythm_agent_headers

    headers = {"User-Agent": "ycode-doctor"}
    headers.update(tokenrhythm_agent_headers(url))
    request = urllib.request.Request(
        str(url).rstrip("/") + "/", headers=headers
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return True, f"HTTP {getattr(response, 'status', 200)}"
    except urllib.error.HTTPError as exc:
        return True, f"HTTP {exc.code}"
    except Exception as exc:
        return False, f"{exc.__class__.__name__}: {exc}"


def _quick_check(path: str) -> str:
    import sqlite3

    connection = sqlite3.connect(path, timeout=2.0)
    try:
        row = connection.execute("PRAGMA quick_check(1)").fetchone()
    finally:
        connection.close()
    return str(row[0]) if row else ""


def check_environment() -> CheckResult:
    python_text = f"Python {sys.version_info[0]}.{sys.version_info[1]}.{sys.version_info[2]}"
    pack_text = "打包版" if getattr(sys, "frozen", False) else "源码运行"
    detail = f"星瑶Code v{YCODE_VERSION} · {python_text} · {sys.platform} · {pack_text}"
    if sys.version_info < (3, 11):
        return CheckResult(
            "环境与版本",
            STATUS_WARN,
            detail + "；Python 版本低于 3.11",
            "本版本要求 Python 3.11+（低版本下 config.toml 覆盖层不可用），建议升级 Python",
        )
    return CheckResult("环境与版本", STATUS_OK, detail)


def check_config() -> CheckResult:
    data = _load_config()
    if not isinstance(data, dict):
        return CheckResult(
            "配置",
            STATUS_FAIL,
            "配置读取失败：返回了非 dict 内容",
            "检查 ~/.ycode/config.json（含 .bak 备份）是否损坏",
        )

    from core.app_context import is_known_permission_mode

    issues = []
    unknown_keys = config.find_unknown_config_keys(data)
    if unknown_keys:
        preview = "、".join(unknown_keys[:5])
        suffix = f" 等 {len(unknown_keys)} 个" if len(unknown_keys) > 5 else ""
        issues.append(f"存在未知配置键：{preview}{suffix}（不会生效）")
    mode_text = str(data.get("permission_mode") or "").strip()
    if mode_text and not is_known_permission_mode(mode_text):
        issues.append(f"权限模式「{mode_text}」不可识别（实际生效值会回落到 confirm）")

    if not issues:
        return CheckResult("配置", STATUS_OK, "配置读取正常：无未知键，权限模式合法")
    return CheckResult(
        "配置",
        STATUS_WARN,
        "；".join(issues),
        "未知键多为拼写错误，请删除或修正；权限模式可选 confirm / accept_edits / auto_decision / plan / full",
    )


def check_credentials() -> CheckResult:
    data = _load_config()
    entries = []
    providers = data.get("providers")
    if isinstance(providers, list) and providers:
        for provider in providers:
            if not isinstance(provider, dict):
                continue
            label = str(provider.get("name") or provider.get("id") or "未命名 provider")
            entries.append((label, str(provider.get("api_key") or "")))
    else:
        entries.append(("默认模型（旧版配置）", str(data.get("api_key") or "")))

    configured = [(label, raw) for label, raw in entries if raw]
    if not configured:
        hint = "使用 /models 配置模型与 API Key"
        if entries:
            return CheckResult("凭据", STATUS_WARN, "未配置任何 API Key，模型无法调用", hint)
        return CheckResult("凭据", STATUS_SKIP, "未配置任何 API Key", hint)

    failures = []
    for label, raw in configured:
        try:
            secret = credentials.unprotect_secret(raw)
        except Exception as exc:
            failures.append(f"{label}: {exc.__class__.__name__}: {exc}")
            continue
        if not secret:
            failures.append(f"{label}: 解密结果为空")
    if failures:
        preview = "；".join(failures[:3])
        return CheckResult(
            "凭据",
            STATUS_FAIL,
            f"{len(failures)}/{len(configured)} 个 API Key 解密失败：{preview}",
            "密钥密文与当前 Windows 账户绑定；请在 /models 或 GUI 模型设置中重新填写并保存",
        )
    return CheckResult("凭据", STATUS_OK, f"{len(configured)} 个 API Key 解密正常")


def check_data_dir() -> CheckResult:
    root = str(config.YCODE_DIR)
    try:
        os.makedirs(root, exist_ok=True)
        probe = os.path.join(root, f".doctor-write-test-{os.getpid()}.tmp")
        with open(probe, "w", encoding="utf-8") as stream:
            stream.write("ok")
        os.remove(probe)
    except Exception as exc:
        return CheckResult(
            "数据目录",
            STATUS_FAIL,
            f"数据目录不可写：{root}（{exc.__class__.__name__}: {exc}）",
            "检查目录权限与磁盘空间；可用环境变量 YCODE_DATA_DIR 将数据目录迁移到可写位置",
        )
    return CheckResult("数据目录", STATUS_OK, f"{root} 可读写")


def check_databases() -> CheckResult:
    targets = (
        ("ycode.db", str(config.DB_FILE or "")),
        ("ycode_gui.db", str(config.GUI_DB_FILE or "")),
    )
    checked, problems, absent = [], [], []
    for label, path in targets:
        if not path or not os.path.exists(path):
            absent.append(label)
            continue
        try:
            outcome = _quick_check(path)
        except Exception as exc:
            problems.append(f"{label}: {exc.__class__.__name__}: {exc}")
            continue
        if outcome == "ok":
            checked.append(label)
        else:
            problems.append(f"{label}: {outcome}")
    if problems:
        return CheckResult(
            "数据库",
            STATUS_FAIL,
            "；".join(problems),
            "数据库可能损坏；备份后删除对应 .db 文件，程序会重建空库（历史会话将丢失）",
        )
    detail = f"PRAGMA quick_check 通过 {len(checked)} 个库"
    if absent:
        detail += f"；{'、'.join(absent)} 尚未创建（首次运行正常）"
    return CheckResult("数据库", STATUS_OK, detail)


def check_mcp() -> CheckResult:
    path = str(config.MCP_FILE or "")
    if not path or not os.path.exists(path):
        return CheckResult("MCP 服务", STATUS_OK, "未配置 MCP 服务（mcp.json 不存在）")
    try:
        with open(path, "r", encoding="utf-8") as stream:
            data = json.load(stream)
    except Exception as exc:
        return CheckResult(
            "MCP 服务",
            STATUS_FAIL,
            f"mcp.json 读取/解析失败：{exc.__class__.__name__}: {exc}",
            "修复 JSON 语法，或删除 mcp.json 重置为空配置；手工编辑会被 /mcp 连接前复核拦截",
        )
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(servers, dict) or not servers:
        return CheckResult("MCP 服务", STATUS_OK, "未配置 MCP 服务")

    from core.mcp_client import _validate_mcp_launch

    problems = []
    for name, conf in servers.items():
        if not isinstance(conf, dict):
            problems.append(f"{name}: 配置必须是对象")
            continue
        launch_ok, launch_error = _validate_mcp_launch(conf.get("command"), conf.get("env"))
        if not launch_ok:
            problems.append(f"{name}: {launch_error}")
    if problems:
        preview = "；".join(problems[:3])
        return CheckResult(
            "MCP 服务",
            STATUS_FAIL,
            f"{len(problems)}/{len(servers)} 个 MCP 服务不符合启动安全规则：{preview}",
            "启动命令仅允许 npx / node / python 系列；env 不得含危险变量；详见 /mcp",
        )
    return CheckResult("MCP 服务", STATUS_OK, f"{len(servers)} 个 MCP 服务启动参数合法")


def check_model_endpoint(*, skip_network: bool = False) -> CheckResult:
    if skip_network:
        return CheckResult(
            "模型端点",
            STATUS_SKIP,
            "已跳过网络探测（skip_network=True）",
        )
    base_url = _active_base_url()
    if not base_url:
        return CheckResult(
            "模型端点",
            STATUS_SKIP,
            "未配置激活模型或 API 地址",
            "使用 /models 配置模型与 API 地址",
        )
    reachable, detail = _probe_endpoint(base_url, timeout=NETWORK_TIMEOUT_SECONDS)
    if reachable:
        return CheckResult("模型端点", STATUS_OK, f"{base_url} 可达（{detail}）")
    return CheckResult(
        "模型端点",
        STATUS_WARN,
        f"{base_url} 不可达：{detail}",
        "检查网络/代理设置，或确认 API 地址（base_url）是否正确",
    )


def check_gui_runtime() -> CheckResult:
    if sys.platform != "win32":
        return CheckResult("GUI 运行时", STATUS_SKIP, "非 Windows 平台，无需 WebView2")
    from core import gui_runtime_check

    version = gui_runtime_check.detect_webview2_runtime()
    if version:
        return CheckResult("GUI 运行时", STATUS_OK, f"WebView2 Runtime {version} 已就绪")
    return CheckResult(
        "GUI 运行时",
        STATUS_WARN,
        "未检测到 Microsoft Edge WebView2 Runtime（GUI 界面可能黑屏/空白）",
        f"下载安装：{gui_runtime_check.WEBVIEW2_DOWNLOAD_URL}",
    )


def _safe_check(name: str, func, kwargs: dict) -> CheckResult:
    """执行单项检查；任何异常都隔离为 fail，绝不向外抛出。"""
    try:
        result = func(**kwargs)
    except Exception as exc:
        return CheckResult(
            name,
            STATUS_FAIL,
            f"检查执行异常：{exc.__class__.__name__}: {exc}",
            "该项内部错误已被隔离，其余检查不受影响；可重试或反馈该错误",
        )
    if not isinstance(result, CheckResult):
        return CheckResult(
            name,
            STATUS_FAIL,
            f"检查返回了意外类型：{type(result).__name__}",
            "该项内部错误已被隔离，其余检查不受影响",
        )
    return CheckResult(name, result.status, result.detail, result.hint)


def run_checks(*, skip_network: bool = False) -> list[CheckResult]:
    """依次执行全部检查项并返回结果列表（顺序固定，单项异常隔离）。"""
    checks = (
        ("环境与版本", check_environment, {}),
        ("配置", check_config, {}),
        ("凭据", check_credentials, {}),
        ("数据目录", check_data_dir, {}),
        ("数据库", check_databases, {}),
        ("MCP 服务", check_mcp, {}),
        ("模型端点", check_model_endpoint, {"skip_network": skip_network}),
        ("GUI 运行时", check_gui_runtime, {}),
    )
    return [_safe_check(name, func, kwargs) for name, func, kwargs in checks]