"""星瑶桌面宠物（Electron 桌宠 xingyao_pet）的进程生命周期管理。

为什么要单独成一个模块：
- 桌宠是一个独立 Electron 进程，pywebview 只是它的"开关面板"。把 Popen、
  探活、清理集中在这里，js_api 层只做参数透传，不必重复实现进程管理细节，
  也保证任何异常都转成 {"ok": False, "msg": ...} 返回，而不会穿透到 pywebview
  的 js_api 线程（那会让整个界面失去响应）。
- 单实例：桌宠 main.js 里调了 app.requestSingleInstanceLock()，同一时刻只允许
  一个实例，后来者会自己退出。所以既不能盲目重复拉起（第二个进程会"启动成功
  但立刻退出"），拉起后立刻退出时也要把这条最可能的原因提示出来；模块级 _LOCK
  保证设置页连点两次不会并发拉起两个进程。
- 探活不引入 psutil：只用 tasklist（Windows 自带）按 PID 查进程名是否含
  electron，避免为一个状态查询多一个依赖；tasklist 不可用时退化成"有 pid 记录
  就当存活"，宁可多给一次 terminate，也不要因为查不到就崩。
- 句柄 + pid 文件双轨：本进程拉起的实例直接用 Popen 句柄 poll/terminate；跨进程
  （上次异常退出留下的实例）只能靠 ~/.ycode/pet.pid 的记录去 taskkill。
- 除了"管理进程"，还留了一条单向写通道：push_bubble 往桌宠 stdin 写一行 UTF-8
  JSON（{"text": 文案, "ms": 停留毫秒}），桌宠读到就冒一个气泡提示（如"正在读
  文件…"）。方向与 stdout/stderr 相反（父写子读），所以 stdin 用 PIPE 是安全的。
"""

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

# 本进程拉起的桌宠进程句柄。模块级单例：星瑶是单进程应用，桌宠只应有一个实例。
_PROC: Optional[subprocess.Popen] = None

# 关键段互斥（start/stop/get_status 全走这把锁；用 RLock 是因为 start/stop 内部
# 还会再调一次 get_status 取最新状态）。
_LOCK = threading.RLock()

# 启动后等待窗口：Electron 冷启动（首次扫描 resources、加载 node_modules）通常
# 0.4~0.8s 就稳定，1.2s 足够覆盖慢机器，又不至于让面板点击明显卡顿。
_START_WAIT = 1.2
# 优雅退出等待：Electron 收到关闭请求后还要落地 pet-state.json，4s 足够；超时才
# taskkill /F /T 强杀整棵进程树（Electron 会派生 GPU/renderer 子进程，不带 /T
# 会留下孤儿进程，继续占着单实例锁让下次启动失败）。
_STOP_WAIT = 4.0

# Windows 下不弹控制台窗口；非 Windows 该常量不存在，取 0。
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_atexit_registered = False


# ---------- 路径解析 ----------
def _pet_dir_candidates():
    """按优先级给出桌宠目录候选：环境变量 → 打包后位置 → 源码树。"""
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
    """返回第一个真实存在的候选目录；都不存在时返回优先级最高的那个（供报错展示）。"""
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
    """桌宠目录（等价于 _pet_dir()），供其它模块复用同一套路径解析规则。

    core/pet_skins.py 也要按同样规则解析桌宠目录（内置星瑶的 assets / 配置都相对
    它定位）。公开这个函数是为了不重复实现路径优先级（环境变量 → 打包位置 →
    源码树）：两处各写一遍，改安装布局时必然有一处悄悄失效。
    """
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
    # 字段缺失/为 None 时逐项回落；assets_dir、config_path 允许为空（含义是"用内置
    # 那份"），由调用方决定不传，这里不擅自填成内置路径免得名字与形象错配。
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
    """读单个配置项（先看模块级属性，再回落 config.json 的载荷）。

    config 模块的 __getattr__ 对未注册名字会抛 AttributeError，这里全部吞掉：
    读不到配置只影响"用默认路径"，不该让桌宠开关整体失败。
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


def _runtime_electron_candidates():
    """用户级运行时目录下的 electron 候选（安装包分发的用户走的就是这一份）。

    路径定义只认 core/pet_runtime.py 的 DIST_DIR：运行时"装到哪"和这里"去哪找"
    必须是同一个常量，否则以后改安装根，这里会悄悄失效却没人发现。
    导入失败（裁剪构建、循环导入等）时返回空列表：只是少一条候选、回落到 PATH，
    绝不能让桌宠开关因为 import 问题整体报错。
    """
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
    """找 Electron 可执行文件，按以下顺序逐级回落，返回第一个真实存在的文件：

    1. 显式参数 electron_path（用户在设置页手填的绝对路径）；
    2. 配置项 pet_electron_path（持久化的同一条路径）；
    3. 桌宠包内自带：<桌宠目录>/node_modules/electron/dist/electron(.exe)
       ——源码开发机 npm install 过就有这一份，优先用它是为了避免多下一遍；
    4. 用户级运行时目录：~/.ycode/pet-runtime/electron/dist/electron(.exe)
       ——安装包只带桌宠源码，用户点开关时由 core/pet_runtime.py 下载安装的那份
       （路径取自 pet_runtime.DIST_DIR，不在这里另写一遍）；
    5. 最后才回落到 PATH 里的 electron 命令（全局安装的情况）。

    全都找不到时返回 None，由调用方给出"填路径 / 装 electron"的可操作指引。
    """
    candidates = []
    for raw in (explicit, _config_value("pet_electron_path", "") or ""):
        raw = str(raw or "").strip().strip('"')
        if raw:
            candidates.append(Path(raw))
    pet = _pet_dir()
    dist = pet / "node_modules" / "electron" / "dist"
    candidates.append(dist / "electron.exe")
    candidates.append(dist / "electron")
    # 包内没有（安装包场景）就回落到用户级自动下载目录，最后才是 PATH。
    candidates.extend(_runtime_electron_candidates())
    for cand in candidates:
        try:
            if cand.is_file():
                return cand
        except OSError:
            continue
    found = shutil.which("electron")
    return Path(found) if found else None


# ---------- pid 记录 ----------
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
        # pid 记录只是"跨进程探活"的辅助手段，写失败不该让启动整体失败。
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
    """pid 是否指向一个存活的 electron 进程。

    找不到 tasklist（非 Windows / 精简系统）时只能按 pid 假设存活：false negative
    会让 stop 放弃清理，false positive 最多多打一次 taskkill，后者代价更小。
    """
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
        # 无输出说明 PID 不存在（tasklist 对不存在的 PID 只会把提示打到 stderr）。
        return False
    return "electron" in out


# ---------- 对外接口 ----------
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
            # 当前激活的桌宠形象：前端设置页可直接显示，不必自己再调 pet_skins。
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
    """异常路径也要能带上 status：连状态都查不出来时给个最小骨架。"""
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
    # 关键：ELECTRON_RUN_AS_NODE 会让 electron.exe 退化成一个 node 解释器去执行
    # 文件名参数，窗口永远不会出现，且进程能一直活着骗过探活。
    env.pop("ELECTRON_RUN_AS_NODE", None)
    # 当前皮肤通过环境变量传给渲染层：桌宠那边只认环境变量，路径解析（环境变量 →
    # 打包位置 → 源码树）全部留在 Python 侧，避免 Electron 里再写一套目录优先级。
    skin = _active_skin_safe()
    env["YCODE_PET_SKIN"] = str(skin.get("id") or "xingyao")
    env["YCODE_PET_NAME"] = str(skin.get("name") or "星瑶")
    assets_dir = str(skin.get("assets_dir") or "")
    if assets_dir:
        # 空串不传：Electron 收到空值不会自己回落，不设置该键才是"用内置那份"。
        env["YCODE_PET_ASSETS"] = assets_dir
    config_path = str(skin.get("config_path") or "")
    if config_path:
        env["YCODE_PET_CONFIG"] = config_path

    proc = subprocess.Popen(
        [str(electron), str(pet)],
        cwd=str(pet),
        env=env,
        # stdout/stderr 用 DEVNULL 而不是 PIPE：这两路是「子进程写、父进程读」，
        # 没人读的管道写满后会把子进程阻塞死。stdin 方向正好相反（父进程写、
        # 子进程读），且是 push_bubble 推气泡的唯一通道；父进程每次只写一行几十
        # 字节并立刻 flush，不存在「写满阻塞子进程」的风险，所以 stdin 用 PIPE。
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

    # 启动即退出：最常见是单实例锁（已有实例在跑）或运行时资源缺失。
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
    """等到进程真正消失（或超时）。句柄退出不够：Electron 的 pid 可能还被
    子进程继承着，统一用 tasklist 复核一次。"""
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

    # 1) 先礼：有句柄就 terminate，只有 pid 就走 taskkill / SIGTERM。
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
        # 2) 后兵：强杀整棵进程树，避免 GPU/renderer 子进程残留占着单实例锁。
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


# 气泡参数边界：文案太长会把气泡撑出屏幕，停留时间太短看不清、太长挡住界面。
_BUBBLE_MAX_CHARS = 80
_BUBBLE_DEFAULT_MS = 2600
_BUBBLE_MIN_MS = 800
_BUBBLE_MAX_MS = 8000


def _drop_dead_proc_locked() -> None:
    """丢掉已死的进程句柄，避免后续 get_status 继续把它当"运行中"。

    调用方须持有 _LOCK。只清理 pid 记录与这个死句柄 pid 相同的情况：若记录已被
    别的实例覆盖，说明那是用户手动开的桌宠，不能顺手删掉。
    """
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


def push_bubble(text: str, ms: int = 2600) -> dict:
    """往桌宠推一条气泡提示（"正在读文件…"这类状态字）。

    通道：往桌宠 stdin 写一行 UTF-8 JSON {"text": ..., "ms": ...} + "\\n"。桌宠侧
    把 fd 0 当作一行一帧的 JSON 流来读（Electron 的 process.stdin 在 Windows 上会
    立刻 EOF，所以那边直接用 net.Socket 接管 fd 0），写完即返回，不等它回包。

    只对"本进程拉起的实例"有效：跨进程遗留的实例拿不到 stdin 句柄，只能报错提示。
    任何异常都转成 {"ok": False, "msg": ...}，绝不外抛（调用方可能正在 js_api 线程）。
    """
    # 1) 先看本进程有没有活句柄。锁内只做 poll，不跑 tasklist 这类等待型操作。
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
        # 锁外探活（tasklist 最慢要几百毫秒），别让它拖住 start/stop。
        record = _read_pid_record()
        if record and _pid_alive(record["pid"]):
            return {"ok": False, "msg": "桌宠不是本进程拉起的，无法推送提示"}
        return {"ok": False, "msg": "桌宠未在运行"}

    # 2) 内容规整：空串直接拒；超长截断（气泡宽度有限）；停留时间钳进合理区间。
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

    # 3) 真正写：一行几十字节 + flush，锁内耗时可以忽略，也绝不等待子进程。
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
            payload = json.dumps({"text": body, "ms": hold}, ensure_ascii=False) + "\n"
            stream.write(payload.encode("utf-8"))
            stream.flush()
            return {"ok": True, "msg": "ok"}
        except Exception as exc:
            # BrokenPipeError / ValueError / 管道已关：句柄已经不可信，清掉再报错。
            logger.debug("推送桌宠气泡失败: %s", exc, exc_info=True)
            _drop_dead_proc_locked()
            return {"ok": False, "msg": f"推送失败：{exc}"}


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
            # config 模块 __getattr__ 对未注册名字会抛 AttributeError，getattr 带默认值兜住。
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
    """星瑶退出时的清理入口：只停本进程拉起的桌宠。

    不动 pid 记录里属于"别人"的实例（用户可能手动开着桌宠），避免退出星瑶
    时把用户的桌宠一起带走。
    """
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
    "ensure_started_if_enabled",
    "shutdown",
]
