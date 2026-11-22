from __future__ import annotations

import atexit
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_PROC: Optional[subprocess.Popen] = None

# 关键段互斥（start/stop/get_status 全走这把锁；用 RLock 是因为 start/stop 内部
# 还会再调一次 get_status 取最新状态）。
_LOCK = threading.RLock()

_START_WAIT = 1.2
_STOP_WAIT = 4.0

# Windows 下不弹控制台窗口；非 Windows 该常量不存在，取 0。
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_atexit_registered = False


def _pet_dir_candidates():
    env_dir = (os.environ.get("YCODE_PET_DIR") or "").strip()
    if env_dir:
        yield Path(env_dir)
    if getattr(sys, "frozen", False):
        # PyInstaller onefile 会把附带资源解到 _MEIPASS，onefolder/exe 旁则是
        # 与 exe 同级的 pets 目录，两条都要试。
        meipass = getattr(sys, "_MEIPASS", "") or ""
        if meipass:
            yield Path(meipass) / "pets" / "xingyao_pet"
        exe_dir = Path(sys.executable).resolve().parent
        yield exe_dir / "pets" / "xingyao_pet"
    yield Path(__file__).resolve().parent.parent / "pets" / "xingyao_pet"


def _resolve_pet_dir() -> Path:
    first = None
    for cand in _pet_dir_candidates():
        if first is None:
            first = cand
        try:
            if cand.is_dir():
                return cand
        except OSError:
            continue
    return first or (Path(__file__).resolve().parent.parent / "pets" / "xingyao_pet")


# 桌宠目录常量。注意运行期请用 _pet_dir()：它会重读 YCODE_PET_DIR，
# 让设置页/测试临时改目录能立即生效，不必重启星瑶。
PET_DIR: Path = _resolve_pet_dir()


def _pet_dir() -> Path:
    env_dir = (os.environ.get("YCODE_PET_DIR") or "").strip()
    if env_dir:
        try:
            if Path(env_dir).is_dir():
                return Path(env_dir)
        except OSError:
            pass
    return PET_DIR


def pet_dir_path() -> Path:
    return _pet_dir()


def _active_skin_safe() -> dict:
    """当前激活皮肤的安全快照（id/name/assets_dir/config_path），任何异常都不外抛。

    桌宠开关绝不能因为宠物库解析失败而整体失败：pet_skins 不可用、宠物包元数据
    损坏、配置里留着已删除的 id，最坏也只是回落成内置星瑶——那仍是一只完整可
    显示的形象，比设置页报错、开关点了没反应好得多。
    """
    pet = _pet_dir()
    fallback = {
        "id": "xingyao",
        "name": "星瑶",
        "assets_dir": str(pet / "assets"),
        "config_path": str(pet / "src" / "renderer" / "config.json"),
    }
    # 不在模块顶层 import pet_skins：pet_skins 内部会延迟导入本模块，顶层互导会绕
    # 成环。两种导入形式都试（本仓库里两种习惯都存在），写法同 _runtime_electron_candidates。
    active_skin_id = None
    resolve_active = None
    try:
        from .pet_skins import active_skin_id, resolve_active
    except Exception:
        try:
            from core.pet_skins import active_skin_id, resolve_active
        except Exception:
            logger.debug("pet_skins 不可用，桌宠回落内置形象", exc_info=True)
    if active_skin_id is None or resolve_active is None:
        return fallback
    try:
        skin = resolve_active(active_skin_id())
    except Exception:
        logger.debug("解析激活皮肤失败，桌宠回落内置形象", exc_info=True)
        return fallback
    if not isinstance(skin, dict):
        return fallback
    try:
        return {
            "id": str(skin.get("id") or fallback["id"]),
            "name": str(skin.get("name") or fallback["name"]),
            "assets_dir": str(skin.get("assets_dir") or ""),
            "config_path": str(skin.get("config_path") or ""),
        }
    except Exception:
        return fallback


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


def _runtime_electron_candidates():
    dist = None
    for mod_name in ("core.pet_runtime", "pet_runtime"):
        try:
            mod = __import__(mod_name, fromlist=["DIST_DIR"])
            dist = Path(mod.DIST_DIR)
            break
        except Exception:
            continue
    if dist is None:
        logger.debug("pet_runtime 不可用，跳过用户级运行时候选", exc_info=True)
        return []
    return [dist / "electron.exe", dist / "electron"]


def _resolve_electron(explicit: str = "") -> Optional[Path]:
    candidates = []
    for raw in (explicit, _config_value("pet_electron_path", "") or ""):
        raw = str(raw or "").strip().strip('"')
        if raw:
            candidates.append(Path(raw))
    pet = _pet_dir()
    dist = pet / "node_modules" / "electron" / "dist"
    candidates.append(dist / "electron.exe")
    candidates.append(dist / "electron")
    candidates.extend(_runtime_electron_candidates())
    for cand in candidates:
        try:
            if cand.is_file():
                return cand
        except OSError:
            continue
    found = shutil.which("electron")
    return Path(found) if found else None


def _norm_path(value) -> str:
    """把路径归一化成可比较的形态（Windows 下大小写不敏感）。"""
    try:
        return os.path.normcase(str(Path(value).resolve()))
    except Exception:
        return os.path.normcase(str(value))


def _pid_file() -> Path:
    base = None
    try:
        base = _config_value("YCODE_DIR", None)
    except Exception:
        base = None
    if base:
        try:
            return Path(base) / "pet.pid"
        except Exception:
            pass
    return Path.home() / ".ycode" / "pet.pid"


def _read_pid_record() -> Optional[dict]:
    path = _pid_file()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception:
        logger.debug("读取桌宠 pid 记录失败: %s", path, exc_info=True)
        return None
    try:
        pid = int(data.get("pid", 0))
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    # pid 记录是全局的（默认 ~/.ycode/pet.pid），必须校验它属于当前桌宠目录：
    # 否则换目录/复制一份桌宠后，旧记录会让状态栏误报"运行中"，还会让
    # push_bubble 往一个陌生进程里写数据。旧格式（无 pet_dir）按当前目录处理。
    recorded_dir = data.get("pet_dir")
    if recorded_dir:
        try:
            if _norm_path(recorded_dir) != _norm_path(_pet_dir()):
                logger.debug("桌宠 pid 记录属于其它目录，忽略: %s", recorded_dir)
                return None
        except Exception:
            pass
    return {"pid": pid, "created": data.get("created"), "pet_dir": recorded_dir}


def _write_pid_record(pid: int) -> None:
    path = _pid_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"pid": int(pid), "created": time.time(), "pet_dir": str(_pet_dir())}
        path.write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
    except Exception:
        logger.debug("写桌宠 pid 记录失败: %s", path, exc_info=True)


def _clear_pid_record() -> None:
    path = _pid_file()
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except Exception:
        logger.debug("清理桌宠 pid 记录失败: %s", path, exc_info=True)


def _pid_alive(pid: int) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    cmd = ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"]
    try:
        res = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=_NO_WINDOW,
        )
    except FileNotFoundError:
        return True
    except Exception:
        logger.debug("tasklist 探活失败 pid=%s", pid, exc_info=True)
        return True
    out = (res.stdout or "").lower()
    if not out.strip():
        return False
    return "electron" in out


def get_status() -> dict:
    """查询桌宠状态。只读：不启动进程，最多跑一次 tasklist 校验 pid。"""
    with _LOCK:
        pid: Optional[int] = None
        proc = _PROC
        if proc is not None:
            try:
                if proc.poll() is None:
                    pid = proc.pid
            except Exception:
                pid = None
        if pid is None:
            record = _read_pid_record()
            if record and _pid_alive(record["pid"]):
                pid = int(record["pid"])
            elif record:
                # 上次留下的僵尸记录，顺手清掉，避免状态页一直显示"运行中"。
                _clear_pid_record()

        electron = _resolve_electron()
        pet = _pet_dir()
        main_js = pet / "src" / "main.js"
        try:
            has_main = main_js.is_file()
        except OSError:
            has_main = False
        available = bool(has_main and electron is not None)

        skin = _active_skin_safe()
        status = {
            "running": pid is not None,
            "pid": pid,
            "electron_path": str(electron) if electron is not None else None,
            "pet_dir": str(pet),
            "available": available,
            "skin_id": str(skin.get("id") or "xingyao"),
            "skin_name": str(skin.get("name") or "星瑶"),
        }
        if not available:
            missing = []
            if not has_main:
                missing.append(f"桌宠主进程脚本缺失（{main_js}）")
            if electron is None:
                # 与 start() 保持同一套可操作指引，避免两处文案各说各话。
                missing.append(
                    "未找到 Electron 运行时。三条路：① 在 pets/xingyao_pet 目录执行 "
                    "npm install（自动装 electron）；② 在设置页「桌面宠物 → Electron 路径」"
                    "填写本机 electron.exe 绝对路径；③ 全局装好后重启星瑶让它走 PATH。"
                    "已查找："
                    f"{pet / 'node_modules' / 'electron' / 'dist' / 'electron.exe'}"
                )
            status["msg"] = "；".join(missing)
        return status


def _safe_status() -> dict:
    try:
        return get_status()
    except Exception:
        return {
            "running": False,
            "pid": None,
            "electron_path": None,
            "pet_dir": str(PET_DIR),
            "available": False,
            # 兜底骨架也带皮肤，保证前端任何时候都能读到这两个字段。
            "skin_id": "xingyao",
            "skin_name": "星瑶",
        }


def start(electron_path: str = "") -> dict:
    """拉起桌宠。返回 {"ok", "msg", "status"}，任何异常都不外抛。"""
    with _LOCK:
        try:
            return _start_locked(electron_path)
        except Exception as exc:
            logger.warning("启动桌宠异常: %s", exc, exc_info=True)
            return {"ok": False, "msg": f"启动桌宠失败：{exc}", "status": _safe_status()}


def _start_locked(electron_path: str) -> dict:
    status = _safe_status()
    if status.get("running"):
        return {"ok": True, "msg": "桌宠已在运行", "status": status}

    pet = _pet_dir()
    main_js = pet / "src" / "main.js"
    problems = []
    try:
        if not pet.is_dir():
            problems.append(f"桌宠目录不存在（{pet}），请确认安装包完整")
    except OSError:
        problems.append(f"桌宠目录不可访问（{pet}）")
    try:
        if not main_js.is_file():
            problems.append(f"桌宠主进程脚本缺失（{main_js}）")
    except OSError:
        problems.append(f"桌宠主进程脚本不可访问（{main_js}）")

    electron = _resolve_electron(electron_path)
    if electron is None:
        problems.append(
            "未找到 Electron 运行时。三条路：① 在 pets/xingyao_pet 目录执行 "
            "npm install（自动装 electron）；② 在设置页「桌面宠物 → Electron 路径」"
            "填写本机 electron.exe 绝对路径；③ 全局装好后重启星瑶让它走 PATH。"
            "已查找："
            f"{pet / 'node_modules' / 'electron' / 'dist' / 'electron.exe'}"
        )
    if problems:
        return {"ok": False, "msg": "；".join(problems), "status": _safe_status()}

    global _PROC
    env = os.environ.copy()
    env.pop("ELECTRON_RUN_AS_NODE", None)
    # 当前皮肤通过环境变量传给渲染层：桌宠那边只认环境变量，路径解析（环境变量 →
    # 打包位置 → 源码树）全部留在 Python 侧，避免 Electron 里再写一套目录优先级。
    skin = _active_skin_safe()
    env["YCODE_PET_SKIN"] = str(skin.get("id") or "xingyao")
    env["YCODE_PET_NAME"] = str(skin.get("name") or "星瑶")
    assets_dir = str(skin.get("assets_dir") or "")
    if assets_dir:
        env["YCODE_PET_ASSETS"] = assets_dir
    config_path = str(skin.get("config_path") or "")
    if config_path:
        env["YCODE_PET_CONFIG"] = config_path

    proc = subprocess.Popen(
        [str(electron), str(pet)],
        cwd=str(pet),
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=_NO_WINDOW,
    )
    _PROC = proc
    _write_pid_record(proc.pid)

    time.sleep(_START_WAIT)
    returncode = proc.poll()
    if returncode is None:
        logger.info("桌宠已启动 pid=%s electron=%s", proc.pid, electron)
        return {
            "ok": True,
            "msg": f"桌宠已启动（pid {proc.pid}）",
            "status": _safe_status(),
        }

    _PROC = None
    _clear_pid_record()
    logger.warning("桌宠启动后立即退出 returncode=%s", returncode)
    return {
        "ok": False,
        "msg": (
            f"桌宠启动后立即退出（returncode={returncode}）。常见原因：已有另一个桌宠实例"
            "占着单实例锁，或 Electron 运行时缺少依赖（资源目录/运行库不完整）。"
            "可先关闭其它桌宠实例后重试；仍失败请手动运行 "
            f"{electron} 查看报错。"
        ),
        "status": _safe_status(),
    }


def _taskkill(pid: int, force: bool = False, tree: bool = False) -> bool:
    cmd = ["taskkill"]
    if force:
        cmd.append("/F")
    if tree:
        cmd.append("/T")
    cmd += ["/PID", str(pid)]
    try:
        res = subprocess.run(
            cmd, capture_output=True, text=True, timeout=10, creationflags=_NO_WINDOW
        )
        return res.returncode == 0
    except Exception:
        logger.debug("taskkill 失败 cmd=%s", cmd, exc_info=True)
        return False


def _wait_exit(proc: Optional[subprocess.Popen], pid: int, timeout: float) -> bool:
    deadline = time.time() + max(0.0, timeout)
    while time.time() < deadline:
        exited = True
        if proc is not None:
            try:
                exited = proc.poll() is not None
            except Exception:
                exited = True
        if exited and not _pid_alive(pid):
            return True
        time.sleep(0.2)
    return not _pid_alive(pid)


def stop() -> dict:
    """关闭桌宠。本来没在运行也返回 ok=True，只把情况写在 msg 里。"""
    with _LOCK:
        try:
            return _stop_locked()
        except Exception as exc:
            logger.warning("停止桌宠异常: %s", exc, exc_info=True)
            return {"ok": False, "msg": f"停止桌宠失败：{exc}", "status": _safe_status()}


def _stop_locked() -> dict:
    global _PROC
    proc = _PROC
    pid: Optional[int] = None
    if proc is not None:
        try:
            if proc.poll() is None:
                pid = proc.pid
        except Exception:
            pid = None
    if pid is None:
        record = _read_pid_record()
        if record and _pid_alive(record["pid"]):
            pid = int(record["pid"])
    if pid is None:
        _PROC = None
        _clear_pid_record()
        return {"ok": True, "msg": "桌宠未在运行", "status": _safe_status()}

    if proc is not None and pid == getattr(proc, "pid", None):
        try:
            proc.terminate()
        except Exception:
            logger.debug("terminate 桌宠失败 pid=%s", pid, exc_info=True)
    elif os.name == "nt":
        _taskkill(pid, force=False)
    else:
        try:
            os.kill(pid, 15)
        except Exception:
            logger.debug("SIGTERM 桌宠失败 pid=%s", pid, exc_info=True)

    if not _wait_exit(proc, pid, _STOP_WAIT):
        logger.warning("桌宠 %s 未在 %.1fs 内退出，强制结束", pid, _STOP_WAIT)
        if os.name == "nt":
            _taskkill(pid, force=True, tree=True)
        else:
            try:
                if proc is not None:
                    proc.kill()
                else:
                    os.kill(pid, 9)
            except Exception:
                logger.debug("强杀桌宠失败 pid=%s", pid, exc_info=True)
        _wait_exit(proc, pid, 3.0)

    still_alive = _pid_alive(pid)
    if still_alive:
        return {
            "ok": False,
            "msg": f"桌宠进程 {pid} 未能结束，请手动在任务管理器里结束 electron.exe",
            "status": _safe_status(),
        }

    _PROC = None
    _clear_pid_record()
    logger.info("桌宠已停止 pid=%s", pid)
    return {"ok": True, "msg": f"桌宠已停止（pid {pid}）", "status": _safe_status()}


_BUBBLE_MAX_CHARS = 80
_BUBBLE_DEFAULT_MS = 2600
_BUBBLE_MIN_MS = 800
_BUBBLE_MAX_MS = 15000


def _drop_dead_proc_locked() -> None:
    global _PROC
    proc = _PROC
    _PROC = None
    if proc is None:
        return
    pid = getattr(proc, "pid", None)
    try:
        if proc.stdin is not None:
            proc.stdin.close()
    except Exception:
        logger.debug("关闭桌宠 stdin 失败 pid=%s", pid, exc_info=True)
    try:
        record = _read_pid_record()
        if record and pid is not None and int(record["pid"]) == int(pid):
            _clear_pid_record()
    except Exception:
        logger.debug("清理桌宠 pid 记录失败 pid=%s", pid, exc_info=True)


def _write_pet_stdin(payload_obj: dict, *, check_external_pid: bool = True) -> dict:
    """向本进程拉起的桌宠 stdin 写入一条 JSON 指令，绝不外抛。"""
    with _LOCK:
        proc = _PROC
        alive = False
        if proc is not None:
            try:
                alive = proc.poll() is None
            except Exception:
                alive = False
            if not alive:
                _drop_dead_proc_locked()
    if not alive:
        if check_external_pid:
            record = _read_pid_record()
            if record and _pid_alive(record["pid"]):
                return {"ok": False, "msg": "桌宠不是本进程拉起的，无法推送提示"}
        return {"ok": False, "msg": "桌宠未在运行"}

    # 2) 真正写：一行几十字节 + flush，锁内耗时可以忽略，也绝不等待子进程。
    with _LOCK:
        proc = _PROC
        try:
            if proc is None:
                raise ValueError("桌宠句柄已释放")
            if proc.poll() is not None:
                raise ValueError("桌宠进程已退出")
            stream = proc.stdin
            if stream is None:
                raise ValueError("桌宠 stdin 不可用")
            payload = json.dumps(payload_obj, ensure_ascii=False) + "\n"
            stream.write(payload.encode("utf-8"))
            stream.flush()
            return {"ok": True, "msg": "ok"}
        except Exception as exc:
            # BrokenPipeError / ValueError / 管道已关：句柄已经不可信，清掉再报错。
            logger.debug("推送桌宠指令失败: %s", exc, exc_info=True)
            _drop_dead_proc_locked()
            return {"ok": False, "msg": f"推送失败：{exc}"}


def push_bubble(
    text: str,
    ms: int = 2600,
    *,
    kind: str = "",
    emote: str = "",
    sticky: bool = False,
) -> dict:
    try:
        body = str(text if text is not None else "").strip()
    except Exception:
        body = ""
    if not body:
        return {"ok": False, "msg": "气泡内容为空"}
    if len(body) > _BUBBLE_MAX_CHARS:
        body = body[:_BUBBLE_MAX_CHARS]
    try:
        hold = int(ms)
    except (TypeError, ValueError):
        hold = _BUBBLE_DEFAULT_MS
    hold = max(_BUBBLE_MIN_MS, min(_BUBBLE_MAX_MS, hold))

    payload: dict = {"text": body, "ms": hold}
    if kind:
        payload["kind"] = str(kind).strip()
    if emote:
        payload["emote"] = str(emote).strip()
    if sticky:
        payload["sticky"] = True
    return _write_pet_stdin(payload, check_external_pid=True)


def clear_bubble(kinds: Optional[list[str] | tuple[str, ...]] = None) -> dict:
    """通知运行中的桌宠收起指定类型（或全部）的提醒气泡。"""
    payload: dict = {"clear": True}
    if kinds:
        clean = [str(k).strip() for k in kinds if str(k).strip()]
        if clean:
            payload["kinds"] = clean
    return _write_pet_stdin(payload, check_external_pid=False)


def ensure_started_if_enabled() -> dict:
    """星瑶启动时的入口：配置里打开了 pet_enabled 才拉起桌宠。"""
    # 与设置页写配置走同一份 config.json / 同一把键名（config.py 已登记
    # pet_enabled、pet_electron_path）；这里只读，导入 save_config_value 是为了
    # 明确依赖通道一致，避免以后有人另建一套存储。
    try:
        from config import save_config_value  # noqa: F401
        import config
    except Exception:
        logger.debug("config 模块不可用，桌宠开关按关闭处理", exc_info=True)
        config = None

    enabled = False
    if config is not None:
        try:
            enabled = bool(getattr(config, "pet_enabled", False))
        except Exception:
            enabled = False
    if not enabled:
        enabled = bool(_config_value("pet_enabled", False) or False)

    if not enabled:
        return {"ok": True, "msg": "桌宠未启用，跳过启动", "skipped": True, "status": _safe_status()}

    result = start()
    result["skipped"] = False
    return result


def shutdown() -> None:
    global _PROC
    try:
        if _PROC is None:
            return
        stop()
    except Exception:
        logger.debug("退出时清理桌宠失败", exc_info=True)


def _register_atexit() -> None:
    global _atexit_registered
    if _atexit_registered:
        return
    try:
        atexit.register(shutdown)
        _atexit_registered = True
    except Exception:
        logger.debug("注册桌宠 atexit 清理失败", exc_info=True)


_register_atexit()

__all__ = [
    "PET_DIR",
    "get_status",
    "start",
    "stop",
    "push_bubble",
    "clear_bubble",
    "ensure_started_if_enabled",
    "shutdown",
]
