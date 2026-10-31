/* Saved secrets stay on the server; the page never receives the saved API key. */
const $ = id => document.getElementById(id);
const state = {tab:'materials', selected:new Set(), materials:[], drafts:[], tasks:[], status:null, editing:null, dirty:false};
let publishingSaving=false, publishingRevision=0;
const statusNames = {waiting:'等待中',running:'运行中',attention:'待人工处理',success:'成功',partial:'部分失败',failed:'失败',stopped:'已停止'};
const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
let toastTimer;

function syncChoices(select){
  const group=$(select.id+'-choices');if(!group)return;
  group.replaceChildren();
  for(const option of select.options){
    const button=document.createElement('button');
    button.type='button';button.className='choice-button';
    button.dataset.value=option.value;button.textContent=option.textContent;
    button.disabled=select.disabled||option.disabled;
    button.setAttribute('aria-pressed',String(option.selected));
    button.addEventListener('click',()=>{
      if(select.multiple){
        if(select.id==='domain'&&option.value===''){
          for(const item of select.options)item.selected=item.value==='';
        }else{
          option.selected=!option.selected;
          if(select.id==='domain'){
            const any=[...select.options].some(item=>item.value!==''&&item.selected);
            select.querySelector('option[value=""]').selected=!any;
          }
        }
      }else select.value=option.value;
      select.dispatchEvent(new Event('change',{bubbles:true}));
      group.querySelector(`button[data-value="${CSS.escape(option.value)}"]`)?.focus({preventScroll:true});
    });
    group.append(button);
  }
}

function initChoices(){
  for(const select of document.querySelectorAll('.settings-column select')){
    const row=document.createElement('div');row.className='option-row';
    const label=document.querySelector(`label[for="${select.id}"]`);
    select.parentNode.insertBefore(row,label||select);
    const caption=label||document.createElement('span');
    if(!label){caption.className='field-label';caption.textContent=select.getAttribute('aria-label')||'选项';}
    caption.id=select.id+'-label';caption.removeAttribute('for');row.append(caption);
    const group=document.createElement('div');group.className='choice-group';group.id=select.id+'-choices';
    group.setAttribute('role','group');group.setAttribute('aria-labelledby',caption.id);
    row.append(group,select);select.hidden=true;select.setAttribute('aria-hidden','true');
    select.addEventListener('change',()=>syncChoices(select));syncChoices(select);
  }
}

function toast(message, error=false) {
  $('toast').textContent = message;
  $('toast').classList.toggle('error',error);
  $('toast').hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {$('toast').hidden=true;},6500);
}

async function api(path, method='GET', body) {
  const options = {method,headers:{}};
  if (method!=='GET') {
    options.headers['X-Workbench-Request']='1';
    options.headers['Content-Type']='application/json';
    if (body!==undefined) options.body=JSON.stringify(body);
  }
  let response;
  try { response=await fetch(path,options); }
  catch { throw new Error('无法连接本地工作台，请确认程序仍在运行。'); }
  const result=await response.json();
  if (!response.ok) {
    let detail=result.detail;
    if (Array.isArray(detail)) detail=detail.map(item=>item.msg).join('；');
    throw new Error(detail || '请求失败，请稍后重试');
  }
  return result;
}

function act(id, fn) {
  $(id).addEventListener('click',async event=>{
    const button=event.currentTarget;
    button.disabled=true;
    try { await fn(event); } catch(error) { toast(error.message,true); }
    finally { button.disabled=false; }
  });
}

function generation() {
  const styles=[...$('style').selectedOptions].map(option=>option.value);
  return {mode:$('mode').value,style:styles[0]||'口语化',styles,degree:$('degree').value,length:$('length').value,
          versions:Number($('versions').value),notes:$('notes').value};
}

function collection() {
  const kinds=[...document.querySelectorAll('input[name="kind"]:checked')].map(input=>input.value);
  const domains=[...$('domain').selectedOptions].map(option=>option.value).filter(Boolean);
  return {keyword:$('keyword').value.trim(),domain:domains[0]||'',domains,kind:kinds[0]||'article',kinds,hours:Number($('hours').value),
          read_min:null,read_max:null,sort:$('sort').value,limit:Number($('limit').value),
          auto_generate:$('auto-generate').checked,generation:generation()};
}

function applyPreferences(prefs) {
  if (!prefs || !Object.keys(prefs).length) return;
  for (const [key,id] of [['keyword','keyword'],['domain','domain'],['hours','hours'],['sort','sort'],['limit','limit']]) {
    if (prefs[key]!==undefined) $(id).value=String(prefs[key]);
  }
  $('auto-generate').checked=Boolean(prefs.auto_generate);
  const kinds=prefs.kinds||[prefs.kind||'article'];
  document.querySelectorAll('input[name="kind"]').forEach(input=>{input.checked=!input.disabled&&kinds.includes(input.value);});
  const domains=prefs.domains||[prefs.domain||''];
  for(const option of $('domain').options)option.selected=option.value?domains.includes(option.value):!domains.some(Boolean);
  const gen=prefs.generation||{};
  for (const key of ['mode','style','degree','length','versions','notes']) {
    if (gen[key]!==undefined) $(key).value=String(gen[key]);
  }
  const styles=gen.styles||[gen.style||'口语化'];
  for(const option of $('style').options)option.selected=styles.includes(option.value);
}

function dateText(value) {
  if (!value) return '未知';
  const date=new Date(value);
  if (Number.isNaN(date.getTime())) return '未知';
  return new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Hong_Kong',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).format(date);
}

const metric = value => value===null || value===undefined ? '<span class="unknown">未知</span>' : esc(Number(value).toLocaleString('zh-CN'));

function showTab(tab) {
  state.tab=tab;
  document.querySelector('.settings-column').hidden=tab!=='materials';
  document.querySelectorAll('[data-tab]').forEach(button=>button.classList.toggle('active',button.dataset.tab===tab));
  for (const name of ['materials','drafts','tasks']) $(name+'-view').hidden=name!==tab;
  const headings={materials:['找到值得写的内容。','从真实资料出发，把选题留给判断，把整理交给工具。'],drafts:['让资料，成为你的表达。','编辑、核对，再决定发布什么。草稿始终保留资料来源。'],tasks:['每一步，都看得见。','查看采集与生成进度，在这里处理失败、重试和停止。']};
  $('page-title').textContent=headings[tab][0];
  $('page-description').textContent=headings[tab][1];
  window.scrollTo(0,0);
  if (tab==='materials') loadMaterials().catch(error=>toast(error.message,true));
  if (tab==='drafts') loadDrafts().catch(error=>toast(error.message,true));
  if (tab==='tasks') loadTasks().catch(error=>toast(error.message,true));
}

function updateSelection() {
  $('selected-count').textContent=state.selected.size;
  $('select-all').checked=Boolean(state.materials.length) && state.materials.every(item=>state.selected.has(item.id));
  $('select-all').indeterminate=state.materials.some(item=>state.selected.has(item.id)) && !$('select-all').checked;
}

async function loadMaterials() {
  const params=new URLSearchParams({query:$('library-query').value,sort:$('sort').value});
  const data=await api('/api/materials?'+params);
  state.materials=data.items;
  $('result-total').textContent=data.items.length;
  $('materials-empty').hidden=data.items.length>0;
  $('unknown-count').textContent=data.unknown_excluded ? `${data.unknown_excluded} 条因指标未知被排除` : '';
  $('materials-body').innerHTML=data.items.map(item=>`<tr>
    <td><input type="checkbox" data-select="${esc(item.id)}" aria-label="选择 ${esc(item.title)}" ${state.selected.has(item.id)?'checked':''}></td>
    <td><button class="material-title" data-source="${esc(item.id)}">${esc(item.title)}</button><div class="material-author">${esc(item.author || '作者未知')} · ${item.kind==='micro'?'微头条':'文章'}</div></td>
    <td>${metric(item.reads)}</td><td>${metric(item.comments)}</td><td>${metric(item.likes)}</td><td>${esc(dateText(item.published_at))}</td>
    <td><a href="${esc(item.url)}" target="_blank" rel="noopener noreferrer" aria-label="打开来源">↗</a></td></tr>`).join('');
  updateSelection();
}

async function loadDrafts() {
  state.drafts=await api('/api/drafts');
  $('drafts-empty').hidden=state.drafts.length>0;
  $('draft-list').innerHTML=state.drafts.map(draft=>`<button class="draft-card" data-draft="${esc(draft.id)}">
    <span class="eyebrow">${esc(draft.options?.style||'')} / ${draft.options?.mode==='multi'?'多来源成稿':'单篇改写'} / V${esc(draft.version)}</span>
    <h4>${esc(draft.title)}</h4><p>${esc(draft.body.slice(0,110))}${draft.body.length>110?'…':''}</p>
    <small>${esc(dateText(draft.created_at))} · ${esc(draft.model)} · ${(draft.sources||[]).length} 个来源</small></button>`).join('');
}

async function loadTasks() {
  state.tasks=await api('/api/tasks');
  const expanded=new Set([...document.querySelectorAll('[data-log][open]')].map(node=>node.dataset.log));
  $('tasks-empty').hidden=state.tasks.length>0;
  $('task-list').innerHTML=state.tasks.map(task=>{
    const live=['waiting','running'].includes(task.status);
    const title=task.kind==='generate'?'生成草稿':task.kind==='import'?'链接导入':'选题采集';
    return `<article class="task-row"><div class="task-heading"><strong>${title} <span class="muted">${esc(task.options.keyword||task.options.domain||'')}</span></strong><span class="task-badge ${live?'live':['failed','partial','attention'].includes(task.status)?'error':''}">${esc(statusNames[task.status]||task.status)}</span></div>
      <div class="task-description">${esc(dateText(task.created_at))} · 已检查 ${task.checked} / 已入选 ${task.accepted} · 草稿 ${task.generated} · 未知排除 ${task.unknown} · 错误 ${task.errors}</div>
      <div class="task-actions">${live||task.status==='attention'?`<button class="button" data-stop="${esc(task.id)}">停止任务</button>`:''}${['failed','partial','stopped'].includes(task.status)?`<button class="button" data-retry="${esc(task.id)}">重试未完成部分</button>`:''}</div>
      <details data-log="${esc(task.id)}" ${expanded.has(task.id)?'open':''}><summary>查看任务日志 · ${task.logs.length} 条</summary><pre>${esc(task.logs.join('\n')||'等待执行…')}</pre></details></article>`;
  }).join('');
  const current=state.tasks.find(task=>task.kind==='collect'||task.kind==='import');
  if (current) $('collection-progress').textContent=`${statusNames[current.status]} · 已检查 ${current.checked} / 已入选 ${current.accepted} · 未知排除 ${current.unknown}`;
}

const publishingNames={pending:'待发布',queued:'已备稿',awaiting_confirmation:'待人工处理',publishing:'发布中',submitted:'已提交／审核中',published:'已发布',rejected:'审核未通过',uncertain:'结果待核实',failed:'失败',skipped:'已跳过',cancelled:'已取消'};
let publishingRows=[],publicationPreview=null;
function renderPublishing(config) {
  $('publishing-enabled').checked=config.enabled;
  $('publishing-ai').checked=config.ai_declaration;
  $('publishing-enabled').disabled=$('publishing-ai').disabled=false;
  $('publishing-state').dataset.state=config.state;
  const labels={disabled:'自动发布已关闭',unverified:'设置已开启 · 待验证，未运行',paused:'自动发布已暂停',idle:'自动发布已就绪 · 等待运行时段',ready:config.running?`正在${config.publish_phase||'核查并发布'} · ${config.publish_elapsed_seconds||0}秒`:config.preparing?'正在自动选题与成稿':'自动执行中 · 等待下一排期'};
  $('publishing-state').textContent=labels[config.state]||'状态待核实';
  $('publishing-reason').textContent=config.reason||'按已保存排期执行，资料不足或检查失败时跳过，不补发。';
  $('publishing-account').textContent='账号：'+config.account;
  const counts=config.counts||{},slots=config.today?.slots||[];
  const stats=[['今日目标',config.today?.target||0],['已提交',config.submitted_total??((counts.submitted||0)+(counts.published||0)+(counts.rejected||0))],['已发布',config.published_total??(counts.published||0)],['已跳过',slots.filter(s=>s.status==='skipped').length],['待核实',counts.uncertain||0]];
  if(config.editorial_profile==='ai-tools-v1'&&config.today?.content_mix){
    stats.push(['新闻计划',config.today.content_mix.news],['工具计划',config.today.content_mix['tool-guide']],['定时新闻已提交',config.content_mix_submitted?.news||0]);
  }
  $('publishing-stats').innerHTML=stats.map(([label,value])=>`<span>${esc(label)}<b>${esc(value)}</b></span>`).join('');
  $('publishing-next').textContent='下一排期：'+(config.next_at?dateText(config.next_at):'今天没有未来排期')+' · 程序'+(config.service_active?'正在运行':'未启动');
  $('legacy-publishing-keywords').hidden=config.editorial_profile==='ai-tools-v1';
  $('publishing-plan').innerHTML=slots.map(slot=>`<span class="publishing-slot" data-status="${esc(slot.status)}" title="${esc(slot.reason||publishingNames[slot.status]||slot.status)}">${esc(new Date(slot.scheduled_at).toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit',timeZone:'Asia/Shanghai'}))}${config.editorial_profile==='ai-tools-v1'&&slot.content_type?' · '+(slot.content_type==='news'?'新闻':'工具'):''} · ${esc(publishingNames[slot.status]||slot.status)}</span>`).join('');
  for(const [id,topic] of [['life','民生'],['ai','AI'],['tech','科技']]) {
    const input=$('publishing-keywords-'+id);
    if(!input.dataset.loaded){input.value=(config.keywords?.[topic]||[]).join('，');input.dataset.loaded='1';}
  }
}

async function loadPublishingQueue(){
  const data=await api('/api/publishing/queue');publishingRows=data.items;
  const today=state.status?.publishing?.today?.date;
  $('publishing-queue').innerHTML=data.items.filter(row=>row.scheduled_at.startsWith(today||'')||row.status==='uncertain').map(row=>`<div class="publication-row"><div><strong>${esc(row.title)}</strong><p>${esc(row.topic)} · ${esc(dateText(row.scheduled_at))} · ${esc(publishingNames[row.status]||row.status)}</p></div><button class="button" data-publication="${esc(row.id)}">查看正文与结果</button></div>`).join('');
  $('publishing-logs').textContent=(data.logs||[]).map(log=>`${dateText(log.at)}  ${log.message}`).join('\n')||'等待运行记录';
}

function showPublication(identity){
  const row=publishingRows.find(item=>item.id===identity);if(!row)return;
  publicationPreview=row;
  $('publication-title').textContent=row.title;
  $('publication-meta').textContent=`${row.topic} · ${dateText(row.scheduled_at)} · ${publishingNames[row.status]||row.status}`;
  $('publication-reason').textContent=row.reason||'发布只提交以下正文，内部管理标题不发布。';
  $('publication-body').textContent=row.body;
  $('publication-sources').innerHTML=(row.sources||[]).map(source=>`<a href="${esc(source.url)}" target="_blank" rel="noopener noreferrer">${esc(source.title||source.url)} ↗</a>`).join('');
  $('publication-confirm').hidden=row.status!=='awaiting_confirmation';
  $('publication-reject').hidden=!['pending','awaiting_confirmation'].includes(row.status);
  $('publication-reconcile').hidden=!['uncertain','submitted'].includes(row.status);
  $('publication-dialog').showModal();
}

async function refreshStatus(initial=false) {
  const revision=publishingRevision, wasSaving=publishingSaving;
  const status=await api('/api/status');state.status=status;
  if (!wasSaving && !publishingSaving && revision===publishingRevision) renderPublishing(status.publishing);
  $('material-count').textContent=status.counts.materials;
  $('draft-count').textContent=status.counts.drafts;
  $('model-dot').classList.toggle('ready',status.model.configured);
  $('model-state').textContent=status.model.configured?`当前模型：${status.model.model} · 已配置密钥`:'模型尚未配置。可以先采集素材，配置后再生成。';
  $('attention').hidden=!status.paused;
  if (status.schedule.enabled) {
    const remaining=status.next_run_in===null?'':` · 距下一周期 ${Math.ceil(status.next_run_in/60)} 分钟`;
    $('schedule-state').textContent=`每 ${status.schedule.minutes} 分钟执行${remaining}${status.paused?' · 已暂停':''}`;
  } else $('schedule-state').textContent='定时已关闭';
  if (initial) {
    for(const kind of ['article','micro']){
      const radio=document.querySelector(`input[name="kind"][value="${kind}"]`);
      radio.disabled=!status.capabilities[kind];
      radio.parentElement.title=status.capabilities[kind]?'':'该体裁正文采集未通过验证';
      radio.nextElementSibling.textContent=(kind==='micro'?'微头条':'文章')+(status.capabilities[kind]?'':' · 暂不可用');
    }
    status.domains.forEach(domain=>{const opt=document.createElement('option');opt.value=domain;opt.textContent=domain;$('domain').append(opt);});
    applyPreferences(status.preferences);
    $('schedule-enabled').checked=status.schedule.enabled;
    $('schedule-minutes').value=status.schedule.minutes;
    updateKindFields();
    document.querySelectorAll('.settings-column select').forEach(syncChoices);
  }
}

function updateKindFields(){
  if(!state.status)return;
  const kinds=[...document.querySelectorAll('input[name="kind"]:checked')].map(input=>input.value);
  const supportsLikes=kinds.some(kind=>(state.status.capabilities.fields?.[kind] || state.status.capabilities).likes);
  const option=$('sort').querySelector('option[value="likes"]');
  option.disabled=!supportsLikes;option.textContent=supportsLikes?'点赞量':'点赞量 · 暂不可用';
  if($('sort').value==='likes'&&!supportsLikes)$('sort').value='published_at';
  syncChoices($('sort'));
}

async function refreshAll(initial=false) {
  await refreshStatus(initial);
  await Promise.all([loadMaterials(),loadDrafts(),loadTasks(),loadPublishingQueue()]);
}

async function openSource(identity) {
  const item=await api('/api/materials/'+identity);
  $('source-title').textContent=item.title;
  $('source-meta').textContent=`${item.author||'作者未知'} · 发布 ${dateText(item.published_at)} · 采集 ${dateText(item.body_collected_at||item.collected_at)}`;
  $('source-link').href=item.url;
  $('source-body').textContent=item.body;
  $('source-dialog').showModal();
}

async function openDraft(identity) {
  const draft=await api('/api/drafts/'+identity);
  state.editing=identity;state.dirty=false;
  $('dirty-mark').textContent='';
  $('draft-title').value=draft.title;
  $('draft-body').value=draft.body;
  $('draft-micro-topic').value=draft.options?.topic||'科技';
  $('draft-verify').value=(draft.verify||[]).join('\n');
  $('draft-sources').innerHTML='<strong>资料来源</strong>'+(draft.sources||[]).map(source=>`<a target="_blank" rel="noopener noreferrer" href="${esc(source.url)}">${esc(source.title||source.url)} ↗</a>`).join('');
  $('draft-meta').textContent=`${draft.model} · ${dateText(draft.created_at)} · ${draft.options?.style||''} · 版本 ${draft.version}`;
  $('draft-dedup-result').textContent='';
  $('draft-dialog').showModal();
}

async function saveDraft() {
  if (!state.editing) return;
  await api('/api/drafts/'+state.editing,'PUT',{title:$('draft-title').value,body:$('draft-body').value,
            verify:$('draft-verify').value.split('\n').map(line=>line.trim()).filter(Boolean)});
  state.dirty=false;$('dirty-mark').textContent='已保存';
  await loadDrafts();
}

function closeDraft() {
  if (state.dirty && !confirm('草稿有未保存的修改，确定关闭吗？')) return;
  $('draft-dialog').close();state.editing=null;state.dirty=false;
}

document.querySelectorAll('[data-tab]').forEach(button=>button.addEventListener('click',()=>showTab(button.dataset.tab)));
document.querySelectorAll('input[name="kind"]').forEach(radio=>radio.addEventListener('change',updateKindFields));
document.querySelectorAll('[data-close]').forEach(button=>button.addEventListener('click',()=>$(button.dataset.close).close()));
$('collection-form').addEventListener('submit',async event=>{
  event.preventDefault();$('collect-start').disabled=true;
  try {const task=await api('/api/tasks/collect','POST',collection());toast('采集任务已加入队列');await refreshAll();}
  catch(error){toast(error.message,true);} finally{$('collect-start').disabled=false;}
});

for (const id of ['import-open','empty-import']) act(id,()=>{$('import-dialog').showModal();});
act('import-submit',async()=>{
  const urls=$('import-urls').value.split('\n').map(line=>line.trim()).filter(Boolean);
  if (!urls.length) throw new Error('请先粘贴文章链接');
  await api('/api/tasks/import','POST',{urls});$('import-dialog').close();toast('链接导入任务已加入队列');await refreshAll();
});
function applyModelConfig(config) {
  $('model-base-url').value=config.base_url||'';
  $('model-name').value=config.model||'';
  $('model-key-state').textContent=config.key_set?'已保存 · 留空保留':'尚未保存密钥';
  $('model-api-key').placeholder=config.key_set?'留空保留已保存密钥':'填写服务商提供的 API Key';
  $('model-dot').classList.toggle('ready',config.configured);
  $('model-state').textContent=config.configured?`当前模型：${config.model} · 已配置密钥`:'模型尚未配置。可以先采集素材，配置后再生成。';
}
function modelBusy(busy) {
  for(const id of ['model-base-url','model-api-key','model-name','model-save','model-test'])$(id).disabled=busy;
}
act('model-open',async()=>{
  $('model-api-key').value='';
  $('model-feedback').textContent='正在读取本机配置…';
  modelBusy(true);$('model-dialog').showModal();
  try {applyModelConfig(await api('/api/model/config'));$('model-feedback').textContent='';}
  catch(error){$('model-feedback').textContent='配置读取失败，请关闭后重试。';throw error;}
  finally {modelBusy(false);}
});
$('model-form').addEventListener('submit',async event=>{
  event.preventDefault();modelBusy(true);$('model-feedback').textContent='正在保存…';
  try {
    const config=await api('/api/model/config','PUT',{
      base_url:$('model-base-url').value.trim(),api_key:$('model-api-key').value,model:$('model-name').value.trim()
    });
    $('model-api-key').value='';applyModelConfig(config);
    $('model-feedback').textContent=config.configured?'配置已保存，可测试连接或生成草稿。':'配置已保存，请填写 API Key 后再测试。';
    toast('模型配置已保存');
  } catch(error){$('model-feedback').textContent='保存失败，请检查配置后重试。';toast(error.message,true);}
  finally {modelBusy(false);}
});
function clearModelKey(){ $('model-api-key').value=''; }
$('model-dialog').addEventListener('close',clearModelKey);
$('model-dialog').addEventListener('cancel',clearModelKey);
document.querySelector('[data-close="model-dialog"]').addEventListener('click',clearModelKey);
act('model-test',async()=>{
  $('model-save').disabled=true;$('model-feedback').textContent='正在测试已保存的模型配置…';
  try {await api('/api/model/test','POST');$('model-feedback').textContent='模型连接与 JSON 输出测试通过。';toast('模型连接与 JSON 输出测试通过');}
  catch(error){$('model-feedback').textContent='模型测试失败，请检查已保存的配置。';throw error;}
  finally {$('model-save').disabled=false;}
});
act('session-open',async()=>{const result=await api('/api/session/open','POST');toast(result.message);$('session-state').textContent='请在专用 Chrome 中登录';});
async function checkSession(){const result=await api('/api/session');$('session-detail').textContent=result.status+'。'+(result.message||'');}
act('session-inspect',async()=>{$('session-dialog').showModal();await checkSession();});
act('session-check',checkSession);
act('session-clear',async()=>{if(!confirm('清除本工具保存的头条登录会话？素材与草稿会保留。'))return;const result=await api('/api/session/clear','POST');$('session-state').textContent='检查会话';$('session-detail').textContent=result.status;toast('专用登录会话已清除');});
act('resume',async()=>{await api('/api/tasks/resume','POST');toast('已恢复采集，将重新检查页面');await refreshAll();});
act('save-settings',async()=>{
  const options=collection();
  await api('/api/schedule','PUT',{enabled:$('schedule-enabled').checked,minutes:Number($('schedule-minutes').value),collection:options});
  await api('/api/preferences','POST',options);toast('采集、草稿设置与定时条件已保存');await refreshStatus();
});
act('generate',async()=>{
  if (!state.selected.size) throw new Error('请先勾选素材');
  if ($('mode').value==='multi' && (state.selected.size<2||state.selected.size>5)) throw new Error('多来源成稿需选择 2～5 篇相关素材');
  await api('/api/tasks/generate','POST',{...generation(),ids:[...state.selected]});toast('生成任务已加入队列，可在任务记录查看进度');await refreshAll();
});
act('refresh-materials',loadMaterials);act('refresh-drafts',loadDrafts);act('refresh-tasks',loadTasks);
$('sort').addEventListener('change',()=>loadMaterials().catch(error=>toast(error.message,true)));
let searchTimer;$('library-query').addEventListener('input',()=>{clearTimeout(searchTimer);searchTimer=setTimeout(()=>loadMaterials().catch(error=>toast(error.message,true)),250);});
$('select-all').addEventListener('change',event=>{
  if (event.target.checked && state.materials.length>30) {event.target.checked=false;toast('每次最多选择 30 篇素材，请逐篇选择',true);return;}
  state.materials.forEach(item=>{if(event.target.checked)state.selected.add(item.id);else state.selected.delete(item.id);});loadMaterials().catch(error=>toast(error.message,true));
});
$('materials-body').addEventListener('change',event=>{
  const id=event.target.dataset.select;if(!id)return;
  if(event.target.checked && state.selected.size>=30){event.target.checked=false;toast('每次最多选择 30 篇素材',true);return;}
  if(event.target.checked)state.selected.add(id);else state.selected.delete(id);updateSelection();
});
document.addEventListener('click',async event=>{
  const button=event.target.closest('[data-source],[data-draft],[data-stop],[data-retry]');if(!button)return;
  try {
    if(button.dataset.source)await openSource(button.dataset.source);
    if(button.dataset.draft)await openDraft(button.dataset.draft);
    if(button.dataset.stop){await api(`/api/tasks/${button.dataset.stop}/stop`,'POST');toast('已请求停止');await loadTasks();}
    if(button.dataset.retry){await api(`/api/tasks/${button.dataset.retry}/retry`,'POST');toast('已加入重试队列');await loadTasks();}
  }catch(error){toast(error.message,true);}
});
for(const id of ['draft-title','draft-body','draft-verify'])$(id).addEventListener('input',()=>{state.dirty=true;$('dirty-mark').textContent='未保存';$('draft-dedup-result').textContent='内容已修改，请重新查重';});
act('draft-close',closeDraft);
$('draft-dialog').addEventListener('cancel',event=>{event.preventDefault();closeDraft();});
act('draft-save',async()=>{await saveDraft();toast('草稿已保存');});
act('draft-copy',async()=>{await navigator.clipboard.writeText($('draft-title').value+'\n\n'+$('draft-body').value);toast('标题与正文已复制');});
for(const format of ['md','txt'])act('draft-export-'+format,async()=>{await saveDraft();window.location.assign(`/api/drafts/${state.editing}/export?format=${format}`);});
window.addEventListener('beforeunload',event=>{if(state.dirty){event.preventDefault();event.returnValue='';}});
$('today').textContent=new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Hong_Kong',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());

initChoices();
refreshAll(true).catch(error=>toast(error.message,true));
let polling=false;
setInterval(async()=>{
  if(polling||document.hidden)return;polling=true;
  try{
    await refreshStatus();await loadTasks();await loadPublishingQueue();
    if(state.tab==='materials')await loadMaterials();
    if(state.tab==='drafts')await loadDrafts();
  }catch(error){$('session-state').textContent='本地服务连接中断';}
  finally{polling=false;}
},4000);

function openModelFromHash(){
  if(window.location.hash==='#model' && !$('model-dialog').open) $('model-open').click();
}
window.addEventListener('hashchange',openModelFromHash);
openModelFromHash();

act('draft-dedup',async()=>{
  const button=$('draft-dedup'), report=$('draft-dedup-result');
  button.disabled=true;
  const identity=state.editing;
  try {
    const snapshot=[$('draft-title').value,$('draft-body').value,$('draft-verify').value].join('\n');
    await saveDraft();
    report.textContent='正在核对本地发布历史与事件，请稍候…';
    const result=await api('/api/drafts/'+identity+'/dedup-check','POST');
    if(state.editing!==identity)return;
    if(snapshot!==[$('draft-title').value,$('draft-body').value,$('draft-verify').value].join('\n')) {
      report.textContent='内容已修改，本次结果已失效，请重新查重';return;
    }
    const labels={clear:'当前历史未发现重复',duplicate:'重复稿件，禁止发送',review:'需要人工核实，未放行发送'};
    report.textContent=(labels[result.decision]||labels.review)+'。'+(result.reasons||[]).join('；');
  } finally {button.disabled=false;}
});

$('publishing-enabled').addEventListener('change',async()=>{
  const toggle=$('publishing-enabled');
  publishingSaving=true;publishingRevision++;toggle.disabled=true;$('publishing-ai').disabled=true;
  $('publishing-state').textContent='正在保存发布设置…';
  try {
    const result=await api('/api/publishing/settings','PUT',{enabled:toggle.checked,ai_declaration:$('publishing-ai').checked});
    renderPublishing(result);
    toast(result.enabled?'自动发布设置已开启，执行状态见面板':'自动发布已关闭');
  } catch(error) {
    toggle.disabled=true;
    $('publishing-state').textContent='无法确认设置是否保存，请等待重新连接';
    $('publishing-reason').textContent='以服务器保存的状态为准，连接恢复后自动核对。';
    toast(error.message,true);
  } finally {publishingSaving=false;}
});

$('publishing-ai').addEventListener('change',()=>{$('publishing-enabled').dispatchEvent(new Event('change'));});
act('publishing-resume',async()=>{await api('/api/publishing/resume','POST');await refreshStatus();toast('已核实发布账号与声明，运行状态见面板');});
act('publishing-pause',async()=>{await api('/api/publishing/pause','POST');await refreshStatus();toast('已暂停后续发布，已提交内容不撤回');});
act('publishing-keywords-save',async()=>{
  const keywords={};for(const [id,topic] of [['life','民生'],['ai','AI'],['tech','科技']])keywords[topic]=$('publishing-keywords-'+id).value.split(/[,，\n]/).map(v=>v.trim()).filter(Boolean);
  await api('/api/publishing/keywords','PUT',{keywords});toast('选题关键词已保存，下轮使用');
});
document.addEventListener('click',event=>{const button=event.target.closest('[data-publication]');if(button)showPublication(button.dataset.publication);});
for(const action of ['confirm','reject','reconcile'])act('publication-'+action,async()=>{
  if(!publicationPreview)return;const row=publicationPreview;
  await api(`/api/publishing/queue/${row.id}/${action}`,'POST',action==='confirm'?{body_sha256:row.body_sha256}:undefined);
  $('publication-dialog').close();await loadPublishingQueue();await refreshStatus();toast('处理完成，状态已更新');
});
act('draft-stage',async()=>{
  await saveDraft();
  const bytes=await crypto.subtle.digest('SHA-256',new TextEncoder().encode($('draft-body').value));
  const body_sha256=[...new Uint8Array(bytes)].map(v=>v.toString(16).padStart(2,'0')).join('');
  await api(`/api/drafts/${state.editing}/queue`,'POST',{topic:$('draft-micro-topic').value,body_sha256});
  await refreshStatus();await loadPublishingQueue();toast('已通过检查，排入未来一小时空闲时段');
});

act('publishing-bind',async()=>{await api('/api/publishing/bind','POST');await refreshStatus();toast('已绑定当前专用浏览器账号，请验证并运行');});
