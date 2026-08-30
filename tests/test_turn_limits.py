import json

import pytest

import config
from core.harness.turn_limits import (
    DEFAULT_TURN_STEP_LIMIT,
    MAX_TURN_STEP_LIMIT,
    MIN_TURN_STEP_LIMIT,
    TurnStepLimitError,
    load_turn_step_limit,
    save_turn_step_limit,
    validate_turn_step_limit,
)


def _use_tmp_config(tmp_path, monkeypatch, data):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(config, "_CONFIG_FILE", str(path))
    return path


def test_missing_turn_step_limit_uses_legacy_default(tmp_path, monkeypatch):
    _use_tmp_config(tmp_path, monkeypatch, {})
    assert load_turn_step_limit() == DEFAULT_TURN_STEP_LIMIT == 100


@pytest.mark.parametrize("value", [MIN_TURN_STEP_LIMIT, 100, MAX_TURN_STEP_LIMIT])
def test_turn_step_limit_accepts_integer_boundaries(value):
    assert validate_turn_step_limit(value) == value


@pytest.mark.parametrize("value", [9, 1001, True, False, 100.0, "100", None])
def test_turn_step_limit_rejects_invalid_explicit_values(value):
    with pytest.raises(TurnStepLimitError):
        validate_turn_step_limit(value)


def test_save_turn_step_limit_persists_validated_integer(tmp_path, monkeypatch):
    path = _use_tmp_config(tmp_path, monkeypatch, {"unrelated": "kept"})
    assert save_turn_step_limit(250) == 250
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "unrelated": "kept",
        "agent_turn_step_limit": 250,
    }


def test_save_turn_step_limit_does_not_write_invalid_value(tmp_path, monkeypatch):
    path = _use_tmp_config(tmp_path, monkeypatch, {"agent_turn_step_limit": 100})
    with pytest.raises(TurnStepLimitError):
        save_turn_step_limit(1001)
    assert json.loads(path.read_text(encoding="utf-8"))["agent_turn_step_limit"] == 100


def test_load_turn_step_limit_rejects_explicit_invalid_config(tmp_path, monkeypatch):
    _use_tmp_config(tmp_path, monkeypatch, {"agent_turn_step_limit": "100"})
    with pytest.raises(TurnStepLimitError):
        load_turn_step_limit()


def test_save_turn_step_limit_reports_atomic_write_failure(mocker):
    mocker.patch("config.save_config_value", return_value=False)
    with pytest.raises(TurnStepLimitError, match="保存失败"):
        save_turn_step_limit(100)
