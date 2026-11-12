// src/hero.js —— 主角模块：自行车 + 鹈鹕 + 二骨骼 IK + 姿态动画
// 依据 SPEC.md：§2.2 接口契约 / §3.5 自行车 / §3.6 鹈鹕 / §3.7 动画规则
// 局部坐标约定：x 向前、y 向上、z 侧向，原点在两轮接地处（SPEC §2.2）
import * as THREE from 'three';

/* ==================== 共享几何与通用工具（构建期创建，update 内只复用） ==================== */
// 单位圆柱（半径 1、高 1、沿 +Y）与单位球：全文件只创建一次，
// 每个部件通过 position/quaternion/scale 摆放，update 内绝不新建几何体或材质。
const GEO_ROD = new THREE.CylinderGeometry(1, 1, 1, 8, 1, false);
const GEO_BALL = new THREE.SphereGeometry(1, 16, 12);

const Y_AXIS = new THREE.Vector3(0, 1, 0);
const ZERO = new THREE.Vector3(0, 0, 0);
const _dir = new THREE.Vector3();
const _p1 = new THREE.Vector3();

const V = (x, y, z) => new THREE.Vector3(x, y, z);

/**
 * rod(a, b, r) —— 两点间圆柱：用四元数把 Y 轴旋到 a→b 方向，半径 r、长度 |b-a|。
 * 全文件复用（车架/前叉/辐条/链条/腿骨等）。
 */
function rod(a, b, r, mat) {
  return placeRod(new THREE.Mesh(GEO_ROD, mat), a, b, r);
}

/** 复用已有圆柱网格摆放（update 内每帧调用腿骨等，不产生新几何/材质）。 */
function placeRod(mesh, a, b, r) {
  _dir.subVectors(b, a);
  const len = _dir.length() || 1e-6;
  mesh.position.copy(a).addScaledVector(_dir, 0.5);
  _dir.divideScalar(len);
  mesh.quaternion.setFromUnitVectors(Y_AXIS, _dir); // Y 轴 → a→b 方向
  mesh.scale.set(r, len, r);
  return mesh;
}

/** 椭球：单位球沿 a→b 拉伸，横截面半径 r、轴向半长 |b-a|/2。 */
function ellipsoid(a, b, r, mat) {
  return placeEllipsoid(new THREE.Mesh(GEO_BALL, mat), a, b, r);
}

/** 复用已有球网格摆放（update 内每帧调用大腿羽毛等）。 */
function placeEllipsoid(mesh, a, b, r) {
  _dir.subVectors(b, a);
  const len = _dir.length() || 1e-6;
  mesh.position.copy(a).addScaledVector(_dir, 0.5);
  _dir.divideScalar(len);
  mesh.quaternion.setFromUnitVectors(Y_AXIS, _dir);
  mesh.scale.set(r, len * 0.5, r);
  return mesh;
}

/** 轴向对齐的椭球（构建期使用，朝向固定，不做四元数对齐）。 */
function ball(p, sx, sy, sz, mat) {
  const m = new THREE.Mesh(GEO_BALL, mat);
  m.position.copy(p);
  m.scale.set(sx, sy, sz);
  return m;
}

/**
 * 构建主角：鹈鹕 + 自行车。
 * @param {THREE.Scene} scene
 * @returns {{ root: THREE.Group, update: (dt:number, s:object) => void }}
 */
export function buildHero(scene) {
  /* ------------------ 材质（构建期创建一次） ------------------ */
  const M = {
    tyre: new THREE.MeshStandardMaterial({ color: 0x1b1b1f, roughness: 0.9, metalness: 0.05 }),
    spoke: new THREE.MeshStandardMaterial({ color: 0xcfd6dd, metalness: 0.9, roughness: 0.25 }),
    // §3.5 前叉/车把镀铬：metalness 1.0, roughness 0.2
    chrome: new THREE.MeshStandardMaterial({ color: 0xd8dde3, metalness: 1.0, roughness: 0.2 }),
    // §3.5 车架红 #e63946：metalness 0.5, roughness 0.3
    frame: new THREE.MeshStandardMaterial({ color: 0xe63946, metalness: 0.5, roughness: 0.3 }),
    gold: new THREE.MeshStandardMaterial({ color: 0xf2c14e, metalness: 0.9, roughness: 0.25 }), // 车铃（金色半球）
    dark: new THREE.MeshStandardMaterial({ color: 0x2b2f36, metalness: 0.5, roughness: 0.6 }),
    wicker: new THREE.MeshStandardMaterial({ color: 0xc79a5b, roughness: 0.9, side: THREE.DoubleSide }), // 藤色前筐
    fish: new THREE.MeshStandardMaterial({ color: 0xc9d6e0, metalness: 0.95, roughness: 0.15 }), // 金属质感小鱼
    // §3.6 白羽 roughness 0.75
    feather: new THREE.MeshStandardMaterial({ color: 0xfaf7f0, roughness: 0.75 }),
    featherOff: new THREE.MeshStandardMaterial({ color: 0xe9e4d8, roughness: 0.8 }),
    cream: new THREE.MeshStandardMaterial({ color: 0xf3e3bd, roughness: 0.85 }), // 胸口奶油色
    wingTip: new THREE.MeshStandardMaterial({ color: 0x8a8f96, roughness: 0.8 }), // 翼尖深灰
    // §3.6 喙 roughness 0.45
    beak: new THREE.MeshStandardMaterial({ color: 0xf6a623, roughness: 0.45 }),
    beakTip: new THREE.MeshStandardMaterial({ color: 0xe8572a, roughness: 0.45 }), // 橙红钩尖
    pouch: new THREE.MeshStandardMaterial({
      color: 0xff9a3c, roughness: 0.5, transparent: true, opacity: 0.6,
      depthWrite: false, side: THREE.DoubleSide
    }), // 半透明橙色喉囊
    leg: new THREE.MeshStandardMaterial({ color: 0xf59f2b, roughness: 0.6 }), // 橙色腿/蹼足
    cap: new THREE.MeshStandardMaterial({ color: 0x2f6fd8, roughness: 0.6 }), // 蓝色骑行帽
    white: new THREE.MeshStandardMaterial({ color: 0xffffff, roughness: 0.5 }),
    eyeWhite: new THREE.MeshStandardMaterial({ color: 0xffffff, roughness: 0.3 }),
    pupil: new THREE.MeshStandardMaterial({ color: 0x14161a, roughness: 0.4 }),
    crest: new THREE.MeshStandardMaterial({ color: 0xf7d94a, roughness: 0.6 }), // 黄色冠羽
    lens: new THREE.MeshStandardMaterial({ color: 0xfff3c4, emissive: 0xffd070, emissiveIntensity: 0, roughness: 0.3 })
  };

  /* ------------------ 层级：root → leanGroup → wheelieGroup(枢轴后轮接地) → rig ------------------ */
  const root = new THREE.Group();
  root.name = 'pelican-hero';

  const leanGroup = new THREE.Group();      // 整车侧倾（§2.2 s.lean）
  const wheelieGroup = new THREE.Group();   // 翘头（§2.2 s.wheelie），枢轴放在后轮接地点
  wheelieGroup.position.set(-0.58, 0, 0);   // §3.5 后轮 x=-0.58 → 以该轮接地点为旋转中心
  const rig = new THREE.Group();            // 抵消枢轴平移：内部一律使用 SPEC 给出的原值坐标
  rig.position.set(0.58, 0, 0);

  root.add(leanGroup);
  leanGroup.add(wheelieGroup);
  wheelieGroup.add(rig);

  /* ==================== §3.5 自行车 ==================== */
  const R_WHEEL = 0.36;                     // §3.5 车轮半径 0.36
  const X_REAR = -0.58, X_FRONT = 0.60;     // §3.5 后轮 x=-0.58、前轮 x=0.60
  const Y_AXLE = R_WHEEL;                   // 轮轴高度 = 半径（接地处 y=0）
  const P_BB = V(-0.02, 0.33, 0);           // §3.5 五通
  const P_SEAT = V(-0.2, 0.86, 0);          // §3.5 座管顶
  const P_HEAD_TOP = V(0.42, 0.88, 0);      // §3.5 车头上
  const P_HEAD_BOT = V(0.47, 0.72, 0);      // §3.5 车头下
  const CRANK_R = 0.16;                     // §3.5 曲柄半径 0.16
  const PEDAL_Z = 0.13;                     // 踏板侧向偏移（取与髋相同的 |z|，使腿与髋严格共面）
  const BAR_Y = 1.0, BAR_HALF = 0.28;       // §3.5 车把宽 0.56（z ±0.28）、高 1.0

  /* ---- 车轮：黑色轮胎圆环 + 镀铬轮圈 + 16 根交错辐条 + 花鼓 ---- */
  function makeWheel() {
    const g = new THREE.Group();
    // 轮胎：外半径 0.36 → 圆环半径 0.325、管半径 0.035
    g.add(new THREE.Mesh(new THREE.TorusGeometry(R_WHEEL - 0.035, 0.035, 8, 32), M.tyre));
    // 镀铬轮圈
    g.add(new THREE.Mesh(new THREE.TorusGeometry(0.272, 0.018, 6, 28), M.chrome));
    // 花鼓（轴向 z）
    const hub = new THREE.Mesh(new THREE.CylinderGeometry(0.035, 0.035, 0.10, 12), M.chrome);
    hub.rotation.x = Math.PI / 2;
    g.add(hub);
    // 16 根交错辐条：花鼓端切向错开 ±0.16 rad、法兰 z 交替 ±0.026 → 交叉编法
    for (let i = 0; i < 16; i++) {
      const a = (i / 16) * Math.PI * 2;
      const even = i % 2 === 0;
      const ha = a + (even ? -0.16 : 0.16);
      const hz = even ? 0.026 : -0.026;
      const p1 = V(Math.cos(ha) * 0.03, Math.sin(ha) * 0.03, hz);
      const p2 = V(Math.cos(a) * 0.272, Math.sin(a) * 0.272, 0);
      g.add(rod(p1, p2, 0.0055, M.spoke));
    }
    return g;
  }

  const wheelRear = makeWheel();
  wheelRear.position.set(X_REAR, Y_AXLE, 0);
  rig.add(wheelRear);

  const wheelFront = makeWheel();
  wheelFront.position.set(X_FRONT, Y_AXLE, 0);
  rig.add(wheelFront);

  /* ---- 车架（红）：座管/下管/上管/头管 + 后三角 ---- */
  const rearAxle = V(X_REAR, Y_AXLE, 0);
  const frontAxle = V(X_FRONT, Y_AXLE, 0);

  rig.add(rod(P_BB, P_SEAT, 0.030, M.frame));                 // 座管
  rig.add(rod(P_BB, P_HEAD_BOT, 0.028, M.frame));             // 下管
  rig.add(rod(P_SEAT, P_HEAD_TOP, 0.026, M.frame));           // 上管
  rig.add(rod(P_HEAD_TOP, P_HEAD_BOT, 0.034, M.frame));       // 头管
  for (const s of [1, -1]) {
    // 后下叉
    rig.add(rod(V(P_BB.x, P_BB.y, s * 0.045), V(rearAxle.x, rearAxle.y, s * 0.045), 0.022, M.frame));
    // 后上叉
    rig.add(rod(V(P_SEAT.x, P_SEAT.y, s * 0.045), V(rearAxle.x, rearAxle.y, s * 0.045), 0.020, M.frame));
  }
  // 五通外壳
  const bbShell = new THREE.Mesh(new THREE.CylinderGeometry(0.05, 0.05, 0.13, 14), M.chrome);
  bbShell.rotation.x = Math.PI / 2;
  bbShell.position.copy(P_BB);
  rig.add(bbShell);

  /* ---- 前叉（镀铬）+ 立管/车把（镀铬）+ 握把 ---- */
  for (const s of [1, -1]) {
    rig.add(rod(V(P_HEAD_BOT.x, P_HEAD_BOT.y, s * 0.035), V(frontAxle.x, frontAxle.y, s * 0.035), 0.022, M.chrome));
  }
  rig.add(rod(P_HEAD_TOP, V(0.40, BAR_Y, 0), 0.020, M.chrome)); // 立管
  const bar = new THREE.Mesh(new THREE.CylinderGeometry(0.022, 0.022, BAR_HALF * 2, 10), M.chrome);
  bar.rotation.x = Math.PI / 2;                                  // 圆柱轴向 → z（车把横置）
  bar.position.set(0.40, BAR_Y, 0);
  rig.add(bar);
  for (const s of [1, -1]) {
    const grip = new THREE.Mesh(new THREE.CylinderGeometry(0.029, 0.029, 0.10, 10), M.dark);
    grip.rotation.x = Math.PI / 2;
    grip.position.set(0.40, BAR_Y, s * 0.24);
    rig.add(grip);
  }

  /* ---- 座垫 ---- */
  rig.add(rod(V(P_SEAT.x, P_SEAT.y, 0), V(-0.21, 0.90, 0), 0.018, M.chrome)); // 座管
  const saddle = ball(V(-0.21, 0.925, 0), 0.135, 0.032, 0.078, M.dark);
  rig.add(saddle);

  /* ---- 曲柄组（含牙盘/踏板）：整体绕 z 旋转 pedalAngle ---- */
  const crankGroup = new THREE.Group();
  crankGroup.position.copy(P_BB);
  rig.add(crankGroup);

  // 牙盘：压扁的 TorusGeometry（§3.5 链条/盘片用压扁圆环）
  const chainring = new THREE.Mesh(new THREE.TorusGeometry(0.085, 0.014, 6, 20), M.spoke);
  chainring.position.set(0, 0, 0.06);
  chainring.scale.set(1, 1, 0.45);            // z 向压扁
  crankGroup.add(chainring);
  // 右侧曲柄臂（+x 方向）、左侧曲柄臂（−x 方向，相差 180°）
  crankGroup.add(rod(V(0, 0, 0.075), V(CRANK_R, 0, 0.075), 0.022, M.chrome));
  crankGroup.add(rod(V(0, 0, -0.075), V(-CRANK_R, 0, -0.075), 0.022, M.chrome));

  // 踏板：反向自转保持水平（左脚相差 180°）
  function makePedal() {
    const g = new THREE.Group();
    const plate = new THREE.Mesh(new THREE.BoxGeometry(0.11, 0.022, 0.075), M.dark);
    g.add(plate);
    const spindle = new THREE.Mesh(new THREE.CylinderGeometry(0.015, 0.015, 0.09, 8), M.chrome);
    spindle.rotation.x = Math.PI / 2;
    spindle.position.z = -0.02;
    g.add(spindle);
    return g;
  }
  const pedalR = makePedal();
  pedalR.position.set(CRANK_R, 0, PEDAL_Z);
  crankGroup.add(pedalR);
  const pedalL = makePedal();
  pedalL.position.set(-CRANK_R, 0, -PEDAL_Z);
  crankGroup.add(pedalL);

  /* ---- 链条：后飞轮压扁圆环 + 上下两段链节 ---- */
  const cog = new THREE.Mesh(new THREE.TorusGeometry(0.05, 0.012, 6, 16), M.spoke);
  cog.position.set(0, 0, 0.06);
  cog.scale.set(1, 1, 0.45);
  wheelRear.add(cog);                          // 随后轮一起转
  rig.add(rod(V(P_BB.x, P_BB.y + 0.085, 0.06), V(rearAxle.x, rearAxle.y + 0.05, 0.06), 0.012, M.dark)); // 上链
  rig.add(rod(V(P_BB.x, P_BB.y - 0.085, 0.06), V(rearAxle.x, rearAxle.y - 0.05, 0.06), 0.012, M.dark)); // 下链

  /* ---- 车铃：金色半球（装在车把右侧） ---- */
  const bell = new THREE.Mesh(new THREE.SphereGeometry(0.055, 16, 8, 0, Math.PI * 2, 0, Math.PI / 2), M.gold);
  bell.position.set(0.40, BAR_Y + 0.022, 0.16);
  rig.add(bell);
  const bellLever = new THREE.Mesh(new THREE.CylinderGeometry(0.008, 0.008, 0.06, 6), M.chrome);
  bellLever.rotation.x = Math.PI / 2;
  bellLever.position.set(0.40, BAR_Y + 0.01, 0.10);
  rig.add(bellLever);

  /* ---- 车灯：灯罩 + 镜片（自发光）+ SpotLight(距离 18, 角度 0.5) ---- */
  const LAMP = V(0.50, 0.80, 0);
  const lampBody = new THREE.Mesh(new THREE.CylinderGeometry(0.045, 0.045, 0.07, 12), M.dark);
  lampBody.rotation.z = Math.PI / 2;           // 轴向 → x（朝前）
  lampBody.position.copy(LAMP);
  rig.add(lampBody);
  const lampLens = new THREE.Mesh(new THREE.CylinderGeometry(0.042, 0.042, 0.014, 12), M.lens);
  lampLens.rotation.z = Math.PI / 2;
  lampLens.position.set(LAMP.x + 0.038, LAMP.y, LAMP.z);
  rig.add(lampLens);

  // §3.5 车灯 SpotLight(距离 18, 角度 0.5)
  const spotLight = new THREE.SpotLight(0xffeec4, 0, 18, 0.5, 0.45, 1.0);
  spotLight.position.copy(LAMP);
  spotLight.visible = false;
  rig.add(spotLight);
  const spotTarget = new THREE.Object3D();
  spotTarget.position.set(9, 0.6, 0);         // 目标点必须在场景图中才会更新矩阵
  rig.add(spotTarget);
  spotLight.target = spotTarget;

  /* ---- 前筐：藤色开口圆柱 + 筐内会扭动的小鱼 ---- */
  const BASKET = V(0.60, 0.92, 0);
  const basket = new THREE.Mesh(new THREE.CylinderGeometry(0.20, 0.155, 0.22, 16, 1, true), M.wicker);
  basket.position.copy(BASKET);
  rig.add(basket);
  const basketBottom = new THREE.Mesh(new THREE.CircleGeometry(0.155, 16), M.wicker);
  basketBottom.rotation.x = -Math.PI / 2;
  basketBottom.position.set(BASKET.x, BASKET.y - 0.11, BASKET.z);
  rig.add(basketBottom);
  const basketRim = new THREE.Mesh(new THREE.TorusGeometry(0.20, 0.012, 6, 16), M.wicker);
  basketRim.rotation.x = Math.PI / 2;
  basketRim.position.set(BASKET.x, BASKET.y + 0.11, BASKET.z);
  rig.add(basketRim);
  for (const s of [1, -1]) {
    // 筐支架：从车把接到筐底
    rig.add(rod(V(0.40, BAR_Y - 0.05, s * 0.10), V(BASKET.x - 0.06, BASKET.y - 0.10, s * 0.07), 0.012, M.dark));
  }
  // 筐里的小鱼：金属质感，sin(t*3) 左右扭动
  const fish = new THREE.Group();
  fish.position.set(BASKET.x, BASKET.y + 0.16, BASKET.z);  // 探头出筐沿，摆动可见
  fish.add(ball(V(0, 0, 0), 0.085, 0.038, 0.028, M.fish));
  const fishTail = new THREE.Mesh(new THREE.ConeGeometry(0.038, 0.07, 5), M.fish);
  fishTail.rotation.z = Math.PI / 2;           // 尾鳍朝后（−x）
  fishTail.position.set(-0.105, 0, 0);
  fishTail.scale.set(1, 1, 0.45);
  fish.add(fishTail);
  fish.add(ball(V(0.055, 0.014, 0.024), 0.009, 0.009, 0.009, M.pupil));
  fish.add(ball(V(0.055, 0.014, -0.024), 0.009, 0.009, 0.009, M.pupil));
  rig.add(fish);

  /* ==================== §3.6 鹈鹕 ==================== */
  const birdGroup = new THREE.Group();         // 身体起伏（§3.7 sin(pedalAngle*2)*0.012）挂这一层
  rig.add(birdGroup);

  // 躯干：单位球缩放 (0.42,0.30,0.28)，前倾 −0.35 rad，中心 (−0.12,1.17)
  const body = ball(V(-0.12, 1.17, 0), 0.42, 0.30, 0.28, M.feather);
  body.rotation.z = -0.35;
  birdGroup.add(body);
  // 胸口奶油色
  birdGroup.add(ball(V(0.10, 1.08, 0), 0.22, 0.20, 0.21, M.cream));
  // 3 根锥形尾羽（向后下方）
  for (let i = -1; i <= 1; i++) {
    const f = new THREE.Mesh(new THREE.ConeGeometry(0.05, 0.34, 6), M.featherOff);
    f.rotation.z = Math.PI / 2 + 0.2;          // 锥尖 → (−sinθ, cosθ) ≈ 后下方
    f.position.set(-0.46, 1.08, i * 0.075);
    birdGroup.add(f);
  }

  // 脖子：CatmullRom S 形 TubeGeometry，半径 0.075
  const neckCurve = new THREE.CatmullRomCurve3([
    V(0.22, 1.32, 0), V(0.20, 1.52, 0), V(0.32, 1.65, 0), V(0.30, 1.82, 0)
  ]);
  const neck = new THREE.Mesh(new THREE.TubeGeometry(neckCurve, 24, 0.075, 10, false), M.feather);
  birdGroup.add(neck);

  // 头（含帽/眼/冠羽/喙），整组绕脖子顶端做张望
  const HEAD = V(0.30, 1.82, 0);
  const headGroup = new THREE.Group();
  headGroup.position.copy(HEAD);
  birdGroup.add(headGroup);
  headGroup.add(ball(V(0.02, 0.08, 0), 0.13, 0.13, 0.13, M.feather)); // 头球 r=0.13
  // 黄色冠羽
  for (let i = -1; i <= 1; i++) {
    const c = new THREE.Mesh(new THREE.ConeGeometry(0.022, 0.13, 5), M.crest);
    c.rotation.z = 1.0;                        // 锥尖指向上后方
    c.position.set(-0.04, 0.16, i * 0.05);
    headGroup.add(c);
  }
  // 白眼球 + 黑瞳孔
  for (const s of [1, -1]) {
    headGroup.add(ball(V(0.05, 0.10, s * 0.085), 0.032, 0.032, 0.032, M.eyeWhite));
    headGroup.add(ball(V(0.07, 0.10, s * 0.10), 0.016, 0.016, 0.016, M.pupil));
  }
  // 蓝色骑行帽：半球 + 帽檐 + 白色顶球
  const helmet = new THREE.Mesh(new THREE.SphereGeometry(0.145, 16, 10, 0, Math.PI * 2, 0, Math.PI * 0.55), M.cap);
  helmet.position.set(0.02, 0.06, 0);
  headGroup.add(helmet);
  const brim = new THREE.Mesh(new THREE.CylinderGeometry(0.15, 0.15, 0.018, 16, 1, false, 0, Math.PI), M.cap);
  brim.position.set(0.055, 0.048, 0);          // 半月板沿 +x 前伸
  brim.rotation.z = -0.08;
  headGroup.add(brim);
  headGroup.add(ball(V(0.02, 0.215, 0), 0.035, 0.035, 0.035, M.white)); // 白色顶球

  // 上喙：压扁圆锥（长 0.62）+ 橙红钩尖；枢轴在头前，按铃时 +0.4 弧度比例张开
  const upperBeakPivot = new THREE.Group();
  upperBeakPivot.position.set(0.12, 0.045, 0);
  headGroup.add(upperBeakPivot);
  const upperBeak = new THREE.Mesh(new THREE.ConeGeometry(0.075, 0.62, 12), M.beak);
  upperBeak.rotation.z = -Math.PI / 2;         // 锥尖指向 +x（前方）
  upperBeak.scale.set(0.55, 1, 1);             // 竖直压扁 → 扁平的上喙
  upperBeak.position.set(0.31, 0, 0);
  upperBeakPivot.add(upperBeak);
  upperBeakPivot.add(ball(V(0.60, 0, 0), 0.028, 0.026, 0.026, M.beakTip));
  const hook = new THREE.Mesh(new THREE.ConeGeometry(0.022, 0.06, 8), M.beakTip);
  hook.rotation.z = Math.PI;                   // 锥尖向下 → 钩尖
  hook.position.set(0.63, -0.03, 0);
  upperBeakPivot.add(hook);

  // 下喙 + 半透明橙色喉囊（缩放 0.28×0.09×0.06）；按铃时 −0.6 弧度比例张开
  const lowerBeakPivot = new THREE.Group();
  lowerBeakPivot.position.set(0.12, 0.025, 0);
  headGroup.add(lowerBeakPivot);
  const lowerBeak = new THREE.Mesh(new THREE.ConeGeometry(0.06, 0.56, 12), M.beak);
  lowerBeak.rotation.z = -Math.PI / 2;
  lowerBeak.scale.set(0.4, 1, 0.95);
  lowerBeak.position.set(0.29, 0, 0);
  lowerBeakPivot.add(lowerBeak);
  const pouch = ball(V(0.24, -0.045, 0), 0.28, 0.09, 0.06, M.pouch);
  lowerBeakPivot.add(pouch);

  /* ---- 双翅：肩→肘→握把三段椭球，握把固定在车把上，翼尖深灰 ---- */
  const S_UP = V(-0.02, 1.24, 0);              // 肩
  const E_UP = V(0.18, 1.13, 0);               // 肘
  const G_UP = V(0.40, 1.00, 0);               // 握把（= 车把端点 0.40 / 1.0）
  const SHOULDER_Z = 0.16, GRIP_Z = BAR_HALF;

  const wings = [1, -1].map((side) => {
    const group = new THREE.Group();
    group.position.set(S_UP.x, S_UP.y, side * SHOULDER_Z);
    birdGroup.add(group);
    // 肩部椭球（随身体，直接挂在躯干侧）
    group.add(ball(V(0, 0, 0), 0.115, 0.10, 0.10, M.feather));
    // 上臂椭球：肩 → 肘（局部常量，一次性摆好）
    const upper = ellipsoid(ZERO, V(E_UP.x - S_UP.x, E_UP.y - S_UP.y, 0), 0.075, M.feather);
    group.add(upper);
    // 前臂椭球：肘 → 握把（每帧按扇翅角重算，保证翼尖始终钉在车把上）
    const fore = ellipsoid(ZERO, V(0.1, 0, 0), 0.062, M.feather);
    group.add(fore);
    // 翼尖深灰
    const tip = ball(V(0, 0, 0), 0.05, 0.055, 0.05, M.wingTip);
    group.add(tip);
    return {
      side, group, fore, tip,
      e: V(E_UP.x - S_UP.x, E_UP.y - S_UP.y, 0),   // 肘（局部常量）
      g: V(0, 0, 0),                                // 握把（局部，每帧按 bob 重算）
      dz: side * (GRIP_Z - SHOULDER_Z)
    };
  });

  /* ---- 双腿：二骨骼 IK（髋 (−0.1,1.0,±0.13)，L1=L2=0.43） ---- */
  const L1 = 0.43, L2 = 0.43;                        // §3.6 大腿/小腿各 0.43
  const HIP_X = -0.1, HIP_Y = 1.0, HIP_Z = 0.13;     // §3.6 髋位置

  /** 三角蹼足：三趾扇形 + 蹼面；作为踏板 Group 的子节点 → 姿态自动跟随踏板 */
  function makeFoot() {
    const g = new THREE.Group();
    for (const i of [-1, 0, 1]) {
      const toe = new THREE.Mesh(new THREE.ConeGeometry(0.034, 0.17, 3), M.leg);
      toe.scale.set(0.42, 1, 1);                   // 竖直压扁（三棱锥 → 扁平脚趾）
      toe.rotation.y = i * 0.34;                   // 绕 y 扇形展开（three 的 XYZ 序 = Rx·Ry·Rz）
      toe.rotation.z = -Math.PI / 2;               // 锥尖指向 +x（前方）
      toe.position.set(0.075, 0.004, i * 0.035);
      g.add(toe);
    }
    const web = new THREE.Mesh(new THREE.CylinderGeometry(0.10, 0.10, 0.008, 3), M.leg);
    web.rotation.y = -Math.PI / 2;                 // 三角蹼面：尖端朝踝，宽边朝前
    g.add(web);
    return g;
  }
  // 蹼足挂在踏板 Group 上（踏板每帧反向自转保持水平 → 蹼足姿态随之保持水平）
  const footR = makeFoot();
  footR.position.set(0, 0.04, 0);
  pedalR.add(footR);
  const footL = makeFoot();
  footL.position.set(0, 0.04, 0);
  pedalL.add(footL);

  const legs = [1, -1].map((side) => {
    const hip = V(0, 0, 0), knee = V(0, 0, 0), ankle = V(0, 0, 0), pedal = V(0, 0, 0);
    const thigh = rod(hip, V(0, -1, 0), 0.045, M.leg);
    const shin = rod(V(0, -0.5, 0), V(0, -1, 0), 0.038, M.leg);
    const feather = ellipsoid(V(0, -0.1, 0), V(0, -0.2, 0), 0.085, M.feather); // 大腿外包白羽
    const kneeBall = ball(V(0, -0.4, 0), 0.05, 0.05, 0.05, M.leg);
    rig.add(thigh, shin, feather, kneeBall);
    return { side, thigh, shin, feather, kneeBall, hip, knee, ankle, pedal };
  });

  /* ---------------- 阴影 ---------------- */
  root.traverse((o) => {
    if (o.isMesh) o.castShadow = true;
  });
  if (scene && scene.add) scene.add(root);

  /* ==================== 每帧动画（§3.7） ==================== */
  let time = 0;         // 内部累积时间（供小鱼/喉囊/扇翅等周期性运动）
  let openAmt = 0;      // 张嘴量（平滑 0..1）
  let flapBase = 0;     // 扇翅强度（平滑 0..1）

  /** 双翅更新：肩部绕 z 转 phi（扇动），前臂重算并让翼尖始终停在车把握把处 */
  function updateWings(phi, bob) {
    const c = Math.cos(phi), s = Math.sin(phi);
    for (const w of wings) {
      w.group.rotation.z = phi;
      // 握把在“未旋转”的翅膀局部坐标里：世界（鸟体局部）y 受 bob 影响 → 反向补偿
      w.g.set(G_UP.x - S_UP.x, G_UP.y - bob - S_UP.y, w.dz);
      // 把固定的握把点反向旋转回父级局部坐标系：局部 = Rz(−phi)·g
      _p1.set(w.g.x * c + w.g.y * s, -w.g.x * s + w.g.y * c, w.g.z);
      placeEllipsoid(w.fore, w.e, _p1, 0.062);   // 肘 → 握把（长度自适应，仍钉在车把上）
      w.tip.position.copy(_p1);
      w.tip.quaternion.copy(w.fore.quaternion);
    }
  }

  /**
   * @param {number} dt
   * @param {object} s { pedalAngle, speed, lean, wheelie, bellT, headTurn, flap, frontLightOn }
   */
  function update(dt, s) {
    const d = Math.min(dt || 0, 0.05);           // §3.7 dt = min(dt, 0.05)
    const st = s || {};
    time += d;

    const pedalAngle = st.pedalAngle || 0;       // 角度用外部传入的累积量，直接使用
    const speed = st.speed || 0;

    /* --- 曲柄 / 车轮转动：曲柄转速 = 车轮转速 × 0.55 → 车轮转角 = 曲柄转角 / 0.55 --- */
    const wheelAngle = pedalAngle / 0.55;
    wheelRear.rotation.z = -wheelAngle;          // 前进时车轮上缘向前 → 取负角
    wheelFront.rotation.z = -wheelAngle;
    crankGroup.rotation.z = pedalAngle;
    pedalR.rotation.z = -pedalAngle;             // 踏板反向自转保持水平
    pedalL.rotation.z = -pedalAngle;

    /* --- 整车姿态：侧倾 / 翘头（绕后轮接地点抬前轮） --- */
    leanGroup.rotation.x = st.lean || 0;
    wheelieGroup.rotation.z = st.wheelie || 0;

    /* --- 身体起伏 sin(pedalAngle*2)*0.012 --- */
    const bob = Math.sin(pedalAngle * 2) * 0.012;
    birdGroup.position.y = bob;

    /* --- 头部张望（外部传入优先，否则用 §3.7 公式） --- */
    const headTurn = (typeof st.headTurn === 'number')
      ? st.headTurn
      : (Math.sin(time * 0.7) * 0.35 + Math.sin(time * 2.3) * 0.05);
    headGroup.rotation.y = headTurn;
    headGroup.rotation.z = Math.sin(time * 1.3) * 0.04;

    /* --- 喉囊 y 缩放随速度轻微晃动 --- */
    const wob = Math.min(1, speed / 6);
    pouch.scale.set(0.28, 0.09 * (1 + 0.20 * Math.sin(time * 6) * wob), 0.06);

    /* --- 按铃张嘴：上喙 +0.4 / 下喙 −0.6 弧度比例 --- */
    const bellT = (typeof st.bellT === 'number') ? st.bellT : -1;
    const openTarget = (bellT >= 0 && bellT < 1.0) ? 1 : 0;
    openAmt += (openTarget - openAmt) * Math.min(1, d * 12);
    upperBeakPivot.rotation.z = 0.4 * openAmt;
    lowerBeakPivot.rotation.z = -0.6 * openAmt;

    /* --- 扇翅：s.flap 为主，按铃 / speed>8 自动叠加（§3.7） --- */
    let flapTarget = THREE.MathUtils.clamp(st.flap || 0, 0, 1);
    if (bellT >= 0 && bellT < 1.6) flapTarget = Math.max(flapTarget, 1);
    if (speed > 8) flapTarget = Math.max(flapTarget, 0.65);
    flapBase += (flapTarget - flapBase) * Math.min(1, d * 6);
    const flapOsc = 0.5 + 0.5 * Math.sin(time * 9);
    updateWings(flapBase * flapOsc * 0.55, bob);  // 最大抬翼约 0.55 rad

    /* --- 车灯开关（自发光 + SpotLight） --- */
    const on = !!st.frontLightOn;
    spotLight.visible = on;
    spotLight.intensity = on ? 12 : 0;
    M.lens.emissiveIntensity = on ? 1.8 : 0;

    /* --- 筐内小鱼：sin(t*3) 左右扭动 --- */
    fish.rotation.y = Math.sin(time * 3) * 0.5;
    fish.rotation.z = -0.22 + Math.sin(time * 3 + 0.8) * 0.08;

    /* --- 双腿二骨骼 IK（在 x-y 矢状面内解算，蹼足落在踏板上方踝点） --- */
    for (const leg of legs) {
      const side = leg.side;
      // 髋随身体起伏一起动
      const hip = leg.hip.set(HIP_X, HIP_Y + bob, side * HIP_Z);
      // 踏板圆心：五通 + Rz(pedalAngle)·(CRANK_R,0,0)（左脚相差 180° 由组旋转自动得到）
      const ca = Math.cos(pedalAngle), sa = Math.sin(pedalAngle);
      const pedal = leg.pedal.set(
        P_BB.x + ca * CRANK_R,
        P_BB.y + sa * CRANK_R,
        side * PEDAL_Z
      );
      const ankle = leg.ankle.set(pedal.x, pedal.y + 0.04, pedal.z); // 踝点在踏板面上方

      const dx = ankle.x - hip.x, dy = ankle.y - hip.y;
      // d 上限 L1+L2（超出会令 acos 参数越界）
      let dist = Math.min(Math.hypot(dx, dy), L1 + L2 - 1e-4);
      dist = Math.max(dist, Math.abs(L1 - L2) + 1e-4);
      // 膝角 = atan2(dy,dx) + acos((L1² + d² − L2²) / (2·L1·d))
      const bend = THREE.MathUtils.clamp(
        (L1 * L1 + dist * dist - L2 * L2) / (2 * L1 * dist), -1, 1
      );
      const kneeAng = Math.atan2(dy, dx) + Math.acos(bend);
      const knee = leg.knee.set(
        hip.x + Math.cos(kneeAng) * L1,
        hip.y + Math.sin(kneeAng) * L1,
        side * HIP_Z                       // z 几乎共面，膝沿用髋的侧向位置
      );

      placeRod(leg.thigh, hip, knee, 0.045);
      placeRod(leg.shin, knee, ankle, 0.038);
      placeEllipsoid(leg.feather, hip, knee, 0.085); // 大腿外包白色羽毛椭球
      leg.kneeBall.position.copy(knee);
    }
  }

  return { root, update };
}
