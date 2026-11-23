from __future__ import annotations

import logging
import mimetypes

logger = logging.getLogger(__name__)

_CACHE_CONTROL = "no-cache, no-store, must-revalidate"

_applied = False


def apply() -> None:
    """修正 JS MIME，并包一层 bottle.static_file 补 no-cache 头。

    Windows 注册表可能把 .js 映射成 text/plain，导致浏览器拒绝执行 ES Module。
    幂等：重复调用只包一层。pywebview 依赖缺失或 import 失败时静默跳过，
    绝不影响 GUI 启动。
    """
    mimetypes.add_type("text/javascript", ".js")

    global _applied
    if _applied:
        return
    try:
        import bottle
    except Exception:
        logger.debug("bottle 不可用，跳过静态服务器缓存头修补", exc_info=True)
        return

    original = bottle.static_file

    def _static_file_with_no_cache(*args, **kwargs):
        response = original(*args, **kwargs)
        try:
            if isinstance(response, bottle.HTTPResponse):
                existing = response.headers.get("Cache-Control")
                if existing:
                    response.headers["Cache-Control"] = (
                        existing if _CACHE_CONTROL in existing
                        else f"{existing}, {_CACHE_CONTROL}"
                    )
                else:
                    response.set_header("Cache-Control", _CACHE_CONTROL)
        except Exception:
            logger.debug("补写 Cache-Control 失败", exc_info=True)
        return response

    bottle.static_file = _static_file_with_no_cache
    _applied = True
    logger.info("pywebview 静态服务器 no-cache 修补已生效")
