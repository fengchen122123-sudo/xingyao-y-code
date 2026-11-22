from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.computer.models import (
    ComputerCapability,
    ComputerObservation,
    ComputerOwner,
    ComputerProtocol,
)
from tests.computer_fakes import FakeComputerBackend, fake_png_bytes


def test_select_candidate_requires_unique_exact_model_and_protocol():
    import scripts.benchmark_computer_model_loop as bench

    candidates = [
        {"model": "openai/gpt-4o", "protocol": "openai", "api_base": "https://a", "api_key": "sk-secret-1"},
        {"model": "anthropic/claude-sonnet", "protocol": "generic", "api_base": "https://b", "api_key": "sk-secret-2"},
        {"model": "openai/gpt-4o-mini", "protocol": "generic", "api_base": "https://c", "api_key": "sk-secret-3"},
    ]
    with pytest.raises(ValueError):
        bench.select_candidate(candidates, model="gpt-4o", protocol="")
    with pytest.raises(ValueError):
        bench.select_candidate(candidates, model="missing", protocol="generic")
    picked = bench.select_candidate(candidates, model="openai/gpt-4o", protocol="openai")
    assert picked["model"] == "openai/gpt-4o"
    assert picked["protocol"] == "openai"


def test_benchmark_model_loop_uses_tool_pipeline_server_oracle_and_redacts_secrets(tmp_path):
    import scripts.benchmark_computer_model_loop as bench

    png_path = tmp_path / "shot.png"
    png_path.write_bytes(fake_png_bytes(1))
    backend = FakeComputerBackend(
        target="browser",
        png_path=str(png_path),
    )
    oracle = bench.BenchmarkOracle()
    server, base = bench.start_benchmark_server(oracle)
    try:
        # 模拟模型通过 ToolPipeline 连续点击 + 输入并提交，最后自报假完成文本（不应采信自报，只看服务端 oracle）
        responses = iter([
            SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content="",
                tool_calls=[SimpleNamespace(
                    id="call-1",
                    function=SimpleNamespace(
                        name="computer_action",
                        arguments=json.dumps({"kind": "observe", "target": "browser"}),
                    ),
                )],
            ))]),
            SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content="任务完成（假装完成，实际服务端未计数）",
                tool_calls=[],
            ))]),
        ])

        def fake_completion(**_kwargs):
            return next(responses)

        report_path = tmp_path / "bench-report.json"
        summary = bench.run_single_trial_with_service(
            trial_index=1,
            completion_fn=fake_completion,
            candidate={
                "model": "generic/test-model",
                "protocol": "generic",
                "api_base": base,
                "api_key": "sk-super-secret-value-do-not-leak",
            },
            backend=backend,
            oracle=oracle,
            base_url=base,
            max_steps=6,
            report_secrets=("sk-super-secret-value-do-not-leak", "SECRET_INPUT_VALUE"),
        )
        # 服务端计数器未达到目标（3 次点击 + 提交），即使模型输出“任务完成”仍判定未通过
        assert summary["oracle_passed"] is False
        assert summary["oracle"]["clicks"] == 0
        assert summary["run_id"].startswith("computer-run-")
        assert summary["model"] == "generic/test-model"
        assert summary["protocol"] == "generic"
        assert summary["used_tool_pipeline"] is True
        assert summary["stop_response_ms"] >= 0
        assert summary["lease_release_ms"] >= 0

        report = bench.summarize_trials([summary])
        report_path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
        raw = report_path.read_text(encoding="utf-8")
        assert "sk-super-secret" not in raw
        assert "PAGE_BODY_SENTINEL" not in raw

        # 更新服务端真实计数与结果状态后再验证 oracle 判定
        oracle.reset()
        oracle.record_click()
        oracle.record_click()
        oracle.record_click()
        oracle.record_submit(matched=True, input_len=4)
        oracle.record_result_page()
        snap = oracle.snapshot()
        assert bench.evaluate_oracle_success(snap, min_clicks=3) is True
    finally:
        server.shutdown()
        server.server_close()


def test_benchmark_local_stats_and_stop_lease_measurement():
    import scripts.benchmark_computer_local as local_bench

    computed = local_bench.stats([10.0, 20.0, 30.0, 40.0, 50.0])
    assert computed["samples"] == 5
    assert computed["p50_ms"] == 30.0
    assert computed["p95_ms"] >= 40.0

    from core.computer.controller import ComputerControlService

    backend = FakeComputerBackend(target="browser")
    service = ComputerControlService(
        backend_factory=lambda _t, _i: backend,
        generation_provider=lambda: 1,
        abort_streams=lambda _ev: None,
    )
    cap = ComputerCapability(
        enabled=True,
        protocol=ComputerProtocol.GENERIC.value,
        targets=("browser",),
        browser="edge",
        settings_generation=1,
    )
    owner = ComputerOwner(
        session_id="bench",
        turn_id="t1",
        session_label="基准",
        cancel_event=threading.Event(),
    )
    timing = local_bench.measure_stop_and_lease(service, owner, cap, target="browser")
    assert timing["stop_response_ms"] >= 0
    assert timing["lease_release_ms"] >= 0
    assert timing["lease_released"] is True
