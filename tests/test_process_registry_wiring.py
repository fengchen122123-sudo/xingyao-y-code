"""进程注册表与命令执行链路接线测试（规格 §9.2 接入点）。"""

import sys
import time

from core.git import process_registry as reg


def test_execute_command_registers_and_unregisters(monkeypatch):
    import core.tools as tools

    calls = {"reg": 0, "unreg": 0}
    real_reg = reg.register_process
    real_unreg = reg.unregister_process

    def fake_reg(ws, handle=None, **kw):
        calls["reg"] += 1
        assert kw.get("kind") == "execute_command"
        assert kw.get("foreground") is True
        record = real_reg(ws, handle, **kw)
        assert reg.active_processes(ws), "执行期间应处于登记状态"
        return record

    def fake_unreg(handle):
        calls["unreg"] += 1
        return real_unreg(handle)

    monkeypatch.setattr(reg, "register_process", fake_reg)
    monkeypatch.setattr(reg, "unregister_process", fake_unreg)

    result = tools.execute_command("echo ok")
    assert "ok" in result
    assert calls["reg"] == 1, f"应登记一次: {calls}"
    assert calls["unreg"] == 1, f"结束后应注销一次: {calls}"


def test_background_task_registers_and_unregisters(tmp_path, monkeypatch):
    import core.task_manager as tm

    monkeypatch.setattr(tm, "get_workdir", lambda: str(tmp_path))
    reg.clear_registry()
    try:
        task_id = tm.start_background_task(
            f'"{sys.executable}" -c "import time; time.sleep(0.3)"'
        )
        assert task_id
        records = reg.active_processes(str(tmp_path))
        assert any(r.handle == task_id for r in records), "后台任务应处于登记状态"
        assert all(r.long_running for r in records if r.handle == task_id)

        # 等待监听线程察觉退出并注销（0.5s 节拍）
        deadline = time.time() + 6
        while time.time() < deadline:
            if not any(r.handle == task_id for r in reg.active_processes(str(tmp_path))):
                break
            time.sleep(0.1)
        assert not any(
            r.handle == task_id for r in reg.active_processes(str(tmp_path))
        ), "任务退出后应从注册表注销"
    finally:
        reg.clear_registry()
