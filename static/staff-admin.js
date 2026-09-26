/* 第 2 期:老板端「派给店员」(#/staff-tasks)。
   - 一句话派活:老板说一句话 → AI 拆成草稿 → 老板确认/修改 → 批量派出去
   - 手动派一件、结论卡「派给店员」预填
   - 列表按状态分组:等审核(看照片 + AI 建议,通过/打回) / 进行中(改派、取消) / 已结束
   依赖 app.js 的全局:api / esc / toast / uiConfirm / uiPrompt / $ / ME / routes / render。
   对外只暴露 window.PH_STAFF_ADMIN = {canDispatch, openDispatch}。 */
(function(){
  "use strict";
  const SA = {meta:null, list:null, drafts:[], prefill:null, filter:{status:"", branch:""}, parsing:false, sending:false};
  const me = ()=>{ try{ return typeof ME!=="undefined" ? ME : null; }catch(_){ return null; } };
  const h = s=>esc(s);
  const STATUS = {todo:"待做", submitted:"等审核", approved:"已通过", cancelled:"已取消", rejected:"被打回"};

  function canDispatch(){
    const u = me();
    if(!u) return false;
    if(u.role==="owner"||u.role==="root") return true;
    return u.role==="member" && ["director","manager"].includes(u.job_title);
  }
  function ensureStyle(){
    if(document.getElementById("ph-sa-style")) return;
    const st = document.createElement("style");
    st.id = "ph-sa-style";
    st.textContent = `
.sa-draft{border:2px dashed rgba(51,41,31,.35);border-radius:14px;padding:12px;margin-top:10px;background:#fffdf7}
.sa-draft.sa-sent{opacity:.6;border-style:solid;background:#eefaf1}
.sa-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:8px;margin-top:8px}
.sa-grid label{display:block;font-weight:800;font-size:13px;margin-bottom:3px}
.sa-grid input,.sa-grid select{width:100%;box-sizing:border-box;min-height:42px;font-size:15px}
.sa-hint{font-size:13px;color:#b35c00;margin-top:6px}
.sa-task{border:2px solid rgba(51,41,31,.22);border-radius:14px;padding:12px;margin-top:10px;background:#fff}
.sa-task.sa-over{border-color:#e03131;background:#fff5f5}
.sa-task.sa-focus{outline:3px solid #ffd166}
.sa-photos{display:grid;grid-template-columns:repeat(auto-fill,minmax(110px,1fr));gap:6px;margin-top:8px}
.sa-photos img{width:100%;aspect-ratio:1/1;object-fit:cover;border-radius:9px;border:2px solid rgba(51,41,31,.3);display:block}
.sa-ai{border-radius:10px;padding:7px 10px;margin-top:8px;font-weight:700;font-size:14px}
.sa-ai.pass{background:#d3f9d8}.sa-ai.doubt{background:#fff1bd}.sa-ai.fail{background:#ffe3e3}.sa-ai.none{background:#f1f3f5}
.sa-events{margin:8px 0 0 18px;font-size:13px}
.sa-one textarea{width:100%;box-sizing:border-box;min-height:74px;font-size:16px}
`;
    document.head.appendChild(st);
  }

  /* ---------- 时间:统一按北京时间 ---------- */
  const pad = n=>String(n).padStart(2,"0");
  function tsToLocal(ts){
    if(!ts) return "";
    const d = new Date(ts*1000+8*3600e3);
    return `${d.getUTCFullYear()}-${pad(d.getUTCMonth()+1)}-${pad(d.getUTCDate())}T${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`;
  }
  function localToTs(v){
    const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/.exec(v||"");
    return m ? Date.UTC(+m[1], +m[2]-1, +m[3], +m[4]-8, +m[5])/1000 : null;
  }
  function defaultDue(){
    const now = Date.now()/1000, d = new Date(now*1000+8*3600e3);
    let ts = Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate(), 18-8, 0)/1000;
    if(ts < now+3600) ts += 86400;
    return ts;
  }
  function newKey(){
    const a = new Uint8Array(12); crypto.getRandomValues(a);
    return "sa-"+Array.from(a, b=>b.toString(16).padStart(2,"0")).join("");
  }
  function blankDraft(extra={}){
    const branches = (SA.meta&&SA.meta.branches)||[];
    return {key:newKey(), title:"", detail:"", branch_id:branches.length===1?branches[0].id:null,
      assignee_user_id:null, due_at:defaultDue(), require_photo:true, hints:[], source:"boss", source_ref:"", ...extra};
  }

  /* ---------- 草稿编辑 ---------- */
  function branchOf(id){ return ((SA.meta&&SA.meta.branches)||[]).find(b=>String(b.id)===String(id)); }
  function draftHtml(d, i){
    const branches = (SA.meta&&SA.meta.branches)||[];
    const b = branchOf(d.branch_id);
    return `<div class="sa-draft ${d.sent?"sa-sent":""}" data-sa-draft="${i}">
      <div style="display:flex;gap:8px;align-items:center;justify-content:space-between;flex-wrap:wrap">
        <b>第 ${i+1} 件${d.sent?" · ✅ 已派出":""}</b>
        ${d.sent?"":`<button type="button" class="btn sm" onclick="PH_STAFF_ADMIN._drop(${i})" aria-label="删掉第 ${i+1} 件">删掉</button>`}</div>
      <div class="sa-grid">
        <div style="grid-column:1/-1"><label>要做什么</label><input maxlength="60" value="${h(d.title)}" ${d.sent?"disabled":""}
          oninput="PH_STAFF_ADMIN._set(${i},'title',this.value)" placeholder="比如:把冷柜清洗一遍"></div>
        <div><label>哪家店</label><select ${d.sent?"disabled":""} onchange="PH_STAFF_ADMIN._set(${i},'branch_id',this.value,true)">
          <option value="">请选择门店</option>${branches.map(x=>`<option value="${x.id}" ${String(x.id)===String(d.branch_id)?"selected":""}>${h(x.name)}</option>`).join("")}</select></div>
        <div><label>派给谁</label><select ${d.sent?"disabled":""} onchange="PH_STAFF_ADMIN._set(${i},'assignee_user_id',this.value)">
          <option value="">先不指定(店里谁有空谁做)</option>${(b?b.members:[]).map(u=>`<option value="${u.id}" ${String(u.id)===String(d.assignee_user_id)?"selected":""}>${h(u.name)}(${h(u.title_label)})</option>`).join("")}</select></div>
        <div><label>截止时间</label><input type="datetime-local" value="${tsToLocal(d.due_at)}" ${d.sent?"disabled":""}
          onchange="PH_STAFF_ADMIN._set(${i},'due_at',this.value)"></div>
        <div><label>&nbsp;</label><label style="display:flex;gap:6px;align-items:center;min-height:42px;font-size:15px">
          <input type="checkbox" style="width:auto;min-height:0" ${d.require_photo?"checked":""} ${d.sent?"disabled":""}
            onchange="PH_STAFF_ADMIN._set(${i},'require_photo',this.checked)"> 要拍照交差</label></div>
        <div style="grid-column:1/-1"><label>说明(可不填)</label><input maxlength="1000" value="${h(d.detail)}" ${d.sent?"disabled":""}
          oninput="PH_STAFF_ADMIN._set(${i},'detail',this.value)"></div>
      </div>
      ${(d.hints||[]).map(x=>`<div class="sa-hint">⚠️ ${h(x)}</div>`).join("")}
      ${d.error?`<div class="sa-hint" role="alert">❌ ${h(d.error)}</div>`:""}
    </div>`;
  }
  function drawDrafts(){
    const box = document.getElementById("sa-drafts"); if(!box) return;
    const waiting = SA.drafts.filter(d=>!d.sent).length;
    box.innerHTML = SA.drafts.length ? SA.drafts.map(draftHtml).join("")
      + `<div class="actions" style="margin-top:10px">
          ${waiting?`<button type="button" class="btn pri" onclick="PH_STAFF_ADMIN._sendAll()" ${SA.sending?"disabled":""}>📤 ${SA.sending?"正在派…":`确认派出去(${waiting} 件)`}</button>`:""}
          <button type="button" class="btn" onclick="PH_STAFF_ADMIN._add()">➕ 再加一件</button>
          <button type="button" class="btn" onclick="PH_STAFF_ADMIN._clear()">清空</button></div>` : "";
  }
  function setField(i, field, value, redraw){
    const d = SA.drafts[i]; if(!d || d.sent) return;
    if(field==="due_at") d.due_at = localToTs(value);
    else if(field==="branch_id"){ d.branch_id = value?+value:null; d.assignee_user_id = null; }
    else if(field==="assignee_user_id") d.assignee_user_id = value?+value:null;
    else d[field] = value;
    d.error = "";
    if(redraw) drawDrafts();
  }
  async function sendAll(){
    if(SA.sending) return;
    const todo = SA.drafts.filter(d=>!d.sent);
    for(const d of todo){
      d.error = !String(d.title||"").trim() ? "请写要做什么" : !d.branch_id ? "请选择门店" : "";
    }
    if(todo.some(d=>d.error)){ drawDrafts(); toast("有几件还没填完,看红字"); return; }
    SA.sending = true; drawDrafts();
    let ok = 0;
    for(const d of todo){
      try{
        await api("/staff/tasks", {method:"POST", body:{title:d.title.trim(), detail:(d.detail||"").trim(),
          branch_id:d.branch_id, assignee_user_id:d.assignee_user_id, due_at:d.due_at,
          require_photo:!!d.require_photo, source:d.source||"boss", source_ref:d.source_ref||"", request_key:d.key}});
        d.sent = true; ok++;
      }catch(e){ d.error = e.message; }
    }
    SA.sending = false;
    toast(ok===todo.length ? `✅ 派出去 ${ok} 件,店员手机上能看到` : `派出去 ${ok} 件,还有 ${todo.length-ok} 件没成功`);
    if(SA.drafts.every(d=>d.sent)) SA.drafts = [];
    await loadList(); draw();
  }
  async function parse(){
    const box = document.getElementById("sa-one"); if(!box || SA.parsing) return;
    const text = box.value.trim();
    if(!text){ toast("先说一句要派什么活"); box.focus(); return; }
    SA.parsing = true; draw();
    try{
      const r = await api("/staff/tasks/parse", {method:"POST", body:{text}, timeout:90000});
      const drafts = (r.drafts||[]).map(d=>blankDraft({title:d.title, detail:d.detail||"", branch_id:d.branch_id,
        assignee_user_id:d.assignee_user_id, due_at:d.due_at||defaultDue(), require_photo:d.require_photo!==false,
        hints:d.hints||[]}));
      SA.drafts = SA.drafts.filter(d=>!d.sent).concat(drafts);
      SA.oneText = "";
      toast(drafts.length ? `拆出 ${drafts.length} 件,看一眼再派` : "没拆出任务,换个说法试试");
    }catch(e){ SA.oneText = text; toast(e.message); }
    SA.parsing = false; draw();
  }

  /* ---------- 列表 ---------- */
  function aiHtml(t){
    const a = t.ai_check;
    if(!a){ return t.status==="submitted"&&(t.photos||[]).length ? `<div class="sa-ai none">${SA.meta?.ai_check_enabled===false?"AI 验照片已关闭，请人工核对照片":"暂无 AI 验照片建议，可先人工核对照片"}</div>` : ""; }
    if(a.error||!a.verdict) return `<div class="sa-ai none">🤖 ${h(a.reason||"AI 这次没看成,请直接看照片")}</div>`;
    const label = {pass:"像是做好了", doubt:"拿不准,建议细看", fail:"像是没做好"}[a.verdict]||"";
    return `<div class="sa-ai ${h(a.verdict)}">🤖 AI 建议:${h(label)}${a.reason?`。${h(a.reason)}`:""}
      <span class="sub">(把握 ${Math.round((a.confidence||0)*100)}%,只是建议,您说了算)</span></div>`;
  }
  function photosHtml(list){
    return (list||[]).length ? `<div class="sa-photos">${list.map(p=>`<a href="${h(p.url)}" target="_blank" rel="noopener"><img src="${h(p.url)}" alt="交差照片" loading="lazy"></a>`).join("")}</div>` : "";
  }
  function reassignHtml(t){
    const b = branchOf(t.branch_id);
    if(!b || !t.can_manage_hint) return "";
    return `<select onchange="PH_STAFF_ADMIN._assign(${t.id},this.value)" aria-label="改派" style="min-height:36px;font-size:14px;max-width:190px">
      <option value="">${t.assignee_name?"改派给…":"指派给…"}</option>${b.members.filter(u=>u.id!==t.assignee_user_id).map(u=>`<option value="${u.id}">${h(u.name)}(${h(u.title_label)})</option>`).join("")}</select>`;
  }
  function taskHtml(t){
    const rejected = t.status==="todo" && t.review_note && t.reviewed_at;
    const meta = [t.branch_name, t.assignee_name?`👤 ${t.assignee_name}`:"👤 还没指定人", t.due_text?`截止 ${t.due_text}`:"",
      t.created_by_name?`${t.created_by_name} 派的`:"", t.remind_count?`已催 ${t.remind_count} 次`:""].filter(Boolean);
    return `<div class="sa-task ${t.overdue?"sa-over":""} ${String(SA.focus)===String(t.id)?"sa-focus":""}" id="sa-task-${t.id}">
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
        <span class="pill ${t.status==="approved"?"done":t.status==="cancelled"?"cancelled":t.status==="submitted"?"awaiting_review":"running"}">${h(t.overdue?"逾期":STATUS[t.status]||t.status)}</span>
        <b style="flex:1;min-width:160px">${h(t.title)}</b>
        ${t.require_photo?`<span class="sub">📷 要照片</span>`:""}</div>
      <div class="sub" style="margin-top:4px">${meta.map(h).join(" · ")}</div>
      ${t.detail?`<div style="margin-top:4px">${h(t.detail)}</div>`:""}
      ${rejected?`<div class="sa-hint">↩️ 打回过:${h(t.review_note)}</div>`:""}
      ${t.status==="submitted"?`${t.submit_note?`<div style="margin-top:6px">📝 ${h(t.submit_note)}</div>`:""}${photosHtml(t.photos)}${aiHtml(t)}`:""}
      ${t.status==="approved"&&(t.photos||[]).length?`<details style="margin-top:6px"><summary class="sub">看交差照片(${t.photos.length})</summary>${photosHtml(t.photos)}</details>`:""}
      <div class="actions" style="margin-top:8px">
        ${t.status==="submitted"?`<button type="button" class="btn sm pri" onclick="PH_STAFF_ADMIN._review(${t.id},true)">👍 通过</button>
          <button type="button" class="btn sm bad" onclick="PH_STAFF_ADMIN._review(${t.id},false)">↩️ 打回重做</button>`:""}
        ${t.status==="todo"?reassignHtml(t):""}
        ${["todo","submitted"].includes(t.status)?`<button type="button" class="btn sm" onclick="PH_STAFF_ADMIN._cancel(${t.id})">取消这件</button>`:""}
        <button type="button" class="btn sm" onclick="PH_STAFF_ADMIN._events(${t.id})">🕘 经过</button>
      </div>
      <div id="sa-ev-${t.id}"></div>
    </div>`;
  }
  function listHtml(){
    const items = ((SA.list&&SA.list.items)||[]).map(t=>({...t, can_manage_hint:true}));
    const groups = [
      ["submitted", "🧐 等您审核"], ["todo", "⏳ 店员在做"],
    ];
    const over = items.filter(t=>t.status==="todo").sort((a,b)=>(b.overdue-a.overdue)||((a.due_at||9e12)-(b.due_at||9e12)));
    const pick = s=>s==="todo"?over:items.filter(t=>t.status===s);
    const ended = items.filter(t=>["approved","cancelled","rejected"].includes(t.status));
    let html = groups.map(([s, title])=>{ const list = pick(s);
      return `<h3 style="margin-top:18px">${title}(${list.length})</h3>`
        + (list.length ? list.map(taskHtml).join("") : `<div class="sub">暂时没有</div>`); }).join("");
    if(ended.length) html += `<details style="margin-top:16px"><summary><b>已结束(${ended.length})</b></summary>${ended.map(taskHtml).join("")}</details>`;
    if(SA.list&&SA.list.next_before_id) html += `<div class="actions"><button type="button" class="btn" onclick="PH_STAFF_ADMIN._more()">加载更多</button></div>`;
    return html;
  }
  function draw(){
    const main = $("#main"); if(!main) return;
    const m = SA.meta || {branches:[]};
    const u = me();
    const owner = u && (u.role==="owner"||u.role==="root");
    const price = (m.prices||{}).staff_parse;
    main.innerHTML = `<div class="hubhead"><h2>🧑‍🍳 派给店员</h2>
        <div class="sub">把活派给店里的真人,店员用手机拍照交差,AI 先帮您看一眼照片,最后您来定。</div></div>
      ${!m.branches.length?`<div class="card"><div class="empty">还没有可以派活的门店。先在「门店 → 巡店」里建门店,再到「我的 → 团队与权限」给店员分配门店。</div></div>`:""}
      <div class="card sa-one">
        <h3 style="margin-top:0">🗣️ 一句话派活</h3>
        <textarea id="sa-one" maxlength="500" placeholder="比如:人民路店明天中午前把冷柜清一遍,拍照给我">${h(SA.oneText||"")}</textarea>
        <div class="actions">
          <button type="button" class="btn pri" onclick="PH_STAFF_ADMIN._parse()" ${SA.parsing||!m.branches.length?"disabled":""}>${SA.parsing?"AI 正在拆…":"✨ 拆成任务"}${price?`(${price} 点)`:""}</button>
          <button type="button" class="btn" onclick="PH_STAFF_ADMIN._add()" ${!m.branches.length?"disabled":""}>✍️ 手动派一件</button>
          <a class="btn" href="/staff" target="_blank" rel="noopener">📱 看店员手机版</a>
        </div>
        <div class="sub">AI 只帮您填表,门店和人对不上时会留空让您选,确认后才会派出去。</div>
        <div id="sa-drafts"></div>
      </div>
      <div class="card">
        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
          <select onchange="PH_STAFF_ADMIN._filter('branch',this.value)" aria-label="按门店看" style="min-height:40px">
            <option value="">全部门店</option>${m.branches.map(b=>`<option value="${b.id}" ${String(b.id)===String(SA.filter.branch)?"selected":""}>${h(b.name)}</option>`).join("")}</select>
          <select onchange="PH_STAFF_ADMIN._filter('status',this.value)" aria-label="按状态看" style="min-height:40px">
            ${[["","全部状态"],["submitted","等审核"],["todo","待做"],["approved","已通过"],["cancelled","已取消"]].map(([v,l])=>`<option value="${v}" ${v===SA.filter.status?"selected":""}>${l}</option>`).join("")}</select>
          <button type="button" class="btn sm" onclick="PH_STAFF_ADMIN._reload()">🔄 刷新</button>
          ${owner?`<label style="display:flex;gap:6px;align-items:center;margin-left:auto;font-size:14px">
            <input type="checkbox" style="width:auto" ${m.ai_check_enabled?"checked":""} onchange="PH_STAFF_ADMIN._aiToggle(this.checked)">
            AI 先看照片${m.prices&&m.prices.staff_ai_check?`(每次 ${m.prices.staff_ai_check} 点)`:""}</label>`:""}
        </div>
        ${listHtml()}
      </div>`;
    drawDrafts();
    if(SA.focus){ const el = document.getElementById("sa-task-"+SA.focus); if(el) el.scrollIntoView({block:"center"}); }
  }

  /* ---------- 数据 ---------- */
  async function loadList(more){
    const qs = new URLSearchParams();
    if(SA.filter.status) qs.set("status", SA.filter.status);
    if(SA.filter.branch) qs.set("branch", SA.filter.branch);
    if(more && SA.list && SA.list.next_before_id) qs.set("before_id", SA.list.next_before_id);
    const r = await api("/staff/tasks"+(qs.toString()?"?"+qs:""));
    const items = Array.isArray(r?.items) ? r.items : [];
    SA.list = more && SA.list ? {...r, items:SA.list.items.concat(items)} : {...(r||{}), items};
  }
  async function view(arg){
    ensureStyle();
    if(!canDispatch()){
      // 店员点通知进来:去店员手机版看这件活
      location.href = "/staff" + (arg ? "#task-"+encodeURIComponent(arg) : "");
      return;
    }
    SA.focus = arg || null;
    const [meta] = await Promise.all([api("/staff/meta"), loadList()]);
    SA.meta = {...(meta||{}), branches:Array.isArray(meta?.branches)?meta.branches:[]};
    if(SA.prefill){ SA.drafts = SA.drafts.filter(d=>!d.sent).concat([blankDraft(SA.prefill)]); SA.prefill = null; }
    draw();
  }
  async function review(id, approve){
    let note = "";
    if(!approve){
      note = await uiPrompt({title:"打回重做", message:"写一句哪里不行,店员手机上会看到", multiline:true,
        placeholder:"比如:冷柜底层没擦干净,再拍一张近照", confirmText:"打回", danger:true,
        requiredMessage:"写一句原因,店员才知道哪里要重做"});
      if(note===null) return;
    }
    try{
      await api(`/staff/tasks/${id}/review`, {method:"POST", body:{action:approve?"approve":"reject", note}});
      toast(approve?"👍 已通过":"↩️ 已打回,店员会收到通知");
      await loadList(); draw();
    }catch(e){ toast(e.message); }
  }
  async function cancel(id){
    if(!await uiConfirm("取消后店员手机上就看不到这件活了。", {title:"取消这件活", confirmText:"取消这件"})) return;
    try{ await api(`/staff/tasks/${id}/cancel`, {method:"POST", body:{}}); toast("已取消"); await loadList(); draw(); }
    catch(e){ toast(e.message); }
  }
  async function assign(id, uid){
    if(!uid) return;
    try{ await api(`/staff/tasks/${id}/assign`, {method:"POST", body:{assignee_user_id:+uid}}); toast("已改派"); await loadList(); draw(); }
    catch(e){ toast(e.message); draw(); }
  }
  async function events(id){
    const box = document.getElementById("sa-ev-"+id); if(!box) return;
    if(box.innerHTML){ box.innerHTML = ""; return; }
    try{
      const t = await api(`/staff/tasks/${id}`);
      const label = {created:"派出", assigned:"指派", submitted:"交差", approved:"通过", rejected:"打回",
        cancelled:"取消", reminded:"提醒", escalated:"升级提醒", ai_checked:"AI 看照片", commented:"留言"};
      box.innerHTML = `<ol class="sa-events">${(t.events||[]).map(e=>`<li>${h(new Date((e.created_at||0)*1000).toLocaleString("zh-CN",{timeZone:"Asia/Shanghai",hour12:false}))}
        · <b>${h(e.actor_name||"系统")}</b> ${h(label[e.kind]||e.kind)}${e.note?`:${h(e.note)}`:""}</li>`).join("")}</ol>`
        + ((t.earlier_photos||[]).length?`<details><summary class="sub">打回前交的照片(${t.earlier_photos.length})</summary>${photosHtml(t.earlier_photos)}</details>`:"");
    }catch(e){ toast(e.message); }
  }
  async function aiToggle(on){
    try{ await api("/staff/settings", {method:"PUT", body:{ai_check_enabled:!!on}}); SA.meta.ai_check_enabled = !!on;
      toast(on?"已打开:店员交差后 AI 先看照片":"已关闭 AI 看照片"); }
    catch(e){ toast(e.message); draw(); }
  }
  function cleanAction(text){
    return String(text||"").replace(/^\s*(店员|店长|老板)\s*[:：]\s*/, "").trim();
  }
  /* 结论卡「派给店员」:打开派活表单并预填这条行动 */
  function openDispatch(opts={}){
    const text = cleanAction(opts.title);
    SA.prefill = {title:text.slice(0, 60), detail:text.length>60?text:(opts.detail||""),
      source:opts.source||"boss", source_ref:String(opts.source_ref||"").slice(0, 200)};
    if(location.hash.startsWith("#/staff-tasks")){
      SA.drafts = SA.drafts.filter(d=>!d.sent).concat([blankDraft(SA.prefill)]); SA.prefill = null;
      drawDrafts(); document.getElementById("sa-drafts")?.scrollIntoView({block:"center"});
    } else location.hash = "#/staff-tasks";
  }

  window.PH_STAFF_ADMIN = {
    canDispatch, openDispatch,
    _set:setField, _parse:parse, _sendAll:sendAll, _review:review, _cancel:cancel, _assign:assign,
    _events:events, _aiToggle:aiToggle,
    _add(){ SA.drafts.push(blankDraft()); drawDrafts(); },
    _drop(i){ SA.drafts.splice(i, 1); drawDrafts(); },
    _clear(){ SA.drafts = []; drawDrafts(); },
    async _filter(k, v){ SA.filter[k] = v; try{ await loadList(); draw(); }catch(e){ toast(e.message); } },
    async _reload(){ try{ await loadList(); draw(); }catch(e){ toast(e.message); } },
    async _more(){ try{ await loadList(true); draw(); }catch(e){ toast(e.message); } },
  };
  try{
    routes["staff-tasks"] = view;
    // app.js 的首次 render() 可能在本文件加载前就跑完了:当前就在这一页时补渲染一次
    if(location.hash.startsWith("#/staff-tasks") && typeof render==="function") render();
  }catch(_){}
})();
