"""全屏任意位置截图：虚拟桌面度量、冻结帧捕获与遮罩页模板。

模块定位（设计 docs/superpowers/specs/2026-10-14-screen-snip-design.md）：

- ``get_virtual_screen_rect()``：用 Win32 ``GetSystemMetrics`` 的 SM_* 四常量
  读取虚拟桌面物理像素矩形，原点允许多显示器负坐标（与
  core/computer/win32_adapter.Win32InputAdapter.virtual_screen_rect 同口径，
  但刻意不复用其实例——那会把整个电脑操控运行时依赖拖进 GUI 截图链路）；
- ``capture_virtual_screen()``：惰性导入 ``PIL.ImageGrab`` 捕获整个虚拟桌面，
  与 core/computer/windows_backend.default_image_grab 同一实现口径，模块顶层
  不做 PIL 副作用导入；
- ``build_snip_page()``：构建截图遮罩窗口的单文件 HTML（内联 CSS/JS），冻结帧
  dataURL 经占位符一次注入。遮罩窗口的创建/回收/桥方法在 gui_snip_mixin.py。

坐标约定：遮罩窗口覆盖虚拟桌面物理矩形；页面只负责显示**压缩预览**并回报选区
（CSS px × 原始尺寸比例 → 原始物理像素），真正的裁剪由 Python 从原始全分辨率
图执行（无损）。这条分工是被逼出来的：遮罩窗口经 ``create_window(html=...)``
加载，走 WebView2 ``NavigateToString``，该接口对 HTML 内容有 **2MB 硬上限**，
整屏 PNG 内联必然超限（超限时抛异常、窗口全黑），故预览必须体积可控、输出必须
绕开页面。当前主场景两屏均 100% 缩放，CSS 像素与物理像素一一对应；混合 DPI
多屏为已知限制（见设计文档）。
"""

from __future__ import annotations

import ctypes
import sys
from typing import Tuple

# SM_* 虚拟桌面度量（与 core/computer/win32_adapter 同口径）：
# 原点可取负值（副屏位于主屏上方/左侧时）。
_SM_XVIRTUALSCREEN = 76
_SM_YVIRTUALSCREEN = 77
_SM_CXVIRTUALSCREEN = 78
_SM_CYVIRTUALSCREEN = 79

# 冻结帧 dataURL 前缀：接受任意 image/* 的 base64 dataURL（PNG/JPEG 皆可）。
# 不锁死 PNG 的原因：遮罩窗口经 create_window(html=...) 加载，走 WebView2 的
# NavigateToString，该接口对 HTML 内容有 2MB 硬上限；整屏 PNG 转 base64 后
# 轻易突破（实测 1920x2280 复杂界面达 2.6MB），超限时 NavigateToString 抛
# "值不在预期的范围内"，遮罩窗口表现为全黑。故上层改用体积可控的 JPEG 冻结帧
# （见 gui_snip_mixin.encode_frozen_frame），本函数只做格式宽松校验。
_IMAGE_DATA_URL_PREFIX = "data:image/"
_BASE64_MARKER = ";base64,"
# 页面模板占位符：base64 字符集仅 [A-Za-z0-9+/=]，str.replace 注入无转义风险
_SNIP_FRAME_PLACEHOLDER = "__SNIP_FRAME_DATA_URL__"
# 原始全分辨率冻结帧的物理宽高占位符（预览可能被压缩，选区换算须以原始尺寸为准）
_ORIG_W_PLACEHOLDER = "__SNIP_ORIG_W__"
_ORIG_H_PLACEHOLDER = "__SNIP_ORIG_H__"


def get_virtual_screen_rect() -> Tuple[int, int, int, int]:
    """虚拟桌面物理像素矩形 ``(x, y, width, height)``；原点允许多显示器负值。

    生产只走 Win32 ``GetSystemMetrics``；非 Windows 平台显式报错（与
    core/computer 的平台守卫同风格，不做静默降级）。尺寸无效（<=0）时同样
    报错——宁可让调用方降级为错误提示，也不在错误坐标系里继续。
    """
    if sys.platform != "win32":
        raise RuntimeError("全屏截图仅支持 Windows 桌面端")
    user32 = ctypes.windll.user32
    origin_x = int(user32.GetSystemMetrics(_SM_XVIRTUALSCREEN))
    origin_y = int(user32.GetSystemMetrics(_SM_YVIRTUALSCREEN))
    width = int(user32.GetSystemMetrics(_SM_CXVIRTUALSCREEN))
    height = int(user32.GetSystemMetrics(_SM_CYVIRTUALSCREEN))
    if width <= 0 or height <= 0:
        raise RuntimeError("无法读取有效的虚拟桌面尺寸")
    return origin_x, origin_y, width, height


def capture_virtual_screen():
    """捕获整个虚拟桌面为 ``PIL.Image``。

    与 core/computer/windows_backend.default_image_grab 同一实现口径：函数内
    惰性导入 ``PIL.ImageGrab``，模块顶层不触发任何 PIL 副作用；失败由调用方
    （gui_snip_mixin.screen_snip_start）转成用户可读错误。
    """
    from PIL import ImageGrab

    return ImageGrab.grab(all_screens=True)


def build_snip_page(frame_data_url: str, orig_width: int, orig_height: int) -> str:
    """构建截图遮罩窗口的完整 HTML（单文件自包含，供 pywebview 窗口加载）。

    冻结帧 dataURL 经占位符一次注入；``orig_width`` / ``orig_height`` 是**原始
    全分辨率**冻结帧的物理尺寸（预览图可能被压缩/缩小，选区换算必须以此为准，
    最终由 Python 从原始图裁剪，保证输出无损）。

    构建后必须不再残留任何占位符（模板不变式，残留即说明模板被改坏，宁可报错
    也不给用户一个空白遮罩）。
    """
    if (
        not isinstance(frame_data_url, str)
        or not frame_data_url.startswith(_IMAGE_DATA_URL_PREFIX)
        or _BASE64_MARKER not in frame_data_url[:64]
    ):
        raise ValueError("冻结帧必须是 base64 图片 dataURL")
    try:
        ow = int(orig_width)
        oh = int(orig_height)
    except (TypeError, ValueError):
        raise ValueError("冻结帧原始尺寸无效")
    if ow <= 0 or oh <= 0:
        raise ValueError("冻结帧原始尺寸无效")
    page = _SNIP_PAGE_TEMPLATE.replace(_SNIP_FRAME_PLACEHOLDER, frame_data_url, 1)
    page = page.replace(_ORIG_W_PLACEHOLDER, str(ow)).replace(_ORIG_H_PLACEHOLDER, str(oh))
    for placeholder in (
        _SNIP_FRAME_PLACEHOLDER,
        _ORIG_W_PLACEHOLDER,
        _ORIG_H_PLACEHOLDER,
    ):
        if placeholder in page:
            raise ValueError("截图页模板含多余占位符，构建中止")
    return page


# ---------------------------------------------------------------------------
# 遮罩页模板：原生 JS + 内联 CSS，零外部资源。
# 交互：拖动框选（重拖=重选）→ 选区旁浮出工具条（发到输入框/复制/保存/取消）；
# Esc 取消、Enter 发到输入框；动作结果由宿主负责关窗，页面只显示失败原因。
# 桥方法缺失时（pywebview 未注入）页面降级为只读提示，绝不静默卡死。
# ---------------------------------------------------------------------------
_SNIP_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>星瑶截图</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  html, body { width: 100%; height: 100%; overflow: hidden; background: #000; }
  body {
    font-family: "Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", sans-serif;
    user-select: none; -webkit-user-select: none; cursor: crosshair;
  }
  #frame {
    position: fixed; inset: 0; width: 100%; height: 100%; display: block;
    object-fit: fill; pointer-events: none; -webkit-user-drag: none;
  }
  #dim { position: fixed; inset: 0; background: rgba(0, 0, 0, 0.35); pointer-events: none; }
  /* 选区：box-shadow 大半径形成"选区外整屏压暗"，避免维护四块遮罩 */
  #sel {
    position: fixed; display: none; border: 2px solid #3b82f6; border-radius: 1px;
    box-shadow: 0 0 0 9999px rgba(0, 0, 0, 0.35);
    pointer-events: none; z-index: 10;
  }
  #size {
    position: fixed; display: none; padding: 2px 8px; border-radius: 4px;
    background: rgba(20, 24, 32, 0.85); color: #f5f7fa;
    font-size: 12px; line-height: 18px; white-space: nowrap;
    pointer-events: none; z-index: 20;
  }
  #hint {
    position: fixed; top: 48px; left: 50%; transform: translateX(-50%);
    padding: 8px 18px; border-radius: 999px; background: rgba(20, 24, 32, 0.82);
    color: #e8ecf3; font-size: 13px; white-space: nowrap;
    box-shadow: 0 6px 20px rgba(0, 0, 0, 0.4); pointer-events: none;
  }
  #toolbar {
    position: fixed; display: none; align-items: center; gap: 2px;
    padding: 5px 6px; border-radius: 10px; background: rgba(28, 32, 42, 0.96);
    border: 1px solid rgba(255, 255, 255, 0.12);
    box-shadow: 0 10px 30px rgba(0, 0, 0, 0.5); z-index: 30;
  }
  #toolbar button {
    display: inline-flex; align-items: center; gap: 5px; border: none;
    background: none; color: #d7dce6; font-size: 12.5px; font-family: inherit;
    padding: 6px 10px; border-radius: 7px; cursor: pointer; white-space: nowrap;
  }
  #toolbar button:hover { background: rgba(255, 255, 255, 0.10); color: #fff; }
  #toolbar button:disabled { opacity: 0.45; cursor: default; }
  #toolbar button.primary { color: #fff; background: #3b82f6; font-weight: 600; }
  #toolbar button.primary:hover { background: #2563eb; }
  #toolbar button.primary:disabled { background: #3b82f6; }
  #toolbar .sep { width: 1px; height: 18px; background: rgba(255, 255, 255, 0.15); margin: 0 3px; }
  #toolbar svg { display: block; }
  #toast {
    position: fixed; top: 48px; left: 50%; transform: translateX(-50%);
    display: none; padding: 8px 16px; border-radius: 999px;
    background: rgba(185, 28, 28, 0.92); color: #fff; font-size: 13px;
    box-shadow: 0 6px 20px rgba(0, 0, 0, 0.45); max-width: 80%;
    text-align: center; z-index: 40; pointer-events: none;
  }
</style>
</head>
<body>
  <img id="frame" src="__SNIP_FRAME_DATA_URL__" alt="" draggable="false">
  <div id="dim"></div>
  <div id="sel"></div>
  <div id="size"></div>
  <div id="hint">按住左键拖动框选要截图的区域，Esc 取消</div>
  <div id="toolbar">
    <button id="btn-send" class="primary" title="把截图作为附件发到星瑶输入框（Enter）">
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h13"/><path d="m12 5 7 7-7 7"/></svg>发到输入框
    </button>
    <button id="btn-copy" title="复制截图到剪贴板">
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect width="13" height="13" x="8" y="8" rx="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/></svg>复制
    </button>
    <button id="btn-save" title="另存为 PNG 文件">
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12"/><path d="m7 10 5 5 5-5"/><path d="M5 21h14"/></svg>保存
    </button>
    <span class="sep"></span>
    <button id="btn-cancel" title="取消（Esc）">
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg>取消
    </button>
  </div>
  <div id="toast"></div>

<script>
(function () {
  'use strict';

  var frame = document.getElementById('frame');
  var dimEl = document.getElementById('dim');
  var selEl = document.getElementById('sel');
  var sizeEl = document.getElementById('size');
  var hintEl = document.getElementById('hint');
  var toolbar = document.getElementById('toolbar');
  var toastEl = document.getElementById('toast');
  var btnSend = document.getElementById('btn-send');
  var btnCopy = document.getElementById('btn-copy');
  var btnSave = document.getElementById('btn-save');
  var btnCancel = document.getElementById('btn-cancel');
  // pywebview 的 JS 桥是两步注入（api.js 先给空 {}，finish.js 再填方法并派发
  // pywebviewready），且注入发生在 on_navigation_completed——即本页面脚本
  // 执行之后。绝不能在这里一次性捕获 api，否则只会拿到 null/空对象；统一走
  // bridgeApi() 在每次操作时动态判定「方法已注入」。用户拖框选必然晚于注入；
  // 极端抢跑只提示可重试，绝不锁死。
  function bridgeApi() {
    var p = window.pywebview;
    var a = p && p.api;
    return (a && typeof a.snip_send === 'function') ? a : null;
  }

  // ------------------------------------------------------------- 就绪上报
  // 遮罩窗口由宿主「隐藏创建」：WebView2 初始化 + 页面装载都要几百毫秒，
  // 若先显示窗口，那段时间用户看到的就是一块黑底（观感像"先黑一下再截图"）。
  // 本页在冻结帧解码完成、可绘制之后主动回报 snip_ready，宿主随即显示窗口
  // ——窗口一出现就是冻结画面，对齐微信截图"画面瞬现"的手感。
  // 桥注入（on_navigation_completed）晚于本脚本执行，故需等两件事同时成立：
  // ① 冻结帧已可绘制；② pywebview.api 已注入。宿主另有超时兜底显示，
  // 即使本页上报失败，窗口也不会一直藏着。
  var frameReady = false;
  var readySignaled = false;
  var readyTries = 0;
  function signalReady() {
    if (readySignaled || !frameReady) return;
    var api = bridgeApi();
    if (!api || typeof api.snip_ready !== 'function') {
      if (readyTries++ < 100) setTimeout(signalReady, 30);  // 桥未就绪：短重试
      return;                                               // 宿主兜底会显示窗口
    }
    readySignaled = true;
    try { api.snip_ready(); } catch (err) { /* 宿主兜底会显示窗口 */ }
  }

  var MIN_SEL = 8;        // CSS px：小于该尺寸的拖动视为误触，不产生选区
  var TOAST_MS = 2600;

  // 原始全分辨率冻结帧的物理尺寸（由宿主注入）。预览图可能被压缩/缩小，
  // 选区一律按「CSS px × 原始尺寸 / 预览显示尺寸」换算成原始物理像素后回报，
  // 由 Python 从原图裁剪——页面绝不自己产出像素数据（见模块 docstring）。
  var ORIG_W = __SNIP_ORIG_W__;
  var ORIG_H = __SNIP_ORIG_H__;

  var busy = false;
  var sel = null;         // 当前选区 {x, y, w, h}（覆盖层 CSS px）
  var anchor = null;      // 拖动起点 {x, y}
  var toastTimer = 0;

  // 原始物理宽 / 预览显示宽：选区 CSS px → 原始物理像素的换算比。
  // 用 getBoundingClientRect 而非 window.innerWidth——两者在铺满布局下相等，
  // 但前者对布局变化更鲁棒（与 RightPanelSnip.vue 的 ratio 同思路）。
  function ratio() {
    var shown = frame.getBoundingClientRect().width || window.innerWidth || 1;
    return ORIG_W / shown;
  }

  function clamp(v, lo, hi) { return v < lo ? lo : (v > hi ? hi : v); }

  function showToast(msg) {
    toastEl.textContent = msg;
    toastEl.style.display = 'block';
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { toastEl.style.display = 'none'; }, TOAST_MS);
  }

  function makeSel(x0, y0, x1, y1) {
    var vw = window.innerWidth;
    var vh = window.innerHeight;
    var x = clamp(Math.min(x0, x1), 0, vw);
    var y = clamp(Math.min(y0, y1), 0, vh);
    var x2 = clamp(Math.max(x0, x1), 0, vw);
    var y2 = clamp(Math.max(y0, y1), 0, vh);
    return { x: x, y: y, w: x2 - x, h: y2 - y };
  }

  // ------------------------------------------------------------- 布局与渲染
  function layout() {
    var vw = window.innerWidth;
    var vh = window.innerHeight;
    if (!sel || sel.w < MIN_SEL || sel.h < MIN_SEL) {
      sel = null;
      selEl.style.display = 'none';
      sizeEl.style.display = 'none';
      toolbar.style.display = 'none';
      dimEl.style.display = 'block';
      hintEl.style.display = 'block';
      return;
    }

    selEl.style.display = 'block';
    selEl.style.left = sel.x + 'px';
    selEl.style.top = sel.y + 'px';
    selEl.style.width = sel.w + 'px';
    selEl.style.height = sel.h + 'px';
    dimEl.style.display = 'none';
    hintEl.style.display = 'none';

    // 尺寸标注（物理像素）：优先选区上方外侧；上方放不下时移入选区内左上
    sizeEl.textContent = Math.round(sel.w * ratio()) + ' \u00d7 ' + Math.round(sel.h * ratio());
    sizeEl.style.display = 'block';
    var labelW = sizeEl.offsetWidth;
    var labelH = sizeEl.offsetHeight;
    var labelX = clamp(sel.x, 4, Math.max(4, vw - labelW - 4));
    var labelY = sel.y - labelH - 6;
    if (labelY < 4) labelY = sel.y + 6;
    sizeEl.style.left = labelX + 'px';
    sizeEl.style.top = labelY + 'px';

    // 工具条：默认选区下方 +10；放不下翻到上方；水平右对齐并防出界
    toolbar.style.display = 'flex';
    var tw = toolbar.offsetWidth;
    var th = toolbar.offsetHeight;
    var tx = clamp(sel.x + sel.w - tw, 4, Math.max(4, vw - tw - 4));
    var ty = sel.y + sel.h + 10;
    if (ty + th > vh - 4) ty = sel.y - th - 10;
    if (ty < 4) ty = 4;
    toolbar.style.left = tx + 'px';
    toolbar.style.top = ty + 'px';
  }

  // ------------------------------------------------------------- 拖动框选
  function onPointerDown(e) {
    if (busy || e.button !== 0) return;
    if (toolbar.contains(e.target)) return;   // 工具条上的按下不触发重选
    anchor = { x: e.clientX, y: e.clientY };
    sel = null;                               // 重新拖动 = 重选
    layout();
    try { e.target.setPointerCapture(e.pointerId); } catch (err) { /* 非关键 */ }
    e.preventDefault();
  }

  function onPointerMove(e) {
    if (!anchor || busy) return;
    sel = makeSel(anchor.x, anchor.y, e.clientX, e.clientY);
    layout();
  }

  function onPointerUp(e) {
    if (!anchor) return;
    var finished = makeSel(anchor.x, anchor.y, e.clientX, e.clientY);
    anchor = null;
    sel = (finished.w >= MIN_SEL && finished.h >= MIN_SEL) ? finished : null;
    layout();
  }

  document.addEventListener('pointerdown', onPointerDown);
  document.addEventListener('pointermove', onPointerMove);
  document.addEventListener('pointerup', onPointerUp);
  document.addEventListener('contextmenu', function (e) { e.preventDefault(); });
  window.addEventListener('blur', function () { anchor = null; });
  window.addEventListener('resize', layout);

  // ------------------------------------------------------------- 选区与动作
  // 把当前选区换算成原始全分辨率图上的物理像素矩形。页面只回报坐标，
  // 裁剪由 Python 从原图执行（页面内联大图会撞 NavigateToString 的 2MB 上限）。
  function selection() {
    if (!sel) return null;
    if (!ORIG_W || !ORIG_H) return null;
    var r = ratio();
    var x = clamp(Math.round(sel.x * r), 0, Math.max(0, ORIG_W - 1));
    var y = clamp(Math.round(sel.y * r), 0, Math.max(0, ORIG_H - 1));
    var w = clamp(Math.round(sel.w * r), 1, ORIG_W - x);
    var h = clamp(Math.round(sel.h * r), 1, ORIG_H - y);
    return { x: x, y: y, w: w, h: h };
  }

  function setBusy(on) {
    busy = on;
    btnSend.disabled = on;
    btnCopy.disabled = on;
    btnSave.disabled = on;
    btnCancel.disabled = on;
  }

  // 动作统一入口：算选区坐标 → 调桥；成功由宿主关窗，失败显示原因并恢复可操作
  function runAction(call) {
    if (busy) return;
    var api = bridgeApi();
    if (!api) { showToast('桌面桥未就绪，请稍后重试'); return; }
    var rect = selection();
    if (!rect) { showToast('请先按住左键拖动框选截图区域'); return; }
    setBusy(true);
    var pending;
    try {
      pending = call(api, rect);
    } catch (err) {
      setBusy(false);
      showToast('操作失败：桥接调用异常');
      return;
    }
    Promise.resolve(pending).then(function (res) {
      if (res && res.ok) return;   // 成功后宿主关闭本窗口
      setBusy(false);
      if (res && res.msg === '已取消选择') return;   // 用户取消保存对话框：静默恢复
      showToast((res && res.msg) || '操作失败，请重试');
    }, function () {
      setBusy(false);
      showToast('操作失败：桥接调用异常');
    });
  }

  function timestamp() {
    var d = new Date();
    function p(n) { return (n < 10 ? '0' : '') + n; }
    return '' + d.getFullYear() + p(d.getMonth() + 1) + p(d.getDate()) + '_' +
      p(d.getHours()) + p(d.getMinutes()) + p(d.getSeconds());
  }

  btnSend.addEventListener('click', function () {
    runAction(function (a, r) { return a.snip_send(r); });
  });
  btnCopy.addEventListener('click', function () {
    runAction(function (a, r) { return a.snip_copy(r); });
  });
  btnSave.addEventListener('click', function () {
    runAction(function (a, r) { return a.snip_save(r, 'ycode截图_' + timestamp() + '.png'); });
  });

  function cancel() {
    if (busy) return;
    var api = bridgeApi();
    try { if (api && api.snip_close) api.snip_close(); } catch (err) { /* 关闭尽力而为 */ }
  }
  btnCancel.addEventListener('click', cancel);

  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') {
      e.preventDefault();
      cancel();
      return;
    }
    if (e.key === 'Enter') {
      e.preventDefault();
      if (sel) runAction(function (a, r) { return a.snip_send(r); });
    }
  });

  // ------------------------------------------------------------- 初始化
  function init() {
    layout();
    frameReady = true;      // 冻结帧已可绘制 → 上报宿主显示窗口（见 signalReady）
    signalReady();
  }

  frame.addEventListener('error', function () {
    hintEl.textContent = '冻结帧加载失败，请按 Esc 关闭后重试';
  });
  if (frame.complete) init();
  else frame.addEventListener('load', init);
})();
</script>
</body>
</html>
"""
