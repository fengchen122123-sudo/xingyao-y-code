# 鹈鹕骑单车 3D 页面 · 工程规格书

> 本文件是唯一事实来源。所有实现项都必须严格遵守这里的接口契约与参数值。
> 工程根目录：`D:\fengchenhao\ycode\xingyao-y-code\pelican-3d`

## 0. 交付目标

- 最终产物：**单个自包含 HTML**：`dist/pelican-bicycle-3d.html`，目标体积 ≈2MB（上限 4MB），
  **不依赖任何外部网络资源**（three.js 打好的 IIFE 内联；图片/视频 base64 内联；音频运行时合成）。
- 运行时必须能在有 CSP 限制的托管环境（允许 `data:` 图片，禁止 `data:` 媒体，允许 `blob:`）下工作。
  因此音频/视频的 base64 必须走 `atob → Uint8Array → Blob → URL.createObjectURL`，**禁止 `fetch(data:)`**。
- 浏览器要求：支持 WebGL2 的现代浏览器；软件渲染（SwiftShader）也能跑（帧率低但不错乱）。

## 1. 目录与文件

```
pelican-3d/
  assets/poster.jpg        # 已生成：1280x731 JPEG 150KB（AI 海报）
  assets/clip.mp4          # 已生成：640 宽 H.264 CRF30 无音轨 240KB（AI 视频，4 秒循环）
  src/scene.js             # 环境模块（天空/海洋/岛屿/赛道/灯塔/道具/大屏/画架）
  src/hero.js              # 主角模块（自行车 + 鹈鹕 + 二骨骼 IK + 姿态动画）
  src/audio.js             # 音频模块（WebAudio 合成 BGM/车铃/叫声 + Web Speech 中文配音）
  src/main.js              # 入口（渲染器/后期/相机/交互/UI/粒子/昼夜/主循环）
  tools/template.html      # HTML 模板（占位符注入）
  tools/build_embed.py     # 组装脚本：bundle + 素材 → 单 HTML
  dist/bundle.js           # esbuild 产物（中间品）
  dist/pelican-bicycle-3d.html  # 最终交付物
  qa/shoot.py              # Playwright 自检截图脚本
  qa/*.png                 # 截图
  qa/report.md             # 自检报告
```

依赖：`three@0.169.0` + `esbuild`（devDependency）。**只允许这两个**，不要引入其它运行时依赖。

## 2. 模块接口契约（必须严格一致）

### 2.1 `src/scene.js`

```js
import * as THREE from 'three';

/**
 * 构建整个环境并加入 scene。
 * @param {THREE.Scene} scene
 * @param {{ videoEl: HTMLVideoElement }} opts  // 大屏幕用的视频元素（由 main.js 创建）
 * @returns {{
 *   update: (dt:number, t:number, night:number) => void, // night: 0=白天 1=夜晚
 *   screenTexture: THREE.VideoTexture,
 *   setTrackLengthInfo: () => void
 * }}
 */
export function buildWorld(scene, opts) { /* ... */ }

/** 赛道路径采样：给定参数 u∈[0,1) 返回 { pos:THREE.Vector3, tangent:THREE.Vector3, curvature:number } */
export function trackSample(u) { /* ... */ }

export const TRACK_LENGTH = /* 曲线总长度（米，CatmullRom.getLength()） */;
```

要点：
- `trackSample` 与 `TRACK_LENGTH` 必须基于同一条闭合 CatmullRom 曲线（12 个控制点，
  `r = 17 + sin(2a)*3.5 + cos(3a)*1.5`，`z *= 0.85`，y 固定在草地表面高度 +0.02）。
- 木栈道条带宽度 3.2，400 段，左中右三个偏移点构成带状几何。
- 大屏幕使用 `opts.videoEl` 建 `VideoTexture`，材质 `MeshBasicMaterial({ map, toneMapped:false })`，双面。

### 2.2 `src/hero.js`

```js
import * as THREE from 'three';

/**
 * 构建鹈鹕 + 自行车（局部坐标：x 向前，y 向上，z 侧向，原点在车轮接地处）。
 * @param {THREE.Scene} scene
 * @returns {{
 *   root: THREE.Group,
 *   update: (dt:number, s:{
 *     pedalAngle:number, speed:number, lean:number, wheelie:number,
 *     bellT:number,            // 按铃后流逝时间，<0 表示未按铃
 *     headTurn:number,         // 头部左右张望角度（弧度）
 *     flap:number,             // 扇翅强度 0..1
 *     frontLightOn:boolean
 *   }) => void
 * }}
 */
export function buildHero(scene) { /* ... */ }
```

### 2.3 `src/audio.js`

```js
/** 创建音频系统（不播放）。所有播放方法内部自行确保 AudioContext 已 resume。 */
export function createAudio() {
  return {
    resume(): Promise<void>,       // 必须在用户手势（点击出发）中调用
    bell(): void,                  // WebAudio 合成车铃：2093Hz + 2637Hz 两声，间隔 0.12s，叠 1x/2.76x/5.4x 泛音，指数衰减
    call(): void,                  // 鹈鹕叫：锯齿波 420→260Hz，带通 900Hz Q3，历时 0.4s
    startMusic(): void,            // 启动循环 BGM
    toggleMusic(): boolean,        // 返回切换后是否在播放
    isMusicOn(): boolean,
    speak(text?: string): void,    // 中文配音；默认台词见 §3.9
    setVolume(v: number): void
  };
}
```

### 2.4 `src/main.js`

入口，无导出。职责：渲染器/后期/相机/光照/雾/主循环/键盘与按钮交互/HUD/开场页/粒子/昼夜。
必须暴露调试钩子：

```js
window.__pel = {
  state,               // 见 §3.10 的字段（至少含 speed, night, cam, lap, distance, bells）
  setCam(i),           // 1..4 切换镜头
  doBell(),            // 等价按空格
  toggleNight(),       // 切昼夜（把 state.night 与目标值都改）
  ready: true          // 场景初始化完成
};
```

## 3. 技术规格

### 3.1 渲染器与后期
| 项 | 设置 |
|---|---|
| 渲染器 | `antialias:true`，`setPixelRatio(Math.min(devicePixelRatio,2))`，ACESFilmicToneMapping，exposure 1.0，outputColorSpace 默认 sRGB |
| 阴影 | `PCFSoftShadowMap`；平行光 `castShadow`，shadow.mapSize 2048²，正交相机 ±40，bias −0.0004；**每帧让阴影相机跟随主角**（`light.target.position.copy(heroPos); light.position.copy(heroPos).add(new THREE.Vector3(40,60,25))`） |
| 灯光 | HemisphereLight(`#cfe8ff`,`#e8d3a8`,0.9)；DirectionalLight(`#fff1d6`,2.6) |
| 后期 | EffectComposer: RenderPass → UnrealBloomPass(strength 0.35, radius 0.6, threshold 0.85) → OutputPass；夜间 strength = 0.25+0.7*night |
| 雾 | `new THREE.Fog(0xbfe3ff, 60, 260)` |
| 相机 | PerspectiveCamera(50, aspect, 0.1, 1200)；OrbitControls(damping, maxPolarAngle 0.49π, minDistance 3, maxDistance 90) |

### 3.2 天空（ShaderMaterial，球半径 600，`BackSide`，`depthWrite:false`，`fog:false`）
- 颜色：`mix(bottomColor, topColor, pow(clamp(vDir.y*1.6+0.1,0,1), 0.8))`
  日间 top `#3d8fe0` / bottom `#d9f0ff`；夜间 top `#020818` / bottom `#1b2a55`。
- 太阳：`sunColor * (pow(s,600)*4.0 + pow(s,12)*0.25)`，`s = max(dot(viewDir, sunDir),0)`。
- 星空：1500 个 Points，半径 500，只在上半球，`opacity = night`。

### 3.3 海洋（ShaderMaterial）
- PlaneGeometry(1200,1200,220,220)，旋转到水平，`position.y = -0.6`。
- 顶点高度：`h = sin(x*0.15+t*1.2)*0.35 + sin(z*0.21+t*0.9)*0.3 + sin((x+z)*0.6+t*2.0)*0.08`；
  以岛中心（半径 30–60）为界做衰减：近岸平静、远海起浪。
- 片元：`dFdx/dFdy` 求法线；深浅色按距离混合（日间 `#0b5c8c` / `#35c4d6`，夜间对应压暗并偏蓝）；
  菲涅尔 `pow(1.0 - dot(N,V), 3.0)` 混入天空色；`h > 0.42` 出白色浪尖；
  半径 ≈30 处用 `sin(angle*14 + t*2)` 扰动生成岸边白色浪花环。
- **必须包含 three 的 fog / tonemapping / colorspace chunk**（`#include <fog_pars_fragment>` 等）。
- 更新 uniform 时改 `oceanMaterial.uniforms.xxx.value`（不要用 UniformsUtils.merge 后再手工换对象）。

### 3.4 岛屿与道具
- 沙岛：CylinderGeometry(30, 34, 2) at y=−0.98，`#f1d9a2`。
- 草地：CircleGeometry(24, 64)，顶点半径按 `1 + sin(5a)*0.06 + sin(11a)*0.03` 扰动，`#7cc95b`，y≈0.03。
- 木栈道：见 §2.1；用 Canvas 画木板条纹 + 白中线，`anisotropy = 8`，`repeat` 沿曲线方向。
- 椰子树 11 棵：二次贝塞尔弯曲树干（TubeGeometry 半径 0.22），7 片弯曲叶片
  （平面顶点 `z = -u*u*1.4`，宽度按 `sin(u*PI)` 收窄），3 个椰子；树冠随风摇摆 ±0.05 rad。
- 灯塔（在海边礁石上）：5 段红白交替圆台 + 灯室 + 发光灯球 + 红色锥顶；
  夜间：加法混合锥形光束（长 40）以 0.8 rad/s 旋转 + PointLight 强度 60。
- 大屏幕：6×3.6 圆角边框 + 视频面片，放在岛上靠海一侧，略仰角。
- 画架：金色画框 + 海报贴图（`MeshStandardMaterial`）+ 三根木支架。
- 云 14 组（每组 4–7 个 Icosahedron，微自发光，缓慢漂移）。
- 海鸥 9 只（两段翅膀正弦扇动，绕岛不同半径盘旋）。
- 帆船 3 艘（随正弦浪起伏、绕岛航行）。
- 沙滩伞 4 把 + 2 个沙滩球。

### 3.5 自行车（局部坐标）
- 车轮半径 0.36；后轮 x=−0.58、前轮 x=0.60；黑色轮胎圆环 + 镀铬轮圈 + 16 根交错辐条 + 花鼓。
- 关键点：五通 (−0.02,0.33)、座管顶 (−0.2,0.86)、车头上 (0.42,0.88)、车头下 (0.47,0.72)。
- 车架红 `#e63946`（metalness 0.5, roughness 0.3）；前叉/车把镀铬（metalness 1.0, roughness 0.2）；车把宽 0.56、高 1.0。
- 曲柄半径 0.16，两侧相差 180°，转速 = 车轮转速 × 0.55；链条用压扁的 TorusGeometry。
- 车铃：金色半球；车灯：夜间自发光 + SpotLight(距离 18, 角度 0.5)。
- 前筐：藤色开口圆柱；筐里一条金属质感小鱼（`sin(t*3)` 左右摆动）。
- 通用工具：`rod(a, b, r)` —— 两点间圆柱，用四元数把 Y 轴旋到 `b-a` 方向。

### 3.6 鹈鹕
- 躯干：单位球缩放 (0.42,0.30,0.28)，前倾 −0.35 rad，中心 (−0.12,1.17)；胸口奶油色；3 根锥形尾羽。
- 脖子：CatmullRom S 形 (0.22,1.32)→(0.2,1.52)→(0.32,1.65)→(0.3,1.82)，TubeGeometry 半径 0.075。
- 头：球 r=0.13 + 黄色冠羽 + 白眼球 + 黑瞳孔 + 蓝色骑行帽（半球 + 帽檐 + 白色顶球）。
- 喙：上喙压扁圆锥（长 0.62）+ 橙红钩尖；下喙 + 半透明橙色喉囊（缩放 0.28×0.09×0.06）。
- 翅膀：每侧肩→肘→握把三段椭球（翼尖深灰），握把固定在车把上。
- 腿：大腿/小腿各 0.43，橙色；**二骨骼 IK**：髋 (`-0.1, 1.0, ±0.13`) 到踏板距离 d（上限 L1+L2）；
  膝角 = `atan2(dy,dx) + acos((L1*L1 + d*d - L2*L2) / (2*L1*d))`；大腿外包白色羽毛椭球；三角蹼足跟随踏板。
- 材质：白羽 roughness 0.75，喙 roughness 0.45。

### 3.7 动画规则（每帧，`dt = min(dt, 0.05)`）
- 速度向目标速度平滑：`speed += (target - speed) * min(1, dt*2)`；沿赛道前进 `u += speed*dt / TRACK_LENGTH`。
- 朝向取 `trackSample(u).tangent`；侧倾 `lean = clamp(-curvature * 8 * speed, ±0.45)` 平滑跟随。
- 翘头 J：持续约 1.4 秒，抬起 0.45 rad 再落回。
- 身体起伏 `sin(pedalAngle*2)*0.012`；头部张望 `sin(0.7t)*0.35 + sin(2.3t)*0.05`。
- 喉囊 y 缩放随速度轻微晃动；按铃时张嘴（上喙 +0.4、下喙 −0.6 弧度比例）并扇翅。
- `speed > 8` 时翅膀持续扇动。
- 粒子：600 个 Points（径向渐变圆形 Canvas 贴图，顶点色）。
  后轮扬尘概率 `speed/10`；按铃爆出 120 片 HSL 随机彩纸，受重力下落。
  **颜色属性每帧必须 `needsUpdate = true`**（否则彩纸显示成黑方块）。
- 昼夜：`night` 以 `dt*0.8` 平滑趋向目标；天空/雾/海洋颜色在日夜两套色板间插值
  （雾夜 `#0e1838`）；太阳强度 2.6→0.25，半球光 0.9→0.25。

### 3.8 镜头
| # | 名称 | 做法 |
|---|---|---|
| 1 | 自由环绕 | OrbitControls；target 平滑跟车，相机随车平移；切回时相机放到车 +(9,6,9) |
| 2 | 追尾跟拍 | 车后 4.2、高 2.2，lookAt 车前 2m |
| 3 | 电影侧拍 | 侧方 5±1.5 缓慢漂移，高 1.4 |
| 4 | 鹈鹕视角 | 头顶上方 0.36、略后 0.05，看向前方 8m（**不要贴太近，会穿模进帽子**） |

### 3.9 音频（无 AI 音乐/配音文件，全部运行时合成/合成语音）
- **BGM**：WebAudio 程序化循环。C–G–Am–F 进行，110 BPM，尤克里里式拨弦（三角波 + 快速衰减包络）
  + 口哨旋律（正弦 + 4Hz 颤音）+ 轻打击（噪声短脉冲）；循环播放，默认音量 0.45。
- **车铃**：两声（2093Hz、2637Hz，间隔 0.12s），每声叠 1×、2.76×、5.4× 正弦泛音，指数衰减。
- **鹈鹕叫**：锯齿波 420→260Hz，带通 900Hz Q3，0.4 秒。
- **中文配音**：优先 `speechSynthesis`（`lang='zh-CN'`），台词：
  「嗨！我是佩利，一只热爱骑车的鹈鹕。系好安全带……哦不，是戴好头盔！拖动屏幕转转视角，按空格键我就按铃，我们出发吧！」
  若浏览器无中文语音，则退化为播放合成的鹈鹕叫声 + 屏幕 toast 显示台词（保证不静默失败）。
- 所有音频必须在「出发」按钮的点击回调里 `resume()` AudioContext 并启动音乐/视频/配音（自动播放策略）。

### 3.10 主循环状态
```js
state = {
  speed: 0, targetSpeed: 0,      // 0..12 m/s，↑/↓ 或 W/S 每按一次 ±1.5
  u: 0, distance: 0, lap: 0,
  cam: 1, night: 0, nightTarget: 0,
  bells: 0, wheelieT: -1, musicOn: true, ready: false
}
```

## 4. UI 与交互

- **开场页**：海报背景（`assets/poster.jpg` 的 data URL）+ 标题「鹈鹕 Pelly 骑单车」+「出发！」按钮。
  点击后淡出开始页，解锁音频，0.6 秒后播报配音。
- **键盘**：空格=按铃；↑/↓ 或 W/S=加减速；J=翘头；C 或 1–4=切换镜头；N=昼夜；M=音乐开关；V=重播语音。
- **屏幕底部按钮**（同等功能，移动端可用）：按铃 / 加速 / 减速 / 翘头 / 镜头 / 昼夜 / 音乐 / 语音。
  - 底部按钮容器：`position:fixed; left:0; right:0; margin:auto; width:fit-content`（避免换行错位）。
  - 触控目标 ≥44×44px。
- **HUD**（左上）：速度 km/h、里程 m、圈数、按铃次数、当前镜头名。
- **中央 toast** 提示（如「已进入夜间」「音乐已关闭」），2 秒淡出。
- 毛玻璃风格：`backdrop-filter: blur(12px)` + 半透明白底 + 圆角 + 细边框；键盘焦点可见（`:focus-visible` 描边）。
- 移动端（≤700px）：隐藏快捷键说明面板与标题，按钮改为小图标+文字。
- 无障碍：按钮带 `aria-label`，`prefers-reduced-motion` 时降低粒子数量。

## 5. 组装脚本 `tools/build_embed.py`

输入：`dist/bundle.js`、`assets/poster.jpg`、`assets/clip.mp4`、`tools/template.html`
输出：`dist/pelican-bicycle-3d.html`

1. 读三份素材 → base64。海报产出 `data:image/jpeg;base64,` 前缀的字符串；视频产出纯 base64。
2. 生成脚本片段：`window.ASSETS = { poster: "data:image/jpeg;base64,...", clip: "<base64>" };`
3. 读 bundle.js → 把出现的 `</script` 全部替换为 `<\/script`。
4. 替换 template.html 里的占位符：`/*__ASSETS__*/`、`/*__BUNDLE__*/`。
5. 写出 HTML，并打印最终体积与各部分贡献。

**CSP 兼容硬要求**：模板内不得出现指向外部域名的 URL；媒体一律走 blob URL。

## 6. QA 脚本 `qa/shoot.py`

- Playwright Chromium，启动参数：
  `--use-gl=angle --use-angle=swiftshader --enable-unsafe-swiftshader --autoplay-policy=no-user-gesture-required`
- 打开 `dist/pelican-bicycle-3d.html`（`file://`），收集 `console` 的 error 与 `pageerror`。
- 等待 `window.__pel && window.__pel.ready`，然后按顺序截图（1280×720）：
  `01_intro.png`（开场页，未点击）、`02_start.png`（点击「出发」后 3 秒）、
  `03_cam2.png`、`04_cam3.png`、`05_cam4.png`（用 `__pel.setCam(i)`，各等 1.5 秒）、
  `06_bell.png`（调 `__pel.doBell()` 后 0.35 秒）、`07_night.png`（直接设 `state.night=0.99` 并等 2 秒）、
  `08_mobile.png`（视口 390×800）。
- 软件渲染帧率极低，等待用 `page.wait_for_timeout`，不要依赖动画时长推算。
- 输出 `qa/report.md`：截图清单 + 每张的目视结论 + 控制台 issue 列表（有无 error/pageerror）。

## 7. 已知坑与修复

| 症状 | 修复 |
|---|---|
| 彩纸显示成黑方块 | 颜色缓冲未设 `needsUpdate` → 每帧设置 |
| 鹈鹕视角镜头卡进帽子 | 相机上移到头顶 0.36 |
| 底部按钮换行错位 | 容器 `left:0;right:0;margin:auto;width:fit-content` |
| 公开页 CSP 拦截 `data:` 媒体 | 改用 blob URL |
| 软件渲染下模拟时间偏慢导致夜间截图失败 | QA 直接设置 `state.night=0.99` |
| `UniformsUtils.merge` 克隆 uniforms 后更新无效 | 直接改材质上的 uniforms 对象 |
| esbuild 打包报 `</script` 断裂 | 组装时转义为 `<\/script` |

## 8. 验收标准

1. `dist/pelican-bicycle-3d.html` 体积 ≤4MB，双击可在浏览器打开、无外部请求、无控制台 error。
2. 开场页海报与标题正常，「出发」后进入 3D 场景，镜头 1–4 均可切换且画面正常（主角可见、不穿模）。
3. 按铃有铃声 + 彩纸；加减速影响速度；J 翘头；N 昼夜平滑切换（夜间有星空、灯塔光束、车灯）。
4. 白天与夜间、桌面与 390×800 移动端截图均正常渲染。
5. 视频在大屏幕上循环播放（blob URL，无 CSP 报错）。
