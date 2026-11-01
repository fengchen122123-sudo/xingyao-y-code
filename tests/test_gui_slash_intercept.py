"""GUI 桥的斜杠命令接入测试（gui_bridge.ApiBridge._dispatch_slash）。

只测「接入点」这一层：命令怎么分类是 dispatch 的职责（见 test_slash_dispatch），
这里要确认的是桥把分类结果转成了入口能用的返回结构——提示词命令返回
{"prompt": ...} 交给调用方替换正文，本地命令返回 slash 标记并弹出提示卡片。

不构造真实 ApiBridge：__init__ 会建数据库、注册 GUI 全局与线程池，成本高且与
本层无关。_dispatch_slash 自身只用 self.post_notice 一个依赖，因此用
types.SimpleNamespace/最小替身挂上它即可直接调用未绑定方法。
"""

import types

from core.slash_commands import local
from gui_bridge import ApiBridge


class _NoticeCollector:
    """最小桥替身：只实现 _dispatch_slash 会调用的 post_notice。"""

    def __init__(self):
        self.notices = []

    def post_notice(self, message: str, session_name: str = "", kind: str = "notice"):
        # 记录调用而不是真的广播，避免测试依赖前端回调
        self.notices.append({"message": message, "session_name": session_name, "kind": kind})
        return {"ok": True}


def test_dispatch_slash_returns_prompt_for_prompt_command():
    """提示词命令不能就地返回成功，必须把 prompt 交回调用方继续走模型回合。"""
    stub = types.SimpleNamespace(post_notice=_NoticeCollector().post_notice)

    result = ApiBridge._dispatch_slash(stub, "/fix boom", "session_a", "/fix boom")

    assert isinstance(result, dict)
    assert "prompt" in result
    assert "boom" in result["prompt"]


def test_dispatch_slash_handles_local_command_and_posts_notice(monkeypatch):
    """本地命令就地完成：返回 slash 标记（调用方据此不启动回合）并弹提示。"""
    # 替换 local 模块内的名字，避免真的往今日记忆日志写文件
    monkeypatch.setattr(local, "append_to_daily_log", lambda entry: True)
    collector = _NoticeCollector()
    stub = types.SimpleNamespace(post_notice=collector.post_notice)

    result = ApiBridge._dispatch_slash(
        stub, "/remember 用中文回答", "session_a", "/remember 用中文回答"
    )

    assert result is not None
    assert result.get("slash") is True
    assert result.get("ok") is True
    assert collector.notices, "本地命令完成后必须给用户一句提示"
    # kind 保持默认 notice：这是成功回执，不该被染成 error 卡片
    assert collector.notices[0]["kind"] == "notice"
    assert collector.notices[0]["session_name"] == "session_a"
