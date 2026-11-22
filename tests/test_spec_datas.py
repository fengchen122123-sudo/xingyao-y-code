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

REQUIRED_TWO_TUPLE_DATAS = [
    ('config.example.json', '.'),
    ('system_prompt.md', '.'),
    ('system_prompt_gui.md', '.'),
    ('system_prompt_secretary_gui.md', '.'),
    ('vendor/ripgrep/rg.exe', 'vendor/ripgrep'),
    ('vendor/mermaid/mermaid-11.17.2.min.js', 'vendor/mermaid'),
    ('frontend/dist', 'frontend/dist'),
    ('frontend-mobile/dist', 'frontend-mobile/dist'),
    ('frontend/mobile-shim.js', 'frontend'),
    ('skills', 'skills'),
    ('plugins', 'plugins'),
    ('assets/ycode.ico', 'assets'),
    ('assets/CHANGELOG.md', 'assets'),
]


class TestRulesSnapshot:

    def test_drop_subtrees_unchanged(self):
        assert DROP_SUBTREES == (
            'litellm/proxy/_experimental/out',
            'litellm/proxy/swagger',
            'litellm/llms/huggingface/huggingface_llms_metadata',
            'playwright/driver/package/types',
            'playwright/driver/package/lib/vite',
        )

    def test_tokenizers_rule_unchanged(self):
        assert TOKENIZERS_DIR == 'litellm/litellm_core_utils/tokenizers'
        assert TOKENIZERS_KEPT_NAMES == frozenset({'anthropic_tokenizer.json'})

    def test_pyfiglet_rule_unchanged(self):
        assert PYFIGLET_FONTS_DIR == 'pyfiglet/fonts'
        assert PYFIGLET_KEPT_FONTS == frozenset({'slant.flf', 'slant_relief.flf'})


class TestKept:

    def test_required_two_tuple_datas_all_kept(self):
        kept, removed_count, removed_bytes = strip_unused_datas(
            list(REQUIRED_TWO_TUPLE_DATAS), 'GUI(Y-code.exe)'
        )
        assert kept == REQUIRED_TWO_TUPLE_DATAS
        assert removed_count == 0
        assert removed_bytes == 0

    def test_repo_root_dest_dot_is_kept(self):
        kept, removed_count, _ = strip_unused_datas([('config.example.json', '.')], 'GUI')
        assert kept == [('config.example.json', '.')]
        assert removed_count == 0

    def test_toc_triple_kept_as_triple(self):
        entry = ('frontend/dist', 'frontend/dist/index.html', 'DATA')
        kept, removed_count, _ = strip_unused_datas([entry], 'GUI')
        assert removed_count == 0
        assert kept == [entry]
        assert len(kept[0]) == 3
        assert kept[0] is entry

    def test_similar_prefix_names_are_kept(self):
        datas = [
            ('litellm/utils.py', 'litellm'),
            ('swagger-ui.txt', 'assets'),
            ('litellm/proxy/swagger_extra/x.js', 'litellm/proxy/swagger_extra'),
            ('litellm/proxyx/swagger/x.js', 'litellm/proxyx/swagger'),
            ('pyfiglet/other/banner.flf', 'pyfiglet/other'),
            ('litellm/other/model.json', 'litellm/other'),
        ]
        kept, removed_count, removed_bytes = strip_unused_datas(datas, 'GUI')
        assert kept == datas
        assert (removed_count, removed_bytes) == (0, 0)


class TestRemoved:

    def test_drop_subtree_exact_match(self):
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
        datas = [('litellm/proxy/swagger', 'litellm/proxy')]
        kept, removed_count, _ = strip_unused_datas(datas, 'GUI')
        assert kept == []
        assert removed_count == 1

    def test_tokenizers_keep_py_and_anthropic_json_drop_rest(self):
        datas = [
            (TOKENIZERS_DIR + '/__init__.py', TOKENIZERS_DIR),
            (TOKENIZERS_DIR + '/anthropic_tokenizer.json', TOKENIZERS_DIR),
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
        datas = [
            (TOKENIZERS_DIR + '/ANTHROPIC_TOKENIZER.JSON', TOKENIZERS_DIR),
            (PYFIGLET_FONTS_DIR + '/SLANT.FLF', PYFIGLET_FONTS_DIR),
        ]
        kept, removed_count, _ = strip_unused_datas(datas, 'GUI')
        assert kept == []
        assert removed_count == 2

    def test_py_suffix_check_is_case_insensitive(self):
        datas = [(TOKENIZERS_DIR + '/MODULE.PY', TOKENIZERS_DIR)]
        kept, removed_count, _ = strip_unused_datas(datas, 'GUI')
        assert kept == datas
        assert removed_count == 0


class TestReturnStats:

    def test_counts_and_bytes(self, tmp_path):
        keep_a = tmp_path / 'keep_a.txt'
        keep_a.write_bytes(b'x' * 5)
        keep_b = tmp_path / 'keep_b.txt'
        keep_b.write_bytes(b'y' * 3)
        drop_a = tmp_path / 'drop_a.js'
        drop_a.write_bytes(b'z' * 11)
        drop_b = tmp_path / 'drop_b.flf'
        drop_b.write_bytes(b'w' * 7)

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
        src = tmp_path / 'payload.bin'
        src.write_bytes(b'q' * 13)
        entry = ('litellm/proxy/swagger', str(src), 'DATA')
        kept, removed_count, removed_bytes = strip_unused_datas([entry], 'GUI')
        assert kept == []
        assert removed_count == 1
        assert removed_bytes == 13

    def test_missing_source_counts_but_adds_no_bytes(self):
        datas = [('litellm/proxy/swagger/definitely-missing.js', 'litellm/proxy/swagger')]
        kept, removed_count, removed_bytes = strip_unused_datas(datas, 'GUI')
        assert kept == []
        assert removed_count == 1
        assert removed_bytes == 0


class TestBoundaries:

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
        entry = ('litellm/proxy/swagger/index.html', 'LiteLLM/Proxy/Swagger')
        kept, removed_count, _ = strip_unused_datas([entry], 'GUI')
        assert kept == [entry]
        assert removed_count == 0

    def test_src_path_only_uses_basename(self):
        datas = [
            (r'C:\venv\Lib\site-packages\pyfiglet\fonts\slant.flf', 'pyfiglet/fonts'),
            (r'C:\venv\Lib\site-packages\pyfiglet\fonts\banner.flf', 'pyfiglet/fonts'),
        ]
        kept, removed_count, _ = strip_unused_datas(datas, 'GUI')
        assert kept == datas[:1]
        assert removed_count == 1


class TestBloatPruning:

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
            (r'webview\lib\runtimes\win-arm64\native\WebView2Loader.dll', s, 'DATA'),
            (r'webview\lib\runtimes\win-x64\native\WebView2Loader.dll', s, 'DATA'),
            (r'webview\lib\runtimes\win-x86\native\WebView2Loader.dll', s, 'DATA'),
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

    def test_strip_unused_binaries_drops_avif_win32ui_and_keeps_webview_runtimes(self, tmp_path):
        src_file = tmp_path / 'mod.pyd'
        src_file.write_bytes(b'y' * 250)
        s = str(src_file)

        dropped = [
            (r'PIL\_avif.cp314-win_amd64.pyd', s, 'EXTENSION'),
            (r'pythonwin\win32ui.pyd', s, 'EXTENSION'),
            (r'mfc140u.dll', s, 'BINARY'),
            (r'webview\lib\WebBrowserInterop.x86.dll', s, 'BINARY'),
        ]
        kept_expected = [
            (r'PIL\_imaging.cp314-win_amd64.pyd', s, 'EXTENSION'),
            (r'PIL\_webp.cp314-win_amd64.pyd', s, 'EXTENSION'),
            (r'win32\win32api.pyd', s, 'EXTENSION'),
            (r'pywin32_system32\pythoncom314.dll', s, 'BINARY'),
            (r'webview\lib\runtimes\win-arm64\native\WebView2Loader.dll', s, 'BINARY'),
            (r'webview\lib\runtimes\win-x64\native\WebView2Loader.dll', s, 'BINARY'),
            (r'webview\lib\runtimes\win-x86\native\WebView2Loader.dll', s, 'BINARY'),
            (r'webview\lib\WebBrowserInterop.x64.dll', s, 'BINARY'),
        ]

        kept, removed_count, removed_bytes = strip_unused_binaries(dropped + kept_expected, 'GUI')
        assert kept == kept_expected
        assert removed_count == len(dropped)
        assert removed_bytes == len(dropped) * 250

    def test_real_pywebview_runtimes_survive_pruning(self):
        import pytest

        hooks = pytest.importorskip('PyInstaller.utils.hooks')
        datas = hooks.collect_data_files('webview', subdir='lib')
        binaries = hooks.collect_dynamic_libs('webview')

        kept_datas, _, _ = strip_bloat_datas(datas, 'GUI')
        kept_bins, _, _ = strip_unused_binaries(binaries, 'GUI')

        kept_rel_paths = set()
        for entry in kept_datas + kept_bins:
            src, dest = entry[0], entry[1]
            norm_dest = dest.replace('\\', '/').strip('/')
            name = Path(src).name
            kept_rel_paths.add(f'{norm_dest}/{name}'.lower() if norm_dest else name.lower())

        for arch in ('win-arm64', 'win-x64', 'win-x86'):
            assert f'webview/lib/runtimes/{arch}/native/webview2loader.dll' in kept_rel_paths
        assert 'webview/lib/microsoft.web.webview2.core.dll' in kept_rel_paths
        assert 'webview/lib/microsoft.web.webview2.winforms.dll' in kept_rel_paths
        assert 'webview/lib/webbrowserinterop.x64.dll' in kept_rel_paths

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
        root = Path(__file__).resolve().parent.parent
        assert (root / 'assets' / 'ycode-logo-168.png').is_file()
        for rel in ('ycode_uninstall.py', 'ycode.spec', 'ycode_uninstall.spec'):
            text = (root / rel).read_text(encoding='utf-8')
            assert 'ycode-logo-168.png' in text
            assert 'ycode.png' not in text

    def test_ycode_spec_bundles_secretary_prompt_and_win32gui(self):
        root = Path(__file__).resolve().parent.parent
        spec_text = (root / 'ycode.spec').read_text(encoding='utf-8')
        assert "('system_prompt_secretary_gui.md', '.')" in spec_text
        assert "'win32gui'" in spec_text
        setup_spec_text = (root / 'ycode_setup.spec').read_text(encoding='utf-8')
        assert "('system_prompt_secretary_gui.md', '.')" in setup_spec_text