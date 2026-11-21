import {esc,icon,button,field,option,empty,keyOf,badge,statusInfo,sendLabel,sendUncertain,methodLabel,methods,sourceLabels,stageLabels,range,lines,actionsOf,conversationLabels,conversationKind} from './core.js';

const heading=(title,subtitle,actions='')=>`<div class="page-heading"><div><h1>${title}</h1><p class="subtitle">${subtitle}</p></div><div class="actions">${actions}</div></div>`;
const input=(value,attrs='')=>`<input value="${esc(value)}" ${attrs}>`;
const area=(value,attrs='')=>`<textarea rows="3" ${attrs}>${esc(lines(value))}</textarea>`;

export function inspector(row,busy){
 const s=statusInfo(row),sent=row.sent===true?(sendUncertain(row)?'可能已发出，需核对':'已发出'):row.sent===false?'0 次 · 未发送':row.confirm?'旧记录 · 发送状态未留存':'尚未发送';
 const error=row.error||(row.confirm==='failed'?'旧版记录未保存准确错误原因。请先在 Telegram 核对，避免重复发送。':'');
 const hint=row.feedback_hint?`<div class="evidence-block draft-warning feedback-hint" role="note"><div class="eyebrow">${row.feedback_hint.code==='rule_mismatch'?'判定词需要核对':row.feedback_hint.code==='buttons_pending'?'按钮尚未完成':'发送证据待核对'}</div><p>${esc(row.feedback_hint.message)}</p>${row.feedback_hint.code==='rule_mismatch'?button('edit-selected','检查判定词',{kind:'small',disabled:busy,extra:`data-key="${esc(keyOf(row))}"`}):''}</div>`:'';
 return `<aside class="inspector" data-inspected-key="${esc(keyOf(row))}" aria-label="会话详情">${button('back-to-task','返回会话列表',{kind:'small mobile-back'})}<div class="eyebrow">会话详情 / ${esc(row.account)}</div><h2>${esc(row.name)}</h2>${badge(row)}${hint}<dl class="detail-fields"><dt>会话类型</dt><dd>${esc(conversationLabels[conversationKind(row)])}</dd><dt>签到方式</dt><dd>${esc(methodLabel(row))}</dd><dt>本次发送</dt><dd>${sent}</dd><dt>反馈来源</dt><dd>${esc(sourceLabels[row.source]||'旧记录 · 归属未留存')}</dd>${row.shared_from?`<dt>实际签到会话</dt><dd>${esc(row.shared_from.name)}</dd>`:''}${row.expected_peer?`<dt>配置目标</dt><dd>${esc(row.expected_peer)}</dd>`:''}${row.actual_peer?`<dt>当前会话</dt><dd>${esc(row.actual_peer)}</dd>`:''}${row.stage?`<dt>执行阶段</dt><dd>${esc(stageLabels[row.stage]||row.stage)}</dd>`:''}</dl>${row.draft_warning?`<div class="evidence-block draft-warning"><div class="eyebrow">原草稿恢复需处理</div><pre>${esc(row.draft_warning)}</pre></div>`:''}${error?`<div class="evidence-block error-block"><div class="eyebrow">执行错误</div><pre>${esc(error)}</pre></div>`:''}<div class="evidence-block"><div class="eyebrow">${row.confirm==='success'?'判定反馈':'反馈记录'}</div><pre>${esc(row.replies?.length?row.replies.join('\n\n'):row.confirm==='failed'?'错误详情优先展示在上方。':row.detail||'尚无反馈。')}</pre></div><div class="candidate-feedback">${row.confirm==='unconfirmed'&&row.candidate_replies?.length?`<div class="evidence-block draft-warning"><div class="eyebrow">候选签到反馈 · 归属待核对</div><pre>${esc(row.candidate_replies.map(item=>item.t).join('\n\n'))}</pre><p class="small-note">已捕获这些话术，但机器人未关联本次消息，暂不自动确认。</p></div>`:''}</div><div class="actions">${row.confirm==='unconfirmed'?button('recheck','仅补查反馈',{iconName:'search',disabled:busy||!row.can_recheck,extra:`data-key="${esc(keyOf(row))}"`}):''}${button('probe-chat','只检查此群',{iconName:'shield',disabled:busy,extra:`data-key="${esc(keyOf(row))}"`})}${button('edit-selected','编辑规则',{iconName:'settings',extra:`data-key="${esc(keyOf(row))}"`})}${button('run-chat',sendLabel(row),{disabled:busy,extra:`data-key="${esc(keyOf(row))}"`})}</div>${row.confirm==='unconfirmed'&&!row.can_recheck?'<p class="footnote">本次未留存发送消息 ID，无法安全补查；请先在 Telegram 核对。</p>':''}<details><summary>动作与消息证据</summary>${(row.actions||[]).map((action,index)=>`<div class="action-evidence">动作 ${index+1} · ${esc(action.confirm||'未确认')}<p class="muted">发送：${action.sent?'已发出':'未发出'}；按钮：${action.clicked?'已点击':'未点击'}</p>${action.error?`<pre>${esc(action.error)}</pre>`:''}${action.warning?`<pre>${esc(action.warning)}</pre>`:''}<pre>消息 ID：${esc((action.sent_mids||[]).join(', ')||'未记录')}</pre></div>`).join('')||'<p class="small-note">此条结果没有动作证据。</p>'}${(row.evidence||[]).map(e=>`<div class="action-evidence"><div class="eyebrow">消息 ${esc(e.mid)} · ${esc(sourceLabels[e.source]||'非关联消息')} · 回复 ${esc(e.rm||'无')}</div><pre>${esc(e.t)}</pre></div>`).join('')}</details><p class="footnote section-gap">补查只读取反馈；重签会再次发送消息。</p></aside>`;
}

export function accountsView(state){
 if(!state.draft)return empty('正在读取配置…');
 const cfg=state.draft,accounts=cfg.accounts||[],account=accounts[state.accountIndex];
 let html=heading('账号与签到规则','配置作为草稿编辑，保存后生效。话题、文件夹与多步动作完整保留。',button('import-config','导入')+button('export-config','导出',{iconName:'download'})+button('discard-config','重新载入')+button('add-account','添加账号',{iconName:'plus'})+button('save-config','保存配置',{primary:true,disabled:state.status?.busy}));
 const cards=accounts.map((a,index)=>{
  const live=state.status?.accounts?.find(x=>x.name===state.origins.get(a));
  const selected=index===state.accountIndex;
  return `<article class="account-card ${selected?'active':''}">
   <h3>${esc(a.name)}</h3><p>${esc(live?.status||'尚未保存或登录')} · ${a.groups?.length||0} 个会话</p>
   <div class="actions">
    ${button('run-account','账号签到',{kind:'small',iconName:'play',disabled:state.status?.busy||state.dirty||!live,extra:`data-account="${esc(state.origins.get(a)||'')}" aria-label="账号签到：${esc(a.name)}"`})}
    ${button('manage-account','管理会话',{kind:'small',extra:`id="manage-account-${index}" data-index="${index}" aria-label="管理会话：${esc(a.name)}" aria-pressed="${selected}"`})}
    ${button('login','扫码登录',{kind:'small',disabled:state.status?.busy,extra:`data-index="${index}"`})}
   </div>
  </article>`;
 });
 html+=`<div class="account-list">${cards.join('')||empty('添加第一个账号','只需起一个名称，随后扫码登录。')}</div>`;
 if(account){
  const groups=account.groups||[],systems=[...new Set(groups.map(g=>g.share_group).filter(Boolean))];
  html+=`<section class="panel"><div class="panel-heading"><div>${field('账号名称',input(account.name,`data-account-name="${state.accountIndex}" aria-label="账号名称"`))}</div><div class="actions">${button('delete-account','删除账号与会话',{kind:'danger small',disabled:state.status?.busy,extra:`data-index="${state.accountIndex}"`})}${button('add-group','添加群',{iconName:'plus'})}</div></div>${systems.length?`<div class="system-strip">${systems.map(system=>`<div class="system-item"><span class="tag">${esc(system)}</span> · ${groups.filter(g=>g.share_group===system).length} 个成员<select data-system-primary="${esc(system)}" aria-label="${esc(system)} 主签到群">${option('','主群：按配置顺序','')}${groups.filter(g=>g.share_group===system).map(g=>option(g.chat,g.title||g.chat,groups.find(x=>x.share_group===system&&x.primary)?.chat||'')).join('')}</select></div>`).join('')}</div>`:''}<div class="bulk-bar" ${state.checkedGroups.size?'':'hidden'}><span>已选 ${state.checkedGroups.size} 个会话</span><input id="bulk-system" aria-label="签到系统名称" placeholder="输入系统名称">${button('assign-system','加入系统',{kind:'small'})}${button('clear-system','移出系统',{kind:'small'})}${button('delete-groups','删除选中',{kind:'danger small'})}</div><div class="table-scroll"><table class="config-table"><thead><tr><th><input type="checkbox" data-control="select-all-groups" aria-label="选择全部群" ${groups.length&&state.checkedGroups.size===groups.length?'checked':''}></th><th>目标会话</th><th>签到方式</th><th>规则 / 系统</th><th>操作</th></tr></thead><tbody>${groups.map((g,index)=>`<tr><td><input type="checkbox" data-group-check="${index}" aria-label="选择 ${esc(g.title||g.chat)}" ${state.checkedGroups.has(index)?'checked':''}></td><td>${esc(g.title||g.chat)}<div class="task-meta">${esc(g.chat)}${g.topic?` · ${esc(g.topic)}`:''}</div></td><td>${esc(actionsOf(g).map(a=>methods[a.method]).join(' → '))}</td><td><span class="muted">${(g.success_keywords?.length||0)+(g.fail_keywords?.length||0)?'专属判定词':'全局判定词'}</span>${g.share_group?`<div class="task-meta"><span class="tag">${esc(g.share_group)}</span>${g.primary?' · 主群':''}</div>`:''}</td><td><div class="actions">${button('run-chat','单群签到',{kind:'small',disabled:state.status?.busy||state.dirty,extra:`data-key="${esc(JSON.stringify([state.origins.get(account),String(g.chat)]))}" aria-label="单群签到：${esc(account.name)} / ${esc(g.title||g.chat)}"`})}${button('edit-group','编辑',{kind:'small',extra:`data-index="${index}"`})}${button('delete-group','删除',{kind:'small danger',extra:`data-index="${index}"`})}</div></td></tr>`).join('')||'<tr><td colspan="5">'+empty('这个账号还没有配置会话','添加群后可从 Telegram 会话列表选择，或批量导入文件夹。')+'</td></tr>'}</tbody></table></div></section>`;
 }
 const gDelay=range(cfg.group_delay_seconds),aDelay=range(cfg.account_delay_seconds,[0,0]);
 html+=`<details class="panel global-settings" ${state.globalOpen?'open':''}><summary>全局设置</summary><div class="fields"><div class="field"><span>群间隔 · 最小 / 最大（秒）</span><div class="actions">${input(gDelay[0],'type="number" min="0" max="3600" data-delay="group:0" aria-label="群间隔最小值"')}${input(gDelay[1],'type="number" min="0" max="3600" data-delay="group:1" aria-label="群间隔最大值"')}</div><small>同一账号发送的最小间隔；导航和等待反馈会抵扣此时间</small></div><div class="field"><span>账号间隔 · 最小 / 最大（秒）</span><div class="actions">${input(aDelay[0],'type="number" min="0" max="3600" data-delay="account:0" aria-label="账号间隔最小值"')}${input(aDelay[1],'type="number" min="0" max="3600" data-delay="account:1" aria-label="账号间隔最大值"')}</div></div>${field('反馈窗口上限（秒）',input(cfg.reply_wait_seconds??6,'type="number" min="0" max="3600" data-global="reply_wait_seconds"'))}${field('网页版代理',input(cfg.web_proxy||'','data-global="web_proxy" placeholder="留空自动探测"'))}${field('全局成功词 · 一行一条',area(cfg.success_keywords||['签到成功','打卡成功','已经签到过了'],'data-global="success_keywords"'))}${field('全局失败词 · 一行一条',area(cfg.fail_keywords||['签到失败','打卡失败','请先加入'],'data-global="fail_keywords"'))}<label class="check-label"><input type="checkbox" data-global="reply_second_pass" ${cfg.reply_second_pass?'checked':''}>本轮结束后补查未确认项（不重发）</label><label class="check-label"><input type="checkbox" data-global="web_headless" ${cfg.web_headless?'checked':''}>命令行默认后台运行（手动任务在执行预览中选择）</label></div><p class="small-note">群聊只用明确关联本轮发送的反馈确认结果；私聊可使用本次发送后的对方反馈。</p></details><div class="actions">${button('save-config','保存所有配置',{primary:true,disabled:state.status?.busy})}<span class="save-state">${state.dirty?'有未保存修改':'配置已载入'}</span></div><input type="file" id="config-file" class="file-import" accept="application/json,.json">`;
 return html;
}

export function groupEditor(editor,state){
 const g=editor.group,actions=editor.actions;
 g.reply_mode??=state.draft.reply_mode||'reply_only';
 return `<form id="group-form"><div class="dialog-heading"><h2>${editor.index<0?'添加会话':'编辑签到规则'}</h2>${button('close-modal','关闭',{kind:'dialog-close',iconName:'close'})}</div><div id="editor-error" class="dialog-error" hidden role="alert"></div><div class="fields"><div class="field span-full"><span>目标会话 ID 或 @用户名</span><div class="actions">${input(g.chat||'','data-editor="chat" aria-label="目标会话 ID 或 @用户名" required style="flex:1"')}${button('open-chooser','从会话列表选择')}</div></div>${field('显示名称',input(g.title||'','data-editor="title"'))}${field('所属共用系统',input(g.share_group||'','data-editor="share_group" placeholder="可选，同系统签中一个即完成"'))}${topicEditor(editor)}${field('Telegram 文件夹',input(g.folder||'','data-editor="folder" placeholder="可选，用于快速定位"'))}${field('反馈采集范围',`<select data-editor="reply_mode">${option('reply_only','仅关联本轮消息的回复',g.reply_mode||state.draft.reply_mode||'reply_only')}${option('after','收集群内新消息，关联回复才算确认',g.reply_mode)}</select>`)}<label class="check-label"><input type="checkbox" data-editor="primary" ${g.primary?'checked':''}>作为共用系统的主签到群</label></div><h3 class="section-gap">按顺序执行动作</h3>${actions.map((action,index)=>`<section class="action-editor"><div class="action-editor-head"><span class="muted">动作 ${index+1}</span><select data-action-method="${index}" aria-label="动作 ${index+1} 签到方式">${Object.entries(methods).map(([key,label])=>option(key,label,action.method)).join('')}</select><div class="actions">${button('move-action-up','上移',{kind:'small',disabled:index===0,extra:`data-index="${index}"`})}${button('move-action-down','下移',{kind:'small',disabled:index===actions.length-1,extra:`data-index="${index}"`})}${button('remove-action','移除',{kind:'small danger',disabled:actions.length===1,extra:`data-index="${index}"`})}</div></div><div class="fields">${!['image_only','button_only'].includes(action.method)?field('文字 / 口令 · 多行表示随机选一条',area(action.message??'签到',`data-action-field="message" data-index="${index}"`)):''}${action.method==='command_button'?field('按钮文字 · 一行一级',area(action.click_button,`data-action-field="click_button" data-index="${index}"`)):''}${action.method==='button_only'?field('按钮完整文字',input(action.click_button||'',`data-action-field="click_button" data-index="${index}" placeholder="点我签到"`))+field('消息链接或 ID',input(action.button_message||'',`data-action-field="button_message" data-index="${index}"`),'私聊可留空，选当天最新的匹配按钮。群聊须指定消息。'):''}${['image_text','image_only'].includes(action.method)?field('图片路径',input(action.image||'',`data-action-field="image" data-index="${index}" placeholder="images/checkin.jpg"`)):''}</div></section>`).join('')}<div class="actions section-gap">${button('add-action','添加动作',{iconName:'plus'})}</div><div class="fields section-gap">${field('本群成功词 · 一行一条',area(g.success_keywords||[],'data-editor="success_keywords"'),'支持 X / {n} 数字通配，留空继承全局')}${field('本群失败词 · 一行一条',area(g.fail_keywords||[],'data-editor="fail_keywords"'),'整条保存，句子里的逗号不会拆开')}</div><div class="actions section-gap">${button('test-rule','测试判定词')}${button('inherit-keywords','从其他账号继承同会话规则')}</div><div class="dialog-actions">${button('close-modal','取消')}<button class="primary" type="submit">保存到草稿</button></div></form>`;
}

function topicEditor(editor){
 const g=editor.group,topics=editor.topics;
 if(/^#?[1-9]\d*$/.test(String(g.chat||'').trim()))return '<div class="field topic-field"><span>发送位置</span><span class="topic-normal" role="status">机器人或私聊 · 无话题</span><small>签到动作在当前会话执行。</small></div>';
 const refresh=button('load-topics',editor.topicsLoading?'正在识别…':'识别 / 刷新话题',{kind:'small',disabled:editor.topicsLoading||!g.chat,extra:'id="load-topics"'});
 if(editor.topicsLoading)return `<div class="field topic-field"><span>发送位置</span><div class="topic-loading" role="status">正在识别会话并读取话题…</div>${refresh}</div>`;
 if(g.is_forum||topics?.is_forum){
  const items=topics?.topics||[];
  const exists=items.some(item=>item.id===g.topic_id&&item.title===g.topic);
  const choices=option('','请选择发送话题',g.topic_id||'')+(!exists&&g.topic?option('__existing',g.topic+'（原配置，待刷新核对）','__existing'):'')+items.map(item=>option(item.id,item.title+(items.filter(other=>other.title===item.title).length>1?' · #'+item.id:'')+(item.closed?' · 已关闭':''),exists?g.topic_id:'')).join('');
  return `<div class="field topic-field"><span>发送到论坛话题 <span class="required-mark">必选</span></span><select id="editor-topic" data-topic-choice aria-label="签到发送话题">${choices}</select>${refresh}${topics?.is_forum?`<small role="status">已读取 ${items.length} 个话题</small>`:''}<small>${esc(g.title||g.chat)}${g.topic?' → '+esc(g.topic):'：请先选择具体话题'}</small>${editor.topicsError?`<small role="alert" class="topic-error">${esc(editor.topicsError)}</small>`:''}</div>`;
 }
 return `<div class="field topic-field"><span>发送位置</span>${topics&&!topics.is_forum?'<span class="topic-normal" role="status">已识别为普通会话 · 无话题</span>':input(g.topic||'','data-editor="topic" aria-label="论坛话题" placeholder="可选，可先识别话题"')}${refresh}${editor.topicsError?`<small role="alert" class="topic-error">${esc(editor.topicsError)}</small>`:'<small>论坛群需选择话题；普通群和私聊直接发送。</small>'}</div>`;
}

export function historyView(state){
 const data=state.history,f=state.historyFilter;
 const states=[['成功','success'],['未确认','unconfirmed'],['失败','failed']];
 const days=new Map();
 for(const row of data?.rows||[]){
  const day=/^\d{4}-\d{2}-\d{2}/.exec(row.time)?.[0]||'日期未记录';
  if(!days.has(day))days.set(day,[]);
  days.get(day).push(row);
 }
 const entries=[...days].map(([day,rows])=>`<section class="history-day">
  <h3>${esc(day)} <span>本页 ${rows.length} 条</span></h3>
  ${rows.map(row=>{
   const detail=String(row.detail||'');
   const result=states.find(([label])=>label===row.status)?.[1];
   const cells=`<time class="history-time" datetime="${esc(String(row.time).replace(' ','T'))}">${esc(day==='日期未记录'?row.time:row.time.slice(11)||'—')}</time>
    <span class="history-account" title="${esc(row.account)}">${esc(row.account)}</span>
    <span class="history-group" title="${esc(row.group)}">${esc(row.group)}</span>
    <span class="history-status">${result?badge({confirm:result}):esc(row.status||'未记录')}</span>
    <span class="history-preview">${esc(detail.trim().replace(/\s+/g,' ')||'无反馈详情')}</span>`;
   return detail?`<details class="history-entry"><summary class="history-grid">${cells}<span class="history-expand" title="展开反馈">${icon('arrow')}</span></summary><pre class="history-detail">${esc(detail)}</pre></details>`:`<div class="history-entry history-grid">${cells}</div>`;
  }).join('')}
 </section>`).join('');
 const totals=data?`<div class="history-totals" aria-label="筛选结果统计"><span>记录 <strong>${data.total}</strong></span>${data.counts?states.map(([label,result])=>`<span>${badge({confirm:result})}<strong>${data.counts[label]||0}</strong></span>`).join(''):''}</div>`:'';
 const lastPage=data?Math.max(1,Math.ceil(data.total/data.limit)):1;
 return heading('执行记录','',button('export-history','导出筛选结果',{iconName:'download',disabled:!data?.total}))+
 `<section class="history-filters-panel"><form id="history-filters" class="filter-form">
  ${field('关键词',input(f.q,'name="q" placeholder="群名、账号或反馈"'))}
  ${field('账号',`<select name="account">${option('','全部账号',f.account)}${(state.status?.accounts||[]).map(a=>option(a.name,a.name,f.account)).join('')}</select>`)}
  ${field('状态',`<select name="status">${option('','全部状态',f.status)}${states.map(([label])=>option(label,label,f.status)).join('')}</select>`)}
  ${field('开始日期',input(f.from,'type="date" name="from"'))}${field('结束日期',input(f.to,'type="date" name="to"'))}
  <div class="actions"><button class="primary" type="submit">筛选</button>${Object.values(f).some(Boolean)?button('history-reset','清空筛选'):''}</div>
 </form>${totals}</section>
 <section class="task-surface history-surface" aria-label="执行记录列表">
  ${entries?`<div class="history-grid history-column-head" aria-hidden="true"><span>时间</span><span>账号</span><span>会话</span><span>结果</span><span>反馈摘要</span></div>${entries}`:empty(data?'没有匹配的执行记录':'正在读取记录…',data?'可清空筛选或调整日期范围。':'')}
 </section>
 ${data?.total?`<div class="pagination history-pagination"><span>第 ${state.historyPage} / ${lastPage} 页</span><div class="actions">${button('history-prev','上一页',{disabled:state.historyPage<=1})}${button('history-next','下一页',{disabled:state.historyPage>=lastPage})}</div></div>`:''}`;
}

export function statsView(state){
 const data=state.stats,t=data?.total||{},sum=(t['成功']||0)+(t['未确认']||0)+(t['失败']||0);
 const rows=items=>items.map(item=>{const ok=item['成功']||0,unc=item['未确认']||0,bad=item['失败']||0,n=ok+unc+bad;return `<div class="stats-row"><span>${esc(item.date||item.account)}</span><div class="stats-track" role="img" aria-label="成功 ${ok}，未确认 ${unc}，失败 ${bad}">${[[ok,'success'],[unc,'unconfirmed'],[bad,'failed']].map(([val,cls])=>`<span class="${cls}" style="width:${n?100*val/n:0}%"></span>`).join('')}</div><span class="stats-numbers">${ok} / ${unc} / ${bad}</span></div>`;}).join('');
 return heading('签到统计','按执行记录统计，包含手动重签次数。')+`<div class="toolbar"><label class="field">时间范围<select data-control="stats-days">${[7,30,90].map(n=>option(n,`近 ${n} 天`,state.statsDays)).join('')}</select></label><span class="muted">共 ${sum} 次记录 · 成功 ${sum?Math.round(100*(t['成功']||0)/sum):0}%</span></div><section class="panel"><div class="stats-legend"><span>成功 ${t['成功']||0}</span><span>未确认 ${t['未确认']||0}</span><span>失败 ${t['失败']||0}</span></div>${rows(data?.per_day||[])||empty('所选时间范围内没有记录')}<h3 class="section-gap">按账号</h3>${rows(data?.per_acc||[])}</section>`;
}

export function logsView(state){return heading('运行日志','保留最近 500 条匹配日志，完整文件可下载。',button('download-logs','下载完整日志',{iconName:'download'})+button('refresh-logs','刷新',{iconName:'refresh'}))+`<form id="log-filters" class="filter-form">${field('级别',`<select name="level">${option('','全部级别',state.logLevel)}${['INFO','WARNING','ERROR','CRITICAL'].map(s=>option(s,s,state.logLevel)).join('')}</select>`)}${field('关键词',input(state.logQuery,'name="q" placeholder="按账号、会话或错误搜索"'))}<button type="submit">筛选</button></form><pre class="log-output">${esc(state.logs??'正在读取日志…')}</pre>`;}
