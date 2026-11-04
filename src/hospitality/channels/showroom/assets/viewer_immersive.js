/* Вид из окна и номер изнутри в просмотрщике 3D-витрины — код прототипа #816.
 *
 * Режимы: "model" — здание, "flying" — перелёт, "window" — вид из окна номера (осмотр
 * перетаскиванием, рама поверх холста, ползунок этажа — setWindowFloor), "room" — схема
 * номера изнутри (viewer_room.js) с панорамой из его окна. Переход между сценами —
 * затемнение; при prefers-reduced-motion всё мгновенно. Плашку поверх вида рисует
 * страница по событию "mode" — {mode, room, floor}.
 */
import { V3, FLOOR_H as H, REDUCED, dirAz, overlay } from "./viewer_kit.js";

export function createImmersive(ctx) {
  const { camera, el, container, tower, env, rig, room, world, orbit, scene, st, typeOf, marker } = ctx;
  const frameEl = overlay(container, { display: "none", boxShadow: "inset 0 0 0 12px rgba(18,22,25,.94), inset 0 0 90px 30px rgba(0,0,0,.32)" });
  const veil = overlay(container, { background: "#07131a", opacity: "0", transition: "opacity .22s", zIndex: "10" });
  let mode = "model";
  let win = null;
  let roomShown = null;

  const fovFor = () => (mode === "window" ? (camera.aspect < 1 ? 76 : 56) : camera.aspect < 1 ? 52 : 40);
  function setNear(n) {
    camera.near = n;
    camera.updateProjectionMatrix();
  }
  function setMode(next) {
    mode = next;
    frameEl.style.display = next === "window" ? "" : "none";
    camera.fov = fovFor();
    camera.updateProjectionMatrix();
    ctx.markDirty();
    if (next === "flying") return;
    const shown = next === "window" ? win.room : next === "room" ? roomShown : null;
    const floor = next === "window" ? win.floor : shown ? tower.rooms.get(shown).floor : null;
    ctx.emit("mode", { mode: next, room: shown, floor });
  }
  function fade(fn) {
    if (REDUCED) { fn(); return; }
    veil.style.opacity = "1";
    setTimeout(() => {
      fn();
      requestAnimationFrame(() => requestAnimationFrame(() => { veil.style.opacity = "0"; }));
    }, 230);
  }
  /** Из окна или номера — к модели без перелёта; false — идёт перелёт, действие ждёт. */
  function leave() {
    if (mode === "window") {
      win = null;
      setNear(1);
      orbit.state.enabled = true;
      setMode("model");
    } else if (mode === "room") {
      room.setActive(false);
      roomShown = null;
      orbit.state.enabled = true;
      setMode("model");
    }
    return mode !== "flying";
  }
  function enterWindowDirect(entry) {
    ctx.collapse(true);
    win = rig.windowState(entry);
    setNear(0.15);
    orbit.state.enabled = false;
    rig.applyWindow(win, 1, true);
    setMode("window");
  }
  function openWindowFlight(entry) {
    ctx.finishAnim();
    ctx.collapse(false);
    ctx.stopAuto();
    orbit.state.autoRotate = false;
    win = rig.windowState(entry);
    const end = rig.windowPos(win);
    const outDir = dirAz(win.az, rig.windowPitch(win.floor));
    const approach = end.clone().add(dirAz(tower.sideAzimuth(entry.side)).multiplyScalar(20)).add(new V3(0, 2, 0));
    const p0 = camera.position.clone();
    const t0 = orbit.target.clone();
    const look = new V3();
    orbit.stop();
    orbit.state.enabled = false;
    setMode("flying");
    ctx.setAnim({
      t: 0,
      dur: REDUCED ? 0 : 0.9,
      update(e) { ctx.cylLerp(p0, approach, e, camera.position); look.lerpVectors(t0, end, e); camera.lookAt(look); },
      done() {
        setNear(0.15);
        const inDir = end.clone().sub(approach).normalize();
        const yIn = Math.atan2(inDir.x, inDir.z);
        const pIn = Math.asin(inDir.y);
        const pOut = Math.asin(outDir.y);
        let dy = Math.atan2(outDir.x, outDir.z) - yIn;
        while (dy > Math.PI) dy -= 2 * Math.PI;
        while (dy < -Math.PI) dy += 2 * Math.PI;
        ctx.setAnim({
          t: 0,
          dur: REDUCED ? 0 : 0.8,
          update(e) {
            camera.position.lerpVectors(approach, end, e);
            const yw = yIn + dy * e;
            const pp = pIn + (pOut - pIn) * e;
            look.set(Math.sin(yw) * Math.cos(pp), Math.sin(pp), Math.cos(yw) * Math.cos(pp)).add(camera.position);
            camera.lookAt(look);
          },
          done() { win.y = end.y; setMode("window"); },
        });
      },
    });
  }
  function renderPanorama(entry) {
    const w = rig.windowState(entry);
    const pos = rig.windowPos(w);
    marker.visible = false;
    env.labels.forEach((l) => { l.visible = false; });
    tower.root.visible = false;
    env.sky.position.copy(pos);
    room.panorama(world, pos, w.az);
    env.labels.forEach((l) => { l.visible = true; });
    tower.root.visible = true;
  }

  // Осмотр из окна: перетаскивание поворачивает взгляд в пределах рамы.
  let drag = null;
  el.addEventListener("pointerdown", (e) => {
    if (mode === "window") drag = { id: e.pointerId, x: e.clientX, y: e.clientY, yaw: win.yaw, pitch: win.pitch };
  });
  el.addEventListener("pointermove", (e) => {
    if (mode !== "window" || !drag || drag.id !== e.pointerId) return;
    win.yaw = Math.max(-60, Math.min(60, drag.yaw - (e.clientX - drag.x) * 0.0042 * 57.3));
    win.pitch = Math.max(-0.55, Math.min(0.4, drag.pitch + (e.clientY - drag.y) * 0.0042));
    ctx.markDirty();
  });
  const endDrag = () => { drag = null; };
  el.addEventListener("pointerup", endDrag);
  el.addEventListener("pointercancel", endDrag);

  return {
    mode: () => mode,
    windowInfo: () => (win ? { room: win.room, floor: win.floor } : null),
    fovFor,
    leave,
    /** Шаг кадра в окне или номере; true — ещё движется. */
    step(dt, k2) {
      if (mode === "window") return rig.applyWindow(win, k2, REDUCED);
      if (mode === "room") return room.update(dt);
      return false;
    },
    openWindow(number) {
      const entry = tower.rooms.get(String(number));
      if (!entry || mode === "flying") return false;
      if (mode === "room") fade(() => { leave(); enterWindowDirect(entry); });
      else if (mode === "window") enterWindowDirect(entry);
      else openWindowFlight(entry);
      return true;
    },
    setWindowFloor(n) {
      const f = Number(n);
      if (mode !== "window" || !Number.isInteger(f) || f < 1 || f > scene.building.floors_total) return false;
      rig.windowMove(win, f);
      setMode("window");
      return true;
    },
    openRoom(number) {
      const entry = tower.rooms.get(String(number));
      if (!entry || mode === "flying") return false;
      fade(() => {
        win = null;
        ctx.finishAnim();
        ctx.collapse(true);
        room.build((scene.room_types[typeOf.get(entry.number)] || {}).windows === 2);
        renderPanorama(entry);
        roomShown = entry.number;
        st.selRoom = entry.number;
        ctx.recolor();
        orbit.state.enabled = false;
        setNear(1);
        room.setActive(true);
        setMode("room");
      });
      return true;
    },
    backToBuilding() {
      if (mode === "window") {
        const p = rig.facadePose({ ...win.entry, floor: win.floor }, (win.floor - 1) * H + H / 2, 82 * rig.k);
        if (win.room) st.selRoom = win.room;
        const back = st.selRoom;
        setNear(1);
        setMode("flying");
        const p0 = camera.position.clone();
        const dir = new V3();
        camera.getWorldDirection(dir);
        const t0 = p0.clone().add(dir.multiplyScalar(30));
        const look = new V3();
        win = null;
        ctx.setAnim({
          t: 0,
          dur: REDUCED ? 0 : 1.0,
          update(e) { ctx.cylLerp(p0, p.pos, e, camera.position); look.lerpVectors(t0, p.target, e); camera.lookAt(look); },
          done() { orbit.target.copy(p.target); orbit.state.enabled = true; setMode("model"); ctx.recolor(); ctx.emit("select", back); },
        });
        return true;
      }
      if (mode === "room") {
        const entry = tower.rooms.get(roomShown);
        fade(() => {
          const p = rig.roomPose(entry);
          room.setActive(false);
          roomShown = null;
          st.selRoom = entry.number;
          camera.position.copy(p.pos);
          orbit.target.copy(p.target);
          camera.lookAt(p.target);
          orbit.state.enabled = true;
          setMode("model");
          ctx.recolor();
          ctx.emit("select", entry.number);
        });
        return true;
      }
      return false;
    },
  };
}
