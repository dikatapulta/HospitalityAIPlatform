/* «Заглянуть в номер» — схема комнаты по типу номера, код прототипа #816.
 *
 * Своя сцена и своя камера-орбита. Комната — стандартная (одно окно) или люкс (тип с
 * `windows: 2`: два окна, гостиная за перегородкой). Это схема, а не планировка отеля:
 * мебель одна на все отели, орнамент «қошқар мүйіз» на покрывале и ковре. За окном —
 * панорама: снимок основной сцены из окна этого номера, натянутый на дугу вокруг комнаты.
 */
import { THREE, V3, REDUCED, dirAz, stdMat, seeded } from "./viewer_kit.js";
import { createOrbit } from "./viewer_orbit.js";

const PANO = { R: 22, SEG: THREE.MathUtils.degToRad(50), V: THREE.MathUtils.degToRad(50), N: 3, EYE: -4 };
PANO.H = 2 * PANO.R * Math.tan(PANO.V / 2);

function canvasTex(w, h, draw) {
  const canvas = document.createElement("canvas");
  canvas.width = w;
  canvas.height = h;
  draw(canvas.getContext("2d"), w, h);
  const tex = new THREE.CanvasTexture(canvas);
  tex.colorSpace = THREE.SRGBColorSpace;
  tex.anisotropy = 4;
  return tex;
}
// Орнамент «қошқар мүйіз» (бараний рог).
function horns(g, cx, cy, s, color) {
  g.strokeStyle = color;
  g.lineWidth = 4 * s;
  g.lineCap = "round";
  g.beginPath();
  g.moveTo(cx, cy + 16 * s);
  g.lineTo(cx, cy);
  for (const k of [-1, 1]) {
    g.moveTo(cx, cy);
    g.bezierCurveTo(cx, cy - 20 * s, cx + k * 24 * s, cy - 20 * s, cx + k * 21 * s, cy - 3 * s);
    g.bezierCurveTo(cx + k * 19 * s, cy + 7 * s, cx + k * 8 * s, cy + 5 * s, cx + k * 9 * s, cy - 4 * s);
  }
  g.stroke();
}

export function createRoomView(el, renderer) {
  const scene = new THREE.Scene();
  scene.background = new THREE.Color("#0c1b22");
  scene.add(new THREE.HemisphereLight("#fff6ea", "#4b4640", 1.0 * Math.PI));
  const sun = new THREE.DirectionalLight("#ffe9cc", 0.8 * Math.PI);
  sun.position.set(3, 9, 7);
  scene.add(sun);
  const lampLight = new THREE.PointLight("#ffcf8a", 2.2, 7);
  scene.add(lampLight);
  const camera = new THREE.PerspectiveCamera(42, 1, 0.1, 300);
  const orbit = createOrbit(camera, el, { minDistance: 5, maxDistance: 13, minPolar: 0.45, maxPolar: 1.3, pan: false, autoSpeed: 0.7 });
  orbit.state.enabled = false;
  const root = new THREE.Group();
  scene.add(root);
  const walls = [];
  const unitBox = new THREE.BoxGeometry(1, 1, 1);
  const cache = {};
  const rmat = (c, o) => {
    const key = c + (o ? JSON.stringify(o) : "");
    return cache[key] || (cache[key] = stdMat(c, Object.assign({ roughness: 0.85 }, o || {})));
  };
  const rbox = (parent, w, h, d, x, y, z, c) => {
    const m = new THREE.Mesh(unitBox, typeof c === "string" ? rmat(c) : c);
    m.scale.set(w, h, d);
    m.position.set(x, y + h / 2, z);
    parent.add(m);
    return m;
  };
  const rnd = seeded(4242);
  const woodTex = canvasTex(512, 512, (g) => {
    const n = 8;
    const pw = 512 / n;
    for (let i = 0; i < n; i += 1) {
      let y = -Math.floor(rnd() * 200);
      while (y < 512) {
        const len = 140 + rnd() * 220;
        const k = 0.86 + rnd() * 0.2;
        g.fillStyle = `rgb(${Math.round(186 * k)},${Math.round(142 * k)},${Math.round(100 * k)})`;
        g.fillRect(i * pw, y, pw, len);
        g.fillStyle = "rgba(70,40,20,.35)";
        g.fillRect(i * pw, y, pw, 2);
        y += len;
      }
      g.fillStyle = "rgba(70,40,20,.45)";
      g.fillRect(i * pw, 0, 2, 512);
    }
  });
  woodTex.wrapS = woodTex.wrapT = THREE.RepeatWrapping;
  const tileTex = canvasTex(256, 256, (g) => {
    g.fillStyle = "#dfe3e4";
    g.fillRect(0, 0, 256, 256);
    g.strokeStyle = "#b9c0c3";
    g.lineWidth = 3;
    for (let i = 0; i <= 256; i += 64) { g.beginPath(); g.moveTo(i, 0); g.lineTo(i, 256); g.moveTo(0, i); g.lineTo(256, i); g.stroke(); }
  });
  tileTex.wrapS = tileTex.wrapT = THREE.RepeatWrapping;
  tileTex.repeat.set(2, 2);
  const runnerTex = canvasTex(128, 512, (g, w, h) => {
    g.fillStyle = "#1f5f74";
    g.fillRect(0, 0, w, h);
    g.fillStyle = "#d9a63a";
    g.fillRect(8, 0, 4, h);
    g.fillRect(w - 12, 0, 4, h);
    for (let y = 40; y < h; y += 80) horns(g, w / 2, y, 1.25, "#e2b44a");
  });
  const rugTex = canvasTex(512, 512, (g, w, h) => {
    g.fillStyle = "#8c3b2e";
    g.fillRect(0, 0, w, h);
    g.strokeStyle = "#e2b44a";
    g.lineWidth = 10;
    g.strokeRect(26, 26, w - 52, h - 52);
    g.lineWidth = 4;
    g.strokeRect(50, 50, w - 100, h - 100);
    horns(g, w / 2, h / 2, 3.2, "#e2b44a");
    for (const [x, y] of [[110, 110], [w - 110, 110], [110, h - 110], [w - 110, h - 110]]) horns(g, x, y, 1.4, "#f0d58a");
  });

  // Панорама за окном: три сегмента дуги, у каждого свой снимок.
  const panoTargets = [];
  const panoCam = new THREE.PerspectiveCamera(50, 1, 0.3, 9000);
  panoCam.aspect = Math.tan(PANO.SEG / 2) / Math.tan(PANO.V / 2);
  panoCam.fov = THREE.MathUtils.radToDeg(PANO.V);
  panoCam.updateProjectionMatrix();
  for (let i = 0; i < PANO.N; i += 1) {
    const rt = new THREE.WebGLRenderTarget(720, Math.round(720 / panoCam.aspect));
    rt.texture.colorSpace = THREE.SRGBColorSpace;
    rt.texture.repeat.x = -1;
    rt.texture.offset.x = 1;
    panoTargets.push(rt);
    const th = (i - 1) * PANO.SEG;
    const m = new THREE.Mesh(
      new THREE.CylinderGeometry(PANO.R, PANO.R, PANO.H, 14, 1, true, th - PANO.SEG / 2, PANO.SEG),
      new THREE.MeshBasicMaterial({ map: rt.texture, side: THREE.BackSide, fog: false, toneMapped: false })
    );
    m.position.y = PANO.EYE;
    scene.add(m);
  }
  /** Снимок основной сцены из окна: world — сцена, pos — точка у окна, az — куда смотрит окно. */
  function panorama(world, pos, az) {
    for (let i = 0; i < PANO.N; i += 1) {
      panoCam.position.copy(pos);
      panoCam.lookAt(pos.clone().add(dirAz(az - (i - 1) * THREE.MathUtils.radToDeg(PANO.SEG))));
      renderer.setRenderTarget(panoTargets[i]);
      renderer.render(world, panoCam);
    }
    renderer.setRenderTarget(null);
  }

  function armchair(parent, x, z, ry, color) {
    const g = new THREE.Group();
    g.position.set(x, 0, z);
    g.rotation.y = ry;
    rbox(g, 0.8, 0.42, 0.78, 0, 0, 0, color);
    rbox(g, 0.8, 0.5, 0.16, 0, 0.42, -0.31, color);
    rbox(g, 0.14, 0.24, 0.78, -0.33, 0.42, 0, color);
    rbox(g, 0.14, 0.24, 0.78, 0.33, 0.42, 0, color);
    parent.add(g);
  }
  function chair(parent, x, z, ry) {
    const g = new THREE.Group();
    g.position.set(x, 0, z);
    g.rotation.y = ry;
    rbox(g, 0.46, 0.06, 0.46, 0, 0.44, 0, "#3b4a52");
    rbox(g, 0.46, 0.5, 0.06, 0, 0.5, -0.2, "#3b4a52");
    for (const [a, b] of [[-0.2, -0.2], [0.2, -0.2], [-0.2, 0.2], [0.2, 0.2]]) rbox(g, 0.04, 0.44, 0.04, a, 0, b, "#2a3338");
    parent.add(g);
  }
  function lamp(parent, x, y, z) {
    const base = new THREE.Mesh(new THREE.CylinderGeometry(0.05, 0.08, 0.3, 8), rmat("#b08a4a", { metalness: 0.5, roughness: 0.4 }));
    base.position.set(x, y + 0.15, z);
    parent.add(base);
    const shade = new THREE.Mesh(
      new THREE.CylinderGeometry(0.12, 0.17, 0.22, 14, 1, true),
      new THREE.MeshStandardMaterial({ color: "#fff1d6", emissive: new THREE.Color("#ffcf8a"), emissiveIntensity: 0.8, side: THREE.DoubleSide })
    );
    shade.position.set(x, y + 0.38, z);
    parent.add(shade);
  }

  function build(suite) {
    while (root.children.length) root.remove(root.children[0]);
    walls.length = 0;
    const RW = suite ? 7.4 : 3.8;
    const RD = 6;
    const WH = 2.7;
    const TH = 0.14;
    const hw = RW / 2;
    const hd = RD / 2;
    const wallC = "#efe8dc";
    const woodC = "#6b4b35";
    rbox(root, RW + 0.5, 0.3, RD + 0.5, 0, -0.3, 0, "#22333b");
    const floorTex = woodTex.clone();
    floorTex.repeat.set(RW / 2.4, RD / 2.4);
    rbox(root, RW, 0.04, RD, 0, 0, 0, new THREE.MeshStandardMaterial({ map: floorTex, roughness: 0.75 }));
    const addWall = (n, c, make) => { const g = new THREE.Group(); make(g); root.add(g); walls.push({ g, n, c }); };
    const doorX = hw - 0.9;
    addWall(new V3(0, 0, -1), new V3(0, 1.3, -hd), (g) => {
      const z = -hd - TH / 2;
      const a = doorX - 0.45;
      const b = doorX + 0.45;
      rbox(g, a + hw + TH, WH, TH, (a - hw - TH) / 2, 0, z, wallC);
      rbox(g, 0.9, WH - 2.1, TH, doorX, 2.1, z, wallC);
      rbox(g, hw + TH - b, WH, TH, (b + hw + TH) / 2, 0, z, wallC);
      rbox(g, 0.86, 2.08, 0.05, doorX, 0, -hd + 0.03, woodC);
    });
    addWall(new V3(-1, 0, 0), new V3(-hw, 1.3, 0), (g) => rbox(g, TH, WH, RD, -hw - TH / 2, 0, 0, wallC));
    addWall(new V3(1, 0, 0), new V3(hw, 1.3, 0), (g) => rbox(g, TH, WH, RD, hw + TH / 2, 0, 0, wallC));
    const wins = suite ? [-hw / 2, hw / 2] : [0];
    const ww = 1.7;
    const sill = 0.55;
    const top = 2.4;
    const glassMat = new THREE.MeshStandardMaterial({ color: "#cfe8f0", transparent: true, opacity: 0.12, roughness: 0.1, depthWrite: false });
    addWall(new V3(0, 0, 1), new V3(0, 1.3, hd), (g) => {
      const z = hd + TH / 2;
      let x = -hw - TH;
      for (const cx of wins) {
        const a = cx - ww / 2;
        rbox(g, a - x, WH, TH, (x + a) / 2, 0, z, wallC);
        rbox(g, ww, sill, TH, cx, 0, z, wallC);
        rbox(g, ww, WH - top, TH, cx, top, z, wallC);
        rbox(g, ww + 0.1, 0.05, 0.26, cx, sill - 0.05, hd - 0.02, "#f7f4ee");
        rbox(g, 0.05, top - sill, 0.06, cx, sill, z, "#d9d6cf");
        const gl = new THREE.Mesh(new THREE.PlaneGeometry(ww, top - sill), glassMat);
        gl.position.set(cx, (sill + top) / 2, z);
        g.add(gl);
        for (const s of [-1, 1]) rbox(g, 0.34, 2.5, 0.1, cx + s * (ww / 2 + 0.15), 0.05, hd - 0.14, "#c9a65a");
        x = cx + ww / 2;
      }
      rbox(g, hw + TH - x, WH, TH, (x + hw + TH) / 2, 0, z, wallC);
    });
    // Ванная у входа.
    const bw = 1.75;
    const bdp = 2.1;
    const bx1 = -hw + bw;
    const bz1 = -hd + bdp;
    const bathC = "#e3ddd2";
    rbox(root, bw, 0.05, bdp, -hw + bw / 2, 0, -hd + bdp / 2, new THREE.MeshStandardMaterial({ map: tileTex, roughness: 0.5 }));
    rbox(root, bw, WH, 0.1, -hw + bw / 2, 0, bz1, bathC);
    rbox(root, 0.1, WH, 0.35, bx1, 0, -hd + 0.175, bathC);
    rbox(root, 0.1, WH - 2.1, 0.8, bx1, 2.1, -hd + 0.75, bathC);
    rbox(root, 0.1, WH, bdp - 1.15, bx1, 0, (-hd + 1.15 + bz1) / 2, bathC);
    rbox(root, 1.55, 0.55, 0.72, -hw + 0.85, 0, -hd + 0.4, "#fbfbfa");
    rbox(root, 0.5, 0.85, 0.85, -hw + 0.3, 0, bz1 - 0.55, "#f2f0ec");
    rbox(root, 0.4, 0.42, 0.6, -hw + 1.3, 0, bz1 - 0.42, "#fbfbfa");
    // Спальная зона.
    const bedZ = 0.65;
    const bx0 = -hw + 0.1;
    rbox(root, 0.12, 1.15, 2.1, -hw + 0.06, 0, bedZ, "#7a5a41");
    rbox(root, 2.05, 0.32, 1.85, bx0 + 1.025, 0, bedZ, "#5e4432");
    rbox(root, 2.0, 0.22, 1.8, bx0 + 1.0, 0.32, bedZ, "#f8f6f1");
    rbox(root, 0.5, 0.03, 1.84, bx0 + 1.6, 0.54, bedZ, new THREE.MeshStandardMaterial({ map: runnerTex, roughness: 0.8 }));
    for (const s of [-1, 1]) rbox(root, 0.4, 0.14, 0.75, bx0 + 0.3, 0.54, bedZ + s * 0.43, "#ffffff");
    for (const s of [-1, 1]) {
      const nz = bedZ + s * 1.2;
      rbox(root, 0.45, 0.5, 0.42, -hw + 0.25, 0, nz, woodC);
      lamp(root, -hw + 0.25, 0.5, nz);
    }
    lampLight.position.set(-hw + 0.3, 1.1, bedZ + 1.2);
    rbox(root, 1.4, 0.012, 2.3, bx0 + 2.25, 0.04, bedZ, new THREE.MeshStandardMaterial({ map: rugTex, roughness: 0.95 }));
    if (!suite) {
      rbox(root, 0.55, 0.05, 1.3, hw - 0.3, 0.72, -0.15, woodC);
      rbox(root, 0.5, 0.72, 0.05, hw - 0.3, 0, -0.78, woodC);
      rbox(root, 0.5, 0.72, 0.05, hw - 0.3, 0, 0.48, woodC);
      chair(root, hw - 0.88, -0.15, -Math.PI / 2);
      rbox(root, 0.05, 0.62, 1.05, hw - 0.04, 1.15, bedZ + 0.25, "#15191c");
      armchair(root, hw - 0.75, hd - 0.85, -2.4, "#c4923a");
    } else {
      // Перегородка между спальней и гостиной, проход у окна.
      rbox(root, 0.12, WH, hd + 1.0, 0, 0, (-hd + 1.0) / 2, "#e8e0d2");
      armchair(root, -0.8, hd - 0.85, -2.4, "#c4923a");
      const sx = hw - 0.48;
      const sz = -0.2;
      const sofaC = "#2f6474";
      rbox(root, 0.86, 0.42, 2.1, sx, 0, sz, sofaC);
      rbox(root, 0.2, 0.5, 2.1, hw - 0.1, 0.42, sz, sofaC);
      rbox(root, 0.86, 0.26, 0.18, sx, 0.42, sz - 1.0, sofaC);
      rbox(root, 0.86, 0.26, 0.18, sx, 0.42, sz + 1.0, sofaC);
      for (const s of [-1, 1]) rbox(root, 0.14, 0.38, 0.5, hw - 0.28, 0.52, sz + s * 0.5, "#e2b44a");
      rbox(root, 0.62, 0.38, 1.05, hw - 1.6, 0, sz, woodC);
      rbox(root, 1.6, 0.012, 2.4, hw - 1.5, 0.04, sz, new THREE.MeshStandardMaterial({ map: rugTex, roughness: 0.95 }));
      armchair(root, hw - 2.75, sz, Math.PI / 2, "#c4923a");
      rbox(root, 0.05, 0.62, 1.1, 0.09, 1.15, sz - 0.4, "#15191c");
      rbox(root, 1.3, 0.05, 0.55, 1.35, 0.72, -hd + 0.3, woodC);
      rbox(root, 0.05, 0.72, 0.5, 0.73, 0, -hd + 0.3, woodC);
      rbox(root, 0.05, 0.72, 0.5, 1.97, 0, -hd + 0.3, woodC);
      chair(root, 1.35, -hd + 0.85, Math.PI);
    }
    // Телефон: смотрим круче сверху и дальше, чтобы номер помещался по ширине.
    const tall = camera.aspect < 1;
    const dist = (suite ? 12.5 : 9.5) * (tall ? (suite ? 1.45 : 1.2) : 1);
    orbit.target.set(0, 0.9, 0.3);
    orbit.limits.minDistance = suite ? 7 : 5;
    orbit.limits.maxDistance = Math.max(suite ? 17 : 13, dist + 3);
    camera.position.setFromSpherical(new THREE.Spherical(dist, tall ? 0.85 : 1.08, Math.PI - 0.5)).add(orbit.target);
    camera.lookAt(orbit.target);
  }

  const tmp = new V3();
  return {
    scene,
    camera,
    orbit,
    build,
    panorama,
    setActive(active) {
      orbit.state.enabled = active;
      orbit.state.autoRotate = active && !REDUCED;
      if (!active) orbit.stop();
    },
    /** Стена между камерой и комнатой прячется — внутрь видно с любой стороны. */
    update(dt) {
      const moving = orbit.update(dt);
      for (const w of walls) {
        tmp.copy(camera.position).sub(w.c);
        w.g.visible = w.n.dot(tmp) < 0;
      }
      return moving;
    },
  };
}
