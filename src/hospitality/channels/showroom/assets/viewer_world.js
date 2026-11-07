/* Окрестности отеля в просмотрщике 3D-витрины (spec 0069 §2.1) — код прототипа #816 на данных сцены.
 *
 * Небо, земля, кварталы с улицами и деревьями, хребет, холм с башней и подписи ориентиров.
 * Формы — из `views` сцены: `landmark` (`ridge` — хребет дугой вокруг азимута вида,
 * `hill_tower` — холм с телебашней, `city` — сторона высоких домов); без поля — по ключу
 * (`mountains` — хребет, `city` — город). Соседние здания и улицы — `building.neighbours`.
 * Подписи — только из сцены: в коде нет ни одного названия места. Кварталы строятся в осях
 * корпуса (сетка улиц вдоль длинной оси отеля), хребет и холм — по азимутам сцены.
 */
import { THREE, V3, UNIT_M, FLOOR_H, dirAz, azOf, relAz, stdMat, smooth, hash2, seeded, instanced, labelTexture } from "./viewer_kit.js";

export const HORIZON = new THREE.Color("#efd7bb");
const SUN_DIR = dirAz(282, 0.36);
const BCOL = ["#e9dfcd", "#dccbb0", "#cfbb9c", "#e4d5c0", "#cab8a4", "#d8cabd", "#c0ae96", "#ebe6dc", "#c9aa8e", "#d6c3a5"].map((c) => new THREE.Color(c));
const TCOL = ["#5c7a45", "#6a8a4c", "#4d6a40", "#7c8f4a", "#c79a36", "#b8742c", "#d0b04a", "#8f9a3e"].map((c) => new THREE.Color(c));
// Прототип строил хребет вокруг азимута 151°: числа его формы — относительно этой оси.
const PROTO_RIDGE_AZ = 151;

function landmarkOf(key, view) {
  if (view.landmark) return view.landmark;
  return key === "mountains" ? "ridge" : key === "city" ? "city" : null;
}

export function buildWorld(world, scene, tower, { textFor }) {
  const rnd = seeded(7731);
  const pick = (arr) => arr[Math.floor(rnd() * arr.length)];
  const b = scene.building;
  const views = Object.entries(scene.views).map(([key, view]) => ({ key, view, form: landmarkOf(key, view) }));
  const ridge = views.find((v) => v.form === "ridge" && v.view.azimuth_deg !== null && v.view.azimuth_deg !== undefined);
  const hill = views.find((v) => v.form === "hill_tower" && v.view.azimuth_deg !== null && v.view.azimuth_deg !== undefined);
  const city = views.find((v) => v.form === "city" && v.view.azimuth_deg !== null && v.view.azimuth_deg !== undefined);
  const ridgeAz = ridge ? ridge.view.azimuth_deg : null;

  world.add(new THREE.HemisphereLight("#dbe6ff", "#8d7c5c", 0.9 * Math.PI));
  const sun = new THREE.DirectionalLight("#ffe2bb", 1.55 * Math.PI);
  sun.position.copy(SUN_DIR).multiplyScalar(800);
  world.add(sun);

  // Небо: градиент от горизонта к зениту и тёплое свечение солнца.
  const sky = new THREE.Mesh(
    new THREE.SphereGeometry(7000, 32, 16),
    new THREE.ShaderMaterial({
      uniforms: { top: { value: new THREE.Color("#4677b4") }, horizon: { value: HORIZON.clone() }, sunDir: { value: SUN_DIR.clone() }, sunCol: { value: new THREE.Color("#ffc983") } },
      vertexShader: "varying vec3 vDir;\nvoid main(){ vDir = position; gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }",
      fragmentShader:
        "uniform vec3 top;\nuniform vec3 horizon;\nuniform vec3 sunDir;\nuniform vec3 sunCol;\nvarying vec3 vDir;\nvoid main(){\n  vec3 d = normalize(vDir);\n  float h = clamp(d.y, 0.0, 1.0);\n  vec3 c = mix(horizon, top, pow(h, 0.5));\n  float s = max(dot(d, sunDir), 0.0);\n  c += sunCol * (pow(s, 700.0) * 2.5 + pow(s, 9.0) * 0.32);\n  gl_FragColor = vec4(c, 1.0);\n  #include <colorspace_fragment>\n}",
      side: THREE.BackSide,
      depthWrite: false,
      fog: false,
    })
  );
  sky.frustumCulled = false;
  sky.renderOrder = -1;
  world.add(sky);
  const ground = new THREE.Mesh(new THREE.CircleGeometry(6000, 64), stdMat("#9a9b74"));
  ground.rotation.x = -Math.PI / 2;
  world.add(ground);

  // Кварталы — в осях прототипа: башня вдоль оси z, сторона south корпуса — +x.
  const cityRoot = new THREE.Group();
  cityRoot.rotation.y = (-b.orientation_deg * Math.PI) / 180;
  world.add(cityRoot);
  const toCity = (az, dist) => {
    const d = dirAz(az - b.orientation_deg).multiplyScalar(dist);
    return { x: d.x, z: d.z };
  };
  const hillDist = hill ? Math.min(1300, Math.max(300, hill.view.distance_m ? hill.view.distance_m / 2.55 : 900)) : 0;
  const HILL = hill ? toCity(hill.view.azimuth_deg, hillDist) : null;
  const nearHill = (x, z, pad = 0) => !!HILL && Math.hypot(x - HILL.x, z - HILL.z) < 270 + pad;
  const ridgeDir = ridgeAz !== null ? toCity(ridgeAz, 1) : null;
  const cityDir = city ? toCity(city.view.azimuth_deg, 1) : null;
  const { S, HW, T } = tower.dims;

  // Соседи из сцены: здания — прямоугольники, улицы — полосы вдоль оси корпуса.
  const houses = [];
  const streets = [];
  for (const n of b.neighbours || []) {
    const at = toCity(n.azimuth_deg, n.distance_m / UNIT_M);
    const name = textFor(n.names);
    if (n.kind === "building") {
      const along = (n.length_m || 60) / UNIT_M;
      const across = (n.width_m || 60) / UNIT_M;
      houses.push({ ...at, w: across, d: along, h: (n.height_m || 22) / UNIT_M, name });
    } else {
      const turn = n.orientation_deg === null || n.orientation_deg === undefined ? 0 : n.orientation_deg - b.orientation_deg;
      const alongZ = Math.abs(Math.cos((turn * Math.PI) / 180)) > 0.7;
      streets.push({ ...at, alongZ, name });
    }
  }
  const home = { x0: -(HW + T + 22.2) - 12, x1: HW + T + 22.2 + 12, z0: -(S + HW + 20.6) - 12, z1: S + HW + 20.6 + 12 };
  for (const h of houses) {
    home.x0 = Math.min(home.x0, h.x - h.w / 2 - 6);
    home.x1 = Math.max(home.x1, h.x + h.w / 2 + 6);
    home.z0 = Math.min(home.z0, h.z - h.d / 2 - 6);
    home.z1 = Math.max(home.z1, h.z + h.d / 2 + 6);
  }
  const XL = [home.x0, home.x1];
  const ZL = [home.z0, home.z1];
  for (let x = home.x0 - 48; x >= -1150; x -= 48) XL.push(x);
  for (let x = home.x1 + 48; x <= 1150; x += 48) XL.push(x);
  for (let z = home.z0 - 48; z >= -1150; z -= 48) ZL.push(z);
  for (let z = home.z1 + 48; z <= 1100; z += 48) ZL.push(z);
  const wide = new Set();
  for (const s of streets) {
    const lines = s.alongZ ? XL : ZL;
    const v = s.alongZ ? s.x : s.z;
    let best = 0;
    lines.forEach((u, i) => { if (Math.abs(u - v) < Math.abs(lines[best] - v)) best = i; });
    if (Math.abs(lines[best] - v) < 24) lines[best] = v;
    else lines.push(v);
    wide.add(`${s.alongZ ? "x" : "z"}${v}`);
  }
  XL.sort((a, c) => a - c);
  ZL.sort((a, c) => a - c);
  const roadW = (axis, v) => (wide.has(`${axis}${v}`) ? 16 : v === home.x0 || v === home.x1 || v === home.z0 || v === home.z1 ? 10 : 7);
  const builds = [];
  const trees = [];
  const roads = [];
  const addBuilding = (x, z, w, d, h) => {
    if (houses.some((q) => Math.abs(x - q.x) < (q.w + w) / 2 && Math.abs(z - q.z) < (q.d + d) / 2)) return;
    builds.push({ x, z, sx: w, sy: h, sz: d, c: pick(BCOL).clone().multiplyScalar(0.92 + rnd() * 0.14) });
  };
  const addTree = (x, z, s = 1, trunk = true) => {
    const r = (1.4 + rnd() * 1.0) * s;
    const h = (1.2 + rnd() * 0.9) * s;
    trees.push({ x, z, r, h, trunk, c: pick(TCOL).clone().multiplyScalar(0.88 + rnd() * 0.22) });
  };
  for (const z of ZL) roads.push({ x: 0, y: 0.06, z, sx: 2300, sy: 1, sz: roadW("z", z) });
  for (const x of XL) roads.push({ x, y: 0.07, z: (ZL[0] + ZL[ZL.length - 1]) / 2, sx: roadW("x", x), sy: 1, sz: ZL[ZL.length - 1] - ZL[0] });
  const toward = (dir, x, z) => (dir ? x * dir.x + z * dir.z : 0);
  function fillBlock(x0, x1, z0, z1) {
    const cx = (x0 + x1) / 2;
    const cz = (z0 + z1) / 2;
    const dist = Math.hypot(cx, cz);
    if (dist > 1120 || nearHill(cx, cz)) return;
    const bx0 = x0 + roadW("x", x0) / 2 + 3;
    const bx1 = x1 - roadW("x", x1) / 2 - 3;
    const bz0 = z0 + roadW("z", z0) / 2 + 3;
    const bz1 = z1 - roadW("z", z1) / 2 - 3;
    if (bx1 - bx0 < 10 || bz1 - bz0 < 10) return;
    if (dist > 480 && rnd() < 0.3) return;
    if (rnd() < (dist < 200 ? 0.12 : 0.08)) {
      for (let i = 0; i < (dist < 480 ? 9 : 3); i += 1) addTree(bx0 + rnd() * (bx1 - bx0), bz0 + rnd() * (bz1 - bz0), 1.1, dist < 480);
      return;
    }
    if (toward(ridgeDir, cx, cz) > 330 && dist > 380) {
      for (let i = 0; i < 5; i += 1) addBuilding(bx0 + 4 + rnd() * (bx1 - bx0 - 8), bz0 + 4 + rnd() * (bz1 - bz0 - 8), 5 + rnd() * 4, 5 + rnd() * 4, 2.4 + rnd() * 2);
      for (let i = 0; i < 4; i += 1) addTree(bx0 + rnd() * (bx1 - bx0), bz0 + rnd() * (bz1 - bz0), 1, false);
      return;
    }
    const dense = dist < 480;
    const tall = toward(cityDir, cx, cz) > 40;
    const height = () => {
      let h = (tall ? 8 : 6) + rnd() * (tall ? 7 : 6);
      if (dist < 300 && rnd() < 0.07) h = 20 + rnd() * 14;
      return dense ? h : h * 0.8;
    };
    for (const edge of dense ? [bz0, bz1] : [rnd() < 0.5 ? bz0 : bz1]) {
      let x = bx0;
      while (x < bx1 - 8) {
        const len = Math.min(bx1 - x, 14 + rnd() * 20);
        const depth = 8 + rnd() * 4;
        addBuilding(x + len / 2, edge === bz0 ? edge + depth / 2 : edge - depth / 2, len, depth, height());
        x += len + 3 + rnd() * 6;
        if (!dense && rnd() < 0.5) break;
      }
    }
    const nt = dense ? 2 + Math.floor(rnd() * 3) : Math.floor(rnd() * 2);
    for (let i = 0; i < nt; i += 1) addTree(bx0 + 4 + rnd() * (bx1 - bx0 - 8), (bz0 + bz1) / 2 + (rnd() - 0.5) * 8, 1, dense);
  }
  // Дальний город до горизонта — кроме стороны хребта: редкие низкие дома.
  for (let x = -2300; x <= 2300; x += 64) {
    for (let z = -2300; z <= 2300; z += 64) {
      const r = Math.hypot(x, z);
      const az = azOf(x, z) + b.orientation_deg;
      if (r < 1150 || r > 2350 || (ridgeAz !== null && Math.abs(relAz(az, ridgeAz)) < 111) || nearHill(x, z, 60) || rnd() < 0.35) continue;
      addBuilding(x + (rnd() - 0.5) * 30, z + (rnd() - 0.5) * 30, 14 + rnd() * 16, 8 + rnd() * 8, 4 + rnd() * 7);
    }
  }
  for (let i = 0; i + 1 < XL.length; i += 1) {
    for (let j = 0; j + 1 < ZL.length; j += 1) {
      if (XL[i] >= home.x0 && XL[i + 1] <= home.x1 && ZL[j] >= home.z0 && ZL[j + 1] <= home.z1) continue;
      fillBlock(XL[i], XL[i + 1], ZL[j], ZL[j + 1]);
    }
  }
  // Аллеи вдоль ближних улиц.
  for (const x of XL) if (Math.abs(x) <= 200) for (let z = -420; z <= 420; z += wide.has(`x${x}`) ? 9 : 12) {
    if (ZL.some((zz) => Math.abs(zz - z) < 7)) continue;
    for (const s of [-1, 1]) addTree(x + s * (roadW("x", x) / 2 + 2), z + (rnd() - 0.5) * 2, wide.has(`x${x}`) ? 1.2 : 1);
  }
  for (const z of ZL) if (Math.abs(z) <= 180) for (let x = -420; x <= 420; x += 12) {
    if (XL.some((xx) => Math.abs(xx - x) < 7)) continue;
    for (const s of [-1, 1]) addTree(x + (rnd() - 0.5) * 2, z + s * (roadW("z", z) / 2 + 2), 1);
  }
  // Сквер в квартале отеля.
  const hotelX = HW + T + 24;
  const hotelZ = S + HW + 22;
  for (let i = 0; i < 70; i += 1) {
    const x = home.x0 + 10 + rnd() * (home.x1 - home.x0 - 18);
    const z = home.z0 + 12 + rnd() * (home.z1 - home.z0 - 20);
    const inHotel = Math.abs(x) < hotelX && Math.abs(z) < hotelZ;
    const inHouse = houses.some((q) => Math.abs(x - q.x) < q.w / 2 + 6 && Math.abs(z - q.z) < q.d / 2 + 6);
    if (!inHotel && !inHouse) addTree(x, z, 1.1);
  }
  const boxGeo = new THREE.BoxGeometry(1, 1, 1).translate(0, 0.5, 0);
  cityRoot.add(instanced(boxGeo, stdMat("#ffffff", { roughness: 0.95 }), builds));
  cityRoot.add(instanced(new THREE.PlaneGeometry(1, 1).rotateX(-Math.PI / 2), stdMat("#6d6c69", { polygonOffset: true, polygonOffsetFactor: -1, polygonOffsetUnits: -2 }), roads));
  cityRoot.add(instanced(new THREE.IcosahedronGeometry(1, 0), stdMat("#ffffff", { flatShading: true }),
    trees.map((t) => ({ x: t.x, y: t.h + t.r * 0.85, z: t.z, sx: t.r, sy: t.r * 1.3, sz: t.r, ry: rnd() * 3, c: t.c }))));
  cityRoot.add(instanced(new THREE.CylinderGeometry(0.16, 0.22, 1, 5).translate(0, 0.5, 0), stdMat("#6b5440"),
    trees.filter((t) => t.trunk).map((t) => ({ x: t.x, z: t.z, sx: 1, sy: t.h + t.r * 0.4, sz: 1 }))));
  // Соседние здания: широкое низкое здание с нависающей кровлей.
  for (const h of houses) {
    const g = new THREE.Group();
    g.position.set(h.x, 0, h.z);
    const k = h.h / 12.6;
    const body = new THREE.Mesh(boxGeo, stdMat("#ebe5da"));
    body.scale.set(h.w - 6, 11 * k, h.d - 6);
    const glass = new THREE.Mesh(boxGeo, stdMat("#4b5a63", { roughness: 0.4, metalness: 0.3 }));
    glass.scale.set(h.w - 5, 6 * k, h.d - 5);
    glass.position.y = 2.5 * k;
    const roof = new THREE.Mesh(boxGeo, stdMat("#f1ece2"));
    roof.scale.set(h.w, 1.6 * k, h.d);
    roof.position.y = 11 * k;
    g.add(body, glass, roof);
    cityRoot.add(g);
  }

  // Хребет: дуга гор вокруг азимута вида, предгорья, ели, скалы и снег.
  const ridgeA = (az) => relAz(az, ridgeAz) + PROTO_RIDGE_AZ;
  function mountainH(a, r) {
    const arc = smooth(46, 68, a) * (1 - smooth(224, 256, a));
    const rf = 1080 + 70 * Math.sin(a * 0.07);
    const foot = smooth(rf, rf + 420, r) * (55 + 22 * Math.sin(a * 0.21 + 1.3));
    const r1c = 1850 + 140 * Math.sin(a * 0.045 + 0.5);
    const ridge1 = Math.exp(-(((r - r1c) / 270) ** 2)) * (185 + 50 * Math.sin(a * 0.19 + 0.6) + 32 * Math.sin(a * 0.53 + 2.2) + 17 * Math.sin(a * 1.3 + 1.1));
    const ridge2 = Math.exp(-(((r - 2650) / 330) ** 2)) * (390 + 75 * Math.sin(a * 0.13 + 2.0) + 45 * Math.sin(a * 0.41 + 0.4) + 22 * Math.sin(a * 0.97));
    const n = (hash2(a * 7.3, r * 0.11) - 0.5) * 30 * smooth(1300, 2100, r);
    return Math.max(0, (foot + ridge1 + ridge2 + n) * arc);
  }
  if (ridgeAz !== null) {
    const NA = 170;
    const NR = 40;
    const A0 = 40;
    const A1 = 282;
    const R0 = 1000;
    const R1 = 3500;
    const pts = [];
    for (let j = 0; j <= NR; j += 1) for (let i = 0; i <= NA; i += 1) {
      let a = A0 + ((A1 - A0) * i) / NA;
      let r = R0 + ((R1 - R0) * j) / NR;
      if (i > 0 && i < NA && j > 0 && j < NR) { a += (rnd() - 0.5) * 0.7 * ((A1 - A0) / NA); r += (rnd() - 0.5) * 0.7 * ((R1 - R0) / NR); }
      const d = dirAz(a - PROTO_RIDGE_AZ + ridgeAz);
      pts.push(new V3(d.x * r, mountainH(a, r) - 0.6, d.z * r));
    }
    const pos = [];
    const col = [];
    const cFoot = new THREE.Color("#9d8d5e");
    const cFoot2 = new THREE.Color("#87703f");
    const cSpruce = new THREE.Color("#4c5b45");
    const cRock = new THREE.Color("#6d6e80");
    const cSnow = new THREE.Color("#f7f4f1");
    const cRockSnow = new THREE.Color("#a6a6b4");
    const e1 = new V3();
    const e2 = new V3();
    const nrm = new V3();
    const c = new THREE.Color();
    const tri = (p0, p1, p2) => {
      e1.subVectors(p1, p0);
      e2.subVectors(p2, p0);
      nrm.crossVectors(e1, e2);
      if (nrm.y < 0) { [p1, p2] = [p2, p1]; nrm.negate(); }
      nrm.normalize();
      const h = (p0.y + p1.y + p2.y) / 3;
      const cx = (p0.x + p1.x + p2.x) / 3;
      const cz = (p0.z + p1.z + p2.z) / 3;
      const snow = 232 + 20 * Math.sin(ridgeA(azOf(cx, cz)) * 0.3);
      if (h > snow) c.copy(nrm.y > 0.55 ? cSnow : cRockSnow);
      else if (h > 160) c.copy(cRock);
      else if (h > 75) c.copy(cSpruce).lerp(cRock, smooth(95, 160, h));
      else c.copy(hash2(cx, cz) > 0.5 ? cFoot : cFoot2).lerp(cSpruce, smooth(38, 75, h) * 0.7);
      c.multiplyScalar(0.94 + hash2(cz, cx) * 0.1);
      for (const q of [p0, p1, p2]) { pos.push(q.x, q.y, q.z); col.push(c.r, c.g, c.b); }
    };
    const at = (i, j) => pts[j * (NA + 1) + i];
    for (let j = 0; j < NR; j += 1) for (let i = 0; i < NA; i += 1) {
      tri(at(i, j), at(i, j + 1), at(i + 1, j));
      tri(at(i + 1, j), at(i, j + 1), at(i + 1, j + 1));
    }
    const g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.Float32BufferAttribute(pos, 3));
    g.setAttribute("color", new THREE.Float32BufferAttribute(col, 3));
    g.computeVertexNormals();
    world.add(new THREE.Mesh(g, stdMat("#ffffff", { vertexColors: true, flatShading: true })));
  }

  // Холм с телебашней.
  let hillTop = 0;
  let hillWorld = null;
  if (hill) {
    hillWorld = dirAz(hill.view.azimuth_deg).multiplyScalar(hillDist);
    const N = 30;
    const half = 330;
    const pos = [];
    const col = [];
    const pts = [];
    const hillH = (dx, dz) => 96 * Math.exp(-((dx / 175) ** 2 + (dz / 150) ** 2)) + (hash2(dx, dz) - 0.5) * 5 - 0.8;
    for (let j = 0; j <= N; j += 1) for (let i = 0; i <= N; i += 1) {
      const dx = -half + (2 * half * i) / N;
      const dz = -half + (2 * half * j) / N;
      pts.push(new V3(hillWorld.x + dx, hillH(dx, dz), hillWorld.z + dz));
    }
    hillTop = hillH(0, 0) + 1;
    const cols = ["#8f8a52", "#7f6a3c", "#5d6c45", "#9a8a55", "#6b7a4a"].map((x) => new THREE.Color(x));
    const at = (i, j) => pts[j * (N + 1) + i];
    const push = (p0, p1, p2) => {
      const cc = cols[Math.floor(hash2(p0.x + p1.x, p2.z) * cols.length)];
      for (const q of [p0, p1, p2]) { pos.push(q.x, q.y, q.z); col.push(cc.r, cc.g, cc.b); }
    };
    for (let j = 0; j < N; j += 1) for (let i = 0; i < N; i += 1) { push(at(i, j), at(i, j + 1), at(i + 1, j)); push(at(i + 1, j), at(i, j + 1), at(i + 1, j + 1)); }
    const g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.Float32BufferAttribute(pos, 3));
    g.setAttribute("color", new THREE.Float32BufferAttribute(col, 3));
    g.computeVertexNormals();
    world.add(new THREE.Mesh(g, stdMat("#ffffff", { vertexColors: true, flatShading: true })));
    const tv = new THREE.Group();
    tv.position.set(hillWorld.x, hillTop - 2, hillWorld.z);
    const white = stdMat("#ecebe6", { roughness: 0.6 });
    const red = stdMat("#c8463c", { roughness: 0.6 });
    const add = (geo, mat, y) => { const m = new THREE.Mesh(geo, mat); m.position.y = y; tv.add(m); };
    add(new THREE.CylinderGeometry(1.6, 5.2, 104, 8).translate(0, 52, 0), white, 0);
    for (const y of [70, 84, 98]) add(new THREE.CylinderGeometry(1.9, 2.1, 5, 8), red, y);
    add(new THREE.CylinderGeometry(5.4, 5.4, 2.2, 14), white, 60);
    add(new THREE.CylinderGeometry(4.2, 4.2, 1.8, 14), white, 76);
    add(new THREE.CylinderGeometry(0.45, 1.2, 46, 6).translate(0, 23, 0), white, 104);
    add(new THREE.SphereGeometry(1.4, 8, 6), new THREE.MeshBasicMaterial({ color: "#ff4a3a", fog: false }), 151);
    world.add(tv);
  }

  // Подписи мест: хребет, холм, прочие виды на горизонте, соседи.
  const labelDefs = [];
  for (const { key, view, form } of views) {
    const name = textFor(view.names);
    if (!name || view.azimuth_deg === null || view.azimuth_deg === undefined || form === "city") continue;
    if (form === "ridge") labelDefs.push({ name, pos: dirAz(ridgeAz + 25).multiplyScalar(2300).setY(560), h: 44 });
    else if (form === "hill_tower" && hill && hill.key === key) labelDefs.push({ name, pos: new V3(hillWorld.x, hillTop + 178, hillWorld.z), h: 20 });
    else if (!form) labelDefs.push({ name, pos: dirAz(view.azimuth_deg).multiplyScalar(Math.min(1500, view.distance_m ? view.distance_m / 2.55 : 900)).setY(60), h: 20 });
  }
  const local = new V3();
  for (const h of houses) labelDefs.push({ name: h.name, pos: cityRoot.localToWorld(local.set(h.x, h.h * 1.5 + 0.5, h.z).clone()), h: 3.6 });
  for (const s of streets) labelDefs.push({ name: s.name, pos: cityRoot.localToWorld(local.set(s.x, 4, s.z).clone()), h: 3.2 });
  cityRoot.updateMatrixWorld(true);
  const labels = labelDefs.filter((d) => d.name).map((d) => {
    const { tex, aspect } = labelTexture(d.name);
    const sp = new THREE.Sprite(new THREE.SpriteMaterial({ map: tex, transparent: true, fog: false, depthWrite: false }));
    sp.position.copy(d.pos);
    sp.scale.set(d.h * aspect, d.h, 1);
    world.add(sp);
    return sp;
  });

  return {
    sky,
    labels,
    ridgeAz,
    cityAz: city ? city.view.azimuth_deg : null,
    floorTop: b.floors_total * FLOOR_H,
  };
}
