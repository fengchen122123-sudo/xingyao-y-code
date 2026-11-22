(() => {
  const stageEl = document.getElementById("stage");
  const breathEl = document.getElementById("breath");
  const frameEl = document.getElementById("frame");
  const bubbleEl = document.getElementById("bubble");

  frameEl.crossOrigin = "anonymous";

  let config = null;
  let assetBase = "";
  let builtinBase = "";
  const frames = {};
  const lines = {};
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
  const reducedMotion = !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);

  function assetUrl(rel, base) {
    return `${base || assetBase}/${rel}`;
  }

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
      } catch { }
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
      img.crossOrigin = "anonymous";
      img.onload = () => resolve(img);
      img.onerror = () => reject(new Error("load fail"));
      img.src = url;
    });
  }

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

  function bounceFeedback() {
    breathEl.classList.remove("landing");
    void breathEl.offsetWidth;
    breathEl.classList.add("landing");
  }

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
        void breathEl.offsetWidth;
        breathEl.classList.add("landing");
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
    if (state === "idle" && !currentBubbleSticky && behaviors.idleTimeoutSec) {
      if (now - sleepSince > behaviors.idleTimeoutSec * 1000) setState("sleep");
    }
    if (state === "sleep" && behaviors.sleepAutoWakeSec) {
      if (now - sleepEnteredAt > behaviors.sleepAutoWakeSec * 1000) setState("idle");
    }
    const idleBubbleMs = (behaviors.idleBubbleSec ?? 8) * 1000;
    if (state === "idle" && !quiet && !currentBubbleSticky && idleBubbleMs > 0 && now - lastBubbleAt > idleBubbleMs && lines.idle && lines.idle.length) {
      showBubble(pick(lines.idle));
      lastBubbleAt = now;
    }
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
      const holdMs = (typeof msOverride === "number" && msOverride > 0 ? msOverride : (config.bubble.showMs || 2500));
      bubbleTimer = setTimeout(() => {
        currentBubbleKind = "";
        currentBubbleSticky = false;
        bubbleEl.classList.remove("show", "sticky", ...BUBBLE_KIND_CLASSES);
      }, holdMs);
    }
  }

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
      stageEl.classList.remove("pressed");
    }
    if (moved) {
      window.petAPI.moveBy(dx, dy);
      lastPos = { x: e.screenX, y: e.screenY };
    }
  });

  function finishPointer() {
    if (!pressing) return;
    pressing = false;
    stageEl.classList.remove("pressed");
    document.body.classList.remove("dragging");
    if (moved) {
      setState("drop");
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
  setInterval(() => {
    if (pressing && moved && performance.now() - lastMoveAt > 1200) finishPointer();
  }, 300);
  document.addEventListener("dblclick", () => {
    setState(state === "sleep" ? "greet" : "sleep");
  });

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

  async function runOnboarding() {
    const seq = ["……拖我能移动。", "双击，我要睡了。"];
    for (const t of seq) {
      showBubble(t);
      await new Promise((r) => setTimeout(r, 2000));
    }
    window.petAPI.markOnboarded();
  }

  function scheduleTick() {
    if (!animate || state === "sleep")
      setTimeout(() => tick(performance.now()), 250);
    else if (state === "idle" || state === "sit")
      setTimeout(() => tick(performance.now()), 100);
    else requestAnimationFrame(tick);
  }

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
    window.petAPI.onBubble((text, ms, meta) => showBubble(text, ms, meta));
    if (typeof window.petAPI.onBubbleClear === "function") {
      window.petAPI.onBubbleClear((kinds) => clearBubble(kinds));
    }
    window.petAPI.onPetSleep(() => {
      markActivity();
      setState(state === "sleep" ? "idle" : "sleep");
    });
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
        setState(allowed);
        if (allowed !== "walk" && allowed !== "sit") bounceFeedback();
      } else {
        bounceFeedback();
      }
    });
    document.addEventListener("contextmenu", (e) => {
      e.preventDefault();
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
