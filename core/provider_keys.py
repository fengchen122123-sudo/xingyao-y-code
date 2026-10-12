"""厂商多 Key 的解析工具与「粘性故障转移」Key 池。

设计见 docs/superpowers/specs/2026-10-12-provider-multi-key-failover-design.md。

本模块提供两部分能力：

1. 解析与回写：``split_provider_keys`` / ``join_provider_keys`` 宽容解析配置里的
   ``api_key`` 字符串（换行、英文/中文逗号、分号分隔；每段 trim、去空、保序去重）。
2. Key 池：``ProviderKeyPool`` 采用「命中率优先」策略——当前 Key 可用时不轮换（粘性）；
   Key 类失败（欠费/鉴权/权限）才把失败 Key 打入冷却并切换到下一个可用 Key；
   全部 Key 冷却时仍按环形顺序兜底尝试。池状态为进程内存态、线程安全（实例内 RLock
   + 模块级 registry 锁），不落盘：重启后全部 Key 默认可选，失效 Key 会再失败一次后
   被跳过，切换无感。

``resolve_provider_for_route`` / ``build_key_failover`` 负责把一次请求（model_name /
base_url / model_id）映射到配置中的 provider 及其 Key 池；配置读取惰性导入
``core.model_manager``，避免模块级循环依赖。本模块只做读取，不修改任何配置。
"""

from __future__ import annotations

import re
import threading
import time

# 失败 Key 的冷却时长（秒）：冷却期内不作为首选，但「全部冷却兜底」时仍参与尝试。
KEY_COOLDOWN_SECONDS = 300

# 多 Key 分隔符：换行（含 \r\n）、英文逗号、中文逗号、分号（中英文均可）。
_RE_KEY_SEPARATOR = re.compile(r"[\r\n,，;；]+")


def _now() -> float:
    """当前单调时钟（秒）。单独成函数，便于测试 monkeypatch 时间前进。"""
    return time.monotonic()


def split_provider_keys(raw) -> list[str]:
    """把配置中的 api_key 字符串解析为 Key 列表。

    - 分隔符：换行、英文逗号、中文逗号、分号；
    - 每段 strip 后去空、保序去重；
    - raw 非字符串（None / 数字 / 列表等）时返回空列表，绝不抛异常。
    """
    try:
        if not isinstance(raw, str):
            return []
        result: list[str] = []
        seen: set[str] = set()
        for part in _RE_KEY_SEPARATOR.split(raw):
            key = part.strip()
            if not key or key in seen:
                continue
            seen.add(key)
            result.append(key)
        return result
    except Exception:
        return []


def join_provider_keys(keys) -> str:
    """把 Key 列表拼回换行分隔的配置字符串（非列表输入安全降级为空串）。"""
    try:
        if keys is None:
            return ""
        if isinstance(keys, str):
            # 已是字符串：原样返回，容忍调用方直接透传配置原文
            return keys
        return "\n".join(
            key.strip() for key in list(keys) if isinstance(key, str) and key.strip()
        )
    except Exception:
        return ""


def _unique_keys(keys) -> list[str]:
    """把任意可迭代输入归一化为「去空 + 保序去重」的字符串列表。"""
    try:
        result: list[str] = []
        seen: set[str] = set()
        for item in keys:
            if not isinstance(item, str):
                continue
            key = item.strip()
            if not key or key in seen:
                continue
            seen.add(key)
            result.append(key)
        return result
    except Exception:
        return []


def _normalize_base_url(value) -> str:
    """base_url 归一化：去首尾空白、去尾部斜杠、统一小写。"""
    if not isinstance(value, str):
        return ""
    return value.strip().rstrip("/").lower()


def resolve_provider_for_route(model_name, base_url, model_id=None) -> dict | None:
    """按请求路由信息在配置中反查对应的 provider（读不到就返回 None）。

    优先级：
    1. ``model_id`` 与某 provider 下任一 ``model["id"]`` 字符串相等；
    2. base_url 归一化相同、且 ``model_name`` 等于某模型的 ``litellm_model`` 或 ``name``；
    3. base_url 归一化后仅对应唯一一个 provider；
    4. 否则 None（自定义/临时路由不做任何干预）。

    任何异常（含配置损坏）都返回 None，保证调用方行为与现状一致。
    """
    try:
        # 惰性导入：避免本模块与 model_manager 之间出现模块级循环依赖。
        from core.model_manager import load_config

        data = load_config()
        if not isinstance(data, dict):
            return None
        providers = [
            provider
            for provider in (data.get("providers") or [])
            if isinstance(provider, dict)
        ]
        if not providers:
            return None

        # 1) model_id 精确命中（顶层路由优先靠它定位，避免同名模型歧义）
        if isinstance(model_id, str) and model_id:
            for provider in providers:
                for model in provider.get("models") or []:
                    if isinstance(model, dict) and model.get("id") == model_id:
                        return provider

        target_base = _normalize_base_url(base_url)
        if not target_base:
            return None

        # 2) base_url 相同 + 模型名（litellm_model / name）匹配
        if isinstance(model_name, str) and model_name:
            for provider in providers:
                if _normalize_base_url(provider.get("base_url")) != target_base:
                    continue
                for model in provider.get("models") or []:
                    if not isinstance(model, dict):
                        continue
                    if (
                        model.get("litellm_model") == model_name
                        or model.get("name") == model_name
                    ):
                        return provider

        # 3) base_url 唯一对应（模型名无法确认时按地址认厂商）
        matched = [
            provider
            for provider in providers
            if _normalize_base_url(provider.get("base_url")) == target_base
        ]
        if len(matched) == 1:
            return matched[0]
        return None
    except Exception:
        return None


class ProviderKeyPool:
    """单个厂商的多 Key 池：粘性优先、失败冷却切换、全冷却兜底。

    选择规则（对应设计文档第 2 节）：
    - ``initial_key()``：新请求链起始选择。sticky 未冷却 → sticky；否则从 sticky 起
      环形找第一个未冷却并设为 sticky；全部冷却 → 仍从 sticky 起取第一个尝试。
    - ``on_failure(failed, tried)``：failed 进入冷却；再从 sticky 起环形扫描，第一遍
      找「不在 tried 且未冷却」的 Key；第二遍兜底找「不在 tried」的（冷却中也试）；
      都没有（本请求链已试完全部 Key）→ None。
    - ``on_success(used)``：清除该 Key 的冷却，并把 sticky 锚定到它。

    所有状态读写都由实例内 RLock 保护，可被 GUI / CLI / 子代理线程共享。
    """

    def __init__(self, keys) -> None:
        self._lock = threading.RLock()
        self._cooldown_until: dict[str, float] = {}
        self._sticky_index = 0
        if isinstance(keys, str):
            keys = split_provider_keys(keys)
        with self._lock:
            self.keys: list[str] = _unique_keys(keys)

    @property
    def key_count(self) -> int:
        """当前池内 Key 数量（与配置对账后会更新）。"""
        with self._lock:
            return len(self.keys)

    def _is_cooled_locked(self, key: str, now: float) -> bool:
        """key 是否处于冷却期（调用方须已持有锁）。"""
        return self._cooldown_until.get(key, 0.0) > now

    def initial_key(self) -> str:
        """新请求链的起始 Key：能粘就粘，冷却才换，全冷却兜底。"""
        with self._lock:
            if not self.keys:
                return ""
            now = _now()
            count = len(self.keys)
            base = self._sticky_index if 0 <= self._sticky_index < count else 0

            # 1) sticky 未冷却：直接沿用（粘性，不轮换）
            if not self._is_cooled_locked(self.keys[base], now):
                self._sticky_index = base
                return self.keys[base]

            # 2) 从 sticky 起环形找第一个未冷却的 Key
            for offset in range(1, count):
                index = (base + offset) % count
                if not self._is_cooled_locked(self.keys[index], now):
                    self._sticky_index = index
                    return self.keys[index]

            # 3) 全部冷却：仍从 sticky 起取第一个尝试（冷却只是「不作为首选」）
            self._sticky_index = base
            return self.keys[base]

    def on_failure(self, failed: str, tried: set[str]) -> str | None:
        """标记 failed 冷却，并返回本请求链下一个可尝试的 Key（无则 None）。

        调用方保证 ``tried`` 已包含 failed；本方法仍防御性地把 failed 并入 tried。
        """
        with self._lock:
            if isinstance(failed, str) and failed:
                self._cooldown_until[failed] = _now() + KEY_COOLDOWN_SECONDS

            if not self.keys:
                return None
            count = len(self.keys)

            tried_set: set[str] = set()
            if isinstance(tried, (set, frozenset, list, tuple)):
                tried_set.update(tried)
            if isinstance(failed, str) and failed:
                tried_set.add(failed)

            now = _now()
            base = self._sticky_index if 0 <= self._sticky_index < count else 0

            # 第一遍：不在 tried 且未冷却
            for offset in range(count):
                index = (base + offset) % count
                candidate = self.keys[index]
                if candidate in tried_set or self._is_cooled_locked(candidate, now):
                    continue
                self._sticky_index = index
                return candidate

            # 第二遍兜底：不在 tried 即可（冷却中也试，保证本链尽量发出去）
            for offset in range(count):
                index = (base + offset) % count
                candidate = self.keys[index]
                if candidate in tried_set:
                    continue
                self._sticky_index = index
                return candidate

            return None

    def on_success(self, used) -> None:
        """某 Key 请求成功：清除其冷却，并把 sticky 锚定到它。"""
        with self._lock:
            if not isinstance(used, str) or not used:
                return
            self._cooldown_until.pop(used, None)
            if used in self.keys:
                self._sticky_index = self.keys.index(used)

    def reconcile_keys(self, new_keys) -> None:
        """与配置对账（get_key_pool 每次调用时执行）。

        Key 列表变化时更新池内 keys：仍存在的 Key 保留其冷却记录；sticky 原 Key
        仍存在则保持指向，否则重置为第一个 Key。
        """
        normalized = _unique_keys(new_keys)
        with self._lock:
            if normalized == self.keys:
                return
            old_keys = self.keys
            sticky_key = (
                old_keys[self._sticky_index]
                if 0 <= self._sticky_index < len(old_keys)
                else None
            )
            self.keys = normalized
            valid_keys = set(normalized)
            self._cooldown_until = {
                key: until
                for key, until in self._cooldown_until.items()
                if key in valid_keys
            }
            if sticky_key is not None and sticky_key in valid_keys:
                self._sticky_index = normalized.index(sticky_key)
            else:
                self._sticky_index = 0


# 模块级 registry：provider_id -> ProviderKeyPool（进程内存态，重启即清空）。
_REGISTRY_LOCK = threading.Lock()
_POOL_REGISTRY: dict[str, ProviderKeyPool] = {}


def get_key_pool(provider: dict) -> ProviderKeyPool | None:
    """按 provider 配置取 Key 池；有效 Key 数 ≤ 1 或无法识别时返回 None。

    同一 provider_id 复用同一实例，并在每次调用时与配置对账（Key 增删/改序）。
    """
    try:
        if not isinstance(provider, dict):
            return None
        keys = split_provider_keys(provider.get("api_key"))
        if len(keys) <= 1:
            return None
        provider_id = provider.get("id")
        if not isinstance(provider_id, str) or not provider_id:
            return None

        with _REGISTRY_LOCK:
            pool = _POOL_REGISTRY.get(provider_id)
            if pool is None:
                pool = ProviderKeyPool(keys)
                _POOL_REGISTRY[provider_id] = pool
                return pool

        pool.reconcile_keys(keys)
        return pool
    except Exception:
        return None


def build_key_failover(model_name, base_url, model_id=None) -> ProviderKeyPool | None:
    """请求入口使用的一站式 helper：路由 → provider → Key 池。

    取不到池（单 Key / 路由未知 / 任何异常）返回 None，调用方保持现状行为。
    """
    try:
        provider = resolve_provider_for_route(model_name, base_url, model_id)
        if not provider:
            return None
        return get_key_pool(provider)
    except Exception:
        return None
