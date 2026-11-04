"""秘书调度台：core 层服务句柄（Agent 工具访问检索/派活/跳转能力的唯一入口）。

用途：GUI 启动时构造内部实现（gui_bridge 侧的 ``_SecretaryDispatchService``，
把检索 / 派活 / 跳转 / 台账查询方法落到既有会话/回合通道上：检索复用
MemoryManager，派活复用 ``_spawn_turn_worker`` / ``_message_queues``，跳转复用
``_set_current_session``，台账查询复用 ``core.secretary_dispatches.DispatchStore``），
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
    - ``open_session``：把用户带到某个历史会话（系统层切换，不模拟点击）；
    - ``list_dispatches``：按状态 / 目录 / 目标会话名或显示名子串查派活台账；
    - ``get_dispatch_result``：按 id 取一条派活的完整详情（含完整 prompt 与摘要）。
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

    def dispatch_batch(self, tasks=None) -> dict:
        """批量派活（升级设计 2026-11-04 第 6.1 节），支持同批次内下标依赖。

        ``tasks`` 每项 ``{title, prompt, target, workdir, depends_on}``，
        ``depends_on`` 是**同批次内的下标数组**。语义：
        - 无依赖项立即派发，有依赖项先落 ``blocked`` 不派发；
        - 单批上限 8 条，下标越界 / 指向自身 / 成环在派发前直接拒绝（抛
          ``SecretaryDispatchError``，成环文案带环路径）；
        - 上游 done 后由桥侧依赖推进器自动启动下游，上游 failed / cancelled
          则把下游标 ``blocked`` 并级联到更深下游。

        返回 ``{ok, batch_id, items: [{index, dispatch_id, state}]}``，同批次所有
        台账行的 batch_id 相同，便于按批汇总。
        """
        raise NotImplementedError

    def resume_dispatch(self, dispatch_id: int = 0) -> dict:
        """把一条落 ``blocked`` 的台账恢复派发（依赖推进器内部使用）。

        与 ``dispatch_task`` 的区别是不新建台账行：目标会话 / 正文 / 目录 / 标题
        全部取自台账行，派发时带同一个 dispatch_id，收尾回到同一条记录。
        返回 ``{ok, dispatch_id, state, target_session}``。
        """
        raise NotImplementedError

    def create_project(self, path: str = "") -> dict:
        """新建项目文件夹并引入为工作区。

        语义等价于用户点侧栏「添加/引入项目文件夹」并选中这个目录：目录不存在
        则逐级创建，已存在则直接复用；随后登记进工作区表，侧栏立即出现该分组，
        之后 ``dispatch_task(workdir=该目录)`` 就能在里面建会话干活。
        返回 {ok, workdir, created, display_name}；路径非法 / 无法创建时抛
        ``SecretaryDispatchError``。
        """
        raise NotImplementedError

    def open_session(self, target: str = "") -> dict:
        """切换到目标会话；返回 {ok, session_name, display_name, workdir}。"""
        raise NotImplementedError

    def list_dispatches(
        self,
        status: str = "",
        workdir: str = "",
        target: str = "",
        limit: int = 20,
    ) -> List[dict]:
        """列出派活台账（升级设计 2026-11-04 第 3.4 节）。

        过滤：``status`` / ``workdir`` 精确匹配，``target`` 按目标会话名或
        显示名做子串匹配（空串不过滤）；按 id 倒序，``limit`` 夹紧到 1..50。
        返回 list[dict]，每项含 id / title / target_display / workdir /
        status / started_at / finished_at / result_excerpt（截 200 字）。
        无命中返回空列表，由工具层转成可读提示。
        """
        raise NotImplementedError

    def get_dispatch_result(self, dispatch_id: int = 0) -> Optional[dict]:
        """按 id 取一条派活的完整详情（同上第 3.4 节）。

        返回 id / title / target_session / target_display / workdir / status /
        started_at / finished_at / prompt（完整）/ result_excerpt（完整，不截断）/
        error / depends_on / batch_id / secretary_session。

        不存在（或 id 非法）时返回 ``None``，由工具层转成「没有找到任务 #N」；
        台账不可用等配置问题抛 ``SecretaryDispatchError``。
        """
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
