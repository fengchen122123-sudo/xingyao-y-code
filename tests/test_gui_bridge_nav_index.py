# tests/test_gui_bridge_nav_index.py — 红点导航全量索引（TDD，先行失败）
"""分页懒加载下，红点导航需要全量 user 消息索引（id+预览）来渲染全部红点；
正文仍分页。同时气泡结构必须透传 _msg_id 作为跨页跳转锚点。"""
import threading

from core.memory import MemoryManager
from gui_bridge import ApiBridge


def _bare_bridge():
    bridge = ApiBridge.__new__(ApiBridge)
    bridge._memory = MemoryManager(":memory:")
    bridge._current_session = "s"
    bridge._turn_start_lock = threading.RLock()
    bridge._message_queues = {}
    bridge._active_turns = {}
    bridge._session_last_errors = {}
    bridge._session_last_errors_lock = threading.Lock()
    return bridge


def _seed(mem, session, n):
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        mem.add_message(session, role, f"msg-{i}")


def test_nav_index_returns_all_user_messages_regardless_of_paging():
    """120 条消息（60 user）→ 索引 60 条全量、id 正序，不受分页影响。"""
    bridge = _bare_bridge()
    _seed(bridge._memory, "s", 120)

    res = bridge.get_session_nav_index("s")

    assert res["ok"] is True
    assert res["total"] == 60
    ids = [it["id"] for it in res["items"]]
    assert len(ids) == 60
    assert ids == sorted(ids)
    assert all(it["preview"] for it in res["items"])
    assert res["items"][0]["preview"].startswith("msg-0")


def test_nav_index_preview_strips_context_suffix_and_folds_ws():
    """预览剥离运行上下文后缀、折叠连续空白。"""
    bridge = _bare_bridge()
    bridge._memory.add_message(
        "s", "user",
        "查一下库存\n\n【当前运行上下文】\n当前模型：x\n\n【当前时间】2026-09-15 10:00:00")

    res = bridge.get_session_nav_index("s")

    assert res["items"][0]["preview"] == "查一下库存"


def test_nav_index_preview_truncates_long_text():
    """超长消息预览截断到 100 字 + 省略号。"""
    bridge = _bare_bridge()
    bridge._memory.add_message("s", "user", "字" * 200)

    res = bridge.get_session_nav_index("s")

    preview = res["items"][0]["preview"]
    assert len(preview) == 101
    assert preview.endswith("…")


def test_nav_index_pure_image_message_placeholder():
    """纯图片（无文本 blocks）消息预览为 [图片] 占位。"""
    bridge = _bare_bridge()
    bridge._memory.add_message("s", "user", [{"type": "image", "url": "x.png"}])

    res = bridge.get_session_nav_index("s")

    assert res["items"][0]["preview"] == "[图片]"


def test_nav_index_unknown_or_empty_session_returns_empty():
    bridge = _bare_bridge()
    res = bridge.get_session_nav_index("ghost")
    assert res == {"ok": True, "items": [], "total": 0}

    bridge._memory.create_session_explicit("empty")
    res2 = bridge.get_session_nav_index("empty")
    assert res2 == {"ok": True, "items": [], "total": 0}


def test_load_session_messages_carries_msg_id_anchor():
    """气泡结构透传 _msg_id（跨页跳转的 DOM 锚点依据）。"""
    bridge = _bare_bridge()
    _seed(bridge._memory, "s", 4)

    msgs, has_more, oldest_id = bridge._load_session_messages("s")

    assert has_more is False
    ids = [m.get("_msg_id") for m in msgs]
    assert all(isinstance(i, int) for i in ids)
    assert ids == sorted(ids)
    assert msgs[0]["_msg_id"] == oldest_id


def test_nav_index_skips_observation_followup_messages():
    """电脑操控观察跟进消息不是用户发送的：红点导航一并跳过。"""
    import json

    from core.computer.provider_generic import _observation_followup_message

    class _Obs:
        target = "browser"
        elements = [
            {"ref": "el-1", "kind": "link", "name": "主页",
             "bounds": (10, 20, 30, 40), "enabled": True},
        ]
        png_path = ""

    bridge = _bare_bridge()
    bridge._memory.add_message("s", "user", "帮我打开浏览器看看")
    followup = _observation_followup_message(_Obs(), run_id="run-1", ordinal=1)
    bridge._memory.add_message("s", "user", json.dumps(followup, ensure_ascii=False))

    res = bridge.get_session_nav_index("s")

    assert res["ok"] is True
    assert res["total"] == 1
    assert res["items"][0]["preview"] == "帮我打开浏览器看看"


def test_nav_index_and_bubbles_skip_system_injected_messages():
    """后台任务回报/护栏催促/hook 上下文等系统注入 user 消息：
    红点导航与对话气泡都不出现，只留用户真实发送的内容。"""
    bridge = _bare_bridge()
    bridge._memory.add_message("s", "user", "真正的问题")
    bridge._memory.add_message("s", "user", (
        "<task-notification>\n"
        "  <task-id>subagent-d66d7cca</task-id>\n"
        "  <status>timed_out</status>\n"
        "  <summary>子任务「超 小范」timed_out</summary>\n"
        "</task-notification>"
    ))
    bridge._memory.add_message(
        "s", "user",
        "计划尚未执行完毕，仍有可执行的计划项: ['P1'](登录修复)。"
        "请继续调用 run_subagent(plan_item_id=...) 执行剩余计划项。",
    )
    bridge._memory.add_message(
        "s", "user",
        "【Stop hook 追加上下文】\n继续执行剩余工作",
    )

    res = bridge.get_session_nav_index("s")

    assert res["ok"] is True
    assert res["total"] == 1
    assert res["items"][0]["preview"] == "真正的问题"

    msgs, _, _ = bridge._load_session_messages("s")
    user_msgs = [m for m in msgs if m["role"] == "user"]
    assert [m["content"] for m in user_msgs] == ["真正的问题"]


def test_nav_index_and_bubbles_skip_internal_turn_injection_messages():
    """内部回合的注入指令（后台子代理汇总 / 后台命令总结）与用户输入同形
    （都是 role=user）：红点导航与对话气泡都必须跳过，否则红点会被污染。"""
    bridge = _bare_bridge()
    bridge._memory.add_message("s", "user", "帮我做两个后台任务测试")
    bridge._memory.add_message("s", "user", (
        "请汇总所有后台子代理任务的结果并告诉我。如果某些任务失败了，"
        "可以用你自身的工具（如 search_news / fetch_url 等）接手完成；"
        "但已被用户取消的任务不要接手或重试，如实说明即可。"
    ))
    bridge._memory.add_message("s", "user", (
        "有一个后台命令任务执行完毕，请用一句话简短总结它的结果"
        "（成功/失败 + 关键信息，不要复述命令本身）。\n"
        "命令: python a.py\n退出码: 0（0=成功，非0=失败）\n日志尾部:\nok"
    ))

    res = bridge.get_session_nav_index("s")

    assert res["ok"] is True
    assert res["total"] == 1
    assert res["items"][0]["preview"] == "帮我做两个后台任务测试"

    msgs, _, _ = bridge._load_session_messages("s")
    user_msgs = [m for m in msgs if m["role"] == "user"]
    assert [m["content"] for m in user_msgs] == ["帮我做两个后台任务测试"]
