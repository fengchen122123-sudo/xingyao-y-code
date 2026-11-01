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
        ffmpeg/                         <- 视频转换组件（背景图转换用，非 Python 包）
            ffmpeg.exe
        ffmpeg-component.zip        <- 下载的组件压缩包（安装成功即删除，失败时保留）
        search/                         <- 网页搜索组件（ddgs 及其依赖，纯 Python 包集合）
            ddgs/  primp/  lxml/  fake_useragent/   <- 四个顶层包目录都必须在
            COMPONENT.json              <- {"search_version": "9.x.x", ...}
        search-component.zip        <- 下载的组件压缩包（安装成功即删除，失败时保留）

依赖约束：本模块只依赖标准库 + ``config`` + 按需 import 的 ``core.updater``，
绝不 import 任何 GUI 模块——CLI（ycode.py / cmd_*）必须能直接使用。

重构说明（组件注册表）：
    本模块已从「playwright 专用」泛化为「组件注册表 + 泛化实现 + playwright 薄包装」：
    ``ComponentSpec`` 描述单个组件的目录/资产/必检文件/版本约束，``_COMPONENTS`` 是
    注册表；所有流程函数都另有 ``_xxx_impl(spec, ...)`` 泛化版本，公开的
    ``get_status`` / ``start_download`` / ``cancel_download`` 只是 ``PLAYWRIGHT_SPEC``
    的薄包装，调用面与行为与重构前完全一致（playwright 行为零变化）。
    新增组件请走通用入口 ``get_component_status`` / ``start_component_download`` /
    ``cancel_component_download``；全局仍同一时刻只允许一个组件在下载。


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
from dataclasses import dataclass

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


# ---------------------------------------------------------------- 组件注册表
#
# 设计：本模块原来是「playwright 专用」的，所有目录名/资产名/必检文件/版本约束都
# 硬编码在流程里。现在把「一个按需组件的静态画像」抽成 ComponentSpec，流程函数一律
# 接收 spec 工作（``_xxx_impl(spec, ...)``），playwright 只保留同名薄包装委托给
# ``PLAYWRIGHT_SPEC``——这样 gui_bridge / browser_backend / 测试的调用面一个字都不用改，
# 后续新增组件只需往 _COMPONENTS 里加一条 spec，不必再复制整套下载/校验/安装流程。


@dataclass(frozen=True)
class ComponentSpec:
    """单个按需组件的静态描述（注册表条目，不可变）。

    字段含义：
        id: 组件唯一标识（对外 API 用它查注册表，也是错误码与目录名来源）
        display_name: 中文展示名（日志/文案用）
        install_dir: 组件根目录下的安装目录名（``<COMPONENT_ROOT>/<install_dir>``）
        archive_name: 下载到组件根目录的压缩包固定文件名
        extract_prefix: 解压暂存目录前缀
        asset_hint: Release 附件名需包含的片段（且以 .zip 结尾）
        required_files: 安装完整的必检文件（相对安装目录，POSIX 风格）
        required_dirs: 安装完整的必检目录（相对安装目录）
        version_spec: 主程序要求的版本约束（PEP 440 风格，逗号分隔）
        manifest_version_key: COMPONENT.json 里记录版本号的键名
        version_probe_file: 回退读取版本号的源文件（相对安装目录）
        import_name: 运行期需要 import 的包名
        env_url_var: 覆盖下载地址的环境变量名
        error_prefix: 错误码前缀（``<prefix>_missing`` / ``_download_failed`` / ``_corrupt``）
        settings_hint: 中文设置页路径提示（出现在错误/告警文案里）
        size_hint_mb: 组件包大小量级（供界面提示，不做逻辑判断）
        version_label: 版本告警文案里的组件名（如 "Playwright"）
    """

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

# 视频转换组件（ffmpeg）：第二个接入注册表的组件，用来验证「新组件只加一条 spec、
# 复用泛化流程」的设计成立——本文件除这条 spec 与 component_dir() 外没有任何
# ffmpeg 专用分支，下载/校验/解压/原子安装全部走 _xxx_impl。
#
# 与 playwright 的差异点（每一项都对应 spec 字段的「空值语义」，泛化实现必须容忍）：
#   required_files=("ffmpeg.exe",)  只检一个可执行文件，没有包目录
#   required_dirs=()                没有目录要检
#   version_spec=""                 主程序不对 ffmpeg 版本提要求（只用稳定命令行接口）
#   version_probe_file=""           ffmpeg.exe 不是 Python 模块，没有 __version__ 文件；
#                                   版本号只能来自 COMPONENT.json，_read_component_version
#                                   在 probe 为空时直接返回 manifest 值或空串，不抛异常
#   import_name=""                  不是 Python 包，下载后跳过 sys.path 导入保障
#   size_hint_mb=97                 仅供参考的包体积量级，逻辑上不做判断
FFMPEG_SPEC = ComponentSpec(
    id="ffmpeg",
    display_name="视频转换组件",
    install_dir="ffmpeg",
    archive_name="ffmpeg-component.zip",
    extract_prefix=".ffmpeg-extract-",
    asset_hint="ffmpeg-component",
    required_files=("ffmpeg.exe",),
    required_dirs=(),
    version_spec="",                  # 不做版本约束
    manifest_version_key="ffmpeg_version",
    version_probe_file="",            # ffmpeg.exe 没有 __version__ 文件
    import_name="",                   # 不是 Python 包，跳过 import 保障
    env_url_var="YCODE_FFMPEG_COMPONENT_URL",
    error_prefix="ffmpeg_component",
    settings_hint="设置 → 背景图转换",
    size_hint_mb=97,
    version_label="ffmpeg",
)

# 网页搜索组件（ddgs）：第三个接入注册表的组件，也是第一个「纯 Python 包集合」型组件。
#
# 为什么需要它：ddgs 运行时依赖 primp（Rust 扩展 .pyd）、lxml（C 扩展 .pyd）与
# fake_useragent，体积与平台绑定性都不适合塞进主安装包；把它们整体做成组件后，
# 用户不用搜索就完全不下载。
#
# 与 ffmpeg 的关键差异（同样落在「空值/集合语义」上，泛化实现必须容忍）：
#   required_files=()              组件里没有单个标志性文件可检，只能按目录判定
#   required_dirs=(ddgs, primp, lxml, fake_useragent)
#                                  四个顶层包目录都在才算装好；_is_component_installed
#                                  在 required_files 为空时纯按 required_dirs 判定
#   version_spec=""                主程序不对 ddgs 版本提要求（只用 news()/text() 接口）
#   version_probe_file=""          包版本只能来自 COMPONENT.json 的 search_version；
#                                  没有 __version__ 探测文件，_read_component_version
#                                  直接返回 manifest 值或空串
#   import_name="ddgs"             装好后需要插 sys.path 才能 import（见 ensure_search_component）
SEARCH_SPEC = ComponentSpec(
    id="search",
    display_name="网页搜索组件",
    install_dir="search",
    archive_name="search-component.zip",
    extract_prefix=".search-extract-",
    asset_hint="search-component",
    required_dirs=("ddgs", "primp", "lxml", "fake_useragent"),
    required_files=(),
    version_spec="",                  # 不做版本约束
    manifest_version_key="search_version",
    version_probe_file="",            # 没有 __version__ 探测文件
    import_name="ddgs",
    env_url_var="YCODE_SEARCH_COMPONENT_URL",
    error_prefix="search_component",
    settings_hint="设置 → 网页搜索",
    size_hint_mb=18,
    version_label="search",
)

# 组件注册表（id -> spec）。新增组件在此登记即可，无需改流程代码。
_COMPONENTS: "dict[str, ComponentSpec]" = {
    PLAYWRIGHT_SPEC.id: PLAYWRIGHT_SPEC,
    FFMPEG_SPEC.id: FFMPEG_SPEC,
    SEARCH_SPEC.id: SEARCH_SPEC,
}


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


def get_component_spec(component_id: str) -> ComponentSpec:
    """按 id 从组件注册表取 spec；未知 id 抛 ``ComponentError``。

    错误码形如 ``<id>_component_unknown``，交给调用层原样展示即可。
    """
    wanted = str(component_id or "").strip()
    spec = _COMPONENTS.get(wanted)
    if spec is None:
        raise ComponentError(f"{component_id}_component_unknown", "未知组件")
    return spec


# ---------------------------------------------------------------- 路径解析

def _component_root() -> str:
    """返回组件根目录（环境变量 YCODE_COMPONENT_DIR > 模块常量 COMPONENT_ROOT）。"""
    override = (os.environ.get("YCODE_COMPONENT_DIR") or "").strip()
    if override:
        return os.path.abspath(override)
    return os.path.abspath(COMPONENT_ROOT)


def _component_dir(spec: ComponentSpec) -> str:
    """返回某个组件的安装目录。

    环境变量 ``YCODE_COMPONENT_DIR`` 覆盖组件根目录时自动跟随
    （``<override>/<spec.install_dir>``）；否则用模块级常量——playwright 用
    ``PLAYWRIGHT_DIR``（测试会 monkeypatch 该常量，因此必须**调用时**从模块
    全局读取，绝不能在 import 期缓存）。
    """
    override = (os.environ.get("YCODE_COMPONENT_DIR") or "").strip()
    if override:
        return os.path.join(os.path.abspath(override), spec.install_dir)
    if spec is PLAYWRIGHT_SPEC:
        return os.path.abspath(globals()["PLAYWRIGHT_DIR"])
    return os.path.abspath(os.path.join(_component_root(), spec.install_dir))


def _playwright_dir() -> str:
    """返回 Playwright 组件安装目录（``_component_dir`` 的薄包装）。"""
    return _component_dir(PLAYWRIGHT_SPEC)


def component_dir(component_id: str) -> str:
    """返回指定组件的安装目录绝对路径（对外公开，按组件 id 查询）。

    为什么需要这个公开出口：gui_bridge._find_ffmpeg 要把「组件安装目录下的
    ffmpeg.exe」加进候选链。若在那边自己拼 ``~/.ycode/components/<install_dir>``，
    ``YCODE_COMPONENT_DIR`` 覆盖规则与 install_dir 约定就会出现两处真相，日后改
    目录名必然漏改一处；这里统一出口，调用方只认组件 id。
    未知 id 抛 ``ComponentError("<id>_component_unknown")``。
    """
    return _component_dir(get_component_spec(component_id))


# ---------------------------------------------------------------- 安装检测

def _is_component_installed(spec: ComponentSpec) -> bool:
    """检查组件是否已完整安装到其安装目录。

    判定条件（全部满足才算已安装）：``spec.required_dirs`` 里的目录都存在、
    ``spec.required_files`` 里的文件也都存在。playwright 即「``playwright/`` 包目录 +
    ``playwright/driver/node.exe`` + 运行期依赖 ``greenlet/`` 与 ``pyee/``」；
    ffmpeg 只有 ``required_files=("ffmpeg.exe",)``、没有目录；网页搜索组件反过来
    只有 ``required_dirs``（四个顶层包目录）、没有单个标志文件。
    只做文件系统探测，绝不 import，保证可被 CLI 低成本调用。
    """
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
    """检查 Playwright 组件是否已完整安装（``_is_component_installed`` 薄包装）。"""
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
    """生成「组件版本与主程序要求不匹配」的中文提示；匹配或无法判断时返回空串。

    文案里的组件名/版本约束/设置页路径都取自 spec，playwright 的产出与重构前逐字一致
    （测试断言 ``1.51.0``、约束串与「重新下载」都在这句里）。
    """
    if _version_compatible(spec, version) is not False:
        return ""
    label = spec.version_label or spec.display_name
    return (
        f"组件版本 {version} 与当前程序要求的 {label} "
        f"{spec.version_spec} 不匹配，"
        f"建议在「{spec.settings_hint}」重新下载组件"
    )


def _version_warning(version: str) -> str:
    """Playwright 版版本不匹配提示（``_version_warning_impl`` 薄包装）。"""
    return _version_warning_impl(PLAYWRIGHT_SPEC, version)


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


# 网页搜索组件的导入缓存。刻意用独立布尔而**不复用** _import_ready：
# 那是 playwright 的专属缓存，两个组件是互相独立的包——「playwright 已就绪」绝不
# 代表 ddgs 已就绪。共用一个布尔会导致两种错误：要么跳过 sys.path 回退（组件装在
# 用户目录时 import 永远失败），要么在 ddgs 缺失时被当成已就绪而静默放过。
_search_import_ready = False


def _try_import_ddgs() -> bool:
    """尝试 ``import ddgs``，成功返回 True，失败（含任何导入异常）返回 False。

    注意不能只捕 ModuleNotFoundError：ddgs 依赖 primp（Rust 扩展）与 lxml（C 扩展）
    及 fake_useragent，组件目录缺了其中任何一个二进制扩展时，``import ddgs`` 抛的是
    ImportError/OSError 而不是「模块不存在」，同样应按「不可用」处理。
    """
    try:
        import ddgs  # noqa: F401  # 只验证包可导入，不发起任何网络请求
    except Exception:
        return False
    return True


def ensure_search_component() -> None:
    """确保网页搜索依赖（ddgs）可被导入，否则抛 ``ComponentError``。

    顺序与 ``ensure_playwright`` 保持一致：
      1. 已能 ``import ddgs``（开发环境装在 site-packages / 打包版已加过 path）→ 返回；
      2. 否则把 ``component_dir("search")`` 插到 ``sys.path`` 首位（已有则跳过，
         绝不重复插入），``importlib.invalidate_caches()`` 后重试一次；
      3. 仍失败抛 ``ComponentError('search_component_missing', ...)``，提示去
         「设置 → 网页搜索」点下载。

    导入成功后置模块级缓存，后续调用零开销。调用方（core.tools.search_news）
    负责把该异常转成给模型看的可读提示，不让它打断整个工具调用。
    """
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
            # 组件目录可能刚被安装到磁盘，清缓存保证新目录里的包能被发现
            importlib.invalidate_caches()
        if _try_import_ddgs():
            _search_import_ready = True
            return
        raise ComponentError(
            "search_component_missing",
            "网页搜索组件未安装，请到「设置 → 网页搜索」下载",
        )


# ---------------------------------------------------------------- 下载状态

_STATE_LOCK = threading.Lock()
# Playwright 的下载线程句柄（历史符号，测试会 monkeypatch，不能改名/删除）
_download_thread: "threading.Thread | None" = None
_cancel_event = threading.Event()


def _new_state() -> dict:
    """新建一个组件的下载状态字典（字段与 playwright 的历史 _state 完全一致）。"""
    return {
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


# Playwright 的下载状态机（单例）：所有字段都在 _STATE_LOCK 内读写。
_state = _new_state()

# 非 playwright 组件的状态字典（按 spec.id 懒创建）
_states: dict[str, dict] = {}
# 当前正在下载的组件 id（None=全局空闲）；全局只允许一个组件在下载
_active_component_id: "str | None" = None
# 非 playwright 组件的下载线程句柄与取消信号
_component_threads: dict[str, threading.Thread] = {}
_component_cancel_events: dict[str, threading.Event] = {}


class _DownloadCancelled(Exception):
    """内部取消信号：由 cancel_download() 置位，工作线程据此尽快退出。"""


def _state_for(spec: ComponentSpec) -> dict:
    """返回某个组件的下载状态字典。

    playwright 用模块级 ``_state``，且**必须动态从模块全局读取**：测试会
    ``monkeypatch.setattr(components, "_state", {...})`` 整体替换该属性，若在 import
    期（或默认参数/闭包里）缓存了旧 dict，测试会拿到过期对象而失败。
    其它组件用 ``_states[spec.id]``（懒创建）。
    """
    if spec is PLAYWRIGHT_SPEC:
        return globals()["_state"]
    return _states.setdefault(spec.id, _new_state())


def _cancel_event_for(spec: ComponentSpec) -> threading.Event:
    """返回某个组件的取消信号（playwright 用模块级 ``_cancel_event``）。"""
    if spec is PLAYWRIGHT_SPEC:
        return globals()["_cancel_event"]
    return _component_cancel_events.setdefault(spec.id, threading.Event())


def _download_thread_for(spec: ComponentSpec) -> "threading.Thread | None":
    """返回某个组件的下载线程句柄（playwright 用模块级 ``_download_thread``）。"""
    if spec is PLAYWRIGHT_SPEC:
        return globals()["_download_thread"]
    return _component_threads.get(spec.id)


def _set_download_thread(spec: ComponentSpec, thread) -> None:
    """记录下载线程句柄（playwright 写回模块全局，测试直接读该符号）。"""
    if spec is PLAYWRIGHT_SPEC:
        globals()["_download_thread"] = thread
    else:
        _component_threads[spec.id] = thread


def _begin_active(spec: ComponentSpec) -> bool:
    """尝试占用全局下载权；已有组件在下载时返回 False（单例语义）。"""
    global _active_component_id
    if _active_component_id is not None:
        return False
    _active_component_id = spec.id
    return True


def _end_active(spec: ComponentSpec) -> None:
    """释放全局下载权（只清理自己占用的那一个）。"""
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
    """Playwright 版进度事件（``_emit_impl`` 薄包装）。"""
    _emit_impl(PLAYWRIGHT_SPEC, on_event, stage, **overrides)


def _update_state_impl(spec: ComponentSpec, **fields) -> None:
    """在锁内更新某个组件的下载状态。"""
    with _STATE_LOCK:
        _state_for(spec).update(fields)


def _update_state(**fields) -> None:
    """Playwright 版状态更新（``_update_state_impl`` 薄包装）。"""
    _update_state_impl(PLAYWRIGHT_SPEC, **fields)


def _check_cancel_impl(spec: ComponentSpec) -> None:
    """某个组件的取消标志已置位时抛 ``_DownloadCancelled``。"""
    if _cancel_event_for(spec).is_set():
        raise _DownloadCancelled()


def _check_cancel() -> None:
    """Playwright 版取消检查（``_check_cancel_impl`` 薄包装）。"""
    _check_cancel_impl(PLAYWRIGHT_SPEC)


def _get_status_impl(spec: ComponentSpec) -> dict:
    """返回组件状态快照（字段契约见公开的 ``get_status``）。"""
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
        # 版本要求与比对结论：required_version 是主程序约束（如 ">=1.55,<2"）；
        # version_compatible 取 True/False/None（None=版本未知，不做判断）；
        # outdated 供前端一眼判断，version_warning 是中文提示（空串=无问题）。
        "required_version": spec.version_spec,
        "version_compatible": compatible,
        "outdated": compatible is False,
        "version_warning": _version_warning_impl(spec, version),
        "dir": _component_dir(spec),
        "stage": str(snapshot["stage"]),
    }


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
    return _get_status_impl(PLAYWRIGHT_SPEC)


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


def _resolve_component_source_impl(spec: ComponentSpec, url: str = "") -> tuple:
    """解析组件包下载源，返回 ""(下载地址, 期望 SHA256)""。

    地址优先级：显式参数 url > 环境变量 ``spec.env_url_var`` >
    Gitee Release 附件（名字含 ``spec.asset_hint`` 且以 .zip 结尾）。
    期望值取自同一 Release 发布说明里该资产的校验行（``_parse_component_digest``）。

    显式 url / 环境变量 url 属人工指定通道，不查发布说明，期望值返回空串
    （由 ``_download_worker`` 告警：这条通道没有官方 digest 可比对）。
    都找不到下载源时抛 ``ComponentError("<error_prefix>_download_failed", ...)``。
    """
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
    """Playwright 版下载源解析（``_resolve_component_source_impl`` 薄包装）。"""
    return _resolve_component_source_impl(PLAYWRIGHT_SPEC, url)


def _resolve_component_url(url: str = "") -> str:
    """解析组件压缩包下载地址（只要地址；要官方 digest 用 ``_resolve_component_source``）。

    优先级：显式参数 url > 环境变量 ``spec.env_url_var`` >
    Gitee Release 附件（名字含 ``spec.asset_hint`` 且以 .zip 结尾）。
    都找不到时抛 ``ComponentError("<error_prefix>_download_failed", ...)``。
    """
    return _resolve_component_source_impl(PLAYWRIGHT_SPEC, url)[0]


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


def _download_archive_impl(spec: ComponentSpec, archive_url: str, tmp_path: str, on_event) -> None:
    """流式分块下载组件压缩包到 tmp_path，并按块推送 ``downloading`` 事件。"""
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
    """Playwright 版分块下载（``_download_archive_impl`` 薄包装）。"""
    _download_archive_impl(PLAYWRIGHT_SPEC, archive_url, tmp_path, on_event)


def _verify_sha256_impl(spec: ComponentSpec, archive_path: str, expected_sha256: str) -> None:
    """校验组件包 SHA256；不匹配时抛 ``ComponentError("<error_prefix>_corrupt")``。"""
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
    """Playwright 版 SHA256 校验（``_verify_sha256_impl`` 薄包装）。"""
    _verify_sha256_impl(PLAYWRIGHT_SPEC, archive_path, expected_sha256)


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


def _extract_zip_impl(spec: ComponentSpec, archive_path: str, dest_dir: str) -> None:
    """把组件压缩包安全解压到 dest_dir（防目录穿越）。

    对每个成员三重校验：拒绝绝对路径/盘符前缀、拒绝含 "".."" 的片段、
    解压目标 realpath 必须仍在 dest_dir 内；命中任一条件即抛
    ``ComponentError("<error_prefix>_corrupt")`` 并停止解压。
    不使用 ``ZipFile.extract``：显式校验 + 自行写文件，行为可测试、可审计。
    """
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
    """Playwright 版安全解压（``_extract_zip_impl`` 薄包装）。"""
    _extract_zip_impl(PLAYWRIGHT_SPEC, archive_path, dest_dir)


def _locate_component_root_impl(spec: ComponentSpec, staging_dir: str) -> str:
    """在解压目录中定位真正的组件根（含 COMPONENT.json 与必检文件/目录）。

    正常包把组件内容（playwright/ 与 COMPONENT.json）放在压缩包根目录；
    兼容外层多套一层目录（如 playwright-component/）的打包方式。

    结构探针的选取顺序（为什么不能只看 required_files[0]）：ffmpeg 与 playwright
    都有单个标志文件，但网页搜索组件是一个纯包集合（ddgs/primp/lxml/fake_useragent），
    required_files 为空——若仍按「第一个必检文件」去 isfile("") 判定，永远为假，
    安装会在最后一步稳定报「结构不完整」。所以 required_files 为空时退到
    「第一个必检目录」，两者都为空才只认 COMPONENT.json。
    """
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
    """Playwright 版组件根定位（``_locate_component_root_impl`` 薄包装）。"""
    return _locate_component_root_impl(PLAYWRIGHT_SPEC, staging_dir)


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

        # 下载完成：临时文件转正，便于失败后人工复用/排查
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

        # 安装已确认成功（新目录就位、旧目录已清掉）后才删下载下来的组件包：
        # 它约几十 MB，留着只会长期占用用户目录。失败路径刻意保留它（见上方
        # 「临时文件转正」注释，供失败后人工复用/排查），所以删除动作只能放在
        # 这里；此时解压与 SHA256 校验都已结束，删掉的是没人再读的副本，
        # 不会碰到刚装好、正在被 import 的组件目录。
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
    """Playwright 版下载线程主体（``_download_worker_impl`` 薄包装）。

    保留该符号：``start_download`` 与历史调用方按这个名字取线程目标。
    """
    _download_worker_impl(PLAYWRIGHT_SPEC, on_event, expected_sha256, url)


def _start_download_impl(
    spec: ComponentSpec, on_event=None, expected_sha256: str = "", url: str = ""
) -> bool:
    """后台下载并安装组件；已有组件在下载中返回 False（全局单例语义）。

    参数语义与公开的 ``start_download`` 完全一致。
    """
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


# ---------------------------------------------------------------- 通用组件入口
# 供后续组件（如 ffmpeg）复用的对外 API：按组件 id 查注册表，未知 id 抛
# ComponentError("<id>_component_unknown", "未知组件")。


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
