import pytest

import core.doctor as doctor
from core.doctor import CheckResult, STATUS_FAIL, STATUS_OK, STATUS_SKIP, STATUS_WARN
from gui_bridge import ApiBridge


def _bare_bridge() -> ApiBridge:
    return ApiBridge.__new__(ApiBridge)


def test_run_doctor_returns_checks_and_summary(monkeypatch):
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

    assert calls == [False]
    assert res["ok"] is True
    assert res["summary"] == {"ok": 1, "warn": 1, "fail": 1, "skip": 1}
    assert res["checks"][0] == {
        "name": "环境与版本",
        "status": "ok",
        "detail": "星瑶Code v1.0.0 · win32",
        "hint": "",
    }
    assert res["checks"][2]["hint"] == "重新填写并保存"


def test_run_doctor_forwards_skip_network(monkeypatch):
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

    def boom(*, skip_network=False):
        raise RuntimeError("自检聚合器崩溃")

    monkeypatch.setattr(doctor, "run_checks", boom)

    res = _bare_bridge().run_doctor()

    assert res["ok"] is False
    assert "崩溃" in res["msg"]
    assert res["checks"] == []
    assert res["summary"] == {"ok": 0, "warn": 0, "fail": 0, "skip": 0}


def test_run_doctor_keeps_unknown_status_out_of_summary(monkeypatch):

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
    calls = []

    def fake_run_checks(*, skip_network=False):
        calls.append(skip_network)
        return []

    monkeypatch.setattr(doctor, "run_checks", fake_run_checks)

    _bare_bridge().run_doctor(skip_network=truthy)

    assert calls == [True]