// 宣传页 3D 世界：一套贯穿全页的 GPU 粒子，随滚动在六种形态之间变形。
//   0 派活核心与 11 个行业节点  1 失序（老板的困境）  2 一句话派出的任务流
//   3 行业星环  4 流水线波面  5 “派活”字形
// 只做展示，不触碰业务接口；WebGL 不可用时由调用方保留 CSS 背景兜底。
import * as THREE from "./vendor/three.module.min.js";

const STAGES = 6;
const PALETTE = [
  ["#ffd978", "#f5b82e", "#5ee6d0"],
  ["#ff6b5e", "#8a5a3c", "#3a2a1c"],
  ["#ffe7a8", "#f5b82e", "#5ee6d0"],
  ["#ff8a5b", "#5ee6d0", "#9b8cff"],
  ["#5ee6d0", "#f5b82e", "#fff4dc"],
  ["#ffe3a3", "#f5b82e", "#ffb347"],
];

function rand(seed) { let s = seed >>> 0; return () => ((s = (s * 1664525 + 1013904223) >>> 0) / 4294967296); }

function sampleGlyphs(text, count, r) {
  const c = document.createElement("canvas"); c.width = 1200; c.height = 520;
  const g = c.getContext("2d");
  g.fillStyle = "#fff"; g.textAlign = "center"; g.textBaseline = "middle";
  g.font = '600 430px "PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif';
  g.fillText(text, 600, 270);
  const d = g.getImageData(0, 0, c.width, c.height).data; const pts = [];
  for (let y = 0; y < c.height; y += 3) for (let x = 0; x < c.width; x += 3) if (d[(y * c.width + x) * 4 + 3] > 140) pts.push([x, y]);
  const out = new Float32Array(count * 3);
  for (let i = 0; i < count; i++) {
    const p = pts.length ? pts[Math.floor(r() * pts.length)] : [600, 260];
    out[i * 3] = (p[0] - 600) / 600 * 6.2 + (r() - .5) * .04;
    out[i * 3 + 1] = -(p[1] - 270) / 600 * 6.2 + (r() - .5) * .04;
    out[i * 3 + 2] = (r() - .5) * .6;
  }
  return out;
}

function buildFormations(n, industries) {
  const r = rand(20261004);
  const F = Array.from({ length: STAGES }, () => new Float32Array(n * 3));
  const nodeCount = industries;
  const nodePos = [];
  for (let i = 0; i < nodeCount; i++) {
    const y = 1 - (i + .5) / nodeCount * 2, rad = Math.sqrt(1 - y * y), th = i * Math.PI * (3 - Math.sqrt(5)), R = 5.4;
    nodePos.push([Math.cos(th) * rad * R, y * R * .55, Math.sin(th) * rad * R]);
  }
  for (let i = 0; i < n; i++) {
    const u = r(), v = r(), w = r();
    // 0 核心 + 节点 + 连线
    let p;
    if (u < .42) { const th = 2 * Math.PI * v, ph = Math.acos(2 * w - 1), rr = 1.25 + Math.pow(r(), 3) * .9; p = [rr * Math.sin(ph) * Math.cos(th), rr * Math.cos(ph), rr * Math.sin(ph) * Math.sin(th)]; }
    else if (u < .78) { const nd = nodePos[i % nodeCount], th = 2 * Math.PI * v, ph = Math.acos(2 * w - 1), rr = .1 + Math.pow(r(), 2) * .32; p = [nd[0] + rr * Math.sin(ph) * Math.cos(th), nd[1] + rr * Math.cos(ph), nd[2] + rr * Math.sin(ph) * Math.sin(th)]; }
    else { const nd = nodePos[i % nodeCount], k = r(); const lift = Math.sin(k * Math.PI) * 1.4; p = [nd[0] * k, nd[1] * k + lift, nd[2] * k]; }
    F[0].set(p, i * 3);
    // 1 失序：大范围散落，带一点下坠感
    F[1].set([(r() - .5) * 26, (r() - .5) * 14 - 1.5, (r() - .5) * 14 - 3], i * 3);
    // 2 一句话：细长的输入条 → 四个工位隆起
    { const x = (r() - .5) * 16; const bump = [-5.4, -1.8, 1.8, 5.4].reduce((a, c) => a + Math.exp(-((x - c) ** 2) * 2.2), 0); const rr = .08 + bump * .55 * Math.sqrt(r()), th = r() * Math.PI * 2; F[2].set([x, Math.cos(th) * rr, Math.sin(th) * rr], i * 3); }
    // 3 行业星环：11 团，平放微倾
    { const k = i % nodeCount, a = k / nodeCount * Math.PI * 2, R = 6.2; const th = r() * Math.PI * 2, rr = Math.pow(r(), 1.6) * .75; const cx = Math.cos(a) * R, cz = Math.sin(a) * R; F[3].set([cx + Math.cos(th) * rr, (r() - .5) * .5 + Math.sin(a * 2) * .4, cz + Math.sin(th) * rr], i * 3); }
    // 4 流水线波面
    { const gx = (r() - .5) * 20, gz = (r() - .5) * 12; F[4].set([gx, Math.sin(gx * .55) * .9 + Math.cos(gz * .7) * .5 - 1.2, gz - 2], i * 3); }
  }
  F[5] = sampleGlyphs("派活", n, r);
  return F;
}

const VERT = `
attribute vec3 p0; attribute vec3 p1; attribute vec3 p2; attribute vec3 p3; attribute vec3 p4; attribute vec3 p5;
attribute float aRnd;
uniform float uStage; uniform float uTime; uniform float uSize; uniform float uPR; uniform vec3 uMouse; uniform float uMouseK;
uniform vec3 uCA[6]; uniform vec3 uCB[6]; uniform vec3 uCC[6];
varying vec3 vColor; varying float vAlpha;
vec3 pick(float i){ return i<.5?p0: i<1.5?p1: i<2.5?p2: i<3.5?p3: i<4.5?p4: p5; }
vec3 col(float i){ vec3 a=uCA[0],b=uCB[0],c=uCC[0];
  if(i>.5&&i<1.5){a=uCA[1];b=uCB[1];c=uCC[1];} else if(i>1.5&&i<2.5){a=uCA[2];b=uCB[2];c=uCC[2];}
  else if(i>2.5&&i<3.5){a=uCA[3];b=uCB[3];c=uCC[3];} else if(i>3.5&&i<4.5){a=uCA[4];b=uCB[4];c=uCC[4];} else if(i>4.5){a=uCA[5];b=uCB[5];c=uCC[5];}
  return aRnd<.55?mix(a,b,aRnd/.55):mix(b,c,(aRnd-.55)/.45); }
void main(){
  float s0=floor(uStage); float s1=min(s0+1.,5.); float f=uStage-s0;
  float d=aRnd*.4; float k=clamp((f-d)/.6,0.,1.); k=k*k*(3.-2.*k);
  vec3 pos=mix(pick(s0),pick(s1),k);
  float tr=sin(k*3.14159);
  vec3 dir=normalize(vec3(sin(aRnd*91.7),cos(aRnd*57.3),sin(aRnd*33.1+1.)));
  pos+=dir*tr*1.6;
  float t=uTime*(.35+aRnd*.4);
  pos+=.06*vec3(sin(t+aRnd*40.+pos.y*.8),cos(t*.9+aRnd*30.+pos.x*.7),sin(t*.8+aRnd*20.));
  vec3 dm=pos-uMouse; float dd=length(dm);
  pos+=normalize(dm+1e-4)*.9*exp(-dd*dd*.55)*uMouseK;
  vec4 mv=modelViewMatrix*vec4(pos,1.);
  gl_Position=projectionMatrix*mv;
  float tw=.65+.35*sin(uTime*2.+aRnd*60.);
  gl_PointSize=uSize*(.45+aRnd*.9)*uPR*(18./-mv.z)*(1.+tr*.6);
  vColor=mix(col(s0),col(s1),k);
  vAlpha=tw*(1.-tr*.25);
}`;
const FRAG = `
varying vec3 vColor; varying float vAlpha; uniform float uFade;
void main(){ float d=length(gl_PointCoord-.5); float a=smoothstep(.5,.0,d); a=pow(a,1.6);
  gl_FragColor=vec4(vColor*(1.+.6*smoothstep(.18,.0,d)),a*vAlpha*uFade); }`;

export function mountWorld(canvas, { industries = 11, reducedMotion = false } = {}) {
  const small = Math.min(innerWidth, innerHeight) < 700;
  const cores = navigator.hardwareConcurrency || 4;
  const N = small ? 6000 : cores >= 8 ? 16000 : 10000;
  const renderer = new THREE.WebGLRenderer({ canvas, antialias: false, alpha: true, powerPreference: "high-performance" });
  const PR = Math.min(devicePixelRatio || 1, small ? 1.6 : 1.75);
  renderer.setPixelRatio(PR);
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(42, 1, .1, 200);
  const group = new THREE.Group(); scene.add(group);

  const F = buildFormations(N, industries);
  const geo = new THREE.BufferGeometry();
  geo.setAttribute("position", new THREE.BufferAttribute(F[0].slice(), 3));
  F.forEach((a, i) => geo.setAttribute("p" + i, new THREE.BufferAttribute(a, 3)));
  const rnd = new Float32Array(N); const r = rand(77); for (let i = 0; i < N; i++) rnd[i] = r();
  geo.setAttribute("aRnd", new THREE.BufferAttribute(rnd, 1));
  geo.boundingSphere = new THREE.Sphere(new THREE.Vector3(), 40);
  const toV = (hex) => new THREE.Color(hex);
  const uniforms = {
    uStage: { value: 0 }, uTime: { value: 0 }, uSize: { value: small ? 3.2 : 2.6 }, uPR: { value: PR },
    uMouse: { value: new THREE.Vector3(99, 99, 0) }, uMouseK: { value: 0 }, uFade: { value: 0 },
    uCA: { value: PALETTE.map((p) => toV(p[0])) }, uCB: { value: PALETTE.map((p) => toV(p[1])) }, uCC: { value: PALETTE.map((p) => toV(p[2])) },
  };
  const mat = new THREE.ShaderMaterial({ vertexShader: VERT, fragmentShader: FRAG, uniforms, transparent: true, depthWrite: false, blending: THREE.AdditiveBlending });
  group.add(new THREE.Points(geo, mat));

  // 核心辉光：只在“核心”和“字形”两种形态下明显
  const gc = document.createElement("canvas"); gc.width = gc.height = 128; const gg = gc.getContext("2d");
  const grd = gg.createRadialGradient(64, 64, 0, 64, 64, 64); grd.addColorStop(0, "rgba(255,220,150,1)"); grd.addColorStop(.3, "rgba(245,184,46,.35)"); grd.addColorStop(1, "rgba(0,0,0,0)");
  gg.fillStyle = grd; gg.fillRect(0, 0, 128, 128);
  const glow = new THREE.Sprite(new THREE.SpriteMaterial({ map: new THREE.CanvasTexture(gc), transparent: true, blending: THREE.AdditiveBlending, depthWrite: false }));
  glow.scale.set(7, 7, 1); group.add(glow);

  const state = { stage: 0, target: 0, dim: 1, tDim: 1, ang: 0, mx: 0, my: 0, tmx: 0, tmy: 0, mouseK: 0, visible: true, offsetX: 0, tOffsetX: 0 };
  const ray = new THREE.Raycaster(); const plane = new THREE.Plane(new THREE.Vector3(0, 0, 1), 0); const hit = new THREE.Vector3(); const ndc = new THREE.Vector2();

  function resize() {
    const w = canvas.clientWidth, h = canvas.clientHeight; if (!w || !h) return;
    renderer.setSize(w, h, false); camera.aspect = w / h;
    const tan = Math.tan(THREE.MathUtils.degToRad(camera.fov / 2));
    camera.position.z = Math.max(15, 7.6 / (tan * camera.aspect));
    camera.updateProjectionMatrix();
    if (reducedMotion) draw(0);
  }
  addEventListener("resize", resize); resize();
  addEventListener("pointermove", (e) => { state.tmx = e.clientX / innerWidth * 2 - 1; state.tmy = -(e.clientY / innerHeight * 2 - 1); state.mouseK = 1; }, { passive: true });
  document.addEventListener("pointerleave", () => { state.mouseK = 0; });
  document.addEventListener("visibilitychange", () => { if (!document.hidden) loop(performance.now()); });

  let last = performance.now(), time = 0, raf = 0, fade = 0;
  function draw(dt) {
    time += dt;
    state.stage += (state.target - state.stage) * Math.min(1, dt * 2.4);
    state.mx += (state.tmx - state.mx) * Math.min(1, dt * 3);
    state.my += (state.tmy - state.my) * Math.min(1, dt * 3);
    state.offsetX += (state.tOffsetX - state.offsetX) * Math.min(1, dt * 2.4);
    fade = Math.min(1, fade + dt * .8);
    state.dim += (state.tDim - state.dim) * Math.min(1, dt * 2.4);
    uniforms.uStage.value = Math.max(0, Math.min(STAGES - 1, state.stage));
    uniforms.uTime.value = time; uniforms.uFade.value = fade * state.dim;
    uniforms.uMouseK.value += (state.mouseK - uniforms.uMouseK.value) * Math.min(1, dt * 4);
    const st = uniforms.uStage.value;
    const textK = Math.max(0, 1 - Math.abs(st - 5) * 1.4);
    group.position.x = state.offsetX;
    group.position.y = textK * (camera.aspect < 1 ? 3.4 : 1.5);
    // 线形与字形需要正对镜头：越接近这两种形态，自转越弱并回正
    const facing = Math.max(0, 1 - Math.abs(st - 2) * 1.4, 1 - Math.abs(st - 5) * 1.4);
    const spin = 1 - facing;
    state.ang += dt * (.05 + (st > 2.5 && st < 3.5 ? .08 : 0)) * spin;
    const wrapped = Math.atan2(Math.sin(state.ang), Math.cos(state.ang));
    group.rotation.y = wrapped * spin + state.mx * (.35 * spin + .12 * facing);
    group.rotation.x = .1 - state.my * .18 + (st > 2.6 && st < 3.4 ? .45 : 0) * Math.max(0, 1 - Math.abs(st - 3));
    const gk = Math.max(0, 1 - Math.min(Math.abs(st - 0), Math.abs(st - 5)) * 1.6);
    glow.material.opacity = gk * (.65 + Math.sin(time * 2) * .12) * fade * state.dim;
    ndc.set(state.mx, state.my); ray.setFromCamera(ndc, camera);
    if (ray.ray.intersectPlane(plane, hit)) uniforms.uMouse.value.copy(group.worldToLocal(hit.clone()));
    renderer.render(scene, camera);
  }
  function loop(now) {
    cancelAnimationFrame(raf);
    if (document.hidden || reducedMotion) return;
    const dt = Math.min(.05, (now - last) / 1000); last = now;
    draw(dt); raf = requestAnimationFrame(loop);
  }
  if (reducedMotion) { fade = 1; draw(0); } else loop(performance.now());

  return {
    setStage(v, offsetX = 0, dim = 1) { state.target = v; state.tOffsetX = offsetX; state.tDim = dim; if (reducedMotion) { state.stage = v; state.offsetX = offsetX; state.dim = dim; draw(0); } },
    particles: N,
  };
}
