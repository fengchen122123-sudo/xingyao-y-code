"""Interactive user question adapter backed by the existing TUI prompt."""

from __future__ import annotations

import asyncio
import json
from concurrent.futures import Future


def ask_user(question: str, options: list[str] | None = None) -> str:
    question = str(question or "").strip()
    if not question:
        return "无法提问：问题内容为空。"

    choices = [str(option).strip() for option in (options or ["继续", "停止"]) if str(option).strip()]
    if not choices or len(choices) > 3:
        return "无法提问：选项数量必须在 1 到 3 个之间。"

    import main

    app = main.app_instance
    if app is None or app.loop is None:
        return "无法提问：当前终端没有活动的交互界面。"

    future: Future = Future()

    async def setup_prompt():
        main.confirm_prompt = {
            "function_name": "ask_user",
            "arguments": {"question": question},
            "options": choices,
            "current_idx": 0,
            "future": future,
        }
        main.app_instance.invalidate()

    asyncio.run_coroutine_threadsafe(setup_prompt(), app.loop).result()
    selected_index = int(future.result())
    return json.dumps({
        "question": question,
        "selected_index": selected_index,
        "selected": choices[selected_index] if 0 <= selected_index < len(choices) else None,
    }, ensure_ascii=False)
