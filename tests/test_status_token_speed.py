import pytest

import main
from core.turn_events import TurnEventFactory


class _RecordingApp:
    def __init__(self):
        self.invalidated = 0

    def invalidate(self):
        self.invalidated += 1


def _step_metrics_event(payload, turn_id="turn-speed"):
    return TurnEventFactory(turn_id).emit("step_metrics", payload)


def test_format_token_speed_only_accepts_measured_positive_value():
    assert main._format_token_speed(42.34) == "42.3 t/s"
    assert main._format_token_speed(1) == "1.0 t/s"
    assert main._format_token_speed(None) == "-- t/s"
    assert main._format_token_speed(0) == "-- t/s"
    assert main._format_token_speed(-3.2) == "-- t/s"
    assert main._format_token_speed("abc") == "-- t/s"
    assert main._format_token_speed(float("inf")) == "-- t/s"
    assert main._format_token_speed(float("nan")) == "-- t/s"


def test_idle_segment_shows_speed_instead_of_idle_placeholder():
    text, style = main._compose_status_task_segment("idle", 56.78)
    assert text == " 56.8 t/s "
    assert "闲置" not in text
    assert style == "class:status.task_speed"


def test_idle_segment_without_measurement_shows_dash():
    text, _style = main._compose_status_task_segment("idle", None)
    assert text == " -- t/s "


@pytest.mark.parametrize("status", ["thinking", "executing"])
def test_running_states_show_speed_without_status_text(status):
    text, style = main._compose_status_task_segment(status, 88.8)
    assert text == " 88.8 t/s "
    assert "思考" not in text and "干活" not in text
    assert style == "class:status.task_speed"


def test_waiting_still_keeps_actionable_status_text():
    text, style = main._compose_status_task_segment("waiting", 88.8)
    assert text == " 等待授权确认... "
    assert style == "class:status.task_active"


def test_unknown_status_falls_back_to_speed_segment():
    text, style = main._compose_status_task_segment("weird", 12.0)
    assert text == " 12.0 t/s "
    assert style == "class:status.task_speed"


def test_step_metrics_updates_speed_and_refreshes_status_bar(monkeypatch):
    app = _RecordingApp()
    monkeypatch.setattr(main, "app_instance", app)
    monkeypatch.setattr(main, "_last_token_speed", None)

    main._render_ui_event(
        _step_metrics_event(
            {
                "duration_s": 3.2,
                "output_duration_s": 2.5,
                "ttft_ms": 700,
                "completion_tokens": 125,
                "token_speed": 50.0,
            }
        )
    )

    assert main._last_token_speed == pytest.approx(50.0)
    assert app.invalidated == 1


def test_step_metrics_without_valid_speed_keeps_previous_value(monkeypatch):
    app = _RecordingApp()
    monkeypatch.setattr(main, "app_instance", app)
    monkeypatch.setattr(main, "_last_token_speed", 12.5)

    for payload in (
        {},
        {"token_speed": None},
        {"token_speed": 0},
        {"token_speed": -3},
        {"token_speed": "unknown"},
        {"token_speed": float("nan")},
    ):
        main._render_ui_event(_step_metrics_event(payload))

    assert main._last_token_speed == pytest.approx(12.5)
    assert app.invalidated == 0
