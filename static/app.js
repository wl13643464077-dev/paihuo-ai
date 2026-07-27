/* 老板的AI集团 — 前端 SPA(多部门数字员工办公室) */
let META = null, STATE = null, EMP = null, CUR_JOB = null, SEL_IDX = null, EDIT_MODE = false;
let MODAL_IDX = null, MODAL_TAB = "skills";
let DEPTS = null, CUR_DEPT = "content", SPEC = null, SPEC_TAB = "task", SPEC_TASK = null;
let EXP_PREFILL = "";   // V27 智能派活:「帮我选/改派」时带给下一位专家派活台的一句话
const LEARN_LOGS = {};   // idx -> [{k,l,ts}]
const LIVE_LINE = {};    // idx -> 最近一条实时动作文本

const $ = s => document.querySelector(s);
const rmb = u => "¥" + ((u||0)*7.2).toFixed(2);
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
// 把值安全塞进双引号事件属性:JSON.stringify 不转义单引号,直接进 onclick='...' 会被 ' 截断→XSS;故用双引号属性+esc
const cp = x => esc(JSON.stringify(x));
function passwordPolicyError(value){
  const password=String(value??"");
  if(password.length<12) return "密码至少需要 12 位";
  if(password.length>128) return "密码不能超过 128 位";
  const classes=[
    /[\p{L}]/u.test(password),
    /[\p{N}]/u.test(password),
    /[^\p{L}\p{N}]/u.test(password),
  ].filter(Boolean).length;
  return classes<2?"密码至少包含字母、数字、符号中的两类":"";
}
function safeExternalUrl(value){
  try{
    const raw=String(value??"").trim(), u=new URL(raw);
    if(!["http:","https:"].includes(u.protocol)||u.username||u.password) return "";
    return u.href;
  }catch(_){ return ""; }
}
function safeAssetUrl(value, kind=""){
  try{
    const u=new URL(String(value??"").trim(),location.origin);
    if(!["http:","https:"].includes(u.protocol)||u.origin!==location.origin
      ||u.username||u.password||!u.pathname.startsWith("/files/")) return "";
    if(kind==="html"&&!/^\/files\/job\d+\/[^?#]+\.html$/i.test(u.pathname)) return "";
    return u.pathname+u.search;
  }catch(_){ return ""; }
}
function safeRouteUrl(value){
  const route=String(value??"").trim();
  return /^#\/[A-Za-z0-9_~.%/?=&:+-]*$/.test(route)?route:"";
}

/* ---------- 迷你 markdown ---------- */
function mdInline(s){
  // 行内:加粗/斜体/代码/链接(先转义再套标签)
  s = esc(s);
  s = s.replace(/`([^`]+)`/g,'<code>$1</code>');
  s = s.replace(/\*\*(.+?)\*\*/g,'<b>$1</b>').replace(/(?<!\*)\*(?!\s)([^*\n]+?)\*/g,'<i>$1</i>');
  s = s.replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g,'<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
  return s;
}
function md(src){
  if(!src) return "";
  const lines = String(src).replace(/\r/g,"").split("\n");
  let out = [], i = 0;
  const flushList = (buf, ord)=>{ if(buf.length) out.push(`<${ord?"ol":"ul"}>`+buf.map(x=>`<li>${mdInline(x)}</li>`).join("")+`</${ord?"ol":"ul"}>`); buf.length=0; };
  while(i < lines.length){
    let ln = lines[i];
    // 表格:当前行含 | 且下一行是分隔行 |---|
    if(/\|/.test(ln) && i+1<lines.length && /^\s*\|?[\s:|-]+\|?\s*$/.test(lines[i+1]) && /-/.test(lines[i+1])){
      const cell = r=>r.replace(/^\s*\|/,"").replace(/\|\s*$/,"").split("|").map(c=>c.trim());
      const head = cell(ln);
      const aligns = cell(lines[i+1]).map(s=>/^:-+:$/.test(s)?"center":/-+:$/.test(s)?"right":/^:-+/.test(s)?"left":"");
      i += 2; const rows = [];
      while(i<lines.length && /\|/.test(lines[i]) && lines[i].trim()){ rows.push(cell(lines[i])); i++; }
      let t = '<div class="mdtable-wrap"><table class="mdtable"><thead><tr>'
        + head.map((h,j)=>`<th${aligns[j]?` style="text-align:${aligns[j]}"`:""}>${mdInline(h)}</th>`).join("")
        + '</tr></thead><tbody>'
        + rows.map(r=>'<tr>'+head.map((_,j)=>`<td${aligns[j]?` style="text-align:${aligns[j]}"`:""}>${mdInline(r[j]||"")}</td>`).join("")+'</tr>').join("")
        + '</tbody></table></div>';
      out.push(t); continue;
    }
    // 标题
    let m = ln.match(/^(#{1,4})\s+(.*)$/);
    if(m){ const lv=m[1].length; out.push(`<h${lv}>${mdInline(m[2])}</h${lv}>`); i++; continue; }
    // 分割线
    if(/^\s*([-*_])\1{2,}\s*$/.test(ln)){ out.push("<hr>"); i++; continue; }
    // 引用
    if(/^>\s?/.test(ln)){ let buf=[]; while(i<lines.length && /^>\s?/.test(lines[i])){ buf.push(lines[i].replace(/^>\s?/,"")); i++; } out.push(`<blockquote>${buf.map(mdInline).join("<br>")}</blockquote>`); continue; }
    // 无序列表
    if(/^\s*[-*]\s+/.test(ln)){ let buf=[]; while(i<lines.length && /^\s*[-*]\s+/.test(lines[i])){ buf.push(lines[i].replace(/^\s*[-*]\s+/,"")); i++; } flushList(buf,false); continue; }
    // 有序列表
    if(/^\s*\d+\.\s+/.test(ln)){ let buf=[]; while(i<lines.length && /^\s*\d+\.\s+/.test(lines[i])){ buf.push(lines[i].replace(/^\s*\d+\.\s+/,"")); i++; } flushList(buf,true); continue; }
    // 空行
    if(!ln.trim()){ i++; continue; }
    // 段落(连续非空行合并)
    let para=[]; while(i<lines.length && lines[i].trim() && !/^(#{1,4}\s|>\s?|\s*[-*]\s|\s*\d+\.\s)/.test(lines[i]) && !(/\|/.test(lines[i])&&i+1<lines.length&&/^\s*\|?[\s:|-]+\|?\s*$/.test(lines[i+1])&&/-/.test(lines[i+1])) && !/^\s*([-*_])\1{2,}\s*$/.test(lines[i])){ para.push(lines[i]); i++; }
    out.push(`<p>${para.map(mdInline).join("<br>")}</p>`);
  }
  return out.join("\n");
}

let BUSY_N = 0, ROUTE_CONTROLLER = null, ACTIVE_ROUTE_SIGNAL = null;
let NAV_SEQ = 0, SHELL_DIRTY = false;
const INFLIGHT_MUTATIONS = new Map();
const STATE_CACHE_MAX_KEYS = 120;
const CLIENT_ERROR_SIGS = new Set();
function setBoundedState(store,key,value,maxKeys=STATE_CACHE_MAX_KEYS){
  if(!Object.prototype.hasOwnProperty.call(store,key)){
    while(Object.keys(store).length>=maxKeys) delete store[Object.keys(store)[0]];
  }
  store[key]=value;
  return value;
}
function pushBoundedState(store,key,value,maxItems=80,maxKeys=STATE_CACHE_MAX_KEYS){
  const items=Object.prototype.hasOwnProperty.call(store,key)&&Array.isArray(store[key])
    ?store[key]:setBoundedState(store,key,[],maxKeys);
  items.push(value);
  if(items.length>maxItems) items.splice(0,items.length-maxItems);
  return items;
}
function normalizeListContract(payload,fallbackLimit=0){
  const objectPayload=payload&&!Array.isArray(payload)&&typeof payload==="object"?payload:null;
  const items=Array.isArray(payload)?payload:
    Array.isArray(objectPayload?.items)?objectPayload.items:
    Array.isArray(objectPayload?.rows)?objectPayload.rows:
    Array.isArray(objectPayload?.jobs)?objectPayload.jobs:[];
  const limit=Math.max(0,Number(objectPayload?.limit||fallbackLimit)||0);
  const hasMore=objectPayload?.has_more===true||objectPayload?.truncated===true
    ||Boolean(limit&&Array.isArray(payload)&&items.length>=limit);
  const offset=Math.max(0,Number(objectPayload?.offset)||0);
  const total=Number.isFinite(Number(objectPayload?.total))?Number(objectPayload.total):null;
  const nextOffset=objectPayload?.next_offset!==null&&objectPayload?.next_offset!==undefined
    &&Number.isFinite(Number(objectPayload.next_offset))
    ?Number(objectPayload.next_offset):null;
  return {items,limit,offset,total,hasMore,truncated:hasMore,nextOffset,
    facets:objectPayload?.facets||{},nextCursor:objectPayload?.next_cursor||null};
}
function listContractNotice(contract,label="记录"){
  if(!contract||contract.total===null) return "";
  const count=contract.items?.length||0, start=count?contract.offset+1:0;
  const end=contract.offset+count;
  return `<div class="notice" role="status">正在显示 ${start}–${end} 项${esc(label)}，共 ${contract.total} 项；筛选和分页均由服务端处理。</div>`;
}
const LIST_PAGE_SIZE=40;
const LIST_OFFSETS={assets:0,knowledge:0,censor:0,video:0,publish:0,
  jobs:0,avatar:0,meetings:0,publog:0};
function listOffset(key){ return Math.max(0,Number(LIST_OFFSETS[key])||0); }
function resetListPage(key){ LIST_OFFSETS[key]=0; }
function listPath(path,key,params={}){
  const query=new URLSearchParams({limit:String(LIST_PAGE_SIZE),offset:String(listOffset(key))});
  Object.entries(params).forEach(([name,value])=>{
    if(value!==undefined&&value!==null&&String(value)!=="") query.set(name,String(value));
  });
  return `${path}?${query.toString()}`;
}
function listGo(key,offset){
  LIST_OFFSETS[key]=Math.max(0,Number(offset)||0);
  render(true);
}
function listPager(contract,key){
  if(!contract||contract.total===null) return "";
  const previous=Math.max(0,contract.offset-contract.limit);
  return `<div class="actions" style="justify-content:flex-end;margin-top:12px">
    <button class="btn sm" ${contract.offset<=0?"disabled":""} onclick="listGo(${cp(key)},${previous})">← 上一页</button>
    <span class="sub">第 ${contract.total?Math.floor(contract.offset/contract.limit)+1:1} 页</span>
    <button class="btn sm" ${contract.hasMore?"":"disabled"} onclick="listGo(${cp(key)},${contract.nextOffset??0})">下一页 →</button>
  </div>`;
}
function clientErrorFingerprint(value){
  let hash=2166136261;
  const text=String(value??"");
  for(let i=0;i<text.length;i++){
    hash^=text.charCodeAt(i);
    hash=Math.imul(hash,16777619);
  }
  return (hash>>>0).toString(16).padStart(8,"0");
}
function reportClientError(kind, err, extra={}){
  if(err?.name==="NavigationAbort") return;
  const errorName=String(err?.name||"Error").replace(/[^A-Za-z0-9_.:-]/g,"").slice(0,40)||"Error";
  const rawMessage=String(err?.message||err||"");
  const rawStack=String(err?.stack||"");
  const fingerprint=clientErrorFingerprint(`${kind}|${errorName}|${rawMessage}|${rawStack}`);
  const sig=`${kind}:${fingerprint}`;
  if(CLIENT_ERROR_SIGS.has(sig)||CLIENT_ERROR_SIGS.size>=20) return;
  CLIENT_ERROR_SIGS.add(sig);
  const payload=JSON.stringify({
    kind:String(kind||"error").replace(/[^A-Za-z0-9_.:-]/g,"").slice(0,40)||"error",
    error_name:errorName,
    fingerprint,
    path:location.pathname,
    hash:String(location.hash||"").slice(0,160),
    release:"web-v39",
    line:Number(extra.line)||0,
  });
  try{
    const blob=new Blob([payload],{type:"application/json"});
    if(navigator.sendBeacon?.("/api/clientlog",blob)) return;
  }catch(_){}
  try{
    fetch("/api/clientlog",{method:"POST",headers:{"Content-Type":"application/json"},
      body:payload,keepalive:true,credentials:"same-origin"}).catch(()=>{});
  }catch(_){}
}
window.addEventListener("error",e=>reportClientError("error",e.error||e.message,
  {source:e.filename,line:e.lineno}));
window.addEventListener("unhandledrejection",e=>reportClientError("unhandledrejection",e.reason));
function busy(on){
  let bar = $("#busybar");
  if(!bar){ document.body.insertAdjacentHTML("beforeend",'<div id="busybar"></div>'); bar = $("#busybar"); }
  BUSY_N += on?1:-1; if(BUSY_N<0) BUSY_N=0;
  if(BUSY_N>0){ bar.classList.add("on"); bar.style.width = "70%"; }
  else { bar.style.width = "100%"; setTimeout(()=>{ if(BUSY_N===0){ bar.classList.remove("on"); bar.style.width="0"; } }, 250); }
}
async function apiRequest(path, opts={}){
  busy(true);
  let r;
  const {timeout=60000, signal:explicitSignal, longRunning=false, routeScoped,
    dedupeKey:_dedupeKey, allowDuplicate:_allowDuplicate, ...fetchOpts} = opts;
  const method=String(fetchOpts.method||"GET").toUpperCase();
  const useRouteSignal=routeScoped===undefined ? method==="GET" : !!routeScoped;
  const ctl = new AbortController();
  // 页面数据读取随路由取消；POST/PUT/DELETE 默认独立完成，避免切页后服务端已扣点而客户端误判失败。
  const parentSignal = explicitSignal || (useRouteSignal ? ACTIVE_ROUTE_SIGNAL : null);
  let timedOut = false, navigationAborted = false;
  const onParentAbort = ()=>{ navigationAborted=true; ctl.abort(); };
  if(parentSignal){
    if(parentSignal.aborted) onParentAbort();
    else parentSignal.addEventListener("abort", onParentAbort, {once:true});
  }
  const tm = setTimeout(()=>{ timedOut=true; ctl.abort(); }, timeout);   // 60s 兜底超时,不再无限转圈
  try{
    r = await fetch("/api"+path, {headers:{"Content-Type":"application/json"}, ...fetchOpts,
      signal:ctl.signal, body: fetchOpts.body!==undefined ? JSON.stringify(fetchOpts.body) : undefined});
  } catch(e){
    if(e.name==="AbortError"&&navigationAborted){
      const stale = new Error("页面已经切换"); stale.name="NavigationAbort"; throw stale;
    }
    if(e.name==="AbortError"&&timedOut){
      const err=new Error(longRunning
        ?"等待响应超时，但服务器可能仍在处理。请先不要重复提交，稍后刷新任务或结果页确认。"
        :"请求超时了:网络不稳或服务器忙,稍后再试");
      err.name="RequestTimeout"; err.uncertain=!!longRunning; throw err;
    }
    throw e;
  } finally {
    clearTimeout(tm);
    if(parentSignal) parentSignal.removeEventListener("abort", onParentAbort);
    busy(false);
  }
  if(r.status===401){
    location.href="/login";
    const err=new Error("请先登录"); err.status=401; throw err;
  }
  if(r.status===402){ const e = await r.json().catch(()=>({detail:"点数不足"})); pay402(e.detail);
    const err=new Error(e.detail||"点数不足"); err.status=402; throw err; }
  if(!r.ok){ const e = await r.json().catch(()=>({detail:r.statusText}));
    const err=new Error(e.detail||"请求失败"); err.status=r.status; throw err; }
  return r.json();
}
function mutationRequestKey(path,opts={}){
  const method=String(opts.method||"GET").toUpperCase();
  let body="";
  try{ body=JSON.stringify(opts.body??null); }catch(_){ body="[不可序列化]"; }
  return String(opts.dedupeKey||`${method}:${path}:${body}`);
}
function api(path,opts={}){
  const method=String(opts.method||"GET").toUpperCase();
  if(["GET","HEAD"].includes(method)||opts.allowDuplicate===true) return apiRequest(path,opts);
  const key=mutationRequestKey(path,opts), existing=INFLIGHT_MUTATIONS.get(key);
  if(existing) return existing;
  const request=apiRequest(path,opts).finally(()=>{
    if(INFLIGHT_MUTATIONS.get(key)===request) INFLIGHT_MUTATIONS.delete(key);
  });
  INFLIGHT_MUTATIONS.set(key,request);
  return request;
}
const optionalResult = fallback => e=>{
  if(e?.name==="NavigationAbort"||e?.status===401||e?.status===403) throw e;
  return fallback;
};
let ME = null;
const can = m => ME && (ME.role!=="member" || ME.modules.includes(m));
const canWork = m => ME?.role!=="tour" && can(m);
const isAdmin = () => ME && (ME.role==="owner" || ME.role==="root");
const isBoss = () => ME && ME.role==="root" && ME.username==="boss";
const FUNNEL_EVENTS = new Set([
  "page_view","pricing_view",
]);
let LAST_FUNNEL_PAGE="";
function funnelDimension(value){
  const clean=String(value||"").toLowerCase().replace(/[^a-z0-9_-]/g,"").slice(0,32);
  return clean||"app";
}
function trackFunnel(event,dimension=""){
  if(!ME||ME.role==="tour"||!FUNNEL_EVENTS.has(event)) return;
  fetch("/api/funnel/events",{
    method:"POST",credentials:"same-origin",keepalive:true,
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({event,dimension:funnelDimension(dimension)}),
  }).catch(()=>{});
}
function trackFunnelSessionOnce(event,dimension=""){
  if(!ME) return;
  const key=`paihuo_funnel_session_${ME.id}_${event}_${funnelDimension(dimension)}`;
  try{ if(sessionStorage.getItem(key)) return; sessionStorage.setItem(key,"1"); }catch(_){}
  trackFunnel(event,dimension);
}
function trackPageView(page){
  const clean=funnelDimension(page||"office");
  if(clean===LAST_FUNNEL_PAGE) return;
  LAST_FUNNEL_PAGE=clean;
  trackFunnel("page_view",clean);
}

function toast(msg){
  // 队列化:连续报错不再互相覆盖(最多同屏 3 条);时长随文本长度伸缩,
  // 长消息开放选中复制(手机上排查类提示此前 2.6 秒就没了,根本读不完)。
  let host=$("#toast-stack");
  if(!host){ host=document.createElement("div"); host.id="toast-stack";
    host.style.cssText="position:fixed;bottom:28px;left:50%;transform:translateX(-50%);z-index:200;display:flex;flex-direction:column;gap:8px;align-items:center;max-width:88vw;pointer-events:none";
    document.body.appendChild(host); }
  const text=String(msg??"");
  const item=document.createElement("div");
  item.className="toast show"; item.textContent=text;
  item.style.cssText="position:static;transform:none;opacity:1;pointer-events:"+(text.length>40?"auto":"none")+";user-select:text";
  host.appendChild(item);
  while(host.children.length>3) host.firstChild.remove();
  const ms=Math.min(9000, 2600+Math.max(0,text.length-20)*55);
  setTimeout(()=>{ item.style.opacity="0"; setTimeout(()=>item.remove(),300); }, ms);
}
let UI_DIALOG_RESOLVE=null;
function closeUiDialog(value=null){
  const box=$("#ui-dialog");
  if(box) box.remove();
  const resolve=UI_DIALOG_RESOLVE;
  UI_DIALOG_RESOLVE=null;
  if(resolve) resolve(value);
}
function openUiDialog({title="请确认",message="",field=null,confirmText="确定",
  cancelText="取消",danger=false,alertOnly=false}={}){
  if(UI_DIALOG_RESOLVE) closeUiDialog(null);
  const fieldHtml=!field?"":field.type==="select"
    ?`<select id="ui-dialog-field" aria-label="${esc(field.label||title)}">${(field.options||[]).map(
      option=>`<option value="${esc(option.value)}" ${String(option.value)===String(field.value??"")?"selected":""}>${esc(option.label)}</option>`
    ).join("")}</select>`
    :field.multiline
      ?`<textarea id="ui-dialog-field" aria-label="${esc(field.label||title)}" placeholder="${esc(field.placeholder||"")}" style="min-height:130px">${esc(field.value||"")}</textarea>`
      :`<input id="ui-dialog-field" type="${esc(field.type||"text")}" aria-label="${esc(field.label||title)}"
          value="${esc(field.value||"")}" placeholder="${esc(field.placeholder||"")}" ${field.min!==undefined?`min="${Number(field.min)}"`:""}>`;
  document.body.insertAdjacentHTML("beforeend",`<div class="overlay ui-dialog-overlay" id="ui-dialog" role="presentation">
    <div class="panel ui-dialog-panel" role="dialog" aria-modal="true" aria-labelledby="ui-dialog-title">
      <div class="phead"><h2 id="ui-dialog-title" style="margin:0;flex:1">${esc(title)}</h2></div>
      <div class="pbody"><div class="ui-dialog-message">${esc(message).replace(/\n/g,"<br>")}</div>
        ${fieldHtml}<div class="sub" id="ui-dialog-error" role="alert" style="color:var(--bad);margin-top:7px"></div>
        <div class="actions" style="justify-content:flex-end">
          ${alertOnly?"":`<button class="btn" type="button" data-dialog-cancel>${esc(cancelText)}</button>`}
          <button class="btn ${danger?"bad":"pri"}" type="button" data-dialog-ok>${esc(confirmText)}</button>
        </div></div></div></div>`);
  const overlay=$("#ui-dialog"), input=$("#ui-dialog-field"), ok=overlay.querySelector("[data-dialog-ok]");
  return new Promise(resolve=>{
    UI_DIALOG_RESOLVE=resolve;
    const finish=value=>closeUiDialog(value);
    overlay.querySelector("[data-dialog-cancel]")?.addEventListener("click",()=>finish(null));
    overlay.addEventListener("click",event=>{ if(event.target===overlay&&!alertOnly) finish(null); });
    ok.addEventListener("click",()=>{
      if(!field) return finish(true);
      const value=input.value;
      if(field.required&&!String(value).trim()){
        $("#ui-dialog-error").textContent=field.requiredMessage||"请填写后再继续";
        input.focus();
        return;
      }
      if(field.validate){
        const error=field.validate(value);
        if(error){ $("#ui-dialog-error").textContent=error; input.focus(); return; }
      }
      finish(value);
    });
    overlay.addEventListener("keydown",event=>{
      if(event.key==="Escape"&&!alertOnly){ event.preventDefault(); finish(null); }
      if(event.key==="Enter"&&input&&!field?.multiline){
        event.preventDefault(); ok.click();
      }
    });
    setTimeout(()=>{ (input||ok).focus(); if(input?.select) input.select(); },0);
  });
}
function uiConfirm(message,options={}){
  return openUiDialog({title:options.title||"请确认",message,confirmText:options.confirmText||"确认",
    cancelText:options.cancelText||"取消",danger:options.danger!==false});
}
function uiPrompt(options={}){
  return openUiDialog({title:options.title||"请填写",message:options.message||"",
    confirmText:options.confirmText||"确定",cancelText:options.cancelText||"取消",
    danger:!!options.danger,field:{type:options.type||"text",multiline:!!options.multiline,
      value:options.value||"",placeholder:options.placeholder||"",label:options.label||"",
      options:options.options||[],min:options.min,required:options.required!==false,
      requiredMessage:options.requiredMessage,validate:options.validate}});
}
function uiAlert(options={}){
  const config=typeof options==="string"?{message:options}:options;
  return openUiDialog({title:config.title||"提示",message:config.message||"",
    confirmText:config.confirmText||"知道了",alertOnly:true});
}
function pay402(msg){
  // 点数不足不再是死胡同:给一条可点的充值出路(root在#/team充值,老板线下收款)
  let b = $("#paybar");
  if(!b){ b = document.createElement("div"); b.id = "paybar"; document.body.appendChild(b); }
  b.style.cssText = "position:fixed;left:50%;transform:translateX(-50%);bottom:18px;z-index:998;background:#fff6dc;border:2.5px solid var(--ink,#222);border-radius:13px;padding:10px 14px;display:flex;gap:10px;align-items:center;flex-wrap:wrap;box-shadow:4px 4px 0 rgba(0,0,0,.18);max-width:92vw";
  b.innerHTML = `<span style="font-weight:700">💎 ${esc(msg||"点数不足")}</span>
    ${ME?.role==="member"?`<span class="sub">充值请联系贵司主账号(企业主)</span>`:""}
    <a class="btn sm pri" href="#/billing" onclick="$('#paybar')?.remove()">看套餐 / 充值</a>
    <button class="btn sm" onclick="$('#paybar')?.remove()">先不用</button>`;
}
function copyText(s){ navigator.clipboard?.writeText(s).then(()=>toast("已复制")).catch(()=>{
  const ta=document.createElement("textarea"); ta.value=s; document.body.appendChild(ta); ta.select();
  document.execCommand("copy"); ta.remove(); toast("已复制");});}

/* ---------- 卡通员工 SVG ----------
   state: idle(打盹) work(敲键盘) await(举手等审批) fail(晕/被打断) learn(读书进修) */
function charSVG(color, emoji, state, size=100){
  const gid = "g" + String(color).replace(/[^a-zA-Z0-9]/g,"");
  return `<svg class="emp st-${state}" viewBox="0 0 120 128" width="${size}" height="${Math.round(size*128/120)}" aria-hidden="true">
  <defs>
    <linearGradient id="${gid}" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="${color}"/><stop offset="1" stop-color="${color}" stop-opacity=".72"/>
    </linearGradient>
  </defs>
  <ellipse cx="60" cy="121" rx="33" ry="5.5" fill="rgba(51,41,31,.14)"/>
  <g class="body-g">
    <path d="M28 112 Q22 46 60 41 Q98 46 92 112 Z" fill="url(#${gid})" stroke="#33291f" stroke-width="3.5" stroke-linejoin="round"/>
    <path d="M31 58 Q38 47 60 45 Q82 47 89 58 Q75 52 60 52 Q45 52 31 58 Z" fill="rgba(255,255,255,.35)"/>
    <path d="M40 88 Q40 112 40 112 L80 112 Q80 88 80 88 Q70 96 60 96 Q50 96 40 88 Z" fill="#fffaf0" stroke="#33291f" stroke-width="3" stroke-linejoin="round"/>
    <path d="M40 88 L60 96 L52 104 Z M80 88 L60 96 L68 104 Z" fill="#fffaf0" stroke="#33291f" stroke-width="2.6" stroke-linejoin="round"/>
    <path d="M60 96 l5.5 6.5 -5.5 10 -5.5 -10 Z" fill="#2b2317" stroke="#33291f" stroke-width="2"/>
    <path d="M46 62 a7.5 7 0 1 0 0 .1 M66.5 62 a7.5 7 0 1 0 0 .1" fill="none" stroke="#33291f" stroke-width="2.2" opacity=".55"/>
    <line x1="53.5" y1="63" x2="66.5" y2="63" stroke="#33291f" stroke-width="2.2" opacity=".55"/>
    <circle class="eye" cx="47" cy="64" r="4.1" fill="#33291f"/>
    <circle class="eye r" cx="73" cy="64" r="4.1" fill="#33291f"/>
    <path class="lid" d="M42 64 q5 4.5 10 0 M68 64 q5 4.5 10 0" stroke="#33291f" stroke-width="3" fill="none" stroke-linecap="round"/>
    <path class="browfail" d="M42 60 l9 7 M51 60 l-9 7 M69 60 l9 7 M78 60 l-9 7" stroke="#33291f" stroke-width="3" stroke-linecap="round"/>
    <circle cx="39" cy="73" r="4" fill="rgba(229,72,77,.28)"/>
    <circle cx="81" cy="73" r="4" fill="rgba(229,72,77,.28)"/>
    <ellipse class="mouth-work" cx="60" cy="79" rx="4.2" ry="5.2" fill="#33291f"/>
    <path class="mouth-await" d="M52 77 q8 8 16 0" stroke="#33291f" stroke-width="3" fill="none" stroke-linecap="round"/>
    <path class="mouth-idle" d="M56 79 q4 3.4 8 0" stroke="#33291f" stroke-width="3" fill="none" stroke-linecap="round"/>
    <path class="mouth-fail" d="M50 81 q5 -5 10 0 q5 5 10 0" stroke="#33291f" stroke-width="3" fill="none" stroke-linecap="round"/>
    <g transform="rotate(-8 40 100)"><rect x="31" y="93" width="17" height="12" rx="2.5" fill="#fffaf0" stroke="#33291f" stroke-width="2.4"/>
      <line x1="39.5" y1="93" x2="42" y2="86" stroke="#33291f" stroke-width="2"/>
      <text x="39.5" y="102" text-anchor="middle" font-size="8">${emoji}</text></g>
    <path class="book" d="M43 95 q8.5 -6 17 0 q8.5 -6 17 0 l0 15 q-8.5 -5 -17 0 q-8.5 -5 -17 0 Z" fill="#fffaf0" stroke="#33291f" stroke-width="3" stroke-linejoin="round"/>
    <g class="laptop"><rect x="42" y="93" width="36" height="21" rx="3.5" fill="#3a3128" stroke="#33291f" stroke-width="3"/>
      <rect class="screenlight" x="45.5" y="96.5" width="29" height="14" rx="2" fill="#9dc6ff"/></g>
    <circle class="hand l" cx="35" cy="106" r="5.6" fill="${color}" stroke="#33291f" stroke-width="3"/>
    <circle class="hand r" cx="85" cy="106" r="5.6" fill="${color}" stroke="#33291f" stroke-width="3"/>
    <circle cx="94" cy="33" r="12.5" fill="#fffaf0" stroke="#33291f" stroke-width="3"/>
    <circle cx="94" cy="33" r="9.5" fill="none" stroke="#ffd166" stroke-width="2"/>
    <text x="94" y="38" text-anchor="middle" font-size="13">${emoji}</text>
    <text class="zzz" x="82" y="48" font-size="12" font-weight="900" fill="#8b5cf6">z</text>
    <text class="zzz z2" x="90" y="40" font-size="16" font-weight="900" fill="#8b5cf6">Z</text>
    <g class="alert"><circle cx="24" cy="50" r="10" fill="#ffd166" stroke="#33291f" stroke-width="3"/>
      <text x="24" y="55" text-anchor="middle" font-weight="900" font-size="14" fill="#33291f">!</text></g>
    <text class="dizzy" x="60" y="30" text-anchor="middle" font-size="15">💫</text>
    <text class="spark" x="26" y="58" font-size="12" fill="#d98a06">✦</text>
    <text class="spark s2" x="92" y="62" font-size="10" fill="#d98a06">✦</text>
    <text class="spark s3" x="34" y="38" font-size="10" fill="#d98a06">✦</text>
  </g></svg>`;
}

/* ---------- 路由 ---------- */
const routes = {"":dashboard,"new":newBrief,"job":jobView,"profiles":profilesView,"assets":assetsView,
  "delivery":deliveryView,"knowledge":knowledgeView,"schedules":schedulesView,"settings":settingsView,
  "avatar":avatarView,"admin":adminView,"team":teamView,"billing":billingView,"meetings":meetingsView,
  "tasks":tasksView,"company":companyView,"production":productionView,"censor":censorView,
  "channels":channelsView,"tools":toolsView,"trash":trashView,"guide":guideReset};
function nav(){
  const cur = location.hash.replace("#/","").split("/")[0];
  const inbox = (STATE?.inbox?.length||0)+(STATE?.notifications?.length||0);
  // 主导航(常用) + 更多(次要,收进下拉)
  const primary = [["","🏢 办公室"]];
  if(ME && ME.role!=="tour") primary.push(["tasks","📋 任务中心"]);
  if(canWork("content")) primary.push(["new","➕ 下达任务"]);
  if(canWork("avatar")) primary.push(["avatar","🎥 数字人"]);
  primary.push(["meetings","🪑 会议室"]);
  if(canWork("content")) primary.push(["tools","🧰 工具箱"]);
  const more = [];
  if(canWork("content") && ME && (ME.role==="owner"||ME.role==="root")) more.push(["channels","📣 发布渠道"]);
  // 审查官是核心卖点(发前合规把关),此前只藏在可被永久关闭的引导卡后面
  if(canWork("content")) more.push(["censor","🛡️ 审查官"]);
  if(canWork("content")) more.push(["schedules","⏰ 定时任务"],["profiles","🎭 人设档案"]);
  if(canWork("library")) more.push(["assets","🗂️ 资产库"],["knowledge","📚 沉淀库"]);
  if(ME && (ME.role==="owner"||ME.role==="root")) more.push(["production","📊 员工产出"],["company","🏢 企业档案"]);
  if(isAdmin()) more.push(["trash","🗑 回收站"]);
  more.push(["billing","💎 套餐"]);
  if(ME && (ME.role==="owner"||ME.role==="root")) more.push(["team","👥 权限管理"]);
  if(ME && ME.role==="root") more.push(["admin","🛠 后台"]);
  // 引导卡(开工四步/发布三件套)只对 owner/root 展示,重看入口同样只给他们
  if(ME && (ME.role==="owner"||ME.role==="root")) more.push(["guide","🧭 重看新手引导"]);
  const link = ([k,l])=>`<a href="#/${k}" class="${cur===k?"on":""}">${l}${k===""&&inbox?`<span class="badge">${inbox}</span>`:""}</a>`;
  const moreOn = more.some(([k])=>k===cur);
  $("#nav").innerHTML = primary.map(link).join("")
    + `<span class="navmore ${moreOn?"on":""}"><button type="button" class="nav-action" onclick="this.parentNode.classList.toggle('open')" aria-haspopup="menu">更多 ▾</button>`
    + `<div class="navmenu">${more.map(link).join("")}</div></span>`
    + `<button type="button" class="nav-action navlogout" onclick="logout()" title="${ME?esc(ME.tenant)+" · "+esc(ME.username):""}">🚪 退出</button>`;
}
document.addEventListener("click", e=>{ if(!e.target.closest(".navmore")) document.querySelectorAll(".navmore.open").forEach(x=>x.classList.remove("open")); });
async function logout(){
  if(ME&&ME.role==="tour"){ location.href="/promo"; return; }
  await api("/auth/logout",{method:"POST"}).catch(()=>{});
  TS = {tab:"hot", busy:{}, hot:null, pcal:null,
    pcalYm:new Date().toISOString().slice(0,7), warm:null, leads:null,
    vars:null, shot:null, menu:null};
  location.href="/login"; }
function forcedPasswordView(){
  $("#nav").innerHTML=`<span style="font-weight:900">🔐 账号安全升级</span>
    <span style="flex:1"></span>
    <button type="button" class="nav-action navlogout" onclick="logout()">🚪 退出</button>`;
  $("#main").innerHTML=`<div class="card" style="max-width:620px;margin:42px auto">
    <h2>请先设置您自己的密码</h2>
    <div class="notice">为了账号安全,初始/旧密码需要换成您自己的新密码(仅需一次),之后就能正常查看任务和使用数字员工。</div>
    <div style="margin-top:14px"><label>当前密码</label>
      <input id="forced-pw-old" type="password" autocomplete="current-password"></div>
    <div class="row" style="margin-top:10px">
      <div><label>新密码（12—128 位，至少两类字符）</label>
        <input id="forced-pw-new" type="password" autocomplete="new-password"></div>
      <div><label>再次输入新密码</label>
        <input id="forced-pw-confirm" type="password" autocomplete="new-password"></div>
    </div>
    <div class="actions"><button class="btn pri" onclick="forcedPasswordChange()">保存新密码</button></div>
    <div class="sub">修改成功后需要使用新密码重新登录。</div>
  </div>`;
  setTimeout(()=>$("#forced-pw-old")?.focus(),50);
}
async function forcedPasswordChange(){
  const oldPassword=$("#forced-pw-old")?.value||"";
  const newPassword=$("#forced-pw-new")?.value||"";
  const confirmation=$("#forced-pw-confirm")?.value||"";
  const policyError=passwordPolicyError(newPassword);
  if(!oldPassword) return toast("请输入当前密码");
  if(policyError) return toast(policyError);
  if(newPassword!==confirmation) return toast("两次输入的新密码不一致");
  try{
    await api("/auth/password",{
      method:"PUT",
      body:{old:oldPassword,new:newPassword},
      routeScoped:false,
    });
    toast("密码已更新，请重新登录");
    setTimeout(()=>{location.href="/login";},500);
  }catch(e){ toast(e.message); }
}
function routeLoading(page){
  const labels={tasks:"任务中心",new:"下达任务",avatar:"数字人摄影棚",meetings:"AI会议室",
    tools:"营销工具箱",channels:"发布渠道",schedules:"定时任务",profiles:"人设档案",
    assets:"资产库",knowledge:"沉淀库",production:"员工产出",company:"企业档案",
    billing:"套餐",team:"权限管理",admin:"后台",job:"工单详情",delivery:"交付中心",
    trash:"回收站"};
  const box=$("#main"); if(!box) return;
  box.innerHTML=`<div class="card route-loading" role="status" aria-live="polite">
    <div style="display:flex;align-items:center;gap:12px"><span class="spin"></span>
      <div><b>正在打开${esc(labels[page]||"页面")}…</b><div class="sub" style="margin-top:3px">网络较慢时请稍候，页面已经收到您的点击。</div></div></div></div>`;
}
function enhanceResponsiveTables(root=document){
  root.querySelectorAll(".dimtable").forEach(table=>{
    const labels=[...table.querySelectorAll("thead th")].map(th=>th.textContent.trim());
    table.querySelectorAll("tbody tr").forEach(row=>{
      [...row.children].forEach((cell,index)=>{
        if(!cell.dataset.label) cell.dataset.label=labels[index]||`第 ${index+1} 项`;
      });
    });
  });
}
async function render(reuseShell=false){
  const renderSeq=++NAV_SEQ;
  if(ROUTE_CONTROLLER) ROUTE_CONTROLLER.abort();
  const routeController = new AbortController();
  ROUTE_CONTROLLER = routeController;
  ACTIVE_ROUTE_SIGNAL = routeController.signal;
  const targetHash=location.hash;
  const [page,arg] = targetHash.replace("#/","").split("/");
  if(reuseShell){
    if(ME&&STATE) nav();
    routeLoading(page);
  }
  try{
    // 每次跨页只轻量刷新身份/权限；大体积办公室状态仍按需复用。
    if(!ME||reuseShell){
      const nextMe=await api("/auth/me");
      const accessChanged=ME && (ME.id!==nextMe.id||ME.role!==nextMe.role
        ||JSON.stringify(ME.modules||[])!==JSON.stringify(nextMe.modules||[]));
      if(accessChanged){
        STATE=null; EMP=null; META=null; DEPTS=null; SHELL_DIRTY=true;
        TOOLS_FETCH_AT=0; TOOLS_META_CACHE=null; TOOLS_JOBS_CACHE=null;
      }
      ME=nextMe;
    }
    if(ME?.must_change_password){
      forcedPasswordView();
      return;
    }
    if(!META) META = await api("/meta");
    // 页面之间切换复用办公室总览；SSE 标脏或数据变更时再取最新状态。
    if(!reuseShell||!STATE||!EMP||SHELL_DIRTY){
      const [nextState,nextEmployees] = await Promise.all([api("/state"), api("/employees")]);
      if(renderSeq!==NAV_SEQ||location.hash!==targetHash) return;
      STATE=nextState; EMP=nextEmployees; SHELL_DIRTY=false;
    }
    if(renderSeq!==NAV_SEQ||location.hash!==targetHash) return;
    nav();
    await (routes[page]||dashboard)(arg);
    if(renderSeq!==NAV_SEQ||location.hash!==targetHash) return;
    trackPageView(page||"office");
    if(MODAL_IDX!==null) drawModal();
    enhanceResponsiveTables();
    if(page==="team") setTimeout(()=>{ if(typeof tmAutoLoad==="function") tmAutoLoad(); }, 0);
    if(window.AV_OPEN_CLONE && page==="avatar"){
      window.AV_OPEN_CLONE = false;
      const sum = [...document.querySelectorAll("summary")].find(x=>x.textContent.includes("克隆我的声音"));
      if(sum){ sum.parentElement.open = true; sum.scrollIntoView({behavior:"smooth",block:"center"});
        sum.parentElement.style.outline="3px solid #ffd166"; setTimeout(()=>sum.parentElement.style.outline="",3000); }
    }
  }catch(e){
    if(e?.name==="NavigationAbort"||renderSeq!==NAV_SEQ||location.hash!==targetHash) return;
    console.error("page render failed",e);
    const box=$("#main");
    if(e?.status===403){
      // 权限不足是确定性的,不是网络故障:说清原因给出路,不再提供无效的「重新加载」
      if(box) box.innerHTML=`<div class="card"><h2>这个页面需要更高权限</h2>
        <div class="sub">${esc(e?.message||"需要主账号权限")}${ME?.role==="member"?"。请联系贵司主账号(企业主)开通对应板块或代为操作":""}</div>
        <div class="actions"><a class="btn pri" href="#/">← 回办公室</a></div></div>`;
      return;
    }
    reportClientError("render",e);
    if(box) box.innerHTML=`<div class="card"><h2>页面暂时没加载出来</h2>
      <div class="sub">${esc(e?.message||"网络连接异常，请稍后重试")}</div>
      <div class="actions"><button class="btn pri" onclick="render(true)">重新加载</button></div></div>`;
  }finally{
    if(ROUTE_CONTROLLER===routeController) ACTIVE_ROUTE_SIGNAL=null;
  }
}
const FORM_EXCLUDED_TYPES = new Set(["password","file","hidden","button","submit","reset"]);
let TYPING_UNTIL = 0;
function safeFormFields(){
  return [...document.querySelectorAll(
    "#main input,#main textarea,#main select,#modal input,#modal textarea,#modal select"
  )].filter(el=>!FORM_EXCLUDED_TYPES.has(String(el.type||"").toLowerCase()));
}
function formFieldKey(el, index){
  const scope=el.closest("#modal")?"modal":"main";
  return el.id ? `${scope}:id:${el.id}` : `${scope}:slot:${index}:${el.name||el.tagName}`;
}
function detailsStateKey(el,index){
  const scope=el.closest("#modal")?"modal":"main";
  const summary=(el.querySelector(":scope > summary")?.textContent||"").trim().slice(0,80);
  return el.id?`${scope}:id:${el.id}`:`${scope}:slot:${index}:${summary}`;
}
function captureFormState(){
  const fields=safeFormFields(), active=document.activeElement;
  const details=[...document.querySelectorAll("#main details,#modal details")];
  return {
    hash:location.hash,
    fields:fields.map((el,index)=>({
      key:formFieldKey(el,index),
      value:el.multiple?[...el.selectedOptions].map(o=>o.value):el.value,
      checked:!!el.checked,
      checkable:["checkbox","radio"].includes(String(el.type||"").toLowerCase()),
      scrollTop:el.scrollTop||0,
    })),
    details:details.map((el,index)=>({
      key:detailsStateKey(el,index),
      open:el.open,
    })),
    activeKey:fields.includes(active)?formFieldKey(active,fields.indexOf(active)):null,
    selectionStart:typeof active?.selectionStart==="number"?active.selectionStart:null,
    selectionEnd:typeof active?.selectionEnd==="number"?active.selectionEnd:null,
  };
}
function restoreFormState(snapshot){
  if(!snapshot||snapshot.hash!==location.hash) return;
  const fields=safeFormFields(), byKey=new Map(snapshot.fields.map(x=>[x.key,x]));
  for(const [index,el] of fields.entries()){
    const saved=byKey.get(formFieldKey(el,index)); if(!saved) continue;
    if(el.multiple&&Array.isArray(saved.value)){
      [...el.options].forEach(o=>{o.selected=saved.value.includes(o.value);});
    }else if(saved.checkable) el.checked=saved.checked;
    else el.value=saved.value;
    el.scrollTop=saved.scrollTop||0;
  }
  const details=[...document.querySelectorAll("#main details,#modal details")];
  const savedDetails=new Map((snapshot.details||[]).map(x=>[x.key,x]));
  details.forEach((el,index)=>{
    const saved=savedDetails.get(detailsStateKey(el,index));
    if(saved) el.open=!!saved.open;
  });
  if(!snapshot.activeKey) return;
  const target=fields.find((el,index)=>formFieldKey(el,index)===snapshot.activeKey);
  if(!target) return;
  try{
    target.focus({preventScroll:true});
    if(snapshot.selectionStart!==null&&typeof target.setSelectionRange==="function")
      target.setSelectionRange(snapshot.selectionStart,snapshot.selectionEnd);
  }catch(_){}
}
document.addEventListener("input",e=>{
  if(e.target?.matches?.("input,textarea,select")
    &&!FORM_EXCLUDED_TYPES.has(String(e.target.type||"").toLowerCase()))
    TYPING_UNTIL=Date.now()+1200;
},true);
let RENDER_T = null, RENDERING = false;
function scheduleRender(){
  if(RENDER_T) return;
  RENDER_T = setTimeout(async ()=>{ RENDER_T=null;
    if(Date.now()<TYPING_UNTIL) return scheduleRender();
    if(RENDERING) return scheduleRender();
    const snapshot=captureFormState();
    RENDERING = true;
    try{ await render(); restoreFormState(snapshot); }
    finally { RENDERING = false; }
  }, 220);
}
window.addEventListener("hashchange",()=>{
  SEL_IDX=null; EDIT_MODE=false; closeModal(); closeSpec();
  window.scrollTo({top:0,left:0,behavior:"auto"});
  render(true);
});

/* ---------- SSE ---------- */
const SSE_STALE_MS = 40000;
let SSE = null, SSE_RETRY_T = null, SSE_RETRY_N = 0;
let SSE_LAST_EVENT_AT=0, SSE_WATCHDOG_T=null;
function refreshAuthoritativeState(){
  SHELL_DIRTY=true;
  if(ME) scheduleRender();
}
function stopSseWatchdog(){
  if(SSE_WATCHDOG_T!==null){ clearInterval(SSE_WATCHDOG_T); SSE_WATCHDOG_T=null; }
}
function startSseWatchdog(){
  stopSseWatchdog();
  SSE_WATCHDOG_T=setInterval(()=>{
    if(document.hidden||!SSE||!SSE_LAST_EVENT_AT) return;
    if(Date.now()-SSE_LAST_EVENT_AT<=SSE_STALE_MS) return;
    SSE_LAST_EVENT_AT=Date.now();
    reportClientError("sse_stale",new Error("事件流超过40秒无心跳"));
    refreshAuthoritativeState();
    stopSse(false);
    scheduleSseReconnect(0);
  },5000);
}
function stopSse(clearRetry=true){
  const es=SSE; SSE=null;
  stopSseWatchdog();
  if(es){
    es.onopen=null; es.onerror=null; es.onmessage=null;
    es.close();
  }
  if(clearRetry&&SSE_RETRY_T!==null){
    clearTimeout(SSE_RETRY_T);
    SSE_RETRY_T=null;
  }
}
function scheduleSseReconnect(delayOverride=null){
  if(SSE_RETRY_T!==null||document.hidden) return;
  const wait=delayOverride===null
    ?Math.min(30000,1000*(2**Math.min(SSE_RETRY_N++,5)))+Math.floor(Math.random()*500)
    :Math.max(0,Number(delayOverride)||0);
  SSE_RETRY_T=setTimeout(async ()=>{
    SSE_RETRY_T=null;
    if(document.hidden) return;
    try{
      const auth=await fetch("/api/auth/me",{cache:"no-store",credentials:"same-origin"});
      if(auth.status===401){ location.href="/login"; return; }
    }catch(_){}
    sse();
  },wait);
}
function sse(){
  if(document.hidden) return;
  stopSse();
  let es;
  try{ es = new EventSource("/api/events"); }
  catch(e){ reportClientError("sse_open",e); scheduleSseReconnect(); return; }
  SSE=es;
  es.onopen = ()=>{
    if(SSE!==es) return;
    SSE_RETRY_N=0;
    SSE_LAST_EVENT_AT=Date.now();
    startSseWatchdog();
    const conn=$("#conn"); if(conn){ conn.textContent="● 在线"; conn.style.color="#7fe0b0"; }
    refreshAuthoritativeState();
  };
  es.onerror = ()=>{
    if(SSE!==es) return;
    stopSse(false);
    const conn=$("#conn"); if(conn){ conn.textContent="● 重连中"; conn.style.color="#ff9d9f"; }
    scheduleSseReconnect();
  };
  es.onmessage = e=>{
    SSE_LAST_EVENT_AT=Date.now();
    try{ const ev = JSON.parse(e.data);
      if(["employee_update","meeting_update","avatar_update","tv_done","tool_update",
          "pub_update","task_update","job_update","gate_running"].includes(ev.type))
        SHELL_DIRTY=true;
      if(ev.type==="station_step"){ applyStep(ev); return; }
      if(ev.type==="employee_step"){ applyEmpStep(ev); return; }
      if(ev.type==="employee_update"){
        if(SPEC && SPEC.idx===ev.idx){
          api("/depts/emp/"+ev.idx,{routeScoped:false}).then(d=>{
            if(!SPEC||SPEC.idx!==ev.idx) return;
            const snapshot=captureFormState();
            SPEC=d; drawSpec(); restoreFormState(snapshot);
          }).catch(err=>reportClientError("sse_employee_refresh",err));
        }
        scheduleRender(); return; }
      if(ev.type==="task_step"){
        if(SPEC_TASK && SPEC_TASK.id===ev.task_id){
          (SPEC_TASK.steps = SPEC_TASK.steps||[]);
          const box = $("#spec-steps");
          if(box){
            const last = box.querySelector(`.step[data-n="${ev.n}"]`);
            if(last) last.outerHTML = stepRow(ev.step, ev.n);
            else{ const w=box.querySelector(".step:not([data-n])"); if(w) w.remove();
              box.insertAdjacentHTML("beforeend", stepRow(ev.step, ev.n)); }
            box.scrollTop = box.scrollHeight;
          }
        }
        const tcBox = document.querySelector(`[data-tasksteps="${ev.task_id}"]`);
        if(tcBox){
          const old = tcBox.querySelector(`.step[data-n="${ev.n}"]`);
          if(old) old.outerHTML = stepRow(ev.step, ev.n);
          else{ const wait=tcBox.querySelector(".step:not([data-n])"); if(wait) wait.remove();
            tcBox.insertAdjacentHTML("beforeend", stepRow(ev.step, ev.n)); }
          tcBox.scrollTop = tcBox.scrollHeight;
        }
        return; }
      if(ev.type==="meeting_msg"){
        const box = $("#mt-msgs");
        if(box && MT_CUR===ev.meeting_id){
          box.insertAdjacentHTML("beforeend", mtBubble(ev.msg));
          box.scrollTop = box.scrollHeight;
        }
        return; }
      if(ev.type==="meeting_update"){
        if(location.hash.startsWith("#/meetings")||location.hash.startsWith("#/tasks")) scheduleRender();
        return; }
      if(ev.type==="avatar_step"){
        const box = document.querySelector(`[data-avsteps="${ev.job_id}"]`);
        if(box){ box.insertAdjacentHTML("beforeend", stepRow(ev.step, ev.n)); box.scrollTop = box.scrollHeight; }
        return; }
      if(ev.type==="avatar_update"){
        if(location.hash.startsWith("#/avatar")||location.hash.startsWith("#/tasks")) scheduleRender();
        return; }
      if(ev.type==="tv_step"||ev.type==="tv_done"){
        if(location.hash.startsWith("#/delivery")||location.hash.startsWith("#/tools")||location.hash.startsWith("#/tasks")) scheduleRender();
        return; }
      if(ev.type==="tool_update"){
        TOOLS_FETCH_AT=0;
        TOOLS_JOBS_CACHE=null;
        if(location.hash.startsWith("#/tools")){
          refreshToolsPreservingDraft().catch(err=>{
            reportClientError("sse_tools_refresh",err);
            scheduleRender();
          });
          return;
        }
        if(location.hash.startsWith("#/tasks")) scheduleRender();
        return; }
      if(ev.type==="pub_update"){
        if(location.hash.startsWith("#/channels")||location.hash.startsWith("#/tasks")) scheduleRender();
        return; }
      if(ev.type==="task_update"){
        if(SPEC_TASK && SPEC_TASK.id===ev.task_id)
          specOpenTask(ev.task_id,true).catch(err=>reportClientError("sse_task_refresh",err));
        else if(SPEC && SPEC.idx===ev.idx){
          api("/depts/emp/"+ev.idx,{routeScoped:false}).then(d=>{
            if(!SPEC||SPEC.idx!==ev.idx) return;
            const snapshot=captureFormState();
            SPEC=d; drawSpec(); restoreFormState(snapshot);
          }).catch(err=>reportClientError("sse_expert_refresh",err));
        }
        if(SOLO_TASK && SOLO_TASK.id===ev.task_id){  // 交付后老板速览补到时刷新单独派活视图
          api("/tasks/"+ev.task_id,{routeScoped:false}).then(d=>{
            if(!SOLO_TASK||SOLO_TASK.id!==ev.task_id) return;
            const snapshot=captureFormState();
            SOLO_TASK=d;
            if(!["queued","running"].includes(d.status)) stopSoloWatch();
            if(MODAL_IDX!==null&&MODAL_TAB==="solo"){ drawModal(); restoreFormState(snapshot); }
          }).catch(err=>reportClientError("sse_solo_refresh",err));
        }
        DEPTS=null;
        if(location.hash.startsWith("#/tasks")){ scheduleRender(); return; }
        if(location.hash==="#/"||!location.hash) scheduleRender();
        return; }
      if(ev.type==="job_update"||ev.type==="gate_running"){
        const [page,arg] = location.hash.replace("#/","").split("/");
        if(page==="job" && +arg===ev.job_id) scheduleRender();
        else if(page==="tasks") scheduleRender();
        else if(page===""||page===undefined) scheduleRender();
      }
    }catch(err){ reportClientError("sse_message",err); }
  };
}
document.addEventListener("visibilitychange",()=>{
  if(document.hidden){ stopSse(); stopSoloWatch(); return; }
  refreshAuthoritativeState();
  sse();
  if(SOLO_TASK&&["queued","running"].includes(SOLO_TASK.status)&&MODAL_IDX!==null&&MODAL_TAB==="solo")
    soloWatch(SOLO_TASK.id);
});
window.addEventListener("online",()=>{ refreshAuthoritativeState(); if(!SSE) sse(); });
window.addEventListener("pagehide",()=>stopSse());
window.addEventListener("pageshow",e=>{ if(e.persisted){ refreshAuthoritativeState(); sse(); } });

/* ---------- 步骤可视化 ---------- */
const STEP_IC = {boot:"🟢",start:"▶️",search:"🔎",fetch:"🌐",tool:"🔧",typing:"✍️",
  retry:"♻️",gate:"🛡️",done:"✅",error:"❌"};
const hhmmss = ts=>new Date(ts*1000).toTimeString().slice(0,8);
function stepRow(s,n){
  return `<div class="step k-${esc(s.k)}" ${n?`data-n="${n}"`:""}><span class="ic">${STEP_IC[s.k]||"•"}</span>
    <span class="lb">${esc(s.l)}</span><span class="ts">${hhmmss(s.ts)}</span></div>`;
}
function stepsBox(idx, steps){
  return `<div class="steps" id="steps-${idx}">${(steps||[]).map((s,i)=>stepRow(s,i+1)).join("")||
    `<div class="step"><span class="ic">⏳</span><span class="lb">等待员工上线…</span></div>`}</div>`;
}
function stepsLog(idx, steps, open){
  if(!steps||!steps.length) return "";
  return `<details class="log"${open?" open":""}><summary>📜 工作日志(${steps.length} 步,每一步都可回看)</summary>
    ${stepsBox(idx,steps)}</details>`;
}
function applyStep(ev){
  setBoundedState(LIVE_LINE,ev.idx,ev.step.l);
  // 办公室房间的实时动作行
  const room = document.querySelector(`.room[data-room="${ev.idx}"] .live`);
  if(room) room.textContent = ev.step.l;
  const [page,arg] = location.hash.replace("#/","").split("/");
  if(page!=="job" || +arg!==ev.job_id) return;
  const stn = document.querySelector(`.stn[data-stn="${ev.idx}"]`);
  if(stn){
    let lv = stn.querySelector(".live");
    if(!lv){ lv = document.createElement("div"); lv.className = "live"; stn.appendChild(lv); }
    lv.textContent = ev.step.l;
  }
  const box = document.querySelector(`#steps-${ev.idx}`);
  if(box){
    const last = box.querySelector(`.step[data-n="${ev.n}"]`);
    if(last) last.outerHTML = stepRow(ev.step, ev.n);
    else{ const w = box.querySelector(".step:not([data-n])"); if(w) w.remove();
      box.insertAdjacentHTML("beforeend", stepRow(ev.step, ev.n)); }
    box.scrollTop = box.scrollHeight;
  }
}
function applyEmpStep(ev){
  pushBoundedState(LEARN_LOGS,ev.idx,ev.step);
  const room = document.querySelector(`.room[data-room="${ev.idx}"] .live`);
  if(room) room.textContent = "📚 " + ev.step.l;
  const box = document.querySelector(`#learnlog-${ev.idx}`);
  if(box){ box.insertAdjacentHTML("beforeend", stepRow(ev.step)); box.scrollTop = box.scrollHeight; }
}

/* ---------- 员工状态计算 ---------- */
const EMP_ST = {work:["work","上班干活中"],await:["await","等老板拍板"],fail:["fail","出状况了"],
  intr:["fail","被老板打断"],learn:["learn","进修中"],idle:["idle","待命中"]};
function empState(idx){
  const e = EMP[idx];
  if(e && e.learning) return {st:"learn", label:"进修中", job:null};
  let best = null; // 优先级 work > await > intr > fail
  const rank = {running:4, awaiting_review:3, interrupted:2, failed:1};
  for(const j of (STATE.jobs||[])){
    if(["done","cancelled"].includes(j.status)) continue;
    const s = (j.stations||[])[idx] || (j.status==="running"&&j.current_idx===idx ? "running":"");
    if(rank[s] && (!best || rank[s]>rank[best.s])) best = {s, job:j};
  }
  if(!best) return {st:"idle", label:"待命中", job:null};
  const map = {running:["work","干活中"], awaiting_review:["await","等您拍板"],
    interrupted:["fail","被打断"], failed:["fail","已失败·点数退回"]};
  const [st,label] = map[best.s];
  return {st, label, job:best.job};
}
const PILL_CLS = {work:"work",await:"await",fail:"fail",learn:"learn",idle:"idle"};

/* ---------- 办公室(工作台) ---------- */
const MODE_LABEL = {fullauto:"完全托管",autopilot:"全自动",copilot:"关键审批",manual:"逐站审批"};
const ST_LABEL = {running:"进行中",awaiting_review:"待您审批",gate_blocked:"质检拦截",failed:"已失败",
  done:"已交付",cancelled:"已终止",paused:"已暂停"};
const RUN_LABEL = {queued:"排队中",running:"工作中…",awaiting_review:"待您审批",done:"已完成",
  failed:"执行失败",stale:"待重算",skipped:"已跳过",rejected:"重做中",interrupted:"被打断"};

function roomCard(s){
  const {st,label,job} = empState(s.idx);
  const e = EMP[s.idx]||{};
  const live = st==="learn" ? "📚 全网进修中…" : (st==="work" ? (LIVE_LINE[s.idx]||"正在赶工…") : "");
  const skillsN = isBoss() ? (e.skills||[]).filter(x=>x.enabled!==false).length : 0;
  const capsN = isBoss() ? (e.capabilities||[]).filter(c=>c.enabled).length : 0;
  return `<div class="room ${st==="work"?"working":""}" data-room="${s.idx}" onclick="openEmp(${s.idx})">
    <div class="roof" style="background:${s.color}"><span class="lamp"></span>${esc(s.dept)}<span style="flex:1"></span>${s.emoji}</div>
    ${job?`<span class="jobtag">工单#${job.id}</span>`:""}
    <div class="scene">${charSVG(s.color, s.emoji, st, 108)}</div>
    <div class="meta">
      <div class="nm"><span>${esc(s.name)}${e.is_custom?` <span title="老板改过提示词">📝</span>`:""}${capsN?` <span class="sub" style="font-weight:700" title="启用能力项">🧰${capsN}</span>`:""}${skillsN?` <span class="sub" style="font-weight:700" title="进修技能数">⚡${skillsN}</span>`:""}</span>
        <span class="stpill ${PILL_CLS[st]}">${label}</span></div>
      <div class="live">${esc(live)}</div>
      <div class="actions" style="margin-top:8px;gap:6px">
        <button class="btn sm" onclick="event.stopPropagation();openEmp(${s.idx})">🪪 ${isBoss()?"面板":"介绍"}</button>
        ${st==="work"&&job?`<button class="btn sm bad" onclick="event.stopPropagation();pauseJob(${job.id})">⏸ 打断</button>`:""}
        ${st==="await"&&job?`<button class="btn sm pri" onclick="event.stopPropagation();location.hash='#/job/${job.id}'">去拍板</button>`:""}
        ${st==="fail"&&job?`<button class="btn sm blue" onclick="event.stopPropagation();location.hash='#/job/${job.id}'">去处理</button>`:""}
      </div>
    </div>
  </div>`;
}
function gateRoom(){
  /* V24.2:质检员升级为「审查官」,正式编入内容生产部——流水线质检关卡 + 发前审查 +
     发草稿箱终审 + 发后数据复盘都是他的活;点房间进他的工作台 */
  const blocked = (STATE.jobs||[]).find(j=>j.status==="gate_blocked");
  const st = blocked?"await":"idle";
  return `<div class="room" data-room="gate" onclick="location.hash='#/censor'">
    <div class="roof" style="background:#ef476f;color:#fff"><span class="lamp"></span>合规审查部<span style="flex:1"></span>🛡️</div>
    ${blocked?`<span class="jobtag">工单#${blocked.id}</span>`:""}
    <div class="scene">${charSVG("#ef476f","🛡️",st,108)}</div>
    <div class="meta"><div class="nm"><span>审查官</span><span class="stpill ${blocked?"fail":"idle"}">${blocked?"拦下了内容":"铁面待命"}</span></div>
      <div class="live">${blocked?"有工单被审查拦截,去工单里处理":"发前审查 · 发布终审 · 发后数据复盘"}</div>
      <div class="actions" style="margin-top:8px;gap:6px">
        <button class="btn sm" onclick="event.stopPropagation();location.hash='#/censor'">🪪 工作台</button>
        ${blocked?`<button class="btn sm blue" onclick="event.stopPropagation();location.hash='#/job/${blocked.id}'">去处理</button>`
          :`<span class="sub" style="font-size:11px">平台固定岗位</span>`}
      </div></div>
  </div>`;
}
/* ---------- V27 首页聚焦:三大动作卡 + 部门楼层折叠(仅非 root 租户) ---------- */
function deptOpenKey(key){ return "deptopen_"+((ME&&ME.tenant)||"")+"_"+key; }
function deptIsOpen(key){
  const v = localStorage.getItem(deptOpenKey(key));
  // 无记录(新租户首访)时默认:内容生产部展开、其他楼层收起;手动开合过则完全尊重记录
  if(v===null) return key==="content";
  return v==="1";
}
function toggleDept(key){
  const k = deptOpenKey(key);
  // 显式记 "0"/"1":收起也落一笔,避免「内容生产部收起后因无记录又默认展开」
  localStorage.setItem(k, deptIsOpen(key)?"0":"1");
  render();
}
function goExperts(){
  const d = (DEPTS||[]).filter(x=>can(x.key))[0];
  if(!d){ toast("您的账号还没有开通产业专家板块,找企业主账号开通"); return; }
  localStorage.setItem(deptOpenKey(d.key),"1");
  render().then(()=>{ const el=document.querySelector(`[data-deptsec="${d.key}"]`);
    if(el) el.scrollIntoView({behavior:"smooth",block:"start"}); });
}
function todoCards(){
  const cards = [];
  if(canWork("content")) cards.push(["📝","发一单内容","图文/短视频脚本 → 整条流水线替您写","location.hash='#/new'"]);
  if((DEPTS||[]).some(d=>can(d.key))) cards.push(["🧑‍🔧","找专家问一件事","产业专家一对一,直接给 TA 派活","goExperts()"]);
  if(canWork("avatar")) cards.push(["🎬","出条视频","照片开口说话;不想烧额度也能图文一键成片","location.hash='#/avatar'"]);
  else if(canWork("content")) cards.push(["🎬","出条视频","把图文一键变竖版成片,自动配音+字幕","TS.tab='vars';location.hash='#/tools/vars'"]);
  if(!cards.length) return "";
  return `<h2 style="margin:20px 0 0;letter-spacing:1px">🧭 今天想干点啥?</h2>
  <div class="todo3">${cards.map(([e,t,d,act])=>`<div class="todocard" onclick="${act}">
    <span class="tc-emoji">${e}</span>
    <span class="tc-body"><span class="tc-t">${t}</span><span class="tc-d">${d}</span></span>
    <span class="tc-arrow">→</span></div>`).join("")}</div>`;
}
function specRoomCard(e){
  const st = e.running_n?"work":e.learning?"learn":"idle";
  return `<div class="room ${st==="work"?"working":""}" data-room="${e.idx}" onclick="openSpec(${e.idx})" style="${e.enabled===false?"opacity:.45;filter:grayscale(.8)":""}">
    <div class="roof" style="background:${e.color}"><span class="lamp"></span>${esc(e.person||e.name).slice(0,10)}<span style="flex:1"></span>${e.emoji}</div>
    <div class="scene">${charSVG(e.color, e.emoji, st, 108)}</div>
    <div class="meta">
      <div class="nm"><span>${e.enabled===false?"⛔":""}${esc(e.person||"")} <span class="sub" style="font-size:11px">${esc(e.name).slice(0,12)}</span>${isBoss()&&e.skills_n?` <span class="sub" style="font-weight:700" title="进修技能">⚡${e.skills_n}</span>`:""}</span>
        <span class="stpill ${PILL_CLS[st]}">${{work:"干活中",learn:"进修中",idle:"待命中"}[st]}</span></div>
      <div class="live">${esc(isBoss()?e.duty:e.intro).slice(0,38)}</div>
      <div class="actions" style="margin-top:8px;gap:6px">
        <button class="btn sm" onclick="event.stopPropagation();openSpec(${e.idx})">🪪 ${isBoss()?"面板":"介绍"}</button>
        ${ME&&ME.role!=="tour"?`<button class="btn sm pri" onclick="event.stopPropagation();openSpec(${e.idx})">📋 派活</button>`:""}
      </div>
    </div></div>`;
}
function deptFloorHtml(d){
  return `<div class="notice" style="margin-top:0">${d.emoji} <b>${esc(d.name)}</b> — ${esc(d.tagline||"")}。${ME&&ME.role==="tour"?"点任何员工查看公开文字介绍。":"点任何专家即可查看介绍或直接派活；产出自动进资产库。"}</div>`
    + (ME&&ME.role==="tour"?"":expFinderCard(d.key))
    + d.groups.map(g=>{
      const members = d.employees.filter(e=>e.group===g.name && (e.enabled!==false || isBoss()));
      return `<div class="grouphead"><span style="font-size:18px">${g.emoji}</span>${esc(g.name)}<span class="sub">(${members.length}人)</span><span class="gline"></span></div>
        <div class="floor">${members.map(specRoomCard).join("")}</div>`;
    }).join("");
}
function deptSection(key, emoji, name, count, tagline, innerFn){
  const open = deptIsOpen(key);
  return `<div class="deptsec" data-deptsec="${esc(key)}">
    <div class="depthead ${open?"open":""}" onclick="toggleDept(${cp(key)})">
      <span class="dh-emoji">${emoji}</span><span class="dh-name">${esc(name)}</span>
      <span class="dh-count">${esc(count)}</span><span class="dh-tag">${esc(tagline||"")}</span>
      <span class="dh-caret">${open?"▾":"▸"}</span></div>
    ${open?`<div class="deptbody">${innerFn()}</div>`:""}
  </div>`;
}
async function dashboard(){
  if(!DEPTS) DEPTS = await api("/depts");
  const jobsContract=normalizeListContract(
    await api(listPath("/state","jobs")),LIST_PAGE_SIZE);
  const inbox = STATE.inbox, jobs = jobsContract.items;
  const notifications = STATE.notifications||[];
  const active = (STATE.jobs||[])
    .filter(j=>!["done","cancelled","failed"].includes(j.status)).length;
  const specN = DEPTS.reduce((n,d)=>n+d.employees.length,0);
  const allowedDepts = DEPTS.filter(d=>can(d.key));
  const isRoot = isBoss();
  if(CUR_DEPT==="content"&&!can("content")) CUR_DEPT = allowedDepts[0]?.key||(can("avatar")?"__avatar":"content");
  const tourBanner = ME&&ME.role==="tour" ? `<div class="notice" style="background:#ece3ff;margin-top:0">👀 <b>参观模式</b>:点击员工卡片,了解每位数字员工可以为您的业务提供什么帮助。<a href="/promo#plans" style="text-decoration:underline;font-weight:900">查看套餐</a> 或联系开通企业账号后直接派活。</div>` : "";
  const heroCard = `
  <div class="card" style="background:linear-gradient(120deg,#fff6dc,#fffaf0 60%)">
    <div style="display:flex;align-items:center;gap:16px;flex-wrap:wrap">
      <div style="flex:1;min-width:240px">
        <h2 style="font-size:22px">👔 ${esc(META.app?.name||"老板的AI集团")} · ${ME.role==="member"?"同事好":"董事长好"}</h2>
        <div class="sub">${esc(META.app?.slogan||"")}。${11+specN} 位数字员工、${1+DEPTS.length} 个产业部门随时待命:内容部整条流水线+审查官把关出成品,行业专家一人一岗随叫随到。</div>
      </div>
      ${ME.role!=="tour"?`<a class="btn blue" href="#/tasks" style="font-size:15px">📋 我的任务在哪</a>`:""}
      ${canWork("content")?`<a class="btn pri" href="#/new" style="font-size:15px">➕ 下达新任务</a>`:""}
    </div>
    <div class="kv" style="margin-top:12px">${ME.role!=="root"?`<span><a href="#/billing" style="font-weight:800;text-decoration:underline">💎 余额 ${Math.round(STATE.balance||0)} 点${STATE.plan?` · ${esc(STATE.plan)}`:""}</a></span>`:""}<span>内容工单进行中 ${active} 单</span><span>内容工单累计 ${jobsContract.total??jobs.length} 单</span>
      <span>数字员工 ${11+specN} 人</span>
      ${isBoss()?`<span>技能储备 ${EMP.reduce((n,e)=>n+(e.skills||[]).length,0)} 条</span>`:""}</div>
  </div>`;
  const inboxCard = inbox.length?`<div class="card" style="background:#fff6dc"><h2>📥 等您拍板(${inbox.length})</h2>${inbox.map(jobRow).join("")}</div>`:"";
  const notificationCard = notifications.length?`<div class="card" style="background:#eef6ff">
    <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap"><h2 style="margin:0;flex:1">🔔 新进展(${notifications.length})</h2>
      ${isAdmin()?`<button class="btn sm" onclick="notificationReadAll(this)">全部已读</button>`:""}</div>
    ${notifications.map(n=>`<div class="topic" style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
      <div style="flex:1;min-width:190px"><b>${esc(n.title)}</b>${n.body?`<div class="sub">${esc(n.body)}</div>`:""}
        <div class="sub">${tcFmt(n.created_at)}</div></div>
      <button class="btn sm pri" onclick="notificationOpen(${n.id},${cp(safeRouteUrl(n.link)||"#/")})">查看</button></div>`).join("")}</div>`:"";
  const jobsCard = `<div class="card" style="margin-top:22px"><h2>📋 全部工单(${jobsContract.total??jobs.length})</h2>
    ${listContractNotice(jobsContract,"内容工单")}
    ${jobs.length?jobs.map(jobRow).join(""):`<div class="empty">${(jobsContract.total??0)===0?"还没有工单。点上面「发一单内容」,3 分钟看你的数字员工团队跑起来。":"这一页没有工单；返回上一页,或点上面「发一单内容」开工。"}</div>`}
    ${listPager(jobsContract,"jobs")}</div>`;
  let floorSection;
  if(isRoot){
    // root 平台视图:保持原「标签页切换 + 全展开楼层」现状
    const deptTabs = `<div class="depttabs">
      ${can("content")?`<span class="dt ${CUR_DEPT==="content"?"on":""}" onclick="CUR_DEPT='content';render()">🎬 内容生产部 <span class="sub">10人流水线</span></span>`:""}
      ${allowedDepts.map(d=>`<span class="dt ${CUR_DEPT===d.key?"on":""}" onclick="CUR_DEPT=${cp(d.key)};render()">${d.emoji} ${esc(d.name)} <span class="sub">${d.employees.length}人</span></span>`).join("")}
      ${can("avatar")?`<span class="dt" onclick="location.hash='#/avatar'">🎥 数字人摄影棚 <span class="sub">新</span></span>`:""}
    </div>`;
    let floor;
    if(CUR_DEPT==="content"&&can("content")){
      floor = `<div class="floor">${META.stations.map(roomCard).join("")}${gateRoom()}</div>`;
    }else{
      const d = DEPTS.find(x=>x.key===CUR_DEPT) || allowedDepts[0];
      floor = !d ? `<div class="empty">您的账号还没有开通任何板块,请联系企业主账号</div>` : deptFloorHtml(d);
    }
    floorSection = `<h2 style="margin:22px 0 0;letter-spacing:1px">🏢 集团楼层</h2>${deptTabs}${floor}
      ${CUR_DEPT==="content"?jobsCard:""}`;
  }else{
    // 租户视图:部门楼层默认折叠为标题行,点击展开;展开状态按租户+部门记忆
    const sections = [];
    if(can("content")) sections.push(deptSection("content","🎬","内容生产部",(META.stations.length+1)+"人",
      "选题→写稿→配图→审查,整条流水线出成品",
      ()=>`<div class="floor">${META.stations.map(roomCard).join("")}${gateRoom()}</div>`));
    allowedDepts.forEach(d=> sections.push(deptSection(d.key, d.emoji, d.name, d.employees.length+"人",
      d.tagline||"行业专家,一人一岗随叫随到", ()=>deptFloorHtml(d))));
    const avatarRow = can("avatar")?`<div class="deptsec"><div class="depthead" onclick="location.hash='#/avatar'">
      <span class="dh-emoji">🎥</span><span class="dh-name">数字人摄影棚</span><span class="dh-count">新</span>
      <span class="dh-tag">照片开口说话 · 出条口播视频</span><span class="dh-caret">→</span></div></div>`:"";
    floorSection = `<h2 style="margin:22px 0 4px;letter-spacing:1px">🏢 集团楼层 <span class="sub" style="font-weight:600;font-size:13px">点部门标题行展开/收起</span></h2>
      ${sections.join("")}${avatarRow}
      ${can("content")?jobsCard:""}`;
  }
  $("#main").innerHTML = tourBanner + heroCard
    + (isRoot?"":todoCards())
    + obCard()
    + trioCard()
    + notificationCard
    + inboxCard
    + floorSection;
}
async function notificationOpen(id,link){
  try{ await api("/notifications/read",{method:"POST",body:{ids:[id]}}); }
  catch(e){ reportClientError("notification_read",e); }
  if(STATE?.notifications) STATE.notifications=STATE.notifications.filter(n=>n.id!==id);
  SHELL_DIRTY=true;
  if(location.hash===link) render(); else location.hash=link;
}
async function notificationReadAll(btn){
  btn.disabled=true;
  try{
    await api("/notifications/read",{method:"POST",body:{}});
    STATE.notifications=[]; nav(); render();
  }catch(e){ toast(e.message); btn.disabled=false; }
}
/* V27.2:「发布三件套」向导——把 成片→审查→排版 串成一条可点的流程(对外主推卖点的上手版) */
function trioCard(){
  if(!ME || !["owner","root"].includes(ME.role) || !can("content")) return "";
  if(localStorage.getItem("trio_hide_"+(ME.tenant||""))) return "";
  const tr = STATE.trio||{};
  const steps = [
    {done:tr.video, e:"🎬", t:"① 图文一键成片", d:"挑一篇交付的图文,自动配音+字幕出竖版视频", act:"trioGo('video')"},
    {done:tr.censor, e:"🛡️", t:"② 审查官把关", d:"发前极速扫描广告法/违禁词,免费", act:"location.hash='#/censor'"},
    {done:tr.publish, e:"📰", t:"③ 公众号排版发出去", d:"12套主题排版,一键复制或发草稿箱", act:"trioGo('mp')"},
  ];
  const undone = steps.filter(x=>!x.done).length;
  if(!undone) return "";
  return `<div class="card" style="background:linear-gradient(120deg,#e8f0ff,#fffaf0 70%)">
    <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
      <h2 style="margin:0;flex:1;min-width:200px">🧰 发布三件套 · 一篇图文这样发出去(还差 ${undone} 步)</h2>
      <span class="sub" style="cursor:pointer;text-decoration:underline" onclick="localStorage.setItem('trio_hide_'+(ME.tenant||''),1);render()">不再提示</span></div>
    <div class="grid3" style="grid-template-columns:repeat(auto-fill,minmax(220px,1fr));margin-top:10px">
      ${steps.map(x=>`<div class="topic" style="margin:0;cursor:pointer;${x.done?"opacity:.55":""}" onclick="${x.act}">
        <b>${x.done?"✅":x.e} ${x.t}</b><div class="sub" style="margin-top:3px">${x.d}${x.done?"":" →"}</div></div>`).join("")}
    </div></div>`;
}
let MP_AUTO = null;   // trio 向导:进交付页后自动弹公众号排版(照 AV_OPEN_CLONE 的路子)
function trioGo(step){
  const j = (STATE.jobs||[]).find(x=>x.status==="done");
  if(!j){ toast("先发一单内容,交付后就能一键成片/排版"); location.hash="#/new"; return; }
  if(step==="mp") MP_AUTO = j.id;
  location.hash = "#/delivery/"+j.id;
}
/* 「更多」菜单里的「重看新手引导」:不是真页面,清掉两张引导卡的「不再提示」标记后回办公室。
   借用 routes 机制(菜单项统一是 #/xxx 链接);改 hash 后本次 render 会因 hash 变化自动中止,由 hashchange 重新渲染办公室 */
function guideReset(){
  localStorage.removeItem("ob_hide_"+((ME&&ME.tenant)||""));
  localStorage.removeItem("trio_hide_"+((ME&&ME.tenant)||""));
  // 四步都完成时引导卡默认不渲染;点了"重看"就强制展示一次完成态
  localStorage.setItem("ob_force_"+((ME&&ME.tenant)||""),"1");
  toast("新手引导已恢复,回到办公室即可重看");
  location.hash = "#/";
}
function obCard(){
  if(!ME || !["owner","root"].includes(ME.role)) return "";
  if(localStorage.getItem("ob_hide_"+(ME.tenant||""))) return "";
  const su = STATE.setup||{};
  const steps = [
    {done:su.profile, t:"① 建人设档案", d:"产出像您本人写的", h:"#/profiles"},
    {done:su.first_job, t:"② 发出第一单", d:"看流水线全程直播", h:"#/new", pts:`${META?.job_points??18} 点`},
    {done:su.wechat, t:"③ 打通微信", d:"通知/公众号草稿箱", h:"#/channels"},
    {done:su.clone, t:"④ 克隆您的声音", d:"视频都用您的原声", h:"#/avatar", pts:`${META?.voice_clone_points??9} 点`},
  ];
  const undone = steps.filter(x=>!x.done).length;
  const forced = localStorage.getItem("ob_force_"+(ME.tenant||""));
  if(!undone && !forced) return "";
  if(!undone && forced){
    localStorage.removeItem("ob_force_"+(ME.tenant||""));
    return `<div class="card" style="background:linear-gradient(120deg,#e7f6ec,#fffaf0 70%)">
      <h2 style="margin:0">🎉 开工四步已全部完成</h2>
      <div class="sub" style="margin-top:6px">配置齐了:人设、首单、微信通知、原声克隆都已就绪。日常从「➕ 下达新任务」或「⏰ 定时任务」开工即可。</div></div>`;
  }
  return `<div class="card" style="background:linear-gradient(120deg,#e8f7ee,#fffaf0 70%)">
    <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
      <h2 style="margin:0;flex:1;min-width:200px">🚀 开工四步(还差 ${undone} 步)</h2>
      <span class="sub" style="cursor:pointer;text-decoration:underline" onclick="localStorage.setItem('ob_hide_'+(ME.tenant||''),1);render()">不再提示</span></div>
    <div class="grid3" style="grid-template-columns:repeat(auto-fill,minmax(220px,1fr));margin-top:10px">
      ${steps.map(x=>`<a href="${x.h}" class="topic" style="margin:0;display:block;text-decoration:none;${x.done?"opacity:.55":""}">
        <b>${x.done?"✅":"⬜"} ${x.t}</b>${x.pts?` <span class="sub">${esc(x.pts)}</span>`:""}<div class="sub" style="margin-top:3px">${x.d}${x.done?"":" →"}</div></a>`).join("")}
    </div>
    <div class="sub" style="margin-top:8px">试用额度有限?②必做,其余可开通后再补,不影响先跑通第一单</div></div>`;
}
function jobRow(j){
  const stn = META.stations[j.current_idx];
  const dots = (j.stations||[]).map((st,i)=>{
    const s = META.stations[i];
    const eff = st || (i===j.current_idx && j.status==="running" ? "running" : "");
    return `<span class="dot ${eff}" title="工位${i+1} ${s?s.name:""}:${RUN_LABEL[eff]||"未开始"}"></span>`;
  }).join("");
  return `<div class="jobrow" onclick="location.hash='#/job/${j.id}'">
    <span style="font-size:22px">${stn?stn.emoji:"📦"}</span>
    <div class="t">#${j.id} ${esc(j.title!=="(未产出标题)"?j.title:j.brief.direction)}
      <div class="sub" style="font-weight:400">${esc(j.brief.template||"")} · ${esc(MODE_LABEL[j.mode]||j.mode)} · ${(j.brief.platforms||[]).map(esc).join("/")}</div></div>
    <div class="dots">${dots}</div>
    <span class="sub">工位${j.current_idx+1}/10 ${stn?stn.name:""}</span>
    <span class="pill ${j.status}">${ST_LABEL[j.status]||j.status}</span>
    ${isAdmin()?`<button class="btn sm" style="padding:2px 8px" title="移入回收站" onclick="event.stopPropagation();deleteJob(${j.id})">🗑</button>`:""}</div>`;
}
async function deleteJob(id){
  const j = (STATE.jobs||[]).find(x=>x.id===id);
  const activeMsg = j && !["done","cancelled","failed"].includes(j.status) ? "该工单还在进行中,删除会先停工。" : "";
  if(!await uiConfirm(`${activeMsg}把工单 #${id} 移入回收站?\n记录与交付物会保留，可从回收站恢复。`,{
    title:"移入回收站",confirmText:"移入回收站"
  })) return;
  try{
    await api(`/jobs/${id}`,{method:"DELETE"});
    toast("🗑 工单已移入回收站");
    if(location.hash.startsWith(`#/job/${id}`)||location.hash.startsWith(`#/delivery/${id}`)) location.hash="#/";
    else render();
  }catch(e){ toast("删除失败:"+e.message, true); }
}

/* ---------- V42:可恢复回收站 ---------- */
const TRASH_KIND_LABEL = {job:"内容工单",task:"数字员工任务",knowledge:"知识沉淀",avatar:"数字人任务",
  profile:"人设档案",asset:"资产"};
let TRASH_ITEMS=[];
async function trashView(offset=0){
  if(!isAdmin()){ $("#main").innerHTML='<div class="empty">需要企业主账号权限</div>'; return; }
  const data=await api(`/trash?limit=200&offset=${Math.max(0,Number(offset)||0)}`);
  if(offset){
    const seen=new Set(TRASH_ITEMS.map(item=>`${item.kind}:${item.id}`));
    TRASH_ITEMS.push(...(data.items||[]).filter(item=>!seen.has(`${item.kind}:${item.id}`)));
  }else TRASH_ITEMS=data.items||[];
  const rows=TRASH_ITEMS;
  $("#main").innerHTML=`<div class="card" style="background:linear-gradient(120deg,#f4edde,#fffaf0)">
    <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
      <div style="flex:1;min-width:220px"><h2 style="margin:0">🗑 回收站</h2>
        <div class="sub" style="margin-top:5px">误删的工单、员工任务、知识沉淀、资产、人设档案和数字人任务可在这里恢复；交付物不会在移入回收站时被销毁,内容<b>长期保留、暂无自动清理</b>。含敏感客户信息的记录可在此「⛔ 彻底删除」,连同交付文件一并销毁。</div></div>
      <button class="btn" onclick="trashView()">↻ 刷新</button></div>
    ${data.truncated?`<div class="notice">当前已展示 ${rows.length} 条，下面还能加载更早记录。</div>`:""}</div>
  <div class="card">${rows.length?rows.map(item=>`<div class="topic" style="display:flex;gap:12px;align-items:center;flex-wrap:wrap">
      <div style="font-size:23px">${{job:"📝",task:"🧑‍💼",knowledge:"📚",avatar:"🎥",profile:"🎭",asset:"🗂️"}[item.kind]||"🗂"}</div>
      <div style="flex:1;min-width:190px"><div><span class="tag">${esc(TRASH_KIND_LABEL[item.kind]||item.kind)}</span>
        <b>#${item.id} · ${esc(item.title)}</b></div>
        <div class="sub" style="margin-top:5px">${item.assignee?`👤 ${esc(item.assignee)} · `:""}移入时间 ${tcFmt(item.deleted_at)}
          ${item.reason?` · ${esc(item.reason)}`:""}</div></div>
      <button class="btn pri" onclick="trashRestore(${cp(item.kind)},${item.id},this)">↩️ 恢复</button>
      <button class="btn sm bad" onclick="trashPurge(${cp(item.kind)},${item.id},this)">⛔ 彻底删除</button>
    </div>`).join(""):'<div class="empty">回收站是空的</div>'}
    ${data.truncated?`<div class="actions"><button class="btn" onclick="trashView(${data.next_offset})">加载更早记录</button></div>`:""}</div>`;
}
async function trashRestore(kind,id,btn){
  btn.disabled=true; btn.innerHTML='<span class="spin"></span> 恢复中…';
  try{
    await api(`/trash/${encodeURIComponent(kind)}/${id}/restore`,{method:"POST"});
    toast("已恢复");
    SHELL_DIRTY=true;
    await trashView();
  }catch(e){
    toast(e.message);
    btn.disabled=false; btn.textContent="↩️ 恢复";
  }
}
async function trashPurge(kind,id,btn){
  if(!await uiConfirm(`彻底删除后不可恢复，交付文件会一并销毁。确定彻底删除 #${id}?`,{
    title:"彻底删除",confirmText:"彻底删除",danger:true})) return;
  if(!await uiConfirm(`最后确认:#${id} 将永久消失，无法找回。`,{
    title:"最后确认",confirmText:"永久删除",danger:true})) return;
  btn.disabled=true; btn.innerHTML='<span class="spin"></span> 删除中…';
  try{
    await api(`/trash/${encodeURIComponent(kind)}/${id}/purge`,{method:"POST"});
    toast("已彻底删除");
    SHELL_DIRTY=true;
    await trashView();
  }catch(e){
    toast(e.message);
    btn.disabled=false; btn.textContent="⛔ 彻底删除";
  }
}

/* ---------- V29:老板全局任务中心 ---------- */
let TC_DATA = null, TC_DATA_Q = "", TC_STATUS = "open", TC_KIND = "all", TC_QUERY = "", TC_LOADING_MORE = false;
const TC_KIND_LABEL = {expert:"数字员工任务",content:"内容工单",meeting:"AI会议",avatar:"数字人视频",
  video:"图文成片",tool:"营销工具",publish:"发布任务",wechat:"公众号草稿投递"};
function tcPill(group){ return group==="done"?"done":group==="failed"?"failed":
  group==="waiting"?"awaiting_review":group==="cancelled"?"cancelled":"running"; }
function tcFmt(ts){
  if(!ts) return "—";
  const d=new Date(ts*1000);
  // 跨年记录必须带年份,否则对账时「1/5」不知道是哪年的
  const opts={month:"numeric",day:"numeric",hour:"2-digit",minute:"2-digit"};
  if(d.getFullYear()!==new Date().getFullYear()) opts.year="numeric";
  return d.toLocaleString("zh-CN",opts);
}
async function tasksView(tid){
  if(tid){
    const ref=String(tid);
    if(ref.includes(":")) return taskCenterRecordView(ref);
    return taskDetailView(+ref);
  }
  TC_LOADING_MORE = false;
  TC_DATA = await api(`/task-center?limit=500&offset=0&q=${encodeURIComponent((TC_QUERY||"").trim())}`);
  TC_DATA_Q = (TC_QUERY||"").trim();
  if(TC_STATUS==="open" && !(TC_DATA.counts?.open)) TC_STATUS="all";
  taskCenterDraw();
}
function tcVisibleItems(){
  if(!TC_DATA) return [];
  const q = TC_QUERY.trim().toLowerCase();
  // 服务端已按当前关键词全局过滤时,客户端不再二次文本过滤——
  // 两套口径(服务端搜参数全文/客户端只搜标题)叠加会让命中数与列表互相矛盾
  const serverFiltered = (TC_DATA_Q||"").toLowerCase()===q;
  return TC_DATA.items.filter(x=>{
    const statusOk = TC_STATUS==="all" || (TC_STATUS==="open"
      ? ["active","waiting"].includes(x.status_group) : x.status_group===TC_STATUS);
    const kindOk = TC_KIND==="all" || x.kind===TC_KIND;
    const textOk = serverFiltered || !q || [x.title,x.assignee,x.source_label,x.kind_label,x.source_detail]
      .join(" ").toLowerCase().includes(q);
    return statusOk && kindOk && textOk;
  });
}
function tcStatusCard(key,emoji,label,n,color){
  return `<div class="topic" style="margin:0;background:${color};cursor:pointer;${TC_STATUS===key?"border-color:#111;box-shadow:4px 4px 0 #33291f55":""}" onclick="tcSetStatus(${cp(key)})">
    <div class="sub">${emoji} ${label}</div><div style="font-size:25px;font-weight:900;margin-top:3px">${n||0}</div></div>`;
}
function tcNewButton(){
  if(canWork("content")) return `<a class="btn pri" href="#/new">➕ 派一个任务</a>`;
  if(canWork("avatar")) return `<a class="btn pri" href="#/avatar">➕ 新建数字人任务</a>`;
  return `<a class="btn pri" href="#/">➕ 去办公室找员工派活</a>`;
}
function taskCenterDraw(){
  const d=TC_DATA||{counts:{},items:[],kind_counts:{}}, c=d.counts||{}, rows=tcVisibleItems();
  const kinds = Object.keys(TC_KIND_LABEL).filter(k=>(d.kind_counts||{})[k]);
  const hasMore = d.has_more===true || (d.has_more===undefined && d.truncated===true);
  $("#main").innerHTML = `<div class="card" style="background:linear-gradient(120deg,#fff2bd,#fffaf0 65%)">
    <div style="display:flex;gap:12px;align-items:center;flex-wrap:wrap">
      <div style="flex:1;min-width:240px"><h2 style="font-size:22px;margin:0">📋 任务中心</h2>
        <div class="sub" style="margin-top:5px">您派出去的活都在这里。先看谁正在干、卡在哪里，再点开任务或追到它的生成来源。</div></div>
      ${tcNewButton()}</div></div>
  <div class="grid3 tc-stats" style="grid-template-columns:repeat(auto-fit,minmax(145px,1fr));margin-bottom:18px">
    ${tcStatusCard("open","🔥","当前任务",c.open,"#fff1bd")}
    ${tcStatusCard("waiting","👔","等我处理",c.waiting,"#ffe1a8")}
    ${tcStatusCard("done","✅","已完成",c.done,"#dcf5e8")}
    ${tcStatusCard("failed","❌","失败",c.failed,"#ffe3e4")}
    ${tcStatusCard("all","📚","全部",c.all,"#eef3ff")}
  </div>
  <div class="card"><div class="row" style="align-items:end">
    <div style="flex:2"><label style="margin-top:0">搜任务</label><input id="tc-q" value="${esc(TC_QUERY)}" placeholder="搜任务主题/标题(全部历史)" oninput="tcSearch(this.value)"></div>
    <div><label style="margin-top:0">任务类型</label><select id="tc-kind" onchange="tcSetKind(this.value)">
      <option value="all">全部类型</option>${kinds.map(k=>`<option value="${k}" ${TC_KIND===k?"selected":""}>${TC_KIND_LABEL[k]} (${d.kind_counts[k]})</option>`).join("")}
    </select></div></div>
    <div style="display:flex;align-items:center;gap:8px;margin:15px 0 9px;flex-wrap:wrap">
      <b style="font-size:16px">${{open:"当前任务",waiting:"等我处理",done:"已完成",failed:"失败",all:"全部任务"}[TC_STATUS]||"任务"}</b>
      <span class="tag" id="tc-count">${rows.length} 条</span>${hasMore?`<span class="sub">已加载 ${d.items.length} / ${c.all||d.items.length} 条</span><button class="btn sm" onclick="tcLoadMore(this)">加载更早任务</button>`:""}
      <button class="btn sm" style="margin-left:auto" onclick="tasksView()">↻ 刷新</button></div>
    ${TC_QUERY.trim()?`<div class="notice" style="margin:0 0 9px">🔍 已按「${esc(TC_QUERY.trim())}」<b>全局搜索</b>全部历史记录,命中 ${c.all||0} 条${hasMore?",下方还有更多可加载":""}。</div>`:""}
    <div id="tc-list">${rows.length?rows.map(tcRow).join(""):`<div class="empty">${(c.all||0)===0?`还没有任务。<div class="actions" style="margin-top:10px;justify-content:center"><a class="btn sm pri" href="#/new">✍️ 发第一单内容</a><a class="btn sm" href="#/">🧑‍🔧 找行业专家派活</a></div>`:"这个筛选下没有任务。换个状态或关键词看看。"}</div>`}</div>
  </div>`;
}
function tcRow(x){
  return `<div class="topic" style="display:flex;gap:12px;align-items:flex-start;flex-wrap:wrap;margin-bottom:10px">
    <div style="width:42px;height:42px;border:2px solid var(--ink);border-radius:12px;background:#fff6dc;display:flex;align-items:center;justify-content:center;font-size:21px;flex:none">${x.emoji}</div>
    <div style="flex:1;min-width:180px">
      <div style="display:flex;gap:7px;align-items:center;flex-wrap:wrap"><span class="tag">${esc(x.kind_label)}</span>
        <span class="pill ${tcPill(x.status_group)}">${esc(x.status_label)}</span>
        <b style="font-size:15px">#${x.record_id} · ${esc(x.title)}</b></div>
      <div class="sub" style="margin-top:6px">👤 ${esc(x.assignee||"待分配")}${x.creator?`　·　✍️ 发起:${esc(x.creator)}`:""}　·　🕒 ${tcFmt(x.created_at)}</div>
      <div style="margin-top:5px"><span class="tag" style="background:#ece3ff">↳ 来源：${esc(x.source_label)}</span>
        ${x.source_detail?`<span class="sub">${esc(x.source_detail)}</span>`:""}</div>
      ${x.retry_block_reason?`<div class="sub" style="margin-top:6px;color:#9b3b35">⚠️ ${esc(x.retry_block_reason)}</div>`:""}</div>
    <div class="actions" style="margin:0;justify-content:flex-end">
      ${x.source_route&&x.source_route!==x.target_route?`<button class="btn sm" onclick="tcGo(${cp(x.key)},true)">看来源</button>`:""}
      ${x.retryable?`<button class="btn sm" onclick="tcRetry(${cp(x.kind)},${x.record_id},this)">🔁 免费重试</button>`:""}
      <button class="btn sm pri" onclick="tcGo(${cp(x.key)},false)">${x.target_exact===false?"去所在工作台":"打开任务"}</button></div></div>`;
}
function tcSetStatus(s){ TC_STATUS=s; taskCenterDraw(); }
function tcSetKind(k){ TC_KIND=k; taskCenterDraw(); }
let TC_SEARCH_TIMER=null;
function tcSearch(q){
  TC_QUERY=q;
  // 先在已加载数据里即时过滤(零延迟反馈)……
  const box=$("#tc-list"), rows=tcVisibleItems();
  if(box) box.innerHTML=rows.length?rows.map(tcRow).join(""):`<div class="empty">没有搜到相关任务</div>`;
  if($("#tc-count")) $("#tc-count").textContent=rows.length+" 条";
  // ……400ms 后真正去服务端全局搜(计数与分页同源,不再只搜已加载)
  clearTimeout(TC_SEARCH_TIMER);
  TC_SEARCH_TIMER=setTimeout(async()=>{
    try{
      const data=await api(`/task-center?limit=500&offset=0&q=${encodeURIComponent(TC_QUERY.trim())}`);
      if(location.hash.startsWith("#/tasks")&&TC_QUERY===q){ TC_DATA=data; TC_DATA_Q=TC_QUERY.trim(); taskCenterDraw(); }
    }catch(e){ if(e?.name!=="NavigationAbort") console.error(e); }
  },400);
}
async function tcLoadMore(btn){
  if(TC_LOADING_MORE||!TC_DATA) return;
  const offset=Number(TC_DATA.next_offset);
  if(!Number.isInteger(offset)||offset<0) return;
  TC_LOADING_MORE=true;
  if(btn){ btn.disabled=true; btn.textContent="加载中…"; }
  try{
    const page=await api(`/task-center?limit=500&offset=${offset}&q=${encodeURIComponent(TC_QUERY.trim())}`);
    const seen=new Set(TC_DATA.items.map(item=>item.key));
    const additions=(page.items||[]).filter(item=>!seen.has(item.key));
    const hasMore=page.has_more===true||(page.has_more===undefined&&page.truncated===true);
    TC_DATA={...TC_DATA,...page,items:[...TC_DATA.items,...additions],
      offset:0,next_offset:page.next_offset,has_more:hasMore,truncated:hasMore};
    taskCenterDraw();
  }catch(e){
    if(e?.name!=="NavigationAbort") toast(e.message||"更早任务加载失败");
    if(btn){ btn.disabled=false; btn.textContent="加载更早任务"; }
  }finally{
    TC_LOADING_MORE=false;
  }
}
function tcGo(key,source){
  const x=(TC_DATA?.items||[]).find(i=>i.key===key); if(!x) return;
  const route=source?x.source_route:x.target_route; if(!route) return;
  if(location.hash===route) render(); else location.hash=route;
}
async function tcRetry(kind,id,btn){
  if(btn){ btn.disabled=true; btn.innerHTML='<span class="spin"></span> 重新排队…'; }
  try{
    await api(`/task-center/${encodeURIComponent(kind)}/${id}/retry`,{method:"POST"});
    toast("已按原任务免费重试，不会再次扣点");
    SHELL_DIRTY=true;
    if(location.hash==="#/tasks") await tasksView();
    else location.hash="#/tasks";
  }catch(e){
    toast(e.message);
    if(btn){ btn.disabled=false; btn.textContent="🔁 免费重试"; }
  }
}
async function taskDetailView(tid){
  if(!Number.isInteger(tid)||tid<1){ $("#main").innerHTML=`<div class="empty">任务编号无效</div>`; return; }
  let t;
  try{ t=await api("/tasks/"+tid); }
  catch(e){
    if(e?.name==="NavigationAbort"||e?.status===403) throw e;
    if(e?.status!==404) throw e;
    $("#main").innerHTML=`<div class="card"><h2>这条任务已经不存在</h2><div class="sub">这条任务可能已被删除。</div><div class="actions"><a class="btn" href="#/tasks">← 返回任务中心</a></div></div>`; return; }
  const group=["queued","running"].includes(t.status)?"active":t.status==="done"?"done":"failed";
  const statusLabel={queued:"排队中",running:"进行中",done:"已交付",failed:"失败"}[t.status]||t.status;
  const sourceRoute=safeRouteUrl(t.source?.route);
  $("#main").innerHTML=`<div class="actions" style="margin:0 0 14px"><a class="btn sm" href="#/tasks">← 返回任务中心</a>
    ${sourceRoute?`<a class="btn sm blue" href="${esc(sourceRoute)}">↳ 查看生成来源</a>`:""}</div>
  <div class="card" style="background:#fffaf0">
    <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap"><h2 style="margin:0;flex:1">📋 任务 #${t.id}</h2>
      <span class="pill ${tcPill(group)}">${esc(statusLabel)}</span></div>
    <div style="font-size:18px;font-weight:900;margin-top:14px">${esc(t.brief?.direction||"未命名任务")}</div>
    <div class="kv"><span>👤 ${esc(t.emp_name||"数字员工")}</span><span>🏢 ${esc(t.dept_name||"")}</span>
      <span>发起人:${esc(t.created_by_name||"—")}</span>
      <span>🕒 ${tcFmt(t.created_at)}</span>${t.cost_usd?`<span>模型成本 ${rmb(t.cost_usd)}</span>`:""}</div>
    <div class="notice violet" style="margin-top:12px"><b>↳ 生成来源：${esc(t.source?.label||"直接派活")}</b>
      ${t.source?.detail?`<div class="sub" style="margin-top:3px">${esc(t.source.detail)}</div>`:""}</div>
    ${["queued","running"].includes(t.status)?`<h3>实时进度</h3><div class="steps" data-tasksteps="${t.id}">${(t.steps||[]).map((s,i)=>stepRow(s,i+1)).join("")||`<div class="step"><span class="ic">⏳</span><span class="lb">员工正在上线…</span></div>`}</div>`:""}
    ${t.status==="done"?`<div class="actions"><button class="btn sm" onclick="copyText(${cp(t.output_md||"")})">📋 复制</button>
      <a class="btn sm" href="/api/tasks/${t.id}/export.pdf">⬇️ PDF</a><a class="btn sm" href="/api/tasks/${t.id}/export.docx">⬇️ Word</a>
      <button class="btn sm" onclick="taskToKnow(${t.id})">📚 存入沉淀库</button></div>${taskBody(t)}`:""}
    ${t.status==="failed"?`<div class="notice red">${esc(t.output_md||"任务执行失败")}
      <div class="actions">${t.retryable?`<button class="btn sm pri" onclick="retryExpertTask(${t.id},this)">🔁 免费重试</button>
        <span class="sub">沿用原任务，不会再次扣点 · 还可重试 ${t.free_retries_remaining} 次</span>`
        :`<span class="sub">免费重试次数已用完，请重新派一个任务</span>`}</div></div>`:""}
  </div>`;
}
async function taskCenterRecordView(ref){
  const [kind,rawId]=ref.split(":"), rid=+rawId;
  if(!["avatar","video","tool","publish"].includes(kind)||!Number.isInteger(rid)||rid<1){
    $("#main").innerHTML=`<div class="empty">任务编号无效</div>`; return; }
  let t;
  try{ t=await api(`/task-center/${kind}/${rid}`); }
  catch(e){
    if(e?.name==="NavigationAbort") throw e;
    if(e?.status!==403&&e?.status!==404) throw e;
    $("#main").innerHTML=`<div class="card"><h2>这条任务已经不存在</h2><div class="sub">可能已被删除，或当前账号没有查看权限。</div><div class="actions"><a class="btn" href="#/tasks">← 返回任务中心</a></div></div>`; return; }
  const group=["queued","running"].includes(t.status)?"active":t.status==="done"?"done":t.status==="failed"?"failed":"waiting";
  const statusLabel={queued:"排队中",running:"进行中",done:"已完成",failed:"失败",cancelled:"已取消"}[t.status]||t.status;
  const detail=t.result||t.fail||null;
  const resultHtml=kind==="tool"&&t.tool_kind==="leads"&&t.result
    ?`<h3>可追溯线索</h3>${leadsHtml(t.result,t.params||{})}`
    :(detail?`<h3>任务结果</h3><pre style="white-space:pre-wrap;word-break:break-word;background:#f4edde;padding:12px;border-radius:12px">${esc(JSON.stringify(detail,null,2))}</pre>`:"");
  const workbenchRoute=kind==="tool"&&t.tool_kind
    ?`#/tools/${encodeURIComponent(t.tool_kind)}`:t.workbench_route;
  const sourceRoute=safeRouteUrl(t.source?.route), safeWorkbench=safeRouteUrl(workbenchRoute);
  const outputUrl=safeAssetUrl(t.output_url);
  $("#main").innerHTML=`<div class="actions" style="margin:0 0 14px"><a class="btn sm" href="#/tasks">← 返回任务中心</a>
    ${sourceRoute?`<a class="btn sm blue" href="${esc(sourceRoute)}">↳ 查看生成来源</a>`:""}
    ${safeWorkbench?`<a class="btn sm" href="${esc(safeWorkbench)}">去所在工作台</a>`:""}</div>
  <div class="card" style="background:#fffaf0"><div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
    <h2 style="margin:0;flex:1">${{avatar:"🎥",video:"🎬",tool:"🧰",publish:"📣"}[kind]} 任务 #${t.id}</h2>
    <span class="pill ${tcPill(group)}">${esc(statusLabel)}</span></div>
    <div style="font-size:18px;font-weight:900;margin-top:14px">${esc(t.title||"未命名任务")}</div>
    <div class="kv"><span>👤 ${esc(t.assignee||"")}</span><span>🕒 ${tcFmt(t.created_at)}</span></div>
    ${t.source?`<div class="notice violet"><b>↳ 生成来源：${esc(t.source.label)}</b></div>`:""}
    ${t.progress?`<div class="notice"><span class="${t.status==="running"?"spin":""}"></span> ${esc(t.progress)}</div>`:""}
    ${t.params&&Object.keys(t.params).length?`<details style="margin-top:12px"><summary style="cursor:pointer;font-weight:800">查看任务参数</summary><pre style="white-space:pre-wrap;word-break:break-word;background:#f4edde;padding:12px;border-radius:12px;margin-top:8px">${esc(JSON.stringify(t.params,null,2))}</pre></details>`:""}
    ${(t.steps||[]).length?`<h3>工作进度</h3><div class="steps">${t.steps.map((s,i)=>stepRow(s,i+1)).join("")}</div>`:""}
    ${outputUrl?`<div class="actions"><a class="btn pri" href="${esc(outputUrl)}" target="_blank" rel="noopener noreferrer">▶️ 打开交付物</a><a class="btn" href="${esc(outputUrl)}" download>⬇️ 下载</a></div>`:""}
    ${resultHtml}
    ${t.log?`<h3>发布日志</h3><div class="steps" style="white-space:pre-wrap">${esc(t.log)}</div>`:""}
    ${t.error?`<div class="notice red">${esc(t.error)}</div>`:""}
    ${t.status==="failed"?`<div class="actions">${t.retryable
      ?`<button class="btn pri" onclick="tcRetry(${cp(kind)},${t.id},this)">🔁 免费重试</button>
        <span class="sub">沿用原任务，不会再次扣点 · 还可重试 ${t.free_retries_remaining} 次</span>`
      :`<span class="sub">${esc(t.retry_block_reason||"当前任务不能安全原单重试，请回工作台处理")}</span>`}</div>`:""}
  </div>`;
}

async function retryExpertTask(id,btn){
  if(btn){ btn.disabled=true; btn.innerHTML='<span class="spin"></span> 重新排队…'; }
  try{
    await api(`/tasks/${id}/retry`,{method:"POST"});
    toast("已免费重试，任务重新排队");
    SHELL_DIRTY=true;
    if(SPEC_TASK&&SPEC_TASK.id===id) await specOpenTask(id,true);
    else if(SOLO_TASK&&SOLO_TASK.id===id){
      SOLO_TASK=await api(`/tasks/${id}`,{routeScoped:false}); drawModal(); soloWatch(id);
    }else await render();
  }catch(e){
    toast(e.message);
    if(btn){ btn.disabled=false; btn.textContent="🔁 免费重试"; }
  }
}

async function retryAvatarJob(id,btn){
  if(btn){ btn.disabled=true; btn.innerHTML='<span class="spin"></span> 重新排队…'; }
  try{
    await api(`/avatar/jobs/${id}/retry`,{method:"POST"});
    toast("已免费重试，数字人任务重新排队");
    SHELL_DIRTY=true;
    await render();
  }catch(e){
    toast(e.message);
    if(btn){ btn.disabled=false; btn.textContent="🔁 免费重试"; }
  }
}

/* ---------- 员工面板(模态) ---------- */
function openEmp(idx){ MODAL_IDX = idx; MODAL_TAB = isBoss()?"caps":"intro"; drawModal(); }
function closeModal(){ stopSoloWatch(); MODAL_IDX = null; $("#modal").innerHTML = ""; }
function drawModal(){
  const idx = MODAL_IDX; if(idx===null) return;
  if(MODAL_TAB!=="solo") stopSoloWatch();
  const s = META.stations[idx], e = EMP[idx];
  const {st,label} = empState(idx);
  const stats = e.stats||{};
  const hasCfg = isBoss() && ["trend","research","benchmark"].includes(s.key);
  const tabs = isBoss()
    ? [["solo","📋 单独派活"],["caps","🧰 能力"],["method","🔬 工作方式"],["skills","⚡ 技能库"],
       ...(hasCfg?[["config","⚙️ 工作配置"]]:[]),["info","🪪 岗位档案"]]
    : [...(ME&&ME.role!=="tour"?[["solo","📋 单独派活"]]:[]),["intro","🪪 员工介绍"]];
  let body = "";
  if(MODAL_TAB==="solo") body = soloTab(s,e);
  else if(MODAL_TAB==="caps") body = capsTab(s,e);
  else if(MODAL_TAB==="method") body = methodTab(s,e);
  else if(MODAL_TAB==="skills") body = skillsTab(s,e);
  else if(MODAL_TAB==="config"&&hasCfg) body = configTab(s,e);
  else if(MODAL_TAB==="info"&&isBoss()) body = infoTab(s,e);
  else body = employeeIntroTab(s,e);
  $("#modal").innerHTML = `<div class="overlay" onclick="if(event.target===this)closeModal()">
    <div class="panel">
      <div class="phead" style="background:linear-gradient(120deg,${s.color}44,transparent)">
        ${charSVG(s.color, s.emoji, st, 88)}
        <div style="flex:1">
          <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
            <b style="font-size:19px">${esc(s.name)}</b>
            <span class="tag">${esc(s.dept)}</span>
            <span class="stpill ${PILL_CLS[st]}">${label}</span></div>
          <div class="sub" style="margin-top:5px">${esc(isBoss()?s.duty:(e.intro||s.intro))}</div>
          ${isBoss()?`<div class="kv" style="margin-top:6px"><span>出勤 ${stats.runs||0} 次</span>
            <span title="TA历次干活的累计成本,不是点击收费">历史成本 ${rmb(stats.cost_usd)}</span>
            <span>平均用时 ${Math.round((stats.avg_ms||0)/1000)}s</span></div>`:""}
        </div>
        <button class="btn sm" onclick="closeModal()">✕</button>
      </div>
      <div class="pbody">
        <div class="tabs">${tabs.map(([k,l])=>`<span class="tb ${MODAL_TAB===k?"on":""}" onclick="MODAL_TAB=${cp(k)};drawModal()">${l}</span>`).join("")}</div>
        ${body}
      </div>
    </div></div>`;
  const box = $(`#learnlog-${idx}`); if(box) box.scrollTop = box.scrollHeight;
}
function employeeIntroTab(s,e){
  return `<div class="card" style="background:linear-gradient(120deg,#fff6dc,#fffaf0);margin-top:0">
    <h2 style="margin-top:0">🪪 ${esc(s.name)}</h2>
    <div style="font-size:16px;line-height:1.9">${esc(e.intro||s.intro||"这是一位随时待命的数字员工。")}</div>
  </div>`;
}
/* ---------- V27:老板速览 + 全文折叠(专家/单独派活共用) ---------- */
const LEN_OPTS = `<label>篇幅</label>
  <select id="LENID">
    <option value="lite">精简 · 只要结论和动作(≈800字)</option>
    <option value="std" selected>标准 · 重点突出(≈2000字,推荐)</option>
    <option value="full">详尽 · 完整展开(不限字数)</option>
  </select>`;
function lenOpts(id){ return LEN_OPTS.replace("LENID", id); }
function lenVal(id){ return ($("#"+id)||{}).value || "std"; }
const TB_STATE = {};  // task_id -> 用户手动展开/收起
const TB_LIVE = {};   // task_id -> 本页曾以全文形态见过(速览晚到时不许把正在读的正文折走)
function taskBody(t){
  const fullMd = `<div class="md" style="margin-top:10px">${md(t.output_md||"")}</div>`;
  if(!(t.summary_md||"").trim()){ setBoundedState(TB_LIVE,t.id,true); return fullMd; }
  const open = (t.id in TB_STATE) ? TB_STATE[t.id] : !!TB_LIVE[t.id];
  return `<div style="margin-top:10px;background:linear-gradient(120deg,#fff3d6,#ffe7c0);border:2.5px solid var(--ink);border-radius:13px;padding:12px 14px;box-shadow:3px 3px 0 #ffd16699">
      <div style="font-weight:900;font-size:15px;margin-bottom:4px">⚡ 老板速览</div>
      <div class="md">${md(t.summary_md||"")}</div></div>
    <div style="margin-top:10px">
      <button class="btn sm" onclick="tbToggle(${t.id},this)">${open?"📖 收起全文":"📖 展开全文"}</button>
      <div class="md" id="tb-full-${t.id}" style="display:${open?"block":"none"};margin-top:10px">${md(t.output_md||"")}</div></div>`;
}
function tbToggle(id,btn){
  const el = $("#tb-full-"+id); if(!el) return;
  const open = el.style.display==="none";
  el.style.display = open?"block":"none";
  btn.textContent = open?"📖 收起全文":"📖 展开全文";
  setBoundedState(TB_STATE,id,open);
}

/* ---------- V7:内容部员工单独派活 ---------- */
let SOLO_TASK = null, SOLO_TIMER = null, SOLO_WATCH_SEQ = 0;
function stopSoloWatch(){
  SOLO_WATCH_SEQ++;
  if(SOLO_TIMER!==null) clearTimeout(SOLO_TIMER);
  SOLO_TIMER=null;
}
function soloTab(s,e){
  const t = SOLO_TASK;
  let cur = "";
  if(t){
    const stL = {queued:"排队中",running:"⚙️ 干活中…",done:"✅ 已交付",failed:"❌ 失败"}[t.status]||t.status;
    cur = `<div class="card" style="background:#fffaf0;margin-top:12px">
      <div style="display:flex;gap:8px;align-items:center"><b style="flex:1">单人任务 #${t.id} ${stL}</b>
        ${t.status==="done"?`<button class="btn sm" onclick="copyText(${cp(t.output_md||"")})">📋 复制</button>
          <button class="btn sm" onclick="taskEdit(${t.id})">✏️ 编辑</button>
          <a class="btn sm" href="/api/tasks/${t.id}/export.pdf">⬇️ PDF</a>
          <a class="btn sm" href="/api/tasks/${t.id}/export.docx">⬇️ Word</a>
          <button class="btn sm" onclick="taskToKnow(${t.id})">📚 存沉淀</button>`:""}
        <button class="btn sm" onclick="stopSoloWatch();SOLO_TASK=null;drawModal()">收起</button></div>
      ${["queued","running"].includes(t.status)?`<div class="steps" data-tasksteps="${t.id}" style="margin-top:8px">${(t.steps||[]).map((x,i)=>stepRow(x,i+1)).join("")||`<div class="step"><span class="ic">⏳</span><span class="lb">员工上线中…</span></div>`}</div>`:""}
      ${t.status==="done"?taskBody(t):""}
      ${t.status==="failed"?`<div class="notice red">${esc(t.output_md||"失败")}
        <div class="actions">${t.retryable?`<button class="btn sm pri" onclick="retryExpertTask(${t.id},this)">🔁 免费重试</button>
          <span class="sub">不再次扣点 · 还可重试 ${t.free_retries_remaining} 次</span>`
          :`<span class="sub">免费重试次数已用完</span>`}</div></div>`:""}</div>`;
  }
  return `<div class="notice" style="margin-top:0">📋 <b>不走流水线,单独用 ${esc(s.name)}</b>:一句话派活,TA 独立交付(自动进资产库)。适合只要一个环节的活,比如只让${esc(s.name)}${{"趋势官":"找 5 个选题","情报员":"查一手资料","撰稿人":"写篇稿子","封面师":"出张封面文案"}[s.name]||"干本职的活"}。</div>
  <label>任务内容 *</label>
  <textarea id="solo-dir" placeholder="例:围绕[主题],按你的岗位职责给我一份专业交付"></textarea>
  <div class="row">
    <div><label>行业(选填)</label><input id="solo-ind" placeholder="如:餐饮"></div>
    <div><label>材料(选填,可传文件/图片/PDF)</label>
      <input type="file" multiple accept=".txt,.md,.csv,.json,.pdf,.jpg,.jpeg,.png,.webp" onchange="parseFileInto(this,'#solo-mat')" style="margin-bottom:6px">
      <textarea id="solo-mat" style="min-height:60px" placeholder="粘贴相关素材或传文件"></textarea></div>
  </div>
  ${lenOpts("solo-len")}
  <div class="actions"><button class="btn pri" onclick="soloSubmit(${s.idx},this)">🚀 派活(1点)</button></div>
  ${cur}`;
}
async function soloSubmit(idx, btn){
  const dir = $("#solo-dir").value.trim();
  if(!dir) return toast("任务内容必填");
  btn.disabled=true; btn.innerHTML=`<span class="spin"></span> 下发中…`;
  try{
    const r = await api("/tasks",{method:"POST",body:{emp_idx:idx, brief:{direction:dir,
      industry:$("#solo-ind").value.trim(), material:$("#solo-mat").value.trim(), length:lenVal("solo-len")}}});
    toast("已派活");
    soloWatch(r.task_id);
  }catch(e){ toast(e.message); btn.disabled=false; btn.textContent="🚀 派活"; }
}
function soloWatch(tid){
  stopSoloWatch();
  const watchSeq=SOLO_WATCH_SEQ;
  const tick = async ()=>{
    try{
      SOLO_TASK = await api("/tasks/"+tid,{routeScoped:false});
    }catch(e){
      if(watchSeq===SOLO_WATCH_SEQ) stopSoloWatch();
      if(e?.name!=="NavigationAbort"){
        reportClientError("solo_poll",e);
        if(MODAL_IDX!==null) toast("任务进度暂时没刷新，稍后重新打开即可");
      }
      return;
    }
    if(watchSeq!==SOLO_WATCH_SEQ) return;
    if(MODAL_IDX!==null && MODAL_TAB==="solo") drawModal();
    if(!["queued","running"].includes(SOLO_TASK.status)){ stopSoloWatch(); return; }
    if(MODAL_IDX===null||MODAL_TAB!=="solo"){ stopSoloWatch(); return; }
    SOLO_TIMER=setTimeout(tick, 12000);
  };
  tick();
}
window.addEventListener("pagehide",()=>stopSoloWatch());

/* ---------- V4:多项能力 ---------- */
function capsTab(s,e){
  const caps = e.capabilities||[];
  const onN = caps.filter(c=>c.enabled).length;
  return `
  <div class="notice" style="margin-top:0">🧰 <b>多项工作能力</b>:${esc(s.name)} 的 ${caps.length} 项硬能力(启用 ${onN}),开工时逐项运用、缺一不可。不需要的可以关掉(比如不想要学术溯源就关它)。</div>
  ${caps.map((c,i)=>`<div class="skillcard ${c.enabled?"":"off"}">
    <span class="switch ${c.enabled?"on":""}" title="启用/停用" onclick="toggleCap(${s.idx},${i})"></span>
    <span style="font-size:22px;width:30px;text-align:center">${c.emoji}</span>
    <div style="flex:1"><div class="t">${esc(c.name)}</div><div class="sub">${esc(c.desc)}</div></div>
  </div>`).join("")}
  <div class="sub" style="margin-top:10px">💡 这里是出厂硬能力;「⚡ 技能库」里还能通过全网进修持续学新招,两者都会用在 TA 的每次工作里。</div>`;
}
async function toggleCap(idx,i){
  const caps = [...(EMP[idx].capabilities||[])];
  caps[i] = {...caps[i], enabled: !caps[i].enabled};
  const off = caps.filter(c=>!c.enabled).map(c=>c.name);
  try{ await api(`/employees/${idx}/capabilities`,{method:"PUT",body:{caps_off:off}}); await render(); }
  catch(e){ toast(e.message); }
}

/* ---------- V4:工作方式可视化 ---------- */
function methodTab(s,e){
  const caps = (e.capabilities||[]).filter(c=>c.enabled);
  const skills = (e.skills||[]).filter(k=>k.enabled!==false);
  const prev = META.stations[s.idx-1], next = META.stations[s.idx+1];
  const appr = {pick:"产出多个候选 → <b>老板挑选</b>后放行",review:"产出后 → <b>老板审批</b>后放行",
    auto:"质量自查后<b>自动放行</b>",force:"<b>强制终审</b>:任何模式都等老板拍板"}[s.approval]||s.approval;
  const {st} = empState(s.idx);
  const liveLine = st==="work" ? (LIVE_LINE[s.idx]||"正在赶工…") : "";
  return `
  <div class="flow">
    <div class="fnode"><b>📥 接单输入</b><div class="sub" style="margin-top:3px">
      ${prev?`来自工位${s.idx}「${esc(prev.name)}」的产出`:"老板的 Brief(内容方向/平台/人设)"} + 老板 Brief + 人设档案 + 公司知识沉淀</div></div>
    <div class="farrow">⬇️</div>
    <div class="fnode" style="border-color:${s.color}"><b>🧰 逐项运用 ${caps.length} 项能力</b>
      <div class="capgrid">${caps.map(c=>`<span class="chip on" title="${esc(c.desc)}">${c.emoji} ${esc(c.name)}</span>`).join("")||`<span class="sub">全部能力被停用了!</span>`}</div>
      ${skills.length?`<div class="sub" style="margin-top:6px">+ ${skills.length} 条进修技能加持:${skills.slice(0,3).map(k=>esc(k.title)).join("、")}${skills.length>3?"…":""}</div>`:""}
      ${liveLine?`<div class="notice green" style="margin-top:8px">🔴 现在正在:${esc(liveLine)}</div>`:""}</div>
    <div class="farrow">⬇️</div>
    <div class="fnode"><b>📤 产出</b><div class="sub" style="margin-top:3px">${esc(s.duty)}(用模型 ${esc(s.model)})</div></div>
    <div class="farrow">⬇️</div>
    <div class="fnode"><b>🚦 放行方式</b><div class="sub" style="margin-top:3px">${appr}</div></div>
    ${s.idx===7?`<div class="farrow">⬇️</div><div class="fnode" style="border-color:#ef476f"><b>🛡️ 质检关卡</b><div class="sub" style="margin-top:3px">发布前自动质检,高风险内容会被拦下等老板定夺</div></div>`:""}
    <div class="farrow">⬇️</div>
    <div class="fnode" style="opacity:.85"><b>➡️ 交棒</b><div class="sub" style="margin-top:3px">${next?`交给工位${next.idx+1}「${esc(next.name)}」继续`:"全部完成,进交付包,复盘经验自动写入沉淀库"}</div></div>
  </div>
  <div class="sub" style="margin-top:10px">工作时的每一步(检索/读网页/写了多少字)都会实时打在办公室房间和工单页的工作日志里。</div>`;
}

function skillsTab(s,e){
  const skills = e.skills||[];
  const logs = LEARN_LOGS[s.idx]||[];
  return `
  <div class="notice violet" style="margin-top:0">📚 <b>全网进修</b>:员工联网检索本岗位最新方法论、平台规则、爆款技巧,学成技能卡。启用中的技能会自动用于 TA 之后的每一次工作。</div>
  <div class="actions" style="margin-top:10px">
    ${ME.role!=="root"?"":`<button class="btn pri" id="learnBtn" ${e.learning?"disabled":""} onclick="startLearn(${s.idx})">${e.learning?`<span class="spin"></span> 进修中…`:"🎓 送去全网进修(3点)"}</button>`}
    <span class="sub">${e.learned_at?`上次进修:${new Date(e.learned_at*1000).toLocaleString("zh-CN")}`:"还没进修过"}</span></div>
  ${(e.learning||logs.length)?`<div class="steps" id="learnlog-${s.idx}" style="margin-top:10px">${logs.map(x=>stepRow(x)).join("")}</div>`:""}
  <h3>已掌握技能(${skills.length})</h3>
  ${skills.length? skills.map((k,i)=>`<div class="skillcard ${k.enabled===false?"off":""}">
    <span class="switch ${k.enabled!==false?"on":""}" title="启用/停用" onclick="toggleSkill(${s.idx},${i})"></span>
    <div style="flex:1"><div class="t">${esc(k.title)}</div><div class="sub">${esc(k.detail)}</div>
      ${(k.source||k.learned_at)?`<div class="src">${k.source?`来源:${esc(k.source)}`:""}${k.source&&k.learned_at?" · ":""}${k.learned_at?new Date(k.learned_at*1000).toLocaleDateString("zh-CN"):""}</div>`:""}</div>
    <button class="btn sm bad" onclick="delSkill(${s.idx},${i})">🗑</button>
  </div>`).join("") : `<div class="empty">还没有技能,点「送去全网进修」收集第一批</div>`}`;
}
/* ---------- V3:工作配置(渠道矩阵 / 拆解师自定义) ---------- */
function configTab(s,e){
  const st = e.settings||{};
  if(s.key==="benchmark"){
    return `
    <div class="notice" style="margin-top:0">🧩 <b>拆解师自定义</b>:指定 TA 优先拆解谁、按什么维度拆。留空则自行检索同题爆款。</div>
    <label>对标账号 / 爆款链接(每行一个,如「小红书@某某」或文章链接)</label>
    <textarea id="cfg-targets" style="min-height:110px" placeholder="小红书@深夜徐老师\nhttps://mp.weixin.qq.com/s/xxxx\n抖音@某某的爆款视频标题">${esc((st.targets||[]).join("\n"))}</textarea>
    <label>拆解维度(每个对标都按这些维度拆透)</label>
    <div class="chips" id="cfg-dims">${(META.default_dimensions.concat((st.dimensions||[]).filter(d=>!META.default_dimensions.includes(d)))).map(d=>
      `<span class="chip${(st.dimensions||META.default_dimensions).includes(d)?" on":""}" onclick="this.classList.toggle('on')">${esc(d)}</span>`).join("")}</div>
    <input id="cfg-dim-new" placeholder="自定义维度,回车添加" onkeydown="if(event.key==='Enter'){addDim(this.value.trim());this.value=''}" style="margin-top:6px">
    <div class="actions">
      <button class="btn pri" onclick="saveConfig(${s.idx},'benchmark')">💾 保存生效</button>
      ${e.settings_custom?`<button class="btn" onclick="resetConfig(${s.idx})">↩️ 恢复默认</button><span class="tag">当前:老板自定义版</span>`:`<span class="tag">当前:出厂默认版</span>`}
    </div>`;
  }
  const catalog = META.channel_catalog[s.key]||[];
  const on = st.channels||[];
  return `
  <div class="notice" style="margin-top:0">📡 <b>检索渠道矩阵</b>:勾选 ${esc(s.name)} 开工时必须覆盖的渠道,越多越全面(也越慢/贵)。TA 会逐渠道检索并按渠道汇报信号。</div>
  <div class="chips" id="cfg-channels" style="margin-top:10px">${catalog.map(c=>
    `<span class="chip${on.includes(c)?" on":""}" onclick="this.classList.toggle('on')">${esc(c)}</span>`).join("")}</div>
  <div class="actions" style="margin-top:12px">
    <button class="btn pri" onclick="saveConfig(${s.idx},'channels')">💾 保存生效</button>
    ${e.settings_custom?`<button class="btn" onclick="resetConfig(${s.idx})">↩️ 恢复默认</button><span class="tag">当前:老板自定义版</span>`:`<span class="tag">当前:出厂默认(${META.default_channels[s.key].length} 渠道)</span>`}
  </div>`;
}
function addDim(v){
  if(!v) return;
  $("#cfg-dims").insertAdjacentHTML("beforeend",`<span class="chip on" onclick="this.classList.toggle('on')">${esc(v)}</span>`);
}
async function saveConfig(idx, kind){
  let settings;
  if(kind==="channels"){
    settings = {channels:[...document.querySelectorAll("#cfg-channels .on")].map(e=>e.textContent)};
    if(!settings.channels.length) return toast("至少保留一个渠道");
  }else{
    settings = {targets:$("#cfg-targets").value.split("\n").map(x=>x.trim()).filter(Boolean),
      dimensions:[...document.querySelectorAll("#cfg-dims .on")].map(e=>e.textContent)};
    if(!settings.dimensions.length) return toast("至少保留一个拆解维度");
  }
  try{ await api(`/employees/${idx}/settings`,{method:"PUT",body:{settings}});
    toast("工作配置已保存,下次开工生效"); await render();
  }catch(e){ toast(e.message); }
}
async function resetConfig(idx){
  if(!await uiConfirm("恢复出厂默认配置?",{title:"恢复配置",confirmText:"恢复默认"})) return;
  try{ await api(`/employees/${idx}/settings`,{method:"PUT",body:{settings:{}}});
    toast("已恢复默认"); await render(); }catch(e){ toast(e.message); }
}

function promptTab(s,e){
  const tpl = e.prompt_template || e.default_template;
  const phs = Object.entries(e.placeholders||{});
  return `
  <div class="notice" style="margin-top:0">📝 这是 <b>${esc(s.name)}</b> 的职责提示词。老板可直接修改(比如加要求、改口味、换产出格式)。
    <b>蓝黑色 {占位符} 会在开工时自动填入实际内容,别删</b>;JSON 结构决定产出格式,改动需谨慎。</div>
  <div style="margin:8px 0 10px">${phs.map(([k,v])=>`<span class="phchip" title="${esc(v)}" onclick="insertPh(${cp(k)})">{${k}}</span><span class="sub" style="margin-right:10px">${esc(v)}</span>`).join("")}</div>
  <textarea id="prompt-ta" class="promptbox">${esc(tpl)}</textarea>
  <div class="actions">
    <button class="btn pri" onclick="savePrompt(${s.idx})">💾 保存生效</button>
    ${e.is_custom?`<button class="btn" onclick="resetPrompt(${s.idx})">↩️ 恢复出厂默认</button><span class="tag">当前:老板自定义版</span>`:`<span class="tag">当前:出厂默认版</span>`}
  </div>`;
}
function infoTab(s,e){
  const stats = e.stats||{};
  return `<div class="topic"><div class="t">🪪 岗位职责</div><div class="sub" style="margin-top:4px">${esc(s.duty)}</div></div>
  <div class="topic"><div class="t">🧩 对标开源能力</div><div class="sub" style="margin-top:4px">Skill:${esc(s.skill)}</div></div>
  <div class="topic"><div class="t">🤖 用的模型</div><div class="sub" style="margin-top:4px">${esc(s.model)}</div></div>
  <div class="topic"><div class="t">🚦 审批方式</div><div class="sub" style="margin-top:4px">${{pick:"产出候选,老板挑选",review:"产出后请老板审批",auto:"自动放行(全自动/关键审批模式下)",force:"强制终审,任何模式都等老板"}[s.approval]||s.approval}</div></div>
  <div class="topic"><div class="t">📈 出勤统计</div><div class="sub" style="margin-top:4px">完成 ${stats.runs||0} 次工作 · 累计花费 ${rmb(stats.cost_usd)} · ${((stats.tokens||0)/1000).toFixed(1)}k tokens · 平均用时 ${Math.round((stats.avg_ms||0)/1000)}s</div></div>`;
}
function insertPh(k){
  const ta = $("#prompt-ta"); if(!ta) return;
  const p = ta.selectionStart||0;
  ta.value = ta.value.slice(0,p)+"{"+k+"}"+ta.value.slice(ta.selectionEnd||p);
  ta.focus();
}
async function savePrompt(idx){
  try{
    const v = $("#prompt-ta").value;
    const e = EMP[idx];
    const body = (v.trim()===e.default_template.trim()) ? {template:""} : {template:v};
    await api(`/employees/${idx}/prompt`,{method:"PUT",body});
    toast("提示词已保存,下次开工生效"); await render();
  }catch(e){ toast(e.message); }
}
async function resetPrompt(idx){
  if(!await uiConfirm("恢复出厂默认提示词?您的修改会丢失。",{
    title:"恢复提示词",confirmText:"恢复默认"
  })) return;
  try{ await api(`/employees/${idx}/prompt`,{method:"PUT",body:{template:""}}); toast("已恢复默认"); await render(); }
  catch(e){ toast(e.message); }
}
async function toggleSkill(idx,i){
  const skills = [...(EMP[idx].skills||[])];
  skills[i] = {...skills[i], enabled: skills[i].enabled===false};
  try{ await api(`/employees/${idx}/skills`,{method:"PUT",body:{skills}}); await render(); }
  catch(e){ toast(e.message); }
}
async function delSkill(idx,i){
  if(!await uiConfirm(`删除技能「${EMP[idx].skills[i].title}」?`,{
    title:"删除技能",confirmText:"删除"
  })) return;
  const skills = EMP[idx].skills.filter((_,n)=>n!==i);
  try{ await api(`/employees/${idx}/skills`,{method:"PUT",body:{skills}}); toast("已删除"); await render(); }
  catch(e){ toast(e.message); }
}
async function startLearn(idx){
  try{
    setBoundedState(LEARN_LOGS,idx,[]);
    await api(`/employees/${idx}/learn`,{method:"POST"});
    toast("已送去进修,全程可在这里围观"); await render();
  }catch(e){ toast(e.message); }
}

/* ---------- V5:行业专家面板(派活/能力/技能/提示词) ---------- */
async function openSpec(idx){
  SPEC = await api("/depts/emp/"+idx); SPEC_TAB = isBoss()?"task":(ME&&ME.role==="tour"?"intro":"intro"); SPEC_TASK = null;
  drawSpec();
}
function closeSpec(){ SPEC = null; SPEC_TASK = null; $("#modal").innerHTML = ""; }
function drawSpec(){
  if(!SPEC) return;
  const e = SPEC;
  const tabs = isBoss()
    ? [["task","📋 派活"],["caps","🧰 能力"],["method","🔬 工作方式"],
       ["skills","⚡ 技能库"],["info","🪪 岗位档案"]]
    : [...(ME&&ME.role!=="tour"?[["task","📋 派活"]]:[]),["intro","🪪 员工介绍"]];
  const st = e.learning?"learn":(e.tasks||[]).some(t=>["queued","running"].includes(t.status))?"work":"idle";
  let body = "";
  if(SPEC_TAB==="task") body = specTaskTab(e);
  else if(SPEC_TAB==="caps") body = specCapsTab(e);
  else if(SPEC_TAB==="method") body = specMethodTab(e);
  else if(SPEC_TAB==="skills") body = specSkillsTab(e);
  else if(SPEC_TAB==="info"&&isBoss()) body = specInfoTab(e);
  else body = specIntroTab(e);
  $("#modal").innerHTML = `<div class="overlay" onclick="if(event.target===this)closeSpec()">
    <div class="panel">
      <div class="phead" style="background:linear-gradient(120deg,${e.color}44,transparent)">
        ${charSVG(e.color, e.emoji, st, 88)}
        <div style="flex:1">
          <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
            <b style="font-size:19px">${esc(e.person||"")} · ${esc(e.name)}</b>
            <span class="tag">${esc(e.dept_name)}${isBoss()&&e.group?" · "+esc(e.group):""}</span>
            <span class="stpill ${PILL_CLS[st]}">${{work:"干活中",learn:"进修中",idle:"待命"}[st]}</span></div>
          <div class="sub" style="margin-top:5px">${esc(isBoss()?e.duty:e.intro)}</div>
          ${isBoss()?`<div class="kv" style="margin-top:6px"><span>交付 ${e.stats.runs} 单</span>
            <span title="累计成本,不是点击收费">历史成本 ${rmb(e.stats.cost_usd)}</span>
            <span>技能 ${(e.skills||[]).length} 条</span></div>`:""}
        </div>
        <button class="btn sm" onclick="closeSpec()">✕</button>
      </div>
      <div class="pbody">
        <div class="tabs">${tabs.map(([k,l])=>`<span class="tb ${SPEC_TAB===k?"on":""}" onclick="SPEC_TAB=${cp(k)};drawSpec()">${l}</span>`).join("")}</div>
        ${body}
      </div>
    </div></div>`;
  const box = $("#spec-steps"); if(box) box.scrollTop = box.scrollHeight;
}
function specIntroTab(e){
  return `<div class="card" style="background:linear-gradient(120deg,#fff6dc,#fffaf0);margin-top:0">
    <h2 style="margin-top:0">🪪 ${esc(e.person||e.name)}${e.person?" · "+esc(e.name):""}</h2>
    <div style="font-size:16px;line-height:1.9">${esc(e.intro||"这是一位随时待命的数字员工。")}</div>
  </div>`;
}
function specTaskTab(e){
  const tasks = e.tasks||[];
  const cur = SPEC_TASK;
  let curHtml = "";
  if(cur){
    const stLabel = {queued:"排队中",running:"⚙️ 干活中…",done:"✅ 已交付",failed:"❌ 失败"}[cur.status]||cur.status;
    curHtml = `<div class="card" style="background:#fffaf0;margin-top:12px">
      <div style="display:flex;gap:8px;align-items:center"><b style="flex:1">任务 #${cur.id} ${stLabel}</b>
        ${cur.status==="done"?`<button class="btn sm" onclick="copyText(${cp(cur.output_md||"")})">📋 复制</button>
          <button class="btn sm" onclick="taskEdit(${cur.id})">✏️ 编辑</button>
          <a class="btn sm" href="/api/tasks/${cur.id}/export.pdf">⬇️ PDF</a>
          <a class="btn sm" href="/api/tasks/${cur.id}/export.docx">⬇️ Word</a>
          <button class="btn sm" onclick="taskToKnow(${cur.id})">📚 存入沉淀库</button>`:""}
        <button class="btn sm" onclick="SPEC_TASK=null;drawSpec()">收起</button></div>
      <div class="sub">任务书:${esc(cur.brief?.direction||"")}</div>
      ${["queued","running"].includes(cur.status)?`<div class="steps" id="spec-steps" style="margin-top:8px">${(cur.steps||[]).map((s,i)=>stepRow(s,i+1)).join("")||`<div class="step"><span class="ic">⏳</span><span class="lb">专家上线中…</span></div>`}</div>`:""}
      ${cur.status==="done"?`${taskBody(cur)}
        <div class="card" style="background:#fff6dc;margin-top:10px"><b>不满意?给意见让 TA 重做</b>
        <textarea id="spec-feedback" placeholder="比如:预算按20万重新算;竞品只看本商圈的"></textarea>
        <div class="actions"><button class="btn pri sm" onclick="specRedo(${cur.id})">🔁 按意见重做</button></div></div>`:""}
      ${cur.status==="failed"?`<div class="notice red">${esc(cur.output_md||"执行失败")}
        <div class="actions">${cur.retryable?`<button class="btn sm pri" onclick="retryExpertTask(${cur.id},this)">🔁 免费重试</button>
          <span class="sub">沿用原任务，不再次扣点 · 还可重试 ${cur.free_retries_remaining} 次</span>`
          :`<span class="sub">免费重试次数已用完，请重新派活</span>`}</div></div>`:""}
    </div>`;
  }
  const prefill = EXP_PREFILL; EXP_PREFILL = "";   // 从「帮我选/改派」带过来的一句话,一次性回填
  return `
  <div class="notice" style="margin-top:0">📋 <b>给「${esc(e.name)}」派活</b>:一句话说清要什么。TA 会按岗位手册联网核实、产出可落地的交付物(自动进资产库)。</div>
  <label>任务内容 *</label>
  <textarea id="spec-dir" placeholder="例:请说明要解决的问题、目标和已有背景材料…">${esc(prefill)}</textarea>
  <div class="row">
    <div><label>行业/业态(选填)</label><input id="spec-industry" placeholder="如:川菜正餐 / 咖啡快闪 / 茶饮连锁"></div>
    <div><label>补充材料(选填,可传文件/图片/PDF)</label>
      <input type="file" multiple accept=".txt,.md,.csv,.json,.pdf,.jpg,.jpeg,.png,.webp" onchange="parseFileInto(this,'#spec-material')" style="margin-bottom:6px">
      <textarea id="spec-material" style="min-height:60px" placeholder="粘贴数据、地址、菜单;或直接传文件"></textarea></div>
  </div>
  ${lenOpts("spec-len")}
  <div class="actions"><button class="btn pri" onclick="specSubmit(${e.idx},this)">🚀 派活(1点)</button>
    <span class="sub">${isBoss()&&e.inputs?.length?"建议材料:"+esc(e.inputs.slice(0,3).join(";").slice(0,60)):"把背景、目标和已有材料说清楚即可；缺失信息 TA 会注明假设"}</span></div>
  <div id="spec-fit"></div>
  ${curHtml}
  ${tasks.length?`<h3>历史任务(${tasks.length})</h3>${tasks.map(t=>`
    <div class="topic" style="cursor:pointer" onclick="specOpenTask(${t.id})">
      <span class="t">#${t.id} ${esc((t.brief?.direction||"").slice(0,40))}</span>
      <span class="pill ${t.status==="done"?"done":t.status==="failed"?"failed":"running"}" style="float:right">${{queued:"排队",running:"干活中",done:"已交付",failed:"失败"}[t.status]}</span>
      <div class="sub" style="margin-top:3px">${new Date(t.created_at*1000).toLocaleString("zh-CN")} · ${rmb(t.cost_usd)}</div>
    </div>`).join("")}`:""}`;
}
async function specSubmit(idx, btn, force){
  const dir = $("#spec-dir").value.trim();
  if(!dir) return toast("任务内容必填");
  const industry = $("#spec-industry")?.value.trim()||"", material = $("#spec-material")?.value.trim()||"";
  btn.disabled = true; btn.innerHTML = `<span class="spin"></span> ${force?"仍派给TA…":"AI 核对岗位是否对口,随后下发…"}`;
  try{
    const body = {emp_idx:idx, brief:{direction:dir, industry, material, length:lenVal("spec-len")}};
    if(force) body.force = true;
    const r = await api("/tasks",{method:"POST",body, timeout:20000});
    if(r.mismatch){ btn.disabled=false; btn.innerHTML="🚀 派活(1点)"; return specShowMismatch(idx, r); }
    toast("已派活,专家马上开工");
    await specOpenTask(r.task_id);
  }catch(e){ toast(e.message); btn.disabled=false; btn.innerHTML="🚀 派活(1点)"; }
}
async function specOpenTask(tid,preserveDraft=false){
  const expectedIdx=SPEC?.idx;
  if(expectedIdx===undefined||expectedIdx===null) return;
  const nextTask = await api("/tasks/"+tid,{routeScoped:!preserveDraft});
  const nextSpec = await api("/depts/emp/"+expectedIdx,{routeScoped:!preserveDraft});
  if(!SPEC||SPEC.idx!==expectedIdx) return;
  const snapshot=preserveDraft?captureFormState():null;
  SPEC_TASK = nextTask;
  SPEC = nextSpec;
  drawSpec();
  if(snapshot) restoreFormState(snapshot);
}
async function specRedo(tid){
  const fb = $("#spec-feedback")?.value.trim();
  if(!fb) return toast("先写点意见");
  try{ const r = await api(`/tasks/${tid}/redo`,{method:"POST",body:{feedback:fb}});
    toast("已让 TA 重做"); await specOpenTask(r.task_id);
  }catch(e){ toast(e.message); }
}

/* ---------- V27:智能派活路由(大白话找专家 + 派单预检引导) ---------- */
function expFinderCard(deptKey){
  return `<div class="card" style="background:linear-gradient(120deg,#eef6ff,#fffaf0);margin-top:10px">
    <b>🎯 不知道找谁?</b> <span class="sub">一句话描述你要办的活,AI 帮你从本部门专家里挑最对口的。</span>
    <div style="display:flex;gap:8px;align-items:flex-start;margin-top:8px;flex-wrap:wrap">
      <textarea id="ef-text-${deptKey}" style="flex:1;min-width:min(100%,240px);min-height:46px"
        placeholder="例:我想给门店做一场周年庆活动,怎么策划引流"></textarea>
      <button class="btn pri" onclick="expFind(${cp(deptKey)},this)">🎯 帮我选</button>
    </div>
    <div id="ef-result-${deptKey}"></div></div>`;
}
let EXP_LAST_QUERY = "";   // 最近一次「帮我选」的那句话,「派给TA」时带进派活框
async function expFind(deptKey, btn){
  const text = $("#ef-text-"+deptKey).value.trim();
  if(!text) return toast("先用一句话说说要办什么活");
  EXP_LAST_QUERY = text;
  btn.disabled=true; const old=btn.innerHTML; btn.innerHTML=`<span class="spin"></span> AI 读花名册挑人中,约十几秒…`;
  try{
    const r = await api("/experts/match",{method:"POST",body:{text, dept_key:deptKey}, timeout:35000});
    const box = $("#ef-result-"+deptKey); const picks = r.picks||[];
    box.innerHTML = picks.length
      ? `<div class="sub" style="margin:10px 0 2px">AI 推荐这几位,点「派给TA」直接进 TA 的派活台(会带上你这句话):</div>`
        + picks.map(expCard).join("")
      : `<div class="sub" style="margin-top:10px">${esc(r.msg||"没匹配到特别对口的,换个说法再试试,或直接点下面的专家卡片。")}</div>`;
  }catch(e){ toast(e.message); }
  finally{ btn.disabled=false; btn.innerHTML=old; }
}
function expCard(p){
  return `<div class="topic" style="margin:8px 0;display:flex;gap:10px;align-items:flex-start;flex-wrap:wrap">
    <span style="font-size:22px">${esc(p.emoji||"🧑‍💼")}</span>
    <div style="flex:1;min-width:0">
      <div><b>${esc(p.name||"")}</b> <span class="sub">${esc(p.role||"")}${p.dept?" · "+esc(p.dept):""}</span></div>
      <div class="sub" style="margin-top:3px">💡 ${esc(p.why||"")}</div>
    </div>
    <button class="btn sm pri" onclick="pickExpert(${p.idx})">派给TA →</button></div>`;
}
// 记住那句话,openSpec 后由 specTaskTab 一次性回填到派活框。
// 帮我选:取部门找人框;改派:取当前派活框里老板刚写的任务。
function pickExpert(idx){ EXP_PREFILL = EXP_LAST_QUERY; openSpec(idx); }
function reassignExpert(idx){ EXP_PREFILL = ($("#spec-dir")?.value || "").trim(); openSpec(idx); }
// 派单预检不对口:显示原因 + 更对口的同事(一键改派)+ 坚持照派(force)
function specShowMismatch(idx, r){
  const box = $("#spec-fit"); if(!box) return;
  const sugg = (r.suggestions||[]).map(s=>`
    <div class="topic" style="margin:8px 0;display:flex;gap:10px;align-items:flex-start;flex-wrap:wrap">
      <span style="font-size:20px">🧑‍💼</span>
      <div style="flex:1;min-width:0"><div><b>${esc(s.name||"")}</b> <span class="sub">${esc(s.role||"")}</span></div>
        <div class="sub" style="margin-top:3px">💡 ${esc(s.why||"")}</div></div>
      <button class="btn sm pri" onclick="reassignExpert(${s.idx})">改派给TA →</button></div>`).join("");
  box.innerHTML = `<div class="card" style="background:#fff6dc;margin-top:12px">
    <b>🤔 这个活可能不太对口</b>
    <div class="sub" style="margin-top:4px">${esc(r.why||"这位专家的岗位和你要办的活不太搭,换个人可能更顺手。")}</div>
    ${sugg?`<div class="sub" style="margin-top:8px"><b>同部门这几位更合适:</b></div>${sugg}`:""}
    <div class="actions" style="margin-top:8px">
      <button class="btn" onclick="specSubmit(${idx},this,true)">坚持派给TA(照派)</button>
      <button class="btn sm" onclick="$('#spec-fit').innerHTML=''">再想想</button></div></div>`;
  box.scrollIntoView({behavior:"smooth", block:"nearest"});
}
function specCapsTab(e){
  const caps = e.capabilities||[];
  return `<div class="notice" style="margin-top:0">🧰 <b>工作流能力</b>:来自 TA 的岗位手册,派活时逐项执行。不需要的步骤可以关。</div>
  ${caps.map((c,i)=>`<div class="skillcard ${c.enabled?"":"off"}">
    <span class="switch ${c.enabled?"on":""}" onclick="specToggleCap(${i})"></span>
    <div style="flex:1"><div class="t">${esc(c.name)}</div><div class="sub">${esc(c.desc)}</div></div></div>`).join("")}`;
}
async function specToggleCap(i){
  const caps = [...SPEC.capabilities];
  caps[i] = {...caps[i], enabled:!caps[i].enabled};
  const off = caps.filter(c=>!c.enabled).map(c=>c.name);
  try{ const r = await api(`/employees/${SPEC.idx}/capabilities`,{method:"PUT",body:{caps_off:off}});
    SPEC.capabilities = r.capabilities; drawSpec();
  }catch(e){ toast(e.message); }
}
function specMethodTab(e){
  return `<div class="flow">
    <div class="fnode"><b>📥 必要输入(老板给得越全,产出越准)</b>
      <div class="sub" style="margin-top:4px">${(e.inputs||[]).map(x=>`· ${esc(x)}`).join("<br>")||"—"}</div></div>
    <div class="farrow">⬇️</div>
    <div class="fnode" style="border-color:${e.color}"><b>🧰 工作流(逐步执行,联网核实)</b>
      <div class="sub" style="margin-top:4px">${(e.steps||[]).map((x,i)=>`${i+1}. ${esc(x)}`).join("<br>")||"—"}</div></div>
    <div class="farrow">⬇️</div>
    <div class="fnode"><b>📤 交付物</b>
      <div class="sub" style="margin-top:4px">${(e.deliverables||[]).map(x=>`· ${esc(x)}`).join("<br>")||"一份可落地的 Markdown 报告"}</div></div>
  </div>`;
}
function specSkillsTab(e){
  const logs = LEARN_LOGS[e.idx]||[];
  return `<div class="notice violet" style="margin-top:0">📚 <b>全网进修</b>:让 TA 联网学本岗位最新方法论与行业新规,学成技能卡自动用于之后的工作。</div>
  <div class="actions" style="margin-top:10px">
    <button class="btn pri" ${e.learning?"disabled":""} onclick="specLearn(${e.idx})">${e.learning?`<span class="spin"></span> 进修中…`:"🎓 送去全网进修(3点)"}</button>
    <span class="sub">${e.learned_at?`上次进修:${new Date(e.learned_at*1000).toLocaleString("zh-CN")}`:"还没进修过"}</span></div>
  ${(e.learning||logs.length)?`<div class="steps" id="learnlog-${e.idx}" style="margin-top:10px">${logs.map(x=>stepRow(x)).join("")}</div>`:""}
  <h3>已掌握技能(${(e.skills||[]).length})</h3>
  ${(e.skills||[]).length? e.skills.map((k,i)=>`<div class="skillcard ${k.enabled===false?"off":""}">
    <span class="switch ${k.enabled!==false?"on":""}" onclick="specToggleSkill(${i})"></span>
    <div style="flex:1"><div class="t">${esc(k.title)}</div><div class="sub">${esc(k.detail)}</div>
      ${k.source?`<div class="src">来源:${esc(k.source)}</div>`:""}</div>
    <button class="btn sm bad" onclick="specDelSkill(${i})">🗑</button>
  </div>`).join("") : `<div class="empty">还没有技能,点上面送 TA 进修</div>`}`;
}
async function specLearn(idx){
  try{ setBoundedState(LEARN_LOGS,idx,[]); await api(`/employees/${idx}/learn`,{method:"POST"});
    toast("已送去进修"); SPEC = await api("/depts/emp/"+idx); drawSpec();
  }catch(e){ toast(e.message); }
}
async function specToggleSkill(i){
  const skills=[...SPEC.skills]; skills[i]={...skills[i],enabled:skills[i].enabled===false};
  try{ await api(`/employees/${SPEC.idx}/skills`,{method:"PUT",body:{skills}});
    SPEC.skills=skills; drawSpec(); }catch(e){ toast(e.message); }
}
async function specDelSkill(i){
  if(!await uiConfirm(`删除技能「${SPEC.skills[i].title}」?`,{
    title:"删除技能",confirmText:"删除"
  })) return;
  const skills=SPEC.skills.filter((_,n)=>n!==i);
  try{ await api(`/employees/${SPEC.idx}/skills`,{method:"PUT",body:{skills}});
    SPEC.skills=skills; drawSpec(); }catch(e){ toast(e.message); }
}
function specPromptTab(e){
  return `<div class="notice" style="margin-top:0">📝 默认提示词由「岗位手册+老板任务书+技能库+知识沉淀」自动拼装,一般不用改。想完全接管就在下面写自定义模板(支持 {direction} {industry} {material} 占位符)。</div>
  <textarea id="spec-prompt" class="promptbox" placeholder="(空 = 用系统自动拼装的默认提示词)">${esc(e.prompt_template||"")}</textarea>
  <div class="actions">
    <button class="btn pri" onclick="specSavePrompt()">💾 保存</button>
    ${e.is_custom?`<button class="btn" onclick="specResetPrompt()">↩️ 恢复自动拼装</button><span class="tag">当前:老板自定义</span>`:`<span class="tag">当前:自动拼装</span>`}
  </div>`;
}
async function specSavePrompt(){
  try{ await api(`/employees/${SPEC.idx}/prompt`,{method:"PUT",body:{template:$("#spec-prompt").value}});
    toast("已保存"); SPEC = await api("/depts/emp/"+SPEC.idx); drawSpec(); }catch(e){ toast(e.message); }
}
async function specResetPrompt(){
  try{ await api(`/employees/${SPEC.idx}/prompt`,{method:"PUT",body:{template:""}});
    toast("已恢复"); SPEC = await api("/depts/emp/"+SPEC.idx); drawSpec(); }catch(e){ toast(e.message); }
}
function specInfoTab(e){
  return `<div class="topic"><div class="t">🪪 岗位职责</div><div class="sub" style="margin-top:4px">${esc(e.desc)}</div></div>
  <div class="topic"><div class="t">🏢 所属</div><div class="sub" style="margin-top:4px">${esc(e.dept_name)} · ${esc(e.group)}</div></div>
  <div class="topic"><div class="t">📈 出勤统计</div><div class="sub" style="margin-top:4px">交付 ${e.stats.runs} 单 · 累计花费 ${rmb(e.stats.cost_usd)}</div></div>`;
}

/* ---------- 打断 / 恢复 ---------- */
async function pauseJob(id){
  try{ await api(`/jobs/${id}/pause`,{method:"POST"}); toast("⏸ 已打断,员工停笔待命"); render(); }
  catch(e){ toast(e.message); }
}
async function resumeJob(id){
  try{ await api(`/jobs/${id}/resume`,{method:"POST"}); toast("▶️ 已恢复,被打断的工位自动重跑"); render(); }
  catch(e){ toast(e.message); }
}

/* ---------- 新建 Brief ---------- */
const BRIEF_EXAMPLES = [
  "本周行业里值得聊的新动态,选和普通人最相关的一条深挖",
  "我的新品[填名字]要上市了,写一篇种草文,突出[卖点]",
  "网上最近很火的[话题],用我的账号视角说说不一样的观点",
  "写一篇新手教程:[主题],要具体到步骤,别讲空话",
];
let XHS_ALL = [], DY_ALL = [];
let BRIEF_DRAFT_T=null;
function briefDraftKey(){ return `brief_draft_user_${ME?.id??"none"}`; }
function selectedBriefChip(containerId,attribute=""){
  const el=document.querySelector(`#${containerId} .chip.on`);
  return el?(attribute?el.getAttribute(attribute):el.textContent.trim()):"";
}
function readBriefDraft(){
  try{
    const draft=JSON.parse(localStorage.getItem(briefDraftKey())||"null");
    if(!draft||draft.version!==1||Date.now()-Number(draft.updatedAt)>7*86400e3){
      localStorage.removeItem(briefDraftKey());
      return null;
    }
    return draft;
  }catch(_){
    try{ localStorage.removeItem(briefDraftKey()); }catch(_ignored){}
    return null;
  }
}
function saveBriefDraft(){
  const form=$("#brief-form"); if(!form||!ME||ME.role==="tour") return;
  const value=(selector,max=4000)=>String($(selector)?.value||"").slice(0,max);
  const draft={
    version:1,updatedAt:Date.now(),
    fields:{
      industryCustom:value("#b-ind-c",300),
      direction:value("#b-dir",5000),
      templateCustom:value("#b-tpl-c",300),
      xhs:value("#b-xhs",50),dy:value("#b-dy",50),
      profile:value("#b-profile",50),mode:value("#b-mode",30),
      ref:value("#b-ref",2048),material:value("#b-mat",12000),
      deck:!!$("#b-deck")?.checked,
    },
    chips:{
      industry:selectedBriefChip("b-ind"),
      template:selectedBriefChip("b-tpl"),
      imageCount:selectedBriefChip("b-imgn"),
      imageMode:selectedBriefChip("b-imode","data-k"),
      platforms:[...document.querySelectorAll("#b-pf .chip.on")].map(
        el=>el.dataset.p||el.textContent.trim()).slice(0,20),
    },
    advanced:!!form.querySelector("details")?.open,
  };
  try{ localStorage.setItem(briefDraftKey(),JSON.stringify(draft)); }catch(_){}
}
function scheduleBriefDraftSave(){
  if(BRIEF_DRAFT_T!==null) clearTimeout(BRIEF_DRAFT_T);
  BRIEF_DRAFT_T=setTimeout(()=>{ BRIEF_DRAFT_T=null; saveBriefDraft(); },350);
}
function selectBriefChips(containerId,values,attribute=""){
  const wanted=new Set((Array.isArray(values)?values:[values]).map(String));
  document.querySelectorAll(`#${containerId} .chip`).forEach(el=>{
    const value=attribute?el.getAttribute(attribute):el.textContent.trim();
    el.classList.toggle("on",wanted.has(String(value)));
  });
}
function restoreBriefDraft(draft=readBriefDraft()){
  if(!draft||!$("#brief-form")) return false;
  const f=draft.fields||{}, assign=(selector,value)=>{
    const el=$(selector); if(el&&value!==undefined&&value!==null) el.value=String(value);
  };
  assign("#b-ind-c",f.industryCustom); assign("#b-dir",f.direction);
  assign("#b-tpl-c",f.templateCustom); assign("#b-xhs",f.xhs); assign("#b-dy",f.dy);
  assign("#b-profile",f.profile); assign("#b-mode",f.mode);
  assign("#b-ref",f.ref); assign("#b-mat",f.material);
  if($("#b-deck")) $("#b-deck").checked=!!f.deck;
  const c=draft.chips||{};
  if(c.industry) selectBriefChips("b-ind",c.industry);
  if(c.template) selectBriefChips("b-tpl",c.template);
  if(c.imageCount) selectBriefChips("b-imgn",c.imageCount);
  if(c.imageMode) selectBriefChips("b-imode",c.imageMode,"data-k");
  if(Array.isArray(c.platforms)) selectBriefChips("b-pf",c.platforms,"data-p");
  const details=$("#brief-form details"); if(details) details.open=!!draft.advanced;
  return true;
}
function clearBriefDraft(){
  if(BRIEF_DRAFT_T!==null){ clearTimeout(BRIEF_DRAFT_T); BRIEF_DRAFT_T=null; }
  try{ localStorage.removeItem(briefDraftKey()); }catch(_){}
}
function discardBriefDraft(){ clearBriefDraft(); render(); }
async function newBrief(){
  const profiles = STATE.profiles;
  const PRE = (()=>{ try{ const v=JSON.parse(localStorage.getItem("prefill_brief")||"null"); localStorage.removeItem("prefill_brief"); return v; }catch(e){ return null; } })();
  if(PRE) clearBriefDraft();
  const ps = await api("/pstyles").catch(optionalResult({}));
  XHS_ALL = [...((ps["小红书"]||{}).custom||[]), ...((ps["小红书"]||{}).presets||[])];
  DY_ALL = [...((ps["抖音"]||{}).custom||[]), ...((ps["抖音"]||{}).presets||[])];
  $("#main").innerHTML = `<div class="card" id="brief-form" style="max-width:820px;margin:0 auto">
    <h2>➕ 下达任务</h2>
    <div id="brief-draft-notice"></div>
    <div class="notice" style="margin-top:8px">👔 <b>老板只需要三步</b>:①选行业 ②说方向 ③点提交。剩下的交给流水线:趋势官找热点 → 情报员查资料 → 拆解师学爆款 → 撰稿改稿配图封面 → 质检 → 各平台发布包。关键节点会停下来等您拍板。</div>
    <label>① 行业/赛道(内容会贴着这个行业做:渠道、黑话、案例、对标)</label>
    <div class="chips" id="b-ind">${META.industries.map((t,i)=>`<span class="chip${i===0?" on":""}" onclick="pick(this)">${t}</span>`).join("")}</div>
    <input id="b-ind-c" value="${esc(PRE?.industry||"")}" placeholder="✏️ 或自己输入行业/赛道(如:宠物烘焙、二手奢侈品)——填了就用您输入的" style="margin-top:6px">
    <label>② 内容方向 * <span class="sub">(一句话说清"聊什么",越具体越好;没灵感点下面的例子)</span></label>
    <textarea id="b-dir" placeholder="例:${esc(BRIEF_EXAMPLES[0])}">${esc(PRE?.direction||"")}</textarea>
    ${PRE?`<div class="notice green">🔥 来自「今日必发」:${esc(PRE.why||"")}</div>`:""}
    <div class="chips" style="margin-top:4px">${BRIEF_EXAMPLES.map(x=>`<span class="chip" style="font-size:11px" onclick="$('#b-dir').value=this.textContent;scheduleBriefDraftSave()">${esc(x)}</span>`).join("")}</div>
    <label>③ 内容类型 <span class="sub">(决定整条流水线的打法)</span></label>
    <div class="chips" id="b-tpl">${META.brief_templates.map((t,i)=>`<span class="chip${i===1?" on":""}" onclick="pick(this)" title="${{蹭热点:"追当下热点,时效优先",日更选题:"从趋势里挑题,稳定日更",产品软文:"带货种草,卖点前置",观点输出:"独到观点,人设优先",教程干货:"手把手教程,信息密度高",二创改写:"对标内容二次创作"}[t]||t}">${t}</span>`).join("")}</div>
    <input id="b-tpl-c" value="${esc(PRE?.template||"")}" placeholder="✏️ 或自己输入内容类型(如:门店探访日记、老板问答)——填了就用您输入的" style="margin-top:6px">
    <label>④ 配图 · 张数</label>
    <div class="chips" id="b-imgn">${["自动","2","3","4","5","6"].map((n,i)=>`<span class="chip${i===0?" on":""}" onclick="pick(this)">${n}${i?"张":""}</span>`).join("")}</div>
    <label>配图来源 <span class="sub">(真实图=全网抓取真实照片,适合公众号/资讯类;AI=生成插画)</span></label>
    <div class="chips" id="b-imode">${(META.image_modes||[{key:"ai",label:"🎨 AI生成"}]).map((m,i)=>`<span class="chip${i===0?" on":""}" data-k="${m.key}" onclick="pick(this)">${m.label}</span>`).join("")}</div>
    <label>⑤ 发到哪些平台(多选,每个平台出专属版本+专属封面尺寸)</label>
    <div class="chips" id="b-pf">${META.platforms.map((p,i)=>`<span class="chip${i===0?" on":""}" data-p="${p}" onclick="this.classList.toggle('on')">${META.platform_specs[p]?.emoji||""} ${p}</span>`).join("")}</div>
    <div class="row">
      <div><label>小红书风格 <span class="sub">(选了小红书时生效)</span></label>
      <select id="b-xhs"><option value="">跟随人设(不指定)</option>
      ${XHS_ALL.map((s,i)=>`<option value="${i}">${esc(s.name)}${s.preset?" · 预设":" · 自定义"}</option>`).join("")}</select></div>
      <div><label>抖音风格 <span class="sub">(选了抖音时生效;<a href="#/channels" style="text-decoration:underline">自定义 →</a>)</span></label>
      <select id="b-dy"><option value="">跟随人设(不指定)</option>
      ${DY_ALL.map((s,i)=>`<option value="${i}">${esc(s.name)}${s.preset?" · 预设":" · 自定义"}</option>`).join("")}</select></div>
    </div>
    <div class="row">
      <div><label>⑥ 用哪个账号人设 <span class="sub">(写出来像您本人)</span></label><select id="b-profile"><option value="">(不使用人设档案)</option>
        ${profiles.map(p=>`<option value="${p.id}">${esc(p.name)}</option>`).join("")}</select>
        <div class="sub" style="margin-top:4px">还没档案?<a href="#/profiles" style="text-decoration:underline">30秒去建一个 →</a></div></div>
      <div><label>⑦ 您想管多少 <span class="sub">(随时可打断)</span></label><select id="b-mode">
        <option value="copilot">关键审批(推荐)— 选题/初稿/视觉/发布 4 处等您拍板</option>
        <option value="fullauto">完全托管 — 一停不停,员工接力干到交付(质检仍生效)</option>
        <option value="autopilot">全自动 — 只在发布前终审等您</option>
        <option value="manual">逐站审批 — 10 个工位每一步都等您</option></select></div>
    </div>
    <details style="margin-top:12px"><summary class="sub" style="cursor:pointer">📎 高级选项:参考链接 / 附加素材 / 演绎稿(选填)</summary>
      <label>参考链接</label><input id="b-ref" placeholder="热点新闻/对标文章链接,情报员会精读" value="${esc(PRE?.ref_link||"")}">
      <label>附加素材(可传文件:txt/pdf/图片自动读成文字)</label>
      <input type="file" multiple accept=".txt,.md,.csv,.json,.pdf,.jpg,.jpeg,.png,.webp" onchange="parseFileInto(this,'#b-mat')" style="margin-bottom:6px">
      <textarea id="b-mat" placeholder="产品资料、您的观点、必须包含的数据…都塞进来,员工会用上">${esc(PRE?.material||"")}</textarea>
      <label style="display:flex;align-items:center"><input type="checkbox" id="b-deck" style="width:auto;margin-right:8px">启用「演绎师」:额外产出一页交互式 HTML 演绎稿(适合教程/知识类)</label>
    </details>
    <div class="actions" style="margin-top:16px"><button class="btn pri" style="font-size:15px" onclick="submitBrief(this)">🚀 提交,让团队开工</button>
      <span class="sub">💎 本单 ${META?.job_points??18} 点 · 余额 ${Math.round(STATE?.balance||0)} 点 · 提交后可看全程直播,随时打断</span></div>
    ${(STATE&&Math.round(STATE.balance||0)<(META?.job_points??18))?`<div class="notice" style="background:#fff3d6;margin-top:8px">⚠️ 余额不足本单所需 ${META?.job_points??18} 点。${ME?.role==="member"?"充值请联系贵司主账号(企业主)。":`<a href="#/billing" style="font-weight:800;text-decoration:underline">先去充值 →</a>`}(余额不够时提交会直接提示,不会扣费)</div>`:""}
  </div>`;
  const restored=!PRE&&restoreBriefDraft();
  if(restored) $("#brief-draft-notice").innerHTML=`<div class="notice green" role="status">
    已恢复您上次未提交的 Brief 草稿。
    <button class="btn sm" type="button" onclick="discardBriefDraft()">清空草稿</button></div>`;
  const form=$("#brief-form");
  form.addEventListener("input",scheduleBriefDraftSave,true);
  form.addEventListener("change",scheduleBriefDraftSave,true);
  form.addEventListener("click",event=>{ if(event.target.closest(".chip,summary")) scheduleBriefDraftSave(); });
}
function pick(el){ [...el.parentNode.children].forEach(c=>c.classList.remove("on")); el.classList.add("on"); }
async function submitBrief(btn){
  const dir = $("#b-dir").value.trim();
  if(!dir) return toast("内容方向必填");
  btn.disabled = true; btn.innerHTML = `<span class="spin"></span> 提交中…`;
  try{
    const r = await api("/jobs",{method:"POST",body:{
      brief:{direction:dir, template:$("#b-tpl-c").value.trim()||$("#b-tpl .on")?.textContent||"",
        industry:$("#b-ind-c").value.trim()||$("#b-ind .on")?.textContent||"",
        image_count: (()=>{const v=$("#b-imgn .on")?.textContent||"";const n=parseInt(v);return isNaN(n)?null:n;})(),
        image_mode: $("#b-imode .on")?.dataset.k||"ai",
        xhs_style: (()=>{const i=$("#b-xhs")?.value; return i!==""&&XHS_ALL[+i]?{name:XHS_ALL[+i].name,desc:XHS_ALL[+i].desc}:null;})(),
        dy_style: (()=>{const i=$("#b-dy")?.value; return i!==""&&DY_ALL[+i]?{name:DY_ALL[+i].name,desc:DY_ALL[+i].desc}:null;})(),
        platforms:[...document.querySelectorAll("#b-pf .on")].map(e=>e.dataset.p||e.textContent.trim()),
        ref_link:$("#b-ref").value.trim(), material:$("#b-mat").value.trim(),
        enable_deck:$("#b-deck").checked},
      profile_id: $("#b-profile").value?+$("#b-profile").value:null,
      mode: $("#b-mode").value}});
    clearBriefDraft();
    location.hash = "#/job/"+r.job_id;
  }catch(e){
    if(e.status===409||e.status===429){ toast(e.message); setTimeout(()=>location.hash="#/tasks", 900); return; }
    toast(e.message); btn.disabled=false; btn.textContent="🚀 提交,让团队开工"; }
}

function rebrief(id){
  const j = CUR_JOB && CUR_JOB.id===id ? CUR_JOB : null;
  const b = j?.brief || {};
  localStorage.setItem("prefill_brief", JSON.stringify({
    direction: b.direction||"", material: b.material||"", ref_link: b.ref_link||"",
    template: b.template||"", industry: b.industry||"",
    why: `已带入失败工单 #${id} 的原 Brief，修改后可直接重新开单`}));
  location.hash = "#/new";
}

/* ---------- 工单详情 ---------- */
function stnState(stat){
  return {running:"work",rejected:"work",awaiting_review:"await",failed:"fail",interrupted:"fail"}[stat]||"idle";
}
function elapsedLabel(seconds){
  const s=Math.max(0,Math.floor(seconds||0));
  if(s<60) return `${s}秒`;
  if(s<3600) return `${Math.floor(s/60)}分`;
  const h=Math.floor(s/3600),m=Math.floor((s%3600)/60);
  return `${h}小时${m?m+"分":""}`;
}
function jobProgress(j){
  const total=META?.stations?.length||10;
  const runs=Object.values(j.runs||{});
  const completed=runs.filter(r=>["done","skipped"].includes(r.status)).length;
  const active=runs.some(r=>["running","queued","rejected"].includes(r.status));
  const waiting=runs.some(r=>r.status==="awaiting_review")||j.status==="awaiting_review";
  const visual=j.status==="done" ? total : completed+(active ? 0.35 : waiting ? 0.7 : 0);
  const percent=Math.max(0,Math.min(100,Math.round(visual/Math.max(total,1)*100)));
  const current=Math.min(total,Math.max(1,(Number(j.current_idx)||0)+1));
  const elapsed=elapsedLabel(Date.now()/1000-(Number(j.created_at)||Date.now()/1000));
  let note;
  if(j.status==="done") note="整单已经交付，可直接查看交付包。";
  else if(j.status==="awaiting_review") note="当前已停在审批点，需您拍板后才继续；离开页面不会丢单。";
  else if(j.status==="gate_blocked") note="内容被质检关卡拦下，需您处理后才会继续。";
  else if(j.status==="paused") note="工单已暂停，恢复开工后会从被打断处重跑。";
  else if(j.status==="failed") note = j.billing_status==="refunded"
    ? "本单执行失败，点数已自动退回，没有扣费。可点上方「复制 Brief 重新开单」再来一次。"
    : "本单中途失败，但已产出可用正文（按已产出部分计）。可在下方工位取回已完成内容，或点上方「复制 Brief 重新开单」。";
  else if(j.status==="cancelled") note="工单已经终止。";
  else note=`可以离开本页，后台继续执行。${STATE?.setup?.wechat?"完成或等您拍板时会发微信提醒。":"随时回「任务中心」查看最新结果。"}`;
  return {total,completed,current,percent,elapsed,note};
}
async function jobView(id){
  const j = await api("/jobs/"+id); CUR_JOB = j;
  if(SEL_IDX===null){
    SEL_IDX = +j.current_idx;
    if(j.status==="awaiting_review"){ const w = Object.values(j.runs).find(r=>r.status==="awaiting_review"); if(w) SEL_IDX=w.station_idx; }
  }
  const gateSt = j.status==="gate_blocked" ? "failed"
    : (j.gate && (j.gate.passed||j.gate.override)) ? "done" : "queued";
  const gateNode = `<div class="stn" data-stn="gate" title="发布前质检关卡">
    ${charSVG("#ef476f","🛡️",gateSt==="failed"?"fail":"idle",56)}<div class="nm">质检关卡</div>
    <div class="st">${{failed:"⛔ 拦截",done:"已通过",queued:"未质检"}[gateSt]}</div></div>`;
  const strip = META.stations.map(s=>{
    const r = j.runs[s.idx], st = r?r.status:(s.idx===j.current_idx&&j.status==="running"?"running":"queued");
    const live = (st==="running"||st==="rejected") && r&&r.steps&&r.steps.length
      ? `<div class="live">${esc(r.steps[r.steps.length-1].l)}</div>` : "";
    const badge = st==="done"?"✅":st==="awaiting_review"?"⏳":st==="failed"?"🆘":st==="interrupted"?"⏸":"";
    return (s.idx===8?gateNode:"")+
      `<div class="stn ${SEL_IDX===s.idx?"sel":""}" data-stn="${s.idx}" onclick="SEL_IDX=${s.idx};EDIT_MODE=false;render()">
      ${charSVG(s.color, s.emoji, stnState(st), 56)}
      <div class="nm">${s.name} ${badge}</div><div class="st">${RUN_LABEL[st]||st}</div>${live}</div>`;
  }).join("");
  let gateHtml = "";
  if(j.gate && (j.status==="gate_blocked" || j.gate.issues?.length)){
    gateHtml = `<div class="card"><h2>🛡️ 质检关卡 ${j.gate.passed?"":"— ⛔ 已阻断发布"}</h2>
      <div class="notice ${j.gate.passed?"green":"red"}">${esc(j.gate.report||"")}</div>
      ${(j.gate.issues||[]).map(i=>`<div class="notice"><b>[${esc(i.severity)}] ${esc(i.type)}</b>:${esc(i.detail)}</div>`).join("")}
      ${j.status==="gate_blocked"?`<div class="actions">
        <button class="btn pri" onclick="gateAct(${j.id},'recheck')">修改后重新质检</button>
        <button class="btn bad" onclick="confirmGateOverride(${j.id})">知晓风险,继续</button>
        <span class="sub">可先到工位5「文风师」修改定稿再重检</span></div>`:""}
      ${stepsLog("gate", j.gate.steps)}
    </div>`;
  }
  const canPause = ["running","awaiting_review","gate_blocked"].includes(j.status);
  const whole=jobProgress(j);
  $("#main").innerHTML = `
  <div class="card">
    <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">
      <h2 style="margin:0;flex:1;min-width:min(100%,220px)">#${j.id} ${esc(j.title!=="(未产出标题)"?j.title:j.brief.direction)}</h2>
      <span class="pill ${j.status}">${ST_LABEL[j.status]||j.status}</span>
      ${canPause?`<button class="btn sm" onclick="pauseJob(${j.id})">⏸ 打断</button>`:""}
      ${j.status==="paused"?`<button class="btn sm ok" onclick="resumeJob(${j.id})">▶️ 恢复开工</button>`:""}
      ${j.status==="done"?`<a class="btn pri" href="#/delivery/${j.id}">📦 查看交付包</a>`:""}
      ${["failed","cancelled"].includes(j.status)?`<button class="btn pri sm" onclick="rebrief(${j.id})">🔁 复制 Brief 重新开单</button>`:""}
      ${!["done","cancelled"].includes(j.status)?`<button class="btn bad sm" onclick="cancelWholeJob(${j.id})">终止工单</button>`:""}
      ${isAdmin()?`<button class="btn bad sm" onclick="deleteJob(${j.id})" title="移入回收站,可恢复">🗑 删除</button>`:""}
    </div>
    <div class="kv"><span>Brief:${esc(j.brief.direction)}</span><span>模式:${esc(MODE_LABEL[j.mode]||j.mode)}</span>
      <span>发起人:${esc(j.created_by_name||"—")}</span>
      <span>平台:${(j.brief.platforms||[]).map(esc).join("/")}</span>
      ${ME.role==="root"?`<span>成本:${rmb(j.cost_usd)} · ${((j.tokens||0)/1000).toFixed(1)}k tokens</span>`:""}</div>
    <div class="notice ${j.status==="done"?"green":j.status==="failed"?"red":""}" style="margin:12px 0 4px">
      <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
        <b style="flex:1">整单进度：已完成 ${whole.completed}/${whole.total} 个工位 · 当前第 ${whole.current}/${whole.total} 步</b>
        <span class="sub">从开工至今 ${whole.elapsed}</span></div>
      <div role="progressbar" aria-label="整单进度" aria-valuemin="0" aria-valuemax="100"
        aria-valuenow="${whole.percent}" style="height:9px;background:#eadfcd;border:1.5px solid var(--ink);border-radius:999px;overflow:hidden;margin:8px 0 6px">
        <div style="height:100%;width:${whole.percent}%;background:${j.status==="failed"?"#e5484d":"#2f9e44"};transition:width .25s ease"></div></div>
      <div class="sub" style="font-weight:700">${esc(whole.note)}</div>
    </div>
    ${j.status==="paused"?`<div class="notice violet">⏸ 工单已被您打断。可以先去改员工提示词/技能,点「恢复开工」后被打断的工位会自动重跑。</div>`:""}
    <div class="pipeline">${strip}</div>
  </div>
  ${gateHtml}
  <div id="stationPanel">${stationPanel(j, SEL_IDX)}</div>`;
  scaleFrames();
}
function stationPanel(j, idx){
  const s = META.stations[idx], r = j.runs[idx];
  const head = `<div style="display:flex;align-items:center;gap:12px">
    ${charSVG(s.color, s.emoji, r?stnState(r.status):"idle", 62)}
    <div style="flex:1"><b>工位${idx+1} · ${s.name}</b><div class="sub">${isBoss()?esc(s.duty)+" · Skill:"+esc(s.skill):esc(s.intro)}</div></div>
    <button class="btn sm" onclick="openEmp(${idx})">🪪 ${isBoss()?"员工面板":"员工介绍"}</button>
    ${r?`<span class="pill ${r.status==='done'?'done':r.status==='awaiting_review'?'awaiting_review':['failed','interrupted'].includes(r.status)?'failed':'running'}">${RUN_LABEL[r.status]||r.status}</span>`:""}
    ${r&&r.versions>1?`<span class="sub">v${r.version}(共${r.versions}版)</span>`:""}</div>`;
  if(!r) return `<div class="card">${head}<div class="out sub">尚未轮到本工位。</div></div>`;
  if(r.status==="running"||r.status==="queued"||r.status==="rejected")
    return `<div class="card">${head}<div class="out"><span class="spin"></span> ${s.name}正在工作,下面是 TA 的实时动作,完成后会${r.needs_review?"请您审批":"自动流转"}…
      <div class="actions" style="margin:10px 0 0"><button class="btn sm bad" onclick="pauseJob(${j.id})">⏸ 打断 TA</button></div>
      <div style="margin-top:10px">${stepsBox(idx, r.steps)}</div></div></div>`;
  if(r.status==="interrupted")
    return `<div class="card">${head}<div class="out"><div class="notice violet">⏸ 本工位执行到一半被您打断。恢复工单后会自动重跑;也可以先改 TA 的提示词再恢复。</div>
      <div class="actions"><button class="btn ok" onclick="resumeJob(${j.id})">▶️ 恢复开工</button>
      <button class="btn" onclick="openEmp(${idx})">📝 改提示词</button></div>
      ${stepsLog(idx, r.steps, true)}</div></div>`;
  if(r.status==="failed")
    return `<div class="card">${head}<div class="out"><div class="notice red">${esc(r.review_comment||"执行失败")}</div>
      <div class="actions"><button class="btn pri" onclick="act(${j.id},${idx},'rerun')">🔄 重试本工位</button></div>
      ${stepsLog(idx, r.steps, true)}</div></div>`;
  if(r.status==="skipped")
    return `<div class="card">${head}<div class="out sub">本工位按配置跳过(演绎师默认关闭,可在 Brief 勾选启用)。</div></div>`;
  if(r.status==="stale")
    return `<div class="card">${head}<div class="out sub">上游产出已更新,本工位待自动重算…${stepsLog(idx, r.steps)}</div></div>`;
  const body = OUT_RENDER[s.key] ? OUT_RENDER[s.key](r, j) : `<pre>${esc(JSON.stringify(r.output,null,2))}</pre>`;
  const reviewedBy = r.status==="done"&&r.reviewed_by_name
    ?`<div class="sub" style="margin-top:8px">✅ 由 ${esc(r.reviewed_by_name)} 拍板</div>`:"";
  return `<div class="card">${head}<div class="out">${body}</div>${reviewedBy}${actionsBar(j,idx,r)}${stepsLog(idx, r.steps)}</div>`;
}
function actionsBar(j, idx, r){
  const s = META.stations[idx];
  if(r.status==="awaiting_review"){
    const isPublish = s.key==="publish";
    return `<div class="actions">
      <button class="btn ok" onclick="approve(${j.id},${idx})">${isPublish?"✅ 终审确认,交付发布":"✅ 通过"}</button>
      ${["draft","style"].includes(s.key)&&!EDIT_MODE?`<button class="btn" onclick="EDIT_MODE=true;render()">✏️ 修改后通过</button>`:""}
      <button class="btn bad" onclick="rejectStation(${j.id},${idx})">↩️ 打回重做</button>
      ${r.versions>1?`<button class="btn sm" onclick="showVersions(${j.id},${idx})">历史版本</button>`:""}
      ${isPublish?`<span class="sub">发布是不可逆动作,永远需要老板终审</span>`:""}</div>`;
  }
  if(r.status==="done")
    return `<div class="actions"><button class="btn" onclick="rejectStation(${j.id},${idx},true)">🔄 携带意见重跑</button>
      ${r.versions>1?`<button class="btn sm" onclick="showVersions(${j.id},${idx})">历史版本</button>`:""}
      <span class="sub">重跑后下游工位将自动重算</span></div>`;
  return "";
}
async function act(jobId, idx, action, payload){
  try{ await api(`/jobs/${jobId}/stations/${idx}/action`,{method:"POST",body:{action,payload:payload||{}}});
    EDIT_MODE=false; toast("已提交,团队继续干活"); render();
  }catch(e){ toast(e.message); }
}
function approve(jobId, idx){
  const payload = {};
  const sel = document.querySelector("input[name=pickone]:checked");
  if(sel) payload.selected = +sel.value;
  const tsel = document.querySelector("input[name=picktitle]:checked");
  if(tsel) payload.selected_title = +tsel.value;
  if(EDIT_MODE){
    const ed = $("#editBody"); if(ed) payload.edits = {body: ed.value};
  }
  act(jobId, idx, "approve", payload);
}
async function rejectStation(jobId, idx, isRerun){
  const c = await uiPrompt({
    title:isRerun?"携带意见重跑":"打回重做",
    message:isRerun?"告诉员工哪里要改。":"请具体说明哪里不符合要求，员工会按意见重做。",
    label:"修改意见",multiline:true,requiredMessage:"必须填写意见",
    confirmText:isRerun?"提交重跑":"打回"
  });
  if(c===null) return;
  act(jobId, idx, isRerun?"rerun":"reject", {comment:c.trim()});
}
async function gateAct(jobId, action){
  try{ await api(`/jobs/${jobId}/gate`,{method:"POST",body:{action}}); toast("已提交"); render(); }
  catch(e){ toast(e.message); }
}
async function confirmGateOverride(jobId){
  if(!await uiConfirm("确认知晓风险并继续发布流程?",{
    title:"跳过质检拦截",confirmText:"知晓风险，继续"
  })) return;
  gateAct(jobId,"override");
}
async function cancelWholeJob(jobId){
  if(!await uiConfirm("终止整个工单? 已完成的产物仍会保留。",{
    title:"终止工单",confirmText:"终止"
  })) return;
  try{ await api(`/jobs/${jobId}/cancel`,{method:"POST"}); toast("工单已终止"); render(); }
  catch(e){ toast(e.message); }
}
async function showVersions(jobId, idx){
  const vs = await api(`/jobs/${jobId}/stations/${idx}/versions`);
  await uiAlert({title:"历史版本",message:vs.map(v=>
    `v${v.version} [${RUN_LABEL[v.status]||v.status}] ${v.review_comment?("意见:"+v.review_comment):""}\n`+
    JSON.stringify(v.output).slice(0,300)).join("\n\n————————\n\n")});
}

/* ---------- 各工位产出渲染 ---------- */
const OUT_RENDER = {
  trend(r){ const o=r.output, sel=o.selected??-1, canPick=r.status==="awaiting_review";
    return `<h3>📡 今日趋势简报</h3><div class="md">${md(o.briefing)}</div>
    ${o.channel_scan?.length?`<h3>渠道扫描(${o.channel_scan.length} 个渠道)</h3><div class="grid3" style="grid-template-columns:1fr 1fr">${o.channel_scan.map(c=>
      `<div class="topic" style="margin:0"><b>${esc(c.channel)}</b><div class="sub" style="margin-top:3px">${esc(c.finding)}</div></div>`).join("")}</div>`:""}
    <h3>候选选题(${canPick?"请老板挑一个":"已选定"})</h3>`+
    (o.topics||[]).map((t,i)=>`<label class="topic ${(canPick? i===0&&sel<0? false: sel===i : sel===i)?"sel":""}" style="display:block">
      ${canPick?`<input type="radio" name="pickone" value="${i}" ${i===(sel>=0?sel:0)?"checked":""} onchange="[...document.querySelectorAll('.topic')].forEach(e=>e.classList.remove('sel'));this.closest('.topic').classList.add('sel')" style="width:auto;margin-right:8px">`:""}
      <span class="t">${esc(t.title)}</span> <span class="pill ${t.heat==="高"?"failed":t.heat==="中"?"awaiting_review":"cancelled"}" style="margin-left:6px">热度·${esc(t.heat||"?")}</span>
      <div class="sub" style="margin-top:5px">角度:${esc(t.angle||"")}<br>钩子:${esc(t.hook||"")}<br>理由:${esc(t.reason||"")}${t.evidence?`<br>热度证据:${esc(t.evidence)}`:""}</div></label>`).join("");},
  research(r){ const o=r.output;
    return `<div class="md"><p><b>摘要:</b>${esc(o.summary||"")}</p></div>
    <h3>核心事实</h3><ul class="list">${(o.facts||[]).map(f=>`<li>${esc(f)}</li>`).join("")}</ul>
    <h3>关键数据</h3><ul class="list">${(o.data_points||[]).map(f=>`<li>${esc(f)}</li>`).join("")}</ul>
    <h3>多方观点</h3><ul class="list">${(o.viewpoints||[]).map(f=>`<li>${esc(f)}</li>`).join("")}</ul>
    ${o.source_coverage?.length?`<h3>情报源覆盖</h3><div class="grid3" style="grid-template-columns:1fr 1fr">${o.source_coverage.map(c=>
      `<div class="topic" style="margin:0"><b>${esc(c.channel)}</b><div class="sub" style="margin-top:3px">${esc(c.got)}</div></div>`).join("")}</div>`:""}
    <h3>来源</h3><ul class="list">${(o.sources||[]).map(s=>{const u=safeExternalUrl(s.url);
      return `<li>${u?`<a href="${esc(u)}" target="_blank" rel="noopener noreferrer nofollow" referrerpolicy="no-referrer" style="text-decoration:underline">${esc(s.title||s.url)}</a>`:esc(s.title||s.url||"来源链接无效")}</li>`;}).join("")}</ul>`;},
  benchmark(r){ const o=r.output;
    return `<h3>对标爆款</h3>`+(o.benchmarks||[]).map(b=>`<div class="topic"><span class="t">${esc(b.title)}</span>
      <span class="tag">${esc(b.platform||"")}</span>${b.account?`<span class="tag">@${esc(b.account)}</span>`:""}
      <div class="sub" style="margin-top:5px">${b.dimensions?Object.entries(b.dimensions).map(([k,v])=>`<b>${esc(k)}</b>:${esc(v)}`).join("<br>")
        :`结构:${esc(b.structure||"")}<br>钩子:${esc(b.hook||"")}`}<br>为什么火:${esc(b.why_hot||"")}</div></div>`).join("")+
    `<h3>评论区洞察</h3><ul class="list">${(o.comment_insights||[]).map(f=>`<li>${esc(f)}</li>`).join("")}</ul>
    <h3>用户语言</h3>${(o.user_language||[]).map(f=>`<span class="tag">${esc(f)}</span>`).join("")}
    <h3>给撰稿人的建议</h3><ul class="list">${(o.takeaways||[]).map(f=>`<li>${esc(f)}</li>`).join("")}</ul>
    <div class="impl">V1 适配说明:联网检索近似替代 MediaCrawler 平台采集;商业授权闭环后切换真实爬取。</div>`;},
  draft(r,j){ return draftLike(r,j); },
  style(r,j){ return draftLike(r,j) + `<div class="notice green">🎭 文风一致性:${esc(r.output.consistency_note||"")}</div>`; },
  media(r){ const o=r.output, canPick=r.status==="awaiting_review", sel=o.selected??0;
    return `<h3>配图素材(${canPick?"点选主图后通过;全部素材都会进交付包":"已确认"})</h3><div class="grid3">`+
    (o.images||[]).map((im,i)=>{const file=safeAssetUrl(im.file); return `<label class="thumb ${sel===i?"sel":""}">
      ${canPick?`<input type="radio" name="pickone" value="${i}" ${i===sel?"checked":""} style="position:absolute;opacity:0" onchange="[...document.querySelectorAll('.thumb')].forEach(e=>e.classList.remove('sel'));this.closest('.thumb').classList.add('sel')">`:""}
      ${file?`<img src="${esc(file)}" loading="lazy">`:`<div style="padding:30px" class="sub">生成失败</div>`}
      <div class="cap"><b>${esc(im.slot||"")}</b> ${esc(im.desc||"")}
        ${file?`<a href="${esc(file)}" target="_blank" rel="noopener noreferrer" style="text-decoration:underline">大图</a> · <a href="${esc(file)}" download>⬇️ 下载</a>`:""}</div></label>`;}).join("")+`</div>
    <div class="impl">满意 → 点下面「✅ 通过」;不满意 → 「↩️ 打回重做」写上意见(比如"第2张换成实拍风"),TA 会重画。</div>`;},
  cover(r){ const o=r.output, canPick=r.status==="awaiting_review", sel=o.selected??0;
    return `<h3>封面卡片 1080×1440(${canPick?"挑一张当封面":"已选定"})</h3><div class="grid3">`+
    (o.covers||[]).map((c,i)=>{const file=safeAssetUrl(c.file); return `<label class="thumb ${sel===i?"sel":""}">
      ${canPick?`<input type="radio" name="pickone" value="${i}" ${i===sel?"checked":""} style="position:absolute;opacity:0" onchange="[...document.querySelectorAll('.thumb')].forEach(e=>e.classList.remove('sel'));this.closest('.thumb').classList.add('sel')">`:""}
      ${file?coverThumb(file):""}
      <div class="cap"><b>${esc(c.style||"")}${c.platform?` · ${esc(c.platform)}`:""}</b> ${file?`<a href="${esc(file)}" target="_blank" rel="noopener noreferrer" style="text-decoration:underline">大图</a> · <a href="${esc(file)}" download>⬇️</a>`:""}</div></label>`;}).join("")+`</div>`;},
  deck(r){ const o=r.output;
    const file=safeAssetUrl(o.file,"html");
    return `<p class="sub">${esc(o.summary||"")}</p>${file?`<div class="actions"><a class="btn pri" href="${esc(file)}" target="_blank" rel="noopener noreferrer">🖥️ 打开演绎稿</a></div>
    <iframe src="${esc(file)}" sandbox referrerpolicy="no-referrer" style="width:100%;height:520px;border:2.5px solid var(--ink);border-radius:14px;margin-top:12px"></iframe>`:""}`;},
  publish(r,j){ const o=r.output;
    return `<h3>各平台发布包</h3>`+(o.versions||[]).map((v,i)=>`<div class="topic">
      <div style="display:flex;align-items:center;gap:8px"><span class="tag">${esc(v.platform)}</span>
        <b style="flex:1">${esc(v.title)}</b>
        ${v.best_time?`<span class="sub">🕐 ${esc(v.best_time)}</span>`:""}
        <button class="btn sm" onclick="copyText(${cp((v.title||"")+"\n\n"+(v.body||"")+"\n\n"+(v.tags||[]).map(t=>"#"+t).join(" "))})">📋 复制全文</button></div>
      <div class="md" style="margin-top:8px;max-height:260px;overflow:auto;border:2px solid var(--line);border-radius:10px;padding:10px">${md(v.body)}</div>
      <div style="margin-top:6px">${(v.tags||[]).map(t=>`<span class="tag">#${esc(t)}</span>`).join("")}</div>
      ${v.checklist?.length?`<ul class="list" style="margin-top:6px">${v.checklist.map(c=>`<li>${esc(c)}</li>`).join("")}</ul>`:""}
      <div class="sub" style="margin-top:4px">⚠️ ${esc(v.note||"")}</div></div>`).join("")+
    `<div class="notice">📅 发布节奏:${esc(o.publish_plan||"")}</div>
    <div class="impl">终审通过后,交付包页面可按平台一键复制/下载 zip/直达各平台发布后台。</div>`;},
  retro(r){ const o=r.output;
    return `<div class="md">${md(o.report)}</div>
    <h3>下次选题建议(已回流选题库)</h3>`+(o.next_topics||[]).map(t=>`<div class="topic"><span class="t">${esc(t.title)}</span><div class="sub">${esc(t.reason||"")}</div></div>`).join("")+
    (o.profile_updates?.length?`<h3>建议写回人设档案</h3><ul class="list">${o.profile_updates.map(u=>`<li>${esc(u)}</li>`).join("")}</ul>`:"");},
};
function draftLike(r){
  const o=r.output, canPick=r.status==="awaiting_review", tsel=o.selected_title??0;
  let bodyHtml;
  if(EDIT_MODE){
    bodyHtml = `<textarea id="editBody" style="min-height:320px">${esc(o.body||"")}</textarea>
    <div class="sub" style="margin-top:4px">直接修改正文,点「✅ 通过」即以您改后的版本流转下游。</div>`;
  }else{
    bodyHtml = `<div class="md" style="border:2px solid var(--line);border-radius:12px;padding:14px;max-height:430px;overflow:auto;background:#fff">${md(o.body)}</div>`;
  }
  return `<h3>标题候选${canPick?"(选一个)":""}</h3>`+
    (o.title_candidates||[]).map((t,i)=>`<label style="display:block;margin:4px 0;font-weight:${i===tsel?800:400}">
      ${canPick?`<input type="radio" name="picktitle" value="${i}" ${i===tsel?"checked":""} style="width:auto;margin-right:8px">`:(i===tsel?"✅ ":"· ")}${esc(t)}</label>`).join("")+
    `<h3 style="display:flex;align-items:center;gap:10px">正文
      <button class="btn sm" style="font-weight:600" onclick="copyText(${cp(((o.title_candidates||[])[tsel]||"")+"\n\n"+(o.body||""))})">📋 复制这版</button></h3>${bodyHtml}`+
    (o.tags?`<div style="margin-top:8px">${o.tags.map(t=>`<span class="tag">#${esc(t)}</span>`).join("")}</div>`:"")+
    (o.image_plan?`<h3>配图点位建议</h3><ul class="list">${o.image_plan.map(p=>`<li><b>${esc(p.slot)}</b>:${esc(p.desc)}</li>`).join("")}</ul>`:"");
}
function coverThumb(file){
  const safe=safeAssetUrl(file);
  if(!safe) return "";
  return /\.(png|jpe?g|webp)$/i.test(safe)
    ? `<img src="${esc(safe)}" loading="lazy" style="width:100%;display:block">`
    : (safeAssetUrl(safe,"html")?`<div class="frame-wrap"><iframe src="${esc(safe)}" sandbox referrerpolicy="no-referrer" scrolling="no"></iframe></div>`:"");
}
function scaleFrames(){
  document.querySelectorAll(".frame-wrap").forEach(w=>{
    const f=w.querySelector("iframe"); if(!f)return;
    const s=w.clientWidth/1080; f.style.transform=`scale(${s})`;
  });
}
window.addEventListener("resize",scaleFrames);

/* ---------- 交付包 ---------- */
async function deliveryView(id){
  const [d, TVS, MX] = await Promise.all([api(`/jobs/${id}/delivery`),
    api(`/text-video?job_id=${id}`).catch(optionalResult([])),
    api("/matrix/accounts").catch(optionalResult({accounts:[]}))]);
  const cover = d.covers[d.cover_selected]||d.covers[0];
  $("#main").innerHTML = `<div class="card">
    <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
      <h2 style="flex:1;margin:0;min-width:min(100%,220px)">📦 交付包 · ${esc(d.title)}</h2>
      <a class="btn" href="#/job/${id}">← 回工单</a>
      <button class="btn pri" onclick="tvCreate(${id},this)">🎬 一键成片 3点</button>
      <a class="btn" href="/api/jobs/${id}/export.md" download>⬇️ 纯文本</a>
      <a class="btn" href="/api/jobs/${id}/export.pdf" download>⬇️ PDF</a>
      <a class="btn" href="/api/jobs/${id}/export.docx" download>⬇️ Word</a>
      ${d.packs?.length?`<a class="btn pri" href="/api/jobs/${id}/pack.zip" download>🚀 全平台发布包 zip</a>`
        :(d.images?.length?`<a class="btn" href="/api/jobs/${id}/pack.zip" download>🖼 正文+全部配图打包 zip</a>`:"")}</div>
    ${ME.role==="root"?`<div class="kv"><span>成本:${rmb(d.cost_usd)}</span><span>${((d.tokens||0)/1000).toFixed(1)}k tokens</span></div>`:""}</div>
  <div class="row" style="align-items:flex-start">
    <div class="card" style="flex:2;min-width:min(340px,100%)"><h3 style="margin-top:0">终稿正文</h3>
      <div class="actions" style="margin:0 0 10px"><button class="btn sm" onclick="copyText(${cp((d.title||"")+"\n\n"+(d.body||""))})">📋 复制正文</button></div>
      <div class="md">${md(d.body)}</div>
      <div style="margin-top:10px">${(d.tags||[]).map(t=>`<span class="tag">#${esc(t)}</span>`).join("")}</div></div>
    <div style="flex:1;min-width:260px">
      ${cover&&cover.file?`<div class="card"><h3 style="margin-top:0">封面</h3><div class="thumb sel">${coverThumb(cover.file)}</div></div>`:""}
      ${d.images?.length?`<div class="card"><h3 style="margin-top:0">配图素材</h3><div class="grid3" style="grid-template-columns:1fr 1fr">${d.images.map(im=>{const file=safeAssetUrl(im.file);return file?`<a class="thumb" href="${esc(file)}" target="_blank" rel="noopener noreferrer"><img src="${esc(file)}"></a>`:"";}).join("")}</div></div>`:""}
      ${safeAssetUrl(d.deck,"html")?`<div class="card"><h3 style="margin-top:0">演绎稿</h3><a class="btn" target="_blank" rel="noopener noreferrer" href="${esc(safeAssetUrl(d.deck,"html"))}">打开 HTML 演绎稿</a></div>`:""}
    </div></div>
  ${d.packs?.length?`<div class="card"><h3 style="margin-top:0">🚀 一键分发(${d.packs.length} 个平台)</h3>
    ${d.publish_plan?`<div class="notice">📅 ${esc(d.publish_plan)}</div>`:""}
    <div class="grid3" style="grid-template-columns:repeat(auto-fill,minmax(320px,1fr))">${d.packs.map(pk=>`
    <div class="topic" style="margin:0;display:flex;flex-direction:column;gap:8px">
      <div style="display:flex;align-items:center;gap:8px">
        <span style="font-size:22px">${pk.emoji||"📄"}</span><b style="flex:1">${esc(pk.platform)}</b>
        ${pk.best_time?`<span class="sub">🕐 ${esc(pk.best_time)}</span>`:""}</div>
      <div class="sub" style="font-weight:700">${esc(pk.title)}</div>
      ${safeAssetUrl(pk.cover?.file)?`<div class="sub">封面:${esc(pk.cover.size||pk.cover.style||"已配")} · <a href="${esc(safeAssetUrl(pk.cover.file))}" target="_blank" rel="noopener noreferrer" style="text-decoration:underline">查看</a></div>`:""}
      ${pk.checklist?.length?`<details><summary class="sub">📋 发布操作清单(${pk.checklist.length})</summary><ul class="list">${pk.checklist.map(c=>`<li>${esc(c)}</li>`).join("")}</ul></details>`:""}
      <div class="actions" style="margin:0">
        <button class="btn sm pri" onclick="copyText(${cp((pk.title||"")+"\n\n"+(pk.body||"")+"\n\n"+(pk.tags||[]).map(t=>"#"+t).join(" "))})">📋 复制全文</button>
        ${pk.platform==="公众号"?`<button class="btn sm pri" onclick="mpOpen(${id})">📰 排版·发草稿箱</button>`:""}
        ${pk.platform!=="公众号"?`<button class="btn sm pri" onclick="semiPub(${id},${cp(pk.platform)},${cp(pk.title||"")},${cp((pk.body||"")+"\n\n"+(pk.tags||[]).map(t=>"#"+t).join(" "))},${cp(pk.upload_url||"")})">🪄 半自动发布</button>`:""}
        ${pk.platform==="小红书"&&(MX.accounts||[]).some(a=>a.platform==="xhs")?`<button class="btn sm" onclick="mxQuickPub('xhs',${id},${cp(pk.title||"")},${cp(pk.body||"")})">🚀 全自动β</button>`:""}
        <button class="btn sm" onclick="censorQuick(${cp(pk.platform)},${cp(pk.title||"")},${cp(pk.body||"")})">🛡️ 审查</button>
        ${safeExternalUrl(pk.upload_url)?`<a class="btn sm" href="${esc(safeExternalUrl(pk.upload_url))}" target="_blank" rel="noopener noreferrer">↗ 去发布</a>`:""}
      </div>
      ${pk.note?`<div class="sub">⚠️ ${esc(pk.note)}</div>`:""}
    </div>`).join("")}</div>
    <div class="impl">复制全文 → 点「去发布」直达该平台后台粘贴;或下载 zip(每平台一个文件夹:正文/发布指南/封面/配图)。绑定平台 Cookie 后可升级为真实自动发布。</div></div>`
  :d.versions?.length?`<div class="card"><h3 style="margin-top:0">各平台版本</h3>${d.versions.map(v=>`<div class="topic">
    <span class="tag">${esc(v.platform)}</span> <b>${esc(v.title)}</b>
    <button class="btn sm" style="float:right" onclick="copyText(${cp((v.title||"")+"\n\n"+(v.body||"")+"\n\n"+(v.tags||[]).map(t=>"#"+t).join(" "))})">📋 复制</button></div>`).join("")}</div>`:""}
  <div class="card"><h3 style="margin-top:0">📷 真实素材图库(全网抓取)</h3>
    <div class="sub">觉得配图不够真实?搜真实图,点选即入库到本工单素材(公众号排版/发布包都会带上)。抓取图仅作素材参考,商用请确认版权。</div>
    <div class="row" style="align-items:flex-end;margin-top:8px">
      <div style="flex:1;min-width:220px"><input id="hunt-q" placeholder="画面关键词,如:火锅店 生意火爆 实拍" value="${esc((d.title||"").slice(0,16))}" onkeydown="if(event.key==='Enter')huntGo(${id})"></div>
      <button class="btn pri" onclick="huntGo(${id})">🔎 全网搜图</button></div>
    <div class="grid3" id="hunt-res" style="grid-template-columns:repeat(auto-fill,minmax(150px,1fr));margin-top:10px"></div></div>
  ${tvListCard(TVS, MX, id)}
  ${d.gate?`<div class="card"><h3 style="margin-top:0">质检报告</h3><div class="notice ${d.gate.passed?"green":"red"}">${esc(d.gate.report||"")}</div>
    ${(d.gate.issues||[]).map(i=>`<div class="sub">[${esc(i.severity)}] ${esc(i.type)}:${esc(i.detail)}</div>`).join("")}</div>`:""}
  ${d.retro?.report?`<div class="card"><h3 style="margin-top:0">复盘报告</h3><div class="md">${md(d.retro.report)}</div></div>`:""}
  <div class="card" style="background:#f4f9f4"><h3 style="margin-top:0">📊 发布一段时间后,把数据丢给审查官复盘</h3>
    <div class="sub">阅读/点赞/评论/涨粉数据(文字或截图)交给审查官:判读数据档次、体检是否被限流、回看合规、给下一步行动清单。</div>
    <div class="actions"><a class="btn" href="#/censor">🛡️ 去审查官那里复盘 →</a></div></div>`;
  scaleFrames();
  if(MP_AUTO!==null && +id===MP_AUTO){ MP_AUTO=null; mpOpen(+id); }  // 三件套向导直达排版
}

/* ---------- 人设档案 ---------- */
async function profilesView(){
  const ps = STATE.profiles;
  $("#main").innerHTML = `<div class="card"><h2>🎭 账号人设档案</h2>
    <div class="sub">定义"为谁生产"。人设会注入全流水线:选题偏好、撰稿、文风固定、视觉规范。</div>
    <div class="actions"><button class="btn pri" onclick="newProfile()">➕ 新建档案</button>
      ${isAdmin()?`<a class="btn" href="/api/records/export.xlsx?kind=profiles">⬇️ 导出全部档案(含语料)</a>`:""}</div></div>
  <div id="plist">${ps.map(profileCard).join("")||`<div class="empty">还没有人设档案</div>`}</div>`;
}
function profileCard(p){
  const s = p.persona||{};
  return `<div class="card">
    <div style="display:flex;gap:10px;align-items:center"><h3 style="margin:0;flex:1">${esc(p.name)}</h3>
      <button class="btn sm" onclick="editProfile(${p.id})">编辑</button>
      ${isAdmin()?`<button class="btn sm bad" onclick="profileDel(${p.id},${cp(p.name)})" title="移入回收站">🗑</button>`:""}</div>
    <div class="kv" style="margin-top:8px">
      <span>定位:${esc(s.positioning||"—")}</span><span>受众:${esc(s.audience||"—")}</span><span>语气:${esc(s.tone||"—")}</span></div>
    ${s.style_notes?`<div class="notice green" style="margin-top:8px">🧬 文风特征:${esc(s.style_notes)}<br>口头禅:${esc(s.catchphrases||"—")}</div>`:""}
  </div>`;
}
function profileForm(p){
  const s = (p&&p.persona)||{};
  return `<div class="card pform" style="max-width:780px">
    <h2>${p?"编辑":"新建"}人设档案</h2>
    <label>账号名称 *</label><input id="p-name" value="${esc(p?.name||"")}" placeholder="如:阿磊聊AI">
    <div class="row">
      <div><label>账号定位</label><input id="p-pos" value="${esc(s.positioning||"")}" placeholder="如:普通人视角的 AI 工具测评"></div>
      <div><label>目标受众</label><input id="p-aud" value="${esc(s.audience||"")}" placeholder="如:25-40岁想提效的职场人"></div></div>
    <div class="row">
      <div><label>语气风格</label><input id="p-tone" value="${esc(s.tone||"")}" placeholder="如:接地气、爱用比喻、偶尔自嘲"></div>
      <div><label>禁忌红线</label><input id="p-taboo" value="${esc(s.taboo||"")}" placeholder="如:不聊政治、不贬低同行、不承诺收益"></div></div>
    <label>视觉规范(封面偏好)</label><input id="p-visual" value="${esc(s.visual||"")}" placeholder="如:大字报风、主色橙色、每张带账号角标">
    <label>历史作品喂养(粘贴 5–20 篇代表作,用于提炼文风)</label>
    <div class="notice" style="margin:4px 0 8px">📁 有文件包?直接选文件(支持多选,<b>Word / PDF / Excel / PPT / 图片 / txt</b> 都能自动提取文字;各种记录、往期文案、语录都行)</div>
    <input type="file" multiple accept=".txt,.md,.markdown,.csv,.json,.log,.pdf,.docx,.xlsx,.pptx,.jpg,.jpeg,.png,.webp" onchange="loadProfileFiles(this)" style="margin-bottom:8px">
    <textarea id="p-corpus" style="min-height:160px" placeholder="把过去的爆款正文直接粘贴进来,多篇用 --- 分隔;或用上面的按钮选文件。注:提炼实际使用前 8000 字,日常写稿注入前 3000 字——放最能代表你风格的几篇即可,不必求多">${esc(s.corpus||"")}</textarea>
    <div class="actions">
      <button class="btn pri" onclick="saveProfile(${p?p.id:"null"})">💾 保存</button>
      ${p?`<button class="btn" onclick="distill(${p.id},this)">🧬 提炼文风特征</button>`
         :`<button class="btn" onclick="distill(null,this)">🧬 保存并提炼文风</button>`}
      <button class="btn" onclick="render()">取消</button></div></div>`;
}
async function loadProfileFiles(input){
  // 走后端 parse-file:Word/PDF/图片都能抽文字,GBK 编码的 txt 也不会乱码
  await parseFileInto(input, "#p-corpus");
  toast("记得点「💾 保存」,语料才会存进档案");
}
function closeProfileForms(){ document.querySelectorAll(".pform").forEach(f=>f.remove()); }
async function profileDel(id,name){
  if(!await uiConfirm(`把人设档案「${name}」移入回收站?\n历史作品语料会保留,可从回收站恢复。正在跑的工单不受影响。`,{
    title:"移入回收站",confirmText:"移入回收站"
  })) return;
  try{ await api("/profiles/"+id,{method:"DELETE"}); toast("已移入回收站"); SHELL_DIRTY=true; render(); }
  catch(e){ toast(e.message); }
}
function newProfile(){
  closeProfileForms(); $("#plist").insertAdjacentHTML("afterbegin", profileForm(null)); }
async function editProfile(id){
  closeProfileForms();
  try{
    const p=await api("/profiles/"+id);
    if(!$("#plist")) return;
    $("#plist").insertAdjacentHTML("afterbegin", profileForm(p)); window.scrollTo(0,0);
  }catch(e){ if(e?.name!=="NavigationAbort") toast(e.message); }
}
async function saveProfile(id){
  const persona = {positioning:$("#p-pos").value, audience:$("#p-aud").value, tone:$("#p-tone").value,
    taboo:$("#p-taboo").value, visual:$("#p-visual").value, corpus:$("#p-corpus").value};
  const old = id? (STATE.profiles.find(x=>x.id===id)?.persona||{}) : {};
  const name = $("#p-name").value.trim();
  if(!name) return toast("先给账号起个名字(第一格「账号名称」)");
  try{
    if(id) await api("/profiles/"+id,{method:"PUT",body:{name, persona:{...old,...persona}}});
    else await api("/profiles",{method:"POST",body:{name, persona}});
    toast("已保存"); render();
  }catch(e){ toast(e.message); }
}
async function distill(id, btn){
  // 先静默保存(不触发整页重绘——重绘会把本表单连同这颗按钮一起摘掉,
  // 老板会以为"刚粘的语料没了",一分钟里零反馈)
  const persona = {positioning:$("#p-pos").value, audience:$("#p-aud").value, tone:$("#p-tone").value,
    taboo:$("#p-taboo").value, visual:$("#p-visual").value, corpus:$("#p-corpus").value};
  const name = $("#p-name").value.trim();
  if(!name) return toast("先给账号起个名字(第一格「账号名称」)");
  try{
    if(id){ const old=(STATE.profiles.find(x=>x.id===id)?.persona||{});
      await api("/profiles/"+id,{method:"PUT",body:{name, persona:{...old,...persona}}}); }
    else{ const r=await api("/profiles",{method:"POST",body:{name, persona}}); id=r.id||id; }
  }catch(e){ return toast("先保存失败:"+e.message); }
  btn.disabled=true; btn.innerHTML=`<span class="spin"></span> 提炼中(约1分钟)…`;
  try{ await api(`/profiles/${id}/distill`,{method:"POST",timeout:330000,longRunning:true}); toast("文风特征已写入档案"); render(); }
  catch(e){ toast(e.message); btn.disabled=false; btn.textContent="🧬 提炼文风特征"; }
}

/* ---------- 资产库(V5:多维表格 + 专家报告) ---------- */
let ASSET_TAB = "topic", AFILTER = {platform:"", category:""}, ASSET_Q = "";
function setAF(k,v){ AFILTER[k]=v; resetListPage("assets"); render(); }
async function assetsView(){
  const assetContract=normalizeListContract(await api(listPath("/assets","assets",{
    type:ASSET_TAB,platform:AFILTER.platform,category:AFILTER.category,q:ASSET_Q})),LIST_PAGE_SIZE);
  const rows=assetContract.items;
  const shown = rows;
  const action = a => ASSET_TAB==="topic"
    ? `<button class="btn sm" onclick="fromTopic(${a.id})">🚀 发起工单</button>`
    : ASSET_TAB==="final" ? `<a class="btn sm" href="#/delivery/${a.job_id}">交付包</a>`
    : `<button class="btn sm" onclick="openReport(${a.payload.task_id})">📄 看报告</button>`;
  const title = a => ASSET_TAB==="report"
    ? `<b>${esc(a.payload.title||"")}</b><div class="sub" style="font-size:11px">${esc(a.payload.emp||"")} · ${esc(a.payload.group||"")}</div>`
    : `<b>${esc(a.payload.title||"")}</b><div class="sub" style="font-size:11px">${esc(a.payload.angle||a.payload.brief||"")}</div>`;
  $("#main").innerHTML = `<div class="card"><h2>🗂️ 资产库</h2>
    <div class="sub">所有产出资产,每条自动做 <b>11 维评估</b>,可按平台/类别筛选。</div>
    ${listContractNotice(assetContract,"资产")}
    <div class="tabs">
      <span class="tb ${ASSET_TAB==="topic"?"on":""}" onclick="ASSET_TAB='topic';AFILTER={platform:'',category:''};resetListPage('assets');render()">💡 选题库</span>
      <span class="tb ${ASSET_TAB==="final"?"on":""}" onclick="ASSET_TAB='final';AFILTER={platform:'',category:''};resetListPage('assets');render()">📦 成品库</span>
      <span class="tb ${ASSET_TAB==="report"?"on":""}" onclick="ASSET_TAB='report';AFILTER={platform:'',category:''};resetListPage('assets');render()">📄 专家报告</span>
      <span style="flex:1"></span>
      <input id="as-q" placeholder="🔍 搜标题" value="${esc(ASSET_Q||"")}" style="max-width:170px"
        onkeydown="if(event.key==='Enter'){ASSET_Q=this.value.trim();resetListPage('assets');render();}">
      <button class="btn sm" onclick="feishuSync('asset',this)">📤 同步到飞书</button>
      <a class="btn sm" href="/api/library/export.xlsx?kind=asset">⬇️ Excel</a></div>
    <div id="fs-box"></div>
    ${dimFilters(rows, AFILTER, "setAF", assetContract.facets)}
    ${ASSET_TAB==="report" ? (shown.length?`<div class="grid3" style="grid-template-columns:repeat(auto-fill,minmax(300px,1fr))">
      ${shown.map(a=>`<div class="topic" style="margin:0;display:flex;flex-direction:column;gap:6px">
        <b>${esc(a.payload.title||"(未命名报告)")}</b>
        <div class="sub">${esc(a.payload.emp||"")}${a.payload.group?` · ${esc(a.payload.group)}`:""} · ${new Date((a.created_at||0)*1000).toLocaleDateString("zh-CN")}</div>
        ${a.meta?.summary?`<div class="sub">${esc((a.meta.summary||"").slice(0,64))}</div>`:""}
        <div>${a.meta?.quality?`<span class="tag">质量 ${esc(String(a.meta.quality))}</span>`:""}
          ${a.meta?.platform?`<span class="tag">${esc(a.meta.platform)}</span>`:""}
          ${a.meta?.category?`<span class="tag">${esc(a.meta.category)}</span>`:""}</div>
        <div class="actions" style="margin:0">
          <button class="btn sm pri" onclick="openReport(${a.payload.task_id||0})">📄 看报告</button>
          <button class="btn sm" onclick="reAnalyze('assets',${a.id},this)">🔁 重评</button>
          ${isAdmin()?`<button class="btn sm bad" onclick="assetDel(${a.id})" title="移入回收站">🗑</button>`:""}</div>
      </div>`).join("")}</div>`
      :`<div class="empty">空的。行业专家(如餐饮部)交付的报告会沉淀到这里;工具箱的报告在工具箱当页看,点「💾 沉淀」的会进沉淀库。</div>`)
    : shown.length?`<div class="dimwrap"><table class="dimtable"><thead><tr>
      <th>标题</th><th>类别</th><th>平台</th><th>行业</th><th>主题</th><th>关键词</th>
      <th>质量</th><th>匹配</th><th>复用</th><th>时效</th><th>情绪</th><th>摘要</th><th>操作</th>
    </tr></thead><tbody>${shown.map(a=>`<tr>
      <td style="min-width:150px">${title(a)}</td>
      ${metaCells(a.meta)}
      <td style="white-space:nowrap">${action(a)}
        <button class="btn sm" onclick="reAnalyze('assets',${a.id},this)" title="重新评估">🔁</button>
        ${isAdmin()?`<button class="btn sm bad" onclick="assetDel(${a.id})" title="移入回收站">🗑</button>`:""}</td>
    </tr>`).join("")}</tbody></table></div>`
    :`<div class="empty">空的。${{topic:"趋势官没被选中的选题会自动沉淀到这里",final:"完成的工单会沉淀到这里",report:"行业专家(如餐饮部)交付的报告会沉淀到这里"}[ASSET_TAB]}</div>`}
    ${listPager(assetContract,"assets")}
  </div>`;
}
async function openReport(tid){
  if(!tid) return toast("这条记录没有关联报告(旧数据),可删除");
  const t = await api("/tasks/"+tid);
  document.body.insertAdjacentHTML("beforeend", `<div class="overlay" onclick="if(event.target===this)this.remove()">
    <div class="panel"><div class="phead"><b style="flex:1;font-size:17px">📄 ${esc(t.emp_name)} 的交付</b>
      <button class="btn sm" onclick="copyText(${cp(t.output_md||"")})">📋 复制</button>
      <a class="btn sm" href="/api/tasks/${t.id}/export.pdf">PDF</a>
      <a class="btn sm" href="/api/tasks/${t.id}/export.docx">Word</a>
      <button class="btn sm" onclick="taskToKnow(${t.id})">📚 存沉淀</button>
      <button class="btn sm" onclick="this.closest('.overlay').remove()">✕</button></div>
    <div class="pbody">${taskBody(t)}</div></div></div>`);
}
async function assetDel(id){
  if(!await uiConfirm("把这条资产移入回收站? 之后可以恢复;关联工单的交付物不受影响。",{
    title:"移入回收站",confirmText:"移入回收站"
  })) return;
  try{ await api("/assets/"+id,{method:"DELETE"}); toast("已移入回收站"); render(); }
  catch(e){ toast(e.message); }
}
async function fromTopic(aid){
  const a = await api("/assets/"+aid).catch(e=>{toast(e.message);return null;});
  if(!a) return;
  location.hash = "#/new";
  setTimeout(()=>{ $("#b-dir").value = a.payload.title + "。切入角度:" + (a.payload.angle||""); },300);
}

/* ---------- V4/V5:沉淀库(多维表格) ---------- */
let KFILTER = {platform:"", category:""};
const scoreCell = v => {
  const n = Number(v);
  if(!Number.isFinite(n)) return `<span class="sub">…</span>`;
  const safe = Math.max(0, Math.min(100, Math.round(n)));
  return `<span class="score ${safe>=80?"hi":safe>=60?"mid":"lo"}">${safe}</span>`;
};
function dimFilters(rows, filter, fnName, facets={}){
  const plats = facets.platforms||[...new Set(rows.map(r=>r.meta?.platform).filter(Boolean))];
  const cats = facets.categories||[...new Set(rows.map(r=>r.meta?.category).filter(Boolean))];
  return `<div class="chips" style="margin:8px 0">
    <span class="chip${!filter.platform?" on":""}" onclick="${fnName}('platform','')">全部平台</span>
    ${plats.map(p=>`<span class="chip${filter.platform===p?" on":""}" onclick="${fnName}('platform',${cp(p)})">${esc(p)}</span>`).join("")}
    <span style="width:12px"></span>
    <span class="chip${!filter.category?" on":""}" onclick="${fnName}('category','')">全部类别</span>
    ${cats.map(c=>`<span class="chip${filter.category===c?" on":""}" onclick="${fnName}('category',${cp(c)})">${esc(c)}</span>`).join("")}
  </div>`;
}
function setKF(k,v){ KFILTER[k]=v; resetListPage("knowledge"); render(); }
function metaCells(m){
  return `<td>${esc(m?.category||"…")}</td><td>${esc(m?.platform||"…")}</td>
    <td>${esc(m?.industry||"…")}</td><td>${esc(m?.theme||"…")}</td>
    <td>${(m?.keywords||[]).map(k=>`<span class="tag">${esc(k)}</span>`).join(" ")||"…"}</td>
    <td>${scoreCell(m?.quality)}</td><td>${scoreCell(m?.match)}</td><td>${scoreCell(m?.reuse)}</td>
    <td>${esc(m?.timeliness||"…")}</td><td>${esc(m?.sentiment||"…")}</td>
    <td class="sub">${esc(m?.summary||"评估中…")}</td>`;
}
async function productionView(){
  const d = await api("/boss/production");
  const t = d.total;
  const fmtDate = ts => ts? new Date(ts*1000).toLocaleDateString("zh-CN",{month:"numeric",day:"numeric"}) : "—";
  const ktok = n => n>=10000? (n/10000).toFixed(1)+"万" : String(n||0);
  $("#main").innerHTML = `<div class="card"><h2>📊 员工产出总览</h2>
    <div class="sub">您手下每个数字员工干了多少活、产出什么、烧了多少 token 和钱,一目了然。点某一行看 TA 的逐条产出。</div>
    <div style="display:flex;gap:12px;flex-wrap:wrap;margin:12px 0">
      ${[["在产员工",t.employees],["总产出",t.runs],["总 tokens",ktok(t.tokens)],["总花费",rmb(t.cost_usd)]]
        .map(([l,v])=>`<div style="flex:1;min-width:110px;background:#fff6dc;border:2px solid var(--ink);border-radius:12px;padding:10px 14px">
          <div style="font-size:22px;font-weight:800">${v}</div><div class="sub">${l}</div></div>`).join("")}
    </div>
    ${d.employees.length?`<div class="dimwrap"><table class="dimtable">
      <thead><tr><th>员工</th><th>部门</th><th>产出数</th><th>tokens</th><th>费用</th><th>最近</th><th></th></tr></thead>
      <tbody>${d.employees.map(e=>`<tr>
        <td><b>${esc(e.name)}</b></td><td class="sub">${esc(e.dept)}</td>
        <td>${e.runs}</td><td>${ktok(e.tokens)}</td><td><b>${rmb(e.cost_usd)}</b></td>
        <td class="sub">${fmtDate(e.last_at)}</td>
        <td><button class="btn sm" onclick="prodDetail(${e.idx},this)">看产出</button></td>
      </tr><tr id="pd-${e.idx}" style="display:none"><td colspan="7" style="background:#faf6ec"></td></tr>`).join("")}</tbody>
    </table></div>`:`<div class="empty">还没有产出记录。派活给员工、或跑一单内容,这里就会有数据。</div>`}
  </div>`;
}
async function prodDetail(idx,btn){
  const row = $("#pd-"+idx); const cell = row.firstElementChild;
  if(row.style.display!=="none"){ row.style.display="none"; btn.textContent="看产出"; return; }
  btn.disabled=true; btn.innerHTML='<span class="spin"></span>';
  try{
    const d = await api("/boss/production/"+idx);
    cell.innerHTML = d.items.length? d.items.map(it=>`<div style="padding:8px 4px;border-bottom:1px solid #eadfc4">
      <div style="display:flex;gap:10px;align-items:baseline;flex-wrap:wrap">
        <b>${it.kind==="task"?"🎯":"🎬"} ${esc(it.title)}</b>
        <span class="sub">${it.tokens} tok · <b>${rmb(it.cost_usd)}</b> · ${new Date(it.at*1000).toLocaleString("zh-CN",{month:"numeric",day:"numeric",hour:"2-digit",minute:"2-digit"})}</span>
        ${it.kind==="task"?`<a class="sub" style="text-decoration:underline" href="#/">🎯专家任务</a>`:`<a class="sub" style="text-decoration:underline" href="#/delivery/${it.job_id}">看交付 →</a>`}
      </div>
      <div class="sub" style="margin-top:3px;white-space:pre-wrap;max-height:66px;overflow:hidden">${esc(it.preview)}</div>
    </div>`).join("") : `<div class="sub">暂无产出</div>`;
    row.style.display=""; btn.textContent="收起";
  }catch(e){ toast(e.message); } btn.disabled=false; if(btn.textContent.includes("spin")) btn.textContent="看产出";
}
async function companyView(){
  const c = await api("/company");
  const p = c.profile || {};
  const f = (k,label,ph)=>`<label>${esc(label)}</label><input id="cp-${k}" value="${esc(p[k]||"")}" placeholder="${esc(ph)}">`;
  $("#main").innerHTML = `<div class="card"><h2>🏢 企业档案</h2>
    <div class="sub">把企业介绍/品牌手册/产品说明/话术规范粘进来,点「提炼并同步」——AI 会压成一份固定档案,<b>自动注入每一个数字员工</b>(内容工位 / 行业专家 / 圆桌会议),让他们产出更懂你的企业、更贴品牌调性、不踩表达禁忌。也会自动带上沉淀库里的企业知识。</div>
    ${c.injected?(c.filled>=(c.total_fields||7)
      ?`<div class="notice" style="background:#e7f6ec;border-color:#8fd3a6">✅ 企业档案已生效(7/7 项齐全),正注入全部数字员工</div>`
      :`<div class="notice" style="background:#fff3d6">🟡 企业档案部分生效:已填 ${c.filled}/${c.total_fields||7} 项。员工只知道已填的部分——<b>空着的字段(如调性/禁忌)不会凭空生效</b>,建议补全后重新保存。</div>`)
      :`<div class="notice">⚠️ 还没生成档案,员工暂时不了解你的企业。填资料 → 点提炼即可。</div>`}
    <label>企业资料(可直接粘贴,或上传文档;最多 2 万字)</label>
    <textarea id="cp-materials" style="min-height:150px" placeholder="例:我们是「川小福」麻辣烫连锁,主打一人食平价麻辣烫,人均25元…目标客户…品牌调性…核心卖点…表达禁忌…slogan…">${esc(c.materials||"")}</textarea>
    <div class="actions">
      <label class="btn" style="cursor:pointer">📎 上传文档(可多选)
        <input type="file" multiple accept=".txt,.md,.csv,.json,.pdf,.docx,.xlsx,.pptx,.jpg,.jpeg,.png,.webp" style="display:none" onchange="companyUpload(this)"></label>
      <button class="btn" onclick="companySaveMat(this)">💾 只存资料</button>
      <button class="btn pri" onclick="companyDistill(this)">✨ 提炼并同步给全部员工</button>
      ${c.has_prev?`<button class="btn" onclick="companyUndo(this)" title="换回上次提炼前的那一版档案">↩️ 撤销上次提炼</button>`:""}</div>
    <div class="sub" style="margin-top:-4px">支持 Word / PDF / Excel / PPT / 图片 / txt——上传后会自动提取文字并追加到上面,再点提炼。</div>
    <h3 style="margin:20px 0 4px">📇 提炼出的企业档案(注入员工的"固定参数",可手动微调)</h3>
    <div class="sub">这 7 项就是每个员工每次开工都会先读到的企业信息。可直接改,改完点保存。</div>
    ${f("brand","品牌 / 企业名","如:川小福")}
    ${f("business","主营业务","一句话说清:卖什么给谁")}
    ${f("audience","目标客群","如:20-35岁上班族与学生")}
    ${f("tone","品牌调性 / 说话风格","如:年轻、亲切、接地气、有烟火气")}
    ${f("selling_points","核心卖点","3-5个,顿号分隔")}
    ${f("taboo","表达禁忌","不能说什么 / 避免的调性")}
    ${f("keywords","常用话术 / 关键词 / slogan","顿号分隔")}
    <div class="actions"><button class="btn pri" onclick="companySaveProfile(this)">💾 保存档案</button></div>
  </div>`;
}
async function companySaveMaterials(){
  const r = await api("/company",{method:"PUT",body:{materials:$("#cp-materials").value}});
  if(r?.materials_truncated) toast(`⚠️ 资料超过 2 万字上限,只保存了前 ${r.materials_saved_chars} 字,超出部分没有存——建议删掉过时内容再粘新的`);
  return r;
}
async function companyUpload(input){
  await parseFileInto(input, "#cp-materials");   // 提取多格式文档文字→追加到资料框
  try{ const r=await companySaveMaterials();
    if(!r?.materials_truncated) toast("文档已读入并保存,点「提炼」即可同步给员工"); }
  catch(e){ toast(e.message); }
}
async function companySaveMat(btn){
  btn.disabled=true;
  try{ const r=await companySaveMaterials();
    if(!r?.materials_truncated) toast("已保存企业资料"); }
  catch(e){ toast(e.message); } btn.disabled=false;
}
async function companyDistill(btn){
  // 先把 7 个字段当前值保存(此前只传素材,老板刚改的字段既没保存又会被覆盖);
  // 再明确告知提炼会重写全部字段。
  if(!await uiConfirm("提炼会依据素材重写全部 7 个字段,覆盖您手动修改的内容。已自动先保存当前填写。继续?",{okText:"✨ 继续提炼",okClass:"pri"})) return;
  try{ await companySaveProfile(null,{silent:true}); }catch(_){}
  btn.disabled=true; const t=btn.textContent; btn.innerHTML='<span class="spin"></span> 提炼中(约1分钟)…';
  try{
    await companySaveMaterials();
    await api("/company/distill",{method:"POST",timeout:330000,longRunning:true});
    toast("已提炼并同步给全部员工"); render();
  }catch(e){ toast(e.message); btn.disabled=false; btn.textContent=t; }
}
async function companyUndo(btn){
  if(!await uiConfirm("换回上次提炼前的那一版档案?当前这版会成为可再次撤销的备份(两版互换)。",{
    okText:"↩️ 撤销",okClass:"pri"})) return;
  btn.disabled=true;
  try{ await api("/company/restore-prev",{method:"POST"}); toast("已换回上一版档案"); render(); }
  catch(e){ toast(e.message); btn.disabled=false; }
}
async function companySaveProfile(btn, opts){
  if(btn) btn.disabled=true;
  const profile={}; ["brand","business","audience","tone","selling_points","taboo","keywords"]
    .forEach(k=>{ const el=$("#cp-"+k); if(el) profile[k]=el.value; });
  try{ await api("/company",{method:"PUT",body:{profile}});
    if(!opts?.silent){ toast("企业档案已保存"); render(); } }
  catch(e){ if(opts?.silent) throw e; toast(e.message); if(btn) btn.disabled=false; }
}
let KB_Q="";
async function knowledgeView(){
  const knowledgeContract=normalizeListContract(await api(listPath("/knowledge","knowledge",{
    platform:KFILTER.platform,category:KFILTER.category,q:KB_Q})),LIST_PAGE_SIZE);
  const rows=knowledgeContract.items;
  KNOW_CACHE = rows;
  const shown = rows;
  $("#main").innerHTML = `<div class="card"><h2>📚 公司沉淀库</h2>
    <div class="sub">交付自动沉淀 + 老板手记。每条自动做 <b>11 维评估</b>(类别/平台/行业/主题/关键词/质量/匹配/复用/时效/情绪/摘要);📌 置顶的会注入员工每次工作(<b>每次最多带 12 条、每条取前 800 字</b>,置顶优先)。</div>
    ${(n=>n>12?`<div class="notice red" style="margin-top:8px">📌 已置顶 ${n} 条,超过单次注入上限 <b>12 条</b>:每次开工只有最新置顶的 12 条会带上,建议把最关键的留在置顶、其余取消。</div>`:"")(rows.filter(k=>k.pinned).length)}
    ${listContractNotice(knowledgeContract,"沉淀")}
    <div class="actions"><button class="btn pri" onclick="knowForm()">➕ 手记一条</button>
      <button class="btn" onclick="feishuSync('knowledge',this)">📤 同步到飞书</button>
      <input id="kb-q" placeholder="🔍 搜标题/标签/来源" value="${esc(KB_Q||"")}" style="max-width:220px" onkeydown="if(event.key==='Enter'){KB_Q=this.value.trim();render();}">
      <a class="btn" href="/api/library/export.xlsx?kind=knowledge">⬇️ 导出Excel</a>
      <button class="btn sm" onclick="feishuConfig()">🔗 飞书配置</button></div>
    <div id="fs-box"></div>
    <div id="kform"></div>
    ${dimFilters(rows, KFILTER, "setKF", knowledgeContract.facets)}
    ${shown.length?`<div class="dimwrap"><table class="dimtable"><thead><tr>
      <th>标题</th><th>类别</th><th>平台</th><th>行业</th><th>主题</th><th>关键词</th>
      <th>质量</th><th>匹配</th><th>复用</th><th>时效</th><th>情绪</th><th>摘要</th><th>来源</th><th>操作</th>
    </tr></thead><tbody>${shown.map(k=>`<tr>
      <td style="min-width:150px"><b style="cursor:pointer;text-decoration:underline dotted" onclick="kbOpen(${k.id})">${k.pinned?"📌 ":""}${esc(k.title)}</b>
        <div class="sub" style="font-size:11px">${new Date(k.created_at*1000).toLocaleDateString("zh-CN")}</div></td>
      ${metaCells(k.meta)}
      <td>${k.source==="auto"?"🤖 自动":"✍️ 手记"}${k.job_id?`<br><a href="#/delivery/${k.job_id}" style="text-decoration:underline">看交付</a>`:""}</td>
      <td style="white-space:nowrap">
        <button class="btn sm pri" onclick="kbOpen(${k.id})">📄 看原件</button>
        <button class="btn sm" onclick="knowPin(${k.id},${k.pinned?0:1})" title="置顶注入员工工作">${k.pinned?"取消置顶":"📌"}</button>
        <button class="btn sm" onclick="knowForm(${k.id})">✏️</button>
        <button class="btn sm" onclick="reAnalyze('knowledge',${k.id},this)" title="重新评估">🔁</button>
        ${isAdmin()?`<button class="btn sm bad" onclick="knowDel(${k.id})" title="移入回收站">🗑</button>`:""}</td>
    </tr>`).join("")}</tbody></table></div>`
    :`<div class="empty">还没有沉淀。完成第一单交付后复盘官会自动写入;也可以点上面手记。</div>`}
    ${listPager(knowledgeContract,"knowledge")}</div>`;
}
async function reAnalyze(kind,id,btn){
  btn.disabled=true; btn.innerHTML=`<span class="spin"></span>`;
  try{ await api(`/${kind==="knowledge"?"knowledge":"assets"}/${id}/analyze`,{method:"POST"});
    toast("已重新评估"); render(); }
  catch(e){ toast(e.message); btn.disabled=false; btn.textContent="🔁"; }
}
async function kbOpen(id){
  let k;
  try{ k = await api("/knowledge/"+id); }
  catch(e){
    if(e?.name==="NavigationAbort") return;
    toast(e?.status===404?"这条沉淀已被删除":e.message);
    return;
  }
  document.body.insertAdjacentHTML("beforeend", `<div class="overlay" id="kb-ov" onclick="if(event.target===this)this.remove()">
    <div class="panel" style="max-width:820px;width:95vw;padding:0;overflow:hidden">
      <div style="display:flex;gap:10px;align-items:center;padding:14px 20px;border-bottom:2px solid var(--line);background:#fff6dc">
        <h2 style="flex:1;margin:0;min-width:min(100%,220px)">📄 ${esc(k.title)}</h2>
        <button class="btn sm" onclick="copyText(${cp(k.content||"")})">📋 复制全文</button>
        <button class="btn sm" onclick="knowForm(${k.id});$('#kb-ov').remove();window.scrollTo(0,0)">✏️ 编辑</button>
        <button class="btn sm" onclick="$('#kb-ov').remove()">✕</button></div>
      <div class="sub" style="padding:8px 20px 0">${k.source==="auto"?"🤖 自动沉淀":"✍️ 老板手记"} · ${new Date(k.created_at*1000).toLocaleString("zh-CN")}${k.meta?.summary?` · ${esc(k.meta.summary)}`:""}</div>
      <div class="md" style="padding:14px 24px 24px;max-height:70vh;overflow:auto;font-size:14.5px;line-height:1.85">${md(k.content||"")}</div>
    </div></div>`);
}
let KNOW_CACHE = null;
async function knowForm(id){
  let k = null;
  if(id){
    try{ k = await api("/knowledge/"+id); }
    catch(e){
      if(e?.name==="NavigationAbort") return;
      toast(e?.status===404?"这条沉淀已被删除":e.message);
      return;
    }
  }
  $("#kform").innerHTML = `<div class="card" style="background:#fff6dc">
    <h3 style="margin-top:0">${k?"编辑沉淀":"手记一条沉淀"}</h3>
    <label>标题 *</label><input id="k-title" value="${esc(k?.title||"")}" placeholder="如:小红书晚8点发互动最高">
    <label>内容</label><textarea id="k-content" style="min-height:100px" placeholder="经验的具体内容,员工工作时会参考">${esc(k?.content||"")}</textarea>
    <label style="display:flex;align-items:center"><input type="checkbox" id="k-pin" style="width:auto;margin-right:8px" ${k?.pinned?"checked":""}>📌 置顶(优先注入员工工作)</label>
    <div class="actions"><button class="btn pri" onclick="knowSave(${k?k.id:"null"})">💾 保存</button>
      <button class="btn" onclick="$('#kform').innerHTML=''">取消</button></div></div>`;
  window.scrollTo(0,0);
}
async function knowSave(id){
  const body = {title:$("#k-title").value.trim(), content:$("#k-content").value.trim(), pinned:$("#k-pin").checked};
  if(!body.title) return toast("标题必填");
  try{
    if(id) await api("/knowledge/"+id,{method:"PUT",body});
    else await api("/knowledge",{method:"POST",body});
    toast("已保存"); render();
  }catch(e){ toast(e.message); }
}
async function knowPin(id,pinned){
  try{
    await api("/knowledge/"+id,{method:"PUT",body:{pinned:!!pinned}});
    if(pinned) toast("📌 已置顶。员工每次开工最多带 12 条置顶知识(每条取前 800 字)");
    render();
  }catch(e){ toast(e.message); }
}
async function knowDel(id){
  if(!await uiConfirm("把这条沉淀移入回收站? 之后可以恢复。",{
    title:"移入回收站",confirmText:"移入回收站"
  })) return;
  try{ await api("/knowledge/"+id,{method:"DELETE"}); toast("已移入回收站"); render(); }catch(e){ toast(e.message); }
}

/* ---------- V4:定时任务 ---------- */
const WD = ["周一","周二","周三","周四","周五","周六","周日"];
let SCHED_ROWS=[];
async function schedulesView(selectedId){
  const rows = await api("/schedules");
  SCHED_ROWS = rows;
  $("#main").innerHTML = `<div class="card"><h2>⏰ 定时任务</h2>
    <div class="sub">设好节奏,内容部到点自动开工(北京时间)。比如「每天 09:00 来一单日更选题」。到点若 3 单并行已满会自动顺延,不硬塞。</div>
    <div class="actions"><button class="btn pri" onclick="schedForm()">➕ 新建定时任务</button></div>
    <div id="sform"></div>
    ${rows.length?rows.map(s=>schedRow(s,+selectedId===s.id)).join(""):`<div class="empty">还没有定时任务</div>`}</div>`;
  if(selectedId) requestAnimationFrame(()=>document.getElementById("schedule-"+selectedId)?.scrollIntoView({block:"center",behavior:"smooth"}));
}
function schedRow(s,selected=false){
  const nxt = s.next_run_at? new Date(s.next_run_at*1000).toLocaleString("zh-CN",{month:"numeric",day:"numeric",hour:"2-digit",minute:"2-digit"}) : "—";
  return `<div id="schedule-${s.id}" class="topic ${s.enabled?"":""}" style="${s.enabled?"":"opacity:.55;"}${selected?"border-color:#111;background:#fff1bd;box-shadow:4px 4px 0 #33291f55":""}">
    <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
      <span class="switch ${s.enabled?"on":""}" title="启用/停用" onclick="schedToggle(${s.id},${s.enabled?0:1})"></span>
      <span class="t" style="flex:1">${esc(s.name)}</span>
      <span class="tag">🔁 ${esc(s.human)}</span>
      <span class="tag">⏭ 下次 ${s.enabled?nxt:"已停用"}</span>
      ${s.last_run_at?`<span class="tag" title="上次实际开工时间">🕘 上次 ${tcFmt(s.last_run_at)}</span>`:""}
      <button class="btn sm" onclick="schedEdit(${s.id})">✏️ 编辑</button>
      <button class="btn sm" onclick="schedRunNow(${s.id})">▶️ 立即来一单</button>
      <button class="btn sm bad" onclick="schedDel(${s.id})">🗑</button></div>
    <div class="sub" style="margin-top:6px">方向:${esc(s.brief.direction||"")} · ${(s.brief.platforms||[]).map(esc).join("/")} · ${esc(MODE_LABEL[s.mode]||s.mode)}</div>
    ${s.last_note?`<div class="sub" style="margin-top:3px">📝 ${esc(s.last_note).replace(/工单 #(\d+)/,'工单 <a href="#/job/$1" style="text-decoration:underline;font-weight:800">#$1</a>')}</div>`:""}
  </div>`;
}
function schedForm(s){
  const profiles = STATE.profiles;
  const b = s?.brief||{};
  const indOn = t => s? t===(b.industry||"") : false;
  const anyInd = s && META.industries.some(indOn);
  const tplOn = t => s? t===(b.template||"") : false;
  const anyTpl = s && META.brief_templates.some(tplOn);
  const pfOn = p => s? (b.platforms||[]).includes(p) : false;
  const anyPf = s && META.platforms.some(pfOn);
  $("#sform").innerHTML = `<div class="card" style="background:#fff6dc">
    <h3 style="margin-top:0">${s?`✏️ 编辑定时任务 #${s.id}`:"新建定时任务"}</h3>
    ${s?"":`<div class="notice" style="margin-top:6px">💡 <b>定好主题,以后每天全自动</b>:到点自动走完整条流水线(<b>每次自动开工扣 ${META?.job_points??18}点</b>,点数不足自动暂停)。建议先跑两单手动任务,满意了再定时。</div>`}
    <label>任务名</label><input id="s-name" value="${esc(s?.name||"")}" placeholder="如:每日行业选题">
    <label>行业/赛道</label>
    <div class="chips" id="s-ind">${META.industries.map((t,i)=>`<span class="chip${(anyInd?indOn(t):i===0)?" on":""}" onclick="pick(this)">${t}</span>`).join("")}</div>
    <label>主题(每天围绕它自动拆解出当天的选题) *</label><textarea id="s-dir" placeholder="如:本行业今天值得聊的新动态,选和普通人最相关的一条">${esc(b.direction||"")}</textarea>
    <label>内容类型</label><div class="chips" id="s-tpl">${META.brief_templates.map((t,i)=>`<span class="chip${(anyTpl?tplOn(t):i===1)?" on":""}" onclick="pick(this)">${t}</span>`).join("")}</div>
    <label>目标平台(多选)</label><div class="chips" id="s-pf">${META.platforms.map((p,i)=>`<span class="chip${(anyPf?pfOn(p):i===0)?" on":""}" data-p="${p}" onclick="this.classList.toggle('on')">${p}</span>`).join("")}</div>
    <div class="row">
      <div><label>频率</label><select id="s-kind" onchange="schedKindUI()">
        ${[["daily","每天一次"],["weekly","每周一次"],["interval","每 N 小时"]].map(([v,t])=>`<option value="${v}"${s?.kind===v?" selected":""}>${t}</option>`).join("")}</select></div>
      <div id="s-when"><label>时间(北京时间)</label><input id="s-time" type="time" value="09:00"></div>
    </div>
    <div class="sub" id="s-cost" style="margin-top:2px"></div>
    <div class="row">
      <div><label>账号人设</label><select id="s-profile"><option value="">(不使用)</option>
        ${profiles.map(p=>`<option value="${p.id}"${s?.profile_id===p.id?" selected":""}>${esc(p.name)}</option>`).join("")}</select></div>
      <div><label>驾驶模式</label><select id="s-mode">
        ${[["copilot","关键审批(推荐)"],["fullauto","完全托管(一停不停)"],["autopilot","全自动"],["manual","逐站审批"]].map(([v,t])=>`<option value="${v}"${s?.mode===v?" selected":""}>${t}</option>`).join("")}</select></div>
    </div>
    <div class="actions"><button class="btn pri" onclick="schedSave(${s?s.id:"null"})">💾 ${s?"保存修改":"创建"}</button>
      <button class="btn" onclick="$('#sform').innerHTML=''">取消</button></div></div>`;
  schedKindUI(s);
  window.scrollTo(0,0);
}
function schedEdit(id){
  const s = SCHED_ROWS.find(x=>x.id===id);
  if(!s) return toast("刷新后再试");
  schedForm(s);
}
function schedKindUI(s){
  const k = $("#s-kind").value;
  const t = esc(s?.at_time||"09:00");
  $("#s-when").innerHTML = k==="interval"
    ? `<label>每几小时来一单</label><input id="s-hours" type="number" min="1" value="${s?.every_hours||24}" oninput="schedCost()">`
    : `<label>${k==="weekly"?"星期几 + 时间":"时间"}(北京时间)</label><div style="display:flex;gap:6px">
       ${k==="weekly"?`<select id="s-wd" style="flex:1">${WD.map((w,i)=>`<option value="${i}"${(s?.weekday??0)===i?" selected":""}>${w}</option>`).join("")}</select>`:""}
       <input id="s-time" type="time" value="${t}" style="flex:1"></div>`;
  schedCost();
}
function schedCost(){
  // 老板设频率前先看清账:这个节奏一天烧多少点、现有余额撑几天。
  const el = $("#s-cost"); if(!el) return;
  const pts = META?.job_points??18, bal = Math.round(STATE?.balance||0);
  const k = $("#s-kind")?.value;
  let perDay = 1;
  if(k==="weekly") perDay = 1/7;
  else if(k==="interval") perDay = 24/Math.max(1, +($("#s-hours")?.value)||24);
  const daily = perDay*pts;
  const runway = daily>0 ? Math.floor(bal/daily) : 0;
  const heavy = daily > pts;   // 高于一天一单就标黄提醒
  el.innerHTML = `💎 该节奏 ≈ <b>${k==="weekly"?`每周 1 单 · ${pts} 点/周`
    :`${perDay===1?"1":(Math.round(perDay*10)/10)} 单/天 · ${Math.round(daily*10)/10} 点/天`}</b>
    (每单 ${pts} 点) · 当前余额 ${bal} 点约可自动跑 <b>${k==="weekly"?`${Math.floor(bal/pts)} 周`:`${runway} 天`}</b>${
    heavy?` <span style="color:#b45309;font-weight:800">⚠️ 高频节奏烧点快,点数不足会自动暂停并通知您</span>`:""}`;
}
async function schedSave(id){
  const dir = $("#s-dir").value.trim();
  if(!dir) return toast("内容方向必填");
  const kind = $("#s-kind").value;
  const body = {name:$("#s-name").value.trim(), kind, mode:$("#s-mode").value,
    profile_id:$("#s-profile").value?+$("#s-profile").value:null,
    at_time:$("#s-time")?.value||"09:00", weekday:$("#s-wd")?+$("#s-wd").value:null,
    every_hours:$("#s-hours")?+$("#s-hours").value:null,
    brief:{direction:dir, template:$("#s-tpl .on")?.textContent||"",
      industry:$("#s-ind .on")?.textContent||"",
      platforms:[...document.querySelectorAll("#s-pf .on")].map(e=>e.dataset.p||e.textContent)}};
  if(id && !body.name) delete body.name;   // 编辑时清空任务名=保留原名
  try{
    if(id){ await api("/schedules/"+id,{method:"PUT",body}); toast("定时任务已更新,下次执行时间已按新节奏重算"); }
    else{ await api("/schedules",{method:"POST",body}); toast("定时任务已创建"); }
    render();
  }catch(e){ toast(e.message); }
}
async function schedToggle(id,enabled){
  try{ await api("/schedules/"+id,{method:"PUT",body:{enabled:!!enabled}}); render(); }catch(e){ toast(e.message); }
}
async function schedRunNow(id){
  if(!await uiConfirm(`立即按该主题跑一单完整流水线,将扣 ${META?.job_points??18} 点?`,{okText:"🚀 开工",okClass:"pri"})) return;
  try{ const r = await api(`/schedules/${id}/run-now`,{method:"POST"});
    toast("已开工 → 工单 #"+r.job_id); location.hash="#/job/"+r.job_id;
  }catch(e){ toast(e.message); }
}
async function schedDel(id){
  if(!await uiConfirm("删除这个定时任务?此操作不进回收站、不可恢复;只想临时停用请用开关。",{title:"删除定时任务",confirmText:"删除"})) return;
  try{ await api("/schedules/"+id,{method:"DELETE"}); toast("已删除"); render(); }catch(e){ toast(e.message); }
}

/* ---------- V4:设置 ---------- */
const fmtB = n => n>1e9?(n/1e9).toFixed(2)+" GB":n>1e6?(n/1e6).toFixed(1)+" MB":Math.round(n/1e3)+" KB";
async function settingsView(){
  const s = await api("/settings");
  const st = s.storage;
  $("#main").innerHTML = `
  <div class="notice" style="margin-top:0">🔐 登录与密码安全统一在“权限管理”中维护；模型与供应商配置位于 <a href="#/admin" style="text-decoration:underline"><b>🛠 管理后台</b></a>。</div>
  <div class="card"><h2>💾 数据储存</h2>
    <div class="kv" style="margin-top:4px">
      <span>数据库 ${fmtB(st.db_bytes)}</span><span>素材文件 ${fmtB(st.assets_bytes)}</span>
      <span>工单 ${st.jobs} 单</span><span>工位记录 ${st.station_runs} 条</span>
      <span>知识沉淀 ${st.knowledge} 条</span><span>进修技能 ${st.skills} 条</span>
      <span>定时任务 ${st.schedules} 个</span><span>资产 ${st.assets} 条</span></div>
    <div class="notice" style="margin-top:10px">📂 全部数据存在服务器 <code>${esc(st.data_dir)}</code>:单文件 SQLite(contentcrew.db)+ 素材目录(assets/)。删除工单/沉淀/资产/人设都是先进 <a href="#/trash" style="text-decoration:underline"><b>🗑 回收站</b></a>(可恢复),交付素材不会被销毁;进修技能库与企业档案长期保留,是越用越值钱的部分。</div></div>`;
}

/* ---------- V6:数字人摄影棚 ---------- */
let AV_PHOTO = null, AV_JOB = null;
async function avatarView(){
  const [meta,jobsPayload,photosPayload] = await Promise.all([
    api("/avatar/meta"),api(listPath("/avatar/jobs","avatar")),api("/avatar/photos")
  ]);
  const jobsContract=normalizeListContract(jobsPayload,50);
  const photosContract=normalizeListContract(photosPayload,0);
  const jobs=jobsContract.items, photos=photosContract.items;
  if(AV_PHOTO && !photos.some(p=>p.name===AV_PHOTO.name)) AV_PHOTO = null;
  if(!AV_PHOTO && photos.length) AV_PHOTO = photos[photos.length-1];
  const active = jobs.filter(j=>["queued","running"].includes(j.status));
  const hgDown = !meta.heygen_ready || meta.heygen_exhausted;
  const avBanner = hgDown ? `<div class="notice violet" style="margin-top:0">
    🎬 <b>${meta.heygen_ready?"HeyGen 额度已用完,需要充值":"当前未接入 HeyGen「会动」引擎"}</b> ——
    照片对口型的 <b>可灵 / 基础版</b> 照常出片,不影响您今天拍视频。${meta.heygen_ready?` 想继续用 HeyGen,请去 <a href="https://app.heygen.com/settings?nav=Subscriptions" target="_blank" style="text-decoration:underline;font-weight:800">充值</a>。`:``}
    <div style="margin-top:5px">💡 想<b>不烧数字人额度</b>也能出视频?<a href="#/tools" onclick="TS.tab='vars'" style="text-decoration:underline;font-weight:900">试试 🎬 图文成片(3点/条,自动配音+字幕+抓真实图,不用照片)→</a></div></div>` : "";
  $("#main").innerHTML = avBanner
    +listContractNotice(jobsContract,"数字人任务")
    +listContractNotice(photosContract,"人物照片")+`
  <div class="card" style="background:linear-gradient(120deg,#e8e3ff,#fffaf0 65%)">
    <div style="display:flex;gap:14px;align-items:center;flex-wrap:wrap">
      ${charSVG("#8b5cf6","🎥", active.length?"work":"idle", 92)}
      <div style="flex:1;min-width:240px"><h2 style="margin:0">🎥 数字人摄影棚</h2>
      <div class="sub" style="margin-top:5px">照片 + 口播稿 → 配音 → 驱动照片开口说话 → 成片。当前引擎:<b>${esc(meta.engine_note)}</b></div></div></div>
  </div>
  <div class="card" style="background:#eef7ff"><h2>⚡ 偷懒入口:贴爆款链接,口播稿自动写</h2>
    <div class="row" style="align-items:flex-end">
      <div style="flex:2;min-width:260px"><label>抖音/公众号爆款链接 <span class="sub">(抖音/公众号成功率高;小红书防抓严,大概率提取不到——小红书内容建议直接复制正文粘到下方口播稿框改写)</span></label>
        <input id="av-link" placeholder="https://…(分享链接直接粘贴)"></div>
      <div style="flex:1;min-width:150px"><label>改写风格(选填)</label>
        <input id="av-style" placeholder="如:接地气、幽默"></div>
      <div style="flex:1;min-width:130px"><label>口播时长</label>
        <select id="av-link-dur">${meta.durations.map(d=>`<option value="${d.s}" ${d.s===30?"selected":""}>${d.label}</option>`).join("")}</select></div>
      <div style="flex:1;min-width:150px"><label>贴合人设(选填)</label>
        <select id="av-link-profile"><option value="">(不用人设)</option>${(STATE.profiles||[]).map(p=>`<option value="${p.id}">${esc(p.name)}</option>`).join("")}</select></div>
      <div><button class="btn pri" onclick="avFromLink(this)">🪄 提取并改写(1点)</button></div>
    </div>
    <div class="sub" style="margin-top:6px">情报员会打开链接提取内容,自动改写成 30-60 秒口播稿并填到下面(约 1 分钟)。</div></div>
  <div class="card"><h2>🎬 开拍</h2>
    <div class="row" style="align-items:flex-start">
      <div style="flex:1;min-width:260px">
        <label>① 数字人照片 *(正脸清晰、光线好,单人)</label>
        <input type="file" accept=".jpg,.jpeg,.png,.webp" onchange="avUpload(this)">
        <div id="av-photo-box" style="margin-top:8px">
          ${photos.length?`<div style="display:flex;gap:10px;flex-wrap:wrap">${photos.map(p=>`
            <div style="position:relative;width:96px">
              <img src="${esc(safeAssetUrl(p.preview))}" onclick="avPickPhoto(${cp(p.name)},${cp(safeAssetUrl(p.preview))})"
                style="width:96px;height:96px;object-fit:cover;cursor:pointer;border-radius:12px;border:3px solid ${AV_PHOTO&&AV_PHOTO.name===p.name?"var(--gold,#c9a227)":"var(--ink)"};${AV_PHOTO&&AV_PHOTO.name===p.name?"box-shadow:0 0 0 3px #c9a22755":""}">
              ${AV_PHOTO&&AV_PHOTO.name===p.name?`<span style="position:absolute;left:6px;bottom:6px;background:var(--ink);color:#fff;font-size:11px;padding:1px 7px;border-radius:8px">已选</span>`:""}
              <button class="btn sm bad" style="position:absolute;top:-8px;right:-8px;padding:1px 7px;border-radius:50%" title="删除这张照片" onclick="avDelPhoto(${cp(p.name)},event)">✕</button>
            </div>`).join("")}</div>
            <div class="sub" style="margin-top:5px">点选一张开拍;✕ 随时删,不占卡槽。</div>`
          :`<div class="empty" style="padding:20px">还没上传照片</div>`}
        </div>
      </div>
      <div style="flex:1.4;min-width:300px">
        <label>② 声音怎么来 <span class="sub" style="font-weight:400">(选错了没关系,开拍后能取消重来)</span></label>
        <div class="chips" style="gap:8px">
          <span class="chip ${AV_VOICE_MODE!=="own"?"on":""}" style="padding:9px 16px" id="av-vm-preset" onclick="avVoiceMode('preset')">🎙 系统音色配音</span>
          <span class="chip ${AV_VOICE_MODE==="own"?"on":""}" style="padding:9px 16px" id="av-vm-own" onclick="avVoiceMode('own')">🗣 用我自己的录音</span>
        </div>
        <div id="av-voice-preset" style="margin-top:6px">
          <select id="av-voice">${meta.voices.map(v=>`<option value="${v.id}">${esc(v.label)}</option>`).join("")}</select>
          <details style="margin-top:6px"><summary class="sub" style="cursor:pointer">🧬 克隆我的声音(录 30 秒,以后任何稿子都是您的原声)</summary>
            <div class="notice" style="margin-top:6px">用手机随便录一段您说话的音频(<b>至少 10 秒</b>,建议 30-60 秒,内容随意),上传后点克隆。克隆好的声音会出现在上面的音色列表最顶部,永久可用。</div>
            <input type="file" accept=".mp3,.m4a,.wav" onchange="avUploadCloneSample(this)">
            <div class="row" style="margin-top:6px;align-items:flex-end">
              <div style="flex:1"><label>给这个声音起个名</label><input id="av-clone-label" placeholder="如:老板本尊" value="老板的声音"></div>
              <div><button class="btn pri sm" id="av-clone-btn" disabled onclick="avClone(this)">🧬 开始克隆(${META?.voice_clone_points??9}点)</button></div>
            </div>
            <div class="sub" id="av-clone-status"></div>
          </details></div>
        <div id="av-voice-own" style="display:none;margin-top:6px">
          <input type="file" accept=".mp3,.m4a,.wav" onchange="avUploadVoice(this)">
          <div class="sub" id="av-own-status" style="margin-top:4px">上传一段您念这份口播稿的录音(手机录就行,成片就是您的原声)</div></div>
        <label>③ 口播稿 *(建议 60-200 字,约 30-60 秒)</label>
        <textarea id="av-script" style="min-height:120px" placeholder="自己写、从内容部交付里复制,或用上面的爆款链接自动生成。"></textarea>
        <label>④ 生成引擎 & 时长档位</label>
        <div class="row">
          <div><select id="av-engine" onchange="avPriceSync()">${meta.engines.map(e=>`<option value="${e.key}">${esc(e.label)}</option>`).join("")}</select></div>
          <div><select id="av-dur" onchange="avPriceSync()">${meta.durations.map(d=>`<option value="${d.s}" ${d.s===30?"selected":""}>${d.label}</option>`).join("")}</select></div>
        </div>
        ${meta.heygen_ready&&meta.heygen_exhausted?`<div class="notice" style="font-size:12px">⚠️ HeyGen 余额不足,选它会自动改用可灵。想继续用请去 <a href="https://app.heygen.com/settings?nav=Subscriptions" target="_blank" style="text-decoration:underline">HeyGen 充值</a>;日常口播用<b>基础版/可灵</b>即可。</div>`:""}
        <label>⑤ 表演提示(选填)</label>
        <input id="av-prompt" placeholder="如:微笑讲述,偶尔点头,手势自然">
        <div class="actions"><button class="btn pri" onclick="avSubmit(this)">🎬 开拍</button>
          <span class="sub" id="av-price"></span></div>
      </div>
    </div>
  </div>
  ${active.length?`<div class="card" style="background:#fffaf0"><h2>🎬 进行中(${active.length} 个并行)</h2>
    ${active.map(j=>`<div style="border:2px solid var(--ink);border-radius:14px;padding:12px;margin-bottom:10px;background:#fff">
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
        <b>#${j.id} ${{queued:"排队中",running:"🎥 拍摄中…"}[j.status]}</b>
        <span class="sub" style="flex:1">${esc((j.params.script||"").slice(0,42))}… · ${esc(j.params.voice_label||"")}${j.params.own_audio_name?" · 🗣本人原声":""}</span>
        <button class="btn sm bad" onclick="avCancel(${j.id})">✖ 取消</button></div>
      <div class="steps" data-avsteps="${j.id}" style="margin-top:8px">${(j.steps||[]).map((s,i)=>stepRow(s,i+1)).join("")||`<div class="step"><span class="ic">⏳</span><span class="lb">导演准备中…</span></div>`}</div>
    </div>`).join("")}</div>`:""}
  ${jobs.length||jobsContract.offset>0?`<div class="card"><h2>历史成片(${jobsContract.total??jobs.length})</h2>${jobs.length?jobs.map(j=>`
    <div class="topic">
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
        <span class="t" style="flex:1">#${j.id} ${esc((j.params.script||"").slice(0,32))}…</span>
        <span class="pill ${j.status==="done"?"done":j.status==="failed"?"failed":j.status==="cancelled"?"cancelled":"running"}">${{queued:"排队",running:"拍摄中",done:"已交付",failed:"失败",cancelled:"已取消"}[j.status]}</span>
        ${j.status==="failed"&&j.retryable?`<button class="btn sm pri" onclick="retryAvatarJob(${j.id},this)">🔁 免费重试(${j.free_retries_remaining})</button>`:""}
        ${isAdmin()?`<button class="btn sm bad" onclick="avDel(${j.id})" title="移入回收站">🗑</button>`:""}</div>
      ${j.status==="done"&&safeAssetUrl(j.video_file)?`<video controls style="max-width:360px;width:100%;border:3px solid var(--ink);border-radius:14px;margin-top:8px" src="${esc(safeAssetUrl(j.video_file))}"></video>
        <div class="actions" style="margin-top:6px"><a class="btn sm pri" href="${esc(safeAssetUrl(j.video_file))}" download>⬇️ 下载成片</a>
        ${safeAssetUrl(j.audio_file)?`<a class="btn sm" href="${esc(safeAssetUrl(j.audio_file))}" target="_blank" rel="noopener noreferrer">🔊 只要配音</a>`:""}</div>`:""}
      ${j.status==="failed"?`<div class="notice red" style="margin-top:6px">${esc(j.error||"生成失败")}</div>`:""}
      <div class="sub" style="margin-top:4px;font-size:11px">${new Date(j.created_at*1000).toLocaleString("zh-CN")}</div>
    </div>`).join(""):`<div class="empty">这一页没有数字人任务。</div>`}
    ${listPager(jobsContract,"avatar")}</div>`:""}`;
  active.forEach(j=>{ const box=document.querySelector(`[data-avsteps="${j.id}"]`); if(box) box.scrollTop=box.scrollHeight; });
  avPriceSync();
}
let AV_VOICE_MODE = "preset", AV_OWN_AUDIO = null;
function avVoiceMode(m){
  AV_VOICE_MODE = m;
  $("#av-vm-preset").classList.toggle("on", m==="preset");
  $("#av-vm-own").classList.toggle("on", m==="own");
  $("#av-voice-preset").style.display = m==="preset"?"":"none";
  $("#av-voice-own").style.display = m==="own"?"":"none";
}
function xhrUpload(url, fd, input){
  return new Promise((resolve,reject)=>{
    const xhr=new XMLHttpRequest(); xhr.open("POST", url);
    const pill=document.createElement("div");
    pill.style.cssText="position:fixed;left:50%;transform:translateX(-50%);bottom:70px;z-index:99;background:#2b2317;color:#ffd166;border:2.5px solid #ffd166;border-radius:12px;padding:8px 16px;font-weight:800;box-shadow:3px 4px 0 rgba(0,0,0,.4)";
    pill.textContent="上传中… 0%"; document.body.appendChild(pill);
    if(input) input.disabled=true;
    const done=()=>{ pill.remove(); if(input){ input.disabled=false; input.value=""; } };
    xhr.upload.onprogress=e=>{ if(e.lengthComputable) pill.textContent=`上传中… ${Math.round(e.loaded/e.total*100)}%${e.total>3e6?"(文件较大,请别关页面)":""}`; };
    xhr.onload=()=>{ done();
      let body={}; try{ body=JSON.parse(xhr.responseText||"{}"); }catch(_){}
      if(xhr.status>=200&&xhr.status<300) resolve(body);
      else{ if(xhr.status===402) pay402(body.detail||"点数不足");
        const err=new Error(body.detail||"上传失败,请重试"); err.status=xhr.status; reject(err); }
    };
    xhr.onerror=()=>{ done(); reject(new Error("网络中断,上传失败;请检查网络后重试")); };
    xhr.ontimeout=()=>{ done(); reject(new Error("上传超时;网络较慢时建议换 WiFi 再试")); };
    xhr.timeout=300000;
    xhr.send(fd);
  });
}
async function avUploadVoice(input){
  const f = input.files[0]; if(!f) return;
  const fd = new FormData(); fd.append("file", f); fd.append("kind", "voice");
  try{
    AV_OWN_AUDIO = await xhrUpload("/api/avatar/upload", fd, input);
    $("#av-own-status").innerHTML = `✅ 已上传:${esc(f.name)} <audio controls src="${esc(safeAssetUrl(AV_OWN_AUDIO.preview))}" style="vertical-align:middle;height:28px"></audio>`;
  }catch(e){ toast(e.message); }
}
let AV_CLONE_SAMPLE = null;
async function avUploadCloneSample(input){
  const f = input.files[0]; if(!f) return;
  const fd = new FormData(); fd.append("file", f); fd.append("kind", "voice");
  try{
    AV_CLONE_SAMPLE = await xhrUpload("/api/avatar/upload", fd, input);
    $("#av-clone-btn").disabled = false;
    $("#av-clone-status").textContent = `已上传:${f.name},点「开始克隆」`;
  }catch(e){ toast(e.message); }
}
async function avClone(btn){
  if(!AV_CLONE_SAMPLE) return toast("先上传录音");
  btn.disabled=true; btn.innerHTML=`<span class="spin"></span> 克隆中(约30秒)…`;
  try{
    const v = await api("/avatar/clone",{method:"POST",body:{audio_name:AV_CLONE_SAMPLE.name,
      label:$("#av-clone-label").value.trim()}});
    toast(`克隆成功!音色「${v.label}」已加入列表`); AV_CLONE_SAMPLE=null; render();
  }catch(e){ toast(e.message); btn.disabled=false; btn.textContent=`🧬 开始克隆(${META?.voice_clone_points??9}点)`; }
}
async function avFromLink(btn){
  const url = $("#av-link").value.trim();
  const errBox = ()=>{ let b=$("#av-link-err"); if(!b){ $("#av-link").insertAdjacentHTML("afterend",'<div id="av-link-err"></div>'); b=$("#av-link-err"); } return b; };
  errBox().innerHTML="";
  if(!url) return toast("先贴一个爆款链接");
  btn.disabled=true; btn.innerHTML=`<span class="spin"></span> 提取中(约1分钟)…`;
  try{
    const r = await api("/avatar/script-from-link",{method:"POST",body:{url, style:$("#av-style").value.trim(),
      duration:+($("#av-link-dur")?.value||30), profile_id:$("#av-link-profile")?.value?+$("#av-link-profile").value:null}});
    $("#av-script").value = r.script;
    if(r.source_text){
      let box = $("#av-source");
      if(!box){ $("#av-link").closest(".card").insertAdjacentHTML("beforeend",
        `<details id="av-source" open style="margin-top:10px"><summary style="cursor:pointer;font-weight:800">📄 提取到的原文(比对用)</summary>
         <div class="notice" style="white-space:pre-wrap;max-height:220px;overflow:auto;font-size:12.5px" id="av-source-txt"></div></details>`); }
      $("#av-source-txt").textContent = r.source_text;
    }
    toast(r.source_text?"口播稿已生成,原文在下方可比对":"⚠️ 原文没抓到,这稿是按标题独立创作的——发布前请自己核对内容");
    $("#av-script").scrollIntoView({behavior:"smooth"});
  }catch(e){
    // 失败原因常驻红条:后端给的两条自救路径值得被读完,不能塞进转瞬即逝的 toast
    errBox().innerHTML = `<div class="notice red" style="margin-top:6px">${esc(e.message)}(1 点已自动退回)</div>`;
  }
  btn.disabled=false; btn.textContent="🪄 提取并改写(1点)";
}
async function avUpload(input){
  const f = input.files[0]; if(!f) return;
  const fd = new FormData(); fd.append("file", f); fd.append("kind", "photo");
  try{
    AV_PHOTO = await xhrUpload("/api/avatar/upload", fd, input);
    toast("照片已上传");
    render();
  }catch(e){ toast(e.message); }
}
function avPickPhoto(name, preview){ AV_PHOTO = {name, preview}; render(); }
async function avDelPhoto(name, ev){
  if(ev) ev.stopPropagation();
  if(!await uiConfirm("删除这张照片?",{title:"删除照片",confirmText:"删除"})) return;
  try{
    await api(`/avatar/photos/${encodeURIComponent(name)}`,{method:"DELETE"});
    if(AV_PHOTO && AV_PHOTO.name===name) AV_PHOTO = null;
    toast("已删除"); render();
  }catch(e){ toast(e.message); }
}
function avPriceSync(){
  const el=$("#av-price"); if(!el) return;
  const eng=$("#av-engine")?.value||"", dur=+($("#av-dur")?.value||30);
  const pts = eng==="basic" ? 6 : (dur<=30 ? 12 : 20);
  el.innerHTML = `💎 本单 <b>${pts} 点</b> · 余额 ${Math.round(STATE?.balance||0)} 点`;
}
async function avSubmit(btn){
  if(!AV_PHOTO) return toast("先上传照片");
  const script = $("#av-script").value.trim();
  if(!script) return toast("口播稿必填");
  btn.disabled=true; btn.innerHTML=`<span class="spin"></span> 开拍中…`;
  if(AV_VOICE_MODE==="own" && !AV_OWN_AUDIO) { btn.disabled=false; btn.textContent="🎬 开拍"; return toast("先上传您的录音"); }
  try{
    await api("/avatar/jobs",{method:"POST",body:{photo_name:AV_PHOTO.name,
      voice_id:$("#av-voice")?.value, script, prompt:$("#av-prompt").value.trim(),
      engine:$("#av-engine")?.value||"", duration:+($("#av-dur")?.value||30),
      own_audio_name: AV_VOICE_MODE==="own"&&AV_OWN_AUDIO ? AV_OWN_AUDIO.name : ""}});
    toast(`已开拍${AV_VOICE_MODE==="own"?"(用您的原声)":""},可继续开新的任务`); render();
  }catch(e){ toast(e.message); btn.disabled=false; btn.textContent="🎬 开拍"; }
}
async function avCancel(id){
  if(!await uiConfirm(`取消这个数字人任务 #${id}? 未完成的渲染会退回。`,{
    title:"取消数字人任务",confirmText:"取消任务"
  })) return;
  try{ await api(`/avatar/jobs/${id}/cancel`,{method:"POST"}); toast("已取消"); render(); }
  catch(e){ toast(e.message); }
}
async function avDel(id){
  if(!await uiConfirm(`把数字人任务 #${id} 移入回收站? 成片会保留，可恢复。`,{
    title:"移入回收站",confirmText:"移入回收站"
  })) return;
  try{ await api("/avatar/jobs/"+id,{method:"DELETE"}); toast("已移入回收站"); render(); }
  catch(e){ toast(e.message); }
}

/* ---------- V6:管理者后台 ---------- */
let ADM = null, ADM_PROMPT = null, ADM_FUNNEL = null;
async function adminView(){
  [ADM,ADM_FUNNEL] = await Promise.all([
    api("/admin/overview"),
    api("/admin/funnel?days=30"),
  ]);
  const tm = ADM.text_models, im = ADM.image_models;
  const opt=(list,cur,blank)=>`<option value="">${blank}</option>`+list.map(m=>`<option value="${m.id}" ${cur===m.id?"selected":""}>${esc(m.label)}</option>`).join("");
  const byDept = {};
  ADM.employees.forEach(e=>(byDept[e.dept]=byDept[e.dept]||[]).push(e));
  const fm=ADM_FUNNEL.summary||{}, fc=ADM_FUNNEL.conversion||{};
  const metric=(key)=>fm[key]||{events:0,actors:0};
  const daily=(ADM_FUNNEL.daily||[]).slice(-14).reverse();
  $("#main").innerHTML = `
  <div class="notice" style="margin-top:0">🛠 <b>管理者后台</b>:数字员工的提示词、模型路由、供应商配置都收在这里,前台员工面板只留业务操作。</div>
  <div class="card"><h2>📈 近 30 天产品转化漏斗</h2>
    <div class="sub">平台自托管统计，只保存事件、日期和不可逆匿名标识；不采集手机号、表单正文、提示词或口令。</div>
    <div class="stats" style="justify-content:flex-start;margin-top:12px">
      <div class="stat"><b>${metric("landing_view").actors}</b><span>落地页访客</span></div>
      <div class="stat"><b>${metric("lead_submitted").actors}</b><span>留资人数</span></div>
      <div class="stat"><b>${metric("registration_complete").actors}</b><span>开通账号</span></div>
      <div class="stat"><b>${metric("first_job_submitted").actors}</b><span>首次派活企业</span></div>
      <div class="stat"><b>${metric("pricing_view").actors}</b><span>定价页访客</span></div>
    </div>
    <div class="kv" style="margin-top:12px">
      <span>落地→留资 <b>${Number(fc.landing_to_lead_pct||0).toFixed(1)}%</b></span>
      <span>留资→开通 <b>${Number(fc.lead_to_registration_pct||0).toFixed(1)}%</b></span>
      <span>开通→首单 <b>${Number(fc.registration_to_first_job_pct||0).toFixed(1)}%</b></span>
      <span>落地→定价 <b>${Number(fc.landing_to_pricing_pct||0).toFixed(1)}%</b></span>
    </div>
    <details style="margin-top:12px"><summary style="cursor:pointer;font-weight:800">查看最近 14 天明细</summary>
      <div class="dimwrap" style="margin-top:8px"><table class="dimtable"><thead><tr>
        <th>日期</th><th>落地访客</th><th>留资</th><th>开通</th><th>首单</th><th>定价访客</th>
      </tr></thead><tbody>${daily.map(row=>`<tr>
        <td>${esc(row.day)}</td>
        <td>${row.landing_view?.actors||0}</td><td>${row.lead_submitted?.actors||0}</td>
        <td>${row.registration_complete?.actors||0}</td><td>${row.first_job_submitted?.actors||0}</td>
        <td>${row.pricing_view?.actors||0}</td></tr>`).join("")||`<tr><td colspan="6">暂无数据，发布后将从真实访问开始累计。</td></tr>`}
      </tbody></table></div>
    </details>
  </div>
  <div class="card"><h2>☁️ 供应商 · 云雾API</h2>
    <div class="row">
      <div><label>接口地址</label><input id="adm-base" value="${esc(ADM.provider.yunwu_base)}"></div>
      <div><label>API Key ${ADM.provider.yunwu_key?`<span class="tag">当前:${esc(ADM.provider.yunwu_key)}</span>`:`<span class="tag">未配置</span>`}</label>
        <input id="adm-key" type="password" placeholder="sk-…(留空不改)"></div>
    </div>
    <div class="actions"><button class="btn pri" onclick="admSaveProvider()">💾 保存供应商</button>
      <span class="sub">所有数字员工与工具能力统一使用此 API,不依赖服务器本地模型登录态。</span></div></div>
  <div class="card"><h2>🎥 数字人引擎</h2>
    <div class="sub">HeyGen(Avatar IV):约 $1-4/分钟,人物会动、支持本人原声,推荐;可灵:走云雾按秒计费,只对口型。配了 HeyGen key 自动切 HeyGen。</div>
    <div class="notice" style="margin-top:8px">🔑 <b>一键申请 HeyGen key</b>:
      ① <a href="https://app.heygen.com/signup" target="_blank" style="text-decoration:underline">点这里注册/登录 HeyGen</a>(可用 Google 账号)
      ② <a href="https://app.heygen.com/settings?nav=API" target="_blank" style="text-decoration:underline">点这里进入 API 设置页</a> → Create API Key → 复制
      ③ 粘到下面输入框 → 保存,立即生效(充值也在那个页面,按量 $5 起)</div>
    <div class="row" style="margin-top:8px">
      <div><label>引擎</label><select id="adm-aveng">
        <option value="" ${!ADM.avatar?.engine_forced?"selected":""}>自动(有 HeyGen key 用 HeyGen)</option>
        <option value="heygen" ${ADM.avatar?.engine_forced==="heygen"?"selected":""}>强制 HeyGen</option>
        <option value="kling" ${ADM.avatar?.engine_forced==="kling"?"selected":""}>强制 可灵</option></select></div>
      <div><label>HeyGen API Key ${ADM.avatar?.heygen_key?`<span class="tag">当前:${esc(ADM.avatar.heygen_key)}</span>`:`<span class="tag">未配置(app.heygen.com→Settings→API)</span>`}</label>
        <input id="adm-hgkey" type="password" placeholder="留空不改"></div>
    </div>
    <div class="row" style="margin-top:8px">
      <div><label>基础版·RunningHub Key ${ADM.avatar?.rh_ready?`<span class="tag">已配</span>`:`<span class="tag">未配</span>`}</label>
        <input id="adm-rhkey" type="password" placeholder="留空不改"></div>
      <div><label>RunningHub 工作流ID</label><input id="adm-rhwf" value="${esc(ADM.avatar?.rh_workflow||"")}" placeholder="1986772059139289089"></div>
    </div>
    <div class="actions"><button class="btn pri" onclick="admSaveAvatar()">💾 保存数字人配置</button>
      <span class="sub">生效引擎:${esc(ADM.avatar?.engine_active||"kling")} · 基础版=RunningHub工作流(最省)</span></div></div>
  <div class="card"><h2>🕊 飞书集成(平台级应用)</h2>
    <div class="sub">在 <a href="https://open.feishu.cn/app" target="_blank" style="text-decoration:underline">飞书开放平台</a> 创建「企业自建应用」→ 权限管理里开通「查看、评论、编辑和管理多维表格」(bitable:app) → 发布版本 → 把凭据填这里。之后每个企业在资产库页粘自己的多维表格链接即可一键同步。</div>
    <div class="row" style="margin-top:8px">
      <div><label>App ID</label><input id="adm-fsid" value="${esc(ADM.feishu?.app_id||"")}" placeholder="cli_xxx"></div>
      <div><label>App Secret ${ADM.feishu?.secret_set?`<span class="tag">已设置</span>`:""}</label><input id="adm-fssec" type="password" placeholder="留空不改"></div>
    </div>
    <div class="actions"><button class="btn pri" onclick="admSaveFeishu()">💾 保存飞书配置</button></div></div>
  <div class="card"><h2>📮 客资邮件通知</h2>
    <div class="sub">访客留资后自动发邮件到您邮箱。需要一次性配置:QQ邮箱网页版 → 设置 → 账户 → 开启「SMTP服务」→ 生成授权码,填到下面。</div>
    <div class="row" style="margin-top:8px">
      <div><label>发件QQ邮箱</label><input id="adm-smtpu" value="${esc(ADM.mail?.smtp_user||"")}"></div>
      <div><label>SMTP授权码 ${ADM.mail?.authcode_set?`<span class="tag">已设置</span>`:`<span class="tag">未设置,邮件不发</span>`}</label><input id="adm-smtpc" type="password" placeholder="16位授权码,留空不改"></div>
      <div><label>客资收件邮箱</label><input id="adm-leadto" value="${esc(ADM.mail?.lead_email||"")}"></div>
    </div>
    <div class="actions"><button class="btn pri" onclick="admSaveMail()">💾 保存邮件配置</button></div></div>
  <div class="card"><h2>🧭 全局模型路由</h2>
    <div class="sub">所有文本员工默认跟随这里的模型;趋势官、情报员、拆解师也可以在下方单独切换。需要实时资料时系统会自动启动云雾能力网关执行检索,再交给您选择的模型完成交付。</div>
    <div class="row" style="margin-top:8px">
      <div><label>默认文本模型</label><select id="adm-dt">${tm.map(m=>`<option value="${m.id}" ${ADM.routing.default_text_model===m.id?"selected":""}>${esc(m.label)}</option>`).join("")}</select></div>
      <div><label>默认生图模型</label><select id="adm-di">${im.map(m=>`<option value="${m.id}" ${ADM.routing.default_image_model===m.id?"selected":""}>${esc(m.label)}</option>`).join("")}</select></div>
    </div>
    <div class="actions"><button class="btn pri" onclick="admSaveRouting()">💾 保存路由</button></div></div>
  <div class="card"><h2>👥 员工级配置(模型 / 提示词)</h2>
    <div class="sub">留空 = 跟随全局默认。带 🌐 的是检索型工位,模型可自由切换;无论选择 GPT、DeepSeek 还是 Claude,实时任务都会自动调用云雾工具能力。</div>
    ${Object.entries(byDept).map(([dept,list])=>`
      <details ${dept==="内容生产部"?"open":""} style="margin-top:10px"><summary style="cursor:pointer;font-weight:900">${esc(dept)}(${list.length}人)</summary>
      <div class="dimwrap" style="margin-top:8px"><table class="dimtable" style="min-width:900px"><thead><tr>
        <th>开关</th><th>员工</th><th>技能</th><th>文本模型</th><th>生图模型</th><th>提示词</th><th>管理</th></tr></thead><tbody>
      ${list.map(e=>`<tr style="${e.enabled?"":"opacity:.5"}">
        <td>${e.core?`<span class="sub" title="流水线必备">🔒必备</span>`
          :`<span class="switch ${e.enabled?"on":""}" onclick="admToggleEmp(${e.idx},${e.enabled?0:1})"></span>`}</td>
        <td><b>${esc(e.name)}</b>${e.web_required?" 🌐":""}${e.is_custom?` <span class="tag">自定义提示词</span>`:""}</td>
        <td>⚡${e.skills_n||0}</td>
        <td><select onchange="admSetModel(${e.idx},'model_text',this.value)" style="padding:4px">${opt(tm,e.model_text,"(默认)")}</select></td>
        <td>${ADM.image_capable.includes(e.idx)?`<select onchange="admSetModel(${e.idx},'model_image',this.value)" style="padding:4px">${opt(im,e.model_image,"(默认)")}</select>`:`<span class="sub">—</span>`}</td>
        <td><button class="btn sm" onclick="admPrompt(${e.idx})">📝 提示词</button></td>
        <td><button class="btn sm" onclick="admDetail(${e.idx})">🧰 技能/档案</button></td>
      </tr>`).join("")}</tbody></table></div></details>`).join("")}
    <div id="adm-prompt-box"></div><div id="adm-detail-box"></div></div>`;
}
async function admSaveProvider(){
  const body = {yunwu_base:$("#adm-base").value.trim()};
  const k = $("#adm-key").value.trim(); if(k) body.yunwu_key = k;
  try{ await api("/settings",{method:"PUT",body}); toast("供应商已保存"); render(); }catch(e){ toast(e.message); }
}
async function admSaveMail(){
  const body = {smtp_user:$("#adm-smtpu").value.trim(), lead_email:$("#adm-leadto").value.trim()};
  const c = $("#adm-smtpc").value.trim(); if(c) body.smtp_authcode = c;
  try{ await api("/settings",{method:"PUT",body}); toast("邮件配置已保存"); render(); }
  catch(e){ toast(e.message); }
}
async function admSaveFeishu(){
  const body = {feishu_app_id: $("#adm-fsid").value.trim()};
  const s = $("#adm-fssec").value.trim(); if(s) body.feishu_app_secret = s;
  try{ await api("/settings",{method:"PUT",body}); toast("飞书配置已保存"); render(); }
  catch(e){ toast(e.message); }
}
async function admSaveAvatar(){
  const body = {avatar_engine: $("#adm-aveng").value, runninghub_workflow: $("#adm-rhwf")?.value.trim()||""};
  const k = $("#adm-hgkey").value.trim(); if(k) body.heygen_key = k;
  const rk = $("#adm-rhkey")?.value.trim(); if(rk) body.runninghub_key = rk;
  try{ await api("/settings",{method:"PUT",body}); toast("数字人配置已保存"); render(); }
  catch(e){ toast(e.message); }
}
async function admSaveRouting(){
  try{ await api("/settings",{method:"PUT",body:{default_text_model:$("#adm-dt").value, default_image_model:$("#adm-di").value}});
    toast("模型路由已保存,下次开工生效"); }catch(e){ toast(e.message); }
}
async function admSetModel(idx, field, val){
  try{ await api(`/admin/employees/${idx}/models`,{method:"PUT",body:{[field]:val}}); toast("已保存"); }
  catch(e){ toast(e.message); }
}
async function admToggleEmp(idx, en){
  try{ await api(`/admin/employees/${idx}/enabled`,{method:"PUT",body:{enabled:!!en}});
    toast(en?"已启用":"已停用(楼层隐藏,派活/会议拦截)"); DEPTS=null; render(); }
  catch(e){ toast(e.message); }
}
async function admDetail(idx){
  const d = await api(`/admin/employees/${idx}/detail`);
  $("#adm-detail-box").innerHTML = `<div class="card" style="background:#eef7ff;margin-top:12px">
    <div style="display:flex;gap:10px;align-items:center">
      <h3 style="margin:0;flex:1">🧰 ${esc(d.name)} · 档案管理</h3>
      ${d.core?`<span class="tag">流水线必备</span>`:`<span class="switch ${d.enabled?"on":""}" onclick="admToggleEmp(${idx},${d.enabled?0:1})"></span>`}
      <button class="btn sm" onclick="$('#adm-detail-box').innerHTML=''">收起</button></div>
    <div class="kv" style="margin-top:6px"><span>${esc(d.dept)}</span><span>${esc(d.duty)}</span>
      <span>出勤 ${d.stats.runs} 次</span><span>历史成本 ${rmb(d.stats.cost_usd)}</span>
      <span>${d.learned_at?"上次进修 "+new Date(d.learned_at*1000).toLocaleDateString("zh-CN"):"未进修过"}</span></div>
    <div class="actions"><button class="btn pri sm" ${d.learning?"disabled":""} onclick="this.disabled=true;this.textContent='🎓 进修中…';api('/employees/${idx}/learn',{method:'POST'}).then(()=>toast('已送去进修(3点),结果会发站内通知')).catch(e=>{toast(e.message);this.disabled=false;this.textContent='🎓 送去进修(3点)';})">🎓 送去进修(3点)</button></div>
    <h3>技能库(${(d.skills||[]).length})</h3>
    ${(d.skills||[]).map((k,i)=>`<div class="skillcard ${k.enabled===false?"off":""}">
      <span class="switch ${k.enabled!==false?"on":""}" onclick="admSkillToggle(${idx},${i})"></span>
      <div style="flex:1"><div class="t">${esc(k.title)}</div><div class="sub">${esc(k.detail)}</div></div>
      <button class="btn sm bad" onclick="admSkillDel(${idx},${i})">🗑</button></div>`).join("")||`<div class="empty">还没技能</div>`}
    <h3>能力项(${(d.capabilities||[]).length})</h3>
    <div class="chips">${(d.capabilities||[]).map(c=>`<span class="chip ${c.enabled?"on":""}">${c.emoji||"🔹"} ${esc(c.name)}</span>`).join("")}</div>
  </div>`;
  window.__ADM_DETAIL = d;
  $("#adm-detail-box").scrollIntoView({behavior:"smooth"});
}
async function admSkillToggle(idx,i){
  const d = window.__ADM_DETAIL; const skills=[...d.skills];
  skills[i] = {...skills[i], enabled: skills[i].enabled===false};
  try{ await api(`/employees/${idx}/skills`,{method:"PUT",body:{skills}}); admDetail(idx); }
  catch(e){ toast(e.message); }
}
async function admSkillDel(idx,i){
  const d = window.__ADM_DETAIL;
  if(!await uiConfirm(`删除技能「${d.skills[i].title}」?`,{
    title:"删除技能",confirmText:"删除"
  })) return;
  const skills = d.skills.filter((_,n)=>n!==i);
  try{ await api(`/employees/${idx}/skills`,{method:"PUT",body:{skills}}); admDetail(idx); }
  catch(e){ toast(e.message); }
}
async function admPrompt(idx){
  ADM_PROMPT = await api(`/admin/employees/${idx}/prompt`);
  const p = ADM_PROMPT;
  $("#adm-prompt-box").innerHTML = `<div class="card" style="background:#fff6dc;margin-top:12px">
    <h3 style="margin-top:0">📝 ${esc(p.name)} 的提示词</h3>
    <div style="margin:6px 0">${Object.entries(p.placeholders||{}).map(([k,v])=>`<span class="phchip">{${k}}</span><span class="sub" style="margin-right:10px">${esc(v)}</span>`).join("")}</div>
    <textarea id="adm-prompt-ta" class="promptbox">${esc(p.prompt_template||p.default_template)}</textarea>
    <div class="actions">
      <button class="btn pri" onclick="admSavePrompt(${p.idx})">💾 保存生效</button>
      <button class="btn" onclick="admResetPrompt(${p.idx})">↩️ 恢复默认</button>
      <button class="btn" onclick="$('#adm-prompt-box').innerHTML=''">收起</button></div></div>`;
  $("#adm-prompt-box").scrollIntoView({behavior:"smooth"});
}
async function admSavePrompt(idx){
  const v = $("#adm-prompt-ta").value;
  const body = (ADM_PROMPT && v.trim()===String(ADM_PROMPT.default_template).trim())?{template:""}:{template:v};
  try{ await api(`/employees/${idx}/prompt`,{method:"PUT",body}); toast("提示词已保存"); render(); }
  catch(e){ toast(e.message); }
}
async function admResetPrompt(idx){
  if(!await uiConfirm("恢复默认提示词?",{title:"恢复提示词",confirmText:"恢复默认"})) return;
  try{ await api(`/employees/${idx}/prompt`,{method:"PUT",body:{template:""}}); toast("已恢复"); render(); }
  catch(e){ toast(e.message); }
}

/* ---------- V9:文件/图片/PDF 解析进素材 ---------- */
async function parseFileInto(input, targetSel){
  const ta = $(targetSel); if(!ta || !input.files.length) return;
  for(const f of input.files){
    const fd = new FormData(); fd.append("file", f);
    try{
      const d = await xhrUpload("/api/parse-file", fd, input);
      ta.value += (ta.value?"\n\n":"") + `【文件:${d.name}】\n${d.text}`;
    }catch(e){ toast(e.message); }
  }
  toast("文件内容已读入素材"); input.value="";
  if(targetSel==="#b-mat") scheduleBriefDraftSave();
}

/* ---------- V8:飞书集成 ---------- */
async function feishuSync(kind, btn){
  btn.disabled=true; btn.innerHTML=`<span class="spin"></span> 同步中…`;
  try{
    const r = await api("/feishu/sync",{method:"POST",body:{kind}});
    toast(`✅ 已同步 ${r.synced} 条到飞书「${r.table}」表`);
  }catch(e){ toast(e.message); if(e.message.includes("链接")||e.message.includes("配置")) feishuConfig(); }
  btn.disabled=false; btn.textContent="📤 同步到飞书";
}
async function feishuConfig(){
  const f = await api("/feishu");
  const box = $("#fs-box"); if(!box) return;
  box.innerHTML = `<div class="card" style="background:#eef7ff">
    <b>🔗 连接飞书多维表格</b>
    ${f.configured?"":`<div class="notice" style="margin-top:8px">平台还没配飞书应用:请老板在 <a href="https://open.feishu.cn/app" target="_blank" style="text-decoration:underline">飞书开放平台</a> 创建「企业自建应用」,开通「多维表格」权限(bitable:app),把 App ID 和 Secret 填到 管理后台。</div>`}
    <div class="sub" style="margin-top:6px">①在飞书里新建一个「多维表格」→ ②把这个应用添加为表格协作者(可编辑) → ③粘贴表格链接:</div>
    <input id="fs-url" value="${esc(f.bitable||"")}" placeholder="https://xxx.feishu.cn/base/xxxxxx" style="margin-top:6px">
    <div class="actions"><button class="btn pri sm" onclick="feishuSaveUrl()">💾 保存</button>
      <button class="btn sm" onclick="$('#fs-box').innerHTML=''">收起</button></div></div>`;
}
async function feishuSaveUrl(){
  try{ await api("/feishu/bitable",{method:"PUT",body:{url:$("#fs-url").value.trim()}});
    toast("已保存,点「同步到飞书」试试"); $("#fs-box").innerHTML=""; }catch(e){ toast(e.message); }
}

/* ---------- V8:权限管理 ---------- */
async function saveSupportContact(btn){
  btn.disabled=true;
  try{ const r=await api('/team/support-contact',{method:'POST',body:{contact:$('#sc-input').value}});
    if(META) META.support_contact=r.contact; toast('已保存,立即生效');
  }catch(e){ toast(e.message); } btn.disabled=false;
}
async function teamView(){
  const t = await api("/team");
  const modChips = (sel)=>t.all_modules.map(m=>`<span class="chip mod-chip${(sel||[]).includes(m.key)?" on":""}" data-k="${m.key}" onclick="this.classList.toggle('on')">${esc(m.label)}</span>`).join("");
  $("#main").innerHTML = `
  <div class="notice" style="margin-top:0">👥 <b>权限管理</b>:企业信息、副账号与板块权限${ME.role==="root"?"、租户(客户企业)管理、平台后台":""}。副账号只能看到被分配的板块,数据按企业完全隔离。</div>
  <div class="card"><h2>🏢 我的企业</h2>
    <div class="row" style="align-items:flex-end">
      <div style="flex:1"><label>企业名称</label><input id="tm-name" value="${esc(t.tenant.name)}"></div>
      <div><button class="btn pri" onclick="tmRename()">💾 保存</button></div></div>
    <div class="kv" style="margin-top:8px"><span>成员 ${t.users.length} 人</span>
      <span>点数余额 ${(t.tenant.balance||0).toFixed(0)} 点</span>
      ${t.tenant.plan?`<span>套餐:${esc(t.tenant.plan)}</span>`:""}</div></div>
  <div class="card"><h2>👤 成员与副账号</h2>
    <div class="actions"><button class="btn pri" onclick="tmUserForm()">➕ 创建副账号</button></div>
    <div id="tm-uform"></div>
    ${t.users.map(u=>`<div class="topic">
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
        <b>${esc(u.username)}</b>
        <span class="tag">${{root:"👑 平台管理员",owner:"⭐ 主账号",member:"副账号"}[u.role]}</span>
        ${u.enabled?"":`<span class="tag" style="background:#ffb3b5">已停用</span>`}
        <span style="flex:1"></span>
        ${u.role==="member"?`<button class="btn sm" onclick="tmEditMods(${u.id},${cp(u.username)},${cp(u.modules)})">🧩 板块</button>
        <button class="btn sm" onclick="tmResetPw(${u.id})">🔑 改密</button>
        <button class="btn sm" onclick="tmToggle(${u.id},${u.enabled?0:1})">${u.enabled?"⏸ 停用":"▶️ 启用"}</button>
        <button class="btn sm bad" onclick="tmDel(${u.id},${cp(u.username)})">🗑</button>`:""}
      </div>
      ${u.role==="member"?`<div class="sub" style="margin-top:4px">板块:${(u.modules||[]).map(k=>esc((t.all_modules.find(m=>m.key===k)||{}).label||k)).join(" · ")||"(未分配,啥也用不了)"}</div>`:""}
    </div>`).join("")}
    <div id="tm-modbox"></div></div>
  <div class="card"><h2>🔑 改我自己的密码</h2>
    <div class="row"><div><label>旧密码</label><input id="tm-old" type="password"></div>
      <div><label>新密码(≥12位，至少两类字符)</label><input id="tm-new" type="password"></div></div>
    <div class="actions"><button class="btn pri" onclick="tmMyPw()">💾 修改</button></div></div>
  ${ME.role==="root"&&(t.guests||[]).length?`
  <div class="card"><h2>📇 客资(访客留资 ${t.guests.length} 条)</h2>
    <div class="dimwrap"><table class="dimtable" style="min-width:min(520px,100%)"><thead><tr>
      <th>手机号</th><th>称呼</th><th>企业/行业</th><th>时间</th><th>状态</th></tr></thead><tbody>
      ${t.guests.map(g=>`<tr><td><b>${esc(g.phone)}</b></td><td>${esc(g.name||"-")}</td>
        <td>${esc(g.company||"-")}</td><td>${new Date(g.created_at*1000).toLocaleString("zh-CN")}</td>
        <td>${g.used?"已体验":"已进参观"}</td></tr>`).join("")}</tbody></table></div>
    <div class="sub" style="margin-top:6px">📮 配好 QQ 邮箱授权码(后台→邮件通知)后,每条新客资会实时发到您邮箱</div></div>`:""}
  ${ME.role==="root"?`
  <div class="card"><h2>📝 开通申请(${(t.applies||[]).filter(a=>!a.status).length} 条待处理)</h2>
    <div class="sub">来自登录页「申请开通账号」。点<b>「⚡ 一键开通」</b>自动建企业租户+主账号+随机密码,弹出可复制的开通话术直接发给客户;开完在下面「租户管理」里给TA充点/开套餐。</div>
    <div class="notice" id="ap-auto-box" style="margin:8px 0">⏳ 载入自动开通设置…</div>
    <div class="dimwrap"><table class="dimtable" style="min-width:640px"><thead><tr>
      <th>手机号</th><th>称呼</th><th>企业/行业</th><th>想用来做什么</th><th>时间</th><th>登录账号</th><th>操作</th></tr></thead><tbody>
      ${(t.applies||[]).map(a=>`<tr style="${a.status&&!a.username?"opacity:.45":""}"><td><b>${esc(a.phone)}</b></td><td>${esc(a.name||"-")}</td>
        <td>${esc(a.company||"-")}</td><td>${esc(a.note||"-")}</td><td>${new Date(a.created_at*1000).toLocaleString("zh-CN")}</td>
        <td>${a.username?`<b>${esc(a.username)}</b><div class="sub" style="font-size:11px">租户#${a.tenant_id||"-"}·密码忘了去成员列表重置</div>`:"<span class='sub'>未开通</span>"}</td>
        <td style="white-space:nowrap">${a.username?"✅ 已开通"
          :a.status?"📦 已归档"
          :`<button class="btn sm pri" onclick="tmApprove(${a.id})">⚡ 一键开通</button>
            <button class="btn sm" onclick="tmApplyDone(${a.id})" title="不开账号,仅归档">归档</button>`}</td></tr>`).join("")}</tbody></table></div></div>`:""}
  ${ME.role==="root"?`
  <div class="card"><h3 style="margin-top:0">📞 对客联系方式</h3>
    <div class="sub">显示在所有租户的套餐页与「点数不足」提示里;不填的话,客户想充值只能靠右下角反馈留言。</div>
    <div class="actions" style="margin-top:8px"><input id="sc-input" placeholder="如:微信 paihuo-vip / 电话 138xxxx" value="${esc(META?.support_contact||"")}" style="flex:1;min-width:220px">
    <button class="btn sm pri" onclick="saveSupportContact(this)">保存</button></div></div>
  <div class="card" style="background:#fff6dc"><h2>🏦 租户管理(平台老板专属)</h2>
    <div class="sub">每个客户企业一个租户:开租户→给主账号→客户登录自建副账号。收款您线下收,这里给TA充点/开套餐。</div>
    <div class="actions"><button class="btn pri" onclick="tmTenantForm()">➕ 开新企业租户</button>
      <a class="btn" href="#/admin">🛠 平台管理后台(模型/提示词/供应商)</a>
      <a class="btn" href="/promo" target="_blank">📣 宣传页(可发客户)</a></div>
    <div id="tm-tform"></div>
    ${(t.tenants||[]).map(x=>`<div class="topic" id="tm-indrow-${x.id}">
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
        <b>#${x.id} ${esc(x.name)}</b><span class="tag">${x.n_users}人</span>
        <span class="tag">💎 ${(x.balance||0).toFixed(0)}点</span>
        ${x.plan?`<span class="tag">${esc(x.plan)}</span>`:""}
        ${x.id===1?`<span class="tag">全行业</span>`:(x.industries||[]).length?`<span class="tag" style="background:#c9e2ff">${(x.industries||[]).map(k=>esc((t.all_industries||[]).find(m=>m.key===k)?.name||k)).join("/")}</span>`:`<span class="tag" style="background:#ffb3b5">未设行业</span>`}
        ${x.enabled?"":`<span class="tag" style="background:#ffb3b5">已停用</span>`}
        <span style="flex:1"></span>
        ${x.id!==1?`<button class="btn sm" onclick='tmSetIndustries(${x.id},${JSON.stringify(x.industries||[])})'>🏭 行业</button>`:""}
        <button class="btn sm" onclick="tmGrant(${x.id})">💎 充点</button>
        <button class="btn sm" onclick="tmSub(${x.id})">📦 开套餐</button>
        ${x.id!==1?`<button class="btn sm" onclick="tmTenantToggle(${x.id},${x.enabled?0:1})">${x.enabled?"⏸ 停用":"▶️ 启用"}</button>`:""}
      </div></div>`).join("")}
  </div>`:""}`;
  window.__modChips = modChips;
  window.__TEAM = t;
}
async function tmRename(){ try{ await api("/team/tenant",{method:"PUT",body:{name:$("#tm-name").value.trim()}}); toast("已保存"); ME=null; render(); }catch(e){ toast(e.message); } }
function tmUserForm(){
  $("#tm-uform").innerHTML = `<div class="card" style="background:#fff6dc">
    <div class="row"><div><label>用户名</label><input id="tm-u"></div><div><label>密码(≥12位，至少两类字符)</label><input id="tm-p" type="password"></div></div>
    <label>开通板块(多选)</label><div class="chips" id="tm-mods">${window.__modChips([])}</div>
    <div class="actions"><button class="btn pri" onclick="tmUserCreate()">💾 创建</button>
      <button class="btn" onclick="$('#tm-uform').innerHTML=''">取消</button></div></div>`;
}
async function tmUserCreate(){
  const mods = [...document.querySelectorAll("#tm-mods .on")].map(e=>e.dataset.k);
  const password=$("#tm-p").value, passwordError=passwordPolicyError(password);
  if(passwordError) return toast(passwordError);
  if(!mods.length && !await uiConfirm(
    "还没勾选任何板块:员工登录后会是一片空白、啥也用不了。\n确定先建空账号,回头再分配?",
    {title:"未分配板块",confirmText:"先建空账号"})) return;
  try{ await api("/team/users",{method:"POST",body:{username:$("#tm-u").value.trim(),
    password, modules:mods}});
    toast(mods.length?"副账号已创建,把用户名密码发给TA即可"
      :"副账号已创建(未分配板块):记得点「板块」分配,TA 才能用"); render(); }catch(e){ toast(e.message); }
}
function tmEditMods(uid, name, mods){
  $("#tm-modbox").innerHTML = `<div class="card" style="background:#fff6dc">
    <b>给「${esc(name)}」分配板块</b>
    <div class="chips" id="tm-mods2" style="margin-top:8px">${window.__modChips(mods)}</div>
    <div class="actions"><button class="btn pri" onclick="tmSaveMods(${uid})">💾 保存</button>
      <button class="btn" onclick="$('#tm-modbox').innerHTML=''">取消</button></div></div>`;
  $("#tm-modbox").scrollIntoView({behavior:"smooth"});
}
async function tmSaveMods(uid){
  const mods = [...document.querySelectorAll("#tm-mods2 .on")].map(e=>e.dataset.k);
  try{ await api(`/team/users/${uid}`,{method:"PUT",body:{modules:mods}}); toast("已保存"); render(); }catch(e){ toast(e.message); }
}
async function tmResetPw(uid){
  const pw = await uiPrompt({
    title:"重置成员密码",message:"请输入至少 12 位、且包含至少两类字符的新密码。",
    label:"新密码",type:"password",confirmText:"重置密码",
    validate:passwordPolicyError
  });
  if(pw===null) return;
  try{ await api(`/team/users/${uid}`,{method:"PUT",body:{password:pw}}); toast("已改密"); }catch(e){ toast(e.message); }
}
async function tmToggle(uid,en){ try{ await api(`/team/users/${uid}`,{method:"PUT",body:{enabled:!!en}}); render(); }catch(e){ toast(e.message); } }
async function tmAutoLoad(){
  const box = $("#ap-auto-box"); if(!box) return;
  try{
    const c = await api("/team/apply-config");
    box.innerHTML = `<label style="display:inline-flex;align-items:center;font-weight:800;margin:0">
      <input type="checkbox" id="ap-auto" style="width:auto;margin-right:6px" ${c.auto?"checked":""} onchange="tmAutoSave()">
      申请<b>自动开通</b>体验账号(竞品都是注册即试用——客户提交申请当场拿到账号密码,送</label>
      <input id="ap-trial" type="number" value="${c.trial_points}" style="width:70px;display:inline-block;margin:0 4px" onchange="tmAutoSave()">
      点体验;关掉则回到人工「⚡一键开通」。开通照样有邮件通知您。
      <div class="sub" style="margin-top:6px">🛡 已内置防滥用:同IP日限3次申请 · 手机号去重(已有账号/已申请不重复建单) · 日自动开通上限
        <input id="ap-cap" type="number" min="1" value="${c.daily_cap}" style="width:60px;display:inline-block;margin:0 4px" onchange="tmAutoSave()">
        个(达上限后新申请自动转人工待处理,不影响您手动「⚡一键开通」),可放心打开跑一周。</div>`;
  }catch(e){ box.innerHTML=""; }
}
async function tmAutoSave(){
  try{ await api("/team/apply-config",{method:"PUT",body:{auto:$("#ap-auto").checked, trial_points:+$("#ap-trial").value||20, daily_cap:+$("#ap-cap").value||20}});
    toast($("#ap-auto").checked?"✅ 已开启自动开通(送"+($("#ap-trial").value||20)+"点·日上限"+($("#ap-cap").value||20)+")":"已改为人工开通"); }
  catch(e){ toast(e.message); }
}
async function tmApplyDone(aid){ try{ await api(`/team/applies/${aid}/done`,{method:"POST"}); toast("已归档"); render(); }catch(e){ toast(e.message); } }
async function tmApprove(aid){
  if(!await uiConfirm("将自动创建企业租户、主账号和随机密码。确定一键开通?",{
    title:"一键开通体验账号",confirmText:"确认开通",danger:false
  })) return;
  try{
    const r = await api(`/team/applies/${aid}/approve`,{method:"POST"});
    document.body.insertAdjacentHTML("beforeend", `<div class="overlay" id="ap-ov" onclick="if(event.target===this)this.remove()">
      <div class="panel" style="max-width:560px;width:94vw;padding:22px">
        <h2 style="margin:0">🎉 账号开通成功</h2>
        <div class="notice green" style="margin:12px 0;font-size:15px">
          企业:<b>${esc(r.tenant_name)}</b>(租户#${r.tenant_id})<br>
          账号:<b style="font-size:18px">${esc(r.username)}</b><br>
          初始密码:<b style="font-size:18px">${esc(r.password)}</b></div>
        <div class="notice" style="font-size:12.5px">⚠️ 密码只显示这一次!下面话术已包含,复制后微信/短信发给客户即可。客户忘密码:租户管理→进该企业成员列表→重置。别忘了给TA<b>充点/开套餐</b>,不然登录了也派不了活。</div>
        <textarea style="min-height:130px;margin-top:8px" id="ap-msg" readonly>${esc(r.notice)}</textarea>
        <div class="actions" style="margin-top:10px">
          <button class="btn pri" onclick="copyText($('#ap-msg').value)">📋 复制开通话术</button>
          <button class="btn" onclick="$('#ap-ov').remove()">完成</button></div>
      </div></div>`);
    render();
  }catch(e){ toast(e.message); }
}
async function tmDel(uid,name){ if(!await uiConfirm(`删除账号「${name}」?此操作不进回收站、不可恢复。如只是员工离职,建议用旁边的「⏸ 停用」。`,{
  title:"删除账号",confirmText:"删除"
})) return;
  try{ await api(`/team/users/${uid}`,{method:"DELETE"}); toast("已删除"); render(); }catch(e){ toast(e.message); } }
async function tmMyPw(){
  const password=$("#tm-new").value, passwordError=passwordPolicyError(password);
  if(passwordError) return toast(passwordError);
  try{ await api("/auth/password",{method:"PUT",body:{old:$("#tm-old").value,new:password}});
    toast("密码已修改，请重新登录"); setTimeout(()=>{location.href="/login";},500);
  }catch(e){ toast(e.message); }
}
let TM_INDS = [];
function tmTenantForm(){
  TM_INDS = window.__TEAM?.all_industries || [];
  $("#tm-tform").innerHTML = `<div class="card">
    <div class="row"><div><label>企业名</label><input id="tt-name" placeholder="如:某某餐饮公司"></div>
      <div><label>主账号用户名</label><input id="tt-owner"></div>
      <div><label>初始密码(≥12位，至少两类字符)</label><input id="tt-pw" type="password"></div></div>
    <label>该企业所属行业(勾选后,他们只能看到「内容部+数字人+库+所选行业」,看不到别的行业)</label>
    <div class="chips" id="tt-inds">${TM_INDS.map(m=>`<span class="chip" data-k="${m.key}" onclick="this.classList.toggle('on')">${m.emoji} ${esc(m.name)}</span>`).join("")}</div>
    <div class="actions"><button class="btn pri" onclick="tmTenantCreate()">💾 开通</button>
      <button class="btn" onclick="$('#tm-tform').innerHTML=''">取消</button></div></div>`;
}
async function tmTenantCreate(){
  const inds = [...document.querySelectorAll("#tt-inds .on")].map(e=>e.dataset.k);
  if(!inds.length) return toast("至少勾一个行业(不选客户就看不到任何行业部门)");
  const password=$("#tt-pw").value, passwordError=passwordPolicyError(password);
  if(passwordError) return toast(passwordError);
  try{ await api("/team/tenants",{method:"POST",body:{name:$("#tt-name").value.trim(),
    owner:$("#tt-owner").value.trim(), password, industries:inds}});
    toast("租户已开通,把主账号密码发给客户"); render(); }catch(e){ toast(e.message); }
}
async function tmSetIndustries(tid, cur){
  const all = window.__TEAM?.all_industries || [];
  const box = document.getElementById("tm-ind-"+tid);
  if(box){ box.innerHTML=""; return; }
  const html = `<div id="tm-ind-${tid}" class="card" style="background:#eef7ff;margin-top:6px">
    <b>改这家企业能看的行业</b>
    <div class="chips" style="margin-top:6px">${all.map(m=>`<span class="chip ind-${tid} ${cur.includes(m.key)?"on":""}" data-k="${m.key}" onclick="this.classList.toggle('on')">${m.emoji} ${esc(m.name)}</span>`).join("")}</div>
    <div class="actions"><button class="btn pri sm" onclick="tmSaveIndustries(${tid})">💾 保存</button></div></div>`;
  document.getElementById("tm-indrow-"+tid).insertAdjacentHTML("afterend", html);
}
async function tmSaveIndustries(tid){
  const inds=[...document.querySelectorAll(`.ind-${tid}.on`)].map(e=>e.dataset.k);
  try{ await api(`/team/tenants/${tid}/industries`,{method:"PUT",body:{industries:inds}});
    toast("行业已更新"); render(); }catch(e){ toast(e.message); }
}
async function tmGrant(tid){
  const pts = await uiPrompt({
    title:"充值积分",message:"1 点 = ¥1，请填写正整数。",
    label:"充值点数",type:"number",min:1,confirmText:"确认充值",
    validate:value=>Number.isInteger(Number(value))&&Number(value)>0?"":"请输入大于 0 的整数"
  });
  if(pts===null) return;
  try{ const r = await api(`/team/tenants/${tid}/grant`,{method:"POST",body:{points:+pts,reason:"平台充值"}});
    toast(`已充值,余额 ${r.balance} 点`); render(); }catch(e){ toast(e.message); }
}
const SUBSCRIPTION_ORDER_IDS=new Map();
function subscriptionOrderStorageKey(tid,plan,period){
  return `paihuo:subscribe:${Number(tid)}:${String(plan)}:${String(period)}`;
}
function newSubscriptionOrderId(tid){
  const cryptoApi=globalThis.crypto;
  if(!cryptoApi?.getRandomValues) throw new Error("当前浏览器无法生成安全订单号，请升级浏览器后重试");
  const token=typeof cryptoApi.randomUUID==="function"
    ?cryptoApi.randomUUID()
    :Array.from(cryptoApi.getRandomValues(new Uint8Array(16)),b=>b.toString(16).padStart(2,"0")).join("");
  return `subscribe:${Number(tid)}:${token}`;
}
function subscriptionOrderId(tid,plan,period){
  const key=subscriptionOrderStorageKey(tid,plan,period);
  if(SUBSCRIPTION_ORDER_IDS.has(key)) return SUBSCRIPTION_ORDER_IDS.get(key);
  try{
    const cached=JSON.parse(localStorage.getItem(key)||"null");
    if(cached?.id&&Number(cached.created_at)>Date.now()-7*86400000){
      SUBSCRIPTION_ORDER_IDS.set(key,String(cached.id));
      return String(cached.id);
    }
  }catch(_){}
  const id=newSubscriptionOrderId(tid);
  SUBSCRIPTION_ORDER_IDS.set(key,id);
  try{ localStorage.setItem(key,JSON.stringify({id,created_at:Date.now()})); }catch(_){}
  return id;
}
function clearSubscriptionOrderId(tid,plan,period,orderId){
  const key=subscriptionOrderStorageKey(tid,plan,period);
  if(SUBSCRIPTION_ORDER_IDS.get(key)===orderId) SUBSCRIPTION_ORDER_IDS.delete(key);
  try{
    const cached=JSON.parse(localStorage.getItem(key)||"null");
    if(cached?.id===orderId) localStorage.removeItem(key);
  }catch(_){}
}
async function submitSubscription(tid,plan,period,orderId){
  const options={method:"POST",body:{plan,period,order_id:orderId}};
  for(let attempt=0;attempt<2;attempt++){
    try{ return await api(`/team/tenants/${tid}/subscribe`,options); }
    catch(e){
      // 超时、断网或网关 5xx 时，原请求可能已成交；用同一订单号重放一次即可
      // 安全取得原回执，绝不能生成第二个订单号。
      if(attempt>0||(e?.status&&e.status<500)) throw e;
    }
  }
}
async function tmSub(tid){
  const plan = await uiPrompt({
    title:"选择套餐",label:"套餐",type:"select",value:"startup",
    options:[
      {value:"trial",label:"体验版"},
      {value:"startup",label:"创业版"},
      {value:"biz",label:"企业版"},
      {value:"flagship",label:"旗舰版"},
    ],confirmText:"下一步"
  });
  if(plan===null) return;
  const period = await uiPrompt({
    title:"选择周期",label:"订阅周期",type:"select",value:"month",
    options:[
      {value:"month",label:"月付"},
      {value:"quarter",label:"季付"},
      {value:"year",label:"年付"},
    ],confirmText:"确认开通"
  });
  if(period===null) return;
  try{
    const orderId=subscriptionOrderId(tid,plan,period);
    const r=await submitSubscription(tid,plan,period,orderId);
    clearSubscriptionOrderId(tid,plan,period,orderId);
    toast(`套餐已开通:${r.points}点,应收¥${r.price}`); render();
  }catch(e){ toast(e.message); }
}
async function tmTenantToggle(tid,en){
  try{ await api(`/team/tenants/${tid}`,{method:"PUT",body:{enabled:!!en}}); render(); }catch(e){ toast(e.message); }
}

/* ---------- V8:套餐与点数 ---------- */
let BILL_TAB="all";
function billReasonHtml(reason){
  // 流水项目里的单号变成可点链接:老板终于能从账单跳回"这笔钱买了哪单"
  // 长别名规则必须先于「工单」执行,否则「数字人工单 #7」会被错链到内容工单 #7
  return esc(reason)
    .replace(/(?:数字人工单|成片任务)\s?#(\d+)/g,m=>m.replace(/#(\d+)/,'<a href="#/tasks" style="text-decoration:underline;font-weight:800">#$1</a>'))
    .replace(/(^|[^人])工单\s?#(\d+)/g,'$1工单 <a href="#/job/$2" style="text-decoration:underline;font-weight:800">#$2</a>')
    .replace(/任务#(\d+)/g,'任务<a href="#/tasks/$1" style="text-decoration:underline;font-weight:800">#$1</a>')
    .replace(/会议#(\d+)/g,'会议<a href="#/meetings" style="text-decoration:underline;font-weight:800">#$1</a>')
    .replace(/工具单#(\d+)/g,'工具单<a href="#/tasks" style="text-decoration:underline;font-weight:800">#$1</a>');
}
async function billingView(){
  trackFunnelSessionOnce("pricing_view","billing");
  const b = await api("/billing");
  const digestConf = await api("/notify/daily-digest").catch(()=>({enabled:true}));
  const tourBanner = ME&&ME.role==="tour" ? `<div class="notice" style="background:#ece3ff;margin-top:0">👀 <b>参观模式</b>:点击员工卡片,了解每位数字员工可以为您的业务提供什么帮助。<a href="/promo#plans" style="text-decoration:underline;font-weight:900">查看套餐</a> 或联系开通企业账号后直接派活。</div>` : "";
  const isRefund = l=>l.delta>0&&String(l.reason||"").startsWith("退回");
  const shown = (BILL_TAB==="in"?b.log.filter(l=>l.delta>0&&!isRefund(l)):BILL_TAB==="out"?b.log.filter(l=>l.delta<0):b.log);
  const spendActs = (!b.is_platform&&b.spend_by_action)?b.spend_by_action.filter(s=>(s.points||0)>0):[];
  const spendTotal = spendActs.reduce((sum,s)=>sum+(s.points||0),0);
  const expDays = b.plan_expires? Math.ceil((b.plan_expires*1000-Date.now())/86400000) : null;
  const expBanner = (expDays!==null&&expDays<=7&&!b.is_platform)?`<div class="notice red" style="margin-top:0">${
    expDays>0?`⏰ 当前套餐 <b>${esc(b.plan||"")}</b> 还有 <b>${expDays} 天</b>到期`
    :`⛔ 套餐 <b>${esc(b.plan||"")}</b> 已到期`},到期后不再按月发点。续费请${
    ME.role==="member"?"联系<b>贵司主账号(企业主)</b>"
    :`联系平台顾问${META?.support_contact?`:<b>${esc(META.support_contact)}</b>`:"(点右下角 💬 留言)"}`}。</div>`:"";
  $("#main").innerHTML = tourBanner + expBanner + `
  <div class="card" style="background:linear-gradient(120deg,#fff6dc,#fffaf0 60%)">
    <div style="display:flex;gap:16px;align-items:center;flex-wrap:wrap">
      <div style="flex:1;min-width:200px"><h2 style="margin:0">💎 我的积分账户</h2>
        <div class="sub" style="margin-top:6px">1 积分 = ¥1${b.plan?` · 当前套餐 <b>${esc(b.plan)}</b>${b.plan_expires?`(至 ${new Date(b.plan_expires*1000).toLocaleDateString("zh-CN")})`:""}`:""}</div></div>
      <div class="stat" style="background:#2b2317;color:#ffd166;border:3px solid var(--ink);border-radius:16px;padding:14px 30px;text-align:center;box-shadow:4px 4px 0 rgba(51,41,31,.25)">
        <div style="font-size:38px;font-weight:900;line-height:1">${b.is_platform?"∞":(b.balance||0).toFixed(0)}</div>
        <div style="font-size:12px;opacity:.85">积分余额</div></div>
    </div>
    ${b.is_platform?`<div class="notice" style="margin-bottom:0">👑 您是平台方,自己用不扣积分(显示∞)。下面看到的是给客户开通/客户消耗的真实记录。客户登录自己账号时,这里就是他的余额和账单。</div>`
      :`<div class="kv" style="margin-top:12px"><span>累计充值 <b style="color:#2f9e6e">+${(b.recharged||0).toFixed(0)}</b></span>
        <span>累计消耗 <b style="color:#e5484d">-${(b.spent||0).toFixed(0)}</b></span>
        ${(b.refunded_total||0)>0?`<span>失败退回 <b style="color:#b45309">+${(b.refunded_total||0).toFixed(0)}</b></span>`:""}
        <span>共 ${b.txn_n||0} 笔</span></div>`}</div>
  ${spendActs.length?`<div class="card"><h2>💸 近30天花在哪</h2>
    <div class="sub">按功能统计最近 30 天实际消耗的积分,帮您看清钱被哪类动作花掉了。</div>
    ${spendActs.map(s=>`<div style="margin-top:12px">
      <div style="display:flex;gap:8px;align-items:baseline;flex-wrap:wrap">
        <b style="flex:1">${esc((b.prices[s.action]&&b.prices[s.action].label)||s.action)}</b>
        <span class="sub">${s.n} 次</span><b style="color:#e5484d">共 ${(s.points||0).toFixed(0)} 点</b></div>
      <div style="height:9px;background:#eadfcd;border:1.5px solid var(--ink);border-radius:999px;overflow:hidden;margin-top:5px">
        <div style="height:100%;width:${spendTotal?Math.max(2,Math.round((s.points||0)/spendTotal*100)):0}%;background:#ffd166"></div></div></div>`).join("")}</div>`:""}
  ${(!b.is_platform&&(b.monthly||[]).length)?`<div class="card"><h2>📅 按月对账(近半年)</h2>
    <div class="dimwrap"><table class="dimtable" style="min-width:min(420px,100%)"><thead><tr>
      <th>月份</th><th>充值</th><th>消耗</th><th>失败退回</th></tr></thead><tbody>
      ${b.monthly.map(m=>`<tr><td><b>${esc(m.ym)}</b></td>
        <td style="color:#2f9e6e">+${(m.recharged||0).toFixed(0)}</td>
        <td style="color:#e5484d">-${(m.spent||0).toFixed(0)}</td>
        <td style="color:#b45309">${(m.refunded||0)>0?`+${(m.refunded||0).toFixed(0)}`:"—"}</td></tr>`).join("")}</tbody></table></div></div>`:""}
  <div class="card"><h2 style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">🧾 积分明细(充值 & 消耗记录)
    ${isAdmin()&&!b.is_platform?`<a class="btn sm" style="margin-left:auto" href="/api/records/export.xlsx?kind=billing">⬇️ 导出全部流水</a>`:""}</h2>
    <label style="display:flex;gap:8px;align-items:flex-start;margin:6px 0 4px;cursor:${isAdmin()?"pointer":"default"}">
      <input type="checkbox" style="margin-top:3px" ${digestConf.enabled?"checked":""} ${isAdmin()?"":"disabled"} onchange="digestToggle(this)">
      <span class="sub">📮 每日经营简报(昨日完成/花销/风险,推到站内+企微)${isAdmin()?"":"(仅企业主可改)"}</span></label>
    <div class="tabs">
      <span class="tb ${BILL_TAB==="all"?"on":""}" onclick="BILL_TAB='all';render()">全部(${b.log.length})</span>
      <span class="tb ${BILL_TAB==="in"?"on":""}" onclick="BILL_TAB='in';render()">💰 充值记录(${b.log.filter(l=>l.delta>0&&!isRefund(l)).length})</span>
      <span class="tb ${BILL_TAB==="out"?"on":""}" onclick="BILL_TAB='out';render()">🔻 消耗记录(${b.log.filter(l=>l.delta<0).length})</span></div>
    ${shown.length?`<div class="dimwrap" style="margin-top:10px"><table class="dimtable" style="min-width:min(520px,100%)"><thead><tr>
      <th>时间</th><th>类型</th><th>项目</th><th>积分变动</th><th>余额</th></tr></thead><tbody>
      ${shown.map(l=>`<tr>
        <td class="sub" style="white-space:nowrap">${tcFmt(l.created_at)}</td>
        <td>${isRefund(l)?'<span class="tag" style="background:#ffe3b3">退回</span>':l.delta>0?'<span class="tag" style="background:#a7ecc9">充值</span>':'<span class="tag" style="background:#ffd7d9">消耗</span>'}</td>
        <td>${billReasonHtml(l.reason||"")}</td>
        <td><b style="color:${l.delta<0?"#e5484d":"#2f9e6e"}">${l.delta>0?"+":""}${l.delta}</b></td>
        <td class="sub">${l.balance.toFixed(0)}</td></tr>`).join("")}</tbody></table></div>`
      :`<div class="empty" style="padding:26px">${b.is_platform?"平台自用不产生账单":BILL_TAB==="in"?"还没有充值记录":BILL_TAB==="out"?"还没有消耗记录":"暂无积分变动记录"}</div>`}</div>
  <div class="card"><h2>📋 价目表(按次消耗)</h2>
    <div class="dimwrap"><table class="dimtable" style="min-width:min(560px,100%)"><thead><tr><th>动作</th><th>消耗</th><th>成本参考</th></tr></thead>
    <tbody>${Object.values(b.prices).map(p=>`<tr><td>${esc(p.label)}</td><td><b>${p.points} 点</b></td><td class="sub">${esc(p.cost||"")}</td></tr>`).join("")}</tbody></table></div></div>
  <div class="card"><h2>📦 套餐(月/季/年)</h2>
    <div class="sub">季付 9 折,年付 8 折(按活动价再折)。${ME.role==="member"?"充值/续费请联系<b>贵司主账号(企业主)</b>操作;":""}开通请联系平台顾问${META?.support_contact?`:<b style="font-size:15px">${esc(META.support_contact)}</b>(微信/电话均可)`:"(点右下角 💬 留下联系方式,顾问会主动联系您)"}${ME.role==="root"?";root 可在「权限管理→租户管理」给客户开通,并设置对客联系方式":""}。</div>
    <div class="grid3" style="grid-template-columns:repeat(auto-fit,minmax(210px,1fr));margin-top:12px">
    ${b.plans.map((p,i)=>`<div class="topic" style="text-align:center;${i===1?"border-color:#ef476f;border-width:3px":""}">
      ${i===1?`<div style="color:#ef476f;font-weight:900;font-size:12px">🔥 最多人选</div>`:""}
      <h3 style="margin:4px 0">${esc(p.name)}</h3>
      <div class="sub" style="text-decoration:line-through">原价 ¥${p.price}/月</div>
      <div style="font-size:30px;font-weight:900">¥${p.sale}<span style="font-size:13px;font-weight:400">/月</span></div>
      <div class="tag" style="margin:6px 0">${p.points} 点/月</div>
      <div class="sub" style="margin:4px 0"><b>季付 ¥${Math.round(p.sale*3*0.9)}</b>(省¥${p.sale*3-Math.round(p.sale*3*0.9)}) · <b>年付 ¥${Math.round(p.sale*12*0.8)}</b>(省¥${p.sale*12-Math.round(p.sale*12*0.8)})</div>
      <div class="sub">${esc(p.desc)}</div></div>`).join("")}</div></div>
`;
}
async function digestToggle(cb){
  try{
    await api("/notify/daily-digest",{method:"POST",body:{enabled:cb.checked}});
    toast(cb.checked?"✅ 每日经营简报已开启,每天早上推昨日情况":"⏸ 每日经营简报已关闭,不再推送");
  }catch(e){ cb.checked=!cb.checked; toast(e.message); }
}

/* ---------- V10:任务编辑/入库 ---------- */
async function taskEdit(tid){
  const t = await api("/tasks/"+tid);
  document.body.insertAdjacentHTML("beforeend", `<div class="overlay" id="te-ov">
    <div class="panel"><div class="phead"><b style="flex:1">✏️ 编辑交付物 #${tid}</b>
      <button class="btn sm pri" onclick="taskEditSave(${tid})">💾 保存</button>
      <button class="btn sm" onclick="$('#te-ov').remove()">✕</button></div>
    <div class="pbody"><textarea id="te-md" class="promptbox" style="min-height:420px">${esc(t.output_md||"")}</textarea></div></div></div>`);
}
async function taskEditSave(tid){
  try{ await api(`/tasks/${tid}/output`,{method:"PUT",body:{md:$("#te-md").value}});
    toast("已保存"); $("#te-ov").remove();
    if(SPEC_TASK&&SPEC_TASK.id===tid) specOpenTask(tid);
    if(SOLO_TASK&&SOLO_TASK.id===tid){ SOLO_TASK=await api("/tasks/"+tid); drawModal(); }
  }catch(e){ toast(e.message); }
}
async function taskToKnow(tid){
  try{ await api(`/tasks/${tid}/to-knowledge`,{method:"POST"});
    toast("✅ 已存入沉淀库(会自动做11维评估)"); }catch(e){ toast(e.message); }
}

/* ---------- V10:圆桌会议室 ---------- */
let MT_CUR = null, MT_SEL = new Set();
const MT_STATE = {};  // meeting_id -> 用户手动展开/收起完整讨论
const MT_LIVE = {};   // meeting_id -> 本页曾以全程直播形态见过(速览晚到时不折正在读的讨论)
const MT_PHASE_LABEL = {queued:"准备中",brainstorm:"提案中",validate:"反向验证中",execute:"形成执行单",
  executing:"正在派活",awaiting_execution:"等老板启动",completed:"已进入执行",stopped:"已止损",failed:"中断"};
function mtDecisionBadge(d){
  if(!d) return "";
  const cfg = {GO:["✅ GO","#a7ecc9"],NO_GO:["⛔ NO-GO","#ffc3c3"],NEED_INFO:["🔎 NEED INFO","#ffe59a"]}[d]||[d,"#efe7d4"];
  return `<span class="tag" style="background:${cfg[1]};font-weight:900">${cfg[0]}</span>`;
}
function mtFlow(cur){
  const phase = cur.phase || (cur.status==="done"?"completed":"queued");
  const at = phase==="queued"||phase==="brainstorm"?0:phase==="validate"?1:2;
  const terminal = ["completed","stopped","awaiting_execution"].includes(phase) && cur.status==="done";
  const stages = [["1","每人一案 · Top N≤3"],["2","失败 / 市场 / 算账"],["3","决策 · 真派活"]];
  return `<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:7px;margin:12px 0">
    ${stages.map((s,i)=>{const done=i<at||(terminal&&i<=at), active=i===at&&!terminal;
      return `<div style="border:2px solid var(--ink);border-radius:11px;padding:8px 9px;background:${done?"#dff6e9":active?"#fff1bd":"#f2eee5"};opacity:${i>at?".58":"1"}">
        <div style="font-weight:900">${done?"✓":s[0]} · ${s[1]}</div>
        ${active?`<div class="sub" style="margin-top:2px">${esc(MT_PHASE_LABEL[phase]||"进行中")}…</div>`:""}</div>`;}).join("")}
  </div>`;
}
function mtConsensus(cur){
  if(!(cur.consensus_md||"").trim()) return "";
  return `<div class="card" style="background:#f0ebff;margin-top:12px;border-color:#6f56b3">
    <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap"><h3 style="margin:0;flex:1">🧠 会议共识 · 跨轮接力棒</h3>${mtDecisionBadge(cur.decision)}</div>
    <div class="md" style="margin-top:8px">${md(cur.consensus_md)}</div></div>`;
}
function mtBody(cur){
  const msgsBox = open => `<div id="mt-msgs" style="max-height:520px;overflow:auto;margin-top:10px;display:${open?"block":"none"}">${(cur.messages||[]).map(mtBubble).join("")||`<div class="empty">等员工进群…</div>`}</div>`;
  if(["failed","cancelled"].includes(cur.status)){
    return `<div class="notice red" style="margin-top:10px"><b>本场会议没有开成。</b> ${esc(cur.next_action||"执行中断")}
      <div class="sub" style="margin-top:4px">会议点数已自动退回,没有扣费。可换一批参会人或改写议题后重新召开。</div>
      <div class="actions" style="margin-top:8px"><a class="btn sm pri" href="#/tasks">🔁 到任务中心看这场会议并重试</a></div></div>${msgsBox(true)}`;
  }
  if(cur.status!=="done" || !(cur.summary_md||"").trim()){
    setBoundedState(MT_LIVE,cur.id,true);
    const eta = ["queued","running"].includes(cur.status)
      ? `<div class="sub" style="margin-top:8px">⏱ 三轮会议一般 3-8 分钟。可以离开本页办别的,结束会自动提醒;点数只在开会时扣一次。</div>` : "";
    return eta + msgsBox(true);
  }
  const open = (cur.id in MT_STATE) ? MT_STATE[cur.id] : !!MT_LIVE[cur.id];
  return `<div style="margin-top:10px;background:linear-gradient(120deg,#fff3d6,#ffe7c0);border:2.5px solid var(--ink);border-radius:13px;padding:12px 14px;box-shadow:3px 3px 0 #ffd16699">
      <div style="font-weight:900;font-size:15px;margin-bottom:4px">⚡ 老板速览</div>
      <div class="md">${md(cur.summary_md||"")}</div></div>
    <div style="margin-top:10px"><button class="btn sm" id="mt-fold" onclick="mtToggleMsgs(${cur.id},this)">${open?"💬 收起完整讨论":`💬 展开完整讨论(${(cur.messages||[]).length}条)`}</button></div>
    ${msgsBox(open)}`;
}
function mtToggleMsgs(id,btn){
  const el = $("#mt-msgs"); if(!el) return;
  const open = el.style.display==="none";
  el.style.display = open?"block":"none";
  btn.textContent = open?"💬 收起完整讨论":`💬 展开完整讨论(${el.children.length}条)`;
  setBoundedState(MT_STATE,id,open);
  if(open) el.scrollTop = el.scrollHeight;
}
function mtBubble(m){
  const boss = m.who==="老板", host = m.who==="会议主持人", sys = m.who==="系统";
  if(sys) return `<div style="text-align:center;margin:10px 0"><span class="sub" style="background:#efe7d4;border-radius:99px;padding:3px 14px;font-size:12px">${esc(m.text)}</span></div>`;
  const align = boss ? "row-reverse" : "row";
  const bubbleR = boss ? "14px 4px 14px 14px" : "4px 14px 14px 14px";
  const bg = host ? "#fff6dc" : boss ? "#e7f0ff" : "#fffdf7";
  return `<div style="display:flex;gap:10px;margin:11px 0;align-items:flex-start;flex-direction:${align}">
    <div style="flex:none;width:40px;height:40px;border:2.5px solid var(--ink);border-radius:11px;display:flex;align-items:center;justify-content:center;font-size:20px;background:${m.color}33">${m.emoji}</div>
    <div style="flex:1;${boss?"text-align:right":""}"><b style="font-size:13px">${esc(m.who)}</b>
      <div class="md" style="display:inline-block;text-align:left;max-width:88%;background:${bg};border:2px solid var(--ink);border-radius:${bubbleR};padding:9px 13px;margin-top:4px;font-size:13px">${md(m.text)}</div></div></div>`;
}
async function meetingsView(mid){
  if(mid && Number.isInteger(+mid) && +mid>0) MT_CUR=+mid;
  if(!DEPTS) DEPTS = await api("/depts");
  const meetingsContract=normalizeListContract(
    await api(listPath("/meetings","meetings")),LIST_PAGE_SIZE);
  const list = meetingsContract.items;
  const cur = MT_CUR ? await api("/meetings/"+MT_CUR).catch(optionalResult(null)) : null;
  const pool = [
    ...META.stations.map(s=>({idx:s.idx,label:s.name,color:s.color,emoji:s.emoji,group:"内容生产部"})),
    ...DEPTS.filter(d=>can(d.key)).flatMap(d=>d.employees.map(e=>({idx:e.idx,label:`${e.person||""}${e.person?"·":""}${e.name}`,color:e.color,emoji:e.emoji,group:e.group})))];
  const groups = {};
  pool.forEach(p=>(groups[p.group]=groups[p.group]||[]).push(p));
  $("#main").innerHTML = `
  <div class="notice" style="margin-top:0">🪑 <b>AI 结果型会议</b>:不是让数字员工无限聊天，而是强制走完「每人一案并按有效方案排 Top N（最多 3 个）→ 失败/市场/单位经济反向验证 → GO / NO-GO 并执行」。每人 1 点。</div>
  <div class="card"><h2>🎯 发起结果型会议</h2>
    <label>议题 *</label><textarea id="mt-q" placeholder="例:我想在老小区开一家 60 平的川湘菜馆,预算 40 万,帮我论证可行性和最大风险"></textarea>
    <div class="row"><div><label>已知约束(选填)</label><input id="mt-constraints" placeholder="例:预算40万；30天内开业；不能增加全职员工"></div>
      <div><label>什么算有结果(选填)</label><input id="mt-acceptance" placeholder="例:必须给明确GO/NO-GO、最大风险和本周可执行动作"></div></div>
    <div class="actions" style="margin-top:8px"><button class="btn blue" onclick="mtSuggest(this)">🤖 按议题自动选人</button>
      <span class="sub">或在下面手动勾选</span></div>
    <label>拉谁进群(2-6 人,已选 <b id="mt-n">0</b> 人)</label>
    <div id="mt-picked" style="margin:6px 0">${mtPickedHtml(pool)}</div>
    ${Object.entries(groups).map(([g,ps])=>`<details ${g==="内容生产部"?"":""} style="margin-top:6px"><summary style="cursor:pointer;font-weight:800;font-size:13px">${esc(g)}(${ps.length})</summary>
      <div class="chips" style="margin-top:6px">${ps.map(p=>`<span class="chip mt-chip${MT_SEL.has(p.idx)?" on":""}" data-i="${p.idx}" onclick="mtToggle(this)">${p.emoji} ${esc(p.label)}</span>`).join("")}</div></details>`).join("")}
    <label style="display:flex;gap:8px;align-items:flex-start;margin-top:12px;cursor:pointer">
      <input id="mt-auto" type="checkbox" checked style="width:auto;margin-top:3px">
      <span><b>决策后自动执行</b><span class="sub" style="display:block">GO 或 NEED INFO 后，自动启动最多 3 个真实员工任务；包含在本次会议中，不重复扣点。取消勾选则等您看完共识后再点执行。</span></span>
    </label>
    <div class="actions"><button class="btn pri" onclick="mtStart(this)">🎯 开会并收敛(每人1点)</button></div></div>
  ${cur?`<div class="card" style="background:#fffaf0"><h2>会议 #${cur.id} ${{queued:"排队",running:"🗣 进行中…",done:"✅ 已收口",failed:"中断"}[cur.status]||cur.status}
    ${cur.status==="done"?`<span style="float:right"><a class="btn sm" href="/api/meetings/${cur.id}/export.pdf">⬇️ PDF</a>
    <a class="btn sm" href="/api/meetings/${cur.id}/export.docx">⬇️ Word</a></span>`:""}</h2>
    <div class="sub" style="margin-bottom:6px">议题:${esc(cur.question)}</div>
    <div class="chips">${(cur.members||[]).map(b=>`<span class="tag">${b.emoji||"🧑‍💼"} ${esc(b.name)}</span>`).join("")}</div>
    ${mtFlow(cur)}
    ${mtConsensus(cur)}
    ${mtBody(cur)}
    ${(cur.actions||[]).length?`<div class="card" style="background:#fff;margin-top:12px"><h3 style="margin-top:0">📋 已锁定行动 · 责任到人</h3>
      ${cur.actions.map((a,i)=>`<div class="topic" style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
        <div style="flex:1;min-width:220px"><b>${esc(a.who)}</b>:${esc(a.task)}${a.acceptance?`<div class="sub">验收:${esc(a.acceptance)}</div>`:""}</div>
        ${a.task_id?`<span class="tag" style="background:#a7ecc9">✅ 任务 #${a.task_id}</span>`:
          !cur.decision?`<button class="btn sm pri" onclick="mtAssign(${cur.id},${i},this)">🚀 派给TA(1点)</button>`:""}</div>`).join("")}
      ${cur.phase==="awaiting_execution"?`<button class="btn pri" style="margin-top:8px" onclick="mtExecute(${cur.id},this)">🚀 执行会议决定</button>`:
        !cur.decision?`<button class="btn sm" style="margin-top:8px" onclick="mtAssignAll(${cur.id},this)">⚡ 全部派活</button>`:""}</div>`:""}
    ${(cur.execution_tasks||[]).length?`<div class="card" style="background:#e9f8ef;margin-top:12px"><h3 style="margin-top:0">🚀 已启动执行</h3>
      ${cur.execution_tasks.map(t=>`<div class="topic" style="display:flex;gap:9px;align-items:center;flex-wrap:wrap">
        <div style="flex:1;min-width:220px"><b>#${t.id} · ${esc(t.name)}</b><div class="sub">${esc(t.task)}</div></div>
        <span class="pill ${t.status==="done"?"done":t.status==="failed"?"failed":"running"}">${{queued:"排队",running:"执行中",done:"已交付",failed:"失败"}[t.status]||t.status}</span>
        <button class="btn sm" onclick="mtOpenTask(${t.id},${t.emp_idx})">查看任务</button></div>`).join("")}</div>`:""}
    ${cur.status!=="running"?`<div class="card" style="background:#e7f0ff;margin-top:12px"><b>👔 老板介入:补约束 / 挑战决定</b>
      <div class="row" style="margin-top:6px"><div style="flex:3"><input id="mt-ask" placeholder="例:预算砍一半还能干吗?/ 这个 GO 的关键证据是什么?"></div>
        <div><button class="btn pri" ${(cur.intervention_count||0)>=2?"disabled":""} onclick="mtAsk(${cur.id},this)">重新验证(${(cur.members||[]).length}点)</button></div></div>
      <div class="sub" style="margin-top:4px">这不是继续闲聊：员工只补新证据，主持人立即重做决定并更新 Next Action。每场最多 2 次，已用 ${cur.intervention_count||0} 次。</div></div>`:""}
  </div>`:""}
  ${list.length||meetingsContract.offset>0?`<div class="card"><h2>历史会议(${meetingsContract.total??list.length})</h2>
    ${listContractNotice(meetingsContract,"历史会议")}${list.length?list.map(x=>`
    <div class="topic" style="cursor:pointer" onclick="location.hash='#/meetings/${x.id}'">
      <span class="t">#${x.id} ${esc(x.question.slice(0,42))}</span>
      <span style="float:right">${mtDecisionBadge(x.decision)} <span class="pill ${x.status==="done"?"done":x.status==="failed"?"failed":"running"}">${{queued:"排队",running:"进行中",done:"已收口",failed:"中断"}[x.status]||x.status}</span></span>
      <div class="sub" style="margin-top:3px">${(x.members||[]).map(b=>esc(b.name)).join(" · ")} · ${esc(MT_PHASE_LABEL[x.phase]||"")}${x.task_count?` · 已派 ${x.task_count} 单`:""}</div></div>`).join(""):
      `<div class="empty">这一页没有历史会议。</div>`}
    ${listPager(meetingsContract,"meetings")}</div>`:""}`;
  const box=$("#mt-msgs"); if(box) box.scrollTop=box.scrollHeight;
}
let MT_POOL = [];
function mtPickedHtml(pool){
  MT_POOL = pool;
  const picked = pool.filter(p=>MT_SEL.has(p.idx));
  if(!picked.length) return `<span class="sub">还没选人。点下面分组里的名字,或「🤖 自动选人」。</span>`;
  return picked.map(p=>`<span class="tag" style="font-size:13px;padding:3px 10px">${p.emoji} ${esc(p.label)}
    <span style="cursor:pointer;font-weight:900" onclick="mtToggleIdx(${p.idx})">✕</span></span>`).join("");
}
function mtRefreshPicked(){ const box=$("#mt-picked"); if(box) box.innerHTML=mtPickedHtml(MT_POOL); $("#mt-n").textContent=MT_SEL.size; }
function mtToggleIdx(i){
  MT_SEL.delete(i);
  document.querySelectorAll(`.mt-chip[data-i="${i}"]`).forEach(el=>el.classList.remove("on"));
  mtRefreshPicked();
}
async function mtSuggest(btn){
  const q = $("#mt-q").value.trim();
  if(!q) return toast("先写议题,我才知道该请谁");
  btn.disabled=true; btn.innerHTML=`<span class="spin"></span> 选人中…`;
  try{
    const r = await api("/meetings/suggest",{method:"POST",body:{question:q}});
    MT_SEL = new Set(r.idxs);
    document.querySelectorAll(".mt-chip").forEach(el=>el.classList.toggle("on", MT_SEL.has(+el.dataset.i)));
    mtRefreshPicked();
    toast(`已自动选好 ${r.idxs.length} 人:${r.members.map(m=>m.name).join("、")}`);
  }catch(e){ toast(e.message); }
  btn.disabled=false; btn.textContent="🤖 按议题自动选人";
}
function mtToggle(el){
  const i = +el.dataset.i;
  if(MT_SEL.has(i)){ MT_SEL.delete(i); el.classList.remove("on"); }
  else if(MT_SEL.size>=6){ toast("最多拉 6 人"); return; }
  else { MT_SEL.add(i); el.classList.add("on"); }
  mtRefreshPicked();
}
async function mtAssign(mid, i, btn){
  const cur = await api("/meetings/"+mid); const a = (cur.actions||[])[i]; if(!a) return;
  btn.disabled=true; btn.innerHTML=`<span class="spin"></span>`;
  try{ const r = await api("/tasks",{method:"POST",body:{emp_idx:a.idx, brief:{direction:a.task, industry:""}}});
    toast(`已派给 ${a.who}`); btn.outerHTML=`<span class="tag" style="background:#a7ecc9">✅ 已派 <button type="button" class="link-btn" onclick="openSpec(${Number(a.idx)||0})" style="text-decoration:underline">看进度</button></span>`;
  }catch(e){ toast(e.message); btn.disabled=false; btn.textContent="🚀 派给TA(1点)"; }
}
async function mtAssignAll(mid, btn){
  const cur = await api("/meetings/"+mid);
  if(!await uiConfirm(`把 ${cur.actions.length} 条行动计划全部派活? 共 ${cur.actions.length} 点。`,{
    title:"批量派活",confirmText:"全部派出",danger:false
  })) return;
  btn.disabled=true; btn.innerHTML=`<span class="spin"></span> 派活中…`;
  let ok=0; const failures=[];
  for(const a of cur.actions){
    try{
      await api("/tasks",{method:"POST",body:{emp_idx:a.idx, brief:{direction:a.task}}});
      ok++;
    }catch(e){
      failures.push({who:a.who||`员工 #${a.idx}`,task:a.task||"",error:e?.message||"请求失败"});
    }
  }
  btn.disabled=false; btn.textContent="⚡ 全部派活";
  const detail=failures.map((item,index)=>
    `${index+1}. ${item.who}：${item.task.slice(0,60)}\n原因：${item.error}`).join("\n\n");
  await uiAlert({
    title:failures.length?"批量派活完成（部分失败）":"批量派活完成",
    message:`成功 ${ok} 单，失败 ${failures.length} 单。${detail?`\n\n${detail}`:"\n\n全部任务已派出，可到员工面板查看进度。"}`
  });
  render();
}
async function mtExecute(mid, btn){
  btn.disabled=true; btn.innerHTML=`<span class="spin"></span> 正在启动任务…`;
  try{ const r=await api(`/meetings/${mid}/execute`,{method:"POST"});
    toast(`已启动 ${r.task_ids.length} 个执行任务`); render();
  }catch(e){ toast(e.message); btn.disabled=false; btn.textContent="🚀 执行会议决定"; }
}
async function mtOpenTask(tid, idx){
  if(idx<100){ MODAL_IDX=idx; MODAL_TAB="solo"; SOLO_TASK=await api("/tasks/"+tid); drawModal(); }
  else { await openSpec(idx); SPEC_TAB="task"; await specOpenTask(tid); }
}
async function mtAsk(mid, btn){
  const q = $("#mt-ask").value.trim(); if(!q) return toast("先写您要追问的话");
  btn.disabled=true; btn.innerHTML=`<span class="spin"></span> 发问中…`;
  try{ await api(`/meetings/${mid}/ask`,{method:"POST",body:{question:q}}); $("#mt-ask").value=""; render(); }
  catch(e){ toast(e.message); btn.disabled=false; btn.textContent="重新验证"; }
}
async function mtStart(btn){
  const q = $("#mt-q").value.trim();
  if(!q) return toast("议题必填");
  if(MT_SEL.size<2) return toast("至少拉 2 位员工");
  btn.disabled=true; btn.innerHTML=`<span class="spin"></span> 开会中…`;
  try{
    const r = await api("/meetings",{method:"POST",body:{question:q, emp_idxs:[...MT_SEL],
      constraints:$("#mt-constraints")?.value.trim()||"",
      acceptance_criteria:$("#mt-acceptance")?.value.trim()||"",
      auto_execute:$("#mt-auto")?.checked!==false}});
    MT_CUR = r.meeting_id; MT_SEL = new Set(); toast("结果型会议开始：提案 → 验证 → 执行");
    location.hash="#/meetings/"+r.meeting_id;
  }catch(e){ toast(e.message); btn.disabled=false; btn.textContent="🎯 开会并收敛(每人1点)"; }
}

/* ---------- 启动 ---------- */
function fbOpen(){
  if($("#fb-ov")) return;
  document.body.insertAdjacentHTML("beforeend", `<div class="overlay" id="fb-ov" onclick="if(event.target===this)$('#fb-ov').remove()">
    <div class="panel" style="max-width:460px"><div class="phead"><b style="flex:1;font-size:17px">💬 意见反馈</b>
      <button class="btn sm" onclick="$('#fb-ov').remove()">✕</button></div>
    <div class="pbody"><div class="sub">用得顺不顺、缺什么功能、哪儿有 bug——直接说,会发到我们邮箱,认真看。</div>
      <textarea id="fb-text" style="min-height:120px;margin-top:8px" placeholder="您的建议或问题…"></textarea>
      <label>联系方式(选填)</label><input id="fb-contact" placeholder="微信/手机/邮箱">
      <div class="actions"><button class="btn pri" onclick="fbSend(this)">📮 提交反馈</button></div>
    </div></div></div>`);
}
async function fbSend(btn){
  const text = $("#fb-text").value.trim(); if(!text) return toast("写点内容再提交");
  btn.disabled=true; btn.innerHTML=`<span class="spin"></span> 提交中…`;
  try{ await api("/feedback",{method:"POST",body:{text, contact:$("#fb-contact").value.trim()}});
    toast("✅ 已收到,谢谢您的反馈!"); $("#fb-ov").remove();
  }catch(e){ toast(e.message); btn.disabled=false; btn.textContent="📮 提交反馈"; }
}
if(!$("#fb-btn")) document.body.insertAdjacentHTML("beforeend", `<button id="fb-btn" onclick="fbOpen()" title="意见反馈">💬</button>`);

/* ================ V24:公众号排版 · 草稿箱 · 审查官 · 真实图库 · 发布渠道 ================ */

/* ---------- 公众号排版弹窗(12 主题 + 一键复制 + 发草稿箱) ---------- */
let MP_CUR = null;
async function mpOpen(jobId){
  MP_CUR = {job:jobId, theme: localStorage.getItem("mp_theme")||"orange"};
  document.body.insertAdjacentHTML("beforeend", `<div class="overlay" id="mp-ov" onclick="if(event.target===this)this.remove()">
    <div class="panel" style="max-width:920px;width:96vw;padding:18px">
      <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
        <h2 style="flex:1;margin:0;min-width:180px">📰 公众号排版</h2>
        <button class="btn sm" onclick="$('#mp-ov').remove()">✕ 关闭</button></div>
      <div class="sub" style="margin:6px 0">选一套主题 → <b>一键复制(带排版)</b>去公众号编辑器粘贴;或直接<b>发草稿箱</b>(发布前审查官自动终审)。素材图已自动织入正文。</div>
      <div class="chips" id="mp-themes" style="margin:8px 0"></div>
      <div id="mp-report"></div>
      <div class="actions" style="margin:10px 0">
        <button class="btn pri" onclick="mpCopy(this)">📋 一键复制(带排版)</button>
        <button class="btn pri" onclick="mpDraft(this)">📮 审查并发草稿箱(1点)</button>
        <a class="btn sm" href="https://mp.weixin.qq.com/" target="_blank">↗ 公众号后台</a>
        <a class="btn sm" href="#/channels" onclick="$('#mp-ov').remove()">⚙️ 配置公众号</a></div>
      <iframe id="mp-frame" sandbox referrerpolicy="no-referrer" style="width:100%;height:56vh;border:2px solid var(--line);border-radius:12px;background:#fff"></iframe>
    </div></div>`);
  await mpLoad();
}
async function mpLoad(){
  try{
    const r = await api(`/jobs/${MP_CUR.job}/mp-html?theme=${MP_CUR.theme}`);
    MP_CUR.html = r.html; MP_CUR.title = r.title;
    $("#mp-themes").innerHTML = r.themes.map(t=>`<span class="chip${t.key===MP_CUR.theme?" on":""}"
      onclick="MP_CUR.theme=${cp(t.key)};localStorage.setItem('mp_theme',${cp(t.key)});mpLoad()">
      <span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:${t.color};margin-right:4px"></span>${t.emoji} ${t.name}</span>`).join("");
    $("#mp-frame").srcdoc = `<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1">
      <body style="margin:0;background:#f2f2f2"><div style="max-width:414px;margin:0 auto;background:#fff;min-height:100vh;padding:20px 16px">
      <div style="font-size:21px;font-weight:800;color:#111;line-height:1.4;margin-bottom:14px">${esc(MP_CUR.title||"")}</div>${r.html}</div></body>`;
  }catch(e){ $("#mp-report").innerHTML = `<div class="notice red">${esc(e.message)}</div>`; }
}
async function mpCopy(btn){
  if(!MP_CUR?.html) return toast("排版还没生成好");
  try{
    const item = new ClipboardItem({
      "text/html": new Blob([MP_CUR.html],{type:"text/html"}),
      "text/plain": new Blob([MP_CUR.html.replace(/<[^>]+>/g,"")],{type:"text/plain"})});
    await navigator.clipboard.write([item]);
    toast("✅ 已复制(带排版):去公众号编辑器直接粘贴,标题另填");
  }catch(e){ copyText(MP_CUR.html); toast("浏览器不支持富文本复制,已复制 HTML 源码"); }
}
async function mpDraft(btn){
  if(!MP_CUR) return;
  btn.disabled=true; btn.innerHTML=`<span class="spin"></span> 审查+推送中…`;
  try{
    const r = await api(`/jobs/${MP_CUR.job}/wechat-draft`,{
      method:"POST", body:{theme:MP_CUR.theme}, timeout:600000, longRunning:true
    });
    if(r.blocked){
      $("#mp-report").innerHTML = `<div class="notice red">⛔ ${esc(r.note)}</div>`+censorReportHtml(r.report);
    }else{
      $("#mp-report").innerHTML = `<div class="notice green">✅ ${esc(r.note)}</div>`+censorReportHtml(r.report);
      toast("📮 已发到公众号草稿箱");
    }
  }catch(e){
    let controls = "";
    if(/待确认|不确定|暂停重复|不会重复|人工确认/.test(e.message)){
      controls = await mpDeliveryControls().catch(()=>"");
    }
    $("#mp-report").innerHTML = `<div class="notice red">${esc(e.message)}${/配置/.test(e.message)?` <a href="#/channels" onclick="$('#mp-ov').remove()" style="text-decoration:underline">去发布渠道页配置 →</a>`:""}</div>${controls}`;
  }
  btn.disabled=false; btn.textContent="📮 审查并发草稿箱(1点)";
}
async function mpDeliveryControls(){
  if(!MP_CUR) return "";
  const state = await api(`/jobs/${MP_CUR.job}/wechat-delivery`);
  const item = (state.items||[]).find(x=>x.needs_reconciliation);
  if(!item) return "";
  const wait = Math.max(0, Number(item.confirm_wait_seconds||0));
  return `<div class="notice" style="margin-top:8px">
    <b>这笔投递不会自动重发。</b>先点“重新对账”，系统只读取草稿箱、不创建新草稿。
    <div class="actions" style="margin-top:8px">
      <button class="btn sm" onclick="mpReconcile(${Number(item.id)})">🔎 重新对账</button>
      <button class="btn sm" ${item.can_confirm_not_delivered&&isAdmin()?"":`disabled title="${isAdmin()?`约 ${wait} 秒后可用`:"退点解锁需企业主账号操作"}"`}
        onclick="mpConfirmNoDraft(${Number(item.id)},${cp(item.title||"")})">↩ 确认未送达并退点</button>
      <a class="btn sm" href="https://mp.weixin.qq.com/" target="_blank">↗ 打开公众号草稿箱</a>
    </div>
    <div class="sub">${!isAdmin()?"退点解锁需企业主账号在公众号后台确认无草稿后操作,请转告主账号。"
      :item.can_confirm_not_delivered
      ?"只有亲自在公众号后台确认没有这篇草稿，才能执行退点解锁。"
      :`微信仍可能在处理，约 ${wait} 秒后才能人工确认。`}</div>
  </div>`;
}
async function mpReconcile(deliveryId){
  try{
    const r = await api(`/wechat-deliveries/${deliveryId}/reconcile`,{
      method:"POST", body:{}, timeout:180000, longRunning:true
    });
    $("#mp-report").innerHTML = `<div class="notice green">✅ ${esc(r.note||"已完成对账")}</div>`+censorReportHtml(r.report);
    toast("草稿对账完成");
  }catch(e){
    const controls = await mpDeliveryControls().catch(()=>"");
    $("#mp-report").innerHTML = `<div class="notice red">${esc(e.message)}</div>${controls}`;
  }
}
async function mpConfirmNoDraft(deliveryId,title){
  const typed = await uiPrompt({
    title:"确认公众号未收到草稿",
    message:`请先打开公众号后台草稿箱确认没有这篇文章。\n\n为防误退，请完整输入标题：\n${title}`,
    label:"完整标题",confirmText:"核对标题",
    validate:value=>value.trim()===String(title||"").trim()?"":"标题不一致，未执行任何操作"
  });
  if(typed===null) return;
  if(!await uiConfirm("公众号草稿箱里确实没有这篇草稿？确认后会退点并允许重新发送。",{
    title:"最后一次确认",confirmText:"确认未收到"
  })) return;
  try{
    const r = await api(`/wechat-deliveries/${deliveryId}/confirm-not-delivered`,{
      method:"POST",
      body:{confirmed_no_draft:true,title_confirmation:typed},
      timeout:180000,
      longRunning:true
    });
    $("#mp-report").innerHTML = `<div class="notice green">✅ ${esc(r.note||"已退点解锁")}</div>`;
    toast("已退点，可以重新发送");
  }catch(e){
    const controls = await mpDeliveryControls().catch(()=>"");
    $("#mp-report").innerHTML = `<div class="notice red">${esc(e.message)}</div>${controls}`;
  }
}

/* ---------- 审查官 ---------- */
function censorReportHtml(r){
  if(!r) return "";
  const cls = r.verdict==="pass"?"green":r.verdict==="warn"?"":"red";
  return `<div class="notice ${cls}" style="margin:8px 0">${esc(r.label||"")} · 合规分 <b>${r.score??"—"}</b>${r.summary?` · ${esc(r.summary)}`:""}</div>`
    + (r.issues||[]).map(i=>`<div class="sub" style="margin:4px 0">${i.hard||i.severity==="高"?"⛔":i.severity==="中"?"⚠️":"ℹ️"}
      [${esc(i.severity||"提示")}·${esc(i.type||"")}] ${esc(i.detail||"")}
      ${i.suggest?`<br>&nbsp;&nbsp;&nbsp;💡 建议:<b>${esc(i.suggest)}</b>`:""}</div>`).join("");
}
async function censorQuick(platform, title, body){
  document.body.insertAdjacentHTML("beforeend", `<div class="overlay" id="cq-ov" onclick="if(event.target===this)this.remove()">
    <div class="panel" style="max-width:720px;width:94vw;padding:18px">
      <div style="display:flex;gap:10px;align-items:center"><h2 style="flex:1;margin:0;min-width:min(100%,200px)">🛡️ 审查官 · ${esc(platform)}终审</h2>
        <button class="btn sm" onclick="$('#cq-ov').remove()">✕</button></div>
      <div id="cq-out"><div class="empty">🛡️ 审查官正在逐条对照${esc(platform)}平台规范审查…(约 20-60 秒)</div></div></div></div>`);
  try{
    const r = await api("/censor/check",{method:"POST",body:{platform, title, body},
      timeout:330000,longRunning:true});
    if($("#cq-out")) $("#cq-out").innerHTML = censorReportHtml(r);
  }catch(e){ if($("#cq-out")) $("#cq-out").innerHTML = `<div class="notice red">${esc(e.message)}</div>`; }
}
let CEN_TAB="pre", CEN_LOG_FILTER={kind:"",platform:""};
const CEN_PLATFORMS = ["公众号","小红书","抖音","视频号","微博","B站"];
function cenPf(){ return $("#cen-pf .on")?.textContent.trim()||"公众号"; }
function setCensorLogFilter(key,value){
  CEN_LOG_FILTER[key]=value; resetListPage("censor"); render();
}
async function censorView(){
  const tabs = [["pre","🛡️ 发前审查"],["retro","📊 发后数据复盘"],["publog","📮 发布台账"],["logs","📜 审查记录"]];
  let body = "", listNotice="";
  if(CEN_TAB==="pre") body = `
    <div class="sub">发布前把内容交给审查官:<b>极速扫描</b>免费查广告法极限词/导流违规/违禁内容;<b>深度审查</b>由 AI 逐条对照目标平台规范,给出改写建议。交付包里每个平台版本也有「🛡️ 审查」按钮。</div>
    <label>目标平台</label><div class="chips" id="cen-pf">${CEN_PLATFORMS.map((p,i)=>`<span class="chip${i===0?" on":""}" onclick="pick(this)">${p}</span>`).join("")}</div>
    <label>标题</label><input id="cen-title" placeholder="要发布的标题">
    <label>正文</label><textarea id="cen-body" style="min-height:180px" placeholder="把要发布的正文粘贴进来"></textarea>
    <div class="actions">
      <button class="btn" onclick="cenScan()">⚡ 极速扫描(免费)</button>
      <button class="btn pri" onclick="cenDeep(this)">🛡️ 深度审查(1点)</button></div>
    <div id="cen-out" style="margin-top:10px"></div>`;
  else if(CEN_TAB==="retro") body = `
    <div class="sub">发布一段时间后,把平台数据丢给审查官:判读数据档次、体检疑似限流信号、回看内容合规、给下一步行动清单。数据截图可直接上传,自动识别成文字。</div>
    <label>平台</label><div class="chips" id="cen-pf">${CEN_PLATFORMS.map((p,i)=>`<span class="chip${i===0?" on":""}" onclick="pick(this)">${p}</span>`).join("")}</div>
    <label>内容标题</label><input id="cenr-title" placeholder="发布的是哪篇">
    <label>发布后的数据(文字或截图)</label>
    <input type="file" multiple accept=".jpg,.jpeg,.png,.webp,.txt,.csv,.pdf" onchange="parseFileInto(this,'#cenr-data')" style="margin-bottom:6px">
    <textarea id="cenr-data" style="min-height:140px" placeholder="例:发布3天,阅读1240,点赞18,在看3,转发7,涨粉12,完读率23%…(后台截图直接传上面)"></textarea>
    <div class="actions"><button class="btn pri" onclick="cenRetro(this)">📊 让审查官复盘(1点)</button></div>
    <div id="cenr-out" style="margin-top:10px"></div>`;
  else if(CEN_TAB==="publog"){
    const publogContract=normalizeListContract(
      await api(listPath("/publog","publog")).catch(optionalResult([])),60);
    const rows=publogContract.items;
    const retroConf=await api("/publog/auto-retro").catch(()=>({enabled:true}));
    listNotice=listContractNotice(publogContract,"发布记录");
    body = `<div class="sub">发布台账 = 自动复盘的钟表:发草稿箱会自动登记;其他平台发完花10秒登记一下,到 T+1/3/7 审查官自动来找您复盘(公众号配了API且已群发的,数据都自动拉)。<b>💎 每次自动复盘扣 1 点(每篇最多 T+1/3/7 三次共 3 点)</b>;不想复盘的记录点 🗑 删除即停止。</div>
    <div class="notice" style="margin-top:8px"><label style="display:flex;gap:8px;align-items:flex-start;margin:0;cursor:${isAdmin()?"pointer":"default"}">
      <input type="checkbox" style="margin-top:3px" ${retroConf.enabled?"checked":""} ${isAdmin()?"":"disabled"} onchange="retroAutoToggle(this)">
      <span><b>自动复盘总开关(全公司)</b>:开=到点自动跑、自动扣点;关=所有台账都不自动复盘、也不发到期提醒,想看数据时点各条的「📥 自动拉数据复盘」手动跑(同样 1 点/次)。${isAdmin()?"":"(仅企业主可改)"}</span></label></div>
    <div class="row" style="align-items:flex-end;margin-top:8px">
      <div style="flex:1;min-width:130px"><label>平台</label><select id="pl-pf">${CEN_PLATFORMS.map(p=>`<option>${p}</option>`).join("")}</select></div>
      <div style="flex:2;min-width:200px"><label>标题</label><input id="pl-title" placeholder="发布的内容标题"></div>
      <div style="flex:1;min-width:110px"><label>发布于</label><select id="pl-days">${[["0","今天"],["1","昨天"],["2","前天"],["3","3天前"],["7","7天前"]].map(([v,l])=>`<option value="${v}">${l}</option>`).join("")}</select></div>
      <button class="btn pri" onclick="plAdd()">➕ 登记</button>
      <a class="btn" href="/api/records/export.xlsx?kind=publog">⬇️ Excel</a></div>
    <div style="margin-top:12px">${rows.length?rows.map(r=>{
      const st = d=>({pending:"⏳",notified:"🔔",done:"✅"}[(r.retro[d]||{}).state]||"—");
      return `<div class="topic"><span class="tag">${esc(r.platform||"")}</span> <b>${esc(r.title||"")}</b>
        <span class="sub" style="float:right">${new Date((r.published_at||r.created_at)*1000).toLocaleDateString("zh-CN")} ${r.source==="draft"?"· 草稿箱推送":""}</span>
        <div class="sub" style="margin-top:5px">复盘:T+1 ${st("1")} · T+3 ${st("3")} · T+7 ${st("7")}
          <button class="btn sm" style="margin-left:8px" onclick="plPull(${r.id},this)">📥 自动拉数据复盘</button>
          <button class="btn sm" onclick="plMark(${r.id})" title="把复盘计时从现在重新起算">✔ 我刚群发</button>
          <button class="btn sm bad" onclick="plDel(${r.id})" title="删除后不再自动复盘扣点">🗑</button></div></div>`;}).join(""):`<div class="empty">还没有登记,发草稿箱会自动记一笔</div>`}</div>
    ${listPager(publogContract,"publog")}`;
  }
  else {
    const logContract=normalizeListContract(
      await api(listPath("/censor/logs","censor",CEN_LOG_FILTER))
        .catch(optionalResult([])),LIST_PAGE_SIZE);
    const logs=logContract.items;
    listNotice=listContractNotice(logContract,"审查记录");
    body = `<div class="row" style="margin-bottom:10px">
      <select onchange="setCensorLogFilter('kind',this.value)">
        <option value="">全部类型</option><option value="pre" ${CEN_LOG_FILTER.kind==="pre"?"selected":""}>发前审查</option>
        <option value="retro" ${CEN_LOG_FILTER.kind==="retro"?"selected":""}>发后复盘</option>
      </select>
      <select onchange="setCensorLogFilter('platform',this.value)">
        <option value="">全部平台</option>${CEN_PLATFORMS.map(p=>`<option ${CEN_LOG_FILTER.platform===p?"selected":""}>${esc(p)}</option>`).join("")}
      </select>
      <a class="btn sm" style="margin-left:auto" href="/api/records/export.xlsx?kind=censor">⬇️ 导出Excel(含完整报告)</a></div>`+(logs.length? logs.map(l=>`<div class="topic">
      <span class="tag">${l.kind==="pre"?"发前审查":"发后复盘"}</span> <span class="tag">${esc(l.platform||"")}</span>
      <b>${esc(l.title||"(无标题)")}</b>
      <span class="sub" style="float:right">${new Date(l.created_at*1000).toLocaleString("zh-CN")}</span>
      <div class="sub" style="margin-top:4px">${l.kind==="pre"?`结论:${esc(l.verdict||"")} · 合规分 ${l.score??"—"} · 问题 ${(l.issues||[]).length} 条`:`评级:${esc(l.verdict||"—")}`}</div>
      ${(l.issues||[]).length||l.report?`<details style="margin-top:6px"><summary class="sub" style="cursor:pointer;font-weight:800">📄 展开完整报告</summary>
        ${(l.issues||[]).map(i=>`<div class="notice" style="margin-top:6px"><b>[${esc(i.severity||"")}] ${esc(i.type||"")}</b>:${esc(i.detail||"")}${i.suggest?`<div class="sub">改法:${esc(i.suggest)}</div>`:""}</div>`).join("")}
        ${l.report?`<div class="md" style="margin-top:6px">${md(l.report)}</div>`:""}</details>`:""}
    </div>`).join("") : `<div class="empty">还没有审查记录</div>`)
      +listPager(logContract,"censor");
  }
  $("#main").innerHTML = `
  <div class="card" style="background:linear-gradient(120deg,#fdecee,#fffaf0 65%)">
    <div style="display:flex;gap:14px;align-items:center;flex-wrap:wrap">
      ${charSVG("#e63946","🛡️","idle",92)}
      <div style="flex:1;min-width:min(100%,240px)"><div class="sub" style="font-weight:800">🎬 内容生产部 · 合规审查部</div>
      <h2 style="margin:2px 0 0">🛡️ 审查官的工作台</h2>
      <div class="sub" style="margin-top:5px">流水线质检关卡就是他;发布前把关(广告法/平台规范/敏感违禁),发布后复盘(数据判读/限流体检)。公众号、小红书等平台规范逐条对照,发草稿箱前会自动终审。</div></div>
      <a class="btn sm" href="#/">← 回办公室</a></div></div>
  <div class="card">
    <div class="tabs">${tabs.map(([k,l])=>`<span class="tb ${CEN_TAB===k?"on":""}" onclick="CEN_TAB=${cp(k)};resetListPage('censor');resetListPage('publog');render()">${l}</span>`).join("")}</div>
    ${listNotice}
    <div style="margin-top:10px">${body}</div></div>`;
}
async function retroAutoToggle(cb){
  try{
    await api("/publog/auto-retro",{method:"POST",body:{enabled:cb.checked}});
    toast(cb.checked?"✅ 自动复盘已开启,到点自动跑":"⏸ 自动复盘已全部暂停,不再自动扣点(手动复盘不受影响)");
  }catch(e){ cb.checked=!cb.checked; toast(e.message); }
}
async function plDel(id){
  if(!await uiConfirm("删除这条台账?删除后该篇不再自动复盘,也不再扣复盘点。",{okText:"删除",okClass:"bad"})) return;
  try{ await api("/publog/"+id,{method:"DELETE"}); toast("已删除,该篇自动复盘停止"); render(); }
  catch(e){ toast(e.message); }
}
async function cenScan(){
  const r = await api("/censor/scan",{method:"POST",body:{title:$("#cen-title").value,body:$("#cen-body").value,platform:cenPf()}}).catch(e=>({error:e.message}));
  $("#cen-out").innerHTML = r.error?`<div class="notice red">${esc(r.error)}</div>`
    :censorReportHtml({...r, label:{pass:"✅ 词库未命中,可以发布",warn:"⚠️ 命中风险词,建议整改",block:"⛔ 命中违禁词,不能发布"}[r.verdict]});
}
async function cenDeep(btn){
  btn.disabled=true; btn.innerHTML=`<span class="spin"></span> 审查官工作中…`;
  try{
    const r = await api("/censor/check",{method:"POST",body:{title:$("#cen-title").value,body:$("#cen-body").value,platform:cenPf()},
      timeout:330000,longRunning:true});
    $("#cen-out").innerHTML = (r.degraded?`<div class="notice red">⚠️ AI 深审临时没跑完,<b>1 点已自动退回</b>;下面是免费规则词库的扫描结果,可稍后再点深度审查。</div>`:"")
      + censorReportHtml(r);
  }catch(e){ $("#cen-out").innerHTML = `<div class="notice red">${esc(e.message)}</div>`; }
  btn.disabled=false; btn.textContent="🛡️ 深度审查(1点)";
}
async function cenRetro(btn){
  btn.disabled=true; btn.innerHTML=`<span class="spin"></span> 复盘中(约1分钟)…`;
  try{
    const r = await api("/censor/retro",{method:"POST",body:{platform:cenPf(),title:$("#cenr-title").value,data_text:$("#cenr-data").value},
      timeout:330000,longRunning:true});
    $("#cenr-out").innerHTML = `<div class="notice ${r.grade==="优秀"||r.grade==="良好"?"green":""}">评级:<b>${esc(r.grade||"—")}</b>${(r.signals||[]).length?` · ⚠️ 疑似信号 ${(r.signals||[]).length} 条`:" · 无明显限流信号"}</div>
      <div class="md">${md(r.report||"")}</div>`;
  }catch(e){ $("#cenr-out").innerHTML = `<div class="notice red">${esc(e.message)}</div>`; }
  btn.disabled=false; btn.textContent="📊 让审查官复盘(1点)";
}

/* ---------- 真实素材图库(交付页) ---------- */
let HUNT_ITEMS = [];
async function huntGo(jobId){
  const q = $("#hunt-q").value.trim(); if(!q) return toast("输入画面关键词");
  $("#hunt-res").innerHTML = `<div class="empty" style="grid-column:1/-1">🔎 必应/百度/360 三路搜图中…</div>`;
  try{
    const r = await api(`/imagehunt?q=${encodeURIComponent(q)}&n=18`);
    HUNT_ITEMS = r.items;
    $("#hunt-res").innerHTML = r.items.length? r.items.map((it,i)=>`
      <div class="thumb" title="点击加入本工单素材" onclick="huntAdd(${jobId},${i},this)" style="aspect-ratio:4/3">
        <img loading="lazy" src="/api/imagehunt/thumb?u=${encodeURIComponent(it.thumb)}&f=${encodeURIComponent(it.from)}"
          style="width:100%;height:100%;object-fit:cover" onerror="this.closest('.thumb').remove()"></div>`).join("")
      : `<div class="empty" style="grid-column:1/-1">没搜到,换个更具体的关键词(如加上「实拍/照片」)</div>`;
  }catch(e){ $("#hunt-res").innerHTML = `<div class="empty" style="grid-column:1/-1">${esc(e.message)}</div>`; }
}
async function huntAdd(jobId, i, el){
  const it = HUNT_ITEMS[i]; if(!it) return;
  el.style.opacity=.35;
  try{
    await api(`/jobs/${jobId}/add-image`,{method:"POST",body:{url:it.img,from:it.from,page:it.page,query:$("#hunt-q").value.trim()}});
    el.style.outline="3px solid #2f9e44"; toast("✅ 已入库到工单素材(排版和发布包都会带上)");
  }catch(e){ el.style.opacity=1; toast(e.message); }
}

/* ---------- 发布渠道(公众号配置 + 小红书风格) ---------- */
let MATRIX_FILTER={platform:"",status:""};
function setMatrixFilter(key,value){
  MATRIX_FILTER[key]=value; resetListPage("publish"); render();
}
async function channelsView(){
  const [wc,pstyles,wh,mx,mtasksPayload] = await Promise.all([
    api("/channels/wechat").catch(optionalResult(null)), api("/pstyles").catch(optionalResult({})),
    api("/channels/webhook").catch(optionalResult(null)), api("/matrix/accounts").catch(optionalResult({platforms:[],accounts:[]})),
    api(listPath("/matrix/tasks","publish",MATRIX_FILTER)).catch(optionalResult([]))]);
  const mtasksContract=normalizeListContract(mtasksPayload,LIST_PAGE_SIZE);
  const mtasks=mtasksContract.items;
  const xs = pstyles["小红书"]||{presets:[],custom:[]}, dys = pstyles["抖音"]||{presets:[],custom:[]};
  $("#main").innerHTML = `
  <div class="card"><h2>📣 发布渠道</h2><div class="sub">把「派活」和您的发布渠道打通:公众号一键进草稿箱;小红书定义您自己的内容风格。</div></div>
  ${wc?`<div class="card"><h3 style="margin-top:0">💬 微信公众号(一键发草稿箱)</h3>
    <div class="row">
      <div style="flex:1;min-width:220px"><label>AppID(开发者ID)</label><input id="wx-appid" value="${esc(wc.appid)}" placeholder="wx 开头的一串"></div>
      <div style="flex:1;min-width:220px"><label>AppSecret(开发者密码)${wc.secret_set?` <span class="tag">已配置 ${esc(wc.secret_masked)}</span>`:""}</label>
        <input id="wx-secret" type="password" placeholder="${wc.secret_set?"不改就留空":"在公众号后台生成后粘贴"}"></div></div>
    <div class="actions"><button class="btn pri" onclick="wxSave()">💾 保存</button>
      <button class="btn" onclick="wxTest(this)">🔌 测试连接</button></div>
    <div id="wx-out" style="margin-top:8px"></div>
    <details style="margin-top:10px"><summary style="cursor:pointer"><b>🧭 一键配置向导(1 分钟,照着点)</b></summary>
      <ol class="list" style="margin-top:8px">
        <li>电脑打开 <a href="https://mp.weixin.qq.com" target="_blank" style="text-decoration:underline">mp.weixin.qq.com</a> 登录您的公众号;</li>
        <li>左侧菜单最底下「设置与开发」→「基本配置」(或「开发接口管理」);</li>
        <li>复制「开发者ID(AppID)」填到上面;点「开发者密码(AppSecret)」旁的<b>生成/重置</b>,管理员扫码后复制密钥填到上面(只显示一次,丢了就再重置);</li>
        <li>同页往下找「IP白名单」→ 点修改 → 把服务器 IP ${wc.server_ip?`<code>${esc(wc.server_ip)}</code> 加进去
          <button class="btn sm" onclick="copyText(${cp(wc.server_ip)})">📋 复制IP</button>`:`加进去
          <span class="notice" style="display:inline-block;padding:4px 8px">⚠️ 平台还没配置服务器出口 IP——请联系平台顾问索取(点右下角 💬),拿到后粘进白名单即可</span>`};</li>
        <li>回到这里点「保存」→「测试连接」,通了就能在交付包一键发草稿箱。</li></ol>
      <div class="notice" style="font-size:12.5px">⚠️ 未认证的<b>个人订阅号</b>没有草稿箱接口权限(微信的限制),需要完成微信认证;企业主体的服务号/订阅号认证后都可用。发进草稿箱后,在公众号后台「草稿箱」里预览确认再群发,更稳。</div>
    </details></div>`:""}
  ${wh?`<div class="card"><h3 style="margin-top:0">🔔 微信通知(企微群机器人)</h3>
    <div class="sub">等拍板/交付完成/审查拦截/该复盘了/周报出炉 → 直接推到您微信。<b>1分钟配置</b>:手机打开企业微信→随便建个群(可以只有自己)→右上角…→「添加群机器人」→ 复制 Webhook 地址粘到下面。企业微信和个人微信消息互通,微信里就能收到。</div>
    <div class="row" style="align-items:flex-end;margin-top:8px">
      <div style="flex:1;min-width:260px"><label>Webhook 地址 ${wh.set?`<span class="tag">已配置</span>`:""}</label>
      <input id="wh-url" placeholder="${wh.set?esc(wh.masked):"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=…"}"></div>
      <button class="btn pri" onclick="whSave()">💾 保存</button>
      <button class="btn" onclick="whTest(this)">📨 发条测试消息</button></div>
    <div id="wh-out" style="margin-top:8px"></div></div>`:""}
  <div class="card"><h3 style="margin-top:0">🚀 矩阵发布中心 <span class="tag">beta</span></h3>
    <div class="sub">绑定小红书/抖音账号后,交付包里可<b>一键真发布</b>(服务器自动开浏览器带您的登录态填好并提交)。行业通行做法是"代持登录态",平台改版可能偶尔失灵——<b>失败必有出路</b>:自动给人话原因和现场截图,可一键转「🪄 半自动发布」或重试;发成功自动登记发布台账,到点自动复盘。建议每账号每天 ≤5 条,内容先过审查官。</div>
    <details style="margin:8px 0"><summary style="cursor:pointer"><b>🧭 取 Cookie 向导(2分钟)</b></summary>
      <ol class="list" style="margin-top:6px">
        <li>电脑 Chrome 登录 creator.xiaohongshu.com(或 creator.douyin.com);</li>
        <li>按 F12 打开开发者工具 → Network(网络)→ 刷新页面 → 点第一个请求;</li>
        <li>右侧 Request Headers 里找到 <code>cookie:</code> 那一行,<b>整行的值</b>复制出来;</li>
        <li>粘到下面,起个备注名,点绑定 → 自动验证登录态。</li></ol>
      <div class="notice red" style="margin-top:6px">🔐 <b>整行 Cookie 等于这个账号的登录密码</b>:只粘到下面的框里,不要截图、不要发给任何人(包括自称客服的);绑定后这里也不会再显示明文。</div></details>
    <div class="row" style="align-items:flex-end">
      <div style="flex:0 0 130px"><label>平台</label><select id="mx-pf">${(mx.platforms||[]).map(pf=>`<option value="${pf.key}">${pf.emoji} ${pf.name}</option>`).join("")}</select></div>
      <div style="flex:0 0 150px"><label>备注名</label><input id="mx-name" placeholder="如:主号"></div>
      <div style="flex:1;min-width:240px"><label>Cookie <span class="sub">(等于登录密码,勿外传)</span></label><input id="mx-cookie" type="password" autocomplete="off" placeholder="按向导复制整行 cookie 粘这里"></div>
      <button class="btn pri" onclick="mxAdd(this)">🔗 绑定并验证</button></div>
    <div id="mx-list" style="margin-top:10px">${(mx.accounts||[]).map(a=>`<div class="topic">
      <span style="font-size:16px">${a.emoji}</span> <b>${esc(a.name)}</b> <span class="sub">${esc(a.platform_name)}${a.nickname?` · ${esc(a.nickname)}`:""}</span>
      <span class="tag" style="${a.status==="ok"?"background:#a7ecc9":a.status==="expired"?"background:#ffc2c5":""}">${{ok:`✅ ${a.checked_at?new Date(a.checked_at*1000).toLocaleDateString("zh-CN")+" 验证有效":"有效"}`,expired:"⚠️ 已失效",unchecked:"未验证"}[a.status]||a.status}</span>
      ${a.status==="ok"&&a.checked_at&&(Date.now()/1000-a.checked_at)>7*86400?`<span class="sub">⏰ 距上次验证已超一周,发布前建议先点「🔌 验证」</span>`:""}
      <span style="float:right"><button class="btn sm" onclick="mxCheck(${cp(a.id)},this)">🔌 验证</button>
      <button class="btn sm bad" onclick="mxDel(${cp(a.id)})">🗑</button></span></div>`).join("")||`<div class="empty">还没绑定账号</div>`}</div>
    ${listContractNotice(mtasksContract,"矩阵发布记录")}
    <div class="row" style="margin-top:10px">
      <select onchange="setMatrixFilter('platform',this.value)">
        <option value="">全部发布平台</option><option value="xhs" ${MATRIX_FILTER.platform==="xhs"?"selected":""}>小红书</option>
        <option value="douyin" ${MATRIX_FILTER.platform==="douyin"?"selected":""}>抖音</option>
      </select>
      <select onchange="setMatrixFilter('status',this.value)">
        <option value="">全部状态</option>${[["queued","排队"],["running","发布中"],["done","完成"],["failed","失败"]]
          .map(([value,label])=>`<option value="${value}" ${MATRIX_FILTER.status===value?"selected":""}>${label}</option>`).join("")}
      </select></div>
    ${(mtasks||[]).length?`<details ${mtasks.some(t=>t.status!=="done")?"open":""} style="margin-top:10px"><summary class="sub" style="cursor:pointer">📜 矩阵发布记录(${mtasks.length})</summary>
      ${mtasks.map(t=>{
        const pfName = {xhs:"📕 小红书",douyin:"🎵 抖音"}[t.platform]||t.platform;
        const f = t.fail;
        return `<div class="topic"><span class="tag">${esc(pfName)}</span> <b>${esc(t.payload?.title||"")}</b>
        <span class="pill ${t.status}">${{queued:"排队",running:"发布中",done:"完成",failed:"失败"}[t.status]||t.status}</span>
        ${t.status==="failed"&&f?`<div class="notice red" style="margin-top:6px">❌ ${esc(f.why||"")}<br>👉 ${esc(f.fix||"")}
          <div class="actions" style="margin-top:8px">
            ${t.payload?.job_id?`<button class="btn sm pri" onclick="semiPub(${t.payload.job_id},${cp(pfName)},${cp(t.payload?.title||"")},${cp(t.payload?.body||"")},${cp(f.home||"")})">🪄 转半自动发布</button>`:""}
            ${t.retryable?`<button class="btn sm" onclick="mxRetry(${t.id},this)">🔁 免费重试</button>`
              :`<span class="sub">⚠️ ${esc(t.retry_block_reason||"这条发布不能自动重试，请先到平台后台核对")}</span>`}
            ${safeAssetUrl(f.shot)?`<a class="btn sm" href="${esc(safeAssetUrl(f.shot))}" target="_blank" rel="noopener noreferrer">📷 失败截图</a>`:""}
          </div></div>`:""}
        ${t.log?`<details style="margin-top:6px"><summary class="sub" style="cursor:pointer;font-size:12px">过程日志</summary><pre style="font-size:11px;white-space:pre-wrap;background:#faf6ec;padding:6px;border-radius:8px;margin-top:4px">${esc(t.log)}</pre></details>`:""}</div>`;}).join("")}</details>`:""}
    ${listPager(mtasksContract,"publish")}
  </div>
  <div class="card"><h3 style="margin-top:0">🎨 内容风格库(小红书 + 抖音)</h3>
    <div class="sub">定义您账号的内容风格,下达任务时选中,撰稿人/文风师/分发官全程贴着写。每个平台都有预设开箱即用,下面是您的自定义:</div>
    <div style="margin-top:8px"><b>📕 小红书</b>(预设 ${xs.presets.length} 套)</div>
    <div id="xhs-list">${(xs.custom||[]).map(s=>styleRow(s,"xhs")).join("")}</div>
    <div class="actions" style="margin-top:6px"><button class="btn sm" onclick="styleAdd('xhs')">➕ 加风格</button>
      <button class="btn sm pri" onclick="styleSave('小红书','xhs')">💾 保存小红书风格</button></div>
    <div style="margin-top:14px"><b>🎵 抖音</b>(预设 ${dys.presets.length} 套)</div>
    <div id="dy-list">${(dys.custom||[]).map(s=>styleRow(s,"dy")).join("")}</div>
    <div class="actions" style="margin-top:6px"><button class="btn sm" onclick="styleAdd('dy')">➕ 加风格</button>
      <button class="btn sm pri" onclick="styleSave('抖音','dy')">💾 保存抖音风格</button></div>
    <details style="margin-top:10px"><summary class="sub" style="cursor:pointer">看看全部平台预设</summary>
      <div class="sub" style="margin-top:6px"><b>小红书:</b></div>
      ${xs.presets.map(p=>`<div class="topic"><b>${esc(p.name)}</b><div class="sub">${esc(p.desc)}</div></div>`).join("")}
      <div class="sub" style="margin-top:6px"><b>抖音:</b></div>
      ${dys.presets.map(p=>`<div class="topic"><b>${esc(p.name)}</b><div class="sub">${esc(p.desc)}</div></div>`).join("")}</details></div>`;
  if(!(xs.custom||[]).length) styleAdd("xhs");
  if(!(dys.custom||[]).length) styleAdd("dy");
}
function styleRow(s, cls){
  return `<div class="row ${cls}-row" style="margin-top:6px;gap:6px">
    <input class="st-name" placeholder="风格名,如:老板娘唠嗑风" value="${esc(s?.name||"")}" style="flex:1;min-width:140px">
    <input class="st-desc" placeholder="描述:口吻/结构/emoji习惯/开头钩子/结尾…越具体越像" value="${esc(s?.desc||"")}" style="flex:3;min-width:220px">
    <button class="btn sm bad" style="flex:0 0 auto;width:auto;align-self:flex-start" onclick="this.parentNode.remove()">🗑</button></div>`;
}
function styleAdd(cls){ $("#"+(cls==="xhs"?"xhs":"dy")+"-list").insertAdjacentHTML("beforeend", styleRow(null,cls)); }
async function styleSave(platform, cls){
  const styles = [...document.querySelectorAll("."+cls+"-row")].map(r=>({
    name:r.querySelector(".st-name").value.trim(), desc:r.querySelector(".st-desc").value.trim()})).filter(s=>s.name);
  try{ await api("/pstyles",{method:"PUT",body:{platform, styles}}); toast(`✅ 已保存 ${styles.length} 个${platform}风格`); }
  catch(e){ toast(e.message); }
}
function xhsRow(s){
  return `<div class="row xhs-row" style="margin-top:6px;gap:6px;align-items:flex-start">
    <input class="xhs-name" placeholder="风格名,如:老板娘唠嗑风" value="${esc(s?.name||"")}" style="flex:1;min-width:140px">
    <input class="xhs-desc" placeholder="描述这个风格:口吻/结构/emoji习惯/开头钩子/结尾…越具体越像" value="${esc(s?.desc||"")}" style="flex:3;min-width:220px">
    <button class="btn sm bad" style="flex:0 0 auto;width:auto;align-self:flex-start" onclick="this.parentNode.remove()">🗑</button></div>`;
}
function xhsAdd(){ $("#xhs-list").insertAdjacentHTML("beforeend", xhsRow()); }
async function xhsSave(){
  const styles = [...document.querySelectorAll(".xhs-row")].map(r=>({
    name:r.querySelector(".xhs-name").value.trim(), desc:r.querySelector(".xhs-desc").value.trim()})).filter(s=>s.name);
  try{ await api("/xhs/styles",{method:"PUT",body:{styles}}); toast(`✅ 已保存 ${styles.length} 个风格,下达任务时可选`); }
  catch(e){ toast(e.message); }
}
async function wxSave(){
  try{ await api("/channels/wechat",{method:"PUT",body:{appid:$("#wx-appid").value,secret:$("#wx-secret").value}});
    toast("✅ 已保存,点「测试连接」验证"); }
  catch(e){ toast(e.message); }
}
async function wxTest(btn){
  btn.disabled=true; btn.textContent="测试中…";
  try{ const r = await api("/channels/wechat/test",{method:"POST"});
    $("#wx-out").innerHTML = `<div class="notice green">✅ ${esc(r.note)}</div>`;
  }catch(e){ $("#wx-out").innerHTML = `<div class="notice red">❌ ${esc(e.message)}</div>`; }
  btn.disabled=false; btn.textContent="🔌 测试连接";
}


/* ================ V25:工具箱 / 成片 / 台账 / 矩阵发布 前端 ================ */
async function tvCreate(jobId, btn){
  if(btn){ btn.disabled=true; btn.innerHTML=`<span class="spin"></span> 排队…`; }
  try{
    await api(`/jobs/${jobId}/text-video`,{method:"POST",body:{}});
    toast("🎬 已开工:配音+合成约2-4分钟,完成会推微信通知"); render();
  }catch(e){ toast(e.message); if(btn){ btn.disabled=false; btn.textContent="🎬 一键成片 3点"; } }
}
function tvListCard(TVS, MX, jobId){
  if(!(TVS||[]).length) return "";
  const dy = (MX.accounts||[]).find(a=>a.platform==="douyin");
  return `<div class="card"><h3 style="margin-top:0">🎬 视频成片(图文转视频)</h3>
    ${TVS.map(t=>{
      const last = (t.steps||[]).slice(-1)[0];
      return `<div class="topic"><b>${esc(t.params?.title||"成片")}</b>
        <span class="pill ${t.status}">${{queued:"排队",running:"合成中",done:"已出片",failed:"失败"}[t.status]||t.status}</span>
        ${t.status==="running"&&last?`<span class="sub"> ${esc(last.msg)}</span>`:""}
        ${t.status==="failed"?`<div class="sub">❌ ${esc(t.error||"")}(点数已退)</div>`:""}
        ${t.status==="done"&&safeAssetUrl(t.video_file)?`<div class="actions" style="margin-top:6px">
          <a class="btn sm pri" href="${esc(safeAssetUrl(t.video_file))}" target="_blank" rel="noopener noreferrer">▶️ 看成片</a>
          <a class="btn sm" href="${esc(safeAssetUrl(t.video_file))}" download>⬇️ 下载</a>
          ${dy?`<button class="btn sm" onclick="mxPubVideo(${Number(t.id)||0},${cp(dy.id)})">🚀 发抖音β</button>`:""}
        </div>`:""}
      </div>`;}).join("")}</div>`;
}
async function mxQuickPub(platform, jobId, title, body){
  const mx = await api("/matrix/accounts");
  const accs = (mx.accounts||[]).filter(a=>a.platform===platform);
  if(!accs.length) return toast("先去「发布渠道」绑定账号");
  const acc = accs[0];
  if(!await uiConfirm(`用「${acc.name}${acc.nickname?"·"+acc.nickname:""}」真实发布到${platform==="xhs"?"小红书":"抖音"}?\n系统会自动打开浏览器提交，失败时会返回原因。`,{
    title:"确认真实发布",confirmText:"确认发布",danger:false
  })) return;
  const d = await api(`/jobs/${jobId}/delivery`);
  const images = (d.images||[]).map(im=>im.file).filter(f=>/\.(png|jpe?g)$/i.test(f||""));
  try{
    const r = await api("/matrix/publish",{method:"POST",body:{platform, account:acc.id, title, body, images, job_id:jobId}});
    // 「发布渠道」页只有主账号能进;成员指到首页🔔新进展(发布结果会推到那里)
    toast("🚀 "+(isAdmin()?r.note:"已进发布队列,结果会推到首页「🔔 新进展」提醒"));
  }catch(e){ toast(e.message); }
}
async function mxRetry(pid, btn){
  btn.disabled=true; btn.innerHTML=`<span class="spin"></span>`;
  try{ const r = await api(`/task-center/publish/${pid}/retry`,{method:"POST"}); toast("🔁 "+r.note); render(); }
  catch(e){ toast(e.message); btn.disabled=false; btn.textContent="🔁 免费重试"; }
}
async function mxPubVideo(tvId, accId){
  const tvs = await api("/text-video");
  const t = tvs.find(x=>x.id===tvId);
  if(!t||!t.video_file) return toast("成片不存在");
  try{
    const r = await api("/matrix/publish",{method:"POST",body:{platform:"douyin", account:accId,
      title:t.params?.title||"", body:"", video:t.video_file}});
    toast("🚀 "+(isAdmin()?r.note:"已进发布队列,结果会推到首页「🔔 新进展」提醒"));
  }catch(e){ toast(e.message); }
}
async function plAdd(){
  try{
    await api("/publog",{method:"POST",body:{platform:$("#pl-pf").value, title:$("#pl-title").value.trim(),
      days_ago:+$("#pl-days").value}});
    toast("✅ 已登记,到点审查官自动来复盘"); render();
  }catch(e){ toast(e.message); }
}
async function plPull(pid, btn){
  btn.disabled=true; btn.textContent="拉取中…";
  try{ const r = await api(`/publog/${pid}/pull`,{method:"POST"}); toast(r.note); CEN_TAB="logs"; render(); }
  catch(e){ toast(e.message); btn.disabled=false; btn.textContent="📥 自动拉数据复盘"; }
}
async function plMark(pid){
  try{ await api(`/publog/${pid}/published`,{method:"POST"}); toast("⏱ 复盘计时已从现在重新起算"); render(); }
  catch(e){ toast(e.message); }
}
async function whSave(){
  try{ await api("/channels/webhook",{method:"PUT",body:{url:$("#wh-url").value.trim()}});
    toast("✅ 已保存,点「发条测试消息」验证"); }catch(e){ toast(e.message); }
}
async function whTest(btn){
  btn.disabled=true; btn.textContent="发送中…";
  try{ const r = await api("/channels/webhook/test",{method:"POST"});
    $("#wh-out").innerHTML = `<div class="notice green">✅ ${esc(r.note)}</div>`;
  }catch(e){ $("#wh-out").innerHTML = `<div class="notice red">❌ ${esc(e.message)}</div>`; }
  btn.disabled=false; btn.textContent="📨 发条测试消息";
}
async function mxAdd(btn){
  btn.disabled=true; btn.textContent="绑定中…";
  try{
    const a = await api("/matrix/accounts",{method:"POST",body:{platform:$("#mx-pf").value,
      name:$("#mx-name").value.trim(), cookie:$("#mx-cookie").value.trim()}});
    try{ const r = await api(`/matrix/accounts/${a.id}/check`,{method:"POST"}); toast("✅ "+r.note); }
    catch(e){ toast("已绑定,但验证失败:"+e.message); }
    render();
  }catch(e){ toast(e.message); btn.disabled=false; btn.textContent="🔗 绑定并验证"; }
}
async function mxCheck(id, btn){
  btn.disabled=true;
  try{ const r = await api(`/matrix/accounts/${id}/check`,{method:"POST"}); toast("✅ "+r.note); render(); }
  catch(e){ toast(e.message); render(); }
}
async function mxDel(id){
  if(!await uiConfirm("解绑该账号?",{title:"解绑发布账号",confirmText:"解绑"})) return;
  await api(`/matrix/accounts/${id}`,{method:"DELETE"}).catch(e=>toast(e.message)); render();
}

/* ---------- 🪄 半自动发布向导(不用Cookie,四步照点) ---------- */
function semiPub(jobId, platform, title, body, url){
  const step = (n,label,hint)=>`<div class="topic" id="sp-${n}" style="margin:8px 0;display:flex;gap:10px;align-items:center">
    <span style="font-size:20px;font-weight:900">${n}</span>
    <div style="flex:1"><b>${label}</b><div class="sub">${hint}</div></div>
    <span class="sp-ok" style="font-size:18px;opacity:.15">✅</span></div>`;
  document.body.insertAdjacentHTML("beforeend", `<div class="overlay" id="sp-ov" onclick="if(event.target===this)this.remove()">
    <div class="panel" style="max-width:620px;width:94vw;padding:18px">
      <div style="display:flex;gap:10px;align-items:center"><h2 style="flex:1;margin:0;min-width:180px">🪄 ${esc(platform)} · 半自动发布</h2>
        <button class="btn sm" onclick="$('#sp-ov').remove()">✕</button></div>
      <div class="sub" style="margin:6px 0">不用绑任何账号:四步照点,每步点完自动打勾。建议电脑操作(手机复制粘贴也行)。</div>
      <div onclick="spDo(1,()=>copyText(${cp(title)}))">${step(1,"复制标题","点这里复制标题,一会儿粘到平台的标题框")}</div>
      <div onclick="spDo(2,()=>copyText(${cp(body)}))">${step(2,"复制正文(已带话题标签)","点这里复制正文,粘到平台的正文框")}</div>
      <div onclick="spDo(3,()=>{const a=document.createElement('a');a.href='/api/jobs/${jobId}/pack.zip?platform='+encodeURIComponent(${cp(platform)});a.download='';document.body.appendChild(a);a.click();a.remove();})">${step(3,"下载素材包(封面+配图)","zip 里是这个平台专属尺寸的封面和配图,解压后拖进上传框")}</div>
      <div onclick="spDo(4,()=>{ if(${cp(url)}) window.open(${cp(url)},'_blank'); })">${step(4,"打开"+esc(platform)+"发布后台","新窗口打开创作后台:标题正文粘贴,图片拖入,点发布")}</div>
      <div class="notice" style="margin-top:10px">💡 发完回「审查官工作台→📮发布台账」登记一下(10秒),到 T+1/3/7 审查官自动来复盘数据。</div>
    </div></div>`);
}
function spDo(n, fn){ try{ fn(); }catch(e){} const el=$("#sp-"+n+" .sp-ok"); if(el) el.style.opacity=1; }

/* ---------- 🧰 营销工具箱 ---------- */
let TS = {tab:"hot", busy:{}, hot:null, pcal:null, pcalYm:new Date().toISOString().slice(0,7), warm:null, leads:null, vars:null, shot:null, menu:null};
let TOOLS_FETCH_AT=0, TOOLS_META_CACHE=null, TOOLS_JOBS_CACHE=null, TOOL_POLL_T=null;
function invalidateToolJobs(){ TOOLS_FETCH_AT=0; TOOLS_JOBS_CACHE=null; }
async function refreshToolsPreservingDraft(){
  if(!location.hash.startsWith("#/tools")) return;
  const quietIn=TYPING_UNTIL-Date.now();
  if(quietIn>0){
    await new Promise(resolve=>setTimeout(resolve,quietIn+30));
    if(!location.hash.startsWith("#/tools")) return;
    return refreshToolsPreservingDraft();
  }
  await toolsView(TS.tab,true);
}
function scheduleToolPoll(){
  if(TOOL_POLL_T){ clearTimeout(TOOL_POLL_T); TOOL_POLL_T=null; }
  if(!location.hash.startsWith("#/tools")||!Object.keys(TS.running||{}).length) return;
  TOOL_POLL_T=setTimeout(async ()=>{
    TOOL_POLL_T=null;
    if(!location.hash.startsWith("#/tools")) return;
    try{
      TOOLS_JOBS_CACHE=normalizeListContract(
        await api("/tools/jobs?limit=40&offset=0"),40).items;
      TOOLS_FETCH_AT=Date.now();
      if(location.hash.startsWith("#/tools")) await refreshToolsPreservingDraft();
    }catch(_){ scheduleToolPoll(); }
  },15000);
}
const toolsCacheKey=()=>`tools_cache_user_${ME?.id??"none"}`;
function saveTools(){
  if(!ME||ME.role==="tour") return;
  try{ localStorage.setItem(toolsCacheKey(), JSON.stringify({hot:TS.hot,pcal:TS.pcal,pcalYm:TS.pcalYm,warm:TS.warm,leads:TS.leads,vars:TS.vars,shot:TS.shot,menu:TS.menu,bench:TS.bench,adopted:TS.adopted,wmPf:TS.wmPf,wmInd:TS.wmInd,ldInd:TS.ldInd,ldCity:TS.ldCity,ldProd:TS.ldProd,t:Date.now()})); }catch(e){}
}
function loadTools(){
  const key=toolsCacheKey();
  if(TS.restoredKey===key) return;
  TS.restoredKey=key;
  try{
    localStorage.removeItem("tools_cache"); // 清掉旧版未按账号隔离的缓存
    const c=JSON.parse(localStorage.getItem(key)||"null");
    if(c && Date.now()-c.t < 7*86400e3) Object.assign(TS, {hot:c.hot,pcal:c.pcal,pcalYm:c.pcalYm||TS.pcalYm,warm:c.warm,leads:c.leads,vars:c.vars,shot:c.shot,menu:c.menu,bench:c.bench,adopted:c.adopted||{},wmPf:c.wmPf,wmInd:c.wmInd,ldInd:c.ldInd,ldCity:c.ldCity,ldProd:c.ldProd});
  }catch(e){}
}
function busyBar(key, text){
  const job=TS.running?.[key], live=job&&typeof job==="object"?job:null;
  if(TS.busy[key]||job){
    const progress=live?.progress?`<div class="sub" style="margin-top:5px">${esc(live.progress)}</div>`:"";
    const cap=live?.timeout_seconds?`，超过${Math.round(live.timeout_seconds/60)}分钟会自动结束并退点`:"";
    return `<div class="notice" style="margin-top:8px"><span class="spin"></span> ${text} — <b>后台进行中</b>:可离开页面，跑完自动提醒${cap}。${progress}</div>`;
  }
  if(TS.toolErr?.[key]) return `<div class="notice red" style="margin-top:8px">❌ 上次任务失败:${esc(TS.toolErr[key]||"")}(如有扣点已自动退回，可重试)${TS[key]?`；下方仍展示的是上一次成功结果`:""}</div>`;
  return ""; }
// V27:联网长任务的耗时预期小字(跑起来后交给 busyBar 提示,避免重复)
function etaHint(key, mins){
  if(TS.busy?.[key]||TS.running?.[key]) return "";
  return `<div class="sub" style="margin-top:7px;font-weight:600;color:var(--ink2)">⏱ 预计${mins} · 后台跑,可离开页面办别的 · 完成自动提醒</div>`;
}
async function toolRun(key, btn, fn){
  TS.busy[key]=true; if(btn){btn.disabled=true;} await render(true);
  try{ await fn(); saveTools(); }catch(e){ toast(e.message); }
  TS.busy[key]=false; TOOLS_FETCH_AT=0; await render();
}
function keepReport(title, md){
  api("/knowledge",{method:"POST",body:{title, content:md}})
    .then(()=>toast("✅ 已沉淀到「📚 沉淀库」")).catch(e=>toast(e.message));
}
const TOOL_TABS = [["hot","🔥 今日必发"],["remix","🎞️ 视频混剪"],["pcal","📅 私域日历"],["bench","👀 竞品盯梢"],["warm","🚀 起号军师"],["leads","🎯 线索雷达"],["shot","📸 产品图/文案"],["vars","📣 口播矩阵"]];
let TOOL_HISTORY_FILTER={kind:"",status:""};
async function toolHistory(offset=0){
  const query=new URLSearchParams({limit:"40",offset:String(Math.max(0,Number(offset)||0))});
  Object.entries(TOOL_HISTORY_FILTER).forEach(([key,value])=>{if(value) query.set(key,value);});
  const contract=normalizeListContract(await api("/tools/jobs?"+query.toString()),40);
  $("#tool-history")?.remove();
  const label=Object.fromEntries(TOOL_TABS);
  document.body.insertAdjacentHTML("beforeend",`<div class="overlay" id="tool-history" onclick="if(event.target===this)this.remove()">
    <div class="panel" style="max-width:760px;width:94vw;padding:18px">
      <div class="phead"><b style="flex:1">🧾 工具任务记录</b><button class="btn sm" onclick="$('#tool-history').remove()">✕</button></div>
      ${listContractNotice(contract,"工具任务")}
      <div class="row" style="margin:10px 0">
        <select onchange="TOOL_HISTORY_FILTER.kind=this.value;toolHistory(0)">
          <option value="">全部工具</option>${TOOL_TABS.filter(([key])=>!["remix","vars","shot"].includes(key))
            .map(([key,name])=>`<option value="${key}" ${TOOL_HISTORY_FILTER.kind===key?"selected":""}>${esc(name)}</option>`).join("")}
        </select>
        <select onchange="TOOL_HISTORY_FILTER.status=this.value;toolHistory(0)">
          <option value="">全部状态</option>${[["running","进行中"],["done","已完成"],["failed","失败"]]
            .map(([value,name])=>`<option value="${value}" ${TOOL_HISTORY_FILTER.status===value?"selected":""}>${name}</option>`).join("")}
        </select></div>
      ${contract.items.length?contract.items.map(job=>`<div class="topic">
        <b>${esc(label[job.kind]||job.kind)}</b>
        <span class="pill ${job.status}">${{running:"进行中",done:"已完成",failed:"失败"}[job.status]||esc(job.status)}</span>
        <span class="sub" style="float:right">${new Date((job.created_at||0)*1000).toLocaleString("zh-CN")}</span>
        ${job.progress?`<div class="sub">${esc(job.progress)}</div>`:""}
        ${job.status==="failed"?`<div class="sub">❌ ${esc(job.error||"任务失败，可重新发起")}</div>`:""}
      </div>`).join(""):`<div class="empty">当前筛选条件下没有任务</div>`}
      <div class="actions" style="justify-content:flex-end">
        <button class="btn sm" ${contract.offset<=0?"disabled":""} onclick="toolHistory(${Math.max(0,contract.offset-contract.limit)})">← 上一页</button>
        <button class="btn sm" ${contract.hasMore?"":"disabled"} onclick="toolHistory(${contract.nextOffset??0})">下一页 →</button>
      </div></div></div>`);
}
async function toolsView(tab,preserveDraft=false){
  if(ME?.role==="tour"||!can("content")){
    $("#main").innerHTML=`<div class="card"><h2>该页面需要内容生产权限</h2><div class="sub">当前账号不能使用营销工具箱。</div><div class="actions"><a class="btn" href="#/">返回办公室</a></div></div>`;
    return;
  }
  if(TOOL_TABS.some(([key])=>key===tab)) TS.tab=tab;
  loadTools();
  let meta=TOOLS_META_CACHE, jobs=TOOLS_JOBS_CACHE;
  if(!meta||!jobs||Date.now()-TOOLS_FETCH_AT>15000){
    const [metaPayload,jobsPayload] = await Promise.all([
      api("/tools/meta"),api("/tools/jobs?limit=40&offset=0")]);
    meta=metaPayload;
    jobs=normalizeListContract(jobsPayload,40).items;
    TOOLS_META_CACHE=meta; TOOLS_JOBS_CACHE=jobs; TOOLS_FETCH_AT=Date.now();
  }
  TS.running={}; TS.toolErr={}; TS.adopted=TS.adopted||{};
  const latestToolKinds=new Set();
  for(const j of jobs){
    if(latestToolKinds.has(j.kind)) continue;
    latestToolKinds.add(j.kind);
    const P=j.params||{};
    if(j.kind==="hot") TS.hotInd=P.industry||TS.hotInd;
    if(j.kind==="pcal") TS.pcalYm=P.ym||TS.pcalYm;
    if(j.kind==="warm"){ TS.wmPf=P.platform||TS.wmPf; TS.wmInd=P.industry||TS.wmInd; }
    if(j.kind==="leads"){ TS.ldInd=P.industry||TS.ldInd; TS.ldCity=P.city||TS.ldCity; TS.ldProd=P.product||TS.ldProd; }
    if(j.status==="running"){ TS.running[j.kind]=j; continue; }
    if(j.status==="failed"){ TS.toolErr[j.kind]=j.error; continue; }
    if(j.status==="done" && j.result && TS.adopted[j.kind]!==j.id){
      TS.adopted[j.kind]=j.id;
      if(j.kind==="hot"){ TS.hot=j.result; TS.hotInd=P.industry||TS.hotInd; }
      if(j.kind==="pcal"){ TS.pcal=j.result; TS.pcalYm=P.ym||TS.pcalYm; TS.pcalEdit=false; }
      if(j.kind==="warm"){ TS.warm=j.result; TS.wmPf=P.platform||TS.wmPf; TS.wmInd=P.industry||TS.wmInd; }
      if(j.kind==="leads"){ TS.leads=j.result; TS.ldInd=P.industry||TS.ldInd; TS.ldCity=P.city||TS.ldCity; TS.ldProd=P.product||TS.ldProd; }
      if(j.kind==="bench"){ TS.bench=j.result; }
    }
  }
  saveTools();
  window.TOOLS_META = meta;
  let body = "";
  const indChips = (id,sel)=>`<div class="chips" id="${id}">${meta.industries.map((t,i)=>`<span class="chip${(sel?t===sel:i===0)?" on":""}" onclick="pick(this)">${t}</span>`).join("")}</div>`;
  if(TS.tab==="hot"){
    const h = TS.hot;
    const savedCh = TS.hotCh || meta.hot_channels_saved || [];
    body = `<div class="sub">每天点一下:趋势官只扫<b>您勾选的渠道</b>,挑 3 个「今天发正合适」的选题,看中直接一键开工。渠道选得准,又快又省。</div>
    <label>行业</label>${indChips("hot-ind", TS.hotInd)}
    <input id="hot-ind-c" placeholder="✏️ 或自己输入行业(填了就用这个)" value="${esc(TS.hotIndC||"")}" oninput="TS.hotIndC=this.value" style="margin-top:6px">
    <label>扫描渠道(多选,按您账号所在的阵地勾)</label>
    <div class="chips" id="hot-ch">${(meta.hot_channels||[]).map(c=>`<span class="chip${savedCh.includes(c)?" on":""}" onclick="this.classList.toggle('on')">${c}</span>`).join("")}</div>
    <div class="actions" style="margin-top:8px"><button class="btn pri" ${TS.busy.hot||TS.running?.hot?"disabled":""} onclick="hotGo(this)">🔥 扫描今日热点(1点)</button>
      <label style="display:inline-flex;align-items:center;font-weight:700;margin-left:8px"><input type="checkbox" id="hd-en" style="width:auto;margin-right:6px" ${(meta.hot_daily||{}).enabled?"checked":""} onchange="hdSave()">⏰ 每天早上自动扫描并推微信(1点/天)</label></div>
    ${(meta.hot_daily||{}).enabled?`<div class="sub" style="margin-top:4px">已订阅:每天 7-9 点自动扫「${esc((meta.hot_daily||{}).industry||"通用")}」,3个选题直接推到您微信${(meta.hot_daily||{}).note?` · ⚠️ ${esc(meta.hot_daily.note)}`:""}</div>`:""}
    ${etaHint("hot","通常1-3分钟，最迟5分钟自动结束")}
    ${busyBar("hot","趋势官正在扫您勾选的渠道")}
    ${(meta.festivals||[]).length?`<div class="notice" style="margin-top:10px">📅 未来节点:${meta.festivals.slice(0,6).map(f=>`<b>${f.name}</b>(${f.in_days===0?"今天":f.in_days+"天后"})`).join(" · ")}</div>`:""}
    <div id="hot-out" style="margin-top:10px">${h?hotHtml(h):""}</div>`;
  } else if(TS.tab==="remix"){
    const clips = TS.clips||[];
    body = `<div class="sub"><b>把您手机里的 vlog 片段变成成片</b>:传几段素材 → 给个主题(AI替您写口播稿)或直接贴文案 → AI 看懂每段画面、逐句配画面,自动配音+字幕+转场+BGM+调色,出竖版成片。<span class="sub">原来的「图文成片」(真实图/AI图配画面)还在:交付包🎬按钮和「📣口播矩阵」里照用。</span></div>
    <label>① 素材片段(手机拍的就行,mp4/mov,可多选;点卡片勾选/取消,最多12段参与)</label>
    <input type="file" multiple accept="video/mp4,video/quicktime,video/webm,.mp4,.mov,.m4v" onchange="clipUpload(this)" style="margin:6px 0">
    ${TS.busy.clipup?`<div class="notice"><span class="spin"></span> 素材上传中,别关页面…</div>`:""}
    <div class="grid3" style="grid-template-columns:repeat(auto-fill,minmax(150px,1fr))">
      ${clips.map(c=>`<div class="thumb clip-card${(TS.clipSel||[]).includes(c.name)?" sel on":""}" data-name="${esc(c.name)}" style="position:relative;aspect-ratio:16/10" onclick="clipToggle(${cp(c.name)})">
        ${safeAssetUrl(c.thumb)?`<img src="${esc(safeAssetUrl(c.thumb))}" style="width:100%;height:100%;object-fit:cover" onerror="this.style.display='none'">`:""}
        <span class="tag" style="position:absolute;left:4px;bottom:4px">${c.dur}s</span>
        <span style="position:absolute;right:4px;top:4px;font-size:16px">${(TS.clipSel||[]).includes(c.name)?"✅":"⬜"}</span>
        <button class="btn sm bad" style="position:absolute;right:4px;bottom:4px;padding:0 6px" onclick="event.stopPropagation();clipDel(${cp(c.name)})">🗑</button>
      </div>`).join("")||`<div class="empty" style="grid-column:1/-1">还没有素材,先传几段(拍店里/产品/街景都行)</div>`}</div>
    <label style="margin-top:12px">② 主题 <span class="sub">(填主题 AI 替您写口播稿;想自己定文案就填下面的)</span></label>
    <input id="rm-topic" placeholder="如:我们家小龙虾锅底新上市,欢迎来尝" value="${esc(TS.rmTopic||"")}" oninput="TS.rmTopic=this.value">
    <label>或 直接贴口播文案 <span class="sub">(填了就按这个念,不再自动写)</span></label>
    <textarea id="rm-script" style="min-height:80px" placeholder="(选填)想让视频念什么就写什么" oninput="TS.rmScript=this.value">${esc(TS.rmScript||"")}</textarea>
    <div class="row" style="align-items:flex-end">
      <div style="flex:1;min-width:200px"><label>配音音色 ${(TOOLS_META.cloned||[]).length?`<span class="sub">(🧬开头是您的克隆声)</span>`:""}</label>
        <div style="display:flex;gap:6px"><select id="rm-voice" style="flex:1">${(TOOLS_META.cloned||[]).concat(TOOLS_META.voices||[]).map(v=>`<option value="${v.id}">${esc(v.label)}</option>`).join("")}</select>
        <button class="btn sm" style="flex:0 0 auto;width:auto" title="录10秒声音,以后视频都用您的原声" onclick="goClone()">🧬 克隆我的声音</button></div></div>
      <div style="flex:0 0 110px"><label>配乐</label><select id="rm-bgm">${(TOOLS_META.bgm_moods||[]).map(m=>`<option value="${m.key}">${m.label}</option>`).join("")}</select></div>
      <button class="btn pri" onclick="remixGo(this)">🎞️ 开始混剪(3点)</button></div>
    <div class="sub" style="margin-top:6px">画面会自动竖屏裁切+轻调色;素材原声会替换成配音+轻音乐。混剪约 3-6 分钟,完成会推微信通知。</div>
    <div id="vr-tvs" style="margin-top:10px"></div>`;
  } else if(TS.tab==="pcal"){
    const c = TS.pcal;
    body = `<div class="sub">生成整月「朋友圈+社群」内容日历:每天1条朋友圈(干货/日常/见证/产品/互动穿插),每周一给社群主题,节日自动借势。发圈直接抄。</div>
    <div class="row" style="align-items:flex-end">
      <div style="flex:0 0 140px"><label>月份</label><input id="pc-ym" type="month" value="${TS.pcalYm}" onchange="TS.pcalYm=this.value"></div>
      <div style="flex:2;min-width:200px"><label>行业</label>${indChips("pc-ind", TS.pcInd)}</div></div>
    <input id="pc-ind-c" placeholder="✏️ 或自己输入行业(填了就用这个)" value="${esc(TS.pcIndC||"")}" oninput="TS.pcIndC=this.value" style="margin-top:6px">
    <label>补充要求(选填)</label><input id="pc-focus" placeholder="如:本月主推新品XX;周末店里有活动" value="${esc(TS.pcFocus||"")}" oninput="TS.pcFocus=this.value">
    <div class="actions"><button class="btn pri" ${TS.busy.pcal||TS.running?.pcal?"disabled":""} onclick="pcalGo(this)">📅 生成本月日历(2点)</button>
      <button class="btn" onclick="pcalLoad()">📂 看已生成</button>
      ${TS.pcal?`<button class="btn" onclick="TS.pcalEdit=!TS.pcalEdit;render()">${TS.pcalEdit?"👁 退出编辑":"✏️ 编辑文案"}</button>
      ${TS.pcalEdit?`<button class="btn pri" onclick="pcalSave(this)">💾 保存修改</button>`:""}
      <button class="btn" onclick="pcalFeishu(this)">📤 同步到飞书</button>`:""}</div>
    ${etaHint("pcal","通常1-3分钟，最迟5分钟自动结束")}
    ${busyBar("pcal","操盘手正在写整月日历")}
    <div id="pc-out" style="margin-top:10px">${c?pcalHtml(c):""}</div>`;
  } else if(TS.tab==="bench"){
    const b = TOOLS_META.bench||{targets:[]};
    body = `<div class="sub">把要盯的对标账号加进来,<b>每周一上午</b>自动出《竞品盯梢周报》(3点/期,自动扣):对手做了什么+值得抄的作业,存进沉淀库并推微信。也可随时手动出一期。</div>
    <div id="bw-list">${(b.targets||[]).map(t=>bwRow(t)).join("")||bwRow()}</div>
    <div class="actions" style="margin-top:8px"><button class="btn" onclick="bwAdd()">➕ 加对标</button>
      <label style="display:inline-flex;align-items:center;font-weight:700;margin:0 8px"><input type="checkbox" id="bw-en" style="width:auto;margin-right:6px" ${b.enabled?"checked":""}>每周自动</label>
      <button class="btn pri" onclick="bwSave()">💾 保存</button>
      <button class="btn pri" ${TS.busy.bench||TS.running?.bench?"disabled":""} onclick="bwRun(this)">📰 立即出一期(3点)</button></div>
    ${etaHint("bench","通常2-5分钟，最迟6分钟自动结束")}
    ${busyBar("bench","情报官正在逐个盯对标的近7天动态")}
    <div class="sub" style="margin-top:6px">${b.last_run?`上次出报:${new Date(b.last_run*1000).toLocaleString("zh-CN")}(每周一自动出的那期在<a href="#/knowledge" style="text-decoration:underline">沉淀库</a>)`:""}</div>
    <div id="bw-out" style="margin-top:8px">${TS.bench?benchResHtml(TS.bench):""}</div>`;
  } else if(TS.tab==="warm"){
    body = `<div class="sub">新账号从 0 起:军师先联网调研您行业在该平台的头部打法,再给《30天冷启动作战计划》——定位诊断/账号名/简介/对标账号/分周打法/30天逐日选题。</div>
    <div class="row">
      <div style="flex:0 0 150px"><label>平台</label><select id="wm-pf">${["小红书","抖音","公众号","视频号"].map(x=>`<option>${x}</option>`).join("")}</select></div>
      <div style="flex:1;min-width:200px"><label>行业</label>${indChips("wm-ind", TS.wmInd)}</div></div>
    <input id="wm-ind-c" placeholder="✏️ 或自己输入行业(填了就用这个)" value="${esc(TS.wmIndC||"")}" oninput="TS.wmIndC=this.value" style="margin-top:6px">
    <label>挂哪个人设档案 <span class="sub">(强烈建议选:账号名/简介/选题全按这个人设来;<a href="#/profiles" style="text-decoration:underline">没有就先建 →</a>)</span></label>
    <select id="wm-profile"><option value="">(不用人设档案)</option>
      ${(STATE.profiles||[]).map(p=>`<option value="${p.id}">${esc(p.name)}</option>`).join("")}</select>
    <label>您的定位想法(选填)</label><input id="wm-pos" placeholder="如:社区宝妈客群的轻食店,想走老板娘人设" value="${esc(TS.wmPos||"")}" oninput="TS.wmPos=this.value">
    <div class="actions"><button class="btn pri" ${TS.busy.warm||TS.running?.warm?"disabled":""} onclick="warmGo(this)">🚀 出30天起号计划(3点)</button></div>
    ${etaHint("warm","通常2-5分钟，最迟6分钟自动结束")}
    ${busyBar("warm","军师正在联网调研头部打法+写30天计划")}
    <div id="wm-out" style="margin-top:10px">${TS.warm?warmHtml(TS.warm):""}</div>`;
  } else if(TS.tab==="leads"){
    body = `<div class="sub">侦察兵联网扫知乎/微博/小红书等公开帖子:谁在求推荐、吐槽同行、找攻略——这些就是您能去承接的线索,附承接话术。<b>合规提示:话术生成后人工去回复,别用软件群发。</b></div>
    <div class="row">
      <div style="flex:1;min-width:160px"><label>行业</label>${indChips("ld-ind", TS.ldInd)}
        <input id="ld-ind-c" placeholder="✏️ 或自己输入" value="${esc(TS.ldIndC||"")}" oninput="TS.ldIndC=this.value" style="margin-top:6px"></div>
      <div style="flex:0 0 150px"><label>城市(选填)</label><input id="ld-city" placeholder="如:杭州" value="${esc(TS.ldCity||"")}" oninput="TS.ldCity=this.value"></div>
      <div style="flex:1;min-width:160px"><label>产品/服务</label><input id="ld-prod" placeholder="如:轻食沙拉外卖" value="${esc(TS.ldProd||"")}" oninput="TS.ldProd=this.value"></div></div>
    <div class="actions"><button class="btn pri" ${TS.busy.leads||TS.running?.leads?"disabled":""} onclick="leadsGo(this)">🎯 开始侦察(3点)</button></div>
    ${etaHint("leads","通常2-5分钟，最迟6分钟自动结束并退点")}
    ${busyBar("leads","侦察兵正在按四类信号全网检索")}
    <div id="ld-out" style="margin-top:10px">${TS.leads?leadsHtml(TS.leads):""}</div>`;
  } else if(TS.tab==="shot"){
    body = `<div class="sub"><b>随手拍变素材</b>:上传一张产品/菜品随手拍 → 一次拿走「商业海报级美图 + 全套文案」(卖点/外卖描述/小红书文案/价格话术)。拍一张,朋友圈、外卖店、小红书三处的素材全有了。</div>
    <input type="file" id="ps-file" accept="image/*" style="margin:8px 0">
    <div class="row">
      <div><label>画面场景(选填)</label><input id="ps-scene" placeholder="如:大理石桌面+咖啡店氛围/深色木桌暖光" value="${esc(TS.psScene||"")}" oninput="TS.psScene=this.value"></div>
      <div><label>文案需求(选填)</label><input id="mc-want" placeholder="如:写美团外卖菜品描述" value="${esc(TS.mcWant||"")}" oninput="TS.mcWant=this.value"></div></div>
    <div class="actions"><button class="btn pri" ${TS.busy.shot?"disabled":""} onclick="factoryGo(this)">✨ 一键出海报+文案(3点)</button></div>
    ${busyBar("shot","正在出海报+写文案(约1分钟)")}
    <div class="row" style="align-items:flex-start;margin-top:10px">
      <div style="flex:1;min-width:260px" id="ps-out">${safeAssetUrl(TS.shot)?`<a class="thumb" href="${esc(safeAssetUrl(TS.shot))}" target="_blank" rel="noopener noreferrer" style="display:block;max-width:300px"><img src="${esc(safeAssetUrl(TS.shot))}" style="width:100%"></a>`:""}</div>
      <div style="flex:1;min-width:260px" id="mc-out">${TS.menu?menuHtml(TS.menu):""}</div></div>`;
  } else if(TS.tab==="vars"){
    body = `<div class="sub">矩阵打法:一篇口播稿 → 裂变 N 个不撞车的版本(换钩子/换叙事/换例子)→ 每版一键「图文成片」出视频(3点/条,自动配音+字幕+抓图)。多账号发布不查重。</div>
    <label>原始口播稿(从交付包复制,或自己写)</label>
    <textarea id="vr-script" style="min-height:120px" placeholder="粘贴口播稿…" oninput="TS.vrScript=this.value">${esc(TS.vrScript||"")}</textarea>
    <div class="row" style="align-items:flex-end">
      <div style="flex:0 0 110px"><label>裂变数量</label><select id="vr-n">${[2,3,4,5,6].map(n=>`<option ${n===3?"selected":""}>${n}</option>`).join("")}</select></div>
      <div style="flex:1;min-width:200px"><label>配音音色 ${(TOOLS_META.cloned||[]).length?`<span class="sub">(🧬开头是您的克隆声)</span>`:""}</label>
        <div style="display:flex;gap:6px"><select id="vr-voice" style="flex:1">${(TOOLS_META.cloned||[]).concat(TOOLS_META.voices||[]).map(v=>`<option value="${v.id}">${esc(v.label)}</option>`).join("")}</select>
        <button class="btn sm" style="flex:0 0 auto;width:auto" title="录10秒声音,以后视频都用您的原声" onclick="goClone()">🧬 克隆我的声音</button></div></div>
      <div style="flex:1;min-width:180px"><label>配图关键词</label><input id="vr-imgq" placeholder="成片抓真实图用,如:火锅 实拍" value="${esc(TS.vrImgq||"")}" oninput="TS.vrImgq=this.value"></div>
      <div style="flex:0 0 110px"><label>配乐</label><select id="vr-bgm">${(TOOLS_META.bgm_moods||[]).map(m=>`<option value="${m.key}">${m.label}</option>`).join("")}</select></div>
      <button class="btn pri" ${TS.busy.vars?"disabled":""} onclick="varsGo(this)">📣 开始裂变(1点)</button></div>
    ${busyBar("vars","正在裂变多版本口播稿…")}
    <div id="vr-out" style="margin-top:10px">${TS.vars?varsHtml(TS.vars):""}</div>
    <div id="vr-tvs" style="margin-top:10px"></div>`;
  }
  if(TS.tab==="remix" && !TS.clipsLoaded){
    TS.clipsLoaded = true;
    TS.clips = await api("/tv/clips").catch(optionalResult([]));
    if(!TS.clipSel) TS.clipSel = (TS.clips||[]).map(c=>c.name);
    return toolsView(tab,preserveDraft);
  }
  const snapshot=preserveDraft?captureFormState():null;
  $("#main").innerHTML = `<div class="card" style="background:linear-gradient(120deg,#fff6dc,#f4f9f4 65%)">
    <h2 style="margin:0">🧰 营销工具箱</h2>
    <div class="sub" style="margin-top:5px">老板的日常武器库:每个工具都是「填两个空 → 点一下 → 拿走就能用」。</div>
    <div class="actions"><button class="btn sm" onclick="toolHistory(0)">🧾 查看全部工具任务</button></div></div>
  <div class="card"><div class="tabs" style="flex-wrap:wrap">${TOOL_TABS.map(([k,l])=>`<a class="tb ${TS.tab===k?"on":""}" href="#/tools/${k}">${l}</a>`).join("")}</div>
  <div style="margin-top:12px">${body}</div></div>`;
  if(snapshot) restoreFormState(snapshot);
  if(TS.tab==="vars"||TS.tab==="remix") varsTvs();
  scheduleToolPoll();
}
function goClone(){ window.AV_OPEN_CLONE=true; location.hash="#/avatar"; toast(`🧬 带您去摄影棚:录一段10秒以上的话,声音就是您的了(${META?.voice_clone_points??9}点,一次永久用)`); }
function clipToggle(name){
  TS.clipSel = TS.clipSel||[];
  TS.clipSel = TS.clipSel.includes(name)? TS.clipSel.filter(x=>x!==name) : [...TS.clipSel, name];
  render();
}
async function clipUpload(input){
  if(!input.files.length) return;
  TS.busy.clipup=true; render();
  for(const f of input.files){
    const fd=new FormData(); fd.append("file",f);
    try{
      const d = await xhrUpload("/api/tv/clips", fd, null);
      TS.clips=[...(TS.clips||[]),d]; TS.clipSel=[...(TS.clipSel||[]),d.name];
    }catch(e){ toast(`${f.name}:${e.message}`); }
  }
  TS.busy.clipup=false; render();
}
async function clipDel(name){
  await api(`/tv/clips/${name}`,{method:"DELETE"}).catch(e=>toast(e.message));
  TS.clips=(TS.clips||[]).filter(c=>c.name!==name);
  TS.clipSel=(TS.clipSel||[]).filter(x=>x!==name); render();
}
async function remixGo(btn){
  const sel = TS.clipSel||[];
  if(!sel.length) return toast("先勾选至少 1 段素材");
  const topic=$("#rm-topic").value.trim(), script=$("#rm-script").value.trim();
  if(!topic&&!script) return toast("主题和文案至少填一个");
  btn.disabled=true; btn.textContent="排队…";
  try{
    await api("/text-video",{method:"POST",body:{mode:"clips", clips:sel, topic, script,
      title:(topic||script).slice(0,20), voice_id:$("#rm-voice").value, bgm:$("#rm-bgm")?.value||"warm"}});
    toast("🎞️ 已开工:看画面→写稿→配音→混剪,约3-6分钟,完成推微信"); varsTvs();
  }catch(e){ toast(e.message); }
  btn.disabled=false; btn.textContent="🎞️ 开始混剪(3点)";
}
function hotHtml(h){
  return `<div class="notice green">📡 ${esc(h.scan_note||"")}</div>` + (h.picks||[]).map(p=>`<div class="topic">
    <b>${esc(p.title)}</b><div class="sub" style="margin:4px 0">🔥 ${esc(p.why||"")} · 角度:${esc(p.angle||"")}</div>
    <button class="btn sm pri" onclick="hotStart(${cp(JSON.stringify({direction:p.direction||p.title, why:p.why||""}))})">⚡ 一键开工</button></div>`).join("");
}
function hotStart(payloadStr){ localStorage.setItem("prefill_brief", payloadStr); location.hash="#/new"; }
async function hdSave(){
  const ind = $("#hot-ind-c").value.trim()||$("#hot-ind .on")?.textContent||"通用";
  const ch = [...document.querySelectorAll("#hot-ch .on")].map(e=>e.textContent.trim());
  try{
    const r = await api("/tools/hot-daily",{method:"PUT",body:{enabled:$("#hd-en").checked, industry:ind, channels:ch}});
    toast(r.enabled?`⏰ 已订阅:每天早上自动扫「${ind}」推微信(记得配好企微通知)`:"已取消每日自动扫描");
    render();
  }catch(e){ toast(e.message); }
}
async function hotGo(btn){
  TS.hotInd = $("#hot-ind-c").value.trim()||$("#hot-ind .on")?.textContent||"通用";
  TS.hotCh = [...document.querySelectorAll("#hot-ch .on")].map(e=>e.textContent.trim());
  if(!TS.hotCh.length) return toast("至少勾一个扫描渠道");
  btn.disabled=true;
  try{ const r = await api("/tools/hotpick",{method:"POST",body:{industry:TS.hotInd, channels:TS.hotCh}}); invalidateToolJobs(); toast("🔥 "+r.note); }
  catch(e){ toast(e.message); }
  render();
}
function pcalHtml(c){
  const edit = TS.pcalEdit;
  return `<div class="notice">${esc(c.tips||"")}</div>${edit?`<div class="notice green">✏️ 编辑模式:直接改下面的文案,改完点上面「💾 保存修改」;保存后再「📤 同步到飞书」</div>`:""}<div style="max-height:60vh;overflow:auto">` +
    (c.days||[]).map(d=>`<div class="topic pcal-day" data-d="${esc(String(d.d??""))}" style="margin:6px 0">
      <b>${esc(String(d.d??""))}日 ${esc(d.weekday||"")}</b>${d.festival?` <span class="tag">🎉 ${esc(d.festival)}</span>`:""}
      ${edit?`<textarea class="pc-m" style="min-height:56px;margin-top:4px">${esc(d.moment||"")}</textarea>
        <textarea class="pc-g" style="min-height:34px;margin-top:4px" placeholder="社群话术(可空)">${esc(d.group||"")}</textarea>`
      :`<div class="sub" style="margin-top:4px">${esc(d.moment||"")}</div>
      ${d.group?`<div class="sub" style="margin-top:4px">👥 社群:${esc(d.group)}</div>`:""}
      <button class="btn sm" style="margin-top:4px" onclick="copyText(${cp(d.moment||"")})">📋 复制</button>`}</div>`).join("")+`</div>`;
}
async function pcalSave(btn){
  const days = [...document.querySelectorAll(".pcal-day")].map(el=>({d:+el.dataset.d,
    moment:el.querySelector(".pc-m")?.value||"", group:el.querySelector(".pc-g")?.value||""}));
  try{
    await api("/tools/pcal",{method:"PUT",body:{ym:$("#pc-ym").value, days}});
    days.forEach(e=>{ const d=(TS.pcal.days||[]).find(x=>+x.d===e.d); if(d){ d.moment=e.moment; d.group=e.group; } });
    TS.pcalEdit=false; toast("✅ 修改已保存"); render();
  }catch(e){ toast(e.message); }
}
async function pcalFeishu(btn){
  btn.disabled=true; btn.textContent="同步中…";
  try{ const r = await api("/tools/pcal/feishu",{method:"POST",body:{ym:$("#pc-ym").value}});
    toast(`✅ 已同步 ${r.synced} 天到飞书「${r.table}」`); }
  catch(e){ toast(e.message+(/(链接|权限)/.test(e.message)?"(去「👥权限管理」页配置飞书表格链接)":"")); }
  btn.disabled=false; btn.textContent="📤 同步到飞书";
}
async function pcalGo(btn){
  TS.pcInd = $("#pc-ind-c").value.trim()||$("#pc-ind .on")?.textContent||"通用";
  TS.pcalYm = $("#pc-ym").value;
  btn.disabled=true;
  try{ const r = await api("/tools/pcal",{method:"POST",body:{ym:TS.pcalYm, industry:TS.pcInd, focus:$("#pc-focus")?.value||TS.pcFocus||""}}); invalidateToolJobs(); toast("📅 "+r.note); }
  catch(e){ toast(e.message); }
  render();
}
async function pcalLoad(){
  const c = await api(`/tools/pcal?ym=${$("#pc-ym").value}`);
  if(!c||!c.days) return toast("该月还没生成过");
  TS.pcal = c; render();
}
function bwRow(t){
  return `<div class="row bw-row" style="margin-top:6px;gap:6px">
    <input class="bw-name" placeholder="对标账号/品牌名" value="${esc(t?.name||"")}" style="flex:1;min-width:140px">
    <input class="bw-pf" placeholder="平台(选填)" value="${esc(t?.platform||"")}" style="flex:0 0 110px;width:auto">
    <input class="bw-note" placeholder="备注(选填):主页链接/关注点" value="${esc(t?.note||"")}" style="flex:2;min-width:160px">
    <button class="btn sm bad" style="flex:0 0 auto;width:auto;align-self:center" onclick="this.parentNode.remove()">🗑</button></div>`;
}
function bwAdd(){ $("#bw-list").insertAdjacentHTML("beforeend", bwRow()); }
async function bwSave(){
  const targets = [...document.querySelectorAll(".bw-row")].map(r=>({name:r.querySelector(".bw-name").value.trim(),
    platform:r.querySelector(".bw-pf").value.trim(), note:r.querySelector(".bw-note").value.trim()})).filter(t=>t.name);
  try{ await api("/tools/bench",{method:"PUT",body:{targets, enabled:$("#bw-en").checked}});
    toast(`✅ 已保存 ${targets.length} 个对标${$("#bw-en").checked?",每周一自动出报":""}`); TOOLS_META.bench=null; render(); }
  catch(e){ toast(e.message); }
}
async function bwRun(btn){
  btn.disabled=true;
  // 先把页面上填的对标静默保存再出报:此前老板填了行没点「保存」直接点
  // 「立即出一期」,会被"先添加并保存"顶回来——两步陷阱。
  const targets = [...document.querySelectorAll(".bw-row")].map(r=>({name:r.querySelector(".bw-name").value.trim(),
    platform:r.querySelector(".bw-pf").value.trim(), note:r.querySelector(".bw-note").value.trim()})).filter(t=>t.name);
  try{
    if(targets.length) await api("/tools/bench",{method:"PUT",body:{targets, enabled:$("#bw-en")?.checked??false}});
    const r = await api("/tools/bench/run-now",{method:"POST",body:{}}); invalidateToolJobs(); toast("📰 "+r.note);
  }
  catch(e){ toast(e.message); }
  render();
}
function benchResHtml(r){
  return `<div class="notice green">📰 ${esc(r.summary||"周报已出")}</div>`
    +(r.items||[]).map(it=>`<div class="topic"><b>👀 ${esc(it.name||"")}</b>
      <div class="sub" style="margin-top:4px">${esc(it.moves||"")}</div>
      <div class="sub" style="margin-top:4px">📌 来源:${esc(it.evidence||"")}</div>
      <div class="sub" style="margin-top:4px">✂️ 抄作业:${esc(it.steal||"")}</div></div>`).join("")
    +(r.actions||[]).map(a=>`<div class="sub" style="margin-top:4px">→ ${esc(a)}</div>`).join("")
    +`<div class="actions" style="margin-top:8px">
      <button class="btn sm pri" onclick="keepReport('竞品盯梢周报 '+new Date().toLocaleDateString('zh-CN'), TS.bench.md||'')">💾 有价值,沉淀到沉淀库</button>
      <button class="btn sm" onclick="copyText(TS.bench.md||'')">📋 复制全文</button></div>`;
}
function warmHtml(w){
  const p = w.persona||{};
  return `<div class="notice green">${esc(w.diagnosis||"")}</div>
  <div class="topic"><b>账号名:</b>${(p.name_ideas||[]).map(esc).join(" / ")}<br><b>简介:</b>${esc(p.bio||"")}<br>
    <b>视觉:</b>${esc(p.visual||"")}<br><b>对标:</b>${(p.benchmark||[]).map(esc).join("、")}</div>
  ${(w.phases||[]).map(f=>`<div class="sub">📍 ${esc(f.week||"")}:${esc(f.goal||"")} — ${esc(f.note||"")}</div>`).join("")}
  <details style="margin-top:8px"><summary><b>📆 30天逐日选题表</b></summary>
    ${(w.days||[]).map(d=>`<div class="sub" style="margin:3px 0">D${d.d} [${esc(d.form||"")}] ${esc(d.topic||"")} · 钩子:${esc(d.hook||"")}</div>`).join("")}</details>
  <div class="notice" style="margin-top:8px">🚧 避坑:${(w.redlines||[]).map(esc).join(";")}</div>
  <div class="actions" style="margin-top:8px"><button class="btn sm pri" onclick="keepReport('30天起号计划·'+(TS.wmPf||'')+(TS.wmInd||''), warmMd(TS.warm))">💾 有价值,沉淀到沉淀库</button>
    <button class="btn sm" onclick="copyText(warmMd(TS.warm))">📋 复制全文</button></div>`;
}
function warmMd(w){
  const p=w.persona||{};
  return [`# 30天起号计划(${TS.wmPf||""}·${TS.wmInd||""})`,"",`## 定位诊断`,w.diagnosis||"","",
    `## 人设`,`- 账号名:${(p.name_ideas||[]).join(" / ")}`,`- 简介:${p.bio||""}`,`- 视觉:${p.visual||""}`,`- 对标:${(p.benchmark||[]).join("、")}`,"",
    `## 分周打法`,...(w.phases||[]).map(f=>`- ${f.week||""}:${f.goal||""} — ${f.note||""}`),"",
    `## 30天逐日选题`,...(w.days||[]).map(d=>`- D${d.d} [${d.form||""}] ${d.topic||""}(钩子:${d.hook||""})`),"",
    `## 避坑`,...(w.redlines||[]).map(x=>`- ${x}`)].join("\n");
}
async function warmGo(btn){
  TS.wmInd = $("#wm-ind-c").value.trim()||$("#wm-ind .on")?.textContent||"通用";
  TS.wmPf = $("#wm-pf").value; TS.wmProfile = $("#wm-profile").value?+$("#wm-profile").value:null;
  btn.disabled=true;
  try{ const r = await api("/tools/warmup",{method:"POST",body:{platform:TS.wmPf, industry:TS.wmInd,
    positioning:TS.wmPos||"", goal:"", profile_id:TS.wmProfile}}); invalidateToolJobs(); toast("🚀 "+r.note); }
  catch(e){ toast(e.message); }
  render();
}
function leadSourceUrl(lead){
  const valid=(value,embedded=false)=>{
    value=String(value||"").trim();
    const found=embedded?value.match(/https?:\/\/[^\s<>"'，。；、]+/i):null;
    const raw=(found?.[0]||value).replace(/[)\]}>.,;!?]+$/,"");
    if(!raw||raw.length>2048||/\s|[\u0000-\u001f]/.test(raw)) return "";
    try{
      const u=new URL(raw), h=u.hostname.toLowerCase().replace(/\.$/,"");
      if(!["http:","https:"].includes(u.protocol)||u.username||u.password) return "";
      if(h==="localhost"||h.endsWith(".localhost")||h.endsWith(".local")||h.endsWith(".internal")) return "";
      if(/^(127\.|10\.|192\.168\.|169\.254\.|0\.|100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.)/.test(h)
        ||/^172\.(?:1[6-9]|2\d|3[01])\./.test(h)||h==="::1"||h.includes(":")) return "";
      const seg=u.pathname.split("/").filter(Boolean).map(x=>x.toLowerCase());
      if(!seg.length) return "";
      if(["search","search_result"].includes(seg[0])||(seg[0]==="web"&&seg[1]?.startsWith("search"))
        ||(seg[0]==="s"&&["q","query","wd","keyword","kw"].some(k=>u.searchParams.has(k)))) return "";
      if(seg.length===2&&["people","person","user","users","profile","u"].includes(seg[0])) return "";
      if(seg.length===3&&seg[0]==="user"&&seg[1]==="profile") return "";
      const social=["zhihu.com","douban.com","xiaohongshu.com","douyin.com","weibo.com","bilibili.com","x.com","twitter.com"];
      if(seg.length<=1&&social.some(x=>h===x||h.endsWith("."+x))) return "";
      const platform=String(lead?.platform||"").toLowerCase();
      const platformDomains=[["知乎",["zhihu.com"]],["豆瓣",["douban.com"]],["小红书",["xiaohongshu.com","xhslink.com"]],
        ["大众点评",["dianping.com"]],["抖音",["douyin.com","iesdouyin.com"]],["微博",["weibo.com","weibo.cn"]],
        ["哔哩哔哩",["bilibili.com","b23.tv"]],["b站",["bilibili.com","b23.tv"]],
        ["百度贴吧",["tieba.baidu.com"]],["公众号",["mp.weixin.qq.com"]]];
      const matched=platformDomains.find(([label])=>platform.includes(label.toLowerCase()));
      if(matched&&!matched[1].some(x=>h===x||h.endsWith("."+x))) return "";
      return u.href;
    }catch(_){ return ""; }
  };
  return valid(lead?.source_url)||valid(lead?.url)||valid(lead?.where,true);
}
function leadsHtml(L, context={}){
  const bc = L.by_category||{};
  const city=context.city??TS.ldCity??"", industry=context.industry??TS.ldInd??"";
  const reportTitle="线索雷达·"+city+industry, reportMd=leadsMd(L,city,industry);
  const iTag = i=>i==="高"?`<span class="tag" style="background:#ffc2c5">🔥 需求高</span>`:i==="中"?`<span class="tag" style="background:#ffe9ad">需求中</span>`:`<span class="tag">需求低</span>`;
  return `<div class="notice green">🧭 ${esc(L.strategy||"")}</div>
  <div class="kv">${Object.entries(bc).map(([k,v])=>`<span>${esc(k)} ${esc(v)} 条</span>`).join("")}</div>
  ${(L.leads||[]).map(l=>{ const source=leadSourceUrl(l); return `<div class="topic lead-card">
    <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap"><span class="tag">${esc(l.category||"")}</span><span class="tag">${esc(l.platform||"")}</span>${iTag(l.intent)}
      <span class="sub" style="margin-left:auto">${esc(l.time||"")}</span></div>
    <div style="margin-top:5px">${esc(l.signal||"")}</div>
    <div class="sub" style="margin-top:4px">👤 ${esc(l.profile||"")}</div>
    ${l.where?`<div class="sub" style="margin-top:4px">📍 哪里找:${esc(l.where)}</div>`:""}
    ${l.source_title?`<div class="sub" style="margin-top:4px">🔗 原帖:${esc(l.source_title)}</div>`:""}
    <div class="sub" style="margin-top:4px">💬 评论区版:${esc(l.script_comment||l.script||"")} <button class="btn sm" onclick="copyText(${cp(l.script_comment||l.script||"")})">📋</button></div>
    ${l.script_dm?`<div class="sub" style="margin-top:4px">✉️ 私信版:${esc(l.script_dm)} <button class="btn sm" onclick="copyText(${cp(l.script_dm)})">📋</button></div>`:""}
    <div class="actions lead-source-row" style="margin-top:8px">${source?`<a class="btn sm pri" href="${esc(source)}" target="_blank" rel="noopener noreferrer nofollow" referrerpolicy="no-referrer">↗ 查看原帖 · ${esc(new URL(source).hostname)}</a>`:
      `<span class="tag" title="这条历史线索当时没有保存真实网址">原帖链接未保存</span>`}</div></div>`; }).join("")}
  ${(L.followup||[]).length?`<div class="card" style="background:#f4f9f4;margin-top:8px"><b>📆 未来3天跟进清单</b>
    ${L.followup.map((f,i)=>`<div class="sub" style="margin-top:4px">${i+1}. ${esc(f)}</div>`).join("")}</div>`:""}
  <div class="notice" style="margin-top:6px">⚠️ ${esc(L.note||"人工逐条回复,别群发")}</div>
  <div class="actions" style="margin-top:8px"><button class="btn sm pri" onclick="keepReport(${cp(reportTitle)},${cp(reportMd)})">💾 有价值,沉淀到沉淀库</button>
    <button class="btn sm" onclick="copyText(${cp(reportMd)})">📋 复制全文</button></div>`;
}
function leadsMd(L, city=TS.ldCity||"", industry=TS.ldInd||""){
  return [`# 线索雷达(${city||"全国"}·${industry})`,"",`## 策略`,L.strategy||"","",
    `## 线索(${(L.leads||[]).length}条)`,
    ...(L.leads||[]).map((l,i)=>[`### ${i+1}. [${l.category||""}·${l.platform||""}·需求${l.intent||""}] ${l.time||""}`,
      l.signal||"",`- 画像:${l.profile||""}`,`- 哪里找:${l.where||""}`,
      leadSourceUrl(l)?`- [查看原帖](${leadSourceUrl(l)})`:"",
      `- 评论区话术:${l.script_comment||""}`,l.script_dm?`- 私信话术:${l.script_dm}`:""].filter(Boolean).join("\n")),"",
    `## 未来3天跟进`,...(L.followup||[]).map((f,i)=>`${i+1}. ${f}`)].join("\n");
}
async function leadsGo(btn){
  TS.ldInd = $("#ld-ind-c").value.trim()||$("#ld-ind .on")?.textContent||"通用";
  btn.disabled=true;
  try{ const r = await api("/tools/leads",{method:"POST",body:{industry:TS.ldInd, city:TS.ldCity||"", product:TS.ldProd||""}}); invalidateToolJobs(); toast("🎯 "+r.note); }
  catch(e){ toast(e.message); }
  render();
}
async function factoryGo(btn){
  const f = $("#ps-file").files[0]; if(!f) return toast("先选一张照片");
  // 合并端点:同一张照片只上传一次(此前并行两个接口要传两遍,4G 下时间翻倍),
  // 且带上传进度;两条腿各自计费退款,失败哪条说哪条。
  const fd = new FormData(); fd.append("file", f);
  fd.append("scene", $("#ps-scene").value); fd.append("want", $("#mc-want").value);
  await toolRun("shot", btn, async ()=>{
    const r = await xhrUpload("/api/tools/photo-factory", fd, $("#ps-file"));
    if(r.file) TS.shot = r.file;
    if(r.menu) TS.menu = r.menu;
    if(r.image_error) toast(r.image_error);
    if(r.copy_error) toast(r.copy_error);
  });
}
async function shotGo(btn){
  const f = $("#ps-file").files[0]; if(!f) return toast("先选一张产品照");
  btn.disabled=true; btn.innerHTML=`<span class="spin"></span> 美化中(约1分钟)…`;
  const fd = new FormData(); fd.append("file", f); fd.append("scene", $("#ps-scene").value);
  try{
    const r = await xhrUpload("/api/tools/product-shot", fd, $("#ps-file"));
    TS.shot = r.file; render();
  }catch(e){ toast(e.message); btn.disabled=false; btn.textContent="✨ 开始美化"; }
}
function menuHtml(m){
  return `<div class="topic"><b>${esc(m.item||"")}</b>
    <div class="sub" style="margin-top:4px">💎 ${esc(m.selling_point||"")}</div>
    <div class="sub" style="margin-top:4px">🛵 ${esc(m.desc||"")} <button class="btn sm" onclick="copyText(${cp(m.desc||"")})">📋</button></div>
    <div class="sub" style="margin-top:4px">📕 ${esc(m.xhs||"")} <button class="btn sm" onclick="copyText(${cp(m.xhs||"")})">📋</button></div>
    <div class="sub" style="margin-top:4px">💰 ${esc(m.price_note||"")}</div></div>`;
}
async function menuGo(btn){
  const f = $("#mc-file").files[0]; if(!f) return toast("先选一张图");
  btn.disabled=true; btn.innerHTML=`<span class="spin"></span> 识图撰写中…`;
  const fd = new FormData(); fd.append("file", f); fd.append("want", $("#mc-want").value);
  try{
    TS.menu = await xhrUpload("/api/tools/menu-copy", fd, $("#mc-file"));
    render();
  }catch(e){ toast(e.message); btn.disabled=false; btn.textContent="✍️ 开始写"; }
}
function varsHtml(V){
  return (V.variants||[]).map((v,i)=>`<div class="topic"><span class="tag">${esc(v.style||("版本"+(i+1)))}</span>
    <b>${esc(v.hook||"")}</b>
    <div class="sub" style="margin-top:4px;white-space:pre-wrap">${esc(v.script||"")}</div>
    <div class="actions" style="margin-top:6px">
      <button class="btn sm" onclick="copyText(${cp(v.script||"")})">📋 复制</button>
      <button class="btn sm pri" onclick="varTv(${i},this)">🎬 图文成片(3点)</button></div></div>`).join("");
}
async function varsGo(btn){
  TS.vrScript = $("#vr-script").value.trim();
  if(TS.vrScript.length<30) return toast("先贴一篇口播稿(至少30字)");
  const n = +$("#vr-n").value;
  await toolRun("vars", btn, async ()=>{ TS.vars = await api("/tools/variants",{method:"POST",body:{script:TS.vrScript, n, styles:""}}); });
}
async function varTv(i, btn){
  const v = (TS.vars?.variants||[])[i]; if(!v) return;
  btn.disabled=true; btn.textContent="排队…";
  try{
    await api("/text-video",{method:"POST",body:{title:(v.hook||v.style||"").slice(0,20), script:v.script,
      voice_id:$("#vr-voice")?.value||"", image_query:$("#vr-imgq")?.value||v.style||"",
      bgm:$("#vr-bgm")?.value||"warm"}});
    toast("🎬 已开工,约2-4分钟,下方可看进度"); varsTvs();
  }catch(e){ toast(e.message); }
  btn.disabled=false; btn.textContent="🎬 图文成片(3点)";
}
async function varsTvs(){
  const box = $("#vr-tvs"); if(!box) return;
  const videoContract=normalizeListContract(
    await api(listPath("/text-video","video",{standalone:1})).catch(optionalResult([])),
    LIST_PAGE_SIZE);
  const tvs = videoContract.items;
  box.innerHTML = (tvs.length?`<h3>🎬 我的成片</h3>`+listContractNotice(videoContract,"成片任务")+tvs.map(t=>{
    const last=(t.steps||[]).slice(-1)[0];
    return `<div class="topic"><b>${esc(t.params?.title||"成片")}</b>
      <span class="pill ${t.status}">${{queued:"排队",running:"合成中",done:"已出片",failed:"失败"}[t.status]||t.status}</span>
      ${t.status==="running"&&last?`<span class="sub"> ${esc(last.msg)}</span>`:""}
      ${t.status==="failed"?`<div class="sub">❌ ${esc(t.error||"")}(已退点)</div>`:""}
      ${t.status==="done"&&safeAssetUrl(t.video_file)?`<div class="actions" style="margin-top:6px">
        <a class="btn sm pri" href="${esc(safeAssetUrl(t.video_file))}" target="_blank" rel="noopener noreferrer">▶️ 看成片</a>
        <a class="btn sm" href="${esc(safeAssetUrl(t.video_file))}" download>⬇️ 下载</a>
        <button class="btn sm bad" onclick="tvDel(${t.id})">🗑</button></div>`:""}</div>`;}).join(""):"");
  box.innerHTML += listPager(videoContract,"video");
}
async function tvDel(id){
  if(!await uiConfirm("删除该成片?",{title:"删除成片",confirmText:"删除"})) return;
  await api(`/text-video/${id}`,{method:"DELETE"}).catch(e=>toast(e.message));
  render();
}

sse(); render();
