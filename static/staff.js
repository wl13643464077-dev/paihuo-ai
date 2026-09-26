/* 店员手机版(第 2 期):独立轻页面,不加载老板端 app.js。
   一屏一件事:首页是「我的待办」(派给我的活 / 今天的清单 / 巡店整改),点开做完拍照交差。
   店长额外有「派活」和「待我审核」。业务数据只放内存,不写 localStorage。 */
(function(){
  "use strict";
  const S = {me:null, todo:null, meta:null, pending:{}, busy:false};
  const $ = s=>document.querySelector(s);
  const esc = v=>String(v??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const main = ()=>$("#main");
  const URGENCY = {overdue:"已逾期", today:"今天要做完", later:"", waiting:"已交，等审核",
    done:"已完成", submitted:"已交，等审核", awaiting_recheck:"已交复查，等审核", missed:"已错过"};
  const KIND_ICON = {task:"📌", checklist:"✅", inspection_action:"🔧"};

  /* ---------- 网络 ---------- */
  function detailText(d, fallback){
    const x = d && d.detail;
    if(typeof x==="string" && x) return x;
    if(x && typeof x==="object" && typeof (x.message||x.msg)==="string") return x.message||x.msg;
    return fallback;
  }
  function netError(){
    return new Error(navigator.onLine===false
      ? "网络断了，连上网再试一次"
      : "网络不好，没连上服务器，请稍后再试");
  }
  async function api(path, opts={}){
    const ctl = new AbortController();
    const timer = setTimeout(()=>ctl.abort(), opts.timeout||20000);
    let r;
    try{
      r = await fetch("/api"+path, {method:opts.method||"GET", credentials:"same-origin",
        headers:opts.body!==undefined?{"Content-Type":"application/json"}:{},
        body:opts.body!==undefined?JSON.stringify(opts.body):undefined, signal:ctl.signal});
    }catch(_){ throw netError(); }
    finally{ clearTimeout(timer); }
    return handle(r.status, await r.json().catch(()=>null));
  }
  function handle(status, data){
    if(status===401){ location.href="/login"; throw new Error("请先登录"); }
    if(status===428){ location.href="/?full=1"; throw new Error("请先改密码"); }
    if(status<200||status>=300){
      const e = new Error(detailText(data, status>=500?"服务器开小差了，请稍后再试":"没办成，请刷新后再试"));
      e.status = status; throw e;
    }
    return data;
  }
  /* 带进度的上传:fetch 拿不到上传进度,用 XHR */
  function upload(path, form, onProgress){
    return new Promise((resolve, reject)=>{
      const x = new XMLHttpRequest();
      x.open("POST", "/api"+path);
      x.timeout = 180000;
      x.upload.onprogress = e=>{ if(e.lengthComputable && onProgress) onProgress(e.loaded/e.total); };
      x.onload = ()=>{
        let d = null; try{ d = JSON.parse(x.responseText); }catch(_){}
        try{ resolve(handle(x.status, d)); }catch(e){ reject(e); }
      };
      x.onerror = ()=>reject(new Error("网络不好，没传上去。照片还在，点「重试」再传一次"));
      x.ontimeout = ()=>reject(new Error("传得太慢超时了。照片还在，换个网络好的地方点「重试」"));
      x.send(form);
    });
  }
  function reqKey(){
    const a = new Uint8Array(12);
    (window.crypto||{}).getRandomValues ? crypto.getRandomValues(a) : a.forEach((_,i)=>a[i]=Math.random()*256);
    return "staff-"+Array.from(a, b=>b.toString(16).padStart(2,"0")).join("");
  }

  /* ---------- 照片:前端先压到长边 1600 的 JPEG,省流量 ---------- */
  async function compress(file){
    if(!file || !/^image\//.test(file.type||"image/")) throw new Error("请选择照片");
    const url = URL.createObjectURL(file);
    try{
      const img = await new Promise((ok, bad)=>{ const i=new Image(); i.onload=()=>ok(i);
        i.onerror=()=>bad(new Error("这张照片读不出来，请重新拍一张")); i.src=url; });
      const w = img.naturalWidth, h = img.naturalHeight, k = Math.min(1, 1600/Math.max(w, h, 1));
      const c = document.createElement("canvas");
      c.width = Math.max(1, Math.round(w*k)); c.height = Math.max(1, Math.round(h*k));
      c.getContext("2d").drawImage(img, 0, 0, c.width, c.height);
      const blob = await new Promise(ok=>c.toBlob(ok, "image/jpeg", 0.82));
      return blob || file;
    } finally { URL.revokeObjectURL(url); }
  }
  function pickPhoto(multiple){
    return new Promise(resolve=>{
      const input = document.createElement("input");
      input.type = "file"; input.accept = "image/*"; input.setAttribute("capture", "environment");
      if(multiple) input.multiple = true;
      input.style.display = "none";
      input.onchange = ()=>{ const files = Array.from(input.files||[]); input.remove(); resolve(files); };
      document.body.appendChild(input);
      input.click();
    });
  }

  /* ---------- 小部件 ---------- */
  function msg(text, kind="err"){ return `<div class="msg ${kind}" role="${kind==="err"?"alert":"status"}">${esc(text)}</div>`; }
  function back(){ return `<button type="button" class="back" data-go="">← 回我的待办</button>`; }
  function dueLine(it){
    if(!it.due_text) return "";
    return (it.urgency==="overdue"?"⏰ 已过截止 ":"截止 ")+esc(it.due_text);
  }
  function tag(it){
    let key = it.urgency, text = URGENCY[key]||"";
    if(it.kind==="task" && it.status==="todo" && it.review_note && it.reviewed_at){ text = "被打回，要重做"; key = "overdue"; }
    return text ? `<span class="tag ${esc(key)}">${esc(text)}</span>` : "";
  }
  function cardHtml(it){
    const sub = [it.branch_name, dueLine(it)].filter(Boolean).join(" · ");
    let extra = "";
    if(it.kind==="checklist" && it.progress) extra = ` · 做了 ${it.progress.done}/${it.progress.total} 项`;
    if(it.kind==="task" && it.require_photo && it.status==="todo") extra = " · 要拍照";
    return `<button type="button" class="card" data-open="${esc(it.kind)}:${esc(it.id)}">
      <div class="t">${KIND_ICON[it.kind]||"📌"} ${esc(it.title)}</div>
      <div class="m">${tag(it)}${sub}${esc(extra)}</div></button>`;
  }
  function findItem(kind, id){
    const all = (S.todo&&S.todo.items)||[];
    return all.find(x=>x.kind===kind && String(x.id)===String(id))
      || (kind==="review" ? ((S.todo&&S.todo.reviews)||[]).find(x=>String(x.id)===String(id)) : null);
  }

  /* ---------- 首页:我的待办 ---------- */
  function header(){
    const t = S.todo || {};
    const names = (t.branches||[]).map(b=>b.name).filter(Boolean);
    $("#today").textContent = `今天 ${t.date_text||""}` + (names.length ? " · "+names.slice(0,2).join("、")+(names.length>2?` 等 ${names.length} 家`:"") : "");
    const u = t.user || {};
    $("#who").textContent = [u.name, u.role_label].filter(Boolean).join(" · ");
  }
  function homeView(note){
    const t = S.todo || {items:[], reviews:[]};
    header();
    const todo = t.items.filter(x=>["overdue","today","later"].includes(x.urgency));
    const rest = t.items.filter(x=>!["overdue","today","later"].includes(x.urgency));
    const boss = t.user && t.user.can_dispatch;
    main().innerHTML = (note||"")
      + (boss ? `<div class="row" style="margin-top:14px">
          <button type="button" class="btn pri" data-go="dispatch">📤 派活</button>
          <button type="button" class="btn" data-go="reviews">🧐 待我审核${t.reviews&&t.reviews.length?`（${t.reviews.length}）`:""}</button></div>` : "")
      + `<h2>我的待办${todo.length?`（${todo.length}）`:""}</h2>`
      + (todo.length ? todo.map(cardHtml).join("") : `<div class="empty">🎉 今天的活都做完了</div>`)
      + (rest.length ? `<h2>已交 / 已做完</h2>`+rest.map(cardHtml).join("") : "")
      + `<div class="foot"><button type="button" class="btn" data-act="refresh">🔄 刷新</button>
          <a class="btn" href="/?full=1">💻 完整版</a>
          <button type="button" class="btn" data-act="logout">🚪 退出</button></div>`;
  }

  /* ---------- 派给我的活:拍照交差 ---------- */
  function photoPicker(key){
    const list = S.pending[key] = S.pending[key] || [];
    return `<div id="thumbs" class="thumbs">${list.map((p,i)=>`<div class="thumb"><img src="${esc(p.url)}" alt="第 ${i+1} 张">
      <button type="button" data-del="${i}" aria-label="删掉第 ${i+1} 张">×</button></div>`).join("")}</div>
      <button type="button" class="btn" data-act="shoot">📷 ${list.length?"再拍一张":"拍照"}</button>`;
  }
  async function taskView(id){
    let t = findItem("task", id);
    try{ t = await api(`/staff/tasks/${encodeURIComponent(id)}`); }catch(e){ if(!t){ main().innerHTML = back()+msg(e.message); return; } }
    S.current = t;
    const key = "task:"+t.id;
    const rejected = t.status==="todo" && t.review_note && t.reviewed_at;
    let body = `${back()}<div class="panel"><div class="t" style="font-size:20px;font-weight:900">${esc(t.title)}</div>
      <p class="sub">${[t.branch_name, t.created_by_name?`${t.created_by_name} 派的`:"", t.due_text?`截止 ${t.due_text}`:""].filter(Boolean).map(esc).join(" · ")}</p>
      ${t.detail?`<p>${esc(t.detail)}</p>`:""}
      ${rejected?msg("上次被打回："+t.review_note,"info"):""}</div>`;
    if(t.status==="todo" && t.can_submit!==false){
      body += `<h2>${t.require_photo?"拍照交差（要照片）":"做完了就交"}</h2>
        <div id="picker">${photoPicker(key)}</div>
        <label for="note">备注（可不填）</label><textarea id="note" maxlength="500" placeholder="比如：冷柜里的过期货已经下架">${esc((S.pending[key+":note"])||"")}</textarea>
        <div id="up" class="hidden"><div class="progress"><i id="bar"></i></div><div class="sub" id="uptext">正在上传…</div></div>
        <div id="err"></div>
        <button type="button" class="btn pri" data-act="submit" style="margin-top:14px">✅ 提交</button>`;
    } else {
      body += msg(`这件事${esc(t.status_label||"")}`, "info") + photosHtml(t.photos) + aiHtml(t);
    }
    main().innerHTML = body;
  }
  function photosHtml(list){
    if(!list || !list.length) return "";
    return `<div class="photos">${list.map(p=>`<a href="${esc(p.url)}" target="_blank" rel="noopener"><img src="${esc(p.url)}" alt="交差照片" loading="lazy"></a>`).join("")}</div>`;
  }
  function aiHtml(t){
    const a = t.ai_check;
    if(!a) return t.status==="submitted"&&t.photos&&t.photos.length ? `<div class="ai none">暂无 AI 验照片建议，等待负责人核对照片</div>` : "";
    if(a.error || !a.verdict) return `<div class="ai none">🤖 ${esc(a.reason||"AI 这次没看成，请直接看照片")}</div>`;
    const label = {pass:"像是做好了", doubt:"拿不准，建议再看看", fail:"像是没做好"}[a.verdict]||"";
    return `<div class="ai ${esc(a.verdict)}">🤖 AI 建议：${esc(label)}${a.reason?`。${esc(a.reason)}`:""}<div class="sub">只是建议，最后由人来定</div></div>`;
  }
  async function addPhotos(key){
    const files = await pickPhoto(true);
    if(!files.length) return;
    const list = S.pending[key] = S.pending[key] || [];
    const err = $("#err");
    for(const f of files.slice(0, 9-list.length)){
      try{ const blob = await compress(f); list.push({blob, url:URL.createObjectURL(blob)}); }
      catch(e){ if(err) err.innerHTML = msg(e.message); }
    }
    rerenderPicker(key);
  }
  function rerenderPicker(key){
    const box = $("#picker"); if(box) box.innerHTML = photoPicker(key);
  }
  async function submitTask(){
    const t = S.current; if(!t || S.busy) return;
    const key = "task:"+t.id, list = S.pending[key]||[];
    const note = ($("#note")||{}).value||"";
    S.pending[key+":note"] = note;
    const err = $("#err");
    if(t.require_photo && !list.length){ err.innerHTML = msg("这件事要拍照交差，请先点「拍照」"); return; }
    if(navigator.onLine===false){ err.innerHTML = msg("网络断了，照片还在，连上网再点提交"); return; }
    const form = new FormData();
    list.forEach((p,i)=>form.append("photos", p.blob, `photo-${i+1}.jpg`));
    form.append("note", note);
    S.busy = true;
    const btn = document.querySelector('[data-act="submit"]'); if(btn){ btn.disabled = true; btn.textContent = "正在提交…"; }
    $("#up").classList.remove("hidden"); err.innerHTML = "";
    try{
      await upload(`/staff/tasks/${encodeURIComponent(t.id)}/submit`, form, f=>{
        $("#bar").style.width = Math.round(f*100)+"%";
        $("#uptext").textContent = f<1 ? `正在上传 ${Math.round(f*100)}%` : "传完了，正在保存…";
      });
      list.forEach(p=>URL.revokeObjectURL(p.url));
      delete S.pending[key]; delete S.pending[key+":note"];
      await loadTodo();
      go("", msg("✅ 交上去了，等老板看", "ok"));
    }catch(e){
      $("#up").classList.add("hidden");
      err.innerHTML = msg(e.message);
      if(btn){ btn.disabled = false; btn.textContent = "🔁 重试提交"; }
    }finally{ S.busy = false; }
  }

  /* ---------- 今天的清单:逐项打勾,要照片的项拍照 ---------- */
  function checklistView(id){
    const run = findItem("checklist", id);
    if(!run){ main().innerHTML = back()+msg("这张清单找不到了，请刷新"); return; }
    S.current = run;
    const items = Array.isArray(run.items) ? run.items : [];
    main().innerHTML = `${back()}<div class="panel"><div class="t" style="font-size:20px;font-weight:900">${esc(run.title)}</div>
      <p class="sub">${[run.branch_name, run.due_text?`${run.due_text} 前做完`:""].filter(Boolean).map(esc).join(" · ")}</p>
      ${run.progress?`<p>做了 ${run.progress.done}/${run.progress.total} 项</p>`:""}</div>
      <div id="err"></div>
      <div class="panel">${items.length ? items.map((it,i)=>{
        const done = !!it.done, text = it.text||it.title||it.key;
        return `<div class="item"><div class="tx ${done?"done":""}">${i+1}. ${esc(text)}${it.require_photo&&!done?` <span class="tag today">要拍照</span>`:""}</div>
          ${done?`<span class="tag done">✓ 做完了</span>`:`<button type="button" class="btn ${it.require_photo?"":"ok"}" data-item="${esc(it.key)}">${it.require_photo?"📷 拍照打勾":"✓ 做完了"}</button>`}</div>`;
      }).join("") : `<div class="empty">这张清单没有项目</div>`}</div>`;
  }
  async function checkItem(key){
    const run = S.current; if(!run || S.busy) return;
    const it = (run.items||[]).find(x=>String(x.key)===String(key)); if(!it) return;
    const err = $("#err"); err.innerHTML = "";
    const form = new FormData();
    if(it.require_photo){
      const files = await pickPhoto(false);
      if(!files.length) return;
      try{ form.append("photo", await compress(files[0]), "photo.jpg"); }
      catch(e){ err.innerHTML = msg(e.message); return; }
    }
    form.append("done", "1"); form.append("note", "");
    S.busy = true;
    const btn = document.querySelector(`[data-item="${CSS.escape(String(key))}"]`);
    if(btn){ btn.disabled = true; btn.textContent = "提交中…"; }
    try{
      await upload(`/checklist/runs/${encodeURIComponent(run.id)}/items/${encodeURIComponent(key)}`, form);
      await loadTodo();
      const fresh = findItem("checklist", run.id);
      if(fresh) checklistView(run.id); else go("", msg("✅ 清单做完了", "ok"));
    }catch(e){
      err.innerHTML = msg(e.message);
      if(btn){ btn.disabled = false; btn.textContent = "🔁 重试"; }
    }finally{ S.busy = false; }
  }

  /* ---------- 巡店整改:拍复查照片 ---------- */
  function actionView(id){
    const a = findItem("inspection_action", id);
    if(!a){ main().innerHTML = back()+msg("这条整改找不到了，请刷新"); return; }
    S.current = a;
    const canSubmit = ["open","in_progress","reopened"].includes(a.status);
    main().innerHTML = `${back()}<div class="panel"><div class="t" style="font-size:20px;font-weight:900">🔧 ${esc(a.title)}</div>
      <p class="sub">${[a.branch_name, a.due_text?`截止 ${a.due_text}`:""].filter(Boolean).map(esc).join(" · ")}</p>
      ${a.plan?`<p><b>怎么改：</b>${esc(a.plan)}</p>`:""}
      ${a.hint?`<p class="sub">${esc(a.hint)}</p>`:""}</div>
      <div id="err"></div>
      ${canSubmit?`<div id="up" class="hidden"><div class="progress"><i id="bar"></i></div></div>
        <button type="button" class="btn pri" data-act="recheck">📷 改好了，拍复查照片提交</button>`
        :msg(URGENCY[a.status]||"已经提交复查了，等老板看", "info")}`;
  }
  async function submitRecheck(){
    const a = S.current; if(!a || S.busy) return;
    const err = $("#err"); err.innerHTML = "";
    const files = await pickPhoto(false);
    if(!files.length) return;
    const form = new FormData();
    try{ form.append("file", await compress(files[0]), "recheck.jpg"); }
    catch(e){ err.innerHTML = msg(e.message); return; }
    form.append("visit_id", a.visit_id); form.append("issue_id", a.issue_id);
    form.append("action_id", a.action_id||a.id);
    form.append("expected_version", a.expected_version??a.version??a.row_version??1);
    form.append("industry_key", a.industry_key||"");
    S.busy = true; $("#up").classList.remove("hidden");
    try{
      await upload("/inspections/rechecks", form, f=>{ $("#bar").style.width = Math.round(f*100)+"%"; });
      await loadTodo();
      go("", msg("✅ 复查照片交上去了，等老板看", "ok"));
    }catch(e){ $("#up").classList.add("hidden"); err.innerHTML = msg(e.message+"（照片没丢，可以再点一次）"); }
    finally{ S.busy = false; }
  }

  /* ---------- 店长:派活 ---------- */
  async function dispatchView(){
    main().innerHTML = back()+`<div class="empty"><span class="spin"></span> 正在加载…</div>`;
    try{ S.meta = await api("/staff/meta"); }catch(e){ main().innerHTML = back()+msg(e.message); return; }
    const m = S.meta;
    if(!m.can_dispatch || !m.branches.length){ main().innerHTML = back()+msg("你还没有可以派活的门店，请联系老板分配门店"); return; }
    const d = new Date(Date.now()+8*3600e3), pad = n=>String(n).padStart(2,"0");
    const def = `${d.getUTCFullYear()}-${pad(d.getUTCMonth()+1)}-${pad(d.getUTCDate())}T18:00`;
    S.dispatchKey = reqKey();
    main().innerHTML = `${back()}<h2>📤 派活给店员</h2><div id="err"></div>
      <label for="d-branch">哪家店</label><select id="d-branch">${m.branches.map(b=>`<option value="${b.id}">${esc(b.name)}</option>`).join("")}</select>
      <label for="d-who">派给谁</label><select id="d-who"></select>
      <label for="d-title">要做什么</label><input id="d-title" maxlength="60" placeholder="比如：把冷柜清洗一遍">
      <label for="d-detail">说明（可不填）</label><textarea id="d-detail" maxlength="1000"></textarea>
      <label for="d-due">什么时候前做完</label><input id="d-due" type="datetime-local" value="${def}">
      <label class="check"><input id="d-photo" type="checkbox" checked> 要拍照交差</label>
      <button type="button" class="btn pri" data-act="dispatch" style="margin-top:12px">📤 派出去</button>`;
    fillWho();
    $("#d-branch").addEventListener("change", fillWho);
  }
  function fillWho(){
    const b = (S.meta.branches||[]).find(x=>String(x.id)===$("#d-branch").value) || {members:[]};
    $("#d-who").innerHTML = `<option value="">先不指定（店里谁有空谁做）</option>`
      + b.members.map(u=>`<option value="${u.id}">${esc(u.name)}（${esc(u.title_label)}）</option>`).join("");
  }
  function cnLocalToTs(v){
    const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/.exec(v||"");
    if(!m) return null;
    return Date.UTC(+m[1], +m[2]-1, +m[3], +m[4]-8, +m[5])/1000;
  }
  async function submitDispatch(){
    if(S.busy) return;
    const err = $("#err"); err.innerHTML = "";
    const title = $("#d-title").value.trim();
    if(!title){ err.innerHTML = msg("请写一下要做什么"); $("#d-title").focus(); return; }
    const body = {title, detail:$("#d-detail").value.trim(), branch_id:+$("#d-branch").value,
      assignee_user_id:$("#d-who").value?+$("#d-who").value:null, due_at:cnLocalToTs($("#d-due").value),
      require_photo:$("#d-photo").checked, request_key:S.dispatchKey};
    S.busy = true;
    try{
      await api("/staff/tasks", {method:"POST", body});
      await loadTodo();
      go("", msg("✅ 派出去了", "ok"));
    }catch(e){ err.innerHTML = msg(e.message); }
    finally{ S.busy = false; }
  }

  /* ---------- 店长:待我审核 ---------- */
  function reviewsView(){
    const list = (S.todo&&S.todo.reviews)||[];
    main().innerHTML = `${back()}<h2>🧐 待我审核${list.length?`（${list.length}）`:""}</h2>`
      + (list.length ? list.map(t=>`<div class="panel" data-review="${t.id}">
          <div class="t" style="font-size:18px;font-weight:900">${esc(t.title)}</div>
          <p class="sub">${[t.branch_name, t.assignee_name?`${t.assignee_name} 交的`:"", t.submit_note?`备注：${t.submit_note}`:""].filter(Boolean).map(esc).join(" · ")}</p>
          ${photosHtml(t.photos)}${aiHtml(t)}
          <div class="err-slot"></div>
          <div class="row"><button type="button" class="btn ok" data-review-act="approve" data-id="${t.id}">👍 通过</button>
            <button type="button" class="btn bad" data-review-act="reject" data-id="${t.id}">↩️ 打回</button></div>
          <div class="reject-box hidden"><label>打回原因（店员会看到）</label><textarea maxlength="500" placeholder="比如：冷柜底层没擦干净，再拍一张近照"></textarea>
            <button type="button" class="btn bad" data-review-act="reject-send" data-id="${t.id}" style="margin-top:8px">确认打回</button></div>
        </div>`).join("") : `<div class="empty">没有等你审核的活</div>`);
  }
  async function review(id, action, box){
    if(S.busy) return;
    const slot = box.querySelector(".err-slot"); slot.innerHTML = "";
    if(action==="reject"){ box.querySelector(".reject-box").classList.remove("hidden"); box.querySelector("textarea").focus(); return; }
    const note = action==="reject-send" ? box.querySelector("textarea").value.trim() : "";
    if(action==="reject-send" && !note){ slot.innerHTML = msg("写一句原因，店员才知道哪里要重做"); return; }
    S.busy = true;
    try{
      await api(`/staff/tasks/${encodeURIComponent(id)}/review`, {method:"POST",
        body:{action:action==="approve"?"approve":"reject", note}});
      await loadTodo();
      reviewsView();
    }catch(e){ slot.innerHTML = msg(e.message); }
    finally{ S.busy = false; }
  }

  /* ---------- 路由与事件 ---------- */
  function go(hash, note){
    if(("#"+hash)!==location.hash && !(hash==="" && !location.hash)){
      S.note = note; location.hash = hash; return;
    }
    route(note);
  }
  function route(note){
    note = note || S.note; S.note = "";
    const h = location.hash.replace(/^#/, "");
    window.scrollTo(0, 0);
    let m;
    if((m = /^task-(\d+)$/.exec(h)) || (m = /^\/staff-tasks\/(\d+)$/.exec(h))) return taskView(m[1]);
    if((m = /^checklist-(.+)$/.exec(h))) return checklistView(m[1]);
    if((m = /^action-(.+)$/.exec(h))) return actionView(m[1]);
    if(h==="dispatch") return dispatchView();
    if(h==="reviews") return reviewsView();
    homeView(note);
  }
  const OPEN_HASH = {task:"task-", checklist:"checklist-", inspection_action:"action-"};
  document.addEventListener("click", e=>{
    const el = e.target.closest("[data-open],[data-go],[data-act],[data-del],[data-item],[data-review-act]");
    if(!el) return;
    if(el.dataset.open){ const [k, id] = el.dataset.open.split(":"); go((OPEN_HASH[k]||"task-")+id); return; }
    if(el.dataset.go!==undefined){ go(el.dataset.go); return; }
    if(el.dataset.del!==undefined && S.current){
      const key = "task:"+S.current.id, list = S.pending[key]||[], p = list.splice(+el.dataset.del, 1)[0];
      if(p) URL.revokeObjectURL(p.url);
      rerenderPicker(key); return;
    }
    if(el.dataset.item!==undefined){ checkItem(el.dataset.item); return; }
    if(el.dataset.reviewAct){ review(el.dataset.id, el.dataset.reviewAct, el.closest("[data-review]")); return; }
    const act = el.dataset.act;
    if(act==="shoot" && S.current) addPhotos("task:"+S.current.id);
    else if(act==="submit") submitTask();
    else if(act==="recheck") submitRecheck();
    else if(act==="dispatch") submitDispatch();
    else if(act==="refresh") refresh();
    else if(act==="logout") logout();
  });
  window.addEventListener("hashchange", ()=>route());
  function setNet(){ $("#net").classList.toggle("show", navigator.onLine===false); }
  window.addEventListener("offline", setNet);
  window.addEventListener("online", ()=>{ setNet(); if(!location.hash) refresh(); });

  async function loadTodo(){ S.todo = await api("/staff/todo"); header(); return S.todo; }
  async function refresh(){
    main().innerHTML = `<div class="empty"><span class="spin"></span> 正在刷新…</div>`;
    try{ await loadTodo(); homeView(); }
    catch(e){ main().innerHTML = msg(e.message)+`<button type="button" class="btn" data-act="refresh">🔄 再试一次</button>`; }
  }
  async function logout(){
    try{ await api("/auth/logout", {method:"POST"}); }catch(_){}
    location.href = "/login";
  }
  async function boot(){
    setNet();
    try{
      await loadTodo();
      route();
    }catch(e){
      main().innerHTML = msg(e.message)+`<button type="button" class="btn" data-act="refresh">🔄 再试一次</button>`;
    }
  }
  window.PH_STAFF = {state:S, route, compress};
  boot();
})();
