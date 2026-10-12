"""测试厂商多 Key 解析与「粘性故障转移」Key 池（core/provider_keys.py）。

覆盖：多分隔符解析与去重、单/双 Key 建池判定、粘性不轮换、失败切换与冷却跳过、
冷却恢复后粘性不抢占、全冷却兜底、配置对账（保留冷却与 sticky）、线程安全冒烟，
以及 resolve_provider_for_route / build_key_failover 的路由解析（注入假配置）。
"""

import threading

import pytest

from core import provider_keys
from core.provider_keys import (
    KEY_COOLDOWN_SECONDS,
    ProviderKeyPool,
    build_key_failover,
    get_key_pool,
    join_provider_keys,
    resolve_provider_for_route,
    split_provider_keys,
)


@pytest.fixture(autouse=True)
def _isolate_pool_registry(monkeypatch):
    """每个用例独享模块级 Key 池 registry，避免跨用例的池状态串扰。"""
    monkeypatch.setattr(provider_keys, "_POOL_REGISTRY", {})


def _make_provider(provider_id, api_key, base_url="https://api.mock.com", models=None):
    return {
        "id": provider_id,
        "name": provider_id,
        "base_url": base_url,
        "api_key": api_key,
        "models": models or [],
    }


# ---------------------------------------------------------------------------
# a) 解析：多分隔符 / 去重 / 空串 / 非字符串
# ---------------------------------------------------------------------------


def test_split_provider_keys_mixed_separators_and_dedupe():
    raw = "k1\nk2,k3，k4;k5\r\nk6\n\nk1\n"
    assert split_provider_keys(raw) == ["k1", "k2", "k3", "k4", "k5", "k6"]
    # 段内空格被 trim，重复 Key 保序去重
    assert split_provider_keys(" k1 , k2 ,k1 ") == ["k1", "k2"]


def test_split_provider_keys_blank_and_non_string():
    assert split_provider_keys("") == []
    assert split_provider_keys("  \n , ；; \r\n ") == []
    assert split_provider_keys(None) == []
    assert split_provider_keys(123) == []
    assert split_provider_keys(["k1", "k2"]) == []


def test_join_provider_keys_roundtrip():
    keys = ["k1", "k2", "k3"]
    assert join_provider_keys(keys) == "k1\nk2\nk3"
    assert split_provider_keys(join_provider_keys(keys)) == keys
    # 容忍非列表输入，不抛异常
    assert join_provider_keys(None) == ""
    assert join_provider_keys("k1") == "k1"
    assert join_provider_keys(42) == ""


# ---------------------------------------------------------------------------
# b) 单 Key 不建池；多 Key 建池并复用实例
# ---------------------------------------------------------------------------


def test_get_key_pool_single_key_returns_none():
    assert get_key_pool(_make_provider("p-single", "k1")) is None
    assert get_key_pool(_make_provider("p-single2", "k1\n")) is None
    assert get_key_pool(_make_provider("p-single3", "")) is None
    assert get_key_pool({}) is None
    assert get_key_pool("not-a-dict") is None


def test_get_key_pool_multi_key_and_reuse():
    provider = _make_provider("p-multi", "k1\nk2")
    pool = get_key_pool(provider)
    assert isinstance(pool, ProviderKeyPool)
    assert pool.keys == ["k1", "k2"]
    assert pool.key_count == 2

    # 同一 provider_id 复用实例，并对外部改序做对账
    same = get_key_pool(dict(provider, api_key="k2,k1\nk2"))
    assert same is pool
    assert same.keys == ["k2", "k1"]


# ---------------------------------------------------------------------------
# c) 粘性：initial_key 连续调用同一 Key；成功也不轮换
# ---------------------------------------------------------------------------


def test_initial_key_sticky_without_rotation():
    pool = ProviderKeyPool(["k1", "k2", "k3"])
    assert pool.initial_key() == "k1"
    assert pool.initial_key() == "k1"
    pool.on_success("k1")
    assert pool.initial_key() == "k1"


# ---------------------------------------------------------------------------
# d) 失败切换：k1 失败 → k2；k2 成功后粘性保持 k2（k1 冷却期不抢占）
# ---------------------------------------------------------------------------


def test_failure_switch_then_sticky_holds():
    pool = ProviderKeyPool(["k1", "k2", "k3"])
    current = pool.initial_key()
    assert current == "k1"

    nxt = pool.on_failure(current, {current})
    assert nxt == "k2"
    pool.on_success(nxt)
    assert pool.initial_key() == "k2"


# ---------------------------------------------------------------------------
# e) 冷却恢复：时间前进 301 秒 k1 冷却过期，但粘性仍是 k2（不抢占）
# ---------------------------------------------------------------------------


def test_cooldown_expiry_does_not_preempt_sticky(monkeypatch):
    assert KEY_COOLDOWN_SECONDS == 300
    clock = [1000.0]
    monkeypatch.setattr(provider_keys, "_now", lambda: clock[0])

    pool = ProviderKeyPool(["k1", "k2"])
    assert pool.initial_key() == "k1"
    assert pool.on_failure("k1", {"k1"}) == "k2"
    pool.on_success("k2")

    # 冷却期内（299 秒）k2 依然是首选
    clock[0] += KEY_COOLDOWN_SECONDS - 1
    assert pool.initial_key() == "k2"

    # 前进到 301 秒：k1 冷却过期，但粘性不抢占，initial_key 仍是 k2
    clock[0] += 2
    assert pool.initial_key() == "k2"
    # k1 已回到候选池：k2 失败时可再次选用 k1
    assert pool.on_failure("k2", {"k2"}) == "k1"


# ---------------------------------------------------------------------------
# f) 全冷却兜底：on_failure 仍返回未试过的 Key；全试过 → None
# ---------------------------------------------------------------------------


def test_all_cooled_fallback_and_exhausted(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(provider_keys, "_now", lambda: clock[0])

    pool = ProviderKeyPool(["k1", "k2"])
    assert pool.on_failure("k1", {"k1"}) == "k2"
    assert pool.on_failure("k2", {"k1", "k2"}) is None  # 全部冷却且全试过

    # 新请求链：全部冷却时 initial_key 仍回到 sticky
    start = pool.initial_key()
    assert start == "k2"
    # 兜底：k1 虽在冷却中，但本链未试过，仍返回它尝试
    fallback = pool.on_failure(start, {start})
    assert fallback == "k1"
    # tried 已包含全部 Key → None（本链结束）
    assert pool.on_failure(fallback, {start, fallback}) is None


# ---------------------------------------------------------------------------
# g) 对账：新增 Key 后保留冷却/粘性；sticky 被移除则重置
# ---------------------------------------------------------------------------


def test_reconcile_keeps_cooldown_and_sticky(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(provider_keys, "_now", lambda: clock[0])

    provider = _make_provider("p-reconcile", "k1\nk2")
    pool = get_key_pool(provider)
    assert isinstance(pool, ProviderKeyPool)
    assert pool.initial_key() == "k1"
    assert pool.on_failure("k1", {"k1"}) == "k2"  # k1 冷却，sticky 指向 k2

    # 配置新增 k3 并调整顺序：同一实例对账更新 keys
    provider2 = _make_provider("p-reconcile", "k2\nk1\nk3")
    pool2 = get_key_pool(provider2)
    assert pool2 is pool
    assert pool2.keys == ["k2", "k1", "k3"]
    assert pool2.key_count == 3
    # sticky 原 Key（k2）仍存在 → 保持；配置未变时重复调用仍是同一实例
    assert pool2.initial_key() == "k2"
    assert get_key_pool(provider2) is pool2
    # k1 的冷却记录被保留：环形扫描先遇到 k1 也会跳过，选中 k3
    assert pool2.on_failure("k2", {"k2"}) == "k3"

    # sticky 原 Key 被移除 → 重置为第一个 Key
    provider3 = _make_provider("p-reconcile", "k9\nk8")
    pool3 = get_key_pool(provider3)
    assert pool3 is pool
    assert pool3.keys == ["k9", "k8"]
    assert pool3.initial_key() == "k9"


# ---------------------------------------------------------------------------
# h) 线程安全冒烟：并发 initial_key / on_failure / on_success 不抛异常
# ---------------------------------------------------------------------------


def test_thread_safety_smoke():
    pool = ProviderKeyPool(["k1", "k2", "k3", "k4"])
    valid_keys = {"k1", "k2", "k3", "k4"}
    errors = []
    seen = []
    barrier = threading.Barrier(8)

    def worker():
        try:
            barrier.wait(timeout=10)
            for _ in range(50):
                current = pool.initial_key()
                assert current in valid_keys
                seen.append(current)
                nxt = pool.on_failure(current, {current})
                if nxt is not None:
                    assert nxt in valid_keys
                    pool.on_success(nxt)
        except Exception as exc:  # noqa: BLE001 - 冒烟用例收集所有线程异常
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == []
    assert pool.key_count == 4
    assert set(pool.keys) == valid_keys
    assert seen  # 线程确实执行过


# ---------------------------------------------------------------------------
# resolve_provider_for_route / build_key_failover（注入假配置）
# ---------------------------------------------------------------------------


def _mock_config():
    return {
        "providers": [
            {
                "id": "p-a",
                "name": "厂商A",
                "base_url": "https://api.a.com/",
                "api_key": "ka1\nka2",
                "models": [
                    {"id": "m-a1", "name": "a-model", "litellm_model": "openai/a-model"},
                ],
            },
            {
                "id": "p-b",
                "name": "厂商B",
                "base_url": "https://api.b.com",
                "api_key": "kb1",
                "models": [
                    {"id": "m-b1", "name": "b-model", "litellm_model": "openai/b-model"},
                ],
            },
            {
                "id": "p-c",
                "name": "厂商C",
                "base_url": "https://api.shared.com",
                "api_key": "kc1\nkc2",
                "models": [
                    {"id": "m-c1", "name": "c-model", "litellm_model": "openai/c-model"},
                ],
            },
            {
                "id": "p-d",
                "name": "厂商D",
                "base_url": "https://api.shared.com/",
                "api_key": "kd1\nkd2",
                "models": [
                    {"id": "m-d1", "name": "d-model", "litellm_model": "openai/d-model"},
                ],
            },
        ]
    }


def test_resolve_by_model_id(mocker):
    cfg = _mock_config()
    mocker.patch("core.model_manager.load_config", return_value=cfg)
    provider = resolve_provider_for_route(
        "whatever", "https://unknown.example.com", model_id="m-b1"
    )
    assert provider is cfg["providers"][1]


def test_resolve_by_base_url_and_model_name(mocker):
    cfg = _mock_config()
    mocker.patch("core.model_manager.load_config", return_value=cfg)
    # litellm_model 精确相等
    assert (
        resolve_provider_for_route("openai/b-model", "https://api.b.com")
        is cfg["providers"][1]
    )
    # 模型 name 也能命中；base_url 归一化（大小写 + 尾部斜杠）
    assert (
        resolve_provider_for_route("a-model", "HTTPS://API.A.COM///")
        is cfg["providers"][0]
    )


def test_resolve_single_provider_by_base_url(mocker):
    cfg = _mock_config()
    mocker.patch("core.model_manager.load_config", return_value=cfg)
    # 模型名未命中，但 base_url 唯一对应 p-a
    assert (
        resolve_provider_for_route("unknown-model", "https://api.a.com")
        is cfg["providers"][0]
    )
    # base_url 对应两个 provider（p-c / p-d）且模型名未命中 → 不猜，返回 None
    assert resolve_provider_for_route("unknown-model", "https://api.shared.com") is None
    # 模型名命中则能区分同 base_url 的两个 provider
    assert (
        resolve_provider_for_route("d-model", "https://api.shared.com")
        is cfg["providers"][3]
    )


def test_resolve_fallthrough_and_misses(mocker):
    cfg = _mock_config()
    mocker.patch("core.model_manager.load_config", return_value=cfg)
    # model_id 未命中时继续走 base_url + 模型名匹配
    assert (
        resolve_provider_for_route(
            "openai/a-model", "https://api.a.com", model_id="m-missing"
        )
        is cfg["providers"][0]
    )
    assert resolve_provider_for_route("nope", "https://api.none.com") is None
    assert resolve_provider_for_route("nope", "") is None


def test_resolve_returns_none_on_errors(mocker):
    mocker.patch(
        "core.model_manager.load_config", side_effect=RuntimeError("config boom")
    )
    assert resolve_provider_for_route("m", "https://api.a.com") is None

    mocker.patch("core.model_manager.load_config", return_value=[])
    assert resolve_provider_for_route("m", "https://api.a.com") is None

    mocker.patch("core.model_manager.load_config", return_value={"providers": "junk"})
    assert resolve_provider_for_route("m", "https://api.a.com") is None


def test_build_key_failover(mocker):
    cfg = _mock_config()
    mocker.patch("core.model_manager.load_config", return_value=cfg)
    # 单 Key provider → 不建池
    assert build_key_failover("openai/b-model", "https://api.b.com") is None
    # 双 Key provider → 返回池
    pool = build_key_failover("openai/a-model", "https://api.a.com")
    assert isinstance(pool, ProviderKeyPool)
    assert pool.key_count == 2
    assert pool.initial_key() == "ka1"
    # 未知路由 → None
    assert build_key_failover("nope", "https://api.none.com") is None


def test_build_key_failover_returns_none_on_error(mocker):
    mocker.patch(
        "core.model_manager.load_config", side_effect=RuntimeError("config boom")
    )
    assert build_key_failover("m", "https://api.a.com") is None
