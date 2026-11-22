from __future__ import annotations

import http.cookiejar
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from html import unescape
from typing import Callable, Iterable

# 伪装成浏览器：360 与必应都会对明显的脚本 UA 降级或直接拦截
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
_DEFAULT_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

_SO360_ENDPOINT = "https://www.so.com/s"
_BAIDU_ENDPOINT = "https://www.baidu.com/s"
_BING_RSS_ENDPOINT = "https://cn.bing.com/search"
_DEFAULT_TIMEOUT = 12.0
_MAX_PAGES = 3

# 反爬冷却：百度连续查询约 3 次后会转到「百度安全验证」页，360 超频后只回骨架页。
# 命中后把该通道冷却一段时间，期间的搜索直接走别的通道，避免每次先白等一轮失败。
_COOLDOWN_SECONDS = 180.0
_cooldown_until: dict[str, float] = {}


def _in_cooldown(name: str) -> bool:
    return time.time() < _cooldown_until.get(name, 0.0)


def _start_cooldown(name: str) -> None:
    _cooldown_until[name] = time.time() + _COOLDOWN_SECONDS


class SearchBackendError(RuntimeError):
    """所有免费后端都不可用（网络、反爬、页面改版等），调用方应自行兜底。"""


class SearchCancelled(RuntimeError):
    """用户取消了本次搜索：不参与后端兜底，直接向上抛。"""


_360_BLOCK_RE = re.compile(r'(?is)<li\s+class="res-list[^"]*"[^>]*>(.*?)</li>')
_H3_RE = re.compile(r"(?is)<h3[^>]*>(.*?)</h3>")
_ANCHOR_RE = re.compile(r"(?is)<a\s([^>]*)>(.*?)</a>")
_MDURL_RE = re.compile(r'data-mdurl="([^"]+)"')
_HREF_RE = re.compile(r'href="([^"]+)"')
_SUMMARY_RE = re.compile(
    r'(?is)<span[^>]*class="[^"]*res-list-summary[^"]*"[^>]*>(.*?)</span>'
)
_ALT_SUMMARY_RE = re.compile(r'(?is)<p[^>]*class="[^"]*res-desc[^"]*"[^>]*>(.*?)</p>')
_CITE_RE = re.compile(r"(?is)<cite[^>]*>(.*?)</cite>")
_TAG_RE = re.compile(r"(?is)<[^>]+>")
_WS_RE = re.compile(r"\s+")

# 360 必须带 cookie 访问：无 cookie 的首次请求只返回 ~3KB 的页面骨架，
# 搜索结果全靠前端脚本异步填充（解析必然是 0 条）。带上 cookie 容器后
# 服务端才会把结果直接渲染进 HTML（同一次请求即可，无需二次取）。
_COOKIE_JAR = http.cookiejar.CookieJar()
_OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_COOKIE_JAR))

_BLOCKED_MARKERS = ("安全验证", "请输入验证码", "访问过于频繁", "异常流量")


def _plain_text(fragment: str) -> str:
    return _WS_RE.sub(" ", unescape(_TAG_RE.sub("", fragment or ""))).strip()


def _is_http_url(url: str) -> bool:
    return url.startswith("http://") or url.startswith("https://")


def parse_360_html(html: str, num_results: int = 10) -> list[dict]:
    results: list[dict] = []
    seen: set[str] = set()
    for block in _360_BLOCK_RE.findall(html or ""):
        heading = _H3_RE.search(block)
        if not heading:
            continue
        anchor = _ANCHOR_RE.search(heading.group(1))
        if not anchor:
            continue
        attrs, inner = anchor.group(1), anchor.group(2)
        # 只认 data-mdurl：360 的真实网页结果都带它；图片/文库等聚合卡片的 href
        # 指向站内频道页（image.so.com、wenku.so.com），同样以 http 开头，
        # 用 href 回退会把它们混进搜索结果里
        mdurl = _MDURL_RE.search(attrs)
        if not mdurl:
            continue
        url = mdurl.group(1).strip()
        if not _is_http_url(url) or url in seen:
            continue
        title = _plain_text(inner)
        if not title:
            continue
        snippet = ""
        summary = _SUMMARY_RE.search(block) or _ALT_SUMMARY_RE.search(block)
        if summary:
            snippet = _plain_text(summary.group(1))
        if not snippet:
            cite = _CITE_RE.search(block)
            if cite:
                snippet = _plain_text(cite.group(1))
        seen.add(url)
        results.append({"title": title, "url": url, "snippet": snippet})
        if len(results) >= max(1, num_results):
            break
    return results


def _fetch(
    url: str,
    timeout: float,
    cancel_check: Callable[[], bool] | None = None,
    opener: urllib.request.OpenerDirector | None = None,
    extra_headers: dict | None = None,
) -> str:
    if cancel_check is not None and cancel_check():
        raise SearchCancelled("操作已被用户取消")
    headers = dict(_DEFAULT_HEADERS)
    if extra_headers:
        headers.update(extra_headers)
    request = urllib.request.Request(url, headers=headers)
    client = opener if opener is not None else _OPENER
    with client.open(request, timeout=timeout) as response:
        raw = response.read()
        charset = response.headers.get_content_charset() or "utf-8"
    return raw.decode(charset, "ignore")


def _looks_blocked(html: str) -> bool:
    return any(marker in (html or "") for marker in _BLOCKED_MARKERS)


def search_360(
    query: str,
    num_results: int = 10,
    timeout: float = _DEFAULT_TIMEOUT,
    cancel_check: Callable[[], bool] | None = None,
) -> list[dict]:
    """走 360 网页搜索，按需翻页凑够 ``num_results`` 条。"""
    collected: list[dict] = []
    seen: set[str] = set()
    for page in range(1, _MAX_PAGES + 1):
        if len(collected) >= num_results:
            break
        params = {"q": query, "pn": page}
        html = _fetch(
            _SO360_ENDPOINT + "?" + urllib.parse.urlencode(params), timeout, cancel_check
        )
        if not html.strip() or _looks_blocked(html) or len(html) < 10000:
            _start_cooldown("360")
            raise SearchBackendError("360 返回骨架页或反爬拦截页")
        page_results = parse_360_html(html, num_results)
        if not page_results:
            break
        fresh = [item for item in page_results if item["url"] not in seen]
        for item in fresh:
            seen.add(item["url"])
            collected.append(item)
            if len(collected) >= num_results:
                break
        if not fresh:
            break
    return collected


_BAIDU_BLOCK_RE = re.compile(r'(?is)<div[^>]*class="result c-container[^"]*"[^>]*>')
_BAIDU_MU_RE = re.compile(r'\smu="([^"]*)"')
_BAIDU_DESC_RE = re.compile(
    r'(?is)<(?:div|span|p)[^>]*class="[^"]*(?:c-abstract|content-right|c-span-last)[^"]*"[^>]*>'
)
_BAIDU_CLOSE_RE = re.compile(r"(?is)</(?:div|span|p)>")
# 百度结果块正文常常没有稳定的摘要类名，回退取正文时先剔掉这些界面噪声
_SNIPPET_NOISE_RE = re.compile(
    r"(百度快照|查看全文|查看更多|展开全部|收起|反馈|举报|收藏|分享|\s{2,})"
)

_BAIDU_OPENER = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
)
_baidu_ready = {"warmed": False}


def _baidu_warm(timeout: float, cancel_check: Callable[[], bool] | None = None) -> None:
    if _baidu_ready["warmed"]:
        return
    if cancel_check is not None and cancel_check():
        raise SearchCancelled("操作已被用户取消")
    request = urllib.request.Request("https://www.baidu.com/", headers=_DEFAULT_HEADERS)
    with _BAIDU_OPENER.open(request, timeout=timeout) as response:
        response.read(2048)
    _baidu_ready["warmed"] = True


def _baidu_snippet(block: str) -> str:
    match = _BAIDU_DESC_RE.search(block)
    if match:
        tail = block[match.end():]
        close = _BAIDU_CLOSE_RE.search(tail)
        segment = tail[: close.start()] if close else tail[:400]
        text = _plain_text(segment)
        # 容器里常常是空的或被广告位占用，过短就继续走回退
        if len(text) >= 12:
            return _SNIPPET_NOISE_RE.sub(" ", text).strip()

    heading = _H3_RE.search(block)
    body = block[heading.end():] if heading else block
    text = _SNIPPET_NOISE_RE.sub(" ", _plain_text(body)).strip()
    return text[:180]


def parse_baidu_html(html: str, num_results: int = 10) -> list[dict]:
    starts = list(_BAIDU_BLOCK_RE.finditer(html or ""))
    results: list[dict] = []
    seen: set[str] = set()
    for index, match in enumerate(starts):
        block = html[match.start() : starts[index + 1].start() if index + 1 < len(starts) else len(html)]
        heading = _H3_RE.search(block)
        if not heading:
            continue
        anchor = _ANCHOR_RE.search(heading.group(1))
        if not anchor:
            continue
        href = _HREF_RE.search(anchor.group(1))
        mu = _BAIDU_MU_RE.search(match.group(0))
        url = ((mu.group(1) if mu else "") or (href.group(1) if href else "")).strip()
        if not _is_http_url(url) or url in seen:
            continue
        title = _plain_text(anchor.group(2))
        if not title:
            continue
        seen.add(url)
        results.append({"title": title, "url": url, "snippet": _baidu_snippet(block)})
        if len(results) >= max(1, num_results):
            break
    return results


def search_baidu(
    query: str,
    num_results: int = 10,
    timeout: float = _DEFAULT_TIMEOUT,
    cancel_check: Callable[[], bool] | None = None,
) -> list[dict]:
    """走百度网页搜索。相关性最好，但连续查询会转「安全验证」页，需预热 + 冷却退避。"""
    params = {"wd": query, "ie": "utf-8"}
    try:
        _baidu_warm(timeout, cancel_check)
        html = _fetch(
            _BAIDU_ENDPOINT + "?" + urllib.parse.urlencode(params),
            timeout,
            cancel_check,
            opener=_BAIDU_OPENER,
            extra_headers={"Referer": "https://www.baidu.com/"},
        )
    except SearchCancelled:
        raise
    except Exception:
        _baidu_ready["warmed"] = False
        raise
    if "百度安全验证" in html or len(html) < 20000:
        _baidu_ready["warmed"] = False
        _start_cooldown("baidu")
        raise SearchBackendError("百度返回安全验证页")
    items = parse_baidu_html(html, num_results)
    if not items:
        _start_cooldown("baidu")
        raise SearchBackendError("百度结果页未解析到网页结果（页面结构可能已变）")
    return items


def parse_bing_rss(xml_text: str, num_results: int = 10) -> list[dict]:
    """解析必应 RSS 的 ``channel/item`` 列表。"""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    results: list[dict] = []
    for item in root.findall("./channel/item"):
        url = (item.findtext("link") or "").strip()
        title = (item.findtext("title") or "").strip()
        if not _is_http_url(url) or not title:
            continue
        results.append(
            {
                "title": title,
                "url": url,
                "snippet": (item.findtext("description") or "").strip(),
            }
        )
        if len(results) >= max(1, num_results):
            break
    return results


def search_bing_rss(
    query: str,
    num_results: int = 10,
    timeout: float = _DEFAULT_TIMEOUT,
    cancel_check: Callable[[], bool] | None = None,
) -> list[dict]:
    """走必应 RSS 搜索（兜底通道）。"""
    params = {"q": query, "format": "rss", "mkt": "zh-CN"}
    xml_text = _fetch(
        _BING_RSS_ENDPOINT + "?" + urllib.parse.urlencode(params), timeout, cancel_check
    )
    return parse_bing_rss(xml_text, num_results)


def search_web(
    query: str,
    num_results: int = 5,
    timeout: float = _DEFAULT_TIMEOUT,
    cancel_check: Callable[[], bool] | None = None,
) -> list[dict]:
    query = str(query or "").strip()
    if not query:
        raise SearchBackendError("查询词为空")
    want = max(1, num_results)
    backends: Iterable[tuple[str, Callable[..., list[dict]]]] = (
        ("baidu", search_baidu),
        ("360", search_360),
        ("bing_rss", search_bing_rss),
    )
    failures: list[str] = []
    merged: list[dict] = []
    seen: set[str] = set()
    for name, backend in backends:
        if len(merged) >= want:
            break
        if name != "bing_rss" and _in_cooldown(name):
            failures.append(f"{name} 反爬冷却中")
            continue
        try:
            results = backend(query, want, timeout, cancel_check)
        except SearchCancelled:
            # 取消是用户意图，不能当故障吞掉去跑下一个后端
            raise
        except Exception as error:
            failures.append(f"{name} {type(error).__name__}: {error}")
            continue
        if not results:
            failures.append(f"{name} 未解析到结果")
            continue
        for item in results:
            url = (item.get("url") or "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            merged.append(item)
            if len(merged) >= want:
                break
    if merged:
        return merged
    raise SearchBackendError("；".join(failures) or "所有免费搜索通道均不可用")
