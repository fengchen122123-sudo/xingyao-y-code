from core.memory_kairos import append_to_daily_log

__all__ = ["handle_remember", "handle_compact"]


def handle_remember(args: str, ctx=None) -> dict:
    content = (args or "").strip()
    if not content:
        return {"ok": False, "msg": "用法: /remember <内容>"}

    try:
        # append_to_daily_log 内部做注入式指令过滤，返回 False 表示已被安全拦截。
        # 这里只翻译结果，不重复实现过滤逻辑，避免两套规则漂移。
        if append_to_daily_log(content):
            return {"ok": True, "notice": "已写入今日记忆日志"}
        return {"ok": False, "msg": "内容含可疑指令，已被安全过滤拦截"}
    except Exception as exc:  # 磁盘/权限等 IO 失败不应让整个回合崩掉
        return {"ok": False, "msg": str(exc)}


def _resolve_compress(ctx):
    if ctx is None:
        return None
    if isinstance(ctx, dict):
        candidate = ctx.get("compress")
    else:
        candidate = getattr(ctx, "compress", None)
    return candidate if callable(candidate) else None


def handle_compact(args: str, ctx=None) -> dict:
    compress = _resolve_compress(ctx)
    if compress is None:
        return {"ok": False, "msg": "当前通道不支持手动压缩"}

    try:
        result = compress(args)
        return {"ok": True, "notice": f"压缩已触发：{result}"}
    except Exception as exc:
        return {"ok": False, "msg": str(exc)}
