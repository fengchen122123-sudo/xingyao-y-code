import atexit
import gc
import logging
import os
import shutil
import sys
import tempfile

import pytest


_TEST_DATA_DIR = tempfile.mkdtemp(prefix="ycode-pytest-")
os.environ["YCODE_DATA_DIR"] = _TEST_DATA_DIR
atexit.register(shutil.rmtree, _TEST_DATA_DIR, ignore_errors=True)


@pytest.fixture(autouse=True)
def _restore_entry_layer_globals():
    from core import app_context
    from core import llm
    from core import mcp_client
    from core import permission_broker as broker_module
    from core import task_manager
    from core.tools import subagent_manager

    app_context_snapshot = (
        app_context._context,
        app_context._ui_flavor,
        app_context._active_model_tuple,
        app_context._current_session_fallback,
        app_context._manager_fallback,
        app_context._permission_mode_fallback,
    )
    broker = broker_module.permission_broker
    with broker._lock:
        broker_snapshot = (
            broker._has_prompt_handlers,
            broker._activate_prompt,
            broker._dismiss_prompt,
        )
    callback_snapshot = (
        llm._token_usage_listener,
        mcp_client._status_logger,
        subagent_manager._on_terminal,
        task_manager._on_task_finished,
    )
    yield
    (
        app_context._context,
        app_context._ui_flavor,
        app_context._active_model_tuple,
        app_context._current_session_fallback,
        app_context._manager_fallback,
        app_context._permission_mode_fallback,
    ) = app_context_snapshot
    with broker._lock:
        (
            broker._has_prompt_handlers,
            broker._activate_prompt,
            broker._dismiss_prompt,
        ) = broker_snapshot
    (
        token_usage_listener,
        status_logger,
        subagent_terminal,
        task_finished,
    ) = callback_snapshot
    llm.set_token_usage_listener(token_usage_listener)
    mcp_client.set_status_logger(status_logger)
    subagent_manager.set_on_terminal(subagent_terminal)
    task_manager.set_task_finished_callback(task_finished)
    try:
        from core.tools import _clear_sandbox_state_for_tests

        _clear_sandbox_state_for_tests()
    except Exception:
        pass
    try:
        from core.computer.capability import take_turn_computer_capability

        take_turn_computer_capability()
    except Exception:
        pass
    try:
        from core.computer import stop_notice

        stop_notice.deactivate_user_stop("test_reset")
    except Exception:
        pass
    feishu_bridge_mod = sys.modules.get("core.feishu.bridge")
    if feishu_bridge_mod is not None and hasattr(feishu_bridge_mod, "stop_all_watchdogs"):
        try:
            feishu_bridge_mod.stop_all_watchdogs()
        except Exception:
            pass


def pytest_sessionfinish(session, exitstatus):
    managers = []
    main_module = sys.modules.get("main")
    if main_module is not None:
        managers.append(getattr(main_module, "manager", None))
    try:
        from core import app_context
        managers.append(app_context.get_manager())
    except Exception:
        pass
    for manager in managers:
        if manager is not None and hasattr(manager, "close"):
            try:
                manager.close()
            except Exception:
                pass

    warning_logger = logging.getLogger("py.warnings")
    for handler in list(warning_logger.handlers):
        if not getattr(handler, "_ycode_runtime_warning_handler", False):
            continue
        warning_logger.removeHandler(handler)
        handler.close()
    gc.collect()
    shutil.rmtree(_TEST_DATA_DIR, ignore_errors=True)
