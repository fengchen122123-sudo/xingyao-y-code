MODEL_SPECS = {
    "deepseek-flash": (1048576, 384000),
    "deepseek-v4-flash": (1048576, 384000),
    "deepseek-v4-flash-0731": (1048576, 384000),
    "deepseek-v4.1-flash": (1048576, 384000),
    "deepseek-v4.1-flash-1017": (1048576, 384000),
    "deepseek-v4-pro": (1048576, 384000),
    "deepseek-v4-pro-0813": (1048576, 384000),
    "kimi-k2.5": (262144, 262144),
    "kimi-k2.6": (262144, 262144),
    "kimi-k2.7-code": (262144, 262144),
    "kimi-k3": (1048576, 131072),
    "kimi-k3.1": (1048576, 131072),
    "kimi-k3.1-pro": (1048576, 131072),
    "k3": (1048576, 131072),
    "k3-256k": (262144, 131072),
    "k3.1": (1048576, 131072),
    "k3.1-pro": (1048576, 131072),
    "k3.1-pro-highspeed": (1048576, 131072),
    "glm-5": (200000, 131072),
    "glm-5.1": (200000, 131072),
    "glm-5.2": (1000000, 131072),
    "glm-5.3": (1000000, 131072),
    "glm-5.3-flash": (1000000, 131072),
    "glm-5.3-flashx": (1048576, 131072),
    "glm-5.3-free": (1000000, 131072),
    "qwen3.7-max": (1000000, 131072),
    "qwen3.7-flash": (1000000, 131072),
    "qwen3.8-max": (1000000, 131072),
    "qwen3.8-flash": (1000000, 131072),
    "qwen3.8-27b": (262144, 131072),
    "minimax-m2.5": (204800, 131072),
    "minimax-m2.7": (204800, 131072),
    "minimax-m3": (1000000, 131072),
    "minimax-m3.1-flash": (1000000, 131072),
    "longcat-2.0": (1048576, 128000),
    "longcat-2.5": (1048576, 128000),
    "longcat-2.5-preview": (1048576, 128000),
    "mimo-v2.6-pro": (1048576, 131072),
    "mimo-v2.6-flash": (1048576, 131072),
    "mimo-v2.6-pro-ultraspeed": (1048576, 131072),
    "mimo-v2.5": (1048576, 131072),
    "mimo-v2.5-pro": (1048576, 131072),
    "seed-2.1-turbo": (262144, 262144),
    "seed-2.1-pro": (262144, 262144),
    # 阶跃（上下文来自 token plan 端点；3.7-flash 输出为 OpenRouter 供应商
    # 元数据 230400，官方未公布；其余输出未公布，回退内置默认）
    "step-3.7-flash": (262144, 230400),
    "step-router-v1": (262144, None),
    "step-5-preview": (1024000, None),
    "step-3.5-flash": (262144, None),
    "step-3.5-flash-2603": (262144, None),
    "agnes-2.5-flash": (512000, 65536),
    "agnes-3.0-flash": (524288, 65536),
    "sensenova-6.7-flash-lite": (262144, None),
    "sensenova-6.8-flash-lite": (262144, None),
    "gpt-5.6-sol": (1050000, 128000),
    "gpt-6-astra": (1050000, 128000),
    "claude-sonnet-4-5": (200000, 64000),
    "claude-opus-4-6": (1000000, 128000),
    "claude-haiku-4-5": (200000, 64000),
    "gemini-3.1-pro": (1048576, 65536),
    "gemini-3-flash": (1048576, 65536),
}


def lookup_model_spec(model_id) -> tuple:
    name = str(model_id or "").strip().lower()
    if not name:
        return None, None
    if "/" in name:
        name = name.rsplit("/", 1)[1]
    spec = MODEL_SPECS.get(name)
    if not spec:
        return None, None
    context_window, max_tokens = spec
    return context_window or None, max_tokens or None
