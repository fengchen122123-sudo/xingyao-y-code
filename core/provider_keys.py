from __future__ import annotations

import re
import threading
import time

# 失败 Key 的冷却时长（秒）：冷却期内不作为首选，但「全部冷却兜底」时仍参与尝试。
KEY_COOLDOWN_SECONDS = 300

_RE_KEY_SEPARATOR = re.compile(r"[\r\n,，;；]+")


def _now() -> float:
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
            return keys
        return "\n".join(
            key.strip() for key in list(keys) if isinstance(key, str) and key.strip()
        )
    except Exception:
        return ""


def _unique_keys(keys) -> list[str]:
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
    if not isinstance(value, str):
        return ""
    return value.strip().rstrip("/").lower()


def resolve_provider_for_route(model_name, base_url, model_id=None) -> dict | None:
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

        if isinstance(model_id, str) and model_id:
            for provider in providers:
                for model in provider.get("models") or []:
                    if isinstance(model, dict) and model.get("id") == model_id:
                        return provider

        target_base = _normalize_base_url(base_url)
        if not target_base:
            return None

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

            if not self._is_cooled_locked(self.keys[base], now):
                self._sticky_index = base
                return self.keys[base]

            for offset in range(1, count):
                index = (base + offset) % count
                if not self._is_cooled_locked(self.keys[index], now):
                    self._sticky_index = index
                    return self.keys[index]

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


_REGISTRY_LOCK = threading.Lock()
_POOL_REGISTRY: dict[str, ProviderKeyPool] = {}


def get_key_pool(provider: dict) -> ProviderKeyPool | None:
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
    try:
        provider = resolve_provider_for_route(model_name, base_url, model_id)
        if not provider:
            return None
        return get_key_pool(provider)
    except Exception:
        return None
