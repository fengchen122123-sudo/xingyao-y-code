"""Turn 起点电脑操控能力决策（设计 9.3）。

resolve_computer_capability 是纯函数：不读全局 config、不做网络探测、不按模型名
分支。判定只依赖显式传入的模型元数据（supports_vision / supports_function_calling /
computer_protocol）、provider 的 protocol/base_url、设置快照与 turn_source。
官方 OpenAI base 只允许规范化 host api.openai.com。
"""

from urllib.parse import urlsplit

from core.computer.models import ComputerCapability, ComputerProtocol, ComputerTarget
from core.computer.settings import ComputerSettingsSnapshot

_OFFICIAL_OPENAI_HOST = "api.openai.com"


def _rejected(settings: ComputerSettingsSnapshot, code: str, reason: str) -> ComputerCapability:
    return ComputerCapability(
        enabled=False,
        protocol=ComputerProtocol.NONE.value,
        targets=(),
        browser="",
        settings_generation=settings.generation,
        error_code=code,
        reason=reason,
    )


def _is_official_openai_base(base_url: object) -> bool:
    """规范化解析 base URL 后 host 必须恰好是 api.openai.com。"""
    if type(base_url) is not str or not base_url.strip():
        return False
    text = base_url.strip()
    if "://" not in text:
        text = "https://" + text
    try:
        hostname = urlsplit(text).hostname
    except ValueError:
        return False
    return hostname == _OFFICIAL_OPENAI_HOST


def resolve_computer_capability(
    model: dict,
    provider: dict,
    settings: ComputerSettingsSnapshot,
    turn_source: str,
) -> ComputerCapability:
    """在 Turn 起点冻结一次能力决策；任何一步不满足都返回带稳定错误码的禁用结果。"""
    model = model or {}
    provider = provider or {}

    if turn_source != "local":
        return _rejected(
            settings, "computer_remote_forbidden", "局域网或移动端发起的回合不允许电脑操控"
        )
    if not settings.platform_supported:
        return _rejected(
            settings, "computer_unsupported_os", "电脑操控目前仅支持 Windows"
        )

    targets = []
    if settings.browser_control_enabled:
        targets.append(ComputerTarget.BROWSER.value)
    if settings.computer_control_enabled:
        targets.append(ComputerTarget.DESKTOP.value)
    if not targets:
        return _rejected(settings, "computer_disabled", "电脑操控未开启")

    protocol = model.get("computer_protocol")
    if protocol == ComputerProtocol.GENERIC.value:
        if (
            model.get("supports_vision") is not True
            or model.get("supports_function_calling") is not True
        ):
            return _rejected(
                settings,
                "computer_model_not_capable",
                "模型未显式声明视觉与工具调用能力，不能使用 generic 电脑操控",
            )
        return ComputerCapability(
            enabled=True,
            protocol=ComputerProtocol.GENERIC.value,
            targets=tuple(targets),
            browser=settings.computer_browser,
            settings_generation=settings.generation,
        )

    if protocol == ComputerProtocol.NATIVE_OPENAI.value:
        if provider.get("protocol") != "responses" or not _is_official_openai_base(
            provider.get("base_url")
        ):
            return _rejected(
                settings,
                "computer_protocol_mismatch",
                "native_openai 要求 OpenAI 官方 API 与 Responses 协议",
            )
        return ComputerCapability(
            enabled=True,
            protocol=ComputerProtocol.NATIVE_OPENAI.value,
            targets=tuple(targets),
            browser=settings.computer_browser,
            settings_generation=settings.generation,
        )

    return _rejected(
        settings, "computer_model_not_capable", "模型未声明电脑操控能力"
    )
