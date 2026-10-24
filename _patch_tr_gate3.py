"""一次性补丁脚本 3：让测试用的 FakeClient 支持在队列里放异常（模拟网络层失败）。"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent
CORE_TESTS = ROOT / "tests" / "test_tokenrhythm_auth.py"

old = """            if not holder["responses"]:
                raise AssertionError(f"未预期的请求：{method} {url}")
            return holder["responses"].pop(0)
"""
new = """            if not holder["responses"]:
                raise AssertionError(f"未预期的请求：{method} {url}")
            queued = holder["responses"].pop(0)
            # 队列里放异常时按"请求在传输层就失败"抛出（模拟超时 / 连接不可用）
            if isinstance(queued, Exception):
                raise queued
            return queued
"""

text = CORE_TESTS.read_text(encoding="utf-8")
count = text.count(old)
if count != 1:
    raise SystemExit(f"[FAIL] 预期匹配 1 处，实际 {count} 处")
CORE_TESTS.write_text(text.replace(old, new, 1), encoding="utf-8")
print("[OK] FakeClient 支持抛队列里的异常")
