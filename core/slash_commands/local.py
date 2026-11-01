"""本地斜杠命令的实现：在进程内直接完成动作，不经过模型。

与 /fix /review 这类提示词命令的区别：提示词命令只是把参数展开成一段提示词再
提交给模型，每次都要跑一次模型回合，消耗 token；本地命令自己动手（写记忆、
压缩上下文），不发起任何模型调用，因此不消耗 token、也不依赖网络，可以离线执行。
这也是把它们单独放进 local 模块、与 prompts 模块分开的原因：kind 不是标签而
是执行路径的分水岭，物理分文件能防止日后误把模型调用塞进本地命令里。

本模块只做参数校验与结果包装，真正的副作用委托给 core.memory_kairos 与会话层，
这样命令层可以被单测完整覆盖（monkeypatch 掉外部函数即可），不必真的写文件。
"""

# 模块级导入而非在函数内 import：一是不必每次调用都查一次 sys.modules，
# 二是让测试能用 monkeypatch.setattr("core.slash_commands.local.append_to_daily_log")
# 精准替换本模块引用的这个对象。若在函数体内做 from ... import，替换点会漂移到
# core.memory_kairos 上，影响该模块的其他使用者，测试就不够隔离。
from core.memory_kairos import append_to_daily_log

__all__ = ["handle_remember", "handle_compact"]


def handle_remember(args: str, ctx=None) -> dict:
    """把一条规则或偏好追加到今日记忆日志。

    返回 dict 而非抛异常，是入口层的统一契约：GUI/CLI 只需读 ok 决定是否提示。
    空参数在这里就地拦住而不是交给下游，因为 append_to_daily_log 会把空白条目
    写成一行无意义的 "- [HH:MM] "，日志被污染后很难清理。
    """
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
    """从调用方注入的 ctx 里取出可调用的 compress。

    ctx 形状不固定：GUI 传对象（带方法），CLI 或测试传 dict，也可能没有会话
    （None）。因此三种形态都要兜住，取不到就返回 None 交给上层提示。
    """
    if ctx is None:
        return None
    if isinstance(ctx, dict):
        candidate = ctx.get("compress")
    else:
        candidate = getattr(ctx, "compress", None)
    # 只认可调用的：dict 里可能放了占位常量，直接调用会抛 TypeError，
    # 那时报错信息对用户毫无意义，不如按“能力缺失”处理。
    return candidate if callable(candidate) else None


def handle_compact(args: str, ctx=None) -> dict:
    """手动触发当前会话上下文压缩。

    args 是给压缩器的保留指令（例如“保留最近 10 条”），可为空；空串表示按
    压缩器默认策略执行，不做额外偏好。压缩能力挂在会话对象上而不是本模块里，
    因为上下文状态归会话层所有，本地命令不应持有会话引用。
    """
    compress = _resolve_compress(ctx)
    if compress is None:
        return {"ok": False, "msg": "当前通道不支持手动压缩"}

    try:
        result = compress(args)
        return {"ok": True, "notice": f"压缩已触发：{result}"}
    except Exception as exc:  # 压缩是重活，模型/外部服务出错要变成可展示的提示
        return {"ok": False, "msg": str(exc)}
