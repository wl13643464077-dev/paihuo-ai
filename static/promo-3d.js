// 宣传页首屏 3D 场景：中央"派活核心"向 11 个行业专家团派发任务，交付沿同一条链路回流。
// 只做展示，不触碰任何业务接口；WebGL 不可用时由调用方保留 CSS 背景兜底。
import * as THREE from "./vendor/three.module.min.js";

const GOLD = 0xf5b82e;
const TEAL = 0x5ee6d0;

function glowTexture(inner = "rgba(255,220,140,1)", outer = "rgba(255,180,60,0)") {
  const c = document.createElement("canvas");
  c.width = c.height = 128;
  const g = c.getContext("2d");
  const grd = g.createRadialGradient(64, 64, 0, 64, 64, 64);
  grd.addColorStop(0, inner);
  grd.addColorStop(0.25, inner.replace(/[\d.]+\)$/, "0.55)"));
  grd.addColorStop(1, outer);
  g.fillStyle = grd;
  g.fillRect(0, 0, 128, 128);
  const tex = new THREE.CanvasTexture(c);
  tex.colorSpace = THREE.SRGBColorSpace;
  return tex;
}

function labelTexture(icon, text) {
  const dpr = 2, w = 220 * dpr, h = 64 * dpr;
  const c = document.createElement("canvas");
  c.width = w; c.height = h;
  const g = c.getContext("2d");
  g.scale(dpr, dpr);
  const r = 22;
  g.fillStyle = "rgba(20,15,9,.72)";
  g.strokeStyle = "rgba(245,184,46,.55)";
  g.lineWidth = 1.5;
  g.beginPath();
  g.roundRect ? g.roundRect(4, 10, 212, 44, r) : g.rect(4, 10, 212, 44);
  g.fill(); g.stroke();
  g.font = '22px "Apple Color Emoji","Segoe UI Emoji","Noto Color Emoji",sans-serif';
  g.textBaseline = "middle";
  g.fillText(icon, 18, 33);
  g.font = '600 19px "PingFang SC","Microsoft YaHei",system-ui,sans-serif';
  g.fillStyle = "#fff4dc";
  g.fillText(text, 52, 33);
  const tex = new THREE.CanvasTexture(c);
  tex.colorSpace = THREE.SRGBColorSpace;
  tex.anisotropy = 4;
  return tex;
}

export function mountHero(canvas, { industries, onFocus, reducedMotion = false } = {}) {
  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true, powerPreference: "high-performance" });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.75));
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;

  const scene = new THREE.Scene();
  scene.fog = new THREE.FogExp2(0x120d08, 0.028);
  const camera = new THREE.PerspectiveCamera(42, 1, 0.1, 200);
  camera.position.set(0, 1.4, 13);

  const small = Math.min(window.innerWidth, window.innerHeight) < 640;
  const world = new THREE.Group();
  scene.add(world);

  // ---- 派活核心 ----
  const core = new THREE.Group();
  world.add(core);
  const coreGlow = new THREE.Sprite(new THREE.SpriteMaterial({ map: glowTexture(), color: GOLD, transparent: true, blending: THREE.AdditiveBlending, depthWrite: false }));
  coreGlow.scale.set(5.2, 5.2, 1);
  core.add(coreGlow);
  core.add(new THREE.Mesh(new THREE.SphereGeometry(0.62, 48, 48), new THREE.MeshBasicMaterial({ color: 0xffe3a3 })));
  const shell = new THREE.LineSegments(
    new THREE.EdgesGeometry(new THREE.IcosahedronGeometry(1.35, 1)),
    new THREE.LineBasicMaterial({ color: GOLD, transparent: true, opacity: 0.6 }),
  );
  core.add(shell);
  const rings = [];
  [[1.9, 0.9, 0.2], [2.25, -0.5, 1.1], [2.6, 0.3, -0.8]].forEach(([r, rx, rz], i) => {
    const ring = new THREE.Mesh(
      new THREE.TorusGeometry(r, 0.012, 8, 160),
      new THREE.MeshBasicMaterial({ color: i === 1 ? TEAL : GOLD, transparent: true, opacity: 0.35 }),
    );
    ring.rotation.set(rx, 0, rz);
    core.add(ring);
    rings.push(ring);
  });

  // ---- 行业节点：黄金角分布在扁椭球壳上 ----
  const orbit = new THREE.Group();
  world.add(orbit);
  const nodeGlowTex = glowTexture("rgba(255,255,255,1)", "rgba(255,255,255,0)");
  const nodes = industries.map((ind, i) => {
    const n = industries.length;
    const y = 1 - (i + 0.5) / n * 2;
    const rad = Math.sqrt(1 - y * y);
    const theta = i * Math.PI * (3 - Math.sqrt(5));
    const R = 5.4;
    const pos = new THREE.Vector3(Math.cos(theta) * rad * R, y * R * 0.55, Math.sin(theta) * rad * R);
    const color = new THREE.Color(ind.color);
    const g = new THREE.Group();
    g.position.copy(pos);
    const ball = new THREE.Mesh(new THREE.SphereGeometry(0.2, 24, 24), new THREE.MeshBasicMaterial({ color }));
    ball.userData.index = i;
    const glow = new THREE.Sprite(new THREE.SpriteMaterial({ map: nodeGlowTex, color, transparent: true, opacity: 0.8, blending: THREE.AdditiveBlending, depthWrite: false }));
    glow.scale.set(0.9, 0.9, 1);
    const label = new THREE.Sprite(new THREE.SpriteMaterial({ map: labelTexture(ind.icon, ind.name), transparent: true, depthWrite: false, opacity: 0.85 }));
    label.scale.set(2.3, 0.67, 1);
    label.position.set(0, 0.6, 0);
    g.add(ball, glow, label);
    orbit.add(g);
    // 链路：核心 → 节点，控制点外抬形成弧线
    const ctrl = pos.clone().multiplyScalar(0.5).add(new THREE.Vector3(0, 1.6, 0)).add(pos.clone().normalize().multiplyScalar(0.8));
    const curve = new THREE.QuadraticBezierCurve3(new THREE.Vector3(), ctrl, pos);
    const line = new THREE.Line(
      new THREE.BufferGeometry().setFromPoints(curve.getPoints(48)),
      new THREE.LineBasicMaterial({ color, transparent: true, opacity: 0.16 }),
    );
    orbit.add(line);
    return { group: g, ball, glow, label, line, curve, color, focus: 0 };
  });

  // ---- 任务脉冲：金色派活，青色交付回流 ----
  const PULSES = small ? 70 : 140;
  const pulseGeo = new THREE.BufferGeometry();
  const pPos = new Float32Array(PULSES * 3);
  const pCol = new Float32Array(PULSES * 3);
  pulseGeo.setAttribute("position", new THREE.BufferAttribute(pPos, 3));
  pulseGeo.setAttribute("color", new THREE.BufferAttribute(pCol, 3));
  const pulses = Array.from({ length: PULSES }, () => spawnPulse({}, true));
  function spawnPulse(p, randomT = false, forceNode = -1) {
    p.node = forceNode >= 0 ? forceNode : Math.floor(Math.random() * nodes.length);
    p.back = Math.random() < 0.38;
    p.t = randomT ? Math.random() : 0;
    p.speed = 0.18 + Math.random() * 0.22;
    return p;
  }
  const gold = new THREE.Color(GOLD), teal = new THREE.Color(TEAL);
  const pulsePoints = new THREE.Points(pulseGeo, new THREE.PointsMaterial({
    size: small ? 0.2 : 0.17, map: nodeGlowTex, vertexColors: true, transparent: true,
    blending: THREE.AdditiveBlending, depthWrite: false,
  }));
  orbit.add(pulsePoints);

  // ---- 星尘背景 ----
  const STARS = small ? 700 : 1600;
  const sPos = new Float32Array(STARS * 3);
  for (let i = 0; i < STARS; i++) {
    const r = 18 + Math.random() * 50, th = Math.random() * Math.PI * 2, ph = Math.acos(2 * Math.random() - 1);
    sPos[i * 3] = r * Math.sin(ph) * Math.cos(th);
    sPos[i * 3 + 1] = r * Math.cos(ph) * 0.6;
    sPos[i * 3 + 2] = r * Math.sin(ph) * Math.sin(th);
  }
  const starGeo = new THREE.BufferGeometry();
  starGeo.setAttribute("position", new THREE.BufferAttribute(sPos, 3));
  const stars = new THREE.Points(starGeo, new THREE.PointsMaterial({ size: 0.09, color: 0xffe9c2, transparent: true, opacity: 0.7, depthWrite: false }));
  scene.add(stars);

  // ---- 交互 ----
  const pointer = new THREE.Vector2(9, 9);
  const parallax = { x: 0, y: 0, tx: 0, ty: 0 };
  const raycaster = new THREE.Raycaster();
  let hovered = -1, focused = 0, lastAuto = 0, scrollK = 0, labelK = 1;
  const balls = nodes.map((n) => n.ball);

  function setFocus(i) {
    if (i === focused) return;
    focused = i;
    for (let k = 0; k < 6; k++) spawnPulse(pulses[(Math.random() * PULSES) | 0], false, i);
    onFocus && onFocus(i);
  }
  canvas.addEventListener("pointermove", (e) => {
    const r = canvas.getBoundingClientRect();
    pointer.x = ((e.clientX - r.left) / r.width) * 2 - 1;
    pointer.y = -((e.clientY - r.top) / r.height) * 2 + 1;
    parallax.tx = pointer.x * 0.5;
    parallax.ty = pointer.y * 0.3;
  });
  canvas.addEventListener("pointerleave", () => { pointer.set(9, 9); parallax.tx = parallax.ty = 0; hovered = -1; canvas.style.cursor = ""; });

  function resize() {
    const w = canvas.clientWidth, h = canvas.clientHeight;
    if (!w || !h) return;
    renderer.setSize(w, h, false);
    camera.aspect = w / h;
    // 按可视范围摆放星系：宽屏放右侧给文案留位，窄屏/竖屏放到文案下方
    const a = camera.aspect;
    const tan = Math.tan(THREE.MathUtils.degToRad(camera.fov / 2));
    // 横向至少容纳整圈节点+标签（半宽约 7.2），竖屏靠拉远镜头保证不裁切
    const dist = Math.max(a > 1.7 ? 14 : a > 1.25 ? 16.5 : 19, 7.2 / (tan * a));
    camera.position.z = dist;
    scene.fog.density = 0.38 / dist;
    const halfH = dist * tan;
    labelK = a < 0.8 ? 1.5 : 1;
    world.position.x = a > 1.25 ? halfH * a * 0.38 : 0;
    world.position.y = a > 1.25 ? -0.3 : -halfH * 0.36;
    camera.updateProjectionMatrix();
    if (reducedMotion) renderer.render(scene, camera);
  }
  new ResizeObserver(resize).observe(canvas);
  resize();

  let visible = true, raf = 0, last = performance.now(), time = 0;
  new IntersectionObserver(([en]) => { visible = en.isIntersecting; if (visible && !raf && !reducedMotion) loop(performance.now()); }).observe(canvas);
  document.addEventListener("visibilitychange", () => { if (!document.hidden && visible && !raf && !reducedMotion) { last = performance.now(); loop(last); } });
  window.addEventListener("scroll", () => { scrollK = Math.min(1, window.scrollY / (window.innerHeight || 800)); }, { passive: true });

  function update(dt) {
    time += dt;
    core.rotation.y += dt * 0.25;
    shell.rotation.x += dt * 0.18;
    rings.forEach((r, i) => { r.rotation.z += dt * (0.15 + i * 0.07) * (i % 2 ? -1 : 1); });
    coreGlow.material.opacity = 0.75 + Math.sin(time * 2.2) * 0.15;
    orbit.rotation.y += dt * 0.06;
    stars.rotation.y -= dt * 0.006;

    parallax.x += (parallax.tx - parallax.x) * Math.min(1, dt * 3);
    parallax.y += (parallax.ty - parallax.y) * Math.min(1, dt * 3);
    world.rotation.x = -parallax.y * 0.35 + 0.12;
    world.rotation.z = parallax.x * 0.06;
    camera.position.y = 1.4 + scrollK * 2.5;
    camera.lookAt(0, 0, 0);

    // 悬停拾取节点；无悬停时自动轮播焦点行业
    if (pointer.x < 5) {
      raycaster.setFromCamera(pointer, camera);
      const hit = raycaster.intersectObjects(balls, false)[0];
      const idx = hit ? hit.object.userData.index : -1;
      if (idx !== hovered) { hovered = idx; canvas.style.cursor = idx >= 0 ? "pointer" : ""; if (idx >= 0) setFocus(idx); }
    }
    if (hovered < 0 && time - lastAuto > 2.6) { lastAuto = time; setFocus((focused + 1) % nodes.length); }

    nodes.forEach((n, i) => {
      n.focus += ((i === focused ? 1 : 0) - n.focus) * Math.min(1, dt * 4);
      const s = 1 + n.focus * 0.7 + Math.sin(time * 2 + i) * 0.05;
      n.ball.scale.setScalar(s);
      n.glow.scale.setScalar(0.9 + n.focus * 1.1);
      n.label.material.opacity = 0.55 + n.focus * 0.45;
      n.label.scale.set(2.3 * labelK * (1 + n.focus * 0.15), 0.67 * labelK * (1 + n.focus * 0.15), 1);
      n.line.material.opacity = 0.12 + n.focus * 0.5;
    });

    const tmp = new THREE.Vector3();
    pulses.forEach((p, i) => {
      p.t += dt * p.speed * (p.node === focused ? 1.7 : 1);
      if (p.t >= 1) spawnPulse(p);
      const n = nodes[p.node];
      n.curve.getPoint(p.back ? 1 - p.t : p.t, tmp);
      pPos[i * 3] = tmp.x; pPos[i * 3 + 1] = tmp.y; pPos[i * 3 + 2] = tmp.z;
      const c = p.back ? teal : (p.node === focused ? n.color : gold);
      const fade = Math.sin(Math.PI * p.t);
      pCol[i * 3] = c.r * fade; pCol[i * 3 + 1] = c.g * fade; pCol[i * 3 + 2] = c.b * fade;
    });
    pulseGeo.attributes.position.needsUpdate = true;
    pulseGeo.attributes.color.needsUpdate = true;
  }

  function loop(now) {
    raf = 0;
    if (!visible || document.hidden) return;
    const dt = Math.min(0.05, (now - last) / 1000);
    last = now;
    update(dt);
    renderer.render(scene, camera);
    raf = requestAnimationFrame(loop);
  }

  if (reducedMotion) {
    update(0.016);
    renderer.render(scene, camera);
  } else {
    loop(performance.now());
  }
  onFocus && onFocus(focused);
  return { renderer };
}
