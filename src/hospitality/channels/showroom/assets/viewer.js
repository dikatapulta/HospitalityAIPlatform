/* Просмотрщик 3D-витрины (spec 0069 §2.1): здание из чисел сцены, без файла модели.
 *
 * Корпус — прямоугольник length_m × width_m, этаж — 3,2 м; номера стоят вдоль
 * сторон: north и south — длинные фасады, east и west — торцы (при азимуте длинной
 * оси 90° это и есть стороны света; иначе здание поворачивается целиком). Этажи
 * без номеров — плитой своего вида; этажи, которых нет в сцене, — призраком.
 *
 * API для страницы и помощника (PR б): focusFloor, focusRoom, setView, highlight,
 * select, reset; событие "select" — номер, выбранный касанием или focusRoom.
 * Камера — своя орбита (перетаскивание, колесо, два пальца), без OrbitControls:
 * тот грузится из `three/addons` голым импортом, а карту импорта CSP не пустит.
 * Рисует только при изменениях — телефон агента не греется на открытой странице.
 */
import * as THREE from "./three-0.170.0.module.min.js";

const FLOOR_H = 3.2;
const COLORS = {
  room: 0xdfe7ef,
  roomSellable: 0xd2e3f3,
  highlight: 0xf2b134,
  selected: 0xe0533d,
  slab: 0xc4cfda,
  ghost: 0xe9eef3,
  edge: 0x8a9bb0,
  ground: 0xf3f5f7,
};
const VIEW_DEFAULTS = { mountains: 180, city: 0 };

function directionOf(azimuthDeg) {
  const a = (azimuthDeg * Math.PI) / 180;
  return new THREE.Vector3(Math.sin(a), 0, -Math.cos(a));
}

function labelSprite(text, { size = 1.6, color = "#334155", background = null } = {}) {
  const canvas = document.createElement("canvas");
  const ctx = canvas.getContext("2d");
  const font = "600 48px system-ui, sans-serif";
  ctx.font = font;
  const width = Math.ceil(ctx.measureText(text).width) + 24;
  canvas.width = width;
  canvas.height = 64;
  ctx.font = font;
  if (background) {
    ctx.fillStyle = background;
    ctx.fillRect(0, 0, width, 64);
  }
  ctx.fillStyle = color;
  ctx.textBaseline = "middle";
  ctx.fillText(text, 12, 34);
  const texture = new THREE.CanvasTexture(canvas);
  texture.colorSpace = THREE.SRGBColorSpace;
  const sprite = new THREE.Sprite(new THREE.SpriteMaterial({ map: texture, depthWrite: false }));
  sprite.scale.set((size * width) / 64, size, 1);
  return sprite;
}

export function createViewer(container, scene, { textFor } = {}) {
  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  container.appendChild(renderer.domElement);
  renderer.domElement.style.touchAction = "none";

  const world = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(38, 1, 0.5, 5000);
  world.add(new THREE.HemisphereLight(0xffffff, 0xb8c2cc, 2.2));
  const sun = new THREE.DirectionalLight(0xffffff, 1.6);
  sun.position.set(-120, 200, 90);
  world.add(sun);

  const b = scene.building;
  const L = b.length_m;
  const W = b.width_m;
  const height = b.floors_total * FLOOR_H;
  const span = Math.max(L, W, height);

  const ground = new THREE.Mesh(
    new THREE.CircleGeometry(span * 3, 64),
    new THREE.MeshStandardMaterial({ color: COLORS.ground })
  );
  ground.rotation.x = -Math.PI / 2;
  world.add(ground);

  const building = new THREE.Group();
  building.rotation.y = (-(b.orientation_deg - 90) * Math.PI) / 180;
  world.add(building);

  const outline = new THREE.LineSegments(
    new THREE.EdgesGeometry(new THREE.BoxGeometry(L, height, W)),
    new THREE.LineBasicMaterial({ color: COLORS.edge, transparent: true, opacity: 0.6 })
  );
  outline.position.y = height / 2;
  building.add(outline);
  if (b.crown) {
    const crown = new THREE.Mesh(
      new THREE.BoxGeometry(L * 0.18, FLOOR_H * 2, W * 0.5),
      new THREE.MeshStandardMaterial({ color: COLORS.slab })
    );
    crown.position.y = height + FLOOR_H;
    building.add(crown);
  }

  const rooms = new Map(); // номер → { mesh, floor, side, base }
  const floorsByNumber = new Map(scene.floors.map((f) => [f.number, f]));
  const pickable = [];

  function addSlab(floorNumber, color, opacity) {
    const slab = new THREE.Mesh(
      new THREE.BoxGeometry(L * 0.98, FLOOR_H * 0.82, W * 0.98),
      new THREE.MeshStandardMaterial({ color, transparent: opacity < 1, opacity })
    );
    slab.position.y = (floorNumber - 0.5) * FLOOR_H;
    building.add(slab);
  }

  function placeRooms(floor) {
    const bySide = { north: [], south: [], east: [], west: [] };
    floor.rooms.forEach((room) => bySide[room.side].push(room));
    const endDepth = bySide.east.length || bySide.west.length ? Math.min(9, L * 0.16) : 0;
    const longDepth = W * 0.42;
    const y = (floor.number - 0.5) * FLOOR_H;
    const place = (room, size, position) => {
      const material = new THREE.MeshStandardMaterial({
        color: floor.sellable ? COLORS.roomSellable : COLORS.room,
        transparent: true,
        opacity: 1,
      });
      const mesh = new THREE.Mesh(new THREE.BoxGeometry(size.x, FLOOR_H * 0.84, size.z), material);
      mesh.position.set(position.x, y, position.z);
      mesh.userData.number = room.number;
      building.add(mesh);
      pickable.push(mesh);
      rooms.set(room.number, { mesh, floor: floor.number, side: room.side, base: material.color.getHex() });
    };
    for (const side of ["north", "south"]) {
      const list = bySide[side];
      const usable = L - 2 * endDepth;
      const step = usable / Math.max(list.length, 1);
      const z = (side === "north" ? -1 : 1) * (W / 2 - longDepth / 2);
      list.forEach((room, i) => {
        const x = -usable / 2 + step * (i + 0.5);
        place(room, { x: step * 0.94, z: longDepth }, { x: side === "north" ? -x : x, z });
      });
    }
    for (const side of ["east", "west"]) {
      const list = bySide[side];
      const step = W / Math.max(list.length, 1);
      const x = (side === "east" ? 1 : -1) * (L / 2 - endDepth / 2);
      list.forEach((room, i) => {
        const z = -W / 2 + step * (i + 0.5);
        place(room, { x: endDepth, z: step * 0.94 }, { x, z: side === "east" ? z : -z });
      });
    }
  }

  for (let n = 1; n <= b.floors_total; n += 1) {
    const floor = floorsByNumber.get(n);
    if (!floor) addSlab(n, COLORS.ghost, 0.35);
    else if (floor.rooms.length) placeRooms(floor);
    else addSlab(n, COLORS.slab, floor.kind === "closed" ? 0.4 : 0.9);
    if (n === 1 || n % 5 === 0 || n === b.floors_total) {
      const tag = labelSprite(String(n), { size: 2.2 });
      tag.position.set(-L / 2 - 4, (n - 0.5) * FLOOR_H, 0);
      building.add(tag);
    }
  }

  // Подписи видов на горизонте: «Горы» там, куда смотрят номера с видом на горы.
  for (const [key, view] of Object.entries(scene.views)) {
    const azimuth = view.azimuth_deg ?? VIEW_DEFAULTS[key];
    if (azimuth === undefined) continue;
    const tag = labelSprite(textFor(view.names), { size: span * 0.07, color: "#1e3a5f", background: "rgba(255,255,255,0.8)" });
    tag.position.copy(directionOf(azimuth).multiplyScalar(span * 1.4)).setY(span * 0.35);
    world.add(tag);
  }

  // Камера: орбита вокруг цели. polar — от вертикали, azimuth — по сторонам света.
  const orbit = { target: new THREE.Vector3(0, height / 2, 0), radius: span * 2.1, azimuth: 210, polar: 62 };
  let animation = null;
  let dirty = true;

  function applyOrbit() {
    const polar = (orbit.polar * Math.PI) / 180;
    const dir = directionOf(orbit.azimuth);
    // Узкий экран телефона: камера отходит дальше, чтобы здание влезло по ширине.
    const radius = orbit.radius * Math.max(1, 1 / camera.aspect);
    camera.position.set(
      orbit.target.x + radius * Math.sin(polar) * dir.x,
      orbit.target.y + radius * Math.cos(polar),
      orbit.target.z + radius * Math.sin(polar) * dir.z
    );
    camera.lookAt(orbit.target);
    dirty = true;
  }

  function moveTo(next, duration = 650) {
    const from = { target: orbit.target.clone(), radius: orbit.radius, azimuth: orbit.azimuth, polar: orbit.polar };
    let delta = ((next.azimuth ?? from.azimuth) - from.azimuth) % 360;
    if (delta > 180) delta -= 360;
    if (delta < -180) delta += 360;
    animation = { from, to: { ...next, azimuth: from.azimuth + delta }, start: performance.now(), duration };
  }

  function worldAzimuthOfSide(side) {
    const local = { north: 0, east: 90, south: 180, west: 270 }[side];
    return (local + b.orientation_deg - 90 + 360) % 360;
  }

  function paint(entry, color, opacity) {
    entry.mesh.material.color.setHex(color);
    entry.mesh.material.opacity = opacity;
    entry.mesh.material.depthWrite = opacity > 0.5;
  }

  let highlighted = new Set();
  let selected = null;
  let focusedFloor = null;

  function repaint() {
    for (const [number, entry] of rooms) {
      const dim = focusedFloor !== null && entry.floor !== focusedFloor;
      if (number === selected) paint(entry, COLORS.selected, 1);
      else if (highlighted.has(number)) paint(entry, COLORS.highlight, 1);
      else paint(entry, entry.base, dim || highlighted.size ? 0.22 : 1);
    }
    dirty = true;
  }

  const listeners = { select: [] };
  function emit(event, value) {
    (listeners[event] || []).forEach((fn) => fn(value));
  }

  const api = {
    on(event, fn) {
      (listeners[event] = listeners[event] || []).push(fn);
    },
    hasRoom(number) {
      return rooms.has(String(number));
    },
    focusFloor(n) {
      if (n < 1 || n > b.floors_total) return false;
      focusedFloor = n;
      repaint();
      moveTo({ target: new THREE.Vector3(0, (n - 0.5) * FLOOR_H, 0), radius: Math.max(L, W) * 1.6, polar: 70 });
      return true;
    },
    focusRoom(number) {
      const entry = rooms.get(String(number));
      if (!entry) return false;
      selected = String(number);
      focusedFloor = entry.floor;
      repaint();
      moveTo({
        target: new THREE.Vector3(0, (entry.floor - 0.5) * FLOOR_H, 0),
        radius: Math.max(L, W) * 1.4,
        polar: 74,
        azimuth: worldAzimuthOfSide(entry.side),
      });
      emit("select", selected);
      return true;
    },
    setView(preset) {
      if (preset === "top") moveTo({ polar: 8, radius: span * 2.2, target: new THREE.Vector3(0, height / 2, 0) });
      else if (preset === "street") moveTo({ polar: 84, radius: span * 1.5, target: new THREE.Vector3(0, FLOOR_H * 3, 0) });
      else {
        const view = scene.views[preset];
        const azimuth = (view && view.azimuth_deg) ?? VIEW_DEFAULTS[preset];
        if (azimuth === undefined) return false;
        moveTo({ azimuth, polar: 66, radius: span * 2, target: new THREE.Vector3(0, height / 2, 0) });
      }
      return true;
    },
    highlight(numbers) {
      highlighted = new Set((numbers || []).map(String).filter((n) => rooms.has(n)));
      focusedFloor = null;
      repaint();
      if (highlighted.size) {
        // Камера — к найденным: со стороны, где их больше, на высоту их среднего этажа.
        const bySide = {};
        let floorSum = 0;
        for (const number of highlighted) {
          const entry = rooms.get(number);
          bySide[entry.side] = (bySide[entry.side] || 0) + 1;
          floorSum += entry.floor;
        }
        const side = Object.keys(bySide).sort((a, c) => bySide[c] - bySide[a])[0];
        moveTo({
          azimuth: worldAzimuthOfSide(side),
          polar: 68,
          radius: span * 1.7,
          target: new THREE.Vector3(0, (floorSum / highlighted.size - 0.5) * FLOOR_H, 0),
        });
      }
      return highlighted.size;
    },
    select(number) {
      selected = number === null ? null : String(number);
      repaint();
      if (selected) emit("select", selected);
    },
    reset() {
      highlighted = new Set();
      selected = null;
      focusedFloor = null;
      repaint();
      moveTo({ target: new THREE.Vector3(0, height / 2, 0), radius: span * 2.1, azimuth: 210, polar: 62 });
    },
  };

  // Ввод: перетаскивание — поворот, колесо и два пальца — приближение, касание — выбор.
  const pointers = new Map();
  let pinch = null;
  let moved = false;
  const el = renderer.domElement;
  el.addEventListener("pointerdown", (e) => {
    el.setPointerCapture(e.pointerId);
    pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
    moved = false;
    animation = null;
  });
  el.addEventListener("pointermove", (e) => {
    const last = pointers.get(e.pointerId);
    if (!last) return;
    const dx = e.clientX - last.x;
    const dy = e.clientY - last.y;
    if (Math.abs(dx) + Math.abs(dy) > 3) moved = true;
    pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (pointers.size === 2) {
      const [a, c] = [...pointers.values()];
      const distance = Math.hypot(a.x - c.x, a.y - c.y);
      if (pinch) orbit.radius = Math.min(span * 4, Math.max(span * 0.35, orbit.radius * (pinch / distance)));
      pinch = distance;
    } else {
      orbit.azimuth -= dx * 0.35;
      orbit.polar = Math.min(88, Math.max(5, orbit.polar - dy * 0.3));
    }
    applyOrbit();
  });
  const release = (e) => {
    pointers.delete(e.pointerId);
    if (pointers.size < 2) pinch = null;
    if (!moved && e.type === "pointerup") pick(e);
  };
  el.addEventListener("pointerup", release);
  el.addEventListener("pointercancel", release);
  el.addEventListener(
    "wheel",
    (e) => {
      e.preventDefault();
      orbit.radius = Math.min(span * 4, Math.max(span * 0.35, orbit.radius * (e.deltaY > 0 ? 1.1 : 0.9)));
      applyOrbit();
    },
    { passive: false }
  );

  const raycaster = new THREE.Raycaster();
  function pick(e) {
    const rect = el.getBoundingClientRect();
    const pointer = new THREE.Vector2(
      ((e.clientX - rect.left) / rect.width) * 2 - 1,
      -((e.clientY - rect.top) / rect.height) * 2 + 1
    );
    raycaster.setFromCamera(pointer, camera);
    const visible = pickable.filter((mesh) => mesh.material.opacity > 0.5);
    const hit = raycaster.intersectObjects(visible, false)[0];
    if (hit) api.select(hit.object.userData.number);
  }

  function resize() {
    const width = container.clientWidth || 1;
    const heightPx = container.clientHeight || 1;
    renderer.setSize(width, heightPx, false);
    camera.aspect = width / heightPx;
    camera.updateProjectionMatrix();
    applyOrbit();
  }
  new ResizeObserver(resize).observe(container);
  resize();
  applyOrbit();

  function frame(now) {
    if (animation) {
      const t = Math.min(1, (now - animation.start) / animation.duration);
      const k = t * t * (3 - 2 * t);
      const { from, to } = animation;
      if (to.target) orbit.target.lerpVectors(from.target, to.target, k);
      for (const key of ["radius", "azimuth", "polar"]) {
        if (to[key] !== undefined) orbit[key] = from[key] + (to[key] - from[key]) * k;
      }
      applyOrbit();
      if (t >= 1) animation = null;
    }
    if (dirty) {
      renderer.render(world, camera);
      dirty = false;
    }
    requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);
  return api;
}
