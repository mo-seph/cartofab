/* WebGL preview of the generated mesh.
 *
 * Loads the same indexed geometry the exporter produces, so what you orbit is
 * literally what you download — no separate preview approximation to drift.
 */
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

const host = document.getElementById('mesh3d');
let renderer, scene, camera, controls, root, raf;

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

  scene.add(new THREE.HemisphereLight(0xdfe7f5, 0x2a2620, 1.5));
  const key = new THREE.DirectionalLight(0xffffff, 2.2);
  key.position.set(-1, -1.4, 2);
  scene.add(key);
  const fill = new THREE.DirectionalLight(0xffffff, 0.6);
  fill.position.set(2, 1, 1);
  scene.add(fill);

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

  const box = new THREE.Box3().setFromObject(root);
  const size = box.getSize(new THREE.Vector3());
  const mid = box.getCenter(new THREE.Vector3());
  controls.target.copy(mid);
  const span = Math.max(size.x, size.y, size.z);
  camera.position.set(mid.x + span * 0.9, mid.y + span * 0.8, mid.z + span * 1.1);
  camera.near = span / 500;
  camera.far = span * 60;
  camera.updateProjectionMatrix();
  controls.update();
  resize();
  return header.stats;
}

export function dispose() {
  if (raf) cancelAnimationFrame(raf);
  clear();
}

window.MeshViewer = { show, dispose, resize };
