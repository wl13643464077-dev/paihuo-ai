/* 第 2 期 · 老板端：开闭店清单(#/checklists) + 门店排行(#/store-rank)。
   店员打勾拍照在 staff.html(另一个页面)，这里只给老板/店长看完成情况、管模板、看排行。
   依赖 app.js 里的全局：routes / api / esc / cp / toast / $ / ME / isOwnerLike。
   在 index.html 里 app.js 之后引入；只通过 routes["xxx"]=fn 接入，不改 app.js 的函数。 */
(function(){
  "use strict";
  const CK = {tab:"today", date:"", overview:null, templates:null, editing:null,
    period:"week", rank:null, openStore:{}};
  const KIND_LABEL = {open:"开店清单", close:"闭店清单", handover:"交班清单", custom:"清单"};
  const STATUS_PILL = {open:"running", done:"done", missed:"failed"};
  const canManageTemplates = ()=>!!ME && (isOwnerLike() || ME.job_title==="director");

  function ckStyle(){
    if(document.getElementById("ck-admin-style")) return;
    const st=document.createElement("style");
    st.id="ck-admin-style";
    st.textContent=`
      .ck-head{display:flex;flex-wrap:wrap;gap:10px;align-items:center;justify-content:space-between;margin-bottom:12px}
      .ck-head h2{margin:0}
      .ck-sum{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px;margin:8px 0 4px}
      .ck-sum div{background:#fff;border:2px solid var(--ink);border-radius:14px;padding:8px 6px;text-align:center}
      .ck-sum b{display:block;font-size:22px}
      .ck-sum span{font-size:13px;color:var(--ink2)}
      .ck-store{padding:14px 16px}
      .ck-store-top{display:flex;gap:10px;align-items:center;justify-content:space-between;cursor:pointer;font-size:16px}
      .ck-bar{height:10px;border:2px solid var(--ink);border-radius:99px;background:#f0e4c8;overflow:hidden;margin-top:8px}
      .ck-bar i{display:block;height:100%;background:#8ee6bd}
      .ck-run{border-top:1.5px dashed var(--line);padding:10px 0 4px;font-size:15px}
      .ck-run-top{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
      .ck-items{margin:8px 0 0;padding:0;list-style:none}
      .ck-items li{display:flex;gap:8px;align-items:flex-start;padding:5px 0;font-size:14px}
      .ck-items .ok{color:var(--ok);font-weight:900}
      .ck-items .no{color:var(--ink2)}
      .ck-items a{white-space:nowrap}
      .ck-tpl{padding:14px 16px}
      .ck-tpl-top{display:flex;flex-wrap:wrap;gap:8px;align-items:center;justify-content:space-between}
      .ck-edit-row{display:flex;gap:8px;align-items:center;margin:6px 0}
      .ck-edit-row input[type=text]{flex:1;min-width:0;font-size:16px}
      .ck-edit-row label{white-space:nowrap;font-size:14px;display:flex;gap:4px;align-items:center}
      .ck-rank{display:flex;gap:12px;align-items:flex-start;padding:14px 16px}
      .ck-rank.warn{background:#fff1f1}
      .ck-rank-no{flex:none;width:42px;height:42px;border-radius:50%;border:2.5px solid var(--ink);display:flex;align-items:center;justify-content:center;font-weight:900;font-size:18px;background:#fff}
      .ck-rank-no.top{background:#ffd166}
      .ck-rank-body{flex:1;min-width:0}
      .ck-rank-score{font-size:26px;font-weight:900;line-height:1}
      .ck-delta{font-size:13px;font-weight:800;margin-left:6px}
      .ck-delta.up{color:var(--ok)} .ck-delta.down{color:var(--bad)}
      .ck-parts{display:grid;grid-template-columns:1fr 1fr;gap:4px 12px;margin-top:8px;font-size:13px;color:var(--ink2)}
      .ck-why{margin:8px 0 0;padding-left:18px;font-size:14px}
      @media (max-width:560px){.ck-sum{grid-template-columns:repeat(2,minmax(0,1fr))}.ck-parts{grid-template-columns:1fr}}
    `;
    document.head.appendChild(st);
  }
  const fmtTime = ts => { if(!ts) return ""; const d=new Date(ts*1000);
    return d.toLocaleTimeString("zh-CN",{hour:"2-digit",minute:"2-digit",hour12:false,timeZone:"Asia/Shanghai"}); };
  const safeFile = url => (typeof url==="string" && /^\/files\/staff\/\d+\/\d+\/[a-f0-9]{32}\.jpg$/.test(url)) ? url : "";

  /* ---------------- #/checklists ---------------- */
  async function checklistsView(){
    ckStyle();
    const tabs = canManageTemplates()
      ? `<div class="tabs" role="tablist">
          <button type="button" class="tb ${CK.tab==="today"?"on":""}" onclick="ckTab('today')">各店完成情况</button>
          <button type="button" class="tb ${CK.tab==="tpl"?"on":""}" onclick="ckTab('tpl')">清单模板</button></div>` : "";
    $("#main").innerHTML = `<div class="ck-head"><h2>✅ 开闭店清单</h2>
        <a class="btn sm" href="#/store-rank">🏆 门店排行</a></div>
      <div class="sub" style="margin-bottom:10px">店员每天在手机上按清单打勾、拍照；过了截止时间没做完会自动提醒店员，再提醒店长和您。打勾拍照不扣点。</div>
      ${tabs}<div id="ck-body"><div class="card"><span class="spin"></span> 正在加载…</div></div>`;
    if(CK.tab==="tpl" && canManageTemplates()) return ckLoadTemplates();
    return ckLoadToday();
  }
  window.ckTab = t => { CK.tab=t; CK.editing=null; checklistsView(); };

  async function ckLoadToday(){
    const q = CK.date ? `?date=${encodeURIComponent(CK.date)}` : "";
    try{ CK.overview = await api("/checklist/runs"+q); }
    catch(e){ if(e?.name==="NavigationAbort") return; $("#ck-body").innerHTML=`<div class="card"><div class="empty">${esc(e.message)}</div></div>`; return; }
    ckRenderToday();
  }
  window.ckPickDate = v => { CK.date = String(v||""); ckLoadToday(); };
  window.ckToggleStore = id => { CK.openStore[id]=!CK.openStore[id]; ckRenderToday(); };

  function ckRunHtml(run){
    const items = (run.items||[]).map(it=>{
      const photo = safeFile(it.photo_url);
      return `<li><span class="${it.done?"ok":"no"}" aria-hidden="true">${it.done?"✔":"○"}</span>
        <span style="flex:1">${esc(it.text)}${it.require_photo?' <span class="tag">要拍照</span>':""}
          ${it.done?`<span class="sub"> · ${esc(it.done_by_name||"")} ${esc(fmtTime(it.done_at))}</span>`:""}
          ${it.note?`<div class="sub">备注：${esc(it.note)}</div>`:""}</span>
        ${photo?`<a class="btn sm" href="${esc(photo)}" target="_blank" rel="noopener">看照片</a>`:""}</li>`;
    }).join("");
    return `<div class="ck-run"><div class="ck-run-top">
        <b>${esc(run.name||KIND_LABEL[run.kind]||"清单")}</b>
        <span class="pill ${STATUS_PILL[run.status]||""}">${esc(run.status_label||run.status)}</span>
        <span class="sub">${esc(run.due_text||"")} · ${run.done_count}/${run.total} 项 · ${esc(run.assignee_name?("负责人 "+run.assignee_name):"门店员工都能做")}</span>
      </div><ul class="ck-items">${items}</ul></div>`;
  }

  function ckRenderToday(){
    const ov = CK.overview || {stores:[], summary:{}};
    const s = ov.summary || {};
    const head = `<div class="card"><div class="ck-head" style="margin:0">
        <b style="font-size:16px">${esc(ov.date||"")} 各店清单</b>
        <label class="sub">换一天 <input type="date" value="${esc(CK.date||ov.date||"")}" onchange="ckPickDate(this.value)" style="font-size:16px"></label></div>
      <div class="ck-sum">
        <div><b>${s.rate==null?"—":Math.round(s.rate)+"%"}</b><span>按时完成率</span></div>
        <div><b>${s.done||0}</b><span>已完成</span></div>
        <div><b>${s.open||0}</b><span>待完成</span></div>
        <div><b style="color:var(--bad)">${s.missed||0}</b><span>超时没做完</span></div></div></div>`;
    if(!ov.stores.length){
      $("#ck-body").innerHTML = head + `<div class="card"><div class="empty">还没有门店，或者老板还没给您分配门店。先在「巡店」里建门店、在「团队」里把店员绑定到门店。</div></div>`;
      return;
    }
    // 没做完的门店排前面，老板先看要盯的
    const stores = [...ov.stores].sort((a,b)=>(b.missed-a.missed)||((b.total-b.done)-(a.total-a.done))||a.branch_name.localeCompare(b.branch_name,"zh"));
    const cards = stores.map(st=>{
      const pct = st.total ? Math.round(st.done*100/st.total) : 0;
      const open = !!CK.openStore[st.branch_id];
      const badge = st.missed ? `<span class="pill failed">${st.missed} 份超时</span>`
        : st.total && st.done===st.total ? `<span class="pill done">全部完成</span>`
        : `<span class="pill running">${st.done}/${st.total}</span>`;
      return `<div class="card ck-store">
        <div class="ck-store-top" role="button" tabindex="0" aria-expanded="${open}"
          onclick="ckToggleStore(${cp(st.branch_id)})" onkeydown="if(event.key==='Enter')ckToggleStore(${cp(st.branch_id)})">
          <b>🏪 ${esc(st.branch_name)}</b><span>${badge} <span aria-hidden="true">${open?"▲":"▼"}</span></span></div>
        <div class="ck-bar" aria-label="完成 ${pct}%"><i style="width:${pct}%"></i></div>
        ${st.total?"":`<div class="sub" style="margin-top:6px">这一天没有要做的清单</div>`}
        ${open?(st.runs||[]).map(ckRunHtml).join(""):""}</div>`;
    }).join("");
    $("#ck-body").innerHTML = head + cards;
  }

  /* ---------------- 模板管理 ---------------- */
  async function ckLoadTemplates(){
    try{ const r = await api("/checklist/templates"); CK.templates = r.items||[]; }
    catch(e){ if(e?.name==="NavigationAbort") return; $("#ck-body").innerHTML=`<div class="card"><div class="empty">${esc(e.message)}</div></div>`; return; }
    ckRenderTemplates();
  }
  function ckIndustryName(key){
    if(!key) return "所有门店";
    const d = (typeof DEPTS!=="undefined" && Array.isArray(DEPTS)) ? DEPTS.find(x=>x.key===key) : null;
    return d ? d.name : key;
  }
  function ckRenderTemplates(){
    const list = CK.templates||[];
    const editing = CK.editing;
    const cards = list.map(t=>{
      if(editing && editing.id===t.id) return ckEditorHtml(editing);
      const photos = t.items.filter(i=>i.require_photo).length;
      return `<div class="card ck-tpl" style="${t.active?"":"opacity:.6"}">
        <div class="ck-tpl-top"><div><b style="font-size:16px">${esc(t.name)}</b>
          <span class="tag">${esc(t.kind_label)}</span><span class="tag">${esc(ckIndustryName(t.industry_key))}</span></div>
          <span class="sub">${t.due_time?esc(t.due_time)+" 前做完":"当天做完"} · ${t.items.length} 项（${photos} 项要拍照）${t.active?"":" · 已停用"}</span></div>
        <ol class="sub" style="margin:8px 0 0;padding-left:20px;font-size:14px">${t.items.slice(0,4).map(i=>`<li>${esc(i.text)}</li>`).join("")}${t.items.length>4?`<li>……共 ${t.items.length} 项</li>`:""}</ol>
        <div class="actions" style="margin-top:10px">
          <button type="button" class="btn sm" onclick="ckEdit(${cp(t.id)})">✏️ 改清单</button>
          <button type="button" class="btn sm" onclick="ckToggleActive(${cp(t.id)},${t.active?"false":"true"})">${t.active?"停用":"重新启用"}</button></div></div>`;
    }).join("");
    const creating = editing && editing.id==null ? ckEditorHtml(editing) : "";
    $("#ck-body").innerHTML = `<div class="notice">改动从明天生成的清单开始生效；今天已经发给店员的清单不变。</div>
      ${creating}${cards||`<div class="card"><div class="empty">还没有清单模板</div></div>`}
      ${editing?"":`<div class="actions"><button type="button" class="btn pri" onclick="ckNew()">＋ 新建一份清单</button></div>`}`;
  }
  function ckEditorHtml(ed){
    const rows = ed.items.map((it,i)=>`<div class="ck-edit-row">
        <input type="text" maxlength="80" value="${esc(it.text)}" aria-label="第 ${i+1} 项" oninput="ckItemText(${i},this.value)">
        <label><input type="checkbox" ${it.require_photo?"checked":""} onchange="ckItemPhoto(${i},this.checked)"> 拍照</label>
        <button type="button" class="btn sm" aria-label="删掉第 ${i+1} 项" onclick="ckItemDel(${i})">✕</button></div>`).join("");
    const kindSel = ed.id==null ? `<div><label>类型</label><select onchange="ckEdField('kind',this.value)" style="font-size:16px">
        ${["open","close","handover","custom"].map(k=>`<option value="${k}" ${ed.kind===k?"selected":""}>${KIND_LABEL[k]}</option>`).join("")}</select></div>` : "";
    return `<div class="card ck-tpl"><b style="font-size:16px">${ed.id==null?"新建清单":"改清单"}</b>
      <div class="row" style="margin-top:8px">
        <div><label>名称</label><input type="text" maxlength="30" value="${esc(ed.name)}" oninput="ckEdField('name',this.value)" style="font-size:16px"></div>
        <div><label>几点前做完</label><input type="time" value="${esc(ed.due_time)}" onchange="ckEdField('due_time',this.value)" style="font-size:16px"></div>
        ${kindSel}</div>
      <div style="margin-top:10px">${rows}</div>
      <div class="actions"><button type="button" class="btn sm" onclick="ckItemAdd()">＋ 加一项</button></div>
      <div class="actions" style="margin-top:12px"><button type="button" class="btn pri" onclick="ckSave()">保存</button>
        <button type="button" class="btn" onclick="ckCancel()">取消</button></div></div>`;
  }
  window.ckEdit = id => { const t=(CK.templates||[]).find(x=>x.id===id); if(!t) return;
    CK.editing = {id:t.id, name:t.name, due_time:t.due_time, kind:t.kind, items:t.items.map(i=>({...i}))}; ckRenderTemplates(); };
  window.ckNew = () => { CK.editing = {id:null, name:"", due_time:"18:00", kind:"custom", items:[{text:"", require_photo:false}]}; ckRenderTemplates(); };
  window.ckCancel = () => { CK.editing=null; ckRenderTemplates(); };
  window.ckEdField = (k,v) => { if(CK.editing) CK.editing[k]=v; };
  window.ckItemText = (i,v) => { if(CK.editing?.items[i]) CK.editing.items[i].text=v; };
  window.ckItemPhoto = (i,v) => { if(CK.editing?.items[i]) CK.editing.items[i].require_photo=!!v; };
  window.ckItemDel = i => { if(!CK.editing) return; CK.editing.items.splice(i,1); ckRenderTemplates(); };
  window.ckItemAdd = () => { if(!CK.editing) return; if(CK.editing.items.length>=30) return toast("一份清单最多 30 项");
    CK.editing.items.push({text:"", require_photo:false}); ckRenderTemplates(); };
  window.ckSave = async () => {
    const ed = CK.editing; if(!ed) return;
    const items = ed.items.map(i=>({...i, text:String(i.text||"").trim()})).filter(i=>i.text);
    if(!items.length) return toast("清单至少要有 1 项");
    const body = {name:String(ed.name||"").trim()||KIND_LABEL[ed.kind]||"清单", due_time:ed.due_time||"", items};
    try{
      if(ed.id==null) await api("/checklist/templates",{method:"POST", body:{...body, kind:ed.kind}});
      else await api(`/checklist/templates/${encodeURIComponent(ed.id)}`,{method:"PUT", body});
      toast("已保存，从明天开始按新清单");
      CK.editing=null; await ckLoadTemplates();
    }catch(e){ toast(e.message); }
  };
  window.ckToggleActive = async (id, active) => {
    try{ await api(`/checklist/templates/${encodeURIComponent(id)}`,{method:"PUT", body:{active:!!active}});
      toast(active?"已启用，明天起生成":"已停用，明天起不再生成"); await ckLoadTemplates(); }
    catch(e){ toast(e.message); }
  };

  /* ---------------- #/store-rank ---------------- */
  async function storeRankView(){
    ckStyle();
    $("#main").innerHTML = `<div class="ck-head"><h2>🏆 门店排行</h2><a class="btn sm" href="#/checklists">✅ 开闭店清单</a></div>
      <div class="tabs" role="tablist">
        <button type="button" class="tb ${CK.period==="week"?"on":""}" onclick="ckPeriod('week')">近 7 天</button>
        <button type="button" class="tb ${CK.period==="month"?"on":""}" onclick="ckPeriod('month')">近 30 天</button></div>
      <div id="ck-rank"><div class="card"><span class="spin"></span> 正在算分…</div></div>`;
    try{ CK.rank = await api(`/stores/ranking?period=${encodeURIComponent(CK.period)}`); }
    catch(e){ if(e?.name==="NavigationAbort") return; $("#ck-rank").innerHTML=`<div class="card"><div class="empty">${esc(e.message)}</div></div>`; return; }
    ckRenderRank();
  }
  window.ckPeriod = p => { CK.period = p==="month"?"month":"week"; storeRankView(); };
  function ckPart(p){
    const v = p.score==null ? "没数据" : `${Math.round(p.score)}${p.label==="巡店问题分"?" 分":"%"}`;
    const extra = p.total!=null && p.total ? `（${p.done}/${p.total}）` : p.visits ? `（巡 ${p.visits} 次 ${p.issues} 个问题）` : "";
    return `<div>${esc(p.label)} ${p.weight}%：<b>${esc(v)}</b>${esc(extra)}</div>`;
  }
  function ckRenderRank(){
    const r = CK.rank || {stores:[]};
    const stores = r.stores||[];
    const cards = stores.map(s=>{
      const d = s.delta;
      const delta = d==null ? "" : d>0 ? `<span class="ck-delta up">↑${d}</span>` : d<0 ? `<span class="ck-delta down">↓${Math.abs(d)}</span>` : `<span class="ck-delta">持平</span>`;
      const move = s.rank_change ? `<span class="sub">（名次${s.rank_change>0?"上升":"下降"} ${Math.abs(s.rank_change)}）</span>` : "";
      const parts = ["checklist","action","task","issue"].map(k=>s.components?.[k]).filter(Boolean).map(ckPart).join("");
      const why = (s.reasons||[]).length ? `<ul class="ck-why">${s.reasons.map(x=>`<li>${esc(x)}</li>`).join("")}</ul>` : "";
      return `<div class="card ck-rank ${s.needs_attention?"warn":""}">
        <div class="ck-rank-no ${s.rank&&s.rank<=3?"top":""}">${s.rank||"—"}</div>
        <div class="ck-rank-body">
          <div style="display:flex;justify-content:space-between;gap:8px;align-items:baseline;flex-wrap:wrap">
            <b style="font-size:17px">${esc(s.branch_name)}</b>
            <span><span class="ck-rank-score">${s.score==null?"—":Math.round(s.score)}</span>${delta}</span></div>
          ${move}${s.needs_attention?`<div style="color:var(--bad);font-weight:800;margin-top:4px">要盯一下</div>`:""}
          ${why}<div class="ck-parts">${parts}</div></div></div>`;
    }).join("");
    $("#ck-rank").innerHTML = `<details class="notice"><summary>分数怎么算的？</summary><div style="margin-top:6px">${esc(r.formula||"")}</div></details>`
      + (cards || `<div class="card"><div class="empty">还没有门店，或者老板还没给您分配门店。</div></div>`);
  }

  routes["checklists"] = checklistsView;
  routes["store-rank"] = storeRankView;
})();
