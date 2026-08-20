"""
core/updater.py
===============
Y-CODE 自动与手动更新管理模块：
对接 Gitee Releases API，支持版本检测、后台流式分块下载、进度通知与安装后自清理。
"""

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
from typing import Any, Callable, Dict, Optional, Tuple

from core.version import __version__ as YCODE_VERSION

logger = logging.getLogger(__name__)

# 默认 Gitee 仓库地址
GITEE_REPO = "feng-chenhao/xingyao-y-code"
GITEE_API_URL = f"https://gitee.com/api/v5/repos/{GITEE_REPO}/releases/latest"
HTTP_USER_AGENT = f"ycode-updater/{YCODE_VERSION} (Windows)"


def parse_version(v_str: str) -> Tuple[int, ...]:
    """将版本号字符串（如 'v1.2.6', '1.2.6-beta', '1.3'）解析为整型元组供比较。"""
    if not v_str:
        return (0, 0, 0)
    # 提取所有数字序列
    nums = re.findall(r"\d+", str(v_str))
    if not nums:
        return (0, 0, 0)
    return tuple(int(n) for n in nums)


def compare_versions(v1: str, v2: str) -> int:
    """比较两个版本号。
    返回值：
      1  若 v1 > v2
      0  若 v1 == v2
     -1  若 v1 < v2
    """
    t1 = parse_version(v1)
    t2 = parse_version(v2)
    # 补齐长度以便对比
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


def cleanup_old_installers() -> int:
    """清理历史残留安装包与临时文件，返回清理的文件数量。"""
    cleaned = 0
    try:
        cache_dir = get_update_cache_dir()
        if not os.path.isdir(cache_dir):
            return 0
        for fname in os.listdir(cache_dir):
            fpath = os.path.join(cache_dir, fname)
            try:
                if os.path.isfile(fpath):
                    os.remove(fpath)
                    cleaned += 1
                elif os.path.isdir(fpath):
                    shutil.rmtree(fpath, ignore_errors=True)
                    cleaned += 1
            except Exception as e:
                logger.debug("清理文件 %s 失败: %s", fpath, e)
    except Exception as e:
        logger.warning("扫描更新缓存目录失败: %s", e)
    return cleaned


def fetch_latest_release(
    repo: str = GITEE_REPO, timeout: float = 8.0
) -> Dict[str, Any]:
    """查询 Gitee Releases 最新版本信息。

    若 Gitee 尚未创建 Release 或返回 404，安全返回 has_update: False。
    """
    url = f"https://gitee.com/api/v5/repos/{repo}/releases/latest"
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
            # 仓库暂无 Release，正常判定为无更新
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

    raw_tag = data.get("tag_name") or ""
    # 清洗 tag 中的版本号（如 'v1.2.6' -> '1.2.6'）
    latest_version = raw_tag.lstrip("vV").strip() or raw_tag
    release_title = data.get("name") or f"Y-CODE v{latest_version}"
    release_notes = data.get("body") or "暂无更新说明。"

    # 寻找匹配的 Windows 安装包附件（.exe）
    assets = data.get("assets") or []
    download_url = ""
    asset_name = ""
    asset_size = 0

    for asset in assets:
        name = asset.get("name", "")
        if name.lower().endswith(".exe"):
            download_url = asset.get("browser_download_url", "")
            asset_name = name
            asset_size = int(asset.get("size") or 0)
            break

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
    }


class UpdateDownloader:
    """后台分块流式下载器，支持实时进度汇报与取消控制。"""

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
    ) -> bool:
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

        def _worker():
            try:
                logger.info("开始下载新版本安装包: %s -> %s", download_url, tmp_path)
                req = urllib.request.Request(
                    download_url,
                    headers={"User-Agent": HTTP_USER_AGENT},
                )
                with urllib.request.urlopen(req, timeout=30.0) as resp:
                    total_size = int(resp.headers.get("Content-Length", 0))
                    downloaded = 0
                    chunk_size = 64 * 1024  # 64 KB
                    last_emit_time = 0.0
                    last_percent = -1

                    with open(tmp_path, "wb") as f:
                        while True:
                            if self._cancel_event.is_set():
                                logger.info("下载被用户主动取消")
                                break
                            chunk = resp.read(chunk_size)
                            if not chunk:
                                break
                            f.write(chunk)
                            downloaded += len(chunk)

                            # 节流上报：每 0.15 秒或进度变化超过 1% 时上报一次
                            now = time.monotonic()
                            percent = (
                                int(downloaded * 100 / total_size)
                                if total_size > 0
                                else 0
                            )
                            if (
                                now - last_emit_time >= 0.15
                                or percent != last_percent
                            ):
                                last_emit_time = now
                                last_percent = percent
                                on_progress(
                                    {
                                        "percent": percent,
                                        "downloaded": downloaded,
                                        "total": total_size,
                                    }
                                )

                if self._cancel_event.is_set():
                    try:
                        if os.path.isfile(tmp_path):
                            os.remove(tmp_path)
                    except Exception:
                        pass
                    return

                # 下载完成，重命名临时文件为正式安装包
                if os.path.exists(dest_path):
                    try:
                        os.remove(dest_path)
                    except Exception:
                        pass
                os.replace(tmp_path, dest_path)
                logger.info("安装包下载成功: %s", dest_path)
                on_complete(dest_path)
            except Exception as e:
                logger.exception("下载安装包失败")
                try:
                    if os.path.isfile(tmp_path):
                        os.remove(tmp_path)
                except Exception:
                    pass
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


# 全局单例下载器
updater_downloader = UpdateDownloader()


def launch_installer_and_cleanup(installer_path: str) -> bool:
    """启动安装程序并在安装完成后由外部批处理自动清理安装包文件。

    使用 cmd.exe /c start /wait 运行安装包，等待安装程序完全关闭后 del 删除文件。
    """
    if not os.path.isfile(installer_path):
        logger.error("安装程序不存在: %s", installer_path)
        return False

    norm_path = os.path.normpath(installer_path)
    # cmd 批处理命令：等待安装向导退出后自删除安装包
    cmd = f'start /wait "" "{norm_path}" & del /f /q "{norm_path}"'
    logger.info("唤起安装程序自清理脚本: %s", cmd)

    try:
        # 隐藏批处理黑框
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
