"""秘书调度台：core 层服务句柄（Agent 工具访问检索/派活/跳转能力的唯一入口）。

用途：GUI 启动时构造内部实现（gui_bridge 侧的 ``_SecretaryDispatchService``，
把三个方法落到既有会话/回合通道上：检索复用 MemoryManager，派活复用
``_spawn_turn_worker`` / ``_message_queues``，跳转复用 ``_set_current_session``），
再调用 ``register(service)``；core 层的 Agent 工具通过 ``get()`` 取得句柄并调用
同名方法，从而在不 ``import gui_bridge`` 的前提下访问调度能力。

约定：
- 本模块是纯依赖注入容器 + 协议说明，不做任何 I/O、不 import gui_bridge /
  webview / sqlite3、不起线程；
- CLI / headless 环境未注册时 ``get()`` 返回 ``None``，工具层据此提示能力不可用；
- 实现方（桥侧）把"参数非法 / 目标不存在 / 目录非法"等可读原因包装成
  ``SecretaryDispatchError`` 抛出，工具层直接转成提示文本。
"""

from __future__ import annotations

from typing import List, Optional

__all__ = [
    "SecretaryDispatchError",
    "SecretaryDispatchService",
    "register",
    "unregister",
    "get",
]


class SecretaryDispatchError(Exception):
    """秘书调度能力的使用错误（参数非法 / 目标不存在 / 目录非法等可读原因）。"""


class SecretaryDispatchService:
    """调度能力的协议声明：真实实现由 gui_bridge 侧提供并注册。

    本类只作类型与文档说明（方法体仅抛 NotImplementedError），不是实现；
    实现方必须逐字满足以下签名与语义：

    - ``search_conversations``：按关键词 / 日期 / 目录检索历史会话，
      关键词与日期都为空时抛 ``SecretaryDispatchError``（避免全表倾倒）；
    - ``dispatch_task``：把任务投给已有会话或按 workdir 新建会话执行，
      语义等价于**用户在目标会话亲手发一条消息**；
    - ``open_session``：把用户带到某个历史会话（系统层切换，不模拟点击）。
    """

    def search_conversations(
        self,
        keyword: str = "",
        date_from: str = "",
        date_to: str = "",
        workdir: str = "",
        limit: int = 20,
    ) -> List[dict]:
        """检索历史会话；返回 list[dict]，每项含 session_name / display_name /
        is_secretary / workdir / updated_at / message_count / snippets。"""
        raise NotImplementedError

    def dispatch_task(
        self,
        target: str = "",
        prompt: str = "",
        workdir: str = "",
        model_id: str = "",
        permission_mode: str = "",
    ) -> dict:
        """派发任务；返回 {ok, target_session, display_name, workdir, state}。"""
        raise NotImplementedError

    def open_session(self, target: str = "") -> dict:
        """切换到目标会话；返回 {ok, session_name, display_name, workdir}。"""
        raise NotImplementedError


_instance: Optional[SecretaryDispatchService] = None


def register(service) -> None:
    """注册（或覆盖）当前进程的秘书调度服务句柄。"""
    global _instance
    _instance = service


def unregister() -> None:
    """清除已注册的句柄；幂等，未注册时调用不抛异常。"""
    global _instance
    _instance = None


def get() -> Optional[SecretaryDispatchService]:
    """返回已注册的句柄；未注册（如 CLI/headless）时返回 ``None``。"""
    return _instance
