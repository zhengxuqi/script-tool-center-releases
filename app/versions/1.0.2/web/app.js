'use strict';
const $ = s => document.querySelector(s), $$ = s => [...document.querySelectorAll(s)];
const TOKEN = $('meta[name="local-token"]').content;
const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const titles = {library:'工具库',tasks:'任务中心',results:'结果文件',updates:'版本更新',settings:'偏好设置'};
const actions = {browser:'打开登录页面',categories:'同步类目',time:'读取可用日期',collect:'采集并导出',preview:'检查查询条件'};
const statuses = {queued:'排队中',running:'运行中',stopping:'正在停止',completed:'已完成',cancelled:'已停止',failed:'未完成',interrupted:'已中断'};
let state, files = [], view = '', toolContext = null, taskFilter = 'all', resultFilter = 'all', search = '', toastTimer, refreshing = false, lastFinished = '';
let originalDialogScroll=null;
const busy = job => ['queued','running','stopping'].includes(job.status);
const humanTime = value => new Date(value * 1000).toLocaleString('zh-CN', {hour12:false});
const bytes = value => value > 1048576 ? (value / 1048576).toFixed(1)+' MB' : Math.max(1,Math.round(value/1024))+' KB';
const nameOf = id => id === 'hub' ? '脚本工具中心' : state?.tools.find(t => t.id === id)?.name || id;

function toast(message) {
  $('#toast').textContent = message; $('#toast').hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => $('#toast').hidden = true, 4800);
}
async function api(path, body, raw = false) {
  const options = {headers:{'X-Local-Token':TOKEN}};
  if (body !== undefined) {
    options.method = 'POST'; options.headers['Content-Type'] = raw ? 'application/zip' : 'application/json';
    options.body = raw ? body : JSON.stringify(body);
  }
  const response = await fetch('/api/'+path, options);
  const result = await response.json();
  if (!response.ok) throw Error(result.error || '操作未完成。');
  return result;
}
function heading(eyebrow, title, description, right = '') {
  return `<div class="page-heading"><div><div class="eyebrow">${eyebrow}</div><h1>${title}</h1><p>${description}</p></div>${right}</div>`;
}
function empty(title, description, icon = '▤') {
  return `<div class="empty"><div class="empty-symbol">${icon}</div><strong>${title}</strong><p>${description}</p></div>`;
}
function tag(status) {
  const tone = status === 'completed' ? 'green' : ['running','queued','stopping'].includes(status) ? 'blue' : ['failed','interrupted'].includes(status) ? 'red' : 'orange';
  return `<span class="tag ${tone}">${statuses[status] || escape(status)}</span>`;
}

async function refresh() {
  if (refreshing || $('#reconnecting') && !$('#reconnecting').hidden) return;
  refreshing = true;
  try {
    state = await api('state');
    $('#connection').textContent = '本地服务已连接';
    const finished = state.jobs.filter(j => !busy(j)).map(j => j.id).join(',');
    if (finished !== lastFinished || !view) {
      files = await api('results');
      if (toolContext && finished !== lastFinished) await refreshCatalog();
      lastFinished = finished;
    }
    if (!view) await route();
    updateLive();
  } catch (error) {
    $('#connection').textContent = '本地服务未连接';
    if (!state) $('#main').innerHTML = empty('正在连接工具中心', '请稍候；如果服务已退出，请重新双击启动工具中心。', '↻');
  } finally { refreshing = false; }
}
function updateLive() {
  if (!state) return;
  const queued = state.jobs.filter(busy).length;
  $('#queue-count').textContent = queued || '';
  $('#hub-version').textContent = 'v'+state.version;
  $('#update-dot').classList.toggle('has-update', state.updates.available.length > 0);
  if (view === 'library') renderLibrary();
  if ($('#pdd-service-status')) {
    const s=state.pdd_service||{};
    $('#pdd-service-status').textContent=s.starting?'首次启动正在准备组件，请稍候…':s.error||(!s.running?'服务尚未启动或已停止。':s.busy?'正在采集，进度见下方原页面。':'拼多多服务已就绪。');
  }
  if (view === 'tasks') renderJobs($('#task-list'), state.jobs);
  if (view.startsWith('tool/') && toolContext) renderJobs($('#tool-jobs'), state.jobs.filter(j => j.tool_id === toolContext.id).slice(0,5));
  if (view === 'updates') renderUpdates();
  if (view === 'results') renderResults();
}
async function route() {
  if (!state) return;
  const requested = location.hash.slice(1) || 'library';
  view = requested in titles || /^tool\/[a-z][a-z0-9-]*$/.test(requested) ? requested : 'library';
  toolContext = null;
  $$('[data-nav]').forEach(link => link.classList.toggle('active', link.dataset.nav === (view.startsWith('tool/') ? 'library' : view)));
  $('#breadcrumb').textContent = view.startsWith('tool/') ? nameOf(view.split('/')[1]) : titles[view];
  const main = $('#main');
  if (view === 'library') { main.innerHTML = ''; renderLibrary(); }
  if (view === 'tasks') {
    main.innerHTML = heading('TASK MONITOR', '任务中心', '词根分析与达摩盘按队列执行；拼多多使用独立浏览器，采集进度在原页面查看。') +
      `<div class="section-title"><div class="filter-tabs"><button data-task-filter="all" class="active">全部任务</button><button data-task-filter="active">正在进行</button><button data-task-filter="failed">需要处理</button></div><span class="badge-count">最近 200 项操作及进行中的任务</span></div><div id="task-list" class="task-list"></div>`;
    taskFilter = 'all'; renderJobs($('#task-list'), state.jobs);
  }
  if (view === 'results') {
    main.innerHTML = heading('YOUR EXPORTS', '结果文件', '每次采集单独保存，随时下载 Excel 或查看原始数据。', '<button data-command="reveal">打开结果文件夹 ↗</button>')+
      `<div class="filters"><select id="result-tool" aria-label="筛选工具"><option value="all">全部工具</option>${state.tools.map(t => `<option value="${escape(t.id)}">${escape(t.name)}</option>`).join('')}</select><select id="result-kind" aria-label="筛选格式"><option value="all">全部格式</option><option value="xlsx">Excel 文件</option><option value="json">原始数据 / 采集进度</option></select><input class="search" id="result-search" placeholder="搜索文件名…" aria-label="搜索结果文件"></div><div id="result-list"></div><p class="update-note">${escape(state.outputs)} · 显示最近 300 份文件</p>`;
    files = await api('results'); renderResults();
  }
  if (view === 'updates') {
    main.innerHTML = heading('KEEP THINGS CURRENT', '版本更新', '从开发者的 GitHub Releases 获取更新，或导入离线发布包。', '<button class="primary" data-command="check-updates">↻ 检查更新</button>')+
      `<div class="updates-grid"><div><div id="update-available"></div><section class="panel"><div class="panel-head"><h2>已安装的版本</h2><small>配置与结果独立保存</small></div><div id="installed-list"></div></section><section class="panel"><div class="panel-head"><h2>更新记录</h2></div><div id="update-status"></div></section></div><div><section class="panel"><h3>离线安装</h3><p class="muted update-note">选择开发者提供的签名发布包。</p><label class="upload gap-top">＋ 选择 .zip 发布包<input id="offline-file" type="file" accept=".zip" aria-label="选择离线更新包"><small>最大 100 MB，安装前自动校验</small></label></section><div class="tip">工具更新后，下一项任务会使用新版本。工具中心自身的更新需要重启后生效。</div><section class="panel gap-top"><h3>发布来源</h3><p class="path-value gap-top">${escape(state.config.repository || '尚未设置 GitHub 仓库')}</p><p class="update-note">${state.public_key_ready ? '✓ 发布签名已配置' : '等待开发者配置发布公钥'}</p><a class="back-link gap-top" href="#settings">修改发布设置 →</a></section></div></div>`;
    renderUpdates();
  }
  if (view === 'settings') renderSettings();
  if (view.startsWith('tool/')) await renderTool(view.split('/')[1]);
}
function renderLibrary() {
  if (!$('#tool-cards')) {
    $('#main').innerHTML = heading('YOUR EVERYDAY TOOLKIT', '工作，从这里开始。', '把常用脚本放在一起，让采集、导出和更新更简单。')+
      `<section class="hero"><div><div class="hero-top"><i></i> BUILT FOR YOUR WORKFLOW</div><h2>一个入口，处理日常重复工作。</h2><p>选好工具，设置查询条件，其余交给任务队列。<br>每次使用自己的账号登录，结果保存在自己的电脑。</p></div><div class="hero-art" aria-hidden="true"><div class="orbit"></div><div class="orbit two"></div><div class="hero-squares"><span></span><span></span><span></span><span></span></div></div></section><div class="stats" id="stats"></div><div class="section-title"><h2>我的工具 <small>为你的工作准备</small></h2><input id="tool-search" class="search" placeholder="搜索工具…" aria-label="搜索工具"></div><div class="tool-grid" id="tool-cards"></div><div class="info-strip"><span class="info-symbol">◉</span><div><b>沿用你的浏览器登录状态</b><br>运行采集时自动打开页面并获取当前登录状态。首次使用请在打开的 Chrome 窗口中正常登录。</div></div>`;
    $('#tool-search').value = search;
  }
  const active = state.jobs.filter(busy).length;
  const done = state.jobs.filter(j => j.action === 'collect' && j.status === 'completed').length;
  const stats = [[state.tools.length,'已接入工具','▦'],[active,'排队与运行','≋'],[done,'近期已完成采集','✓'],[files.filter(f=>f.path.endsWith('.xlsx')).length,'近期 Excel 结果','▤']];
  $('#stats').innerHTML = stats.map(([value,label,icon]) => `<div class="stat"><span class="stat-icon">${icon}</span><div><strong>${value}</strong><small>${label}</small></div></div>`).join('');
  const selected = state.tools.filter(t => (t.name+t.description).toLowerCase().includes(search.toLowerCase()));
  $('#tool-cards').innerHTML = selected.map(t => {
    const running = state.jobs.find(j => j.tool_id === t.id && busy(j)) || (t.id==='pdd-review'&&state.pdd_service?.busy?{status:'running'}:null);
    return `<article class="tool-card"><div class="tool-card-head"><span class="tool-icon ${t.id}">${t.id === 'boundary' ? '词' : t.id==='pdd-review'?'评':'D'}</span>${running ? tag(running.status) : '<span class="tag green">● 已就绪</span>'}</div><h3>${escape(t.name)}</h3><p>${escape(t.description)}</p><div class="tool-tags">${(t.id==='boundary'?['词根分析','周期查询','Excel 导出']:t.id==='pdd-review'?['商品评论','自动采集','Excel 导出']:['多类目批量','金额区间','进度恢复']).map(v=>`<span>${v}</span>`).join('')}</div><div class="tool-card-bottom"><small>VERSION ${escape(t.version)} · 本地运行</small><a href="#tool/${escape(t.id)}">打开工具 <span>↗</span></a></div></article>`;
  }).join('') || empty('没有找到对应工具', '试试其他关键词。', '⌕');
}
function renderJobs(container, jobs) {
  if (!container) return;
  if (view === 'tasks' && taskFilter !== 'all') jobs = jobs.filter(j => taskFilter === 'active' ? busy(j) : ['failed','interrupted','cancelled'].includes(j.status));
  const open = [...container.querySelectorAll('details[open]')].map(el => el.dataset.job);
  const scrolls = Object.fromEntries([...container.querySelectorAll('.logs')].map(el => [el.dataset.log, el.scrollTop]));
  container.innerHTML = jobs.length ? jobs.map(job => {
    const allowResume = !job.params._portal_ticket && job.tool_id === 'dmp' && job.action === 'collect' && ['failed','cancelled','interrupted'].includes(job.status);
    return `<article class="task-card"><div class="task-top"><div class="task-title">${escape(nameOf(job.tool_id))}<span class="muted">/ ${actions[job.action] || escape(job.action)}</span></div>${tag(job.status)}</div><p>${escape(job.message)}</p>${job.total ? `<progress class="progress" max="${job.total}" value="${job.completed}"></progress>` : ''}${job.action === 'preview' && job.result?.total ? `<div class="notice">共 ${job.result.categories} 个类目 × ${job.result.periods} 个日期段，合计 ${job.result.total} 个查询条件。</div>`:''}<div class="task-bottom"><span>${humanTime(job.created)} · v${escape(job.version)}${job.total?' · '+job.completed+' / '+job.total:''}</span><div class="actions">${busy(job)?`<button class="small" data-stop="${job.id}" ${job.status==='stopping'?'disabled':''}>${job.status==='queued'?'取消排队':'停止'}</button>`:`${allowResume?`<button class="small subtle" data-resume="${job.id}">继续未完成采集</button>`:''}${job.action==='collect'?`<button class="small" data-rerun="${job.id}">重新运行</button><a href="#results" class="small muted">查看结果 →</a>`:''}`}</div></div>${job.logs.length?`<details data-job="${job.id}" ${open.includes(job.id)?'open':''}><summary>运行日志</summary><div class="logs" data-log="${job.id}">${escape(job.logs.join('\n'))}</div></details>`:''}</article>`;
  }).join('') : empty('这里还没有任务', '在工具中提交查询后，可以在这里跟踪进度与查看日志。', '≋');
  container.querySelectorAll('.logs').forEach(el => { if (scrolls[el.dataset.log]) el.scrollTop=scrolls[el.dataset.log]; });
}
function renderResults() {
  if (!$('#result-list')) return;
  const tool = $('#result-tool').value, kind = $('#result-kind').value, query = $('#result-search').value.toLowerCase();
  const rows = files.filter(row => (tool === 'all' || row.tool_id === tool) && (kind === 'all' || row.path.endsWith('.'+kind)) && row.name.toLowerCase().includes(query));
  $('#result-list').innerHTML = rows.length ? `<div class="table-wrap"><table><thead><tr><th>文件名称</th><th>来源工具</th><th>保存时间</th><th>大小</th><th></th></tr></thead><tbody>${rows.map(row=>`<tr><td class="file-name">${escape(row.name)}<small>${row.path.endsWith('.xlsx')?'EXCEL WORKBOOK':'JSON · 原始数据 / 进度'}</small></td><td>${escape(nameOf(row.tool_id))}</td><td>${humanTime(row.modified)}</td><td>${bytes(row.bytes)}</td><td><button class="small subtle" data-download="${escape(row.path)}">下载 ↓</button></td></tr>`).join('')}</tbody></table></div>` : empty('还没有对应结果', '采集生成的 Excel 与原始数据会保存在这里。');
}
function renderUpdates() {
  if (!$('#installed-list')) return;
  const updating = state.updates.running, active = state.jobs.some(busy);
  $('#installed-list').innerHTML = Object.entries(state.installed).map(([id,record]) => `<div class="release-row"><div><strong>${escape(nameOf(id))}</strong><p>当前 ${escape(record.current)}${id==='hub'&&record.current!==state.version?' · 重启后生效':''}${record.previous?' · 可恢复 '+escape(record.previous):''}</p></div><button class="small" data-rollback="${escape(id)}" ${!record.previous||updating||active?'disabled':''}>回退版本</button></div>`).join('')+
    `<div class="separator"></div><div class="actions end"><button class="small" data-command="restart" ${updating||active?'disabled':''}>重启工具中心</button></div>`;
  $('#update-available').innerHTML = state.updates.available.map(row=>`<section class="panel"><div class="release-row"><div><span class="tag green">有可用更新</span><h3 class="gap-top">${escape(nameOf(row.id))}</h3><p>${escape(row.current)} → ${escape(row.version)}</p></div><button class="primary small" data-install="${escape(row.id)}" ${updating||active||!row.compatible?'disabled':''}>${row.compatible?'安装更新':'请先升级工具中心'}</button></div><p class="muted update-note">${escape(row.notes || '更新后保留个人设置与采集结果。')}</p></section>`).join('');
  $('#update-status').innerHTML = `<p class="${state.updates.error?'danger':'muted'}">${escape(state.updates.message)}</p>${state.updates.logs.length?`<div class="logs">${escape(state.updates.logs.join('\n'))}</div>`:''}`;
  const check = $('[data-command="check-updates"]'); if(check) check.disabled=updating;
  $('#offline-file').disabled=updating||active;
}
function renderSettings() {
  $('#main').innerHTML = heading('MAKE IT YOURS', '偏好设置', '这些设置只作用于当前电脑。排队任务沿用提交时的浏览器设置。')+
    `<form id="settings-form" class="settings-panel"><section class="panel"><div class="panel-head"><h2>浏览器与账号</h2><span class="tag green">每人使用自己的账号</span></div><div class="form-grid"><label class="field full"><span>Chrome 用户目录</span><input id="profile" value="${escape(state.config.profile)}" required><small>自动打开此目录对应的 Chrome，采集前重新获取当前登录状态。</small></label><label class="field"><span>浏览器连接端口</span><input id="browser-port" type="number" min="1024" max="65535" value="${state.config.port}" required><small>默认 9351，可与现有采集脚本共用。</small></label></div></section><section class="panel"><div class="panel-head"><h2>GitHub 发布来源</h2><small>使用公开 Releases</small></div><label class="field"><span>发布仓库</span><input id="repository" value="${escape(state.config.repository)}" placeholder="账号名/仓库名"><small>开发者上传发行包后，使用者在“版本更新”中检查并安装。</small></label></section><section class="panel"><div class="panel-head"><h2>本地存储</h2></div><div class="key-value"><span>结果文件夹</span><b class="path-value">${escape(state.outputs)}</b></div><div class="key-value"><span>运行中的版本</span><b>v${escape(state.version)}</b></div><div class="key-value"><span>发布签名</span><b>${state.public_key_ready?'已配置':'等待配置'}</b></div></section><div class="settings-bottom"><button type="button" data-command="shutdown" class="danger small">退出工具中心</button><button class="primary" type="submit">保存设置</button></div></form>`;
}

async function renderTool(id) {
  const tool = state.tools.find(t=>t.id===id);
  if (!tool) { $('#main').innerHTML=empty('工具不存在','请返回工具库选择已安装工具。'); return; }
  if(tool.ui === 'pdd-original') {
    $('#main').innerHTML='<a class="back-link" href="#library">← 返回工具库</a><div class="actions end"><button data-command="pdd-stop">停止拼多多服务</button><button data-command="pdd-reopen">重新打开采集页</button></div><p class="muted update-note">保留原采集页面，暂不使用钉钉登录。刷新和切换工具会保留记录；Excel 下载后也会保存在结果文件中。</p><div id="pdd-service-status" class="muted update-note"></div><iframe id="original-tool-frame" class="original-tool-frame" title="拼多多原评论采集页面" src="/tools/pdd-review/?_token='+encodeURIComponent(TOKEN)+'"></iframe>';
    return;
  }
  if(tool.ui === 'original') {
    $('#main').innerHTML='<a class="back-link" href="#library">← 返回工具库</a><iframe id="original-tool-frame" class="original-tool-frame" title="达摩盘原采集页面" src="/tools/dmp/"></iframe>';
    return;
  }
  if (!['boundary','dmp'].includes(id)) return renderGenericTool(tool);
  $('#main').innerHTML = empty('正在读取工具设置','请稍候…','↻');
  const [catalog, saved] = await Promise.all([api('catalog?tool='+id),api('tool-settings?tool='+id)]);
  if (view !== 'tool/'+id) return;
  toolContext = {id, catalog, saved:saved.settings, selected:[...(saved.settings.category_ids||[])], extras:saved.settings.category_price_ranges||{}};
  const explanation = id==='boundary'?'选择类目和时间，获取完整词根与关键词并导出 Excel。':'组合类目、时间与金额条件，按顺序采集覆盖人数并导出 Excel。';
  $('#main').innerHTML = `<a class="back-link" href="#library">← 返回工具库</a><div class="page-heading"><div class="tool-heading"><span class="tool-icon ${id}">${id==='boundary'?'词':'D'}</span><div><h1>${escape(tool.name)}</h1><p>${explanation}</p></div></div><span class="tag green">v${escape(tool.version)}</span></div><div class="split-layout"><div><form id="tool-form"><section class="panel"><div class="panel-head"><h2>01 <span class="muted">/</span> 选择类目</h2><div class="actions"><button type="button" class="small" data-tool-action="browser">打开登录页面 ↗</button><button type="button" class="small" data-tool-action="categories">同步${id==='boundary'?'一级':'全部'}类目</button></div></div><div class="form-grid triple"><label class="field"><span>一级类目</span><select id="cat-root"></select></label><label class="field"><span>二级类目 <small>可选</small></span><select id="cat-second"></select></label><label class="field"><span>三级类目 <small>可选</small></span><select id="cat-third"></select></label></div>${id==='boundary'?'<div class="actions gap-top"><button type="button" class="small subtle" data-tool-action="children">同步当前一级类目的全部下级</button><span class="inline-label">未选下级时，查询所选上级类目</span></div>':'<div class="actions gap-top"><button type="button" class="small subtle" id="add-category">＋ 添加当前类目</button><span class="inline-label">支持跨一级类目组合</span></div><div id="selected-categories"></div>'}</section><section class="panel"><div class="panel-head"><h2>02 <span class="muted">/</span> ${id==='boundary'?'选择时间':'日期与金额'}</h2>${id==='boundary'?'<button type="button" class="small" data-tool-action="time">读取可用日期</button>':''}</div><div id="period-fields"></div><div id="time-limits"></div></section><section class="panel"><div class="settings-bottom"><div><h3>开始这次采集</h3><p class="muted update-note">自动打开页面并获取当前登录状态，按队列顺序执行。</p></div><div class="actions">${id==='dmp'?'<button type="button" data-tool-action="preview">检查查询数量</button>':''}<button type="submit" class="primary">加入队列并采集 →</button></div></div></section></form><div class="section-title"><h2>最近操作</h2><a href="#tasks" class="inline-label">所有任务 →</a></div><div id="tool-jobs" class="task-list"></div></div><aside class="panel side-panel"><h3>使用步骤</h3><div class="separator"></div><div class="step"><b>1</b><div><strong>确认登录账号</strong><span>在打开的 Chrome 中正常登录。</span></div></div><div class="step"><b>2</b><div><strong>设置本次条件</strong><span>选择类目与时间，提交任务。</span></div></div><div class="step"><b>3</b><div><strong>等待并下载结果</strong><span>在任务中心查看进度，在结果文件中下载 Excel。</span></div></div><div class="separator"></div><p>浏览器用户目录</p><div class="path-value">${escape(state.config.profile)}</div><a class="back-link gap-top" href="#settings">修改设置 →</a></aside></div>`;
  const today = new Date(), date = d => `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
  const previousFirst = new Date(today.getFullYear(),today.getMonth()-1,1), previousLast = new Date(today.getFullYear(),today.getMonth(),0);
  const p = saved.settings;
  if (id === 'boundary') {
    $('#period-fields').innerHTML=`<div class="form-grid"><label class="field"><span>统计周期</span><select id="period"><option value="7">最近 7 天</option><option value="14">最近 14 天</option><option value="30">最近 30 天</option><option value="week">自然周（周日–周六）</option><option value="month">自然月</option></select></label><label class="field"><span id="date-label">指定日期</span><input id="selected-date" type="date" value="${escape(p.date||date(today))}"><small id="period-help"></small></label></div>`;
    $('#period').value=p.period||'7'; periodChanged(); showLimits(saved.limits);
  } else {
    const ranges = (p.price_ranges || [{min_amount:'0',max_amount:'100'},{min_amount:'100',max_amount:'300'},{min_amount:'300',max_amount:'999999'}]).map(b=>`${b.min_amount}~${b.max_amount}`).join('\n');
    $('#period-fields').innerHTML=`<div class="form-grid"><label class="field"><span>开始日期</span><input id="start-date" type="date" value="${escape(p.start_date||date(previousFirst))}" required></label><label class="field"><span>结束日期</span><input id="end-date" type="date" value="${escape(p.end_date||date(previousLast))}" required></label><label class="checkbox full"><input id="monthly" type="checkbox" ${p.monthly!==false?'checked':''}>按自然月拆分查询（首尾月份使用所选日期范围）</label><label class="field full"><span>默认金额区间</span><textarea id="price-ranges" rows="4" required>${escape(ranges)}</textarea><small>每行一个区间，例如 0~100。也可在已选类目中单独填写金额区间。</small></label></div>`;
  }
  fillCategories(p.root_id || '50002768',p.second_id || '',p.third_id || '');
  if(id==='dmp') renderSelected();
  renderJobs($('#tool-jobs'),state.jobs.filter(j=>j.tool_id===id).slice(0,5));
}
function roots() {
  return toolContext.id==='boundary' ? toolContext.catalog.roots||[] : toolContext.catalog.parents||[];
}
const categoryId = node => String(node?.id ?? node?.category_id ?? '');
const categoryName = node => node?.name ?? node?.category_name ?? '';
function children(id) {
  if(!id) return [];
  if(toolContext.id==='boundary') return toolContext.catalog.children?.[id]||[];
  for(const root of roots()) {
    if(categoryId(root)===id) return root.children||[];
    const second=(root.children||[]).find(c=>categoryId(c)===id);
    if(second) return second.children||[];
  }
  return [];
}
function options(selector,rows,value,placeholder) {
  $(selector).innerHTML=`<option value="">${placeholder}</option>`+rows.map(c=>`<option value="${escape(categoryId(c))}">${escape(categoryName(c))}</option>`).join('');
  $(selector).value=rows.some(c=>categoryId(c)===value)?value:'';
}
function fillCategories(root='',second='',third='') {
  options('#cat-root',roots(),root,'请选择一级类目');
  options('#cat-second',children($('#cat-root').value),second,'查询上级全部 / 选择二级');
  options('#cat-third',children($('#cat-second').value),third,'查询上级全部 / 选择三级');
}
function allCategories() {
  const result = new Map();
  for(const root of roots()) {
    result.set(categoryId(root),root);
    for(const second of children(categoryId(root))) {
      result.set(categoryId(second),second);
      for(const third of children(categoryId(second))) result.set(categoryId(third),third);
    }
  }
  return result;
}
function renderSelected() {
  const lookup=allCategories();
  $('#selected-categories').innerHTML=toolContext.selected.map(id=>`<div class="selected-row"><span>${escape(categoryName(lookup.get(id))||'缓存中未找到的类目')}<small>${escape(id)}</small></span><input data-category-band="${escape(id)}" placeholder="沿用默认金额区间" aria-label="${escape(categoryName(lookup.get(id)))}的金额区间" value="${escape((toolContext.extras[id]||[]).map(b=>`${b.min_amount}~${b.max_amount}`).join('; '))}"><button type="button" data-remove-category="${escape(id)}" aria-label="移除类目">×</button></div>`).join('');
}
function periodChanged() {
  const period=$('#period').value,input=$('#selected-date'),saved=input.value;
  input.type=period==='month'?'month':'date';
  input.disabled=['7','14','30'].includes(period);
  if(period==='month') input.value=saved.slice(0,7);
  else if(saved.length===7) input.value=saved+'-01';
  $('#date-label').textContent=period==='month'?'指定月份':'周内任意日期';
  $('#period-help').textContent=period==='month'?'按完整自然月查询。':period==='week'?'自动使用所选日期所在的周日–周六。':'以网站返回的最新可用日期为准。';
}
function showLimits(value) {
  if(!$('#time-limits')) return;
  $('#time-limits').innerHTML=value?`<div class="notice">类目 ${escape(value.category_id)} 的可用范围：<br>日 ${escape(value.limits.minDayDate)} — ${escape(value.limits.maxDayDate)}<br>周 ${escape(value.limits.minWeekDate)} — ${escape(value.limits.maxWeekDate)}<br>月 ${escape(value.limits.minMonthDate)} — ${escape(value.limits.maxMonthDate)}</div>`:'';
}
async function refreshCatalog() {
  const ctx=toolContext;
  if(!ctx || ctx.generic) return;
  const value=await api('catalog?tool='+ctx.id);
  if(toolContext!==ctx || !$('#cat-root')) return;
  const root=$('#cat-root').value,second=$('#cat-second').value,third=$('#cat-third').value;
  ctx.catalog=value; fillCategories(root,second,third);
  if(ctx.id==='boundary') { const saved=await api('tool-settings?tool=boundary'); if(toolContext===ctx) showLimits(saved.limits); }
}
function parseBands(text) {
  const rows=text.trim().split(/[\n;,；，]+/).filter(s=>s.trim());
  if(!rows.length) throw Error('请至少填写一个金额区间。');
  return rows.map(row=>{
    const match=row.trim().match(/^(\d+(?:\.\d+)?)\s*[~～\-至]\s*(\d+(?:\.\d+)?)$/);
    if(!match || Number(match[1])>Number(match[2])) throw Error('金额区间格式应为 0~100，且起始金额不大于结束金额。');
    return {min_amount:match[1],max_amount:match[2]};
  });
}
function toolParams() {
  if(toolContext.generic) {
    const result={};
    for(const field of toolContext.fields) {
      const input=document.getElementById('generic-'+field.key);
      result[field.key]=field.type==='checkbox'?input.checked:field.type==='number'?Number(input.value):field.type==='lines'?input.value.split('\n').filter(s=>s.trim()):input.value;
    }
    return result;
  }
  const common={root_id:$('#cat-root').value,second_id:$('#cat-second').value,third_id:$('#cat-third').value};
  if(toolContext.id==='boundary') {
    if(!common.root_id) throw Error('请选择一级类目。');
    return {...common,period:$('#period').value,date:$('#selected-date').disabled?'':$('#selected-date').value};
  }
  if(!toolContext.selected.length) throw Error('请先添加至少一个类目。');
  const extras={};
  $$('[data-category-band]').forEach(input=>{ if(input.value.trim()) extras[input.dataset.categoryBand]=parseBands(input.value); });
  return {...common,category_ids:[...toolContext.selected],start_date:$('#start-date').value,end_date:$('#end-date').value,
    monthly:$('#monthly').checked,price_ranges:parseBands($('#price-ranges').value),category_price_ranges:extras};
}
async function submitTool(action) {
  let params={};
  if(toolContext.generic || ['collect','preview','time'].includes(action)) params=toolParams();
  if(action==='children') {
    if(!$('#cat-root').value) throw Error('请选择一级类目。');
    params={parent:$('#cat-root').value}; action='categories';
  }
  if(['collect','preview'].includes(action)) await api('tool-settings',{tool_id:toolContext.id,params});
  await api('submit',{tool_id:toolContext.id,action,params});
  toast('已加入任务队列，可在下方或任务中心查看进度。'); await refresh();
}

async function renderGenericTool(tool) {
  const saved=await api('tool-settings?tool='+tool.id);
  if(view!=='tool/'+tool.id) return;
  const fields=(tool.form?.fields||[]).filter(field=>/^[a-zA-Z][\w-]{0,63}$/.test(field.key));
  toolContext={id:tool.id,generic:true,fields,saved:saved.settings};
  const markup=fields.map(field=>{
    const value=saved.settings[field.key]??field.default??'',id='generic-'+field.key;
    const allowed=['text','number','date','month'];
    let input;
    if(field.type==='checkbox') return `<label class="checkbox full"><input id="${id}" type="checkbox" ${value?'checked':''}>${escape(field.label||field.key)}</label>`;
    if(['textarea','lines'].includes(field.type)) input=`<textarea id="${id}" rows="4" ${field.required?'required':''}>${escape(Array.isArray(value)?value.join('\n'):value)}</textarea>`;
    else if(field.type==='select') input=`<select id="${id}">${(field.options||[]).map(option=>`<option value="${escape(option.value)}" ${String(option.value)===String(value)?'selected':''}>${escape(option.label||option.value)}</option>`).join('')}</select>`;
    else input=`<input id="${id}" type="${allowed.includes(field.type)?field.type:'text'}" value="${escape(value)}" placeholder="${escape(field.placeholder||'')}" ${field.required?'required':''}>`;
    return `<label class="field ${['textarea','lines'].includes(field.type)?'full':''}"><span>${escape(field.label||field.key)}</span>${input}${field.help?`<small>${escape(field.help)}</small>`:''}</label>`;
  }).join('');
  $('#main').innerHTML=`<a class="back-link" href="#library">← 返回工具库</a>${heading('TOOL WORKSPACE',escape(tool.name),escape(tool.description||''),'<span class="tag green">v'+escape(tool.version)+'</span>')}<form id="tool-form"><section class="panel"><div class="panel-head"><h2>运行设置</h2></div><div class="form-grid">${markup||'<p class="muted">此工具可直接运行，无需额外设置。</p>'}</div><div class="actions end gap-top">${(tool.actions||[]).filter(a=>a!=='collect').map(action=>`<button type="button" data-tool-action="${escape(action)}">${escape(actions[action]||tool.action_labels?.[action]||action)}</button>`).join('')}<button type="submit" class="primary">加入队列并运行 →</button></div></section></form><div class="section-title"><h2>最近操作</h2></div><div id="tool-jobs" class="task-list"></div>`;
  renderJobs($('#tool-jobs'),state.jobs.filter(job=>job.tool_id===tool.id).slice(0,5));
}

document.addEventListener('click',async event=>{
  const target=event.target.closest('button,[data-download]'); if(!target || target.disabled) return;
  try {
    if(target.dataset.toolAction) await submitTool(target.dataset.toolAction);
    if(target.id==='add-category') {
      const id=$('#cat-third').value||$('#cat-second').value||$('#cat-root').value;
      if(!id) throw Error('请先选择类目。');
      if(!toolContext.selected.includes(id)) toolContext.selected.push(id);
      else toast('这个类目已经添加。');
      // Preserve edited per-category bands before rebuilding the list.
      $$('[data-category-band]').forEach(input=>{ if(input.value.trim()) toolContext.extras[input.dataset.categoryBand]=parseBands(input.value); });
      renderSelected();
    }
    if(target.dataset.removeCategory) {
      $$('[data-category-band]').forEach(input=>{ if(input.value.trim()) toolContext.extras[input.dataset.categoryBand]=parseBands(input.value); else delete toolContext.extras[input.dataset.categoryBand]; });
      toolContext.selected=toolContext.selected.filter(id=>id!==target.dataset.removeCategory);
      renderSelected();
    }
    if(target.dataset.stop) { await api('stop',{job_id:target.dataset.stop}); await refresh(); }
    if(target.dataset.resume || target.dataset.rerun) {
      const id=target.dataset.resume||target.dataset.rerun,job=state.jobs.find(j=>j.id===id);
      if(job.params._portal_ticket) {
        location.hash='tool/dmp';
        toast('请在原采集页面中预览条件或继续未完成采集。');
        return;
      }
      await api('submit',{tool_id:job.tool_id,action:job.action,params:job.params,...(target.dataset.resume?{resume:id}:{})});
      toast('已加入任务队列。'); await refresh();
    }
    if(target.dataset.taskFilter) {
      taskFilter=target.dataset.taskFilter;
      $$('[data-task-filter]').forEach(b=>b.classList.toggle('active',b===target));
      renderJobs($('#task-list'),state.jobs);
    }
    if(target.dataset.download) {
      target.disabled=true;
      const response=await fetch('/api/download?path='+encodeURIComponent(target.dataset.download),{headers:{'X-Local-Token':TOKEN}});
      if(!response.ok) throw Error((await response.json()).error);
      const url=URL.createObjectURL(await response.blob()),link=document.createElement('a');
      link.href=url; link.download=target.dataset.download.split('/').pop(); link.click(); setTimeout(()=>URL.revokeObjectURL(url),60000);
      target.disabled=false;
    }
    if(target.dataset.install || target.dataset.rollback) {
      await api('updates/'+(target.dataset.install?'install':'rollback'),{tool_id:target.dataset.install||target.dataset.rollback}); await refresh();
    }
    if(target.dataset.command==='check-updates') {
      if(!state.config.repository) {location.hash='settings'; throw Error('请先填写 GitHub 发布仓库。');}
      await api('updates/check',{}); await refresh();
    }
    if(target.dataset.command==='reveal') await api('reveal',{});
    if(target.dataset.command==='pdd-stop') {
      await api('pdd/stop',{});
      $('#original-tool-frame')?.remove();
      toast('拼多多服务和专用浏览器已停止。'); await refresh();
    }
    if(target.dataset.command==='pdd-reopen') {
      await renderTool('pdd-review');
    }
    if(target.dataset.command==='restart') {
      await api('restart',{}); $('#reconnecting').hidden=false;
      setTimeout(()=>{const timer=setInterval(async()=>{try {const response=await fetch('/api/health',{cache:'no-store'});if(response.ok){clearInterval(timer);location.reload();}}catch{ /* Wait for the local server. */ }},1500);},2500);
    }
    if(target.dataset.command==='shutdown') { await api('shutdown',{}); $('#main').innerHTML=empty('工具中心已退出','下次双击“启动工具中心.cmd”即可重新打开。','✓'); }
  } catch(error) {toast(error.message); target.disabled=false;}
});
document.addEventListener('change',async event=>{
  const target=event.target;
  try {
    if(target.id==='cat-root') fillCategories(target.value);
    if(target.id==='cat-second') options('#cat-third',children(target.value),'','查询上级全部 / 选择三级');
    if(target.id==='period') periodChanged();
    if(['result-tool','result-kind'].includes(target.id)) renderResults();
    if(target.id==='offline-file' && target.files[0]) {
      const file=target.files[0];
      if(file.size>100*1024*1024) throw Error('更新包不能超过 100 MB。');
      await api('updates/import',file,true); toast('正在校验并安装更新包。'); target.value=''; await refresh();
    }
  } catch(error) {toast(error.message);}
});
document.addEventListener('input',event=>{
  if(event.target.id==='tool-search') {search=event.target.value;renderLibrary();}
  if(event.target.id==='result-search') renderResults();
});
document.addEventListener('submit',async event=>{
  event.preventDefault();
  try {
    if(event.target.id==='tool-form') await submitTool('collect');
    if(event.target.id==='settings-form') {
      await api('settings',{profile:$('#profile').value,port:Number($('#browser-port').value),repository:$('#repository').value});
      toast('设置已保存。'); await refresh();
    }
  } catch(error) {toast(error.message);}
});
window.addEventListener('hashchange',()=>route().catch(error=>toast(error.message)));
window.addEventListener('message',event=>{
  const frame=$('#original-tool-frame');
  if(event.origin!==location.origin || !frame || event.source!==frame.contentWindow) return;
  if(['dmp-frame-size','pdd-frame-size'].includes(event.data?.kind)) {
    const height=Number(event.data.height);
    if(Number.isFinite(height)&&height>0) frame.style.height=Math.min(Math.max(height,600),20000)+'px';
  }
  if(event.data?.kind==='dmp-dialog-open') {
    const top=Number(event.data.top),height=Number(event.data.height);
    if(!Number.isFinite(top)||!Number.isFinite(height)||height<=0) return;
    if(originalDialogScroll===null) originalDialogScroll=window.scrollY;
    window.scrollBy(0,frame.getBoundingClientRect().top+top-Math.max(15,(innerHeight-height)/2));
  }
  if(event.data?.kind==='dmp-dialog-closed'&&originalDialogScroll!==null) {
    window.scrollTo(0,originalDialogScroll);originalDialogScroll=null;
  }
});
refresh(); setInterval(refresh,2000);
