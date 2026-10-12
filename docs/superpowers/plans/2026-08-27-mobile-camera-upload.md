# 手机/平板拍照与相册上传 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让手机/平板（触摸设备）通过「拍照/相册/文件」入口把文件经局域网 WebSocket RPC 上传到电脑并作为附件发送。

**Architecture:** 触摸判定用 `pointer: coarse`（与布局宽度断点解耦）；上传复用现有 WS 连接发 `upload_file` RPC（base64），后端落盘到临时目录返回路径，前端 `addPaths` 加入附件列表。桌面端走 `pickFiles` 的行为完全不变。

**Tech Stack:** Vue 3 + Pinia + vitest（前端）；Python `websockets.sync.server` + pytest（后端）。

## Global Constraints

- 上传大小上限 **50MB**（`50 * 1024 * 1024` 字节），超过拒绝（前端预检 + 后端兜底）。
- `serve(...)` 必须调大 `max_size=70 * 1024 * 1024`（50MB 文件 base64 后约 67MB）。
- `upload_file` **不得**加入 `lan_server.py` 的 `FORBIDDEN_METHODS`。
- 落盘目录沿用 `tempfile.gettempdir()/ycode_paste`，与 `save_clipboard_image` 一致。
- 触摸判定 `matchMedia('(pointer: coarse)')`，`matchMedia` 不可用时按桌面（false）处理。
- 桌面（非触摸）端附件按钮行为不变（仍走 `store.pickFiles()`）。
- 后端测试命令：`python -m pytest tests/ -k <关键字> -v`；前端测试命令：`cd frontend; npx vitest run <文件>`。

---

### Task 1: 后端上传 RPC `upload_file` + 调大 WS max_size

**Files:**
- Modify: `gui_bridge.py`（在 `save_clipboard_image` 附近新增 `upload_file` 与落盘 helper）
- Modify: `core/lan_server.py:360-365`（`serve(...)` 加 `max_size`）
- Test: `tests/test_lan_upload.py`（新建）

**Interfaces:**
- Consumes: 现有 `save_clipboard_image` 的落盘目录逻辑。
- Produces:
  - `GuiBridge.upload_file(name: str, data_b64: str) -> Dict[str, Any]`，返回 `{"ok": True, "path": str}` 或 `{"ok": False, "msg": str}`。
  - `LanServer` WS 消息上限 70MB。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_lan_upload.py`：

```python
import base64
import os
import pytest

from gui_bridge import GuiBridge


@pytest.fixture
def bridge():
    # GuiBridge 构造若需依赖，按现有测试惯例传最小可用实例；此处假设可无参构造
    return GuiBridge()


def test_upload_file_ok(bridge, tmp_path, monkeypatch):
    monkeypatch.setenv("TEMP", str(tmp_path))  # tempfile.gettempdir 走 TEMP
    data = base64.b64encode(b"hello-image-bytes").decode()
    res = bridge.upload_file("photo.jpg", data)
    assert res["ok"] is True
    assert os.path.isfile(res["path"])
    assert res["path"].endswith(".jpg")
    with open(res["path"], "rb") as f:
        assert f.read() == b"hello-image-bytes"


def test_upload_file_over_50mb_rejected(bridge):
    big = base64.b64encode(b"x" * (50 * 1024 * 1024 + 1)).decode()
    res = bridge.upload_file("big.bin", big)
    assert res["ok"] is False
    assert "50MB" in res["msg"]


def test_upload_file_invalid_base64(bridge):
    res = bridge.upload_file("x.png", "!!!not-base64!!!")
    assert res["ok"] is False
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_lan_upload.py -v`
Expected: FAIL（`GuiBridge` 无 `upload_file` 方法 / AttributeError）

- [ ] **Step 3: 实现 `upload_file` + 落盘 helper**

在 `gui_bridge.py` 的 `save_clipboard_image` 之后新增：

```python
    UPLOAD_MAX_BYTES = 50 * 1024 * 1024  # 50MB

    def _save_bytes_to_temp(self, raw: bytes, ext: str) -> str:
        """把二进制写入 ycode_paste 临时目录，返回完整路径（时间戳+随机后缀防撞名）。"""
        import tempfile
        import time
        paste_dir = os.path.join(tempfile.gettempdir(), "ycode_paste")
        os.makedirs(paste_dir, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        ms = int(time.time() * 1000) % 1000
        path = os.path.join(
            paste_dir,
            f"upload_{ts}_{ms:03d}_{uuid.uuid4().hex[:6]}{ext}",
        )
        with open(path, "wb") as f:
            f.write(raw)
        return path

    def upload_file(self, name: str, data_b64: str) -> Dict[str, Any]:
        """手机/平板端经 WS RPC 上传文件（base64），落盘后返回路径供作为附件发送。"""
        try:
            import base64
            raw = base64.b64decode(data_b64 or "", validate=True)
        except Exception:
            return {"ok": False, "msg": "无效的文件数据"}
        try:
            if len(raw) > self.UPLOAD_MAX_BYTES:
                return {"ok": False, "msg": "文件超过 50MB，无法上传"}
            base = os.path.basename(str(name or "file"))
            _, ext = os.path.splitext(base)
            path = self._save_bytes_to_temp(raw, ext or ".bin")
            return {"ok": True, "path": path}
        except Exception as e:
            logger.exception("upload_file 失败")
            return {"ok": False, "msg": str(e)}
```

确认 `gui_bridge.py` 顶部已 `import os, uuid, logging`（`logger`、`uuid`、`os` 沿用现有导入；缺则补）。

- [ ] **Step 4: 调大 WS max_size**

修改 `core/lan_server.py` 的 `serve(...)` 调用：

```python
        self._server = serve(
            self._handle_ws,
            host="0.0.0.0",
            port=self.port,
            process_request=self._serve_http,
            max_size=70 * 1024 * 1024,  # 支持手机上传 ≤50MB 文件（base64 约 67MB）
        )
```

确认 `upload_file` 不在 `FORBIDDEN_METHODS`（默认就不在，无需改）。

- [ ] **Step 5: 跑测试确认通过**

Run: `python -m pytest tests/test_lan_upload.py -v`
Expected: PASS（3 个测试）

- [ ] **Step 6: Commit**

```bash
git add gui_bridge.py core/lan_server.py tests/test_lan_upload.py
git commit -m "feat: 手机端文件上传 RPC upload_file + WS max_size 调大到 70MB"
```

---

### Task 2: 前端 `useIsTouch` composable

**Files:**
- Create: `frontend/src/composables/useIsTouch.ts`
- Test: `frontend/src/composables/useIsTouch.test.ts`（新建）

**Interfaces:**
- Consumes: 无。
- Produces: `useIsTouch(): Ref<boolean>`——`pointer: coarse` 匹配结果，matchMedia 不可用时为 `false`。

- [ ] **Step 1: 写失败测试**

新建 `frontend/src/composables/useIsTouch.test.ts`：

```ts
import { describe, expect, it, vi, afterEach } from 'vitest';
import { useIsTouch } from './useIsTouch';

function mockMatchMedia(matches: boolean) {
  (window as unknown as { matchMedia: unknown }).matchMedia = vi
    .fn()
    .mockImplementation((query: string) => ({
      matches,
      media: query,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    }));
}

describe('useIsTouch', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('pointer: coarse 匹配时为 true（手机/平板）', () => {
    mockMatchMedia(true);
    expect(useIsTouch().value).toBe(true);
  });

  it('pointer: coarse 不匹配时为 false（桌面）', () => {
    mockMatchMedia(false);
    expect(useIsTouch().value).toBe(false);
  });

  it('matchMedia 不可用时按桌面 false 处理', () => {
    delete (window as unknown as { matchMedia?: unknown }).matchMedia;
    expect(useIsTouch().value).toBe(false);
  });
});
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd frontend; npx vitest run src/composables/useIsTouch.test.ts`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现 `useIsTouch`**

新建 `frontend/src/composables/useIsTouch.ts`：

```ts
import { ref, onUnmounted, type Ref } from 'vue';

// 触摸设备判定（手机/平板）：与布局宽度断点解耦。
// iPad 横屏再宽 pointer 仍是 coarse，桌面窗口拖再窄仍是 fine。
export function useIsTouch(): Ref<boolean> {
  const supported =
    typeof window !== 'undefined' && typeof window.matchMedia === 'function';
  const mq = supported ? window.matchMedia('(pointer: coarse)') : null;
  const isTouch = ref(mq ? mq.matches : false);
  const onChange = (e: MediaQueryListEvent) => {
    isTouch.value = e.matches;
  };
  mq?.addEventListener?.('change', onChange);
  onUnmounted(() => mq?.removeEventListener?.('change', onChange));
  return isTouch;
}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd frontend; npx vitest run src/composables/useIsTouch.test.ts`
Expected: PASS（3 个测试）

- [ ] **Step 5: Commit**

```bash
git add frontend/src/composables/useIsTouch.ts frontend/src/composables/useIsTouch.test.ts
git commit -m "feat: useIsTouch composable 用 pointer:coarse 判定触摸设备"
```

---

### Task 3: store 上传 action + useAttachments.uploadFiles

**Files:**
- Modify: `frontend/src/stores/appStore.ts`（`saveClipboardImage` 附近加 `uploadFile`）
- Modify: `frontend/src/composables/useAttachments.ts`（加 `uploadFiles`）
- Test: `frontend/src/composables/useAttachments.test.ts`（新建或补充）

**Interfaces:**
- Consumes: Task 1 的 `upload_file` RPC；Task 2 的 `useIsTouch`（本任务不直接用，由组件用）。
- Produces:
  - `store.uploadFile(name: string, dataB64: string): Promise<string>`（成功返回路径，失败返回 `''`）。
  - `useAttachments().uploadFiles(files: File[]): Promise<void>`（内部预检 50MB、逐个上传、`addPaths`、失败 `notifyChat`）。

- [ ] **Step 1: 写失败测试**

新建 `frontend/src/composables/useAttachments.test.ts`：

```ts
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { setActivePinia, createPinia } from 'pinia';
import { useAttachments } from './useAttachments';
import { useAppStore } from '../stores/appStore';

function makeFile(name: string, size: number): File {
  const f = new File(['x'], name);
  Object.defineProperty(f, 'size', { value: size });
  return f;
}

describe('useAttachments.uploadFiles', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  it('超过 50MB 的文件被预检拦截，不发起上传', async () => {
    const store = useAppStore();
    const spy = vi.spyOn(store, 'uploadFile').mockResolvedValue('/tmp/x');
    const notify = vi.spyOn(store, 'notifyChat').mockImplementation(() => {});
    const { uploadFiles, attachments } = useAttachments();
    await uploadFiles([makeFile('big.jpg', 50 * 1024 * 1024 + 1)]);
    expect(spy).not.toHaveBeenCalled();
    expect(notify).toHaveBeenCalled();
    expect(attachments.value).toHaveLength(0);
  });

  it('上传成功后路径加入附件列表', async () => {
    const store = useAppStore();
    vi.spyOn(store, 'uploadFile').mockResolvedValue('C:/tmp/upload_a.jpg');
    vi.spyOn(store, 'getFilesInfo').mockResolvedValue([
      { path: 'C:/tmp/upload_a.jpg', name: 'a.jpg', ext: '.jpg', size: 3, ok: true },
    ]);
    const { uploadFiles, attachments } = useAttachments();
    await uploadFiles([makeFile('a.jpg', 3)]);
    expect(attachments.value.map(a => a.path)).toContain('C:/tmp/upload_a.jpg');
  });
});
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd frontend; npx vitest run src/composables/useAttachments.test.ts`
Expected: FAIL（`store.uploadFile` 不存在 / `uploadFiles` 未定义）

- [ ] **Step 3: 实现 store.uploadFile**

在 `appStore.ts` 的 `saveClipboardImage` 之后新增：

```ts
    // 手机/平板端经 WS RPC 上传文件（base64），返回落盘路径（失败返回空串并 toast）
    async uploadFile(name: string, dataB64: string): Promise<string> {
      const api = this._api();
      if (!api) return '';
      try {
        const res = await api.upload_file(name, dataB64);
        if (res && res.ok && res.path) return res.path;
        this.notifyChat('上传失败: ' + (res && res.msg ? res.msg : '未知错误'));
        return '';
      } catch (err) {
        this.notifyChat('上传失败: ' + err);
        return '';
      }
    },
```

并在 `frontend/src/types/pywebview.d.ts` 的 API 接口里加：`upload_file(name: string, dataB64: string): Promise<any>;`

- [ ] **Step 4: 实现 useAttachments.uploadFiles**

在 `useAttachments.ts` 中（`pickAttachments` 之后）新增，并在 return 里导出：

```ts
  const UPLOAD_MAX_BYTES = 50 * 1024 * 1024;

  function fileToDataB64(file: File): Promise<string> {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => {
        const dataUrl = String(reader.result || '');
        resolve(dataUrl.slice(dataUrl.indexOf(',') + 1));
      };
      reader.onerror = () => reject(reader.error);
      reader.readAsDataURL(file);
    });
  }

  async function uploadFiles(files: File[]) {
    const paths: string[] = [];
    for (const file of files) {
      if (file.size > UPLOAD_MAX_BYTES) {
        store.notifyChat(`「${file.name}」超过 50MB，无法上传`);
        continue;
      }
      try {
        const b64 = await fileToDataB64(file);
        const path = await store.uploadFile(file.name, b64);
        if (path) paths.push(path);
      } catch {
        store.notifyChat(`「${file.name}」上传失败，请重试`);
      }
    }
    if (paths.length) await addPaths(paths);
  }
```

在 `return { ... }` 中加入 `uploadFiles`。

- [ ] **Step 5: 跑测试确认通过**

Run: `cd frontend; npx vitest run src/composables/useAttachments.test.ts`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add frontend/src/stores/appStore.ts frontend/src/types/pywebview.d.ts frontend/src/composables/useAttachments.ts frontend/src/composables/useAttachments.test.ts
git commit -m "feat: store.uploadFile + useAttachments.uploadFiles 手机端上传"
```

---

### Task 4: 两个输入区的触摸附件菜单（拍照/相册/文件）

**Files:**
- Modify: `frontend/src/components/InputArea.vue`（回形针按钮 + 菜单 + 隐藏 input）
- Modify: `frontend/src/components/KimiEmptyState.vue`（同上）
- Test: 在各自组件测试补触摸分支

**Interfaces:**
- Consumes: Task 2 `useIsTouch()`；Task 3 `uploadFiles(files)`；现有 `handlePickFiles`。
- Produces: 触摸设备点回形针弹「拍照/相册/文件」菜单并触发对应隐藏 input；非触摸仍 `handlePickFiles`。

- [ ] **Step 1: 抽出共享逻辑**

两个输入区都需：三个隐藏 input（capture 拍照 / image 相册 / any 文件）、菜单开关、`onPickInputChange(e)` 读取 `e.target.files` 调 `uploadFiles`。建议在 `useAttachments` 导出 `uploadFiles` 后，组件内联实现菜单（菜单 UI 各处样式不同，不强行抽组件）。

- [ ] **Step 2: InputArea.vue 改造**

模板：回形针按钮 `@click` 改为 `onAttachClick`；新增菜单浮层与三个隐藏 input：

```html
<button data-test="attachment-button" class="chip-btn plus-btn" title="附加文件"
  @click.stop="onAttachClick"><AppIcon name="paperclip" :size="14" /></button>

<input ref="cameraInputRef" type="file" accept="image/*" capture="environment" style="display:none" @change="onPickInputChange">
<input ref="albumInputRef" type="file" accept="image/*" multiple style="display:none" @change="onPickInputChange">
<input ref="fileInputRef" type="file" multiple style="display:none" @change="onPickInputChange">

<Teleport to="body">
  <div v-if="showAttachMenu" class="attach-menu" data-test="attach-menu" @click.stop>
    <button data-test="attach-camera" @click="pickFrom('camera')">📷 拍照</button>
    <button data-test="attach-album" @click="pickFrom('album')">🖼 相册</button>
    <button data-test="attach-file" @click="pickFrom('file')">📁 文件</button>
  </div>
</Teleport>
```

脚本：

```ts
import { useIsTouch } from '../composables/useIsTouch';
const isTouch = useIsTouch();
const showAttachMenu = ref(false);
const cameraInputRef = ref<HTMLInputElement | null>(null);
const albumInputRef = ref<HTMLInputElement | null>(null);
const fileInputRef = ref<HTMLInputElement | null>(null);

function onAttachClick() {
  if (!isTouch.value) { handlePickFiles(); return; }  // 桌面不变
  showAttachMenu.value = !showAttachMenu.value;
}
function pickFrom(kind: 'camera' | 'album' | 'file') {
  showAttachMenu.value = false;
  const el = kind === 'camera' ? cameraInputRef.value : kind === 'album' ? albumInputRef.value : fileInputRef.value;
  el?.click();
}
async function onPickInputChange(e: Event) {
  const input = e.target as HTMLInputElement;
  const files = Array.from(input.files || []);
  input.value = '';  // 允许重复选同一文件
  if (files.length) await uploadFiles(files);
}
```

从 `useAttachments()` 解构出 `uploadFiles`。菜单样式仿 ReasoningSelector 的浮层。

- [ ] **Step 3: KimiEmptyState.vue 同步改造**

复用与 Step 2 相同的模板片段与脚本逻辑（该组件回形针当前为 `handlePickFiles`，改为 `onAttachClick`）。

- [ ] **Step 4: 补组件测试**

在 `InputArea.test.ts` / `KimiEmptyState.test.ts`：mock `matchMedia('(pointer: coarse)')` 为 true，点 `attachment-button` 断言出现 `attach-menu` 三项；为 false 时断言调用了 `pickFiles`（不弹菜单）。

Run: `cd frontend; npx vitest run src/components/InputArea.test.ts src/components/KimiEmptyState.test.ts`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/InputArea.vue frontend/src/components/KimiEmptyState.vue frontend/src/components/InputArea.test.ts frontend/src/components/KimiEmptyState.test.ts
git commit -m "feat: 触摸设备附件按钮弹拍照/相册/文件菜单"
```

---

### Task 5: 全量验证

- [ ] **Step 1: 前端全量测试**

Run: `cd frontend; npx vitest run`
Expected: 全 PASS

- [ ] **Step 2: 前端构建**

Run: `cd frontend; npm run build`
Expected: 成功无报错

- [ ] **Step 3: 后端相关测试**

Run: `python -m pytest tests/test_lan_upload.py -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git commit -m "test: 手机拍照/相册上传全量验证" --allow-empty
```
