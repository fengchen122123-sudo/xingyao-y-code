def say_hello(name):
    return f"你好，{name}！这是一个测试技能返回的结果。我成功接入了技能系统！"

def register_skill():
    schema = {
        "type": "function",
        "function": {
            "name": "say_hello",
            "description": "一个测试技能插件，用于向某人问候并验证技能系统是否正常工作",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "要问候的人的名字"}
                },
                "required": ["name"]
            }
        }
    }
    return schema, say_hello
