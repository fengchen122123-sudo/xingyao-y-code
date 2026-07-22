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
from typing import Dict, Any, Optional, List

from config import DB_FILE, MY_PERMISSION_MODE
from core.memory import MemoryManager
from core.llm import chat_with_model_events
from core.model_manager import get_active_model, load_config, set_active_model
from core.permission_broker import permission_broker, PermissionRequest
from core.task_manager import list_active_tasks, kill_background_task
from core.skills import scan_all_skills
from core.mcp_client import mcp_manager

class ApiBridge:
    def __init__(self):
        # 内部状态变量使用下划线开头，防止 PyWebView 在 Windows .NET 检查中产生属性递归问题
        self._window = None
        self._memory = MemoryManager(db_path=DB_FILE)
        self._mcp = mcp_manager
        self._current_session = f"session_{uuid.uuid4().hex[:6]}"
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
        """获取界面初始数据"""
        workdir = os.getcwd()
        sessions = self._memory.get_all_sessions(workdir=workdir)
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
        except Exception:
            pass
        return items

    # ---------------------------------------------------------------------------
    # 会话管理接口
    # ---------------------------------------------------------------------------
    def get_sessions(self) -> List[Dict[str, Any]]:
        return self._memory.get_all_sessions(workdir=os.getcwd())

    def create_session(self, title: str = "新会话") -> Dict[str, Any]:
        new_name = f"session_{uuid.uuid4().hex[:6]}"
        self._memory.add_message(new_name, "system", f"会话创建: {title}")
        self._current_session = new_name
        return {"session_name": new_name, "title": title}

    def switch_session(self, session_name: str) -> List[Dict[str, Any]]:
        self._current_session = session_name
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
        return msgs

    def rename_session(self, old_name: str, new_name: str) -> bool:
        try:
            self._memory.rename_session(old_name, new_name)
            return True
        except Exception:
            return False

    def delete_session(self, session_name: str) -> bool:
        try:
            self._memory.delete_session(session_name)
            if self._current_session == session_name:
                self._current_session = f"session_{uuid.uuid4().hex[:6]}"
            return True
        except Exception:
            return False

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
        try:
            # 捕获流式回调
            for event in chat_with_model_events(
                history=history,
                model_info=active_model,
                cancel_event=self._cancel_event
            ):
                if self._cancel_event.is_set():
                    break

                etype = event.get("type")
                data = event.get("data")

                if etype == "thinking_delta":
                    self.emit_event("thinking_delta", {"text": str(data)})
                elif etype == "token_delta":
                    full_response += str(data)
                    self.emit_event("token_delta", {"text": str(data)})
                elif etype == "tool_start":
                    self.emit_event("tool_start", data)
                elif etype == "tool_finish":
                    self.emit_event("tool_finish", data)
                elif etype == "permission_request":
                    req_id = str(uuid.uuid4())[:8]
                    event_wait = threading.Event()
                    self._pending_permissions[req_id] = event_wait
                    
                    self.emit_event("permission_request", {
                        "request_id": req_id,
                        "tool_name": data.get("tool_name"),
                        "details": data.get("details")
                    })

                    event_wait.wait(timeout=300)
                    allowed, always = self._permission_results.pop(req_id, (False, False))
                    self._pending_permissions.pop(req_id, None)

                    if "callback" in data:
                        data["callback"](allowed, always)

            if full_response:
                self._memory.add_message(session_name, "assistant", full_response)

            self.emit_event("turn_finish", {"full_text": full_response})

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
