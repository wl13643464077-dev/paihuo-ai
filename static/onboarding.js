/* 第 1 期:新老板首次上手卡片(首页顶部)+ 平台后台「短信验证码登录」配置卡片。
   对外只暴露 window.PH_ONBOARDING:
   - card()          → 首页顶部插入的 HTML 字符串(有缓存时同步返回;第一次返回 Promise,数据到了首页再一起渲染)
   - smsAdminCard()  → 平台后台的短信登录配置卡片(同样先占位后填充)
   依赖 app.js 的全局工具:api / esc / toast / copyText / render / uiConfirm / passwordPolicyError / ME。 */
(function(){
  "use strict";
  const S = {state:null, loading:null, editing:false, generating:false, industries:null, expand:false, reopened:false, sms:null, storeMade:false};
  const me = ()=>{ try{ return typeof ME!=="undefined" ? ME : null; }catch(_){ return null; } };
  const h = s=>{ try{ return esc(s); }catch(_){ return String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c])); } };
  const say = msg=>{ try{ toast(msg); }catch(_){ alert(msg); } };
  const $id = id=>document.getElementById(id);

  function ensureStyle(){
    if($id("ph-onb-style")) return;
    const st=document.createElement("style");
    st.id="ph-onb-style";
    st.textContent=`
.ph-onb{background:linear-gradient(120deg,#fff4cf,#fffaf0 72%);border-width:3px}
.ph-onb h2{margin:0;font-size:19px}
.ph-onb .ph-step{border:2px solid rgba(51,41,31,.18);border-radius:14px;padding:12px 14px;margin-top:12px;background:#fffdf7}
.ph-onb .ph-step.ph-now{border-color:#33291f;box-shadow:3px 3px 0 rgba(51,41,31,.12)}
.ph-onb .ph-step.ph-ok{opacity:.8}
.ph-onb .ph-step-h{display:flex;align-items:center;gap:8px;font-weight:900;font-size:15px}
.ph-onb .ph-badge{display:inline-flex;align-items:center;justify-content:center;min-width:24px;height:24px;border-radius:12px;background:#ffd166;border:2px solid #33291f;font-size:13px}
.ph-onb .ph-ok .ph-badge{background:#9ee6b0}
.ph-onb .ph-fields{display:flex;flex-wrap:wrap;gap:10px;margin-top:10px}
.ph-onb .ph-fields>div{flex:1 1 220px;min-width:0}
.ph-onb .ph-fields label{display:block;font-weight:800;font-size:13px;margin-bottom:4px}
.ph-onb .ph-fields input{width:100%;box-sizing:border-box;min-height:44px;font-size:16px}
.ph-onb .ph-chips{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}
.ph-onb .ph-chips .btn{min-height:44px}
.ph-onb .ph-post{border:2px dashed rgba(51,41,31,.3);border-radius:12px;padding:10px 12px;margin-top:10px;background:#fff}
.ph-onb .ph-post-h{display:flex;align-items:center;gap:8px;justify-content:space-between;flex-wrap:wrap}
.ph-onb .ph-post-t{white-space:pre-wrap;word-break:break-word;line-height:1.7;margin-top:6px;font-size:15px}
.ph-onb .ph-post .btn{min-height:40px}
.ph-onb .ph-go{width:100%;min-height:48px;font-size:16px;margin-top:10px}
.ph-onb .ph-foot{display:flex;gap:10px;align-items:center;justify-content:space-between;flex-wrap:wrap;margin-top:12px}
.ph-onb .ph-link{background:none;border:none;color:#8b7355;text-decoration:underline;cursor:pointer;font-size:13px;padding:6px 0}
.ph-onb .ph-pw{margin-top:12px;padding:10px 12px;border-radius:12px;background:#eef6ff;font-size:13.5px;line-height:1.7}
.ph-onb .ph-pw summary{cursor:pointer;font-weight:800}
.ph-onb-done{display:flex;align-items:center;gap:10px;flex-wrap:wrap;padding:12px 16px}
.ph-onb-done .ph-grow{flex:1;min-width:180px;font-weight:800}
.ph-onb-line{padding:8px 14px}
.ph-onb-line .ph-grow a{color:inherit;text-decoration:underline}
.ph-onb-line .ph-grow .ph-link{font-size:inherit;font-weight:800;color:inherit;padding:0}
.ph-onb-line .ph-did{white-space:nowrap;font-weight:700}
.ph-onb .ph-nostore{margin-top:8px;padding:8px 10px;border-radius:10px;background:#fff;border:2px dashed rgba(51,41,31,.3);font-size:14px;font-weight:800}
.ph-onb .ph-nostore .ph-link{font-size:14px;font-weight:900;color:#33291f;padding:0 2px}
.ph-onb .ph-next{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:8px}
.ph-onb .ph-next-i{display:flex;flex-direction:column;gap:2px;min-height:44px;padding:10px 12px;border:2px solid rgba(51,41,31,.18);border-radius:12px;background:#fffdf7;color:inherit;text-decoration:none}
.ph-onb .ph-next-i.ph-ok{opacity:.65}
.ph-x{background:none;border:none;font-size:20px;line-height:1;cursor:pointer;padding:6px 8px;color:#8b7355}
@media (max-width:640px){
  .ph-onb .ph-fields>div{flex-basis:100%}
  .ph-onb .ph-post-h .btn{flex:1}
}`;
    document.head.appendChild(st);
  }

  /* ---------- 数据 ---------- */
  function load(force){
    if(S.loading && !force) return S.loading;
    S.loading = api("/onboarding",{routeScoped:false}).then(st=>{
      S.state=st||{show:false};
      if(!S.state.steps) S.state.steps={};   // 接口没带 steps 时也不能让卡片渲染报错
      if(S.state.show!==false&&!S.state.steps.industry) loadIndustries();
      paint();
      return S.state;
    }).catch(()=>{ S.state={show:false}; paint(); }).finally(()=>{ S.loading=null; });
    return S.loading;
  }
  function loadIndustries(){
    if(S.industries) return;
    S.industries=[];
    fetch("/api/guest/industries").then(r=>r.ok?r.json():{}).then(d=>{
      S.industries=Array.isArray(d&&d.industries)?d.industries.filter(x=>x&&x.key):[];
      paint();
    }).catch(()=>{});
  }
  function paint(){
    const box=$id("ph-onb");
    if(box) box.innerHTML=inner();
  }

  /* ---------- 渲染 ---------- */
  function stepBox(no,title,state,body){
    const cls = state==="ok" ? "ph-ok" : (state==="now" ? "ph-now" : "");
    return `<div class="ph-step ${cls}"><div class="ph-step-h"><span class="ph-badge">${state==="ok"?"✓":no}</span>${h(title)}</div>${body||""}</div>`;
  }
  function industryBody(st){
    if(st.steps.industry) return `<div class="sub" style="margin-top:4px">您的行业:<b>${h(st.industry?.name||"")}</b></div>`;
    const list=S.industries||[];
    if(!list.length) return `<div class="sub" style="margin-top:6px">正在加载行业列表…</div>`;
    return `<div class="sub" style="margin-top:6px">选好后,您这一行的专属数字员工就会出现。只能选 1 个,之后想加请联系顾问。</div>
      <div class="ph-chips">${list.map((x,i)=>`<button type="button" class="btn" onclick="PH_ONBOARDING.pickIndustry(${i},this)">${h(x.name||x.key)}</button>`).join("")}</div>`;
  }
  function storeForm(store){
    const f=(id,label,ph,val,extra="")=>`<div><label for="ph-s-${id}">${label}</label><input id="ph-s-${id}" placeholder="${h(ph)}" value="${h(val??"")}" ${extra}></div>`;
    return `<div class="ph-fields">
      ${f("name","店名 *","如:王记牛肉面",store.name,'maxlength="30" autocomplete="organization"')}
      ${f("city","城市 / 商圈","如:成都 春熙路",store.city,'maxlength="30"')}
      ${f("product","主打产品 / 服务 *","如:现熬牛骨汤面、卤味小菜",store.product,'maxlength="60"')}
      ${f("feature","一句话特色","如:汤底每天熬 8 小时,面是手工现拉的",store.feature,'maxlength="60"')}
      ${f("price","客单价(元,可不填)","如:35",store.price,'inputmode="decimal" maxlength="8"')}
    </div>
    <div class="actions" style="margin-top:10px"><button type="button" class="btn pri" onclick="PH_ONBOARDING.saveStore(this)">保存,下一步</button>
      ${S.editing?`<button type="button" class="btn" onclick="PH_ONBOARDING.cancelEdit()">不改了</button>`:""}</div>`;
  }
  // 店铺信息填了但还没建门店(弹窗点了「先不建」或误点到外面):留一行入口,点了打开预填好的建店表单。
  function noStoreHint(){
    if(hasStore()) return "";
    let can=true; try{ can=typeof openStoreForm==="function"; }catch(_){ can=false; }
    if(!can) return "";
    return `<div class="ph-nostore">🏪 还没建门店 → <button type="button" class="ph-link" onclick="PH_ONBOARDING.createStore()">建第一家店</button></div>`;
  }
  function storeBody(st){
    if(!st.steps.industry) return `<div class="sub" style="margin-top:4px">先选好行业,再填店铺信息。</div>`;
    const s=st.store||{};
    if(st.steps.store && !S.editing){
      const bits=[s.name,s.city,s.product].filter(Boolean).map(h).join(" · ");
      return `<div class="sub" style="margin-top:4px">${bits||"已填好"} <button type="button" class="ph-link" onclick="PH_ONBOARDING.editStore()">改一下</button></div>${noStoreHint()}`;
    }
    return `<div class="sub" style="margin-top:6px">花 1 分钟填一下,以后所有数字员工写东西都会照着您家的情况来。</div>${storeForm(s)}`;
  }
  function postsList(st){
    const posts=Array.isArray(st.posts)?st.posts:[];
    if(!posts.length) return "";
    return `${st.posts_date?`<div class="sub" style="margin-top:8px">${h(st.posts_date)} 写的,复制就能发:</div>`:""}`
      + posts.map((p,i)=>`<div class="ph-post">
        <div class="ph-post-h"><span class="tag">${h(p.platform)}</span>
          <button type="button" class="btn sm pri" onclick="PH_ONBOARDING.copy(${i})">📋 复制</button></div>
        <div class="ph-post-t">${h(p.text)}</div>
        ${p.tip?`<div class="sub" style="margin-top:6px">💡 ${h(p.tip)}</div>`:""}
      </div>`).join("");
  }
  function postsBody(st){
    const ready=st.steps.industry&&st.steps.store&&!S.editing;
    const left=Number(st.gen_left||0);
    let btn;
    if(S.generating){
      btn=`<button type="button" class="btn pri ph-go" disabled>✍️ 正在写,大约半分钟…</button>`;
    }else if(left<=0){
      btn=`<div class="sub" style="margin-top:8px">免费的 ${h(st.gen_limit||3)} 次已经用完啦。想天天写,去「营销工具箱」看看 →</div>`;
    }else{
      btn=`<button type="button" class="btn pri ph-go" ${ready?"":"disabled"} onclick="PH_ONBOARDING.generate()">✍️ 帮我写今天的 3 条文案(免费,还能用 ${left} 次)</button>`;
    }
    return `<div class="sub" style="margin-top:4px">朋友圈、小红书、大众点评各 1 条,按您家店和今天的日子写,复制就能发。</div>${btn}${postsList(st)}`;
  }
  function pwHint(st){
    if(!st.password_hint) return "";
    return `<details class="ph-pw"><summary>🔑 建议把密码改成您好记的</summary>
      <div>现在的密码是系统随机生成的,不好记。改一个自己记得住的(12 位以上,字母+数字)。</div>
      <div class="ph-fields">
        <div><label for="ph-pw-old">现在的密码</label><input id="ph-pw-old" type="password" onkeydown="if(event.key==='Enter'&&!event.isComposing){event.preventDefault();PH_ONBOARDING.changePassword(this.closest('.ph-pw').querySelector('.actions .btn'))}" autocomplete="current-password"></div>
        <div><label for="ph-pw-new">新密码</label><input id="ph-pw-new" type="password" onkeydown="if(event.key==='Enter'&&!event.isComposing){event.preventDefault();PH_ONBOARDING.changePassword(this.closest('.ph-pw').querySelector('.actions .btn'))}" autocomplete="new-password"></div>
        <div><label for="ph-pw-new2">再输一遍新密码</label><input id="ph-pw-new2" type="password" onkeydown="if(event.key==='Enter'&&!event.isComposing){event.preventDefault();PH_ONBOARDING.changePassword(this.closest('.ph-pw').querySelector('.actions .btn'))}" autocomplete="new-password"></div>
      </div>
      <div class="actions" style="margin-top:8px"><button type="button" class="btn" onclick="PH_ONBOARDING.changePassword(this)">保存新密码</button>
        <span class="sub">改好后这台设备不用重新登录</span></div></details>`;
  }
  // 上手 3 步做完后，同一个位置换成「接下来 3 件事」，每件直接链到真实页面。
  // 进度两处来源取「做过」:app.js 的 STATE.setup(建店后会先乐观改掉)和 /api/onboarding 每次重取的 progress。
  function progress(){
    let setup={};
    try{ setup=(typeof STATE!=="undefined"&&STATE&&STATE.setup)||{}; }catch(_){}
    const p=(S.state&&S.state.progress)||{};
    const any=k=>!!setup[k]||!!p[k];
    return {store:any("store")||S.storeMade, staff:any("staff"), staff_task:any("staff_task"), wechat:any("wechat")};
  }
  function nextSteps(){
    const setup=progress();
    const items=[
      {done:setup.store, icon:"🏪", t:"建门店", d:"录好门店，店长拍照就能巡店", href:"#/store"},
      {done:!!setup.staff, icon:"👥", t:"给店员开账号", d:"店员手机上就能看到今天的活", href:"#/team"},
      {done:!!setup.wechat, icon:"💬", t:"绑定微信", d:"做完了微信里直接收通知", href:"#/channels"},
    ];
    return `<div class="ph-next">${items.map(x=>`<a class="ph-next-i${x.done?" ph-ok":""}" href="${x.href}">
        <b>${x.done?"✅":x.icon} ${x.t}</b><span class="sub">${x.done?"已完成":x.d}</span></a>`).join("")}</div>`;
  }
  function hasStore(){ return progress().store; }
  // 已经有门店：上手卡收成一行，不再每天压在首屏；想写文案点一下再展开 3 步。
  // 做过的事打勾，不再当成「接下来」；三件都做完只剩一句「上手完成」。
  function storeLine(st){
    const p=progress();
    const todo=[
      {done:p.staff, html:`<a href="#/team">开店员账号</a>`, t:"开店员账号"},
      {done:p.staff_task, html:`<a href="#/staff-tasks">派第一件活</a>`, t:"派第一件活"},
      {done:!!st.done, html:`<button type="button" class="ph-link" onclick="PH_ONBOARDING.toggle(true)">写今天的文案</button>`, t:"写今天的文案"},
    ];
    const left=todo.filter(x=>!x.done);
    const doneBits=todo.filter(x=>x.done).map(x=>`<span class="ph-did">✓ ${x.t}</span>`).join(" ");
    const text=left.length
      ? `✅ 基本信息好了 · 接下来：${left.map(x=>x.html).join(" / ")}${doneBits?` <span class="sub">${doneBits}</span>`:""}`
      : `✅ 上手完成 <span class="sub">门店、店员账号、派活、文案都弄好了</span>`;
    return `<div class="card ph-onb ph-onb-done ph-onb-line${left.length?"":" ph-onb-all"}">
        <span class="ph-grow">${text}</span>
        <button type="button" class="ph-x" aria-label="关闭上手提示" onclick="PH_ONBOARDING.dismiss()">×</button>
        ${st.password_hint?`<div style="flex-basis:100%">${pwHint(st)}</div>`:""}
      </div>`;
  }
  function inner(){
    const st=S.state;
    if(!st||(!st.show&&!S.reopened)) return "";
    const collapsible=st.done||hasStore();
    if(hasStore() && (st.steps||{}).industry && !S.expand && !S.editing && !S.generating) return storeLine(st);
    if(st.done && !S.expand){
      return `<div class="card ph-onb ph-onb-done">
        <span class="ph-grow">✅ 上手完成。接下来 3 件事：</span>
        <button type="button" class="ph-link" onclick="PH_ONBOARDING.toggle(true)">再看今天的文案</button>
        <button type="button" class="ph-x" aria-label="关闭上手提示" onclick="PH_ONBOARDING.dismiss()">×</button>
        <div style="flex-basis:100%">${nextSteps()}</div>
        ${st.password_hint?`<div style="flex-basis:100%">${pwHint(st)}</div>`:""}
      </div>`;
    }
    const s=st.steps||{};
    const now=!s.industry?1:(!s.store||S.editing?2:3);
    const stateOf=(no,ok)=> ok&&!(no===2&&S.editing) ? "ok" : (no===now?"now":"");
    return `<div class="card ph-onb">
      <div style="display:flex;align-items:flex-start;gap:8px">
        <div style="flex:1"><h2>👋 3 分钟上手：选行业、填店铺，AI 专家帮您写今天的文案</h2>
          <div class="sub" style="margin-top:4px">跟着 3 步走，马上拿到能直接发的朋友圈、小红书、点评文案。</div></div>
        ${collapsible?`<button type="button" class="ph-x" aria-label="收起" onclick="PH_ONBOARDING.toggle(false)">−</button>`:""}
      </div>
      ${stepBox(1,"选您的行业",stateOf(1,s.industry),industryBody(st))}
      ${stepBox(2,"填一下店铺基本信息",stateOf(2,s.store),storeBody(st))}
      ${stepBox(3,"一键写今天的 3 条文案",stateOf(3,s.posts),postsBody(st))}
      ${st.done?`<div class="ph-step ph-now"><div class="ph-step-h">✅ 上手完成。接下来 3 件事：</div><div style="margin-top:10px">${nextSteps()}</div></div>`:""}
      ${pwHint(st)}
      <div class="ph-foot"><span class="sub">这张卡片只有老板账号能看到</span>
        <button type="button" class="ph-link" onclick="PH_ONBOARDING.dismiss()">先不用了,关掉</button></div>
    </div>`;
  }

  /* ---------- 动作 ---------- */
  async function pickIndustry(i,btn){
    const x=(S.industries||[])[i]; if(!x) return;
    let ok=true;
    try{ ok=await uiConfirm(`确认您做的是「${x.name||x.key}」吗?选定后要改只能联系顾问。`,{title:"确认行业",confirmText:"就选这个",danger:false}); }catch(_){ ok=confirm(`确认您做的是「${x.name||x.key}」吗?`); }
    if(!ok) return;
    document.querySelectorAll("#ph-onb .ph-chips button").forEach(b=>b.disabled=true);
    try{
      await api("/auth/industry",{method:"POST",body:{industry:x.key},routeScoped:false});
      say(`已开通「${x.name||x.key}」行业`);
      S.state=null;
      // 行业变了,员工/板块都要重新拉:清掉 app.js 的缓存后整页重绘。
      try{ ME=null; }catch(_){}
      try{ STATE=null; EMP=null; META=null; DEPTS=null; SHELL_DIRTY=true; }catch(_){}
      try{ render(); }catch(_){ location.reload(); }
    }catch(e){ say(e.message); document.querySelectorAll("#ph-onb .ph-chips button").forEach(b=>b.disabled=false); }
  }
  async function saveStore(btn){
    const v=id=>($id("ph-s-"+id)?.value||"").trim();
    const body={name:v("name"),city:v("city"),product:v("product"),feature:v("feature"),price:v("price")};
    if(!body.name) return say("先写一下店名");
    if(!body.product) return say("写一下您主要卖什么");
    if(body.price && !/^\d+(\.\d{1,2})?$/.test(body.price)) return say("客单价请填数字,比如 35");
    btn.disabled=true; const t=btn.textContent; btn.textContent="保存中…";
    try{
      await api("/onboarding/store",{method:"PUT",body,routeScoped:false});
      S.editing=false;
      say("店铺信息已保存,数字员工都记住了");
      await load(true);
      await offerFirstStore(body);
    }catch(e){ say(e.message); btn.disabled=false; btn.textContent=t; }
  }
  // 第 2 步填的店名、城市和「新建门店」是同一份信息:企业还没有门店时问一句,点一下就建好第一家。
  async function firstStoreMissing(){
    try{
      const r=await api("/inspections?limit=1",{routeScoped:false});
      const s=r&&r.summary;
      // 拿不准(没开巡店、接口没给数)就不问,宁可少问也不重复建店
      return !!s && s.availability!==false && typeof s.total_branches==="number" && s.total_branches===0;
    }catch(_){ return false; }
  }
  async function offerFirstStore(body){
    if(!body||!body.name||!(await firstStoreMissing())) return;
    const label=[body.name,body.city].filter(Boolean).join(" · ");
    let ok=false;
    try{
      ok=await uiConfirm(`「${label}」。建好后店长拍照就能巡店，派活、开闭店清单也都按门店来；以后可以在「门店 → 我的门店」改名。`,
        {title:"用这个名字建第一家门店？",confirmText:"建这家门店",cancelText:"先不建",danger:false});
    }catch(_){ return; }
    if(!ok) return;
    try{
      await api("/inspections/branches",{method:"POST",body:{name:body.name,region:body.city||""},routeScoped:false});
      S.storeMade=true;
      try{ markSetupChanged({store:true}); }catch(_){ try{ if(STATE&&STATE.setup) STATE.setup.store=true; }catch(_){} }
      try{ STORE_TODAY={key:"",at:0,data:null}; }catch(_){}
      say(`第一家门店「${body.name}」已建好`);
      // 在「今天」页时把门店卡一起刷新;别的页面下次进来自然是新的
      const onToday=!location.hash||location.hash==="#/"||location.hash==="#";
      if(onToday){ try{ await render(); }catch(_){ paint(); } } else paint();
    }catch(e){ say(e.message); }
  }
  async function generate(){
    if(S.generating) return;
    S.generating=true; paint();
    try{
      const r=await api("/onboarding/posts",{method:"POST",body:{},timeout:180000,longRunning:true,routeScoped:false});
      Object.assign(S.state,{posts:r.posts,posts_date:r.posts_date,gen_used:r.gen_used,gen_left:r.gen_left,done:true});
      if(S.state.steps) S.state.steps.posts=true;
      S.expand=true;
      say("写好了,点「复制」就能发");
    }catch(e){ say(e.message); if(!e.uncertain) load(true); }
    finally{ S.generating=false; paint(); }
  }
  function copy(i){
    const p=(S.state?.posts||[])[i]; if(!p) return;
    try{ copyText(p.text); }catch(_){ navigator.clipboard?.writeText(p.text).then(()=>say("已复制")); }
  }
  async function dismiss(){
    try{
      await api("/onboarding/dismiss",{method:"POST",body:{},routeScoped:false});
      if(S.state) S.state.show=false;
      S.reopened=false;
      paint();
      say("已关掉。随时可以在「我的 → 新手引导重看」找回来");
    }catch(e){ say(e.message); }
  }
  async function changePassword(btn){
    const oldPw=$id("ph-pw-old")?.value||"", nw=$id("ph-pw-new")?.value||"", nw2=$id("ph-pw-new2")?.value||"";
    if(!oldPw) return say("先输入现在的密码");
    let err=""; try{ err=passwordPolicyError(nw); }catch(_){ err=nw.length<12?"密码至少需要 12 位":""; }
    if(err) return say(err);
    if(nw!==nw2) return say("两次输入的新密码不一样");
    btn.disabled=true;
    try{
      await api("/auth/password",{method:"PUT",body:{old:oldPw,new:nw},routeScoped:false});
      // 服务端已给这台设备补发新会话，不用重新登录。
      say("密码改好了，下次登录用新密码");
      if(S.state) S.state.password_hint=false;
      paint();
    }catch(e){ say(e.message); btn.disabled=false; }
  }

  /* ---------- 平台后台:短信验证码登录 ---------- */
  function smsInner(){
    const c=S.sms;
    if(!c) return `<div class="sub">加载中…</div>`;
    return `<div class="sub">打开后,登录页多一个「手机号 + 验证码」登录方式。短信走阿里云短信服务:在阿里云短信控制台申请好<b>签名</b>和<b>验证码模板</b>(模板变量写 <code>\${code}</code>),再建一个只有短信权限的 AccessKey 填到这里。没填齐之前登录页不会显示这个入口。</div>
      <div class="row" style="margin-top:8px">
        <div><label>AccessKey ID ${c.key_id_set?`<span class="tag">已设置</span>`:`<span class="tag">未设置</span>`}</label><input id="ph-sms-kid" type="password" placeholder="留空不改" autocomplete="off"></div>
        <div><label>AccessKey Secret ${c.key_secret_set?`<span class="tag">已设置</span>`:`<span class="tag">未设置</span>`}</label><input id="ph-sms-ksec" type="password" placeholder="留空不改" autocomplete="off"></div>
      </div>
      <div class="row" style="margin-top:8px">
        <div><label>短信签名名称</label><input id="ph-sms-sign" value="${h(c.sign_name||"")}" placeholder="如:派活"></div>
        <div><label>验证码模板 Code</label><input id="ph-sms-tpl" value="${h(c.template_code||"")}" placeholder="如:SMS_123456789"></div>
      </div>
      <label style="display:flex;align-items:center;gap:8px;margin-top:10px;font-weight:800"><input type="checkbox" id="ph-sms-en" ${c.enabled?"checked":""} style="width:auto"> 开启验证码登录</label>
      <div class="actions"><button class="btn pri" onclick="PH_ONBOARDING.saveSms(this)">💾 保存短信配置</button>
        <span class="sub">当前:${c.active?"登录页已显示验证码登录":"未启用"}</span></div>`;
  }
  function smsAdminCard(){
    const m=me();
    if(!m||m.role!=="root") return "";
    api("/admin/sms").then(c=>{ S.sms=c; const b=$id("ph-sms-admin-body"); if(b) b.innerHTML=smsInner(); })
      .catch(()=>{ const b=$id("ph-sms-admin-body"); if(b) b.innerHTML=`<div class="sub">读取短信配置失败,刷新再试</div>`; });
    return `<div class="card" id="ph-sms-admin"><h2>📱 短信验证码登录(默认关闭)</h2><div id="ph-sms-admin-body">${smsInner()}</div></div>`;
  }
  async function saveSms(btn){
    const body={sign_name:$id("ph-sms-sign").value.trim(),template_code:$id("ph-sms-tpl").value.trim(),enabled:$id("ph-sms-en").checked};
    const kid=$id("ph-sms-kid").value.trim(), ksec=$id("ph-sms-ksec").value.trim();
    if(kid) body.key_id=kid; if(ksec) body.key_secret=ksec;
    btn.disabled=true;
    try{
      const c=await api("/admin/sms",{method:"PUT",body});
      S.sms=c; say(c.warning||"短信配置已保存");
      const b=$id("ph-sms-admin-body"); if(b) b.innerHTML=smsInner();
    }catch(e){ say(e.message); btn.disabled=false; }
  }

  window.PH_ONBOARDING = {
    card(){
      const m=me();
      if(!m||m.role!=="owner") return "";
      ensureStyle();
      // 第一次没有数据时等它到了再交给首页一起首屏渲染(首页会 await),页面不会晚一拍跳动。
      if(!S.state) return load().then(()=>`<div id="ph-onb">${inner()}</div>`);
      // 每次回首页顺手刷新一次(比如别的页面改了企业档案);先用缓存立即出卡片。
      setTimeout(()=>load(), 0);
      return `<div id="ph-onb">${inner()}</div>`;
    },
    refresh(){ return load(true); },
    // 新建门店表单用:还没有门店时拿上手卡第 2 步填过的店名、城市来预填(只有老板有这份数据)
    async storeBasics(){
      const m=me();
      if(!m||m.role!=="owner") return null;
      try{ if(!S.state) await load(); }catch(_){ return null; }
      const s=S.state&&S.state.store;
      return s&&String(s.name||"").trim()?{name:String(s.name||""),city:String(s.city||"")}:null;
    },
    createStore(){ try{ openStoreForm(); }catch(_){ location.hash="#/store"; } },
    // 「我的 → 新手引导重看」:关掉过的卡片在本次打开期间重新显示(展开 3 步)。
    reopen(){ S.reopened=true; S.expand=true; },
    pickIndustry, saveStore, generate, copy, dismiss, changePassword,
    editStore(){ S.editing=true; paint(); },
    cancelEdit(){ S.editing=false; paint(); },
    toggle(open){ S.expand=!!open; paint(); },
    smsAdminCard, saveSms,
  };
})();
