from __future__ import annotations

import logging
import os
import platform
import re
import shutil
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# ---------- 安装位置 ----------
# 全部落在用户主目录：安装包装在 Program Files 时那里不可写，只有 home 一定可写。
RUNTIME_ROOT = Path.home() / ".ycode" / "pet-runtime"
ELECTRON_ROOT = RUNTIME_ROOT / "electron"
DIST_DIR = ELECTRON_ROOT / "dist"
DOWNLOAD_DIR = RUNTIME_ROOT / "download"

DEFAULT_VERSION = "44.11.3"
VERSION = DEFAULT_VERSION

# npmmirror 是国内镜像（快），GitHub releases 是官方源（兜底）。
MIRROR_BASE = "https://npmmirror.com/mirrors/electron"
GITHUB_BASE = "https://github.com/electron/electron/releases/download"

_CONNECT_TIMEOUT = 20.0
_READ_TIMEOUT = 60.0
_CHUNK = 256 * 1024
_NO_PROGRESS_LIMIT = 600.0

_UA = "XingyaoPetRuntime/1.0"

_PUBLIC_KEYS = ("state", "downloaded", "total", "percent", "msg", "version", "mirror")

_PLATFORM_MAP = {"win32": "win32", "darwin": "darwin", "linux": "linux"}
_ARCH_MAP = {
    "x86_64": "x64",
    "amd64": "x64",
    "x64": "x64",
    "aarch64": "arm64",
    "arm64": "arm64",
}
_SUPPORTED_TAGS = (
    "win32-x64",
    "win32-arm64",
    "darwin-x64",
    "darwin-arm64",
    "linux-x64",
    "linux-arm64",
)

_STATE_LOCK = threading.Lock()
_INSTALL_LOCK = threading.Lock()
_WORKER: Optional[threading.Thread] = None
_PROGRESS: Optional[Callable[[dict], None]] = None

_STATE = {
    "state": "idle",
    "downloaded": 0,
    "total": 0,
    "percent": 0,
    "msg": "",
    "version": "",
    "mirror": "",
    "url": "",
    "updated_at": 0.0,
}


class PetRuntimeError(Exception):
    """运行时安装失败（内部用，最终都会被转成 state=failed 的 status）。"""


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


def _active_version() -> str:
    """当前要装的 Electron 版本：配置项 pet_electron_version 优先，否则用 VERSION。"""
    raw = str(_config_value("pet_electron_version", "") or "").strip().strip('"')
    if raw.lower().startswith("v"):
        raw = raw[1:]
    if raw and re.fullmatch(r"\d+\.\d+\.\d+([-+][0-9A-Za-z.\-+]+)?", raw):
        return raw
    fallback = str(VERSION or DEFAULT_VERSION).strip() or DEFAULT_VERSION
    return fallback


def _mirror_base() -> str:
    raw = str(_config_value("pet_electron_mirror", "") or "").strip().strip('"')
    return (raw or MIRROR_BASE).rstrip("/")


def _recompute_percent_locked() -> None:
    total = int(_STATE.get("total") or 0)
    done = int(_STATE.get("downloaded") or 0)
    if total > 0:
        _STATE["percent"] = max(0, min(100, int(done * 100 / total)))
    else:
        _STATE["percent"] = 0


def _update(**fields) -> None:
    """合并式更新状态；锁外回调进度，回调异常不能影响下载线程。"""
    with _STATE_LOCK:
        _STATE.update(fields)
        _STATE["updated_at"] = time.time()
        _recompute_percent_locked()
        snap = {k: _STATE.get(k) for k in _PUBLIC_KEYS}
        cb = _PROGRESS
    if cb is not None:
        try:
            cb(dict(snap))
        except Exception:
            logger.debug("进度回调异常", exc_info=True)


def status() -> dict:
    """当前下载/安装状态快照（界面轮询这个画进度条）。"""
    with _STATE_LOCK:
        snap = {k: _STATE.get(k) for k in _PUBLIC_KEYS}
    if not snap.get("version"):
        snap["version"] = _active_version()
    snap["downloaded"] = int(snap.get("downloaded") or 0)
    snap["total"] = int(snap.get("total") or 0)
    snap["percent"] = int(snap.get("percent") or 0)
    snap["msg"] = str(snap.get("msg") or "")
    snap["mirror"] = str(snap.get("mirror") or "")
    snap["state"] = str(snap.get("state") or "idle")
    return snap


def reset_for_test() -> None:
    """清空内存状态（不删已装文件），方便测试重复跑。"""
    global _WORKER, _PROGRESS
    with _STATE_LOCK:
        _STATE.update(
            state="idle",
            downloaded=0,
            total=0,
            percent=0,
            msg="",
            version="",
            mirror="",
            url="",
            updated_at=0.0,
        )
    _WORKER = None
    _PROGRESS = None


def _exe_name() -> str:
    return "electron.exe" if sys.platform.startswith("win") else "electron"


def _dir_size(path: Path) -> int:
    total = 0
    try:
        for item in path.rglob("*"):
            try:
                if item.is_file():
                    total += item.stat().st_size
            except OSError:
                continue
    except OSError:
        return 0
    return total


def _safe_unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        logger.debug("删除文件失败: %s", path, exc_info=True)


def _safe_rmtree(path: Path) -> None:
    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        pass
    except OSError:
        logger.debug("删除目录失败: %s", path, exc_info=True)


def _platform_tag() -> str:
    raw = (sys.platform or "").lower()
    if raw.startswith("win"):
        base = _PLATFORM_MAP["win32"]
    elif raw == "darwin":
        base = _PLATFORM_MAP["darwin"]
    elif raw.startswith("linux"):
        base = _PLATFORM_MAP["linux"]
    else:
        raise PetRuntimeError(
            f"当前系统（{sys.platform}）暂不支持自动安装 Electron 运行时；"
            "可在设置页手动填写 electron.exe 路径"
        )
    machine = (platform.machine() or "").lower()
    arch = _ARCH_MAP.get(machine)
    if not arch:
        raise PetRuntimeError(
            f"当前架构（{machine or '未知'}）暂不支持自动安装 Electron 运行时；"
            "可在设置页手动填写 electron.exe 路径"
        )
    tag = f"{base}-{arch}"
    if tag not in _SUPPORTED_TAGS:
        raise PetRuntimeError(
            f"Electron 未提供 {tag} 的运行时包；可在设置页手动填写 electron.exe 路径"
        )
    return tag


def _candidate_sources(version: str, tag: str) -> list:
    zip_name = f"electron-v{version}-{tag}.zip"
    return [
        (f"{_mirror_base()}/{version}/{zip_name}", f"{zip_name}"),
        (f"{GITHUB_BASE}/v{version}/{zip_name}", f"{zip_name}"),
    ]


def _host_of(url: str) -> str:
    try:
        return urllib.parse.urlsplit(url).netloc or url
    except Exception:
        return url


def _relax_read_timeout(resp, seconds: float) -> None:
    try:
        sock = getattr(getattr(getattr(resp, "fp", None), "raw", None), "_sock", None)
        if sock is not None:
            sock.settimeout(seconds)
    except Exception:
        logger.debug("调整 socket 读超时失败，沿用建连超时", exc_info=True)


def detect() -> dict:
    exe = DIST_DIR / _exe_name()
    installed = False
    try:
        installed = exe.is_file() and exe.stat().st_size > 0
    except OSError:
        installed = False
    version = None
    try:
        version = (ELECTRON_ROOT / "version").read_text(encoding="utf-8").strip() or None
    except Exception:
        version = None
    return {
        "installed": installed,
        "exe": str(exe) if installed else None,
        "version": version,
        "size": _dir_size(DIST_DIR),
    }


def _download_once(url: str, part: Path) -> int:
    offset = 0
    try:
        if part.is_file():
            offset = part.stat().st_size
    except OSError:
        offset = 0

    headers = {"User-Agent": _UA, "Accept": "application/octet-stream"}
    if offset > 0:
        headers["Range"] = f"bytes={offset}-"
    req = urllib.request.Request(url, headers=headers)
    resp = urllib.request.urlopen(req, timeout=_CONNECT_TIMEOUT)
    _relax_read_timeout(resp, _READ_TIMEOUT)
    try:
        code = int(getattr(resp, "status", None) or resp.getcode() or 200)
        if code == 416 and offset > 0:
            resp.close()
            _safe_unlink(part)
            return _download_once_no_range(url, part)
        if code == 200 and offset > 0:
            # 服务端忽略了 Range：只能从头写，不能把整包追加到半包后面。
            offset = 0

        length_header = resp.headers.get("Content-Length")
        total = 0
        try:
            if length_header and str(length_header).isdigit():
                total = int(length_header) + (offset if code == 206 else 0)
        except Exception:
            total = 0

        _update(
            state="downloading",
            downloaded=offset,
            total=total,
            mirror=_host_of(url),
            url=url,
        )

        mode = "ab" if offset > 0 else "wb"
        written = offset
        last_tick = time.time()
        with open(part, mode) as fh:
            while True:
                chunk = resp.read(_CHUNK)
                if not chunk:
                    break
                fh.write(chunk)
                written += len(chunk)
                now = time.time()
                if now - last_tick > _NO_PROGRESS_LIMIT:
                    raise PetRuntimeError("下载超过 10 分钟没有进展，已放弃")
                last_tick = now
                _update(downloaded=written)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                logger.debug("fsync 下载文件失败", exc_info=True)
    finally:
        try:
            resp.close()
        except Exception:
            pass

    actual = 0
    try:
        actual = part.stat().st_size
    except OSError:
        actual = 0
    if total and actual != total:
        raise PetRuntimeError(
            f"下载不完整：Content-Length {total} 字节，实际 {actual} 字节"
        )
    _update(downloaded=actual, total=total or actual)
    return actual


def _download_once_no_range(url: str, part: Path) -> int:
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    resp = urllib.request.urlopen(req, timeout=_CONNECT_TIMEOUT)
    _relax_read_timeout(resp, _READ_TIMEOUT)
    total = 0
    try:
        length_header = resp.headers.get("Content-Length")
        if length_header and str(length_header).isdigit():
            total = int(length_header)
        _update(
            state="downloading",
            downloaded=0,
            total=total,
            mirror=_host_of(url),
            url=url,
        )
        written = 0
        last_tick = time.time()
        with open(part, "wb") as fh:
            while True:
                chunk = resp.read(_CHUNK)
                if not chunk:
                    break
                fh.write(chunk)
                written += len(chunk)
                now = time.time()
                if now - last_tick > _NO_PROGRESS_LIMIT:
                    raise PetRuntimeError("下载超过 10 分钟没有进展，已放弃")
                last_tick = now
                _update(downloaded=written)
    finally:
        try:
            resp.close()
        except Exception:
            pass
    actual = part.stat().st_size if part.is_file() else 0
    if total and actual != total:
        raise PetRuntimeError(
            f"下载不完整：Content-Length {total} 字节，实际 {actual} 字节"
        )
    return actual


def _download_with_fallback(version: str, tag: str, part: Path) -> Path:
    errors = []
    for url, zip_name in _candidate_sources(version, tag):
        host = _host_of(url)
        _update(
            state="downloading",
            downloaded=0,
            total=0,
            mirror=host,
            url=url,
            msg=f"正在从 {host} 下载 Electron {version}…",
        )
        try:
            size = _download_once(url, part)
            if not size:
                raise PetRuntimeError("下载到 0 字节")
            logger.info("Electron 运行时下载完成：%s（%d 字节）", host, size)
            return part
        except urllib.error.HTTPError as exc:
            errors.append(f"{host}: HTTP {exc.code}")
            logger.warning("从 %s 下载失败：HTTP %s", host, exc.code)
        except Exception as exc:
            errors.append(f"{host}: {exc}")
            logger.warning("从 %s 下载失败：%s", host, exc)
        _safe_unlink(part)
    raise PetRuntimeError(
        "下载 Electron 运行时失败（" + "；".join(errors or ["未知错误"]) + "）。"
        "可稍后重试，或自行下载 electron.exe 后在设置页填写路径"
    )


def _swap_into_place(tmp: Path) -> None:
    old = None
    if DIST_DIR.exists():
        old = ELECTRON_ROOT / f"dist.old-{os.getpid()}"
        _safe_rmtree(old)
        try:
            DIST_DIR.rename(old)
        except OSError:
            # Windows 上目录被占用时改名会失败：退化成先删再放，删不掉就只能放弃。
            try:
                shutil.rmtree(DIST_DIR)
            except OSError as exc:
                raise PetRuntimeError(f"旧运行时目录无法替换：{exc}")
            old = None
    try:
        tmp.rename(DIST_DIR)
    except OSError as exc:
        if old is not None:
            try:
                old.rename(DIST_DIR)
            except OSError:
                logger.debug("回滚旧运行时目录失败", exc_info=True)
        raise PetRuntimeError(f"运行时目录替换失败：{exc}")
    if old is not None:
        _safe_rmtree(old)


def _install_from_zip(zip_path: Path, version: str) -> dict:
    _update(state="verifying", msg="正在校验安装包…")
    try:
        zf = zipfile.ZipFile(zip_path)
    except Exception as exc:
        raise PetRuntimeError(f"安装包无法打开：{exc}")
    with zf:
        bad = zf.testzip()
        if bad is not None:
            raise PetRuntimeError(f"安装包校验失败（损坏的文件：{bad}）")
        _update(state="extracting", msg="正在解压 Electron 运行时…")
        tmp = ELECTRON_ROOT / f"dist.tmp-{os.getpid()}"
        _safe_rmtree(tmp)
        try:
            tmp.mkdir(parents=True, exist_ok=True)
            zf.extractall(tmp)
        except Exception as exc:
            _safe_rmtree(tmp)
            raise PetRuntimeError(f"解压失败：{exc}")

    exe_name = _exe_name()
    exe_path = tmp / exe_name
    if not exe_path.is_file() or exe_path.stat().st_size == 0:
        _safe_rmtree(tmp)
        raise PetRuntimeError(f"解压后未找到 {exe_name}，安装包结构异常")
    if not sys.platform.startswith("win"):
        # zipfile 不还原可执行位，Linux/macOS 上手动补一下，否则拉不起来。
        try:
            os.chmod(exe_path, 0o755)
        except OSError:
            logger.debug("设置 electron 可执行位失败", exc_info=True)

    _swap_into_place(tmp)

    try:
        (ELECTRON_ROOT / "path.txt").write_text(exe_name, encoding="utf-8")
        (ELECTRON_ROOT / "version").write_text(version + "\n", encoding="utf-8")
    except OSError as exc:
        logger.warning("写入运行时元信息失败: %s", exc)
    return detect()


def _cleanup_artifacts() -> None:
    """失败收口：删 .part 与所有 dist.tmp-* / dist.old-*，但绝不碰完整的 DIST_DIR。"""
    try:
        if DOWNLOAD_DIR.is_dir():
            for item in DOWNLOAD_DIR.glob("*.part"):
                _safe_unlink(item)
    except OSError:
        logger.debug("清理下载临时文件失败", exc_info=True)
    try:
        if ELECTRON_ROOT.is_dir():
            for pattern in ("dist.tmp-*", "dist.old-*"):
                for item in ELECTRON_ROOT.glob(pattern):
                    _safe_rmtree(item)
    except OSError:
        logger.debug("清理解压临时目录失败", exc_info=True)


def _fail(msg: str) -> None:
    _cleanup_artifacts()
    logger.warning("Electron 运行时安装失败：%s", msg)
    _update(state="failed", msg=str(msg or "安装失败"))


def _do_install() -> None:
    version = _active_version()
    tag = _platform_tag()
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    ELECTRON_ROOT.mkdir(parents=True, exist_ok=True)

    zip_name = f"electron-v{version}-{tag}.zip"
    part = DOWNLOAD_DIR / f"{zip_name}.part"
    _update(
        state="downloading",
        downloaded=0,
        total=0,
        msg=f"正在准备下载 Electron {version}…",
        version=version,
    )
    _download_with_fallback(version, tag, part)
    _install_from_zip(part, version)

    _safe_unlink(part)

    info = detect()
    size_mb = max(1, int(round((info.get("size") or 0) / (1024 * 1024))))
    _update(
        state="done",
        downloaded=info.get("size") or 0,
        total=info.get("size") or 0,
        percent=100,
        version=version,
        msg=f"Electron {version} 已安装（{size_mb}MB），可以启用桌宠了",
    )


def _install_worker() -> None:
    global _WORKER
    try:
        _do_install()
    except PetRuntimeError as exc:
        _fail(str(exc))
    except Exception as exc:
        # 兜底：任何未预期异常（磁盘满、权限、zip 结构怪异）都只反映成 failed。
        logger.warning("Electron 运行时安装异常", exc_info=True)
        _fail(f"安装 Electron 运行时出错：{exc}")
    finally:
        with _INSTALL_LOCK:
            _WORKER = None
        with _STATE_LOCK:
            final_state = _STATE.get("state")
        if final_state != "done":
            _cleanup_artifacts()


def install(progress: Optional[Callable[[dict], None]] = None) -> dict:
    global _WORKER, _PROGRESS
    try:
        with _INSTALL_LOCK:
            running = _WORKER is not None and _WORKER.is_alive()
            if running:
                return status()
            info = detect()
            if info["installed"]:
                _update(
                    state="done",
                    downloaded=info.get("size") or 0,
                    total=info.get("size") or 0,
                    percent=100,
                    version=info.get("version") or _active_version(),
                    msg=f"Electron {info.get('version') or _active_version()} 已安装，可以启用桌宠了",
                )
                return status()
            _PROGRESS = progress if callable(progress) else None
            _update(
                state="downloading",
                downloaded=0,
                total=0,
                msg=f"正在准备下载 Electron {_active_version()}…",
                version=_active_version(),
                mirror="",
                url="",
            )
            worker = threading.Thread(
                target=_install_worker,
                name="pet-runtime-install",
                daemon=True,
            )
            _WORKER = worker
            worker.start()
    except Exception as exc:
        logger.debug("启动安装线程失败", exc_info=True)
        _fail(f"启动安装失败：{exc}")
    return status()
