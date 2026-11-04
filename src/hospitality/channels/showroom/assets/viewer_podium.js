/* Корона, подиум и площадь башни в просмотрщике 3D-витрины — код прототипа #816.
 *
 * Все три — в локальных осях корпуса (viewer_tower.js): x — длинная ось, z — поперёк.
 * Размеры — от корпуса сцены: S — полудлина прямой части, HW — полуширина, T — зубец.
 */
import { THREE, V3, FLOOR_H as H, stdMat, extrude, roundedRect } from "./viewer_kit.js";

/** Корона на крыше: светящийся пояс и золотые лопасти по периметру. */
export function buildCrown(towerShape, { S, HW, TOP, rounded }) {
  const crown = new THREE.Group();
  crown.position.y = TOP;
  crown.add(new THREE.Mesh(extrude(towerShape(1.1, false), 1.5), stdMat("#cdbf9f", { roughness: 0.6, emissive: new THREE.Color("#2a1d00") })));
  const band = new THREE.Mesh(extrude(towerShape(1.12, false), 0.35), new THREE.MeshBasicMaterial({ color: "#ffd98a", toneMapped: false }));
  band.position.y = 0.6;
  crown.add(band);
  const finGeo = new THREE.ConeGeometry(0.9, 7.5, 4);
  finGeo.translate(0, 3.75, 0);
  const finMat = stdMat("#e2c27a", { emissive: new THREE.Color("#7a5810"), emissiveIntensity: 0.5, metalness: 0.45, roughness: 0.45 });
  const R = HW * 1.1;
  const ex = rounded ? S : S + HW * 0.1;
  const per = rounded ? 4 * S + 2 * Math.PI * R : 4 * ex + 4 * R;
  const N = Math.max(12, Math.round(per / 1.78));
  const fins = new THREE.InstancedMesh(finGeo, finMat, N);
  const d0 = new THREE.Object3D();
  for (let i = 0; i < N; i += 1) {
    let d = (i / N) * per;
    let x;
    let z;
    let nx;
    let nz;
    if (rounded) {
      if (d < 2 * S) { x = -S + d; z = R; nx = 0; nz = 1; }
      else if ((d -= 2 * S) < Math.PI * R) { const a = Math.PI / 2 - d / R; x = S + R * Math.cos(a); z = R * Math.sin(a); nx = Math.cos(a); nz = Math.sin(a); }
      else if ((d -= Math.PI * R) < 2 * S) { x = S - d; z = -R; nx = 0; nz = -1; }
      else { d -= 2 * S; const a = -Math.PI / 2 - d / R; x = -S + R * Math.cos(a); z = R * Math.sin(a); nx = Math.cos(a); nz = Math.sin(a); }
    } else if (d < 2 * ex) { x = -ex + d; z = R; nx = 0; nz = 1; }
    else if ((d -= 2 * ex) < 2 * R) { x = ex; z = R - d; nx = 1; nz = 0; }
    else if ((d -= 2 * R) < 2 * ex) { x = ex - d; z = -R; nx = 0; nz = -1; }
    else { d -= 2 * ex; x = -ex; z = -R + d; nx = -1; nz = 0; }
    d0.position.set(x, 1.5, z);
    d0.rotation.set(0, Math.atan2(nx, nz), 0);
    d0.rotateX(0.2);
    d0.scale.set(1, 1, 0.28);
    d0.updateMatrix();
    fins.setMatrixAt(i, d0.matrix);
  }
  fins.frustumCulled = false;
  crown.add(fins);
  crown.userData = { baseY: TOP, lift: 0, fade: 1, op: 1, mats: [] };
  crown.traverse((o) => { if (o.material) crown.userData.mats.push(o.material); });
  return crown;
}

/** Подиум с ресторанами: стекло по периметру, золотой кант, крыша с газонами. */
export function buildPodium(root, floorsCount, { S, HW, T }) {
  const ph = floorsCount * H * 0.97;
  const px = S + HW + 10.6;
  const pz = HW + T + 9.7;
  const shape = roundedRect(px, pz, 5);
  root.add(new THREE.Mesh(extrude(shape, ph), stdMat("#d3c9b8", { roughness: 0.85 })));
  const glassMat = stdMat("#5b6a70", { roughness: 0.3, metalness: 0.3, emissive: new THREE.Color("#8a5a22"), emissiveIntensity: 0.45 });
  const glass = new THREE.Mesh(extrude(shape, Math.min(3.2, ph * 0.46)), [new THREE.MeshBasicMaterial({ visible: false }), glassMat]);
  glass.scale.set(1.006, 1, 1.008);
  glass.position.y = ph * 0.2;
  root.add(glass);
  const edge = shape.getPoints(12).map((pt) => new V3(pt.x, ph + 0.05, pt.y));
  root.add(new THREE.LineLoop(new THREE.BufferGeometry().setFromPoints(edge), new THREE.LineBasicMaterial({ color: "#b8860b" })));
  const roofBox = new THREE.BoxGeometry(1, 1, 1).translate(0, 0.5, 0);
  const lawn = stdMat("#7f9a5a");
  const hvac = stdMat("#9aa0a3", { roughness: 0.7 });
  const skylight = stdMat("#a9c3cf", { roughness: 0.25, metalness: 0.2 });
  const kx = px / 24;
  const kz = pz / 16.5;
  for (const [x, z, w, d, m, h] of [[-19, 9, 8, 9, lawn, 0.25], [-19, -9, 8, 9, lawn, 0.25], [19, 9.5, 7, 8, lawn, 0.25], [19, -10, 4, 3, hvac, 1.4], [16, -12.5, 3, 2.4, hvac, 1.1],
    [-16.5, 0, 3, 5, skylight, 0.7], [16.5, 0, 3, 5, skylight, 0.7], [0, 12, 10, 3, hvac, 1.2], [0, -12.5, 8, 2.4, lawn, 0.25]]) {
    const box = new THREE.Mesh(roofBox, m);
    box.scale.set(w * kx, h, d * kz);
    box.position.set(x * kx, ph, z * kz);
    // Под башней крыши подиума не видно — то, что легло бы на корпус, не ставим.
    if (Math.abs(box.position.x) - (w * kx) / 2 < S + HW + 0.5 && Math.abs(box.position.z) - (d * kz) / 2 < HW + T + 0.5) continue;
    root.add(box);
  }
  return { hx: px, hz: pz, h: ph };
}

/** Площадь перед отелем. */
export function buildPlaza(root, { S, HW, T }) {
  const plaza = new THREE.Mesh(
    new THREE.ShapeGeometry(roundedRect(S + HW + 20.6, HW + T + 22.2, 8), 6).rotateX(Math.PI / 2),
    stdMat("#d8cfbf", { side: THREE.DoubleSide, polygonOffset: true, polygonOffsetFactor: -2, polygonOffsetUnits: -4 })
  );
  plaza.position.y = 0.12;
  root.add(plaza);
}
