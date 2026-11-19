import {api,download,notify,copy,esc,icon,button,pages,flatten,keyOf,methods,option,range,words,normalizeValue,actionsOf,lines,formatTime} from './core.js';
import {accountsView,groupEditor,historyView,statsView,logsView} from './views.js';
import {automationView} from './automation.js';
import {workbench} from './workbench.js';

const main=document.querySelector('#main'),modal=document.querySelector('#modal'),chooser=document.querySelector('#chooser');
const state={page:'overview',status:null,selected:null,query:'',accountFilter:'',filter:'all',focus:false,kindFilter:'',methodFilter:'',structureFilter:'',systemFilter:'',catalogOpen:false,
 cfg:null,draft:null,revision:null,dirty:false,origins:new WeakMap(),accountIndex:0,checkedGroups:new Set(),globalOpen:false,
 history:null,historyFilter:{q:'',account:'',status:'',from:'',to:''},historyPage:1,stats:null,statsDays:30,logs:null,logLevel:'',logQuery:'',editor:null,
 chooser:{dialogs:[],folders:[],checked:new Set(),query:'',folder:'',loading:false,error:''},pendingPlan:null,pendingDelete:null,
 automation:null,automationDraft:null,automationKey:'',automationScope:'all',automationDirty:false,testingNotification:false};

function syncExecutionButtons(){
 const busy=state.status?.busy,accounts=state.status?.accounts||[];
 main.querySelectorAll('[data-action="run-chat"],[data-action="run-account"],[data-action="run-account-filter"]').forEach(el=>{
  let unavailable=busy||(state.page==='accounts'&&state.dirty);
  if(el.dataset.action==='run-account-filter')unavailable||=!state.accountFilter;
  if(el.dataset.action==='run-account')unavailable||=!accounts.some(a=>a.name===el.dataset.account);
  el.disabled=!!unavailable;
 });
}
function syncActivityRows(){
 main.querySelectorAll('[data-row-key]').forEach(el=>{
  const [account,chat]=JSON.parse(el.dataset.rowKey),progress=state.status?.progress?.[account];
  const active=!!(state.status?.busy&&progress?.chat===chat),stage=el.querySelector('.row-stage');
  el.classList.toggle('is-running',active);
  if(stage){stage.hidden=!active;stage.textContent=active?'进行中 · '+progress.stage+sendElapsed(progress):'';}
 });
}
function sendElapsed(progress){return progress?.sent_at?' · 发送后 '+Math.max(0,Math.floor((state.status?.server_time||Date.now()/1000)-progress.sent_at))+' 秒':'';}
function activeConversation(){
 if(!state.status?.busy||!['checkin','recheck','probe'].includes(state.status.job?.mode))return null;
 for(const [account,item] of Object.entries(state.status.progress||{})){
  if(!item.chat||['已完成','已取消'].includes(item.stage))continue;
  const row=flatten(state.status).find(row=>row.account===account&&row.chat===item.chat);
  if(row)return row;
 }
 return null;
}
function markDirty(){state.dirty=true;document.querySelector('#draft-indicator').hidden=false;main.querySelectorAll('.save-state').forEach(el=>el.textContent='有未保存修改');syncExecutionButtons();}
function render(){
 const focused=document.activeElement?.id,position=document.activeElement?.selectionStart;
 const inspector=main.querySelector('.inspector'),inspected=inspector?.dataset.inspectedKey,evidenceOpen=inspector?.querySelector('details')?.open;
 document.querySelector('#navigation').innerHTML=Object.entries(pages).map(([key,[label,iconName]])=>`<button type="button" id="nav-${key}" data-page="${key}" ${state.page===key?'aria-current="page"':''}>${icon(iconName)}<span>${label}</span></button>`).join('');
 document.querySelector('#breadcrumb').textContent=pages[state.page][0];
 document.querySelector('#draft-indicator').hidden=!(state.dirty||state.automationDirty);
 main.innerHTML=({overview:workbench,accounts:accountsView,automation:automationView,history:historyView,stats:statsView,logs:logsView}[state.page])(state);
 syncExecutionButtons();
 syncActivityRows();
 const nextInspector=main.querySelector('.inspector');
 if(evidenceOpen&&inspected===nextInspector?.dataset.inspectedKey)nextInspector.querySelector('details').open=true;
 if(focused){const next=document.getElementById(focused);if(next){next.focus();if(typeof position==='number'&&next.setSelectionRange)next.setSelectionRange(position,position);}}
}

function banner(){
 const el=document.querySelector('#job-banner'),job=state.status?.job;
 if(state.status?.busy){
  el.hidden=false;
  const modes={checkin:'正在签到',probe:'正在只读体检',recheck:'仅补查反馈',scan:'读取会话列表',login:'等待扫码登录'};
  el.innerHTML=`<div class="job-head"><span>${esc(modes[job?.mode]||'任务运行中')} · ${esc(job?.status==='queued'?'准备执行':job?.status==='cancelling'?'正在停止并释放浏览器':state.status.activity||'请稍候')} · ${job?.show_browser===false?'后台浏览器':'显示浏览器过程'}</span><div class="actions">${activeConversation()?button('follow-active','查看当前会话',{iconName:'search',kind:'small'}):''}${button('cancel','取消任务',{iconName:'stop',kind:'small',disabled:job?.status==='cancelling'})}</div></div><div class="job-accounts">${Object.entries(state.status.progress||{}).map(([name,item])=>`<span>${esc(name)} · ${esc(item.stage)} ${esc(item.name||'')}${esc(sendElapsed(item))}</span>`).join('')}</div>`;
 }else if(state.status?.cfg_error||job?.status==='failed'){
  el.hidden=false;el.textContent=state.status.cfg_error||job.error||'任务失败，请查看运行日志';
 }else if(job?.status==='completed'&&['checkin','recheck'].includes(job.mode)){
  const unresolved=Object.values(job.retry_map||{}).reduce((total,chats)=>total+chats.length,0);
  const drafts=flatten(state.status).filter(row=>row.draft_warning).length;
  el.hidden=false;
  el.innerHTML=`<div class="job-head"><span>${job.mode==='recheck'?'反馈补查':'签到任务'}已结束 · ${unresolved?`${unresolved} 个会话需要核对`:'所选范围已确认完成'}${drafts?` · ${drafts} 份原草稿恢复需处理`:''}</span>${unresolved||drafts?button('show-issues','查看待处理',{kind:'small',iconName:'search'}):''}</div>`;
  if(job.notification?.ok===false)el.innerHTML+=`<div class="notification-error">微信推送未完成：${esc(job.notification.error)} ${button('go-automation','检查通知设置',{kind:'small'})}</div>`;
 }else el.hidden=true;
}

async function refreshStatus(){
 try{
  const data=await api('/api/status');
  const content=s=>JSON.stringify({today:s?.today,accounts:s?.accounts,busy:s?.busy,automation:s?.automation,notification:s?.notification});
  const changed=content(data)!==content(state.status);
  const previous=state.status?.job;
  state.status=data;
  document.querySelector('#connection').textContent='服务已连接';
  document.querySelector('.connection-dot').classList.remove('offline');
  document.querySelector('#last-update').textContent='更新于 '+new Date().toLocaleTimeString('zh-CN',{hour12:false});
  if(previous&&previous.id===data.job?.id&&['queued','running','cancelling'].includes(previous.status)&&!data.busy){
   notify(data.job.status==='cancelled'?'任务已取消，已发出的消息请核对反馈':data.job.status==='failed'?data.job.error:'本轮执行结束，结果已更新',data.job.status==='failed');
   if(data.job.mode==='probe'&&data.probe.result)showProbe(data.probe.result);
  }
  banner();
  syncExecutionButtons();
  syncActivityRows();
  if(state.page==='automation')syncAutomationRuntime();
  if(changed&&state.page==='overview')render();
 }catch(error){document.querySelector('#connection').textContent='连接中断：'+error.message;document.querySelector('.connection-dot').classList.add('offline');}
}

async function loadConfig(force=false){
 if(state.draft&&!force)return;
 const {_revision,...cfg}=await api('/api/config');
 cfg.accounts??=[];
 state.cfg=cfg;state.draft=copy(cfg);state.revision=_revision;state.dirty=false;state.origins=new WeakMap();
 state.draft.accounts.forEach(a=>{a.groups??=[];state.origins.set(a,a.name);});
 state.accountIndex=Math.max(0,Math.min(state.accountIndex,state.draft.accounts.length-1));
 state.checkedGroups.clear();
}

function syncAutomationRuntime(){
 const runtime=state.status?.automation,delivery=state.status?.notification;
 const next=main.querySelector('.automation-next'),last=main.querySelector('.automation-last'),notification=main.querySelector('.notification-summary'),detail=main.querySelector('.notification-detail'),service=main.querySelector('.automation-service');
 if(next&&runtime)next.textContent=runtime.settings?.enabled?formatTime(runtime.next_run,runtime.settings.timezone)||'等待执行':'定时签到未启用';
 if(last&&runtime?.last_run?.status)last.textContent='上次：'+({queued:'已排队',running:'执行中',completed:'已结束',failed:'任务异常',cancelled:'已取消',interrupted:'进程中断，请核对已发送内容'}[runtime.last_run.status]||runtime.last_run.status)+' · '+formatTime(runtime.last_run.started,runtime.settings.timezone);
 if(service&&runtime)service.textContent=runtime.error||runtime.service_error||(runtime.active?'控制台服务正在管理计划':'定时服务未就绪，请检查服务进程');
 if(notification&&delivery?.time)notification.textContent=(delivery.ok?'Server酱已接受':'推送未完成')+' · '+formatTime(delivery.time,runtime?.settings?.timezone);
 if(detail&&delivery?.time)detail.textContent=delivery.error||delivery.message||'';
 main.querySelectorAll('[data-action="save-automation"]').forEach(el=>el.disabled=!!state.status?.busy);
}
async function loadAutomation(force=false){
 if(state.automationDraft&&!force)return;
 const data=await api('/api/automation');state.automation=data;
 state.automationDraft={schedule:copy(data.settings),notifications:{enabled:false,only_on_issues:false,...data.notifications}};
 state.automationScope=data.settings.accounts.length?'selected':'all';
 state.automationDirty=false;state.automationKey='';
}
function markAutomationDirty(){state.automationDirty=true;document.querySelector('#draft-indicator').hidden=false;main.querySelectorAll('.save-state').forEach(el=>el.textContent='有未保存修改');main.querySelector('[data-action="test-notification"]')?.setAttribute('disabled','');const reload=main.querySelector('[data-action="reload-automation"]');if(reload)reload.textContent='放弃修改并载入';}
async function saveAutomation(){
 if(state.automationScope==='selected'&&!state.automationDraft.schedule.accounts.length)throw new Error('请至少勾选一个定时账号，或明确选择全部账号');
 await api('/api/automation',{...state.automationDraft,revision:state.automation.revision,...(state.automationKey?{sendkey:state.automationKey}:{})});
 await loadAutomation(true);
 if(state.dirty){state.draft.schedule=copy(state.automationDraft.schedule);state.draft.notifications=copy(state.automationDraft.notifications);state.revision=state.automation.revision;}
 else await loadConfig(true);
 await refreshStatus();render();notify('定时与通知设置已保存');
}

async function saveConfig(){
 if(!state.draft)return;
 const renames={};
 for(const a of state.draft.accounts){const old=state.origins.get(a);if(old&&old!==a.name)renames[old]=a.name;}
 await api('/api/config',{cfg:state.draft,revision:state.revision,renames});
 await loadConfig(true);await refreshStatus();render();notify('配置已保存');
}

const historyQuery=()=>new URLSearchParams({...state.historyFilter,page:state.historyPage,limit:100});
async function loadHistory(){state.history=await api('/api/history?'+historyQuery());if(state.page==='history')render();}
async function loadStats(){state.stats=await api('/api/stats?days='+state.statsDays);if(state.page==='stats')render();}
async function loadLogs(){state.logs=(await api('/api/logs?'+new URLSearchParams({level:state.logLevel,q:state.logQuery}))).text;if(state.page==='logs')render();}
async function navigate(page){
 if(!pages[page])page='overview';
 state.page=page;location.hash=page;
 if(page==='accounts')await loadConfig();
 if(page==='automation')await loadAutomation();
 render();
 if(page==='history')await loadHistory();
 if(page==='stats')await loadStats();
 if(page==='logs')await loadLogs();
}

window.addEventListener('hashchange',()=>{const page=location.hash.slice(1);if(pages[page]&&page!==state.page)navigate(page).catch(error=>notify(error.message,true));});

function showModal(html,label='操作窗口'){modal.setAttribute('aria-label',label);modal.innerHTML=html;if(!modal.open)modal.showModal();}
function closeModal(){modal.close();state.editor=null;state.pendingPlan=null;state.pendingDelete=null;}
function selectedRow(key=state.selected){return flatten(state.status).find(row=>keyOf(row)===key);}
function openEditor(index){
 const account=state.draft.accounts[state.accountIndex];
 state.editor={index,group:copy(index<0?{chat:'',method:'text',message:'签到',reply_mode:state.draft.reply_mode||'reply_only'}:account.groups[index]),actions:[]};
 state.editor.actions=actionsOf(state.editor.group);
 drawEditor();
}
function drawEditor(){showModal(groupEditor(state.editor,state),'编辑签到规则');}

async function loadEditorTopics(refresh=false){
 const editor=state.editor;if(!editor)return;
 const account=state.draft.accounts[state.accountIndex],name=state.origins.get(account);
 const chat=String(editor.group.chat||'').trim().replace(/^#/,'');
 if(!name||name!==account.name)throw new Error('请先保存账号名称，再读取会话话题');
 if(!/^-?\d+$/.test(chat))throw new Error('请从会话列表选择数字 ID，再识别话题');
 const requestId=Symbol();editor.topicRequest=requestId;editor.topicsLoading=true;editor.topicsError='';editor.topics=null;
 drawEditor();
 try{
  const result=/^\d+$/.test(chat)?{is_forum:false,topics:[]}:await api('/api/topics?'+new URLSearchParams({account:name,chat,title:editor.group.title||'',folder:editor.group.folder||'',refresh:refresh?'1':'0'}));
  if(state.editor!==editor||editor.topicRequest!==requestId||String(editor.group.chat).trim().replace(/^#/,'')!==chat)return;
  editor.topics=result;editor.group.is_forum=result.is_forum;
  if(!result.is_forum){delete editor.group.topic;delete editor.group.topic_id;}
  else{
   const matches=result.topics.filter(topic=>topic.title===editor.group.topic&&(!editor.group.topic_id||topic.id===editor.group.topic_id));
   if(matches.length===1)editor.group.topic_id=matches[0].id;
   else {delete editor.group.topic;delete editor.group.topic_id;}
  }
 }catch(error){if(state.editor===editor&&editor.topicRequest===requestId)editor.topicsError='识别未完成：'+error.message+'。请重试后选择发送位置。';}
 finally{if(state.editor===editor&&editor.topicRequest===requestId){editor.topicsLoading=false;drawEditor();}}
}

async function saveEditor(){
 const {group:g,actions,index}=state.editor,account=state.draft.accounts[state.accountIndex];
 if(state.editor.topicsLoading)throw new Error('正在识别发送位置，请稍候');
 if(state.editor.topicsError)throw new Error('请先重试会话识别，确认发送位置后保存');
 if(g.is_forum&&(!g.topic||!g.topic_id))throw new Error('请先选择签到发送话题');
 g.chat=String(g.chat||'').trim().replace(/^#/,'');
 if(!/^-?\d+$|^@[A-Za-z0-9_]{3,}$/.test(g.chat))throw new Error('请输入有效的数字会话 ID 或 @用户名');
 if(account.groups.some((other,i)=>i!==index&&String(other.chat)===g.chat))throw new Error('这个会话已经存在，请在原会话中添加动作');
 const normalized=actions.map(a=>{
  const result={...a,method:a.method||'text'};
  if(['image_text','image_only'].includes(result.method)&&!String(result.image||'').trim())throw new Error('图片签到必须配置图片路径');
  if(result.method==='command_button'&&!(Array.isArray(result.click_button)?result.click_button.length:String(result.click_button||'').trim()))throw new Error('按钮签到必须配置按钮文字');
  if(['command','command_button'].includes(result.method)&&!lines(result.message).trim())throw new Error('请填写口令');
  if(!['image_text','image_only'].includes(result.method))delete result.image;
  if(result.method!=='command_button')delete result.click_button;
  if(result.method==='image_only')delete result.message;
  return result;
 });
 for(const key of ['method','message','image','click_button','actions'])delete g[key];
 if(normalized.length===1)Object.assign(g,normalized[0]);else g.actions=normalized;
 g.reply_mode??=state.draft.reply_mode||'reply_only';
 if(g.primary){if(!g.share_group)throw new Error('指定主群之前请填写共用系统名称');account.groups.forEach(other=>{if(other.share_group===g.share_group)delete other.primary;});}
 if(!g.share_group)delete g.primary;
 if(index<0)account.groups.push(g);else account.groups[index]=g;
 markDirty();closeModal();render();notify('规则已加入草稿，请保存配置后执行');
}

async function openChooser(refresh=false){
 const account=state.draft.accounts[state.accountIndex],original=state.origins.get(account);
 if(!original||original!==account.name)throw new Error('请先保存账号及名称修改，再读取会话列表');
 const cc=state.chooser;
 if(cc.loading)return;
 if(cc.loadedFolder!==cc.folder){cc.dialogs=[];cc.updated=null;cc.cached=false;cc.checked.clear();}
 cc.loading=true;cc.error='';drawChooser();if(!chooser.open)chooser.showModal();
 try{
  const data=await api('/api/dialogs?'+new URLSearchParams({account:original,folder:cc.folder,...(refresh?{refresh:'1'}:{})}));
  cc.dialogs=data.dialogs;cc.folders=data.folders||[];cc.updated=data.updated;cc.cached=data.cached;cc.loadedFolder=cc.folder;cc.checked.clear();
 }catch(error){cc.error=error.message;cc.cached=cc.dialogs.length>0;}
 finally{cc.loading=false;if(state.chooser===cc&&chooser.open)drawChooser();}
}
function drawChooser(){
 const cc=state.chooser,list=cc.dialogs.filter(d=>(d.title+' '+d.id).toLowerCase().includes(cc.query.toLowerCase()));
 const count=cc.loading?'正在连接 Telegram 并读取会话…':`${list.length} 个会话 · 已选 ${cc.checked.size} 个${cc.updated?' · '+(cc.cached?'缓存于 ':'更新于 ')+esc(cc.updated):''}`;
 const rows=list.map(d=>`<div class="chooser-row"><input type="checkbox" data-choose-check="${esc(d.id)}" aria-label="勾选 ${esc(d.title||d.id)}" ${cc.loading||d.is_forum?'disabled':''} ${d.is_forum?'title="论坛群请单独选择话题"':''} ${cc.checked.has(String(d.id))?'checked':''}><div class="identity">${esc(d.title||d.id)}<small>${esc(d.id)} · ${esc(d.type||'')}</small></div>${button('choose-one',d.is_forum?'选择话题':'选用',{kind:'small',disabled:cc.loading,extra:`data-id="${esc(d.id)}"`})}</div>`).join('');
 const emptyText=cc.loading?'读取中…':cc.error?'读取未完成，可刷新列表或返回填写会话 ID。':cc.query?'没有匹配的会话':'当前列表没有会话';
 chooser.innerHTML=`<div class="dialog-heading"><h2>从 Telegram 会话中选择</h2>${button('close-chooser','返回规则编辑',{kind:'dialog-close',iconName:'close'})}</div>
 <div class="toolbar"><div class="search-group"><select data-control="chooser-folder" aria-label="Telegram 文件夹" ${cc.loading?'disabled':''}>${option('','全部会话',cc.folder)}${cc.folders.map(f=>option(f,f,cc.folder)).join('')}</select><input id="chooser-search" data-control="chooser-search" aria-label="搜索会话" placeholder="搜索会话" value="${esc(cc.query)}"></div>${button('refresh-chooser','刷新列表',{iconName:'refresh',disabled:cc.loading})}</div>
 <p class="small-note" role="status">${cc.error&&!cc.dialogs.length?'会话列表读取失败':count}</p>${cc.error?`<div class="dialog-error" role="alert">${esc(cc.error)}</div>`:''}
 <div class="chooser-list" aria-busy="${cc.loading}">${rows||`<div class="empty-state">${emptyText}</div>`}</div>
 <p class="small-note">普通会话可批量添加；论坛群请单独选择话题。已有会话自动跳过。</p>
 <div class="dialog-actions">${button('close-chooser','返回')}${button('choose-batch',`添加选中的 ${cc.checked.size} 个会话`,{primary:true,disabled:!cc.checked.size||cc.loading})}</div>`;
}

function plan({row=null,account=null}={}){
 if(state.status?.busy)throw new Error('当前任务正在运行，请等待结束或取消任务');
 if(account!==null&&!(state.status?.accounts||[]).some(a=>a.name===account))throw new Error('请选择有效账号');
 const targets=row?[row]:flatten(state.status).filter(r=>(account===null||r.account===account)&&!r.done&&r.confirm!=='success');
 if(!targets.length){notify(account!==null?'此账号今天没有待签到会话；需要再次发送时请选择单群重签。':'今天没有待执行的会话；需要重签时请在会话详情中单独选择。');return;}
 state.pendingPlan=row?{account:row.account,chats:[row.chat]}:account!==null?{account,chats:targets.map(r=>r.chat)}:{};
 const byAccount=Map.groupBy?Map.groupBy(targets,r=>r.account):targets.reduce((map,r)=>(map.set(r.account,[...(map.get(r.account)||[]),r]),map),new Map());
 const visible=state.status?.browser?.visible_available!==false;
 showModal(`<h2>${row?.confirm?'核对后再次发送':row?'确认单群签到':account!==null?'确认单账号签到':'确认本轮签到'}</h2><p class="muted">本操作会按已保存的规则发送消息。共用系统签中一个后，其余成员共享结果。</p>${state.dirty?'<div class="dialog-error">你有未保存的配置，本轮将使用磁盘上已保存的规则。</div>':''}<div class="plan-list">${[...byAccount].map(([name,rows])=>`<p><strong>${esc(name)}</strong> · ${rows.length} 个目标<br>${rows.map(r=>esc(r.name)).join('、')}</p>`).join('')}</div>${targets.some(r=>r.confirm)?'<p class="small-note">部分目标已有执行记录。已发未确认或旧记录不完整时，再次发送可能重复签到，请先核对 Telegram。</p>':''}<label class="check-label"><input type="checkbox" id="show-run-browser" ${visible?'checked':'disabled'}>显示签到过程（打开 Telegram 浏览器窗口）</label>${visible?'':'<p class="small-note">当前服务器没有可视桌面，本轮使用后台浏览器。</p>'}<div class="dialog-actions">${button('close-modal','返回检查')}${button('confirm-run','确认发送',{primary:true,iconName:'play'})}</div>`,'确认签到发送');
}

async function startJob(mode,params={}){await api('/api/'+mode,params);await refreshStatus();notify(mode==='recheck'?'正在补查反馈，不会重新发送':mode==='probe'?'只读体检已开始（打开会话会标为已读）':'签到任务已提交');}

function showProbe(result){
 const summary=result.summary;
 showModal(`<h2>只读体检结果</h2><p class="muted">检查 ${summary.total} 个会话，可用 ${summary.ok}，不可用 ${summary.bad}；账号错误 ${summary.account_errors||0}。</p><div class="table-scroll"><table class="config-table"><thead><tr><th>账号 / 会话</th><th>结果</th><th>原因 / 耗时</th></tr></thead><tbody>${result.accounts.flatMap(a=>a.chats.length?a.chats.map(c=>`<tr><td>${esc(a.account)}<br>${esc(c.name)}</td><td>${c.ok?'可用':'不可用'}</td><td>${esc(c.error||'目标与输入框已核对')} · ${c.elapsed}s</td></tr>`):[`<tr><td>${esc(a.account)}</td><td>未完成</td><td>${esc(a.error)}</td></tr>`]).join('')}</tbody></table></div><div class="dialog-actions">${button('close-modal','关闭',{primary:true})}</div>`,'体检结果');
}

async function removeAccount(index){
 if(state.dirty)throw new Error('请先保存其他修改，再删除账号');
 const account=state.draft.accounts[index];state.pendingDelete={type:'account',index,name:account.name};
 showModal(`<h2>删除账号与本地登录会话？</h2><p class="muted">将删除「${esc(account.name)}」的配置、今日状态及本地登录会话。以后使用需要重新扫码。</p><div class="dialog-actions">${button('close-modal','保留账号')}${button('confirm-delete','删除账号与会话',{kind:'danger'})}</div>`,'删除账号');
}

async function onAction(el){
 if(el.dataset.action==='go-automation')return navigate('automation');
 if(el.dataset.action==='save-automation')return saveAutomation();
 if(el.dataset.action==='reload-automation'){await loadAutomation(true);render();return;}
 if(el.dataset.action==='test-notification'){
  state.testingNotification=true;el.disabled=true;
  try{const result=await api('/api/notifications/test',{});notify(result.message);}
  finally{state.testingNotification=false;await refreshStatus();if(state.page==='automation')render();}
  return;
 }
 const action=el.dataset.action,index=Number(el.dataset.index),account=state.draft?.accounts[state.accountIndex];
 if(action==='go-accounts')return navigate('accounts');
 if(action==='toggle-focus'){state.focus=!state.focus;render();}
 if(action==='toggle-catalog'){state.catalogOpen=!state.catalogOpen;render();}
 if(action==='clear-filter'&&['query','kindFilter','methodFilter','structureFilter','systemFilter'].includes(el.dataset.field)){state[el.dataset.field]='';render();}
 if(action==='reset-filters'){Object.assign(state,{query:'',filter:'all',accountFilter:'',kindFilter:'',methodFilter:'',structureFilter:'',systemFilter:''});render();}
 if(action==='reset-classification'){Object.assign(state,{kindFilter:'',methodFilter:'',structureFilter:'',systemFilter:''});render();}
 if(action==='show-issues'){Object.assign(state,{accountFilter:'',query:'',filter:'attention',kindFilter:'',methodFilter:'',structureFilter:'',systemFilter:''});await navigate('overview');}
 if(action==='follow-active'){
  const row=activeConversation();if(!row)return;
  Object.assign(state,{accountFilter:row.account,query:'',filter:'all',kindFilter:'',methodFilter:'',structureFilter:'',systemFilter:'',selected:keyOf(row)});
  await navigate('overview');
  const target=matchMedia('(max-width:1100px)').matches?main.querySelector('.inspector'):[...main.querySelectorAll('[data-row-key]')].find(el=>el.dataset.rowKey===keyOf(row));
  target?.scrollIntoView({behavior:'smooth',block:'center'});
 }
 if(action==='inspect'){state.selected=el.dataset.key;render();if(matchMedia('(max-width:1100px)').matches)main.querySelector('.inspector')?.scrollIntoView({behavior:'smooth',block:'start'});}
 if(action==='back-to-task'){const row=[...main.querySelectorAll('[data-row-key]')].find(el=>el.dataset.rowKey===state.selected);row?.scrollIntoView({behavior:'smooth',block:'center'});}
 if(action==='run')plan();
 if(action==='run-account-filter')plan({account:state.accountFilter});
 if(action==='run-account')plan({account:el.dataset.account||''});
 if(action==='retry'||action==='run-chat'){const row=selectedRow(el.dataset.key);if(!row)throw new Error('此会话尚未保存或已移除，请保存配置并刷新后再执行');plan({row});}
 if(action==='confirm-run'){if(!state.pendingPlan)throw new Error('执行预览已失效，请重新选择签到目标');const params={...state.pendingPlan,show_browser:document.querySelector('#show-run-browser').checked};closeModal();await startJob('checkin_now',params);}
 if(action==='cancel'){await api('/api/cancel',{});await refreshStatus();}
 if(action==='probe')await startJob('probe',state.accountFilter?{account:state.accountFilter}:{});
 if(action==='probe-chat'){const row=selectedRow(el.dataset.key);if(!row)throw new Error('目标会话已改变，请刷新后重试');await startJob('probe',{account:row.account,chats:[row.chat]});}
 if(action==='recheck'){const row=selectedRow(el.dataset.key);await startJob('recheck',{account:row.account,chats:[row.chat]});}
 if(action==='edit-selected'){
  const row=selectedRow(el.dataset.key);if(!row)throw new Error('目标会话已改变，请重新选择');await loadConfig();
  const mappedIndex=state.draft.accounts.findIndex(a=>state.origins.get(a)===row.account);
  const accountIndex=mappedIndex>=0?mappedIndex:state.draft.accounts.findIndex(a=>a.name===row.account);
  const groupIndex=accountIndex<0?-1:state.draft.accounts[accountIndex].groups.findIndex(g=>String(g.chat)===row.chat);
  if(groupIndex<0)throw new Error('当前配置草稿已修改或移除此会话，请先核对草稿');
  state.accountIndex=accountIndex;await navigate('accounts');openEditor(groupIndex);
 }
 if(action==='save-config')await saveConfig();
 if(action==='discard-config')showModal(`<h2>重新载入已保存的配置？</h2><p class="muted">当前尚未保存的草稿修改将被放弃。</p><div class="dialog-actions">${button('close-modal','保留草稿')}${button('confirm-discard','放弃草稿并载入')}</div>`,'重新载入配置');
 if(action==='confirm-discard'){await loadConfig(true);closeModal();render();notify('已重新载入保存的配置');}
 if(action==='add-account')showModal(`<form id="account-form"><h2>添加账号</h2><p class="muted">只需一个名称，不需要 API 凭据。保存后扫码登录。</p><label class="field">账号名称<input name="name" required placeholder="例如：备用账号"></label><div class="dialog-actions">${button('close-modal','取消')}<button type="submit" class="primary">添加到草稿</button></div></form>`,'添加账号');
 if(action==='manage-account'){state.accountIndex=index;state.checkedGroups.clear();render();}
 if(action==='login'){const a=state.draft.accounts[index];if(state.dirty)throw new Error('请先保存账号配置，再扫码登录');await api('/api/web_login',{account:a.name});await refreshStatus();notify('浏览器已启动，请用手机扫码，登录完成后自动关闭');}
 if(action==='delete-account')await removeAccount(index);
 if(action==='confirm-delete'){const result=await api('/api/delete_account',{account:state.pendingDelete.name,revision:state.revision});closeModal();await loadConfig(true);await refreshStatus();render();notify('账号已移除');for(const warning of result.warnings||[])notify(warning,true);}
 if(action==='add-group')openEditor(-1);
 if(action==='edit-group')openEditor(index);
 if(action==='delete-group'){account.groups.splice(index,1);state.checkedGroups.clear();markDirty();render();}
 if(action==='delete-groups'){account.groups=account.groups.filter((g,i)=>!state.checkedGroups.has(i));state.checkedGroups.clear();markDirty();render();}
 if(action==='assign-system'||action==='clear-system'){
  const system=action==='clear-system'?'':document.querySelector('#bulk-system').value.trim();if(action==='assign-system'&&!system)throw new Error('请输入共用系统名称');
  state.checkedGroups.forEach(i=>{account.groups[i].share_group=system;delete account.groups[i].primary;});markDirty();render();
 }
 if(action==='close-modal')closeModal();
 if(action==='open-chooser'){state.chooser={dialogs:[],folders:[],checked:new Set(),query:'',folder:'',loading:false,error:''};await openChooser();}
 if(action==='load-topics')await loadEditorTopics(true);
 if(action==='refresh-chooser')await openChooser(true);
 if(action==='close-chooser')chooser.close();
 if(action==='choose-one'){
  const dialog=state.chooser.dialogs.find(d=>String(d.id)===el.dataset.id),g=state.editor.group;
  if(String(g.chat)!==String(dialog.id)){delete g.topic;delete g.topic_id;delete g.is_forum;}
  g.chat=String(dialog.id);g.title=dialog.title;g.folder=state.chooser.folder||'';
  state.editor.topics=null;state.editor.topicsError='';
  chooser.close();
  if(!String(dialog.id).startsWith('-')){g.is_forum=false;delete g.topic;delete g.topic_id;state.editor.topics={is_forum:false,topics:[]};drawEditor();}
  else await loadEditorTopics();
 }
 if(action==='choose-batch'){
  const chooserState=state.chooser,editor=state.editor;
  const selected=state.chooser.dialogs.filter(d=>state.chooser.checked.has(String(d.id)));
  if(selected.some(d=>d.is_forum||String(d.id).startsWith('-')&&d.is_forum===undefined))throw new Error('论坛群需要逐个选择话题；请刷新列表后单独选用这些群。');
  for(const dialog of selected.filter(d=>String(d.id).startsWith('-'))){
   const info=await api('/api/topics?'+new URLSearchParams({account:state.origins.get(account),chat:String(dialog.id),title:dialog.title||'',folder:state.chooser.folder||''}));
   if(!chooser.open||state.chooser!==chooserState||state.editor!==editor)return;
   if(info.is_forum){dialog.is_forum=true;state.chooser.checked.delete(String(dialog.id));drawChooser();throw new Error('「'+dialog.title+'」是论坛群，请单独选择话题后添加。');}
  }
  const existing=new Set(account.groups.map(g=>String(g.chat)));let added=0;
  for(const id of state.chooser.checked){if(existing.has(id))continue;const dialog=state.chooser.dialogs.find(d=>String(d.id)===id);const g=copy(state.editor.group);g.chat=id;g.title=dialog.title;g.folder=state.chooser.folder||'';g.is_forum=false;g.actions=copy(state.editor.actions);for(const k of ['method','message','image','click_button','primary','topic','topic_id'])delete g[k];account.groups.push(g);existing.add(id);added++;}
  chooser.close();closeModal();markDirty();render();notify(`已加入 ${added} 个会话到草稿，请保存配置`);
 }
 if(action==='add-action'){state.editor.actions.push({method:'text',message:'签到'});drawEditor();}
 if(action==='remove-action'){state.editor.actions.splice(index,1);drawEditor();}
 if(action==='move-action-up'||action==='move-action-down'){const other=index+(action==='move-action-up'?-1:1);[state.editor.actions[index],state.editor.actions[other]]=[state.editor.actions[other],state.editor.actions[index]];drawEditor();}
 if(action==='inherit-keywords'){
  const g=state.editor.group,other=state.draft.accounts.flatMap(a=>a.groups).find(x=>String(x.chat)===String(g.chat)&&(x.success_keywords?.length||x.fail_keywords?.length));if(!other)throw new Error('其他账号还没有为此会话设置专属判定词');g.success_keywords=copy(other.success_keywords||[]);g.fail_keywords=copy(other.fail_keywords||[]);drawEditor();
 }
 if(action==='test-rule'){
  const group=state.editor?copy(state.editor.group):{};state.judgeGroup=group;
  // Keep the rule editor alive beneath its own independent chooser dialog.
  chooser.innerHTML=`<form id="judge-form"><div class="dialog-heading"><h2>判定词沙盒</h2>${button('close-chooser','返回规则编辑',{kind:'ghost'})}</div><p class="muted">粘贴机器人反馈，检查话术匹配。不会发送消息，也不代表消息已归属本账号。</p><label class="field">反馈原文<textarea name="text" rows="5" required></textarea></label><div id="judge-result" class="evidence-block" hidden></div><div class="dialog-actions"><button class="primary" type="submit">测试匹配</button></div></form>`;chooser.showModal();
 }
 if(action==='history-prev'||action==='history-next'){state.historyPage+=action==='history-prev'?-1:1;await loadHistory();}
 if(action==='history-reset'){state.historyFilter={q:'',account:'',status:'',from:'',to:''};state.historyPage=1;await loadHistory();}
 if(action==='export-history')await download('/api/history/download?'+historyQuery(),'checkin-history.csv');
 if(action==='download-logs')await download('/api/logs/download','checkin.log');
 if(action==='refresh-logs')await loadLogs();
 if(action==='export-config'){
  const url=URL.createObjectURL(new Blob([JSON.stringify(state.draft,null,2)],{type:'application/json'}));const link=document.createElement('a');link.href=url;link.download='checkin-config.json';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
 }
 if(action==='import-config')document.querySelector('#config-file').click();
}

document.addEventListener('click',event=>{
 const el=event.target.closest('button');if(!el||el.disabled)return;
 if(el.dataset.page){navigate(el.dataset.page).catch(e=>notify(e.message,true));return;}
 if(el.dataset.filter){state.filter=el.dataset.filter;render();return;}
 if(el.dataset.action)onAction(el).catch(error=>{const box=document.querySelector('#editor-error');if(modal.open&&box){box.textContent=error.message;box.hidden=false;}else notify(error.message,true);});
});

document.addEventListener('input',event=>{
 const el=event.target;
 if(el.dataset.schedule){const key=el.dataset.schedule;state.automationDraft.schedule[key]=key==='accounts'?[...el.selectedOptions].map(o=>o.value):el.type==='checkbox'?el.checked:el.value;markAutomationDirty();}
 if(el.dataset.scheduleScope){state.automationScope=el.dataset.scheduleScope;state.automationDraft.schedule.accounts=state.automationScope==='all'?[]:(state.automationDraft.schedule.accounts.length?state.automationDraft.schedule.accounts:(state.status?.accounts||[]).map(a=>a.name));markAutomationDirty();render();}
 if(el.dataset.scheduleAccount){const names=new Set(state.automationDraft.schedule.accounts);el.checked?names.add(el.dataset.scheduleAccount):names.delete(el.dataset.scheduleAccount);state.automationDraft.schedule.accounts=[...names];markAutomationDirty();}
 if(el.dataset.notification){state.automationDraft.notifications[el.dataset.notification]=el.checked;markAutomationDirty();}
 if(el.id==='serverchan-key'){state.automationKey=el.value.trim();markAutomationDirty();}
 if(el.dataset.control==='search'){state.query=el.value;render();}
 if(el.dataset.control==='chooser-search'){state.chooser.query=el.value;const pos=el.selectionStart;drawChooser();const next=document.querySelector('#chooser-search');next.focus();next.setSelectionRange(pos,pos);}
 if(el.dataset.accountName!==undefined){state.draft.accounts[Number(el.dataset.accountName)].name=el.value;markDirty();}
 if(el.dataset.delay){const [type,side]=el.dataset.delay.split(':'),key=type+'_delay_seconds';const values=range(state.draft[key],type==='account'?[0,0]:[1,3]);values[Number(side)]=Number(el.value);state.draft[key]=values;markDirty();}
 if(el.dataset.global){const key=el.dataset.global;state.draft[key]=el.type==='checkbox'?el.checked:key.endsWith('_keywords')?words(el.value):el.type==='number'?Number(el.value):el.value;markDirty();}
 if(el.dataset.editor){const key=el.dataset.editor;state.editor.group[key]=el.type==='checkbox'?el.checked:key.endsWith('_keywords')?words(el.value):el.value;if(key==='topic')delete state.editor.group.topic_id;if(key==='chat'){delete state.editor.group.topic;delete state.editor.group.topic_id;delete state.editor.group.is_forum;state.editor.topics=null;state.editor.topicsError='';state.editor.topicRequest=null;state.editor.topicsLoading=false;}}
 if(el.dataset.actionField){const key=el.dataset.actionField;state.editor.actions[Number(el.dataset.index)][key]=key==='image'?el.value:normalizeValue(el.value);}
});

document.addEventListener('change',event=>{
 const el=event.target;
 if(el.dataset.editor==='chat'&&/^-?\d+$/.test(el.value.trim().replace(/^#/,'')))loadEditorTopics().catch(e=>notify(e.message,true));
 if(el.hasAttribute('data-topic-choice')){const topic=state.editor.topics?.topics.find(item=>item.id===el.value);if(topic){state.editor.group.topic=topic.title;state.editor.group.topic_id=topic.id;}else if(el.value!=='__existing'){delete state.editor.group.topic;delete state.editor.group.topic_id;}drawEditor();document.querySelector('#editor-topic')?.focus();}
 if(['kindFilter','methodFilter','structureFilter','systemFilter'].includes(el.dataset.classification)){state[el.dataset.classification]=el.value;render();}
 if(el.dataset.control==='account-filter'){state.accountFilter=el.value;render();}
 if(el.dataset.control==='stats-days'){state.statsDays=Number(el.value);loadStats().catch(e=>notify(e.message,true));}
 if(el.dataset.control==='chooser-folder'){state.chooser.folder=el.value;openChooser().catch(e=>notify(e.message,true));}
 if(el.dataset.chooseCheck!==undefined){el.checked?state.chooser.checked.add(el.dataset.chooseCheck):state.chooser.checked.delete(el.dataset.chooseCheck);drawChooser();}
 if(el.dataset.groupCheck!==undefined){el.checked?state.checkedGroups.add(Number(el.dataset.groupCheck)):state.checkedGroups.delete(Number(el.dataset.groupCheck));render();}
 if(el.dataset.control==='select-all-groups'){state.checkedGroups=el.checked?new Set(state.draft.accounts[state.accountIndex].groups.map((_,i)=>i)):new Set();render();}
 if(el.dataset.systemPrimary!==undefined){state.draft.accounts[state.accountIndex].groups.filter(g=>g.share_group===el.dataset.systemPrimary).forEach(g=>{if(String(g.chat)===el.value)g.primary=true;else delete g.primary;});markDirty();}
 if(el.dataset.actionMethod!==undefined){state.editor.actions[Number(el.dataset.actionMethod)].method=el.value;drawEditor();}
 if(el.id==='theme'){localStorage.setItem('theme',el.value);applyTheme(el.value);}
 if(el.id==='config-file'&&el.files[0])el.files[0].text().then(text=>{const cfg=JSON.parse(text);if(!cfg||!Array.isArray(cfg.accounts))throw new Error('配置文件必须包含 accounts 数组');state.draft=cfg;state.origins=new WeakMap();cfg.accounts.forEach(a=>{a.groups??=[];if(state.cfg.accounts.some(old=>old.name===a.name))state.origins.set(a,a.name);});state.accountIndex=0;state.checkedGroups.clear();markDirty();render();notify('配置已导入草稿，保存前请核对账号与会话');}).catch(e=>notify(e.message,true));
});

document.addEventListener('submit',event=>{
 const form=event.target;event.preventDefault();
 (async()=>{
  if(form.id==='token-form'){sessionStorage.setItem('gui_token',document.querySelector('#token-input').value.trim());document.querySelector('#token-dialog').close();await loadConfig();await refreshStatus();render();}
  if(form.id==='account-form'){const name=new FormData(form).get('name').trim();if(state.draft.accounts.some(a=>a.name.toLowerCase()===name.toLowerCase()))throw new Error('已有同名账号');state.draft.accounts.push({name,groups:[]});state.accountIndex=state.draft.accounts.length-1;markDirty();closeModal();render();}
  if(form.id==='group-form')await saveEditor();
  if(form.id==='history-filters'){state.historyFilter=Object.fromEntries(new FormData(form));state.historyPage=1;await loadHistory();}
  if(form.id==='log-filters'){const data=new FormData(form);state.logLevel=data.get('level');state.logQuery=data.get('q');await loadLogs();}
  if(form.id==='judge-form'){const result=await api('/api/judge',{text:new FormData(form).get('text'),group:state.judgeGroup,settings:{success_keywords:state.draft.success_keywords,fail_keywords:state.draft.fail_keywords}});const box=document.querySelector('#judge-result');box.hidden=false;box.textContent=({success:'匹配成功话术',failed:'匹配失败话术',unconfirmed:'没有匹配到判定词'})[result.confirm]+' · 命中：'+Object.values(result.hits).flat().join('、');}
 })().catch(error=>{const box=document.querySelector('#editor-error');if(form.id==='group-form'&&box){box.hidden=false;box.textContent=error.message;}else notify(error.message,true);});
});

modal.addEventListener('cancel',event=>{if(state.editor){event.preventDefault();notify('请使用保存到草稿或取消按钮关闭，避免丢失规则修改');}});
main.addEventListener('toggle',event=>{if(event.target.matches('.global-settings'))state.globalOpen=event.target.open;},true);
window.addEventListener('beforeunload',event=>{if(state.dirty||state.automationDirty){event.preventDefault();event.returnValue='';}});
function applyTheme(theme){if(theme==='system')delete document.documentElement.dataset.theme;else document.documentElement.dataset.theme=theme;}
const initialTheme=['system','light','dark'].includes(localStorage.getItem('theme'))?localStorage.getItem('theme'):'system';
document.querySelector('#theme').value=initialTheme;applyTheme(initialTheme);
state.page=pages[location.hash.slice(1)]?location.hash.slice(1):'overview';
await Promise.allSettled([loadConfig(),refreshStatus()]);render();
if(state.page==='history')loadHistory().catch(e=>notify(e.message,true));
if(state.page==='stats')loadStats().catch(e=>notify(e.message,true));
if(state.page==='logs')loadLogs().catch(e=>notify(e.message,true));
if(state.page==='automation')loadAutomation().then(render).catch(e=>notify(e.message,true));
async function poll(){await refreshStatus();setTimeout(poll,state.status?.busy?1000:4000);}setTimeout(poll,2000);
