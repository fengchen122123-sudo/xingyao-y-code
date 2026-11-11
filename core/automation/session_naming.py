"""运行会话名生成（纯函数层）。

设计规格第 3 节：per_run 模式下每次运行前新建会话，会话名为
``{YYYY-MM-DD} {任务名}``。名字里刻意不带触发时刻，因此同一任务同一天
无论触发多少次（含手动补跑）都会落到同一个会话，历史自然按天切开。

本模块不读时钟、不碰数据库、不 import GUI 模块：当前时间由调用方传入，
保证可穷举单测。
"""

from __future__ import annotations

from datetime import datetime

from core.automation.models import MAX_SESSION_CHARS

# 会话名里的日期部分格式（本地墙钟日期，不含时分秒）。
SESSION_DATE_FORMAT = "%Y-%m-%d"


def build_run_session_name(task_name: str, now: datetime) -> str:
    """生成运行会话名：``"{YYYY-MM-DD} {任务名}"``。

    - ``task_name`` 两边空白被去掉；去空白后为空则只返回日期部分；
    - 结果超过 ``MAX_SESSION_CHARS``（100）时按字符截断；
    - 不写触发时刻，同一任务同一天多次调用返回同一名字。
    """
    date_part = f"{now:{SESSION_DATE_FORMAT}}"
    name = str(task_name or "").strip()
    if not name:
        return date_part
    return f"{date_part} {name}"[:MAX_SESSION_CHARS]