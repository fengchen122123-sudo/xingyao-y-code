"""Provider dialects, model features, prompt ledger, cache affinity, and payload shaping."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
import uuid
from copy import deepcopy
from typing import Dict, List, Optional
from urllib.parse import urlsplit

import core.app_context as app_context
from core.reasoning_capabilities import (
    get_reasoning_capability,
    normalize_reasoning_model_name,
)

logger = logging.getLogger(__name__)

# 支持原生推理字段回传的 provider 白名单（按 provider 路由前缀匹配）
_REASONING_FIELD_PROVIDERS = ("anthropic", "deepseek", "moonshot")

_KIMI_NAME_RE = re.compile(r"(?:^|[-_/])k(?:2|3)(?:[._-]\d+)?(?:$|[-_])")


def _family_names(model_name):
    """按模型名判定归属的已知家族名集合（纯字符串，刻意不接收 api_base）。

    刻意不调用 _is_kimi_family / _is_deepseek_family / _is_glm_family /
    _is_qwen_family 本体：四个家族函数内部要调用本函数做「其他族名称探测」，
    互相调用会形成 qwen → kimi → qwen 的无限递归。
    """
    model = str(model_name or "").split("/")[-1].lower()
    names = set()
    if "kimi" in model or "moonshot" in model or _KIMI_NAME_RE.search(model):
        names.add("kimi")
    if "deepseek" in model:
        names.add("deepseek")
    if "glm" in model:
        names.add("glm")
    if "qwen" in model or "dashscope" in model:
        names.add("qwen")
    return frozenset(names)


def _is_kimi_family(model_name, api_base=""):
    """判断是否 Kimi/Moonshot 系模型（模型名优先，api_base 兜底）。

    经销商/聚合网关（api.scnet.cn、OpenRouter 等）的 base_url 不含
    moonshot 字样，必须靠模型名识别，否则所有 Kimi 深度适配全部失效。
    名称已属其他已知族（deepseek/glm/qwen）时域名兜底不生效；仅当名称
    无任何家族归属时才看 api_base 兜底（与 _is_qwen_family 同一道门）。
    """
    m = str(model_name or "").split("/")[-1].lower()
    if "kimi" in m or "moonshot" in m:
        return True
    if _KIMI_NAME_RE.search(m):
        return True
    if _family_names(model_name):
        return False  # 名称已属其他已知族 → 域名兜底不生效
    b = str(api_base or "").lower()
    return "moonshot" in b or "kimi" in b


def _is_official_kimi_route(api_base=""):
    """只识别已知支持 Kimi 缓存亲和私有字段的官方 API。"""
    try:
        parsed = urlsplit(str(api_base or ""))
        host = str(parsed.hostname or "").lower()
        path = str(parsed.path or "").rstrip("/").lower()
        port = parsed.port
    except Exception:
        return False
    if (
        str(parsed.scheme or "").lower() != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.query
        or parsed.fragment
    ):
        return False
    if host == "api.kimi.com":
        return path in {"/coding", "/coding/v1"}
    return (
        host in {"api.moonshot.cn", "api.moonshot.ai"}
        and path in {"", "/v1"}
    )


def _is_deepseek_family(model_name, api_base=""):
    """判断是否 DeepSeek 系模型（模型名优先，api_base 兜底）。

    名称已属其他已知族（kimi/glm/qwen）时域名兜底不生效；仅当名称无任何
    家族归属时才看 api_base 兜底（与 _is_qwen_family 同一道门）。
    """
    model = str(model_name or "").split("/")[-1].lower()
    if "deepseek" in model:
        return True
    if _family_names(model_name):
        return False  # 名称已属其他已知族 → 域名兜底不生效
    return _is_official_deepseek_route(api_base)


def _is_official_deepseek_route(api_base=""):
    normalized = str(api_base or "").rstrip("/").lower()
    return normalized in {
        "https://api.deepseek.com",
        "https://api.deepseek.com/v1",
        "https://api.deepseek.com/beta",
    }


def _is_official_zhipu_route(api_base=""):
    """只识别智谱官方隐式缓存路由（open.bigmodel.cn）。

    智谱上下文缓存为服务端隐式自动命中（无亲和参数、无显式标记），仅官方
    直连可确定生效；聚合网关/经销商挂载的 GLM 模型不放行。
    同域官方端点覆盖普通 API 与 CodingPlan 两种套餐（OpenAI/Anthropic 协议）。
    """
    try:
        parsed = urlsplit(str(api_base or ""))
        host = str(parsed.hostname or "").lower()
        path = str(parsed.path or "").rstrip("/").lower()
        port = parsed.port
    except Exception:
        return False
    if (
        str(parsed.scheme or "").lower() != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.query
        or parsed.fragment
    ):
        return False
    return host == "open.bigmodel.cn" and path in {
        "",
        "/api/paas/v4",
        "/api/coding/paas/v4",
        "/api/anthropic",
    }


def _is_glm_family(model_name, api_base=""):
    """判断是否智谱 GLM 系模型（模型名优先，api_base 兜底）。

    名称已属其他已知族（kimi/deepseek/qwen）时域名兜底不生效；仅当名称
    无任何家族归属时才看 api_base 兜底（与 _is_qwen_family 同一道门）。
    """
    model = str(model_name or "").split("/")[-1].lower()
    if "glm" in model:
        return True
    if _family_names(model_name):
        return False  # 名称已属其他已知族 → 域名兜底不生效
    return _is_official_zhipu_route(api_base)


def _is_official_dashscope_route(api_base=""):
    """只识别阿里云百炼/灵积官方端点（dashscope.aliyuncs.com、maas.aliyuncs.com 等）。"""
    try:
        parsed = urlsplit(str(api_base or ""))
        host = str(parsed.hostname or "").lower()
        port = parsed.port
    except Exception:
        return False
    if (
        str(parsed.scheme or "").lower() != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.query
        or parsed.fragment
    ):
        return False
    if host in {"dashscope.aliyuncs.com", "dashscope-intl.aliyuncs.com"}:
        return True
    if host.endswith(".aliyuncs.com") and "dashscope" in host:
        return True
    if host.endswith(".maas.aliyuncs.com"):
        return True
    return False


def _is_qwen_family(model_name, api_base=""):
    """判断是否 Qwen/通义千问系模型（名称优先，api_base 兜底）。

    名称优先：模型名命中本族（qwen/dashscope 子串）直接 True——`openai/qwen3.8-flash`
    这类本族名行为零变化。名称已属其他已知族（kimi/deepseek/glm）时域名兜底不生效，
    避免把多模型聚合域（如 .maas.aliyuncs.com 同时托管 deepseek/qwen/glm/kimi）
    上的外族模型误判进 Qwen 家族。仅当名称无任何家族归属时才看域名兜底。

    api_base 只认官方百炼/灵积端点（_is_official_dashscope_route 严格校验
    https + 域名），不做 "dashscope" 子串宽匹配——否则 base_url 恰含该
    子串的第三方中转会把其上的非 Qwen 模型误判进 Qwen 家族，导致
    _supports_reasoning_fields 等按 Qwen 语义处理消息。
    """
    model = str(model_name or "").split("/")[-1].lower()
    if "qwen" in model or "dashscope" in model:
        return True
    if _family_names(model_name):
        return False  # 名称已属其他已知族 → 域名兜底不生效
    return _is_official_dashscope_route(api_base)


_QWEN_CACHE_CONFIG_KEYS = (
    "enable_prompt_cache",
    "prompt_cache",
    "enable_cache_control",
    "cache_control",
)

# 顶层 cache_keepalive_routes：显式声明可安全保活的第三方路由（域名/子串列表）。
# 官方路由之外的网关默认不保活（未验证自动缓存时保活只是白烧配额），但实测
# 验证过缓存的网关可在 config.json 顶层显式放行——与 Qwen 的「官方默认 +
# 第三方显式配置」通道同构；官方路由判定逻辑不受此配置影响。
_KEEPALIVE_ROUTES_CONFIG_KEY = "cache_keepalive_routes"

# 顶层键解析结果缓存，指纹口径与 _resolve_model_config 一致（见 _qwen_cache_config_stamp）：
# stamp = (loader 身份, 配置文件 mtime)；条目格式
# (过期时间, stamp, routes, evidence_gate_enabled)——最后一项是
# cache_keepalive_evidence_gate（Task 5 保活证据门开关），与 routes 共用同一次读盘。
_keepalive_routes_cfg_cache = {"entry": None}

# 顶层 deepseek_cache_routes：显式声明可安全启用 DeepSeek Prompt Ledger 与
# tools 字母序的第三方网关（域名/子串列表）。官方路由之外默认不启用——
# 账本语义（服务端按 2048 token 块匹配的磁盘前缀缓存）只在 DeepSeek 上游成立，
# 挂载非 DeepSeek 模型的网关加进本配置只会白做规范化。判定逻辑与
# cache_keepalive_routes 同构：只新增显式通道，不改任何官方路由判定。
_DEEPSEEK_CACHE_ROUTES_CONFIG_KEY = "deepseek_cache_routes"

# 条目格式同 _keepalive_routes_cfg_cache：`{"entry": (过期时间, stamp, routes) | None}`。
# 测试可直接把 entry 置 None 强制重读配置。
_deepseek_cache_routes_cfg_cache = {"entry": None}

# 缓存开关判定在一轮对话里会被反复触发（_qwen_cache_enabled / _supports_cache_control /
# _supports_cache_keepalive / tools 规范化），而 load_config 每次都要读盘并解析整个
# config.json。这里按 (模型, 路由) 记忆解析结果，并用「配置指纹」做失效：
# 指纹 = load_config 函数身份（热重载/测试替换）+ 配置文件 mtime（用户真实编辑），
# 再叠加短 TTL 兜底，保证配置变更最迟 _QWEN_CACHE_CFG_TTL_SECONDS 秒后被看到。
_qwen_cache_cfg_cache = {}
_qwen_cache_cfg_lock = threading.RLock()
_QWEN_CACHE_CFG_TTL_SECONDS = 10.0


def _qwen_cache_config_stamp():
    """返回配置来源指纹；拿不到时返回 None，此时宁可不缓存也不给出过期结论。"""
    try:
        import config as config_module
        from core.model_manager import load_config as loader
    except Exception:
        return None
    try:
        mtime = os.stat(str(config_module._CONFIG_FILE)).st_mtime_ns
    except Exception:
        mtime = 0
    # 连同 loader 引用一起返回：持有引用可防止该对象被回收后 id 被新函数复用
    return (id(loader), loader, mtime)


def _resolve_model_config(model_name, api_base="", model_cfg=None):
    """获取模型/厂商配置（优先使用传入的 model_cfg，否则从 config.json 读取并继承 provider 缓存配置）。"""
    cfg = dict(model_cfg) if isinstance(model_cfg, dict) and model_cfg else None
    if cfg is not None and any(k in cfg for k in _QWEN_CACHE_CONFIG_KEYS):
        return cfg

    # 显式传入 model_cfg 时不缓存：它的字段要参与本次合并，缓存会丢掉调用方上下文
    stamp = None if cfg else _qwen_cache_config_stamp()
    key = (str(model_name or "").strip(), str(api_base or "").rstrip("/").lower())
    now = time.monotonic()
    if stamp is not None:
        with _qwen_cache_cfg_lock:
            cached = _qwen_cache_cfg_cache.get(key)
            if cached and cached[0] > now and cached[1] == stamp:
                return cached[2]

    resolved = None
    try:
        from core.model_manager import load_config

        data = load_config()
        # 同一份配置顺手缓存顶层 cache_keepalive_routes：同一轮里随后的
        # _supports_cache_keepalive 显式配置通道即可命中缓存，不再重复读盘
        _remember_keepalive_routes(data, stamp)
        # 同一份配置也顺手缓存顶层 deepseek_cache_routes：随后 is_deepseek_ledger_route
        # / _select_request_tools_for_route 的判定即可命中缓存，不再重复读盘
        _remember_deepseek_cache_routes(data, stamp)
        m_str = str(model_name or "").strip()
        base_url = str(api_base or "").rstrip("/").lower()
        for provider in data.get("providers", []):
            p_base = str(provider.get("base_url") or "").rstrip("/").lower()
            for model in provider.get("models", []):
                if model.get("litellm_model") != m_str:
                    continue
                if base_url and p_base and p_base != base_url:
                    continue
                merged = {}
                for k in _QWEN_CACHE_CONFIG_KEYS:
                    if k in provider:
                        merged[k] = provider[k]
                merged.update(model)
                if cfg:
                    merged.update(cfg)
                resolved = merged
                break
            if resolved is not None:
                break
    except Exception:
        pass
    if resolved is None:
        resolved = cfg or {}
    if stamp is not None:
        with _qwen_cache_cfg_lock:
            _qwen_cache_cfg_cache[key] = (
                now + _QWEN_CACHE_CFG_TTL_SECONDS, stamp, resolved,
            )
    return resolved


def _qwen_cache_enabled(model_name, api_base="", model_cfg=None):
    """判断当前 Qwen 模型路由是否开启缓存加速：只看模型家族，不看渠道。

    （model_cfg 参数仅为调用方签名兼容保留；enable_prompt_cache /
    prompt_cache 配置键仍被解析但不再是必要条件。）
    """
    return _is_qwen_family(model_name, api_base)


def _qwen_cache_control_configured(model_name, api_base="", model_cfg=None):
    """Qwen 路由是否启用显式 cache_control 断点：只看模型家族，不看渠道。

    不含运行期 400 黑名单降级：黑名单只关掉标记本身，tools 规范化等请求
    形状稳定措施不随 TTL 黑名单来回翻转（形状抖动比标记更伤缓存前缀）。
    """
    return _is_qwen_family(model_name, api_base)


def _normalize_route_match_key(api_base):
    """路由匹配键：去 scheme/用户信息/端口/路径/查询后的小写主机名（含子域）。

    配置条目与 api_base 走同一归一化：用户写完整 URL（https://example.com:8443/v1）
    或裸域名（example.com）都按域名匹配；归一化拿不到主机名时退化为小写原文，
    仍支持纯子串条目。
    """
    raw = str(api_base or "").strip()
    if not raw:
        return ""
    candidate = raw if "://" in raw else "//" + raw
    try:
        host = str(urlsplit(candidate).hostname or "")
    except Exception:
        host = ""
    return (host or raw).lower()


def _parse_route_match_entries(data, key):
    """从一份已加载的 config 中提取指定顶层键的路由条目并逐条归一化。"""
    raw = data.get(key) if isinstance(data, dict) else None
    if not isinstance(raw, (list, tuple)):
        return ()
    routes = []
    for item in raw:
        entry = _normalize_route_match_key(item)
        if entry:
            routes.append(entry)
    return tuple(routes)


def _parse_keepalive_routes(data):
    """从一份已加载的 config 中提取 cache_keepalive_routes 并逐条归一化。"""
    return _parse_route_match_entries(data, _KEEPALIVE_ROUTES_CONFIG_KEY)


def _parse_deepseek_cache_routes(data):
    """从一份已加载的 config 中提取 deepseek_cache_routes 并逐条归一化。"""
    return _parse_route_match_entries(data, _DEEPSEEK_CACHE_ROUTES_CONFIG_KEY)


def _remember_keepalive_routes(data, stamp):
    """把顶层 cache_keepalive_routes 的解析结果按配置指纹写入缓存（重复读盘去重）。

    同一份 entry 顺带记住 cache_keepalive_evidence_gate（Task 5 保活证据门
    开关）：两者都出自同一次 load_config，各存一份会让同一个键多付一次读盘。
    条目格式：(过期时间, stamp, routes, evidence_gate_enabled)。
    """
    if stamp is None:
        return
    routes = _parse_keepalive_routes(data)
    with _qwen_cache_cfg_lock:
        _keepalive_routes_cfg_cache["entry"] = (
            time.monotonic() + _QWEN_CACHE_CFG_TTL_SECONDS, stamp, routes,
            _parse_keepalive_evidence_gate(data),
        )


def _configured_keepalive_routes():
    """读取顶层 cache_keepalive_routes（域名/子串列表），带配置指纹缓存。"""
    stamp = _qwen_cache_config_stamp()
    now = time.monotonic()
    if stamp is not None:
        with _qwen_cache_cfg_lock:
            cached = _keepalive_routes_cfg_cache.get("entry")
            if cached and cached[0] > now and cached[1] == stamp:
                return cached[2]
    routes = ()
    gate_enabled = True
    try:
        from core.model_manager import load_config

        data = load_config()
        routes = _parse_keepalive_routes(data)
        gate_enabled = _parse_keepalive_evidence_gate(data)
    except Exception:
        pass
    if stamp is not None:
        with _qwen_cache_cfg_lock:
            _keepalive_routes_cfg_cache["entry"] = (
                now + _QWEN_CACHE_CFG_TTL_SECONDS, stamp, routes, gate_enabled,
            )
    return routes


def _keepalive_route_explicitly_enabled(api_base):
    """api_base 归一化后包含显式配置中任一条目 → 该路由启用保活。"""
    host = _normalize_route_match_key(api_base)
    if not host:
        return False
    return any(entry in host for entry in _configured_keepalive_routes())


def _remember_deepseek_cache_routes(data, stamp):
    """把顶层 deepseek_cache_routes 的解析结果按配置指纹写入缓存（重复读盘去重）。"""
    if stamp is None:
        return
    routes = _parse_deepseek_cache_routes(data)
    with _qwen_cache_cfg_lock:
        _deepseek_cache_routes_cfg_cache["entry"] = (
            time.monotonic() + _QWEN_CACHE_CFG_TTL_SECONDS, stamp, routes,
        )


def _configured_deepseek_cache_routes():
    """读取顶层 deepseek_cache_routes（域名/子串列表），带配置指纹缓存。"""
    stamp = _qwen_cache_config_stamp()
    now = time.monotonic()
    if stamp is not None:
        with _qwen_cache_cfg_lock:
            cached = _deepseek_cache_routes_cfg_cache.get("entry")
            if cached and cached[0] > now and cached[1] == stamp:
                return cached[2]
    routes = ()
    try:
        from core.model_manager import load_config

        routes = _parse_deepseek_cache_routes(load_config())
    except Exception:
        pass
    if stamp is not None:
        with _qwen_cache_cfg_lock:
            _deepseek_cache_routes_cfg_cache["entry"] = (
                now + _QWEN_CACHE_CFG_TTL_SECONDS, stamp, routes,
            )
    return routes


def _is_cache_optimized_deepseek_route(api_base=""):
    """DeepSeek 缓存优化路由 = 官方端点 ∨ 显式配置声明的 DeepSeek 上游网关。

    归一化走 _normalize_route_match_key（去 scheme/端口/路径后的小写主机名），
    用户写完整 URL 或裸域名都能命中；官方判定分支保持不变。
    """
    if _is_official_deepseek_route(api_base):
        return True
    host = _normalize_route_match_key(api_base)
    if not host:
        return False
    return any(entry in host for entry in _configured_deepseek_cache_routes())


def _glm_keepalive_thinking_locked(model_name):
    """GLM 强制思考模型判定：思考没有关闭档，保活不得翻转成 disabled。

    能力表里 glm-5.3 / glm-5.3-flash 强制思考（thinking.type=disabled 直接
    报错），无 off/on 关闭档即视为强制思考。显式限定 GLM 家族：Kimi-K3、
    kimi-for-coding、minimax 等同样没有关闭档的非 GLM 模型不进此分支，
    其保活快照保持既有字节不变。
    """
    if not _is_glm_family(model_name):
        return False
    from core.reasoning_capabilities import get_reasoning_capability

    capability = get_reasoning_capability(model_name)
    if capability.kind == "hidden":
        return False
    return not ({"off", "on"} & set(capability.options))


def is_deepseek_ledger_route(model_name, api_base):
    """DeepSeek 家族缓存优化路由判定：主会话不可变 Prompt Ledger 的核心条件。

    只看模型家族，不看渠道：任意网关（tokenrhythm、阿里云 token-plan、
    其它中转）上的 deepseek 模型一律放行。供 agent_service 的水位线基线与
    主动轮换判定复用（账本启用还需 persist / parent_session / cache_scope=main
    等会话层条件，见 _uses_deepseek_prompt_ledger）。
    """
    return _is_deepseek_family(model_name, api_base)


def _uses_deepseek_prompt_ledger(
    model_name, api_base, cache_scope, persist, parent_session
):
    """Select the isolated ledger only for official DeepSeek main sessions."""
    return bool(
        persist
        and parent_session
        and str(cache_scope or "main") == "main"
        and is_deepseek_ledger_route(model_name, api_base)
    )


def _select_request_tools_for_route(model_name, api_base, tools, model_cfg=None):
    """Canonicalize tools for cache-optimized routes; preserve every other provider payload.

    覆盖 DeepSeek 家族（账本 + 工具字母序）与 Qwen 家族（断点 + 会话缓存头 +
    工具字母序）的全部渠道——tools 字母序是缓存前缀字节稳定的前提，任何开启
    缓存优化的路由都必须排序，否则 MCP 工具顺序一抖动前缀整体失效。
    """
    canonicalize = (
        _is_deepseek_family(model_name, api_base)
        or _is_qwen_family(model_name, api_base)
    )
    if not canonicalize:
        return tools
    selected = deepcopy(list(tools or []))
    return sorted(
        selected,
        key=lambda tool: str(tool.get("function", {}).get("name") or ""),
    )


def _deepseek_stable_header_matches(generation, current_messages, current_tools):
    """Compare the stable request header represented by this OpenAI payload."""
    if generation["tools"] != current_tools:
        return False
    old_messages = generation["messages"]
    if not old_messages or not current_messages:
        return old_messages == current_messages
    old_system = old_messages[0] if old_messages[0].get("role") == "system" else None
    current_system = (
        current_messages[0] if current_messages[0].get("role") == "system" else None
    )
    return old_system == current_system


# 账本换代原因取值域（写进 cache_request_diagnostics.request_reason）：
#   ""                     未换代，本次为严格追加（热路径）
#   first_generation       该会话冷启动，创建首个账本代
#   pending_rotation       主动轮换登记被消费（水位线压缩成果落库后的计划内重建）
#   caller_force_new       调用方显式 force_new（超窗压缩成功后的重试）
#   header_tools_changed   工具契约变化导致稳定头失配
#   header_system_changed  system 头变化导致稳定头失配
#   boundary_ambiguous     账本前缀无法与当前历史建立唯一追加边界
def _deepseek_header_rotation_reason(generation, current_messages, current_tools):
    """稳定头失配的具体归因：工具契约先于 system 头判定，与比对顺序一致。"""
    if generation["tools"] != current_tools:
        return "header_tools_changed"
    return "header_system_changed"


def _new_tail_after_ledger_prefix(ledger_messages, current_messages):
    """Return the append-only tail without trusting rewritten normal history.

    The common case is a literal prefix.  After normal-history maintenance, the
    immutable ledger prefix may no longer occur in full; in that case its final
    message must occur exactly once in the current request and acts as the
    explicit hand-off boundary.  Ambiguous or missing boundaries fail loudly.
    """
    if current_messages[:len(ledger_messages)] == ledger_messages:
        return deepcopy(current_messages[len(ledger_messages):])
    if not ledger_messages:
        return deepcopy(current_messages)
    boundary = ledger_messages[-1]
    matches = [
        index for index, message in enumerate(current_messages)
        if message == boundary
    ]
    if len(matches) != 1:
        raise RuntimeError(
            "DeepSeek prompt 账本无法与当前历史建立唯一追加边界"
        )
    return deepcopy(current_messages[matches[0] + 1:])


# 会话级待轮换代登记表：agent_service 的水位线压缩成功后，若实际请求
# （账本字节）仍超出窗口，可在此登记一次主动轮换——下一次账本请求以
# 压缩后的历史 force_new 重建，避免"等 provider 报超限错误才被动轮换"
# 的三段等待（失败请求排队 + 强制压缩 + 全价重发）。只记会话名不记正文。
_pending_ledger_rotations = {}
_pending_ledger_rotations_lock = threading.Lock()


def mark_deepseek_generation_rotation(session_name):
    """登记指定会话在下一次账本请求时主动轮换代（幂等，一次消费）。"""
    key = str(session_name or "").strip()
    if not key:
        return
    with _pending_ledger_rotations_lock:
        _pending_ledger_rotations[key] = True


def consume_deepseek_generation_rotation(session_name) -> bool:
    """取走并清除待轮换代标记（一次登记只生效一次）。"""
    key = str(session_name or "").strip()
    if not key:
        return False
    with _pending_ledger_rotations_lock:
        return bool(_pending_ledger_rotations.pop(key, False))


def _prepare_deepseek_ledger_request(
    manager, session_name, model_name, api_base, cache_scope, messages, tools,
    *, force_new=False
):
    """Build and persist the immutable request view for one DeepSeek call.

    返回值额外带 `rotation_reason`：本次是否换代、为什么换代。空串表示
    沿用了既有账本代（严格追加）。取值域见 `_deepseek_header_rotation_reason`
    上方的注释；调用方（core/llm.py）把它覆写进 cache_request_diagnostics.request_reason，
    让线上低命中能被直接归因，而不是只看到一个笼统的"上下文压缩或历史重写"。
    """
    rotation_reason = "caller_force_new" if force_new else ""
    if not force_new and consume_deepseek_generation_rotation(session_name):
        force_new = True
        rotation_reason = "pending_rotation"
    generation = manager.get_or_create_deepseek_prompt_generation(
        session_name,
        model_name,
        api_base,
        cache_scope,
        deepcopy(messages),
        deepcopy(tools),
        force_new=force_new,
    )
    if generation.get("created"):
        return {
            "generation_id": generation["id"],
            "messages": generation["messages"],
            "tools": generation["tools"],
            "rotation_reason": rotation_reason or "first_generation",
        }
    if not _deepseek_stable_header_matches(generation, messages, tools):
        rotation_reason = _deepseek_header_rotation_reason(
            generation, messages, tools
        )
        generation = manager.get_or_create_deepseek_prompt_generation(
            session_name,
            model_name,
            api_base,
            cache_scope,
            deepcopy(messages),
            deepcopy(tools),
            force_new=True,
        )
        return {
            "generation_id": generation["id"],
            "messages": generation["messages"],
            "tools": generation["tools"],
            "rotation_reason": rotation_reason,
        }
    try:
        tail = _new_tail_after_ledger_prefix(generation["messages"], messages)
    except RuntimeError:
        generation = manager.get_or_create_deepseek_prompt_generation(
            session_name,
            model_name,
            api_base,
            cache_scope,
            deepcopy(messages),
            deepcopy(tools),
            force_new=True,
        )
        return {
            "generation_id": generation["id"],
            "messages": generation["messages"],
            "tools": generation["tools"],
            "rotation_reason": "boundary_ambiguous",
        }
    if tail:
        manager.append_deepseek_prompt_messages(
            generation["id"], tail, expected_count=len(generation["messages"])
        )
        generation["messages"].extend(tail)
    return {
        "generation_id": generation["id"],
        "messages": generation["messages"],
        "tools": generation["tools"],
        "rotation_reason": "",
    }


def _deepseek_thinking_enabled(model_name, api_base="", model_cfg=None):
    if not _is_deepseek_family(model_name, api_base):
        return False
    cfg = model_cfg or {}
    if isinstance(cfg.get("enable_thinking"), bool):
        return cfg["enable_thinking"]
    if not _is_official_deepseek_route(api_base):
        return False
    model = str(model_name or "").split("/")[-1].lower()
    return "v4" in model or "reasoner" in model


# ---------------------------------------------------------------------------
# 保活按车道证据止损（跨渠道缓存命中率修复 · Task 5）
#
# 保活是每 120 秒一次的全价前缀请求；在亏损车道上（如阿里云
# deepseek-v4.1-flash keepalive 118 轮仅 24.04% 命中）纯属烧 token。
# 规则（写死）：
# - 双条件同时满足才停：该车道 keepalive 的 token 加权命中率 < 0.5 且轮次 >= 10；
# - 样本不足绝不动作（R3 保险丝）：轮次 < 10 一律不停，哪怕命中率 0%；
# - 止损 TTL 小时级（3600s）：保活亏损是稳态特征而非偶发 400，短 TTL 会导致
#   反复复活（900s 那套是给「上游明确拒绝」用的，语义不同）；
# - 顶层开关 cache_keepalive_evidence_gate（默认开）；关掉时判定行为与改动前
#   逐字等价——本门只可能停「已判定亏损」的车道，健康车道零变化。
# ---------------------------------------------------------------------------

_KEEPALIVE_EVIDENCE_GATE_CONFIG_KEY = "cache_keepalive_evidence_gate"
_KEEPALIVE_EVIDENCE_LOSS_THRESHOLD = 0.5
_KEEPALIVE_EVIDENCE_MIN_TURNS = 10
_KEEPALIVE_EVIDENCE_DISABLED_TTL_SECONDS = 3600.0

# (model, api_base) 归一化键 -> 止损到期时间戳（monotonic）。与 400 拉黑表
# （_affinity_disabled_routes / _cache_control_disabled_routes）分开：触发
# 条件（车道证据亏损 vs 上游拒绝）、TTL（小时级 vs 900s）与语义都不同。
_keepalive_evidence_disabled_routes = {}
_keepalive_evidence_disabled_lock = threading.RLock()


def _keepalive_evidence_route_key(model_name, api_base):
    """止损表键：(模型, 归一化 api_base)，与 _cache_usage_key 的键口径一致。"""
    return (
        str(model_name or "").strip().lower(),
        str(api_base or "").rstrip("/").lower(),
    )


def _parse_keepalive_evidence_gate(data):
    """从一份已加载的 config 提取开关；缺省/非布尔 → 默认开（True）。"""
    raw = (
        data.get(_KEEPALIVE_EVIDENCE_GATE_CONFIG_KEY)
        if isinstance(data, dict)
        else None
    )
    return raw if isinstance(raw, bool) else True


def _keepalive_evidence_gate_enabled():
    """读取顶层 cache_keepalive_evidence_gate（默认开），带配置指纹缓存。

    与 _configured_keepalive_routes 共用同一份缓存条目：开关与保活白名单出自
    同一次 load_config，天真的做法是各存一份、各读一次盘——那会让保活判定在
    每轮多付一次配置读取。这里只在共用 entry 缺失时委托 routes 侧填充。
    """
    stamp = _qwen_cache_config_stamp()
    now = time.monotonic()
    if stamp is not None:
        with _qwen_cache_cfg_lock:
            cached = _keepalive_routes_cfg_cache.get("entry")
            if cached and cached[0] > now and cached[1] == stamp and len(cached) >= 4:
                return bool(cached[3])
    # 未命中（或拿不到指纹）：委托 routes 读取，由它填同一份 entry。
    _configured_keepalive_routes()
    with _qwen_cache_cfg_lock:
        cached = _keepalive_routes_cfg_cache.get("entry")
        if cached and len(cached) >= 4:
            return bool(cached[3])
    return True


def _disable_keepalive_lane(model_name, api_base, turns, hit_rate, ttl_seconds=None):
    """止损某车道的保活（小时级 TTL），并记录判定依据。

    日志只记键名与数字（model / api_base / turns / hit_rate / at /
    ttl_seconds），绝不记录任何 prompt 或消息正文。
    """
    ttl = (
        _KEEPALIVE_EVIDENCE_DISABLED_TTL_SECONDS
        if ttl_seconds is None else float(ttl_seconds)
    )
    key = _keepalive_evidence_route_key(model_name, api_base)
    with _keepalive_evidence_disabled_lock:
        _keepalive_evidence_disabled_routes[key] = time.monotonic() + ttl
    logger.warning(
        "保活按车道证据止损 model=%s api_base=%s turns=%d hit_rate=%.4f "
        "at=%s ttl_seconds=%.0f",
        model_name,
        api_base,
        int(turns),
        float(hit_rate),
        time.strftime("%Y-%m-%dT%H:%M:%S"),
        ttl,
    )
    return key


def _keepalive_evidence_route_disabled(model_name, api_base):
    """该车道是否仍在止损期内（过期条目顺手清理）。"""
    key = _keepalive_evidence_route_key(model_name, api_base)
    now = time.monotonic()
    with _keepalive_evidence_disabled_lock:
        expire_at = _keepalive_evidence_disabled_routes.get(key)
        if expire_at is None:
            return False
        if expire_at <= now:
            _keepalive_evidence_disabled_routes.pop(key, None)
            return False
        return True


def _keepalive_lane_disabled_by_evidence(model_name, api_base):
    """车道证据门：低命中且样本足够 → 停该车道保活；否则不动作。

    顺序即语义：开关关闭 → 不动作（与改动前逐字等价）；止损期内 → 直接停；
    样本不足 → 不动作（R3 保险丝）；命中率 >= 阈值 → 不动作（健康车道零变化）。
    """
    if not _keepalive_evidence_gate_enabled():
        return False
    if _keepalive_evidence_route_disabled(model_name, api_base):
        return True
    from core.llm_diagnostics import lane_keepalive_evidence

    turns, _prompt_tokens, _cached_tokens, hit_rate = lane_keepalive_evidence(
        model_name, api_base
    )
    if turns < _KEEPALIVE_EVIDENCE_MIN_TURNS:
        return False  # 样本不足：绝不动作（R3 保险丝）
    if hit_rate is None or hit_rate >= _KEEPALIVE_EVIDENCE_LOSS_THRESHOLD:
        return False  # 命中率未低于阈值：健康车道行为零变化
    _disable_keepalive_lane(model_name, api_base, turns, hit_rate)
    return True


def _supports_cache_keepalive(model_name, api_base="", model_cfg=None):
    """保活适用于四族模型（DeepSeek/Kimi/智谱/Qwen）的任意渠道路由。

    家族判定之外，config.json 顶层 cache_keepalive_routes 显式声明的网关路由
    （按域名归一化匹配）继续为「非四族模型」提供显式保活通道。

    在此之上叠加「车道证据门」（Task 5）：该车道 keepalive 实测命中率 < 0.5
    且样本轮次 >= 10 → 停该车道保活（小时级自愈）；样本不足绝不动作。
    顶层开关 cache_keepalive_evidence_gate=false 时本门整体关闭，行为与
    改动前逐字等价。
    """
    supported = (
        _is_kimi_family(model_name, api_base)
        or _is_deepseek_family(model_name, api_base)
        or _is_glm_family(model_name, api_base)
        or _is_qwen_family(model_name, api_base)
        or _keepalive_route_explicitly_enabled(api_base)
    )
    if not supported:
        return False
    return not _keepalive_lane_disabled_by_evidence(model_name, api_base)


def _supports_reasoning_fields(model_name, api_base=""):
    prefix = str(model_name or "").split("/")[0].lower()
    if prefix in _REASONING_FIELD_PROVIDERS:
        return True
    # Kimi / DeepSeek / Qwen 推理模型（含各类经销商 openai/ 路由）：多轮工具调用要求 assistant
    # 消息保留 reasoning_content，剥掉会导致 400、推理链断裂或 KV cache 前缀断层失效
    return (
        _is_kimi_family(model_name, api_base)
        or _is_deepseek_family(model_name, api_base)
        or _is_qwen_family(model_name, api_base)
    )


def _requires_tool_call_reasoning(model_name, api_base=""):
    """DeepSeek thinking 模式：带 tool_calls 的 assistant 必须回传 reasoning_content key。"""
    try:
        from core.model_manager import get_model_config_by_litellm_model
        model_cfg = get_model_config_by_litellm_model(model_name, api_base) or {}
    except Exception:
        model_cfg = {}
    return _deepseek_thinking_enabled(model_name, api_base, model_cfg)


# 需要显式 cache_control 标记才启用 prompt caching 的 provider 白名单（按 provider 前缀）：
#   - anthropic: 必须显式标记，命中后缓存 token 按 1/10 计费
# Qwen 不在此白名单里，但 Qwen 家族任意渠道默认启用（见 _supports_cache_control）；
# 其余 OpenAI 兼容网关默认不标记——隐式缓存本就命中，而显式标记可能被严格校验的
# 网关拒绝为 400（真被拒绝时由 _disable_cache_control_route 临时降级兜底）。
_CACHE_CONTROL_PROVIDERS = ("anthropic",)

# 被上游以 400 明确拒绝过 cache_control 的路由 -> 恢复时间戳（monotonic）。
# 只临时拉黑：一次误判（例如中转站偶发的参数校验抖动）不该把该路由的优化
# 永久关掉直到进程重启，到期后自动再给一次机会。
_cache_control_disabled_routes = {}
_cache_control_disabled_lock = threading.RLock()
_CACHE_CONTROL_REJECTION_TTL_SECONDS = 900.0


def _cache_control_route_key(model_name, api_base):
    return (str(model_name or ""), str(api_base or "").rstrip("/").lower())


def _disable_cache_control_route(model_name, api_base, ttl_seconds=None):
    """临时禁用某路由的 cache_control 标记，返回被拉黑的路由 key。"""
    ttl = (
        _CACHE_CONTROL_REJECTION_TTL_SECONDS
        if ttl_seconds is None else float(ttl_seconds)
    )
    key = _cache_control_route_key(model_name, api_base)
    with _cache_control_disabled_lock:
        _cache_control_disabled_routes[key] = time.monotonic() + ttl
    return key


def _cache_control_route_disabled(model_name, api_base):
    """该路由是否仍在 cache_control 拉黑期内（过期条目顺手清理）。"""
    key = _cache_control_route_key(model_name, api_base)
    now = time.monotonic()
    with _cache_control_disabled_lock:
        expire_at = _cache_control_disabled_routes.get(key)
        if expire_at is None:
            return False
        if expire_at <= now:
            _cache_control_disabled_routes.pop(key, None)
            return False
        return True


def _is_cache_control_rejection(api_err):
    """判定上游是否以 400/bad request 明确拒绝了 cache_control 字段。

    必须同时满足两个条件，缺一不判：
    1. 状态码确为 400（或异常类型名为 BadRequestError）；
    2. 错误文本点名 cache_control / ephemeral 这类缓存断点字段。
    只按文案关键词（"400"/"invalid"/"schema"）宽泛匹配的话，一次无关故障
    （网络抖动文案里带字节数、上游 5xx）就会把该路由的优化整体关掉。
    """
    if api_err is None:
        return False
    is_bad_request = (
        getattr(api_err, "status_code", None) == 400
        or type(api_err).__name__ == "BadRequestError"
    )
    if not is_bad_request:
        return False
    text = str(api_err).lower()
    return "cache_control" in text or "cache control" in text or "ephemeral" in text


# 被上游以 400 明确拒绝过 Kimi 缓存亲和键（prompt_cache_key / metadata.user_id）
# 的路由 -> 恢复时间戳（monotonic）。与 cache_control 降级完全同构：一次误判
# 不该把该路由的亲和优化永久关掉，TTL 到期后自动再给一次机会。
_affinity_disabled_routes = {}
_affinity_disabled_lock = threading.RLock()


def _disable_affinity_route(model_name, api_base, ttl_seconds=None):
    """临时禁用某路由的 Kimi 缓存亲和键，返回被拉黑的路由 key。"""
    ttl = (
        _CACHE_CONTROL_REJECTION_TTL_SECONDS
        if ttl_seconds is None else float(ttl_seconds)
    )
    key = _cache_control_route_key(model_name, api_base)
    with _affinity_disabled_lock:
        _affinity_disabled_routes[key] = time.monotonic() + ttl
    return key


def _affinity_route_disabled(model_name, api_base):
    """该路由是否仍在亲和键拉黑期内（过期条目顺手清理）。"""
    key = _cache_control_route_key(model_name, api_base)
    now = time.monotonic()
    with _affinity_disabled_lock:
        expire_at = _affinity_disabled_routes.get(key)
        if expire_at is None:
            return False
        if expire_at <= now:
            _affinity_disabled_routes.pop(key, None)
            return False
        return True


def _is_kimi_affinity_rejection(api_err):
    """判定上游是否以 400/bad request 明确拒绝了 Kimi 缓存亲和键字段。

    必须同时满足两个条件，缺一不判：
    1. 状态码确为 400（或异常类型名为 BadRequestError）；
    2. 错误文本点名 prompt_cache_key / user_id 这类亲和字段。
    只按文案关键词宽泛匹配的话，一次无关故障（网络抖动、上游 5xx）就会把
    该路由的亲和优化整体关掉。
    """
    if api_err is None:
        return False
    is_bad_request = (
        getattr(api_err, "status_code", None) == 400
        or type(api_err).__name__ == "BadRequestError"
    )
    if not is_bad_request:
        return False
    text = str(api_err).lower()
    return "prompt_cache_key" in text or "user_id" in text


def _strip_cache_affinity_options(result):
    """剥离请求参数中的 Kimi 缓存亲和字段（400 降级重试用）。

    只动 prompt_cache_key 与 metadata.user_id 两个亲和字段本身；
    extra_body / metadata 剥离后若为空容器则整体移除，保证重试请求
    与「从未附加亲和」的请求形状一致。
    """
    extra_body = result.get("extra_body")
    if isinstance(extra_body, dict):
        extra_body.pop("prompt_cache_key", None)
        if not extra_body:
            result.pop("extra_body", None)
    metadata = result.get("metadata")
    if isinstance(metadata, dict):
        metadata.pop("user_id", None)
        if not metadata:
            result.pop("metadata", None)


def _supports_cache_control(model_name, api_base="", model_cfg=None):
    """判断 provider 是否支持 cache_control 字段。

    保守策略：
    - Anthropic：始终支持
    - Qwen 家族：任意渠道默认启用（家族判定，不再依赖渠道或配置）
    - 若先前已被上游 400 拒绝，降级一段时间（TTL 到期后自动恢复）
    """
    if _cache_control_route_disabled(model_name, api_base):
        return False

    prefix = str(model_name or "").split("/")[0].lower()
    if prefix in _CACHE_CONTROL_PROVIDERS:
        return True

    return _qwen_cache_control_configured(model_name, api_base, model_cfg)


def _tool_contract_fingerprint(tools):
    """返回最终工具 schema 的确定性指纹，用于隔离不同请求契约的缓存通道。"""
    payload = json.dumps(
        tools or [],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _apply_cache_control(messages, model_name, api_base="", model_cfg=None, disabled=False):
    """给消息列表加 cache_control 标记，启用 provider 侧 prompt caching。

    标记策略：
    - system 消息：必缓存（固定前缀，单次会话内不变）
    - 最后一条 assistant 消息：设置缓存边界。
      选择 assistant 而非倒数第 2 条，是因为工具轮次中 messages[-2] 可能是 tool 结果
      而非稳定前缀（多 tool 结果会让断点每轮移动，二级缓存几乎永远 miss）。
      最后一条 assistant 消息是上一轮的回复，在当前轮 tool 调用期间保持稳定，
      provider 可以缓存到该位置，下一轮 tool 结果追加时前缀稳定命中。
    - 尾部 user 消息（Qwen 分支）：把本轮新增内容当场写入缓存，省掉一个轮次的
      miss 窗口。详见下方第 3 步注释。

    Anthropic / DashScope 限制：单次请求最多 4 个 cache_control 标记。
    本策略最多用 3 个（system + 上一条 assistant + 尾部 user），留 1 个余量。

    对于 DashScope / Qwen 兼容模式：格式为 content 文本块内的 cache_control: {"type": "ephemeral"}；
    对于 Anthropic 协议：格式为消息顶层 cache_control: {"type": "ephemeral"}，由 native 的
    Messages 协议适配层翻译。

    本函数原地修改并返回 messages；调用方已通过 _strip_media_and_merge 处理过副本，
    不会污染 messages 列表本身。
    """
    if disabled:
        return messages
    if not _supports_cache_control(model_name, api_base, model_cfg):
        return messages
    if not messages:
        return messages

    is_qwen = _is_qwen_family(model_name, api_base)

    # 清除已有标记，避免累积（_strip_media_and_merge 不传 cache_control，但兜底防御）
    for msg in messages:
        if isinstance(msg, dict):
            msg.pop("cache_control", None)
            content = msg.get("content")
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict):
                        item.pop("cache_control", None)

    def _mark_message(msg):
        """打缓存断点；返回是否实际标上（Qwen 空文本 content 打不上块标记）。"""
        if is_qwen:
            content = msg.get("content")
            if isinstance(content, str) and content:
                msg["content"] = [
                    {
                        "type": "text",
                        "text": content,
                        "cache_control": {"type": "ephemeral"},
                    }
                ]
                return True
            if isinstance(content, list) and content:
                for item in reversed(content):
                    if isinstance(item, dict) and item.get("type") in ("text", "input_text"):
                        item["cache_control"] = {"type": "ephemeral"}
                        return True
                return False
            return False
        msg["cache_control"] = {"type": "ephemeral"}
        return True

    # 1. 标记 system 消息（如果有）
    if messages[0].get("role") == "system":
        _mark_message(messages[0])

    # 2. 标记最后一条 assistant 消息（缓存边界）
    #    遍历从后往前找第一条 role=="assistant" 的消息。
    #    如果最后一条已经是 assistant（无 tool 调用），就标它；
    #    如果末尾是 tool 结果，就标它们之前的 assistant（更稳定）。
    #    Qwen 空文本 assistant（纯工具调用轮的典型形态）打不上 content 块
    #    断点，且 tool 消息受协议约束不可标记：继续向前找最近一条可标记的
    #    assistant。否则工具循环内的各 Step 只剩 system 断点，循环新增内容
    #    （用户问题 + 全部工具结果）每步全额 miss；把边界推进到循环前最后
    #    一条文本 assistant，至少让前缀覆盖到循环起点，且与上一轮请求字节
    #    （该 assistant 当时已带标记）保持一致。
    last_assistant_idx = None
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "assistant":
            last_assistant_idx = i
            if _mark_message(messages[i]):
                break

    # 3. 尾部断点（仅 Qwen content 块形态）：把本轮新增内容立刻写进缓存，
    #    下一轮的前缀就能覆盖到它——稳态 miss 从「上一轮 + 本轮新增」降到
    #    「仅本轮新增」。基元律动实测：热轮 miss 105 → 52 token，hot_rate +0.43pp。
    #    约束：
    #    - 只标 user 尾部。tool 消息在 OpenAI 兼容协议里 content 必须是字符串，
    #      改成 content 块会被上游判非法载荷；工具轮次里新增的 tool 结果本来就在
    #      下一个 assistant 断点之前，会随断点前移被写入缓存，无需额外标记。
    #    - 尾部已是刚标过的 assistant 时不重复标记（省断点配额）。
    #    - 与 system、上一条 assistant 合计 3 个，仍低于 provider 的 4 个上限。
    if is_qwen and len(messages) - 1 != last_assistant_idx:
        tail = messages[-1]
        if isinstance(tail, dict) and tail.get("role") == "user":
            _mark_message(tail)

    return messages


# _supports_vision 结果缓存（30s TTL）：该函数在工具循环里每轮都会被调用
# （最多 100 次/用户轮），config 扫描与缓存判读不该每轮重做。
_vision_support_cache = {}
_vision_support_cache_lock = threading.RLock()


def _supports_vision(model_name):
    """判断模型是否支持图像输入。

    判定顺序：
    1. 模型配置中的 supports_vision 显式标注（config.json 的 models 条目，可选）
    2. 未标注的模型默认按不支持处理，避免把 image_url 负载发给纯文本模型触发
       "Model only support text input" 等 400 错误
    """
    if not model_name:
        return False
    with _vision_support_cache_lock:
        return _supports_vision_locked(model_name)


def _supports_vision_llm_first(model_name):
    """llm-first 解析视觉能力判定。

    core.llm 保留兼容入口，测试/外部调用方 patch core.llm._supports_vision
    必须生效；解析失败回落本模块实现。
    """
    try:
        import core.llm as _llm
        fn = getattr(_llm, "_supports_vision", None)
        if fn is not None:
            return fn(model_name)
    except Exception:
        pass
    return _supports_vision(model_name)


def _supports_vision_locked(model_name):
    cached = _vision_support_cache.get(model_name)
    if cached is not None and cached[1] > time.monotonic():
        return cached[0]
    result = False
    try:
        from core.model_manager import load_config
        for provider in load_config().get("providers", []):
            for model in provider.get("models", []):
                if model.get("litellm_model") == model_name and "supports_vision" in model:
                    result = bool(model["supports_vision"])
                    _vision_support_cache[model_name] = (result, time.monotonic() + 30)
                    return result
    except Exception:
        pass
    _vision_support_cache[model_name] = (result, time.monotonic() + 30)
    return result


def _flatten_media_content(content_list):
    """把多模态 list 内容折叠为纯文本：图片用短占位符，文档也折叠为占位说明，绝不内联原始 base64 数据。"""
    text_parts = []
    for item in content_list:
        if isinstance(item, dict):
            item_type = item.get("type")
            if item_type == "text":
                text_parts.append(item.get("text", ""))
            elif item_type in ("image", "image_url"):
                text_parts.append("[图片]")
            elif item_type == "document":
                text_parts.append("[文档]")
            else:
                text_parts.append(str(item)[:200])
        else:
            text_parts.append(str(item))
    return " ".join(text_parts)


def _requires_single_leading_system(model_name, api_base=""):
    """neohorse 系列（tokenrhythm 网关）要求 system 消息有且仅有一条、且位于开头：
    多条或对话中段出现 system 一律 400（"System message must be at the beginning."）。
    KAIROS 记忆注入会在最后一条 user 前补第二条 system，对此类路由必须在上线路径合并。
    """
    return "neohorse" in str(model_name or "").lower()


def _merge_system_messages_into_leading(messages):
    """把所有 system 消息按原顺序合并为开头单条 system，其余消息相对顺序不变。"""
    systems = [m for m in messages if m.get("role") == "system"]
    if len(systems) <= 1:
        return messages
    merged_content = "\n\n".join(str(m.get("content") or "") for m in systems)
    rest = [m for m in messages if m.get("role") != "system"]
    return [{"role": "system", "content": merged_content}] + rest


def _strip_media_and_merge(messages, model_name=None, api_base=""):
    """
    1. 视觉输入处理：支持视觉的模型保留最近一条消息中的图片/视频（多模态负载原样透传），
       更早的历史图片一律折叠为占位符，避免每轮重复发送烧 Token；
       不支持视觉的模型把图片折叠为占位符（绝不把 base64 当文本发给模型）。
       文档/文本文件已在 input_parser 中转为普通文本，此处不视为视觉输入。
    2. 合并连续相同 role 的消息，满足 Anthropic 协议对 user/assistant 严格轮流的要求
    3. reasoning_content / thinking_blocks 仅对支持的 provider 完整回传，避免其他
       provider 报 400，同时保持 Kimi 等模型要求的连续 provider 消息信封

    返回 (fixed_messages, folded_media_count, keep_media)。
    """
    keep_reasoning_fields = _supports_reasoning_fields(model_name, api_base)
    keep_thinking_blocks = str(model_name or "").split("/")[0].lower() == "anthropic"
    keep_media = _supports_vision_llm_first(model_name)

    # 仅保留最近一条用户消息中的视觉输入（图片/视频），
    # 更早的历史图片全部折叠为占位符，避免重复发送、浪费 Token。
    last_media_idx = -1
    if keep_media:
        for idx in range(len(messages) - 1, -1, -1):
            msg = messages[idx]
            if msg.get("role") != "user":
                continue
            content = msg.get("content")
            if isinstance(content, list) and any(
                isinstance(item, dict) and item.get("type") in ("image", "image_url")
                for item in content
            ):
                last_media_idx = idx
            break

    folded_count = 0
    cleaned = []
    for idx, msg in enumerate(messages):
        role = msg.get("role")
        content = msg.get("content")

        if isinstance(content, list):
            if idx == last_media_idx:
                # 多模态负载保留，模型按 image_url 接收图片；
                # 剥离本地私有的 source_path 字段，避免严格校验的 provider 报 400
                cleaned_msg = {"role": role, "content": [
                    {k: v for k, v in item.items() if k != "source_path"}
                    if isinstance(item, dict) else item
                    for item in content
                ]}
            else:
                cleaned_msg = {"role": role, "content": _flatten_media_content(content)}
                folded_count += sum(
                    1
                    for item in content
                    if isinstance(item, dict) and item.get("type") in ("image", "image_url")
                )
        else:
            cleaned_msg = {"role": role, "content": str(content) if content is not None else ""}

        # 保留工具链和 provider 原生推理字段，避免工具轮次破坏签名思考块。
        if "tool_calls" in msg and msg["tool_calls"]:
            cleaned_msg["tool_calls"] = msg["tool_calls"]
        if "tool_call_id" in msg:
            cleaned_msg["tool_call_id"] = msg["tool_call_id"]
        if "name" in msg:
            cleaned_msg["name"] = msg["name"]
        # 支持原生推理字段的 provider 需要收到自己上轮返回的完整消息信封。
        # 是否存在 tool_calls 不能作为删除条件：Kimi OpenAI 路由会在普通 assistant
        # 消息中返回 reasoning_content，Anthropic 路由还可能返回签名 thinking_blocks。
        if keep_reasoning_fields and msg.get("reasoning_content") is not None:
            cleaned_msg["reasoning_content"] = msg["reasoning_content"]
        if keep_reasoning_fields and keep_thinking_blocks and msg.get("thinking_blocks") is not None:
            cleaned_msg["thinking_blocks"] = deepcopy(msg["thinking_blocks"])
        # DeepSeek thinking 契约：带 tool_calls 的 assistant 必须携带
        # reasoning_content 字符串字段。流组装层正常会保留显式空串；但
        # 从数据库重建或中途切换思考开关的历史消息可能缺失该字段（None
        # 或非字符串），这里回填空串占位，避免整轮请求被契约校验卡死。
        if (
            role == "assistant"
            and cleaned_msg.get("tool_calls")
            and _requires_tool_call_reasoning(model_name, api_base)
            and not isinstance(cleaned_msg.get("reasoning_content"), str)
        ):
            cleaned_msg["reasoning_content"] = ""

        # 净化 assistant 消息：若 content 包含 protocol sanitised 占位符且有 tool_calls/reasoning，将 content 重置为空字符串
        if role == "assistant" and isinstance(cleaned_msg.get("content"), str):
            if "[System: Empty message content sanitised to satisfy protocol]" in cleaned_msg["content"]:
                cleaned_msg["content"] = ""

        # 过滤完全没有内容的空白 assistant 消息（无 content, 无 tool_calls, 无 reasoning）
        if role == "assistant":
            has_text = bool(cleaned_msg.get("content"))
            has_tools = bool(cleaned_msg.get("tool_calls"))
            has_reasoning = bool(cleaned_msg.get("reasoning_content") or cleaned_msg.get("thinking_blocks"))
            if not (has_text or has_tools or has_reasoning):
                continue

        cleaned.append(cleaned_msg)
        
    # 合并同角色消息 (注意：不要合并有 tool_calls 或 role 是 tool/system 的消息)
    fixed = []
    if cleaned:
        fixed.append(cleaned[0])
        for msg in cleaned[1:]:
            can_merge = (
                msg["role"] == fixed[-1]["role"] and
                msg["role"] not in ("tool", "system") and
                "tool_calls" not in msg and
                "tool_calls" not in fixed[-1] and
                "reasoning_content" not in msg and
                "reasoning_content" not in fixed[-1] and
                "thinking_blocks" not in msg and
                "thinking_blocks" not in fixed[-1] and
                # 多模态 list 内容不参与文本合并
                not isinstance(msg["content"], list) and
                not isinstance(fixed[-1]["content"], list)
            )
            if can_merge:
                fixed[-1]["content"] = (fixed[-1]["content"] or "") + "\n" + (msg["content"] or "")
            else:
                fixed.append(msg)
    if _requires_single_leading_system(model_name, api_base):
        fixed = _merge_system_messages_into_leading(fixed)
    return fixed, folded_count, keep_media


def _resolve_cache_session_identity(cache_session_id=None):
    """解析请求所属会话；后台任务可用捕获值覆盖当前 UI 会话。"""
    global _process_cache_key_fallback
    sid = str(cache_session_id or "").strip()
    if not sid:
        try:
            sid = str(app_context.get_current_session() or "").strip()
        except Exception:
            sid = ""
    if not sid:
        if not _process_cache_key_fallback:
            _process_cache_key_fallback = uuid.uuid4().hex
        sid = _process_cache_key_fallback
    return sid


def _cache_affinity_session_id(
    *,
    cache_scope="main",
    model_name="",
    api_base="",
    tool_contract="",
    cache_session_id=None,
):
    """返回 Kimi prompt 缓存亲和使用的会话级隔离 ID。

    Moonshot 后端是多集群架构，请求带 prompt_cache_key（OpenAI 路由）或
    metadata.user_id（Anthropic 路由）时，网关会把同 ID 的请求优先路由到
    持有其前缀 KV cache 的集群，显著提高缓存命中率（命中部分自动折扣计费）。
    官方建议该值在会话生命周期内保持稳定，会话恢复后沿用原值——正好对应
    app_context 的 current_session；后台任务可显式传入启动时捕获的父会话，避免 UI
    切换会话后串线。原始会话名只参与 SHA-256，不发送给 provider。

    通道维度包含角色、模型、API 路由与工具契约。这样主代理、不同子代理角色、
    不同工具 schema 不会共用同一个 prompt_cache_key。
    """
    sid = _resolve_cache_session_identity(cache_session_id)

    session_hash = hashlib.sha256(sid.encode("utf-8")).hexdigest()[:12]
    lane_material = json.dumps(
        {
            "scope": str(cache_scope or "main"),
            "model": str(model_name or "").lower(),
            "api_base": str(api_base or "").rstrip("/").lower(),
            "tool_contract": str(tool_contract or ""),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    lane_hash = hashlib.sha256(lane_material.encode("utf-8")).hexdigest()[:16]
    return f"ycode-{session_hash}-{lane_hash}"


_process_cache_key_fallback = None

_REASONING_EFFORT_LEVELS = ("low", "high", "max")
# Kimi Code / Moonshot Anthropic 兼容端点：档位翻译为原生 thinking.budget_tokens；
# kimi-for-coding（K2.8 Preview）与 K3 同为 low/high/max 三档。
_K3_REASONING_MODEL_NAMES = frozenset(
    {"k3", "k3-256k", "kimi-k3", "kimi-k3-256k", "kimi-for-coding"}
)
# 阶跃星辰 Anthropic 兼容端点（/step_plan/v1/messages）不接受 Anthropic 原生
# thinking/budget_tokens，推理深度由官方扩展字段 output_config.effort 控制
# （token plan 全系 LLM 均为 low/medium/high，见官方 /v1/models 的
# reasoning_effort_support_list）。自研引擎原样透传该字段。
_STEPFUN_ANTHROPIC_EFFORT_MODELS = frozenset(
    {
        "step-3.7-flash",
        "step-router-v1",
        "step-5-preview",
        "step-3.5-flash",
        "step-3.5-flash-2603",
    }
)
_ANTHROPIC_REASONING_BUDGETS = {
    "low": 1024,
    "high": 4096,
    "max": 16384,
}


def _attach_kimi_request_extras(
    result,
    model_name,
    model_cfg,
    *,
    api_base="",
    cache_scope="main",
    cache_session_id=None,
    tool_contract="",
):
    """给 Kimi 系模型的请求参数附加缓存亲和配置（原地修改 result）。

    - 缓存亲和：OpenAI 兼容路由传 prompt_cache_key，Anthropic 路由传 metadata.user_id；
      判定只看模型家族，任意渠道都发送（被上游 400 拒绝的路由由
      _disable_affinity_route 临时拉黑兜底）。
    """
    anthropic_route = str(model_name or "").split("/")[0].lower() == "anthropic"

    session_id = None
    if _is_kimi_family(model_name, api_base) and not _affinity_route_disabled(
        model_name, api_base
    ):
        session_id = _cache_affinity_session_id(
            cache_scope=cache_scope,
            model_name=model_name,
            api_base=api_base,
            tool_contract=tool_contract,
            cache_session_id=cache_session_id,
        )
    if session_id:
        if anthropic_route:
            result.setdefault("metadata", {"user_id": session_id})
        else:
            result.setdefault("extra_body", {})["prompt_cache_key"] = session_id


def _attach_qwen_request_extras(
    result,
    model_name,
    model_cfg,
    *,
    api_base="",
    cache_scope="main",
    cache_session_id=None,
    tool_contract="",
):
    """给 Qwen 系模型的请求参数附加缓存优化配置（原地修改 result）。

    - 官方路由（dashscope.aliyuncs.com 等）或配置显式开启缓存：
      主会话、基准测试及子代理请求（main, benchmark:main, subagent:*）
      附加 extra_headers={"x-dashscope-session-cache": "enable"}
      开启服务端多轮会话自动上下文缓存。
    """
    scope_str = str(cache_scope or "main")
    scope_eligible = scope_str in {"main", "benchmark:main"} or scope_str.startswith("subagent")
    if scope_eligible and _qwen_cache_enabled(model_name, api_base, model_cfg):
        if not isinstance(result.get("extra_headers"), dict):
            result["extra_headers"] = {}
        result["extra_headers"]["x-dashscope-session-cache"] = "enable"


def _configured_reasoning_choice(model_cfg, capability):
    """把新选择字段和历史配置统一成能力表中的一个合法选择。"""
    configured_choice = str(model_cfg.get("reasoning_choice") or "").strip().lower()
    if configured_choice:
        if configured_choice not in capability.options:
            raise ValueError(
                f"模型不支持思考选项 {configured_choice!r}，"
                f"仅支持 {', '.join(capability.options) or '固定开启'}"
            )
        return configured_choice

    legacy_enabled = model_cfg.get("enable_thinking")
    if legacy_enabled is False and "off" in capability.options:
        return "off"

    legacy_effort = str(model_cfg.get("reasoning_effort") or "").strip().lower()
    if legacy_effort:
        if legacy_effort not in capability.options:
            raise ValueError(
                f"模型 reasoning_effort={legacy_effort!r} 不受支持，"
                f"仅支持 {', '.join(capability.options) or '固定开启'}"
            )
        return legacy_effort

    if legacy_enabled is True and "on" in capability.options:
        return "on"
    return "default"


def _apply_reasoning_choice(result, model_name, model_cfg, *, anthropic_route=False):
    """按模型能力表把思考选择编码为该模型的精确请求参数。"""
    from core.reasoning_capabilities import (
        get_reasoning_capability,
        normalize_reasoning_model_name,
    )

    capability = get_reasoning_capability(model_name)
    has_explicit_choice = bool(str(model_cfg.get("reasoning_choice") or "").strip())
    has_legacy_choice = (
        bool(str(model_cfg.get("reasoning_effort") or "").strip())
        or isinstance(model_cfg.get("enable_thinking"), bool)
    )
    if not has_explicit_choice and not has_legacy_choice:
        return capability

    if capability.kind == "hidden":
        # 未登记能力表的模型（厂商改名、新增未适配模型）：一律静默回落
        # 默认档，不注入任何思考参数，也不因残留的历史思考选择打断请求。
        return capability
    if capability.kind == "fixed":
        if has_explicit_choice:
            raise ValueError(f"模型 {model_name!r} 的思考固定开启，不能设置思考选项")
        logger.info("模型 %s 的思考固定开启，忽略历史思考配置", model_name)
        return capability

    choice = _configured_reasoning_choice(model_cfg, capability)
    if choice == "default":
        return capability

    strategy = capability.request_strategy
    if strategy == "always_on_effort":
        if anthropic_route:
            normalized_model = normalize_reasoning_model_name(model_name)
            if normalized_model in _STEPFUN_ANTHROPIC_EFFORT_MODELS:
                # 阶跃 Anthropic 兼容路由：思考深度用官方 output_config.effort 表达，
                # 不接受 K3 那套 thinking.budget_tokens（服务端按未列字段拒绝）。
                result["output_config"] = {"effort": choice}
                result["allowed_openai_params"] = ["output_config"]
                return capability
            if normalized_model not in _K3_REASONING_MODEL_NAMES:
                raise ValueError("Anthropic 路由不支持该模型的 reasoning_effort 参数")
            budget_tokens = _ANTHROPIC_REASONING_BUDGETS[choice]
            result["thinking"] = {
                "type": "enabled",
                "budget_tokens": budget_tokens,
            }
            result["max_tokens"] = max(
                int(result.get("max_tokens") or 0), budget_tokens + 1
            )
        else:
            result.setdefault("extra_body", {})["reasoning_effort"] = choice
    elif strategy == "thinking_object_effort":
        enabled = choice != "off"
        result.setdefault("extra_body", {})["thinking"] = {
            "type": "enabled" if enabled else "disabled"
        }
        if enabled:
            result["reasoning_effort"] = choice
            result["allowed_openai_params"] = ["reasoning_effort"]
        else:
            result.pop("reasoning_effort", None)
    elif strategy == "enable_thinking_effort":
        enabled = choice != "off"
        extra_body = result.setdefault("extra_body", {})
        extra_body["enable_thinking"] = enabled
        if enabled:
            extra_body["reasoning_effort"] = choice
        else:
            extra_body.pop("reasoning_effort", None)
    elif strategy == "effort_switch":
        result["reasoning_effort"] = "none" if choice == "off" else choice
        result["allowed_openai_params"] = ["reasoning_effort"]
    elif strategy == "thinking_object_toggle":
        result.setdefault("extra_body", {})["thinking"] = {
            "type": "enabled" if choice == "on" else "disabled"
        }
    elif strategy == "enable_thinking_toggle":
        result.setdefault("extra_body", {})["enable_thinking"] = choice == "on"
    elif strategy == "agnes_enable_thinking":
        # Agnes AI 的 OpenAI 兼容接口把思考开关嵌套在 chat_template_kwargs 下。
        result.setdefault("extra_body", {}).setdefault(
            "chat_template_kwargs", {}
        )["enable_thinking"] = choice == "on"
    else:
        raise ValueError(f"未实现的思考请求策略: {strategy}")
    return capability


def _assert_reasoning_request_consistent(result, capability):
    """禁止把思考强度单独发送给需要显式开启思考的模型。"""
    extra_body = result.get("extra_body") or {}
    top_level_thinking_enabled = (
        (result.get("thinking") or {}).get("type") == "enabled"
    )
    extra_thinking_disabled = (
        (extra_body.get("thinking") or {}).get("type") == "disabled"
        or extra_body.get("enable_thinking") is False
    )
    if top_level_thinking_enabled and extra_thinking_disabled:
        raise ValueError("请求同时包含开启和关闭两套冲突的思考参数")
    has_effort = "reasoning_effort" in result or "reasoning_effort" in extra_body
    if not has_effort:
        return

    strategy = capability.request_strategy
    if strategy in {"always_on_effort", "effort_switch"}:
        return
    if strategy == "thinking_object_effort":
        enabled = (extra_body.get("thinking") or {}).get("type") == "enabled"
    elif strategy == "enable_thinking_effort":
        enabled = extra_body.get("enable_thinking") is True
    else:
        enabled = False
    if not enabled:
        raise ValueError("请求包含思考强度，但未开启思考")


def _apply_route_request_overrides(result, model_name, api_base):
    """合并渠道请求形态覆盖；无配置时原样返回（零影响）。

    必须在 _assert_reasoning_request_consistent 之后调用：覆盖值不参与一致性
    校验，配置错误才不会抛错阻断对话（§4.4 / §4.7 的硬约束）。
    """
    from core.route_overrides import resolve_route_override  # 局部导入，避免模块循环依赖

    override = resolve_route_override(model_name, api_base)
    headers = override.get("headers") or {}
    body = override.get("body") or {}
    if headers:
        merged = dict(result.get("extra_headers") or {})
        merged.update(headers)
        result["extra_headers"] = merged
    if body:
        merged = dict(result.get("extra_body") or {})
        merged.update(body)
        result["extra_body"] = merged
    return result


def _provider_cache_affinity_options(
    model_name,
    api_base,
    *,
    cache_scope,
    cache_session_id=None,
    tool_contract="",
):
    """仅返回 provider 缓存亲和参数，不改变输出长度或推理配置。"""
    result = {}
    if _is_kimi_family(model_name, api_base):
        _attach_kimi_request_extras(
            result,
            model_name,
            {},
            api_base=api_base,
            cache_scope=cache_scope,
            cache_session_id=cache_session_id,
            tool_contract=tool_contract,
        )
    elif _is_qwen_family(model_name, api_base):
        _attach_qwen_request_extras(
            result,
            model_name,
            {},
            api_base=api_base,
            cache_scope=cache_scope,
            cache_session_id=cache_session_id,
            tool_contract=tool_contract,
        )
    return result


_BUILTIN_DEFAULT_MAX_TOKENS = 32000


def _builtin_default_max_tokens(model_name, api_base=""):
    """返回内置默认输出上限（config.json 未显式配置 max_tokens 时使用）。

    规则按序匹配，先命中先返回；仅看模型名与路由，不读取用户配置。
    """
    model = str(model_name or "").split("/")[-1].lower()
    base = str(api_base or "").lower()
    if "kimi-k2.7-code" in model and "tokenrhythm.studio" in base:
        return 16000
    if model.startswith("deepseek") or _is_deepseek_family(model_name, api_base):
        # DeepSeek 官方最大输出 384K；不限定官方路由，任意厂商挂载的
        # deepseek 前缀模型（不区分大小写）统一按此给足输出预算。
        return 384000
    if model.startswith("agnes"):
        # Agnes AI 前缀模型（含未来新版本）统一默认 65535 输出预算；
        # config.json 显式配置的 max_tokens 仍优先生效。
        return 65535
    if _is_glm_family(model_name, api_base):
        if "glm-5.3" in model:
            return 131000
        if "glm-5" in model:
            return 128000
        return _BUILTIN_DEFAULT_MAX_TOKENS
    if _is_qwen_family(model_name, api_base):
        return 131000
    if "kimi-k2.5" in model:
        return 64000
    if "kimi-k2.6" in model:
        return 128000
    if "kimi-k2.7-code" in model:
        return 128000
    if "k3" in model:
        return 131072
    if "longcat" in model:
        return 128000
    if "seed-2.1-pro" in model or "seed-2.1-turbo" in model:
        return 131000
    if "mimo-v2.6" in model or model.startswith("mimo-v2.5"):
        # 上游网关实测上限 131072（128K），超限直接 400；与 model_spec_table 官方规格一致。
        return 131072
    if "minimax-m2.5" in model:
        return 200000
    if "minimax-m2.7" in model:
        return 192000
    return _BUILTIN_DEFAULT_MAX_TOKENS


def _model_system_prompt_extra(model_name, api_base="", model_id=None) -> str:
    """读取模型条目上的可选字段 system_prompt_extra（模型级系统提示扩展，B 方案）。

    只有显式配置了该字段的模型才会拿到非空返回；未配置的模型一律返回空串，
    调用方据此完全跳过注入，请求字节与既有行为逐字节一致。
    任何异常（配置损坏、条目缺失等）都静默降级为空串，绝不让扩展字段影响主链路。
    注意：该字段只用于请求时追加独立 system 消息，不参与稳定前缀构建，
    build_stable_system_message 的结果与字节金样保持完全不变。
    """
    try:
        from core.model_manager import (
            get_model_config_by_id,
            get_model_config_by_litellm_model,
        )

        config_entry = None
        if model_id:
            config_entry = get_model_config_by_id(str(model_id))
        if not isinstance(config_entry, dict):
            config_entry = get_model_config_by_litellm_model(model_name, api_base)
        if not isinstance(config_entry, dict):
            return ""
        extra = config_entry.get("system_prompt_extra")
        if not isinstance(extra, str):
            return ""
        # 全空白视为未配置，避免给请求塞入一条纯空白的 system 消息
        return extra if extra.strip() else ""
    except Exception:
        return ""


def _provider_request_options(
    model_name,
    api_base,
    *,
    model_id=None,
    cache_scope="main",
    cache_session_id=None,
    tool_contract="",
):
    """Return options required by a known provider/model pair.

    优先级：
    1. config.json 中模型条目的显式 thinking_budget / max_tokens / enable_thinking
    2. 内置模型默认输出上限表
    3. 其他模型兜底 max_tokens=32000

    无论走哪个分支，Kimi 系模型都会附加缓存亲和配置。思考参数随后按模型能力表编码。
    """
    normalized_base = str(api_base or "").rstrip("/").lower()
    normalized_model = str(model_name or "").split("/")[-1].lower()

    # 1. 读取用户在 config.json 中的显式配置
    try:
        from core.model_manager import (
            get_model_config_by_id,
            get_model_config_by_litellm_model,
        )
        model_cfg = (
            get_model_config_by_id(model_id)
            if model_id
            else get_model_config_by_litellm_model(model_name, api_base)
        ) or {}
    except Exception:
        if model_id:
            raise
        model_cfg = {}
    if model_id and not model_cfg:
        raise ValueError(f"未找到模型配置: {model_id}")

    cfg_max_tokens = model_cfg.get("max_tokens")
    cfg_thinking_budget = model_cfg.get("thinking_budget")
    cfg_enable_thinking = model_cfg.get("enable_thinking")
    has_reasoning_choice = bool(str(model_cfg.get("reasoning_choice") or "").strip())
    has_legacy_reasoning_choice = (
        isinstance(cfg_enable_thinking, bool)
        or bool(str(model_cfg.get("reasoning_effort") or "").strip())
    )
    from core.reasoning_capabilities import get_reasoning_capability
    configured_capability = get_reasoning_capability(model_name)
    has_registered_reasoning = configured_capability.kind != "hidden"

    # 判定同时看模型名与 api_base（经销商/聚合网关的 base_url 千奇百怪，
    # 只认 base 会漏判，比如 api.scnet.cn 上的 Kimi-K2.6）。
    kimi_family = _is_kimi_family(model_name, api_base)
    deepseek_family = _is_deepseek_family(model_name, api_base)
    deepseek_thinking = _deepseek_thinking_enabled(
        model_name, api_base, model_cfg
    )
    route_prefix = str(model_name or "").split("/")[0].lower()
    anthropic_route = route_prefix == "anthropic"

    result = {}

    if cfg_max_tokens is not None:
        result["max_tokens"] = int(cfg_max_tokens)
    else:
        result["max_tokens"] = _builtin_default_max_tokens(model_name, api_base)

    if deepseek_family:
        if not has_reasoning_choice and (
            not has_registered_reasoning or not has_legacy_reasoning_choice
        ):
            if _is_official_deepseek_route(api_base) or isinstance(cfg_enable_thinking, bool):
                result.setdefault("extra_body", {})["thinking"] = {
                    "type": "enabled" if deepseek_thinking else "disabled"
                }
            if deepseek_thinking:
                effort = str(model_cfg.get("reasoning_effort") or "max").strip().lower()
                if effort not in _REASONING_EFFORT_LEVELS:
                    raise ValueError(
                        "DeepSeek reasoning_effort 仅支持 low/high/max，"
                        f"实际为 {effort!r}"
                    )
                result["reasoning_effort"] = effort
                # reasoning_effort 属于非标准参数：以白名单随请求显式声明，
                # 保证支持该参数的网关放行（native 全量透传，不做客户端裁剪）。
                result["allowed_openai_params"] = ["reasoning_effort"]
    elif not has_reasoning_choice and not has_registered_reasoning and (
        cfg_max_tokens is not None
        or cfg_thinking_budget is not None
        or (cfg_enable_thinking is not None and not has_reasoning_choice)
    ):
        # thinking 仅在显式启用或配置了 budget 时注入
        if cfg_enable_thinking is True or cfg_thinking_budget is not None:
            budget = int(cfg_thinking_budget) if cfg_thinking_budget is not None else 2048
            result["thinking"] = {"type": "enabled", "budget_tokens": budget}
            result["allowed_openai_params"] = ["thinking"]
    elif kimi_family:
        # 2. 内置默认表：Kimi/Moonshot 深度适配。
        is_kimi_coding = "api.kimi.com/coding" in normalized_base

        def _match_any(keys):
            return any(k in normalized_model for k in keys)

        is_k3 = _match_any(("k3",)) or "for-coding" in normalized_model

        if anthropic_route:
            # Anthropic 协议路由（api.kimi.com/coding 等 anthropic 兼容端点）：
            # 接受 Anthropic 形状的 thinking 参数。
            # 必须同时声明 allowed_openai_params：thinking 对 OpenAI 兼容语义是
            # 非标准参数，显式声明以保证网关按白名单放行（native 全量透传，
            # 不做客户端裁剪）。
            if is_k3:
                result.setdefault("thinking", {
                    "type": "enabled", "budget_tokens": 4096 if is_kimi_coding else 2048,
                })
            else:
                result.setdefault("thinking", {"type": "enabled", "budget_tokens": 2048})
            result.setdefault("allowed_openai_params", ["thinking"])
        # OpenAI 兼容路由（api.moonshot.cn/v1 及各类经销商）：绝不发送 Anthropic
        # 形状的 thinking 参数——Moonshot 的 /v1/chat/completions 不认这个键，
        # 发了会直接 400。Kimi 推理模型（k2.5/k2.6/k2.7/k3 等）在该路由上会原生
        # 输出 reasoning_content，无需显式开启；输出上限由内置默认表统一给出。

    if kimi_family:
        _attach_kimi_request_extras(
            result,
            model_name,
            model_cfg,
            api_base=api_base,
            cache_scope=cache_scope,
            cache_session_id=cache_session_id,
            tool_contract=tool_contract,
        )
    if _is_qwen_family(model_name, api_base):
        _attach_qwen_request_extras(
            result,
            model_name,
            model_cfg,
            api_base=api_base,
            cache_scope=cache_scope,
            cache_session_id=cache_session_id,
            tool_contract=tool_contract,
        )
    capability = _apply_reasoning_choice(
        result,
        model_name,
        model_cfg,
        anthropic_route=anthropic_route,
    )
    _assert_reasoning_request_consistent(result, capability)
    return _apply_route_request_overrides(result, model_name, api_base)

