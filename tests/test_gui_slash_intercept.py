import types

from core.slash_commands import local
from gui_bridge import ApiBridge


class _NoticeCollector:

    def __init__(self):
        self.notices = []

    def post_notice(self, message: str, session_name: str = "", kind: str = "notice"):
        self.notices.append({"message": message, "session_name": session_name, "kind": kind})
        return {"ok": True}


def test_dispatch_slash_returns_prompt_for_prompt_command():
    stub = types.SimpleNamespace(post_notice=_NoticeCollector().post_notice)

    result = ApiBridge._dispatch_slash(stub, "/fix boom", "session_a", "/fix boom")

    assert isinstance(result, dict)
    assert "prompt" in result
    assert "boom" in result["prompt"]


def test_dispatch_slash_handles_local_command_and_posts_notice(monkeypatch):
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
    assert collector.notices[0]["kind"] == "notice"
    assert collector.notices[0]["session_name"] == "session_a"
