// renderer.js — 稳定优先版
// 关键决策：待机/睡觉/落地锁单帧（原始立绘），动效用平滑几何变换；
// 动作帧一律单图层硬切。双层 crossfade 会把两张不同姿势半透明叠成鬼影，那是闪烁主因。
(() => {
  const stageEl = document.getElementById("stage");
  const breathEl = document.getElementById("breath");
  const frameEl = document.getElementById("frame");
  const bubbleEl = document.getElementById("bubble");

  // 可见帧图也走匿名 CORS 加载：hitPet 会把它 drawImage 到 canvas 读像素，
  // 少了这个属性画布会被判成跨源污染，getImageData 抛 SecurityError（CORS 头由素材协议侧回）
  frameEl.crossOrigin = "anonymous";

  let config = null;
  // 素材根：assetBase 是当前宠物包，builtinBase 是随包内置素材（整状态回落时用）
  let assetBase = "";
  let builtinBase = "";
  const frames = {};   // state -> [Image...]
  const lines = {};    // state -> [string...]
  let state = "idle";
  let frameIdx = 0;
  let lastFrameAt = 0;
  let lastTickAt = performance.now();
  let stateEnteredAt = 0;
  let walkRemain = 0;
  let sitRemain = 0;
  let sitLoopIdx = 0;
  let sleepSince = 0;
  let lastBubbleAt = 0;
  let sleepEnteredAt = 0;
  let lastBounceAt = 0;
  let nextBlinkAt = performance.now() + 2600;
  let blinkUntil = 0;
  let doubleBlinkPending = false;
  let nextTiltAt = performance.now() + 11000;
  let tiltUntil = 0;
  let autonomousCooldownUntil = 0;
  let quiet = false;
  let animate = true;
  let walkDir = -1;
  let lastActivityAt = performance.now();
  // 尊重系统「减弱动态效果」无障碍开关
  const reducedMotion = !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);

  // ---------- 素材加载与运行时校验 ----------
  // 两个素材根都走 pet-asset 协议：与页面同源，画布不会被污染
  function assetUrl(rel, base) {
    return `${base || assetBase}/${rel}`;
  }

  // 单个状态整组加载：只从给定素材根读，读到第一张缺帧就停（探测式，不需要预先知道帧数）
  async function loadFrames(st, base) {
    const list = [];
    for (let i = 0; i <= 64; i++) {
      try {
        const url = assetUrl(`pet/${st}/frame_${String(i).padStart(3, "0")}.png`, base);
        const img = await loadImage(url);
        if (!img.naturalWidth || img.naturalWidth <= 0) throw new Error("naturalWidth=0");
        if (img.naturalWidth !== config.canvas.width) throw new Error(`size ${img.naturalWidth}!=${config.canvas.width}`);
        list.push(img);
      } catch {
        break;
      }
    }
    return list;
  }

  async function loadAll() {
    const states = Object.keys(config.animations);
    const report = {};
    let ok = true;
    for (const st of states) {
      // 先按当前宠物包加载；这一整个状态一帧都没有时，才整组换成内置素材。
      // 绝不逐帧混用：否则自绘宠物没画 walk 时，走路动作会在两只宠物的姿势之间来回跳。
      let list = await loadFrames(st, assetBase);
      if (list.length === 0 && builtinBase && builtinBase !== assetBase) {
        list = await loadFrames(st, builtinBase);
      }
      if (list.length === 0) {
        // 兜底矢量图固定取内置素材：宠物包不要求自带 fallback
        const fb = await loadImage(assetUrl("fallback/pet.svg", builtinBase));
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
        const res = await fetch(assetUrl(`bubbles/${st}${bubbleSuffix}.txt`));
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
      // 匿名跨源加载：帧图之后会被 drawImage 到 canvas 读像素做点击命中检测，
      // 不声明 crossOrigin 会被判成污染源（协议侧已回 Access-Control-Allow-Origin）。
      // 失败仍按 load fail reject：帧探测正是靠这个 reject 判断「没有更多帧」。
      img.crossOrigin = "anonymous";
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

  // 呼吸只在「静止 / 坐下」类状态启用，走路与大幅动作不叠加位移，避免双重运动
  function applyBreath() {
    const spec = config && config.animations[state];
    breathEl.classList.toggle("breathing", !!spec && (!!spec.breath || !!spec.still) && animate && !reducedMotion);
  }

  function applyFacing() {
    frameEl.classList.toggle("flipped", state === "walk" && walkDir > 0);
  }

  // 互动反馈：复用落地动画（弹性缩放）当「被摸到」的即时反馈，不额外新增素材
  function bounceFeedback() {
    breathEl.classList.remove("landing");
    void breathEl.offsetWidth;          // 强制重排，重启动画
    breathEl.classList.add("landing");
  }

  // ---------- 状态机 ----------
  const SIT_LOOP = [1, 2, 1, 3];
  const IDLE_AUTO_ACTIONS = ["stretch", "sit", "warm_hands", "look_around"];

  function pickWalkDirection() {
    window.petAPI.getMoveBounds().then(({ win, work }) => {
      if (state !== "walk") return;
      if (win.x + win.width >= work.x + work.width - 90) {
        walkDir = -1;
      } else if (win.x <= work.x + 90) {
        walkDir = 1;
      } else {
        walkDir = Math.random() < 0.5 ? -1 : 1;
      }
      applyFacing();
    }).catch(() => {});
  }

  function setState(next, suppressBubble = false) {
    if (!config) return;
    const spec = config.animations[next];
    if (!spec) return;
    if (state !== next) {
      state = next;
      frameIdx = 0;
      lastFrameAt = 0;
      stateEnteredAt = performance.now();
      breathEl.classList.remove("landing");
      if (next === "walk") {
        walkRemain = 3200 + Math.random() * 4400;
        walkDir = Math.random() < 0.5 ? -1 : 1;
        pickWalkDirection();
      }
      if (next === "sit") {
        sitRemain = 8000 + Math.random() * 6000;
        sitLoopIdx = 0;
      }
      if (next === "idle") {
        sleepSince = performance.now();
        nextBlinkAt = sleepSince + 2000 + Math.random() * 3200;
        nextTiltAt = sleepSince + 8000 + Math.random() * 9000;
        autonomousCooldownUntil = Math.max(autonomousCooldownUntil, sleepSince + 2500);
      }
      if (next === "drop") {
        void breathEl.offsetWidth;          // 强制重排，重启动画
        breathEl.classList.add("landing");  // 落地冲击用几何弹性表达
      }
      if (!suppressBubble && !currentBubbleSticky) {
        if (next === "sleep") { sleepEnteredAt = performance.now(); showBubble("Zzz…"); }
        else if (lines[next] && lines[next].length && !quiet) showBubble(pick(lines[next]));
      } else if (next === "sleep") {
        sleepEnteredAt = performance.now();
      }
      applyBreath();
      applyFacing();
    }
  }

  // drag 优先级最高：任何时候都要能拖动宠物，不能被 greet/interact 这些动作卡住
  const PRIORITY = {
    drag: 5,
    greet: 4,
    interact: 4,
    happy: 4,
    shy: 3,
    drop: 3,
    stretch: 2,
    warm_hands: 2,
    sit: 1,
    look_around: 1,
    walk: 1,
    idle: 0,
    sleep: -1,
  };
  function canInterrupt(cur, next) {
    if (cur === next) return true;
    return (PRIORITY[next] ?? 0) >= (PRIORITY[cur] ?? 0);
  }

  function markActivity() {
    const now = performance.now();
    lastActivityAt = now;
    autonomousCooldownUntil = now + 4500;
    // 「说句话」这类不换状态的操作也算活动，不能让原来的闲置入睡计时继续偷跑。
    if (state === "idle") sleepSince = now;
  }

  function tickIdleFrames(now, arr) {
    if (!animate || reducedMotion || arr.length < 2) {
      showFrame(arr[0]);
      return;
    }
    if (now >= nextBlinkAt) {
      blinkUntil = now + 135;
      if (!doubleBlinkPending && Math.random() < 0.25) {
        doubleBlinkPending = true;
        nextBlinkAt = now + 260;
      } else {
        doubleBlinkPending = false;
        nextBlinkAt = now + 2800 + Math.random() * 3400;
      }
    }
    if (arr.length >= 3 && now >= nextTiltAt) {
      tiltUntil = now + 950;
      nextTiltAt = now + 10000 + Math.random() * 10000;
    }
    if (now < blinkUntil) showFrame(arr[1]);
    else if (arr.length >= 3 && now < tiltUntil) showFrame(arr[2]);
    else showFrame(arr[0]);
  }

  function tick(now) {
    const dt = Math.min(Math.max(now - lastTickAt, 8), 80);
    lastTickAt = now;
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
    } else if (state === "idle") {
      tickIdleFrames(now, arr);
    } else if (state === "sit") {
      if (now - lastFrameAt >= interval) {
        lastFrameAt = now;
        if (frameIdx === 0) {
          showFrame(arr[0]);
          frameIdx = 1;
        } else {
          const targetIdx = SIT_LOOP[sitLoopIdx % SIT_LOOP.length];
          showFrame(arr[Math.min(targetIdx, arr.length - 1)]);
          sitLoopIdx++;
        }
      }
      sitRemain -= dt;
      if (sitRemain <= 0) setState("idle");
    } else if (now - lastFrameAt >= interval) {
      lastFrameAt = now;
      const safeIdx = Math.min(frameIdx, arr.length - 1);
      showFrame(arr[safeIdx]);
      frameIdx++;
      if (frameIdx >= arr.length) {
        if (spec.loop) frameIdx = 0;
        else {
          // 播完一轮后短暂停留再回 idle；有明确短动作（如 drop）不拖延 2 秒
          const tailPause = state === "drop" ? 80 : 900;
          if (now - stateEnteredAt >= holdMs + tailPause) {
            setState(spec.next || "idle");
          } else {
            frameIdx = arr.length - 1;
          }
        }
      }
    }

    const behaviors = config.behaviors || {};
    // walk 位移与贴边反弹（反弹检测节流，避免每帧一次 IPC 导致窗口抖动）
    if (state === "walk" && animate) {
      const speedPx = (config.animations.walk.moveSpeed || 48) * (dt / 1000);
      window.petAPI.moveBy(walkDir * speedPx, 0);
      walkRemain -= dt;
      const bounceMs = (config.motion && config.motion.bounceCheckMs) || 150;
      if (now - lastBounceAt > bounceMs) {
        lastBounceAt = now;
        maybeBounce().then((hit) => {
          if (hit) {
            walkDir *= -1;
            applyFacing();
          }
        });
      }
      if (walkRemain <= 0) {
        const roll = Math.random();
        if (roll < 0.30 && frames.look_around) setState("look_around");
        else if (roll < 0.55 && frames.sit) setState("sit");
        else if (roll < 0.70 && frames.stretch) setState("stretch");
        else setState("idle");
      }
    }
    // idle 超时入睡（正显示常驻待确认提示时保持清醒等待）
    if (state === "idle" && !currentBubbleSticky && behaviors.idleTimeoutSec) {
      if (now - sleepSince > behaviors.idleTimeoutSec * 1000) setState("sleep");
    }
    // 睡眠随机自动醒
    if (state === "sleep" && behaviors.sleepAutoWakeSec) {
      if (now - sleepEnteredAt > behaviors.sleepAutoWakeSec * 1000) setState("idle");
    }
    // 闲置气泡（安静模式、间隔为 0 或正显示常驻待确认提示时抑制）
    const idleBubbleMs = (behaviors.idleBubbleSec ?? 8) * 1000;
    if (state === "idle" && !quiet && !currentBubbleSticky && idleBubbleMs > 0 && now - lastBubbleAt > idleBubbleMs && lines.idle && lines.idle.length) {
      showBubble(pick(lines.idle));
      lastBubbleAt = now;
    }
    // 自主行为：散步 + 伸懒腰 / 坐下 / 暖手 / 张望（关闭动效、减弱动态或待确认时抑制）
    if (state === "idle" && !currentBubbleSticky && animate && !reducedMotion && now >= autonomousCooldownUntil) {
      if (behaviors.walkProbPerMinute && Math.random() < behaviors.walkProbPerMinute * (dt / 60000)) {
        setState("walk");
      } else if (behaviors.idleActionProbPerMinute && Math.random() < behaviors.idleActionProbPerMinute * (dt / 60000)) {
        const pool = IDLE_AUTO_ACTIONS.filter((k) => config.animations[k] && frames[k] && frames[k].length);
        if (pool.length) setState(pool[Math.floor(Math.random() * pool.length)]);
      }
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
  let currentBubbleKind = "";
  let currentBubbleSticky = false;
  const BUBBLE_KIND_CLASSES = ["kind-finish", "kind-error", "kind-question", "kind-permission", "kind-cancel"];
  const usedLines = [];
  function pick(arr) {
    if (usedLines.length >= arr.length) usedLines.length = 0;
    let idx;
    do { idx = Math.floor(Math.random() * arr.length); } while (usedLines.includes(idx) && arr.length > 1);
    usedLines.push(idx);
    return arr[idx];
  }
  function triggerNotifyEmote(emote) {
    if (!animate || reducedMotion || state === "drag" || state === "drop") return;
    markActivity();
    const target = emote && config && config.animations && config.animations[emote] && frames[emote] && frames[emote].length
      ? emote
      : "";
    if (target) {
      if (state === target) {
        frameIdx = 0;
        lastFrameAt = 0;
        stateEnteredAt = performance.now();
      } else {
        setState(target, true);
      }
    } else if (state === "sleep") {
      setState("idle", true);
    }
    bounceFeedback();
  }
  function clearBubble(kinds) {
    if (Array.isArray(kinds) && kinds.length > 0 && !kinds.includes(currentBubbleKind)) return;
    clearTimeout(bubbleTimer);
    bubbleTimer = null;
    currentBubbleKind = "";
    currentBubbleSticky = false;
    bubbleEl.classList.remove("show", "sticky", ...BUBBLE_KIND_CLASSES);
  }
  function showBubble(text, msOverride, meta) {
    if (!config || !config.bubble || !config.bubble.enabled) return;
    const info = meta && typeof meta === "object" ? meta : {};
    const kind = typeof info.kind === "string" ? info.kind.trim() : "";
    const sticky = info.sticky === true;
    bubbleEl.classList.remove("sticky", ...BUBBLE_KIND_CLASSES);
    if (kind && BUBBLE_KIND_CLASSES.includes(`kind-${kind}`)) {
      bubbleEl.classList.add(`kind-${kind}`);
    }
    if (sticky) bubbleEl.classList.add("sticky");
    currentBubbleKind = kind;
    currentBubbleSticky = sticky;
    bubbleEl.textContent = text;
    bubbleEl.classList.add("show");
    clearTimeout(bubbleTimer);
    bubbleTimer = null;
    if (info.emote) triggerNotifyEmote(info.emote);
    if (!sticky) {
      // msOverride：外部（父进程 stdin 通道）指定的显示时长，缺省回落到 config
      const holdMs = (typeof msOverride === "number" && msOverride > 0 ? msOverride : (config.bubble.showMs || 2500));
      bubbleTimer = setTimeout(() => {
        currentBubbleKind = "";
        currentBubbleSticky = false;
        bubbleEl.classList.remove("show", "sticky", ...BUBBLE_KIND_CLASSES);
      }, holdMs);
    }
  }

  // ---------- 拖拽与点击（focusable:false 下走 ipc 移窗） ----------
  let pressing = false, moved = false, lastPos = null, lastMoveAt = 0;
  const DRAG_THRESHOLD = 4;
  let clickCycle = 0;

  document.addEventListener("mousedown", (e) => {
    markActivity();
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
      if (currentBubbleSticky) clearBubble();
      setTimeout(() => {
        if (state === "drag" || state === "drop") return;
        const isSleep = state === "sleep";
        if (isSleep) { setState("greet"); }
        else if (canInterrupt(state, "interact") && frames.interact) {
          const clickStates = ["interact", "happy", "shy"].filter((k) => frames[k] && config.animations[k]);
          const chosen = clickStates.length > 1 && Math.random() < 0.4
            ? clickStates[1 + Math.floor(Math.random() * (clickStates.length - 1))]
            : "interact";
          setState(chosen);
          const arr = (lines[chosen] && lines[chosen].length ? lines[chosen] : (lines.interact || lines.idle)) || [];
          if (arr.length && !quiet) showBubble(arr[clickCycle++ % arr.length]);
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
    let x = Math.floor((e.clientX - r.left) / r.width * frameEl.naturalWidth);
    const y = Math.floor((e.clientY - r.top) / r.height * frameEl.naturalHeight);
    if (frameEl.classList.contains("flipped")) {
      x = frameEl.naturalWidth - 1 - x;
    }
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
    applyFacing();
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

  // ---------- 调度：静止/睡眠降频，避免 60fps 空转 ----------
  function scheduleTick() {
    if (!animate || state === "sleep")
      setTimeout(() => tick(performance.now()), 250);
    else if (state === "idle" || state === "sit")
      setTimeout(() => tick(performance.now()), 100);
    else requestAnimationFrame(tick);
  }

  // ---------- 启动 ----------
  (async () => {
    // 皮肤信息由主进程按 YCODE_PET_* 解析好（含配置合并），渲染端只管用，不再自己 fetch 配置。
    // getSkin 可能不存在（旧版 preload）或直接 reject，这里必须显式兜底：宁可静止也绝不白屏。
    let skin = null;
    try {
      if (window.petAPI && typeof window.petAPI.getSkin === "function") {
        skin = await window.petAPI.getSkin();
      }
    } catch (err) {
      console.error("[pet] getSkin failed:", err);
      if (window.petAPI && window.petAPI.rendererError) {
        window.petAPI.rendererError({ reason: "getSkin failed", error: String((err && err.message) || err) });
      }
    }
    // 没有配置就无从得知画布尺寸、状态表与动效参数，空转只会抛错，直接收工
    if (!skin || !skin.config || !skin.config.animations) {
      if (window.petAPI && window.petAPI.rendererError) {
        window.petAPI.rendererError({ reason: "skin config unavailable" });
      }
      return;
    }
    assetBase = skin.assetsBase || "pet-asset://skin";
    builtinBase = skin.builtinBase || "pet-asset://builtin";
    config = skin.config;
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
    // 外部气泡通道：父进程经 stdin 发来的气泡，自带显示时长与样式/动作语义
    window.petAPI.onBubble((text, ms, meta) => showBubble(text, ms, meta));
    if (typeof window.petAPI.onBubbleClear === "function") {
      window.petAPI.onBubbleClear((kinds) => clearBubble(kinds));
    }
    window.petAPI.onPetSleep(() => {
      markActivity();
      setState(state === "sleep" ? "idle" : "sleep");
    });
    // 右键菜单只发语义，动画与台词仍走状态机；talk 保持原行为，不切换动作。
    window.petAPI.onEmote((kind) => {
      markActivity();
      if (kind === "talk") {
        // 「让她说句话」不换动作，只冒一句现有台词 + 轻微弹跳，避免每次点都像抽风
        const arr = (lines.interact && lines.interact.length ? lines.interact : lines.idle) || [];
        if (arr.length) showBubble(pick(arr));
        bounceFeedback();
        return;
      }
      const allowed = config && config.animations && config.animations[kind] ? kind : "interact";
      if (frames[allowed]) {
        setState(allowed);      // setState 内部会按 lines[next] 自动冒一句台词
        if (allowed !== "walk" && allowed !== "sit") bounceFeedback();
      } else {
        bounceFeedback();    // 皮肤包缺这个状态时至少给一个动作反馈
      }
    });
    document.addEventListener("contextmenu", (e) => {
      e.preventDefault();
      // 传窗口内容区坐标（clientX/clientY）：menu.popup 在 Windows 上按窗口坐标定位，
      // 传 screenX/screenY 会让菜单被推到一个窗口外的偏移位置（离宠物很远）
      window.petAPI.contextMenu(e.clientX, e.clientY);
    });

    setState("greet");
    applyBreath();
    scheduleTick();
    if (config.bubble && config.bubble.enabled) {
      window.petAPI.getOnboarded().then((done) => { if (!done) runOnboarding(); });
    }
  })().catch((err) => {
    // 启动链上任何未预期异常都记一笔到 gate 日志，避免只留一个静默白屏
    console.error("[pet] startup failed:", err);
    if (window.petAPI && window.petAPI.rendererError) {
      window.petAPI.rendererError({ reason: "startup failed", error: String((err && err.message) || err) });
    }
  });
})();
