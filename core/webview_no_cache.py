"""修复 pywebview 内置静态服务器的缓存控制头从未真正发出的问题。

背景现象（2026-09-19）：打包版启动后整窗白屏、无任何 JS 报错，CDP 抓包发现
入口资产 404。根因链：
  1. pywebview 6.2.1 的 webview/http.py 在 asset 路由里先
     ``bottle.response.set_header('Cache-Control', 'no-cache, ...')`` 再
     ``return bottle.static_file(...)``；
  2. bottle 的 static_file 返回一个全新的 HTTPResponse，路由返回 HTTPResponse
     时，此前设在 thread-local response 上的头被整体丢弃——no-cache 实际从未
     发出（实测响应只有 Last-Modified/Etag）；
  3. WebView2 按启发式缓存 index.html，而 pywebview 每次启动随机分配端口、
     网站数据目录固定（gui_main._webview_start_options 的 storage_path），
     同一端口被旧前端构建用过就会命中过期 index.html；
  4. 浏览器拿旧哈希资产名请求新 dist → 404 → 主 bundle 不加载 →
     #app 为空 → 白屏。dev 源码版与安装版共用同一数据目录时尤其容易互相投毒。

修复：在 pywebview 服务器开始服务前调用 apply()，用包装过的
bottle.static_file 把缓存控制头直接补到返回的 HTTPResponse 上——与
pywebview 想做而没做到的行为一致。GUI 进程内 bottle.static_file 的消费方只有
pywebview 的静态路由（lan_server 自建 wsgiref 响应，不走这里），影响面可控。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_CACHE_CONTROL = "no-cache, no-store, must-revalidate"

_applied = False


def apply() -> None:
    """包一层 bottle.static_file，确保静态响应带上 no-cache 头。

    幂等：重复调用只包一层。pywebview 依赖缺失或 import 失败时静默跳过，
    绝不影响 GUI 启动。
    """
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
