"""安装包 datas 裁剪规则的单元测试（原 ycode.spec 内 `_strip_unused_datas`）。

背景：ycode.spec 是 PyInstaller 的 spec 文件，由 PyInstaller 用 exec 执行，
不是可 import 的普通模块，所以「哪些资源会被删出安装包」的逻辑长期没有测试。
删错一项的后果是打包版运行时崩溃（缺内置技能、缺前端静态资源、缺第三方库
导入期必读的表），因此这里把逻辑搬到 packaging_datas.py 并用测试锁住：

1. 主程序必需的 datas 必须原样保留（含 TOC 三元组的形态）；
2. 命中排除规则的条目必须被删；
3. 返回值 (kept, removed_count, removed_bytes) 的计数与字节数必须正确；
4. 边界情况（空列表、全部命中、分隔符 / 大小写差异）按现有规则的实际行为。

另外用快照断言把规则常量本身固定住——本次重构只搬移、不得改规则。
"""

from pathlib import Path

from packaging_datas import (
    DROP_SUBTREES,
    PYFIGLET_FONTS_DIR,
    PYFIGLET_KEPT_FONTS,
    TOKENIZERS_DIR,
    TOKENIZERS_KEPT_NAMES,
    filter_hidden_imports,
    strip_bloat_datas,
    strip_unused_binaries,
    strip_unused_datas,
)

# 抽自 ycode.spec 的 common_datas：主程序运行期必需，裁剪后必须一项不少。
REQUIRED_TWO_TUPLE_DATAS = [
    ('config.example.json', '.'),
    ('system_prompt.md', '.'),
    ('system_prompt_gui.md', '.'),
    ('vendor/ripgrep/rg.exe', 'vendor/ripgrep'),
    ('vendor/mermaid/mermaid-11.17.2.min.js', 'vendor/mermaid'),
    ('frontend/dist', 'frontend/dist'),
    ('frontend-mobile/dist', 'frontend-mobile/dist'),
    ('frontend/mobile-shim.js', 'frontend'),
    ('skills', 'skills'),
    ('plugins', 'plugins'),
    ('assets/ycode.ico', 'assets'),
    ('assets/CHANGELOG.md', 'assets'),
    # 注：这里刻意不再有 vendor/ffmpeg/*。ffmpeg 已改成按需组件（运行期由
    # core/components.py 的 FFMPEG_SPEC 下载到 ~/.ycode/components/ffmpeg/），
    # ycode.spec 不再把它追加进 common_datas，安装包体积因此少 ~35MB。
    # 仓库里的 vendor/ffmpeg 文件保留，仍受 scripts/verify_vendor.py 校验。
]


class TestRulesSnapshot:
    """规则快照：搬移只允许换位置，不允许顺手改规则。"""

    def test_drop_subtrees_unchanged(self):
        assert DROP_SUBTREES == (
            'litellm/proxy/_experimental/out',
            'litellm/proxy/swagger',
            'litellm/llms/huggingface/huggingface_llms_metadata',
            'playwright/driver/package/types',
            'playwright/driver/package/lib/vite',
        )

    def test_tokenizers_rule_unchanged(self):
        # anthropic_tokenizer.json 被 litellm/utils.py 在导入期读取，必须留在白名单里
        assert TOKENIZERS_DIR == 'litellm/litellm_core_utils/tokenizers'
        assert TOKENIZERS_KEPT_NAMES == frozenset({'anthropic_tokenizer.json'})

    def test_pyfiglet_rule_unchanged(self):
        assert PYFIGLET_FONTS_DIR == 'pyfiglet/fonts'
        assert PYFIGLET_KEPT_FONTS == frozenset({'slant.flf', 'slant_relief.flf'})


class TestKept:
    """保留项：主程序必需的 datas 原样保留。"""

    def test_required_two_tuple_datas_all_kept(self):
        kept, removed_count, removed_bytes = strip_unused_datas(
            list(REQUIRED_TWO_TUPLE_DATAS), 'GUI(Y-code.exe)'
        )
        assert kept == REQUIRED_TWO_TUPLE_DATAS
        assert removed_count == 0
        assert removed_bytes == 0

    def test_repo_root_dest_dot_is_kept(self):
        # ('config.example.json', '.') —— dest 为 '.' 表示包根，归一化后不能误判
        kept, removed_count, _ = strip_unused_datas([('config.example.json', '.')], 'GUI')
        assert kept == [('config.example.json', '.')]
        assert removed_count == 0

    def test_toc_triple_kept_as_triple(self):
        """TOC 三元组必须原样（仍是三元组），降级成二元组会让 COLLECT 崩。"""
        entry = ('frontend/dist', 'frontend/dist/index.html', 'DATA')
        kept, removed_count, _ = strip_unused_datas([entry], 'GUI')
        assert removed_count == 0
        assert kept == [entry]
        assert len(kept[0]) == 3
        assert kept[0] is entry

    def test_similar_prefix_names_are_kept(self):
        """子串相似但不在规则子树下的路径不能被误删。"""
        datas = [
            # 名字里带 swagger，但不在 litellm/proxy/swagger 子树下
            ('litellm/utils.py', 'litellm'),
            ('swagger-ui.txt', 'assets'),
            # 前缀相同但不是 "<规则>/" 子树（前缀边界必须按 '/' 判断）
            ('litellm/proxy/swagger_extra/x.js', 'litellm/proxy/swagger_extra'),
            ('litellm/proxyx/swagger/x.js', 'litellm/proxyx/swagger'),
            # 非 fonts 目录下的 .flf 不受 pyfiglet 规则影响
            ('pyfiglet/other/banner.flf', 'pyfiglet/other'),
            # 非 tokenizers 目录下的 json 不受 tokenizer 规则影响
            ('litellm/other/model.json', 'litellm/other'),
        ]
        kept, removed_count, removed_bytes = strip_unused_datas(datas, 'GUI')
        assert kept == datas
        assert (removed_count, removed_bytes) == (0, 0)


class TestRemoved:
    """命中排除规则的条目被删。"""

    def test_drop_subtree_exact_match(self):
        # litellm/proxy/swagger 本身作为 dest（子树根）也要整段删
        datas = [('litellm/proxy/swagger/index.html', 'litellm/proxy/swagger')]
        kept, removed_count, _ = strip_unused_datas(datas, 'GUI')
        assert kept == []
        assert removed_count == 1

    def test_drop_subtree_children(self):
        datas = [
            ('litellm/proxy/swagger/swagger-ui.css', 'litellm/proxy/swagger'),
            ('litellm/proxy/_experimental/out/index.txt', 'litellm/proxy/_experimental/out'),
            (
                'litellm/llms/huggingface/huggingface_llms_metadata/key.bin',
                'litellm/llms/huggingface/huggingface_llms_metadata',
            ),
            ('playwright/driver/package/types/index.d.ts', 'playwright/driver/package/types'),
            ('playwright/driver/package/lib/vite/index.js', 'playwright/driver/package/lib/vite'),
        ]
        kept, removed_count, _ = strip_unused_datas(datas, 'GUI')
        assert kept == []
        assert removed_count == len(datas)

    def test_drop_pair_matched_via_dest_and_basename(self):
        """dest 是父目录、由 basename 拼出的完整相对路径命中规则，也要删。

        对应实现里 `_under(rel_path, rule)` 那条判断分支。
        """
        datas = [('litellm/proxy/swagger', 'litellm/proxy')]
        kept, removed_count, _ = strip_unused_datas(datas, 'GUI')
        assert kept == []
        assert removed_count == 1

    def test_tokenizers_keep_py_and_anthropic_json_drop_rest(self):
        datas = [
            (TOKENIZERS_DIR + '/__init__.py', TOKENIZERS_DIR),
            (TOKENIZERS_DIR + '/anthropic_tokenizer.json', TOKENIZERS_DIR),
            # 9b5ad71b2ce5302211f9c61530b329a4922fc6a4 这类哈希命名的二进制分词表
            (TOKENIZERS_DIR + '/9b5ad71b2ce5302211f9c61530b329a4922fc6a4', TOKENIZERS_DIR),
            (TOKENIZERS_DIR + '/fb374d419588a4632f3f557e76b4b70aebbca790', TOKENIZERS_DIR),
        ]
        kept, removed_count, _ = strip_unused_datas(datas, 'GUI')
        assert kept == datas[:2]
        assert removed_count == 2

    def test_pyfiglet_keeps_whitelist_only(self):
        datas = [
            (PYFIGLET_FONTS_DIR + '/slant.flf', PYFIGLET_FONTS_DIR),
            (PYFIGLET_FONTS_DIR + '/slant_relief.flf', PYFIGLET_FONTS_DIR),
            (PYFIGLET_FONTS_DIR + '/banner.flf', PYFIGLET_FONTS_DIR),
            (PYFIGLET_FONTS_DIR + '/big.flf', PYFIGLET_FONTS_DIR),
        ]
        kept, removed_count, _ = strip_unused_datas(datas, 'GUI')
        assert kept == datas[:2]
        assert removed_count == 2

    def test_case_is_sensitive_for_keep_lists(self):
        """白名单匹配是大小写敏感的（保持原实现行为）。"""
        datas = [
            (TOKENIZERS_DIR + '/ANTHROPIC_TOKENIZER.JSON', TOKENIZERS_DIR),
            (PYFIGLET_FONTS_DIR + '/SLANT.FLF', PYFIGLET_FONTS_DIR),
        ]
        kept, removed_count, _ = strip_unused_datas(datas, 'GUI')
        assert kept == []
        assert removed_count == 2

    def test_py_suffix_check_is_case_insensitive(self):
        """`.py` 后缀判断走 lower()，所以 .PY 也保留（保持原实现行为）。"""
        datas = [(TOKENIZERS_DIR + '/MODULE.PY', TOKENIZERS_DIR)]
        kept, removed_count, _ = strip_unused_datas(datas, 'GUI')
        assert kept == datas
        assert removed_count == 0


class TestReturnStats:
    """返回值 (kept, removed_count, removed_bytes) 的计数与字节数。"""

    def test_counts_and_bytes(self, tmp_path):
        keep_a = tmp_path / 'keep_a.txt'
        keep_a.write_bytes(b'x' * 5)
        keep_b = tmp_path / 'keep_b.txt'
        keep_b.write_bytes(b'y' * 3)
        drop_a = tmp_path / 'drop_a.js'
        drop_a.write_bytes(b'z' * 11)  # 落在 litellm/proxy/swagger 下
        drop_b = tmp_path / 'drop_b.flf'
        drop_b.write_bytes(b'w' * 7)  # 落在 pyfiglet/fonts 下且不在白名单

        datas = [
            (str(keep_a), 'frontend/dist'),
            (str(drop_a), 'litellm/proxy/swagger'),
            (str(keep_b), 'skills'),
            (str(drop_b), 'pyfiglet/fonts'),
        ]
        kept, removed_count, removed_bytes = strip_unused_datas(datas, 'GUI')
        assert kept == [(str(keep_a), 'frontend/dist'), (str(keep_b), 'skills')]
        assert removed_count == 2
        assert removed_bytes == 11 + 7

    def test_toc_triple_bytes_counted_from_src(self, tmp_path):
        """TOC 三元组是 (dest, src, typecode)，取 sizes 用的必须是 src 而不是 dest。"""
        src = tmp_path / 'payload.bin'
        src.write_bytes(b'q' * 13)
        entry = ('litellm/proxy/swagger', str(src), 'DATA')
        kept, removed_count, removed_bytes = strip_unused_datas([entry], 'GUI')
        assert kept == []
        assert removed_count == 1
        assert removed_bytes == 13

    def test_missing_source_counts_but_adds_no_bytes(self):
        """源文件不可读（已删除 / 无权限）只影响字节统计，不影响裁剪决策。"""
        datas = [('litellm/proxy/swagger/definitely-missing.js', 'litellm/proxy/swagger')]
        kept, removed_count, removed_bytes = strip_unused_datas(datas, 'GUI')
        assert kept == []
        assert removed_count == 1
        assert removed_bytes == 0


class TestBoundaries:
    """边界情况。"""

    def test_empty_datas(self):
        assert strip_unused_datas([], 'GUI') == ([], 0, 0)

    def test_all_dropped(self):
        datas = [
            ('litellm/proxy/swagger/a.js', 'litellm/proxy/swagger'),
            ('litellm/proxy/swagger/b.css', 'litellm/proxy/swagger'),
            (PYFIGLET_FONTS_DIR + '/c.flf', PYFIGLET_FONTS_DIR),
        ]
        kept, removed_count, _ = strip_unused_datas(datas, 'GUI')
        assert kept == []
        assert removed_count == 3

    def test_input_list_not_mutated(self):
        datas = [
            ('frontend/dist', 'frontend/dist'),
            ('litellm/proxy/swagger/a.js', 'litellm/proxy/swagger'),
        ]
        snapshot = list(datas)
        strip_unused_datas(datas, 'GUI')
        assert datas == snapshot

    def test_backslash_dest_is_normalized_and_dropped(self):
        """Windows 上 PyInstaller 生成的 dest 是反斜杠形式，必须照样命中规则。"""
        datas = [('litellm/proxy/swagger/index.html', r'litellm\proxy\swagger')]
        kept, removed_count, _ = strip_unused_datas(datas, 'GUI')
        assert kept == []
        assert removed_count == 1

    def test_trailing_and_leading_slash_dest_is_normalized(self):
        datas = [('litellm/proxy/swagger/index.html', '/litellm/proxy/swagger/')]
        kept, removed_count, _ = strip_unused_datas(datas, 'GUI')
        assert kept == []
        assert removed_count == 1

    def test_uppercase_dest_is_not_dropped(self):
        """规则匹配是大小写敏感的（保持原实现行为）：大写路径不会被删。"""
        entry = ('litellm/proxy/swagger/index.html', 'LiteLLM/Proxy/Swagger')
        kept, removed_count, _ = strip_unused_datas([entry], 'GUI')
        assert kept == [entry]
        assert removed_count == 0

    def test_src_path_only_uses_basename(self):
        """src 的盘符 / 目录分隔符差异不影响判定：只取 basename 参与拼接。"""
        datas = [
            (r'C:\venv\Lib\site-packages\pyfiglet\fonts\slant.flf', 'pyfiglet/fonts'),
            (r'C:\venv\Lib\site-packages\pyfiglet\fonts\banner.flf', 'pyfiglet/fonts'),
        ]
        kept, removed_count, _ = strip_unused_datas(datas, 'GUI')
        assert kept == datas[:1]
        assert removed_count == 1


class TestBloatPruning:
    """零功能损失冗余裁剪规则（strip_bloat_datas / strip_unused_binaries / filter_hidden_imports）。"""

    def test_strip_bloat_datas_drops_redundant_and_keeps_runtime_assets(self, tmp_path):
        src_file = tmp_path / 'sample.bin'
        src_file.write_bytes(b'x' * 100)
        s = str(src_file)

        dropped = [
            (r'reportlab\fonts\DarkGarden.sfd', s, 'DATA'),
            (r'reportlab\graphics\charts\axes.py', s, 'DATA'),
            (r'reportlab\platypus\paragraph.py', s, 'DATA'),
            (r'reportlab\pdfgen\canvas.py', s, 'DATA'),
            (r'openpyxl\__init__.py', s, 'DATA'),
            (r'pypdf\_reader.py', s, 'DATA'),
            (r'_tcl_data\tzdata\Asia\Shanghai', s, 'DATA'),
            (r'_tk_data\images\logo.eps', s, 'DATA'),
            (r'_tk_data\demos\widget', s, 'DATA'),
            (r'tcl8\8.5\tcltest-2.5.8.tm', s, 'DATA'),
            (r'winpty\tests\test_pty.py', s, 'DATA'),
            (r'webview\lib\runtimes\win-arm64\native\WebView2Loader.dll', s, 'DATA'),
            (r'webview\lib\runtimes\win-x86\native\WebView2Loader.dll', s, 'DATA'),
            (r'webview\lib\pywebview-android.jar', s, 'DATA'),
            (r'webview\lib\WebBrowserInterop.x86.dll', s, 'DATA'),
            (r'pythonnet\runtime\Python.Runtime.xml', s, 'DATA'),
            (r'plugins\office\__pycache__\plugin.cpython-314.pyc', s, 'DATA'),
            (r'relay_server\__pycache__\app.cpython-314.pyc', s, 'DATA'),
        ]
        kept_expected = [
            (r'pypdf\py.typed', s, 'DATA'),
            (r'_tcl_data\encoding\cp936.enc', s, 'DATA'),
            (r'_tk_data\tk.tcl', s, 'DATA'),
            (r'winpty\winpty.dll', s, 'DATA'),
            (r'webview\lib\runtimes\win-x64\native\WebView2Loader.dll', s, 'DATA'),
            (r'webview\lib\WebBrowserInterop.x64.dll', s, 'DATA'),
            (r'webview\js\api.js', s, 'DATA'),
            (r'pythonnet\runtime\Python.Runtime.dll', s, 'DATA'),
            (r'plugins\office\plugin.py', s, 'DATA'),
            (r'relay_server\app.py', s, 'DATA'),
            (r'skills\baoyu-post-to-wechat\scripts\wechat-api.ts', s, 'DATA'),
        ]

        kept, removed_count, removed_bytes = strip_bloat_datas(dropped + kept_expected, 'GUI')
        assert kept == kept_expected
        assert removed_count == len(dropped)
        assert removed_bytes == len(dropped) * 100

    def test_strip_unused_binaries_drops_avif_win32ui_and_non_x64_webview(self, tmp_path):
        src_file = tmp_path / 'mod.pyd'
        src_file.write_bytes(b'y' * 250)
        s = str(src_file)

        dropped = [
            (r'PIL\_avif.cp314-win_amd64.pyd', s, 'EXTENSION'),
            (r'pythonwin\win32ui.pyd', s, 'EXTENSION'),
            (r'mfc140u.dll', s, 'BINARY'),
            (r'webview\lib\runtimes\win-x86\native\WebView2Loader.dll', s, 'BINARY'),
            (r'webview\lib\WebBrowserInterop.x86.dll', s, 'BINARY'),
        ]
        kept_expected = [
            (r'PIL\_imaging.cp314-win_amd64.pyd', s, 'EXTENSION'),
            (r'PIL\_webp.cp314-win_amd64.pyd', s, 'EXTENSION'),
            (r'win32\win32api.pyd', s, 'EXTENSION'),
            (r'pywin32_system32\pythoncom314.dll', s, 'BINARY'),
            (r'webview\lib\runtimes\win-x64\native\WebView2Loader.dll', s, 'BINARY'),
            (r'webview\lib\WebBrowserInterop.x64.dll', s, 'BINARY'),
        ]

        kept, removed_count, removed_bytes = strip_unused_binaries(dropped + kept_expected, 'GUI')
        assert kept == kept_expected
        assert removed_count == len(dropped)
        assert removed_bytes == len(dropped) * 250

    def test_filter_hidden_imports_removes_excluded_subtrees(self):
        mods = [
            'reportlab.pdfgen.canvas',
            'reportlab.pdfbase.ttfonts',
            'reportlab.graphics',
            'reportlab.graphics.charts.axes',
            'reportlab.platypus.paragraph',
            'winpty.ptyprocess',
            'winpty.tests',
            'winpty.tests.test_pty',
        ]
        assert filter_hidden_imports(mods, ('reportlab.graphics', 'reportlab.platypus', 'winpty.tests')) == [
            'reportlab.pdfgen.canvas',
            'reportlab.pdfbase.ttfonts',
            'winpty.ptyprocess',
        ]

    def test_uninstaller_reuses_prescaled_logo_168(self):
        """卸载器与安装器共用 assets/ycode-logo-168.png，不再携带 1MB+ ycode.png。"""
        root = Path(__file__).resolve().parent.parent
        assert (root / 'assets' / 'ycode-logo-168.png').is_file()
        for rel in ('ycode_uninstall.py', 'ycode.spec', 'ycode_uninstall.spec'):
            text = (root / rel).read_text(encoding='utf-8')
            assert 'ycode-logo-168.png' in text
            assert 'ycode.png' not in text