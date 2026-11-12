/**
 * src/main.js —— 入口：渲染器 / 后期 / 相机 / 光照 / 主循环 / 交互 / HUD / 粒子 / 昼夜
 *
 * 规格来源：SPEC.md §2.4（调试钩子）、§3.1（渲染与后期）、§3.7（动画规则）、
 *           §3.8（镜头四模式）、§3.9（音频）、§3.10（主循环状态）、§4（UI 与交互）
 *
 * 本文件只做「组装 + 驱动」：环境 scene.js、主角 hero.js、音频 audio.js 三个模块
 * 按 §2 契约原样 import 使用，未作任何修改。
 */
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { EffectComposer } from 'three/addons/postprocessing/EffectComposer.js';
import { RenderPass } from 'three/addons/postprocessing/RenderPass.js';
import { UnrealBloomPass } from 'three/addons/postprocessing/UnrealBloomPass.js';
import { OutputPass } from 'three/addons/postprocessing/OutputPass.js';
import { buildWorld, trackSample, TRACK_LENGTH } from './scene.js';
import { buildHero } from './hero.js';
import { createAudio } from './audio.js';

/* ==========================================================================
 * 0. 常量与小工具
 * ========================================================================== */
const WHEEL_R = 0.36;            // §3.5 车轮半径（用于 speed → 车轮角速度）
const CRANK_RATIO = 0.55;        // §3.5 曲柄转速 = 车轮转速 × 0.55
const SUN_OFFSET = new THREE.Vector3(40, 60, 25); // §3.1 阴影相机跟随用的固定偏移
const SUN_DAY = 2.6, SUN_NIGHT = 0.25;            // §3.7 太阳强度
const HEMI_DAY = 0.9, HEMI_NIGHT = 0.25;          // §3.7 半球光强度
const CAM_NAMES = ['自由环绕', '追尾跟拍', '电影侧拍', '鹈鹕视角']; // §3.8

// 无障碍：prefers-reduced-motion 时降低粒子数量（§4）
const REDUCED = !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
const CONFETTI_N = REDUCED ? 40 : 120;   // §3.7 按铃彩纸 120 片
const DUST_RATE = REDUCED ? 0.5 : 1;     // 扬尘概率缩放

const ASSETS = (window.ASSETS && typeof window.ASSETS === 'object') ? window.ASSETS : {};
const el = (id) => document.getElementById(id);

/* ==========================================================================
 * 1. 主循环状态（SPEC §3.10）
 * ========================================================================== */
const state = {
  speed: 0, targetSpeed: 0,        // 0..12 m/s，每按一次 ±1.5
  u: 0, distance: 0, lap: 0,
  cam: 1, night: 0, nightTarget: 0,
  bells: 0, wheelieT: -1, musicOn: true, ready: false,
  // 以下为渲染侧辅助量（不属于 §3.10 必需字段，但供 HUD / hero 动画使用）
  bellT: -1, lean: 0, wheelie: 0
};

/* ==========================================================================
 * 2. 渲染器 / 场景 / 相机 / 后期（SPEC §3.1）
 * ========================================================================== */
const stage = el('stage');
const renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: 'high-performance' });
renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
renderer.setSize(window.innerWidth, window.innerHeight);
renderer.toneMapping = THREE.ACESFilmicToneMapping;
renderer.toneMappingExposure = 1.0;
renderer.outputColorSpace = THREE.SRGBColorSpace;
renderer.shadowMap.enabled = true;
renderer.shadowMap.type = THREE.PCFSoftShadowMap; // §3.1 柔和阴影
if (stage) stage.appendChild(renderer.domElement);

const scene = new THREE.Scene();
scene.fog = new THREE.Fog(0xbfe3ff, 60, 260);     // §3.1 雾（夜间由 scene.update 改成 #0e1838）

const camera = new THREE.PerspectiveCamera(50, window.innerWidth / window.innerHeight, 0.1, 1200);

// 镜头 1 专用的自由环绕控制器（§3.8）
const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;
controls.dampingFactor = 0.08;
controls.maxPolarAngle = 0.49 * Math.PI;          // 不允许钻到地面以下
controls.minDistance = 3;
controls.maxDistance = 90;
controls.target.set(0, 1, 0);

// 后期：RenderPass → UnrealBloomPass(0.35/0.6/0.85) → OutputPass
const composer = new EffectComposer(renderer);
composer.setPixelRatio(renderer.getPixelRatio());
composer.setSize(window.innerWidth, window.innerHeight);
composer.addPass(new RenderPass(scene, camera));
const bloomPass = new UnrealBloomPass(
  new THREE.Vector2(window.innerWidth, window.innerHeight), 0.35, 0.6, 0.85
);
composer.addPass(bloomPass);
composer.addPass(new OutputPass());

/* ==========================================================================
 * 3. 光照（SPEC §3.1）：半球光 + 带阴影的平行光（每帧跟随主角）
 * ========================================================================== */
const hemi = new THREE.HemisphereLight(0xcfe8ff, 0xe8d3a8, 0.9);
scene.add(hemi);

const sun = new THREE.DirectionalLight(0xfff1d6, 2.6);
sun.castShadow = true;
sun.shadow.mapSize.set(2048, 2048);               // 2048²
sun.shadow.camera.left = -40;
sun.shadow.camera.right = 40;
sun.shadow.camera.top = 40;
sun.shadow.camera.bottom = -40;
sun.shadow.camera.near = 1;
sun.shadow.camera.far = 220;
sun.shadow.camera.updateProjectionMatrix();
sun.shadow.bias = -0.0004;                        // §3.1 消除条纹
sun.shadow.normalBias = 0.02;
scene.add(sun);
scene.add(sun.target);                            // target 必须入场景图才会更新矩阵

/* ==========================================================================
 * 4. 媒体（video / poster）：一律走 blob URL，禁止 fetch(data:)（§0 / §5 CSP 要求）
 * ========================================================================== */

/**
 * base64 → Blob → ObjectURL。
 * 不用 fetch(data:xxx)：部分托管环境的 CSP 禁止 data: 媒体，blob: 才是安全通道。
 * @param {string} b64 纯 base64（不含 data: 前缀）
 * @param {string} mime
 * @returns {string} blob URL
 */
function base64ToBlobUrl(b64, mime) {
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return URL.createObjectURL(new Blob([bytes], { type: mime }));
}

let videoEl = null;
if (ASSETS.clip) {
  const videoUrl = base64ToBlobUrl(ASSETS.clip, 'video/mp4'); // §2.1 大屏视频源
  videoEl = document.createElement('video');
  videoEl.muted = true;
  videoEl.loop = true;
  videoEl.autoplay = true;
  videoEl.playsInline = true;
  videoEl.setAttribute('muted', '');
  videoEl.setAttribute('playsinline', '');
  videoEl.setAttribute('webkit-playsinline', '');
  videoEl.preload = 'auto';
  videoEl.src = videoUrl;
  // 挂到 DOM 上但几乎不可见：部分浏览器对脱离文档的视频不推进解码，
  // VideoTexture 就会一直拿不到新帧。用 opacity 而非 display:none，保证解码继续。
  videoEl.style.cssText =
    'position:fixed;left:0;top:0;width:2px;height:2px;opacity:0.01;pointer-events:none;z-index:-1';
  document.body.appendChild(videoEl);
}

// 海报：data URL（模板/脚本侧不出现外部域名），全部作为 §3.4 画架贴图
let posterTexture = null;
if (ASSETS.poster) {
  posterTexture = new THREE.TextureLoader().load(ASSETS.poster, (t) => {
    t.colorSpace = THREE.SRGBColorSpace;
    t.needsUpdate = true;
  });
  posterTexture.colorSpace = THREE.SRGBColorSpace;
}

// 开场页海报背景（同一份 data URL，不产生第二次网络请求）
const introPoster = el('intro-poster');
if (introPoster && ASSETS.poster) introPoster.src = ASSETS.poster;

/* ==========================================================================
 * 5. 世界 + 主角
 * ========================================================================== */
const world = buildWorld(scene, { videoEl, posterTexture });
world.setTrackLengthInfo();

const hero = buildHero(scene);          // root 已被 hero.js 加入 scene
const heroPos = new THREE.Vector3();    // 主角世界位置（两轮接地处）
let heroYaw = 0;                        // 主角朝向（绕 y）
let pedalAngle = 0;                     // 曲柄角（= 车轮角 × 0.55，与 hero.js 内部一致）
let chaseYaw = 0;                       // 追尾镜头用的平滑朝向

/** 主角局部坐标 (x 前 / y 上 / z 侧) → 世界坐标（只含位置与绕 y 的朝向）。 */
const _tmpW = new THREE.Vector3();
function heroLocalToWorld(lx, ly, lz, out) {
  const c = Math.cos(heroYaw), s = Math.sin(heroYaw);
  return out.set(
    heroPos.x + lx * c + lz * s,
    heroPos.y + ly,
    heroPos.z - lx * s + lz * c
  );
}

// 初始位置：赛道起点
(function initHero() {
  const s0 = trackSample(0);
  heroPos.set(s0.pos.x, s0.pos.y + 0.02, s0.pos.z);
  heroYaw = Math.atan2(-s0.tangent.z, s0.tangent.x);
  chaseYaw = heroYaw;
  hero.root.position.copy(heroPos);
  hero.root.rotation.y = heroYaw;
})();

/* ==========================================================================
 * 6. 音频（SPEC §3.9 / §2.3）
 * ========================================================================== */
const audio = createAudio();
audio.setVolume(0.45); // 默认音量

/* ==========================================================================
 * 7. 粒子（SPEC §3.7 / §7）：一个 Points 系统承载扬尘 + 彩纸
 *    —— 顶点色每帧必须置 needsUpdate，否则彩纸会渲染成黑方块
 * ========================================================================== */
const P_COUNT = REDUCED ? 220 : 600;
const pPos = new Float32Array(P_COUNT * 3);
const pCol = new Float32Array(P_COUNT * 3);
const pVel = new Float32Array(P_COUNT * 3);
const pBase = new Float32Array(P_COUNT * 3);   // 基准颜色
const pLife = new Float32Array(P_COUNT);
const pMax = new Float32Array(P_COUNT);
const pGrav = new Float32Array(P_COUNT);
const pDrag = new Float32Array(P_COUNT);
const pAlive = new Uint8Array(P_COUNT);
for (let i = 0; i < P_COUNT; i++) pPos[i * 3 + 1] = -9999; // 未激活的点藏到地下

/** 径向渐变圆形贴图：中心不透明白 → 边缘全透明，点精灵即柔和圆斑。 */
function makeParticleTexture() {
  const size = 64;
  const cv = document.createElement('canvas');
  cv.width = cv.height = size;
  const g = cv.getContext('2d');
  const grad = g.createRadialGradient(size / 2, size / 2, 0, size / 2, size / 2, size / 2);
  grad.addColorStop(0, 'rgba(255,255,255,1)');
  grad.addColorStop(0.4, 'rgba(255,255,255,0.55)');
  grad.addColorStop(1, 'rgba(255,255,255,0)');
  g.fillStyle = grad;
  g.fillRect(0, 0, size, size);
  const tex = new THREE.CanvasTexture(cv);
  tex.colorSpace = THREE.SRGBColorSpace;
  return tex;
}

const pGeo = new THREE.BufferGeometry();
pGeo.setAttribute('position', new THREE.BufferAttribute(pPos, 3));
pGeo.setAttribute('color', new THREE.BufferAttribute(pCol, 3));
const pMat = new THREE.PointsMaterial({
  size: 0.26,
  map: makeParticleTexture(),
  vertexColors: true,      // 顶点色（彩纸的随机 HSL 走这里）
  transparent: true,
  depthWrite: false,
  sizeAttenuation: true
});
const particles = new THREE.Points(pGeo, pMat);
particles.frustumCulled = false;
scene.add(particles);

let pCursor = 0;
/** 取一个空闲粒子槽（全部在用则放弃本次生成，粒子数不会无限增长）。 */
function allocParticle() {
  for (let k = 0; k < P_COUNT; k++) {
    const i = (pCursor + k) % P_COUNT;
    if (!pAlive[i]) { pCursor = (i + 1) % P_COUNT; return i; }
  }
  return -1;
}

/** 后轮扬尘：沿车后方向抛出、轻微上扬，颜色取沙土色。 */
function spawnDust() {
  const i = allocParticle();
  if (i < 0) return;
  heroLocalToWorld(-0.58 + Math.random() * 0.12, 0.05 + Math.random() * 0.06,
    (Math.random() - 0.5) * 0.3, _tmpW);
  const c = Math.cos(heroYaw), s = Math.sin(heroYaw);
  const sp = Math.min(state.speed, 10) / 10;
  const back = 0.6 + Math.random() * 1.2;
  pPos[i * 3] = _tmpW.x;
  pPos[i * 3 + 1] = _tmpW.y;
  pPos[i * 3 + 2] = _tmpW.z;
  pVel[i * 3] = -c * back * sp + (Math.random() - 0.5) * 0.5;
  pVel[i * 3 + 1] = 0.35 + Math.random() * 0.55;
  pVel[i * 3 + 2] = s * back * sp + (Math.random() - 0.5) * 0.5;
  pBase[i * 3] = 0.78; pBase[i * 3 + 1] = 0.73; pBase[i * 3 + 2] = 0.62;
  pMax[i] = pLife[i] = 0.7 + Math.random() * 0.6;
  pGrav[i] = 0.3;    // 尘土几乎不受重力，飘着散开
  pDrag[i] = 1.1;
  pAlive[i] = 1;
}

/** 按铃彩纸：120 片随机 HSL + 重力下落。 */
function spawnConfetti() {
  heroLocalToWorld(-0.05, 1.95, 0, _tmpW);
  const col = new THREE.Color();
  for (let n = 0; n < CONFETTI_N; n++) {
    const i = allocParticle();
    if (i < 0) return;
    const a = Math.random() * Math.PI * 2;
    const r = Math.random() * 0.9;
    pPos[i * 3] = _tmpW.x + Math.cos(a) * r * 0.4;
    pPos[i * 3 + 1] = _tmpW.y + Math.random() * 0.4;
    pPos[i * 3 + 2] = _tmpW.z + Math.sin(a) * r * 0.4;
    const spd = 2.2 + Math.random() * 3.6;
    pVel[i * 3] = Math.cos(a) * spd;
    pVel[i * 3 + 1] = 2.0 + Math.random() * 3.2;   // 先向上炸开
    pVel[i * 3 + 2] = Math.sin(a) * spd;
    col.setHSL(Math.random(), 0.85, 0.55 + Math.random() * 0.15); // 随机彩纸色
    pBase[i * 3] = col.r; pBase[i * 3 + 1] = col.g; pBase[i * 3 + 2] = col.b;
    pMax[i] = pLife[i] = 1.6 + Math.random() * 1.2;
    pGrav[i] = 4.5 + Math.random() * 3.0;          // 受重力下落
    pDrag[i] = 1.5;
    pAlive[i] = 1;
  }
}

/** 粒子积分 + 颜色写入。无论如何每帧都要把 color 标记为 needsUpdate（§7 已知坑）。 */
function updateParticles(dt) {
  for (let i = 0; i < P_COUNT; i++) {
    if (!pAlive[i]) continue;
    pLife[i] -= dt;
    if (pLife[i] <= 0) {
      pAlive[i] = 0;
      pPos[i * 3 + 1] = -9999;   // 移出视野
      continue;
    }
    const f = Math.max(0, 1 - pDrag[i] * dt);
    pVel[i * 3] *= f;
    pVel[i * 3 + 1] = pVel[i * 3 + 1] * f - pGrav[i] * dt;
    pVel[i * 3 + 2] *= f;
    pPos[i * 3] += pVel[i * 3] * dt;
    pPos[i * 3 + 1] += pVel[i * 3 + 1] * dt;
    pPos[i * 3 + 2] += pVel[i * 3 + 2] * dt;
    // 生命末期线性淡出（顶点色 × 衰减系数）
    const fade = Math.min(1, pLife[i] / 0.55);
    pCol[i * 3] = pBase[i * 3] * fade;
    pCol[i * 3 + 1] = pBase[i * 3 + 1] * fade;
    pCol[i * 3 + 2] = pBase[i * 3 + 2] * fade;
  }
  pGeo.attributes.position.needsUpdate = true;
  pGeo.attributes.color.needsUpdate = true;   // ★ 必须每帧置 true，否则彩纸是黑方块
}

/* ==========================================================================
 * 8. 镜头四模式（SPEC §3.8）
 * ========================================================================== */
const _camPos = new THREE.Vector3();
const _look = new THREE.Vector3();

function updateCamLabel() {
  const node = el('hud-cam');
  if (node) node.textContent = state.cam + ' ' + CAM_NAMES[state.cam - 1];
}

/**
 * 镜头切换。
 * 1 自由环绕（OrbitControls，target 平滑跟车）／2 追尾跟拍／3 电影侧拍／4 鹈鹕视角。
 * @param {number} i 1..4
 */
function setCam(i) {
  const n = Math.min(4, Math.max(1, Math.round(Number(i) || 1)));
  const prev = state.cam;
  state.cam = n;
  controls.enabled = (n === 1);
  if (n === 1 && prev !== 1) {
    // §3.8：切回镜头 1 时把相机放到车 +(9,6,9)
    heroLocalToWorld(9, 6, 9, _camPos);
    camera.position.copy(_camPos);
    controls.target.copy(heroPos);
    controls.update();
  }
  updateCamLabel();
}

function updateCamera(dt, t) {
  if (state.cam === 1) {
    // target 平滑跟车：相机与 target 同步平移，保持相对视角不被破坏
    _tmpW.copy(heroPos).sub(controls.target).multiplyScalar(Math.min(1, dt * 3));
    controls.target.add(_tmpW);
    camera.position.add(_tmpW);
    controls.update();
    return;
  }
  if (state.cam === 2) {
    // 追尾跟拍：车后 4.2、高 2.2，看向车前 2m
    heroLocalToWorld(-4.2, 2.2, 0, _camPos);
    heroLocalToWorld(2.0, 0.9, 0, _look);
  } else if (state.cam === 3) {
    // 电影侧拍：侧方 5±1.5 缓慢漂移，高 1.4
    const side = 5 + Math.sin(t * 0.35) * 1.5;
    heroLocalToWorld(-0.4, 1.4, side, _camPos);
    heroLocalToWorld(0.1, 0.95, 0, _look);
  } else {
    // 鹈鹕视角：头顶上方 0.36、略后 0.05（§7 已知坑：贴太近会穿模进帽子）
    heroLocalToWorld(0.25, 2.41, 0, _camPos);
    heroLocalToWorld(8.0, 1.90, 0, _look);
  }
  camera.position.copy(_camPos);
  camera.lookAt(_look);
}

/* ==========================================================================
 * 9. UI：HUD / toast / 字幕 / 底部按钮 / 开场页（SPEC §4）
 * ========================================================================== */
const hudSpeed = el('hud-speed'), hudDist = el('hud-dist'), hudLap = el('hud-lap'),
  hudBell = el('hud-bell'), hudCam = el('hud-cam');

let toastTimer = 0;
function toast(msg) {
  const node = el('toast');
  if (!node) return;
  node.textContent = msg;
  node.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => node.classList.remove('show'), 2000); // 2 秒淡出
}

let subTimer = 0;
function showSubtitle(text) {
  const node = el('subtitle');
  if (!node) return;
  node.textContent = text;
  node.classList.add('show');
  clearTimeout(subTimer);
  subTimer = setTimeout(() => node.classList.remove('show'), 4200);
}

function updateHud() {
  if (hudSpeed) hudSpeed.textContent = (state.speed * 3.6).toFixed(1); // km/h
  if (hudDist) hudDist.textContent = state.distance.toFixed(0);
  if (hudLap) hudLap.textContent = String(state.lap);
  if (hudBell) hudBell.textContent = String(state.bells);
}

function updateMusicBtn() {
  const on = audio.isMusicOn();
  state.musicOn = on;
  const btn = el('btn-music') || document.querySelector('[data-act="music"]');
  if (btn) {
    btn.textContent = on ? '🎵 音乐' : '🔇 静音';
    btn.setAttribute('aria-pressed', on ? 'true' : 'false');
  }
}

/* ---- 动作 ---- */
function doBell() {
  state.bells++;
  state.bellT = 0;          // hero.js 用 bellT 驱动张嘴 / 扇翅
  audio.bell();
  spawnConfetti();          // §3.7 按铃爆彩纸
}

function doWheelie() {
  state.wheelieT = 0;       // 主循环里 1.4 秒抬起 0.45 rad 再落回
}

function accel(dv) {
  state.targetSpeed = Math.min(12, Math.max(0, state.targetSpeed + dv));
  toast('目标速度 ' + (state.targetSpeed * 3.6).toFixed(0) + ' km/h');
}

function toggleNight() {
  state.nightTarget = state.nightTarget > 0.5 ? 0 : 1;
  // 按要求 state.night 与目标值都改：给一个 35% 的初速，视觉上立刻响应，之后仍平滑收敛
  state.night += (state.nightTarget - state.night) * 0.35;
  prevNight = state.night;  // 标记为「本循环写入」，避免被外部改写检测误判
  toast(state.nightTarget > 0.5 ? '已进入夜间' : '已回到白天');
}

function toggleMusic() {
  audio.toggleMusic();
  updateMusicBtn();
  toast(audio.isMusicOn() ? '音乐已开启' : '音乐已关闭');
}

function speakLine() {
  audio.speak();
}

/* ---- 键盘（§4） ---- */
window.addEventListener('keydown', (e) => {
  const k = e.key;
  if (k === ' ' || e.code === 'Space') { e.preventDefault(); doBell(); return; }
  if (k === 'ArrowUp' || k === 'w' || k === 'W') { e.preventDefault(); accel(1.5); return; }
  if (k === 'ArrowDown' || k === 's' || k === 'S') { e.preventDefault(); accel(-1.5); return; }
  if (k === 'j' || k === 'J') { doWheelie(); return; }
  if (k === 'c' || k === 'C') { setCam(state.cam % 4 + 1); return; }
  if (k >= '1' && k <= '4') { setCam(parseInt(k, 10)); return; }
  if (k === 'n' || k === 'N') { toggleNight(); return; }
  if (k === 'm' || k === 'M') { toggleMusic(); return; }
  if (k === 'v' || k === 'V') { speakLine(); return; }
});

/* ---- 底部按钮：与键盘完全同等功能 ---- */
const ACTIONS = {
  bell: doBell,
  faster: () => accel(1.5),
  slower: () => accel(-1.5),
  wheelie: doWheelie,
  cam: () => setCam(state.cam % 4 + 1),
  night: toggleNight,
  music: toggleMusic,
  speak: speakLine
};
document.querySelectorAll('[data-act]').forEach((btn) => {
  const fn = ACTIONS[btn.getAttribute('data-act')];
  if (!fn) return;
  btn.addEventListener('click', (ev) => { ev.preventDefault(); fn(); });
});

/* ---- 字幕事件（audio.js 降级时会派发 pelican-subtitle） ---- */
window.addEventListener('pelican-subtitle', (e) => {
  const text = e && e.detail ? e.detail.text : '';
  if (text) showSubtitle(text);
});

/* ---- 开场页 → 出发 ---- */
let started = false;
function start() {
  if (started) return;
  started = true;
  const intro = el('intro');
  if (intro) {
    intro.classList.add('hidden');            // 淡出
    setTimeout(() => { intro.style.display = 'none'; }, 700);
  }
  // 音频解锁必须在用户手势回调里（§3.9 自动播放策略）
  audio.resume();
  audio.startMusic();
  if (videoEl) {
    const pr = videoEl.play();                // 大屏视频同步开播（blob URL）
    if (pr && pr.catch) pr.catch(() => {});
  }
  updateMusicBtn();
  setTimeout(() => { audio.speak(); }, 600);  // 0.6 秒后播报配音
}
const startBtn = el('start-btn');
if (startBtn) startBtn.addEventListener('click', start);

/* ---- 自适应 ---- */
window.addEventListener('resize', () => {
  const w = window.innerWidth, h = window.innerHeight;
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.setSize(w, h);
  composer.setPixelRatio(renderer.getPixelRatio());
  composer.setSize(w, h);
});

/* ==========================================================================
 * 10. 主循环（SPEC §3.7 / §3.10）
 * ========================================================================== */
const clock = new THREE.Clock();
let t = 0;
let prevNight = 0;      // 上一帧「主循环写回」的 night，用于识别外部直接改写
let hudTimer = 0;

function frame() {
  requestAnimationFrame(frame);
  const dt = Math.min(clock.getDelta(), 0.05);   // §3.7 dt = min(dt, 0.05)
  t += dt;

  /* ---- 昼夜：外部直接改写 state.night（QA 会设 0.99）时采纳并快进 ---- */
  if (Math.abs(state.night - prevNight) > 0.02) state.nightTarget = state.night;
  state.night += (state.nightTarget - state.night) * Math.min(1, dt * 0.8);
  prevNight = state.night;
  const night = state.night;

  /* ---- 速度 / 前进 / 圈数 ---- */
  state.speed += (state.targetSpeed - state.speed) * Math.min(1, dt * 2);
  if (state.speed < 0.002) state.speed = 0;
  state.u = (state.u + (state.speed * dt) / TRACK_LENGTH) % 1;
  state.distance += state.speed * dt;
  const lapNew = Math.floor(state.distance / TRACK_LENGTH);
  if (lapNew > state.lap) state.lap = lapNew;

  /* ---- 沿赛道姿态：位置 / 朝向 / 侧倾 ---- */
  const sample = trackSample(state.u);           // ⚠ 复用对象，本帧用完即弃
  heroPos.set(sample.pos.x, sample.pos.y + 0.02, sample.pos.z);
  heroYaw = Math.atan2(-sample.tangent.z, sample.tangent.x); // 局部 +x 对准切线
  const leanTarget = THREE.MathUtils.clamp(-sample.curvature * 8 * state.speed, -0.45, 0.45);
  state.lean += (leanTarget - state.lean) * Math.min(1, dt * 3); // 平滑跟随

  /* ---- 曲柄角：车轮角 × 0.55（与 hero.js 内部 wheelAngle = pedalAngle / 0.55 一致） ---- */
  pedalAngle += (state.speed / WHEEL_R) * CRANK_RATIO * dt;

  /* ---- 翘头 J：1.4 秒抬起 0.45 rad 再落回 ---- */
  if (state.wheelieT >= 0) {
    state.wheelieT += dt;
    const p = state.wheelieT / 1.4;
    if (p >= 1) { state.wheelieT = -1; state.wheelie = 0; }
    else state.wheelie = 0.45 * Math.sin(Math.PI * p);
  }

  /* ---- 按铃计时 / 头部张望 / 扇翅 ---- */
  if (state.bellT >= 0) {
    state.bellT += dt;
    if (state.bellT > 2.0) state.bellT = -1;
  }
  const headTurn = Math.sin(0.7 * t) * 0.35 + Math.sin(2.3 * t) * 0.05; // §3.7
  const flap = state.speed > 8 ? 0.65 : 0;      // speed>8 持续扇动（按铃由 hero 内部叠加）

  /* ---- 主角 ---- */
  hero.root.position.copy(heroPos);
  hero.root.rotation.y = heroYaw;
  hero.update(dt, {
    pedalAngle,
    speed: state.speed,
    lean: state.lean,
    wheelie: state.wheelie,
    bellT: state.bellT,
    headTurn,
    flap,
    frontLightOn: night > 0.5              // §3.7 夜间开车灯
  });
  hero.root.updateMatrixWorld();

  /* ---- 阴影相机每帧跟随主角（§3.1） ---- */
  sun.target.position.copy(heroPos);
  sun.target.updateMatrixWorld();
  sun.position.copy(heroPos).add(SUN_OFFSET);

  /* ---- 昼夜：灯光 / 泛光（§3.7 / §3.1） ---- */
  sun.intensity = SUN_DAY + (SUN_NIGHT - SUN_DAY) * night;
  hemi.intensity = HEMI_DAY + (HEMI_NIGHT - HEMI_DAY) * night;
  bloomPass.strength = 0.25 + 0.7 * night;

  /* ---- 环境（天空 / 雾 / 海洋 / 灯塔 / 云 / 海鸥 / 帆船） ---- */
  world.update(dt, t, night);

  /* ---- 粒子：后轮扬尘 + 彩纸 ---- */
  if (state.speed > 0.4 && Math.random() < (state.speed / 10) * DUST_RATE) spawnDust();
  updateParticles(dt);

  /* ---- 相机 ---- */
  updateCamera(dt, t);

  /* ---- HUD（10Hz 节流，避免每帧写 DOM） ---- */
  hudTimer += dt;
  if (hudTimer > 0.1) { hudTimer = 0; updateHud(); }

  composer.render();
}

/* ==========================================================================
 * 11. 启动 + 调试钩子（SPEC §2.4 / §3.10）
 * ========================================================================== */
setCam(1);
updateHud();
updateMusicBtn();
// 相机初始视角：从赛道斜上方看主角
heroLocalToWorld(9, 6, 9, _camPos);
camera.position.copy(_camPos);
controls.target.copy(heroPos);
controls.update();

state.ready = true;
window.__pel = {
  state,
  setCam,
  doBell,
  toggleNight,
  ready: true
};

frame();
