"""core.web_search 的解析与兜底测试（不联网：全部用内联样本）。"""

import pytest

from core import web_search


# --- 360 解析 ---------------------------------------------------------------
SO360_HTML = """
<ul>
<li class="res-list" data-result-type="richsum"><h3 class="res-title">
  <a href="https://www.so.com/link?m=abc" data-mdurl="https://www.cnblogs.com/a/1.html">
    <em>python</em>库<em>asyncio</em>的用法 - 博客园</a></h3>
  <div class='res-rich'><span class="res-list-summary" data-result-summary>
    asyncio 是 Python 的标准库之一，用于编写异步程序。</span>
    <p class="g-linkinfo"><cite><a href="https://www.so.com/link?m=def">www.cnblogs.com</a></cite></p>
  </div>
</li>
<li class="res-list"><h3 class="res-title">
  <a href="https://www.so.com/link?m=ghi" data-mdurl="https://python.org/doc">Python 文档</a></h3>
</li>
<li class="res-list"><h3 class="res-title">
  <a href="https://image.so.com/i?q=xxx">python 图片</a></h3>
</li>
<li class="res-list"><h3 class="res-title">
  <a href="https://www.so.com/link?m=dup" data-mdurl="https://www.cnblogs.com/a/1.html">重复条目</a></h3>
</li>
</ul>
"""


def test_parse_360_html_uses_real_url_and_summary():
    rows = web_search.parse_360_html(SO360_HTML, 10)
    assert [r["url"] for r in rows] == [
        "https://www.cnblogs.com/a/1.html",
        "https://python.org/doc",
    ]
    assert rows[0]["title"] == "python库asyncio的用法 - 博客园"
    assert "标准库" in rows[0]["snippet"]


def test_parse_360_html_drops_aggregate_cards_and_duplicates():
    rows = web_search.parse_360_html(SO360_HTML, 10)
    # 图片聚合卡片没有 data-mdurl，重复 URL 只保留一条
    assert len(rows) == 2


def test_parse_360_html_respects_num_results():
    assert len(web_search.parse_360_html(SO360_HTML, 1)) == 1


def test_parse_360_html_tolerates_garbage():
    assert web_search.parse_360_html("", 5) == []
    assert web_search.parse_360_html("<html>没有结果</html>", 5) == []


# --- 百度解析 ---------------------------------------------------------------
BAIDU_HTML = """
<div class="result c-container xpath-log new-pmd" mu="https://xingyaocode.cn/">
  <h3 class="cosc-title cos-link t"><a class="cosc-title-a"
      href="http://www.baidu.com/link?url=xyz">星瑶Code — 你的本地 AI 编程伙伴</a></h3>
  <div class="c-abstract"><span>从想法到可以运行的作品。</span></div>
</div>
<div class="result c-container" mu="">
  <h3 class="t"><a href="http://www.baidu.com/link?url=no-mu">没有真实地址的结果</a></h3>
  <div class="c-abstract">摘要二</div>
</div>
<div class="result c-container" mu="https://example.com/doc">
  <h3 class="t"><a href="http://www.baidu.com/link?url=doc">示例文档</a></h3>
  <p class="content-right_8Zs40">示例摘要。</p>
</div>
"""


def test_parse_baidu_html_prefers_mu_and_falls_back_to_href():
    rows = web_search.parse_baidu_html(BAIDU_HTML, 10)
    assert rows[0]["url"] == "https://xingyaocode.cn/"
    assert rows[0]["title"] == "星瑶Code — 你的本地 AI 编程伙伴"
    assert "从想法" in rows[0]["snippet"]
    # mu 为空时退用 href 的跳转链接
    assert rows[1]["url"].startswith("http://www.baidu.com/link?url=")
    assert rows[2]["url"] == "https://example.com/doc"


def test_parse_baidu_html_tolerates_garbage():
    assert web_search.parse_baidu_html("", 5) == []


# --- 必应 RSS 解析 ----------------------------------------------------------
BING_RSS = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><channel>
<item><title>标题一</title><link>https://a.example/1</link><description>摘要一</description></item>
<item><title>标题二</title><link>https://b.example/2</link><description>摘要二</description></item>
<item><title>坏条目</title><link>not-a-url</link><description>x</description></item>
</channel></rss>
"""


def test_parse_bing_rss_filters_invalid_links():
    rows = web_search.parse_bing_rss(BING_RSS, 10)
    assert [r["url"] for r in rows] == ["https://a.example/1", "https://b.example/2"]
    assert web_search.parse_bing_rss("not xml", 5) == []


# --- 通道编排 ---------------------------------------------------------------
@pytest.fixture(autouse=True)
def _clear_cooldown():
    web_search._cooldown_until.clear()
    yield
    web_search._cooldown_until.clear()


def test_search_web_falls_through_to_bing(monkeypatch):
    def boom(*args, **kwargs):
        raise web_search.SearchBackendError("被拦截")

    monkeypatch.setattr(web_search, "search_baidu", boom)
    monkeypatch.setattr(web_search, "search_360", boom)
    monkeypatch.setattr(
        web_search, "search_bing_rss", lambda *a, **k: [{"title": "t", "url": "https://x", "snippet": "s"}]
    )
    rows = web_search.search_web("查询", 3)
    assert rows and rows[0]["url"] == "https://x"


def test_search_web_raises_when_all_backends_fail(monkeypatch):
    def boom(*args, **kwargs):
        raise web_search.SearchBackendError("被拦截")

    for name in ("search_baidu", "search_360", "search_bing_rss"):
        monkeypatch.setattr(web_search, name, boom)
    with pytest.raises(web_search.SearchBackendError) as info:
        web_search.search_web("查询", 3)
    assert "被拦截" in str(info.value)


def test_search_web_marks_cooldown_after_antibot(monkeypatch):
    def boom(*args, **kwargs):
        web_search._start_cooldown("baidu")
        raise web_search.SearchBackendError("安全验证")

    monkeypatch.setattr(web_search, "search_baidu", boom)
    monkeypatch.setattr(web_search, "search_360", lambda *a, **k: [])
    monkeypatch.setattr(web_search, "search_bing_rss", lambda *a, **k: [{"title": "t", "url": "https://x", "snippet": "s"}])
    web_search.search_web("查询", 3)
    assert web_search._in_cooldown("baidu")
    # 冷却期内不会再调用百度
    calls = []
    monkeypatch.setattr(web_search, "search_baidu", lambda *a, **k: calls.append(1) or [])
    web_search.search_web("第二次查询", 3)
    assert calls == []


def test_search_web_rejects_empty_query():
    with pytest.raises(web_search.SearchBackendError):
        web_search.search_web("   ", 3)


def test_search_web_tops_up_from_later_backends(monkeypatch):
    """前面的通道条数不够时，用后续通道补足（泛词在百度常见只回 1~2 条）。"""
    monkeypatch.setattr(
        web_search,
        "search_baidu",
        lambda *a, **k: [{"title": "百度一条", "url": "https://b/1", "snippet": ""}],
    )
    monkeypatch.setattr(web_search, "search_360", lambda *a, **k: [])
    monkeypatch.setattr(
        web_search,
        "search_bing_rss",
        lambda *a, **k: [
            {"title": "必应一条", "url": "https://x/1", "snippet": ""},
            {"title": "必应二条", "url": "https://x/2", "snippet": ""},
        ],
    )
    rows = web_search.search_web("泛词", 3)
    assert [r["url"] for r in rows] == ["https://b/1", "https://x/1", "https://x/2"]


def test_search_web_dedupes_across_backends(monkeypatch):
    """不同通道返回同一条结果时只保留一次。"""
    duplicate = {"title": "同一条", "url": "https://same/1", "snippet": ""}
    monkeypatch.setattr(web_search, "search_baidu", lambda *a, **k: [dict(duplicate)])
    monkeypatch.setattr(web_search, "search_360", lambda *a, **k: [dict(duplicate)])
    monkeypatch.setattr(
        web_search,
        "search_bing_rss",
        lambda *a, **k: [{"title": "新", "url": "https://new/1", "snippet": ""}],
    )
    rows = web_search.search_web("查询", 3)
    assert [r["url"] for r in rows] == ["https://same/1", "https://new/1"]



