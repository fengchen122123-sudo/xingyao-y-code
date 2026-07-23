"""
gui_bridge.py
PyWebView JS-API 桥接层：为前端 demo HTML 提供直接可用的 Python 后端方法，
并支持流式 Token、思考过程、工具卡片、权限确认及后台任务的实时推送。
"""
import os
import sys
import json
import uuid
import time
import queue
import threading
import subprocess
import re
import pathlib
from typing import Dict, Any, Optional, List

from config import DB_FILE, MY_PERMISSION_MODE
from core.memory import MemoryManager
from core.llm import chat_with_model_events
from core.model_manager import get_active_model, load_config, set_active_model

def _resolve_real_path(path: str) -> str:
    try:
        p = pathlib.Path(path)
        if p.exists():
            return str(p.resolve())
        return os.path.abspath(path)
    except Exception:
        return os.path.abspath(path)
from core.permission_broker import permission_broker, PermissionRequest
from core.task_manager import list_active_tasks, kill_background_task
from core.skills import scan_all_skills
from core.mcp_client import mcp_manager

class ApiBridge:
    def __init__(self, mcp_manager=None):
        # 内部状态变量使用下划线开头，防止 PyWebView 在 Windows .NET 检查中产生属性递归问题
        self._window = None
        self._memory = MemoryManager(db_path=DB_FILE)
        self._mcp = mcp_manager
        self._current_session = f"session_{uuid.uuid4().hex[:6]}"
        try:
            self._memory.add_session(self._current_session, workdir="GLOBAL")
        except Exception:
            pass
        self._current_permission_mode = MY_PERMISSION_MODE
        self._active_turn_thread: Optional[threading.Thread] = None
        self._cancel_event = threading.Event()
        self._pending_permissions: Dict[str, threading.Event] = {}
        self._permission_results: Dict[str, tuple] = {} # req_id -> (allowed, always)

    def set_window(self, window):
        """设置 PyWebView window 引用"""
        self._window = window

    def emit_event(self, event_type: str, data: Any):
        """向 JS 传递结构化事件"""
        if not self._window:
            return
        payload = json.dumps({"type": event_type, "data": data}, ensure_ascii=False)
        try:
            self._window.evaluate_js(f"window.onBridgeEvent && window.onBridgeEvent({payload})")
        except Exception as e:
            print(f"[GuiBridge] Error emitting event {event_type}: {e}", file=sys.stderr)

    # ---------------------------------------------------------------------------
    # 状态获取接口
    # ---------------------------------------------------------------------------
    def get_initial_state(self) -> Dict[str, Any]:
        """获取界面初始数据：默认不选中工作目录"""
        workdir = "GLOBAL"
        sessions = self.get_sessions()
        active_model = get_active_model() or {}
        
        # 转换 models 格式供下拉框使用
        config_data = load_config()
        models = []
        for provider in config_data.get("providers", []):
            for m in provider.get("models", []):
                models.append({
                    "id": str(m["id"]),
                    "name": str(m["name"]),
                    "litellm_model": str(m["litellm_model"]),
                    "provider": str(provider["name"])
                })

        # 技能与 MCP 数据
        try:
            raw_skills = scan_all_skills()
            skills = []
            for s in (raw_skills if isinstance(raw_skills, list) else []):
                skills.append({
                    "id": str(s.get("id", "")),
                    "name": str(s.get("name", "")),
                    "description": str(s.get("description", ""))
                })
        except Exception:
            skills = []

        try:
            raw_mcps = self._mcp.get_schemas() if hasattr(self._mcp, "get_schemas") else []
            mcps = []
            for m in (raw_mcps if isinstance(raw_mcps, list) else []):
                func = m.get("function", {})
                mcps.append({
                    "name": str(func.get("name", "")),
                    "description": str(func.get("description", ""))
                })
        except Exception:
            mcps = []

        return {
            "workdir": str(workdir),
            "current_session": str(self._current_session),
            "sessions": sessions,
            "current_model": str(active_model.get("name", "未激活模型")),
            "models": models,
            "permission_mode": str(self._current_permission_mode),
            "skills": skills,
            "mcps": mcps,
            "file_tree": self._get_file_tree(workdir)
        }

    def _get_file_tree(self, path: str, depth: int = 2) -> List[Dict[str, Any]]:
        """获取目录树结构（限定深度与软链接避让）"""
        if depth <= 0:
            return []
        items = []
        try:
            entries = os.listdir(path)
            for entry in sorted(entries):
                if entry.startswith(".") or entry in ("__pycache__", "venv", "build", "dist", "node_modules", "mytemp", "zip"):
                    continue
                full_path = os.path.join(path, entry)
                if os.path.islink(full_path):
                    continue
                is_dir = os.path.isdir(full_path)
                item = {
                    "n": str(entry),
                    "d": 1 if is_dir else 0
                }
                if is_dir and depth > 1:
                    item["children"] = self._get_file_tree(full_path, depth - 1)
                items.append(item)
        except Exception as e:
            print("Error scanning file tree:", e)
        return items

    def select_workspace_folder(self) -> Optional[Dict[str, Any]]:
        """弹出原生文件对话框，选择并切换项目工作区文件夹"""
        import webview
        if not self._window:
            return None
        try:
            result = self._window.create_file_dialog(
                webview.FOLDER_DIALOG,
                directory=os.getcwd()
            )
            if result and len(result) > 0:
                selected_path = _resolve_real_path(result[0])
                if os.path.isdir(selected_path):
                    os.chdir(selected_path)
                    self._memory.add_workspace(selected_path)
                    file_tree = self._get_file_tree(selected_path, depth=3)
                    print(f"[GUI Bridge] Switched workspace to: {selected_path}, total top items: {len(file_tree)}")
                    return {
                        "workdir": selected_path,
                        "file_tree": file_tree,
                        "skills": scan_all_skills(),
                        "sessions": self.get_sessions()
                    }
        except Exception as e:
            print("Select workspace folder error:", e)
        return None

    # ---------------------------------------------------------------------------
    # 会话管理接口
    # ---------------------------------------------------------------------------
    def get_sessions(self) -> List[Dict[str, Any]]:
        raw = self._memory.get_all_sessions(workdir=None)
        saved_workspaces = self._memory.get_all_workspaces()
        current_wdir = _resolve_real_path(os.getcwd())

        if current_wdir != "GLOBAL":
            self._memory.add_workspace(current_wdir)

        groups_dict = {}

        # 自由通用对话分组（独立于任何项目文件夹）
        global_group = {
            "folder_path": "GLOBAL",
            "folder_name": "对话",
            "is_global": True,
            "is_current": (current_wdir == "GLOBAL"),
            "sessions": []
        }

        # 1. 初始化所有已记录引入过的项目文件夹，哪怕会话数为 0 也保留！
        all_known_wdirs = set(saved_workspaces)
        if current_wdir != "GLOBAL":
            all_known_wdirs.add(current_wdir)

        for s in raw:
            wdir_item = s.get("workdir")
            if wdir_item and wdir_item != "GLOBAL":
                all_known_wdirs.add(_resolve_real_path(wdir_item))

        for w_path in all_known_wdirs:
            if not w_path or w_path == "GLOBAL":
                continue
            norm_p = _resolve_real_path(w_path)
            folder_name = os.path.basename(norm_p) or norm_p
            groups_dict[norm_p] = {
                "folder_path": norm_p,
                "folder_name": folder_name,
                "is_current": (norm_p == current_wdir),
                "sessions": []
            }

        # 2. 填充各分组的具体会话
        for s in raw:
            msg_count = s.get("count", 0)
            name = s.get("name", "")
            is_default_name = bool(re.match(r"^session_[a-f0-9]{6}$", name))

            # 自动跳过没有消息记录的垃圾临时会话
            if msg_count == 0 and is_default_name and name != self._current_session:
                continue

            wdir = s.get("workdir")
            display = "新会话" if is_default_name else name

            if wdir == "GLOBAL":
                s_info = {
                    "name": name,
                    "display_name": display,
                    "count": msg_count,
                    "workdir": "GLOBAL",
                    "is_global": True
                }
                global_group["sessions"].append(s_info)
                if name == self._current_session:
                    global_group["is_current"] = True
                continue

            wdir_norm = _resolve_real_path(wdir) if wdir else current_wdir
            s_info = {
                "name": name,
                "display_name": display,
                "count": msg_count,
                "workdir": wdir_norm,
                "is_global": False
            }

            if wdir_norm not in groups_dict:
                folder_name = os.path.basename(wdir_norm) or wdir_norm
                groups_dict[wdir_norm] = {
                    "folder_path": wdir_norm,
                    "folder_name": folder_name,
                    "is_current": (wdir_norm == current_wdir),
                    "sessions": []
                }
            groups_dict[wdir_norm]["sessions"].append(s_info)

        result_groups = []
        if len(global_group["sessions"]) > 0 or global_group["is_current"]:
            result_groups.append(global_group)

        # 3. 包含了用户引入的所有项目文件夹，哪怕里面是 0 个会话，也完整保留！
        project_groups = list(groups_dict.values())
        project_groups.sort(key=lambda g: (0 if g["is_current"] else 1, g["folder_name"]))
        result_groups.extend(project_groups)

        return result_groups

    def create_session(self, title: str = "新会话", workdir: Optional[str] = None) -> Dict[str, Any]:
        new_name = f"session_{uuid.uuid4().hex[:6]}"
        target_workdir = workdir or _resolve_real_path(os.getcwd())
        self._memory.create_session_explicit(new_name, workdir=target_workdir)
        self._current_session = new_name
        return {"session_name": new_name, "title": title, "display_name": title, "workdir": target_workdir}

    def create_global_session(self, title: str = "新对话") -> Dict[str, Any]:
        """创建跳出文件夹限定的独立通用对话"""
        new_name = f"session_{uuid.uuid4().hex[:6]}"
        self._memory.create_session_explicit(new_name, workdir="GLOBAL")
        self._current_session = new_name
        return {"session_name": new_name, "title": title, "display_name": title, "workdir": "GLOBAL"}

    def switch_session(self, session_name: str, target_workdir: Optional[str] = None) -> Dict[str, Any]:
        self._current_session = session_name

        stored_wdir = None
        raw_sessions = self._memory.get_all_sessions(workdir=None)
        for s in raw_sessions:
            if s.get("name") == session_name:
                stored_wdir = s.get("workdir")
                break

        is_global = (target_workdir == "GLOBAL" or stored_wdir == "GLOBAL")

        if not is_global and target_workdir and os.path.isdir(target_workdir):
            if os.path.abspath(target_workdir) != os.path.abspath(os.getcwd()):
                os.chdir(target_workdir)

        history = self._memory.get_history(session_name)
        msgs = []
        for msg in history:
            role = msg.get("role")
            if role in ("user", "assistant", "tool"):
                msgs.append({
                    "role": role,
                    "content": msg.get("content") or "",
                    "reasoning_content": msg.get("reasoning_content") or "",
                    "tool_calls": msg.get("tool_calls") or [],
                    "name": msg.get("name") or "",
                    "tool_call_id": msg.get("tool_call_id") or ""
                })

        if is_global:
            return {
                "session_name": session_name,
                "messages": msgs,
                "workdir": "GLOBAL",
                "is_global": True,
                "file_tree": [],
                "skills": scan_all_skills(),
                "sessions": self.get_sessions()
            }
        else:
            current_workdir = _resolve_real_path(os.getcwd())
            file_tree = self._get_file_tree(current_workdir, depth=3)
            return {
                "session_name": session_name,
                "messages": msgs,
                "workdir": current_workdir,
                "is_global": False,
                "file_tree": file_tree,
                "skills": scan_all_skills(),
                "sessions": self.get_sessions()
            }

    def rename_session(self, old_name: str, new_name: str) -> bool:
        try:
            self._memory.rename_session(old_name, new_name)
            return True
        except Exception:
            return False

    def delete_session(self, session_name: str) -> Dict[str, Any]:
        try:
            self._memory.delete_session(session_name)
            if self._current_session == session_name:
                self._current_session = f"session_{uuid.uuid4().hex[:6]}"
            return {"success": True, "sessions": self.get_sessions(), "current_session": self._current_session}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def delete_folder_group(self, workdir: str) -> Dict[str, Any]:
        """移除项目文件夹分组：仅删除 DB 记录中的会话与分组记录，绝不删除磁盘文件"""
        try:
            self._memory.delete_sessions_by_workdir(workdir)
            self._memory.delete_workspace(workdir)
            return {"success": True, "sessions": self.get_sessions()}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ---------------------------------------------------------------------------
    # 设置与模式
    # ---------------------------------------------------------------------------
    def set_model(self, model_id_or_name: str) -> bool:
        try:
            set_active_model(model_id_or_name)
            return True
        except Exception:
            return False

    def set_permission_mode(self, mode: str) -> bool:
        self._current_permission_mode = mode
        mapping = {"默认模式": "confirm", "自动接受编辑": "plan", "计划模式": "plan", "YOLO (全部允许)": "full"}
        val = mapping.get(mode, "confirm")
        try:
            from config import save_config_value
            save_config_value("permission_mode", val)
            return True
        except Exception:
            return False

    # ---------------------------------------------------------------------------
    # 交互式权限确认桥接
    # ---------------------------------------------------------------------------
    def respond_permission(self, request_id: str, allow: bool, always: bool = False):
        if request_id in self._pending_permissions:
            self._permission_results[request_id] = (allow, always)
            self._pending_permissions[request_id].set()
            return True
        return False

    # ---------------------------------------------------------------------------
    # 对话流与 AI 运行
    # ---------------------------------------------------------------------------
    def send_user_message(self, text: str, session_name: Optional[str] = None):
        """用户提交消息：在后台子线程中启动对话流"""
        if not session_name:
            session_name = self._current_session

        if self._active_turn_thread and self._active_turn_thread.is_alive():
            return {"status": "error", "message": "已有正在运行的回合"}

        self._cancel_event.clear()
        self._active_turn_thread = threading.Thread(
            target=self._run_turn_worker,
            args=(text, session_name),
            daemon=True
        )
        self._active_turn_thread.start()
        return {"status": "ok"}

    def stop_current_turn(self):
        """强行打断当前回合"""
        self._cancel_event.set()
        self.emit_event("turn_cancelled", {})

    def _run_turn_worker(self, user_text: str, session_name: str):
        """后台线程处理模型事件与工具调用"""
        self.emit_event("turn_start", {"user_text": user_text, "session_name": session_name})

        # 写入用户消息到 SQLite 记忆
        self._memory.add_message(session_name, "user", user_text)

        # 读取完整历史上下文
        history = self._memory.get_history(session_name)
        active_model = get_active_model()
        if not active_model:
            self.emit_event("error", {"message": "当前未激活任何 LLM 模型，请先配置模型"})
            return

        full_response = ""
        model_name = active_model.get("litellm_model") or active_model.get("name", "")
        api_key = active_model.get("api_key", "")
        api_base = active_model.get("base_url", None)
        turn_id = f"{session_name}:{uuid.uuid4().hex[:8]}"

        try:
            # 捕获流式回调
            for event in chat_with_model_events(
                model_name,
                api_key,
                history,
                api_base=api_base,
                persist=True,
                turn_id=turn_id,
                cancel_event=self._cancel_event,
                parent_session=session_name,
                parent_turn_id=turn_id,
                permission_owner=turn_id,
                permission_source="GUI"
            ):
                if self._cancel_event.is_set():
                    break

                kind = getattr(event, "kind", None) or (event.get("type") if isinstance(event, dict) else None)
                payload = getattr(event, "payload", None) or (event.get("data") if isinstance(event, dict) else {})

                if kind == "thinking_delta":
                    text = payload.get("text", "")
                    self.emit_event("thinking_delta", {"text": text})
                elif kind in ("assistant_delta", "token_delta"):
                    text = payload.get("text", "")
                    full_response += text
                    self.emit_event("token_delta", {"text": text})
                elif kind in ("tool_started", "tool_start"):
                    payload_dict = dict(payload) if isinstance(payload, dict) else getattr(payload, "__dict__", {})
                    tname = payload_dict.get("name") or payload_dict.get("tool_name") or "tool"
                    targs = payload_dict.get("arguments") if payload_dict.get("arguments") is not None else payload_dict.get("args", {})
                    tid = str(payload_dict.get("id") or uuid.uuid4().hex[:8])
                    self.emit_event("tool_start", {
                        "id": tid,
                        "tool_name": tname,
                        "args": targs
                    })
                elif kind in ("tool_finished", "tool_finish"):
                    payload_dict = dict(payload) if isinstance(payload, dict) else getattr(payload, "__dict__", {})
                    tname = payload_dict.get("name") or payload_dict.get("tool_name") or "tool"
                    targs = payload_dict.get("arguments") if payload_dict.get("arguments") is not None else payload_dict.get("args", {})
                    tid = str(payload_dict.get("id") or uuid.uuid4().hex[:8])
                    res_text = payload_dict.get("summary") or payload_dict.get("result") or ("✓ 完成" if payload_dict.get("ok", True) else "✕ 失败")
                    self.emit_event("tool_finish", {
                        "id": tid,
                        "tool_name": tname,
                        "args": targs,
                        "result": res_text,
                        "error": None if payload_dict.get("ok", True) else res_text
                    })
                elif kind == "permission_request":
                    req_id = str(uuid.uuid4())[:8]
                    event_wait = threading.Event()
                    self._pending_permissions[req_id] = event_wait
                    
                    self.emit_event("permission_request", {
                        "request_id": req_id,
                        "tool_name": payload.get("tool_name"),
                        "details": payload.get("details")
                    })

                    event_wait.wait(timeout=300)
                    allowed, always = self._permission_results.pop(req_id, (False, False))
                    self._pending_permissions.pop(req_id, None)

                    if "callback" in payload:
                        payload["callback"](allowed, always)
                elif kind == "turn_error":
                    self.emit_event("error", {"message": payload.get("message", "模型生成错误")})

            self.emit_event("turn_finish", {"full_text": full_response})

            # 首轮对话完成后，如果还是默认的 session_xxxxxx 命名，自动提炼标题重命名
            if re.match(r"^session_[a-f0-9]{6}$", session_name):
                history = self._memory.get_history(session_name)
                user_msgs = [m for m in history if m.get("role") == "user"]
                if user_msgs:
                    first_text = str(user_msgs[0].get("content", "")).strip().replace("\n", " ")
                    if first_text:
                        new_title = first_text[:12]
                        try:
                            self._memory.rename_session(session_name, new_title)
                            self._current_session = new_title
                            self.emit_event("session_renamed", {
                                "old_name": session_name,
                                "new_name": new_title,
                                "sessions": self.get_sessions()
                            })
                        except Exception:
                            pass

        except Exception as e:
            self.emit_event("error", {"message": str(e)})
        finally:
            self.emit_event("turn_idle", {})

    # ---------------------------------------------------------------------------
    # 终端与后台任务命令执行
    # ---------------------------------------------------------------------------
    def execute_terminal_cmd(self, cmd: str) -> Dict[str, Any]:
        """在工作区直接运行 shell 命令"""
        try:
            res = subprocess.run(
                cmd,
                shell=True,
                cwd=os.getcwd(),
                capture_output=True,
                text=True,
                timeout=30
            )
            return {
                "exit_code": res.returncode,
                "stdout": res.stdout,
                "stderr": res.stderr
            }
        except subprocess.TimeoutExpired:
            return {"exit_code": -1, "stdout": "", "stderr": "命令超时（超过 30s）"}
        except Exception as e:
            return {"exit_code": -1, "stdout": "", "stderr": str(e)}

    def get_background_tasks(self) -> List[Dict[str, Any]]:
        """获取后台任务"""
        try:
            return list_active_tasks()
        except Exception:
            return []

    def kill_task(self, task_id: str) -> bool:
        """终止后台任务"""
        try:
            return kill_background_task(task_id)
        except Exception:
            return False
