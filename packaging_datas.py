"""安装包 datas 裁剪规则（原 ycode.spec 内的 `_strip_unused_datas`，搬移出来便于单测）。

ycode.spec 是 PyInstaller 的 spec 文件（由 PyInstaller 用 exec 执行），不是可 import
的普通模块，规则写在里面就永远无法被 pytest 覆盖；而 datas 裁剪一旦删错，只会在
打包版运行期才炸（缺内置技能、缺前端静态资源、缺 litellm 导入期必读的 tokenizer /
成本表），代价极高。

因此把规则常量与实现**原样**搬到这里，spec 只保留 `from packaging_datas import
strip_unused_datas` 并直接调用。规则内容与判定行为与搬移前完全一致，未做任何调整。
"""

import os

# ---------------------------------------------------------------------------
# datas 裁剪：剔除第三方库 collect_all 带进来的无用资产
#
# collect_all 会把整个包目录（含静态前端、文档、备用数据文件）原样塞进 datas，
# 其中相当一部分本项目运行期永远读不到。下面用「dest 归一化后的相对路径」精确
# 匹配来裁剪：datas 元素是 (src, dest) 元组，dest 是「目标目录」而不是文件路径
# （Windows 下 PyInstaller 生成的是反斜杠形式，如 'litellm\\proxy\\swagger'），
# 所以先把 dest 的分隔符统一成 '/' 再比对；文件名级规则用
# 「dest + 源文件 basename」拼出的完整包内相对路径判断 —— 不直接对 src 绝对路径
# 比对，避免盘符/分隔符差异导致漏匹配或误匹配。
#
# 只裁剪下面明确列出的路径；其余 datas（config.example.json、
# system_prompt*.md、vendor/ripgrep、vendor/mermaid、frontend/dist、
# frontend-mobile/dist、frontend/mobile-shim.js、skills、plugins、assets/*、
# vendor/ffmpeg/*）一律保留。
# ---------------------------------------------------------------------------

# A. 整段剔除：相对路径与规则相等，或位于 "<规则>/" 子树下，整棵剔除。
DROP_SUBTREES = (
    # litellm 的 proxy server（litellm/proxy/*）在本项目里从不启动：全仓库没有
    # litellm.proxy / proxy_server 的任何引用，模型调用直连 provider；这两份是
    # 只有 proxy HTTP 服务才会对外提供的静态资源（Next.js 实验控制台产物与
    # swagger UI），运行期绝无读取路径，纯死重量。
    'litellm/proxy/_experimental/out',
    'litellm/proxy/swagger',
    # 【故意不剔除】litellm/model_prices_and_context_window_backup.json：
    # 名字虽带 _backup，但它是 litellm 唯一的本地成本/上下文窗口表
    # （site-packages/litellm 下没有非 _backup 的同名文件），
    # litellm_core_utils/get_model_cost_map.py:41 的 load_local_model_cost_map()
    # 直接 importlib.resources 读它，且 litellm/__init__.py:460 在「导入时」就执行
    # model_cost = get_model_cost_map(url=...)：远端拉取失败/离线时走该本地兜底，
    # 此处没有 try/except。删掉它会让 import litellm 抛 FileNotFoundError，
    # 整个程序起不来。只省 1.3MB，不值得，故保留。
    # HuggingFace tokenizer 元数据：本项目只调用云端 provider API，不在本地跑
    # 任何 HF 本地 tokenizer，该目录不会被加载。
    'litellm/llms/huggingface/huggingface_llms_metadata',
    # Playwright 的 TS 类型声明与内置 vite 打包产物：只有开发时生成 node driver
    # 的构建链会用，运行期 driver 完全不读。playwright 现已整体移出打包（见
    # common_excludes），这两条规则作为兜底保留：若将来又被 hook 带回资源，
    # 也不会把这两坨带进安装包。
    'playwright/driver/package/types',
    'playwright/driver/package/lib/vite',
)

# B. 条件剔除 1：litellm 自带离线 tokenizer 资源目录。目录里有 1 个 .py（逻辑）+
# anthropic_tokenizer.json + 3 个哈希命名的二进制分词表（合计约 7.5MB）。
#   - 3 个哈希二进制：只在 litellm.token_counter / litellm.encode 内部按模型挑选
#     分词器时才被读；本项目 token 估算走自研启发式
#     core/llm_native_transport.fast_token_counter，用量以 provider 返回的 usage
#     为准，从不调用 litellm.token_counter —— 删掉可省约 6.2MB。
#   - 【故意保留】anthropic_tokenizer.json：litellm/utils.py:213 在「模块导入时」
#     就用 importlib.resources 读它（except 只兜 ImportError/AttributeError/
#     TypeError，漏文件抛的是 FileNotFoundError），删掉会让 import litellm 直接失败。
TOKENIZERS_DIR = 'litellm/litellm_core_utils/tokenizers'
TOKENIZERS_KEPT_NAMES = frozenset({'anthropic_tokenizer.json'})

# B. 条件剔除 2：pyfiglet 字体。main.py 里只用 Figlet(font='slant')，
# slant_relief.flf 一并保留（防止将来切到浮雕斜体时静默缺字体），其余 570+ 个
# .flf 字体永不加载。
PYFIGLET_FONTS_DIR = 'pyfiglet/fonts'
PYFIGLET_KEPT_FONTS = frozenset({'slant.flf', 'slant_relief.flf'})


def _dest_rel_path(src, dest):
    """把 (src, dest) 归一化成包内相对路径，返回 (归一化 dest, 完整相对路径)。

    dest 是目标目录（Windows 下为反斜杠），文件名取自 src 的 basename
    —— basename 不含路径分隔符，不会引入分隔符差异。
    """
    norm_dest = (dest or '').replace('\\', '/').strip('/')
    if norm_dest == '.':
        # 静态 datas 里仓库根目录写作 '.'，归一成空前缀（rel 就是纯文件名）
        norm_dest = ''
    name = os.path.basename((src or '').replace('\\', '/'))
    return norm_dest, (f'{norm_dest}/{name}' if norm_dest else name)


def _under(path, prefix):
    """path 与 prefix 相等，或位于 prefix 子树下（"prefix/" 前缀）。"""
    return path == prefix or path.startswith(prefix + '/')


def strip_unused_datas(datas, label):
    """按上面的规则裁剪 datas，返回 (kept, removed_count, removed_bytes)。

    label 只用于调用方打印统计时标明是哪个 Analysis（GUI/CLI），本函数不打印，
    保持纯函数便于在构建前单独验证、也便于单测（见 tests/test_spec_datas.py）。
    """
    kept = []
    removed_count = 0
    removed_bytes = 0
    for entry in datas:
        # 条目有两种形态：Analysis 之后的 a_*.datas 是 TOC 三元组
        # (dest, src, typecode)；common_datas 里的静态条目是 (src, dest) 二元组。
        # 早期版本统一按二元组解包，会把 TOC 的 dest/src 颠倒，并在写回时把
        # 三元组降级成二元组——COLLECT 的 normalize_toc 随即抛
        # ValueError: not enough values to unpack (expected 3, got 2)。
        if len(entry) >= 3:
            dest, src = entry[0], entry[1]
        else:
            src, dest = entry[0], entry[1]
        norm_dest, rel_path = _dest_rel_path(src, dest)
        name = os.path.basename(rel_path)

        drop = any(_under(norm_dest, rule) or _under(rel_path, rule) for rule in DROP_SUBTREES)
        if not drop and _under(norm_dest, TOKENIZERS_DIR):
            # 只保留 .py 源码逻辑 + 导入期必读的 anthropic_tokenizer.json
            drop = not (name.lower().endswith('.py') or name in TOKENIZERS_KEPT_NAMES)
        if not drop and _under(norm_dest, PYFIGLET_FONTS_DIR):
            # 字体白名单之外的全部剔除
            drop = name not in PYFIGLET_KEPT_FONTS

        if drop:
            removed_count += 1
            try:
                removed_bytes += os.path.getsize(src)
            except OSError:
                # 源文件不可读（已删除/无权限）不影响裁剪决策，仅少统计字节数
                pass
        else:
            # 原样保留 entry：TOC 三元组必须保持三元组，否则 COLLECT 崩
            kept.append(entry)
    return kept, removed_count, removed_bytes