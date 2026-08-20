from core.permission_broker import CANCELLED_RESULT, PermissionBroker
import threading
import time


def test_broker_activates_only_one_request_at_a_time():
    activated = []
    broker = PermissionBroker(activate_prompt=activated.append)

    first = broker.enqueue(
        "execute_command", {"command": "one"}, "child-1", "turn-1", "子代理一"
    )
    second = broker.enqueue(
        "execute_command", {"command": "two"}, "child-2", "turn-1", "子代理二"
    )

    assert [request.request_id for request in activated] == [first.request_id]
    assert broker.resolve(first.request_id, 0) is True
    assert [request.request_id for request in activated] == [
        first.request_id,
        second.request_id,
    ]
    assert first.future.result() == 0


def test_stale_request_cannot_resolve_the_active_future():
    broker = PermissionBroker(activate_prompt=lambda request: None)
    request = broker.enqueue("write_file", {}, "root", "turn-1", "主代理")

    assert broker.resolve("missing-request", 0) is False
    assert request.future.done() is False
    assert broker.active_request().request_id == request.request_id


def test_resolving_last_request_dismisses_the_prompt():
    dismissed = []
    broker = PermissionBroker(
        activate_prompt=lambda request: None,
        dismiss_prompt=lambda: dismissed.append(True),
    )
    request = broker.enqueue("write_file", {}, "root", "turn-1", "主代理")

    assert broker.resolve(request.request_id, 1) is True

    assert request.future.result() == 1
    assert broker.active_request() is None
    assert dismissed == [True]


def test_cancel_turn_rejects_active_and_queued_requests():
    activated = []
    dismissed = []
    broker = PermissionBroker(
        activate_prompt=activated.append,
        dismiss_prompt=lambda: dismissed.append(True),
    )
    first = broker.enqueue("write_file", {}, "child-1", "turn-1", "一")
    second = broker.enqueue("edit_file", {}, "child-2", "turn-1", "二")

    assert broker.cancel_turn("turn-1") == 2

    assert first.future.result() == CANCELLED_RESULT
    assert second.future.result() == CANCELLED_RESULT
    assert broker.active_request() is None
    assert dismissed == [True]


def test_cancel_owner_preserves_other_owners_in_queue():
    activated = []
    broker = PermissionBroker(activate_prompt=activated.append)
    first = broker.enqueue("write_file", {}, "child-1", "turn-1", "一")
    second = broker.enqueue("edit_file", {}, "child-2", "turn-1", "二")

    assert broker.cancel_owner("child-1") == 1

    assert first.future.result() == CANCELLED_RESULT
    assert first.request_id != second.request_id
    assert broker.active_request().request_id == second.request_id
    assert [request.request_id for request in activated] == [
        first.request_id,
        second.request_id,
    ]


def test_broker_reports_active_or_queued_owner():
    broker = PermissionBroker(activate_prompt=lambda request: None)
    first = broker.enqueue("write_file", {}, "child-1", "turn-1", "一")
    second = broker.enqueue("edit_file", {}, "child-2", "turn-1", "二")

    assert broker.has_owner("child-1") is True
    assert broker.has_owner("child-2") is True
    assert broker.has_owner("missing") is False

    assert broker.resolve(first.request_id, 0) is True
    assert broker.has_owner("child-1") is False
    assert broker.has_owner("child-2") is True
    assert broker.resolve(second.request_id, 1) is True
    assert broker.has_owner("child-2") is False


def test_permission_checks_from_two_threads_are_presented_serially(monkeypatch):
    import core.llm as llm
    import main

    class ImmediateLoop:
        @staticmethod
        def call_soon_threadsafe(callback):
            callback()

    class FakeApp:
        loop = ImmediateLoop()

        @staticmethod
        def invalidate():
            return None

    def wait_until(predicate):
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.001)
        raise AssertionError("condition was not reached")

    broker = PermissionBroker()
    monkeypatch.setattr(llm, "permission_broker", broker)
    monkeypatch.setattr(main, "permission_broker", broker)
    broker.set_prompt_handlers(
        main._activate_permission_request,
        main._dismiss_permission_prompt,
    )
    monkeypatch.setattr(main, "app_instance", FakeApp())
    monkeypatch.setattr(main, "current_permission_mode", "confirm")
    monkeypatch.setattr(main, "confirm_prompt", None)
    monkeypatch.setattr(llm, "safe_print", lambda *args, **kwargs: None)
    llm.session_allowed_commands.clear()

    results = {}

    def request_permission(label):
        results[label] = llm.check_permission_and_ask(
            "execute_command",
            {"command": label},
            permission_owner=label,
            parent_turn_id="turn-1",
            permission_source=f"来源：{label}",
        )

    first = threading.Thread(target=request_permission, args=("one",))
    second = threading.Thread(target=request_permission, args=("two",))
    first.start()
    wait_until(lambda: main.confirm_prompt is not None)
    first_request_id = main.confirm_prompt["request_id"]
    second.start()
    wait_until(lambda: broker.active_request() is not None)

    assert main.confirm_prompt["request_id"] == first_request_id
    assert main._resolve_confirm_prompt(0) is True
    wait_until(lambda: main.confirm_prompt is not None and main.confirm_prompt["request_id"] != first_request_id)
    assert main.confirm_prompt["arguments"]["command"] == "two"
    assert main._resolve_confirm_prompt(1) is True

    first.join(timeout=1)
    second.join(timeout=1)
    assert first.is_alive() is False
    assert second.is_alive() is False
    assert results == {"one": True, "two": False}


def test_ask_user_uses_the_same_broker_with_custom_options(monkeypatch):
    import json
    import core.ask_user as ask_user_module
    import main

    class FakeApp:
        loop = object()

    activated = []
    broker = PermissionBroker(activate_prompt=activated.append)
    monkeypatch.setattr(ask_user_module, "permission_broker", broker, raising=False)
    monkeypatch.setattr(main, "app_instance", FakeApp())
    result = {}

    thread = threading.Thread(
        target=lambda: result.setdefault(
            "value",
            ask_user_module.ask_user(
                "选择方式",
                ["串行", "并行"],
                owner_id="child-1",
                parent_turn_id="turn-1",
                source="子代理：规划",
            ),
        )
    )
    thread.start()
    deadline = time.monotonic() + 1
    while not activated and time.monotonic() < deadline:
        time.sleep(0.001)

    assert activated[0].options == ("串行", "并行")
    assert activated[0].source == "子代理：规划"
    assert broker.resolve(activated[0].request_id, 1) is True
    thread.join(timeout=1)

    payload = json.loads(result["value"])
    assert payload["selected_index"] == 1
    assert payload["selected"] == "并行"


def test_ask_user_reports_explicit_cancellation(monkeypatch):
    import json
    import core.ask_user as ask_user_module
    import main

    class FakeApp:
        loop = object()

    broker = PermissionBroker(activate_prompt=lambda request: None)
    monkeypatch.setattr(ask_user_module, "permission_broker", broker, raising=False)
    monkeypatch.setattr(main, "app_instance", FakeApp())
    result = {}

    thread = threading.Thread(
        target=lambda: result.setdefault(
            "value",
            ask_user_module.ask_user(
                "选择城市",
                ["北京", "上海"],
                owner_id="cancelled-child",
                parent_turn_id="turn-cancelled-child",
            ),
        )
    )
    thread.start()
    deadline = time.monotonic() + 1
    while not broker.has_owner("cancelled-child") and time.monotonic() < deadline:
        time.sleep(0.001)

    assert broker.cancel_owner("cancelled-child") == 1
    thread.join(timeout=1)
    assert json.loads(result["value"])["status"] == "cancelled"


def test_ask_user_rejects_non_sequence_options():
    import core.ask_user as ask_user_module

    assert "选项必须是列表" in ask_user_module.ask_user("选择", "北京")


def test_ask_user_out_of_range_index_is_reported_as_cancelled(monkeypatch):
    import json
    import core.ask_user as ask_user_module
    import main

    class FakeApp:
        loop = object()

    activated = []
    broker = PermissionBroker(activate_prompt=activated.append)
    monkeypatch.setattr(ask_user_module, "permission_broker", broker, raising=False)
    monkeypatch.setattr(main, "app_instance", FakeApp())
    result = {}

    thread = threading.Thread(
        target=lambda: result.setdefault(
            "value",
            ask_user_module.ask_user(
                "选择城市",
                ["北京", "上海"],
                owner_id="child-range",
                parent_turn_id="turn-range",
            ),
        )
    )
    thread.start()
    deadline = time.monotonic() + 1
    while not activated and time.monotonic() < deadline:
        time.sleep(0.001)

    assert broker.resolve(activated[0].request_id, 5) is True
    thread.join(timeout=1)

    payload = json.loads(result["value"])
    assert payload["status"] == "cancelled"
    assert payload["selected"] is None


def test_permission_wait_breaks_out_promptly_on_cancel_event(monkeypatch):
    """future.result 轮询等待：cancel_event 置位后立即返回，线程不再永久挂起。"""
    import core.llm as llm
    import main

    class FakeApp:
        loop = object()

    broker = PermissionBroker(activate_prompt=lambda request: None)
    monkeypatch.setattr(llm, "permission_broker", broker)
    monkeypatch.setattr(main, "permission_broker", broker)
    monkeypatch.setattr(main, "app_instance", FakeApp())
    monkeypatch.setattr(main, "current_permission_mode", "confirm")
    monkeypatch.setattr(llm, "safe_print", lambda *args, **kwargs: None)
    llm.session_allowed_commands.clear()

    cancel_event = threading.Event()
    results = {}

    def request_permission():
        results["approved"] = llm.check_permission_and_ask(
            "execute_command",
            {"command": "make build"},
            event_callback=lambda *args, **kwargs: None,
            permission_owner="turn-cancel-wait",
            parent_turn_id="turn-cancel-wait",
            cancel_event=cancel_event,
        )

    thread = threading.Thread(target=request_permission)
    thread.start()
    deadline = time.monotonic() + 1
    while not broker.has_owner("turn-cancel-wait") and time.monotonic() < deadline:
        time.sleep(0.001)
    cancel_event.set()
    thread.join(timeout=2)

    assert thread.is_alive() is False
    assert results["approved"] is False
    assert broker.has_owner("turn-cancel-wait") is False


def test_resolve_out_of_range_choice_is_treated_as_cancelled():
    """resolve 越界 choice_index 不抛 KeyError，统一按取消处理并推进队列。"""
    activated = []
    broker = PermissionBroker(activate_prompt=activated.append)
    first = broker.enqueue("write_file", {}, "child-1", "turn-1", "一")
    second = broker.enqueue("edit_file", {}, "child-2", "turn-1", "二")

    assert broker.resolve(first.request_id, 99) is True

    assert first.future.result() == CANCELLED_RESULT
    # 队列正常推进，下一个请求被激活
    assert broker.active_request().request_id == second.request_id
    assert [request.request_id for request in activated] == [
        first.request_id,
        second.request_id,
    ]


def test_resolve_negative_choice_is_treated_as_cancelled():
    broker = PermissionBroker(activate_prompt=lambda request: None)
    request = broker.enqueue("write_file", {}, "child-1", "turn-1", "一")

    assert broker.resolve(request.request_id, -2) is True
    assert request.future.result() == CANCELLED_RESULT
    assert broker.active_request() is None


def test_permission_poll_exception_releases_enqueued_request(monkeypatch):
    """轮询等待期间发生异常（如 KeyboardInterrupt）时回收 broker 中的请求。"""
    import core.llm as llm
    import main

    class FakeApp:
        loop = object()

    class ExplodingFuture:
        def done(self):
            return False

        def result(self, timeout=None):
            raise KeyboardInterrupt

    class FakeBroker:
        def __init__(self):
            self.cancelled = []

        def enqueue(self, *args, **kwargs):
            from types import SimpleNamespace

            return SimpleNamespace(request_id="req-1", future=ExplodingFuture())

        def cancel_owner(self, owner_id):
            self.cancelled.append(owner_id)
            return 1

    broker = FakeBroker()
    monkeypatch.setattr(llm, "permission_broker", broker)
    monkeypatch.setattr(main, "app_instance", FakeApp())
    monkeypatch.setattr(main, "current_permission_mode", "confirm")
    llm.session_allowed_commands.clear()

    try:
        llm.check_permission_and_ask(
            "execute_command",
            {"command": "make build"},
            event_callback=lambda *args, **kwargs: None,
            permission_owner="turn-explode",
            parent_turn_id="turn-explode",
        )
        raised = False
    except KeyboardInterrupt:
        raised = True

    assert raised is True
    assert broker.cancelled == ["turn-explode"]


def test_ask_user_poll_exception_releases_enqueued_request(monkeypatch):
    """ask_user 轮询异常退出时同样回收 broker 请求，避免后续提示永久排队。"""
    import core.ask_user as ask_user_module
    import main

    class FakeApp:
        loop = object()

    class ExplodingFuture:
        def done(self):
            return False

        def result(self, timeout=None):
            raise KeyboardInterrupt

    class FakeBroker:
        def __init__(self):
            self.cancelled = []

        def has_prompt_handlers(self):
            return True

        def enqueue(self, *args, **kwargs):
            from types import SimpleNamespace

            return SimpleNamespace(request_id="req-ask", future=ExplodingFuture())

        def cancel_owner(self, owner_id):
            self.cancelled.append(owner_id)
            return 1

    broker = FakeBroker()
    monkeypatch.setattr(ask_user_module, "permission_broker", broker, raising=False)
    monkeypatch.setattr(main, "app_instance", FakeApp())

    try:
        ask_user_module.ask_user(
            "选择城市",
            ["北京", "上海"],
            owner_id="child-explode",
            parent_turn_id="turn-explode",
        )
        raised = False
    except KeyboardInterrupt:
        raised = True

    assert raised is True
    assert broker.cancelled == ["child-explode"]


def test_ask_user_custom_answer_is_forwarded_as_custom_payload(monkeypatch):
    """GUI 第四项输入框 / CLI 直接输入：自由文本回答原样透传给模型。"""
    import json
    import core.ask_user as ask_user_module
    import main

    class FakeApp:
        loop = object()

    activated = []
    broker = PermissionBroker(activate_prompt=activated.append)
    monkeypatch.setattr(ask_user_module, "permission_broker", broker, raising=False)
    monkeypatch.setattr(main, "app_instance", FakeApp())
    result = {}

    thread = threading.Thread(
        target=lambda: result.setdefault(
            "value",
            ask_user_module.ask_user(
                "选择方案",
                ["方案A", "方案B"],
                owner_id="child-custom",
                parent_turn_id="turn-custom",
            ),
        )
    )
    thread.start()
    deadline = time.monotonic() + 1
    while not activated and time.monotonic() < deadline:
        time.sleep(0.001)

    assert broker.resolve(activated[0].request_id, -1, custom_answer="  两个都不要，用方案C ") is True
    thread.join(timeout=1)

    payload = json.loads(result["value"])
    assert payload["custom"] is True
    assert payload["selected"] == "两个都不要，用方案C"
    assert payload["selected_index"] == -1


def test_custom_answer_for_non_ask_user_is_treated_as_cancelled():
    """权限确认（允许/拒绝类）不开放自由文本：按取消兜底，不产出字符串结果。"""
    broker = PermissionBroker(activate_prompt=lambda request: None)
    request = broker.enqueue("execute_command", {"command": "rm -rf"}, "root", "turn-1", "主代理")

    assert broker.resolve(request.request_id, 0, custom_answer="随便说点什么") is True
    assert request.future.result() == CANCELLED_RESULT


def test_resolve_custom_answer_ignores_choice_index(monkeypatch):
    """自定义回答时 choice_index 不参与语义（前端传 -1 也不按取消处理）。"""
    broker = PermissionBroker(activate_prompt=lambda request: None)
    request = broker.enqueue("ask_user", {"question": "选哪个"}, "root", "turn-1", "主代理")

    assert broker.resolve(request.request_id, -1, custom_answer="自定义内容") is True
    assert request.future.result() == "自定义内容"
