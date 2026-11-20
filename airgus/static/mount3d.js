// 3D view of the rig: Sky-Watcher Explorer 150P (150/750 Newtonian) on an EQ6-R Pro,
// driven by hour angle, declination, pier side and site latitude (northern hemisphere).
// Own simplified model; dimensions in metres, taken from the manufacturers' specifications.
//
// Scene frame: x = east, y = up, z = south (north = -z).
// Pier side follows PHD2/ASCOM: "West" = telescope west of the pier
// (counterweight down while the target is east of the meridian).

import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";

const UP = new THREE.Vector3(0, 1, 0);
const DEG = Math.PI / 180;

// --- rig dimensions ------------------------------------------------------------
const OTA = {
  radius: 0.0925,      // tube outer diameter ~185 mm
  length: 0.70,        // tube length ~700 mm (750 mm focal length, f/5)
  focuserFromFront: 0.13,
  ringsAt: [-0.14, 0.12],
};
const HEAD_Y = 0.98;   // tripod head height (legs half extended)

const COLORS = {
  tubeWhite: 0xf1f2f4, black: 0x1d1f24, body: 0x2b2f36, bodyLight: 0x3b414b,
  silver: 0xc4c9d1, chrome: 0xdde1e7, camRed: 0xb3261e, warn: 0xe5484d,
};

function material(color, { metal = 0.25, rough = 0.55 } = {}) {
  return new THREE.MeshStandardMaterial({ color, roughness: rough, metalness: metal });
}

function cylinder(radius, length, color, { segments = 32, metal, rough, top } = {}) {
  const mesh = new THREE.Mesh(
    new THREE.CylinderGeometry(top ?? radius, radius, length, segments),
    material(color, { metal, rough }),
  );
  mesh.castShadow = true;
  return mesh;
}

function box(x, y, z, color, opts) {
  const mesh = new THREE.Mesh(new THREE.BoxGeometry(x, y, z), material(color, opts));
  mesh.castShadow = true;
  return mesh;
}

// Place a y-axis-aligned mesh at `center`, its axis along `dir`.
function place(mesh, center, dir) {
  mesh.position.copy(center);
  mesh.quaternion.setFromUnitVectors(UP, dir.clone().normalize());
}

// Mesh positioned in a group's local frame, its axis along a local axis ("x" | "y" | "z").
function local(group, mesh, [x, y, z], axis = "y") {
  mesh.position.set(x, y, z);
  if (axis === "x") mesh.rotation.z = -Math.PI / 2;
  if (axis === "z") mesh.rotation.x = Math.PI / 2;
  group.add(mesh);
  return mesh;
}

function label(text, color = "#8b93a6") {
  const c = document.createElement("canvas");
  c.width = c.height = 64;
  const ctx = c.getContext("2d");
  ctx.fillStyle = color;
  ctx.font = "bold 40px system-ui, sans-serif";
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillText(text, 32, 34);
  const sprite = new THREE.Sprite(new THREE.SpriteMaterial({ map: new THREE.CanvasTexture(c), depthTest: false }));
  sprite.scale.set(0.22, 0.22, 0.22);
  return sprite;
}

export class MountView {
  constructor(el) {
    this.el = el;
    this.counterweightUp = false;
    this.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
    this.renderer.setPixelRatio(window.devicePixelRatio || 1);
    this.renderer.shadowMap.enabled = true;
    el.appendChild(this.renderer.domElement);

    this.scene = new THREE.Scene();
    this.camera = new THREE.PerspectiveCamera(38, 1, 0.05, 50);
    this.camera.position.set(1.5, 1.75, 1.85); // from south-east
    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.target.set(0, 1.18, 0);
    this.controls.enableDamping = true;
    this.controls.maxPolarAngle = Math.PI * 0.49;
    this.controls.minDistance = 0.8;
    this.controls.maxDistance = 7;

    this.scene.add(new THREE.HemisphereLight(0xffffff, 0x445066, 1.1));
    const sun = new THREE.DirectionalLight(0xffffff, 1.5);
    sun.position.set(2.5, 5, 3);
    sun.castShadow = true;
    sun.shadow.mapSize.set(1024, 1024);
    this.scene.add(sun);

    this._buildGround();
    this._buildTripod();
    this._buildMount();
    this._buildOta();
    this.update({ ha: null, dec: null, pier: null, lat: 51 });

    new ResizeObserver(() => this._resize()).observe(el);
    this._resize();
    const loop = () => {
      this.controls.update();
      this.renderer.render(this.scene, this.camera);
      requestAnimationFrame(loop);
    };
    loop();
  }

  _buildGround() {
    const ground = new THREE.Mesh(
      new THREE.CircleGeometry(1.9, 64),
      new THREE.MeshStandardMaterial({ color: 0x808a99, transparent: true, opacity: 0.25 }),
    );
    ground.rotation.x = -Math.PI / 2;
    ground.receiveShadow = true;
    this.scene.add(ground);
    const ring = new THREE.Mesh(new THREE.RingGeometry(1.88, 1.9, 64),
      new THREE.MeshBasicMaterial({ color: 0x8b93a6, side: THREE.DoubleSide }));
    ring.rotation.x = -Math.PI / 2;
    ring.position.y = 0.002;
    this.scene.add(ring);
    for (const [t, x, z, color] of [["N", 0, -1.7, "#e5484d"], ["S", 0, 1.7], ["O", 1.7, 0], ["W", -1.7, 0]]) {
      const s = label(t, color);
      s.position.set(x, 0.1, z);
      this.scene.add(s);
    }
  }

  // EQ6-R steel tripod: 2" stainless legs with extensions, accessory tray, north leg first.
  _buildTripod() {
    const g = new THREE.Group();
    const head = cylinder(0.105, 0.07, COLORS.body, { metal: 0.4 });
    head.position.set(0, HEAD_Y, 0);
    g.add(head);
    for (const az of [0, 120, 240]) {
      const a = az * DEG;
      const top = new THREE.Vector3(Math.sin(a) * 0.09, HEAD_Y - 0.03, -Math.cos(a) * 0.09);
      const foot = new THREE.Vector3(Math.sin(a) * 0.55, 0, -Math.cos(a) * 0.55);
      const dir = top.clone().sub(foot);
      const knee = foot.clone().addScaledVector(dir, 0.42);
      const upper = cylinder(0.025, top.distanceTo(knee), COLORS.silver, { segments: 16, metal: 0.7, rough: 0.35 });
      place(upper, top.clone().add(knee).multiplyScalar(0.5), dir);
      const lower = cylinder(0.019, knee.distanceTo(foot) + 0.06, COLORS.silver, { segments: 16, metal: 0.7, rough: 0.3 });
      place(lower, knee.clone().add(foot).multiplyScalar(0.5), dir);
      const clamp = cylinder(0.031, 0.05, COLORS.black, { segments: 16 });
      place(clamp, knee, dir);
      const shoe = cylinder(0.03, 0.03, COLORS.black, { segments: 16 });
      shoe.position.copy(foot).setY(0.015);
      g.add(upper, lower, clamp, shoe);
    }
    // accessory tray on the leg spreader
    const tray = cylinder(0.19, 0.012, COLORS.black, { segments: 3 });
    tray.position.set(0, 0.5, 0);
    const shaft = cylinder(0.012, HEAD_Y - 0.5, COLORS.silver, { segments: 12, metal: 0.7 });
    shaft.position.set(0, (HEAD_Y + 0.5) / 2, 0);
    g.add(tray, shaft);
    this.scene.add(g);
  }

  _buildMount() {
    const body = { metal: 0.35, rough: 0.5 };
    // azimuth base and latitude block (fixed)
    this.azBase = cylinder(0.095, 0.07, COLORS.body, body);
    this.azBase.position.set(0, HEAD_Y + 0.07, 0);
    this.altBlock = box(0.15, 0.1, 0.16, COLORS.body, body);
    this.altBlock.position.set(0, HEAD_Y + 0.14, 0);
    this.azKnobs = [-1, 1].map((s) => {
      const k = cylinder(0.012, 0.09, COLORS.silver, { segments: 12, metal: 0.7 });
      k.position.set(s * 0.06, HEAD_Y + 0.08, -0.07);
      k.rotation.x = Math.PI / 2;
      return k;
    });
    // RA axis: housing with polar-scope cap at the lower end
    this.raHousing = cylinder(0.072, 0.30, COLORS.body, body);
    this.raMotor = box(0.1, 0.14, 0.09, COLORS.bodyLight, body);
    this.polarCap = cylinder(0.03, 0.04, COLORS.silver, { metal: 0.7, segments: 16 });
    // Dec axis: housing, clutch, saddle (Vixen/Losmandy dual), counterweight shaft and 2 x 5 kg
    this.decHousing = cylinder(0.066, 0.28, COLORS.body, body);
    this.decClutch = cylinder(0.072, 0.03, COLORS.silver, { metal: 0.7, rough: 0.3 });
    this.saddle = box(0.035, 0.2, 0.1, COLORS.black, { metal: 0.3 });
    this.saddleKnob = cylinder(0.012, 0.05, COLORS.silver, { metal: 0.7, segments: 12 });
    this.cwShaft = cylinder(0.01, 0.46, COLORS.chrome, { metal: 0.9, rough: 0.2, segments: 16 });
    this.cwStop = cylinder(0.022, 0.02, COLORS.silver, { metal: 0.8, segments: 16 });
    this.cws = [0, 1].map(() => cylinder(0.065, 0.052, COLORS.chrome, { metal: 0.9, rough: 0.2 }));
    this.ray = new THREE.Line(new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(), new THREE.Vector3()]),
      new THREE.LineDashedMaterial({ color: 0x5b8cff, dashSize: 0.06, gapSize: 0.05 }));
    for (const m of [this.azBase, this.altBlock, ...this.azKnobs, this.raHousing, this.raMotor, this.polarCap,
                     this.decHousing, this.decClutch, this.saddle, this.saddleKnob, this.cwShaft, this.cwStop,
                     ...this.cws, this.ray]) this.scene.add(m);
  }

  // Explorer 150P in its local frame: y = optical axis (towards the sky end),
  // x = away from the dovetail, z = side of the focuser.
  _buildOta() {
    const g = (this.ota = new THREE.Group());
    const r = OTA.radius, half = OTA.length / 2;
    const tube = local(g, cylinder(r, OTA.length - 0.07, COLORS.tubeWhite, { rough: 0.35, metal: 0.05, segments: 48 }),
      [0, 0.01, 0]);
    tube.material.emissive.set(0x3a3c40); // keep the white tube white on its shaded side
    // front ring with open aperture, secondary holder and spider
    local(g, cylinder(r + 0.004, 0.03, COLORS.black, { segments: 48 }), [0, half - 0.015, 0]);
    const aperture = new THREE.Mesh(new THREE.CircleGeometry(r - 0.006, 48),
      new THREE.MeshStandardMaterial({ color: 0x050608, roughness: 1 }));
    aperture.rotation.x = -Math.PI / 2;
    aperture.position.y = half + 0.001;
    g.add(aperture);
    local(g, cylinder(0.022, 0.05, COLORS.black, { segments: 16 }), [0, half - 0.06, 0]);
    for (const a of [0, 90, 180, 270]) {
      const vane = box(r - 0.01, 0.025, 0.002, COLORS.black);
      vane.position.set(Math.cos(a * DEG) * r / 2, half - 0.06, Math.sin(a * DEG) * r / 2);
      vane.rotation.y = -a * DEG;
      g.add(vane);
    }
    // primary mirror cell with collimation screws
    local(g, cylinder(r + 0.005, 0.05, COLORS.black, { segments: 48 }), [0, -half + 0.025, 0]);
    for (const a of [30, 150, 270]) {
      local(g, cylinder(0.008, 0.03, COLORS.black, { segments: 10 }),
        [Math.cos(a * DEG) * 0.055, -half - 0.012, Math.sin(a * DEG) * 0.055]);
    }
    // tube rings and Vixen dovetail
    for (const y of OTA.ringsAt) {
      local(g, cylinder(r + 0.008, 0.022, COLORS.black, { segments: 48 }), [0, y, 0]);
      local(g, box(0.03, 0.022, 0.03, COLORS.black), [-(r + 0.015), y, 0]);
      local(g, box(0.03, 0.022, 0.03, COLORS.black), [r + 0.015, y, 0]);
    }
    local(g, box(0.016, 0.36, 0.044, COLORS.silver, { metal: 0.6, rough: 0.35 }), [-(r + 0.038), -0.01, 0]);
    // 2" Crayford focuser with camera on the side
    const fy = half - OTA.focuserFromFront;
    local(g, box(0.07, 0.07, 0.03, COLORS.black), [0, fy, r + 0.008]);
    local(g, cylinder(0.032, 0.05, COLORS.black), [0, fy, r + 0.045], "z");
    local(g, cylinder(0.026, 0.04, COLORS.silver, { metal: 0.7 }), [0, fy, r + 0.09], "z");
    local(g, cylinder(0.008, 0.11, COLORS.silver, { metal: 0.7, segments: 12 }), [0, fy - 0.035, r + 0.04], "x");
    this.camera3d = local(g, cylinder(0.039, 0.05, COLORS.camRed, { metal: 0.4 }), [0, fy, r + 0.135], "z");
    local(g, cylinder(0.039, 0.012, COLORS.black), [0, fy, r + 0.166], "z");
    // guide scope on top of the rings (opposite the dovetail)
    const gx = r + 0.065;
    local(g, box(0.012, 0.2, 0.04, COLORS.black), [r + 0.022, 0, 0]);
    for (const y of [-0.06, 0.06]) local(g, cylinder(0.032, 0.015, COLORS.black, { segments: 24 }), [gx, y, 0]);
    local(g, cylinder(0.026, 0.2, COLORS.black, { segments: 24 }), [gx, 0.0, 0]);
    local(g, cylinder(0.03, 0.04, COLORS.black, { segments: 24 }), [gx, 0.12, 0]);
    local(g, cylinder(0.02, 0.035, COLORS.camRed, { segments: 20 }), [gx, -0.12, 0]);
    this.scene.add(g);
  }

  update({ ha, dec, pier, lat }) {
    const known = ha !== null && ha !== undefined && dec !== null && dec !== undefined;
    const phi = (lat ?? 51) * DEG;
    const h = known ? ha * 15 * DEG : -90 * DEG; // park: pointing at the pole, counterweight down
    const d = known ? dec * DEG : 90 * DEG;
    const side = pier === "East" ? -1 : 1;

    const P = new THREE.Vector3(0, Math.sin(phi), -Math.cos(phi)); // polar axis (up = north celestial pole)
    const X = new THREE.Vector3(0, Math.cos(phi), Math.sin(phi));  // equator on the meridian
    const Y = new THREE.Vector3(-1, 0, 0);                         // west
    const e = (a) => X.clone().multiplyScalar(Math.cos(a)).add(Y.clone().multiplyScalar(Math.sin(a)));
    const p = e(h).multiplyScalar(Math.cos(d)).add(P.clone().multiplyScalar(Math.sin(d))).normalize();
    const o = e(h + Math.PI / 2).multiplyScalar(side).normalize(); // dec axis, telescope end
    const n = o.clone().cross(p).normalize();

    // RA axis pivots in the latitude block
    const M = new THREE.Vector3(0, HEAD_Y + 0.2, 0);
    place(this.raHousing, M, P);
    place(this.polarCap, M.clone().addScaledVector(P, -0.17), P);
    this.raMotor.position.copy(M.clone().addScaledVector(P, 0.03).add(new THREE.Vector3(0, -0.05, 0)));
    this.raMotor.quaternion.setFromUnitVectors(UP, P);
    // Dec housing on top of the RA axis, rotating with it
    const D = M.clone().addScaledVector(P, 0.2);
    place(this.decHousing, D, o);
    place(this.decClutch, D.clone().addScaledVector(o, 0.1), o);
    const S = D.clone().addScaledVector(o, 0.157);
    this.saddle.position.copy(S);
    this.saddle.quaternion.setFromRotationMatrix(new THREE.Matrix4().makeBasis(o, p, n));
    place(this.saddleKnob, S.clone().addScaledVector(n, 0.07), n);
    // counterweight shaft with two 5 kg weights and safety stop
    const shaftStart = D.clone().addScaledVector(o, -0.14);
    place(this.cwShaft, shaftStart.clone().addScaledVector(o, -0.23), o);
    place(this.cwStop, shaftStart.clone().addScaledVector(o, -0.46), o);
    this.cws.forEach((cw, i) => place(cw, shaftStart.clone().addScaledVector(o, -(0.3 + i * 0.056)), o));

    // OTA: dovetail on the saddle, tube axis along p, focuser to the side (n)
    // saddle face (half its thickness) + tube radius + ring foot and dovetail below the tube
    this.ota.position.copy(S).addScaledVector(o, 0.0175 + OTA.radius + 0.046).addScaledVector(p, 0.04);
    this.ota.quaternion.setFromRotationMatrix(new THREE.Matrix4().makeBasis(o, p, n));
    const front = this.ota.position.clone().addScaledVector(p, OTA.length / 2);
    this.ray.geometry.setFromPoints([front, front.clone().addScaledVector(p, 1.6)]);
    this.ray.computeLineDistances();
    this.ray.visible = known;

    this.counterweightUp = known && o.y < -0.05; // counterweight is at -o
    for (const cw of this.cws) cw.material.color.set(this.counterweightUp ? COLORS.warn : COLORS.chrome);
  }

  _resize() {
    const w = this.el.clientWidth, h = this.el.clientHeight;
    if (!w || !h) return;
    this.renderer.setSize(w, h, false);
    this.renderer.domElement.style.width = "100%";
    this.renderer.domElement.style.height = "100%";
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
  }
}
