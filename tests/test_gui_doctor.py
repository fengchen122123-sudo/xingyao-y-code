"""GUI「运行自检」桥接（ApiBridge.run_doctor）单元测试（T3b）。

复用 test_gui_hook_trust.py 的 _bare_bridge 模式：ApiBridge.__new__ 绕过
__init__（run_doctor 无实例状态依赖，内部为局部导入 core.doctor.run_checks，
因此打桩需 patch core.doctor 的模块属性）。全部用例不触碰真实 ~/.ycode 与网络。
"""

import pytest

import core.doctor as doctor
from core.doctor import CheckResult, STATUS_FAIL, STATUS_OK, STATUS_SKIP, STATUS_WARN
from gui_bridge import ApiBridge


def _bare_bridge() -> ApiBridge:
    """不经 __init__ 构造裸实例（run_doctor 只用模块级 logger + 局部导入）。"""
    return ApiBridge.__new__(ApiBridge)


def test_run_doctor_returns_checks_and_summary(monkeypatch):
    """正常路径：CheckResult.to_dict 逐项透传，summary 按四态计数。"""
    calls = []

    def fake_run_checks(*, skip_network=False):
        calls.append(skip_network)
        return [
            CheckResult("环境与版本", STATUS_OK, "星瑶Code v1.0.0 · win32"),
            CheckResult("配置", STATUS_WARN, "存在未知配置键", "未知键多为拼写错误"),
            CheckResult("凭据", STATUS_FAIL, "1/1 个 API Key 解密失败", "重新填写并保存"),
            CheckResult("模型端点", STATUS_SKIP, "未配置激活模型"),
        ]

    monkeypatch.setattr(doctor, "run_checks", fake_run_checks)

    res = _bare_bridge().run_doctor()

    # GUI 由用户主动点击触发：默认完整自检（含模型端点联网探测）
    assert calls == [False]
    assert res["ok"] is True
    assert res["summary"] == {"ok": 1, "warn": 1, "fail": 1, "skip": 1}
    # schema 与 CLI `/doctor --json` 一致：name/status/detail/hint 四字段
    assert res["checks"][0] == {
        "name": "环境与版本",
        "status": "ok",
        "detail": "星瑶Code v1.0.0 · win32",
        "hint": "",
    }
    assert res["checks"][2]["hint"] == "重新填写并保存"


def test_run_doctor_forwards_skip_network(monkeypatch):
    """skip_network=True 原样透传到 run_checks（快速重跑/离线场景）。"""
    calls = []

    def fake_run_checks(*, skip_network=False):
        calls.append(skip_network)
        return []

    monkeypatch.setattr(doctor, "run_checks", fake_run_checks)

    res = _bare_bridge().run_doctor(skip_network=True)

    assert calls == [True]
    assert res == {
        "ok": True,
        "checks": [],
        "summary": {"ok": 0, "warn": 0, "fail": 0, "skip": 0},
    }


def test_run_doctor_swallows_exceptions(monkeypatch):
    """聚合器抛异常：桥接层兜底为 ok=False + 空结果，绝不让异常穿过。"""

    def boom(*, skip_network=False):
        raise RuntimeError("自检聚合器崩溃")

    monkeypatch.setattr(doctor, "run_checks", boom)

    res = _bare_bridge().run_doctor()

    assert res["ok"] is False
    assert "崩溃" in res["msg"]
    assert res["checks"] == []
    assert res["summary"] == {"ok": 0, "warn": 0, "fail": 0, "skip": 0}


def test_run_doctor_keeps_unknown_status_out_of_summary(monkeypatch):
    """未知 status：原文透传给前端兜底渲染，但不得污染 summary 计数。"""

    class WeirdResult:
        def to_dict(self):
            return {"name": "X", "status": "mystery", "detail": "", "hint": ""}

    monkeypatch.setattr(
        doctor, "run_checks", lambda *, skip_network=False: [WeirdResult()]
    )

    res = _bare_bridge().run_doctor()

    assert res["ok"] is True
    assert res["checks"][0]["status"] == "mystery"
    assert res["summary"] == {"ok": 0, "warn": 0, "fail": 0, "skip": 0}


@pytest.mark.parametrize("truthy", ["1", 1])
def test_run_doctor_normalises_skip_network_to_bool(monkeypatch, truthy):
    """pywebview 可能把 JS 值透传为字符串等真值：统一归一为 bool 再下发。"""
    calls = []

    def fake_run_checks(*, skip_network=False):
        calls.append(skip_network)
        return []

    monkeypatch.setattr(doctor, "run_checks", fake_run_checks)

    _bare_bridge().run_doctor(skip_network=truthy)

    assert calls == [True]