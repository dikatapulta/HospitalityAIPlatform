/* Башня отеля в просмотрщике 3D-витрины (spec 0069 §2.1) — код прототипа #816 на числах сцены.
 *
 * Локальные оси корпуса: x — длинная ось (азимут `orientation_deg`), +z — сторона south,
 * −z — north, торцы — east (+x) и west (−x). Скруглённые торцы (`rounded_ends`), фасад
 * «гармошкой» — окно-зуб (`facade_step`), корона (`crown`), подиум (`podium_floors`).
 * Зубцов на фасаде столько, сколько номеров на самой длинной стороне этажа; номера стоят
 * по порядку сцены (south — от торца west к east, north — обратно) и прижаты к концу
 * этого порядка, люкс с `windows: 2` занимает два зубца, если на стороне есть место.
 * Номера торцов — окнами на торце. Занятости нет: окно номера на продаваемом этаже —
 * светлое, на остальных — серо-голубое, без номера — тёмное.
 */
import { THREE, V3, FLOOR_H as H, UNIT_M, stdMat, extrude, labelSprite, numberSprite } from "./viewer_kit.js";
import { buildCrown, buildPodium, buildPlaza } from "./viewer_podium.js";

const C = {
  room: new THREE.Color("#f7f1e2"),
  group: new THREE.Color("#ffb81c"),
  sel: new THREE.Color("#ffffff"),
  nodata: new THREE.Color("#3d464c"),
  dim: new THREE.Color("#27303a"),
  nonsale: new THREE.Color("#6b8794"),
};
const SLAB = new THREE.Color("#7a736a");
const SLAB_DIM = new THREE.Color("#3b4247");
const GAP = 9;
const FADE = 0.14;
const tmpC = new THREE.Color();

export function buildTower(scene, { floorLabel, sideName }) {
  const b = scene.building;
  const L = b.length_m / UNIT_M;
  const HW = b.width_m / UNIT_M / 2;
  const rounded = !!b.rounded_ends && L - 2 * HW > 2;
  const S = rounded ? (L - 2 * HW) / 2 : L / 2;
  const T = b.facade_step ? 0.8 : 0;
  const TOP = b.floors_total * H;
  const floorsByNumber = new Map(scene.floors.map((f) => [f.number, f]));
  const bySide = (floor) => {
    const out = { north: [], south: [], east: [], west: [] };
    floor.rooms.forEach((r) => out[r.side].push(r));
    return out;
  };
  let TEETH = 1;
  for (const f of scene.floors) {
    const s = bySide(f);
    TEETH = Math.max(TEETH, s.north.length, s.south.length);
  }
  const Wt = (2 * S) / TEETH;

  const root = new THREE.Group();
  root.rotation.y = (-(b.orientation_deg - 90) * Math.PI) / 180;

  // Раскладка номеров: зубцы длинных сторон и окна торцов.
  const rooms = new Map();
  const floorRooms = new Map();
  for (const floor of scene.floors) {
    const s = bySide(floor);
    const list = [];
    for (const side of ["south", "north"]) {
      const arr = s[side];
      let extra = TEETH - arr.length;
      const spans = arr.map((r) => {
        const two = (scene.room_types[r.type_key] || {}).windows === 2 && extra > 0;
        if (two) extra -= 1;
        return two ? 2 : 1;
      });
      let k = TEETH - spans.reduce((sum, x) => sum + x, 0);
      const sgn = side === "south" ? 1 : -1;
      arr.forEach((r, i) => {
        const k0 = k;
        const k1 = k + spans[i] - 1;
        k += spans[i];
        const a = side === "south" ? k0 : TEETH - 1 - k1;
        const bb = side === "south" ? k1 : TEETH - 1 - k0;
        list.push({ number: r.number, floor: floor.number, side, a, b: bb, lx: -S + ((a + bb + 1) / 2) * Wt, lz: sgn * (HW + T), nx: 0, nz: sgn, end: false });
      });
    }
    for (const side of ["east", "west"]) {
      const arr = s[side];
      const sx = side === "east" ? 1 : -1;
      arr.forEach((r, i) => {
        const u = (i + 0.5) / arr.length;
        let lx;
        let lz;
        let nx;
        let nz;
        if (rounded) {
          const ang = THREE.MathUtils.degToRad(60 - 120 * u);
          nx = sx * Math.cos(ang);
          nz = sx * Math.sin(ang);
          lx = sx * S + nx * HW;
          lz = nz * HW;
        } else {
          nx = sx;
          nz = 0;
          lx = sx * S;
          lz = sx * HW * (1 - 2 * u);
        }
        list.push({ number: r.number, floor: floor.number, side, lx, lz, nx, nz, end: true, endIndex: i, endCount: arr.length, a: 0, b: 0 });
      });
    }
    list.forEach((e) => rooms.set(e.number, e));
    floorRooms.set(floor.number, list);
  }

  function towerShape(k = 1, teeth = true) {
    const s = new THREE.Shape();
    const hw = HW * k;
    const tt = teeth ? T * k : 0;
    const sx = rounded ? S : S + HW * (k - 1);
    s.moveTo(-sx, hw);
    if (tt) for (let i = 0; i < TEETH; i += 1) { const x0 = -S + i * Wt; s.lineTo(x0 + Wt / 2, hw + tt); s.lineTo(x0 + Wt, hw); }
    if (rounded) s.absarc(S, 0, hw, Math.PI / 2, -Math.PI / 2, true);
    else { s.lineTo(sx, hw); s.lineTo(sx, -hw); }
    if (tt) for (let i = 0; i < TEETH; i += 1) { const x0 = S - i * Wt; s.lineTo(x0 - Wt / 2, -hw - tt); s.lineTo(x0 - Wt, -hw); }
    if (rounded) s.absarc(-S, 0, hw, -Math.PI / 2, (-3 * Math.PI) / 2, true);
    else { s.lineTo(-sx, -hw); s.lineTo(-sx, hw); }
    return s;
  }

  // Окна: на зубец — две грани (или одна, если фасад ровный), на номер торца — одно.
  function toothFaces(sgn, j) {
    const x0 = -S + j * Wt;
    if (!T) return [{ cx: x0 + Wt / 2, cz: sgn * (HW + 0.04), rot: sgn > 0 ? 0 : Math.PI, len: Wt }];
    const p0 = new THREE.Vector2(x0, sgn * HW);
    const p1 = new THREE.Vector2(x0 + Wt / 2, sgn * (HW + T));
    const p2 = new THREE.Vector2(x0 + Wt, sgn * HW);
    return [[p0, p1, new THREE.Vector2(-T, (sgn * Wt) / 2)], [p1, p2, new THREE.Vector2(T, (sgn * Wt) / 2)]].map(([a, c, n]) => {
      n.normalize();
      return { cx: (a.x + c.x) / 2 + n.x * 0.04, cz: (a.y + c.y) / 2 + n.y * 0.04, rot: Math.atan2(n.x, n.y), len: a.distanceTo(c) };
    });
  }
  const slots = [];
  for (const [side, sgn] of [["south", 1], ["north", -1]]) {
    for (let j = 0; j < TEETH; j += 1) for (const fc of toothFaces(sgn, j)) slots.push({ ...fc, side, j });
  }
  const endWidth = (count) => Math.min(2.2, (rounded ? (HW * 2.1) / count : (2 * HW) / count) * 0.8);

  const slabGeo = extrude(towerShape(), H * 0.93);
  const plateGeo = extrude(towerShape(1.015), 0.12);
  const plateMat = stdMat("#ece6da", { roughness: 0.7 });
  const winGeo = new THREE.PlaneGeometry(1, 1);
  const floors = [];
  const pickables = [];
  const m4 = new THREE.Matrix4();
  const q = new THREE.Quaternion();
  const sc = new V3();
  const p = new V3();
  const up = new V3(0, 1, 0);
  for (let f = 1; f <= b.floors_total; f += 1) {
    const g = new THREE.Group();
    g.position.y = (f - 1) * H;
    const mat = stdMat("#7a736a", { roughness: 0.6, metalness: 0.15, emissive: new THREE.Color("#000000") });
    const slab = new THREE.Mesh(slabGeo, mat);
    slab.userData.floor = f;
    g.add(slab);
    pickables.push(slab);
    const plate = new THREE.Mesh(plateGeo, plateMat.clone());
    g.add(plate);
    const ends = (floorRooms.get(f) || []).filter((e) => e.end);
    const count = slots.length + ends.length;
    const inst = new THREE.InstancedMesh(winGeo, new THREE.MeshBasicMaterial({ side: THREE.DoubleSide, toneMapped: false }), count);
    inst.frustumCulled = false;
    inst.userData = { floor: f, slots: [] };
    const placeWindow = (k, cx, cz, rot, len) => {
      q.setFromAxisAngle(up, rot);
      p.set(cx, H * 0.52, cz);
      sc.set(len * 0.8, H * 0.62, 1);
      m4.compose(p, q, sc);
      inst.setMatrixAt(k, m4);
      inst.setColorAt(k, C.nodata);
    };
    slots.forEach((fc, k) => {
      placeWindow(k, fc.cx, fc.cz, fc.rot, fc.len);
      const room = (floorRooms.get(f) || []).find((e) => !e.end && e.side === fc.side && fc.j >= e.a && fc.j <= e.b);
      inst.userData.slots.push(room ? room.number : null);
    });
    ends.forEach((e, i) => {
      placeWindow(slots.length + i, e.lx + e.nx * 0.04, e.lz + e.nz * 0.04, Math.atan2(e.nx, e.nz), endWidth(e.endCount) / 0.8);
      inst.userData.slots.push(e.number);
    });
    g.add(inst);
    pickables.push(inst);
    g.userData = { f, slab, mat, inst, tiles: null, useLabel: null, baseY: (f - 1) * H, lift: 0, fade: 1, op: 1, mats: [mat, plate.material, inst.material] };
    root.add(g);
    floors.push(g);
  }

  // Корона, подиум и площадь — viewer_podium.js.
  const liftables = [...floors];
  if (b.crown) {
    const crown = buildCrown(towerShape, { S, HW, TOP, rounded });
    root.add(crown);
    liftables.push(crown);
  }
  const podium = b.podium_floors > 0 ? buildPodium(root, b.podium_floors, { S, HW, T }) : null;
  buildPlaza(root, { S, HW, T });

  // План раскрытого этажа: коридор, плитки номеров с номерами, подписи сторон.
  const tileGeo = new THREE.BoxGeometry(1, 1, 1);
  function buildTiles(g) {
    const grp = new THREE.Group();
    grp.position.y = H * 0.93 + 0.01;
    const corr = 1.2;
    grp.add(new THREE.Mesh(extrude(towerShape(0.985, false), 0.04), stdMat("#1a313b", { roughness: 0.9 })));
    const hall = new THREE.Mesh(tileGeo, stdMat("#5d6b70", { roughness: 0.8 }));
    hall.scale.set(2 * S + HW * 0.9, 0.08, 2 * corr - 0.5);
    hall.position.set(0, 0.08, 0);
    grp.add(hall);
    for (const r of floorRooms.get(g.userData.f) || []) {
      let x0;
      let x1;
      let z0;
      let z1;
      if (!r.end) {
        x0 = -S + r.a * Wt + 0.08;
        x1 = -S + (r.b + 1) * Wt - 0.08;
        z0 = r.side === "south" ? corr : -HW + 0.1;
        z1 = r.side === "south" ? HW - 0.1 : -corr;
      } else {
        const sx = r.side === "east" ? 1 : -1;
        const depth = Math.max(1.2, HW * 0.75);
        x0 = sx > 0 ? S + 0.1 : -S - depth;
        x1 = sx > 0 ? S + depth : -S - 0.1;
        const step = (2 * HW * 0.8) / r.endCount;
        const k = sx > 0 ? r.endIndex : r.endCount - 1 - r.endIndex;
        z1 = HW * 0.8 - k * step - 0.06;
        z0 = z1 - step + 0.12;
      }
      const m = new THREE.Mesh(tileGeo, stdMat("#ffffff", { roughness: 0.5, emissive: new THREE.Color("#000000") }));
      m.scale.set(x1 - x0, 0.22, z1 - z0);
      m.position.set((x0 + x1) / 2, 0.15, (z0 + z1) / 2);
      m.userData.room = r.number;
      grp.add(m);
      const label = numberSprite(r.number);
      label.position.set((x0 + x1) / 2, 0.75, (z0 + z1) / 2);
      grp.add(label);
    }
    for (const [side, sgn] of [["south", 1], ["north", -1]]) {
      const text = sideName(side);
      if (!text) continue;
      const sp = labelSprite(text, 1.25, { onTop: true });
      sp.position.set(0, 1.4, sgn * (HW + T + 1.8));
      grp.add(sp);
    }
    g.add(grp);
    g.userData.tiles = grp;
  }

  function roomColor(entry, st, out) {
    const floor = floorsByNumber.get(entry.floor);
    out.copy(st.group.has(entry.number) ? C.group : floor && floor.sellable ? C.room : C.nonsale);
    if (st.selFloor !== null && st.selFloor !== entry.floor) out.lerp(C.dim, 0.5);
    if (st.selRoom === entry.number) out.lerp(C.sel, 0.55);
    return out;
  }

  const api = {
    root,
    rooms,
    floors,
    pickables,
    floorRooms,
    dims: { S, HW, T, TOP, L, podium },
    recolor(st) {
      for (const g of floors) {
        const { inst, f } = g.userData;
        const dim = st.selFloor !== null && st.selFloor !== f;
        g.userData.mat.color.copy(dim ? SLAB_DIM : SLAB);
        const floor = floorsByNumber.get(f);
        inst.userData.slots.forEach((num, k) => {
          if (num) roomColor(rooms.get(num), st, tmpC);
          else {
            tmpC.copy(floor && floor.kind !== "rooms" ? C.nonsale : C.nodata);
            if (dim) tmpC.lerp(C.dim, 0.6);
          }
          inst.setColorAt(k, tmpC);
        });
        if (inst.instanceColor) inst.instanceColor.needsUpdate = true;
        if (g.userData.tiles) {
          for (const m of g.userData.tiles.children) {
            const num = m.userData.room;
            if (!num) continue;
            roomColor(rooms.get(num), st, m.material.color);
            m.material.emissive.set(st.selRoom === num ? "#444444" : "#000000");
            m.position.y = st.selRoom === num ? 0.45 : 0.15;
          }
        }
      }
    },
    setEmissive(f, color) {
      const g = floors[f - 1];
      if (g) g.userData.mat.emissive.set(color);
    },
    /** Раскрыть этаж f (этажи выше уходят вверх и тают) или собрать здание (null). */
    setLifts(f) {
      for (const g of liftables) {
        const above = f !== null && (g.userData.f === undefined || g.userData.f > f);
        g.userData.lift = above ? GAP : 0;
        g.userData.fade = above ? FADE : 1;
      }
      for (const g of floors) {
        if (g.userData.tiles) g.userData.tiles.visible = false;
        if (g.userData.useLabel) g.userData.useLabel.visible = false;
      }
      if (f === null) return;
      const g = floors[f - 1];
      const floor = floorsByNumber.get(f);
      if (floor && floor.rooms.length) {
        if (!g.userData.tiles) buildTiles(g);
        g.userData.tiles.visible = true;
      } else {
        if (!g.userData.useLabel) {
          g.userData.useLabel = labelSprite(floorLabel(f), 1.25, { onTop: true });
          g.userData.useLabel.position.set(0, H + 1.6, 0);
          g.add(g.userData.useLabel);
        }
        g.userData.useLabel.visible = true;
      }
    },
    /** Шаг анимации подъёма и прозрачности; true — ещё движется. */
    tick(k, snap) {
      let moving = false;
      for (const g of liftables) {
        const d = g.userData;
        const goal = d.baseY + d.lift;
        g.position.y = snap ? goal : g.position.y + (goal - g.position.y) * k;
        if (Math.abs(goal - g.position.y) < 0.01) g.position.y = goal;
        else moving = true;
        if (d.op !== d.fade) {
          d.op = snap ? d.fade : d.op + (d.fade - d.op) * k;
          if (Math.abs(d.op - d.fade) < 0.01) d.op = d.fade;
          else moving = true;
          for (const m of d.mats) {
            const transparent = d.op < 0.99;
            if (m.transparent !== transparent) { m.transparent = transparent; m.needsUpdate = true; }
            m.opacity = d.op;
            m.depthWrite = d.op > 0.6;
          }
        }
      }
      return moving;
    },
    /** Этаж и номер по попаданию луча. */
    hit(h) {
      const o = h.object;
      if (o.userData.room) return { floor: rooms.get(o.userData.room).floor, room: o.userData.room };
      if (o.isInstancedMesh) return { floor: o.userData.floor, room: o.userData.slots[h.instanceId] || null };
      const f = o.userData.floor;
      const lp = root.worldToLocal(h.point.clone());
      let room = null;
      if (Math.abs(lp.x) <= S && Math.abs(lp.z) > HW * 0.75) {
        const side = lp.z > 0 ? "south" : "north";
        const j = Math.min(TEETH - 1, Math.floor((lp.x + S) / Wt));
        const e = (floorRooms.get(f) || []).find((r) => !r.end && r.side === side && j >= r.a && j <= r.b);
        room = e ? e.number : null;
      }
      return { floor: f, room };
    },
    /** Точка перед окном номера в мире (с учётом подъёма этажа). */
    windowPoint(number, out = new V3(), offset = 0.9) {
      const e = rooms.get(number);
      const g = floors[e.floor - 1];
      out.set(e.lx + e.nx * offset, g.position.y + H * 0.52, e.lz + e.nz * offset);
      return root.localToWorld(out);
    },
    /** Мировой азимут, куда смотрят окна стороны. */
    sideAzimuth(side) {
      const local = { east: 0, south: 90, west: 180, north: 270 }[side];
      return (((local + b.orientation_deg) % 360) + 360) % 360;
    },
  };
  return api;
}
