from core.model_catalog import API_PROTOCOLS, get_preset_providers, litellm_model_name


def test_catalog_exposes_all_real_api_protocols():
    assert [(item["id"], item["path"]) for item in API_PROTOCOLS] == [
        ("anthropic", "/v1/messages"),
        ("openai", "/chat/completions"),
        ("responses", "/responses"),
    ]


def test_catalog_preserves_cli_model_metadata():
    providers = get_preset_providers()
    deepseek = next(item for item in providers if item["id"] == "deepseek")

    assert deepseek["name"] == "深度求索"
    assert deepseek["protocol"] == "responses"
    assert deepseek["base_url"] == "https://api.deepseek.com"
    assert deepseek["models"] == [
        {"name": "deepseek-v4-pro", "context_window": 1000000, "supports_vision": False},
        {"name": "deepseek-v4-flash", "context_window": 1000000, "supports_vision": False},
    ]


def test_catalog_queries_return_isolated_copies():
    first = get_preset_providers()
    first[0]["models"][0]["name"] = "mutated"

    second = get_preset_providers()
    assert second[0]["models"][0]["name"] != "mutated"
    assert len({provider["id"] for provider in second}) == len(second)


def test_responses_uses_openai_litellm_route():
    assert litellm_model_name("responses", "deepseek-v4-flash") == "openai/deepseek-v4-flash"


def test_vendor_scoped_model_id_still_gets_transport_prefix():
    assert litellm_model_name("openai", "zai-org/GLM-5.2") == "openai/zai-org/GLM-5.2"


def test_explicit_transport_prefix_is_not_duplicated():
    assert litellm_model_name("openai", "openai/gpt-4o") == "openai/gpt-4o"
