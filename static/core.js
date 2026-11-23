export const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
export const copy = value => structuredClone(value);
export const lines = value => Array.isArray(value) ? value.join('\n') : String(value ?? '');
export const words = value => value.split('\n').map(s => s.trim()).filter(Boolean);
export const normalizeValue = value => { const result=words(value); return result.length===1?result[0]:result; };
export const range = (value, fallback=[1,3]) => typeof value==='number'?[value,value]:Array.isArray(value)&&value.length===2?value:fallback;
export const formatTime = (value,zone='Asia/Hong_Kong') => {if(!value)return '';try{return new Date(value).toLocaleString('zh-CN',{timeZone:zone,hour12:false});}catch{return String(value);}};
export const methods = {text:'文字签到',command:'口令签到',command_button:'口令＋按钮',button_only:'仅按钮',image_text:'图片＋文字',image_only:'仅图片'};
export const sourceLabels = {button_popup:'机器人按钮反馈弹窗',shared:'共用系统代签',reply_to_sent:'明确回复本次消息',mention_self:'明确提及本账号',private_chat:'本次发送后的私聊反馈',unknown:'尚无可信归属证据',legacy:'旧版记录 · 未留存归属证据'};
export const stageLabels = {completed:'已完成',failed:'已终止',awaiting_confirmation:'等待确认',recheck_failed:'补查未完成'};
export const pages = {overview:['签到工作台','layout'],accounts:['账号与规则','users'],automation:['定时与通知','settings'],history:['执行记录','history'],stats:['统计','chart'],logs:['运行日志','terminal']};
const paths = {close:'M6 6l12 12M18 6L6 18',layout:'M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z', users:'M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2 M16 3a4 4 0 0 1 0 8 M22 21v-2a4 4 0 0 0-3-3.87 M13 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0',history:'M3 12a9 9 0 1 0 3-6.7 M3 3v6h6 M12 7v5l3 2',chart:'M4 20V10 M10 20V4 M16 20v-7 M22 20H2',terminal:'m5 7 5 5-5 5 M13 17h6',play:'m8 4 12 8-12 8z',shield:'m12 3 8 4v6c0 5-8 8-8 8s-8-3-8-8V7z m-4 9 3 3 5-6',search:'M21 21l-5-5 M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0',settings:'M4 7h10 M18 7h2 M4 12h4 M12 12h8 M4 17h9 M17 17h3',refresh:'M20 7a9 9 0 1 0 1 7 M20 3v5h-5',stop:'M6 6h12v12H6z',plus:'M12 5v14 M5 12h14',arrow:'m9 5 7 7-7 7',download:'M12 3v12 m-5-5 5 5 5-5 M5 17v4h14v-4',check:'m5 12 4 4L19 6',clock:'M12 7v5l3.5 2 M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0',globe:'M12 2a10 10 0 1 0 0 20 10 10 0 1 0 0-20 M2 12h20 M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10',chevron:'m6 9 6 6 6-6'};
export const icon = name => `<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${paths[name]||paths.settings}"></path></svg>`;
export const button = (action,label,{primary=false,iconName='',extra='',disabled=false,kind=''}={}) => `<button type="button" data-action="${action}" class="${primary?'primary ':''}${kind}" ${disabled?'disabled':''} ${extra}>${iconName?icon(iconName):''}${label}</button>`;
export const keyOf = row => JSON.stringify([row.account,row.chat]);
export const flatten = status => (status?.accounts||[]).flatMap(a=>(a.groups||[]).map(g=>({...g,account:a.name,login:a.status})));
export const actionsOf = group => copy(group.actions?.length?group.actions:[{method:group.method||(group.image?(group.message?'image_text':'image_only'):group.click_button?'command_button':'text'),message:group.message??'签到',image:group.image||'',click_button:group.click_button||'',button_message:group.button_message||''}]);
export const conversationLabels = {group:'群组/频道',forum:'论坛话题',private:'私聊/机器人',unknown:'类型待识别'};
export function conversationKind(row){
 const peer=String(row.chat).startsWith('@')&&row.sent===true&&row.source!=='shared'?String(row.expected_peer||row.chat):String(row.chat);
 return row.is_forum||row.topic?.trim()?'forum':/^-\d+$/.test(peer)?'group':/^\d+$/.test(peer)?'private':'unknown';
}
export function actionMethods(row){
 const listed=row.methods?.length?row.methods:row.actions?.map(action=>action.method).filter(method=>methods[method]);
 return [...new Set((listed?.length?listed:[row.method||'text']).filter(method=>methods[method]))];
}
export const isMultiStep = row => Number(row.action_count||row.methods?.length||row.actions?.length||1)>1;
export function configuredRows(status,cfg){
 return flatten(status).map(row=>{
  if(row.methods?.length)return row;
  const group=cfg?.accounts?.find(account=>account.name===row.account)?.groups?.find(group=>String(group.chat)===String(row.chat));
  if(!group)return row;
  const actions=actionsOf(group);
  return {...row,methods:actions.map(action=>action.method||(action.image?(action.message?'image_text':'image_only'):action.click_button?'command_button':'text')),action_count:actions.length};
 });
}
export const matchesSearch = (row,query) => !query.trim() || [row.name,row.chat,row.account,row.topic,row.share_group,...actionMethods(row).map(method=>methods[method])].filter(Boolean).join(' ').toLocaleLowerCase().includes(query.trim().toLocaleLowerCase());
export function methodLabel(group){
 const names={text:'文字',command:'口令',command_button:'口令＋按钮',button_only:'仅按钮',image_text:'图文',image_only:'图片'};
 return isMultiStep(group)?`${actionMethods(group).map(method=>names[method]).join(' / ')} · ${group.action_count||group.methods?.length||group.actions?.length} 步`:methods[group.method]||'文字签到';
}
export const sendUncertain = row => (row.actions||[]).some(action=>action.sent&&!action.confirmed_by_reply&&(action.warning&&!action.sent_mids?.length||action.sent_mids?.length&&action.sent_mids.every(mid=>String(mid).includes('.'))));
export const sendLabel = row => row.confirm&&!(row.confirm==='failed'&&row.sent===false)?'单群重签':'单群签到';
export function resultHint(row){
 if(row.draft_warning)return '原草稿恢复需要处理';
 if(row.confirm==='unconfirmed'&&sendUncertain(row))return row.actions.some(action=>action.sent_mids?.length)?'仅有临时消息 ID，请先核对 Telegram':'缺少发送消息 ID，请先核对 Telegram';
 if(row.confirm==='unconfirmed'&&row.feedback_hint?.message)return row.feedback_hint.message;
 if(row.confirm==='unconfirmed')return row.candidate_replies?.length?'已捕获候选反馈，需核对归属':row.can_recheck?'可先补查反馈，无需重发':'缺少发送消息 ID，请先核对 Telegram';
 if(row.confirm!=='failed')return '';
 const error=row.error||'';
 if(/消息区|最新消息/.test(error))return '消息区尚未就绪，本次未发送';
 if(/话题/.test(error))return '话题定位未完成，请检查目标';
 if(/登录/.test(error))return '请检查账号登录状态';
 if(/Timeout|超时|未能打开|超过/.test(error))return '会话定位未完成，可先只读检查';
 return row.sent===false?'尚未发送，请查看错误详情':'动作中断，请先核对已发内容';
}
export function statusInfo(row){
 if(row.confirm==='success')return {code:'success',label:row.source==='shared'?'共享成功':row.source==='legacy'?'旧版判定成功':'已确认',symbol:row.source==='legacy'?'~':'✓',legacy:row.source==='legacy'};
 if(row.confirm==='unconfirmed')return {code:'unconfirmed',label:sendUncertain(row)?'发送待核对':'已发未确认',symbol:'◷'};
 if(row.confirm==='failed')return {code:'failed',label:row.sent===false?'未发送 · 失败':'执行失败',symbol:'!'};
 return {code:'pending',label:'待执行',symbol:'○'};
}
export const badge = row => {const s=statusInfo(row);return `<span class="status-badge ${s.code}${s.legacy?' legacy':''}"><span aria-hidden="true">${s.symbol}</span>${s.label}</span>`;};
export function notify(message,error=false){const el=document.createElement('div');el.className='toast'+(error?' error':'');el.textContent=message;const host=document.querySelector('#notifications');while(host.children.length>=2)host.firstElementChild.remove();host.append(el);if(typeof host.showPopover==='function'){try{host.hidePopover();host.showPopover();}catch{}}setTimeout(()=>{el.remove();if(!host.children.length&&typeof host.hidePopover==='function'){try{host.hidePopover();}catch{}}},error?8000:4000);}
export async function api(path,body,options={}){
 const response=await fetch(path,{...options,method:body===undefined?'GET':'POST',headers:{'X-Token':sessionStorage.getItem('gui_token')||localStorage.getItem('gui_token')||'',...(body===undefined?{}:{'Content-Type':'application/json'}),...options.headers},...(body===undefined?{}:{body:JSON.stringify(body)})});
 if(response.status===401){const dialog=document.querySelector('#token-dialog');if(!dialog.open)dialog.showModal();throw new Error('请输入访问令牌');}
 const result=await response.json().catch(()=>({error:'服务响应异常，请查看运行日志'}));
 if(!response.ok||result.ok===false)throw new Error(result.error||`请求失败 (${response.status})`);
 return result;
}
export async function download(path,filename){const response=await fetch(path,{headers:{'X-Token':sessionStorage.getItem('gui_token')||localStorage.getItem('gui_token')||''}});if(!response.ok)throw new Error('下载失败，请检查访问令牌');const url=URL.createObjectURL(await response.blob());const link=document.createElement('a');link.href=url;link.download=filename;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
export const option = (value,label,selected) => `<option value="${esc(value)}" ${String(value)===String(selected)?'selected':''}>${esc(label)}</option>`;
export const field = (label,control,hint='') => `<label class="field"><span>${label}</span>${control}${hint?`<small>${hint}</small>`:''}</label>`;
export const empty = (title,description='') => `<div class="empty-state"><h3>${esc(title)}</h3>${description?`<p>${esc(description)}</p>`:''}</div>`;
