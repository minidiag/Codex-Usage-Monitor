'use strict';
(() => {
  const base = new URL('../', document.currentScript.src);
  const $ = id => document.getElementById(id);
  const labels = {total:'总 Token',input:'输入',output:'输出',cached_input:'缓存输入',reasoning_output:'推理输出'};
  const keys = ['input','cached_input','output','reasoning_output','total'];
  const fmt = n => BigInt(n).toLocaleString('zh-CN');
  const date = (s, hour=false) => new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',...(hour?{hour:'2-digit',minute:'2-digit',hourCycle:'h23'}:{})}).format(new Date(s));
  const fullDate = s => new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hourCycle:'h23'}).format(new Date(s));
  let token='',data=null,accountKeys=[],serverNames={},aliases={},generation=0,controller=null;
  try { aliases=JSON.parse(localStorage.getItem('codex-account-aliases')||'{}'); if(!aliases||typeof aliases!=='object'||Array.isArray(aliases)) aliases={}; } catch {}
  function message(text='') { $('message').textContent=text; $('message').hidden=!text; }
  function clear() { data=null; for(const k of ['total','input','output','cached_input']) $('stat-'+k).textContent='—'; $('chart').replaceChildren(); $('rows').replaceChildren(); $('table-total').replaceChildren(); $('empty').hidden=true; $('range').textContent='正在查询…'; $('events').textContent=''; $('cache-share').textContent='输入的子集，不重复计数'; }
  async function api(path,signal) {
    const response=await fetch(new URL(path,base),{headers:{Authorization:'Bearer '+token},cache:'no-store',credentials:'omit',signal});
    if(!response.ok) { if(response.status===401) throw Error('凭据无效或已撤销，请断开后重新连接。'); if(response.status===403) throw Error('需要有效的遥测只读凭据，或当前账号超出访问权限。'); if(response.status===429) throw Error('请求较频繁，请稍后刷新。'); throw Error('查询失败（'+response.status+'），请稍后重试。'); }
    return response.json();
  }
  function catalog(value) { accountKeys=value.accounts;serverNames=value.account_display_names||{}; }
  function accountOptions(selected='') { $('account').replaceChildren(new Option('全部账号（含未知归属）','')); for(const a of accountKeys) $('account').add(new Option((aliases[a]||serverNames[a]?.display_name||'账号 '+a.slice(0,10))+' · '+a.slice(-6),a)); $('account').value=accountKeys.includes(selected)?selected:''; }
  async function load() {
    const current=++generation; controller?.abort(); controller=new AbortController(); clear(); message(); $('refresh').disabled=true;
    const signal=controller.signal;
    try { const names=await api('v2/identities',signal);if(current!==generation)return;const selected=$('account').value;catalog(names);accountOptions(selected);
      const query=new URLSearchParams({period:$('period').value}); if($('account').value) query.set('account',$('account').value);
      const v=await api('v3/usage/trend?'+query,signal); if(current!==generation) return; data=v; render(); $('updated').textContent='更新于 '+fullDate(v.as_of)+'（北京）'; }
    catch(e) { if(current===generation&&e.name!=='AbortError') { message(e.message||'网络连接失败，请检查服务地址。'); $('range').textContent='数据暂不可用'; $('updated').textContent='本次查询失败'; } }
    finally { if(current===generation) $('refresh').disabled=false; }
  }
  function cell(row,text,tag='td') { const td=document.createElement(tag); td.textContent=text; row.append(td); }
  function render() {
    if(!data)return;
    for(const k of ['total','input','output','cached_input']) $('stat-'+k).textContent=fmt(data.totals[k]);
    const input=BigInt(data.totals.input); $('cache-share').textContent=input?'占输入 '+Number(BigInt(data.totals.cached_input)*1000n/input)/10+'% · 不重复计数':'输入的子集，不重复计数';
    $('scope').textContent=(data.scope==='principal'?'当前 Windows 用户':'只读凭据可见的全部用户')+' · '+($('account').value?$('account').selectedOptions[0].textContent:'全部账号，含未知归属')+' · 部分覆盖';
    $('range').textContent=fullDate(data.start)+' — '+fullDate(data.end)+' · 北京时间 · '+(data.interval==='hour'?'按小时':'按日');
    $('events').textContent=fmt(data.events)+' 条增量';
    $('rows').replaceChildren();
    for(const p of data.points) { const tr=document.createElement('tr');cell(tr,date(p.start,data.interval==='hour')); for(const k of keys)cell(tr,fmt(p.totals[k]));cell(tr,fmt(p.events));$('rows').append(tr); }
    const tr=document.createElement('tr');cell(tr,'合计');for(const k of keys)cell(tr,fmt(data.totals[k]));cell(tr,fmt(data.events));$('table-total').replaceChildren(tr);
    $('empty').hidden=BigInt(data.events)!==0n; draw();
  }
  function svg(tag,attrs={},text) { const e=document.createElementNS('http://www.w3.org/2000/svg',tag);for(const [k,v] of Object.entries(attrs))e.setAttribute(k,String(v));if(text!==undefined)e.textContent=text;return e; }
  function short(n) { if(n>=1000000000n)return Number(n/1000000n)/1000+'B'; if(n>=1000000n)return Number(n/1000n)/1000+'M';if(n>=1000n)return Number(n/100n)/10+'k';return String(n); }
  function draw() {
    if(!data)return;const metric=$('metric').value,points=data.points;
    $('legend-primary').textContent=metric==='total'?'输入':labels[metric];$('legend-output').hidden=metric!=='total';
    const width=Math.max(320,Math.round($('chart').getBoundingClientRect().width)-36),height=290,left=65,right=18,top=20,bottom=40,plotHeight=height-top-bottom,plotWidth=width-left-right;
    const max=points.reduce((m,p)=>{const v=BigInt(p.totals[metric]);return v>m?v:m;},1n);
    const root=svg('svg',{viewBox:`0 0 ${width} ${height}`,role:'img','aria-label':labels[metric]+'用量趋势，详细数值可切换表格查看'});
    for(let i=0;i<=4;i++){const y=top+plotHeight*i/4;root.append(svg('line',{x1:left,y1:y,x2:width-right,y2:y,class:'grid'}));root.append(svg('text',{x:left-12,y:y+4,'text-anchor':'end',class:'axis-label'},short(max*BigInt(4-i)/4n)));}
    const step=plotWidth/points.length,bar=Math.min(step*.58,42),scale=n=>Number(BigInt(n)*1000000n/max)/1000000*plotHeight;
    const ticks=Math.max(2,Math.floor(plotWidth/65)),tickStep=Math.max(1,Math.ceil(points.length/ticks));
    points.forEach((p,i)=>{const x=left+step*(i+.5)-bar/2;const a=metric==='total'?p.totals.input:p.totals[metric];const b=metric==='total'?p.totals.output:'0';const ah=scale(a),bh=scale(b);const g=svg('g');g.append(svg('title',{},date(p.start,data.interval==='hour')+' · '+labels[metric]+' '+fmt(p.totals[metric])));g.append(svg('rect',{x,y:top+plotHeight-ah,width:bar,height:ah,class:'bar-input',rx:2}));if(bh)g.append(svg('rect',{x,y:top+plotHeight-ah-bh,width:bar,height:bh,class:'bar-output',rx:2}));root.append(g);if(i%tickStep===0){const label=data.interval==='hour'?new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',hour:'2-digit',minute:'2-digit',hourCycle:'h23'}).format(new Date(p.start)):date(p.start);root.append(svg('text',{x:left+step*(i+.5),y:height-12,'text-anchor':'middle',class:'axis-label'},label));}});
    $('chart').replaceChildren(root);
  }
  $('login-form').addEventListener('submit',async e=>{e.preventDefault();token=$('token').value.trim();message();const button=e.submitter;button.disabled=true;try {const v=await api('v2/identities');catalog(v);accountOptions();$('token').value='';$('login').hidden=true;$('workspace').hidden=false;$('logout').hidden=false;$('connection').textContent='● 已连接账本';await load();} catch(e){token='';message(e.message||'连接失败，请检查网络。');}finally{button.disabled=false;}});
  $('logout').addEventListener('click',()=>{generation++;controller?.abort();token='';clear();serverNames={};accountKeys=[];accountOptions();$('token').value='';$('workspace').hidden=true;$('login').hidden=false;$('logout').hidden=true;$('connection').textContent='尚未连接';message();});
  $('refresh').addEventListener('click',load);$('account').addEventListener('change',load);$('period').addEventListener('change',load);$('metric').addEventListener('change',draw);
  function view(table) {$('table-view').hidden=!table;$('chart-view').hidden=table;$('show-chart').setAttribute('aria-pressed',String(!table));$('show-table').setAttribute('aria-pressed',String(table));}
  $('show-chart').addEventListener('click',()=>view(false));$('show-table').addEventListener('click',()=>view(true));
  $('rename').addEventListener('click',()=>{const a=$('account').value;if(!a){message('请先选择一个账号，再设置仅本浏览器可见的备注名。');return;}$('alias-dialog').dataset.account=a;$('alias-input').value=aliases[a]||'';$('alias-dialog').showModal();$('alias-input').focus();});
  $('alias-cancel').addEventListener('click',()=>$('alias-dialog').close());
  $('alias-form').addEventListener('submit',e=>{e.preventDefault();const a=$('alias-dialog').dataset.account;aliases[a]=$('alias-input').value.trim().slice(0,60);try{localStorage.setItem('codex-account-aliases',JSON.stringify(aliases));}catch{message('浏览器未允许保存备注名，本次页面仍可使用。');}accountOptions(a);if(data)render();$('alias-dialog').close();});
  $('export').addEventListener('click',()=>{if(!data)return;const rows=[['北京时间','输入','缓存输入','输出','推理输出','总量','增量条数'],...data.points.map(p=>[fullDate(p.start),...keys.map(k=>p.totals[k]),p.events]),['合计',...keys.map(k=>data.totals[k]),data.events]];const url=URL.createObjectURL(new Blob(['\ufeff'+rows.map(r=>r.join(',')).join('\r\n')],{type:'text/csv;charset=utf-8'}));const a=document.createElement('a');a.href=url;a.download='codex-usage-'+data.period+'.csv';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);});
  new ResizeObserver(()=>draw()).observe($('chart'));
})();
