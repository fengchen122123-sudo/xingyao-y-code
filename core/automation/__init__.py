"""星轨自动化任务：GUI 常驻调度引擎的领域包。

分层纪律（见 待开发/星轨自动化任务/01-完整设计规格.md 第 6 节）：
- scheduling.py  触发时间计算纯函数，可脱离 GUI/DB 测试；
- models.py      常量、任务字段校验与 UI 摘要；
- storage.py     SQLite 存取（复用 MemoryManager 连接与锁，CAS 认领）；
- engine.py      调度线程（只做认领与分发，绝不做 LLM 请求、不触碰 JS 桥）。

与 core/task_scheduler.py（计划项依赖调度器）职责无关，互不调用。

- service.py     进程级服务句柄：GUI 注入回调，Agent 工具经 get() 访问自动化能力
                 （纯依赖注入，不 import gui_bridge，未注册时返回 None）。
"""

from core.automation import service

__all__ = ["service"]
