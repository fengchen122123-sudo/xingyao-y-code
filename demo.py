from core.llm import chat_with_model
import os

# 1. 配置模型与密钥
# 建议从环境变量读取，避免把密钥写进代码：
# MY_KEY = os.environ.get("AGNES_API_KEY", "")
MY_MODEL = "openai/agnes-2.0-flash"
MY_KEY = os.environ.get("AGNES_API_KEY", "sk-your-api-key-here")
MY_BASE_URL = "https://apihub.agnes-ai.com/v1"

# 2. 构造一段真实的对话历史
messages = [
    {"role": "system", "content": "你是一个幽默的终端编码助手 Y-code。"},
    {"role": "user", "content": "你好，能给我讲个程序员的冷笑话吗？"}
]

print("⏳ 正在跨越网线思考中...")

# 3. 真正去调用
reply = chat_with_model(
    model_name=MY_MODEL,
    api_key=MY_KEY,
    messages=messages,
    api_base=MY_BASE_URL
)

print("\n🤖 Y-code 回复：")
print(reply)
