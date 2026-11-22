from __future__ import annotations

import hashlib
import importlib
import json
import logging
import os
import re
import shutil
import sys
import threading
import time
import urllib.request
import zipfile
from dataclasses import dataclass

import config

logger = logging.getLogger(__name__)

_CHUNK_SIZE = 256 * 1024

_READ_TIMEOUT = 60.0

_DOWNLOAD_PERCENT_MAX = 90
_EXTRACT_PERCENT = 95

_COMPONENT_ARCHIVE_NAME = "playwright-component.zip"

# 组件元信息文件名（解压后必须存在）
_COMPONENT_MANIFEST = "COMPONENT.json"

# 解压暂存目录前缀（期号避免并发/残留互相踩）
_EXTRACT_DIR_PREFIX = ".playwright-extract-"

_REQUIRED_NODE_DRIVER = os.path.join("playwright", "driver", "node.exe")

_REPO_VERSION_RE = re.compile(
    r"""^__version__\s*=\s*['"]([^'"]+)['"]""", re.MULTILINE
)

_COMPONENT_ASSET_HINT = "playwright-component"

# 主程序要求的 Playwright 组件版本约束（PEP 440 风格，逗号分隔多个条件）。
#
# 权威来源是仓库根的 requirements.txt（``playwright>=1.55,<2``，即主程序开发/测试
# 所使用的 playwright 约束）。打包版运行期读不到 requirements.txt（PyInstaller 只收
# 代码与 datas 白名单，不含仓库文件），因此这里保留一份运行期副本；
# tests/test_components.py::test_required_playwright_spec_matches_requirements_txt
# 会解析 requirements.txt 与本常量逐字比对——任一侧改了另一侧没跟上，测试立刻失败，
# 所以不存在「悄悄漂移的硬编码版本号」。
REQUIRED_PLAYWRIGHT_SPEC = ">=1.55,<2"

_COMPONENT_DIGEST_RE = re.compile(
    r"(?im)^[^\S\n]*(\S+\.zip)[^\S\n]+SHA256[^\S\n]*=[^\S\n]*([0-9a-f]{64})[^\S\n]*$"
)

_USER_AGENT = "ycode-components/1.0 (Windows)"

COMPONENT_ROOT = os.environ.get("YCODE_COMPONENT_DIR") or os.path.join(
    config.YCODE_DIR, "components"
)
PLAYWRIGHT_DIR = os.path.join(COMPONENT_ROOT, "playwright")


# ---------------------------------------------------------------- 组件注册表
#
# 设计：本模块原来是「playwright 专用」的，所有目录名/资产名/必检文件/版本约束都
# 硬编码在流程里。现在把「一个按需组件的静态画像」抽成 ComponentSpec，流程函数一律
# 接收 spec 工作（``_xxx_impl(spec, ...)``），playwright 只保留同名薄包装委托给
# ``PLAYWRIGHT_SPEC``——这样 gui_bridge / browser_backend / 测试的调用面一个字都不用改，
# 后续新增组件只需往 _COMPONENTS 里加一条 spec，不必再复制整套下载/校验/安装流程。


@dataclass(frozen=True)
class ComponentSpec:

    id: str
    display_name: str
    install_dir: str
    archive_name: str
    extract_prefix: str
    asset_hint: str
    required_files: tuple[str, ...] = ()
    required_dirs: tuple[str, ...] = ()
    version_spec: str = ""
    manifest_version_key: str = ""
    version_probe_file: str = ""
    import_name: str = ""
    env_url_var: str = ""
    error_prefix: str = ""
    settings_hint: str = ""
    size_hint_mb: int = 0
    version_label: str = ""


# Playwright 组件画像：每一项都必须与重构前的硬编码逐字一致（tests/test_components.py
# 与 gui_bridge.py 的文案/错误码/目录名断言都建立在这些值上）。
# version_spec 直接引用 REQUIRED_PLAYWRIGHT_SPEC：requirements.txt 的逐字比对测试只盯
# 那一个常量，避免「常量改了、spec 忘了改」的两处维护。
PLAYWRIGHT_SPEC = ComponentSpec(
    id="playwright",
    display_name="浏览器操控组件",
    install_dir="playwright",
    archive_name="playwright-component.zip",
    extract_prefix=".playwright-extract-",
    asset_hint="playwright-component",
    required_files=("playwright/driver/node.exe",),
    required_dirs=("playwright", "greenlet", "pyee"),
    version_spec=REQUIRED_PLAYWRIGHT_SPEC,
    manifest_version_key="playwright_version",
    version_probe_file="playwright/_repo_version.py",
    import_name="playwright",
    env_url_var="YCODE_PLAYWRIGHT_COMPONENT_URL",
    error_prefix="playwright_component",
    settings_hint="设置 → 电脑操控",
    size_hint_mb=25,
    version_label="Playwright",
)

FFMPEG_SPEC = ComponentSpec(
    id="ffmpeg",
    display_name="视频转换组件",
    install_dir="ffmpeg",
    archive_name="ffmpeg-component.zip",
    extract_prefix=".ffmpeg-extract-",
    asset_hint="ffmpeg-component",
    required_files=("ffmpeg.exe",),
    required_dirs=(),
    version_spec="",
    manifest_version_key="ffmpeg_version",
    version_probe_file="",
    import_name="",
    env_url_var="YCODE_FFMPEG_COMPONENT_URL",
    error_prefix="ffmpeg_component",
    settings_hint="设置 → 背景图转换",
    size_hint_mb=97,
    version_label="ffmpeg",
)

SEARCH_SPEC = ComponentSpec(
    id="search",
    display_name="网页搜索组件",
    install_dir="search",
    archive_name="search-component.zip",
    extract_prefix=".search-extract-",
    asset_hint="search-component",
    required_dirs=("ddgs", "primp", "lxml", "fake_useragent"),
    required_files=(),
    version_spec="",
    manifest_version_key="search_version",
    version_probe_file="",
    import_name="ddgs",
    env_url_var="YCODE_SEARCH_COMPONENT_URL",
    error_prefix="search_component",
    settings_hint="设置 → 网页搜索",
    size_hint_mb=18,
    version_label="search",
)

_COMPONENTS: "dict[str, ComponentSpec]" = {
    PLAYWRIGHT_SPEC.id: PLAYWRIGHT_SPEC,
    FFMPEG_SPEC.id: FFMPEG_SPEC,
    SEARCH_SPEC.id: SEARCH_SPEC,
}


class ComponentError(Exception):

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def __str__(self) -> str:
        return self.message


def get_component_spec(component_id: str) -> ComponentSpec:
    wanted = str(component_id or "").strip()
    spec = _COMPONENTS.get(wanted)
    if spec is None:
        raise ComponentError(f"{component_id}_component_unknown", "未知组件")
    return spec


def _component_root() -> str:
    override = (os.environ.get("YCODE_COMPONENT_DIR") or "").strip()
    if override:
        return os.path.abspath(override)
    return os.path.abspath(COMPONENT_ROOT)


def _component_dir(spec: ComponentSpec) -> str:
    override = (os.environ.get("YCODE_COMPONENT_DIR") or "").strip()
    if override:
        return os.path.join(os.path.abspath(override), spec.install_dir)
    if spec is PLAYWRIGHT_SPEC:
        return os.path.abspath(globals()["PLAYWRIGHT_DIR"])
    return os.path.abspath(os.path.join(_component_root(), spec.install_dir))


def _playwright_dir() -> str:
    return _component_dir(PLAYWRIGHT_SPEC)


def component_dir(component_id: str) -> str:
    return _component_dir(get_component_spec(component_id))


def _is_component_installed(spec: ComponentSpec) -> bool:
    # 空判据兜底：required_dirs 与 required_files 同时为空时，循环一个都不跑，
    # 函数会「恒真」——那等于把任何空目录都当成已安装。这是注册表泛化后新增的
    # 语义漏洞（spec 写错就会静默成立），所以显式判未安装。playwright / ffmpeg /
    # search 三者都至少有一个判据，本分支不影响它们的行为。
    if not spec.required_dirs and not spec.required_files:
        return False
    base = _component_dir(spec)
    for directory in spec.required_dirs:
        if not os.path.isdir(os.path.join(base, directory)):
            return False
    for filename in spec.required_files:
        if not os.path.isfile(os.path.join(base, filename)):
            return False
    return True


def playwright_installed() -> bool:
    return _is_component_installed(PLAYWRIGHT_SPEC)


def _read_component_version(spec: ComponentSpec) -> str:
    """读组件版本号；无法确定时返回空串。

    优先读组件目录 ``COMPONENT.json`` 的 ``spec.manifest_version_key`` 字段；
    其次读 ``spec.version_probe_file``（playwright 是 ``_repo_version.py``）里的
    ``__version__``。任何读取/解析异常都降级为空串（版本号只用于界面展示，绝不能报错）。
    """
    base = _component_dir(spec)
    if spec.manifest_version_key:
        manifest = os.path.join(base, _COMPONENT_MANIFEST)
        try:
            with open(manifest, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            version = str((data or {}).get(spec.manifest_version_key) or "").strip()
            if version:
                return version
        except Exception:
            pass
    if spec.version_probe_file:
        try:
            repo_version = os.path.join(base, spec.version_probe_file)
            with open(repo_version, "r", encoding="utf-8") as handle:
                text = handle.read(64 * 1024)
            match = _REPO_VERSION_RE.search(text)
            if match:
                return match.group(1).strip()
        except Exception:
            pass
    return ""


def playwright_version() -> str:
    """返回已安装 Playwright 组件的版本号（``_read_component_version`` 薄包装）。"""
    return _read_component_version(PLAYWRIGHT_SPEC)


_VERSION_PREFIX_RE = re.compile(r"^\s*[vV]?(\d+(?:\.\d+)*)")
_SPEC_ITEM_RE = re.compile(r"^\s*(>=|<=|==|>|<)\s*(\d+(?:\.\d+)*)\s*$")


def _version_tuple(version: str):
    match = _VERSION_PREFIX_RE.match(str(version or ""))
    if not match:
        return None
    try:
        return tuple(int(part) for part in match.group(1).split("."))
    except ValueError:  # pragma: no cover - 正则已保证只含数字与点
        return None


def _compare_versions(left: tuple, right: tuple) -> int:
    """比较两个版本元组（短的一侧补 0）：left 小返回 -1，相等 0，否则 1。"""
    length = max(len(left), len(right))
    filled_left = left + (0,) * (length - len(left))
    filled_right = right + (0,) * (length - len(right))
    return (filled_left > filled_right) - (filled_left < filled_right)


def _parse_version_spec(spec: str) -> list:
    parsed = []
    for chunk in str(spec or "").split(","):
        match = _SPEC_ITEM_RE.match(chunk)
        if not match:
            continue
        version = _version_tuple(match.group(2))
        if version is None:
            continue
        parsed.append((match.group(1), version))
    return parsed


_COMPARATORS = {
    ">=": lambda relation: relation >= 0,
    ">": lambda relation: relation > 0,
    "<=": lambda relation: relation <= 0,
    "<": lambda relation: relation < 0,
    "==": lambda relation: relation == 0,
}


def _version_compatible(spec: ComponentSpec, version: str = ""):
    """判断组件版本是否满足 ``spec.version_spec``。

    返回 True（满足）/ False（确定不满足）/ None（版本号为空或约束解析不出来——
    属于「不知道」，绝不当成不匹配去误报）。
    """
    actual = _version_tuple(version)
    if actual is None:
        return None
    constraints = _parse_version_spec(spec.version_spec)
    if not constraints:
        return None
    for operator, required in constraints:
        comparator = _COMPARATORS.get(operator)
        if comparator is None:  # pragma: no cover - 正则已限制运算符集合
            continue
        if not comparator(_compare_versions(actual, required)):
            return False
    return True


def playwright_version_compatible(version: str = ""):
    """判断组件版本是否满足主程序要求 ``REQUIRED_PLAYWRIGHT_SPEC``（薄包装）。"""
    return _version_compatible(PLAYWRIGHT_SPEC, version)


def _version_warning_impl(spec: ComponentSpec, version: str) -> str:
    if _version_compatible(spec, version) is not False:
        return ""
    label = spec.version_label or spec.display_name
    return (
        f"组件版本 {version} 与当前程序要求的 {label} "
        f"{spec.version_spec} 不匹配，"
        f"建议在「{spec.settings_hint}」重新下载组件"
    )


def _version_warning(version: str) -> str:
    return _version_warning_impl(PLAYWRIGHT_SPEC, version)


_IMPORT_LOCK = threading.Lock()
_import_ready = False


def _path_in_sys_path(path: str) -> bool:
    target = os.path.normcase(os.path.abspath(path))
    for entry in list(sys.path):
        if not entry:
            continue
        try:
            if os.path.normcase(os.path.abspath(entry)) == target:
                return True
        except Exception:
            continue
    return False


def _try_import_playwright() -> bool:
    try:
        import playwright  # noqa: F401  # 只验证包可导入，不启动任何浏览器
    except Exception:
        return False
    return True


def ensure_playwright() -> None:
    global _import_ready
    if _import_ready:
        return
    with _IMPORT_LOCK:
        if _import_ready:
            return
        if _try_import_playwright():
            _import_ready = True
            return
        playwright_dir = _playwright_dir()
        if not _path_in_sys_path(playwright_dir):
            sys.path.insert(0, playwright_dir)
            importlib.invalidate_caches()
        if _try_import_playwright():
            _import_ready = True
            return
        raise ComponentError(
            "playwright_component_missing",
            "Playwright 组件未安装，电脑操控无法启动。"
            "请打开「设置 → 电脑操控」点击「下载组件」后重试。",
        )


# 网页搜索组件的导入缓存。刻意用独立布尔而**不复用** _import_ready：
# 那是 playwright 的专属缓存，两个组件是互相独立的包——「playwright 已就绪」绝不
# 代表 ddgs 已就绪。共用一个布尔会导致两种错误：要么跳过 sys.path 回退（组件装在
# 用户目录时 import 永远失败），要么在 ddgs 缺失时被当成已就绪而静默放过。
_search_import_ready = False


def _try_import_ddgs() -> bool:
    try:
        import ddgs  # noqa: F401  # 只验证包可导入，不发起任何网络请求
    except Exception:
        return False
    return True


def ensure_search_component() -> None:
    global _search_import_ready
    if _search_import_ready:
        return
    with _IMPORT_LOCK:
        if _search_import_ready:
            return
        if _try_import_ddgs():
            _search_import_ready = True
            return
        search_dir = _component_dir(SEARCH_SPEC)
        if not _path_in_sys_path(search_dir):
            sys.path.insert(0, search_dir)
            importlib.invalidate_caches()
        if _try_import_ddgs():
            _search_import_ready = True
            return
        raise ComponentError(
            "search_component_missing",
            "网页搜索组件未安装，请到「设置 → 网页搜索」下载",
        )


_STATE_LOCK = threading.Lock()
# Playwright 的下载线程句柄（历史符号，测试会 monkeypatch，不能改名/删除）
_download_thread: "threading.Thread | None" = None
_cancel_event = threading.Event()


def _new_state() -> dict:
    return {
        "downloading": False,
        "stage": "",
        "percent": 0,
        "downloaded_bytes": 0,
        "total_bytes": 0,
        "error": "",
        "digest_verified": False,
        # 非致命告警（例如 Release 说明里没有组件包校验行）：安装继续但必须可见
        "warning": "",
    }


_state = _new_state()

_states: dict[str, dict] = {}
_active_component_id: "str | None" = None
_component_threads: dict[str, threading.Thread] = {}
_component_cancel_events: dict[str, threading.Event] = {}


class _DownloadCancelled(Exception):
    pass


def _state_for(spec: ComponentSpec) -> dict:
    if spec is PLAYWRIGHT_SPEC:
        return globals()["_state"]
    return _states.setdefault(spec.id, _new_state())


def _cancel_event_for(spec: ComponentSpec) -> threading.Event:
    if spec is PLAYWRIGHT_SPEC:
        return globals()["_cancel_event"]
    return _component_cancel_events.setdefault(spec.id, threading.Event())


def _download_thread_for(spec: ComponentSpec) -> "threading.Thread | None":
    if spec is PLAYWRIGHT_SPEC:
        return globals()["_download_thread"]
    return _component_threads.get(spec.id)


def _set_download_thread(spec: ComponentSpec, thread) -> None:
    if spec is PLAYWRIGHT_SPEC:
        globals()["_download_thread"] = thread
    else:
        _component_threads[spec.id] = thread


def _begin_active(spec: ComponentSpec) -> bool:
    global _active_component_id
    if _active_component_id is not None:
        return False
    _active_component_id = spec.id
    return True


def _end_active(spec: ComponentSpec) -> None:
    global _active_component_id
    if _active_component_id == spec.id:
        _active_component_id = None


def _emit_impl(spec: ComponentSpec, on_event, stage: str, **overrides) -> None:
    """向调用方回调一条进度事件；回调自身抛错绝不影响下载线程。"""
    if on_event is None:
        return
    with _STATE_LOCK:
        state = _state_for(spec)
        payload = {
            "stage": stage,
            "percent": int(state["percent"]),
            "downloaded_bytes": int(state["downloaded_bytes"]),
            "total_bytes": int(state["total_bytes"]),
            "error": str(state["error"]),
        }
    payload.update(overrides)
    try:
        on_event(payload)
    except Exception:  # pragma: no cover - GUI 回调异常不得中断下载
        logger.warning("组件下载进度回调异常", exc_info=True)


def _emit(on_event, stage: str, **overrides) -> None:
    _emit_impl(PLAYWRIGHT_SPEC, on_event, stage, **overrides)


def _update_state_impl(spec: ComponentSpec, **fields) -> None:
    with _STATE_LOCK:
        _state_for(spec).update(fields)


def _update_state(**fields) -> None:
    _update_state_impl(PLAYWRIGHT_SPEC, **fields)


def _check_cancel_impl(spec: ComponentSpec) -> None:
    if _cancel_event_for(spec).is_set():
        raise _DownloadCancelled()


def _check_cancel() -> None:
    _check_cancel_impl(PLAYWRIGHT_SPEC)


def _get_status_impl(spec: ComponentSpec) -> dict:
    installed = _is_component_installed(spec)
    version = _read_component_version(spec) if installed else ""
    # 版本比对（Minor 4）：只判断「能不能 import」不够——Release 升级主程序后沿用旧
    # 组件时，界面必须能看见「组件版本与当前程序要求不匹配」的中文提示。
    compatible = _version_compatible(spec, version) if installed else None
    with _STATE_LOCK:
        snapshot = dict(_state_for(spec))
    return {
        "installed": installed,
        "version": version,
        "downloading": bool(snapshot["downloading"]),
        "percent": int(snapshot["percent"]),
        "downloaded_bytes": int(snapshot["downloaded_bytes"]),
        "total_bytes": int(snapshot["total_bytes"]),
        "error": str(snapshot["error"]),
        "digest_verified": bool(snapshot.get("digest_verified", False)),
        "warning": str(snapshot.get("warning") or ""),
        "required_version": spec.version_spec,
        "version_compatible": compatible,
        "outdated": compatible is False,
        "version_warning": _version_warning_impl(spec, version),
        "dir": _component_dir(spec),
        "stage": str(snapshot["stage"]),
    }


def get_status() -> dict:
    return _get_status_impl(PLAYWRIGHT_SPEC)


def _iter_release_assets(release: dict):
    assets = (release or {}).get("assets")
    if isinstance(assets, list):
        for item in assets:
            if isinstance(item, dict):
                yield item
    yield {
        "name": (release or {}).get("asset_name") or "",
        "download_url": (release or {}).get("download_url") or "",
    }


_RELEASE_TIMEOUT = 8.0


def _fetch_latest_release_entry() -> dict:
    from core.updater import GITEE_REPO, HTTP_USER_AGENT

    url = (
        f"https://gitee.com/api/v5/repos/{GITEE_REPO}/releases"
        "?per_page=5&direction=desc"
    )
    request = urllib.request.Request(
        url,
        headers={"User-Agent": HTTP_USER_AGENT, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=_RELEASE_TIMEOUT) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception as error:
        logger.warning("获取组件 Release 条目失败: %s", error)
        return {}
    if not isinstance(data, list):
        return {}
    entry = next(
        (
            item
            for item in data
            if isinstance(item, dict)
            and str(item.get("tag_name") or "").strip()
            and not item.get("prerelease")
        ),
        None,
    )
    return entry if isinstance(entry, dict) else {}


def _parse_component_digest(release_notes: str, asset_name: str = "") -> str:
    matches = [
        (str(name).strip().lower(), str(digest).strip().lower())
        for name, digest in _COMPONENT_DIGEST_RE.findall(str(release_notes or ""))
    ]
    if not matches:
        return ""
    wanted = (asset_name or "").strip().lower()
    if wanted:
        for name, digest in matches:
            if name == wanted:
                return digest
    if len(matches) == 1:
        return matches[0][1]
    logger.warning(
        "Release 说明里有 %d 条组件包校验行，但没有一条匹配资产名 %r，本次不做校验",
        len(matches),
        asset_name,
    )
    return ""


def _resolve_component_source_impl(spec: ComponentSpec, url: str = "") -> tuple:
    explicit = (url or "").strip()
    if explicit:
        return explicit, ""
    env_url = ""
    if spec.env_url_var:
        env_url = (os.environ.get(spec.env_url_var) or "").strip()
    if env_url:
        return env_url, ""
    # 主路径：读 Release 的完整条目——组件包与主安装包挂在同一个 Release 下，
    # fetch_latest_release 只挑主安装包且不返回发布说明，必须自己取 assets + body。
    entry = _fetch_latest_release_entry()
    notes = str((entry or {}).get("body") or "")
    for asset in (entry or {}).get("assets") or []:
        if not isinstance(asset, dict):
            continue
        name = str(asset.get("name") or "").strip()
        lowered = name.lower()
        if spec.asset_hint not in lowered or not lowered.endswith(".zip"):
            continue
        link = str(
            asset.get("download_url") or asset.get("browser_download_url") or ""
        ).strip()
        if link:
            return link, _parse_component_digest(notes, name)
    # 回退：fetch_latest_release 的摊平字段（兼容只挂单个附件的旧 Release）
    try:
        from core.updater import fetch_latest_release

        release = fetch_latest_release() or {}
    except Exception:
        release = {}
    for asset in _iter_release_assets(release):
        name = str(asset.get("name") or "").strip()
        lowered = name.lower()
        if spec.asset_hint not in lowered or not lowered.endswith(".zip"):
            continue
        link = str(
            asset.get("download_url") or asset.get("browser_download_url") or ""
        ).strip()
        if link:
            notes = str(release.get("release_notes") or release.get("body") or "")
            return link, _parse_component_digest(notes, name)
    detail = str(release.get("msg") or "").strip()
    hint = f"（{detail}）" if detail else ""
    raise ComponentError(
        f"{spec.error_prefix}_download_failed",
        f"未找到组件下载源：Release 里没有 {spec.asset_hint} 压缩包，"
        "请检查网络或稍后重试" + hint,
    )


def _resolve_component_source(url: str = "") -> tuple:
    return _resolve_component_source_impl(PLAYWRIGHT_SPEC, url)


def _resolve_component_url(url: str = "") -> str:
    return _resolve_component_source_impl(PLAYWRIGHT_SPEC, url)[0]


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(_CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _download_archive_impl(spec: ComponentSpec, archive_url: str, tmp_path: str, on_event) -> None:
    request = urllib.request.Request(
        archive_url,
        headers={"User-Agent": _USER_AGENT, "Accept": "*/*"},
    )
    try:
        response = urllib.request.urlopen(request, timeout=_READ_TIMEOUT)
    except Exception as error:
        raise ComponentError(
            f"{spec.error_prefix}_download_failed",
            f"组件包下载失败：{type(error).__name__}: {error}",
        ) from error
    with response:
        try:
            total = int(response.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            total = 0
        downloaded = 0
        _update_state_impl(
            spec, downloaded_bytes=0, total_bytes=total, percent=0, error=""
        )
        _emit_impl(
            spec, on_event, "downloading", percent=0, downloaded_bytes=0, total_bytes=total
        )
        try:
            with open(tmp_path, "wb") as handle:
                while True:
                    _check_cancel_impl(spec)
                    chunk = response.read(_CHUNK_SIZE)
                    if not chunk:
                        break
                    handle.write(chunk)
                    downloaded += len(chunk)
                    percent = 0
                    if total > 0:
                        percent = min(
                            _DOWNLOAD_PERCENT_MAX, int(downloaded * 100 / total)
                        )
                    _update_state_impl(spec, downloaded_bytes=downloaded, percent=percent)
                    _emit_impl(
                        spec,
                        on_event,
                        "downloading",
                        percent=percent,
                        downloaded_bytes=downloaded,
                        total_bytes=total,
                    )
        except ComponentError:
            raise
        except Exception as error:
            if isinstance(error, _DownloadCancelled):
                raise
            raise ComponentError(
                f"{spec.error_prefix}_download_failed",
                f"组件包下载中断：{type(error).__name__}: {error}",
            ) from error
        if total > 0 and downloaded < total:
            raise ComponentError(
                f"{spec.error_prefix}_download_failed",
                f"组件包下载不完整（{downloaded}/{total} 字节），请重试",
            )


def _download_archive(archive_url: str, tmp_path: str, on_event) -> None:
    _download_archive_impl(PLAYWRIGHT_SPEC, archive_url, tmp_path, on_event)


def _verify_sha256_impl(spec: ComponentSpec, archive_path: str, expected_sha256: str) -> None:
    expected = (expected_sha256 or "").strip().lower()
    if not expected:
        return
    actual = _sha256_file(archive_path)
    if actual != expected:
        raise ComponentError(
            f"{spec.error_prefix}_corrupt",
            "组件包校验失败（SHA256 不匹配），文件可能已损坏或被篡改，请重新下载",
        )


def _verify_sha256(archive_path: str, expected_sha256: str) -> None:
    _verify_sha256_impl(PLAYWRIGHT_SPEC, archive_path, expected_sha256)


def _is_unsafe_member_name(name: str) -> bool:
    if not name:
        return True
    unified = name.replace("\\", "/")
    if unified.startswith("/"):
        return True
    if os.path.isabs(name):
        return True
    first = unified.split("/", 1)[0]
    if len(first) == 2 and first[1] == ":" and first[0].isalpha():
        return True
    return True if ".." in unified.split("/") else False


def _extract_zip_impl(spec: ComponentSpec, archive_path: str, dest_dir: str) -> None:
    base = os.path.realpath(dest_dir)
    os.makedirs(base, exist_ok=True)
    try:
        archive = zipfile.ZipFile(archive_path)
    except Exception as error:
        raise ComponentError(
            f"{spec.error_prefix}_corrupt",
            f"组件包无法打开（可能不是有效的 zip）：{type(error).__name__}",
        ) from error
    with archive:
        for member in archive.infolist():
            name = member.filename
            if _is_unsafe_member_name(name):
                raise ComponentError(
                    f"{spec.error_prefix}_corrupt",
                    f"组件包含不安全的路径（{name}），已拒绝解压",
                )
            unified = name.replace("\\", "/")
            parts = [part for part in unified.split("/") if part not in ("", ".")]
            if not parts:
                continue
            target = os.path.realpath(os.path.join(base, *parts))
            if target != base and not target.startswith(base + os.sep):
                raise ComponentError(
                    f"{spec.error_prefix}_corrupt",
                    f"组件包解压路径越界（{name}），已拒绝解压",
                )
            if unified.endswith("/"):
                os.makedirs(target, exist_ok=True)
                continue
            try:
                os.makedirs(os.path.dirname(target) or base, exist_ok=True)
                with archive.open(member) as source, open(target, "wb") as sink:
                    shutil.copyfileobj(source, sink, _CHUNK_SIZE)
            except ComponentError:
                raise
            except Exception as error:
                raise ComponentError(
                    f"{spec.error_prefix}_corrupt",
                    f"组件包解压失败（{name}）：{type(error).__name__}: {error}",
                ) from error


def _extract_zip(archive_path: str, dest_dir: str) -> None:
    _extract_zip_impl(PLAYWRIGHT_SPEC, archive_path, dest_dir)


def _locate_component_root_impl(spec: ComponentSpec, staging_dir: str) -> str:
    probes = list(spec.required_files) or list(spec.required_dirs)
    probe_relative = probes[0] if probes else ""
    check_isfile = bool(spec.required_files)

    def _looks_like_component(candidate: str) -> bool:
        if not os.path.isfile(os.path.join(candidate, _COMPONENT_MANIFEST)):
            return False
        if not probe_relative:
            return True
        target = os.path.join(candidate, probe_relative)
        return os.path.isfile(target) if check_isfile else os.path.isdir(target)

    candidates = [staging_dir]
    try:
        for entry in sorted(os.listdir(staging_dir)):
            full = os.path.join(staging_dir, entry)
            if os.path.isdir(full):
                candidates.append(full)
    except OSError:
        pass
    for candidate in candidates:
        if _looks_like_component(candidate):
            return candidate
    raise ComponentError(
        f"{spec.error_prefix}_corrupt",
        f"组件包结构不完整（缺少 {_COMPONENT_MANIFEST} 或 {probe_relative}），"
        "请重新下载",
    )


def _locate_component_root(staging_dir: str) -> str:
    return _locate_component_root_impl(PLAYWRIGHT_SPEC, staging_dir)


def _install_from_staging(staging_root: str, target_dir: str) -> None:
    parent = os.path.dirname(target_dir)
    os.makedirs(parent, exist_ok=True)
    backup_dir = ""
    if os.path.exists(target_dir):
        backup_dir = f"{target_dir}.bak-{time.strftime('%Y%m%d%H%M%S')}"
        suffix = 1
        while os.path.exists(backup_dir):
            backup_dir = f"{target_dir}.bak-{time.strftime('%Y%m%d%H%M%S')}-{suffix}"
            suffix += 1
        os.rename(target_dir, backup_dir)
    try:
        os.rename(staging_root, target_dir)
    except Exception:
        if backup_dir and os.path.exists(backup_dir) and not os.path.exists(target_dir):
            try:
                os.rename(backup_dir, target_dir)
            except Exception:  # pragma: no cover - 回滚失败只能靠日志排查
                logger.exception("组件安装回滚失败，旧目录保留在 %s", backup_dir)
        raise
    if backup_dir:
        shutil.rmtree(backup_dir, ignore_errors=True)


def _cleanup_paths(*paths: str) -> None:
    """删除临时文件/目录；失败只告警，绝不影响主流程结论。"""
    for path in paths:
        if not path:
            continue
        try:
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
            elif os.path.exists(path):
                os.remove(path)
        except Exception:
            logger.warning("清理组件临时路径失败: %s", path)


def _download_worker_impl(
    spec: ComponentSpec, on_event, expected_sha256: str, url: str
) -> None:
    """下载线程主体：解析下载源与官方 digest → 下载 → 校验 → 解压 → 校验结构 → 原子安装。"""
    root = _component_root()
    target_dir = _component_dir(spec)
    archive_path = os.path.join(root, spec.archive_name)
    tmp_archive = archive_path + ".tmp"
    staging_dir = os.path.join(root, f"{spec.extract_prefix}{os.getpid()}")
    try:
        archive_url, release_sha256 = _resolve_component_source_impl(spec, url)
        expected = (expected_sha256 or "").strip() or release_sha256
        if not expected:
            # 拿不到官方 digest 时不能静默放行：日志 + 进度事件 + 状态三处都要可见
            warning = (
                "未能从 Release 说明获取组件包 SHA256，本次安装未做完整性校验"
                "（存在被篡改风险），请提醒发版方补上组件包校验行"
            )
            logger.warning(f"{warning}（下载地址：{archive_url}）")
            _update_state_impl(spec, warning=warning, digest_verified=False)
            _emit_impl(spec, on_event, "downloading", warning=warning)
        else:
            _update_state_impl(spec, warning="", digest_verified=True)
        os.makedirs(root, exist_ok=True)
        _cleanup_paths(tmp_archive, staging_dir)

        _download_archive_impl(spec, archive_url, tmp_archive, on_event)
        _check_cancel_impl(spec)

        _verify_sha256(tmp_archive, expected)
        _check_cancel_impl(spec)

        try:
            os.replace(tmp_archive, archive_path)
        except Exception:
            _cleanup_paths(tmp_archive)
            raise ComponentError(
                f"{spec.error_prefix}_download_failed", "组件包落盘失败，请重试"
            )

        _update_state_impl(spec, stage="extracting", percent=_EXTRACT_PERCENT)
        _emit_impl(spec, on_event, "extracting", percent=_EXTRACT_PERCENT)
        _cleanup_paths(staging_dir)
        _extract_zip_impl(spec, archive_path, staging_dir)
        _check_cancel_impl(spec)

        staging_root = _locate_component_root_impl(spec, staging_dir)
        _install_from_staging(staging_root, target_dir)

        _cleanup_paths(archive_path)

        _update_state_impl(
            spec,
            stage="done",
            percent=100,
            error="",
            downloading=False,
        )
        _emit_impl(spec, on_event, "done", percent=100, error="")
        logger.info(f"{spec.display_name}安装完成: {target_dir}")
    except _DownloadCancelled:
        _update_state_impl(spec, stage="", percent=0, error="", downloading=False)
        logger.info(f"{spec.display_name}下载已取消")
    except ComponentError as error:
        _update_state_impl(
            spec,
            stage="error",
            error=str(error),
            downloading=False,
        )
        _emit_impl(spec, on_event, "error", error=str(error))
        logger.warning(f"{spec.display_name}安装失败[{error.code}]: {error}")
    except Exception as error:  # 兜底：任何意外都转成 error 事件，绝不让线程静默死掉
        message = f"组件安装失败：{type(error).__name__}: {error}"
        _update_state_impl(spec, stage="error", error=message, downloading=False)
        _emit_impl(spec, on_event, "error", error=message)
        logger.exception(f"{spec.display_name}安装异常")
    finally:
        _cleanup_paths(tmp_archive, staging_dir)
        _end_active(spec)
        with _STATE_LOCK:
            _state_for(spec).update(downloading=False)


def _download_worker(on_event, expected_sha256: str, url: str) -> None:
    _download_worker_impl(PLAYWRIGHT_SPEC, on_event, expected_sha256, url)


def _start_download_impl(
    spec: ComponentSpec, on_event=None, expected_sha256: str = "", url: str = ""
) -> bool:
    state = _state_for(spec)
    with _STATE_LOCK:
        if state.get("downloading") or not _begin_active(spec):
            return False
        state.update(
            downloading=True,
            stage="downloading",
            percent=0,
            downloaded_bytes=0,
            total_bytes=0,
            error="",
            warning="",
            digest_verified=False,
        )
    _cancel_event_for(spec).clear()
    thread = threading.Thread(
        target=_download_worker_impl,
        args=(spec, on_event, expected_sha256, url),
        name=f"ycode-{spec.id}-component-download",
        daemon=True,
    )
    _set_download_thread(spec, thread)
    try:
        thread.start()
    except Exception as error:  # pragma: no cover - 线程创建失败属极端情况
        _end_active(spec)
        with _STATE_LOCK:
            state.update(
                downloading=False,
                stage="error",
                error=f"无法启动下载线程：{type(error).__name__}",
            )
        return False
    return True


def start_download(on_event=None, expected_sha256: str = "", url: str = "") -> bool:
    return _start_download_impl(
        PLAYWRIGHT_SPEC, on_event=on_event, expected_sha256=expected_sha256, url=url
    )


def _cancel_download_impl(spec: ComponentSpec) -> None:
    """请求取消某个组件的下载；只在它自己空闲或正在下载时生效。

    全局同一时刻只允许一个组件下载，因此别的组件在下载时本调用直接返回，
    绝不去置别人的取消标志（否则会把无辜的下载打断）。
    """
    with _STATE_LOCK:
        active = _active_component_id
        if active is not None and active != spec.id:
            return
        _cancel_event_for(spec).set()
        state = _state_for(spec)
        if state.get("downloading"):
            state["error"] = ""


def cancel_download() -> None:
    """请求取消当前下载：置取消标志，工作线程在下一块下载时退出并清理临时文件。"""
    _cancel_download_impl(PLAYWRIGHT_SPEC)


def get_component_status(component_id: str) -> dict:
    """返回指定组件的状态快照（字段与 ``get_status`` 完全一致）。"""
    return _get_status_impl(get_component_spec(component_id))


def start_component_download(
    component_id: str, on_event=None, expected_sha256: str = "", url: str = ""
) -> bool:
    """后台下载并安装指定组件；已有组件在下载中返回 False。"""
    return _start_download_impl(
        get_component_spec(component_id),
        on_event=on_event,
        expected_sha256=expected_sha256,
        url=url,
    )


def cancel_component_download(component_id: str) -> None:
    """请求取消指定组件的下载（只取消当前正在下载的那个）。"""
    _cancel_download_impl(get_component_spec(component_id))
