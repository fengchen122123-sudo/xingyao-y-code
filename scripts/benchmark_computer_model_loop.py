# -*- coding: utf-8 -*-
"""Task 0 电脑操控模型端到端对照夹具：
- 严格唯一 model/protocol 匹配（不跨候选切换或沿用旧现场）
- 通过真实 ToolPipeline / ToolBatchExecutor / ComputerTurnLease 执行
- 以服务端计数器、延迟启用与结果页状态判成败（不读取模型自报正文）
- 输出脱敏：不记录 API key、页面正文、输入原文或完整异常堆栈
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import json
import statistics
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.computer.actions import COMPUTER_ACTION_TOOL_SCHEMA
from core.computer.models import (
    ComputerAction,
    ComputerCapability,
    ComputerOwner,
    ComputerProtocol,
)
from core.computer.tool_adapter import ComputerTurnLease
from core.harness.tool_pipeline import (
    ToolBatchExecutor,
    ToolExecutionContext,
    ToolPipeline,
)
from core.tool_registry import ToolRegistry

EXPECTED_SUBMIT_TOKEN = "done"

TASK_TEXT = (
    "测试页已在当前浏览器中打开。请用电脑操控依次完成：\n"
    "1) 点击计数器按钮 #btn 3 次；\n"
    "2) 点击 #enable-btn 触发输入框 #field 启用（存在短延迟，可配合 wait_for 等待可用）；\n"
    "3) 在已启用的 #field 中填入 done 并点击 #submit-btn 进入结果页 /result。\n"
    "完成后停止调用工具。"
)

PAGE_HTML = """<!doctype html><meta charset="utf-8"><title>YCODE BENCHMARK</title>
<style>body{margin:0;padding:24px;font:20px Arial}button,input{margin:8px;font-size:18px;padding:10px 18px}</style>
<button id="btn">Counter click</button>
<button id="enable-btn">Enable input (delayed)</button>
<input id="field" disabled placeholder="wait until enabled">
<button id="submit-btn" type="button">Submit</button>
<p id="status">clicks=0</p>
<script>
window.bench = { clicks: 0, delayedEnabled: false };
const btn = document.getElementById('btn');
const enableBtn = document.getElementById('enable-btn');
const field = document.getElementById('field');
const submitBtn = document.getElementById('submit-btn');
const statusEl = document.getElementById('status');

btn.addEventListener('click', () => {
  bench.clicks += 1;
  statusEl.textContent = 'clicks=' + bench.clicks;
  fetch('/api/click', { method: 'POST' }).catch(() => {});
});
enableBtn.addEventListener('click', () => {
  setTimeout(() => {
    field.disabled = false;
    bench.delayedEnabled = true;
    fetch('/api/delayed_enabled', { method: 'POST' }).catch(() => {});
  }, 250);
});
submitBtn.addEventListener('click', () => {
  fetch('/api/submit', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ value: field.value || '' })
  }).then(() => { location.href = '/result'; }).catch(() => {});
});
</script>"""

RESULT_HTML = """<!doctype html><meta charset="utf-8"><title>YCODE BENCH RESULT</title>
<p id="result-ok">RESULT_PAGE_REACHED</p>"""


class BenchmarkOracle:
    """线程安全服务端真值计数器：仅以服务器收到的状态判断任务成功与否。"""

    def __init__(self, expected_token: str = EXPECTED_SUBMIT_TOKEN) -> None:
        self._lock = threading.Lock()
        self._expected_token = expected_token
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self._clicks = 0
            self._delayed_enabled = False
            self._submitted_match = False
            self._submitted_len = 0
            self._result_reached = False

    def record_click(self) -> None:
        with self._lock:
            self._clicks += 1

    def record_delayed_enabled(self) -> None:
        with self._lock:
            self._delayed_enabled = True

    def record_submit(self, *, matched: bool, input_len: int) -> None:
        with self._lock:
            self._submitted_match = bool(matched)
            self._submitted_len = max(0, int(input_len))

    def record_result_page(self) -> None:
        with self._lock:
            self._result_reached = True

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "clicks": self._clicks,
                "delayed_enabled": self._delayed_enabled,
                "submitted_match": self._submitted_match,
                "submitted_len": self._submitted_len,
                "result_reached": self._result_reached,
            }


def evaluate_oracle_success(snap: dict[str, Any], *, min_clicks: int = 3) -> bool:
    return bool(
        int(snap.get("clicks", 0)) >= min_clicks
        and snap.get("submitted_match") is True
        and snap.get("result_reached") is True
    )


def _make_handler(oracle: BenchmarkOracle, expected_token: str):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path.startswith("/reset"):
                oracle.reset()
                body = b"reset-ok"
            elif self.path.startswith("/result"):
                oracle.record_result_page()
                body = RESULT_HTML.encode("utf-8")
            elif self.path.startswith("/api/state"):
                body = json.dumps(oracle.snapshot()).encode("utf-8")
            else:
                body = PAGE_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or "0")
            raw = self.rfile.read(min(length, 4096)) if length > 0 else b""
            if self.path == "/api/click":
                oracle.record_click()
            elif self.path == "/api/delayed_enabled":
                oracle.record_delayed_enabled()
            elif self.path == "/api/submit":
                try:
                    data = json.loads(raw.decode("utf-8") or "{}")
                except Exception:
                    data = {}
                val = str(data.get("value") or "")
                oracle.record_submit(matched=(val == expected_token), input_len=len(val))
            body = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


def start_benchmark_server(
    oracle: BenchmarkOracle | None = None,
    expected_token: str = EXPECTED_SUBMIT_TOKEN,
):
    active_oracle = oracle or BenchmarkOracle(expected_token=expected_token)
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), _make_handler(active_oracle, expected_token)
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}"


def candidate_providers() -> list[dict[str, str]]:
    """从用户 config 构建候选（协议/model/base/key），仅内存使用，不输出凭据。"""
    import config

    data = config._load()
    out: list[dict[str, str]] = []
    for provider in data.get("providers", []):
        base = str(provider.get("base_url") or "")
        key = str(provider.get("api_key") or "")
        protocol = str(provider.get("protocol") or "generic")
        if not key:
            continue
        for model in provider.get("models", []):
            name = str(model.get("litellm_model") or model.get("id") or "")
            if not name:
                continue
            resolved_base = base
            if name.startswith("anthropic/") and resolved_base.rstrip("/").endswith("/v1"):
                resolved_base = resolved_base.rstrip("/").removesuffix("/v1")
            out.append(
                {
                    "model": name,
                    "protocol": protocol,
                    "api_base": resolved_base,
                    "api_key": key,
                }
            )
    return out


def select_candidate(
    candidates: Iterable[dict[str, str]],
    *,
    model: str,
    protocol: str,
) -> dict[str, str]:
    """要求严格唯一匹配精确 model 与 protocol，拒绝模糊过滤与跨候选回退。"""
    target_model = str(model or "").strip()
    target_protocol = str(protocol or "").strip()
    if not target_model or not target_protocol:
        raise ValueError("正式对照必须同时指定精确的 model 与 protocol")
    matches = [
        c
        for c in candidates
        if c.get("model") == target_model and c.get("protocol") == target_protocol
    ]
    if len(matches) != 1:
        raise ValueError(
            f"模型夹具必须唯一精确匹配 model={target_model!r}, protocol={target_protocol!r} "
            f"（命中 {len(matches)} 个）"
        )
    return dict(matches[0])


def _service_with(backend):
    from core.computer.controller import ComputerControlService

    return ComputerControlService(
        backend_factory=lambda _target, _identity: backend,
        generation_provider=lambda: 1,
        abort_streams=lambda _event: None,
    )


def _hint_provider():
    from core.computer.platforms import get_platform_adapter

    provider = getattr(get_platform_adapter(), "browser_window_hints", None)
    return provider if callable(provider) else None


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, int(len(ordered) * pct) - 1))
    return round(ordered[idx], 3)


def _redact_code(value: object, secrets: tuple[str, ...] = ()) -> str:
    text = str(value or "")
    for secret in secrets:
        if secret and secret in text:
            text = text.replace(secret, "***")
    return text[:120]


def _normalize_completion_tool_calls(tool_calls) -> list[dict]:
    out = []
    for idx, call in enumerate(tool_calls or ()):
        fn = getattr(call, "function", None)
        out.append(
            {
                "id": str(getattr(call, "id", "") or f"bench-call-{idx}"),
                "type": "function",
                "function": {
                    "name": str(getattr(fn, "name", "") or ""),
                    "arguments": str(getattr(fn, "arguments", "{}") or "{}"),
                },
            }
        )
    return out


def run_single_trial_with_service(
    *,
    trial_index: int,
    completion_fn,
    candidate: dict[str, str],
    backend,
    oracle: BenchmarkOracle,
    base_url: str,
    max_steps: int,
    report_secrets: tuple[str, ...] = (),
) -> dict[str, Any]:
    oracle.reset()
    service = _service_with(backend)
    segments: list[dict[str, Any]] = []
    service.add_listener(
        lambda ev: segments.append(dict(ev))
        if ev.get("type") == "computer_action_finished"
        else None
    )
    cancel_event = threading.Event()
    owner = ComputerOwner(
        session_id=f"model-bench-{trial_index}",
        turn_id=f"turn-{trial_index}",
        session_label="基准",
        cancel_event=cancel_event,
    )
    cap = ComputerCapability(
        enabled=True,
        protocol=ComputerProtocol.GENERIC.value,
        targets=("browser",),
        browser="edge",
        settings_generation=1,
    )
    run = service.start_run(owner, cap, target="browser")
    pipeline = ToolPipeline()
    executor = ToolBatchExecutor(pipeline)
    ctx = ToolExecutionContext(
        event_callback=None,
        parent_session=owner.session_id,
        parent_turn_id=owner.turn_id,
        cancel_event=cancel_event,
        allow_subagents=False,
        permission_owner=owner.session_id,
        permission_source="benchmark",
        permission_checker=lambda *_a, **_kw: True,
        registry=ToolRegistry(),
        mcp_manager=None,
        computer_lease=ComputerTurnLease(
            capability=cap,
            owner=owner,
            controller=service,
            run_id=run.run_id,
        ),
    )

    backend.execute(
        run.run_id, ComputerAction(kind="navigate", target="browser", url=base_url)
    )
    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": (
                "你是电脑操控代理。通过 computer_action 工具完成浏览器页面任务；"
                "每次调用后依据最新观察结果继续，全部完成后停止调用工具。"
            ),
        },
        {"role": "user", "content": TASK_TEXT},
    ]

    request_count = 0
    action_count = 0
    provider_total_ms = 0.0
    rejected_or_timeout = 0
    error_codes: list[str] = []
    phase_sums: dict[str, float] = defaultdict(float)
    seg_cursor = 0
    trial_started = time.perf_counter()

    try:
        for _ in range(max_steps):
            req_start = time.perf_counter()
            response = completion_fn(
                model=candidate["model"],
                messages=messages,
                tools=[COMPUTER_ACTION_TOOL_SCHEMA],
                api_base=candidate["api_base"],
                api_key=candidate["api_key"],
                temperature=0,
                timeout=120,
            )
            request_count += 1
            provider_total_ms += (time.perf_counter() - req_start) * 1000.0

            choice = response.choices[0]
            msg = choice.message
            raw_calls = list(getattr(msg, "tool_calls", None) or [])
            if not raw_calls:
                break
            norm_calls = _normalize_completion_tool_calls(raw_calls)
            messages.append(
                {
                    "role": "assistant",
                    "content": msg.content or "",
                    "tool_calls": norm_calls,
                }
            )
            records = executor.execute(norm_calls, ctx, on_event=None)
            for rec in records:
                action_count += 1
                messages.append(rec.message)
                for followup in rec.followup_messages:
                    messages.append(followup)
                try:
                    parsed = json.loads(str(rec.message.get("content") or "{}"))
                except Exception:
                    parsed = {}
                err = _redact_code(
                    parsed.get("error_code") or "",
                    secrets=report_secrets,
                )
                if err:
                    rejected_or_timeout += 1
                    error_codes.append(err)
            while len(segments) > seg_cursor:
                ev = segments[seg_cursor]
                seg_cursor += 1
                for key in ("queue_ms", "input_ms", "wait_ms", "observe_ms", "preview_ms"):
                    val = ev.get(key)
                    if isinstance(val, (int, float)) and val >= 0:
                        phase_sums[key] += float(val)
    finally:
        stop_started = time.perf_counter()
        service.stop(run.run_id, "benchmark_done")
        stop_response_ms = round((time.perf_counter() - stop_started) * 1000.0, 3)
        lease_released = service._run is None
        lease_release_ms = round((time.perf_counter() - stop_started) * 1000.0, 3)

    total_ms = round((time.perf_counter() - trial_started) * 1000.0, 3)
    oracle_snap = oracle.snapshot()
    passed = evaluate_oracle_success(oracle_snap)
    return {
        "trial_index": trial_index,
        "run_id": run.run_id,
        "model": candidate["model"],
        "protocol": candidate["protocol"],
        "used_tool_pipeline": True,
        "oracle": oracle_snap,
        "oracle_passed": passed,
        "request_count": request_count,
        "action_count": action_count,
        "rejected_or_timeout_count": rejected_or_timeout,
        "error_codes": error_codes,
        "provider_ms": round(provider_total_ms, 3),
        "total_ms": total_ms,
        "stop_response_ms": stop_response_ms,
        "lease_release_ms": lease_release_ms,
        "lease_released": lease_released,
        "phases_ms": {k: round(v, 3) for k, v in phase_sums.items()},
    }


def summarize_trials(trials: list[dict[str, Any]]) -> dict[str, Any]:
    totals = [float(t["total_ms"]) for t in trials]
    stops = [float(t["stop_response_ms"]) for t in trials]
    leases = [float(t["lease_release_ms"]) for t in trials]
    passed = sum(1 for t in trials if t.get("oracle_passed"))
    return {
        "trial_count": len(trials),
        "oracle_pass_count": passed,
        "oracle_pass_rate": round(passed / len(trials), 4) if trials else 0.0,
        "rejected_or_timeout_total": sum(
            int(t.get("rejected_or_timeout_count", 0)) for t in trials
        ),
        "total_ms_p50": _percentile(totals, 0.5),
        "total_ms_p95": _percentile(totals, 0.95),
        "stop_response_ms_p95": _percentile(stops, 0.95),
        "lease_release_ms_p95": _percentile(leases, 0.95),
        "trials": trials,
    }


def run_benchmark(
    model: str,
    protocol: str,
    max_steps: int,
    trials: int,
    output: Path,
):
    from core.computer.browser_backend import BrowserComputerBackend
    from core.llm_native_transport import native_completion

    candidate = select_candidate(
        candidate_providers(), model=model, protocol=protocol
    )
    oracle = BenchmarkOracle()
    server, base = start_benchmark_server(oracle)
    trial_results: list[dict[str, Any]] = []
    try:
        for idx in range(1, trials + 1):
            with tempfile.TemporaryDirectory(prefix="ycode-model-bench-") as directory:
                backend = BrowserComputerBackend(
                    "edge",
                    Path(directory) / "profile",
                    window_hint_provider=_hint_provider(),
                    use_process_worker=True,
                )
                trial_results.append(
                    run_single_trial_with_service(
                        trial_index=idx,
                        completion_fn=native_completion,
                        candidate=candidate,
                        backend=backend,
                        oracle=oracle,
                        base_url=base,
                        max_steps=max_steps,
                        report_secrets=(candidate["api_key"],),
                    )
                )
    finally:
        server.shutdown()
        server.server_close()

    report = summarize_trials(trial_results)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({k: v for k, v in report.items() if k != "trials"}, ensure_ascii=False))
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True, help="唯一精确匹配的 model 标识")
    parser.add_argument("--protocol", required=True, help="唯一精确匹配的协议标识（如 generic）")
    parser.add_argument("--steps", type=int, default=12, help="单次 trial 动作/请求上限")
    parser.add_argument("--trials", type=int, default=20, help="重复 trial 样本数")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    run_benchmark(args.model, args.protocol, args.steps, args.trials, args.output)
