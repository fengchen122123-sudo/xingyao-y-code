/**
 * src/scene.js —— 环境模块（天空 / 海洋 / 沙岛草地 / 木栈道赛道 / 灯塔 / 椰子树 / 沙滩伞 / 云 / 海鸥 / 帆船 / 大屏 / 画架）
 *
 * 事实来源：SPEC.md §2.1（接口契约）、§3.1（渲染参数）、§3.2（天空）、§3.3（海洋）、§3.4（岛屿与道具）。
 * 所有几何均为程序化建模，不加载任何外部资源（海报贴图由调用方传入，可为 null）。
 *
 * 依赖：仅 three（以及 three/examples/jsm/*）。ESM。
 */

import * as THREE from 'three';

/* ==========================================================================
 * 0. 共享常量与调色板
 * ========================================================================== */

const UP = new THREE.Vector3(0, 1, 0);

/** 草皮层表面高度（沙岛顶面 y=0.02 上再铺 0.01 的草皮） */
const GRASS_Y = 0.03;
/** 木栈道面高度 = 草地表面 + 0.02 m：略高于草皮，避免共面 z-fighting */
const TRACK_Y = GRASS_Y + 0.02;
/** 木栈道条带宽度（SPEC §2.1） */
const TRACK_WIDTH = 3.2;
/** 木栈道纵向段数（SPEC §2.1） */
const TRACK_SEGMENTS = 400;
/** 海平面高度（SPEC §3.3：海洋 position.y = -0.6） */
const SEA_Y = -0.6;
/** 沙岛顶面半径（沙岛 CylinderGeometry(30,34,2) 的顶半径） */
const ISLAND_TOP_R = 30;
/** 夜空星星数量与半径（SPEC §3.2） */
const STAR_COUNT = 1500;
const STAR_RADIUS = 500;
/** 太阳方向：与 main.js 平行光偏移 (40,60,25) 同向，日轮与阴影方向一致 */
const SUN_DIR = new THREE.Vector3(40, 60, 25).normalize();
/** 灯塔摆放方位角（弧度）——放在岛前侧浅滩礁石上，从起点视角可见 */
const LIGHTHOUSE_ANGLE = 1.25;
/** 大屏与画架的方位角（弧度），位于靠海一侧的沙滩上 */
const SCREEN_ANGLE = 0.35;
const EASEL_ANGLE = -0.42;

// ---- 日夜两套色板（数值全部来自 SPEC §3.2 / §3.3 / §3.7） ----
const SKY_TOP_DAY = new THREE.Color(0x3d8fe0);
const SKY_TOP_NIGHT = new THREE.Color(0x020818);
const SKY_BOTTOM_DAY = new THREE.Color(0xd9f0ff);
const SKY_BOTTOM_NIGHT = new THREE.Color(0x1b2a55);
/** 日轮颜色：夜间压暗并偏冷，避免夜里出现一个刺眼的白色圆盘 */
const SUN_COLOR_DAY = new THREE.Color(0xfff3d0);
const SUN_COLOR_NIGHT = new THREE.Color(0x93a9cf);
const FOG_DAY = new THREE.Color(0xbfe3ff);
const FOG_NIGHT = new THREE.Color(0x0e1838);
/** 海面深水/浅水色（SPEC §3.3：日间 #0b5c8c / #35c4d6；夜间压暗并偏蓝） */
const OCEAN_DEEP_DAY = new THREE.Color(0x0b5c8c);
const OCEAN_DEEP_NIGHT = new THREE.Color(0x061a33);
const OCEAN_SHALLOW_DAY = new THREE.Color(0x35c4d6);
const OCEAN_SHALLOW_NIGHT = new THREE.Color(0x0f4166);
const CLOUD_DAY = new THREE.Color(0xffffff);
const CLOUD_NIGHT = new THREE.Color(0x9db0cf);

// ---- 模块级临时量：update()/trackSample() 内绝不再 new 对象 ----
const _c1 = new THREE.Color();
const _v1 = new THREE.Vector3();
const _v2 = new THREE.Vector3();
const _sPos = new THREE.Vector3();
const _sTan = new THREE.Vector3();
const _sNext = new THREE.Vector3();
const _sPrev = new THREE.Vector3();
const _sDiff = new THREE.Vector3();
const AXIS_X = new THREE.Vector3(1, 0, 0);

/* ==========================================================================
 * 1. 赛道路径（SPEC §2.1：12 控制点闭合 CatmullRom）
 *    r = 17 + sin(2a)*3.5 + cos(3a)*1.5，z *= 0.85，y 固定在草地表面高度 +0.02
 * ========================================================================== */

const trackPoints = [];
for (let i = 0; i < 12; i++) {
  const a = (i / 12) * Math.PI * 2;
  const r = 17 + Math.sin(2 * a) * 3.5 + Math.cos(3 * a) * 1.5;
  trackPoints.push(new THREE.Vector3(Math.cos(a) * r, TRACK_Y, Math.sin(a) * r * 0.85));
}
const trackCurve = new THREE.CatmullRomCurve3(trackPoints, true, 'catmullrom', 0.5);

/** 赛道总长度（米），CatmullRom.getLength() */
export const TRACK_LENGTH = trackCurve.getLength();

/** 曲率估计用的弧长参数增量：0.004·L ≈ 0.4 m 的步长，噪声与灵敏度折中 */
const CURV_DU = 0.004;

/** trackSample 的复用返回对象（避免每帧分配，见下方注释） */
const _sampleOut = { pos: _sPos, tangent: _sTan, curvature: 0 };

/**
 * 赛道路径采样。
 * ⚠ 为避免逐帧分配，返回的 pos / tangent 是模块级复用的向量，返回对象本身也被复用；
 *   调用方若需跨帧保存，请自行 .clone()。
 * @param {number} u 参数（任意实数，内部按 1 取模成环）
 * @returns {{pos: THREE.Vector3, tangent: THREE.Vector3, curvature: number}}
 */
export function trackSample(u) {
  let uu = u % 1;
  if (uu < 0) uu += 1;

  trackCurve.getPointAt(uu, _sPos);
  trackCurve.getTangentAt(uu, _sTan);
  if (_sTan.lengthSq() > 1e-12) _sTan.normalize();

  // 曲率：以弧长参数做二阶中心差分 κ ≈ |P(u+du) - 2P(u) + P(u-du)| / (du·L)²
  // （u 归一化参数 → 弧长 s = u·L，故分母除以 L²）
  const step = CURV_DU * TRACK_LENGTH;
  trackCurve.getPointAt((uu + CURV_DU) % 1, _sNext);
  trackCurve.getPointAt((uu - CURV_DU + 1) % 1, _sPrev);
  _sDiff.copy(_sNext).add(_sPrev).addScaledVector(_sPos, -2);
  _sampleOut.curvature = _sDiff.length() / (step * step);

  return _sampleOut;
}

// 赛道参考折线（仅模块初始化时算一次），用于道具摆放的避让检测
const TRACK_POLYLINE = (() => {
  const out = [];
  for (let i = 0; i < 160; i++) out.push(trackCurve.getPointAt(i / 160, new THREE.Vector3()));
  return out;
})();

/** 某个水平位置到赛道中心线的近似距离（0.6 m 采样精度，足够做摆设避让） */
function distanceToTrack(x, z) {
  let best = Infinity;
  for (let i = 0; i < TRACK_POLYLINE.length; i++) {
    const p = TRACK_POLYLINE[i];
    const dx = p.x - x;
    const dz = p.z - z;
    const d2 = dx * dx + dz * dz;
    if (d2 < best) best = d2;
  }
  return Math.sqrt(best);
}

/**
 * 海面波高（与 §3.3 顶点着色器公式一致的 CPU 版，供帆船随浪起伏使用）。
 * 近岸（半径 <30）平静、远海（>60）起浪，衰减系数 0.25 → 1.0。
 */
function waveHeight(x, z, t) {
  const raw =
    Math.sin(x * 0.15 + t * 1.2) * 0.35 +
    Math.sin(z * 0.21 + t * 0.9) * 0.3 +
    Math.sin((x + z) * 0.6 + t * 2.0) * 0.08;
  const d = Math.hypot(x, z);
  return raw * (0.25 + 0.75 * THREE.MathUtils.smoothstep(d, ISLAND_TOP_R, 60));
}

/* ==========================================================================
 * 2. 着色器（天空 / 海洋）
 *    两者都按 SPEC §3.3 的硬要求包含 three 的 fog / tonemapping / colorspace chunk：
 *    - tonemapping_fragment + colorspace_fragment 必须在写 gl_FragColor 之后、
 *      fog_fragment 之前（与 meshbasic_frag 等内置片元着色器顺序一致），
 *      否则在 ACES 色调映射 + 线性工作空间的场景里会和其它材质色调对不上；
 *    - fog_fragment 放在最后：three 的 refreshFogUniforms 会在有渲染目标时传线性色、
 *      直出画布时传 sRGB 色，只有保持内置顺序才能得到正确雾色。
 *    注意：colorspace_pars_fragment / tonemapping_pars_fragment 由 three 的程序前缀
 *    自动注入（含 linearToOutputTexel、toneMapping() 定义），这里**不能**重复 #include，
 *    否则 const mat3 / 函数重定义会编译失败。
 * ========================================================================== */

const SKY_VERT = /* glsl */ `
varying vec3 vDir;

#include <fog_pars_vertex>

void main() {
  // 天空球以原点为球心，物体空间方向即视线方向（插值后用 normalize 还原）
  vDir = normalize(position);

  vec4 mvPosition = modelViewMatrix * vec4(position, 1.0);
  gl_Position = projectionMatrix * mvPosition;

  #include <fog_vertex>
}
`;

const SKY_FRAG = /* glsl */ `
uniform vec3 topColor;
uniform vec3 bottomColor;
uniform vec3 sunColor;
uniform vec3 sunDir;
uniform float sunIntensity;

varying vec3 vDir;

#include <fog_pars_fragment>

void main() {
  vec3 dir = normalize(vDir);

  // 天顶-地平线渐变：指数 0.8 + y*1.6 抬升，让地平线附近过渡更柔和（SPEC §3.2）
  float k = pow(clamp(dir.y * 1.6 + 0.1, 0.0, 1.0), 0.8);
  vec3 col = mix(bottomColor, topColor, k);

  // 太阳：pow 600 的窄核是日轮本体，pow 12 的宽晕模拟大气散射光晕
  float s = max(dot(dir, normalize(sunDir)), 0.0);
  col += sunColor * (pow(s, 600.0) * 4.0 + pow(s, 12.0) * 0.25) * sunIntensity;

  gl_FragColor = vec4(col, 1.0);

  #include <tonemapping_fragment>
  #include <colorspace_fragment>
  #include <fog_fragment>
}
`;

const OCEAN_VERT = /* glsl */ `
uniform float uTime;

varying vec3 vWorld;
varying float vWaveH;

#include <fog_pars_vertex>

void main() {
  vec3 p = position;

  // 三组正弦叠加：大涌浪（波长 ≈42m）+ 中波（≈30m）+ 细碎波纹（≈10m）
  float h = sin(p.x * 0.15 + uTime * 1.2) * 0.35
          + sin(p.z * 0.21 + uTime * 0.9) * 0.3
          + sin((p.x + p.z) * 0.6 + uTime * 2.0) * 0.08;

  // 以岛中心半径 30–60 为界衰减：近岸 0.25 倍（岸线平静不穿沙滩），远海 1.0 倍（起浪）
  float d = length(p.xz);
  float shore = smoothstep(30.0, 60.0, d);
  h *= 0.25 + 0.75 * shore;

  p.y += h;
  vWaveH = h;

  vec4 worldPos = modelMatrix * vec4(p, 1.0);
  vWorld = worldPos.xyz;

  vec4 mvPosition = viewMatrix * worldPos;
  gl_Position = projectionMatrix * mvPosition;

  #include <fog_vertex>
}
`;

const OCEAN_FRAG = /* glsl */ `
uniform float uTime;
uniform float uNight;
uniform vec3 uDeepColor;
uniform vec3 uShallowColor;
uniform vec3 uSkyTop;
uniform vec3 uSkyBottom;
uniform vec3 uSunDir;
uniform vec3 uSunColor;

varying vec3 vWorld;
varying float vWaveH;

#include <fog_pars_fragment>

void main() {
  // dFdx/dFdy 从屏幕空间导数重建法线：波在顶点着色器里位移，无需重算法线缓冲
  vec3 N = normalize(cross(dFdx(vWorld), dFdy(vWorld)));
  if (N.y < 0.0) N = -N;

  vec3 V = normalize(cameraPosition - vWorld);
  float dist = length(vWorld.xz);

  // 深浅色按距岛距离混合：近岸浅滩偏青绿，远海深蓝
  vec3 base = mix(uShallowColor, uDeepColor, smoothstep(30.0, 220.0, dist));

  // 菲涅尔：掠射角反射天空色（pow 3 让反射集中在水面远处，近处保留水色）
  float fres = pow(1.0 - clamp(dot(N, V), 0.0, 1.0), 3.0);
  vec3 R = reflect(-V, N);
  vec3 skyCol = mix(uSkyBottom, uSkyTop, clamp(R.y * 1.6 + 0.1, 0.0, 1.0));
  vec3 col = mix(base, skyCol, fres * 0.85);

  // 太阳镜面高光（夜间衰减，避免夜里水面一片亮斑）
  float spec = pow(max(dot(R, normalize(uSunDir)), 0.0), 90.0) * 0.6 * (1.0 - uNight * 0.75);
  col += uSunColor * spec;

  // 浪尖白沫：波高超过 0.42 才出白（与顶点着色器衰减后的实际波高对应）
  float crest = smoothstep(0.42, 0.56, vWaveH);
  col = mix(col, vec3(1.0), crest * 0.75);

  // 岸边浪花环：半径 30 处用 sin(angle*14 + t*2) 扰动环半径，形成涌上沙滩的碎浪
  float ang = atan(vWorld.z, vWorld.x);
  float rWob = 30.0 + sin(ang * 14.0 + uTime * 2.0) * 0.45;
  float ring = 1.0 - smoothstep(0.0, 1.8, abs(dist - rWob));
  float foam = ring * (0.55 + 0.45 * sin(ang * 14.0 + uTime * 2.0));
  // 贴岸静沫：25→30m 渐入，30→34m 渐出
  foam += smoothstep(25.0, 30.0, dist) * (1.0 - smoothstep(30.0, 34.0, dist)) * 0.28;
  col = mix(col, vec3(0.95, 0.99, 1.0), clamp(foam, 0.0, 1.0) * 0.8);

  gl_FragColor = vec4(col, 1.0);

  #include <tonemapping_fragment>
  #include <colorspace_fragment>
  #include <fog_fragment>
}
`;

/* ==========================================================================
 * 3. 通用小工具
 * ========================================================================== */

/** 确定性随机数（mulberry32）：布局可复现，便于 QA 截图与回归对比 */
function makeRand(seed) {
  let s = seed >>> 0;
  return function rand() {
    s = (s + 0x6d2b79f5) >>> 0;
    let t = s;
    t = Math.imul(t ^ (t >>> 15), 1 | t);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function makeCanvas(w, h) {
  const c = document.createElement('canvas');
  c.width = w;
  c.height = h;
  return c;
}

/**
 * 两点间圆柱（与 hero.js 的 rod 同思路）：用四元数把局部 +Y 轴旋到 b-a 方向。
 * 仅用于建模期（非每帧），允许分配。
 */
const _rodDir = new THREE.Vector3();
const _rodMid = new THREE.Vector3();
const _rodUp = new THREE.Vector3(0, 1, 0);
function rod(a, b, r, material, seg = 8) {
  _rodDir.copy(b).sub(a);
  const len = _rodDir.length();
  const mesh = new THREE.Mesh(new THREE.CylinderGeometry(r, r, Math.max(len, 1e-3), seg), material);
  _rodMid.copy(a).add(b).multiplyScalar(0.5);
  mesh.position.copy(_rodMid);
  if (len > 1e-6) {
    mesh.quaternion.setFromUnitVectors(_rodUp, _rodDir.normalize());
  }
  return mesh;
}

/** 圆角矩形 Shape（大屏边框用） */
function roundedRectShape(w, h, r) {
  const s = new THREE.Shape();
  s.moveTo(-w / 2 + r, -h / 2);
  s.lineTo(w / 2 - r, -h / 2);
  s.quadraticCurveTo(w / 2, -h / 2, w / 2, -h / 2 + r);
  s.lineTo(w / 2, h / 2 - r);
  s.quadraticCurveTo(w / 2, h / 2, w / 2 - r, h / 2);
  s.lineTo(-w / 2 + r, h / 2);
  s.quadraticCurveTo(-w / 2, h / 2, -w / 2, h / 2 - r);
  s.lineTo(-w / 2, -h / 2 + r);
  s.quadraticCurveTo(-w / 2, -h / 2, -w / 2 + r, -h / 2);
  return s;
}

/** 让物体的 +Z 面朝向岛心 (0,0)：rotation.y = atan2(-x, -z) */
function yawToCenter(x, z) {
  return Math.atan2(-x, -z);
}

/* ==========================================================================
 * 4. 程序化贴图
 * ========================================================================== */

/**
 * 木栈道贴图：木板条纹（沿行进方向为板长，板缝垂直于行进方向）+ 白色中线。
 * v（纹理纵向）对应"横跨栈道"方向，v=0.5 正好落在中线 → 与左中右三点几何吻合。
 */
function makeBoardwalkTexture() {
  const W = 512;
  const H = 256;
  const cv = makeCanvas(W, H);
  const g = cv.getContext('2d');

  // 基色（热带硬木）
  g.fillStyle = '#a8793f';
  g.fillRect(0, 0, W, H);

  const planks = 4;
  const pw = W / planks;
  for (let i = 0; i < planks; i++) {
    // 每块板亮度抖动：模拟木材色差，避免整条栈道死板
    const l = 0.90 + ((i * 37) % 11) / 55;
    g.fillStyle = `rgb(${Math.round(168 * l)},${Math.round(121 * l)},${Math.round(63 * l)})`;
    g.fillRect(i * pw, 0, pw, H);

    // 木纹：沿板长（canvas x）方向的长细纹
    g.strokeStyle = 'rgba(92,62,28,0.20)';
    g.lineWidth = 1;
    for (let k = 0; k < 12; k++) {
      const y = (k + 0.5) * (H / 12) + Math.sin(i * 1.7 + k) * 3.5;
      g.beginPath();
      g.moveTo(i * pw + 2, y);
      g.lineTo((i + 1) * pw - 2, y + Math.sin(k * 2.1 + i) * 1.6);
      g.stroke();
    }

    // 板缝（垂直于行进方向）
    g.fillStyle = 'rgba(66,42,18,0.55)';
    g.fillRect(i * pw - 1, 0, 2, H);
  }

  // 中线两侧的磨损渐变（视觉上让步道中线有"被踩出来"的过渡）
  const wear = g.createLinearGradient(0, H / 2 - 26, 0, H / 2 + 26);
  wear.addColorStop(0, 'rgba(255,246,220,0.0)');
  wear.addColorStop(0.5, 'rgba(255,246,220,0.22)');
  wear.addColorStop(1, 'rgba(255,246,220,0.0)');
  g.fillStyle = wear;
  g.fillRect(0, H / 2 - 26, W, 52);

  // 白色中线（SPEC §3.4）
  g.fillStyle = 'rgba(246,246,238,0.94)';
  g.fillRect(0, H / 2 - 5, W, 10);

  const tex = new THREE.CanvasTexture(cv);
  tex.colorSpace = THREE.SRGBColorSpace;
  tex.wrapS = THREE.RepeatWrapping;
  tex.wrapT = THREE.RepeatWrapping;
  // SPEC §3.4：木板贴图 anisotropy = 8
  tex.anisotropy = 8;
  // repeat 沿曲线方向（u = 行进方向）：一圈重复 nTiles 次，v 方向保持 1 段
  tex.repeat.set(Math.max(1, Math.round(TRACK_LENGTH / 3.2)), 1);
  return tex;
}

/** 沙滩伞伞面：6 条交替色扇形（u 沿圆锥周向） */
function makeUmbrellaTexture() {
  const W = 384;
  const H = 64;
  const cv = makeCanvas(W, H);
  const g = cv.getContext('2d');
  const cols = ['#ff5d6c', '#fff3e0', '#ffb703', '#fff3e0', '#4cc9f0', '#fff3e0'];
  const seg = W / cols.length;
  for (let i = 0; i < cols.length; i++) {
    g.fillStyle = cols[i];
    g.fillRect(i * seg, 0, seg + 1, H);
  }
  // 伞面边缘稍暗，增加体积感
  const edge = g.createLinearGradient(0, H, 0, 0);
  edge.addColorStop(0, 'rgba(0,0,0,0.18)');
  edge.addColorStop(0.45, 'rgba(0,0,0,0.0)');
  g.fillStyle = edge;
  g.fillRect(0, 0, W, H);

  const tex = new THREE.CanvasTexture(cv);
  tex.colorSpace = THREE.SRGBColorSpace;
  tex.wrapS = THREE.RepeatWrapping;
  return tex;
}

/** 沙滩球：径向（经度）彩色分瓣 */
function makeBallTexture() {
  const W = 256;
  const H = 64;
  const cv = makeCanvas(W, H);
  const g = cv.getContext('2d');
  const cols = ['#ff4d6d', '#ffd166', '#f8f9fa', '#4cc9f0', '#f8f9fa', '#8ac926'];
  const seg = W / cols.length;
  for (let i = 0; i < cols.length; i++) {
    g.fillStyle = cols[i];
    g.fillRect(i * seg, 0, seg + 1, H);
  }
  const tex = new THREE.CanvasTexture(cv);
  tex.colorSpace = THREE.SRGBColorSpace;
  tex.wrapS = THREE.RepeatWrapping;
  return tex;
}

/* ==========================================================================
 * 5. 分件建模函数（全部程序化，无外部资源）
 * ========================================================================== */

/** 木栈道条带：左中右三个偏移点 × (400+1) 个站点，中点在 v=0.5 正好压住白色中线 */
function buildBoardwalk(map) {
  const segs = TRACK_SEGMENTS;
  const stations = segs + 1;
  const halfW = TRACK_WIDTH / 2;

  const positions = new Float32Array(stations * 3 * 3);
  const normals = new Float32Array(stations * 3 * 3);
  const uvs = new Float32Array(stations * 3 * 2);
  const indices = new Uint16Array(segs * 2 * 3);

  let vi = 0;
  let ui = 0;
  for (let i = 0; i < stations; i++) {
    // 闭合环：最后一站回到 u=0，保证首尾接缝完全重合
    const u = (i % segs) / segs;
    const s = trackSample(u);
    // 行进方向右侧向量（tangent × up），三个偏移点沿它铺开
    _v1.copy(s.tangent).cross(UP).normalize();

    for (let k = -1; k <= 1; k++) {
      const off = k * halfW;
      positions[vi] = s.pos.x + _v1.x * off;
      positions[vi + 1] = TRACK_Y; // 栈道面水平，保证主角轮子始终贴面
      positions[vi + 2] = s.pos.z + _v1.z * off;
      normals[vi] = 0;
      normals[vi + 1] = 1;
      normals[vi + 2] = 0;
      uvs[ui] = u; // u 沿曲线（纹理 repeat.x 负责重复）
      uvs[ui + 1] = (k + 1) * 0.5; // v=0/0.5/1 → 左缘/中线/右缘
      vi += 3;
      ui += 2;
    }
  }

  let ii = 0;
  for (let i = 0; i < segs; i++) {
    for (let k = 0; k < 2; k++) {
      const a = i * 3 + k;
      const b = i * 3 + k + 1;
      const c = (i + 1) * 3 + k;
      const d = (i + 1) * 3 + k + 1;
      // 逆时针（俯视）→ 法线朝上
      indices[ii++] = a;
      indices[ii++] = b;
      indices[ii++] = c;
      indices[ii++] = b;
      indices[ii++] = d;
      indices[ii++] = c;
    }
  }

  const geo = new THREE.BufferGeometry();
  geo.setAttribute('position', new THREE.BufferAttribute(positions, 3));
  geo.setAttribute('normal', new THREE.BufferAttribute(normals, 3));
  geo.setAttribute('uv', new THREE.BufferAttribute(uvs, 2));
  geo.setIndex(new THREE.BufferAttribute(indices, 1));

  const mat = new THREE.MeshStandardMaterial({
    map,
    roughness: 0.85,
    metalness: 0.0,
    // 双面：栈道从沙岛下方/海面低角度看过去也不穿孔
    side: THREE.DoubleSide
  });
  const mesh = new THREE.Mesh(geo, mat);
  mesh.name = 'boardwalk';
  mesh.receiveShadow = true;
  return mesh;
}

/** 椰子树：二次贝塞尔弯曲树干 + 7 片下垂叶片 + 3 个椰子 */
function buildPalm(rand, frondGeo, mats) {
  const group = new THREE.Group();
  const h = 3.2 + rand() * 1.1;
  // 二次贝塞尔：树干向一侧弯曲（热带海岸树常年被风吹弯的形态）
  const bend = 0.25 + rand() * 0.35;
  const curve = new THREE.QuadraticBezierCurve3(
    new THREE.Vector3(0, 0, 0),
    new THREE.Vector3(bend * 0.45, h * 0.55, 0),
    new THREE.Vector3(bend, h, 0)
  );
  // 树干半径 0.22（SPEC §3.4）
  const trunk = new THREE.Mesh(new THREE.TubeGeometry(curve, 14, 0.22, 8, false), mats.trunk);
  trunk.castShadow = true;
  trunk.receiveShadow = true;
  group.add(trunk);

  const canopy = new THREE.Group();
  const top = curve.getPoint(1, _v2);
  canopy.position.copy(top);
  group.add(canopy);

  // 7 片叶片：绕树顶均布，先微仰再自然下垂（下垂已在几何里做进 -u²·1.4）
  const spin = rand() * Math.PI * 2;
  for (let i = 0; i < 7; i++) {
    const leaf = new THREE.Mesh(frondGeo, mats.frond);
    leaf.rotation.y = spin + (i / 7) * Math.PI * 2;
    leaf.rotation.z = 0.30 + rand() * 0.14;
    leaf.castShadow = true;
    canopy.add(leaf);
  }

  // 3 个椰子
  for (let i = 0; i < 3; i++) {
    const a = spin + (i / 3) * Math.PI * 2 + 0.4;
    const nut = new THREE.Mesh(PALM_NUT_GEO, mats.nut);
    nut.position.set(Math.cos(a) * 0.26, -0.22, Math.sin(a) * 0.26);
    nut.castShadow = true;
    canopy.add(nut);
  }

  return { group, canopy };
}

// ---- 共享几何（建模期懒构建一次，多实例复用） ----
let FROND_GEO = null;
let PALM_NUT_GEO = null;
let GULL_WING_GEO = null;
let GULL_TIP_GEO = null;

/** 叶片几何：沿 x 长 1.6m，横向按 sin(u·PI) 收窄，纵向 z→y 做 -u²·1.4 的下垂 */
function makeFrondGeometry() {
  const segs = 8;
  const len = 1.6;
  const halfW = 0.28;
  const geo = new THREE.PlaneGeometry(1, 1, segs, 1);
  const pos = geo.attributes.position;
  for (let i = 0; i < pos.count; i++) {
    const u = pos.getX(i) + 0.5; // 0..1 沿叶长
    const lat = pos.getY(i) * 2; // -1..1 横向
    const w = Math.sin(u * Math.PI) * halfW; // 根/尖收窄、中段最宽
    pos.setXYZ(i, u * len, -u * u * 1.4, lat * w);
  }
  pos.needsUpdate = true;
  geo.computeVertexNormals();
  return geo;
}

/** 灯塔：5 段红白圆台 + 灯室 + 发光灯球 + 红色锥顶 + 礁石；返回夜间驱动器 */
function buildLighthouse(rand) {
  const group = new THREE.Group();

  const rockMat = new THREE.MeshStandardMaterial({ color: 0x6b6257, roughness: 1.0, flatShading: true });
  const redMat = new THREE.MeshStandardMaterial({ color: 0xd93a3a, roughness: 0.65, metalness: 0.05 });
  const whiteMat = new THREE.MeshStandardMaterial({ color: 0xf5f2ea, roughness: 0.65, metalness: 0.05 });
  const metalMat = new THREE.MeshStandardMaterial({ color: 0x2b3038, roughness: 0.5, metalness: 0.7 });
  const coneMat = new THREE.MeshStandardMaterial({ color: 0xb8272f, roughness: 0.6, metalness: 0.1 });

  // 礁石：顶面抬到 +0.55，保证常年在浪花之上
  const rock = new THREE.Mesh(new THREE.CylinderGeometry(4.2 + rand(), 5.8 + rand(), 3.6, 9), rockMat);
  rock.position.y = 0.55 - 1.8;
  rock.castShadow = true;
  rock.receiveShadow = true;
  group.add(rock);

  // 5 段圆台，半径自下而上 1.5 → 0.75，红白交替
  const segH = 1.5;
  let y = 0.55;
  for (let i = 0; i < 5; i++) {
    const rB = 1.5 - i * 0.15;
    const rT = 1.5 - (i + 1) * 0.15;
    const seg = new THREE.Mesh(
      new THREE.CylinderGeometry(rT, rB, segH, 20, 1),
      i % 2 === 0 ? whiteMat : redMat
    );
    seg.position.y = y + segH / 2;
    seg.castShadow = true;
    seg.receiveShadow = true;
    group.add(seg);
    y += segH;
  }

  // 观景台栏杆
  const rail = new THREE.Mesh(new THREE.TorusGeometry(0.95, 0.05, 6, 20), metalMat);
  rail.rotation.x = Math.PI / 2;
  rail.position.y = y + 0.02;
  group.add(rail);

  // 灯室：半透明金属框，夜里能透出里面灯球的光
  const lampRoomMat = new THREE.MeshStandardMaterial({
    color: 0x2b3038,
    roughness: 0.45,
    metalness: 0.7,
    transparent: true,
    opacity: 0.42,
    side: THREE.DoubleSide
  });
  const lampRoom = new THREE.Mesh(new THREE.CylinderGeometry(0.62, 0.68, 0.9, 12, 1, true), lampRoomMat);
  lampRoom.position.y = y + 0.45;
  group.add(lampRoom);

  // 发光灯球（夜间自发光增强，白天几乎不发光）
  const bulbMat = new THREE.MeshStandardMaterial({
    color: 0xfff2c4,
    emissive: 0xffcf5c,
    emissiveIntensity: 0.35,
    roughness: 0.35
  });
  const bulb = new THREE.Mesh(new THREE.SphereGeometry(0.42, 16, 12), bulbMat);
  bulb.position.y = y + 0.45;
  group.add(bulb);

  // 红色锥顶
  const roof = new THREE.Mesh(new THREE.ConeGeometry(0.8, 0.95, 16), coneMat);
  roof.position.y = y + 0.9 + 0.475;
  roof.castShadow = true;
  group.add(roof);

  const topBall = new THREE.Mesh(new THREE.SphereGeometry(0.09, 8, 6), metalMat);
  topBall.position.y = y + 0.9 + 0.95 + 0.07;
  group.add(topBall);

  // ---- 夜间：加法混合锥形光束（长 40），绕竖轴 0.8 rad/s 旋转 ----
  const beamH = 40;
  const beamGeo = new THREE.ConeGeometry(1.35, beamH, 24, 1, true);
  beamGeo.translate(0, -beamH / 2, 0); // 顶点（灯口）移到原点
  beamGeo.rotateX(-Math.PI / 2); // 光轴指向局部 +Z
  const beamMat = new THREE.MeshBasicMaterial({
    color: 0xfff3c0,
    transparent: true,
    opacity: 0,
    blending: THREE.AdditiveBlending,
    depthWrite: false,
    side: THREE.DoubleSide,
    toneMapped: false
  });
  const beam = new THREE.Mesh(beamGeo, beamMat);
  const beamPivot = new THREE.Group();
  beamPivot.position.y = y + 0.45;
  beamPivot.add(beam);
  beam.visible = false;
  group.add(beamPivot);

  // 夜间点光源：强度 60（SPEC §3.4）
  const lampLight = new THREE.PointLight(0xffe0a0, 0, 55, 2);
  lampLight.position.y = y + 0.45;
  lampLight.visible = false;
  group.add(lampLight);

  return { group, beam, beamMat, beamPivot, lampLight, bulbMat };
}

/** 大屏幕：6×3.6 圆角边框 + 视频面片（MeshBasicMaterial + VideoTexture，双面） */
function buildBigScreen(videoEl, posterFallback) {
  const W = 6;
  const H = 3.6;
  const group = new THREE.Group();

  // 圆角金属边框
  const frameGeo = new THREE.ExtrudeGeometry(roundedRectShape(W + 0.3, H + 0.3, 0.35), {
    depth: 0.22,
    bevelEnabled: true,
    bevelSize: 0.03,
    bevelThickness: 0.03,
    bevelSegments: 2,
    curveSegments: 6
  });
  frameGeo.translate(0, 0, -0.11);
  const frameMat = new THREE.MeshStandardMaterial({ color: 0x22262c, roughness: 0.45, metalness: 0.6 });
  const frame = new THREE.Mesh(frameGeo, frameMat);
  frame.castShadow = true;
  group.add(frame);

  // 视频面片：VideoTexture（若调用方未提供 videoEl 则退化为占位贴图，不抛错）
  let screenTexture;
  if (videoEl) {
    screenTexture = new THREE.VideoTexture(videoEl);
    screenTexture.colorSpace = THREE.SRGBColorSpace;
    screenTexture.minFilter = THREE.LinearFilter;
    screenTexture.magFilter = THREE.LinearFilter;
    screenTexture.generateMipmaps = false;
    screenTexture.wrapS = THREE.ClampToEdgeWrapping;
    screenTexture.wrapT = THREE.ClampToEdgeWrapping;
  } else {
    screenTexture = posterFallback;
  }
  const screenMat = new THREE.MeshBasicMaterial({
    map: screenTexture,
    toneMapped: false, // 屏幕是自发光显示设备，不参与色调映射
    side: THREE.DoubleSide
  });
  const panel = new THREE.Mesh(new THREE.PlaneGeometry(W, H), screenMat);
  panel.position.z = 0.13;
  group.add(panel);

  // 两根支腿
  const legMat = new THREE.MeshStandardMaterial({ color: 0x3a3f47, roughness: 0.5, metalness: 0.5 });
  group.add(rod(new THREE.Vector3(-W * 0.32, -H / 2 - 0.15, 0), new THREE.Vector3(-W * 0.32, -H / 2 - 1.6, 0.25), 0.09, legMat));
  group.add(rod(new THREE.Vector3(W * 0.32, -H / 2 - 0.15, 0), new THREE.Vector3(W * 0.32, -H / 2 - 1.6, 0.25), 0.09, legMat));

  return { group, screenTexture };
}

/**
 * 画架：金色画框 + 海报 + 三根木支架。
 * posterTexture 可为 null —— 为 null 时海报用纯色板占位（SPEC 未提供海报贴图时的降级路径），
 * 由 main.js 决定是否传入 assets/poster.jpg 解码后的 THREE.Texture。
 */
function buildEasel(posterTexture) {
  const group = new THREE.Group();
  const W = 1.5;
  const H = 0.86;

  const goldMat = new THREE.MeshStandardMaterial({ color: 0xd9a441, roughness: 0.25, metalness: 0.9 });
  const woodMat = new THREE.MeshStandardMaterial({ color: 0x8b5e34, roughness: 0.85, metalness: 0.0 });

  // 画框板（略微前倾，像真的立在画架上）
  const panel = new THREE.Group();
  panel.position.y = 1.3;
  panel.rotation.x = -0.14;
  group.add(panel);

  const bar = 0.07;
  const mk = (w, h, x, y) => {
    const m = new THREE.Mesh(new THREE.BoxGeometry(w, h, 0.06), goldMat);
    m.position.set(x, y, 0);
    m.castShadow = true;
    return m;
  };
  panel.add(mk(W + bar * 2, bar, 0, H / 2 + bar / 2));
  panel.add(mk(W + bar * 2, bar, 0, -H / 2 - bar / 2));
  panel.add(mk(bar, H, -W / 2 - bar / 2, 0));
  panel.add(mk(bar, H, W / 2 + bar / 2, 0));

  // 海报：有贴图用贴图，否则纯色占位
  const posterMat = new THREE.MeshStandardMaterial({
    map: posterTexture || null,
    color: posterTexture ? 0xffffff : 0xe8dcc0, // 占位色（无海报贴图时）
    roughness: 0.7,
    metalness: 0.0,
    side: THREE.DoubleSide
  });
  const poster = new THREE.Mesh(new THREE.PlaneGeometry(W, H), posterMat);
  poster.position.z = 0.035;
  panel.add(poster);

  // 三根木支架：两根后腿撑开 + 一根前腿（直接挂在根节点上，落地不随画框倾斜）
  group.add(
    rod(new THREE.Vector3(-0.66, 2.5, -0.08), new THREE.Vector3(-0.86, 0.0, -0.74), 0.045, woodMat)
  );
  group.add(
    rod(new THREE.Vector3(0.66, 2.5, -0.08), new THREE.Vector3(0.86, 0.0, -0.74), 0.045, woodMat)
  );
  group.add(rod(new THREE.Vector3(0, 0.9, 0.08), new THREE.Vector3(0, 0.0, 0.78), 0.045, woodMat));

  return group;
}

/** 云组：4–7 个 Icosahedron，微自发光；返回用于缓慢漂移的参数 */
function buildCloud(rand, mat) {
  const group = new THREE.Group();
  const n = 4 + Math.floor(rand() * 4); // 4..7
  for (let i = 0; i < n; i++) {
    const r = 1.5 + rand() * 2.0;
    const puff = new THREE.Mesh(new THREE.IcosahedronGeometry(r, 0), mat);
    puff.position.set((rand() - 0.5) * 7.5, (rand() - 0.5) * 1.2, (rand() - 0.5) * 4.0);
    puff.scale.set(1, 0.6 + rand() * 0.25, 1);
    group.add(puff);
  }
  const s = 1.4 + rand() * 1.9;
  group.scale.set(s, s * 0.9, s);
  group.position.set((rand() - 0.5) * 360, 26 + rand() * 22, (rand() - 0.5) * 360);
  return { group, speed: 0.5 + rand() * 1.0 }; // 缓慢漂移 m/s
}

/**
 * 海鸥：两段翅膀正弦扇动。
 * 翅膀展向沿局部 ±z；扇动用 rotation.x（绕机身前后轴），因此左右翼可以同号驱动，
 * 镜像靠"翼面 mesh 自身的 rotation.y = π"实现，避免欧拉角顺序歧义。
 */
function buildGull(rand, mats) {
  const group = new THREE.Group();

  const body = new THREE.Mesh(new THREE.SphereGeometry(0.22, 12, 8), mats.body);
  body.scale.set(1.9, 0.75, 0.9);
  body.castShadow = true;
  group.add(body);

  const head = new THREE.Mesh(new THREE.SphereGeometry(0.11, 10, 8), mats.body);
  head.position.set(0.36, 0.06, 0);
  group.add(head);

  const beak = new THREE.Mesh(new THREE.ConeGeometry(0.05, 0.2, 6), mats.beak);
  beak.rotation.z = -Math.PI / 2; // 锥尖指向 +x（机头方向）
  beak.position.set(0.5, 0.04, 0);
  group.add(beak);

  const tail = new THREE.Mesh(new THREE.ConeGeometry(0.09, 0.3, 6), mats.wingTip);
  tail.rotation.z = Math.PI / 2;
  tail.position.set(-0.44, 0.02, 0);
  group.add(tail);

  const wings = [];
  for (let side = 0; side < 2; side++) {
    // side=0 → +z 翼；side=1 → -z 翼（镜像由翼面 mesh 的 rotation.y = π 完成）
    const mirror = side === 1;
    const wing = new THREE.Group();
    wing.position.set(0.03, 0.05, mirror ? -0.09 : 0.09);
    group.add(wing);

    const inner = new THREE.Mesh(GULL_WING_GEO, mats.wing);
    inner.rotation.y = mirror ? Math.PI : 0;
    wing.add(inner);

    const outerGroup = new THREE.Group();
    outerGroup.position.set(0, 0, 0.7);
    wing.add(outerGroup);

    const outer = new THREE.Mesh(GULL_TIP_GEO, mats.wingTip);
    outer.rotation.y = mirror ? Math.PI : 0;
    outerGroup.add(outer);

    wings.push({ wing, outerGroup });
  }

  return {
    group,
    left: wings[0],
    right: wings[1],
    radius: 30 + rand() * 45, // 绕岛不同半径盘旋
    height: 12 + rand() * 11,
    omega: (0.12 + rand() * 0.12) * (rand() < 0.5 ? 1 : -1),
    phase: rand() * Math.PI * 2,
    flapPhase: rand() * Math.PI * 2,
    bob: 0.4 + rand() * 0.9
  };
}

/** 帆船：随正弦浪起伏 + 绕岛航行 */
function buildBoat(rand, mats) {
  const group = new THREE.Group();

  const hull = new THREE.Mesh(new THREE.SphereGeometry(0.7, 16, 10), mats.hull);
  hull.scale.set(1.6, 0.5, 0.8);
  hull.position.y = 0.18;
  hull.castShadow = true;
  group.add(hull);

  const deck = new THREE.Mesh(new THREE.BoxGeometry(1.9, 0.12, 0.86), mats.deck);
  deck.position.y = 0.3;
  group.add(deck);

  const mast = new THREE.Mesh(new THREE.CylinderGeometry(0.05, 0.05, 3.2, 8), mats.deck);
  mast.position.set(0.1, 1.8, 0);
  mast.castShadow = true;
  group.add(mast);

  // 三角主帆（ShapeGeometry，位于 xy 平面 → 帆面含船的纵轴，方向正确）
  const sailShape = new THREE.Shape();
  sailShape.moveTo(0, 0);
  sailShape.lineTo(0, 2.9);
  sailShape.lineTo(1.4, 0.4);
  sailShape.lineTo(0, 0);
  const sail = new THREE.Mesh(new THREE.ShapeGeometry(sailShape), mats.sail);
  sail.position.set(0.14, 0.4, 0);
  sail.castShadow = true;
  group.add(sail);

  const flag = new THREE.Mesh(new THREE.PlaneGeometry(0.42, 0.24), mats.sail);
  flag.position.set(0.34, 3.35, 0);
  group.add(flag);

  group.scale.setScalar(1.6); // 帆船在 45–80m 外航行，放大到肉眼可辨

  return {
    group,
    radius: 44 + rand() * 34,
    omega: (0.03 + rand() * 0.03) * (rand() < 0.5 ? 1 : -1),
    phase: rand() * Math.PI * 2
  };
}

/** 沙滩伞：木杆 + 条纹伞面（圆锥开口）+ 顶球 */
function buildUmbrella(rand, poleMat, canopyMat, ballMat) {
  const group = new THREE.Group();
  const pole = new THREE.Mesh(new THREE.CylinderGeometry(0.055, 0.055, 2.3, 8), poleMat);
  pole.position.y = 1.15;
  pole.castShadow = true;
  group.add(pole);

  const canopyGeo = new THREE.ConeGeometry(1.45, 0.62, 12, 1, true);
  const canopy = new THREE.Mesh(canopyGeo, canopyMat);
  canopy.position.y = 2.42;
  canopy.castShadow = true;
  group.add(canopy);

  const tip = new THREE.Mesh(new THREE.SphereGeometry(0.07, 8, 6), ballMat);
  tip.position.y = 2.78;
  group.add(tip);

  group.rotation.z = 0.08 + rand() * 0.06; // 略微歪斜，像被风吹过
  group.rotation.y = rand() * Math.PI * 2;
  return group;
}

/** 沙滩球 */
function buildBeachBall(ballTex) {
  const mat = new THREE.MeshStandardMaterial({ map: ballTex, roughness: 0.55, metalness: 0.0 });
  const ball = new THREE.Mesh(new THREE.SphereGeometry(0.34, 20, 14), mat);
  ball.castShadow = true;
  return ball;
}

/* ==========================================================================
 * 6. 主入口
 * ========================================================================== */

/**
 * 构建整个环境并加入 scene。
 * @param {THREE.Scene} scene
 * @param {{ videoEl?: HTMLVideoElement, posterTexture?: THREE.Texture|null }} opts
 *        videoEl —— 大屏幕视频元素（由 main.js 创建）；
 *        posterTexture —— 画架海报贴图，可为 null（为 null 时画架用纯色板占位）。
 * @returns {{
 *   update: (dt:number, t:number, night:number) => void,
 *   screenTexture: THREE.Texture,
 *   setTrackLengthInfo: () => void
 * }}
 */
export function buildWorld(scene, opts) {
  const o = opts || {};
  const rand = makeRand(20240921); // 固定种子：布局可复现

  const root = new THREE.Group();
  root.name = 'world';
  scene.add(root);

  /* ------------------------------ 6.1 天空 ------------------------------ */
  const skyMat = new THREE.ShaderMaterial({
    uniforms: {
      topColor: { value: SKY_TOP_DAY.clone() },
      bottomColor: { value: SKY_BOTTOM_DAY.clone() },
      sunColor: { value: SUN_COLOR_DAY.clone() },
      sunDir: { value: SUN_DIR.clone() },
      sunIntensity: { value: 1.0 }
    },
    vertexShader: SKY_VERT,
    fragmentShader: SKY_FRAG,
    side: THREE.BackSide, // 球内观看
    depthWrite: false, // 天空永远作为背景，不参与深度写入
    fog: false // 天空自身不参与雾衰减，否则地平线会出现一条灰带
  });
  const sky = new THREE.Mesh(new THREE.SphereGeometry(600, 32, 24), skyMat);
  sky.name = 'sky';
  sky.renderOrder = -1;
  sky.frustumCulled = false; // 相机在球内部，包围盒剔除会误判
  root.add(sky);

  /* 星空：1500 个 Points，半径 500，只在上半球，opacity = night（SPEC §3.2） */
  const starPos = new Float32Array(STAR_COUNT * 3);
  for (let i = 0; i < STAR_COUNT; i++) {
    const y = rand(); // 均匀 y → 半球上均匀分布
    const a = rand() * Math.PI * 2;
    const rxz = Math.sqrt(Math.max(0, 1 - y * y));
    starPos[i * 3] = Math.cos(a) * rxz * STAR_RADIUS;
    starPos[i * 3 + 1] = y * STAR_RADIUS;
    starPos[i * 3 + 2] = Math.sin(a) * rxz * STAR_RADIUS;
  }
  const starGeo = new THREE.BufferGeometry();
  starGeo.setAttribute('position', new THREE.BufferAttribute(starPos, 3));
  const starMat = new THREE.PointsMaterial({
    color: 0xffffff,
    size: 2.2,
    sizeAttenuation: false,
    transparent: true,
    opacity: 0,
    depthWrite: false,
    fog: false,
    blending: THREE.AdditiveBlending
  });
  const stars = new THREE.Points(starGeo, starMat);
  stars.name = 'stars';
  stars.frustumCulled = false;
  stars.visible = false;
  root.add(stars);

  /* ------------------------------ 6.2 海洋 ------------------------------ */
  const oceanGeo = new THREE.PlaneGeometry(1200, 1200, 220, 220);
  // 一次性把平面转到水平：顶点 y 即波高、x/z 即世界水平坐标，着色器里无需再换坐标轴
  oceanGeo.rotateX(-Math.PI / 2);

  const oceanMat = new THREE.ShaderMaterial({
    uniforms: {
      uTime: { value: 0 },
      uNight: { value: 0 },
      uDeepColor: { value: OCEAN_DEEP_DAY.clone() },
      uShallowColor: { value: OCEAN_SHALLOW_DAY.clone() },
      uSkyTop: { value: SKY_TOP_DAY.clone() },
      uSkyBottom: { value: SKY_BOTTOM_DAY.clone() },
      uSunDir: { value: SUN_DIR.clone() },
      uSunColor: { value: SUN_COLOR_DAY.clone() },
      // fog 三个 uniform 必须存在：three 在 material.fog === true 时会直接写它们
      fogColor: { value: FOG_DAY.clone() },
      fogNear: { value: 60 },
      fogFar: { value: 260 }
    },
    vertexShader: OCEAN_VERT,
    fragmentShader: OCEAN_FRAG,
    fog: true // 启用 three 的雾 chunk
  });
  const ocean = new THREE.Mesh(oceanGeo, oceanMat);
  ocean.name = 'ocean';
  ocean.position.y = SEA_Y;
  root.add(ocean);

  /* --------------------------- 6.3 沙岛与草地 --------------------------- */
  const sandMat = new THREE.MeshStandardMaterial({ color: 0xf1d9a2, roughness: 0.98, metalness: 0.0 });
  const sand = new THREE.Mesh(new THREE.CylinderGeometry(ISLAND_TOP_R, 34, 2, 64, 1), sandMat);
  sand.name = 'sand';
  sand.position.y = -0.98; // 顶面 y = 0.02，底面 y = -1.98（沉到水下）
  sand.receiveShadow = true;
  root.add(sand);

  const grassGeo = new THREE.CircleGeometry(24, 64);
  {
    // 顶点半径扰动 1 + sin(5a)*0.06 + sin(11a)*0.03 → 自然的不规则草地轮廓
    const pos = grassGeo.attributes.position;
    for (let i = 0; i < pos.count; i++) {
      const x = pos.getX(i);
      const y = pos.getY(i);
      const r = Math.hypot(x, y);
      if (r > 1e-4) {
        const a = Math.atan2(y, x);
        const f = 1 + Math.sin(5 * a) * 0.06 + Math.sin(11 * a) * 0.03;
        pos.setXY(i, x * f, y * f);
      }
    }
    pos.needsUpdate = true;
    grassGeo.computeVertexNormals();
    grassGeo.rotateX(-Math.PI / 2); // 转到水平面
  }
  const grassMat = new THREE.MeshStandardMaterial({ color: 0x7cc95b, roughness: 0.95, metalness: 0.0 });
  const grass = new THREE.Mesh(grassGeo, grassMat);
  grass.name = 'grass';
  grass.position.y = GRASS_Y;
  grass.receiveShadow = true;
  root.add(grass);

  /* ---------------------------- 6.4 木栈道赛道 ---------------------------- */
  const boardwalkTex = makeBoardwalkTexture();
  root.add(buildBoardwalk(boardwalkTex));

  /* ---------------------------- 6.5 椰子树 11 棵 --------------------------- */
  FROND_GEO = makeFrondGeometry();
  PALM_NUT_GEO = new THREE.SphereGeometry(0.17, 10, 8);
  const palmMats = {
    trunk: new THREE.MeshStandardMaterial({ color: 0x9c7a4e, roughness: 0.9, metalness: 0.0 }),
    frond: new THREE.MeshStandardMaterial({
      color: 0x3f9c46,
      roughness: 0.8,
      metalness: 0.0,
      side: THREE.DoubleSide // 叶片是零厚度面片
    }),
    nut: new THREE.MeshStandardMaterial({ color: 0x6b4a2a, roughness: 0.85 })
  };

  // 道具避让：已摆放位置列表 + 赛道中心线安全距离（栈道半宽 1.6 + 余量）
  const placed = [];
  function findSpot(minR, maxR, trackClear, otherClear) {
    for (let k = 0; k < 260; k++) {
      // 后 1/3 的尝试放宽约束，保证任何情况下都能凑够数量
      const relax = k > 175 ? 0.65 : 1;
      const a = rand() * Math.PI * 2;
      const r = minR + rand() * (maxR - minR);
      const x = Math.cos(a) * r;
      const z = Math.sin(a) * r;
      if (distanceToTrack(x, z) < trackClear * relax) continue;
      let ok = true;
      const oc = otherClear * relax;
      for (let i = 0; i < placed.length; i++) {
        const dx = placed[i].x - x;
        const dz = placed[i].z - z;
        if (dx * dx + dz * dz < oc * oc) {
          ok = false;
          break;
        }
      }
      if (!ok) continue;
      placed.push({ x, z });
      return { x, z, r, a };
    }
    placed.push({ x: 0, z: 0 }); // 极端兜底（几乎不会发生）：退到岛心
    return { x: 0, z: 0, r: 0, a: 0 };
  }

  const palms = [];
  for (let i = 0; i < 11; i++) {
    const spot = findSpot(4.0, 23.0, 2.8, 2.7);
    const palm = buildPalm(rand, FROND_GEO, palmMats);
    palm.group.position.set(spot.x, GRASS_Y, spot.z);
    palm.group.rotation.y = spot.a; // 树干弯曲方向随方位角旋转，朝向更自然
    palm.group.scale.setScalar(0.85 + rand() * 0.35);
    root.add(palm.group);
    palms.push({ canopy: palm.canopy, phase: rand() * Math.PI * 2 });
  }

  /* ----------------------------- 6.6 灯塔 ------------------------------ */
  const lhX = Math.cos(LIGHTHOUSE_ANGLE) * 36;
  const lhZ = Math.sin(LIGHTHOUSE_ANGLE) * 36;
  const lighthouse = buildLighthouse(rand);
  lighthouse.group.position.set(lhX, 0, lhZ);
  lighthouse.group.rotation.y = yawToCenter(lhX, lhZ);
  lighthouse.group.scale.setScalar(1.15);
  root.add(lighthouse.group);

  /* ---------------------- 6.7 大屏幕 + 画架（靠海一侧） -------------------- */
  const posterFallback = new THREE.CanvasTexture(
    (() => {
      // 无海报贴图时的纯色占位（SPEC 未提供 posterTexture 时的降级路径）
      const cv = makeCanvas(4, 4);
      const g = cv.getContext('2d');
      g.fillStyle = '#e8dcc0';
      g.fillRect(0, 0, 4, 4);
      return cv;
    })()
  );
  posterFallback.colorSpace = THREE.SRGBColorSpace;

  const screenA = buildBigScreen(o.videoEl, posterFallback);
  const sx = Math.cos(SCREEN_ANGLE) * 27;
  const sz = Math.sin(SCREEN_ANGLE) * 27;
  // 外层负责朝向岛心，内层负责"略仰角"（避免一次设置两个欧拉角带来顺序歧义）
  const screenYaw = new THREE.Group();
  screenYaw.position.set(sx, 2.0, sz);
  screenYaw.rotation.y = yawToCenter(sx, sz);
  const screenTilt = new THREE.Group();
  screenTilt.rotation.x = -0.07; // 屏幕法线略向上抬
  screenTilt.add(screenA.group);
  screenYaw.add(screenTilt);
  root.add(screenYaw);

  const ex = Math.cos(EASEL_ANGLE) * 26.5;
  const ez = Math.sin(EASEL_ANGLE) * 26.5;
  const easel = buildEasel(o.posterTexture || null);
  easel.position.set(ex, 0.02, ez);
  easel.rotation.y = yawToCenter(ex, ez);
  root.add(easel);

  /* --------------------------- 6.8 沙滩伞 + 沙滩球 --------------------------- */
  const umbrellaTex = makeUmbrellaTexture();
  const poleMat = new THREE.MeshStandardMaterial({ color: 0xded3bd, roughness: 0.7 });
  const canopyMat = new THREE.MeshStandardMaterial({
    map: umbrellaTex,
    roughness: 0.7,
    metalness: 0.0,
    side: THREE.DoubleSide
  });
  const tipMat = new THREE.MeshStandardMaterial({ color: 0xffb703, roughness: 0.5 });
  const umbrellaAngles = [-1.62, -2.74, 2.18, 3.55];
  for (let i = 0; i < 4; i++) {
    const r = 25.6 + i * 0.8;
    const a = umbrellaAngles[i];
    const u = buildUmbrella(rand, poleMat, canopyMat, tipMat);
    u.position.set(Math.cos(a) * r, 0.02, Math.sin(a) * r);
    root.add(u);
  }

  const ballTex = makeBallTexture();
  const balls = [];
  for (let i = 0; i < 2; i++) {
    const a = umbrellaAngles[i * 2] + 0.28;
    const r = 27.4 + i * 0.6;
    const ball = buildBeachBall(ballTex);
    const bx = Math.cos(a) * r;
    const bz = Math.sin(a) * r;
    ball.position.set(bx, 0.36, bz);
    ball.rotation.y = rand() * Math.PI;
    root.add(ball);
    balls.push({ mesh: ball, baseY: 0.36, phase: rand() * Math.PI * 2 });
  }

  /* ------------------------------ 6.9 云 14 组 ----------------------------- */
  const cloudMat = new THREE.MeshStandardMaterial({
    color: CLOUD_DAY.clone(),
    emissive: 0xdfe9ff,
    emissiveIntensity: 0.3, // 微自发光：白天不脏、夜间不黑
    roughness: 1.0,
    metalness: 0.0,
    flatShading: true
  });
  const clouds = [];
  for (let i = 0; i < 14; i++) {
    const c = buildCloud(rand, cloudMat);
    root.add(c.group);
    clouds.push(c);
  }

  /* ------------------------------ 6.10 海鸥 9 只 ---------------------------- */
  GULL_WING_GEO = (() => {
    const g = new THREE.PlaneGeometry(0.34, 0.7, 1, 1);
    g.rotateX(-Math.PI / 2); // 展成水平翼面（x=弦向，z=展向）
    g.translate(0, 0, 0.35); // 展向从肩部向 +z 伸出
    return g;
  })();
  GULL_TIP_GEO = (() => {
    const g = new THREE.PlaneGeometry(0.26, 0.5, 1, 1);
    g.rotateX(-Math.PI / 2);
    g.translate(0, 0, 0.25);
    return g;
  })();
  const gullMats = {
    body: new THREE.MeshStandardMaterial({ color: 0xf7f9fc, roughness: 0.75, side: THREE.DoubleSide }),
    wing: new THREE.MeshStandardMaterial({ color: 0xeef2f8, roughness: 0.75, side: THREE.DoubleSide }),
    wingTip: new THREE.MeshStandardMaterial({ color: 0x6b7684, roughness: 0.8, side: THREE.DoubleSide }),
    beak: new THREE.MeshStandardMaterial({ color: 0xffb703, roughness: 0.5 })
  };
  const gulls = [];
  for (let i = 0; i < 9; i++) {
    const g = buildGull(rand, gullMats);
    root.add(g.group);
    gulls.push(g);
  }

  /* ------------------------------ 6.11 帆船 3 艘 ---------------------------- */
  const boatMats = {
    hull: new THREE.MeshStandardMaterial({ color: 0xf4f1ea, roughness: 0.6 }),
    deck: new THREE.MeshStandardMaterial({ color: 0xa06a3b, roughness: 0.8 }),
    sail: new THREE.MeshStandardMaterial({ color: 0xfff6e2, roughness: 0.8, side: THREE.DoubleSide })
  };
  const boats = [];
  for (let i = 0; i < 3; i++) {
    const b = buildBoat(rand, boatMats);
    root.add(b.group);
    boats.push(b);
  }

  /* ========================================================================
   * 7. 每帧更新：night（0=白天 1=夜晚）线性驱动所有昼夜相关参数。
   *    调用方（main.js）已对 night 做过平滑，这里不再叠加低通，
   *    因此 QA 直接置 state.night = 0.99 也会立刻生效。
   *    所有 uniform 均为"就地改材质上的 uniforms 对象"，不做 UniformsUtils 替换。
   * ====================================================================== */
  const skyU = skyMat.uniforms;
  const oceanU = oceanMat.uniforms;

  function update(dt, t, night) {
    const n = THREE.MathUtils.clamp(night === undefined ? 0 : night, 0, 1);
    const d = Math.min(Math.max(dt || 0, 0), 0.1); // 掉帧时钳制，避免云/动画瞬跳
    const tt = t || 0;

    /* ---- 天空：色板插值 + 日轮强度 ---- */
    skyU.topColor.value.copy(_c1.copy(SKY_TOP_DAY).lerp(SKY_TOP_NIGHT, n));
    skyU.bottomColor.value.copy(_c1.copy(SKY_BOTTOM_DAY).lerp(SKY_BOTTOM_NIGHT, n));
    _c1.copy(SUN_COLOR_DAY).lerp(SUN_COLOR_NIGHT, n);
    skyU.sunColor.value.copy(_c1);
    skyU.sunIntensity.value = 1.0 - n * 0.88; // 夜里日轮几乎熄灭，只留一点天光

    /* ---- 星空：opacity = night ---- */
    starMat.opacity = n;
    stars.visible = n > 0.01;

    /* ---- 雾：同步场景雾色（SPEC §3.7 夜雾 #0e1838），并保证海面雾色一致 ---- */
    _c1.copy(FOG_DAY).lerp(FOG_NIGHT, n);
    if (scene.fog && scene.fog.color) scene.fog.color.copy(_c1);
    oceanU.fogColor.value.copy(_c1);

    /* ---- 海洋：时间 + 日夜色板 + 菲涅尔天空色 ---- */
    oceanU.uTime.value = tt;
    oceanU.uNight.value = n;
    oceanU.uDeepColor.value.copy(_c1.copy(OCEAN_DEEP_DAY).lerp(OCEAN_DEEP_NIGHT, n));
    oceanU.uShallowColor.value.copy(_c1.copy(OCEAN_SHALLOW_DAY).lerp(OCEAN_SHALLOW_NIGHT, n));
    oceanU.uSkyTop.value.copy(_c1.copy(SKY_TOP_DAY).lerp(SKY_TOP_NIGHT, n));
    oceanU.uSkyBottom.value.copy(_c1.copy(SKY_BOTTOM_DAY).lerp(SKY_BOTTOM_NIGHT, n));
    oceanU.uSunColor.value.copy(_c1.copy(SUN_COLOR_DAY).lerp(SUN_COLOR_NIGHT, n));

    /* ---- 灯塔：0.8 rad/s 旋转光束 + 夜间点光源 60 + 灯球自发光 ---- */
    lighthouse.beamPivot.rotation.y = tt * 0.8;
    const beamOn = n > 0.02;
    lighthouse.beam.visible = beamOn;
    if (beamOn) lighthouse.beamMat.opacity = 0.34 * n;
    lighthouse.lampLight.visible = beamOn;
    lighthouse.lampLight.intensity = 60 * n;
    lighthouse.bulbMat.emissiveIntensity = 0.35 + n * 2.4;

    /* ---- 云：缓慢漂移（x 方向，越过边界回绕）+ 夜间压暗 ---- */
    for (let i = 0; i < clouds.length; i++) {
      const c = clouds[i];
      c.group.position.x += c.speed * d;
      if (c.group.position.x > 200) c.group.position.x -= 400;
    }
    cloudMat.color.copy(_c1.copy(CLOUD_DAY).lerp(CLOUD_NIGHT, n));
    cloudMat.emissiveIntensity = 0.3 - n * 0.18;

    /* ---- 椰子树树冠：随风摇摆 ±0.05 rad ---- */
    for (let i = 0; i < palms.length; i++) {
      const p = palms[i];
      p.canopy.rotation.z = Math.sin(tt * 1.1 + p.phase) * 0.05;
      p.canopy.rotation.x = Math.cos(tt * 0.83 + p.phase * 1.3) * 0.03;
    }

    /* ---- 海鸥：绕岛盘旋 + 两段翅膀正弦扇动 ---- */
    for (let i = 0; i < gulls.length; i++) {
      const g = gulls[i];
      const ang = tt * g.omega + g.phase;
      const ca = Math.cos(ang);
      const sa = Math.sin(ang);
      g.group.position.set(ca * g.radius, g.height + Math.sin(tt * 1.6 + g.phase) * g.bob, sa * g.radius);
      // 机头（局部 +x）对准速度方向 f = (-sin, 0, cos)：rotation.y = atan2(-fz, fx)
      g.group.rotation.y = Math.atan2(-ca, -sa);
      g.group.rotation.z = -0.22; // 盘旋时向内压的侧倾

      const flap = Math.sin(tt * 8 + g.flapPhase) * 0.55;
      const flapLag = Math.sin(tt * 8 + g.flapPhase - 0.7) * 0.45;
      g.left.wing.rotation.x = flap;
      g.right.wing.rotation.x = flap;
      g.left.outerGroup.rotation.x = flapLag;
      g.right.outerGroup.rotation.x = flapLag;
    }

    /* ---- 帆船：随正弦浪起伏 + 绕岛航行 ---- */
    for (let i = 0; i < boats.length; i++) {
      const b = boats[i];
      const ang = tt * b.omega + b.phase;
      const ca = Math.cos(ang);
      const sa = Math.sin(ang);
      const bx = ca * b.radius;
      const bz = sa * b.radius;
      // 船体吃水 0.3m + 随波升沉（与海面顶点着色器同公式）
      b.group.position.set(bx, SEA_Y + waveHeight(bx, bz, tt) * 0.75 + 0.30, bz);
      b.group.rotation.y = Math.atan2(-ca, -sa); // 船头（局部 +x）对准航向
      b.group.rotation.z = Math.sin(tt * 1.3 + b.phase) * 0.07; // 横摇
      b.group.rotation.x = Math.sin(tt * 0.9 + b.phase * 1.7) * 0.05; // 纵摇
    }

    /* ---- 沙滩球：轻微起伏（沙滩上的静态道具也带一点生气） ---- */
    for (let i = 0; i < balls.length; i++) {
      const bo = balls[i];
      bo.mesh.position.y = bo.baseY + Math.sin(tt * 1.4 + bo.phase) * 0.02;
    }
  }

  /**
   * 输出赛道长度信息（调试用；接口契约为 () => void）。
   * 只做 console 输出，不返回值。
   */
  function setTrackLengthInfo() {
    if (typeof console !== 'undefined' && console.log) {
      console.log(
        `[scene] 赛道：闭合 CatmullRom，12 控制点，长度 ${TRACK_LENGTH.toFixed(2)} m，` +
          `${TRACK_SEGMENTS} 段，栈道宽 ${TRACK_WIDTH} m`
      );
    }
  }

  return {
    update,
    screenTexture: screenA.screenTexture,
    setTrackLengthInfo
  };
}
