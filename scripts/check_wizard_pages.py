"""安装向导页面注册一致性校验。

新增或删除安装向导页面时，页面名必须在三处同时登记，否则运行时
`show_page()` 会 KeyError：

1. `SetupWizard._flow`        —— 导航顺序
2. `SetupWizard._BANNERS`     —— 标题栏文案
3. `self._page("<name>")`     —— 实际构建

用法（在仓库根目录执行）：

    python scripts/check_wizard_pages.py

退出码 0 表示一致，1 表示存在漏注册。
"""

import io
import re
import sys

SOURCE = "ycode_setup.py"


def main() -> int:
    try:
        src = io.open(SOURCE, encoding="utf-8").read()
    except OSError as exc:
        print("无法读取 %s：%s" % (SOURCE, exc))
        return 1

    built = set(re.findall(r'self\._page\("([a-z_]+)"\)', src))

    match_banner = re.search(r"_BANNERS = \{(.*?)\n    \}", src, re.S)
    banners = set(re.findall(r'"([a-z_]+)":', match_banner.group(1))) if match_banner else set()

    match_flow = re.search(r"self\._flow = \((.*?)\)\n", src, re.S)
    # 只取列表元素（后跟逗号或右括号），避免把 if mode == "full" 的条件值算进来
    flow = set(re.findall(r'"([a-z_]+)"(?=\s*[,\]])', match_flow.group(1))) if match_flow else set()

    print("已构建页面       :", sorted(built))
    print("横幅注册         :", sorted(banners))
    print("导航流程         :", sorted(flow))

    missing_banner = sorted(flow - banners)
    missing_page = sorted(flow - built)
    orphan_page = sorted(built - flow)

    print("流程中缺横幅     :", missing_banner or "无")
    print("流程中缺页面     :", missing_page or "无")
    print("构建但不在流程中 :", orphan_page or "无")

    ok = not missing_banner and not missing_page
    print("结论             :", "OK" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
