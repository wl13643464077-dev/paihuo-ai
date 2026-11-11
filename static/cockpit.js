/* 老板数据驾驶舱（#/cockpit）：一屏看清员工用派活做了什么、门店执行、等老板拍板的事。
   数据全部来自 /api/cockpit/summary（派活自己的业务表），不接外部数据、不做估算。
   导出 Excel / 打印 PDF、AI 经营分析、下发给门店或具体的人（复用「派给店员」）、追踪下发结果。
   依赖 app.js 全局：routes / api / esc / toast / $ / ME / isOwnerLike。 */
(function(){
  "use strict";
  const CP = {days:7, data:null, meta:null, aiBusy:false, pick:{}, peopleAll:false};

  function style(){
    if(document.getElementById("cp-style")) return;
    const st=document.createElement("style"); st.id="cp-style";
    st.textContent=`
    .cp{--cp-navy:#1d2740;--cp-navy2:#2b3a5e;--cp-line:#e6dfd0;--cp-soft:#f7f3ea;--cp-up:#1f9d6b;--cp-down:#d64545;--cp-blue:#3d6fd9;--cp-amber:#e0a019;--cp-violet:#7c5cd6}
    .cp *{box-sizing:border-box}
    .cp-hero{background:linear-gradient(135deg,var(--cp-navy) 0%,var(--cp-navy2) 100%);color:#fff;border-radius:22px;padding:22px 24px;margin-bottom:16px;border:2.5px solid var(--ink);box-shadow:var(--shadow)}
    .cp-hero-top{display:flex;flex-wrap:wrap;gap:12px;align-items:flex-start;justify-content:space-between}
    .cp-eyebrow{font-size:12px;letter-spacing:2px;color:#ffd166;font-weight:800}
    .cp-hero h2{margin:4px 0 2px;font-size:24px;color:#fff}
    .cp-hero .cp-meta{font-size:12.5px;color:#b9c3dc}
    .cp-seg{display:inline-flex;background:rgba(255,255,255,.1);border-radius:12px;padding:3px;gap:2px}
    .cp-seg button{border:0;background:transparent;color:#d7deef;font-weight:800;font-size:13px;padding:6px 12px;border-radius:9px;cursor:pointer}
    .cp-seg button.on{background:#ffd166;color:var(--cp-navy)}
    .cp-acts{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-top:10px}
    .cp-btn{display:inline-flex;align-items:center;gap:6px;border:1.5px solid rgba(255,255,255,.35);background:rgba(255,255,255,.08);color:#fff;border-radius:11px;padding:7px 13px;font-weight:800;font-size:13px;cursor:pointer;text-decoration:none}
    .cp-btn:hover{background:rgba(255,255,255,.18)}
    .cp-btn.gold{background:#ffd166;color:var(--cp-navy);border-color:#ffd166}
    .cp-btn[disabled]{opacity:.55;cursor:wait}
    .cp-head-lines{margin:16px 0 0;padding:14px 16px;background:rgba(255,255,255,.07);border-radius:14px;font-size:14.5px;line-height:1.75}
    .cp-head-lines b{color:#ffd166}
    .cp-kpis{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin-bottom:16px}
    .cp-kpi{background:#fff;border:2px solid var(--ink);border-radius:16px;padding:14px 16px;position:relative;overflow:hidden}
    .cp-kpi::before{content:"";position:absolute;left:0;top:0;bottom:0;width:5px;background:var(--cp-accent,var(--cp-blue))}
    .cp-kpi .l{font-size:12.5px;color:var(--ink2);font-weight:800;display:flex;justify-content:space-between;gap:6px}
    .cp-kpi .v{font-size:30px;font-weight:900;margin:6px 0 2px;letter-spacing:-.5px;font-variant-numeric:tabular-nums}
    .cp-kpi .v small{font-size:13px;font-weight:800;color:var(--ink2);margin-left:3px}
    .cp-kpi .h{font-size:12px;color:var(--ink2);line-height:1.45}
    .cp-delta{font-size:11.5px;font-weight:900;padding:1px 7px;border-radius:99px;white-space:nowrap}
    .cp-delta.good{background:#dff5ea;color:var(--cp-up)} .cp-delta.bad{background:#fde4e4;color:var(--cp-down)} .cp-delta.flat{background:#efeae0;color:var(--ink2)}
    .cp-grid{display:grid;grid-template-columns:minmax(0,1.65fr) minmax(0,1fr);gap:16px;margin-bottom:16px}
    .cp-panel{background:var(--card);border:2.5px solid var(--ink);border-radius:20px;padding:18px 20px;box-shadow:var(--shadow);min-width:0}
    .cp-panel h3{margin:0 0 4px;font-size:16px;display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap}
    .cp-panel .cp-sub{font-size:12px;color:var(--ink2);margin-bottom:12px}
    .cp-legend{display:flex;gap:14px;flex-wrap:wrap;font-size:12px;color:var(--ink2);font-weight:700}
    .cp-legend i{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:5px;vertical-align:-1px}
    .cp-chart{width:100%;height:auto;display:block}
    .cp-chart text{font-size:10.5px;fill:#8a7d68;font-family:inherit}
    .cp-mix{display:flex;flex-direction:column;gap:9px}
    .cp-mix-row{display:grid;grid-template-columns:118px 1fr 36px;gap:8px;align-items:center;font-size:13px}
    .cp-mix-row .bar{height:12px;background:var(--cp-soft);border-radius:99px;overflow:hidden}
    .cp-mix-row .bar i{display:block;height:100%;border-radius:99px}
    .cp-mix-row b{text-align:right;font-variant-numeric:tabular-nums}
    .cp-list{display:flex;flex-direction:column;gap:8px;max-height:420px;overflow:auto;padding-right:2px}
    .cp-item{display:grid;grid-template-columns:auto 1fr auto;gap:10px;align-items:center;background:#fff;border:1.5px solid var(--cp-line);border-radius:13px;padding:10px 12px}
    .cp-item .dot{width:9px;height:9px;border-radius:50%}
    .cp-item .t{font-weight:800;font-size:13.5px;line-height:1.35}
    .cp-item .s{font-size:12px;color:var(--ink2);margin-top:2px}
    .cp-item .ops{display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end}
    .cp-mini{border:1.5px solid var(--ink);background:#fff;border-radius:9px;padding:3px 10px;font-size:12px;font-weight:800;cursor:pointer;color:var(--ink);text-decoration:none;white-space:nowrap}
    .cp-mini.pri{background:var(--cp-navy);color:#ffd166;border-color:var(--cp-navy)}
    .cp-tag{display:inline-block;font-size:11px;font-weight:800;padding:1px 8px;border-radius:99px;background:#efeae0;color:var(--ink2);margin-right:4px}
    .cp-tag.high{background:#fde4e4;color:var(--cp-down)} .cp-tag.mid{background:#fff1d1;color:#9a6a00} .cp-tag.ok{background:#dff5ea;color:var(--cp-up)} .cp-tag.blue{background:#e4ecfd;color:var(--cp-blue)}
    .cp-table-wrap{overflow:auto;margin:0 -4px}
    .cp-table{width:100%;border-collapse:collapse;font-size:13px;min-width:640px}
    .cp-table th{text-align:left;font-size:11.5px;color:var(--ink2);font-weight:800;padding:8px 8px;border-bottom:2px solid var(--cp-line);white-space:nowrap}
    .cp-table td{padding:9px 8px;border-bottom:1px solid var(--cp-line);vertical-align:middle}
    .cp-table td.n{text-align:right;font-variant-numeric:tabular-nums;font-weight:800}
    .cp-table tr:hover td{background:#fffdf6}
    .cp-who{display:flex;align-items:center;gap:9px}
    .cp-av{width:30px;height:30px;border-radius:50%;background:var(--cp-navy);color:#ffd166;display:grid;place-items:center;font-weight:900;font-size:13px;flex:none}
    .cp-spark{display:inline-block;height:8px;border-radius:99px;background:var(--cp-blue);vertical-align:middle;margin-right:6px;min-width:2px}
    .cp-stores{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:12px}
    .cp-store{background:#fff;border:2px solid var(--ink);border-radius:16px;padding:14px}
    .cp-store.warn{border-color:var(--cp-down);box-shadow:0 0 0 3px #fde4e4}
    .cp-store-top{display:flex;align-items:center;gap:12px}
    .cp-ring{flex:none}
    .cp-ring text{font-family:inherit}
    .cp-comp{display:grid;grid-template-columns:72px 1fr 34px;gap:6px;align-items:center;font-size:11.5px;color:var(--ink2);margin-top:5px}
    .cp-comp .bar{height:6px;background:var(--cp-soft);border-radius:99px;overflow:hidden}
    .cp-comp .bar i{display:block;height:100%;background:var(--cp-blue)}
    .cp-ai{background:linear-gradient(180deg,#f5f1ff,#fff);border-color:var(--cp-violet)}
    .cp-ai .verdict{font-size:16px;font-weight:900;line-height:1.5;margin:6px 0 10px}
    .cp-ai ul{margin:4px 0 10px;padding-left:18px;font-size:13px;line-height:1.7}
    .cp-ai .sec{font-size:12px;font-weight:900;color:var(--cp-violet);letter-spacing:1px;margin-top:8px}
    .cp-empty{color:var(--ink2);font-size:13px;text-align:center;padding:26px 8px}
    .cp-foot{font-size:12px;color:var(--ink2);text-align:center;margin:8px 0 20px}
    .cp-modal{position:fixed;inset:0;background:rgba(20,24,36,.55);z-index:200;display:grid;place-items:center;padding:16px}
    .cp-dialog{background:var(--card);border:2.5px solid var(--ink);border-radius:20px;width:min(520px,100%);max-height:92vh;overflow:auto;padding:20px 22px;box-shadow:var(--shadow)}
    .cp-dialog h3{margin:0 0 12px}
    .cp-dialog label{display:block;font-size:12.5px;font-weight:800;margin:10px 0 4px}
    .cp-lv{display:flex;gap:6px;flex-wrap:wrap}
    .cp-lv button{border:2px solid var(--ink);background:#fff;border-radius:10px;padding:6px 12px;font-weight:800;cursor:pointer;font-size:13px}
    .cp-lv button.on{background:var(--cp-navy);color:#ffd166}
    .cp-row2{display:grid;grid-template-columns:1fr 1fr;gap:10px}
    @media(max-width:900px){.cp-kpis{grid-template-columns:repeat(2,minmax(0,1fr))}.cp-grid{grid-template-columns:1fr}}
    @media(max-width:520px){.cp-hero{padding:18px 16px}.cp-kpi .v{font-size:24px}.cp-mix-row{grid-template-columns:96px 1fr 30px}.cp-item{grid-template-columns:auto 1fr}.cp-item .ops{grid-column:1/-1;justify-content:flex-start}.cp-row2{grid-template-columns:1fr}}
    @media print{header,nav,.cp-acts,.cp-mini,.cp-noprint{display:none!important}.cp-hero{background:#1d2740!important;-webkit-print-color-adjust:exact;print-color-adjust:exact}.cp-panel,.cp-kpi,.cp-store{break-inside:avoid;box-shadow:none}.cp-list{max-height:none;overflow:visible}body{background:#fff}}
    `;
    document.head.appendChild(st);
  }

  /* ---------- 小工具 ---------- */
  const num = (v,d=0)=> v==null ? "—" : Number(v).toLocaleString("zh-CN",{maximumFractionDigits:d});
  // 一律按北京时间显示，和服务器、店员端口径一致
  const CN = t => { const p={}; new Intl.DateTimeFormat("zh-CN",{timeZone:"Asia/Shanghai",year:"numeric",month:"numeric",day:"numeric",hour:"2-digit",minute:"2-digit",hour12:false})
    .formatToParts(new Date(t*1000)).forEach(x=>p[x.type]=x.value); return p; };
  const when = t => { if(!t) return "—"; const now=Date.now()/1000, diff=now-t;
    if(diff>=0&&diff<3600) return `${Math.max(1,Math.round(diff/60))} 分钟前`;
    if(diff>=0&&diff<86400) return `${Math.round(diff/3600)} 小时前`;
    const p=CN(t); return `${p.month}月${p.day}日 ${p.hour}:${p.minute}`; };
  const day = t => { if(!t) return "—"; const p=CN(t); return `${p.month}/${p.day}`; };
  function dueText(days){ const p=CN(Date.now()/1000+Math.max(1,days||3)*86400); return `${p.year}-${String(p.month).padStart(2,"0")}-${String(p.day).padStart(2,"0")}`; }
  function delta(k){
    if(k.delta==null) return `<span class="cp-delta flat">上期无数据</span>`;
    if(k.delta===0) return `<span class="cp-delta flat">持平</span>`;
    const better = k.good==="down" ? k.delta<0 : k.delta>0;
    return `<span class="cp-delta ${better?"good":"bad"}">${k.delta>0?"↑":"↓"} ${num(Math.abs(k.delta),1)}${k.unit==="%"?" 个点":""}</span>`;
  }
  const ACCENT = {dispatched:"#3d6fd9", ai_done:"#7c5cd6", staff_done:"#1f9d6b", staff_rate:"#1f9d6b",
    checklist_rate:"#e0a019", inspection:"#e07a19", active_people:"#3d6fd9", points:"#8a7d68"};
  const MIX_COLOR = {expert:"#7c5cd6", content:"#3d6fd9", video:"#d6458f", tool:"#e07a19", meeting:"#5b6b8a",
    staff:"#1f9d6b", checklist:"#e0a019", inspection:"#c2552d"};

  /* ---------- 图表（纯 SVG） ---------- */
  function trendChart(rows){
    if(!rows?.length) return `<div class="cp-empty">暂无数据</div>`;
    const W=640,H=220,L=30,R=8,T=12,B=26, iw=W-L-R, ih=H-T-B;
    const max=Math.max(1,...rows.map(r=>r.ai+r.staff+r.checklist));
    const step=iw/rows.length, bw=Math.max(3,Math.min(26,step*.62));
    const ticks=[0,.5,1].map(f=>Math.round(max*f));
    let g=ticks.map(v=>{const y=T+ih-(v/max)*ih;return `<line x1="${L}" x2="${W-R}" y1="${y}" y2="${y}" stroke="#ece5d6" stroke-dasharray="${v?"3 4":""}"/><text x="${L-6}" y="${y+3.5}" text-anchor="end">${v}</text>`;}).join("");
    const every=Math.ceil(rows.length/10);
    rows.forEach((r,i)=>{
      const x=L+i*step+(step-bw)/2; let y=T+ih;
      [["checklist","#e0a019"],["staff","#1f9d6b"],["ai","#7c5cd6"]].forEach(([k,c])=>{
        const h=(r[k]/max)*ih; if(h>0){ y-=h; g+=`<rect x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${bw.toFixed(1)}" height="${h.toFixed(1)}" rx="2.5" fill="${c}"><title>${esc(r.day)}  AI交付 ${r.ai} · 店员交差 ${r.staff} · 清单 ${r.checklist}</title></rect>`; }
      });
      if(i%every===0||i===rows.length-1) g+=`<text x="${(L+i*step+step/2).toFixed(1)}" y="${H-8}" text-anchor="middle">${esc(r.day.slice(5).replace("-","/"))}</text>`;
    });
    return `<svg class="cp-chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="每日完成趋势">${g}</svg>`;
  }
  function ring(score){
    const r=26,c=2*Math.PI*r,v=score==null?0:Math.max(0,Math.min(100,score));
    const col=score==null?"#cfc6b4":v>=80?"#1f9d6b":v>=60?"#e0a019":"#d64545";
    return `<svg class="cp-ring" width="64" height="64" viewBox="0 0 64 64"><circle cx="32" cy="32" r="${r}" fill="none" stroke="#efe8da" stroke-width="7"/>
      <circle cx="32" cy="32" r="${r}" fill="none" stroke="${col}" stroke-width="7" stroke-linecap="round" stroke-dasharray="${(c*v/100).toFixed(1)} ${c.toFixed(1)}" transform="rotate(-90 32 32)"/>
      <text x="32" y="37" text-anchor="middle" font-size="16" font-weight="900" fill="#33291f">${score==null?"—":Math.round(score)}</text></svg>`;
  }

  /* ---------- 渲染 ---------- */
  async function view(){
    style();
    if(!isOwnerLike()){ $("#main").innerHTML=`<div class="card"><div class="empty">数据驾驶舱只有老板账号能看。</div></div>`; return; }
    $("#main").innerHTML=`<div class="cp"><div class="cp-hero"><div class="cp-eyebrow">派活 · 数据驾驶舱</div><h2>正在汇总…</h2><div class="cp-meta"><span class="spin"></span> 从派活里统计员工、门店和 AI 的工作记录</div></div></div>`;
    try{
      const [data, meta] = await Promise.all([
        api(`/cockpit/summary?days=${CP.days}`),
        api("/staff/meta").catch(e=>{ if(e?.name==="NavigationAbort") throw e; return null; }),
      ]);
      CP.data=data; CP.meta=meta;
    }catch(e){
      if(e?.name==="NavigationAbort") return;
      $("#main").innerHTML=`<div class="card"><div class="empty">${esc(e.message)}</div></div>`; return;
    }
    draw();
  }

  function draw(){
    const d=CP.data, k=d.kpis;
    const order=["dispatched","ai_done","staff_done","staff_rate","checklist_rate","inspection","active_people","points"];
    const kpis=order.map(key=>{const x=k[key];return `<div class="cp-kpi" style="--cp-accent:${ACCENT[key]}">
      <div class="l"><span>${esc(x.label)}</span>${delta(x)}</div>
      <div class="v">${num(x.value,1)}<small>${x.value==null?"":esc(x.unit)}</small></div>
      <div class="h">${esc(x.hint)}</div></div>`;}).join("");
    const mixMax=Math.max(1,...d.mix.map(m=>m.count));
    const mix=d.mix.map(m=>`<div class="cp-mix-row"><span>${esc(m.label)}</span><div class="bar"><i style="width:${(m.count/mixMax*100).toFixed(1)}%;background:${MIX_COLOR[m.key]||"#3d6fd9"}"></i></div><b>${num(m.count)}</b></div>`).join("");
    const head=(d.headline||[]).map(x=>esc(x).replace(/(\d+(?:\.\d+)?)/g,"<b>$1</b>")).join("<br>");
    $("#main").innerHTML=`<div class="cp">
      <section class="cp-hero">
        <div class="cp-hero-top">
          <div><div class="cp-eyebrow">派活 · 数据驾驶舱</div><h2>${esc(d.tenant.name||"我的企业")}</h2>
            <div class="cp-meta">近 ${d.period.days} 天 · ${esc(fmtRange(d.period))} · 点数余额 ${num(d.tenant.balance,1)} · 更新于 ${esc(when(d.generated_at))}</div></div>
          <div class="cp-seg" role="tablist">${d.period.options.map(n=>`<button type="button" class="${n===d.period.days?"on":""}" onclick="cpDays(${n})">近 ${n} 天</button>`).join("")}</div>
        </div>
        <div class="cp-acts">
          <button type="button" class="cp-btn gold" id="cp-ai-btn" onclick="cpAi()" ${CP.aiBusy?"disabled":""}>✨ ${CP.aiBusy?"AI 正在分析…":"AI 经营分析"}</button>
          <a class="cp-btn" href="/api/cockpit/export.xlsx?days=${d.period.days}">⬇ 导出 Excel</a>
          <button type="button" class="cp-btn" onclick="window.print()">🖨 打印 / 存 PDF</button>
          <button type="button" class="cp-btn" onclick="cpDispatch(null)">📤 下发任务</button>
          <button type="button" class="cp-btn" onclick="cpReload()">↻ 刷新</button>
        </div>
        <div class="cp-head-lines">${head||"这段时间还没有记录。"}</div>
      </section>

      <div class="cp-kpis">${kpis}</div>

      <div class="cp-grid">
        <section class="cp-panel"><h3>每天完成了多少<span class="cp-legend"><span><i style="background:#7c5cd6"></i>AI 交付</span><span><i style="background:#1f9d6b"></i>店员交差</span><span><i style="background:#e0a019"></i>清单完成</span></span></h3>
          <div class="cp-sub">按北京时间自然日统计，鼠标停在柱子上看明细</div>${trendChart(d.trend)}</section>
        <section class="cp-panel"><h3>都做了什么</h3><div class="cp-sub">本期完成的工作，按类型</div><div class="cp-mix">${mix}</div></section>
      </div>

      <div class="cp-grid">
        <section class="cp-panel"><h3>等您拍板 <span>${d.todo_counts.high?`<span class="cp-tag high">逾期 ${d.todo_counts.high}</span>`:""}<span class="cp-tag">${d.todo_counts.total} 件</span></span></h3>
          <div class="cp-sub">逾期的排在最前；逾期和高风险的事可以直接下发给门店或负责人</div>
          <div class="cp-list">${todoList(d.todo)}</div></section>
        ${aiPanel(d.ai_analysis)}
      </div>

      <section class="cp-panel" style="margin-bottom:16px"><h3>员工用派活做了什么
          ${(()=>{const nv=d.people.filter(p=>p.flag==="never"&&!isOwnerRole(p)).length, idl=d.people.filter(p=>p.flag==="idle"&&!isOwnerRole(p)).length;
            return `<span><span class="cp-tag ${nv?"high":""}">从没用过 ${nv}</span><span class="cp-tag ${idl?"mid":""}">7 天没用 ${idl}</span></span>`;})()}</h3>
        <div class="cp-sub">每个账号本期派给 AI 的活、派给店员的活、自己交差的任务和打卡的清单；“最近使用”看全部历史</div>
        ${peopleTable(d.people)}</section>

      <section class="cp-panel" style="margin-bottom:16px"><h3>门店表现 <a class="cp-mini" href="#/store-rank">查看排行和算法</a></h3>
        <div class="cp-sub">综合分 = 清单按时 40% + 整改按时 30% + 派活按时 20% + 巡店问题分 10%（和「门店排行」同一套算法）</div>
        ${storeCards(d.stores)}</section>

      <div class="cp-grid">
        <section class="cp-panel"><h3>下发追踪 <span class="cp-tag">${d.tracking.length} 件</span></h3>
          <div class="cp-sub">从驾驶舱下发出去的事，店员在派活里拍照交差，状态实时更新</div>${trackTable(d.tracking)}</section>
        <section class="cp-panel"><h3>AI 数字员工产出</h3><div class="cp-sub">本期交付最多的 AI 员工</div>${aiEmployees(d.ai_employees)}
          <h3 style="margin-top:16px">内容发布</h3><div class="cp-sub">发布台账里本期登记的发布</div>${content(d.content)}</section>
      </div>
      <div class="cp-foot">${esc(d.notes||"")}</div>
    </div>`;
  }
  const isOwnerRole = p => p.role==="owner"||p.role==="root";
  function fmtRange(p){ return `${day(p.start)} – ${day(p.end)}`; }

  function todoList(items){
    if(!items.length) return `<div class="cp-empty">🎉 没有等您处理的事</div>`;
    const col={high:"#d64545",mid:"#e0a019",normal:"#3d6fd9"};
    CP.pick.todo=items;
    return items.map((t,i)=>`<div class="cp-item"><span class="dot" style="background:${col[t.level]||"#3d6fd9"}"></span>
      <div><div class="t">${esc(t.title||"未命名")}</div>
        <div class="s"><span class="cp-tag ${t.level==="high"?"high":t.level==="mid"?"mid":"blue"}">${esc(t.label)}</span>${t.branch?esc(t.branch)+" · ":""}${t.who?esc(t.who)+" · ":""}${esc(when(t.since))}</div></div>
      <div class="ops"><a class="cp-mini" href="${esc(t.route||"#/")}">去处理</a>${t.dispatchable?`<button type="button" class="cp-mini pri" onclick="cpDispatchTodo(${i})">下发</button>`:""}</div></div>`).join("");
  }

  function aiPanel(a){
    if(!a) return `<section class="cp-panel cp-ai"><h3>✨ AI 经营分析</h3>
      <div class="cp-sub">AI 只读上面这些派活里的统计，帮您找出做得好的、要警惕的，并给出可以直接下发的动作。每次 1 点，失败自动退。</div>
      <div class="cp-empty"><button type="button" class="cp-mini pri" onclick="cpAi()" ${CP.aiBusy?"disabled":""}>${CP.aiBusy?"分析中…":"开始分析"}</button></div></section>`;
    CP.pick.actions=a.actions||[];
    const acts=(a.actions||[]).map((x,i)=>`<div class="cp-item" style="grid-template-columns:1fr auto"><div><div class="t">${esc(x.title)}</div>
      <div class="s">${x.priority==="high"?`<span class="cp-tag high">紧急</span>`:""}${x.branch?esc(x.branch)+" · ":""}${x.owner?esc(x.owner)+" · ":""}${x.due_days} 天内${x.detail?" · "+esc(x.detail):""}</div></div>
      <div class="ops"><button type="button" class="cp-mini pri" onclick="cpDispatchAi(${i})">下发</button></div></div>`).join("");
    return `<section class="cp-panel cp-ai"><h3>✨ AI 经营分析 <button type="button" class="cp-mini cp-noprint" onclick="cpAi()" ${CP.aiBusy?"disabled":""}>${CP.aiBusy?"分析中…":"重新分析"}</button></h3>
      <div class="cp-sub">基于近 ${a.days} 天数据 · ${esc(when(a.created_at))}</div>
      <div class="verdict">${esc(a.verdict)}</div>
      ${a.highlights?.length?`<div class="sec">做得好</div><ul>${a.highlights.map(x=>`<li>${esc(x)}</li>`).join("")}</ul>`:""}
      ${a.risks?.length?`<div class="sec">要警惕</div><ul>${a.risks.map(x=>`<li>${esc(x)}</li>`).join("")}</ul>`:""}
      ${acts?`<div class="sec">建议下发</div><div class="cp-list" style="margin-top:6px">${acts}</div>`:""}</section>`;
  }

  function peopleTable(rows){
    if(!rows.length) return `<div class="cp-empty">还没有员工账号，去「团队与权限」给店长、店员开账号</div>`;
    const list=CP.peopleAll?rows:rows.slice(0,12);
    const max=Math.max(1,...rows.map(r=>r.total));
    CP.pick.people=list;
    const flag=p=>isOwnerRole(p)?"":p.flag==="never"?`<span class="cp-tag high">从没用过</span>`:p.flag==="idle"?`<span class="cp-tag mid">${p.idle_days} 天没用</span>`:p.flag==="overdue"?`<span class="cp-tag high">有逾期</span>`:`<span class="cp-tag ok">正常</span>`;
    const body=list.map((p,i)=>`<tr><td><div class="cp-who"><span class="cp-av">${esc((p.name||"?").slice(0,1).toUpperCase())}</span><div><b>${esc(p.name)}</b><div class="sub">${esc(p.role_label)}${p.branches.length?" · "+esc(p.branches.join("、")):""}</div></div></div></td>
      <td><span class="cp-spark" style="width:${(p.total/max*90).toFixed(0)}px"></span>${num(p.total)}</td>
      <td class="n">${num(p.ai_tasks)}</td><td class="n">${num(p.dispatched)}</td><td class="n">${num(p.staff_done)}</td><td class="n">${num(p.checklists)}</td>
      <td class="n" style="color:${p.overdue?"var(--cp-down)":"inherit"}">${num(p.overdue)}</td>
      <td>${esc(when(p.last_active))}</td><td>${flag(p)}</td>
      <td>${isOwnerRole(p)?"":`<button type="button" class="cp-mini" onclick="cpDispatchPerson(${i})">派活</button>`}</td></tr>`).join("");
    return `<div class="cp-table-wrap"><table class="cp-table"><thead><tr><th>账号</th><th>本期总量</th><th style="text-align:right">派给AI</th><th style="text-align:right">派给店员</th><th style="text-align:right">交差</th><th style="text-align:right">清单</th><th style="text-align:right">逾期</th><th>最近使用</th><th>状态</th><th></th></tr></thead><tbody>${body}</tbody></table></div>
      ${rows.length>12?`<div style="text-align:center;margin-top:8px"><button type="button" class="cp-mini" onclick="cpPeopleAll()">${CP.peopleAll?"收起":`看全部 ${rows.length} 个账号`}</button></div>`:""}`;
  }

  function storeCards(rows){
    if(!rows.length) return `<div class="cp-empty">还没有门店。在「巡店」里添加门店后，这里会显示每家店的执行情况</div>`;
    CP.pick.stores=rows;
    const comp=(label,v)=>`<div class="cp-comp"><span>${label}</span><div class="bar"><i style="width:${v==null?0:Math.max(0,Math.min(100,v))}%"></i></div><span>${v==null?"—":Math.round(v)}</span></div>`;
    return `<div class="cp-stores">${rows.map((s,i)=>`<div class="cp-store ${s.needs_attention?"warn":""}">
      <div class="cp-store-top">${ring(s.score)}<div style="min-width:0;flex:1"><b style="font-size:15px">${s.rank?`No.${s.rank} `:""}${esc(s.name)}</b>
        <div class="sub">${s.delta==null?"上期无数据":s.delta>0?`<span style="color:var(--cp-up);font-weight:800">较上期 ↑${num(s.delta,1)}</span>`:s.delta<0?`<span style="color:var(--cp-down);font-weight:800">较上期 ↓${num(Math.abs(s.delta),1)}</span>`:"较上期持平"}${s.open_issues?` · 未闭环问题 ${s.open_issues}`:""}</div>
        ${s.needs_attention?`<span class="cp-tag high" style="margin-top:4px">要盯一下</span>`:""}</div></div>
      ${comp("清单按时",s.checklist)}${comp("整改按时",s.action)}${comp("派活按时",s.task)}${comp("巡店问题分",s.issue)}
      ${s.reasons?.length?`<div class="sub" style="margin-top:8px;line-height:1.5">${s.reasons.map(esc).join("；")}</div>`:""}
      <div style="margin-top:10px;display:flex;gap:6px"><button type="button" class="cp-mini pri" onclick="cpDispatchStore(${i})">下发到这家店</button></div></div>`).join("")}</div>`;
  }

  function trackTable(rows){
    if(!rows.length) return `<div class="cp-empty">还没从驾驶舱下发过任务。点「下发」把事情交给门店或具体的人，这里会跟踪进度</div>`;
    const cls=t=>t.overdue||t.status==="rejected"?"high":t.status==="approved"?"ok":t.status==="submitted"?"blue":"";
    return `<div class="cp-table-wrap"><table class="cp-table" style="min-width:520px"><thead><tr><th>事项</th><th>门店 / 负责人</th><th>状态</th><th>截止</th></tr></thead><tbody>
      ${rows.map(t=>`<tr><td><b>${esc(t.title)}</b>${t.priority==="high"?` <span class="cp-tag high">紧急</span>`:""}<div class="sub">下发于 ${esc(when(t.created_at))}</div></td>
        <td>${esc(t.branch||"—")}<div class="sub">${esc(t.who)}</div></td><td><span class="cp-tag ${cls(t)}">${esc(t.status_label)}</span></td><td>${esc(when(t.due_at))}</td></tr>`).join("")}
      </tbody></table></div><div style="margin-top:8px"><a class="cp-mini" href="#/staff-tasks">在「派给店员」里查看照片和验收</a></div>`;
  }

  function aiEmployees(rows){
    if(!rows.length) return `<div class="cp-empty">本期还没有 AI 员工交付</div>`;
    const max=Math.max(1,...rows.map(r=>r.count));
    return `<div class="cp-mix">${rows.map(r=>`<div class="cp-mix-row" style="grid-template-columns:150px 1fr 36px"><span title="${esc(r.dept)}"><b>${esc(r.name)}</b><div class="sub" style="font-size:11px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(r.dept)}</div></span><div class="bar"><i style="width:${(r.count/max*100).toFixed(1)}%;background:#7c5cd6"></i></div><b>${num(r.count)}</b></div>`).join("")}</div>`;
  }
  function content(c){
    const entries=Object.entries(c.platforms||{});
    if(!entries.length) return `<div class="cp-empty">本期发布台账里还没有记录</div>`;
    return `<div style="display:flex;gap:8px;flex-wrap:wrap">${entries.map(([p,n])=>`<span class="cp-tag blue" style="font-size:12.5px;padding:4px 12px">${esc(p)} · ${num(n)}</span>`).join("")}</div>`;
  }

  /* ---------- 交互 ---------- */
  window.cpDays = n => { CP.days=n; view(); };
  window.cpReload = () => view();
  window.cpPeopleAll = () => { CP.peopleAll=!CP.peopleAll; draw(); };
  window.cpAi = async () => {
    if(CP.aiBusy) return;
    CP.aiBusy=true; draw();
    try{
      const a=await api("/cockpit/ai",{method:"POST", body:{days:CP.days}, timeout:150000, longRunning:true});
      CP.data.ai_analysis=a; toast(a.points?`分析好了，扣 ${a.points} 点`:"分析好了");
    }catch(e){ toast(e.message); }
    finally{ CP.aiBusy=false; if(location.hash.startsWith("#/cockpit")) draw(); }
  };

  /* 下发：门店（全店）或门店里具体的人。复用 POST /api/staff/tasks。 */
  function branchesMeta(){ return (CP.meta?.branches)||[]; }
  function findUserBranch(uid){ return branchesMeta().find(b=>(b.members||[]).some(m=>m.id===uid)); }
  window.cpDispatchTodo = i => { const t=CP.pick.todo?.[i]; if(!t) return;
    openDispatch({title:`跟进：${t.title}`, detail:`${t.label}${t.who?`（原负责人 ${t.who}）`:""}，请尽快处理并拍照交差。`, branch_id:t.branch_id, priority:t.level==="high"?"high":"normal", days:1, ref:t.key}); };
  window.cpDispatchAi = i => { const a=CP.pick.actions?.[i]; if(!a) return;
    openDispatch({title:a.title, detail:a.detail, branch_id:a.branch_id||(a.owner_id?findUserBranch(a.owner_id)?.id:null), assignee:a.owner_id, priority:a.priority, days:a.due_days, ref:"ai"}); };
  window.cpDispatchStore = i => { const s=CP.pick.stores?.[i]; if(!s) return;
    openDispatch({title:"", detail:s.reasons?.length?`驾驶舱提示：${s.reasons.join("；")}`:"", branch_id:s.branch_id, days:3, ref:`store:${s.branch_id}`}); };
  window.cpDispatchPerson = i => { const p=CP.pick.people?.[i]; if(!p) return;
    const b=findUserBranch(p.id);
    if(!b) return toast(`${p.name} 还没绑定门店，先在「团队与权限」里给他分门店`);
    openDispatch({title:"", detail:"", branch_id:b.id, assignee:p.id, days:3, ref:`user:${p.id}`}); };
  window.cpDispatch = () => openDispatch({days:3, ref:"manual"});

  function openDispatch(o){
    const bs=branchesMeta();
    if(!CP.meta?.can_dispatch) return toast("当前账号不能派活");
    if(!bs.length) return toast("还没有门店，先在「巡店」里添加门店，才能把事情下发下去");
    const st={title:o.title||"", detail:o.detail||"", branch_id:o.branch_id||bs[0].id, assignee:o.assignee||null,
      level:o.assignee?"person":"store", priority:o.priority||"normal", due:dueText(o.days), photo:true, ref:o.ref||"manual"};
    if(!bs.some(b=>b.id===st.branch_id)) st.branch_id=bs[0].id;
    const wrap=document.createElement("div"); wrap.className="cp-modal"; wrap.id="cp-modal";
    wrap.onclick=e=>{ if(e.target===wrap) wrap.remove(); };
    const render=()=>{
      const b=bs.find(x=>x.id===st.branch_id)||bs[0], members=b.members||[];
      if(st.level==="person"&&!members.some(m=>m.id===st.assignee)) st.assignee=members[0]?.id||null;
      wrap.innerHTML=`<div class="cp-dialog" role="dialog" aria-label="下发任务"><h3>📤 下发任务</h3>
        <label>要做的事</label><input id="cp-d-title" maxlength="60" value="${esc(st.title)}" placeholder="例如：今天下班前把冷柜温度调到 4℃ 并拍照">
        <label>说明（可选）</label><textarea id="cp-d-detail" rows="3" maxlength="500" placeholder="做到什么程度、注意什么">${esc(st.detail)}</textarea>
        <label>下发给</label><div class="cp-lv"><button type="button" class="${st.level==="store"?"on":""}" data-lv="store">整家门店</button><button type="button" class="${st.level==="person"?"on":""}" data-lv="person">门店里的某个人</button></div>
        <div class="cp-row2"><div><label>门店</label><select id="cp-d-branch">${bs.map(x=>`<option value="${x.id}" ${x.id===b.id?"selected":""}>${esc(x.name)}</option>`).join("")}</select></div>
          <div>${st.level==="person"?`<label>负责人</label>${members.length?`<select id="cp-d-user">${members.map(m=>`<option value="${m.id}" ${m.id===st.assignee?"selected":""}>${esc(m.name)}（${esc(m.title_label||"")}）</option>`).join("")}</select>`:`<div class="sub" style="padding:10px 0">这家店还没绑定员工</div>`}`:`<label>&nbsp;</label><div class="sub" style="padding:10px 0">店里谁先接谁做，店长统一跟进</div>`}</div></div>
        <div class="cp-row2"><div><label>截止日期（当天 18:00）</label><input id="cp-d-due" type="date" value="${esc(st.due)}"></div>
          <div><label>紧急程度</label><select id="cp-d-pri"><option value="normal" ${st.priority==="normal"?"selected":""}>普通</option><option value="high" ${st.priority==="high"?"selected":""}>紧急</option><option value="low" ${st.priority==="low"?"selected":""}>不急</option></select></div></div>
        <label style="display:flex;gap:8px;align-items:center;font-weight:700"><input id="cp-d-photo" type="checkbox" style="width:auto" ${st.photo?"checked":""}> 要求拍照交差</label>
        <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:16px"><button type="button" class="btn" data-x="cancel">取消</button><button type="button" class="btn pri" data-x="ok">确认下发</button></div></div>`;
      const keep=()=>{ st.title=wrap.querySelector("#cp-d-title").value; st.detail=wrap.querySelector("#cp-d-detail").value; st.due=wrap.querySelector("#cp-d-due").value; st.priority=wrap.querySelector("#cp-d-pri").value; st.photo=wrap.querySelector("#cp-d-photo").checked; const u=wrap.querySelector("#cp-d-user"); if(u) st.assignee=Number(u.value); };
      wrap.querySelectorAll("[data-lv]").forEach(btn=>btn.onclick=()=>{ keep(); st.level=btn.dataset.lv; render(); });
      wrap.querySelector("#cp-d-branch").onchange=e=>{ keep(); st.branch_id=Number(e.target.value); st.assignee=null; render(); };
      wrap.querySelector("[data-x=cancel]").onclick=()=>wrap.remove();
      wrap.querySelector("[data-x=ok]").onclick=async ev=>{
        keep();
        if(!st.title.trim()) return toast("写一下要做的事");
        if(st.level==="person"&&!st.assignee) return toast("选一个负责人");
        ev.target.disabled=true;
        try{
          await api("/staff/tasks",{method:"POST", body:{title:st.title.trim(), detail:st.detail.trim(), branch_id:st.branch_id,
            assignee_user_id:st.level==="person"?st.assignee:null, due_at:st.due||null, priority:st.priority, require_photo:st.photo,
            source:"boss", source_ref:`cockpit:${st.ref}`.slice(0,200),
            request_key:`cockpit-${Date.now()}-${Math.random().toString(36).slice(2,10)}`}});
          wrap.remove(); toast("已下发，店员会在派活里收到"); view();
        }catch(e){ ev.target.disabled=false; toast(e.message); }
      };
    };
    document.body.appendChild(wrap); render(); setTimeout(()=>wrap.querySelector("#cp-d-title")?.focus(),30);
  }

  routes["cockpit"] = view;
})();
