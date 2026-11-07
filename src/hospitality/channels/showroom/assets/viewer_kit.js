/* Общие части просмотрщика 3D-витрины (spec 0069 §2.1): единицы, материалы, формы, подписи.
 *
 * Единица сцены просмотрщика — 1,84 м, этаж — 1,8 единицы, как у прототипа (#816): числа
 * окрестностей, камеры и ракурсов перенесены из прототипа как есть, а корпус отеля
 * переводится из метров сцены. Оси: +x — восток, −z — север, y — вверх.
 */
import * as THREE from "./three-0.170.0.module.min.js";

export { THREE };
export const V3 = THREE.Vector3;
export const UNIT_M = 1.84;
export const FLOOR_H = 1.8;
export const REDUCED = typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches;

/** Направление по азимуту (0 — север, 90 — восток) и углу над горизонтом. */
export const dirAz = (az, pitch = 0) => {
  const a = THREE.MathUtils.degToRad(az);
  return new V3(Math.sin(a) * Math.cos(pitch), Math.sin(pitch), -Math.cos(a) * Math.cos(pitch));
};
/** Азимут точки на плоскости. */
export const azOf = (x, z) => ((Math.atan2(x, -z) * 180) / Math.PI + 360) % 360;
/** Разница азимутов в −180…180. */
export const relAz = (az, center) => ((((az - center) % 360) + 540) % 360) - 180;
export const stdMat = (color, extra) =>
  new THREE.MeshStandardMaterial(Object.assign({ color, roughness: 1, metalness: 0 }, extra || {}));
export const smooth = (a, b, x) => {
  const q = Math.min(1, Math.max(0, (x - a) / (b - a)));
  return q * q * (3 - 2 * q);
};
export const hash2 = (x, z) => {
  const s = Math.sin(x * 12.9898 + z * 78.233) * 43758.5453;
  return s - Math.floor(s);
};

/** Повторяемый генератор случайных чисел: та же сцена — те же дома и деревья. */
export function seeded(seed) {
  let s = seed | 0;
  return () => {
    s = (s + 0x6d2b79f5) | 0;
    let q = Math.imul(s ^ (s >>> 15), 1 | s);
    q = (q + Math.imul(q ^ (q >>> 7), 61 | q)) ^ q;
    return ((q ^ (q >>> 14)) >>> 0) / 4294967296;
  };
}

const dummy = new THREE.Object3D();
/** Много одинаковых тел одним вызовом отрисовки: позиция, поворот, масштаб, цвет. */
export function instanced(geo, mat, items) {
  const mesh = new THREE.InstancedMesh(geo, mat, Math.max(items.length, 1));
  mesh.count = items.length;
  items.forEach((it, i) => {
    dummy.position.set(it.x, it.y || 0, it.z);
    dummy.rotation.set(0, it.ry || 0, 0);
    dummy.scale.set(it.sx, it.sy, it.sz);
    dummy.updateMatrix();
    mesh.setMatrixAt(i, dummy.matrix);
    if (it.c) mesh.setColorAt(i, it.c);
  });
  mesh.instanceMatrix.needsUpdate = true;
  if (mesh.instanceColor) mesh.instanceColor.needsUpdate = true;
  mesh.frustumCulled = false;
  return mesh;
}

/** Плоская фигура (x, z) → тело высотой depth, стоящее на y = 0. */
export function extrude(shape, depth) {
  const g = new THREE.ExtrudeGeometry(shape, { depth, bevelEnabled: false, curveSegments: 20 });
  g.rotateX(Math.PI / 2);
  g.translate(0, depth, 0);
  return g;
}

/** Прямоугольник со скруглёнными углами: полуразмеры hx, hz и радиус r. */
export function roundedRect(hx, hz, r) {
  const s = new THREE.Shape();
  s.moveTo(-hx + r, -hz);
  s.lineTo(hx - r, -hz);
  s.absarc(hx - r, -hz + r, r, -Math.PI / 2, 0, false);
  s.lineTo(hx, hz - r);
  s.absarc(hx - r, hz - r, r, 0, Math.PI / 2, false);
  s.lineTo(-hx + r, hz);
  s.absarc(-hx + r, hz - r, r, Math.PI / 2, Math.PI, false);
  s.lineTo(-hx, -hz + r);
  s.absarc(-hx + r, -hz + r, r, Math.PI, 1.5 * Math.PI, false);
  return s;
}

/** Подпись-«таблетка»: тёмный фон, светлый текст. */
export function labelTexture(text) {
  const canvas = document.createElement("canvas");
  const g = canvas.getContext("2d");
  const font = '600 42px system-ui, -apple-system, "Segoe UI", Arial, sans-serif';
  g.font = font;
  const w = Math.ceil(g.measureText(text).width) + 48;
  canvas.width = w;
  canvas.height = 72;
  g.font = font;
  g.fillStyle = "rgba(8,22,30,.8)";
  g.beginPath();
  g.moveTo(36, 0);
  g.lineTo(w - 36, 0);
  g.arc(w - 36, 36, 36, -Math.PI / 2, Math.PI / 2);
  g.lineTo(36, 72);
  g.arc(36, 36, 36, Math.PI / 2, -Math.PI / 2);
  g.fill();
  g.fillStyle = "#f6f1e6";
  g.textAlign = "center";
  g.textBaseline = "middle";
  g.fillText(text, w / 2, 38);
  const tex = new THREE.CanvasTexture(canvas);
  tex.colorSpace = THREE.SRGBColorSpace;
  tex.anisotropy = 4;
  return { tex, aspect: w / 72 };
}

/** Спрайт-подпись высотой h единиц; поверх всего, если onTop. */
export function labelSprite(text, h, { onTop = false } = {}) {
  const { tex, aspect } = labelTexture(text);
  const sprite = new THREE.Sprite(
    new THREE.SpriteMaterial({ map: tex, transparent: true, fog: false, depthWrite: false, depthTest: !onTop })
  );
  if (onTop) sprite.renderOrder = 7;
  sprite.scale.set(h * aspect, h, 1);
  return sprite;
}

/** Золотое кольцо над окном выбранного номера. */
export function ringSprite() {
  const canvas = document.createElement("canvas");
  canvas.width = canvas.height = 128;
  const g = canvas.getContext("2d");
  g.strokeStyle = "#ffd04d";
  g.lineWidth = 9;
  g.shadowColor = "#ffb800";
  g.shadowBlur = 14;
  g.beginPath();
  g.arc(64, 64, 42, 0, Math.PI * 2);
  g.stroke();
  const tex = new THREE.CanvasTexture(canvas);
  tex.colorSpace = THREE.SRGBColorSpace;
  const sprite = new THREE.Sprite(new THREE.SpriteMaterial({ map: tex, depthTest: false, transparent: true, fog: false }));
  sprite.renderOrder = 5;
  sprite.visible = false;
  return sprite;
}

/** Слой поверх холста: рамка окна или затемнение при смене сцены. */
export function overlay(container, style) {
  const node = document.createElement("div");
  Object.assign(node.style, { position: "absolute", inset: "0", pointerEvents: "none" }, style);
  container.appendChild(node);
  return node;
}

/** Номер на плитке плана этажа: тёмный текст без фона. */
export function numberSprite(text) {
  const canvas = document.createElement("canvas");
  canvas.width = 256;
  canvas.height = 128;
  const g = canvas.getContext("2d");
  g.font = '700 70px system-ui, -apple-system, "Segoe UI", Arial, sans-serif';
  g.fillStyle = "#0a1a20";
  g.textAlign = "center";
  g.textBaseline = "middle";
  g.fillText(text, 128, 68);
  const tex = new THREE.CanvasTexture(canvas);
  tex.colorSpace = THREE.SRGBColorSpace;
  const sprite = new THREE.Sprite(new THREE.SpriteMaterial({ map: tex, transparent: true, fog: false, depthTest: false }));
  sprite.renderOrder = 6;
  sprite.scale.set(1.75, 0.87, 1);
  return sprite;
}
