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
        {"name": "deepseek-v4-flash-vision-exp", "context_window": 1000000, "supports_vision": True,
         "supports_function_calling": True, "computer_protocol": "generic"},
        {"name": "deepseek-v4.1-flash-expires-on-0910", "context_window": 1000000, "supports_vision": True,
         "supports_function_calling": True},
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


def test_confirmed_preset_models_carry_literal_computer_metadata():
    """只给 brief/设计 9.2 明确点名的条目写字面量元数据，不得按名字批量推断。"""
    providers = get_preset_providers()
    generic = {
        (provider["id"], model["name"])
        for provider in providers
        for model in provider["models"]
        if model.get("computer_protocol") == "generic"
    }
    assert generic == {
        ("deepseek", "deepseek-v4-flash-vision-exp"),
        ("kimi-coding-anthropic", "k3"),
        ("kimi-coding-anthropic", "k3-256k"),
        ("kimi-coding-openai", "k3"),
        ("kimi-coding-openai", "k3-256k"),
        ("moonshot", "kimi-k3"),
        ("sensenova", "kimi-k3"),
        ("token-rhythm", "glm-5.3-flash"),
        ("zhipu", "glm-5.3-flash"),
        ("zhipu-coding-openai", "glm-5.3-flash"),
        ("zhipu-coding-anthropic", "glm-5.3-flash"),
    }


def test_preset_computer_metadata_never_forms_half_valid_combination():
    """generic 条目必须同时声明视觉与工具能力；未知协议值不得出现在目录中。"""
    for provider in get_preset_providers():
        for model in provider["models"]:
            protocol = model.get("computer_protocol", "none")
            assert protocol in {"none", "generic", "native_openai"}
            if protocol == "generic":
                assert model.get("supports_vision") is True
                assert model.get("supports_function_calling") is True


def test_token_rhythm_preset_contains_qwen38_flash():
    providers = get_preset_providers()
    token_rhythm = next(item for item in providers if item["id"] == "token-rhythm")
    model = next(m for m in token_rhythm["models"] if m["name"] == "qwen3.8-flash")

    assert model["context_window"] == 1000000
    assert model["supports_vision"] is True
    assert model["supports_function_calling"] is True


def test_token_rhythm_preset_contains_deepseek_v4_1_flash_expires_on_0910():
    providers = get_preset_providers()
    token_rhythm = next(item for item in providers if item["id"] == "token-rhythm")
    model = next(
        m
        for m in token_rhythm["models"]
        if m["name"] == "deepseek-v4.1-flash-expires-on-0910"
    )

    assert model["context_window"] == 1000000
    assert model["supports_vision"] is True
    assert model["supports_function_calling"] is True
