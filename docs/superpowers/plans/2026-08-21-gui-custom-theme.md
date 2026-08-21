# GUI Custom Theme Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 Y-CODE GUI 提供可视化、分区化、可导入导出的声明式自定义主题系统，同时保持 CLI 主题行为不变。

**Architecture:** Python `core.gui_theme` 是主题 Schema、持久化和资源安全的唯一事实源，`gui_bridge.ApiBridge` 只暴露窄接口并负责原生对话框/DWM。Vue 通过类型化主题快照和独立运行时把已校验令牌映射为预定义 CSS 变量，设置页只编辑前端草稿，保存成功后才更新活动主题。

**Tech Stack:** Python 3.12、Pillow、zipfile、PyWebView、Windows DWM、Vue 3、Pinia、TypeScript、Vitest、pytest。

**Spec:** `docs/superpowers/specs/2026-08-21-gui-custom-theme-design.md`

## Global Constraints

- 只修改 GUI 主题；不得改变 `core/themes.py`、CLI `/theme`、`config.theme` 或 prompt_toolkit 样式。
- 用户主题根目录固定为 `config.YCODE_DIR/gui-themes/`；不得把用户资源写入安装目录或 PyInstaller `datas`。
- 第一版只接受 Schema v1 的声明式令牌、PNG/JPEG/WebP；不得加载任意 CSS、JavaScript、HTML、GIF、视频或远程 URL。
- 单图上限 10 MiB、8192×8192；单主题解压后总量上限 30 MiB。
- 主题 ID 只允许 `[a-z0-9][a-z0-9-]{0,63}`；用户主题只允许继承 `builtin.dark` 或 `builtin.light`。
- 未知字段、非法颜色、路径逃逸、符号链接和不支持 Schema 必须显式失败，不得静默丢弃、猜测或降级。
- 现有 `gui_theme_light` 只做一次确定性兼容迁移；新真值为 `gui_theme_id`。
- 主题错误不得自动改写活动主题；只有用户显式重置才能切换到内置主题。
- 所有 JSON/配置写入必须使用临时文件和原子替换；SQLite 不在本功能范围。
- GUI 线程安全规则保持不变：事件线程不得直接 `evaluate_js`；本功能不新增绕过 `gui-js-dispatcher` 的调用。

## File Structure

- `core/gui_theme.py`：Schema v1、内置主题、校验、继承、主题仓库、图片和 ZIP 资源操作。
- `tests/test_gui_theme_manager.py`：领域、资源安全、原子性和迁移测试。
- `gui_bridge.py`：主题 CRUD、原生文件对话框、资源桥和 DWM 颜色应用接口。
- `gui_main.py`：启动前解析活动主题并传递首屏背景/appearance。
- `tests/test_gui_theme.py`：桥接契约、DWM 和旧接口兼容测试。
- `frontend/src/themes/types.ts`：前后端主题契约。
- `frontend/src/themes/runtime.ts`：CSS 变量映射、Blob URL 获取/释放和首屏应用。
- `frontend/src/themes/runtime.test.ts`：纯运行时测试。
- `frontend/src/stores/appStore.ts`：活动主题、保存快照、草稿预览和后端动作。
- `frontend/src/stores/appStore.guiTheme.test.ts`：Pinia 主题状态机测试。
- `frontend/src/components/GuiThemeSettings.vue`：主题库与编辑流程编排。
- `frontend/src/components/theme/ThemeRegionEditor.vue`：分区字段、颜色和图片编辑。
- `frontend/src/components/GuiThemeSettings.test.ts`：主题设置组件行为测试。
- `frontend/src/components/SettingsPage.vue`：新增“外观与主题”导航并挂载独立组件。
- `frontend/src/App.vue`、`Topbar.vue`、`Sidebar.vue`、`InputArea.vue`、`RightPanel.vue`、`style.css`：标注主题分区并消费语义 CSS 变量。
- `frontend/src/types/pywebview.d.ts`：主题桥接 API 类型。
- `frontend/src/main.ts`：Vue 挂载前恢复首屏 appearance/background。
- `frontend/src/components/SettingsPage.test.ts`：设置页导航集成测试。
- `docs/all_features.md`、`assets/CHANGELOG.md`：用户功能和版本变更说明。

---

### Task 1: Theme Schema, Built-ins, Persistence, and Migration

**Files:**
- Create: `core/gui_theme.py`
- Create: `tests/test_gui_theme_manager.py`
- Modify: `tests/test_gui_theme.py`

**Interfaces:**
- Produces: `ThemeError(code: str, path: str, message: str)`.
- Produces: `ThemeRepository(root: Path, config_file: Path)`.
- Produces: `ThemeRepository.list_themes() -> list[dict[str, Any]]`.
- Produces: `ThemeRepository.get_theme(theme_id: str) -> dict[str, Any]`，返回已合并完整快照。
- Produces: `ThemeRepository.save_theme(theme: Mapping[str, Any]) -> dict[str, Any]`.
- Produces: `ThemeRepository.set_active(theme_id: str) -> dict[str, Any]`.
- Produces: `ThemeRepository.get_active() -> dict[str, Any]`.
- Produces: `get_repository() -> ThemeRepository`，生产环境使用 `config.YCODE_DIR/gui-themes` 与 `config._CONFIG_FILE`。

Add these shared helpers at the top of `tests/test_gui_theme_manager.py`; later tasks reuse them from the same file:

```python
def make_repo(tmp_path, config_data=None):
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps(config_data or {}), encoding="utf-8")
    return ThemeRepository(tmp_path / "gui-themes", config_file)


def minimal_theme(theme_id, accent=None):
    tokens = {} if accent is None else {"accent": accent}
    return {
        "schema_version": 1,
        "id": theme_id,
        "name": theme_id,
        "appearance": "light",
        "extends": "builtin.light",
        "tokens": tokens,
        "native_titlebar": {},
        "regions": {},
    }
```

- [ ] **Step 1: Write failing Schema and inheritance tests**

```python
def test_builtin_dark_is_complete():
    repo = ThemeRepository(tmp_path / "gui-themes", tmp_path / "config.json")
    theme = repo.get_theme("builtin.dark")
    assert theme["id"] == "builtin.dark"
    assert set(theme["regions"]) == {
        "app_header", "sidebar", "conversation_toolbar", "workspace",
        "composer", "right_panel", "statusbar",
    }
    assert theme["native_titlebar"]["background"] == "#20201e"


def test_user_theme_inherits_only_missing_fields(tmp_path):
    repo = make_repo(tmp_path)
    repo.save_theme(minimal_theme("rose", accent="#d96b9f"))
    theme = repo.get_theme("rose")
    assert theme["tokens"]["accent"] == "#d96b9f"
    assert theme["tokens"]["text_primary"] == repo.get_theme("builtin.light")["tokens"]["text_primary"]


@pytest.mark.parametrize("field,value,code", [
    ("id", "../escape", "invalid_id"),
    ("schema_version", 2, "unsupported_schema"),
    ("extends", "rose-parent", "invalid_base"),
])
def test_invalid_manifest_is_rejected(tmp_path, field, value, code):
    repo = make_repo(tmp_path)
    manifest = minimal_theme("rose")
    manifest[field] = value
    with pytest.raises(ThemeError) as exc:
        repo.save_theme(manifest)
    assert exc.value.code == code
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `venv\Scripts\python -m pytest tests/test_gui_theme_manager.py -q`

Expected: collection fails with `ModuleNotFoundError: No module named 'core.gui_theme'`.

- [ ] **Step 3: Implement exact v1 constants, validation, and inheritance**

```python
SCHEMA_VERSION = 1
THEME_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
BUILTIN_IDS = ("builtin.dark", "builtin.light")
REGION_IDS = (
    "app_header", "sidebar", "conversation_toolbar", "workspace",
    "composer", "right_panel", "statusbar",
)
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}(?:[0-9a-fA-F]{2})?$")


class ThemeError(ValueError):
    def __init__(self, code: str, path: str, message: str):
        super().__init__(message)
        self.code = code
        self.path = path
        self.message = message

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "path": self.path, "message": self.message}
```

Implement `BUILTIN_THEMES` by transcribing the current dark/light values from `frontend/src/style.css`, define exact allowed keys for the root, tokens, native titlebar, and regions, and merge user overrides onto a deep copy of the selected built-in. Reject every unknown key with `ThemeError("unknown_field", field_path, ...)`.

- [ ] **Step 4: Add atomic save, list, active selection, and migration tests**

```python
def test_old_light_flag_migrates_once(tmp_path):
    config_file = tmp_path / "config.json"
    config_file.write_text('{"gui_theme_light": true, "theme": "dracula"}', encoding="utf-8")
    repo = ThemeRepository(tmp_path / "gui-themes", config_file)
    assert repo.get_active()["id"] == "builtin.light"
    saved = json.loads(config_file.read_text(encoding="utf-8"))
    assert saved["gui_theme_id"] == "builtin.light"
    assert saved["theme"] == "dracula"


def test_invalid_active_theme_does_not_rewrite_config(tmp_path):
    repo = make_repo(tmp_path, {"gui_theme_id": "missing-theme"})
    before = repo.config_file.read_bytes()
    with pytest.raises(ThemeError) as exc:
        repo.get_active()
    assert exc.value.code == "theme_not_found"
    assert repo.config_file.read_bytes() == before


def test_failed_save_preserves_previous_manifest(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    repo.save_theme(minimal_theme("rose"))
    before = (repo.root / "rose" / "theme.json").read_bytes()
    monkeypatch.setattr(os, "replace", Mock(side_effect=OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        repo.save_theme({**minimal_theme("rose"), "name": "changed"})
    assert (repo.root / "rose" / "theme.json").read_bytes() == before
```

- [ ] **Step 5: Implement atomic repository operations**

Use `config._atomic_write_json` for config mutation under `config._CONFIG_LOCK`. For `theme.json`, write UTF-8 JSON to a unique sibling temporary file, flush and `os.fsync`, then `os.replace`. `set_active()` must resolve and validate the theme before writing `gui_theme_id`.

- [ ] **Step 6: Run tests and verify GREEN**

Run: `venv\Scripts\python -m pytest tests/test_gui_theme_manager.py tests/test_gui_theme.py -q`

Expected: all focused tests pass; existing light/dark bridge tests still pass before bridge migration.

- [ ] **Step 7: Commit the domain model**

```bash
git add core/gui_theme.py tests/test_gui_theme_manager.py tests/test_gui_theme.py
git commit -m "feat: add GUI theme domain model"
```

### Task 2: Theme Images and ZIP Import/Export Security

**Files:**
- Modify: `core/gui_theme.py`
- Modify: `tests/test_gui_theme_manager.py`

**Interfaces:**
- Consumes: `ThemeRepository`, `ThemeError` from Task 1.
- Produces: `ThemeRepository.import_image(theme_id: str, region: str, source: Path) -> str`，返回 `assets/<sha256-prefix>.<ext>`.
- Produces: `ThemeRepository.remove_image(theme_id: str, region: str) -> dict[str, Any]`.
- Produces: `ThemeRepository.read_asset(theme_id: str, asset_path: str) -> tuple[str, bytes]`.
- Produces: `ThemeRepository.import_zip(source: Path) -> dict[str, Any]`.
- Produces: `ThemeRepository.export_zip(theme_id: str, destination: Path) -> Path`.
- Produces: `ThemeRepository.delete_theme(theme_id: str) -> None`.

Add these helpers to `tests/test_gui_theme_manager.py` before the Task 2 tests:

```python
def make_repo_with_saved_theme(tmp_path, theme_id):
    repo = make_repo(tmp_path)
    repo.save_theme(minimal_theme(theme_id))
    return repo


def write_image(path, image_format, size):
    Image.new("RGB", size, "#d96b9f").save(path, format=image_format)
    return path
```

- [ ] **Step 1: Write failing image validation tests**

```python
def test_import_image_copies_valid_webp_into_theme(tmp_path):
    repo = make_repo_with_saved_theme(tmp_path, "rose")
    source = write_image(tmp_path / "source.webp", "WEBP", (32, 24))
    rel = repo.import_image("rose", "workspace", source)
    assert rel.startswith("assets/") and rel.endswith(".webp")
    assert (repo.root / "rose" / rel).is_file()
    assert repo.get_theme("rose")["regions"]["workspace"]["image"] == rel


@pytest.mark.parametrize("factory,code", [
    (lambda p: p.write_bytes(b"not an image"), "invalid_image"),
    (lambda p: write_image(p, "GIF", (32, 24)), "unsupported_image"),
])
def test_import_image_rejects_bad_content(tmp_path, factory, code):
    repo = make_repo_with_saved_theme(tmp_path, "rose")
    source = tmp_path / "input.bin"
    factory(source)
    with pytest.raises(ThemeError) as exc:
        repo.import_image("rose", "workspace", source)
    assert exc.value.code == code
```

- [ ] **Step 2: Write failing archive/path tests**

```python
def test_read_asset_rejects_escape(tmp_path):
    repo = make_repo_with_saved_theme(tmp_path, "rose")
    with pytest.raises(ThemeError) as exc:
        repo.read_asset("rose", "../config.json")
    assert exc.value.code == "asset_escape"


def test_import_zip_rejects_zip_slip_without_partial_directory(tmp_path):
    repo = make_repo(tmp_path)
    archive = tmp_path / "evil.zip"
    with ZipFile(archive, "w") as zf:
        zf.writestr("theme.json", json.dumps(minimal_theme("evil")))
        zf.writestr("../escaped.txt", "owned")
    with pytest.raises(ThemeError) as exc:
        repo.import_zip(archive)
    assert exc.value.code == "asset_escape"
    assert not (repo.root / "evil").exists()
    assert not (tmp_path / "escaped.txt").exists()


def test_import_zip_rejects_declared_size_over_30_mib(tmp_path):
    repo = make_repo(tmp_path)
    archive = tmp_path / "large.zip"
    with ZipFile(archive, "w", compression=ZIP_STORED) as zf:
        zf.writestr("theme.json", json.dumps(minimal_theme("large")))
        with zf.open("assets/large.bin", "w") as entry:
            chunk = b"0" * (1024 * 1024)
            for _ in range(31):
                entry.write(chunk)
    with pytest.raises(ThemeError) as exc:
        repo.import_zip(archive)
    assert exc.value.code == "theme_too_large"
```

- [ ] **Step 3: Run tests and verify RED**

Run: `venv\Scripts\python -m pytest tests/test_gui_theme_manager.py -k "image or asset or zip or delete" -q`

Expected: failures report missing repository methods.

- [ ] **Step 4: Implement strict image and archive operations**

Use `PIL.Image.open(...); image.verify()` followed by a second decode to inspect `format` and dimensions. Reject files over `10 * 1024 * 1024`, dimensions above 8192, unknown region IDs, symlinks, ZIP encrypted entries, more than 128 entries, total expanded size above `30 * 1024 * 1024`, and compression ratio above 100. Extract into `tempfile.TemporaryDirectory(dir=repo.root.parent)`, validate the complete theme, then atomically rename it into `repo.root`.

`delete_theme()` must reject built-ins and the active theme with `builtin_read_only` or `active_theme_delete`. `export_zip()` writes only the validated manifest and referenced assets.

- [ ] **Step 5: Run focused tests and verify GREEN**

Run: `venv\Scripts\python -m pytest tests/test_gui_theme_manager.py -q`

Expected: all theme manager tests pass.

- [ ] **Step 6: Commit resource security**

```bash
git add core/gui_theme.py tests/test_gui_theme_manager.py
git commit -m "feat: secure GUI theme assets"
```

### Task 3: PyWebView Bridge, DWM Colors, and Startup Theme

**Files:**
- Modify: `gui_bridge.py`
- Modify: `gui_main.py`
- Modify: `tests/test_gui_theme.py`

**Interfaces:**
- Consumes: `get_repository()` and repository methods from Tasks 1–2.
- Produces module alias: `from core.gui_theme import get_repository as get_gui_theme_repository` in both `gui_bridge.py` and `gui_main.py` for explicit dependency injection in tests.
- Produces bridge envelopes shaped as `{"ok": True, ...}` or `{"ok": False, "error": ThemeError.as_dict()}`.
- Produces initial state fields `gui_theme`, `gui_themes`, and `gui_theme_error`.
- Produces URL fragment `theme=<light|dark>&bg=<RRGGBB>`.

Add these doubles to `tests/test_gui_theme.py` before the new tests:

```python
def complete_light_theme():
    return copy.deepcopy(BUILTIN_THEMES["builtin.light"])


class BrokenRepo:
    def list_themes(self):
        raise ThemeError("theme_not_found", "gui_theme_id", "missing")


class RecordingRepo:
    def __init__(self, theme):
        self.theme = theme
        self.calls = []

    def get_theme(self, theme_id):
        self.calls.append(f"get:{theme_id}")
        return self.theme

    def set_active(self, theme_id):
        self.calls.append(f"set_active:{theme_id}")
        return self.theme


class RepoWithActive:
    def __init__(self, theme):
        self.theme = theme

    def get_active(self):
        return self.theme
```

- [ ] **Step 1: Write failing bridge contract tests**

```python
def test_list_gui_themes_returns_structured_error(monkeypatch):
    bridge = _bare_bridge()
    monkeypatch.setattr(gui_bridge, "get_gui_theme_repository", lambda: BrokenRepo())
    result = bridge.list_gui_themes()
    assert result == {
        "ok": False,
        "error": {"code": "theme_not_found", "path": "gui_theme_id", "message": "missing"},
    }


def test_set_active_gui_theme_applies_dwm_before_persisting(monkeypatch):
    bridge = _bare_bridge()
    repo = RecordingRepo(theme=complete_light_theme())
    monkeypatch.setattr(gui_bridge, "get_gui_theme_repository", lambda: repo)
    monkeypatch.setattr(bridge, "_apply_titlebar_colors", Mock(return_value=True))
    result = bridge.set_active_gui_theme("rose")
    assert result["ok"] is True
    assert repo.calls == ["get:rose", "set_active:rose"]


def test_dwm_failure_does_not_change_active_id(monkeypatch):
    bridge = _bare_bridge()
    repo = RecordingRepo(theme=complete_light_theme())
    monkeypatch.setattr(gui_bridge, "get_gui_theme_repository", lambda: repo)
    monkeypatch.setattr(bridge, "_apply_titlebar_colors", Mock(return_value=False))
    result = bridge.set_active_gui_theme("rose")
    assert result["ok"] is False
    assert result["error"]["code"] == "native_titlebar_failed"
    assert repo.calls == ["get:rose"]
```

- [ ] **Step 2: Write failing startup and COLORREF tests**

```python
@pytest.mark.parametrize("color,expected", [
    ("#20201e", 0x001E2020),
    ("#ffffff", 0x00FFFFFF),
    ("#6a92d8", 0x00D8926A),
])
def test_rgb_hex_to_colorref(color, expected):
    assert gui_bridge._rgb_hex_to_colorref(color) == expected


def test_initial_theme_metadata_comes_from_repository(monkeypatch):
    monkeypatch.setattr(gui_main, "get_gui_theme_repository", lambda: RepoWithActive(complete_light_theme()))
    meta = gui_main._initial_gui_theme()
    assert meta == {"appearance": "light", "background": "#f4f4f1"}
```

- [ ] **Step 3: Run focused tests and verify RED**

Run: `venv\Scripts\python -m pytest tests/test_gui_theme.py -q`

Expected: failures report missing new bridge and startup functions.

- [ ] **Step 4: Implement the bridge and startup path**

Replace fixed `_TITLEBAR_COLORS` lookup with `_rgb_hex_to_colorref()` and `_apply_titlebar_colors(native_titlebar) -> bool`. Add repository-backed methods for list/get/save/set/create/delete/import/export/image/read-asset. File dialogs must run only through `self._window.create_file_dialog`; `get_gui_theme_asset()` returns Base64 from validated `read_asset()` bytes.

`get_initial_state()` must include:

```python
{
    "gui_theme": active_theme_or_none,
    "gui_themes": theme_summaries,
    "gui_theme_error": error.as_dict() if error else None,
}
```

`gui_main._initial_gui_theme()` calls the same repository before window creation. When the active theme is invalid, return an explicit startup-error marker in the fragment and keep the existing static splash shell; do not edit `gui_theme_id`.

- [ ] **Step 5: Preserve the old boolean shortcut explicitly**

Implement `set_gui_theme(theme_light: bool)` as a compatibility adapter selecting `builtin.light` or `builtin.dark` through `set_active_gui_theme()`. Keep writing the derived `gui_theme_light` key only for one-release compatibility, while `gui_theme_id` remains authoritative.

- [ ] **Step 6: Run Python tests and verify GREEN**

Run: `venv\Scripts\python -m pytest tests/test_gui_theme.py tests/test_gui_theme_manager.py -q`

Expected: all theme tests pass.

- [ ] **Step 7: Commit bridge and startup support**

```bash
git add gui_bridge.py gui_main.py tests/test_gui_theme.py
git commit -m "feat: expose GUI themes to desktop bridge"
```

### Task 4: Typed Frontend Theme Runtime

**Files:**
- Create: `frontend/src/themes/types.ts`
- Create: `frontend/src/themes/runtime.ts`
- Create: `frontend/src/themes/runtime.test.ts`
- Modify: `frontend/src/types/pywebview.d.ts`
- Modify: `frontend/src/main.ts`

**Interfaces:**
- Consumes bridge `get_gui_theme_asset(themeId, assetPath)`.
- Produces `GuiTheme`, `GuiThemeSummary`, `GuiThemeError`, `ThemeRegionId`, `ThemeDraft` types.
- Produces `applyGuiTheme(theme: GuiTheme, api: PyWebViewAPI) -> Promise<AppliedTheme>`.
- Produces `AppliedTheme.dispose() -> void`.
- Produces `applyBootTheme(hash: string, body: HTMLElement) -> void`.

Define deterministic fixtures in `runtime.test.ts`:

```ts
const regionIds: ThemeRegionId[] = [
  'app_header', 'sidebar', 'conversation_toolbar', 'workspace',
  'composer', 'right_panel', 'statusbar',
];

function theme(): GuiTheme {
  const region = {
    background: '#faf6f8', text: '#2c2529', border: '#e5ccd8', accent: '#d96b9f',
    background_opacity: 1, image: null, image_opacity: 0.2,
    image_size: 'cover' as const, image_position: 'center' as const,
  };
  return {
    schema_version: 1, id: 'rose', name: 'Rose', appearance: 'light',
    extends: 'builtin.light', builtin: false,
    tokens: { accent: '#d96b9f' },
    native_titlebar: { background: '#fff7fb', text: '#2c2529', border: '#e5ccd8', mode: 'light' },
    regions: Object.fromEntries(regionIds.map(id => [id, { ...region }])) as GuiTheme['regions'],
  };
}

function themeWithWorkspaceImage(): GuiTheme {
  const value = theme();
  value.regions.workspace.image = 'assets/workspace.webp';
  return value;
}

function fakeApiWithAsset(): PyWebViewAPI {
  return { get_gui_theme_asset: vi.fn().mockResolvedValue({ ok: true, mime: 'image/webp', base64: 'AA==' }) } as unknown as PyWebViewAPI;
}

function fakeApi(): PyWebViewAPI {
  return fakeApiWithAsset();
}
```

- [ ] **Step 1: Define exact TypeScript contracts**

```ts
export type ThemeRegionId =
  | 'app_header' | 'sidebar' | 'conversation_toolbar' | 'workspace'
  | 'composer' | 'right_panel' | 'statusbar';

export interface ThemeRegion {
  background: string;
  text: string;
  border: string;
  accent: string;
  background_opacity: number;
  image: string | null;
  image_opacity: number;
  image_size: 'cover' | 'contain' | 'auto';
  image_position: 'center' | 'top' | 'bottom' | 'left' | 'right';
}

export interface GuiTheme {
  schema_version: 1;
  id: string;
  name: string;
  appearance: 'light' | 'dark';
  extends: 'builtin.light' | 'builtin.dark';
  builtin: boolean;
  tokens: Record<string, string | number>;
  native_titlebar: { background: string; text: string; border: string; mode: 'light' | 'dark' };
  regions: Record<ThemeRegionId, ThemeRegion>;
}
```

- [ ] **Step 2: Write failing runtime tests**

```ts
it('maps semantic and region tokens without injecting a style element', async () => {
  const api = fakeApi();
  const applied = await applyGuiTheme(theme(), api);
  expect(document.documentElement.style.getPropertyValue('--theme-accent')).toBe('#d96b9f');
  expect(document.documentElement.style.getPropertyValue('--theme-workspace-bg')).toBe('#faf6f8');
  expect(document.querySelector('style[data-user-theme]')).toBeNull();
  applied.dispose();
});

it('creates and revokes one blob URL per validated asset', async () => {
  vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:workspace');
  const revoke = vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => undefined);
  const applied = await applyGuiTheme(themeWithWorkspaceImage(), fakeApiWithAsset());
  expect(document.documentElement.style.getPropertyValue('--theme-workspace-image')).toBe('url("blob:workspace")');
  applied.dispose();
  expect(revoke).toHaveBeenCalledWith('blob:workspace');
});

it('applies boot appearance and validated six-digit background', () => {
  applyBootTheme('#theme=light&bg=f4f4f1', document.body);
  expect(document.body.classList.contains('light')).toBe(true);
  expect(document.body.style.backgroundColor).toBe('rgb(244, 244, 241)');
});
```

- [ ] **Step 3: Run Vitest and verify RED**

Run: `npm test -- --run src/themes/runtime.test.ts`

Working directory: `frontend`

Expected: TypeScript import failures for missing theme modules.

- [ ] **Step 4: Implement runtime mapping and disposal**

Use an explicit constant map from semantic token names and seven region IDs to CSS variable names. Fetch assets, decode Base64 to `Blob`, create URLs, and set only mapped variables with `document.documentElement.style.setProperty`. If any asset fails, dispose every URL created in that attempt and reject the whole apply operation.

- [ ] **Step 5: Extend bridge types and boot application**

Add typed theme methods to `PyWebViewAPI`; replace the current `main.ts` light/dark parser with `applyBootTheme(location.hash, document.body)`. The parser accepts only `theme=light|dark` and `bg=[0-9a-fA-F]{6}`.

- [ ] **Step 6: Run runtime tests and build**

Run: `npm test -- --run src/themes/runtime.test.ts`

Run: `npm run build`

Working directory: `frontend`

Expected: runtime tests and type-check/build pass.

- [ ] **Step 7: Commit frontend runtime**

```bash
git add frontend/src/themes frontend/src/types/pywebview.d.ts frontend/src/main.ts
git commit -m "feat: add typed GUI theme runtime"
```

### Task 5: Pinia Theme State Machine and Startup Error Gate

**Files:**
- Modify: `frontend/src/stores/appStore.ts`
- Create: `frontend/src/stores/appStore.guiTheme.test.ts`
- Create: `frontend/src/components/Topbar.test.ts`
- Modify: `frontend/src/App.vue`

**Interfaces:**
- Consumes: `applyGuiTheme`, `AppliedTheme`, bridge theme APIs.
- Produces store state `guiTheme`, `savedGuiTheme`, `guiThemeDraft`, `guiThemes`, `guiThemeError`, `themeDirty`.
- Produces actions `previewGuiTheme`, `cancelGuiThemePreview`, `saveGuiThemeDraft`, `activateGuiTheme`, `resetInvalidGuiTheme`.

In `appStore.guiTheme.test.ts`, reuse a local `makeTheme(id)` fixture shaped exactly like the `GuiTheme` fixture from Task 4 and install this API before each test:

```ts
const saveTheme = vi.fn().mockResolvedValue({ ok: true });
const setActive = vi.fn().mockImplementation(async (id: string) => ({ ok: true, theme: makeTheme(id) }));

beforeEach(() => {
  setActive.mockClear();
  saveTheme.mockClear();
  (window as any).pywebview = {
    api: {
      save_gui_theme: saveTheme,
      set_active_gui_theme: setActive,
      get_gui_theme_asset: vi.fn(),
      get_initial_state: vi.fn().mockResolvedValue({
        gui_theme: null,
        gui_themes: [],
        gui_theme_error: null,
      }),
    },
  };
});
```

- [ ] **Step 1: Write failing store state-machine tests**

```ts
it('previews a draft without persisting it and cancel restores the saved snapshot', async () => {
  const store = useAppStore();
  store.savedGuiTheme = makeTheme('saved');
  store.guiTheme = makeTheme('saved');
  await store.previewGuiTheme(makeTheme('draft'));
  expect(setActive).not.toHaveBeenCalled();
  await store.cancelGuiThemePreview();
  expect(store.guiTheme?.id).toBe('saved');
  expect(store.themeDirty).toBe(false);
});

it('commits only after backend save and activation both succeed', async () => {
  const store = useAppStore();
  store.guiThemeDraft = makeTheme('rose');
  await store.saveGuiThemeDraft();
  expect(saveTheme).toHaveBeenCalledBefore(setActive);
  expect(store.savedGuiTheme?.id).toBe('rose');
});

it('keeps the blocking error until the user resets explicitly', async () => {
  const store = useAppStore();
  await store.initFromBackend();
  expect(store.guiThemeError?.code).toBe('theme_not_found');
  expect(setActive).not.toHaveBeenCalled();
  await store.resetInvalidGuiTheme('builtin.dark');
  expect(setActive).toHaveBeenCalledWith('builtin.dark');
});
```

- [ ] **Step 2: Run the store test and verify RED**

Run: `npm test -- --run src/stores/appStore.guiTheme.test.ts`

Working directory: `frontend`

Expected: missing state/actions cause failures.

- [ ] **Step 3: Implement store state and actions**

Maintain one `AppliedTheme | null` handle. Every successful preview disposes the previous handle after the new apply completes. Failed preview keeps the previous applied theme and returns the bridge/runtime error. `saveGuiThemeDraft()` must call `save_gui_theme` then `set_active_gui_theme`; only both success responses may replace `savedGuiTheme` and clear dirty state.

- [ ] **Step 4: Add an explicit startup theme error gate**

In `App.vue`, render a blocking `data-test="gui-theme-error"` panel when `store.guiThemeError` exists. It shows the backend message and two buttons calling `resetInvalidGuiTheme('builtin.dark')` and `resetInvalidGuiTheme('builtin.light')`. Do not render the normal workspace behind this panel.

- [ ] **Step 5: Preserve topbar built-in shortcut semantics**

Change `toggleTheme()` to call `activateGuiTheme(themeLight ? 'builtin.dark' : 'builtin.light')`. It may update `themeLight` only after activation succeeds. Remove the `ycode-theme-light` localStorage read/write path entirely; the validated URL fragment and backend initial state are the only startup sources.

- [ ] **Step 6: Run store and existing app tests**

Run: `npm test -- --run src/stores/appStore.guiTheme.test.ts src/stores/appStore.modelSettings.test.ts src/components/Topbar.test.ts`

Working directory: `frontend`

Expected: new and existing store/topbar tests pass.

- [ ] **Step 7: Commit store integration**

```bash
git add frontend/src/stores/appStore.ts frontend/src/stores/appStore.guiTheme.test.ts frontend/src/App.vue
git commit -m "feat: manage GUI theme previews and activation"
```

### Task 6: Visual Theme Editor in Settings

**Files:**
- Create: `frontend/src/components/GuiThemeSettings.vue`
- Create: `frontend/src/components/theme/ThemeRegionEditor.vue`
- Create: `frontend/src/components/GuiThemeSettings.test.ts`
- Modify: `frontend/src/components/SettingsPage.vue`
- Modify: `frontend/src/components/SettingsPage.test.ts`

**Interfaces:**
- Consumes store theme state/actions from Task 5.
- Produces `data-test="appearance-nav"` and `data-test="gui-theme-settings"`.
- Produces region editor events `update:modelValue`, `choose-image`, `remove-image`, `clear-overrides`.

`GuiThemeSettings.test.ts` defines `makeTheme(id, builtin)` with the exact `GuiTheme` shape from Task 4, then defines `builtinLight = () => makeTheme('builtin.light', true)` and `userTheme = () => makeTheme('rose', false)`. It defines `mountThemeSettings(options)` by creating a fresh testing Pinia, assigning `guiTheme`, `savedGuiTheme`, `guiThemeDraft`, `guiThemes`, and `themeDirty`, and spying on the five store actions used below. The helper returns `mount(GuiThemeSettings, { global: { plugins: [pinia] } })`; no network or real PyWebView calls occur in component tests.

- [ ] **Step 1: Write failing navigation and read-only built-in tests**

```ts
it('opens the GUI appearance page from settings navigation', async () => {
  const wrapper = mount(SettingsPage);
  await wrapper.get('[data-test="appearance-nav"]').trigger('click');
  expect(wrapper.get('[data-test="gui-theme-settings"]').isVisible()).toBe(true);
});

it('requires copying a built-in theme before editing', async () => {
  const wrapper = mountThemeSettings({ active: builtinLight() });
  expect(wrapper.get('[data-test="theme-name"]').attributes('disabled')).toBeDefined();
  expect(wrapper.get('[data-test="copy-theme"]').text()).toContain('复制为自定义主题');
});
```

- [ ] **Step 2: Write failing color, preview, image, and dirty-exit tests**

```ts
it('keeps color picker and hex input synchronized and previews immediately', async () => {
  const wrapper = mountThemeSettings({ active: userTheme() });
  await wrapper.get('[data-test="region-workspace"]').trigger('click');
  await wrapper.get('[data-test="background-hex"]').setValue('#112233');
  expect(wrapper.get<HTMLInputElement>('[data-test="background-picker"]').element.value).toBe('#112233');
  expect(previewGuiTheme).toHaveBeenCalled();
});

it('rejects invalid hex locally and does not save', async () => {
  const wrapper = mountThemeSettings({ active: userTheme() });
  await wrapper.get('[data-test="background-hex"]').setValue('#xyz');
  await wrapper.get('[data-test="save-theme"]').trigger('click');
  expect(wrapper.get('[data-test="field-error"]').text()).toContain('#RRGGBB');
  expect(saveGuiThemeDraft).not.toHaveBeenCalled();
});

it('asks before leaving a dirty editor', async () => {
  const wrapper = mountThemeSettings({ active: userTheme(), dirty: true });
  await wrapper.get('[data-test="theme-builtin-dark"]').trigger('click');
  expect(askConfirm).toHaveBeenCalled();
  expect(activateGuiTheme).not.toHaveBeenCalled();
});
```

- [ ] **Step 3: Run component tests and verify RED**

Run: `npm test -- --run src/components/GuiThemeSettings.test.ts src/components/SettingsPage.test.ts`

Working directory: `frontend`

Expected: missing components/navigation cause failures.

- [ ] **Step 4: Implement focused editor components**

`GuiThemeSettings.vue` owns theme selection, CRUD/import/export commands, selected region and dirty-exit confirmation. `ThemeRegionEditor.vue` owns only field controls. Use `<input type="color">` plus a controlled HEX text input, number/range inputs for opacity, selects for `cover|contain|auto` and five positions, and explicit choose/remove buttons.

Built-ins expose only Activate and Copy. User themes expose Rename, Delete, Import image, Export, Save, Cancel, and Restore inheritance. Disable Save while any field is invalid or a bridge request is active.

- [ ] **Step 5: Wire the Settings page**

Add `activeSection === 'appearance'`, the `appearance-nav` button under “基础设置”, and render `<GuiThemeSettings />` without moving existing model/token/permission/about sections.

- [ ] **Step 6: Run component tests and verify GREEN**

Run: `npm test -- --run src/components/GuiThemeSettings.test.ts src/components/SettingsPage.test.ts`

Working directory: `frontend`

Expected: all focused component tests pass.

- [ ] **Step 7: Commit visual editor**

```bash
git add frontend/src/components/GuiThemeSettings.vue frontend/src/components/theme/ThemeRegionEditor.vue frontend/src/components/GuiThemeSettings.test.ts frontend/src/components/SettingsPage.vue frontend/src/components/SettingsPage.test.ts
git commit -m "feat: add visual GUI theme editor"
```

### Task 7: Wire Every GUI Region to Stable Theme Variables

**Files:**
- Modify: `frontend/src/App.vue`
- Modify: `frontend/src/components/Topbar.vue`
- Modify: `frontend/src/components/Sidebar.vue`
- Modify: `frontend/src/components/InputArea.vue`
- Modify: `frontend/src/components/RightPanel.vue`
- Modify: `frontend/src/style.css`
- Create: `frontend/src/components/GuiThemeRegions.test.ts`

**Interfaces:**
- Consumes CSS variables produced by `runtime.ts`.
- Produces DOM markers `data-theme-region` for all seven region IDs.
- Produces no theme-dependent DOM or class names beyond the stable region markers.

Add this helper to `GuiThemeRegions.test.ts`:

```ts
function piniaWithInitializedStore() {
  const pinia = createPinia();
  setActivePinia(pinia);
  const store = useAppStore();
  store.initialized = true;
  store.guiThemeError = null;
  return pinia;
}
```

- [ ] **Step 1: Write failing region-marker tests**

```ts
it('marks all public theme regions with stable IDs', () => {
  const wrapper = mount(App, { global: { plugins: [piniaWithInitializedStore()] } });
  const ids = wrapper.findAll('[data-theme-region]').map(node => node.attributes('data-theme-region'));
  expect(new Set(ids)).toEqual(new Set([
    'app_header', 'sidebar', 'conversation_toolbar', 'workspace',
    'composer', 'right_panel', 'statusbar',
  ]));
});
```

- [ ] **Step 2: Run the region test and verify RED**

Run: `npm test -- --run src/components/GuiThemeRegions.test.ts`

Working directory: `frontend`

Expected: no stable region markers exist yet.

- [ ] **Step 3: Add stable markers without changing layout**

Apply markers to the existing roots: `#topbar`, Sidebar root, `#chat-header`, `#chat-scroll`, InputArea root, RightPanel root, and `#statusbar`. Do not add wrapper elements that alter flex/grid sizing.

- [ ] **Step 4: Replace direct palette consumption with region variables**

Define a common region layer:

```css
[data-theme-region] {
  position: relative;
  isolation: isolate;
  color: var(--theme-region-text);
  background-color: color-mix(
    in srgb,
    var(--theme-region-bg) calc(var(--theme-region-bg-opacity) * 100%),
    transparent
  );
  border-color: var(--theme-region-border);
}

[data-theme-region]::before {
  content: '';
  position: absolute;
  inset: 0;
  z-index: -1;
  pointer-events: none;
  background-image: var(--theme-region-image);
  background-size: var(--theme-region-image-size);
  background-position: var(--theme-region-image-position);
  background-repeat: no-repeat;
  opacity: var(--theme-region-image-opacity);
}
```

For each marker, bind `--theme-region-*` aliases to its `--theme-<region>-*` variables. Keep current `--bg0`/`--bg1` aliases mapped to semantic global tokens during migration so untouched components retain identical dark/light rendering.

- [ ] **Step 5: Add style regression assertions**

Extend `GuiThemeRegions.test.ts` to assert each root has the expected stable marker and no component reads a user-provided style string. Add a source-level test that `runtime.ts` contains no `innerHTML`, `insertAdjacentHTML`, or `data-user-theme` style injection.

- [ ] **Step 6: Run region, component, and full frontend tests**

Run: `npm test -- --run src/components/GuiThemeRegions.test.ts src/components/GuiThemeSettings.test.ts src/themes/runtime.test.ts`

Run: `npm test`

Working directory: `frontend`

Expected: focused and full Vitest suites pass.

- [ ] **Step 7: Commit GUI region wiring**

```bash
git add frontend/src/App.vue frontend/src/components/Topbar.vue frontend/src/components/Sidebar.vue frontend/src/components/InputArea.vue frontend/src/components/RightPanel.vue frontend/src/style.css frontend/src/components/GuiThemeRegions.test.ts
git commit -m "feat: theme GUI application regions"
```

### Task 8: Documentation, Full Verification, and Package-Size Evidence

**Files:**
- Modify: `docs/all_features.md`
- Modify: `assets/CHANGELOG.md`
- Modify: `docs/superpowers/plans/2026-08-21-gui-custom-theme.md` only to check completed boxes after evidence exists.

**Interfaces:**
- Consumes all prior tasks.
- Produces user-facing theme authoring and recovery documentation.

- [ ] **Step 1: Document the finished user workflow**

Add exact instructions for Settings → 外观与主题, creating/copying a theme, editing each region, uploading images, importing/exporting ZIP, and recovering from an invalid active theme. State that CLI `/theme` is independent and unchanged.

- [ ] **Step 2: Run Python theme and syntax verification**

Run: `venv\Scripts\python -m pytest tests/test_gui_theme_manager.py tests/test_gui_theme.py -q`

Run: `venv\Scripts\python -m py_compile gui_main.py gui_bridge.py core/gui_theme.py`

Expected: all tests pass and compile emits no output.

- [ ] **Step 3: Run full frontend verification**

Run: `npm test`

Run: `npm run build`

Working directory: `frontend`

Expected: all Vitest tests pass and Vite production build succeeds.

- [ ] **Step 4: Verify CLI isolation**

Run: `venv\Scripts\python -m py_compile main.py core/themes.py`

Run: `git diff 5c81f93 -- core/themes.py main.py`

Expected: compile emits no output and Git diff is empty, proving CLI theme files were not modified after the approved design commit.

- [ ] **Step 5: Measure frontend/package input delta**

Record `frontend/dist` byte size before and after the production build using PowerShell `Get-ChildItem frontend\dist -File -Recurse | Measure-Object Length -Sum`. Verify no user theme directory or background image appears in `ycode.spec`, `frontend/dist`, or `dist/ycode`.

- [ ] **Step 6: Perform visual desktop verification**

Launch: `venv\Scripts\python gui_main.py`

Verify built-in dark, built-in light, and one user theme with workspace/header images. Check all seven regions, native titlebar, live preview, Cancel, Save, restart persistence, import/export, and 100%/125%/150% DPI. Record any failure before claiming completion.

- [ ] **Step 7: Run final diff and status checks**

Run: `git diff --check`

Run: `git status --short`

Expected: no whitespace errors; only intended documentation or plan checkbox changes remain unstaged.

- [ ] **Step 8: Commit documentation and verification record**

```bash
git add docs/all_features.md assets/CHANGELOG.md docs/superpowers/plans/2026-08-21-gui-custom-theme.md
git commit -m "docs: document GUI custom themes"
```
