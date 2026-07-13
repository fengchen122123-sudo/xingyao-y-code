import os
import subprocess
import time
import uuid
import json

# 任务管理器数据库或全局内存状态
ACTIVE_TASKS = {}

# 任务日志目录
LOG_DIR = os.path.abspath(os.path.join(os.getcwd(), ".ycode_tasks"))
if not os.path.exists(LOG_DIR):
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
    except:
        pass

TASK_DB_PATH = os.path.join(LOG_DIR, "tasks.json")

def is_pid_running(pid):
    """检查 PID 对应的进程是否仍在系统后台运行"""
    try:
        import psutil
        return psutil.pid_exists(pid)
    except ImportError:
        try:
            if os.name == 'nt':
                # Windows tasklist 兜底检测
                out = subprocess.run(f"tasklist /FI \"PID eq {pid}\"", capture_output=True, text=True, shell=True, timeout=2)
                return str(pid) in out.stdout
            else:
                # Unix kill -0 信号检测
                os.kill(pid, 0)
                return True
        except Exception:
            return False

def _load_tasks():
    """从本地 JSON 文件中反序列化加载任务记录，用于跨 Y-code 重启的生命周期接管"""
    global ACTIVE_TASKS
    if not os.path.exists(TASK_DB_PATH):
        return
    try:
        with open(TASK_DB_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
            for tid, info in data.items():
                if tid not in ACTIVE_TASKS:
                    ACTIVE_TASKS[tid] = {
                        "id": tid,
                        "command": info["command"],
                        "pid": info["pid"],
                        "start_time": info["start_time"],
                        "log_file_path": os.path.join(LOG_DIR, f"task_{tid}.log"),
                        "process": None,     # 重建的任务 Popen 对象为 None，依赖 PID 检测
                        "log_handle": None,  # 文件句柄在需要时懒加载/写入时关闭
                    }
    except Exception:
        pass

def _save_tasks():
    """将当前的活跃任务持久化写入磁盘 JSON 库"""
    try:
        data = {}
        for tid, info in ACTIVE_TASKS.items():
            pid = info.get("pid") or (info["process"].pid if info.get("process") else None)
            if pid:
                data[tid] = {
                    "command": info["command"],
                    "pid": pid,
                    "start_time": info["start_time"]
                }
        with open(TASK_DB_PATH, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

# 启动时首先尝试加载之前托管的常驻任务
_load_tasks()


def start_background_task(command: str) -> str:
    """在后台开启一个异步长周期任务（如 npm run dev），重定向日志输出"""
    task_id = str(uuid.uuid4())[:8]
    log_file_path = os.path.join(LOG_DIR, f"task_{task_id}.log")
    
    try:
        # 打开日志文件
        log_file = open(log_file_path, "w", encoding="utf-8", errors="replace")
        
        # 注入环境变量，强制让 Python 的输出不进行缓冲，实时写入日志文件
        import os as native_os
        env = native_os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        
        # 启动后台进程
        # stdout 和 stderr 都合并写入日志文件
        # shell=True 以支持复杂的脚本串联，creationflags 为后台启动做优化
        process = subprocess.Popen(
            command,
            shell=True,
            stdout=log_file,
            stderr=log_file,
            text=True,
            encoding='utf-8',
            errors='replace',
            env=env,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0
        )
        
        ACTIVE_TASKS[task_id] = {
            "id": task_id,
            "command": command,
            "process": process,
            "pid": process.pid,
            "log_file_path": log_file_path,
            "log_handle": log_file,
            "start_time": time.time(),
        }
        _save_tasks()
        return task_id
    except Exception as e:
        raise RuntimeError(f"启动后台任务失败: {e}")

def list_active_tasks():
    """返回当前所有活跃的任务列表，具备死亡状态自动清理与磁盘存盘同步"""
    results = []
    # 强制重新从本地加载以接管外部常驻进程，同时自动清理已死亡进程
    _load_tasks()
    
    dirty = False
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
            is_dead = True
            
        if is_dead:
            # 进程已死，关闭文件句柄并从活跃库移除
            try:
                if info.get("log_handle"):
                    info["log_handle"].close()
            except:
                pass
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
    log_file_path = os.path.join(LOG_DIR, f"task_{task_id}.log")
    if not os.path.exists(log_file_path):
        return f"🚨 未找到任务 {task_id} 的输出日志。"
        
    try:
        # 强制刷盘以确保读取到最新内容
        if task_id in ACTIVE_TASKS:
            handle = ACTIVE_TASKS[task_id].get("log_handle")
            if handle:
                try:
                    handle.flush()
                except:
                    pass
                
        with open(log_file_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        tail = lines[-tail_lines:] if len(lines) > tail_lines else lines
        return "".join(tail)
    except Exception as e:
        return f"🚨 读取日志发生异常: {e}"

def kill_background_task(task_id: str) -> bool:
    """终止后台运行的指定任务"""
    # 刷新状态，确保最新的 tasks.json 已被同步
    list_active_tasks()
    
    if task_id not in ACTIVE_TASKS:
        return False
        
    info = ACTIVE_TASKS[task_id]
    proc = info.get("process")
    pid = info.get("pid")
    
    try:
        if pid:
            # Windows 下利用 taskkill 杀死整个进程树
            if os.name == 'nt':
                subprocess.run(f"taskkill /F /T /PID {pid}", shell=True, capture_output=True)
            else:
                try:
                    import os as native_os
                    native_os.kill(pid, 15) # SIGTERM
                except:
                    if proc:
                        proc.terminate()
        elif proc:
            proc.terminate()
            
        try:
            if info.get("log_handle"):
                info["log_handle"].close()
        except:
            pass
            
        del ACTIVE_TASKS[task_id]
        _save_tasks()
        return True
    except Exception:
        # 强制清理
        try:
            if proc: proc.kill()
        except:
            pass
        if task_id in ACTIVE_TASKS:
            del ACTIVE_TASKS[task_id]
        _save_tasks()
        return True
