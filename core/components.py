"""core/components.py
====================
按需组件（首次使用时下载）管理模块。

背景：安装包已不再自带 Playwright（见 ycode.spec 的 collect_all/excludes 注释），
打包版首次进入「电脑操控」时才需要把 Playwright 组件补装到用户目录。

组件目录约定（``YCODE_COMPONENT_DIR`` 可覆盖根目录）::

    ~/.ycode/components/
        playwright/                     <- PLAYWRIGHT_DIR（组件安装根）
            COMPONENT.json              <- {"playwright_version": "1.4x.x", ...}
            playwright/                 <- Playwright 包本体
                driver/node.exe         <- node driver（必须存在，否则视为损坏）
            greenlet/                   <- 运行期依赖
            pyee/                       <- 运行期依赖
        playwright-component.zip        <- 下载的组件压缩包（安装成功即删除，失败时保留）

依赖约束：本模块只依赖标准库 + ``config`` + 按需 import 的 ``core.updater``，
绝不 import 任何 GUI 模块——CLI（ycode.py / cmd_*）必须能直接使用。

下载/安装流程（全后台线程，可取消）：
    download -> 校验 SHA256 -> 安全解压（防目录穿越）-> 校验目录结构
    -> 原子替换安装（旧目录先改名为 .bak-<时间戳>，成功后删除）

失败一律不留半装状态：临时 zip / 临时解压目录都在 finally 中清理，
PLAYWRIGHT_DIR 在替换成功前保持原状；安装成功（stage=done）后删除下载的组件包，
失败时保留该包供人工排查/复用。
"""

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

import config

logger = logging.getLogger(__name__)

# 下载分块大小：256KB。与 core.updater 的分块流式下载保持一致的体感，
# 又不会因过小的块导致回调风暴。
_CHUNK_SIZE = 256 * 1024

# 下载超时（秒）。组件包通常几十 MB，给足首包与单次 read 的时间。
_READ_TIMEOUT = 60.0

# 进度区间：下载阶段映射到 0-90，解压阶段 95，完成 100（percent 全为 int）。
_DOWNLOAD_PERCENT_MAX = 90
_EXTRACT_PERCENT = 95

# 组件压缩包文件名（下载目标固定命名，便于复用与人工排查）
_COMPONENT_ARCHIVE_NAME = "playwright-component.zip"

# 组件元信息文件名（解压后必须存在）
_COMPONENT_MANIFEST = "COMPONENT.json"

# 解压暂存目录前缀（期号避免并发/残留互相踩）
_EXTRACT_DIR_PREFIX = ".playwright-extract-"

# 组件内必检的关键文件（相对 PLAYWRIGHT_DIR）
_REQUIRED_NODE_DRIVER = os.path.join("playwright", "driver", "node.exe")

# 版本号提取：playwright/_repo_version.py 里的 __version__ = "1.4x.x"
_REPO_VERSION_RE = re.compile(
    r"""^__version__\s*=\s*['"]([^'"]+)['"]""", re.MULTILINE
)

# 下载源资产名匹配：Release 附件名需包含该片段且以 .zip 结尾
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

# Release 说明里的组件包校验行（由 build_release.py 生成，格式固定）：
#     playwright-component-1.51.0.zip  SHA256=<64 位十六进制>
# 刻意用 "=" 而不是 ":"：core.updater 的 _SHA256_RE 只认 "SHA256: <hex>"，
# 用冒号会让主安装包的官方校验和解析错取组件包 digest。
_COMPONENT_DIGEST_RE = re.compile(
    r"(?im)^[^\S\n]*(\S+\.zip)[^\S\n]+SHA256[^\S\n]*=[^\S\n]*([0-9a-f]{64})[^\S\n]*$"
)

_USER_AGENT = "ycode-components/1.0 (Windows)"

# 默认组件根目录；环境变量 YCODE_COMPONENT_DIR 优先（隔离运行/测试用）。
COMPONENT_ROOT = os.environ.get("YCODE_COMPONENT_DIR") or os.path.join(
    config.YCODE_DIR, "components"
)
# Playwright 组件安装目录
PLAYWRIGHT_DIR = os.path.join(COMPONENT_ROOT, "playwright")


class ComponentError(Exception):
    """组件领域错误；``code`` 为稳定英文错误码，message 为中文提示。

    错误码：
      - ``playwright_component_missing``：组件未安装/无法导入
      - ``playwright_component_download_failed``：下载源解析或网络传输失败
      - ``playwright_component_corrupt``：包校验失败、解压不安全或结构不完整
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def __str__(self) -> str:
        return self.message


# ---------------------------------------------------------------- 路径解析

def _component_root() -> str:
    """返回组件根目录（环境变量 YCODE_COMPONENT_DIR > 模块常量 COMPONENT_ROOT）。"""
    override = (os.environ.get("YCODE_COMPONENT_DIR") or "").strip()
    if override:
        return os.path.abspath(override)
    return os.path.abspath(COMPONENT_ROOT)


def _playwright_dir() -> str:
    """返回 Playwright 组件安装目录。

    环境变量覆盖组件根目录时自动跟随；否则使用模块常量 ``PLAYWRIGHT_DIR``
    （测试可 monkeypatch 该常量）。
    """
    override = (os.environ.get("YCODE_COMPONENT_DIR") or "").strip()
    if override:
        return os.path.join(os.path.abspath(override), "playwright")
    return os.path.abspath(PLAYWRIGHT_DIR)


# ---------------------------------------------------------------- 安装检测

def playwright_installed() -> bool:
    """检查 Playwright 组件是否已完整安装到组件目录。

    判定条件（全部满足才算已安装）：存在 ``playwright/`` 包目录、
    ``playwright/driver/node.exe``、运行期依赖 ``greenlet/`` 与 ``pyee/``。
    只做文件系统探测，绝不 import，保证可被 CLI 低成本调用。
    """
    base = _playwright_dir()
    package_dir = os.path.join(base, "playwright")
    if not os.path.isdir(package_dir):
        return False
    if not os.path.isfile(os.path.join(base, _REQUIRED_NODE_DRIVER)):
        return False
    for dependency in ("greenlet", "pyee"):
        if not os.path.isdir(os.path.join(base, dependency)):
            return False
    return True


def playwright_version() -> str:
    """返回已安装组件的 Playwright 版本号；无法确定时返回空串。

    优先读组件目录 ``COMPONENT.json`` 的 ``playwright_version`` 字段；
    其次读 ``playwright/_repo_version.py`` 里的 ``__version__``。
    任何读取/解析异常都降级为空串（版本号只用于界面展示，绝不能报错）。
    """
    base = _playwright_dir()
    manifest = os.path.join(base, _COMPONENT_MANIFEST)
    try:
        with open(manifest, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        version = str((data or {}).get("playwright_version") or "").strip()
        if version:
            return version
    except Exception:
        pass
    try:
        repo_version = os.path.join(base, "playwright", "_repo_version.py")
        with open(repo_version, "r", encoding="utf-8") as handle:
            text = handle.read(64 * 1024)
        match = _REPO_VERSION_RE.search(text)
        if match:
            return match.group(1).strip()
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------- 版本比对

# 版本号前缀解析：``1.62.0`` / ``v1.55`` / ``1.62.0.post1`` 都取前导数字段
_VERSION_PREFIX_RE = re.compile(r"^\s*[vV]?(\d+(?:\.\d+)*)")
# 约束片段解析：``>=1.55`` / ``<2``
_SPEC_ITEM_RE = re.compile(r"^\s*(>=|<=|==|>|<)\s*(\d+(?:\.\d+)*)\s*$")


def _version_tuple(version: str):
    """把版本号字符串解析成整数元组；无法解析时返回 None。"""
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
    """把 ``>=1.55,<2`` 解析成 ``[('>=', (1, 55)), ('<', (2,))]``。

    无法识别的片段直接跳过（与 pip 对未知标记的宽容处理一致）。
    """
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


def playwright_version_compatible(version: str = ""):
    """判断组件版本是否满足主程序要求 ``REQUIRED_PLAYWRIGHT_SPEC``。

    返回 True（满足）/ False（确定不满足）/ None（版本号为空或约束解析不出来——
    属于「不知道」，绝不当成不匹配去误报）。
    """
    actual = _version_tuple(version)
    if actual is None:
        return None
    constraints = _parse_version_spec(REQUIRED_PLAYWRIGHT_SPEC)
    if not constraints:
        return None
    for operator, required in constraints:
        comparator = _COMPARATORS.get(operator)
        if comparator is None:  # pragma: no cover - 正则已限制运算符集合
            continue
        if not comparator(_compare_versions(actual, required)):
            return False
    return True


def _version_warning(version: str) -> str:
    """生成「组件版本与主程序要求不匹配」的中文提示；匹配或无法判断时返回空串。"""
    if playwright_version_compatible(version) is not False:
        return ""
    return (
        f"组件版本 {version} 与当前程序要求的 Playwright "
        f"{REQUIRED_PLAYWRIGHT_SPEC} 不匹配，"
        "建议在「设置 → 电脑操控」重新下载组件"
    )


# ---------------------------------------------------------------- 导入保障

_IMPORT_LOCK = threading.Lock()
_import_ready = False


def _path_in_sys_path(path: str) -> bool:
    """判断某个路径是否已在 sys.path 中（Windows 大小写/分隔符归一后比较）。"""
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
    """尝试 ``import playwright``，成功返回 True，失败（含任何导入异常）返回 False。"""
    try:
        import playwright  # noqa: F401  # 只验证包可导入，不启动任何浏览器
    except Exception:
        return False
    return True


def ensure_playwright() -> None:
    """确保 Playwright 可被导入，否则抛出 ``ComponentError``。

    顺序：已可 ``import playwright``（例如开发环境装在 site-packages）→ 直接返回；
    否则把组件目录插到 ``sys.path`` 首位后重试一次；仍失败则抛
    ``ComponentError('playwright_component_missing', ...)``，提示用户去
    「设置 → 电脑操控」点「下载组件」。

    导入成功后置模块级缓存，后续调用零开销；同一个路径绝不重复插入 sys.path。
    """
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
            # 组件目录可能刚刚被安装进磁盘，清缓存保证新目录里的包能被发现
            importlib.invalidate_caches()
        if _try_import_playwright():
            _import_ready = True
            return
        raise ComponentError(
            "playwright_component_missing",
            "Playwright 组件未安装，电脑操控无法启动。"
            "请打开「设置 → 电脑操控」点击「下载组件」后重试。",
        )


# ---------------------------------------------------------------- 下载状态

_STATE_LOCK = threading.Lock()
_download_thread: "threading.Thread | None" = None
_cancel_event = threading.Event()

# 下载状态机（单例）：所有字段都在 _STATE_LOCK 内读写
_state = {
    "downloading": False,
    "stage": "",
    "percent": 0,
    "downloaded_bytes": 0,
    "total_bytes": 0,
    "error": "",
    # 本次/最近一次下载是否用到了真实 SHA256 期望值（False 表示未校验，见 warning）
    "digest_verified": False,
    # 非致命告警（例如 Release 说明里没有组件包校验行）：安装继续但必须可见
    "warning": "",
}


class _DownloadCancelled(Exception):
    """内部取消信号：由 cancel_download() 置位，工作线程据此尽快退出。"""


def _emit(on_event, stage: str, **overrides) -> None:
    """向调用方回调一条进度事件；回调自身抛错绝不影响下载线程。"""
    if on_event is None:
        return
    with _STATE_LOCK:
        payload = {
            "stage": stage,
            "percent": int(_state["percent"]),
            "downloaded_bytes": int(_state["downloaded_bytes"]),
            "total_bytes": int(_state["total_bytes"]),
            "error": str(_state["error"]),
        }
    payload.update(overrides)
    try:
        on_event(payload)
    except Exception:  # pragma: no cover - GUI 回调异常不得中断下载
        logger.warning("组件下载进度回调异常", exc_info=True)


def _update_state(**fields) -> None:
    """在锁内更新下载状态。"""
    with _STATE_LOCK:
        _state.update(fields)


def _check_cancel() -> None:
    """取消标志已置位时抛 ``_DownloadCancelled``。"""
    if _cancel_event.is_set():
        raise _DownloadCancelled()


def get_status() -> dict:
    """返回组件状态快照（供 GUI 设置页/CLI 轮询）。

    字段：``installed``（是否已安装）、``version``（版本号，未知为空串）、
    ``downloading``（是否正在下载）、``percent``（0-100 整数）、
    ``downloaded_bytes`` / ``total_bytes``（total 未知时为 0）、
    ``error``（最近一次失败的中文原因）、``dir``（组件安装目录）、
    ``stage``（最近阶段：downloading/extracting/done/error/空）、
    ``digest_verified``（本次是否用官方 SHA256 校验过）、
    ``warning``（非致命告警，例如 Release 说明缺组件包校验行）、
    ``required_version`` / ``version_compatible`` / ``outdated`` / ``version_warning``
    （组件版本与主程序要求 ``REQUIRED_PLAYWRIGHT_SPEC`` 的比对结论；版本未知时
    version_compatible 为 None、outdated 为 False、version_warning 为空串）。
    """
    installed = playwright_installed()
    version = playwright_version() if installed else ""
    # 版本比对（Minor 4）：只判断「能不能 import」不够——Release 升级主程序后沿用旧
    # 组件时，界面必须能看见「组件版本与当前程序要求不匹配」的中文提示。
    compatible = playwright_version_compatible(version) if installed else None
    with _STATE_LOCK:
        snapshot = dict(_state)
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
        # 版本要求与比对结论：required_version 是主程序约束（如 ">=1.55,<2"）；
        # version_compatible 取 True/False/None（None=版本未知，不做判断）；
        # outdated 供前端一眼判断，version_warning 是中文提示（空串=无问题）。
        "required_version": REQUIRED_PLAYWRIGHT_SPEC,
        "version_compatible": compatible,
        "outdated": compatible is False,
        "version_warning": _version_warning(version),
        "dir": _playwright_dir(),
        "stage": str(snapshot["stage"]),
    }


# ---------------------------------------------------------------- 下载源

def _iter_release_assets(release: dict):
    """遍历 Release 返回结构里的「资产」条目。

    核实结论（core/updater.py::fetch_latest_release）：该函数**不返回** ``assets``
    列表，而是把挑中的附件摊平为顶层字段 ``asset_name`` / ``download_url`` /
    ``asset_size``（见该函数 return 语句）。为兼容未来版本可能补回 ``assets``
    列表，这里先尝试列表形态（Gitee 原始字段名 ``browser_download_url``），
    再退化为顶层摊平字段。
    """
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
    """直接拉取 Gitee Releases 列表，返回最新非预发布条目的完整原始条目。

    为什么不用 core.updater.fetch_latest_release：那个函数只把「挑中的主安装包」
    摊平成顶层字段返回（asset_name / download_url / asset_size），同一个 Release
    下的其它附件（如 playwright 组件包）与发布说明（组件包 SHA256 就写在这里）
    根本拿不到。组件包必须自己取完整条目。
    网络失败或结构异常一律返回空 dict，由调用方给出统一的"未找到下载源"错误。
    """
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
    except Exception as error:  # 网络/解析失败按"无下载源"处理，不抛给 UI
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


def _fetch_release_assets() -> list:
    """返回最新非预发布 Release 的完整 assets 列表（网络/结构异常时为空列表）。

    组件包与主安装包挂在同一个 Release 下：fetch_latest_release 只挑主安装包并
    摊平字段，拿不到其它附件，所以必须自己取全量 assets。
    """
    assets = _fetch_latest_release_entry().get("assets")
    return assets if isinstance(assets, list) else []


def _parse_component_digest(release_notes: str, asset_name: str = "") -> str:
    """从 Release 说明里解析组件包 SHA256（小写归一）；没有可用的行时返回空串。

    校验行由 build_release.py::step4c_make_release_parts 生成，固定格式::

        playwright-component-1.51.0.zip  SHA256=<64 位十六进制>

    多条命中时优先取资产名完全一致的那条；只有一条时按「发版改了文件名」容错认它；
    多条又都对不上名字时不瞎猜（返回空串，由调用层告警）。
    """
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


def _resolve_component_source(url: str = "") -> tuple:
    """解析组件包下载源，返回 ``(下载地址, 期望 SHA256)``。

    地址优先级：显式参数 url > 环境变量 ``YCODE_PLAYWRIGHT_COMPONENT_URL`` >
    Gitee Release 附件（名字含 ``playwright-component`` 且以 .zip 结尾）。
    期望值取自同一 Release 发布说明里该资产的校验行（``_parse_component_digest``）。

    显式 url / 环境变量 url 属人工指定通道，不查发布说明，期望值返回空串
    （由 ``_download_worker`` 告警：这条通道没有官方 digest 可比对）。
    都找不到下载源时抛 ``ComponentError('playwright_component_download_failed', ...)``。
    """
    explicit = (url or "").strip()
    if explicit:
        return explicit, ""
    env_url = (os.environ.get("YCODE_PLAYWRIGHT_COMPONENT_URL") or "").strip()
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
        if _COMPONENT_ASSET_HINT not in lowered or not lowered.endswith(".zip"):
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
        if _COMPONENT_ASSET_HINT not in lowered or not lowered.endswith(".zip"):
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
        "playwright_component_download_failed",
        "未找到组件下载源：Release 里没有 playwright-component 压缩包，"
        "请检查网络或稍后重试" + hint,
    )


def _resolve_component_url(url: str = "") -> str:
    """解析组件压缩包下载地址（只要地址；要官方 digest 用 ``_resolve_component_source``）。

    优先级：显式参数 url > 环境变量 ``YCODE_PLAYWRIGHT_COMPONENT_URL`` >
    Gitee Release 附件（名字含 ``playwright-component`` 且以 .zip 结尾）。
    都找不到时抛 ``ComponentError('playwright_component_download_failed', ...)``。
    """
    return _resolve_component_source(url)[0]


# ---------------------------------------------------------------- 下载与安装

def _sha256_file(path: str) -> str:
    """分块计算文件 SHA256（组件包较大，不一次性读入内存）。"""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(_CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _download_archive(archive_url: str, tmp_path: str, on_event) -> None:
    """流式分块下载组件压缩包到 tmp_path，并按块推送 ``downloading`` 事件。"""
    request = urllib.request.Request(
        archive_url,
        headers={"User-Agent": _USER_AGENT, "Accept": "*/*"},
    )
    try:
        response = urllib.request.urlopen(request, timeout=_READ_TIMEOUT)
    except Exception as error:
        raise ComponentError(
            "playwright_component_download_failed",
            f"组件包下载失败：{type(error).__name__}: {error}",
        ) from error
    with response:
        try:
            total = int(response.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            total = 0
        downloaded = 0
        _update_state(
            downloaded_bytes=0, total_bytes=total, percent=0, error=""
        )
        _emit(on_event, "downloading", percent=0, downloaded_bytes=0, total_bytes=total)
        try:
            with open(tmp_path, "wb") as handle:
                while True:
                    _check_cancel()
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
                    _update_state(downloaded_bytes=downloaded, percent=percent)
                    _emit(
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
                "playwright_component_download_failed",
                f"组件包下载中断：{type(error).__name__}: {error}",
            ) from error
        if total > 0 and downloaded < total:
            raise ComponentError(
                "playwright_component_download_failed",
                f"组件包下载不完整（{downloaded}/{total} 字节），请重试",
            )


def _verify_sha256(archive_path: str, expected_sha256: str) -> None:
    """校验组件包 SHA256；不匹配时抛 ``ComponentError('playwright_component_corrupt')``。"""
    expected = (expected_sha256 or "").strip().lower()
    if not expected:
        return
    actual = _sha256_file(archive_path)
    if actual != expected:
        raise ComponentError(
            "playwright_component_corrupt",
            "组件包校验失败（SHA256 不匹配），文件可能已损坏或被篡改，请重新下载",
        )


def _is_unsafe_member_name(name: str) -> bool:
    """判断 zip 成员名是否不安全（绝对路径、盘符、``..`` 片段、空名）。"""
    if not name:
        return True
    unified = name.replace("\\", "/")
    if unified.startswith("/"):
        return True
    if os.path.isabs(name):
        return True
    first = unified.split("/", 1)[0]
    # 形如 "C:" 的盘符前缀
    if len(first) == 2 and first[1] == ":" and first[0].isalpha():
        return True
    return True if ".." in unified.split("/") else False


def _extract_zip(archive_path: str, dest_dir: str) -> None:
    """把组件压缩包安全解压到 dest_dir（防目录穿越）。

    对每个成员三重校验：拒绝绝对路径/盘符前缀、拒绝含 ``..`` 的片段、
    解压目标 realpath 必须仍在 dest_dir 内；命中任一条件即抛
    ``ComponentError('playwright_component_corrupt')`` 并停止解压。
    不使用 ``ZipFile.extract``：显式校验 + 自行写文件，行为可测试、可审计。
    """
    base = os.path.realpath(dest_dir)
    os.makedirs(base, exist_ok=True)
    try:
        archive = zipfile.ZipFile(archive_path)
    except Exception as error:
        raise ComponentError(
            "playwright_component_corrupt",
            f"组件包无法打开（可能不是有效的 zip）：{type(error).__name__}",
        ) from error
    with archive:
        for member in archive.infolist():
            name = member.filename
            if _is_unsafe_member_name(name):
                raise ComponentError(
                    "playwright_component_corrupt",
                    f"组件包含不安全的路径（{name}），已拒绝解压",
                )
            unified = name.replace("\\", "/")
            parts = [part for part in unified.split("/") if part not in ("", ".")]
            if not parts:
                continue
            target = os.path.realpath(os.path.join(base, *parts))
            if target != base and not target.startswith(base + os.sep):
                raise ComponentError(
                    "playwright_component_corrupt",
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
                    "playwright_component_corrupt",
                    f"组件包解压失败（{name}）：{type(error).__name__}: {error}",
                ) from error


def _locate_component_root(staging_dir: str) -> str:
    """在解压目录中定位真正的组件根（含 COMPONENT.json 与 playwright/driver/node.exe）。

    正常包把 ``playwright/`` 与 ``COMPONENT.json`` 放在压缩包根目录；
    兼容外层多套一层目录（如 ``playwright-component/``）的打包方式。
    """
    node_relative = _REQUIRED_NODE_DRIVER
    candidates = [staging_dir]
    try:
        for entry in sorted(os.listdir(staging_dir)):
            full = os.path.join(staging_dir, entry)
            if os.path.isdir(full):
                candidates.append(full)
    except OSError:
        pass
    for candidate in candidates:
        if os.path.isfile(os.path.join(candidate, _COMPONENT_MANIFEST)) and os.path.isfile(
            os.path.join(candidate, node_relative)
        ):
            return candidate
    raise ComponentError(
        "playwright_component_corrupt",
        "组件包结构不完整（缺少 COMPONENT.json 或 playwright/driver/node.exe），"
        "请重新下载",
    )


def _install_from_staging(staging_root: str, target_dir: str) -> None:
    """把解压好的组件目录原子安装到 target_dir。

    target_dir 已存在时先改名为 ``.bak-<时间戳>`` 再替换；替换失败立即回滚。
    替换成功后删除备份目录；删除失败只告警（不破坏已生效的新组件）。
    """
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


def _download_worker(on_event, expected_sha256: str, url: str) -> None:
    """下载线程主体：解析下载源与官方 digest → 下载 → 校验 → 解压 → 校验结构 → 原子安装。"""
    root = _component_root()
    playwright_dir = _playwright_dir()
    archive_path = os.path.join(root, _COMPONENT_ARCHIVE_NAME)
    tmp_archive = archive_path + ".tmp"
    staging_dir = os.path.join(root, f"{_EXTRACT_DIR_PREFIX}{os.getpid()}")
    try:
        archive_url, release_sha256 = _resolve_component_source(url)
        expected = (expected_sha256 or "").strip() or release_sha256
        if not expected:
            # 拿不到官方 digest 时不能静默放行：日志 + 进度事件 + 状态三处都要可见
            warning = (
                "未能从 Release 说明获取组件包 SHA256，本次安装未做完整性校验"
                "（存在被篡改风险），请提醒发版方补上组件包校验行"
            )
            logger.warning("%s（下载地址：%s）", warning, archive_url)
            _update_state(warning=warning, digest_verified=False)
            _emit(on_event, "downloading", warning=warning)
        else:
            _update_state(warning="", digest_verified=True)
        os.makedirs(root, exist_ok=True)
        _cleanup_paths(tmp_archive, staging_dir)

        _download_archive(archive_url, tmp_archive, on_event)
        _check_cancel()

        _verify_sha256(tmp_archive, expected)
        _check_cancel()

        # 下载完成：临时文件转正，便于失败后人工复用/排查
        try:
            os.replace(tmp_archive, archive_path)
        except Exception:
            _cleanup_paths(tmp_archive)
            raise ComponentError(
                "playwright_component_download_failed", "组件包落盘失败，请重试"
            )

        _update_state(stage="extracting", percent=_EXTRACT_PERCENT)
        _emit(on_event, "extracting", percent=_EXTRACT_PERCENT)
        _cleanup_paths(staging_dir)
        _extract_zip(archive_path, staging_dir)
        _check_cancel()

        staging_root = _locate_component_root(staging_dir)
        _install_from_staging(staging_root, playwright_dir)

        # 安装已确认成功（新目录就位、旧目录已清掉）后才删下载下来的组件包：
        # 它约 25MB，留着只会长期占用用户目录。失败路径刻意保留它（见上方
        # 「临时文件转正」注释，供失败后人工复用/排查），所以删除动作只能放在
        # 这里；此时解压与 SHA256 校验都已结束，删掉的是没人再读的副本，
        # 不会碰到刚装好、正在被 import 的组件目录。
        _cleanup_paths(archive_path)

        _update_state(
            stage="done",
            percent=100,
            error="",
            downloading=False,
        )
        _emit(on_event, "done", percent=100, error="")
        logger.info("Playwright 组件安装完成: %s", playwright_dir)
    except _DownloadCancelled:
        _update_state(stage="", percent=0, error="", downloading=False)
        logger.info("Playwright 组件下载已取消")
    except ComponentError as error:
        _update_state(
            stage="error",
            error=str(error),
            downloading=False,
        )
        _emit(on_event, "error", error=str(error))
        logger.warning("Playwright 组件安装失败[%s]: %s", error.code, error)
    except Exception as error:  # 兜底：任何意外都转成 error 事件，绝不让线程静默死掉
        message = f"组件安装失败：{type(error).__name__}: {error}"
        _update_state(stage="error", error=message, downloading=False)
        _emit(on_event, "error", error=message)
        logger.exception("Playwright 组件安装异常")
    finally:
        _cleanup_paths(tmp_archive, staging_dir)
        with _STATE_LOCK:
            _state["downloading"] = False


def start_download(on_event=None, expected_sha256: str = "", url: str = "") -> bool:
    """后台下载并安装 Playwright 组件；已在下载中返回 False。

    参数：
        on_event: 进度回调，收到 dict 事件，``stage`` 为
            ``downloading`` / ``extracting`` / ``done`` / ``error``，
            并携带 ``percent``（int）、``downloaded_bytes``、``total_bytes``、
            ``error`` 字段。回调异常会被吞掉并告警，不影响下载。
        expected_sha256: 期望的包校验和（64 位十六进制），优先级高于 Release 说明里的
            组件包校验行；不匹配即抛 ``playwright_component_corrupt`` 并拒绝安装。
            留空时下载线程会从 Release 说明里该资产的 ``SHA256=`` 行自动取
            （见 ``_parse_component_digest``）；两条来源都拿不到时仍安装，但会写
            WARNING 日志并在进度事件/状态里带 ``warning``，绝不静默放行。
        url: 显式下载地址；留空则按 ``_resolve_component_url`` 顺序解析。

    单例：模块级状态 + 锁保证同一时刻只有一个下载线程；成功启动返回 True。
    """
    global _download_thread
    with _STATE_LOCK:
        if _state["downloading"]:
            return False
        _state.update(
            downloading=True,
            stage="downloading",
            percent=0,
            downloaded_bytes=0,
            total_bytes=0,
            error="",
            warning="",
            digest_verified=False,
        )
    _cancel_event.clear()
    thread = threading.Thread(
        target=_download_worker,
        args=(on_event, expected_sha256, url),
        name="ycode-playwright-component-download",
        daemon=True,
    )
    _download_thread = thread
    try:
        thread.start()
    except Exception as error:  # pragma: no cover - 线程创建失败属极端情况
        with _STATE_LOCK:
            _state.update(
                downloading=False,
                stage="error",
                error=f"无法启动下载线程：{type(error).__name__}",
            )
        return False
    return True


def cancel_download() -> None:
    """请求取消当前下载：置取消标志，工作线程在下一块下载时退出并清理临时文件。"""
    _cancel_event.set()
    with _STATE_LOCK:
        if _state["downloading"]:
            _state["error"] = ""
