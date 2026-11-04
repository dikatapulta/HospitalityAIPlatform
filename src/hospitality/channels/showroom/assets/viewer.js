/* Просмотрщик 3D-витрины (spec 0069 §2.1): здание и окрестности из чисел сцены — по коду
 * прототипа #816. Башня — viewer_tower.js (корона и подиум — viewer_podium.js),
 * окрестности — viewer_world.js, ракурсы и вид из окна — viewer_camera.js, камера —
 * viewer_orbit.js, номер изнутри — viewer_room.js, общее — viewer_kit.js. Файла модели нет.
 *
 * API для страницы и помощника (договор — комментарий PR #818):
 * focusFloor(n | null) — раскрыть этаж (выше уходят вверх и тают, на перекрытии — план
 * с номерами) или собрать здание; focusRoom — перелёт к окну номера; setView(mountains |
 * city | top | street | home); highlight(номера) — группа золотым; select, reset, hasRoom,
 * setTapMode("room" | "floor"), state(); openWindow(номер) — вид из окна, setWindowFloor(n) —
 * тот же столбец окон на другом этаже, openRoom(номер) — схема номера изнутри,
 * backToBuilding() — назад к зданию, mode() — "model" | "window" | "room" | "flying".
 * События on(...): "select" — номер (null — снят), "floor" — раскрытый этаж, "hover" —
 * {floor, room, x, y} под мышью или null, "mode" — {mode, room, floor}.
 * Вид из окна и номер изнутри — viewer_immersive.js. Рисует, пока что-то движется; в покое здание медленно вращается (не при
 * prefers-reduced-motion — тогда и перелёты мгновенные).
 */
import { THREE, V3, FLOOR_H as H, REDUCED, azOf, ringSprite } from "./viewer_kit.js";
import { buildTower } from "./viewer_tower.js";
import { buildWorld, HORIZON } from "./viewer_world.js";
import { createOrbit } from "./viewer_orbit.js";
import { createCameraRig } from "./viewer_camera.js";
import { createRoomView } from "./viewer_room.js";
import { createImmersive } from "./viewer_immersive.js";

const PRESET_ALIASES = { mountains: "mount", mount: "mount", city: "city", top: "top", street: "street", home: "home" };

export function createViewer(container, scene, { textFor = (v) => (v ? v.en || "" : ""), floorLabel = null, labelInset = 0 } = {}) {
  const renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: "high-performance" });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  container.appendChild(renderer.domElement);
  const el = renderer.domElement;
  el.style.touchAction = "none";
  if (getComputedStyle(container).position === "static") container.style.position = "relative";

  const world = new THREE.Scene();
  world.background = HORIZON.clone();
  world.fog = new THREE.Fog(HORIZON.clone(), 420, 4600);
  const camera = new THREE.PerspectiveCamera(40, 1, 1, 9000);
  const floorsByNumber = new Map(scene.floors.map((f) => [f.number, f]));
  const typeOf = new Map();
  for (const f of scene.floors) for (const r of f.rooms) typeOf.set(r.number, r.type_key);
  const labelOf = floorLabel || ((n) => {
    const f = floorsByNumber.get(n);
    return f && f.label ? `${n} · ${textFor(f.label)}` : String(n);
  });
  const tower = buildTower(scene, { floorLabel: labelOf, sideName: (side) => textFor((scene.sides[side] || {}).names) });
  world.add(tower.root);
  const env = buildWorld(world, scene, tower, { textFor });
  const rig = createCameraRig(scene, tower, env, camera);
  const { k } = rig;
  const room = createRoomView(el, renderer);
  const marker = ringSprite();
  world.add(marker);

  const raycaster = new THREE.Raycaster();
  const ndc = new THREE.Vector2();
  const groundPlane = new THREE.Plane(new V3(0, 1, 0), 0);
  function aim(cx, cy) {
    const rect = el.getBoundingClientRect();
    ndc.set(((cx - rect.left) / rect.width) * 2 - 1, -((cy - rect.top) / rect.height) * 2 + 1);
    raycaster.setFromCamera(ndc, camera);
  }
  function hitAt(cx, cy) {
    aim(cx, cy);
    // Прозрачные этажи над раскрытым не перехватывают касание: оно доходит до плана.
    const targets = st.selFloor === null ? tower.pickables.slice() : tower.pickables.filter((o) => o.userData.floor <= st.selFloor);
    if (st.selFloor !== null) {
      const tiles = tower.floors[st.selFloor - 1].userData.tiles;
      if (tiles) targets.push(...tiles.children.filter((m) => m.userData.room));
    }
    const hit = raycaster.intersectObjects(targets, false)[0];
    return hit ? tower.hit(hit) : null;
  }
  function pickPoint(cx, cy) {
    aim(cx, cy);
    const hit = raycaster.intersectObjects(tower.pickables, false)[0];
    if (hit) return hit.point.clone();
    const p = new V3();
    return raycaster.ray.intersectPlane(groundPlane, p) && p.distanceTo(camera.position) < 1500 ? p : null;
  }

  const st = { selFloor: null, selRoom: null, group: new Set() };
  let hoverFloor = null;
  let tapMode = "room";
  let anim = null;
  let dirty = true;
  let idleTimer = null;
  let activePreset = "home";
  const listeners = {};
  const emit = (event, value) => (listeners[event] || []).forEach((fn) => fn(value));

  const orbit = createOrbit(camera, el, {
    minDistance: 16,
    maxDistance: 420 * k,
    maxPolar: Math.PI * 0.485,
    clamp: rig.clamp,
    pickPoint,
    onStart: () => {
      if (anim || imm.mode() !== "model") return;
      stopAuto();
      activePreset = null;
      dirty = true;
    },
    onTap: (e) => tap(hitAt(e.clientX, e.clientY)),
    onHover: (e) => {
      const h = e ? hitAt(e.clientX, e.clientY) : null;
      setHover(h ? h.floor : null);
      const rect = el.getBoundingClientRect();
      emit("hover", h ? { ...h, x: e.clientX - rect.left, y: e.clientY - rect.top } : null);
    },
  });

  const ease = (u) => (u < 0.5 ? 4 * u * u * u : 1 - Math.pow(-2 * u + 2, 3) / 2);
  function cylLerp(a, b, e, out) {
    const ra = Math.hypot(a.x, a.z);
    const rb = Math.hypot(b.x, b.z);
    const aa = Math.atan2(a.x, a.z);
    let d = Math.atan2(b.x, b.z) - aa;
    while (d > Math.PI) d -= 2 * Math.PI;
    while (d < -Math.PI) d += 2 * Math.PI;
    out.set(Math.sin(aa + d * e) * (ra + (rb - ra) * e), a.y + (b.y - a.y) * e, Math.cos(aa + d * e) * (ra + (rb - ra) * e));
  }
  function flyTo(pos, target, dur, onDone) {
    const p0 = camera.position.clone();
    const t0 = orbit.target.clone();
    const look = new V3();
    orbit.stop();
    orbit.state.enabled = false;
    anim = {
      t: 0,
      dur: REDUCED ? 0 : dur,
      update(e) { cylLerp(p0, pos, e, camera.position); look.lerpVectors(t0, target, e); camera.lookAt(look); },
      done() { orbit.target.copy(target); orbit.state.enabled = true; if (onDone) onDone(); },
    };
    dirty = true;
  }
  function stopAuto() {
    orbit.state.autoRotate = false;
    clearTimeout(idleTimer);
    if (REDUCED) return;
    idleTimer = setTimeout(() => {
      if (imm.mode() === "model" && !anim && st.selFloor === null && !st.selRoom && !st.group.size && (!activePreset || activePreset === "home")) orbit.state.autoRotate = true;
    }, 20000);
  }
  function recolor() {
    tower.recolor(st);
    dirty = true;
  }
  function setHover(f) {
    if (hoverFloor === f) return;
    if (hoverFloor && hoverFloor !== st.selFloor) tower.setEmissive(hoverFloor, "#000000");
    hoverFloor = f;
    if (f && f !== st.selFloor) tower.setEmissive(f, "#0c3d50");
    dirty = true;
  }
  function collapse(snap = false) {
    if (st.selFloor) tower.setEmissive(st.selFloor, "#000000");
    st.selFloor = null;
    tower.setLifts(null);
    if (snap) tower.tick(1, true);
  }
  function selectFloor(f, roomNumber = null) {
    if (st.selFloor) tower.setEmissive(st.selFloor, "#000000");
    st.selFloor = f;
    st.selRoom = roomNumber;
    activePreset = null;
    tower.setLifts(f);
    stopAuto();
    if (f !== null) {
      tower.setEmissive(f, "#2a2400");
      const p = rig.floorPose(f, orbit.target);
      flyTo(p.pos, p.target, 0.9);
    } else {
      const p = rig.presets.home();
      activePreset = "home";
      flyTo(p.pos, p.target, 0.9, () => { if (!REDUCED && !st.selRoom && !st.group.size && st.selFloor === null) orbit.state.autoRotate = true; });
    }
    recolor();
  }
  function tap(h) {
    if (h && h.room && (tapMode === "room" || st.selFloor === h.floor)) {
      api.select(st.selRoom === h.room ? null : h.room);
    } else if (h && tapMode === "floor") {
      selectFloor(h.floor, h.room);
      emit("floor", h.floor);
      if (h.room) emit("select", h.room);
    } else if (st.selRoom) {
      api.select(null);
    }
  }

  const imm = createImmersive({
    camera, el, container, tower, env, rig, room, world, orbit, scene, st, typeOf, marker, emit, recolor, collapse, stopAuto, cylLerp,
    setAnim: (next) => { anim = next; dirty = true; },
    finishAnim: () => { if (anim) { const a = anim; anim = null; a.update(1); a.done(); } },
    markDirty: () => { dirty = true; },
  });
  const leaveImmersive = imm.leave;

  const api = {
    on(event, fn) {
      (listeners[event] = listeners[event] || []).push(fn);
    },
    hasRoom(number) {
      return tower.rooms.has(String(number));
    },
    mode: imm.mode,
    state() {
      return { floor: st.selFloor, room: st.selRoom, preset: activePreset, mode: imm.mode(), window: imm.windowInfo() };
    },
    setTapMode(next) {
      tapMode = next === "floor" ? "floor" : "room";
    },
    focusFloor(n) {
      if (!leaveImmersive()) return false;
      if (n === null) { selectFloor(null); return true; }
      const f = Number(n);
      if (!Number.isInteger(f) || f < 1 || f > scene.building.floors_total) return false;
      selectFloor(f);
      emit("floor", f);
      return true;
    },
    focusRoom(number) {
      const entry = tower.rooms.get(String(number));
      if (!entry || !leaveImmersive()) return false;
      collapse();
      st.selRoom = entry.number;
      activePreset = null;
      stopAuto();
      recolor();
      const p = rig.roomPose(entry);
      flyTo(p.pos, p.target, 0.9);
      emit("select", entry.number);
      return true;
    },
    setView(preset) {
      const name = PRESET_ALIASES[preset];
      if (!name || !leaveImmersive()) return false;
      collapse();
      st.selRoom = null;
      activePreset = name;
      stopAuto();
      const p = rig.presets[name]();
      flyTo(p.pos, p.target, 0.9, () => { if (name === "home" && activePreset === "home" && !REDUCED && !st.group.size) orbit.state.autoRotate = true; });
      recolor();
      return true;
    },
    highlight(numbers) {
      st.group = new Set((numbers || []).map(String).filter((n) => tower.rooms.has(n)));
      if (!leaveImmersive()) return st.group.size;
      collapse();
      st.selRoom = null;
      recolor();
      if (st.group.size) {
        // Камера — к группе: со стороны, где её номеров больше, на высоту их середины.
        const list = [...st.group].map((n) => tower.rooms.get(n));
        const ys = list.map((e) => (e.floor - 1) * H + H / 2);
        const count = {};
        list.forEach((e) => { count[e.side] = (count[e.side] || 0) + 1; });
        const side = Object.keys(count).sort((a, c) => count[c] - count[a])[0];
        const same = list.filter((e) => e.side === side);
        const anchor = { ...same[0], lx: same.reduce((sum, e) => sum + e.lx, 0) / same.length };
        const y0 = Math.min(...ys);
        const y1 = Math.max(...ys);
        const p = rig.facadePose(anchor, (y0 + y1) / 2, Math.max(88 * k, (y1 - y0) * 1.5 + 80 * k));
        activePreset = null;
        stopAuto();
        flyTo(p.pos, p.target, 1.0);
      }
      return st.group.size;
    },
    select(number) {
      st.selRoom = number === null || number === undefined ? null : String(number);
      recolor();
      emit("select", st.selRoom);
    },
    reset() {
      st.group = new Set();
      st.selRoom = null;
      leaveImmersive();
      collapse();
      recolor();
      api.setView("home");
    },
    openWindow(number) {
      const ok = imm.openWindow(number);
      if (ok) activePreset = null;
      return ok;
    },
    setWindowFloor: imm.setWindowFloor,
    openRoom(number) {
      const ok = imm.openRoom(number);
      if (ok) activePreset = null;
      return ok;
    },
    backToBuilding: imm.backToBuilding,
  };

  function resize() {
    const w = Math.max(1, container.clientWidth);
    const h = Math.max(1, container.clientHeight);
    renderer.setSize(w, h, false);
    camera.aspect = w / h;
    camera.fov = imm.fovFor();
    camera.updateProjectionMatrix();
    room.camera.aspect = w / h;
    room.camera.fov = w < h ? 58 : 42;
    room.camera.updateProjectionMatrix();
    dirty = true;
  }
  new ResizeObserver(resize).observe(container);
  resize();
  const start = rig.presets.home();
  camera.position.copy(start.pos);
  orbit.target.copy(start.target);
  camera.lookAt(orbit.target);
  orbit.state.autoRotate = !REDUCED;
  recolor();

  const projected = new V3();
  let last = performance.now();
  function frame(now) {
    const dt = Math.min((now - last) / 1000, 0.05);
    last = now;
    const k2 = REDUCED ? 1 : 1 - Math.pow(0.0015, dt);
    let active = tower.tick(k2, REDUCED);
    if (anim) {
      anim.t += dt;
      const u = anim.dur ? Math.min(1, anim.t / anim.dur) : 1;
      anim.update(ease(u));
      if (u >= 1) { const a = anim; anim = null; a.done(); }
      active = true;
    } else if (imm.mode() !== "model") {
      if (imm.step(dt, k2)) active = true;
    } else if (orbit.update(dt)) active = true;
    const mode = imm.mode();
    marker.visible = !!st.selRoom && (mode === "model" || mode === "flying");
    if (marker.visible) {
      tower.windowPoint(st.selRoom, marker.position);
      const s = REDUCED ? 3.4 : 3.4 + Math.sin(now / 250) * 0.35;
      marker.scale.set(s, s, 1);
      if (!REDUCED) active = true;
    }
    if (active || dirty) {
      if (mode === "room") renderer.render(room.scene, room.camera);
      else {
        env.sky.position.copy(camera.position);
        // Матрица камеры обновляется только внутри render — для подписей нужна текущая.
        camera.updateMatrixWorld();
        const height = el.clientHeight;
        for (const sp of env.labels) {
          projected.copy(sp.position).project(camera);
          sp.visible = projected.z < 1 && ((1 - projected.y) / 2) * height > labelInset;
        }
        renderer.render(world, camera);
      }
      dirty = false;
    }
    requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);
  // Для проверки снимками: азимут камеры.
  api.debug = {
    azimuth: () => azOf(camera.position.x - orbit.target.x, camera.position.z - orbit.target.z),
    labels: () => env.labels.map((l) => [l.visible, l.position.clone().project(camera).toArray().map((v) => +v.toFixed(3))]),
  };
  return api;
}
