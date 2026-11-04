/* Камера просмотрщика 3D-витрины: орбита вокруг цели, как OrbitControls прототипа #816.
 *
 * Перетаскивание — поворот, правая кнопка или Shift — сдвиг, колесо и щипок — приближение
 * к точке под курсором, два пальца — сдвиг; инерция и медленное вращение в покое. Своя, а не
 * OrbitControls из three: тот импортирует 'three' голым именем, а карту импорта CSP страницы
 * не пустит. Касание без сдвига — onTap, движение мыши без кнопок — onHover.
 */
import { THREE, V3 } from "./viewer_kit.js";

const DAMPING = 0.05;
const AUTO_SPEED = 0.4;

export function createOrbit(camera, el, { minDistance, maxDistance, maxPolar, minPolar = 0.05, pan: canPan = true, autoSpeed = AUTO_SPEED, clamp, pickPoint, onStart, onTap, onHover }) {
  const limits = { minDistance, maxDistance, minPolar, maxPolar };
  const target = new V3();
  const spherical = new THREE.Spherical();
  const delta = { theta: 0, phi: 0 };
  const panOffset = new V3();
  const offset = new V3();
  const v = new V3();
  const state = { enabled: true, autoRotate: false };

  function pan(dx, dy) {
    offset.copy(camera.position).sub(target);
    const distance = offset.length() * Math.tan(((camera.fov / 2) * Math.PI) / 180);
    const h = el.clientHeight || 1;
    v.setFromMatrixColumn(camera.matrix, 0).multiplyScalar((-2 * dx * distance) / h);
    panOffset.add(v);
    v.setFromMatrixColumn(camera.matrix, 1).multiplyScalar((2 * dy * distance) / h);
    panOffset.add(v);
  }

  /** Приближение в k раз к точке под (cx, cy): она остаётся на месте экрана. */
  function zoomAt(cx, cy, k) {
    const dist = camera.position.distanceTo(target);
    const next = Math.min(limits.maxDistance, Math.max(limits.minDistance, dist * k));
    const ratio = next / dist;
    if (Math.abs(ratio - 1) < 1e-4) return;
    const point = (canPan && pickPoint(cx, cy)) || target.clone();
    camera.position.sub(point).multiplyScalar(ratio).add(point);
    target.sub(point).multiplyScalar(ratio).add(point);
  }

  function update(dt) {
    if (!state.enabled) return false;
    offset.copy(camera.position).sub(target);
    spherical.setFromVector3(offset);
    if (state.autoRotate) spherical.theta -= ((2 * Math.PI) / 60) * autoSpeed * dt;
    spherical.theta += delta.theta * DAMPING;
    spherical.phi = Math.min(limits.maxPolar, Math.max(limits.minPolar, spherical.phi + delta.phi * DAMPING));
    spherical.radius = Math.min(limits.maxDistance, Math.max(limits.minDistance, spherical.radius));
    target.addScaledVector(panOffset, DAMPING);
    offset.setFromSpherical(spherical);
    camera.position.copy(target).add(offset);
    if (clamp) clamp(target, camera.position);
    camera.lookAt(target);
    delta.theta *= 1 - DAMPING;
    delta.phi *= 1 - DAMPING;
    panOffset.multiplyScalar(1 - DAMPING);
    return state.autoRotate || Math.abs(delta.theta) + Math.abs(delta.phi) > 1e-5 || panOffset.lengthSq() > 1e-6;
  }

  const pointers = new Map();
  let down = null;
  let pinch = null;
  el.addEventListener("contextmenu", (e) => e.preventDefault());
  el.addEventListener("pointerdown", (e) => {
    try { el.setPointerCapture(e.pointerId); } catch (error) { /* не критично */ }
    pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (onStart) onStart();
    if (pointers.size === 1) down = { x: e.clientX, y: e.clientY, id: e.pointerId, pan: canPan && (e.button === 2 || e.shiftKey), moved: e.button !== 0 };
    else if (down) down.moved = true;
    pinch = null;
  });
  el.addEventListener("pointermove", (e) => {
    const last = pointers.get(e.pointerId);
    if (!last) {
      if (e.pointerType === "mouse" && !e.buttons && state.enabled && onHover) onHover(e);
      return;
    }
    const dx = e.clientX - last.x;
    const dy = e.clientY - last.y;
    pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (!state.enabled) return;
    if (down && Math.hypot(e.clientX - down.x, e.clientY - down.y) > 6) down.moved = true;
    if (pointers.size >= 2) {
      const [a, c] = [...pointers.values()];
      const mid = { x: (a.x + c.x) / 2, y: (a.y + c.y) / 2 };
      const distance = Math.hypot(a.x - c.x, a.y - c.y);
      if (pinch) {
        if (distance > 0) zoomAt(mid.x, mid.y, pinch.distance / distance);
        if (canPan) pan(mid.x - pinch.mid.x, mid.y - pinch.mid.y);
      }
      pinch = { distance, mid };
      return;
    }
    const h = el.clientHeight || 1;
    if (down && down.pan) pan(dx, dy);
    else {
      delta.theta -= (2 * Math.PI * dx) / h;
      delta.phi -= (2 * Math.PI * dy) / h;
    }
  });
  const release = (e) => {
    pointers.delete(e.pointerId);
    if (pointers.size < 2) pinch = null;
    if (!down || e.pointerId !== down.id) return;
    const tap = !down.moved && e.type === "pointerup";
    down = null;
    if (tap && state.enabled && onTap) onTap(e);
  };
  el.addEventListener("pointerup", release);
  el.addEventListener("pointercancel", release);
  el.addEventListener("pointerleave", (e) => { if (e.pointerType === "mouse" && !pointers.size && onHover) onHover(null); });
  el.addEventListener(
    "wheel",
    (e) => {
      e.preventDefault();
      if (!state.enabled) return;
      if (onStart) onStart();
      const dy = e.deltaY * (e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? 400 : 1);
      zoomAt(e.clientX, e.clientY, Math.min(1.6, Math.max(0.6, Math.exp(dy * (e.ctrlKey ? 0.012 : 0.0016)))));
    },
    { passive: false }
  );

  return {
    target,
    state,
    limits,
    update,
    zoomAt,
    stop() {
      delta.theta = 0;
      delta.phi = 0;
      panOffset.set(0, 0, 0);
    },
  };
}
