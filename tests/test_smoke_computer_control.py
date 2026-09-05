"""人工烟测脚本 scripts/smoke_computer_control.py 的代码路径测试（Task 16）。

烟测脚本只由人工显式运行（真实键鼠/截屏/浏览器绝不进 pytest）；本文件用
注入 fake backend + monkeypatch input 验证其代码路径：--desktop-input 的
确认门（缺确认/错确认 exit 2 且零触碰）、单步 Enter 确认的固定
move/click/type 序列、非 Enter 输入立即中止、测试后 release/close。
"""

from types import SimpleNamespace

import pytest

import scripts.smoke_computer_control as smoke


class _FakeSmokeBackend:
    """desktop_input 代码路径的 fake backend：绝不触碰真实键鼠/截屏。"""

    def __init__(self):
        self.log = []

    @classmethod
    def create_default(cls):
        return cls()

    def start(self):
        self.log.append("start")

    def observe(self, run_id):
        self.log.append(f"observe:{run_id}")
        return SimpleNamespace(width=1000, height=600)

    def execute(self, run_id, action):
        self.log.append(("execute", action.kind, action.x, action.y, action.text))

    def release_inputs(self):
        self.log.append("release")

    def close(self):
        self.log.append("close")


@pytest.fixture
def fake_backend(monkeypatch):
    backend = _FakeSmokeBackend()
    created = []
    monkeypatch.setattr(
        "core.computer.windows_backend.WindowsComputerBackend",
        type(
            "PatchedBackend",
            (),
            {
                "create_default": staticmethod(lambda: (created.append(backend), backend)[1]),
            },
        ),
    )
    return backend, created


def _answers(monkeypatch, sequence):
    """把 builtins.input 换成按序弹出的 fake（每项模拟一次回车输入）。"""
    remaining = list(sequence)

    def _fake_input(_prompt=""):
        if not remaining:
            raise AssertionError("input 被调用次数超出预期")
        return remaining.pop(0)

    monkeypatch.setattr("builtins.input", _fake_input)
    return remaining


def test_desktop_input_without_confirmation_exits_2_without_touching(fake_backend):
    _backend, created = fake_backend
    assert smoke.desktop_input(None) == 2
    assert smoke.desktop_input("yes") == 2
    assert smoke.desktop_input("i_understand") == 2  # 大小写敏感
    assert created == []  # 未创建 backend：零触碰


def test_desktop_input_runs_confirmed_move_click_type_then_releases(
    monkeypatch, fake_backend
):
    backend, created = fake_backend
    remaining = _answers(
        monkeypatch, ["", "", "", ""]  # 前置说明 + 三个动作步骤各一次 Enter
    )

    assert smoke.desktop_input("I_UNDERSTAND") == 0
    assert len(created) == 1
    assert remaining == []
    assert [entry if isinstance(entry, str) else entry[1] for entry in backend.log] == [
        "start",
        "observe:smoke-desktop-input",
        "move",
        "click",
        "type_text",
        "release",
        "close",
    ]
    executed = [entry for entry in backend.log if isinstance(entry, tuple)]
    # move/click 作用于截图中心；type_text 敲固定测试文本（内容脱敏长度）
    assert executed[0][2] == 500 and executed[0][3] == 300
    assert executed[1][2] == 500 and executed[1][3] == 300
    assert executed[2][4] == smoke.SMOKE_TYPE_TEXT


def test_desktop_input_aborts_on_non_enter_answer(monkeypatch, fake_backend):
    backend, _created = fake_backend
    remaining = _answers(
        monkeypatch, ["", "", "stop"]  # 步骤 2（click）输入非 Enter：立即中止
    )

    assert smoke.desktop_input("I_UNDERSTAND") == 2
    assert remaining == []
    kinds = [entry if isinstance(entry, str) else entry[1] for entry in backend.log]
    assert kinds == ["start", "observe:smoke-desktop-input", "move", "close"]
    # 中止路径：后续动作未执行；backend.close 在 finally 中仍被调用（close
    # 内部会 release_inputs），绝不遗留输入状态或截图目录


def test_main_without_modes_prints_help_and_exits_2(capsys):
    assert smoke.main([]) == 2
    assert "--desktop-input" in capsys.readouterr().out
