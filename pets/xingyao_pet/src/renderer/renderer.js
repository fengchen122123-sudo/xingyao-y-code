// renderer.js — 稳定优先版
// 关键决策：待机/睡觉/落地锁单帧（原始立绘），动效用平滑几何变换；
// 动作帧一律单图层硬切。双层 crossfade 会把两张不同姿势半透明叠成鬼影，那是闪烁主因。
(() => {
  const cfgUrl = new URL("./config.json", location.href);
  const stageEl = document.getElementById("stage");
  const breathEl = document.getElementById("breath");
  const frameEl = document.getElementById("frame");
  const bubbleEl = document.getElementById("bubble");

  let config = null;
  const frames = {};   // state -> [Image...]
  const lines = {};    // state -> [string...]
  let state = "idle";
  let frameIdx = 0;
  let lastFrameAt = 0;
  let stateEnteredAt = 0;
  let walkRemain = 0;
  let sleepSince = 0;
  let lastBubbleAt = 0;
  let sleepEnteredAt = 0;
  let lastBounceAt = 0;
  let quiet = false;
  let animate = true;
  let walkDir = 1;
  // 尊重系统「减弱动态效果」无障碍开关
  const reducedMotion = !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);

  // ---------- 素材加载与运行时校验 ----------
  async function loadAll() {
    const states = Object.keys(config.animations);
    const report = {};
    let ok = true;
    for (const st of states) {
      const list = [];
      let i = 0;
      while (true) {
        const url = new URL(`../../assets/pet/${st}/frame_${String(i).padStart(3, "0")}.png`, location.href);
        try {
          const img = await loadImage(url.href);
          if (!img.naturalWidth || img.naturalWidth <= 0) throw new Error("naturalWidth=0");
          if (img.naturalWidth !== config.canvas.width) throw new Error(`size ${img.naturalWidth}!=${config.canvas.width}`);
          list.push(img);
          i++;
          if (i > 64) break;
        } catch {
          break;
        }
      }
      if (list.length === 0) {
        const fb = await loadImage(new URL("../../assets/fallback/pet.svg", location.href).href);
        list.push(fb);
        ok = false;
        report[st] = "fallback";
      } else {
        report[st] = list.length;
      }
      frames[st] = list;
    }
    const lang = (config && config.language) || "zh";
    const bubbleSuffix = lang === "en" ? "_en" : "";
    for (const st of states) {
      try {
        const res = await fetch(new URL(`../../assets/bubbles/${st}${bubbleSuffix}.txt`, location.href));
        if (res.ok) {
          lines[st] = (await res.text()).split("\n").map(s => s.trim()).filter(Boolean);
        }
      } catch { /* 无台词也可运行 */ }
    }
    if (ok) {
      window.petAPI.rendererReady({ frames: report, configVersion: config.version });
    } else {
      window.petAPI.rendererError({ frames: report, reason: "some states missing, fallback engaged" });
    }
  }

  function loadImage(url) {
    return new Promise((resolve, reject) => {
      const img = new Image();
      img.onload = () => resolve(img);
      img.onerror = () => reject(new Error("load fail"));
      img.src = url;
    });
  }

  // 单图层硬切：画面上永远只有一张完整的图，无叠影、无明暗呼吸
  function showFrame(img) {
    if (!img || frameEl.src === img.src) return;
    frameEl.src = img.src;
  }

  // 呼吸只在「静止类」状态启用，动作状态不叠加位移，避免双重运动
  function applyBreath() {
    const spec = config && config.animations[state];
    breathEl.classList.toggle("breathing", !!spec && !!spec.still && animate && !reducedMotion);
  }

  // ---------- 状态机 ----------
  function setState(next) {
    if (!config) return;
    const spec = config.animations[next];
    if (!spec) return;
    if (state !== next) {
      state = next;
      frameIdx = 0;
      lastFrameAt = 0;
      stateEnteredAt = performance.now();
      breathEl.classList.remove("landing");
      if (next === "walk") walkRemain = 2000 + Math.random() * 3000;
      if (next === "idle") sleepSince = performance.now();
      if (next === "drop") {
        void breathEl.offsetWidth;          // 强制重排，重启动画
        breathEl.classList.add("landing");  // 落地冲击用几何弹性表达，不换一张画风不同的图
      }
      if (next === "sleep") { sleepEnteredAt = performance.now(); showBubble("Zzz…"); }
      else if (lines[next] && lines[next].length) showBubble(pick(lines[next]));
      applyBreath();
    }
  }

  const PRIORITY = { greet: 4, interact: 4, drop: 3, drag: 2, walk: 1, idle: 0, sleep: -1 };
  function canInterrupt(cur, next) {
    if (cur === next) return true;
    return (PRIORITY[next] ?? 0) >= (PRIORITY[cur] ?? 0);
  }

  function tick(now) {
    const spec = config.animations[state];
    const arr = frames[state] || [];
    const interval = 1000 / (spec.fps || 4);
    const holdMs = spec.holdMs || arr.length * interval;
    // still 状态 / 关闭动效：锁死第 0 帧，画面永不换像素
    const locked = !animate || !!spec.still || arr.length < 2;
    if (locked) {
      showFrame(arr[0]);
      frameIdx = 0;
      if (!spec.loop && now - stateEnteredAt >= holdMs) setState(spec.next || "idle");
    } else if (now - lastFrameAt >= interval) {
      lastFrameAt = now;
      showFrame(arr[frameIdx]);
      frameIdx++;
      if (frameIdx >= arr.length) {
        if (spec.loop) frameIdx = 0;
        else setState(spec.next || "idle");
      }
    }

    const behaviors = config.behaviors || {};
    // walk 位移与贴边反弹（反弹检测节流，避免每帧一次 IPC 导致窗口抖动）
    if (state === "walk" && animate) {
      const speed = (config.animations.walk.moveSpeed || 42) / (1000 / 16);
      window.petAPI.moveBy(walkDir * speed, 0);
      walkRemain -= 16;
      const bounceMs = (config.motion && config.motion.bounceCheckMs) || 150;
      if (now - lastBounceAt > bounceMs) {
        lastBounceAt = now;
        maybeBounce().then((hit) => { if (hit) walkDir *= -1; });
      }
      if (walkRemain <= 0) setState("idle");
    }
    // idle 超时入睡
    if (state === "idle" && behaviors.idleTimeoutSec) {
      if (now - sleepSince > behaviors.idleTimeoutSec * 1000) setState("sleep");
    }
    // 睡眠随机自动醒
    if (state === "sleep" && behaviors.sleepAutoWakeSec) {
      if (now - sleepEnteredAt > behaviors.sleepAutoWakeSec * 1000) setState("idle");
    }
    // 闲置气泡（安静模式或间隔为 0 时抑制）
    const idleBubbleMs = (behaviors.idleBubbleSec ?? 8) * 1000;
    if (state === "idle" && !quiet && idleBubbleMs > 0 && now - lastBubbleAt > idleBubbleMs && lines.idle && lines.idle.length) {
      showBubble(pick(lines.idle));
      lastBubbleAt = now;
    }
    // 随机散步：关闭动效 / 减弱动态时不触发
    if (state === "idle" && animate && !reducedMotion && behaviors.walkProbPerMinute) {
      if (Math.random() < behaviors.walkProbPerMinute / (60 * 1000 / 16)) setState("walk");
    }
    scheduleTick();
  }

  async function maybeBounce() {
    const { win, work } = await window.petAPI.getMoveBounds();
    const hitLeft = win.x <= work.x + 2;
    const hitRight = win.x + win.width >= work.x + work.width - 2;
    return (walkDir < 0 && hitLeft) || (walkDir > 0 && hitRight);
  }

  // ---------- 气泡 ----------
  let bubbleTimer = null;
  const usedLines = [];
  function pick(arr) {
    if (usedLines.length >= arr.length) usedLines.length = 0;
    let idx;
    do { idx = Math.floor(Math.random() * arr.length); } while (usedLines.includes(idx) && arr.length > 1);
    usedLines.push(idx);
    return arr[idx];
  }
  function showBubble(text, msOverride) {
    if (!config || !config.bubble || !config.bubble.enabled) return;
    bubbleEl.textContent = text;
    bubbleEl.classList.add("show");
    clearTimeout(bubbleTimer);
    // msOverride：外部（父进程 stdin 通道）指定的显示时长，缺省回落到 config
    const holdMs = (typeof msOverride === "number" && msOverride > 0 ? msOverride : (config.bubble.showMs || 2500));
    bubbleTimer = setTimeout(() => bubbleEl.classList.remove("show"), holdMs);
  }

  // ---------- 拖拽与点击（focusable:false 下走 ipc 移窗） ----------
  let pressing = false, moved = false, lastPos = null, lastMoveAt = 0;
  const DRAG_THRESHOLD = 4;
  let clickCycle = 0;

  document.addEventListener("mousedown", (e) => {
    pressing = true; moved = false; lastPos = { x: e.screenX, y: e.screenY };
    lastMoveAt = performance.now();
    stageEl.classList.add("pressed");
  });
  window.addEventListener("mousemove", (e) => {
    if (!pressing) return;
    const dx = e.screenX - lastPos.x, dy = e.screenY - lastPos.y;
    lastMoveAt = performance.now();
    if (!moved && Math.hypot(dx, dy) > DRAG_THRESHOLD) {
      moved = true;
      if (canInterrupt(state, "drag")) setState("drag");
      document.body.classList.add("dragging");
      stageEl.classList.remove("pressed"); // 拖拽期间不做任何缩放反馈
    }
    if (moved) {
      window.petAPI.moveBy(dx, dy);
      lastPos = { x: e.screenX, y: e.screenY };
    }
  });

  // 统一结束出口：窗口跟着鼠标走时 mouseup 很容易落在窗口外而丢失，
  // 一旦丢失 pressing 永远为 true，窗口会一直粘在鼠标上并持续触发移窗 → 越拖越大。
  function finishPointer() {
    if (!pressing) return;
    pressing = false;
    stageEl.classList.remove("pressed");
    document.body.classList.remove("dragging");
    if (moved) {
      setState("drop"); // 落地 → next: idle
    } else {
      setTimeout(() => {
        if (state === "drag" || state === "drop") return;
        const isSleep = state === "sleep";
        if (isSleep) { setState("greet"); }
        else if (canInterrupt(state, "interact") && frames.interact) {
          setState("interact");
          const arr = lines.interact || lines.idle || [];
          if (arr.length) showBubble(arr[clickCycle++ % arr.length]);
        } else if (lines.idle) {
          showBubble(pick(lines.idle));
        }
      }, 250);
    }
    moved = false;
  }
  window.addEventListener("mouseup", () => finishPointer());
  window.addEventListener("blur", () => finishPointer());
  document.addEventListener("mouseleave", () => { if (moved) finishPointer(); });
  // 看门狗：1.2s 内没有任何 mousemove 就视为已松手
  setInterval(() => {
    if (pressing && moved && performance.now() - lastMoveAt > 1200) finishPointer();
  }, 300);
  document.addEventListener("dblclick", () => {
    setState(state === "sleep" ? "greet" : "sleep");
  });

  // ---------- 穿白区穿透（IPC 去重 + alpha 掩码缓存） ----------
  // 原实现每个 mousemove 都发一次 IPC、重建一次 canvas 并读像素，拖拽时把主进程淹没，
  // 移窗排队滞后会加重拖影。现在只在「在宠物上 / 不在宠物上」翻转时发 IPC。
  let lastThrough = null;
  let clickThroughHooked = false;
  let maskKey = "", maskData = null, maskW = 0, maskH = 0;
  function setupClickThrough() {
    if (clickThroughHooked) return;
    clickThroughHooked = true;
    document.addEventListener("mousemove", (e) => {
      const onPet = hitPet(e);
      if (onPet !== lastThrough) {
        lastThrough = onPet;
        window.petAPI.applyClickThrough(!onPet);
      }
      stageEl.classList.toggle("grab-hint", onPet);
    });
  }
  function hitPet(e) {
    if (!frameEl.complete || !frameEl.naturalWidth) return true;
    const r = frameEl.getBoundingClientRect();
    const x = Math.floor((e.clientX - r.left) / r.width * frameEl.naturalWidth);
    const y = Math.floor((e.clientY - r.top) / r.height * frameEl.naturalHeight);
    if (x < 0 || y < 0 || x >= frameEl.naturalWidth || y >= frameEl.naturalHeight) return false;
    if (frameEl.src !== maskKey) {
      const c = document.createElement("canvas");
      c.width = frameEl.naturalWidth; c.height = frameEl.naturalHeight;
      const ctx = c.getContext("2d", { willReadFrequently: true });
      ctx.drawImage(frameEl, 0, 0);
      maskData = ctx.getImageData(0, 0, c.width, c.height).data;
      maskW = c.width; maskH = c.height; maskKey = frameEl.src;
    }
    return maskData[(y * maskW + x) * 4 + 3] > 20;
  }

  // ---------- 动效总开关（托盘） ----------
  function setAnimate(on) {
    animate = !!on;
    document.body.classList.toggle("no-motion", !animate);
    if (!animate) {
      frameIdx = 0;
      setState("idle");
      showFrame(frames.idle && frames.idle[0]);
      breathEl.classList.remove("landing");
    }
    applyBreath();
  }

  // ---------- 首次引导（缩短到 2 句，高冷设定不连番冒泡） ----------
  async function runOnboarding() {
    const seq = ["……拖我能移动。", "双击，我要睡了。"];
    for (const t of seq) {
      showBubble(t);
      await new Promise((r) => setTimeout(r, 2000));
    }
    window.petAPI.markOnboarded();
  }

  // ---------- 调度：静止状态降频到 4Hz，避免 60fps 空转 ----------
  function scheduleTick() {
    if (state === "idle" || state === "sleep" || !animate)
      setTimeout(() => tick(performance.now()), 250);
    else requestAnimationFrame(tick);
  }

  // ---------- 启动 ----------
  fetch(cfgUrl).then(r => r.json()).then(async (c) => {
    config = c;
    const mo = config.motion || {};
    document.documentElement.style.setProperty("--breath-px", `${mo.breathPx ?? 2}px`);
    document.documentElement.style.setProperty("--breath-sec", `${mo.breathSec ?? 4.2}s`);
    await loadAll();

    // 设置一律在 config / 帧资产就绪后再应用，避免竞态
    const s = await window.petAPI.getSettings();
    if (s && s.clickThrough) setupClickThrough();
    if (s && s.quiet) quiet = true;
    if (s && typeof s.animate === "boolean") setAnimate(s.animate);
    window.petAPI.onClickThroughChanged((on) => {
      if (on) setupClickThrough();
      else { window.petAPI.applyClickThrough(false); lastThrough = null; }
    });
    window.petAPI.onQuietChanged((on) => { quiet = on; });
    window.petAPI.onAnimateChanged((on) => setAnimate(on));
    // 外部气泡通道：父进程经 stdin 发来的气泡，自带显示时长
    window.petAPI.onBubble((text, ms) => showBubble(text, ms));
    window.petAPI.onPetSleep(() => { setState(state === "sleep" ? "idle" : "sleep"); });
    document.addEventListener("contextmenu", (e) => {
      e.preventDefault();
      window.petAPI.contextMenu(e.screenX, e.screenY);
    });

    setState("greet");
    applyBreath();
    scheduleTick();
    if (config.bubble && config.bubble.enabled) {
      window.petAPI.getOnboarded().then((done) => { if (!done) runOnboarding(); });
    }
  });
})();
