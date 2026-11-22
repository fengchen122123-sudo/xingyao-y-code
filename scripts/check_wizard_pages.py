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
