/* Ракурсы и позы камеры просмотрщика 3D-витрины — числа прототипа #816.
 *
 * Прототип строил ракурсы для башни в 26 этажей и хребта вокруг 151°: здесь те же числа,
 * повёрнутые на азимут хребта сцены и отмасштабированные по высоте и длине корпуса.
 * Вид из окна: окно смотрит по нормали стороны, а номер с собственными видами
 * (`room.view_keys` с азимутом) — к ним, не дальше 50° от нормали.
 */
import { THREE, V3, FLOOR_H as H, dirAz, relAz } from "./viewer_kit.js";

const PROTO_RIDGE_AZ = 151;

export function createCameraRig(scene, tower, env, camera) {
  const { S, HW, T, TOP, podium } = tower.dims;
  // Масштаб ракурсов: башня прототипа — 26 этажей и 26,8 единицы в длину.
  const k = Math.max(TOP / 46.8, ((2 * (S + HW)) / 26.8) * 0.8, 0.45);
  const ridgeAz = env.ridgeAz ?? PROTO_RIDGE_AZ;
  const rel = (deg) => ridgeAz - PROTO_RIDGE_AZ + deg;
  const flat = (az, len) => dirAz(az).multiplyScalar(len);
  const orbitPose = (target, az, dist, elev) => ({ pos: target.clone().add(dirAz(az, elev).multiplyScalar(dist)), target });
  const phone = () => camera.aspect < 1;
  const presets = {
    home: () => ({ pos: flat(rel(300), 140 * k).setY(40 * k), target: new V3(0, 31 * k, 0) }),
    mount: () => orbitPose(flat(rel(90), 20 * k).setY(44 * k), rel(342), (phone() ? 235 : 190) * k, 0.1),
    city: () => orbitPose(flat(rel(45), 8.5 * k).setY(26 * k), rel(205), (phone() ? 215 : 165) * k, 0.2),
    top: () => orbitPose(flat(rel(155.8), 85.5 * k).setY(6 * k), rel(225), (phone() ? 380 : 300) * k, 1.3),
    street: () => ({ pos: flat(rel(250), S + HW + 46).setY(2.6), target: new V3(0, TOP * 0.42, 0) }),
  };

  // Камера не уходит под землю, дальше окрестностей, внутрь башни и подиума.
  const local = new V3();
  function clamp(target, p) {
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

  /** Камера перед фасадом номера: чуть сбоку, так за башней видны окрестности. */
  function facadePose(entry, y, dist) {
    const ty = y * 0.65 + 27 * k * 0.35;
    const target = tower.root.localToWorld(new V3(entry.lx * 0.4, ty, entry.end ? entry.lz * 0.4 : Math.sign(entry.lz) * 2));
    const face = tower.sideAzimuth(entry.side);
    const az = entry.end ? face : entry.side === "south" ? face - 28 : face + 28;
    const pos = target.clone().add(dirAz(az).multiplyScalar(dist));
    pos.y = ty + dist * 0.1;
    return { pos, target };
  }
  const roomPose = (entry) => facadePose(entry, (entry.floor - 1) * H + H / 2, 82 * k);

  /** Камера над раскрытым этажом — с того же азимута, что сейчас. */
  function floorPose(f, target0) {
    const target = new V3(0, (f - 1) * H + H, 0);
    const sph = new THREE.Spherical().setFromVector3(camera.position.clone().sub(target0));
    sph.radius = (phone() ? 58 : 42) * Math.max(1, ((S + HW) / 13.4) * 0.9);
    sph.phi = 0.7;
    return { pos: new V3().setFromSpherical(sph).add(target), target };
  }

  // Вид из окна.
  const sceneRooms = new Map();
  for (const f of scene.floors) for (const r of f.rooms) sceneRooms.set(r.number, r);
  function windowAz(entry) {
    const face = tower.sideAzimuth(entry.side);
    const room = sceneRooms.get(entry.number);
    const azimuths = (room ? room.view_keys : []).map((key) => (scene.views[key] || {}).azimuth_deg).filter((a) => a !== null && a !== undefined);
    if (!azimuths.length) return face;
    const x = azimuths.reduce((s, a) => s + Math.sin((a * Math.PI) / 180), 0);
    const z = azimuths.reduce((s, a) => s + Math.cos((a * Math.PI) / 180), 0);
    const mean = (Math.atan2(x, z) * 180) / Math.PI;
    return face + Math.max(-50, Math.min(50, relAz(mean, face)));
  }
  const windowPitch = (floor) => (floor <= 7 ? 0.02 : -0.06);
  function windowState(entry) {
    return { room: entry.number, entry, floor: entry.floor, az: windowAz(entry), yaw: 0, pitch: 0, y: (entry.floor - 1) * H + H * 0.62 };
  }
  function windowPos(win, out = new V3()) {
    const e = win.entry;
    out.set(e.lx + e.nx * 0.35, win.y, e.lz + e.nz * 0.35);
    return tower.root.localToWorld(out);
  }
  /** Тот же столбец окон на другом этаже: номер там или null. */
  function windowMove(win, floor) {
    win.floor = floor;
    const e = win.entry;
    const there = (tower.floorRooms.get(floor) || []).find((r) =>
      r.side === e.side && (e.end ? r.end && r.endIndex === e.endIndex && r.endCount === e.endCount : !r.end && r.a <= e.a && e.a <= r.b));
    win.room = there ? there.number : null;
    if (there) win.az = windowAz(there);
  }
  const look = new V3();
  function applyWindow(win, k2, snap) {
    const ty = (win.floor - 1) * H + H * 0.62;
    win.y = snap ? ty : win.y + (ty - win.y) * Math.min(1, k2 * 0.7);
    windowPos(win, camera.position);
    look.copy(dirAz(win.az + win.yaw, windowPitch(win.floor) + win.pitch)).add(camera.position);
    camera.lookAt(look);
    return Math.abs(ty - win.y) > 0.005;
  }

  return { k, presets, clamp, facadePose, roomPose, floorPose, windowState, windowPos, windowMove, windowPitch, applyWindow };
}
