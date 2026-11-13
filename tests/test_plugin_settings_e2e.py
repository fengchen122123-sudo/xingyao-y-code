"""插件设置页端到端（隔离环境）：真实 loader 加载外部插件 → 钩子渲染 → 删除卸载。

隔离约定：子进程 + 临时 YCODE_DATA_DIR + 临时用户插件目录；
install 一份最小测试插件（带 plugin_settings 钩子）验证通用能力，
再验证"删除目录 → refresh 后记录消失 + 模块/工具卸载"。
"""

import json
import os
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent

_CHILD = r'''
import json, os, sys
sys.path.insert(0, r"{repo_root}")
from core.plugin_loader import call_plugin_hook, get_plugin_infos, refresh_plugins, _LOADED_MODULES

user_dir = os.path.join(os.environ["YCODE_DATA_DIR"], "plugins")
plugin_dir = os.path.join(user_dir, "demo_settings")
os.makedirs(plugin_dir, exist_ok=True)
with open(os.path.join(plugin_dir, "plugin.json"), "w", encoding="utf-8") as f:
    json.dump({{"name": "demo_settings", "version": "0.0.1", "entry": "plugin.py", "enabled": True}}, f)
with open(os.path.join(plugin_dir, "plugin.py"), "w", encoding="utf-8") as f:
    f.write(
        "def register(registry):\n"
        "    pass\n"
        "def plugin_settings(payload):\n"
        "    action = (payload or {{}}).get('action')\n"
        "    if action == 'render':\n"
        "        return {{\"page\": {{\"title\": \"Demo\", \"sections\": []}}}}\n"
        "    if action == 'ping':\n"
        "        return {{\"ok\": True, \"message\": \"pong\"}}\n"
        "    return {{\"ok\": False, \"message\": \"unknown\"}}\n"
    )

# 信任（用户目录插件需要）+ 加载
import core.plugin_loader as pl
pl._user_plugin_dir = lambda: __import__("pathlib").Path(user_dir)   # 把用户插件目录指到隔离位置
ok, msg = pl.trust_plugin("demo_settings")
assert ok, msg
refresh_plugins()
names = [i.get("name") for i in get_plugin_infos() if i.get("status") not in ("overridden",)]
assert "demo_settings" in names, names

# 钩子渲染
result = call_plugin_hook("demo_settings", "plugin_settings", {{"action": "render"}})
assert result["ok"] and result["data"]["page"]["title"] == "Demo", result
result = call_plugin_hook("demo_settings", "plugin_settings", {{"action": "ping"}})
assert result["ok"] and result["data"]["message"] == "pong"

# 删除目录 → refresh → 记录消失 + 模块卸载
import shutil
shutil.rmtree(plugin_dir)
refresh_plugins()
names = [i.get("name") for i in get_plugin_infos()]
assert "demo_settings" not in names, names
assert "demo_settings" not in _LOADED_MODULES
print("E2E-CHILD-OK")
'''


def test_plugin_settings_e2e(tmp_path):
    script = _CHILD.format(repo_root=str(_REPO_ROOT))
    env = dict(os.environ)
    env["YCODE_DATA_DIR"] = str(tmp_path)
    env["PYTHONIOENCODING"] = "utf-8:replace"
    proc = subprocess.run(
        [sys.executable, "-c", script],
        env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
    )
    assert proc.returncode == 0, (proc.stdout + "\n" + proc.stderr)[-2000:]
    assert "E2E-CHILD-OK" in proc.stdout
