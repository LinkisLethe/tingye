/* 听页的页面仅访问同源本地服务。外部标题和文字始终通过 textContent 渲染。 */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const token = document.querySelector('meta[name="local-token"]').content;
  const settingsDialog = $('settings-dialog');
  const activeStatuses = new Set(['queued', 'running', 'paused']);
  const statusNames = {queued:'等待中', paused:'等待远端', running:'处理中', completed:'已完成', failed:'未完成', cancelled:'已取消', interrupted:'已中断'};
  const stageNames = {queued:'等待开始', metadata:'读取视频信息', fetching:'读取视频信息', resolving:'读取视频信息', subtitles:'查找已有字幕', download:'下载音轨', downloading:'下载音轨', model:'准备本地模型', loading_model:'加载本地模型', transcribe:'本地转写', transcribing:'本地转写', transcription:'本地转写', export:'保存到笔记库', exporting:'保存到笔记库', completed:'已保存到笔记库', cancelled:'已取消', interrupted:'上次运行已中断', failed:'处理未完成'};
  let state = null;
  let connected = false;
  let initialized = false;
  let currentFilter = 'all';
  let pollTimer = null;
  let toastTimer = null;
  let stopped = false;
  let polling = false;
  let submitting = false;
  let previousJobStates = new Map();
  const jobNodes = new Map();
  const busyActions = new Set();

  function element(tag, className, value) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (value !== undefined) node.textContent = value;
    return node;
  }
  function icon(kind) {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox','0 0 24 24'); svg.setAttribute('fill','none'); svg.setAttribute('aria-hidden','true');
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    const paths = {check:'m5 12 4 4L19 6', wave:'M4 13v-2m4 6V7m4 13V4m4 13V7m4 6v-2', file:'M7 3h7l4 4v14H6V3h1m7 0v5h4M9 12h6m-6 4h6', warning:'M12 8v5m0 3v.01M3 20h18L12 3 3 20Z'};
    path.setAttribute('d',paths[kind] || paths.file); path.setAttribute('stroke','currentColor'); path.setAttribute('stroke-width','1.5'); path.setAttribute('stroke-linecap','round'); path.setAttribute('stroke-linejoin','round'); svg.append(path);
    return svg;
  }
  function errorText(error) { return error && error.message ? error.message : '操作没有完成，请重试。'; }
  async function api(path, method='GET', body, timeout=15000) {
    const controller = new AbortController();
    const timeoutId = timeout ? window.setTimeout(() => controller.abort(), timeout) : null;
    try {
      const options = {method, headers:{'X-Local-Token':token}, signal:controller.signal, cache:'no-store'};
      if (body !== undefined) {options.headers['Content-Type']='application/json';options.body=JSON.stringify(body);}
      const response = await fetch('/api/' + path, options);
      let result;
      try {result = await response.json();} catch {throw new Error('本地服务返回了无法读取的内容，请重新连接。');}
      if (!response.ok) throw new Error(result.error || '操作没有完成，请重试。');
      return result;
    } catch(error) {
      if (error.name === 'AbortError') throw new Error('本地服务暂时没有响应，请稍后重试。');
      if (error instanceof TypeError) throw new Error('未连接到本地服务，请确认听页仍在运行。');
      throw error;
    } finally {if (timeoutId) window.clearTimeout(timeoutId);}
  }
  function toast(message, isError=false) {
    clearTimeout(toastTimer); $('toast').textContent=message; $('toast').classList.toggle('is-error',isError); $('toast').hidden=false;
    toastTimer=window.setTimeout(() => {$('toast').hidden=true;}, isError?6500:3600);
  }
  function setConnection(isConnected, message='') {
    connected=isConnected;
    $('connection-status').classList.remove('is-loading');
    $('connection-status').classList.toggle('is-offline',!isConnected);
    $('connection-text').textContent=isConnected?'本地服务已连接':stopped?'服务已退出':'连接已断开';
    $('connection-error').hidden=isConnected || stopped;
    $('connection-error-text').textContent=message;
    $('links').disabled=!initialized;
    $('start-button').disabled=!isConnected || submitting;
  }
  function fillSettings(settings) {
    $('vault-path').value=settings.vault_path || '';
    $('subfolder').value=settings.subfolder || '';
    $('model').value=settings.model || 'large-v3-turbo';
    $('language').value=settings.language || 'auto';
    $('cpu-threads').value=settings.cpu_threads || 4;
    $('hotwords').value=settings.hotwords || '';
    $('prefer-subtitles').checked=Boolean(settings.prefer_subtitles);
    syncVaultSelection();
    updateModelNote();
  }
  function updateVaultOptions() {
    const select=$('vault-select');
    const options=[element('option','', '手动选择文件夹')];options[0].value='';
    for (const vault of state.vaults || []) {const option=element('option','',vault.name);option.value=vault.path;options.push(option);}
    select.replaceChildren(...options);syncVaultSelection();
  }
  function syncVaultSelection() {
    const path=$('vault-path').value.trim();
    const match=Array.from($('vault-select').options).find(option => option.value.toLocaleLowerCase() === path.toLocaleLowerCase());
    $('vault-select').value=match ? match.value : '';
  }
  function modelLabel(modelId) {return modelId === 'small' ? 'Small' : 'Turbo';}
  function updateModelNote() {
    const selected=(state?.models || []).find(model => model.id === $('model').value);
    const prefix=modelLabel($('model').value);
    $('model-cache-note').textContent=selected?.cached ? prefix + ' 模型已在本机，可直接使用。' : prefix + ' 模型首次使用时会下载，下载完成后保存在本机。';
  }
  function updateSummary() {
    const s=state.settings;
    const path=s.vault_path || '';
    const fullPath=path ? path.replace(/[\\/]+$/,'') + (s.subfolder ? ' / ' + s.subfolder.replace(/\\/g,'/') : '') : '还未选择笔记库，点击右侧设置保存位置';
    $('destination-path').textContent=fullPath; $('destination-path').title=fullPath;
    $('choose-destination').firstChild.textContent=path?'更改位置':'选择笔记库';
    $('mode-summary').textContent=s.prefer_subtitles?'已有字幕优先，缺少时本地转写':'下载音轨，在本地转写';
    const language={auto:'自动识别语言',zh:'中文',en:'英语'}[s.language] || '自动识别语言';
    $('model-summary').textContent=modelLabel(s.model) + ' · ' + language + ' · 仅保存 Markdown';
    $('app-version').textContent=state.app?.version ? 'v' + state.app.version.replace(/^v/,'') : '';
  }
  function readSettings() {
    return {vault_path:$('vault-path').value.trim(), subfolder:$('subfolder').value.trim(),model:$('model').value,language:$('language').value,cpu_threads:Number($('cpu-threads').value),hotwords:$('hotwords').value.trim(),prefer_subtitles:$('prefer-subtitles').checked,keep_audio:false};
  }
  function openSettings(focusTranscription=false) {
    if (!initialized) {toast('还在连接本地服务，请稍候。');return;}
    fillSettings(state.settings); $('settings-error').hidden=true; settingsDialog.showModal();
    if (focusTranscription) {$('model').focus();$('transcription-settings').scrollIntoView({block:'center'});}
  }
  function closeSettings() {settingsDialog.close();fillSettings(state.settings);}
  async function saveSettings(settings) {
    const result=await api('settings','POST',settings);
    state.settings=result.settings; updateSummary();return result.settings;
  }
  function countLinks() {
    const count=$('links').value.split(/\r?\n/).map(line=>line.trim()).filter(Boolean).length;
    $('link-count').textContent=count + ' 个视频';
  }
  function dateLabel(value) {
    if (!value) return '';
    const date=new Date(value);
    if (Number.isNaN(date.getTime())) return '';
    return date.toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false});
  }
  function sourceLabel(input) {const match=String(input || '').match(/BV[a-zA-Z0-9]{10}/);return match?match[0]:String(input || '视频');}
  function makeAction(label, action, job, className='text-button') {
    const button=element('button',className,label); button.type='button';button.dataset.action=action;
    button.disabled=busyActions.has(job.id);button.addEventListener('click',()=>jobAction(job.id,action,button));
    return button;
  }
  function renderJob(job) {
    const article=element('article','job');article.dataset.id=job.id;
    const top=element('div','job-topline');
    const symbol=element('span','job-symbol');
    const body=element('div','job-body');
    const title=element('h3','job-title');
    const meta=element('p','job-meta');
    const badge=element('span','job-badge');
    body.append(title,meta);
    top.append(symbol,body,badge);article.append(top);
    const detail=element('div','job-detail');
    const stageLine=element('div','job-stage-line');
    const stage=element('span');
    const percentText=element('span','job-percent');
    stageLine.append(stage,percentText);
    const track=element('div','progress-track');track.setAttribute('role','progressbar');track.setAttribute('aria-valuemin','0');track.setAttribute('aria-valuemax','100');
    const fill=element('div','progress-fill');track.append(fill);
    const message=element('p','job-message');
    const error=element('p','job-error');
    detail.append(stageLine,track,message,error);
    const actions=element('div','job-actions');
    const open=element('a','','在 Obsidian 中打开');open.addEventListener('click',()=>toast('已请求打开 Obsidian。若未跳转，可打开文件夹查看笔记。'));
    const folder=makeAction('打开文件夹','open-folder',job);
    const retry=makeAction('重新收录','retry',job);
    const cancel=makeAction('取消','cancel',job,'text-button cancel-action');
    actions.append(open,folder,retry,cancel);detail.append(actions);article.append(detail);
    // Keep the same card and action elements for its entire lifetime. Polling only
    // changes their values, so an in-flight click or keyboard focus remains valid.
    const record={node:article,status:null,refs:{symbol,title,meta,badge,stageLine,stage,percentText,track,fill,message,error,actions,open,folder,retry,cancel}};
    updateJob(record,job);
    return record;
  }
  function updateJob(record, job) {
    const r=record.refs;
    r.title.textContent=job.title || sourceLabel(job.input);
    r.title.title=job.title || job.input || '视频';
    r.meta.textContent=[sourceLabel(job.input),dateLabel(job.created_at)].filter(Boolean).join(' · ');
    r.badge.className='job-badge ' + (statusNames[job.status] ? job.status : '');
    r.badge.textContent=statusNames[job.status] || '处理中';
    if(record.status!==job.status){
      r.symbol.classList.toggle('is-running',job.status==='running');
      r.symbol.replaceChildren(icon(job.status==='completed'?'check':job.status==='running'?'wave':['failed','interrupted'].includes(job.status)?'warning':'file'));
      record.status=job.status;
    }
    const running=job.status==='running';
    r.stageLine.hidden=!running;r.track.hidden=!running;
    if(running){
      const percent=Math.max(0,Math.min(100,Number(job.progress)||0));
      const stageMessage=(job.message || stageNames[job.stage] || '正在处理').replace(/\s*\d+(?:\.\d+)?%$/, '');
      r.stage.textContent=stageMessage;
      r.percentText.textContent=Math.round(percent) + '%';
      r.track.setAttribute('aria-label',stageMessage || '视频收录进度');
      r.track.setAttribute('aria-valuenow',String(Math.round(percent)));
      r.fill.style.width=percent + '%';
    }
    let message='';
    if(job.status==='queued')message='已加入队列，等待前面的任务完成。';
    if(job.status==='completed')message=job.result?.overwritten?'已覆盖原有笔记，同一视频仅保留一份。':job.message || 'Markdown 已存入笔记库。';
    if(job.status==='cancelled')message=job.message || '收录已取消，可以重新添加。';
    r.message.textContent=message;r.message.hidden=!message;
    const error=['failed','interrupted'].includes(job.status) ? job.error || '上次服务退出时未处理完成，可以重试。' : '';
    r.error.textContent=error;r.error.hidden=!error;
    const completed=job.status==='completed' && Boolean(job.result);
    const uri=job.result?.obsidian_uri;
    const validUri=completed && typeof uri==='string' && uri.startsWith('obsidian://');
    r.open.hidden=!validUri;
    if(validUri)r.open.href=uri;else r.open.removeAttribute('href');
    r.folder.hidden=!completed;
    r.retry.hidden=!['failed','interrupted','cancelled'].includes(job.status);
    r.cancel.hidden=!activeStatuses.has(job.status);
    for(const button of [r.folder,r.retry,r.cancel])button.disabled=busyActions.has(job.id);
    r.actions.hidden=r.open.hidden && r.folder.hidden && r.retry.hidden && r.cancel.hidden;
  }
  async function jobAction(id, action, button) {
    if (busyActions.has(id)) return;
    busyActions.add(id);button.disabled=true;const original=button.textContent;button.textContent=action==='cancel'?'正在取消…':action==='retry'?'正在提交…':'正在打开…';
    try {
      const result=await api('jobs/' + encodeURIComponent(id) + '/' + action,'POST',{});
      if (result.job && state) {const index=state.jobs.findIndex(job=>job.id===id);if(index>=0)state.jobs[index]=result.job;else state.jobs.push(result.job);}
      toast(action==='cancel'?'已请求取消收录':action==='retry'?'已重新加入收录队列':'已打开结果文件夹');
    } catch(error) {toast(errorText(error),true);}
    finally {busyActions.delete(id);button.disabled=false;button.textContent=original;renderJobs();}
  }
  function renderJobs() {
    const jobs=state.jobs || [];
    const active=jobs.filter(job=>activeStatuses.has(job.status));
    const completed=jobs.filter(job=>job.status==='completed');
    $('queue-total').textContent=jobs.length; $('count-all').textContent=jobs.length; $('count-active').textContent=active.length; $('count-completed').textContent=completed.length;
    $('queue-status').textContent=active.length ? active.length + ' 个任务待完成' : '依次处理';
    $('runtime-note').textContent=active.some(job=>job.execution_backend==='ssh')?'正在远端 GPU 转写，结果会保存回本机':active.some(job=>job.status==='running')?'正在本机处理，请保持听页服务运行':'转写任务已就绪';
    const filtered=jobs.filter(job=>currentFilter==='active'?activeStatuses.has(job.status):currentFilter==='completed'?job.status==='completed':true).slice().sort((a,b)=>{
      const rank=s=>s==='running'?0:s==='queued'?1:2;
      const diff=rank(a.status)-rank(b.status);if(diff)return diff;
      return new Date(b.created_at || 0)-new Date(a.created_at || 0);
    });
    $('queue-loading').hidden=true; $('queue-empty').hidden=filtered.length>0; $('job-list').hidden=!filtered.length;
    $('empty-title').textContent=currentFilter==='completed'?'完成的笔记会留在这里':currentFilter==='active'?'当前没有待处理的视频':'第一篇笔记，从一个链接开始';
    $('empty-copy').textContent=currentFilter==='completed'?'收录完成后，可直接打开 Obsidian 或结果文件夹。':currentFilter==='active'?'添加新链接，就可以继续收录。':'添加视频后，下载和转写进度会显示在这里。';
    const focused=document.activeElement;
    const focusJob=focused?.closest?.('.job')?.dataset.id;
    const focusAction=focused?.dataset?.action;
    const list=$('job-list');const visibleIds=new Set(filtered.map(job=>job.id));
    for (const [id,record] of jobNodes) {if(!visibleIds.has(id)){record.node.remove();jobNodes.delete(id);}}
    filtered.forEach((job,index)=>{
      let record=jobNodes.get(job.id);
      if(!record){record=renderJob(job);jobNodes.set(job.id,record);}
      else updateJob(record,job);
      if(list.children[index]!==record.node)list.insertBefore(record.node,list.children[index] || null);
    });
    if(focusJob && focusAction && !document.activeElement?.closest?.('.job')) {
      const record=jobNodes.get(focusJob);
      const candidate=record && Array.from(record.node.querySelectorAll('button')).find(button=>button.dataset.action===focusAction);
      if(candidate && !candidate.disabled)candidate.focus({preventScroll:true});
    }
    const announcements=[];
    for(const job of jobs){const prior=previousJobStates.get(job.id);if(prior && prior!==job.status && ['completed','failed','interrupted'].includes(job.status))announcements.push((job.title || sourceLabel(job.input)) + '，' + statusNames[job.status]);}
    if(announcements.length)$('job-announcement').textContent=announcements.join('。');
    previousJobStates=new Map(jobs.map(job=>[job.id,job.status]));
  }
  async function poll() {
    if(polling || stopped)return;
    polling=true;
    try {
      const result=await api('state');
      state=result;
      if(!initialized){updateVaultOptions();fillSettings(state.settings);initialized=true;}
      updateSummary();updateModelNote();renderJobs();setConnection(true);
    } catch(error) {
      setConnection(false,errorText(error));
      if(!initialized){$('queue-loading').hidden=true;$('destination-path').textContent='连接本地服务后选择笔记库';}
    } finally {
      polling=false;clearTimeout(pollTimer);if(!stopped)pollTimer=window.setTimeout(poll,connected?2200:4500);
    }
  }
  $('capture-form').addEventListener('submit',async event=>{
    event.preventDefault();if(submitting || !connected)return;
    $('submit-error').hidden=true;
    const links=$('links').value.trim();if(!links){$('links').focus();return;}
    if(!state.settings.vault_path){openSettings();$('vault-path').focus();toast('先选择笔记库，再开始收录。');return;}
    submitting=true;$('start-button').disabled=true;$('start-label').textContent='正在加入队列…';
    try {
      await saveSettings({...state.settings});
      const result=await api('jobs','POST',{links});
      for(const job of result.jobs || []){const index=state.jobs.findIndex(item=>item.id===job.id);if(index<0)state.jobs.push(job);else state.jobs[index]=job;}
      // Only clear the submitted value, preserving anything typed while the request was in flight.
      if($('links').value.trim()===links)$('links').value='';
      countLinks();setFilter('all');renderJobs();toast('已加入收录队列，完成后会保存到笔记库。');
    } catch(error) {$('submit-error').textContent=errorText(error);$('submit-error').hidden=false;}
    finally{submitting=false;$('start-label').textContent='开始收录';$('start-button').disabled=!connected;}
  });
  $('settings-form').addEventListener('submit',async event=>{
    event.preventDefault();$('settings-error').hidden=true;
    const button=$('save-settings');button.disabled=true;button.textContent='正在保存…';
    try{await saveSettings(readSettings());settingsDialog.close();toast('收录设置已保存');}
    catch(error){$('settings-error').textContent=errorText(error);$('settings-error').hidden=false;$('settings-error').scrollIntoView({block:'nearest'});}
    finally{button.disabled=false;button.textContent='保存设置';}
  });
  $('pick-vault').addEventListener('click',async()=>{
    const button=$('pick-vault');button.disabled=true;button.textContent='等待选择…';$('settings-error').hidden=true;
    try{const result=await api('pick-vault','POST',{},0);if(result.path){$('vault-path').value=result.path;syncVaultSelection();}}
    catch(error){$('settings-error').textContent=errorText(error);$('settings-error').hidden=false;}
    finally{button.disabled=false;button.textContent='选择文件夹';}
  });
  $('vault-select').addEventListener('change',()=>{if($('vault-select').value)$('vault-path').value=$('vault-select').value;else $('vault-path').focus();});
  $('vault-path').addEventListener('input',syncVaultSelection);$('model').addEventListener('change',updateModelNote);
  $('settings-top').addEventListener('click',()=>openSettings());$('choose-destination').addEventListener('click',()=>openSettings());$('edit-transcription').addEventListener('click',()=>openSettings(true));
  $('close-settings').addEventListener('click',closeSettings);$('cancel-settings').addEventListener('click',closeSettings);
  settingsDialog.addEventListener('cancel',()=>fillSettings(state.settings));
  $('links').addEventListener('input',()=>{countLinks();$('submit-error').hidden=true;});
  function setFilter(filter){currentFilter=filter;document.querySelectorAll('[data-filter]').forEach(button=>{const selected=button.dataset.filter===filter;button.classList.toggle('is-active',selected);button.setAttribute('aria-selected',String(selected));button.tabIndex=selected?0:-1;});$('job-list').setAttribute('aria-labelledby','tab-'+filter);if(state)renderJobs();}
  document.querySelectorAll('[data-filter]').forEach(button=>{
    button.addEventListener('click',()=>setFilter(button.dataset.filter));
    button.addEventListener('keydown',event=>{const tabs=Array.from(document.querySelectorAll('[data-filter]'));const index=tabs.indexOf(button);let next;if(event.key==='ArrowRight')next=tabs[(index+1)%tabs.length];if(event.key==='ArrowLeft')next=tabs[(index+tabs.length-1)%tabs.length];if(event.key==='Home')next=tabs[0];if(event.key==='End')next=tabs[tabs.length-1];if(next){event.preventDefault();next.focus();setFilter(next.dataset.filter);}});
  });
  $('reconnect-button').addEventListener('click',()=>{clearTimeout(pollTimer);poll();});
  $('shutdown-button').addEventListener('click',()=>{$('shutdown-copy').textContent=state?.jobs.some(job=>activeStatuses.has(job.status))?'当前还有收录任务。退出会停止处理，下次打开后可以重试。':'退出后，双击启动文件即可再次打开。';$('shutdown-dialog').showModal();});
  $('cancel-shutdown').addEventListener('click',()=>$('shutdown-dialog').close());
  $('confirm-shutdown').addEventListener('click',async()=>{const button=$('confirm-shutdown');button.disabled=true;try{await api('shutdown','POST',{});stopped=true;clearTimeout(pollTimer);$('shutdown-dialog').close();setConnection(false);$('connection-error').hidden=false;$('connection-error-text').textContent='听页服务已退出。双击启动文件即可再次使用。';$('reconnect-button').hidden=true;toast('服务已退出，可以关闭这个页面。');}catch(error){toast(errorText(error),true);}finally{button.disabled=false;}});
  document.addEventListener('visibilitychange',()=>{if(!document.hidden && !stopped){clearTimeout(pollTimer);poll();}});
  poll();
})();
