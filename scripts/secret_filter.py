# -*- coding: utf-8 -*-
"""已废弃说明（2026-09-03）。

本脚本原本负责 core/hunyuan_image.py 内置混淆 Key 的 git 出库脱敏
（clean 占位符化 / smudge 还原）。混元生图 Key 已改为仅从环境变量
HUNYUAN_API_KEY 读取，代码中不再存储任何 Key，过滤器随之失去作用。

.gitattributes 中的 filter=ycode-secret 绑定已移除；本机仓库的
filter.ycode-secret.clean / smudge git 配置可以自行清理：

  git config --unset filter.ycode-secret.clean
  git config --unset filter.ycode-secret.smudge

真实 Key 此前备份于 ~/.ycode/secrets/hunyuan_builtin_key.txt（不入库），
需要配置环境变量时可从该文件取用。
"""


def main():
    print("secret_filter 已废弃：混元 Key 现仅从环境变量 HUNYUAN_API_KEY 读取。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
