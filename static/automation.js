import {esc,button,field,empty,formatTime,icon} from './core.js';

const input=(value,attrs='')=>`<input value="${esc(value)}" ${attrs}>`;
const labels={queued:'已排队',running:'执行中',completed:'已结束',cancelled:'已取消',failed:'任务异常',interrupted:'进程中断，请核对已发送内容'};
const TIME_PRESETS=['00:05','08:00','09:00','12:00','21:00'];
const HOURS=Array.from({length:24},(_,i)=>String(i).padStart(2,'0'));
const MINUTES=Array.from({length:60},(_,i)=>String(i).padStart(2,'0'));

const POPULAR_TIMEZONES=[
 {group:'常用 · 东亚',items:[
  ['Asia/Hong_Kong','香港'],
  ['Asia/Shanghai','上海 · 北京时间'],
  ['Asia/Taipei','台北'],
  ['Asia/Macau','澳门'],
  ['Asia/Tokyo','东京'],
  ['Asia/Seoul','首尔'],
  ['Asia/Singapore','新加坡'],
 ]},
 {group:'亚太与大洋洲',items:[
  ['Asia/Bangkok','曼谷'],
  ['Asia/Ho_Chi_Minh','胡志明市'],
  ['Asia/Kuala_Lumpur','吉隆坡'],
  ['Asia/Jakarta','雅加达'],
  ['Asia/Manila','马尼拉'],
  ['Asia/Kolkata','加尔各答 · 印度'],
  ['Asia/Dubai','迪拜'],
  ['Australia/Sydney','悉尼'],
  ['Australia/Melbourne','墨尔本'],
  ['Pacific/Auckland','奥克兰'],
 ]},
 {group:'欧洲与协调世界时',items:[
  ['UTC','协调世界时'],
  ['Europe/London','伦敦'],
  ['Europe/Paris','巴黎'],
  ['Europe/Berlin','柏林'],
  ['Europe/Amsterdam','阿姆斯特丹'],
  ['Europe/Zurich','苏黎世'],
  ['Europe/Moscow','莫斯科'],
  ['Europe/Istanbul','伊斯坦布尔'],
 ]},
 {group:'美洲',items:[
  ['America/New_York','纽约 · 美东'],
  ['America/Chicago','芝加哥 · 美中'],
  ['America/Denver','丹佛 · 美山'],
  ['America/Los_Angeles','洛杉矶 · 美西'],
  ['America/Toronto','多伦多'],
  ['America/Vancouver','温哥华'],
  ['America/Sao_Paulo','圣保罗'],
 ]},
];

const POPULAR_MAP=new Map(POPULAR_TIMEZONES.flatMap(g=>g.items));
const OFFSET_CACHE=new Map();
let ALL_IANA=null;

export function tzOffset(zone){
 const z=String(zone||'').trim();
 if(!z)return '';
 if(OFFSET_CACHE.has(z))return OFFSET_CACHE.get(z);
 let result='';
 try{
  const parts=new Intl.DateTimeFormat('en-US',{timeZone:z,timeZoneName:'shortOffset'}).formatToParts(new Date());
  const raw=parts.find(p=>p.type==='timeZoneName')?.value||'';
  result=raw==='GMT'?'UTC':raw.replace(/^GMT/,'UTC');
 }catch{result='';}
 OFFSET_CACHE.set(z,result);
 return result;
}

function allIanaZones(){
 if(ALL_IANA)return ALL_IANA;
 try{
  const list=typeof Intl.supportedValuesOf==='function'?Intl.supportedValuesOf('timeZone'):[];
  const set=new Set(['UTC',...list]);
  ALL_IANA=[...set];
 }catch{
  ALL_IANA=[...POPULAR_MAP.keys()];
 }
 return ALL_IANA;
}

function browserTimezone(){
 try{return Intl.DateTimeFormat().resolvedOptions().timeZone||'';}catch{return '';}
}

export function parseTimeParts(value){
 const m=/^(\d{1,2}):(\d{1,2})$/.exec(String(value||'').trim());
 if(!m)return {hour:'09',minute:'00'};
 const h=Number(m[1]),min=Number(m[2]);
 if(h<0||h>23||min<0||min>59)return {hour:'09',minute:'00'};
 return {hour:String(h).padStart(2,'0'),minute:String(min).padStart(2,'0')};
}

function tzItemHtml(id,label,current){
 const selected=String(current||'').trim()===id;
 const offset=tzOffset(id);
 return `<button type="button" class="tz-option${selected?' active':''}" data-action="pick-timezone" data-tz="${esc(id)}" ${selected?'data-selected="true" aria-selected="true"':'aria-selected="false"'}>
  <span class="tz-option-main"><span class="tz-option-id">${esc(id)}</span>${label?`<span class="tz-option-label">${esc(label)}</span>`:''}</span>
  <span class="tz-option-meta">${offset?`<span class="tz-offset">${esc(offset)}</span>`:''}${selected?`<span class="tz-check">${icon('check')}</span>`:''}</span>
 </button>`;
}

export function renderTimezoneList(currentZone,query='',showAll=false){
 const q=String(query||'').trim().toLocaleLowerCase();
 if(!q&&!showAll){
  const total=allIanaZones().length;
  const sections=POPULAR_TIMEZONES.map(sec=>`<div class="tz-group"><div class="tz-group-title">${esc(sec.group)}</div>${sec.items.map(([id,label])=>tzItemHtml(id,label,currentZone)).join('')}</div>`).join('');
  const more=total>POPULAR_MAP.size?`<div class="tz-more"><button type="button" class="small ghost" data-action="tz-show-all">展开全部 ${total} 个 IANA 时区</button></div>`:'';
  return sections+more;
 }
 const seen=new Set();
 const matchedPopular=[];
 for(const sec of POPULAR_TIMEZONES){
  const items=sec.items.filter(([id,label])=>{
   if(!q)return true;
   const hay=`${id} ${label} ${tzOffset(id)}`.toLocaleLowerCase();
   return hay.includes(q);
  });
  if(items.length){
   items.forEach(([id])=>seen.add(id));
   matchedPopular.push({group:sec.group,items});
  }
 }
 const others=allIanaZones().filter(id=>{
  if(seen.has(id))return false;
  if(!q)return true;
  const hay=`${id} ${id.replace(/_/g,' ')} ${tzOffset(id)}`.toLocaleLowerCase();
  return hay.includes(q);
 });
 if(!matchedPopular.length&&!others.length){
  return `<div class="tz-empty">未找到匹配「${esc(query)}」的时区，可直接输入完整 IANA 标识（如 Asia/Hong_Kong）。</div>`;
 }
 let html=matchedPopular.map(sec=>`<div class="tz-group"><div class="tz-group-title">${esc(sec.group)}</div>${sec.items.map(([id,label])=>tzItemHtml(id,label,currentZone)).join('')}</div>`).join('');
 if(others.length){
  const cap=q?80:others.length;
  html+=`<div class="tz-group"><div class="tz-group-title">${q?'其他匹配的 IANA 时区':'全部其他 IANA 时区'}（${others.length}）</div>${others.slice(0,cap).map(id=>tzItemHtml(id,'',currentZone)).join('')}</div>`;
 }
 return html;
}

export function timePopoverHtml(value){
 const {hour,minute}=parseTimeParts(value);
 const normalized=/^(?:[01]\d|2[0-3]):[0-5]\d$/.test(String(value||'').trim())?String(value).trim():`${hour}:${minute}`;
 return `<div class="picker-popover time-popover" role="dialog" aria-label="时间选择器">
  <div class="picker-header">
   <span class="picker-title">选择执行时间</span>
   <span class="picker-preview mono">${esc(normalized)}</span>
  </div>
  <div class="time-presets" role="group" aria-label="常用时间">${TIME_PRESETS.map(t=>`<button type="button" class="time-preset${normalized===t?' active':''}" data-action="pick-time" data-time="${t}">${t}</button>`).join('')}</div>
  <div class="time-columns">
   <div class="time-col">
    <div class="time-col-head">小时</div>
    <div class="time-col-list" data-time-col="hour">${HOURS.map(h=>`<button type="button" class="time-cell${hour===h?' active':''}" data-action="pick-hour" data-hour="${h}" ${hour===h?'data-selected="true"':''}>${h}</button>`).join('')}</div>
   </div>
   <div class="time-col">
    <div class="time-col-head">分钟</div>
    <div class="time-col-list" data-time-col="minute">${MINUTES.map(m=>`<button type="button" class="time-cell${minute===m?' active':''}" data-action="pick-minute" data-minute="${m}" ${minute===m?'data-selected="true"':''}>${m}</button>`).join('')}</div>
   </div>
  </div>
  <div class="picker-footer">
   <span class="small-note">也可直接在上方输入 HH:MM</span>
   <button type="button" class="primary small" data-action="close-picker">完成</button>
  </div>
 </div>`;
}

export function timezonePopoverHtml(value,query='',showAll=false){
 const localZone=browserTimezone();
 return `<div class="picker-popover tz-popover" role="dialog" aria-label="时区选择器">
  <div class="tz-toolbar">
   <div class="tz-search-wrap">${icon('search')}<input id="tz-search" class="tz-search" type="search" data-control="tz-search" value="${esc(query||'')}" placeholder="搜索城市、时区名或 UTC+8…" autocomplete="off" aria-label="搜索时区"></div>
   ${localZone?`<button type="button" class="small ghost tz-local-btn" data-action="pick-timezone" data-tz="${esc(localZone)}">${icon('globe')}本机（${esc(localZone)}）</button>`:''}
  </div>
  <div class="tz-list" role="listbox" aria-label="可选时区列表">${renderTimezoneList(value,query,showAll)}</div>
 </div>`;
}

function timePickerField(value,isOpen){
 return `<div class="field">
  <label for="schedule-time">每天执行时间</label>
  <div class="picker-wrap${isOpen?' is-open':''}" data-picker="time">
   <div class="picker-input-row">
    <input id="schedule-time" class="picker-input mono" type="text" inputmode="numeric" maxlength="5" autocomplete="off" placeholder="HH:MM" value="${esc(value)}" data-schedule="time" aria-label="每天执行时间" required>
    <button type="button" class="picker-trigger" data-action="toggle-time-picker" aria-label="选择执行时间" aria-expanded="${isOpen?'true':'false'}">${icon('clock')}</button>
   </div>
   ${isOpen?timePopoverHtml(value):''}
  </div>
 </div>`;
}

function timezonePickerField(value,isOpen,query,showAll){
 const offset=tzOffset(value);
 return `<div class="field">
  <label for="schedule-timezone">运行时区</label>
  <div class="picker-wrap${isOpen?' is-open':''}" data-picker="timezone">
   <div class="picker-input-row">
    <input id="schedule-timezone" class="picker-input" type="text" autocomplete="off" placeholder="例如 Asia/Hong_Kong" value="${esc(value)}" data-schedule="timezone" aria-label="运行时区" required>
    <span class="tz-offset-badge" data-tz-badge ${offset?'':'hidden'}>${esc(offset)}</span>
    <button type="button" class="picker-trigger" data-action="toggle-tz-picker" aria-label="选择运行时区" aria-expanded="${isOpen?'true':'false'}">${icon('chevron')}</button>
   </div>
   ${isOpen?timezonePopoverHtml(value,query,showAll):''}
  </div>
  <small>签到日期和历史记录也使用此时区</small>
 </div>`;
}

export function automationView(state){
 const data=state.automation,draft=state.automationDraft;
 if(!data||!draft)return empty('正在读取定时与通知设置…');
 const s=draft.schedule,n=draft.notifications,busy=state.status?.busy;
 const runtime=state.status?.automation||data,delivery=state.status?.notification||data.notification||{};
 const savedZone=runtime.settings?.timezone||s.timezone;
 return `<div class="page-heading"><div><h1>定时与微信通知</h1><p class="subtitle">设置按天执行的签到计划，整轮结束后合并推送结果。</p></div><div class="actions">${button('reload-automation',state.automationDirty?'放弃修改并载入':'重新载入')}${button('save-automation','保存设置',{primary:true,disabled:busy})}</div></div>
 <div class="automation-grid"><section class="panel"><div class="panel-heading"><div><div class="eyebrow">每日计划</div><h2>每天自动签到</h2></div><label class="check-label"><input type="checkbox" data-schedule="enabled" ${s.enabled?'checked':''}>启用</label></div>
 <div class="fields">${timePickerField(s.time,state.openPicker==='time')}${timezonePickerField(s.timezone,state.openPicker==='timezone',state.tzQuery,state.tzShowAll)}
 <fieldset class="account-scope"><legend>定时执行账号</legend><div class="actions"><label class="check-label"><input type="radio" name="schedule-scope" data-schedule-scope="all" ${state.automationScope==='all'?'checked':''}>全部账号</label><label class="check-label"><input type="radio" name="schedule-scope" data-schedule-scope="selected" ${state.automationScope==='selected'?'checked':''}>指定账号</label></div>${state.automationScope==='selected'?`<div class="schedule-accounts">${(state.status?.accounts||[]).map(a=>`<label class="check-label"><input type="checkbox" data-schedule-account="${esc(a.name)}" ${s.accounts.includes(a.name)?'checked':''}>${esc(a.name)}</label>`).join('')}</div>`:''}<p class="small-note">${state.automationScope==='all'?'全部已配置账号，新增账号也会加入计划。':'仅执行勾选账号；至少选择一个。'}</p></fieldset>
 <div><label class="check-label"><input type="checkbox" data-schedule="headless" ${s.headless?'checked':''}>定时任务使用后台浏览器</label><label class="check-label"><input type="checkbox" data-schedule="catch_up" ${s.catch_up?'checked':''}>当天错过时间，服务恢复后补跑一次</label></div></div>
 <div class="runtime-card"><div class="eyebrow">已保存的计划</div><p class="automation-next">${runtime.settings?.enabled?esc(formatTime(runtime.next_run,savedZone)||'等待执行'):'定时签到未启用'}</p><p class="small-note automation-last">上次：${esc(labels[runtime.last_run?.status]||'尚无定时执行记录')}${runtime.last_run?.started?' · '+esc(formatTime(runtime.last_run.started,savedZone)):''}</p><p class="small-note automation-service">${esc(runtime.error||runtime.service_error||(!runtime.active?'定时服务未就绪，请检查服务进程':'控制台服务正在管理计划'))}</p></div>
 <p class="footnote">服务需保持运行。当天已发送或发送状态不明的群不会自动重发；进程中断后，同日自动计划也不会再次发起。已运行的当天计划不会因修改时间重跑。</p></section>
 <section class="panel"><div class="panel-heading"><div><div class="eyebrow">SERVER酱</div><h2>微信结果通知</h2></div><label class="check-label"><input type="checkbox" data-notification="enabled" ${n.enabled?'checked':''}>启用</label></div>
 <p class="small-note">整轮汇总成功、待核对、失败和草稿恢复异常。未确认的反馈会如实标记。</p><div class="fields">${field('SendKey',input(state.automationKey||'',`type="password" id="serverchan-key" aria-label="SendKey" autocomplete="new-password" placeholder="${data.key?.configured?'已配置，留空保留原密钥':'粘贴 SCT 或 sctp 开头的 SendKey'}"`),'保存后不回显；环境变量 SERVERCHAN_SENDKEY 优先')}<label class="check-label"><input type="checkbox" data-notification="only_on_issues" ${n.only_on_issues?'checked':''}>仅在待核对、失败或任务异常时推送</label></div>
 <div class="actions section-gap"><span class="tag">${data.key?.configured?'密钥已配置'+(data.key.source==='environment'?' · 环境变量':''):'尚未配置密钥'}</span>${button('test-notification','发送测试通知',{disabled:state.automationDirty||!data.key?.configured||state.testingNotification})}</div><p class="small-note">${esc(data.key?.error||'先保存设置，再用当前已保存结果预览分类样式；测试不触发签到。')}</p>
 <div class="runtime-card"><div class="eyebrow">最近推送</div><p class="notification-summary">${esc(delivery.time?`${delivery.ok?'Server酱已接受':'推送未完成'} · ${formatTime(delivery.time,savedZone)}`:'尚未推送')}</p><p class="small-note notification-detail">${esc(delivery.error||delivery.message||'收到微信消息后即可确认接收通道。')}</p></div>
 <p class="footnote"><a href="https://sct.ftqq.com/" target="_blank" rel="noreferrer">打开 Server酱</a> 配置微信通道。推送失败不会影响签到结果，不会自动重试占用额度。</p></section></div><div class="actions"><span class="save-state">${state.automationDirty?'有未保存修改':'设置已保存'}</span></div>`;
}
