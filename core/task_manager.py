import json
import logging
import os
import re
import subprocess
import threading
import time
import uuid

from core.process_io import decode_process_output, platform_console_encoding

_logger = logging.getLogger("py.warnings")

# 任务管理器全局内存状态（所有读写由 _TASKS_LOCK 保护）
ACTIVE_TASKS = {}
_TASKS_LOCK = threading.RLock()

# 任务日志目录（懒初始化：首次使用时按当前 cwd 解析，避免模块导入期副作用）
LOG_DIR = None
TASK_DB_PATH = None
_LOADED = False

# get_task_output 只从日志尾部读取的字节数上限，避免 GB 级日志撑爆内存
_TAIL_READ_BYTES = 256 * 1024
# 最多保留的历史任务日志数量（超出后按修改时间滚动清理）
_MAX_TASK_LOGS = 50

# get_task_output 的 task_id 白名单，防止路径穿越读取任意日志文件
_TASK_ID_PATTERN = re.compile(r"^[\w-]{1,64}$")

from core.command_policy import is_dangerous_command

# 兼容已有内部调用和测试；策略实现集中在 command_policy。
_is_dangerous_command = is_dangerous_command


def _ensure_paths():
    """按需解析日志目录与任务库路径（首次使用时按当前 cwd 确定）。"""
    global LOG_DIR, TASK_DB_PATH
    if LOG_DIR is None:
        LOG_DIR = os.path.abspath(os.path.join(os.getcwd(), ".ycode_tasks"))
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
        except OSError as error:
            _logger.warning("无法创建任务日志目录 %s: %s", LOG_DIR, error)
    if TASK_DB_PATH is None:
        TASK_DB_PATH = os.path.join(LOG_DIR, "tasks.json")


def _ensure_loaded():
    """首次使用时从磁盘接管历史任务，替代模块导入期的立即读盘。"""
    global _LOADED
    # 整个加载过程持锁（_TASKS_LOCK 为 RLock，_load_tasks 内可重入），
    # 避免置位 _LOADED 后锁外加载期间其他线程读到未完成的状态
    with _TASKS_LOCK:
        if _LOADED:
            return
        _LOADED = True
        _load_tasks()


def is_pid_running(pid):
    """检查 PID 对应的进程是否仍在系统后台运行"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    try:
        import psutil
        return psutil.pid_exists(pid)
    except ImportError:
        try:
            if os.name == 'nt':
                # Windows tasklist 兜底检测（列表参数，不做 shell 插值）；
                # 用 CSV 输出精确比较第二列 PID，避免 123 子串误匹配 1234
                out = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                    capture_output=True, text=True, timeout=5,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                for line in out.stdout.splitlines():
                    if not line.strip():
                        continue
                    columns = [column.strip().strip('"') for column in line.split('","')]
                    if len(columns) >= 2 and columns[1] == str(pid):
                        return True
                return False
            else:
                # Unix kill -0 信号检测
                os.kill(pid, 0)
                return True
        except Exception:
            return False


def _process_create_time(pid):
    """返回进程启动时间戳；无法获取时返回 None（用于 PID 复用校验）。"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    try:
        import psutil
        return psutil.Process(pid).create_time()
    except Exception:
        return None


def _process_cmdline(pid):
    """返回进程命令行；无法获取时返回 None（用于 PID 复用兜底校验）。"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    try:
        import psutil
        return " ".join(psutil.Process(pid).cmdline() or [])
    except Exception:
        return None


def _load_tasks():
    """从本地 JSON 文件中反序列化加载任务记录，用于跨 Y-code 重启的生命周期接管"""
    _ensure_paths()
    if not os.path.exists(TASK_DB_PATH):
        return
    try:
        with open(TASK_DB_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception as error:
        _logger.warning("读取任务记录失败 %s: %s", TASK_DB_PATH, error)
        return
    with _TASKS_LOCK:
        for tid, info in data.items():
            if tid in ACTIVE_TASKS:
                continue
            # 防御手工编辑或旧版本格式导致字段缺失，避免 KeyError 击穿整个任务系统
            if not isinstance(info, dict):
                _logger.warning("忽略无效任务记录 %s: %s", tid, info)
                continue
            command = info.get("command")
            pid = info.get("pid")
            start_time = info.get("start_time")
            if command is None or pid is None or start_time is None:
                _logger.warning("忽略不完整任务记录 %s: %s", tid, info)
                continue
            ACTIVE_TASKS[tid] = {
                "id": tid,
                "command": command,
                "pid": pid,
                "start_time": start_time,
                "create_time": info.get("create_time"),
                "log_file_path": os.path.join(LOG_DIR, f"task_{tid}.log"),
                "process": None,     # 重建的任务 Popen 对象为 None，依赖 PID 检测
                "log_handle": None,  # 文件句柄在需要时懒加载/写入时关闭
                "output_encoding": (
                    info.get("output_encoding")
                    or platform_console_encoding()
                ),
            }


def _save_tasks():
    """将当前的活跃任务持久化写入磁盘 JSON 库（原子写，防止崩溃时文件损坏）"""
    _ensure_paths()
    try:
        with _TASKS_LOCK:
            data = {}
            for tid, info in ACTIVE_TASKS.items():
                pid = info.get("pid") or (info["process"].pid if info.get("process") else None)
                if pid:
                    data[tid] = {
                        "command": info["command"],
                        "pid": pid,
                        "start_time": info["start_time"],
                        "create_time": info.get("create_time"),
                        "output_encoding": (
                            info.get("output_encoding")
                            or platform_console_encoding()
                        ),
                    }
            # 先写临时文件，成功后再原子替换目标文件，
            # 避免写到一半进程崩溃或磁盘满时 tasks.json 损坏。
            tmp_path = TASK_DB_PATH + ".tmp"
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, TASK_DB_PATH)
    except Exception as error:
        _logger.warning("写入任务记录失败 %s: %s", TASK_DB_PATH, error)


def start_background_task(command: str) -> str:
    """在后台开启一个异步长周期任务（如 npm run dev），重定向日志输出"""
    _ensure_paths()
    _ensure_loaded()
    if _is_dangerous_command(command):
        raise RuntimeError(f"拒绝执行包含危险模式的后台命令：{command}")
    # 8 位 uuid 有碰撞可能，若目标日志已存在则重新生成
    for _ in range(10):
        task_id = str(uuid.uuid4())[:8]
        log_file_path = os.path.join(LOG_DIR, f"task_{task_id}.log")
        if not os.path.exists(log_file_path) and task_id not in ACTIVE_TASKS:
            break
    else:
        raise RuntimeError("无法生成唯一任务 ID")

    log_file = None
    try:
        # 打开日志文件
        log_file = open(log_file_path, "wb")

        # 注入环境变量，强制让 Python 的输出不进行缓冲，实时写入日志文件
        env = os.environ.copy()
        output_encoding = platform_console_encoding()
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = output_encoding
        env["PYTHONUTF8"] = "0"

        # 启动后台进程
        # stdout 和 stderr 都合并写入日志文件
        # shell=True 以支持复杂的脚本串联，creationflags 为后台启动做优化
        try:
            process = subprocess.Popen(
                command,
                shell=True,
                stdout=log_file,
                stderr=log_file,
                env=env,
                creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW) if os.name == 'nt' else 0,
                start_new_session=os.name != "nt",
            )
        except Exception:
            # Popen 失败时关闭日志句柄，避免文件句柄泄露
            log_file.close()
            log_file = None
            raise

        with _TASKS_LOCK:
            ACTIVE_TASKS[task_id] = {
                "id": task_id,
                "command": command,
                "process": process,
                "pid": process.pid,
                "create_time": _process_create_time(process.pid),
                "log_file_path": log_file_path,
                "log_handle": log_file,
                "start_time": time.time(),
                "output_encoding": output_encoding,
            }
            _save_tasks()
        _prune_old_logs()
        return task_id
    except Exception as e:
        if log_file is not None:
            try:
                log_file.close()
            except Exception:
                pass
        raise RuntimeError(f"启动后台任务失败: {e}")


def _prune_old_logs():
    """滚动清理历史任务日志，只保留最近 _MAX_TASK_LOGS 个（活跃任务日志除外）。"""
    try:
        names = [
            name for name in os.listdir(LOG_DIR)
            if name.startswith("task_") and name.endswith(".log")
        ]
        if len(names) <= _MAX_TASK_LOGS:
            return
        with _TASKS_LOCK:
            active_logs = {
                os.path.basename(info.get("log_file_path") or "")
                for info in ACTIVE_TASKS.values()
            }
        names.sort(key=lambda name: os.path.getmtime(os.path.join(LOG_DIR, name)))
        remaining = len(names)
        for name in names:
            if remaining <= _MAX_TASK_LOGS:
                break
            if name in active_logs:
                continue
            try:
                os.remove(os.path.join(LOG_DIR, name))
                remaining -= 1
            except OSError:
                pass
    except Exception as error:
        _logger.warning("清理历史任务日志失败: %s", error)


def list_active_tasks():
    """返回当前所有活跃的任务列表，具备死亡状态自动清理与磁盘存盘同步"""
    _ensure_paths()
    results = []
    # 强制重新从本地加载以接管外部常驻进程，同时自动清理已死亡进程
    _load_tasks()

    dirty = False
    with _TASKS_LOCK:
        for tid, info in list(ACTIVE_TASKS.items()):
            proc = info.get("process")
            pid = info.get("pid")

            # 判定进程是否已经彻底结束
            is_dead = False
            if proc is not None:
                poll = proc.poll()
                if poll is not None:
                    is_dead = True
            elif pid is not None:
                if not is_pid_running(pid):
                    is_dead = True
                else:
                    # PID 复用防护：磁盘重建任务需校验进程启动时间，
                    # 否则无关进程复用 PID 会让死任务永远挂在活跃列表里
                    recorded_create_time = info.get("create_time")
                    current_create_time = _process_create_time(pid)
                    if (
                        recorded_create_time is not None
                        and current_create_time is not None
                        and abs(current_create_time - recorded_create_time) > 2
                    ):
                        is_dead = True
            else:
                is_dead = True

            if is_dead:
                # 进程已死，关闭文件句柄并从活跃库移除
                try:
                    if info.get("log_handle"):
                        info["log_handle"].close()
                except Exception as error:
                    _logger.warning("关闭任务日志句柄失败 %s: %s", tid, error)
                del ACTIVE_TASKS[tid]
                dirty = True
            else:
                running_time = int(time.time() - info["start_time"])
                results.append({
                    "task_id": tid,
                    "command": info["command"],
                    "pid": pid or (proc.pid if proc else -1),
                    "duration": f"{running_time}秒"
                })

        if dirty:
            _save_tasks()

    return results


def get_task_output(task_id: str, tail_lines: int = 30) -> str:
    """读取指定任务的日志输出尾部"""
    _ensure_paths()
    if not _TASK_ID_PATTERN.match(str(task_id or "")):
        return f"🚨 无效的任务 ID: {task_id}"
    log_file_path = os.path.join(LOG_DIR, f"task_{task_id}.log")
    if not os.path.exists(log_file_path):
        return f"🚨 未找到任务 {task_id} 的输出日志。"

    try:
        with _TASKS_LOCK:
            info = ACTIVE_TASKS.get(task_id) or {}
            handle = info.get("log_handle")
            output_encoding = info.get("output_encoding") or platform_console_encoding()
        # 强制刷盘以确保读取到最新内容
        if handle:
            try:
                handle.flush()
            except Exception:
                pass

        with open(log_file_path, "rb") as f:
            # 只从文件尾部读取有限字节，避免 GB 级日志全量载入内存
            f.seek(0, os.SEEK_END)
            size = f.tell()
            truncated = size > _TAIL_READ_BYTES
            f.seek(max(0, size - _TAIL_READ_BYTES))
            raw = f.read()
        output = decode_process_output(raw, output_encoding)
        lines = output.splitlines(keepends=True)
        if truncated and lines:
            # 丢弃被截断的首行残片
            lines = lines[1:]
        tail = lines[-tail_lines:] if len(lines) > tail_lines else lines
        result = "".join(tail)
        if truncated:
            result = f"... (日志超过 {_TAIL_READ_BYTES // 1024} KB，仅显示末尾内容)\n{result}"
        return result
    except Exception as e:
        return f"🚨 读取日志发生异常: {e}"


def _kill_posix_tree(pid: int, proc=None) -> bool:
    """POSIX 下尝试终止进程树；优先使用 psutil，否则回退到单进程信号。"""
    try:
        import psutil
        parent = psutil.Process(pid)
        children = parent.children(recursive=True)
        for child in children:
            try:
                child.terminate()
            except Exception:
                pass
        parent.terminate()
        gone, alive = psutil.wait_procs(children + [parent], timeout=5)
        for p in alive:
            try:
                p.kill()
            except Exception:
                pass
        return not is_pid_running(pid)
    except ImportError:
        pass
    except Exception as error:
        _logger.warning("psutil 终止进程树失败 %s: %s", pid, error)

    # 无 psutil 回退：先 SIGTERM，再给几秒自行结束，仍存活则 SIGKILL
    try:
        os.kill(pid, 15)
    except OSError:
        if proc:
            proc.terminate()
    if proc is not None:
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
    else:
        for _ in range(50):
            if not is_pid_running(pid):
                break
            time.sleep(0.1)
        if is_pid_running(pid):
            try:
                os.kill(pid, 9)
            except OSError:
                pass
    return not is_pid_running(pid)


def kill_background_task(task_id: str) -> bool:
    """终止后台运行的指定任务；按实际结果返回成功与否"""
    # 刷新状态，确保最新的 tasks.json 已被同步
    list_active_tasks()

    with _TASKS_LOCK:
        info = ACTIVE_TASKS.get(task_id)
    if info is None:
        return False

    proc = info.get("process")
    pid = info.get("pid")

    killed = False
    try:
        if pid:
            pid = int(pid)
            # PID 复用防护：进程启动时间与记录不一致，说明原进程已死、PID 被无关进程复用
            recorded_create_time = info.get("create_time")
            current_create_time = _process_create_time(pid)
            pid_reused = (
                recorded_create_time is not None
                and current_create_time is not None
                and abs(current_create_time - recorded_create_time) > 2
            )
            if not pid_reused and proc is None:
                if recorded_create_time is not None and current_create_time is None:
                    # psutil 缺失或权限不足导致无法校验进程身份时保守拒绝 kill，
                    # 只清理任务记录，避免误杀复用 PID 的无关进程
                    pid_reused = True
                elif recorded_create_time is None:
                    # 旧记录缺少启动时间：用命令行匹配兜底，避免误杀复用 PID 的无关进程；
                    # 命令行同样无法获取（psutil 缺失）时保守拒绝 kill
                    cmdline = _process_cmdline(pid)
                    if cmdline is None or str(info.get("command", ""))[:30] not in cmdline:
                        pid_reused = True
            if pid_reused:
                # 视为任务已结束，只清理记录，不执行 taskkill
                killed = False
            elif not is_pid_running(pid):
                # 进程已自行结束
                killed = True
            elif os.name == 'nt':
                # Windows 下利用 taskkill 杀死整个进程树（列表参数，不做 shell 插值）
                result = subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    capture_output=True, timeout=15,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                killed = result.returncode == 0 or not is_pid_running(pid)
            else:
                # POSIX：尝试终止整个进程树并等待实际结束，避免壳进程死了孙子进程仍存活
                killed = _kill_posix_tree(pid, proc)
        elif proc:
            proc.terminate()
            killed = True
    except Exception as error:
        _logger.warning("终止后台任务失败 %s: %s", task_id, error)
        try:
            if proc:
                proc.kill()
                killed = True
        except Exception:
            killed = False

    try:
        if info.get("log_handle"):
            info["log_handle"].close()
    except Exception as error:
        _logger.warning("关闭任务日志句柄失败 %s: %s", task_id, error)

    with _TASKS_LOCK:
        ACTIVE_TASKS.pop(task_id, None)
        _save_tasks()
    return killed
