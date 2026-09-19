# -*- coding: utf-8 -*-
"""真实模型端到端耗时测量（R9 验收补充）：Provider 决策 vs 本地动作分段。

显式人工探针，不进入 pytest。真实 Provider 调用 + 真实浏览器（受管子进程、
独立临时 profile、仅访问本机 loopback 测试页）；动作全部经
ComputerControlService 固定链路执行（含取消/暂停/surface/无进展门禁），
不绕过 Controller。截图只含本地测试页。

输出 JSON：逐步 Provider 耗时 / 输入耗时 / 观察耗时 / 截图字节数 / 累计，
以及汇总（Provider 决策占比、任务步数）。凭据来自用户 config，不在输出中
记录 API Key；输入正文只计长度不落盘。
"""

import argparse
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.computer.actions import (
    COMPUTER_ACTION_TOOL_SCHEMA,
    observation_provider_projection,
    parse_computer_action,
)

TASK_TEXT = (
    "测试页已在当前浏览器中打开。请用电脑操控完成：连续点击页面上的按钮 3 次"
    "（每次点击后先观察再继续），然后在输入框中输入 done 并观察确认。"
    "全部完成后输出“任务完成”并停止调用工具。"
)

PAGE_HTML = """<!doctype html><meta charset="utf-8"><title>YCODE MODEL BENCH</title>
<style>body{margin:0;font:24px Arial}button{margin:40px;font-size:24px;padding:12px 32px}
input{margin:40px;font-size:24px;padding:8px}</style>
<button id="btn">Bench button</button><input id="field"><p id="log"></p>
<script>window.bench={clicks:0,typed:""};
btn.addEventListener('click',()=>{bench.clicks++;log.textContent='clicks='+bench.clicks;});
field.addEventListener('input',()=>{bench.typed=field.value;});</script>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        body = PAGE_HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def candidate_providers():
    """从用户 config 构建候选（协议/model/base/key），不打印凭据。"""
    import config

    data = config._load()
    out = []
    for provider in data.get("providers", []):
        base = provider.get("base_url") or ""
        key = provider.get("api_key") or ""
        protocol = provider.get("protocol") or ""
        if not key:
            continue
        for model in provider.get("models", []):
            name = model.get("litellm_model") or model.get("id") or ""
            if not name:
                continue
            # Anthropic 协议路由的 base 以 /v1 结尾时 native 会自行拼接 /v1/messages：去掉末尾 /v1。
            if name.startswith("anthropic/") and base.rstrip("/").endswith("/v1"):
                base = base.rstrip("/").removesuffix("/v1")
            out.append({"model": name, "api_base": base, "api_key": key})
    return out


def run_benchmark(model_filter: str, max_steps: int, output: Path):
    from core.llm_native_transport import native_completion
    from core.computer.models import ComputerAction, ComputerOwner
    from core.computer.browser_backend import BrowserComputerBackend
    import tempfile

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"

    with tempfile.TemporaryDirectory(prefix="ycode-model-bench-") as directory:
        backend = BrowserComputerBackend(
            "edge",
            Path(directory) / "profile",
            window_hint_provider=_hint_provider(),
            use_process_worker=True,
        )
        service = _service_with(backend)
        owner = ComputerOwner(
            session_id="model-bench", turn_id="model-bench",
            session_label="基准", cancel_event=threading.Event(),
        )
        segments = []
        service.add_listener(lambda event: _capture(event, segments))
        run = service.start_run(owner, _capability(), target="browser")
        # 任务页由探针自身打开（不经模型决策），模型从该页开始操控。
        backend.execute(
            run.run_id, ComputerAction(kind="navigate", target="browser", url=base)
        )
        backend.observe(run.run_id)

        messages = [
            {"role": "system", "content": (
                "你是电脑操控代理。通过 computer_action 工具逐步完成任务："
                "每次只调用一个动作，动作后依据最新截图判断结果，不得猜测成功。"
                "坐标以最新截图左上角为 (0,0)。任务完成后输出最终结论，不再调用工具。"
            )},
            {"role": "user", "content": TASK_TEXT},
        ]

        candidates = candidate_providers()
        if model_filter:
            candidates = [c for c in candidates if model_filter.lower() in c["model"].lower()]
        steps = []
        provider_total_ms = 0.0
        local_total_ms = 0.0
        final_text = ""
        errors = []
        started = time.perf_counter()
        try:
            for candidate in candidates:
                try:
                    steps, provider_total_ms, local_total_ms, final_text = _loop(
                        native_completion, candidate, messages, COMPUTER_ACTION_TOOL_SCHEMA,
                        service, run, owner, segments, max_steps,
                    )
                    break
                except Exception as error:  # 换下一个候选（视觉/协议不支持等）
                    errors.append({"model": candidate["model"], "error": f"{type(error).__name__}: {error}"[:200]})
                    continue
        finally:
            total_ms = round((time.perf_counter() - started) * 1000, 1)
            service.stop()
            server.shutdown()
            server.server_close()

        local_total_ms = round(local_total_ms, 1)
        provider_total_ms = round(provider_total_ms, 1)
        result = {
            "steps": steps,
            "summary": {
                "step_count": len(steps),
                "total_ms": total_ms,
                "provider_ms": provider_total_ms,
                "local_action_ms": local_total_ms,
                "provider_share_percent": round(
                    100 * provider_total_ms / max(provider_total_ms + local_total_ms, 1), 1
                ),
                "final_text": final_text[:200],
                "task_done": "任务完成" in final_text,
            },
            "candidate_errors": errors,
        }
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(json.dumps(result["summary"], ensure_ascii=False), flush=True)
        for item in result["candidate_errors"]:
            print(json.dumps(item, ensure_ascii=False), flush=True)
        return result


def _hint_provider():
    from core.computer.platforms import get_platform_adapter

    provider = getattr(get_platform_adapter(), "browser_window_hints", None)
    return provider if callable(provider) else None


def _capability():
    from core.computer.models import ComputerCapability

    return ComputerCapability(
        enabled=True, protocol="generic", targets=("browser",),
        browser="edge", settings_generation=1,
    )


def _service_with(backend):
    from core.computer.controller import ComputerControlService

    return ComputerControlService(
        backend_factory=lambda target, identity: backend,
        generation_provider=lambda: 1,
        abort_streams=lambda event: None,
    )


def _capture(event, segments):
    if event.get("type") == "computer_action_finished":
        segments.append(event)


def _observation_image_message(png_path: str) -> dict:
    import base64

    png = Path(png_path).read_bytes()
    url = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": "【电脑观察】请只依据这张最新截图决定下一步。"},
            {"type": "image_url", "image_url": {"url": url}},
        ],
    }


def _loop(completion_fn, candidate, messages, tool_schema, service, run, owner, segments, max_steps):
    steps = []
    provider_total = 0.0
    local_total = 0.0
    final_text = ""
    seg_index = 0
    for _step in range(max_steps):
        seg_base = len(segments)
        request_started = time.perf_counter()
        response = completion_fn(
            model=candidate["model"],
            messages=messages,
            tools=[tool_schema],
            api_base=candidate["api_base"],
            api_key=candidate["api_key"],
            temperature=0,
            timeout=120,
        )
        provider_ms = round((time.perf_counter() - request_started) * 1000, 1)
        provider_total += provider_ms

        choice = response.choices[0]
        message = choice.message
        tool_calls = list(getattr(message, "tool_calls", None) or [])
        if not tool_calls:
            final_text = message.content or ""
            break
        # assistant 消息（带 tool_calls）必须回填进历史
        messages.append({
            "role": "assistant",
            "content": message.content or "",
            "tool_calls": [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.function.name,
                        "arguments": call.function.arguments,
                    },
                }
                for call in tool_calls
            ],
        })
        for call in tool_calls:
            observation = None
            png_path = ""
            if call.function.name != "computer_action":
                payload = {"ok": False, "error_code": "computer_invalid_action",
                           "error_message": "只允许 computer_action"}
            else:
                try:
                    arguments = json.loads(call.function.arguments)
                    action = parse_computer_action(arguments)
                except Exception as error:
                    payload = {"ok": False, "error_code": "computer_invalid_action",
                               "error_message": f"参数解析失败：{type(error).__name__}",
                               "attempted": str(call.function.arguments)[:160]}
                else:
                    action_started = time.perf_counter()
                    observation = service.execute(run.run_id, owner, action)
                    local_ms = round((time.perf_counter() - action_started) * 1000, 1)
                    local_total += local_ms
                    payload = observation_provider_projection(observation)
                    png_path = observation.png_path
                    local_ms = local_ms
            messages.append({
                "role": "tool",
                "tool_call_id": call.id,
                "name": call.function.name,
                "content": json.dumps(payload, ensure_ascii=False),
            })
            if observation is not None and observation.png_path:
                messages.append(_observation_image_message(observation.png_path))
            # 本步骤的分段耗时来自 controller 实测事件
            timing = {}
            while len(segments) > seg_index:
                event = segments[seg_index]
                seg_index += 1
                if event["type"] == "computer_action_finished":
                    timing = {
                        "duration_ms": event.get("duration_ms"),
                        "queue_ms": event.get("queue_ms"),
                        "input_ms": event.get("input_ms"),
                        "observe_ms": event.get("observe_ms"),
                    }
            steps.append({
                "model": candidate["model"].split("/", 1)[-1],
                "action": payload.get("action") or "",
                "error_code": payload.get("error_code") or "",
                "error_message": payload.get("error_message") or "",
                "attempted": payload.get("attempted") or "",
                "provider_ms": provider_ms,
                **timing,
                "screenshot_bytes": (
                    Path(png_path).stat().st_size
                    if observation is not None and observation.png_path
                    else 0
                ),
            })
    return steps, provider_total, local_total, final_text


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="", help="候选过滤子串；默认按 config 顺序尝试")
    parser.add_argument("--steps", type=int, default=12)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    run_benchmark(args.model, args.steps, args.output)
