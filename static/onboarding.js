/* 第 1 期:新老板首次上手卡片(首页顶部)+ 平台后台「短信验证码登录」配置卡片。
   对外只暴露 window.PH_ONBOARDING:
   - card()          → 首页顶部插入的 HTML 字符串(同步返回;数据没到时先给占位,到了自动填充)
   - smsAdminCard()  → 平台后台的短信登录配置卡片(同样先占位后填充)
   依赖 app.js 的全局工具:api / esc / toast / copyText / render / uiConfirm / passwordPolicyError / ME。 */
(function(){
  "use strict";
  const S = {state:null, loading:null, editing:false, generating:false, industries:null, expand:false, sms:null};
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
      if(S.state.steps&&!S.state.steps.industry) loadIndustries();
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
  function storeBody(st){
    if(!st.steps.industry) return `<div class="sub" style="margin-top:4px">先选好行业,再填店铺信息。</div>`;
    const s=st.store||{};
    if(st.steps.store && !S.editing){
      const bits=[s.name,s.city,s.product].filter(Boolean).map(h).join(" · ");
      return `<div class="sub" style="margin-top:4px">${bits||"已填好"} <button type="button" class="ph-link" onclick="PH_ONBOARDING.editStore()">改一下</button></div>`;
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
        <div><label for="ph-pw-old">现在的密码</label><input id="ph-pw-old" type="password" autocomplete="current-password"></div>
        <div><label for="ph-pw-new">新密码</label><input id="ph-pw-new" type="password" autocomplete="new-password"></div>
        <div><label for="ph-pw-new2">再输一遍新密码</label><input id="ph-pw-new2" type="password" autocomplete="new-password"></div>
      </div>
      <div class="actions" style="margin-top:8px"><button type="button" class="btn" onclick="PH_ONBOARDING.changePassword(this)">保存新密码</button>
        <span class="sub">改好后需要用新密码重新登录一次</span></div></details>`;
  }
  function inner(){
    const st=S.state;
    if(!st||!st.show) return "";
    if(st.done && !S.expand){
      return `<div class="card ph-onb ph-onb-done">
        <span class="ph-grow">✅ 已完成上手,<a href="#/tools">去看看更多 →</a></span>
        <button type="button" class="ph-link" onclick="PH_ONBOARDING.toggle(true)">再看今天的文案</button>
        <button type="button" class="ph-x" aria-label="关闭上手提示" onclick="PH_ONBOARDING.dismiss()">×</button>
        ${st.password_hint?`<div style="flex-basis:100%">${pwHint(st)}</div>`:""}
      </div>`;
    }
    const s=st.steps||{};
    const now=!s.industry?1:(!s.store||S.editing?2:3);
    const stateOf=(no,ok)=> ok&&!(no===2&&S.editing) ? "ok" : (no===now?"now":"");
    return `<div class="card ph-onb">
      <div style="display:flex;align-items:flex-start;gap:8px">
        <div style="flex:1"><h2>👋 3 分钟上手:先让数字员工帮您写今天的文案</h2>
          <div class="sub" style="margin-top:4px">跟着 3 步走,马上拿到能直接发的朋友圈、小红书、点评文案。</div></div>
        ${st.done?`<button type="button" class="ph-x" aria-label="收起" onclick="PH_ONBOARDING.toggle(false)">−</button>`:""}
      </div>
      ${stepBox(1,"选您的行业",stateOf(1,s.industry),industryBody(st))}
      ${stepBox(2,"填一下店铺基本信息",stateOf(2,s.store),storeBody(st))}
      ${stepBox(3,"一键写今天的 3 条文案",stateOf(3,s.posts),postsBody(st))}
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
    }catch(e){ say(e.message); btn.disabled=false; btn.textContent=t; }
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
      paint();
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
      say("密码改好了,请用新密码重新登录");
      setTimeout(()=>{ location.href="/login"; },600);
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
      if(!S.state){ load(); return `<div id="ph-onb"></div>`; }
      // 每次回首页顺手刷新一次(比如别的页面改了企业档案);先用缓存立即出卡片。
      setTimeout(()=>load(), 0);
      return `<div id="ph-onb">${inner()}</div>`;
    },
    refresh(){ return load(true); },
    pickIndustry, saveStore, generate, copy, dismiss, changePassword,
    editStore(){ S.editing=true; paint(); },
    cancelEdit(){ S.editing=false; paint(); },
    toggle(open){ S.expand=!!open; paint(); },
    smsAdminCard, saveSms,
  };
})();
