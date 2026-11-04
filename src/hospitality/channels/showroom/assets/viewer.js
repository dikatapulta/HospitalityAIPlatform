/* Просмотрщик 3D-витрины (spec 0069 §2.1): здание и окрестности из чисел сцены — по коду
 * прототипа #816 (башня — viewer_tower.js, окрестности — viewer_world.js, камера —
 * viewer_orbit.js, общее — viewer_kit.js). Файла модели нет.
 *
 * API для страницы и помощника: focusFloor(n | null) — раскрыть этаж (выше уходят вверх
 * и тают, на перекрытии — план с номерами) или собрать здание; focusRoom — перелёт к окну
 * номера; setView(mountains | city | top | street | home) — ракурс; highlight(номера) —
 * группа золотым; select, reset, hasRoom, setTapMode("room" | "floor"), state().
 * События on(...): "select" — номер (касанием или focusRoom; null — снят), "floor" —
 * раскрытый этаж, "hover" — {floor, room, x, y} под мышью или null.
 * Рисует, пока что-то движется; в покое здание медленно вращается (не при
 * prefers-reduced-motion — тогда и перелёты мгновенные).
 */
import { THREE, V3, FLOOR_H as H, REDUCED, dirAz, azOf } from "./viewer_kit.js";
import { buildTower } from "./viewer_tower.js";
import { buildWorld, HORIZON } from "./viewer_world.js";
import { createOrbit } from "./viewer_orbit.js";

const PROTO_RIDGE_AZ = 151;
const PRESET_ALIASES = { mountains: "mount", mount: "mount", city: "city", top: "top", street: "street", home: "home" };

export function createViewer(container, scene, { textFor = (v) => (v ? v.en || "" : ""), floorLabel = null, labelInset = 0 } = {}) {
  const renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: "high-performance" });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  container.appendChild(renderer.domElement);
  const el = renderer.domElement;
  el.style.touchAction = "none";

  const world = new THREE.Scene();
  world.background = HORIZON.clone();
  world.fog = new THREE.Fog(HORIZON.clone(), 420, 4600);
  const camera = new THREE.PerspectiveCamera(40, 1, 1, 9000);
  const floorsByNumber = new Map(scene.floors.map((f) => [f.number, f]));
  const labelOf =
    floorLabel ||
    ((n) => {
      const f = floorsByNumber.get(n);
      return f && f.label ? `${n} · ${textFor(f.label)}` : String(n);
    });
  const tower = buildTower(scene, { floorLabel: labelOf, sideName: (side) => textFor((scene.sides[side] || {}).names) });
  world.add(tower.root);
  const env = buildWorld(world, scene, tower, { textFor });
  const { S, HW, T, TOP, podium } = tower.dims;
  // Масштаб ракурсов прототипа: его башня — 26 этажей и 26,8 единицы в длину.
  const k = Math.max(TOP / 46.8, ((2 * (S + HW)) / 26.8) * 0.8, 0.45);
  const ridgeAz = env.ridgeAz ?? PROTO_RIDGE_AZ;
  const rel = (deg) => ridgeAz - PROTO_RIDGE_AZ + deg;
  const flat = (az, len) => dirAz(az).multiplyScalar(len);
  const orbitPose = (target, az, dist, elev) => ({ pos: target.clone().add(dirAz(az, elev).multiplyScalar(dist)), target });
  const phone = () => camera.aspect < 1;
  const PRESETS = {
    home: () => {
      const target = new V3(0, 31 * k, 0);
      return { pos: flat(rel(300), 140 * k).setY(40 * k), target };
    },
    mount: () => orbitPose(flat(rel(90), 20 * k).setY(44 * k), rel(342), (phone() ? 235 : 190) * k, 0.1),
    city: () => orbitPose(flat(rel(45), 8.5 * k).setY(26 * k), rel(205), (phone() ? 215 : 165) * k, 0.2),
    top: () => orbitPose(flat(rel(155.8), 85.5 * k).setY(6 * k), rel(225), (phone() ? 380 : 300) * k, 1.3),
    street: () => ({ pos: flat(rel(250), S + HW + 46).setY(2.6), target: new V3(0, TOP * 0.42, 0) }),
  };

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
    if (raycaster.ray.intersectPlane(groundPlane, p) && p.distanceTo(camera.position) < 1500) return p;
    return null;
  }

  // Камера не уходит под землю, дальше окрестностей, внутрь башни и подиума.
  const local = new V3();
  function clampCam(target, p) {
    target.y = Math.min(70 * k, Math.max(0.5, target.y));
    const tr = Math.hypot(target.x, target.z);
    if (tr > 420 * k) {
      target.x *= (420 * k) / tr;
      target.z *= (420 * k) / tr;
    }
    if (p.y < 2.5) p.y = 2.5;
    tower.root.worldToLocal(local.copy(p));
    if (p.y < TOP + 12) {
      const cx = Math.max(-S, Math.min(S, local.x));
      let dx = local.x - cx;
      let dz = local.z;
      let d = Math.hypot(dx, dz);
      const RT = HW + T + 3.7;
      if (d < RT) {
        if (d < 1e-4) { dx = 0; dz = 1; d = 1; }
        local.x = cx + (dx / d) * RT;
        local.z = (dz / d) * RT;
        p.copy(tower.root.localToWorld(local));
      }
    }
    if (podium && Math.abs(local.x) < podium.hx && Math.abs(local.z) < podium.hz && p.y < podium.h + 2) p.y = podium.h + 2;
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
    clamp: clampCam,
    pickPoint,
    onStart: () => {
      if (anim) return;
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
    const ang = aa + d * e;
    const r = ra + (rb - ra) * e;
    out.set(Math.sin(ang) * r, a.y + (b.y - a.y) * e, Math.cos(ang) * r);
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
    if (!REDUCED) {
      idleTimer = setTimeout(() => {
        if (!anim && st.selFloor === null && !st.selRoom && !st.group.size && (!activePreset || activePreset === "home")) orbit.state.autoRotate = true;
      }, 20000);
    }
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
  function collapse() {
    if (st.selFloor) tower.setEmissive(st.selFloor, "#000000");
    st.selFloor = null;
    tower.setLifts(null);
  }
  function facadePose(entry, y, dist) {
    const ty = y * 0.65 + 27 * k * 0.35;
    const target = tower.root.localToWorld(new V3(entry.lx * 0.4, ty, entry.end ? entry.lz * 0.4 : Math.sign(entry.lz) * 2));
    const face = tower.sideAzimuth(entry.side);
    const az = entry.end ? face : entry.side === "south" ? face - 28 : face + 28;
    const pos = target.clone().add(dirAz(az).multiplyScalar(dist));
    pos.y = ty + dist * 0.1;
    return { pos, target };
  }
  function selectFloor(f, room = null) {
    if (st.selFloor) tower.setEmissive(st.selFloor, "#000000");
    st.selFloor = f;
    st.selRoom = room;
    activePreset = null;
    tower.setLifts(f);
    stopAuto();
    if (f !== null) {
      tower.setEmissive(f, "#2a2400");
      const target = new V3(0, (f - 1) * H + H, 0);
      const sph = new THREE.Spherical().setFromVector3(camera.position.clone().sub(orbit.target));
      sph.radius = (phone() ? 58 : 42) * Math.max(1, ((S + HW) / 13.4) * 0.9);
      sph.phi = 0.7;
      flyTo(new V3().setFromSpherical(sph).add(target), target, 0.9);
    } else {
      const p = PRESETS.home();
      activePreset = "home";
      flyTo(p.pos, p.target, 0.9, () => { if (!REDUCED && !st.selRoom && !st.group.size && st.selFloor === null) orbit.state.autoRotate = true; });
    }
    recolor();
  }
  function tap(h) {
    if (!h) {
      if (st.selRoom) { st.selRoom = null; recolor(); emit("select", null); }
      return;
    }
    if (h.room && (tapMode === "room" || st.selFloor === h.floor)) {
      api.select(st.selRoom === h.room ? null : h.room);
      return;
    }
    if (tapMode === "room") {
      if (st.selRoom) { st.selRoom = null; recolor(); emit("select", null); }
      return;
    }
    selectFloor(h.floor, h.room);
    emit("floor", h.floor);
    if (h.room) emit("select", h.room);
  }

  const api = {
    on(event, fn) {
      (listeners[event] = listeners[event] || []).push(fn);
    },
    hasRoom(number) {
      return tower.rooms.has(String(number));
    },
    state() {
      return { floor: st.selFloor, room: st.selRoom, preset: activePreset };
    },
    setTapMode(mode) {
      tapMode = mode === "floor" ? "floor" : "room";
    },
    focusFloor(n) {
      if (n === null) { selectFloor(null); return true; }
      const f = Number(n);
      if (!Number.isInteger(f) || f < 1 || f > scene.building.floors_total) return false;
      selectFloor(f);
      emit("floor", f);
      return true;
    },
    focusRoom(number) {
      const entry = tower.rooms.get(String(number));
      if (!entry) return false;
      collapse();
      st.selRoom = entry.number;
      activePreset = null;
      stopAuto();
      recolor();
      const p = facadePose(entry, (entry.floor - 1) * H + H / 2, 82 * k);
      flyTo(p.pos, p.target, 0.9);
      emit("select", entry.number);
      return true;
    },
    setView(preset) {
      const name = PRESET_ALIASES[preset];
      if (!name) return false;
      collapse();
      st.selRoom = null;
      activePreset = name;
      stopAuto();
      const p = PRESETS[name]();
      flyTo(p.pos, p.target, 0.9, () => { if (name === "home" && activePreset === "home" && !REDUCED && !st.group.size) orbit.state.autoRotate = true; });
      recolor();
      return true;
    },
    highlight(numbers) {
      st.group = new Set((numbers || []).map(String).filter((n) => tower.rooms.has(n)));
      collapse();
      st.selRoom = null;
      recolor();
      if (st.group.size) {
        // Камера — к группе: со стороны, где её номеров больше, на высоту их середины.
        const list = [...st.group].map((n) => tower.rooms.get(n));
        const ys = list.map((e) => (e.floor - 1) * H + H / 2);
        const y0 = Math.min(...ys);
        const y1 = Math.max(...ys);
        const count = {};
        list.forEach((e) => { count[e.side] = (count[e.side] || 0) + 1; });
        const side = Object.keys(count).sort((a, c) => count[c] - count[a])[0];
        const same = list.filter((e) => e.side === side);
        const lx = same.reduce((sum, e) => sum + e.lx, 0) / same.length;
        const anchor = { ...same[0], lx };
        const p = facadePose(anchor, (y0 + y1) / 2, Math.max(88 * k, (y1 - y0) * 1.5 + 80 * k));
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
      collapse();
      recolor();
      api.setView("home");
    },
  };

  // Кольцо над окном выбранного номера.
  const marker = (() => {
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
    const sp = new THREE.Sprite(new THREE.SpriteMaterial({ map: tex, depthTest: false, transparent: true, fog: false }));
    sp.renderOrder = 5;
    sp.visible = false;
    world.add(sp);
    return sp;
  })();

  function resize() {
    const w = Math.max(1, container.clientWidth);
    const h = Math.max(1, container.clientHeight);
    renderer.setSize(w, h, false);
    camera.aspect = w / h;
    camera.fov = camera.aspect < 1 ? 52 : 40;
    camera.updateProjectionMatrix();
    dirty = true;
  }
  new ResizeObserver(resize).observe(container);
  resize();
  const start = PRESETS.home();
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
    let active = tower.tick(REDUCED ? 1 : 1 - Math.pow(0.0015, dt), REDUCED);
    if (anim) {
      anim.t += dt;
      const u = anim.dur ? Math.min(1, anim.t / anim.dur) : 1;
      anim.update(ease(u));
      if (u >= 1) { const a = anim; anim = null; a.done(); }
      active = true;
    } else if (orbit.update(dt)) active = true;
    marker.visible = !!st.selRoom;
    if (marker.visible) {
      tower.windowPoint(st.selRoom, marker.position);
      const s = REDUCED ? 3.4 : 3.4 + Math.sin(now / 250) * 0.35;
      marker.scale.set(s, s, 1);
      if (!REDUCED) active = true;
    }
    if (active || dirty) {
      env.sky.position.copy(camera.position);
      const height = el.clientHeight;
      for (const sp of env.labels) {
        projected.copy(sp.position).project(camera);
        sp.visible = projected.z < 1 && ((1 - projected.y) / 2) * height > labelInset;
      }
      renderer.render(world, camera);
      dirty = false;
    }
    requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);
  // Для проверки снимками: азимут камеры и мгновенные перелёты.
  api.debug = { camera, azimuth: () => azOf(camera.position.x - orbit.target.x, camera.position.z - orbit.target.z), renderer };
  return api;
}
