"""router 场景：**回合级模型档位**覆盖（一次性，不写任何全局/会话状态）。

设计依据：`docs/superpowers/specs/2026-10-18-jev-decision-integration.md`（router 行）。
接线方案：**方案 A —— 入口层局部变量替换 + 显式参数透传**。

职责边界（很重要，改动前先读）：

- 本模块只回答一个问题：「这一回合该用主模型还是副模型」；
- 结论是一份**参数包**（litellm_model / api_key / base_url / context_window /
  model_id / name），由入口层（gui_bridge._run_turn_worker、main.py 的对话
  worker）用局部变量替换的方式，把这份参数显式透传给本回合的
  ``prepare_turn_messages`` 与 ``chat_with_model_events``；
- 这就是一次**本回合的一次性覆盖**：不调用 ``get_active_model`` /
  ``set_active_model_tuple``，不写任何会话级模型绑定，不落盘、不动配置。
  并行会话与子代理继承链必须继续看到各自的会话模型——把覆盖写进全局/会话
  状态会让两个并行会话互相串味（硬故障，禁止）；
- ``model_id`` 必须和 ``litellm_model`` **成对替换**：``core/llm_dialects.py``
  按模型 id 读取 thinking / max_tokens 等模型级配置，只换名字不换 id，会拿主模型
  的推理参数去发副模型的请求（可能直接被上游 400）；
- 任何异常一律吞掉返回 ``None``（=不覆盖，沿用会话当前模型）：决策链路永远
  不能成为主流程的故障点；
- 隐私：发给决策模型的 state **只放本轮用户输入本身**（截断 500 字符），绝不
  拼接文件内容、历史消息、工具结果或密钥。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# 场景名（与 core.decision_gateway.SCENES / decision_service.DEFAULT_CONFIG 对齐）
SCENE = "router"

# choice 题的两个候选档位
MAIN_OPTION = "main"
SECONDARY_OPTION = "secondary"
ROUTER_OPTIONS = (MAIN_OPTION, SECONDARY_OPTION)

# 单次决策超时：真实决策宁可不用，也不能拖慢主流程
ROUTER_TIMEOUT_MS = 250

# state 裁剪上限：只把「这段输入有多复杂」告诉决策模型，长粘贴没有额外信息
STATE_CHAR_LIMIT = 500

# router 场景的决策题（形状与 search 场景保持一致）
ROUTER_QUESTION = {
    "type": "choice",
    "instructions": (
        "判断这一回合该用哪个模型：main=能力更强但更贵，适合复杂推理/大范围改代码/"
        "多文件任务；secondary=更便宜，适合简单问答、单点修改、格式转换、闲聊。"
    ),
    "criteria": [MAIN_OPTION, SECONDARY_OPTION],
}


# ---------------------------------------------------------------------------
# 小工具（保持纯函数：便于测试与复用）
# ---------------------------------------------------------------------------

def _state_text(text) -> str:
    """把本轮用户输入裁成决策用 state：只保留用户输入的 500 字符以内。

    - 非字符串（图片块等结构化内容）一律视为「没有可判断的文本」→ 空串；
    - 折叠连续空白：多行粘贴的缩进/换行对「复杂度判断」没有信息量，只占字节。
    """
    if not isinstance(text, str):
        return ""
    collapsed = " ".join(text.split())
    # 用户粘贴的代码/命令里可能带密钥，发给决策模型前先过一遍脱敏
    from core.decision_gateway import redact_sensitive

    return redact_sensitive(collapsed[:STATE_CHAR_LIMIT])


def _positive_int(value) -> int:
    """转正整数；非法（含布尔）返回 0。"""
    if isinstance(value, bool):
        return 0
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


# ---------------------------------------------------------------------------
# 廉价前置判断（供入口层在「算 token 基线」之前调用）
# ---------------------------------------------------------------------------

def router_scene_live() -> bool:
    """router 场景当前是否处于 live（真采纳）状态。

    只看配置，**零网络请求、零审计副作用**。入口层先用它做一次廉价闸门：
    JEV 关闭 / 影子时直接跳过整套覆盖逻辑（不读历史、不算 token、不问决策），
    从而保证「JEV 关闭时与接入前完全等价」，不引入任何额外开销。

    判定口径与 ``core.decision_gateway.decide`` 的 shadow 计算保持一致：
    全局 ``shadow_mode`` 或场景 ``shadow``（默认 True）任一为真，就不是 live。
    """
    try:
        from core import decision_service

        cfg = decision_service.load_decision_config()
        if not isinstance(cfg, dict) or not bool(cfg.get("enabled")):
            return False
        if bool(cfg.get("shadow_mode")):
            return False
        scenes = cfg.get("scenes")
        if not isinstance(scenes, dict):
            return False
        scene_cfg = scenes.get(SCENE)
        if not isinstance(scene_cfg, dict) or not bool(scene_cfg.get("enabled")):
            return False
        return not bool(scene_cfg.get("shadow", True))
    except Exception:
        # 配置读不出来就当没开：宁可不用决策，也不能影响回合
        return False


# ---------------------------------------------------------------------------
# 对外唯一入口
# ---------------------------------------------------------------------------

def resolve_turn_model(
    active_model: dict,
    *,
    turn_source: str = "local",
    pending_model_id: str = "",
    baseline_tokens: int = 0,
    user_text: str = "",
) -> dict | None:
    """判定本回合是否临时改用副模型；返回参数包或 ``None``。

    参数：
    - ``active_model``：入口层已经读到的会话激活模型 dict（含 litellm_model /
      api_key / base_url / context_window / id）；
    - ``turn_source``：回合来源，只有 ``"local"`` 才允许决策（LAN 回合、外部注入
      回合一律沿用会话模型）；
    - ``pending_model_id``：本回合入口临时指定的模型（星轨任务 / 秘书派发）。
      非空说明用户/调度方已经显式选了模型，决策无权覆盖；
    - ``baseline_tokens``：本会话当前上下文占用基线，用于窗口硬护栏；传 0 表示
      调用方拿不到基线，跳过这条护栏；
    - ``user_text``：本轮用户输入本身，**唯一**进入决策 state 的文本。

    返回：``None``（不覆盖）或
    ``{"litellm_model", "api_key", "base_url", "context_window", "model_id", "name"}``。

    返回值只是**本回合的一次性覆盖**，调用方必须用局部变量承接后显式透传，
    绝不能写回全局/会话模型状态（并行会话与子代理会串味）。
    """
    try:
        # --- 1) 前置条件：任一不满足就直接沿用本回合已有的主模型参数 ---
        if str(turn_source or "") != "local":
            return None
        if str(pending_model_id or "").strip():
            return None
        if not isinstance(active_model, dict) or not active_model:
            return None
        main_litellm = str(active_model.get("litellm_model") or "").strip()
        if not main_litellm:
            return None
        # 没有用户输入就没有可判断的对象（系统内部回合），不浪费一次决策
        state = _state_text(user_text)
        if not state:
            return None

        # --- 2) 取副模型（唯一允许使用的「便宜档」来源）---
        from core.model_manager import get_secondary_model

        secondary = get_secondary_model()
        if not isinstance(secondary, dict) or not secondary:
            return None
        secondary_litellm = str(secondary.get("litellm_model") or "").strip()
        secondary_id = str(secondary.get("id") or "").strip()
        if not secondary_litellm or not secondary_id:
            return None
        # 副模型与主模型是同一个：覆盖没有意义（还会白付一次决策）
        if secondary_litellm == main_litellm:
            return None
        # 关键字段缺失：宁可不覆盖，也不能拼出半套参数发请求
        secondary_key = str(secondary.get("api_key") or "").strip()
        secondary_base = str(secondary.get("base_url") or "").strip()
        if not secondary_key or not secondary_base:
            return None
        secondary_window = _positive_int(secondary.get("context_window"))
        if secondary_window <= 0:
            # 窗口未知就无法护栏：保守放弃覆盖
            return None

        # 窗口硬护栏：本会话已有上下文已经塞不进副模型窗口，覆盖只会换来 400。
        # 这是最后一道保险，优先于任何决策结论。
        baseline = _positive_int(baseline_tokens)
        if baseline > 0 and secondary_window < baseline:
            logger.info(
                "router 场景跳过覆盖：会话上下文基线 %s tokens 超过副模型窗口 %s tokens",
                baseline,
                secondary_window,
            )
            return None

        # --- 3) 问一次决策（网关已把 off/shadow/失败/非法答案收敛成 None）---
        from core.decision_gateway import decide

        picked = decide(
            SCENE,
            state,
            dict(ROUTER_QUESTION),
            options=ROUTER_OPTIONS,
            timeout_ms=ROUTER_TIMEOUT_MS,
            # 审计只附带非敏感的结构化信息，便于事后核对决策当时的处境
            audit_extra={"turn_source": "local", "baseline_tokens": baseline},
        )
        # 只认「副模型」这一档：None（未启用/影子/失败/低置信）= 保持主模型
        if picked != SECONDARY_OPTION:
            return None

        # --- 4) 输出参数包（五元组 + 展示名，全部取自副模型配置）---
        return {
            "litellm_model": secondary_litellm,
            "api_key": secondary_key,
            "base_url": secondary_base,
            "context_window": secondary_window,
            "model_id": secondary_id,
            "name": str(secondary.get("name") or "").strip(),
        }
    except Exception as error:
        # 决策模型永远不能成为主流程的故障点：任何异常都退回「沿用主模型」
        logger.debug("router 场景模型档位判定失败，本回合沿用主模型: %s", error)
        return None
