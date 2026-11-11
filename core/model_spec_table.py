"""已知模型的官方规格表（上下文窗口 / 最大输出）。

数据来源：各厂商官方文档与发布页，2026-09 经网络核实整理。主要出处：
- DeepSeek: api-docs.deepseek.com/quick_start/pricing（1M 上下文 / 384K 输出全系）
- Kimi: platform.kimi.ai/docs/models、Kimi K2.6 官方博客（262144/262144）
- GLM: docs.z.ai/guides/llm（glm-5/5.1 为 200K 上下文，5.2 起 1M；输出统一 128K）
- Qwen: help.aliyun.com 百炼模型文档（3.7/3.8 系 1M/131072）
- MiniMax: OpenRouter 与官方模型卡（204800/131072）
- MiMo: mimo.mi.com 官方模型页（V2.6/V2.5 全系 1M 上下文 / 128K 输出）
- Seed: 火山方舟模型列表（256k 输入/输出对齐）
- 阶跃: token plan /v1/models 端点 max_input_tokens（输出 230400 来自
  OpenRouter 供应商元数据，官方未公布）
- Agnes: wiki.agnes-ai.com Limits 表
- GPT: openai.com 发布公告（1.05M 上下文 / 128K 输出）

用途：GUI「获取模型列表」选中模型后一键填充上下文窗口与最大输出。
厂商 /v1/models 自带的元数据永远优先于本表；未收录模型一律回退内置默认，
绝不猜测。表键为模型叶子名（小写）。
"""

# 模型叶子名 -> (上下文窗口, 最大输出)
MODEL_SPECS = {
    # DeepSeek 全系（官方定价页 1M / 384K；v4-flash 为退役别名，请求由
    # V4.1-Flash 即 deepseek-flash 同规格服务）
    "deepseek-flash": (1048576, 384000),
    "deepseek-v4-flash": (1048576, 384000),
    "deepseek-v4-flash-0731": (1048576, 384000),
    "deepseek-v4-pro": (1048576, 384000),
    "deepseek-v4-pro-0813": (1048576, 384000),
    # Kimi / Moonshot
    "kimi-k2.5": (262144, 262144),
    "kimi-k2.6": (262144, 262144),
    "kimi-k2.7-code": (262144, 262144),
    "kimi-k3": (1048576, 131072),
    "k3": (1048576, 131072),
    "k3-256k": (262144, 131072),
    # GLM（5/5.1 官方标 200K；5.2 起 1M；输出全系 128K）
    "glm-5": (200000, 131072),
    "glm-5.1": (200000, 131072),
    "glm-5.2": (1000000, 131072),
    "glm-5.3": (1000000, 131072),
    "glm-5.3-flash": (1000000, 131072),
    "glm-5.3-free": (1000000, 131072),
    # Qwen（百炼 API 模型 1M/131072；27b 开源原生 256K）
    "qwen3.7-max": (1000000, 131072),
    "qwen3.8-max": (1000000, 131072),
    "qwen3.8-flash": (1000000, 131072),
    "qwen3.8-27b": (262144, 131072),
    # MiniMax
    "minimax-m2.5": (204800, 131072),
    "minimax-m2.7": (204800, 131072),
    # 小米 MiMo / 字节 Seed
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
    # Agnes AI
    "agnes-2.5-flash": (512000, 65536),
    "agnes-3.0-flash": (524288, 65536),
    # 商汤（输出未公布）
    "sensenova-6.7-flash-lite": (262144, None),
    # OpenAI（中转站型号 gpt-5.6-sol / gpt-6-astra 的官方本体规格）
    "gpt-5.6-sol": (1050000, 128000),
    "gpt-6-astra": (1050000, 128000),
}


def lookup_model_spec(model_id) -> tuple:
    """按模型 ID 查规格，返回 (上下文窗口, 最大输出)；未收录返回 (None, None)。

    匹配键取最后一个 "/" 之后的叶子名并转小写，兼容网关挂载名
    （如 z-ai/glm-5.3-free、openai/deepseek-flash）。
    """
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
