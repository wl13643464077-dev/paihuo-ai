// 宣传页声音：WebAudio 实时合成，无音频文件。默认关闭，仅在用户点击“声音”后创建音频上下文。
//   环境声床：随 3D 形态切换和弦；交互音：悬停、点击、换段、打字、工位完成、终章绽放。
const CHORDS = [
  [48, 55, 64, 71],   // 0 核心：Cmaj7
  [45, 52, 60, 63],   // 1 失序：Am(add b3)，略不安
  [50, 57, 62, 69],   // 2 任务流：D5 开阔
  [41, 48, 57, 64],   // 3 行业星环：Fmaj7
  [43, 50, 59, 66],   // 4 流水线：G(add #11)，向前
  [48, 55, 64, 72],   // 5 终章：C 大三和弦
];
const PENTA = [72, 74, 76, 79, 81, 84, 86, 88];
const hz = (m) => 440 * Math.pow(2, (m - 69) / 12);

export function createSound() {
  const ctx = new (window.AudioContext || window.webkitAudioContext)();
  const master = ctx.createGain(); master.gain.value = 0;
  const comp = ctx.createDynamicsCompressor(); comp.threshold.value = -18; comp.ratio.value = 3;
  master.connect(comp).connect(ctx.destination);

  // 简易混响：衰减噪声脉冲响应
  const rev = ctx.createConvolver();
  const len = ctx.sampleRate * 2.4, ir = ctx.createBuffer(2, len, ctx.sampleRate);
  for (let c = 0; c < 2; c++) { const d = ir.getChannelData(c); for (let i = 0; i < len; i++) d[i] = (Math.random() * 2 - 1) * Math.pow(1 - i / len, 3); }
  rev.buffer = ir; const revGain = ctx.createGain(); revGain.gain.value = .35; rev.connect(revGain).connect(master);
  const fx = ctx.createGain(); fx.gain.value = .9; fx.connect(master); fx.connect(rev);

  // 环境声床
  const bedFilter = ctx.createBiquadFilter(); bedFilter.type = "lowpass"; bedFilter.frequency.value = 700; bedFilter.Q.value = .7;
  const bed = ctx.createGain(); bed.gain.value = .16; bedFilter.connect(bed).connect(master); bed.connect(rev);
  const lfo = ctx.createOscillator(); lfo.frequency.value = .07; const lfoAmt = ctx.createGain(); lfoAmt.gain.value = 260; lfo.connect(lfoAmt).connect(bedFilter.frequency); lfo.start();
  const voices = CHORDS[0].map((m, i) => {
    const o1 = ctx.createOscillator(), o2 = ctx.createOscillator(), g = ctx.createGain();
    o1.type = "triangle"; o2.type = "sine"; o1.frequency.value = hz(m); o2.frequency.value = hz(m) * 1.003;
    g.gain.value = i === 0 ? .5 : .28; o1.connect(g); o2.connect(g); g.connect(bedFilter); o1.start(); o2.start();
    return { o1, o2 };
  });
  // 空气感噪声
  const nbuf = ctx.createBuffer(1, ctx.sampleRate * 2, ctx.sampleRate); const nd = nbuf.getChannelData(0); for (let i = 0; i < nd.length; i++) nd[i] = Math.random() * 2 - 1;
  const air = ctx.createBufferSource(); air.buffer = nbuf; air.loop = true;
  const airF = ctx.createBiquadFilter(); airF.type = "bandpass"; airF.frequency.value = 2400; airF.Q.value = .6;
  const airG = ctx.createGain(); airG.gain.value = .012; air.connect(airF).connect(airG).connect(master); air.start();

  let stageIdx = 0, lastHover = 0, on = false;
  const now = () => ctx.currentTime;
  function env(g, t, a, peak, d) { g.gain.cancelScheduledValues(t); g.gain.setValueAtTime(0, t); g.gain.linearRampToValueAtTime(peak, t + a); g.gain.exponentialRampToValueAtTime(.0001, t + a + d); }
  function tone(freq, { type = "sine", peak = .12, a = .005, d = .25, dest = fx, detune = 0 } = {}) {
    const t = now(), o = ctx.createOscillator(), g = ctx.createGain(); o.type = type; o.frequency.value = freq; o.detune.value = detune;
    o.connect(g).connect(dest); env(g, t, a, peak, d); o.start(t); o.stop(t + a + d + .05);
  }
  function noiseHit({ f0 = 400, f1 = 4000, dur = .5, peak = .12, q = 1.2 } = {}) {
    const t = now(), s = ctx.createBufferSource(), f = ctx.createBiquadFilter(), g = ctx.createGain();
    s.buffer = nbuf; f.type = "bandpass"; f.Q.value = q; f.frequency.setValueAtTime(f0, t); f.frequency.exponentialRampToValueAtTime(f1, t + dur);
    s.connect(f).connect(g).connect(fx); g.gain.setValueAtTime(0, t); g.gain.linearRampToValueAtTime(peak, t + dur * .45); g.gain.exponentialRampToValueAtTime(.0001, t + dur);
    s.start(t); s.stop(t + dur + .05);
  }

  const api = {
    get on() { return on; },
    async enable() { await ctx.resume(); on = true; CHORDS[stageIdx].forEach((m, k) => { voices[k].o1.frequency.setValueAtTime(hz(m), now()); voices[k].o2.frequency.setValueAtTime(hz(m) * 1.003, now()); }); master.gain.cancelScheduledValues(now()); master.gain.setTargetAtTime(.55, now(), .6); tone(hz(84), { peak: .08, d: 1.2 }); tone(hz(91), { peak: .05, d: 1.4 }); },
    async disable() { on = false; master.gain.setTargetAtTime(0, now(), .25); setTimeout(() => { if (!on) ctx.suspend(); }, 900); },
    stage(v) {
      const i = Math.max(0, Math.min(5, Math.round(v))); if (i === stageIdx) return; stageIdx = i;
      if (!on) return;
      CHORDS[i].forEach((m, k) => { voices[k].o1.frequency.setTargetAtTime(hz(m), now(), .8); voices[k].o2.frequency.setTargetAtTime(hz(m) * 1.003, now(), .8); });
      bedFilter.frequency.setTargetAtTime(i === 1 ? 420 : i === 5 ? 1300 : 760, now(), 1.2);
      noiseHit({ f0: 300, f1: 3600, dur: .7, peak: .05 });
      if (i === 5) CHORDS[5].forEach((m, k) => setTimeout(() => tone(hz(m + 12), { type: "triangle", peak: .05, a: .02, d: 2.6 }), k * 90));
    },
    hover() { if (!on) return; const t = performance.now(); if (t - lastHover < 70) return; lastHover = t; tone(2200 + Math.random() * 400, { peak: .025, d: .05 }); },
    click() { if (!on) return; tone(hz(76), { type: "triangle", peak: .09, d: .22 }); tone(hz(83), { peak: .04, d: .3 }); },
    type() { if (!on) return; noiseHit({ f0: 3000, f1: 2600, dur: .035, peak: .05, q: 3 }); },
    chime(i) { if (!on) return; tone(hz(PENTA[i % PENTA.length]), { peak: .07, d: 1.1 }); tone(hz(PENTA[i % PENTA.length] + 12), { peak: .025, d: .9 }); },
    send() { if (!on) return; noiseHit({ f0: 600, f1: 5000, dur: .35, peak: .07 }); tone(hz(79), { type: "triangle", peak: .07, d: .4 }); },
  };
  return api;
}
