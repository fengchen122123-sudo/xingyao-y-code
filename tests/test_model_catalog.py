from core.model_catalog import API_PROTOCOLS, get_preset_providers, provider_route_name
from core.reasoning_capabilities import get_reasoning_capability


def test_catalog_exposes_all_real_api_protocols():
    assert [(item["id"], item["path"]) for item in API_PROTOCOLS] == [
        ("anthropic", "/v1/messages"),
        ("openai", "/chat/completions"),
        ("responses", "/responses"),
    ]


def test_catalog_preserves_cli_model_metadata():
    providers = get_preset_providers()
    kimi = next(item for item in providers if item["id"] == "kimi-coding-openai")

    assert kimi["name"] == "月之暗面kimi-for-coding（OpenAI格式）"
    assert kimi["protocol"] == "openai"
    assert kimi["base_url"] == "https://api.kimi.com/coding/v1"


def test_catalog_queries_return_isolated_copies():
    first = get_preset_providers()
    first[0]["name"] = "mutated"

    second = get_preset_providers()
    assert second[0]["name"] != "mutated"
    assert len({provider["id"] for provider in second}) == len(second)


def test_responses_uses_openai_route():
    assert provider_route_name("responses", "deepseek-v4-flash") == "openai/deepseek-v4-flash"


def test_vendor_scoped_model_id_still_gets_transport_prefix():
    assert provider_route_name("openai", "zai-org/GLM-5.2") == "openai/zai-org/GLM-5.2"


def test_explicit_transport_prefix_is_not_duplicated():
    assert provider_route_name("openai", "openai/gpt-4o") == "openai/gpt-4o"


def test_preset_providers_carry_no_models():
    """预置厂商只预填厂商信息，不携带模型（模型由用户经「获取模型列表」添加）。"""
    for provider in get_preset_providers():
        assert "models" not in provider
        assert provider["name"]
        assert provider["base_url"].startswith("https://")
        assert provider["protocol"] == "openai"
