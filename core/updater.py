import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.version import __version__ as YCODE_VERSION

logger = logging.getLogger(__name__)

GITEE_REPO = "feng-chenhao/xingyao-y-code"
GITEE_API_URL = f"https://gitee.com/api/v5/repos/{GITEE_REPO}/releases/latest"
HTTP_USER_AGENT = f"ycode-updater/{YCODE_VERSION} (Windows)"

RETRY_DELAYS = (2, 5, 10)

# 版本号白名单：tag_name 来自远程 API，拼接进文件名与 cmd 前必须约束字符集
_SAFE_VERSION_RE = re.compile(r"^[0-9A-Za-z.\-_]+$")

# Release 描述中的官方 SHA256 标记行（约定：SHA256: <64 位十六进制>）。
# 分卷/单文件安装包下载完成后必须与之比对，不一致即拒绝安装
_SHA256_RE = re.compile(r"SHA256\s*[:：]\s*([0-9a-fA-F]{64})", re.IGNORECASE)

_PART_SUFFIX_RE = re.compile(r"\.part(\d+)$", re.IGNORECASE)

_INSTALLER_NAME_RE = re.compile(r"^Y-CODE-Setup-v(.+)\.exe$", re.IGNORECASE)

# 复用缓存安装包的最小体积兜底（字节）：Release 既无 SHA256 也无 size 时，
# 仅凭文件名无法识别半截下载/HTML 错误页，至少要像正常安装包（与下载器
# 拒绝 <1MB 分片的阈值保持一致）。
_MIN_INSTALLER_BYTES = 1024 * 1024


def is_safe_version_string(version: str) -> bool:
    """校验版本号仅含安全字符（数字/字母/点/横线/下划线），防路径穿越与 shell 注入。"""
    return bool(version) and bool(_SAFE_VERSION_RE.match(version))


def parse_release_sha256(body: str) -> str:
    """从 Release 描述解析官方 SHA256（小写归一）。未发布校验和时返回空串。"""
    m = _SHA256_RE.search(str(body or ""))
    return m.group(1).lower() if m else ""


def _collect_split_parts(assets: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    groups: Dict[str, Dict[int, Dict[str, Any]]] = {}
    for asset in assets or []:
        name = str(asset.get("name") or "")
        m = _PART_SUFFIX_RE.search(name)
        if not m:
            continue
        idx = int(m.group(1))
        if idx < 1:
            continue
        stem = name[: m.start()]
        groups.setdefault(stem, {})[idx] = asset
    complete: Dict[str, List[Dict[str, Any]]] = {}
    for stem, idx_map in groups.items():
        count = max(idx_map)
        if sorted(idx_map) == list(range(1, count + 1)):
            complete[stem] = [idx_map[i] for i in range(1, count + 1)]
        else:
            logger.warning("分卷 %s 序号不连续（1..%d 缺片），忽略该组", stem, count)
    return complete


def _pick_main_installer_group(
    complete: Dict[str, List[Dict[str, Any]]], version: str
) -> Optional[Tuple[str, List[Dict[str, Any]]]]:
    if not complete:
        return None
    for exact in (f"XingYaoCode-{version}.exe", f"Y-code-{version}.exe"):
        if exact in complete:
            return exact, complete[exact]
    for stem in complete:
        low = stem.lower()
        if (low.startswith("xingyaocode") or low.startswith("y-code")) and "cli" not in low:
            return stem, complete[stem]
    stem = next(iter(complete))
    return stem, complete[stem]


def _pick_main_installer_exe(
    assets: List[Dict[str, Any]], version: str
) -> Optional[Dict[str, Any]]:
    exe_assets = [
        a for a in (assets or [])
        if str(a.get("name") or "").lower().endswith(".exe")
    ]
    if not exe_assets:
        return None
    for exact in (f"XingYaoCode-{version}.exe", f"Y-code-{version}.exe"):
        for asset in exe_assets:
            if str(asset.get("name")) == exact:
                return asset
    for asset in exe_assets:
        low = str(asset.get("name") or "").lower()
        if (low.startswith("xingyaocode") or low.startswith("y-code")) and "cli" not in low:
            return asset
    return exe_assets[0]


def parse_version(v_str: str) -> Tuple[int, ...]:
    """将版本号字符串（如 'v1.2.6', '1.2.6-beta', '1.3'）解析为整型元组供比较。"""
    if not v_str:
        return (0, 0, 0)
    nums = re.findall(r"\d+", str(v_str))
    if not nums:
        return (0, 0, 0)
    return tuple(int(n) for n in nums)


def compare_versions(v1: str, v2: str) -> int:
    t1 = parse_version(v1)
    t2 = parse_version(v2)
    max_len = max(len(t1), len(t2), 3)
    t1_padded = t1 + (0,) * (max_len - len(t1))
    t2_padded = t2 + (0,) * (max_len - len(t2))
    if t1_padded > t2_padded:
        return 1
    elif t1_padded < t2_padded:
        return -1
    return 0


def get_update_cache_dir() -> str:
    """获取安装包临时存放目录：%TEMP%\\ycode_updates"""
    temp_root = tempfile.gettempdir()
    cache_dir = os.path.join(temp_root, "ycode_updates")
    os.makedirs(cache_dir, exist_ok=True)
    return cache_dir


def _remove_file_quietly(path: str) -> bool:
    """删除单个文件，失败只记 debug 日志（不抛出）。返回是否删除成功。"""
    try:
        os.remove(path)
        return True
    except Exception as e:
        logger.debug("删除文件 %s 失败: %s", path, e)
        return False


def cleanup_old_installers(current_version: str = "", keep_days: int = 7) -> int:
    cleaned = 0
    try:
        if not current_version:
            try:
                from core.version import __version__ as _current
                current_version = str(_current or "")
            except Exception as e:
                logger.debug("读取当前版本号失败，按无法比较处理: %s", e)
                current_version = ""

        can_compare = bool(current_version)

        cache_dir = get_update_cache_dir()
        if not os.path.isdir(cache_dir):
            return 0

        now = time.time()
        max_age_seconds = max(int(keep_days), 0) * 24 * 60 * 60

        for fname in os.listdir(cache_dir):
            fpath = os.path.join(cache_dir, fname)
            try:
                if os.path.isdir(fpath):
                    shutil.rmtree(fpath, ignore_errors=True)
                    cleaned += 1
                    continue
                if not os.path.isfile(fpath):
                    continue

                if fname.lower().endswith(".downloading"):
                    if _remove_file_quietly(fpath):
                        logger.info("清理下载残片: %s", fpath)
                        cleaned += 1
                    continue

                m = _INSTALLER_NAME_RE.match(fname)
                ver_str = m.group(1) if m else ""
                # 版本号里必须含数字才算解析成功（parse_version 对无数字串返回 (0,0,0)）
                if ver_str and re.search(r"\d", ver_str) and can_compare:
                    if compare_versions(ver_str, current_version) <= 0:
                        if _remove_file_quietly(fpath):
                            logger.info(
                                "清理已过时安装包（v%s <= 当前 v%s）: %s",
                                ver_str, current_version, fpath,
                            )
                            cleaned += 1
                    continue

                # 走到这里：命名非标准，或当前版本不可比较。
                # 无法比较时不删任何 exe（可能是用户还要装的包）
                if not can_compare and fname.lower().endswith(".exe"):
                    continue

                try:
                    mtime = os.path.getmtime(fpath)
                except OSError as e:
                    logger.debug("读取修改时间失败，跳过 %s: %s", fpath, e)
                    continue
                if max_age_seconds > 0 and now - mtime > max_age_seconds:
                    if _remove_file_quietly(fpath):
                        logger.info("清理超期残留文件（>%d 天）: %s", keep_days, fpath)
                        cleaned += 1
            except Exception as e:
                logger.debug("清理文件 %s 失败: %s", fpath, e)
    except Exception as e:
        logger.warning("扫描更新缓存目录失败: %s", e)
    return cleaned


def find_cached_installer(
    target_version: str,
    expected_sha256: str = "",
    expected_size: int = 0,
) -> str:
    try:
        if not target_version or not is_safe_version_string(target_version):
            logger.warning("非法目标版本号，跳过缓存查找: %r", target_version)
            return ""
        cache_dir = get_update_cache_dir()
        cached_path = os.path.join(cache_dir, f"Y-CODE-Setup-v{target_version}.exe")
        if not os.path.isfile(cached_path):
            return ""

        try:
            actual_size = os.path.getsize(cached_path)
        except OSError as e:
            logger.warning("读取缓存安装包大小失败，视为无效: %s - %s", cached_path, e)
            return ""

        if expected_size and int(expected_size) > 0 and actual_size != int(expected_size):
            logger.warning(
                "缓存安装包大小不符（期望 %d，实际 %d），删除并重新下载: %s",
                int(expected_size), actual_size, cached_path,
            )
            _remove_file_quietly(cached_path)
            return ""

        if actual_size < _MIN_INSTALLER_BYTES:
            logger.warning(
                "缓存安装包体积异常（%d 字节 < %d），视为未完成下载，删除并重新下载: %s",
                actual_size, _MIN_INSTALLER_BYTES, cached_path,
            )
            _remove_file_quietly(cached_path)
            return ""

        try:
            with open(cached_path, "rb") as f:
                magic = f.read(2)
        except OSError as e:
            logger.warning("读取缓存安装包文件头失败，视为无效: %s - %s", cached_path, e)
            return ""
        if magic != b"MZ":
            logger.warning(
                "缓存安装包不是有效的 Windows 可执行文件（MZ 头缺失），删除并重新下载: %s",
                cached_path,
            )
            _remove_file_quietly(cached_path)
            return ""

        if expected_sha256:
            try:
                actual_hash = _sha256_file(cached_path)
            except Exception as e:
                logger.warning("计算缓存安装包 SHA256 失败，视为无效: %s - %s", cached_path, e)
                return ""
            if actual_hash.lower() != str(expected_sha256).strip().lower():
                logger.warning(
                    "缓存安装包 SHA256 校验失败（期望 %s，实际 %s），删除并重新下载: %s",
                    str(expected_sha256).strip().lower(), actual_hash, cached_path,
                )
                _remove_file_quietly(cached_path)
                return ""

        return cached_path
    except Exception as e:
        logger.warning("查找缓存安装包失败: %s", e)
        return ""


def fetch_latest_release(
    repo: str = GITEE_REPO, timeout: float = 8.0
) -> Dict[str, Any]:
    url = f"https://gitee.com/api/v5/repos/{repo}/releases?per_page=5&direction=desc"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": HTTP_USER_AGENT,
            "Accept": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {
                "ok": True,
                "has_update": False,
                "latest_version": YCODE_VERSION,
                "current_version": YCODE_VERSION,
                "msg": "当前已是最新版本（暂无更新发布）",
            }
        logger.warning("请求 Gitee Releases HTTP %s: %s", e.code, e.reason)
        return {
            "ok": False,
            "has_update": False,
            "latest_version": YCODE_VERSION,
            "current_version": YCODE_VERSION,
            "msg": f"检查更新失败 (HTTP {e.code})",
        }
    except Exception as e:
        logger.warning("请求 Gitee Releases 异常: %s", e)
        return {
            "ok": False,
            "has_update": False,
            "latest_version": YCODE_VERSION,
            "current_version": YCODE_VERSION,
            "msg": f"网络连接超时或失败: {e}",
        }

    if not isinstance(data, list):
        logger.warning("Gitee Releases 列表返回非数组: %s", type(data).__name__)
        return {
            "ok": False,
            "has_update": False,
            "latest_version": YCODE_VERSION,
            "current_version": YCODE_VERSION,
            "msg": "检查更新失败: 响应格式异常",
        }
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
    if entry is None:
        return {
            "ok": True,
            "has_update": False,
            "latest_version": YCODE_VERSION,
            "current_version": YCODE_VERSION,
            "msg": "当前已是最新版本（暂无更新发布）",
        }
    data = entry

    raw_tag = data.get("tag_name") or ""
    latest_version = raw_tag.lstrip("vV").strip() or raw_tag
    release_title = data.get("name") or f"星瑶Code v{latest_version}"
    release_notes = data.get("body") or "暂无更新说明。"
    official_sha256 = parse_release_sha256(release_notes)

    assets = data.get("assets") or []
    download_url = ""
    asset_name = ""
    asset_size = 0
    part_urls: List[str] = []
    part_count = 0

    # 优先识别分卷序列（主安装包 > 100MB 时按 .partN 分卷发布）。
    # 同一 Release 可能同时挂主安装包与 CLI 安装包两组分卷，必须按
    # 命名规则挑主安装包那组，否则自动更新可能错下 CLI 安装包
    picked = _pick_main_installer_group(_collect_split_parts(assets), latest_version)
    if picked:
        parts_base, part_assets = picked
        part_urls = [str(a.get("browser_download_url") or "") for a in part_assets]
        part_count = len(part_assets)
        asset_name = parts_base
        asset_size = sum(int(a.get("size") or 0) for a in part_assets)
        download_url = f"https://gitee.com/{repo}/releases/tag/{raw_tag}"
        logger.info(
            "识别到分卷安装包: %s 共 %d 片 (%d 字节)", parts_base, part_count, asset_size
        )
    else:
        # 回退单 exe 附件：同 Release 多个 exe 时优先主安装包（非 CLI）
        picked_exe = _pick_main_installer_exe(assets, latest_version)
        if picked_exe:
            download_url = picked_exe.get("browser_download_url", "")
            asset_name = picked_exe.get("name", "")
            asset_size = int(picked_exe.get("size") or 0)

    # 若未找到 exe 附件，回退到 Release 页面直链
    if not download_url:
        download_url = f"https://gitee.com/{repo}/releases/tag/{raw_tag}"

    has_update = compare_versions(latest_version, YCODE_VERSION) > 0

    return {
        "ok": True,
        "has_update": has_update,
        "latest_version": latest_version,
        "current_version": YCODE_VERSION,
        "release_title": release_title,
        "release_notes": release_notes,
        "download_url": download_url,
        "asset_name": asset_name,
        "asset_size": asset_size,
        "part_urls": part_urls,
        "part_count": part_count,
        "is_split": bool(part_urls),
        "sha256": official_sha256,
    }


def _sha256_file(path: str) -> str:
    """分块计算文件 SHA256（安装包几百 MB，不能一次读入内存）。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(4 * 1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


class UpdateDownloader:

    def __init__(self):
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._cancel_event = threading.Event()
        self._is_downloading = False
        self._current_dest_path: Optional[str] = None
        self._current_tmp_path: Optional[str] = None

    @property
    def is_downloading(self) -> bool:
        with self._lock:
            return self._is_downloading

    def start_download(
        self,
        download_url: str,
        target_version: str,
        on_progress: Callable[[Dict[str, Any]], None],
        on_complete: Callable[[str], None],
        on_error: Callable[[str], None],
        part_urls: Optional[List[str]] = None,
        expected_sha256: str = "",
        total_size_hint: int = 0,
    ) -> bool:
        # 版本号白名单校验：target_version 会拼进文件名与 shell 命令，必须先约束
        if not is_safe_version_string(target_version):
            logger.error("非法版本号，拒绝下载: %r", target_version)
            try:
                on_error(f"非法版本号: {target_version!r}")
            except Exception:
                pass
            return False

        clean_parts: List[str] = []
        for u in (part_urls or []):
            u = str(u or "").strip()
            if u.lower().startswith(("http://", "https://")):
                clean_parts.append(u)
        if len(clean_parts) == 1:
            download_url = clean_parts[0]
            clean_parts = []

        if not clean_parts and not str(download_url or "").strip():
            logger.error("下载地址为空，拒绝启动")
            try:
                on_error("下载地址为空")
            except Exception:
                pass
            return False

        with self._lock:
            if self._is_downloading:
                return False
            self._is_downloading = True
            self._cancel_event.clear()

        cache_dir = get_update_cache_dir()
        filename = f"Y-CODE-Setup-v{target_version}.exe"
        dest_path = os.path.join(cache_dir, filename)
        tmp_path = os.path.join(cache_dir, f"{filename}.downloading")
        self._current_dest_path = dest_path
        self._current_tmp_path = tmp_path

        def _cleanup_tmp():
            try:
                if os.path.isfile(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass

        def _worker():
            try:
                urls = clean_parts or [str(download_url or "").strip()]
                logger.info(
                    "开始下载新版本安装包: %d 个分卷 -> %s", len(urls), tmp_path
                )
                downloaded_total = 0
                grand_total = int(total_size_hint or 0)
                last_emit_time = 0.0
                last_percent = -1
                part_index = 0

                def _report_progress(
                    force: bool = False, part_done: int = 0, part_size: int = 0
                ):
                    nonlocal last_emit_time, last_percent
                    now = time.monotonic()
                    if len(urls) > 1 and grand_total <= 0:
                        part_frac = part_done / part_size if part_size > 0 else 0
                        percent = int(min(99, ((part_index - 1 + part_frac) / len(urls)) * 100))
                    else:
                        # 分母是元数据/首片长度估出来的，实际字节可能略多
                        # （重定向后响应头与实际不符等），这里兜到 100 以内，
                        # 进度条绝不显示超过 100%
                        percent = (
                            min(100, int(downloaded_total * 100 / grand_total))
                            if grand_total > 0
                            else 0
                        )
                    if force:
                        percent = 100
                    if force or now - last_emit_time >= 0.15 or percent != last_percent:
                        last_emit_time = now
                        last_percent = percent
                        on_progress(
                            {
                                "percent": percent,
                                "downloaded": downloaded_total,
                                "total": grand_total,
                                "part_index": part_index,
                                "part_count": len(urls),
                            }
                        )

                import http.cookiejar as _cj

                urllib.request.install_opener(
                    urllib.request.build_opener(
                        urllib.request.HTTPCookieProcessor(_cj.CookieJar())
                    )
                )

                open_mode = "wb"

                def _download_one_part(i: int, url: str) -> bool:
                    nonlocal open_mode, downloaded_total, part_index, grand_total
                    part_index = i + 1
                    start_offset = 0 if i == 0 else (
                        os.path.getsize(tmp_path) if os.path.exists(tmp_path) else 0
                    )
                    last_reason = ""
                    # 本片开始前已累计的字节数：重试时临时文件要截断回片起点，
                    # 进度分子必须同步回滚到这里，否则上一轮失败前读到的字节会
                    # 被重复计入，进度突破 100%（用户反馈过 76.7MB / 65.9MB = 116%）
                    base_downloaded = downloaded_total
                    for attempt in range(len(RETRY_DELAYS) + 1):
                        if self._cancel_event.is_set():
                            _cleanup_tmp()
                            return False
                        if attempt:
                            delay = RETRY_DELAYS[attempt - 1]
                            logger.info(
                                "分卷 %d/%d 下载失败（%s），%ds 后重试（%d/%d）",
                                i + 1, len(urls), last_reason, delay,
                                attempt, len(RETRY_DELAYS),
                            )
                            time.sleep(delay)
                            try:
                                with open(tmp_path, "r+b") as f:
                                    f.truncate(start_offset)
                            except OSError:
                                pass
                            # 文件回退了，进度分子必须同步回退，否则重复计数
                            downloaded_total = base_downloaded
                        req = urllib.request.Request(
                            url, headers={"User-Agent": HTTP_USER_AGENT},
                        )
                        part_total = 0
                        part_downloaded = 0
                        try:
                            with urllib.request.urlopen(req, timeout=30.0) as resp:
                                content_type = str(
                                    resp.headers.get("Content-Type", "")
                                ).lower()
                                if (
                                    "text/html" in content_type
                                    or "text/plain" in content_type
                                ):
                                    last_reason = f"返回网页 ({content_type})"
                                    continue
                                part_total = int(
                                    resp.headers.get("Content-Length", 0)
                                )
                                if grand_total <= 0 and part_total > 0:
                                    grand_total = part_total * len(urls)
                                with open(tmp_path, open_mode) as f:
                                    while True:
                                        if self._cancel_event.is_set():
                                            _cleanup_tmp()
                                            return False
                                        chunk = resp.read(64 * 1024)
                                        if not chunk:
                                            break
                                        f.write(chunk)
                                        part_downloaded += len(chunk)
                                        downloaded_total += len(chunk)
                                        _report_progress(
                                            part_done=part_downloaded,
                                            part_size=part_total,
                                        )
                        except (urllib.error.URLError, OSError) as error:
                            last_reason = f"网络错误 ({error})"
                            continue
                        # 完整性校验：声明了 Content-Length 的分片，实际字节数
                        # 必须一致，否则视为连接中断的截断下载（可重试）
                        if part_total > 0 and part_downloaded != part_total:
                            last_reason = (
                                f"不完整 {part_downloaded}/{part_total} 字节"
                            )
                            continue
                        # 安全防线 2：单片分卷必须 > 1MB（真实分卷 ≥ 95MB，HTML
                        # 错误页通常只有几十 KB）。小于阈值视为异常（可重试）
                        if part_downloaded < 1024 * 1024:
                            last_reason = f"大小异常 ({part_downloaded} 字节)"
                            continue
                        open_mode = "ab"
                        return True
                    logger.error(
                        "分卷 %d/%d 重试 %d 次后仍失败: %s",
                        i + 1, len(urls), len(RETRY_DELAYS), last_reason,
                    )
                    _cleanup_tmp()
                    retry_note = f"已自动重试 {len(RETRY_DELAYS)} 次" if RETRY_DELAYS else ""
                    on_error(
                        f"安装包下载失败（{last_reason}）"
                        f"{'，' + retry_note if retry_note else ''}，"
                        "请稍后重试，或前往 Release 页面手动下载"
                    )
                    return False

                for i, url in enumerate(urls):
                    if not _download_one_part(i, url):
                        return
                    if self._cancel_event.is_set():
                        _cleanup_tmp()
                        return

                if expected_sha256:
                    actual_hash = _sha256_file(tmp_path)
                    if actual_hash != expected_sha256.lower():
                        logger.error(
                            "安装包 SHA256 校验失败: 期望 %s，实际 %s",
                            expected_sha256.lower(),
                            actual_hash,
                        )
                        _cleanup_tmp()
                        on_error(
                            "安装包完整性校验失败（SHA256 不匹配），"
                            "文件可能被篡改或下载损坏，已拒绝安装"
                        )
                        return

                if os.path.exists(dest_path):
                    try:
                        os.remove(dest_path)
                    except Exception:
                        pass
                os.replace(tmp_path, dest_path)
                logger.info("安装包下载成功: %s", dest_path)
                _report_progress(force=True)
                on_complete(dest_path)
            except Exception as e:
                logger.exception("下载安装包失败")
                _cleanup_tmp()
                on_error(str(e))
            finally:
                with self._lock:
                    self._is_downloading = False

        self._thread = threading.Thread(
            target=_worker,
            daemon=True,
            name="gui-update-downloader",
        )
        self._thread.start()
        return True

    def cancel(self):
        """取消当前正在进行的下载。"""
        self._cancel_event.set()


updater_downloader = UpdateDownloader()


def launch_installer(installer_path: str) -> bool:
    if not os.path.isfile(installer_path):
        logger.error("安装程序不存在: %s", installer_path)
        return False

    # 安全防线 3：启动前检查 PE 头（MZ 魔数），非可执行文件直接拒绝
    # （防止把 HTML 错误页当安装包启动导致静默失败）
    try:
        with open(installer_path, "rb") as f:
            magic = f.read(2)
        if magic != b"MZ":
            logger.error(
                "安装包不是有效的 Windows 可执行文件（MZ 头缺失，可能是 HTML 错误页）: %s",
                installer_path,
            )
            return False
    except Exception as e:
        logger.error("读取安装包文件头失败: %s - %s", installer_path, e)
        return False

    norm_path = os.path.normpath(installer_path)
    # 纵深防御：路径将拼进 cmd 字符串，必须不含引号/& 等 shell 特殊字符
    base_name = os.path.basename(norm_path)
    if not is_safe_version_string(base_name.replace(".exe", "")):
        logger.error("安装包文件名含特殊字符，拒绝启动: %s", norm_path)
        return False
    # cmd 批处理命令：只启动安装向导（安装包清理由向导/启动策略清理负责，
    # 这里绝不能跟 & del，否则用户关掉向导包就没了，必须重新下载）
    cmd = f'start /wait "" "{norm_path}"'
    logger.info("唤起安装程序（不在此处清理安装包）: %s", cmd)

    try:
        creationflags = 0
        if sys.platform == "win32":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

        subprocess.Popen(
            cmd,
            shell=True,
            creationflags=creationflags,
        )
        return True
    except Exception as e:
        logger.exception("启动安装程序失败: %s", e)
        return False
