# -*- coding: utf-8 -*-
"""prompt_cache_key（Task 6）A/B 采样驱动。

设计 §Task6 说「先手工试、拿到数字再决定白名单放哪几个车道」，本脚本把这件事
变成一条命令，且**不碰用户的 config.json、不改产品代码**：

- A 相：独立沙箱 `YCODE_DATA_DIR`，config.json 里**不写** `cache_affinity_lanes`
  → 走「默认关闭」的真实代码路径，不发 `prompt_cache_key`；
- B 相：另一个沙箱，config.json 写 `cache_affinity_lanes=["<目标域名>"]`
  → 走 Task 6 的真实白名单路径（发不发、400 拉黑兜底都由产品代码自己决定）。

两相都调用仓库既有的 `cache_benchmark.py`（它经 `_provider_request_options`
出口构造请求、`cache_scope="benchmark:main"` 记账），同一固定脚本、同一车道、
同样轮次与间隔，唯一变量就是那个字段。

用法：
  # 1) 先看两相到底会发什么（不联网、不花钱，随时可跑）
  python bench/ab_affinity_key.py --dry-run

  # 2) 真实采样（会调用付费渠道，需要你先导出该渠道的 key）
  $env:YCODE_BENCH_API_KEY="..."      # 只从环境变量读，脚本不打印它
  python bench/ab_affinity_key.py --rounds 30 --gap-seconds 20

数字口径说明：结果用 `MemoryManager.query_lane_cache_health` 读（prev 由 SQL
窗口函数按会话分链，已与对标报告 §二 基线同口径），因此可直接与报告数字比较。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

DEFAULT_MODEL = "openai/deepseek-v4.1-flash"
DEFAULT_API_BASE = "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"

# 沙箱里只放缓存相关键；其余配置一律走产品默认值，保证两相只差白名单。
PHASES = (
    ("A-off", {}),
    ("B-on", {"cache_affinity_lanes": ["__HOST__"]}),
)
PHASES_DICT = dict(PHASES)


def _host_of(api_base: str) -> str:
    from urllib.parse import urlsplit

    return (urlsplit(str(api_base or "")).hostname or "").lower()


def _substitute_host(value, host: str):
    """递归替换配置里的 __HOST__ 占位（白名单是列表，不能直接对列表 replace）。"""
    if isinstance(value, str):
        return value.replace("__HOST__", host)
    if isinstance(value, list):
        return [_substitute_host(item, host) for item in value]
    if isinstance(value, dict):
        return {key: _substitute_host(item, host) for key, item in value.items()}
    return value


def _make_sandbox(label: str, api_base: str) -> str:
    """给某一相建独立数据目录 + 最小 config.json（与真实 ~/.ycode 完全隔离）。"""
    path = tempfile.mkdtemp(prefix=f"ycode-ab-{label}-")
    config = _substitute_host(PHASES_DICT[label], _host_of(api_base))
    with open(os.path.join(path, "config.json"), "w", encoding="utf-8") as fh:
        json.dump(config, fh, ensure_ascii=False, indent=2)
    return path


PHASES_DICT = dict(PHASES)

# 只探测「会发哪些缓存字段」，不建连接、不发请求
PROBE_SNIPPET = """
import json, sys
sys.path.insert(0, r"{root}")
from core.llm_dialects import _provider_request_options
opts = _provider_request_options(
    {model!r}, {api_base!r},
    cache_scope="benchmark:main", cache_session_id="ab-probe", tool_contract="",
)
body = {{
    "prompt_cache_key": opts.get("prompt_cache_key") or (opts.get("extra_body") or {{}}).get("prompt_cache_key"),
    "extra_headers": opts.get("extra_headers"),
}}
print(json.dumps(body, ensure_ascii=False, sort_keys=True))
"""


def probe(model: str, api_base: str, sandbox: str) -> dict:
    """在该相的沙箱里跑一次纯本地探测，返回实际会发出的缓存相关字段。"""
    env = dict(os.environ, YCODE_DATA_DIR=sandbox, PYTHONIOENCODING="utf-8")
    code = PROBE_SNIPPET.format(root=ROOT, model=model, api_base=api_base)
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True,
        env=env, cwd=ROOT, timeout=120,
    )
    if out.returncode != 0:
        raise RuntimeError(f"探测失败：{out.stderr.strip()[:400]}")
    return json.loads(out.stdout.strip().splitlines()[-1])


def run_phase(model: str, api_base: str, sandbox: str, rounds: int) -> int:
    """真实采样：调用仓库既有的 cache_benchmark（会花钱，需显式运行本函数）。"""
    key_env = os.environ.get("YCODE_BENCH_API_KEY", "")
    if not key_env:
        raise SystemExit("缺少 YCODE_BENCH_API_KEY（真实采样要调用付费渠道；脚本不读取也不打印密钥本身）")
    cmd = [
        sys.executable, os.path.join(ROOT, "cache_benchmark.py"),
        "--model", model, "--api-base", api_base,
        "--api-key-env", "YCODE_BENCH_API_KEY",
        "--rounds", str(rounds), "--minimum-hot-rate", "0",
    ]
    env = dict(os.environ, YCODE_DATA_DIR=sandbox, PYTHONIOENCODING="utf-8")
    return subprocess.run(cmd, env=env, cwd=ROOT).returncode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--api-base", default=DEFAULT_API_BASE)
    parser.add_argument("--rounds", type=int, default=30, help="每相轮数（§5.3 要求 ≥30）")
    parser.add_argument("--gap-seconds", type=float, default=20.0, help="轮间隔，模拟真实对话节奏")
    parser.add_argument("--dry-run", action="store_true", help="只看两相会发什么，不联网")
    parser.add_argument("--keep-sandbox", action="store_true")
    args = parser.parse_args()

    print(f"车道：{args.model} @ {args.api_base}")
    print(f"白名单匹配域名：{_host_of(args.api_base)}\n")

    sandboxes = {}
    try:
        for label, _cfg in PHASES:
            sandboxes[label] = _make_sandbox(label, args.api_base)
            if args.dry_run:
                seen = probe(args.model, args.api_base, sandboxes[label])
                key = seen.get("prompt_cache_key")
                print(f"[{label}] prompt_cache_key = {key if key else '（不发）'}")
                print(f"[{label}] extra_headers    = {seen.get('extra_headers')}")
                if label == "B-on" and not key:
                    print("        ⚠️ 白名单已配但没发键 → 先查 _affinity_lane_enabled 的域名匹配")
            else:
                print(f"[{label}] 开始真实采样 {args.rounds} 轮 …")
                code = run_phase(args.model, args.api_base, sandboxes[label], args.rounds)
                print(f"[{label}] 退出码 {code}")
                time.sleep(args.gap_seconds)
        if args.dry_run:
            print("\ndry-run 完成：上面两行的差异就是 A/B 的唯一变量。"
                  "确认无误后再去掉 --dry-run 跑真实采样（会产生真实费用）。")
        else:
            print("\n两相采样结束。用同一口径读回数字：")
            print("  python -c \"from core.memory import MemoryManager; ...\"  # 见 --help 说明")
        return 0
    finally:
        if not args.keep_sandbox:
            for path in sandboxes.values():
                shutil.rmtree(path, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
