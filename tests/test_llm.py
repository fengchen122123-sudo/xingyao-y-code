import pytest
from core.llm import chat_with_model

def test_chat_with_model_success(mocker):
    """测试大模型通信是否正常 (使用 Mock 拦截真实网络请求)"""
    
    mock_response = mocker.MagicMock()
    
    # 1. 精细化定制我们的假消息
    mock_message = mocker.MagicMock()
    mock_message.content = "你好，我是伪造的 AI 回复！"
    # 🌟 核心修复点：明确告诉网关，这次假星瑶没有调用任何工具！
    mock_message.tool_calls = None 
    
    mock_response.choices = [
        mocker.MagicMock(message=mock_message)
    ]
    
    mocker.patch('core.llm.completion', return_value=mock_response)
    
    reply = chat_with_model(
        model_name="gpt-4o",
        api_key="fake-key-123",
        messages=[{"role": "user", "content": "你好"}]
    )
    
    assert reply == "你好，我是伪造的 AI 回复！"