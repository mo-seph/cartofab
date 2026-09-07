/* WebGL preview of the generated mesh.
 *
 * Loads the same indexed geometry the exporter produces, so what you orbit is
 * literally what you download — no separate preview approximation to drift.
 */
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

const host = document.getElementById('mesh3d');
let renderer, scene, camera, controls, root, raf;
let bounds = null;                       // model box, for the view presets
let framedView = 'iso';                  // the preset the framing came from
let userMoved = false;                   // ...unless the view has been touched

function init() {
  if (renderer) return;
  renderer = new THREE.WebGLRenderer({ antialias: true });
  renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
  host.appendChild(renderer.domElement);

  scene = new THREE.Scene();
  scene.background = new THREE.Color(0x14161a);

  camera = new THREE.PerspectiveCamera(38, 1, 0.5, 20000);
  controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  // Orbiting a fixed centre is the default and it is the wrong default here:
  // you usually want to look at a corner of the map, not its middle.
  controls.zoomToCursor = true;          // scroll goes where you point
  controls.screenSpacePanning = true;    // pan follows the cursor, not the ground
  controls.panSpeed = 0.9;

  // Shift held = drag to pan. OrbitControls has no shift binding of its own,
  // and right-drag (which it does have) is both undiscoverable and awkward on
  // a trackpad, so swap the left button over while shift is down.
  const ROTATE = THREE.MOUSE.ROTATE, PAN = THREE.MOUSE.PAN;
  const setLeft = (b) => { controls.mouseButtons = {
    LEFT: b, MIDDLE: THREE.MOUSE.DOLLY, RIGHT: PAN }; };
  setLeft(ROTATE);
  addEventListener('keydown', (e) => { if (e.key === 'Shift') setLeft(PAN); });
  addEventListener('keyup', (e) => { if (e.key === 'Shift') setLeft(ROTATE); });
  addEventListener('blur', () => setLeft(ROTATE));

  // Double-click re-centres the orbit on whatever is under the pointer, which
  // is the direct answer to "I can only spin around the middle".
  const ray = new THREE.Raycaster();
  renderer.domElement.addEventListener('dblclick', (e) => {
    if (!root) return;
    const r = renderer.domElement.getBoundingClientRect();
    ray.setFromCamera(new THREE.Vector2(
      ((e.clientX - r.left) / r.width) * 2 - 1,
      -((e.clientY - r.top) / r.height) * 2 + 1), camera);
    const hit = ray.intersectObject(root, true)[0];
    if (!hit) return;
    // keep the camera where it is and move only what it looks at, so the view
    // does not jump — the pivot slides under the model
    const shift = hit.point.clone().sub(controls.target);
    controls.target.add(shift);
    camera.position.add(shift);
    controls.update();
  });

  scene.add(new THREE.HemisphereLight(0xdfe7f5, 0x2a2620, 1.5));
  const key = new THREE.DirectionalLight(0xffffff, 2.2);
  key.position.set(-1, -1.4, 2);
  scene.add(key);
  const fill = new THREE.DirectionalLight(0xffffff, 0.6);
  fill.position.set(2, 1, 1);
  scene.add(fill);

  // Reframing on every resize would fight you mid-drag, but leaving a freshly
  // loaded model cropped because the pane was a different shape when it landed
  // is worse. So: refit on resize only until the view is first touched.
  controls.addEventListener('start', () => { userMoved = true; });

  const loop = () => { raf = requestAnimationFrame(loop); controls.update();
                       renderer.render(scene, camera); };
  loop();
  new ResizeObserver(resize).observe(host);
}

function resize() {
  if (!renderer) return;
  const w = host.clientWidth, h = host.clientHeight;
  if (!w || !h) return;
  renderer.setSize(w, h, false);
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
  if (root && !userMoved) setView(framedView);
}

function clear() {
  if (!root) return;
  root.traverse((o) => {
    if (o.geometry) o.geometry.dispose();
    if (o.material) o.material.dispose();
  });
  scene.remove(root);
  root = null;
}

/** buffer: <u32 header length><json header><float32 verts | uint32 faces>… */
export function show(buffer) {
  init();
  const dv = new DataView(buffer);
  const hlen = dv.getUint32(0, true);
  const header = JSON.parse(new TextDecoder().decode(
    new Uint8Array(buffer, 4, hlen)));
  const base = 4 + hlen;

  clear();
  root = new THREE.Group();
  for (const o of header.objects) {
    const pos = new Float32Array(buffer.slice(base + o.voff, base + o.voff + o.vlen));
    const idx = new Uint32Array(buffer.slice(base + o.foff, base + o.foff + o.flen));
    const g = new THREE.BufferGeometry();
    g.setAttribute('position', new THREE.BufferAttribute(pos, 3));
    g.setIndex(new THREE.BufferAttribute(idx, 1));
    g.computeVertexNormals();
    const [r, gg, b] = o.colour;
    root.add(new THREE.Mesh(g, new THREE.MeshStandardMaterial({
      color: new THREE.Color(r, gg, b), roughness: 0.85, metalness: 0.0,
      flatShading: false,
    })));
  }
  // model is built with Z up; tip it so orbiting feels natural
  root.rotation.x = -Math.PI / 2;
  scene.add(root);

  bounds = new THREE.Box3().setFromObject(root);
  const span = Math.max(...bounds.getSize(new THREE.Vector3()).toArray());
  camera.near = span / 500;
  camera.far = span * 60;
  userMoved = false;
  setView('iso');
  resize();
  return header.stats;
}

/* Named viewpoints, all framed on the model. Directions are in the tipped
   frame the model is displayed in (Z up becomes Y up). */
const DIRS = {
  iso: [0.9, 0.8, 1.1],
  top: [0, 1, 0.0001],       // not exactly straight down: a pure Y axis has no
  front: [0, 0.15, 1],       // stable "up" and the camera rolls unpredictably
  side: [1, 0.15, 0],
};

export function setView(name = 'iso') {
  if (!root || !bounds) return;
  framedView = DIRS[name] ? name : 'iso';
  userMoved = false;
  const d = DIRS[framedView];
  const size = bounds.getSize(new THREE.Vector3());
  const mid = bounds.getCenter(new THREE.Vector3());
  const span = Math.max(size.x, size.y, size.z);
  // pull back far enough that the whole model fits the narrower field of view
  const fov = THREE.MathUtils.degToRad(camera.fov);
  const dist = (span / 2) / Math.tan(fov / 2)
               * (camera.aspect < 1 ? 1 / Math.max(camera.aspect, 0.2) : 1) * 1.25;
  const v = new THREE.Vector3(...d).normalize().multiplyScalar(dist);
  controls.target.copy(mid);
  camera.position.copy(mid).add(v);
  camera.updateProjectionMatrix();
  controls.update();
}

export function dispose() {
  if (raf) cancelAnimationFrame(raf);
  clear();
}

window.MeshViewer = { show, dispose, resize, setView };
