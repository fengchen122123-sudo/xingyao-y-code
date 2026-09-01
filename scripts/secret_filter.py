# -*- coding: utf-8 -*-
"""git clean/smudge 过滤器：core/hunyuan_image.py 内置 Key 出库脱敏、本地还原。

目的：公开仓库（Gitee）中的代码不含真实混元 Server API Key，但本机工作区
代码保持完整（开箱即用）。机制：

- clean（工作区 → 暂存区/仓库）：把 `_BUILTIN_KEY_B64 = ( ... )` 整块替换为
  占位符单行（占位符是合法 Python，克隆者未配置 Key 时功能回退到环境变量
  HUNYUAN_API_KEY，缺 Key 时报错提示而非崩溃）。
- smudge（仓库 → 工作区）：把占位符行还原为本机保存的真实 Key 块。
  真实 Key 块保存在 ~/.ycode/secrets/hunyuan_builtin_key.txt（不入库）。

一次性配置（每台开发机执行一次；.gitattributes 已绑定 filter=ycode-secret）：

  python scripts/secret_filter.py extract      # 从当前工作区提取 Key 块到本机
  git config filter.ycode-secret.clean "python scripts/secret_filter.py clean"
  git config filter.ycode-secret.smudge "python scripts/secret_filter.py smudge"
  git add --renormalize core/hunyuan_image.py  # 用 clean 后的内容刷新暂存区

Key 轮换（在 CloudBase 控制台重签并作废旧 Key 后执行；无需强推，历史里的
旧 Key 随作废失效）——新 Key 不进入 git、不进入聊天记录，只落本机：

  type 新key.txt | python scripts/secret_filter.py set-key
  python scripts/secret_filter.py set-key --file 新key.txt

安全边界（如实说明）：
- 本方案只保证"今后的提交/推送"不含 Key；历史提交中已存在混淆形式的 Key。
  彻底清除须二选一：① 轮换 Key（推荐，CloudBase 控制台重签后运行 extract
  更新本机密钥文件与 tests/test_hunyuan_image_auth.py 同步说明）；② 重写
  全部历史并强推（破坏性操作，需所有协作者重新克隆）。
- 新克隆者 checkout 时会看到占位符版本；git 会提示 filter 未配置，属预期。
  需要 Key 时由项目所有者私发，或自行设置 HUNYUAN_API_KEY。
"""
import base64
import os
import re
import sys

TARGET_NAME = "_BUILTIN_KEY_B64"
# 与 core/hunyuan_image.py 保持一致：Key 以 XOR 0x5A + Base64 混淆存储
_XOR_MASK = 0x5A
SOURCE_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "core", "hunyuan_image.py")
SECRET_FILE = os.path.join(os.path.expanduser("~"), ".ycode", "secrets",
                           "hunyuan_builtin_key.txt")

# 匹配整个 Key 块：从赋值开始到首个行首（含缩进）的右括号，不吞尾部换行
BLOCK_RE = re.compile(r"_BUILTIN_KEY_B64 = \(.*?^\s*\)", re.S | re.M)
# 匹配占位符行（clean 后仓库中的形态）
PLACEHOLDER_RE = re.compile(r'^_BUILTIN_KEY_B64 = ""[^\n]*$', re.M)

PLACEHOLDER = (
    '_BUILTIN_KEY_B64 = ""  # [已脱敏] 真实 Key 不入库：本机由 git smudge 过滤器'
    "（scripts/secret_filter.py）还原，或设置环境变量 HUNYUAN_API_KEY"
)


def _read_secret_block():
    """读本机保存的真实 Key 块；不存在或内容异常时返回 None。"""
    try:
        with open(SECRET_FILE, "r", encoding="utf-8") as f:
            block = f.read().strip("\r\n")
        return block if block.startswith(TARGET_NAME) else None
    except OSError:
        return None


def cmd_extract():
    """从当前工作区的 core/hunyuan_image.py 提取真实 Key 块存到本机密钥文件。"""
    with open(SOURCE_FILE, "r", encoding="utf-8") as f:
        text = f.read()
    match = BLOCK_RE.search(text)
    if not match:
        print("错误：在 core/hunyuan_image.py 中未找到 Key 块（可能已被脱敏）。", file=sys.stderr)
        return 1
    os.makedirs(os.path.dirname(SECRET_FILE), exist_ok=True)
    with open(SECRET_FILE, "w", encoding="utf-8") as f:
        f.write(match.group(0) + "\n")
    print("已保存真实 Key 块到:", SECRET_FILE)
    print("请勿把该文件加入任何仓库。")
    return 0


def cmd_clean():
    """工作区 → 仓库：Key 块替换为占位符。幂等；无需本机密钥文件。"""
    text = sys.stdin.read()
    sys.stdout.write(BLOCK_RE.sub(lambda _m: PLACEHOLDER, text))
    return 0


def _write_block_into_source(block):
    """把（新）Key 块写入工作区 core/hunyuan_image.py（替换现有块或占位符）。"""
    with open(SOURCE_FILE, "r", encoding="utf-8") as f:
        text = f.read()
    new_text, replaced = BLOCK_RE.subn(lambda _m: block, text, count=1)
    if not replaced:
        new_text, replaced = PLACEHOLDER_RE.subn(lambda _m: block, text, count=1)
    if not replaced:
        print("错误：源文件中未找到 Key 块或占位符。", file=sys.stderr)
        return 1
    with open(SOURCE_FILE, "w", encoding="utf-8") as f:
        f.write(new_text)
    return 0


def cmd_set_key():
    """轮换 Key：从 stdin 或 --file 读新 Key，更新本机密钥文件与工作区源文件。"""
    args = sys.argv[2:]
    if args:
        if args[0] != "--file" or len(args) < 2:
            print("用法: python scripts/secret_filter.py set-key [--file 路径]",
                  file=sys.stderr)
            return 2
        try:
            with open(args[1], "r", encoding="utf-8") as f:
                key = "".join(f.read().split())
        except OSError as exc:
            print("错误：无法读取文件 %s：%s" % (args[1], exc), file=sys.stderr)
            return 1
    else:
        key = "".join(sys.stdin.read().split())  # 粘贴常带换行/空格，全部去除
    if not key:
        print("错误：Key 为空。", file=sys.stderr)
        return 1
    try:
        key.encode("ascii")
    except UnicodeEncodeError:
        print("错误：Key 含非 ASCII 字符。", file=sys.stderr)
        return 1
    encoded = base64.b64encode(
        bytes(c ^ _XOR_MASK for c in key.encode("utf-8"))
    ).decode("ascii")
    lines = [encoded[i:i + 76] for i in range(0, len(encoded), 76)]
    if len(lines) == 1:
        block = '_BUILTIN_KEY_B64 = "%s"' % lines[0]
    else:
        block = "_BUILTIN_KEY_B64 = (\n%s\n)" % "\n".join(
            '    "%s"' % ln for ln in lines
        )
    os.makedirs(os.path.dirname(SECRET_FILE), exist_ok=True)
    with open(SECRET_FILE, "w", encoding="utf-8") as f:
        f.write(block + "\n")
    rc = _write_block_into_source(block)
    if rc == 0:
        print("已更新本机密钥文件:", SECRET_FILE)
        print("已更新 core/hunyuan_image.py（本地完整；提交时 clean 过滤器仍自动脱敏）。")
        print("请确认已在 CloudBase 控制台作废旧 Key。")
    return rc


def cmd_smudge():
    """仓库 → 工作区：占位符还原为真实 Key 块。幂等；无密钥文件时原样通过。"""
    text = sys.stdin.read()
    block = _read_secret_block()
    if block is not None:
        text = PLACEHOLDER_RE.sub(lambda _m: block, text, count=1)
    sys.stdout.write(text)
    return 0


def main():
    # Windows 管道默认 GBK，强制 UTF-8 避免中文注释乱码
    try:
        sys.stdin.reconfigure(encoding="utf-8")
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    action = sys.argv[1] if len(sys.argv) > 1 else ""
    handlers = {
        "extract": cmd_extract,
        "clean": cmd_clean,
        "smudge": cmd_smudge,
        "set-key": cmd_set_key,
    }
    handler = handlers.get(action)
    if handler is None:
        print("用法: python scripts/secret_filter.py extract|clean|smudge|set-key",
              file=sys.stderr)
        return 2
    return handler()


if __name__ == "__main__":
    sys.exit(main())
