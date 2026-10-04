// 宣传页交互引擎：预加载、拆字揭示、滚动点亮、固定演示、横向能力轴、磁吸按钮、光标、导航。
// 不依赖第三方库；所有动效在“减少动态效果”下退化为直接呈现。
(() => {
  const root = document.documentElement;
  const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const fine = matchMedia("(hover:hover) and (pointer:fine)").matches;
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];
  const clamp = (x, a = 0, b = 1) => Math.max(a, Math.min(b, x));

  // ---------- 拆字 ----------
  $$("[data-split]").forEach((el) => {
    let i = 0;
    const walk = (node, out) => {
      node.childNodes.forEach((c) => {
        if (c.nodeType === 3) {
          [...c.textContent].forEach((ch) => {
            if (ch === "\n") return;
            const s = document.createElement("span"); s.className = "ch"; s.style.setProperty("--i", i++);
            s.textContent = ch === " " ? " " : ch; out.appendChild(s);
          });
        } else if (c.nodeName === "BR") {
          out.appendChild(document.createElement("br"));
        } else {
          const clone = c.cloneNode(false); out.appendChild(clone); walk(c, clone);
        }
      });
    };
    const label = el.textContent.replace(/\s+/g, " ").trim();
    const frag = document.createElement("span"); walk(el, frag);
    // 按块级子元素/换行切成“行”，每行单独遮罩
    const lines = []; let cur = document.createElement("span"); cur.className = "ln";
    [...frag.childNodes].forEach((n) => {
      if (n.nodeName === "BR") { lines.push(cur); cur = document.createElement("span"); cur.className = "ln"; return; }
      if (n.classList && n.classList.contains("row2")) { lines.push(cur); n.classList.add("ln"); lines.push(n); cur = document.createElement("span"); cur.className = "ln"; return; }
      cur.appendChild(n);
    });
    if (cur.childNodes.length) lines.push(cur);
    el.textContent = ""; lines.forEach((l) => el.appendChild(l));
    el.setAttribute("aria-label", label);
    lines.forEach((l) => l.setAttribute("aria-hidden", "true"));
  });

  // ---------- 揭示 ----------
  const io = new IntersectionObserver((es) => es.forEach((e) => { if (e.isIntersecting) { e.target.classList.add("in"); io.unobserve(e.target); } }), { threshold: .15, rootMargin: "0px 0px -6% 0px" });
  const watchReveals = () => $$("[data-split],[data-reveal],[data-mask],.shot").forEach((el) => {
    if (el.closest(".hero")) return; io.observe(el);
  });

  // ---------- 滚动点亮 ----------
  const scrub = $("[data-scrub]");
  let words = [];
  if (scrub) {
    const label = scrub.textContent.replace(/\|/g, "");
    const parts = scrub.textContent.split("|");
    scrub.textContent = "";
    parts.forEach((p, pi) => {
      [...p].forEach((ch) => { const s = document.createElement("span"); s.className = "w" + (pi === 1 || pi === 3 ? " hl" : ""); s.textContent = ch; s.setAttribute("aria-hidden", "true"); scrub.appendChild(s); words.push(s); });
      if (pi < parts.length - 1) scrub.appendChild(document.createElement("br"));
    });
    scrub.setAttribute("aria-label", label);
  }

  // ---------- 一句话派活 ----------
  const dispatch = $("#dispatch"), promptQ = $("#prompt-q"), prompt = $("#prompt"), deliver = $("#deliver");
  const stages = $$(".stage");
  const Q = "帮我做一套周末外卖满减方案，别亏本";

  // ---------- 横向能力轴 ----------
  const caps = $("#caps"), track = $("#caps-track"), capsN = $("#caps-n"), capsBar = $("#caps-bar");
  const horizontal = () => innerWidth > 860 && !reduced;
  function sizeCaps() {
    if (!caps || !track) return;
    if (!horizontal()) { caps.style.removeProperty("--caps-h"); return; }
    const dist = track.scrollWidth - innerWidth;
    caps.style.setProperty("--caps-h", `${innerHeight + dist}px`);
  }

  // ---------- 导航 / 进度 ----------
  const nav = $("#nav"), bar = $(".progress");
  let lastY = scrollY;
  const menuBtn = $(".menu-btn"), sheet = $("#sheet");
  menuBtn && menuBtn.addEventListener("click", () => {
    const open = !root.classList.contains("menu-open");
    root.classList.toggle("menu-open", open); menuBtn.setAttribute("aria-expanded", open); sheet.setAttribute("aria-hidden", !open); sheet.inert = !open;
  });
  $$("#sheet a").forEach((a) => a.addEventListener("click", () => { root.classList.remove("menu-open"); menuBtn.setAttribute("aria-expanded", false); sheet.setAttribute("aria-hidden", true); sheet.inert = true; }));

  // 平滑锚点
  $$('a[href^="#"]').forEach((a) => a.addEventListener("click", (e) => {
    const id = a.getAttribute("href"); const el = id.length > 1 && $(id); if (!el) return;
    e.preventDefault(); const y = el.getBoundingClientRect().top + scrollY - (id === "#top" ? 0 : 40);
    scrollTo({ top: y, behavior: reduced ? "auto" : "smooth" }); history.replaceState(null, "", id);
  }));

  // ---------- 3D 世界：分段形态 ----------
  const stageEls = $$("[data-stage]");
  let world = window.__paihuoWorld || null;
  addEventListener("paihuo:world", () => { world = window.__paihuoWorld; tick(); });
  const hudFlow = $("#hud-flow");

  function tick() {
    const y = scrollY, vh = innerHeight, max = root.scrollHeight - vh;
    bar && (bar.style.transform = `scaleX(${max > 0 ? y / max : 0})`);
    if (nav) {
      nav.classList.toggle("solid", y > 40);
      if (!root.classList.contains("menu-open")) nav.classList.toggle("hide", y > lastY && y > vh * .6);
      lastY = y;
    }
    // 滚动点亮
    if (scrub && words.length) {
      const r = scrub.closest(".manifesto").getBoundingClientRect();
      const p = clamp((-r.top) / (r.height - vh) * 1.15);
      const n = Math.round(p * words.length);
      words.forEach((w, i) => w.classList.toggle("on", i < n));
    }
    // 一句话派活
    if (dispatch) {
      const r = dispatch.getBoundingClientRect();
      const p = clamp((-r.top) / (r.height - vh));
      const tp = clamp((p - .04) / .26);
      promptQ.textContent = Q.slice(0, Math.round(tp * Q.length));
      prompt.classList.toggle("sent", p > .33);
      stages.forEach((s) => { const at = parseFloat(s.dataset.at); const k = clamp((p - at) / .12); s.style.setProperty("--p", k); s.classList.toggle("on", k > 0); s.classList.toggle("done", k >= 1); });
      deliver.classList.toggle("on", p > .93);
    }
    // 横向
    if (caps && track && horizontal()) {
      const r = caps.getBoundingClientRect(); const dist = track.scrollWidth - innerWidth;
      const p = clamp(-r.top / Math.max(1, r.height - vh));
      track.style.transform = `translate3d(${-p * dist}px,0,0)`;
      const cards = 7; const idx = clamp(Math.round(p * cards), 1, cards);
      capsN && (capsN.textContent = String(idx).padStart(2, "0"));
      capsBar && capsBar.style.setProperty("--p", p);
    }
    // 3D 形态：取视口中线所在区块；区块之间按中线位置连续过渡
    if (world) {
      const mid = vh * .5; let stage = 0, off = 0, dim = 1;
      for (let i = 0; i < stageEls.length; i++) {
        const r = stageEls[i].getBoundingClientRect();
        if (r.top <= mid && r.bottom > mid) {
          stage = +stageEls[i].dataset.stage; off = +(stageEls[i].dataset.offset || 0); dim = +(stageEls[i].dataset.dim || 1);
          const next = stageEls[i + 1];
          if (next) { const k = clamp((mid - (r.bottom - vh * .35)) / (vh * .35)); stage += (+next.dataset.stage - stage) * k; off += (+(next.dataset.offset || 0) - off) * k; dim += (+(next.dataset.dim || 1) - dim) * k; }
          break;
        }
      }
      if (innerWidth <= 860) off = 0;
      world.setStage(stage, off, dim);
    }
    if (hudFlow) hudFlow.textContent = String(Math.floor(performance.now() / 37) % 10000).padStart(4, "0");
  }
  let ticking = false;
  const onScroll = () => { if (!ticking) { ticking = true; requestAnimationFrame(() => { ticking = false; tick(); }); } };
  addEventListener("scroll", onScroll, { passive: true });
  addEventListener("resize", () => { sizeCaps(); tick(); });
  if (hudFlow && !reduced) setInterval(() => { hudFlow.textContent = String(Math.floor(performance.now() / 37) % 10000).padStart(4, "0"); }, 120);

  // ---------- 磁吸 + 光标 ----------
  if (fine && !reduced) {
    const cur = $(".cursor"), dot = $(".cursor-dot");
    let x = innerWidth / 2, y = innerHeight / 2, cx = x, cy = y;
    addEventListener("pointermove", (e) => { x = e.clientX; y = e.clientY; cur.classList.add("on"); dot.classList.add("on"); dot.style.transform = `translate3d(${x}px,${y}px,0)`; }, { passive: true });
    document.addEventListener("pointerleave", () => { cur.classList.remove("on"); dot.classList.remove("on"); });
    (function loop() { cx += (x - cx) * .18; cy += (y - cy) * .18; cur.style.transform = `translate3d(${cx}px,${cy}px,0)`; requestAnimationFrame(loop); })();
    document.addEventListener("pointerover", (e) => { if (e.target.closest("a,button,select,input,.ind-row,.cap,video")) cur.classList.add("hover"); });
    document.addEventListener("pointerout", (e) => { if (e.target.closest("a,button,select,input,.ind-row,.cap,video")) cur.classList.remove("hover"); });
    $$(".magnetic").forEach((el) => {
      el.addEventListener("pointermove", (e) => { const r = el.getBoundingClientRect(); const dx = e.clientX - (r.left + r.width / 2), dy = e.clientY - (r.top + r.height / 2); el.style.transform = `translate(${dx * .22}px,${dy * .32}px)`; });
      el.addEventListener("pointerleave", () => { el.style.transition = "transform .6s cubic-bezier(.16,1,.3,1)"; el.style.transform = ""; setTimeout(() => (el.style.transition = ""), 600); });
    });
  }

  // ---------- 预加载 ----------
  const loader = $("#loader"), pct = $("#ld-pct"), word = $(".ld-word");
  function finish() {
    if (!loader || loader.classList.contains("done")) return;
    loader.classList.add("done");
    setTimeout(() => { $$(".hero [data-split],.hero [data-reveal]").forEach((el) => el.classList.add("in")); }, reduced ? 0 : 280);
    setTimeout(() => loader.remove(), 1300);
  }
  if (reduced || !loader) { finish(); }
  else {
    let p = 0, ready = false; const t0 = performance.now();
    const done = () => { ready = true; };
    if (document.readyState === "complete") done(); else addEventListener("load", done);
    addEventListener("paihuo:world", done);
    (function step() {
      const el = performance.now() - t0;
      const target = ready ? Math.min(100, el / 13) : Math.min(88, el / 18);
      p += (target - p) * .12;
      const v = Math.round(p); pct.textContent = String(v).padStart(3, "0"); word.style.setProperty("--p", v + "%");
      if (v >= 99 && el > 1400) return setTimeout(finish, 160);
      if (el > 4500) return finish();
      requestAnimationFrame(step);
    })();
  }

  sizeCaps(); watchReveals(); tick();
  document.fonts && document.fonts.ready.then(() => { sizeCaps(); tick(); });
})();
