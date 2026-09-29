"""飞书通道配置读取（只读；写入在 gui_bridge 的设置接口里）。

配置键定义在根 `config.py`，密钥写盘前经 `core.credentials` 加密。
"""

from dataclasses import dataclass, field
from typing import List

import config as app_config
from core.credentials import unprotect_secret


@dataclass(frozen=True)
class FeishuConfig:
    enabled: bool = False
    app_id: str = ""
    app_secret: str = ""
    approvers: List[str] = field(default_factory=list)
    notify_chat_id: str = ""

    @property
    def configured(self) -> bool:
        """是否具备启动条件：开关打开且凭据齐全。"""
        return bool(self.enabled and self.app_id and self.app_secret)

    @property
    def has_secret(self) -> bool:
        """仅用于界面回显：密钥是否已配置（绝不回传明文）。"""
        return bool(self.app_secret)


def _as_bool(value, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    if value is None:
        return default
    return bool(value)


def _as_str_list(value) -> List[str]:
    """白名单兼容「逗号分隔字符串」与「列表」两种写法。"""
    if isinstance(value, str):
        items = value.replace("，", ",").split(",")
    elif isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        return []
    return [str(item).strip() for item in items if str(item).strip()]


def load_feishu_config() -> FeishuConfig:
    """读取当前飞书配置。任何异常都降级为「未配置」，绝不因此影响主流程。"""
    try:
        data = app_config._load() or {}
    except Exception:
        return FeishuConfig()

    raw_secret = str(data.get(app_config.FEISHU_APP_SECRET_KEY, app_config.FEISHU_APP_SECRET_DEFAULT) or "")
    secret = ""
    if raw_secret:
        try:
            secret = (unprotect_secret(raw_secret) or "").strip()
        except Exception:
            # 解密失败（换机器/换用户）按未配置处理，不让启动链崩溃
            secret = ""

    return FeishuConfig(
        enabled=_as_bool(data.get(app_config.FEISHU_ENABLED_KEY, app_config.FEISHU_ENABLED_DEFAULT)),
        app_id=str(data.get(app_config.FEISHU_APP_ID_KEY, app_config.FEISHU_APP_ID_DEFAULT) or "").strip(),
        app_secret=secret,
        approvers=_as_str_list(data.get(app_config.FEISHU_APPROVERS_KEY, app_config.FEISHU_APPROVERS_DEFAULT)),
        notify_chat_id=str(
            data.get(app_config.FEISHU_NOTIFY_CHAT_ID_KEY, app_config.FEISHU_NOTIFY_CHAT_ID_DEFAULT) or ""
        ).strip(),
    )
