/**
 * src/audio.js — 音频模块
 *
 * 规格来源：SPEC.md §2.3（接口契约）/ §3.9（音频规格）
 *
 * 设计要点：
 *  - 纯 ESM，零外部依赖；不 fetch 任何音频文件，不引用 document（speechSynthesis 除外）。
 *  - 背景音乐全程 WebAudio 实时合成：C–G–Am–F 四和弦循环，110 BPM，
 *    尤克里里式拨弦（三角波 + 快速指数衰减包络 + 轻微失谐第二根弦），
 *    口哨旋律（正弦波 + 约 4Hz 颤音，中高音区），轻打击（白噪声短脉冲 + 带通）。
 *  - 采用「提前调度音符」的 lookahead 调度器：每 0.1s 检查一次、调度未来 0.5s 内的音符，
 *    时间基准取自 AudioContext.currentTime（单调时钟）并逐音符累加固定步长，
 *    因此长时间循环不会漂移；只调度「未来一小段」而非一次性生成超长 buffer。
 *  - 每个发声节点都在结束后 disconnect，避免长期运行后音频节点泄漏。
 */

// ---------------------------------------------------------------------------
// 音乐常量（模块级只读，供多个 createAudio 实例共享）
// ---------------------------------------------------------------------------

/** 每分钟拍数 */
const BPM = 110;
/** 每拍秒数 */
const SPB = 60 / BPM; // ≈ 0.54545s
/** 八分音符步长（调度器的最小时间栅格） */
const STEP = SPB / 2; // ≈ 0.27273s
/** 每小节 8 个八分音符步 */
const STEPS_PER_BAR = 8;
/** 和弦进行：C – G – Am – F，每小节一个和弦 */
const CHORDS = [
  { name: 'C', notes: [60, 64, 67, 72] }, // C4 E4 G4 C5
  { name: 'G', notes: [55, 59, 62, 67] }, // G3 B3 D4 G4
  { name: 'Am', notes: [57, 60, 64, 69] }, // A3 C4 E4 A4
  { name: 'F', notes: [53, 57, 60, 65] }, // F3 A3 C4 F4
];
/** 口哨旋律（32 个八分格 = 4 小节，null 为休止），中高音区（A4–G5） */
const MELODY = [
  // C  (第 1 小节)
  76, null, 79, null, 76, null, 72, null,
  // G  (第 2 小节)
  74, null, 79, null, 74, null, 71, null,
  // Am (第 3 小节)
  72, null, 76, null, 79, null, 76, null,
  // F  (第 4 小节)
  69, null, 72, null, 77, null, 76, null,
];

/** §3.9 默认中文台词（原文照抄） */
const DEFAULT_LINE =
  '嗨！我是佩利，一只热爱骑车的鹈鹕。系好安全带……哦不，是戴好头盔！拖动屏幕转转视角，按空格键我就按铃，我们出发吧！';

/** 车铃两声的频率（Hz）与间隔（s） */
const BELL_FREQS = [2093, 2637];
const BELL_GAP = 0.12;
/** 车铃泛音倍频 */
const BELL_PARTIALS = [1, 2.76, 5.4];
const BELL_PARTIAL_AMPS = [0.5, 0.24, 0.1];

/** 调度器参数（SPEC：每 0.1s 检查、调度未来 0.5s） */
const LOOKAHEAD = 0.1;
const SCHEDULE_AHEAD = 0.5;

/** MIDI 音高 → 频率 */
function midiToFreq(midi) {
  return 440 * Math.pow(2, (midi - 69) / 12);
}

// ---------------------------------------------------------------------------
// 工厂函数
// ---------------------------------------------------------------------------

/**
 * 创建音频系统（不播放）。所有播放方法内部自行确保 AudioContext 已 resume。
 * @returns {{
 *   resume(): Promise<void>,
 *   bell(): void,
 *   call(): void,
 *   startMusic(): void,
 *   toggleMusic(): boolean,
 *   isMusicOn(): boolean,
 *   speak(text?: string): void,
 *   setVolume(v: number): void
 * }}
 */
export function createAudio() {
  // ---- 运行期状态 ----
  let ctx = null; // AudioContext
  let master = null; // 主增益（音乐总线），setVolume 控制
  let musicBus = null; // 音乐总线增益（用于音乐开关淡入淡出）
  let musicOn = false;
  let volume = 0.45; // 默认音量（§3.9）
  let noiseBuffer = null; // 复用白噪声 buffer
  // 调度器状态
  let schedulerTimer = null;
  let nextStepTime = 0;
  let stepIndex = 0;

  // -------------------------------------------------------------------------
  // 上下文 / 总线
  // -------------------------------------------------------------------------

  /** 惰性创建 AudioContext 与总线（不 resume）。兼容 webkitAudioContext。 */
  function ensureContext() {
    if (ctx) return ctx;
    const AC =
      typeof window !== 'undefined'
        ? window.AudioContext || window.webkitAudioContext
        : null;
    if (!AC) return null;
    try {
      ctx = new AC();
    } catch (e) {
      ctx = null;
      return null;
    }
    // master（主增益 / 音乐总线）：默认音量
    master = ctx.createGain();
    master.gain.value = volume;
    master.connect(ctx.destination);
    // musicBus：音乐专用，便于开关时淡入淡出（铃/叫不经过它）
    musicBus = ctx.createGain();
    musicBus.gain.value = 1.0;
    musicBus.connect(master);
    return ctx;
  }

  /** 确保上下文已创建并尽力恢复（不在手势中时 resume 可能挂起，但不报错）。 */
  function ensureRunning() {
    const c = ensureContext();
    if (!c) return null;
    if (c.state === 'suspended') {
      try {
        c.resume();
      } catch (e) {
        /* 忽略：无手势时恢复失败是预期行为 */
      }
    }
    return c;
  }

  /** resume()：创建/恢复 AudioContext，返回 Promise；应在用户手势回调里调用。 */
  function resume() {
    const c = ensureContext();
    if (!c) return Promise.resolve();
    if (c.state === 'running') return Promise.resolve();
    try {
      const p = c.resume();
      if (p && typeof p.then === 'function') {
        return p.then(
          function () {},
          function () {}
        );
      }
    } catch (e) {
      /* 忽略 */
    }
    return Promise.resolve();
  }

  /** setVolume(v)：控制主增益（音乐总线），默认 0.45。 */
  function setVolume(v) {
    const val = Number(v);
    if (!isFinite(val)) return;
    volume = val < 0 ? 0 : val > 1 ? 1 : val;
    if (master && ctx) {
      try {
        master.gain.setTargetAtTime(volume, ctx.currentTime, 0.02);
      } catch (e) {
        master.gain.value = volume;
      }
    }
  }

  // -------------------------------------------------------------------------
  // 基础发声构件
  // -------------------------------------------------------------------------

  /** 复用的 1 秒白噪声 buffer。 */
  function getNoiseBuffer() {
    if (noiseBuffer) return noiseBuffer;
    const len = Math.floor(ctx.sampleRate * 1.0);
    const buf = ctx.createBuffer(1, len, ctx.sampleRate);
    const data = buf.getChannelData(0);
    for (let i = 0; i < len; i++) data[i] = Math.random() * 2 - 1;
    noiseBuffer = buf;
    return buf;
  }

  /**
   * 尤克里里拨弦：三角波 + 快速指数衰减包络；叠加一根轻微失谐的第二根弦。
   * @param {number} midi MIDI 音高
   * @param {number} when 起始时间（AudioContext 时间轴）
   * @param {number} amp  峰值振幅
   */
  function pluck(midi, when, amp) {
    if (!ctx) return;
    const freq = midiToFreq(midi);
    const dur = 0.9;
    for (let k = 0; k < 2; k++) {
      const osc = ctx.createOscillator();
      osc.type = 'triangle';
      osc.frequency.setValueAtTime(freq, when);
      if (k === 1) osc.detune.setValueAtTime(7, when); // 轻微失谐（约 7 音分）
      const g = ctx.createGain();
      const a = amp * (k === 0 ? 1 : 0.5);
      g.gain.setValueAtTime(0.0001, when);
      g.gain.exponentialRampToValueAtTime(a, when + 0.008); // 快速起音
      g.gain.exponentialRampToValueAtTime(0.0001, when + dur); // 指数衰减
      osc.connect(g);
      g.connect(musicBus);
      osc.start(when);
      osc.stop(when + dur + 0.02);
      osc.onended = function () {
        try {
          g.disconnect();
        } catch (e) {}
      };
    }
  }

  /** 一次扫弦：和弦内各弦按 12ms 逐根拨响，模拟扫弦。 */
  function strum(midis, when, amp) {
    for (let i = 0; i < midis.length; i++) {
      pluck(midis[i], when + i * 0.012, amp);
    }
  }

  /**
   * 口哨：正弦波 + 约 4Hz 颤音；中高音区。
   * @param {number} midi MIDI 音高
   * @param {number} when 起始时间
   * @param {number} amp  峰值振幅
   */
  function whistle(midi, when, amp) {
    if (!ctx) return;
    const freq = midiToFreq(midi);
    const dur = 0.5;
    const osc = ctx.createOscillator();
    osc.type = 'sine';
    osc.frequency.setValueAtTime(freq, when);
    // 颤音：4Hz LFO → 频率参数（约 ±1.2%）
    const vib = ctx.createOscillator();
    vib.type = 'sine';
    vib.frequency.value = 4;
    const vibGain = ctx.createGain();
    vibGain.gain.value = freq * 0.012;
    vib.connect(vibGain);
    vibGain.connect(osc.frequency);
    const g = ctx.createGain();
    g.gain.setValueAtTime(0.0001, when);
    g.gain.exponentialRampToValueAtTime(amp, when + 0.04);
    g.gain.exponentialRampToValueAtTime(0.0001, when + dur);
    osc.connect(g);
    g.connect(musicBus);
    osc.start(when);
    vib.start(when);
    osc.stop(when + dur);
    vib.stop(when + dur);
    osc.onended = function () {
      try {
        g.disconnect();
        vibGain.disconnect();
      } catch (e) {}
    };
  }

  /** 轻打击：白噪声短脉冲 + 带通（类踩镲）。 */
  function tick(when, amp) {
    if (!ctx) return;
    const dur = 0.06;
    const src = ctx.createBufferSource();
    src.buffer = getNoiseBuffer();
    const bp = ctx.createBiquadFilter();
    bp.type = 'bandpass';
    bp.frequency.value = 6000;
    bp.Q.value = 1.2;
    const g = ctx.createGain();
    g.gain.setValueAtTime(0.0001, when);
    g.gain.exponentialRampToValueAtTime(amp, when + 0.004);
    g.gain.exponentialRampToValueAtTime(0.0001, when + dur);
    src.connect(bp);
    bp.connect(g);
    g.connect(musicBus);
    src.start(when);
    src.stop(when + dur);
    src.onended = function () {
      try {
        bp.disconnect();
        g.disconnect();
      } catch (e) {}
    };
  }

  // -------------------------------------------------------------------------
  // 调度器（lookahead）
  // -------------------------------------------------------------------------

  /** 调度单个八分格上的所有乐器事件。 */
  function scheduleStep(when, step) {
    if (!ctx) return;
    const bar = Math.floor(step / STEPS_PER_BAR) % 4;
    const s = step % STEPS_PER_BAR;
    const chord = CHORDS[bar];

    // 尤克里里扫弦：强拍（0/4）全扫，弱拍（2/6）轻扫 —— 「下 下 上 上」型海岛节奏
    if (s === 0 || s === 4) strum(chord.notes, when, 0.2);
    else if (s === 2 || s === 6) strum(chord.notes, when, 0.12);

    // 口哨旋律
    const m = MELODY[((step % 32) + 32) % 32];
    if (m !== null && m !== undefined) whistle(m, when, 0.15);

    // 轻打击：每个八分格一次，强拍略重
    tick(when, s % 2 === 0 ? 0.09 : 0.05);
  }

  /** 调度器 tick：每 0.1s 触发，补齐未来 0.5s 内的音符。 */
  function schedulerTick() {
    if (!musicOn || !ctx) return;
    const horizon = ctx.currentTime + SCHEDULE_AHEAD;
    // 防御：若被后台节流导致落后太多，直接跳到当前时间之后，避免「追赶式」爆音
    if (nextStepTime < ctx.currentTime - 0.2) {
      nextStepTime = ctx.currentTime + 0.02;
    }
    let guard = 0;
    while (nextStepTime < horizon && guard < 128) {
      scheduleStep(nextStepTime, stepIndex);
      nextStepTime += STEP; // 固定步长累加，不随 wall-clock 漂移
      stepIndex++;
      guard++;
    }
  }

  // -------------------------------------------------------------------------
  // 音乐控制
  // -------------------------------------------------------------------------

  /** startMusic()：启动循环 BGM。 */
  function startMusic() {
    const c = ensureRunning();
    if (!c) return;
    if (musicOn) return;
    musicOn = true;
    const now = c.currentTime;
    try {
      musicBus.gain.cancelScheduledValues(now);
      musicBus.gain.setValueAtTime(Math.max(0.0001, musicBus.gain.value), now);
      musicBus.gain.linearRampToValueAtTime(1.0, now + 0.08);
    } catch (e) {}
    nextStepTime = now + 0.08;
    stepIndex = 0;
    if (schedulerTimer === null) {
      schedulerTimer = setInterval(schedulerTick, LOOKAHEAD * 1000);
    }
    schedulerTick(); // 立即补一次，避免首个 0.1s 空档
  }

  /** 停止音乐调度并让音乐总线淡出。 */
  function stopMusic() {
    if (!musicOn) return;
    musicOn = false;
    if (schedulerTimer !== null) {
      clearInterval(schedulerTimer);
      schedulerTimer = null;
    }
    if (ctx && musicBus) {
      const now = ctx.currentTime;
      try {
        musicBus.gain.cancelScheduledValues(now);
        musicBus.gain.setValueAtTime(Math.max(0.0001, musicBus.gain.value), now);
        musicBus.gain.linearRampToValueAtTime(0.0001, now + 0.35);
      } catch (e) {}
    }
  }

  /** toggleMusic()：返回切换后是否在播放。 */
  function toggleMusic() {
    if (musicOn) {
      stopMusic();
    } else {
      startMusic();
    }
    return musicOn;
  }

  /** isMusicOn()：音乐当前是否在播放。 */
  function isMusicOn() {
    return musicOn;
  }

  // -------------------------------------------------------------------------
  // 音效
  // -------------------------------------------------------------------------

  /**
   * 单声车铃：1× / 2.76× / 5.4× 正弦泛音，指数衰减；
   * 再叠一层略延迟、更短的衰减层模拟一点点混响尾（不使用 ConvolverNode）。
   */
  function ringBellTone(freq, when) {
    const dur = 1.1;
    for (let i = 0; i < BELL_PARTIALS.length; i++) {
      const f = freq * BELL_PARTIALS[i];
      const baseAmp = BELL_PARTIAL_AMPS[i];
      for (let layer = 0; layer < 2; layer++) {
        const delay = layer === 0 ? 0 : 0.06; // 第二层略延迟
        const lAmp = layer === 0 ? 1 : 0.35; // 混响层更轻
        const lDur = layer === 0 ? dur : dur * 0.6;
        const start = when + delay;
        const osc = ctx.createOscillator();
        osc.type = 'sine';
        osc.frequency.setValueAtTime(f, start);
        const g = ctx.createGain();
        g.gain.setValueAtTime(0.0001, start);
        g.gain.exponentialRampToValueAtTime(baseAmp * lAmp, start + 0.003);
        g.gain.exponentialRampToValueAtTime(0.0001, start + lDur);
        osc.connect(g);
        g.connect(master); // 音效不经音乐总线，音乐开关不影响车铃
        osc.start(start);
        osc.stop(start + lDur + 0.02);
        osc.onended = function () {
          try {
            g.disconnect();
          } catch (e) {}
        };
      }
    }
  }

  /** bell()：两声车铃 2093Hz / 2637Hz，间隔 0.12s。 */
  function bell() {
    const c = ensureRunning();
    if (!c) return;
    const t0 = c.currentTime + 0.02;
    for (let i = 0; i < BELL_FREQS.length; i++) {
      ringBellTone(BELL_FREQS[i], t0 + i * BELL_GAP);
    }
  }

  /** call()：锯齿波 420→260Hz 扫频，带通 900Hz Q=3，历时 0.4s。 */
  function call() {
    const c = ensureRunning();
    if (!c) return;
    const when = c.currentTime + 0.02;
    const dur = 0.4;
    const osc = c.createOscillator();
    osc.type = 'sawtooth';
    osc.frequency.setValueAtTime(420, when);
    osc.frequency.exponentialRampToValueAtTime(260, when + dur);
    const bp = c.createBiquadFilter();
    bp.type = 'bandpass';
    bp.frequency.value = 900;
    bp.Q.value = 3;
    const g = c.createGain();
    g.gain.setValueAtTime(0.0001, when);
    g.gain.exponentialRampToValueAtTime(0.5, when + 0.03);
    g.gain.exponentialRampToValueAtTime(0.0001, when + dur);
    osc.connect(bp);
    bp.connect(g);
    g.connect(master);
    osc.start(when);
    osc.stop(when + dur + 0.02);
    osc.onended = function () {
      try {
        bp.disconnect();
        g.disconnect();
      } catch (e) {}
    };
  }

  // -------------------------------------------------------------------------
  // 中文配音
  // -------------------------------------------------------------------------

  /** 字幕广播：把台词文本抛给 UI（主程序可监听 'pelican-subtitle' 显示字幕）。 */
  function dispatchSubtitle(text) {
    try {
      if (
        typeof window !== 'undefined' &&
        typeof window.CustomEvent === 'function' &&
        typeof window.dispatchEvent === 'function'
      ) {
        window.dispatchEvent(
          new window.CustomEvent('pelican-subtitle', { detail: { text: text } })
        );
      }
    } catch (e) {
      /* 忽略：UI 层未监听也不影响 */
    }
  }

  /** 获取可用的 speechSynthesis（不可用返回 null）。 */
  function getSynth() {
    try {
      if (
        typeof window !== 'undefined' &&
        window.speechSynthesis &&
        typeof window.SpeechSynthesisUtterance === 'function'
      ) {
        return window.speechSynthesis;
      }
    } catch (e) {}
    return null;
  }

  /** 检查语音列表：返回 { voices, zh }（zh 为命中的中文语音，可能为 null）。 */
  function inspectVoices(synth) {
    let voices = [];
    try {
      voices = synth.getVoices() || [];
    } catch (e) {
      voices = [];
    }
    let zh = null;
    for (let i = 0; i < voices.length; i++) {
      const v = voices[i];
      const lang = (v && v.lang ? String(v.lang) : '').toLowerCase();
      if (lang.indexOf('zh') === 0 || lang.indexOf('cmn') === 0) {
        zh = v;
        break;
      }
    }
    return { voices: voices, zh: zh };
  }

  /** 用 speechSynthesis 播报；失败/被拒时触发降级。成功返回 true。 */
  function trySpeak(synth, line, voice) {
    try {
      const u = new window.SpeechSynthesisUtterance(line);
      u.lang = 'zh-CN';
      if (voice) u.voice = voice;
      u.rate = 1.0;
      u.pitch = 1.0;
      let fallenBack = false;
      u.onerror = function () {
        // 合成失败（例如自动播放策略拒绝）→ 保证不静默失败
        if (!fallenBack) {
          fallenBack = true;
          degradeToCall(line);
        }
      };
      try {
        if (synth.speaking || synth.pending) synth.cancel();
      } catch (e) {}
      synth.speak(u);
      return true;
    } catch (e) {
      return false;
    }
  }

  /** 降级：播放合成鹈鹕叫声 + 广播字幕，绝不静默失败。 */
  function degradeToCall(line) {
    try {
      call();
    } catch (e) {}
    dispatchSubtitle(line);
  }

  /**
   * speak(text?)：中文配音。
   * 优先 window.speechSynthesis（lang='zh-CN'）；
   * 若无中文语音或 speechSynthesis 不可用，则退化为 call() + 'pelican-subtitle' 事件。
   */
  function speak(text) {
    const line =
      typeof text === 'string' && text.trim().length > 0 ? text : DEFAULT_LINE;
    const synth = getSynth();
    if (synth) {
      const info = inspectVoices(synth);
      if (info.zh) {
        // 有中文语音：直接播报
        if (trySpeak(synth, line, info.zh)) return;
      } else if (info.voices.length === 0) {
        // 语音列表尚未加载：仍尝试合成（lang=zh-CN），失败由 onerror 降级
        if (trySpeak(synth, line, null)) return;
      }
      // 有语音列表但无中文语音 → 落到降级
    }
    degradeToCall(line);
  }

  // -------------------------------------------------------------------------
  // 对外接口（严格对齐 SPEC §2.3）
  // -------------------------------------------------------------------------

  return {
    resume: resume,
    bell: bell,
    call: call,
    startMusic: startMusic,
    toggleMusic: toggleMusic,
    isMusicOn: isMusicOn,
    speak: speak,
    setVolume: setVolume,
  };
}
