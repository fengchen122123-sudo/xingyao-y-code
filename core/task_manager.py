import json
import logging
import os
import re
import subprocess
import threading
import time
import uuid

from collections import deque
from dataclasses import dataclass

from core.process_io import decode_process_output, platform_console_encoding, strip_ansi_escapes
from core.workspace_context import get_workdir

_logger = logging.getLogger("py.warnings")

# 任务管理器全局内存状态（所有读写由 _TASKS_LOCK 保护）
ACTIVE_TASKS = {}
_TASKS_LOCK = threading.RLock()

# 本进程显式删除（kill/死亡清理）的任务 ID 墓碑：_save_tasks 做跨进程合并读时，
# 用于区分"其他进程新启动的任务"（合并保留）与"本进程已删除的任务"（防止复活）
_REMOVED_TIDS = set()
_REMOVED_TIDS_FIFO = deque()
_REMOVED_TIDS_CAP = 512

# 任务生命周期状态：running 之外均为终态（terminal）
TASK_STATUS_RUNNING = "running"
TASK_STATUS_COMPLETED = "completed"
TASK_STATUS_FAILED = "failed"
TASK_STATUS_TIMED_OUT = "timed_out"
TASK_STATUS_CANCELLED = "cancelled"
_TERMINAL_STATUSES = frozenset({
    TASK_STATUS_COMPLETED, TASK_STATUS_FAILED,
    TASK_STATUS_TIMED_OUT, TASK_STATUS_CANCELLED,
})
# 状态变更条件变量：revision 递增与 notify_all 必须在同一把锁内完成，
# manage_task(wait) 的等待方借此在状态/输出变化时被即时唤醒，而非固定轮询忙等
_TASKS_COND = threading.Condition(_TASKS_LOCK)
# 终态任务保留上限，防止 ACTIVE_TASKS 无限增长
_TERMINAL_KEEP_CAP = 64


@dataclass(frozen=True)
class TaskStatusSnapshot:
    """后台任务某一时刻的不可变状态快照；revision 单调递增，仅在可观察状态变化时增长。"""

    task_id: str
    status: str
    revision: int
    terminal: bool
    output: str
    exit_code: int | None


def _record_removed_locked(task_id: str) -> None:
    """记录本进程移除的任务 ID，并以 FIFO 保留有界墓碑。"""
    if task_id in _REMOVED_TIDS:
        return
    while len(_REMOVED_TIDS) >= _REMOVED_TIDS_CAP:
        _REMOVED_TIDS.discard(_REMOVED_TIDS_FIFO.popleft())
    _REMOVED_TIDS.add(task_id)
    _REMOVED_TIDS_FIFO.append(task_id)

# 任务日志目录（懒初始化：首次使用时按当前 cwd 解析，避免模块导入期副作用）
LOG_DIR = None
TASK_DB_PATH = None
_LOADED = False

# get_task_output 只从日志尾部读取的字节数上限，避免 GB 级日志撑爆内存
_TAIL_READ_BYTES = 256 * 1024
# 日志"仅有启动标记、尚无实际命令输出"的判定阈值：turn 标记行约 30 字节，
# 低于该值说明命令还没有任何输出落盘（list_active_tasks 据此给 GUI 面板
# 提供 no_output_yet 字段，运行中提示由前端渲染）
_STARTUP_MARKER_MAX_BYTES = 64
# 最多保留的历史任务日志数量（超出后按修改时间滚动清理）
_MAX_TASK_LOGS = 50

# get_task_output 的 task_id 白名单，防止路径穿越读取任意日志文件；
# 必须用 fullmatch 校验（去掉 ^/$ 锚点，避免 $ 放行 "abc\n" 这类尾随换行）
_TASK_ID_PATTERN = re.compile(r"[\w-]{1,64}")

from core.command_policy import (
    build_child_env,
    is_dangerous_command,
    load_shell_env_policy,
    redact_sensitive_text,
)

# 兼容已有内部调用和测试；策略实现集中在 command_policy。
_is_dangerous_command = is_dangerous_command

# 后台命令任务完成回调（GUI 注册，用于任务退出后向前端推送通知）
_on_task_finished = None


def set_task_finished_callback(callback):
    """注册后台命令任务完成回调。

    签名：callback(task_id, command, returncode, session_id, log_tail)。
    """
    global _on_task_finished
    _on_task_finished = callback


def _read_log_tail(task_id, max_chars=2000):
    """读取后台任务日志尾部（供 LLM 简短总结用），失败返回空串。"""
    try:
        log_file_path = os.path.join(LOG_DIR, f"task_{task_id}.log")
        if not os.path.exists(log_file_path):
            return ""
        with open(log_file_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_chars))
            raw = f.read()
        from core.process_io import decode_process_output, platform_console_encoding
        # 日志是子进程原始字节，可能带 ANSI 颜色码；log_tail 直接进 LLM 上下文，先清洗
        return strip_ansi_escapes(
            decode_process_output(raw, platform_console_encoding())
        )
    except Exception:
        return ""


def _watch_task_completion(task_id, process, command, session_id):
    """监听线程：0.5s 节拍同步任务状态（日志大小/进程退出），退出后触发完成回调（仅一次）。

    每拍在 _TASKS_COND 内执行 _sync_task_locked：日志 size 变化或进程退出都会
    bump revision 并 notify_all，manage_task(wait) 的等待方据此即时被唤醒。
    """
    returncode = None
    while True:
        try:
            returncode = process.poll()
        except Exception as error:
            _logger.warning("轮询后台任务失败 %s: %s", task_id, error)
            # Snapshot 用 None 表示无法取得真实退出码；回调必须有非零失败码，
            # 否则 GUI 的 int(returncode or 0) 会将异常误报为成功。
            returncode = -1
            with _TASKS_COND:
                info = ACTIVE_TASKS.get(task_id)
                if info is not None and info.get("process") is process:
                    _transition_terminal_locked(
                        task_id, info, TASK_STATUS_FAILED, None
                    )
                    _save_tasks()
            break
        with _TASKS_COND:
            info = ACTIVE_TASKS.get(task_id)
            if info is not None and info.get("process") is process:
                _sync_task_locked(task_id, info)
                if returncode is None and info.get("status") in _TERMINAL_STATUSES:
                    # 外层 poll 与同步 poll 之间也可能退出或抛错。同步已经
                    # 落定终态时必须在本拍持久化、退出并回调，不能进入下一拍。
                    returncode = info.get("exit_code")
                    if returncode is None:
                        returncode = -1
                if returncode is not None:
                    # 进程退出后落盘一次：磁盘库只保留 running 任务
                    _save_tasks()
        if returncode is not None:
            break
        time.sleep(0.5)
    cb = _on_task_finished
    if cb is not None:
        try:
            cb(task_id, command, returncode, session_id, _read_log_tail(task_id))
        except Exception as error:
            _logger.warning("后台任务完成回调失败 %s: %s", task_id, error)
    # 进程注册表注销（幂等）：任务进入终态后不再阻塞 Git 面板操作
    try:
        from core.git import process_registry as _proc_registry

        _proc_registry.unregister_process(task_id)
    except Exception as error:
        _logger.warning("后台任务注销进程注册表失败 %s: %s", task_id, error)


def _ensure_paths():
    """按需解析日志目录与任务库路径（首次使用时按当前线程工作目录确定）。"""
    global LOG_DIR, TASK_DB_PATH
    if LOG_DIR is None:
        LOG_DIR = os.path.abspath(os.path.join(get_workdir(), ".ycode_tasks"))
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
            if tid in ACTIVE_TASKS or tid in _REMOVED_TIDS:
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
                "turn_id": info.get("turn_id") or "",
                "session_id": str(info.get("session_id") or ""),
                "status": TASK_STATUS_RUNNING,
                "exit_code": None,
                "revision": 0,
                "last_log_size": 0,
            }


def _save_tasks():
    """将当前的活跃任务持久化写入磁盘 JSON 库（原子写，防止崩溃时文件损坏）"""
    _ensure_paths()
    try:
        with _TASKS_LOCK:
            data = {}
            for tid, info in ACTIVE_TASKS.items():
                # 只落盘仍在运行的任务；终态任务仅保留在内存供快照/wait 查询（磁盘格式不变）
                if info.get("status", TASK_STATUS_RUNNING) != TASK_STATUS_RUNNING:
                    continue
                pid = info.get("pid") or (info["process"].pid if info.get("process") else None)
                if pid:
                    data[tid] = {
                        # 落盘前脱敏：内联凭据（export TOKEN=sk-...）不明文持久化
                        "command": redact_sensitive_text(info["command"]),
                        "pid": pid,
                        "start_time": info["start_time"],
                        "create_time": info.get("create_time"),
                        "output_encoding": (
                            info.get("output_encoding")
                            or platform_console_encoding()
                        ),
                        # T4 回合关联：父回合随任务记录持久化（旧记录无此键，读取侧 .get 兼容）
                        "turn_id": info.get("turn_id") or "",
                        # 会话归属随任务记录持久化：GUI 重启接管后任务仍归属发起会话，
                        # 执行条据此做会话过滤（旧记录无此键，读取侧 .get 兼容）
                        "session_id": str(info.get("session_id") or ""),
                    }
            # 跨进程合并读：GUI/CLI 双进程并发写同一份 tasks.json 时，
            # 把磁盘上其他进程新启动的任务记录合并进来，避免内存全量覆盖写
            # 互相丢失任务；本进程已删除的记录靠墓碑集合防止被合并步骤复活
            disk_data = None
            if os.path.exists(TASK_DB_PATH):
                try:
                    with open(TASK_DB_PATH, 'r', encoding='utf-8') as f:
                        disk_data = json.load(f)
                except Exception:
                    disk_data = None
            known_ids = set(ACTIVE_TASKS)
            if isinstance(disk_data, dict):
                for tid, info in disk_data.items():
                    if (
                        tid not in data
                        and tid not in known_ids
                        and tid not in _REMOVED_TIDS
                        and isinstance(info, dict)
                    ):
                        data[tid] = info
            # 先写临时文件，成功后再原子替换目标文件，
            # 避免写到一半进程崩溃或磁盘满时 tasks.json 损坏。
            tmp_path = TASK_DB_PATH + ".tmp"
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, TASK_DB_PATH)
    except Exception as error:
        _logger.warning("写入任务记录失败 %s: %s", TASK_DB_PATH, error)


def turn_short_code(turn_id: str | None) -> str:
    """回合短码：turn_id 前 8 位（与 turn_undo prompt_context 的展示惯例一致）。

    用于 /tasks 展示与任务日志标注；无值返回空串。
    """
    return str(turn_id or "")[:8]


def start_background_task(
    command: str, session_id: str = "", shell_id: str = "", turn_id: str = ""
) -> str:
    """在后台开启一个异步长周期任务（如 npm run dev），重定向日志输出。

    session_id 记录发起该任务所属的会话，完成回调据此触发 LLM 总结。
    turn_id 记录发起该任务的父回合（T4 回合关联：/tasks 展示与日志标注）。
    """
    _ensure_paths()
    _ensure_loaded()
    if _is_dangerous_command(command):
        raise RuntimeError(f"拒绝执行包含危险模式的后台命令：{command}")
    from core.command_shell import build_command_invocation
    invocation = build_command_invocation(command, shell_id=shell_id)
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
        # T1 安全默认：后台子进程默认不继承敏感环境变量（*KEY*/*SECRET*/*TOKEN*/*PASSWORD*），
        # 逃生门 config.json "shell_env_policy": {"exclude_sensitive": false}；
        # 实时日志所需的注入在 build_child_env 结果之上显式叠加。
        env = build_child_env(os.environ, policy=load_shell_env_policy())
        output_encoding = platform_console_encoding()
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = output_encoding
        env["PYTHONUTF8"] = "0"
        # 管道日志只认纯文本：禁用子进程彩色输出（vitest 等在 CI/FORCE_COLOR 下
        # 非 TTY 也会上色）。FORCE_COLOR 优先级高于 NO_COLOR，必须一并移除；
        # 读取侧 get_task_output 仍会兜底剥离，防止不认 NO_COLOR 的工具漏网
        env.setdefault("NO_COLOR", "1")
        env.pop("FORCE_COLOR", None)

        # 启动后台进程
        # stdout 和 stderr 都合并写入日志文件
        # 命令由系统设置选择的解释器承载；auto 保持平台默认 Shell 兼容行为。
        try:
            # cwd 跟随发起任务的会话工作目录（多会话并行时命令跑在各自项目下），
            # 不再隐式继承进程启动目录
            popen_kwargs = {
                "shell": invocation.use_system_shell,
                "stdout": log_file,
                "stderr": log_file,
                "env": env,
                "cwd": str(get_workdir()),
                "creationflags": (subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW) if os.name == 'nt' else 0,
                "start_new_session": os.name != "nt",
            }
            if invocation.executable:
                popen_kwargs["executable"] = invocation.executable
            process = subprocess.Popen(
                invocation.argv,
                **popen_kwargs,
            )
        except Exception:
            # Popen 失败时关闭日志句柄，避免文件句柄泄露
            log_file.close()
            log_file = None
            raise

        # T4 回合关联：任务日志首行标注父回合短码（副作用任务日志可追溯到回合）
        if turn_id:
            try:
                log_file.write(
                    f"[turn:{turn_short_code(turn_id)}] 后台任务启动\n".encode(
                        output_encoding, errors="replace"
                    )
                )
                log_file.flush()
            except Exception:
                pass

        with _TASKS_LOCK:
            ACTIVE_TASKS[task_id] = {
                "id": task_id,
                "command": command,
                "session_id": session_id,
                "turn_id": str(turn_id or ""),
                "process": process,
                "pid": process.pid,
                "create_time": _process_create_time(process.pid),
                "log_file_path": log_file_path,
                "log_handle": log_file,
                "start_time": time.time(),
                "output_encoding": output_encoding,
                "status": TASK_STATUS_RUNNING,
                "exit_code": None,
                "revision": 0,
                "last_log_size": 0,
            }
            _save_tasks()
        # Workspace Process Registry 登记（规格 §9.2）：后台任务为长期监听服务，
        # 登记失败只记日志，绝不影响任务本身
        try:
            from core.git import process_registry as _proc_registry

            _proc_registry.register_process(
                get_workdir(),
                handle=task_id,
                kind="background_task",
                foreground=False,
                long_running=True,
                risk_class=_proc_registry.risk_class_for_command(command),
            )
        except Exception as reg_error:
            _logger.warning("后台任务登记进程注册表失败 %s: %s", task_id, reg_error)
        _prune_old_logs()
        # 启动完成监听线程：进程退出后触发完成回调，向前端推送通知
        _watcher = threading.Thread(
            target=_watch_task_completion,
            args=(task_id, process, command, session_id),
            daemon=True,
            name=f"bg-task-watcher-{task_id}",
        )
        _watcher.start()
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


def _bump_revision_locked(info):
    """锁内递增任务 revision 并唤醒所有 wait 等待方（必须已持有 _TASKS_COND/_TASKS_LOCK）。"""
    info["revision"] = int(info.get("revision", 0)) + 1
    _TASKS_COND.notify_all()


def _transition_terminal_locked(task_id, info, status, exit_code) -> bool:
    """将 running 任务一次性转换为终态，并执行终态记录容量淘汰。"""
    if info.get("status") in _TERMINAL_STATUSES:
        return False
    info["status"] = status
    info["exit_code"] = exit_code
    _bump_revision_locked(info)
    _evict_terminal_locked()
    return True


def _sync_task_locked(tid, info):
    """锁内同步单个任务：日志大小变化/进程退出 → 状态与 revision。返回是否变化。"""
    changed = False
    log_file_path = info.get("log_file_path")
    if log_file_path:
        try:
            size = os.path.getsize(log_file_path)
        except OSError:
            size = info.get("last_log_size", 0)
    else:
        size = info.get("last_log_size", 0)
    if size != info.get("last_log_size", 0):
        info["last_log_size"] = size
        _bump_revision_locked(info)
        changed = True
    if info.get("status") == TASK_STATUS_RUNNING:
        exit_code = None
        dead = False
        poll_failed = False
        proc = info.get("process")
        if proc is not None:
            try:
                poll = proc.poll()
            except Exception as error:
                _logger.warning("轮询后台任务失败 %s: %s", tid, error)
                poll = None
                poll_failed = True
            if poll is not None:
                dead, exit_code = True, int(poll)
            elif poll_failed:
                dead = True
        elif info.get("pid") is not None:
            if not is_pid_running(info["pid"]):
                dead = True  # 重建任务无法取退出码，exit_code 保持 None
            else:
                # PID 复用防护：磁盘重建任务需校验进程启动时间，
                # 否则无关进程复用 PID 会让死任务永远挂在活跃列表里
                recorded_create_time = info.get("create_time")
                current_create_time = _process_create_time(info["pid"])
                if (
                    recorded_create_time is not None
                    and current_create_time is not None
                    and abs(current_create_time - recorded_create_time) > 2
                ):
                    dead = True
        else:
            dead = True
        if dead:
            status = (
                TASK_STATUS_FAILED if poll_failed
                else TASK_STATUS_COMPLETED if exit_code in (None, 0)
                else TASK_STATUS_FAILED
            )
            try:
                if info.get("log_handle"):
                    info["log_handle"].close()
            except Exception:
                pass
            changed = _transition_terminal_locked(tid, info, status, exit_code)
    return changed


def _snapshot_locked(tid, info, tail_lines=40) -> TaskStatusSnapshot:
    return TaskStatusSnapshot(
        task_id=tid,
        status=info.get("status", TASK_STATUS_RUNNING),
        revision=int(info.get("revision", 0)),
        terminal=info.get("status", TASK_STATUS_RUNNING) in _TERMINAL_STATUSES,
        output=get_task_output(tid, tail_lines=tail_lines),
        exit_code=info.get("exit_code"),
    )


def get_task_snapshot(task_id):
    """返回任务当前快照；任务不存在（含终态记录已淘汰）返回 None。"""
    _ensure_paths()
    _ensure_loaded()
    with _TASKS_COND:
        if _evict_terminal_locked():
            _save_tasks()
        info = ACTIVE_TASKS.get(task_id)
        if info is None:
            return None
        _sync_task_locked(task_id, info)
        if ACTIVE_TASKS.get(task_id) is not info:
            return None
        return _snapshot_locked(task_id, info)


def list_task_snapshots():
    """返回全部任务（含终态）的快照列表。"""
    _ensure_paths()
    _ensure_loaded()
    with _TASKS_COND:
        for tid, info in list(ACTIVE_TASKS.items()):
            _sync_task_locked(tid, info)
        if _evict_terminal_locked():
            _save_tasks()
        return [
            _snapshot_locked(tid, info)
            for tid, info in ACTIVE_TASKS.items()
        ]


def wait_task_change(task_id, after_revision, timeout_seconds, cancel_event=None):
    """条件等待任务变化：revision 超过 after_revision、进入终态或超时后返回快照。

    任务不存在返回 None；超时返回当前快照（是否变化由调用方比较 revision 得出），
    超时不是错误；cancel_event 置位抛 KeyboardInterrupt（不伪装超时）。
    """
    _ensure_paths()
    _ensure_loaded()
    deadline = time.monotonic() + timeout_seconds
    with _TASKS_COND:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise KeyboardInterrupt("用户终止了工具执行。")
            if _evict_terminal_locked():
                _save_tasks()
            info = ACTIVE_TASKS.get(task_id)
            if info is None:
                return None
            _sync_task_locked(task_id, info)
            if ACTIVE_TASKS.get(task_id) is not info:
                return None
            snap = _snapshot_locked(task_id, info)
            if snap.revision > after_revision or snap.terminal:
                return snap
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return snap
            _TASKS_COND.wait(timeout=min(remaining, 0.5))


def background_tasks_fingerprint() -> str:
    """全部任务 {task_id: [status, revision, terminal, exit_code]} canonical JSON 的 SHA-256。"""
    import hashlib
    _ensure_paths()
    with _TASKS_COND:
        _ensure_loaded()
        dirty = False
        for tid, info in list(ACTIVE_TASKS.items()):
            if _sync_task_locked(tid, info):
                dirty = True
        if _evict_terminal_locked():
            dirty = True
        if dirty:
            _save_tasks()
        state = {
            tid: [info.get("status", TASK_STATUS_RUNNING),
                  int(info.get("revision", 0)),
                  info.get("status", TASK_STATUS_RUNNING) in _TERMINAL_STATUSES,
                  info.get("exit_code")]
            for tid, info in sorted(ACTIVE_TASKS.items())
        }
    canonical = json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _evict_terminal_locked():
    """终态任务超过 _TERMINAL_KEEP_CAP 时淘汰最旧终态（按 start_time），并记墓碑。"""
    terminal = [
        (tid, info) for tid, info in ACTIVE_TASKS.items()
        if info.get("status", TASK_STATUS_RUNNING) in _TERMINAL_STATUSES
    ]
    if len(terminal) <= _TERMINAL_KEEP_CAP:
        return False
    terminal.sort(key=lambda pair: pair[1].get("start_time") or 0)
    for tid, _info in terminal[: len(terminal) - _TERMINAL_KEEP_CAP]:
        del ACTIVE_TASKS[tid]
        _record_removed_locked(tid)
    return True


def list_active_tasks():
    """返回当前所有仍在运行的任务列表（返回值结构与历史一致，只含 running 任务）。

    每行附带 session_id（发起会话名）：GUI 执行条据此只显示当前会话的任务。
    死亡任务不再直接删除，而是标记终态保留在 ACTIVE_TASKS 供快照/wait 查询；
    终态记录超过 _TERMINAL_KEEP_CAP 时淘汰最旧；磁盘库只保存 running 任务。
    """
    _ensure_paths()
    results = []
    # 强制重新从本地加载以接管外部常驻进程
    _load_tasks()

    dirty = False
    with _TASKS_COND:
        for tid, info in list(ACTIVE_TASKS.items()):
            was_terminal = info.get("status", TASK_STATUS_RUNNING) in _TERMINAL_STATUSES
            _sync_task_locked(tid, info)
            if ACTIVE_TASKS.get(tid) is not info:
                # 同步终态时可能因容量淘汰删除当前记录，不能再使用旧局部引用。
                dirty = True
                continue
            if info.get("status", TASK_STATUS_RUNNING) in _TERMINAL_STATUSES:
                if not was_terminal:
                    dirty = True
                continue
            proc = info.get("process")
            pid = info.get("pid")
            running_time = int(time.time() - info["start_time"])
            # 输出心跳：日志字节数 + 距最后一次写入的秒数。GUI 任务面板据此在
            # 运行期区分"仍在运行只是暂无输出"与"面板失联"（缓冲管道命令的
            # 运行期日志天然只有启动标记一行，size 长期停在几十字节）
            log_file_path = info.get("log_file_path")
            try:
                output_size = os.path.getsize(log_file_path) if log_file_path else 0
                last_output_age = max(0, int(time.time() - os.path.getmtime(log_file_path))) \
                    if log_file_path and output_size > 0 else None
            except OSError:
                output_size, last_output_age = 0, None
            results.append({
                "task_id": tid,
                "command": info["command"],
                "pid": pid or (proc.pid if proc else -1),
                "duration": f"{running_time}秒",
                "turn_id": info.get("turn_id") or "",
                "session_id": info.get("session_id") or "",
                "output_size": output_size,
                "last_output_age": last_output_age,
                "no_output_yet": output_size <= _STARTUP_MARKER_MAX_BYTES,
            })

        if _evict_terminal_locked():
            dirty = True
        if dirty:
            _save_tasks()

    return results


def get_task_output(task_id: str, tail_lines: int = 30) -> str:
    """读取指定任务的日志输出尾部"""
    _ensure_paths()
    if not _TASK_ID_PATTERN.fullmatch(str(task_id or "")):
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
        # 子进程写文件的原始字节常带 ANSI 颜色码（vitest 在 CI/FORCE_COLOR 下
        # 非 TTY 也上色），日志尾部同时供 GUI 面板、CLI /tasks 与任务快照消费，
        # 统一在此清洗成纯文本；CSI 序列不含换行，剥离不影响分行截断逻辑
        output = strip_ansi_escapes(output)
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


def _confirm_process_stopped(pid, proc=None, timeout_seconds=2.0) -> bool:
    """在发出终止请求后有限等待，并以真实进程状态确认是否已退出。"""
    deadline = time.monotonic() + max(0.0, float(timeout_seconds))
    while True:
        if proc is not None:
            try:
                if proc.poll() is not None:
                    return True
            except Exception as error:
                _logger.warning("终止后轮询进程失败 %s: %s", pid, error)
        if pid is None or not is_pid_running(pid):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


# kill_background_task_detailed 结果分类：调用方按类别给出准确反馈，
# 避免把"进程已自然结束"这类无害结果误报为"终止失败"。
KILL_RESULT_KILLED = "killed"                      # 已实际执行终止且确认进程退出
KILL_RESULT_NATURAL_DEATH = "natural_death"        # 终止前进程已自行结束（终态按自然退出落定）
KILL_RESULT_NOT_FOUND = "not_found"                # 无该任务记录
KILL_RESULT_IDENTITY_UNVERIFIABLE = "identity_unverifiable"  # 无法校验进程身份，保守拒绝 kill
KILL_RESULT_FAILED = "failed"                      # 终止动作已执行但进程未确认退出


def kill_background_task(task_id: str) -> bool:
    """终止后台运行的指定任务；按实际结果返回成功与否"""
    return kill_background_task_detailed(task_id) == KILL_RESULT_KILLED


def kill_background_task_detailed(task_id: str) -> str:
    """终止后台任务并返回结果分类（KILL_RESULT_* 常量之一）。"""
    # 刷新状态，确保最新的 tasks.json 已被同步
    list_active_tasks()

    with _TASKS_LOCK:
        info = ACTIVE_TASKS.get(task_id)
    if info is None:
        return KILL_RESULT_NOT_FOUND

    proc = info.get("process")
    pid = info.get("pid")

    killed = False
    natural_death = False  # 进程在 kill 之前已自行结束（终态按自然退出落定）
    identity_unverifiable = False
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
                    # 终态记 failed，避免把“没有执行 kill”谎报为 cancelled。
                    identity_unverifiable = True
                elif recorded_create_time is None:
                    # 旧记录缺少启动时间：用命令行匹配兜底，避免误杀复用 PID 的无关进程；
                    # 命令行同样无法获取（psutil 缺失）时保守拒绝 kill
                    cmdline = _process_cmdline(pid)
                    if cmdline is None or str(info.get("command", ""))[:30] not in cmdline:
                        identity_unverifiable = True
            if pid_reused:
                # 视为任务已结束，只清理记录，不执行 taskkill
                killed = False
                natural_death = True
            elif identity_unverifiable:
                killed = False
            elif not is_pid_running(pid):
                # 进程已自行结束
                killed = False
                natural_death = True
            elif os.name == 'nt':
                # Windows 下利用 taskkill 杀死整个进程树（列表参数，不做 shell 插值）
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    capture_output=True, timeout=15,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                # 返回码只作诊断；taskkill=0 也可能有进程仍存活，必须复核。
                killed = _confirm_process_stopped(pid, proc)
            else:
                # POSIX：尝试终止整个进程树并等待实际结束，避免壳进程死了孙子进程仍存活
                killed = _kill_posix_tree(pid, proc)
        elif proc:
            proc.terminate()
            killed = _confirm_process_stopped(getattr(proc, "pid", None), proc)
    except Exception as error:
        _logger.warning("终止后台任务失败 %s: %s", task_id, error)
        try:
            if proc:
                proc.kill()
                killed = _confirm_process_stopped(pid, proc)
        except Exception:
            killed = False

    # 只有任务确实进入终态时才关闭日志。taskkill 失败且进程仍存活时必须
    # 保持 running 与日志句柄，允许后续查询和再次终止。
    if killed or natural_death or identity_unverifiable:
        try:
            if info.get("log_handle"):
                info["log_handle"].close()
        except Exception as error:
            _logger.warning("关闭任务日志句柄失败 %s: %s", task_id, error)

    with _TASKS_COND:
        current = ACTIVE_TASKS.get(task_id)
        if current is not None:
            if natural_death:
                # 进程在 kill 之前已自行结束：按自然终态落定，不覆盖为 cancelled
                _sync_task_locked(task_id, current)
            elif identity_unverifiable and current.get("status") == TASK_STATUS_RUNNING:
                _transition_terminal_locked(
                    task_id, current, TASK_STATUS_FAILED, None
                )
            elif killed and current.get("status") == TASK_STATUS_RUNNING:
                # 保留记录为终态（快照/wait 可继续查询），不再立即删除
                _transition_terminal_locked(
                    task_id, current, TASK_STATUS_CANCELLED, None
                )
        # 墓碑防止 _save_tasks 的跨进程合并读把磁盘旧记录复活
        if current is not None and current.get("status") in _TERMINAL_STATUSES:
            _record_removed_locked(task_id)
        _save_tasks()
    if killed:
        return KILL_RESULT_KILLED
    if natural_death:
        return KILL_RESULT_NATURAL_DEATH
    if identity_unverifiable:
        return KILL_RESULT_IDENTITY_UNVERIFIABLE
    return KILL_RESULT_FAILED
