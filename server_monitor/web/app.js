'use strict';
const $ = id => document.getElementById(id);
const token = location.hash.slice(1) || sessionStorage.getItem('server-monitor-token') || '';
if (token) sessionStorage.setItem('server-monitor-token', token);
if (location.hash) history.replaceState(null, '', location.pathname);
let state = {servers: [], settings: {scope: 'mine', interval: 10, paused: false}};
let selected = '', query = '', sortKey = 'cpu_percent', descending = true, page = 1;
let diskQuery = '';
const processStates = ['active', 'all', 'R', 'S', 'D', 'T', 'Z'];
let processState = localStorage.getItem('process-state-filter') || 'active';
if (!processStates.includes(processState)) processState = 'active';
const isActiveProcess = process => process.state === 'R' || Number(process.cpu_percent) > 0;
function matchesProcessState(process) {
  if (processState === 'all') return true;
  if (processState === 'active') return isActiveProcess(process);
  return processState === 'T' ? ['T', 't'].includes(process.state) : process.state === processState;
}
let polling = false, stopped = false, toastTimer, renderedFilter = '';
const pageSize = 60, cards = new Map(), navs = new Map(), processRows = new Map();
const diskRows = new Map();
const gib = bytes => bytes === null || bytes === undefined ? '—' : fmt(bytes / 1024**3);
const fmt = (v, digits = 1) => v === null || v === undefined || !Number.isFinite(Number(v)) ? '—' : Number(v).toLocaleString('en-US', {minimumFractionDigits: digits, maximumFractionDigits: digits});
const timeText = v => v ? new Date(v).toLocaleTimeString('zh-CN', {hour12:false}) : '等待采样';
function age(seconds) { const s = Math.max(0, Math.floor(seconds || 0)); return (s >= 86400 ? Math.floor(s / 86400) + '天 ' : '') + [Math.floor(s / 3600) % 24, Math.floor(s / 60) % 60, s % 60].map(n => String(n).padStart(2,'0')).join(':'); }
function friendly(message) {
  if (/Permission denied/.test(message)) return '认证失败，请检查用户名、密码或 SSH 密钥。';
  if (/REMOTE HOST IDENTIFICATION HAS CHANGED|Host key verification failed/.test(message)) return '主机密钥与本机记录不符，请先向管理员核实。';
  if (/Connection refused/.test(message)) return '连接被拒绝，请检查 SSH 端口和服务状态。';
  if (/timed out|超时/.test(message)) return '连接或采样超时，请检查网络、VPN或服务器负载。';
  if (/No route to host|Network is unreachable/.test(message)) return '网络不可达，请检查内网或 VPN。';
  return message || '连接失败';
}
function toast(text, error = false) { const el = $('toast'); clearTimeout(toastTimer); el.textContent = text; el.className = error ? 'error' : ''; el.hidden = false; toastTimer = setTimeout(() => {el.hidden = true;}, 4000); }
async function api(path, body) {
  const headers = {'X-Monitor-Token': token};
  const options = {headers, cache: 'no-store'};
  if (body !== undefined) {options.method = 'POST'; headers['Content-Type'] = 'application/json'; options.body = JSON.stringify(body);}
  const controller = new AbortController(); options.signal = controller.signal;
  const timeout = setTimeout(() => controller.abort(), 8000);
  try {
    const response = await fetch('/api/' + path, options);
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || '操作失败');
    return data;
  } finally {clearTimeout(timeout);}
}
function node(tag, className, text) { const element = document.createElement(tag); if (className) element.className = className; if (text !== undefined) element.textContent = text; return element; }
function statusOf(server) {
  if (!server.enabled) return {name:'已停用', style:'', dot:''};
  if (server.status === 'login') return {name:'待登录', style:'', dot:'amber'};
  if (server.status === 'error') return {name:'连接失败', style:'error', dot:'red'};
  if (server.busy && !server.data) return {name:'连接中', style:'', dot:'amber pulse'};
  if (server.data && server.status === 'online') return {name:'在线', style:'online', dot:'green'};
  return {name:server.busy ? '连接中' : '待连接', style:'', dot:'amber'};
}
function selectServer(sid) { selected = sid; page = 1; $('server-filter').value = sid; $('disk-server-filter').value = sid; render(); }
function createCard(server) {
  const el = node('article','server-card');
  el.tabIndex = 0; el.dataset.id = server.id; el.setAttribute('aria-label', server.name + '，点击筛选磁盘和进程');
  const heading = node('div','card-heading'), identity = node('div','server-identity');
  identity.append(node('strong','server-name'));
  const actions = node('div','card-actions'), badge = node('span','status-badge'), edit = node('button','icon-button card-edit','⚙');
  badge.append(node('span','dot'),node('span','status-text'));
  edit.title = '编辑连接配置'; edit.setAttribute('aria-label','编辑 ' + server.name);
  edit.addEventListener('click', event => {event.stopPropagation(); editServer(server.id);});
  actions.append(badge,edit); heading.append(identity,actions);
  const cpu = node('div','metric'), cpuTop = node('div','metric-top');
  cpuTop.append(node('span','','CPU 空闲'),node('strong','cpu-value','—'));
  const cpuTrack = node('div','track');cpuTrack.append(node('span','cpu-bar'));cpu.append(cpuTop,cpuTrack);
  const mem = node('div','metric'), memTop = node('div','metric-top');
  memTop.append(node('span','','可用内存'),node('strong','memory-value','—'));
  const memTrack = node('div','track memory-track'); memTrack.append(node('span','memory-bar'));mem.append(memTop,memTrack);
  const storage = node('button','storage-link','等待磁盘采样');
  storage.type = 'button';
  storage.addEventListener('click', event => {event.stopPropagation(); selectServer(server.id); $('disk-section').scrollIntoView({behavior:'smooth',block:'start'});});
  const footer = node('div','card-footer'); footer.append(node('span','tasks'),node('span','sample-time'));
  const err = node('p','card-error');err.hidden = true;
  el.append(heading,node('p','server-address'),cpu,mem,storage,err,footer);
  el.addEventListener('click',()=>selectServer(selected === server.id ? '' : server.id));
  el.addEventListener('keydown',event=>{if(event.target === el && (event.key==='Enter'||event.key===' ')){event.preventDefault();selectServer(selected===server.id?'':server.id);}});
  $('server-cards').append(el);cards.set(server.id,el);return el;
}
function updateCard(server) {
  const el = cards.get(server.id) || createCard(server), status = statusOf(server), data = server.data;
  el.classList.toggle('selected',selected === server.id);el.classList.toggle('is-disabled',!server.enabled);el.classList.toggle('stale',server.status === 'error' && !!data);
  el.querySelector('.server-name').textContent=server.name;
  el.querySelector('.server-address').textContent=(server.user?server.user+' @ ':'')+server.host+':'+server.port;
  el.querySelector('.server-address').title=server.alias || '';
  el.querySelector('.status-badge').className='status-badge '+status.style;
  el.querySelector('.status-badge .dot').className='dot '+status.dot;
  el.querySelector('.status-text').textContent=status.name;
  el.querySelector('.cpu-value').textContent=data ? fmt(data.cpu_idle_percent)+'% · '+fmt(data.idle_cpu_equivalents)+' / '+fmt(data.logical_cpus,0)+' 核' : '—';
  el.querySelector('.memory-value').textContent=data ? fmt(data.memory_available_gib)+' / '+fmt(data.memory_total_gib)+' GiB' : '—';
  const bar=el.querySelector('.cpu-bar'), idle=data?.cpu_idle_percent || 0;
  bar.style.width=Math.max(0,Math.min(100,idle))+'%';bar.classList.toggle('critical',!!data&&idle<10);bar.classList.toggle('low',!!data&&idle>=10&&idle<25);
  el.querySelector('.memory-bar').style.width=(data&&data.memory_total_gib ? Math.max(0,Math.min(100,100*data.memory_available_gib/data.memory_total_gib)) : 0)+'%';
  el.querySelector('.storage-link').textContent=data?.disk_error?'磁盘采样异常 · 查看详情':data?.disks?data.disks.length+' 个挂载 · 查看容量与路径':'等待磁盘采样';
  const err=el.querySelector('.card-error');err.hidden=!['error','login'].includes(server.status);err.textContent=friendly(server.error);err.title=server.error;
  const mode=state.settings.scope==='mine'?'我的':'全部';
  el.querySelector('.tasks').textContent=server.scope_pending?'正在切换进程模式…':data?mode+'进程 '+data.processes.length+' · 负载 '+fmt(data.load_1_5_15?.[0]):'尚无采样数据';
  const stale=server.status==='error'&&data;
  el.querySelector('.sample-time').textContent=(stale?'旧数据 ':server.busy?'更新中 · ':'')+timeText(server.last_success);
  el.querySelector('.sample-time').title=server.last_success?new Date(server.last_success).toLocaleString():'首次SSH连接可能需要数十秒';
  let nav=navs.get(server.id);
  if(!nav){nav=node('button');nav.append(node('span','dot'),node('span','nav-name'),node('span','nav-status'));nav.addEventListener('click',()=>{selectServer(server.id);$('process-search').scrollIntoView({behavior:'smooth',block:'center'});});$('server-nav').append(nav);navs.set(server.id,nav);}
  nav.querySelector('.dot').className='dot '+status.dot;nav.querySelector('.nav-name').textContent=server.name;nav.querySelector('.nav-status').textContent=server.enabled?'':'停用';nav.classList.toggle('selected',selected===server.id);
}
function renderSummary() {
  const enabled=state.servers.filter(s=>s.enabled), online=enabled.filter(s=>s.status==='online'&&s.data);
  const failed=enabled.filter(s=>s.status==='error').length, busy=enabled.filter(s=>s.busy).length;
  $('nav-count').textContent=state.servers.length;
  $('sum-online').textContent=online.length;$('sum-total').textContent='/ '+enabled.length;
  const login=enabled.filter(s=>s.status==='login').length;
  $('sum-status').textContent=failed?failed+' 台连接失败':login?login+' 台待登录':busy?busy+' 台更新中':enabled.length?'已更新':'未启用服务器';
  $('sum-cpu').textContent=online.length?fmt(online.reduce((n,s)=>n+(s.data.idle_cpu_equivalents||0),0)):'—';
  const memoryRows=online.filter(s=>s.data.memory_available_gib!==null);
  $('sum-memory').textContent=memoryRows.length?fmt(memoryRows.reduce((n,s)=>n+s.data.memory_available_gib,0),0):'—';
  $('sum-process').textContent=online.length?fmt(online.reduce((n,s)=>n+(s.scope_pending?0:s.data.processes.filter(isActiveProcess).length),0),0):'—';
  $('sum-process-label').textContent='运行中的进程';
  $('sum-process-note').textContent=state.settings.scope==='mine'?'当前登录账号':'所有可见用户';
  $('inventory-caption').textContent=state.servers.length+' 台';
  const paused=state.settings.paused;$('pause-button').textContent=paused?'继续':'暂停';
  $('poll-label').textContent=paused?'已暂停':'已连接';$('poll-dot').className='dot '+(paused?'amber':'green');
  $('interval').value=String(state.settings.interval);
  for(const scope of ['mine','all']){const selectedScope=state.settings.scope===scope;$('scope-'+scope).classList.toggle('selected',selectedScope);$('scope-'+scope).setAttribute('aria-pressed',String(selectedScope));}
  const mismatch=state.servers.some(s=>s.enabled&&s.scope_pending);
  $('process-scope-note').textContent=mismatch?'切换中…':state.settings.scope==='mine'?'当前登录账号':'所有可见用户';
  const latest=online.map(s=>s.last_success).filter(Boolean).sort().pop();$('updated-label').textContent=latest?'最近成功采样 '+timeText(latest):'等待首次采样';
}
function processDetail(row) {
  const content=$('detail-content');content.replaceChildren();
  const fields=[['服务器',row.server.name+' · '+row.server.user+'@'+row.server.host+':'+row.server.port],['进程', 'PID '+row.pid+' · PPID '+row.ppid+' · 用户 '+(row.user||row.server.user)],['资源','CPU '+fmt(row.cpu_percent)+'% · 内存 '+fmt(row.rss_gib,3)+' GiB · '+row.threads+' 线程'],['运行时间',age(row.elapsed_seconds)+' · 状态 '+row.state],['完整命令',row.command],['工作目录',row.cwd||'未读取到目录'],['采样时间',new Date(row.server.last_success).toLocaleString()]];
  if(row.server.status!=='online') fields.push(['连接状态','当前未连接，显示最近成功采样。']);
  for(const [label,value] of fields){const container=node('div','detail-row');container.append(node('label','',label),node('div',label==='完整命令'||label==='工作目录'?'mono':'',value));content.append(container);}
  $('detail-dialog').showModal();
}
function renderDisks() {
  const servers=state.servers.filter(s=>s.enabled&&(!selected||s.id===selected));
  const needle=diskQuery.trim().toLowerCase(), warnings=[];
  let rows=[];
  for(const server of servers){
    if(server.data?.disk_error)warnings.push(server.name+'：'+server.data.disk_error);
    for(const disk of server.data?.disks||[])rows.push({...disk,server});
  }
  const total=rows.length;
  if(needle)rows=rows.filter(d=>[d.mountpoint,d.device,d.filesystem,d.server.name].some(v=>String(v||'').toLowerCase().includes(needle)));
  rows.sort((a,b)=>a.server.name.localeCompare(b.server.name,'zh-CN',{numeric:true})||(Number(a.mountpoint!=='/')-Number(b.mountpoint!=='/')||a.mountpoint.localeCompare(b.mountpoint)));
  const scroll=$('disk-table-scroll'),top=scroll.scrollTop,left=scroll.scrollLeft,body=$('disk-body'),keep=new Set();
  for(const disk of rows){
    const key=JSON.stringify([disk.server.id,disk.mountpoint,disk.device]);keep.add(key);
    let tr=diskRows.get(key);
    if(!tr){tr=node('tr');for(let i=0;i<8;i++)tr.append(node('td'));diskRows.set(key,tr);}
    const td=tr.children,stale=disk.server.status==='error';
    tr.className='disk-row'+(stale?' stale':'');
    td[0].className='server-cell';td[0].textContent=disk.server.name+(stale?' · 旧数据':'');
    td[1].className='mount-cell';td[1].textContent=disk.mountpoint;td[1].title=disk.mountpoint;
    td[2].className='disk-device-cell';const device=node('span','disk-device',disk.device),fs=node('span','disk-filesystem',disk.filesystem);device.title=disk.device;td[2].replaceChildren(device,fs);
    for(const [index,field]of [[3,'total_bytes'],[4,'used_bytes'],[5,'available_bytes']]){td[index].className='numeric';td[index].textContent=disk.ok?gib(disk[field]):'—';}
    td[5].classList.toggle('disk-low',disk.ok&&disk.used_percent>=90);
    td[6].className='disk-usage-cell';td[6].replaceChildren();
    if(disk.ok){const label=node('span','disk-percent',fmt(disk.used_percent)+'%'),track=node('div','track disk-track'),bar=node('span');bar.style.width=Math.max(0,Math.min(100,disk.used_percent||0))+'%';bar.className=disk.used_percent>=90?'critical':disk.used_percent>=80?'low':'';track.append(bar);td[6].append(label,track);}
    else td[6].textContent='—';
    td[7].className='disk-status';const badge=node('span','process-state'+(disk.ok?'':' disk-error-state'),disk.ok?(disk.read_only?'只读挂载':'读写挂载'):'读取失败');td[7].replaceChildren(badge);td[7].title=disk.ok?'保留空间 '+gib(disk.reserved_bytes)+' GiB':disk.error||'无法读取容量';
    body.append(tr);
  }
  for(const [key,tr]of diskRows)if(!keep.has(key)){tr.remove();diskRows.delete(key);}
  scroll.scrollTop=top;scroll.scrollLeft=left;
  $('disk-count').textContent=fmt(rows.length,0);$('disk-empty').hidden=rows.length>0;
  const empty=$('disk-empty');empty.querySelector('strong').textContent=needle?'未找到匹配的挂载点':servers.some(s=>s.data&&'disks'in s.data)?'没有可显示的存储挂载':'等待磁盘采样';
  empty.querySelector('p').textContent=needle?'可修改搜索条件。':'';
  $('disk-warning').hidden=!warnings.length;$('disk-warning').textContent=warnings.join('；');
  const latest=servers.filter(s=>s.data?.disks).map(s=>s.last_success).filter(Boolean).sort().pop();
  $('disk-updated').textContent=latest?(needle?rows.length+' / '+total+' 个挂载 · ':'')+'最近采样 '+timeText(latest):'等待采样';
}
function renderProcesses() {
  let rows=[];
  for(const server of state.servers){if(!server.enabled||!server.data||server.scope_pending||selected&&server.id!==selected)continue;for(const p of server.data.processes)rows.push({...p,user:p.user||server.user,server});}
  const total=rows.length,needle=query.trim().toLowerCase();
  rows=rows.filter(matchesProcessState);
  $('process-state-filter').value=processState;
  if(needle)rows=rows.filter(p=>[p.command,p.cwd,p.user,p.pid,p.server.name].some(v=>String(v||'').toLowerCase().includes(needle)));
  rows.sort((a,b)=>{let av=sortKey==='server'?a.server.name:a[sortKey],bv=sortKey==='server'?b.server.name:b[sortKey];if(av===null||av===undefined)return bv===null||bv===undefined?0:1;if(bv===null||bv===undefined)return -1;const comparison=typeof av==='number'?av-bv:String(av).localeCompare(String(bv),'zh-CN',{numeric:true});return(descending?-comparison:comparison)||String(a.server.id).localeCompare(String(b.server.id))||a.pid-b.pid;});
  const pages=Math.max(1,Math.ceil(rows.length/pageSize));page=Math.min(page,pages);const visible=rows.slice((page-1)*pageSize,page*pageSize),body=$('process-body'),keep=new Set();
  const scroll=$('table-scroll'), scrollTop=scroll.scrollTop, scrollLeft=scroll.scrollLeft;
  for(const p of visible){const key=p.server.id+':'+p.pid;keep.add(key);let tr=processRows.get(key);if(!tr){tr=node('tr');for(let i=0;i<9;i++)tr.append(node('td'));tr.tabIndex=0;tr.addEventListener('click',()=>processDetail(tr.process));tr.addEventListener('keydown',e=>{if(e.key==='Enter')processDetail(tr.process);});processRows.set(key,tr);}tr.process=p;const td=tr.children;
    td[0].className='server-cell';td[0].textContent=p.server.name+(p.server.status==='error'?' · 旧数据':'');td[1].className='mono';td[1].textContent=p.pid;td[2].className='owner-cell';td[2].textContent=p.user;td[2].title=p.user;
    td[3].className='numeric'+(p.cpu_percent>=100?' cpu-hot':'');td[3].textContent=fmt(p.cpu_percent);td[4].className='numeric';td[4].textContent=fmt(p.rss_gib,2);td[5].className='numeric';td[5].textContent=fmt(p.threads,0);td[6].className='mono';td[6].textContent=age(p.elapsed_seconds);
    const processStatus=node('span','process-state'+(p.state==='R'?' running':''),p.state==='R'?'运行中':p.state==='S'?'休眠':p.state==='D'?'I/O等待':p.state==='Z'?'僵尸':p.state);td[7].replaceChildren(processStatus);
    td[8].className='command-cell';const cmd=node('span','command-text',p.command),cwd=node('span','command-cwd',p.cwd||'');cmd.title=p.command;cwd.title=p.cwd||'';td[8].replaceChildren(cmd,cwd);
    body.append(tr);
  }
  for(const [key,tr] of processRows){if(!keep.has(key)){tr.remove();processRows.delete(key);}}
  scroll.scrollTop=scrollTop;scroll.scrollLeft=scrollLeft;
  $('process-count').textContent=fmt(rows.length,0);$('table-empty').hidden=rows.length>0;
  if(!rows.length){const title=$('table-empty').querySelector('strong'),description=$('table-empty').querySelector('p');const waiting=state.servers.some(s=>s.enabled&&(s.busy&&!s.data||s.scope_pending));title.textContent=query?'未找到匹配的进程':waiting?'正在读取进程':'当前条件下没有进程';description.textContent=waiting?'正在读取服务器数据。':!state.servers.length?'添加服务器后查看进程。':'可调整状态、搜索条件或用户范围。';}
  $('pagination-label').textContent=rows.length?((page-1)*pageSize+1)+'–'+Math.min(page*pageSize,rows.length)+' / '+fmt(rows.length,0)+' 个进程'+(needle||processState!=='all'?'（共 '+fmt(total,0)+' 个）':''):'0 个进程'+(total?'（共 '+fmt(total,0)+' 个）':'');
  $('page-number').textContent=page+' / '+pages;$('previous-page').disabled=page<=1;$('next-page').disabled=page>=pages;
  document.querySelectorAll('[data-sort]').forEach(button=>{button.querySelector('span').textContent=button.dataset.sort===sortKey?(descending?'↓':'↑'):'↕';});
}
function render() {
  if(selected&&!state.servers.some(s=>s.id===selected))selected='';
  renderSummary();for(const server of state.servers)updateCard(server);
  const ids=new Set(state.servers.map(s=>s.id));for(const [id,el]of cards)if(!ids.has(id)){el.remove();cards.delete(id);navs.get(id)?.remove();navs.delete(id);}
  let empty=$('server-cards').querySelector('.empty-cards');if(!ids.size&&!empty){empty=node('div','empty-cards','还没有服务器，点击“添加服务器”开始。');$('server-cards').append(empty);}else if(ids.size&&empty)empty.remove();
  const filterSignature=JSON.stringify(state.servers.map(s=>[s.id,s.name]));if(filterSignature!==renderedFilter){for(const id of ['server-filter','disk-server-filter']){const filter=$(id);filter.replaceChildren(new Option('所有服务器',''));state.servers.forEach(s=>filter.add(new Option(s.name,s.id)));}renderedFilter=filterSignature;}$('server-filter').value=selected;$('disk-server-filter').value=selected;
  renderDisks();
  renderProcesses();
}
async function poll() {
  if(polling||stopped)return;polling=true;
  try{state=await api('state');$('connection-banner').hidden=true;render();}catch(error){$('connection-banner').hidden=false;$('connection-banner').textContent='连接已断开，请重新启动程序。';$('poll-label').textContent='已断开';$('poll-dot').className='dot red';}finally{polling=false;}
}
async function mutate(path,body,message){try{await api(path,body);if(message)toast(message);await poll();}catch(error){toast(error.message,true);}}
function passwordHelp(){const previous=state.servers.find(s=>s.id===$('field-id').value);const same=previous&&['host','port','user'].every(k=>String(previous[k])===$('field-'+k).value);$('field-password').placeholder=same&&previous.has_password?'已填写，留空保留':'请输入登录密码';$('password-help').textContent=$('field-remember-password').checked?'保存到'+(state.password_storage||'系统凭据存储'):'本次有效';}
function authFields(){const auth=$('field-auth').value;$('identity-field').hidden=auth==='password';$('password-field').hidden=auth==='key';passwordHelp();}
function editServer(sid){const server=state.servers.find(s=>s.id===sid);$('server-form').reset();$('field-id').value=server?.id||'';$('dialog-title').textContent=server?'编辑服务器':'添加服务器';for(const [field,key,defaultValue]of [['name','name',''],['host','host',''],['port','port',22],['user','user',''],['auth','auth','password'],['identity','identity_file',''],['python','python','python3']])$('field-'+field).value=server?.[key]??defaultValue;
  $('field-remember-user').checked=server?.remember_user??true;$('field-remember-password').checked=server?.remember_password??false;$('field-enabled').checked=server?.enabled??true;$('field-password').value='';$('field-password').type='password';$('toggle-password').textContent='显示';$('form-error').hidden=true;$('delete-server').hidden=!server;authFields();$('server-dialog').showModal();}
$('server-form').addEventListener('submit',async event=>{event.preventDefault();const raw={};for(const [field,key]of [['id','id'],['name','name'],['host','host'],['port','port'],['user','user'],['auth','auth'],['identity','identity_file'],['python','python'],['password','password']])raw[key]=$('field-'+field).value;if(!raw.id)delete raw.id;const previous=state.servers.find(s=>s.id===raw.id);raw.alias=previous?.alias||'';raw.enabled=$('field-enabled').checked;raw.clear_password=$('field-clear-password').checked;$('save-server').disabled=true;
  raw.remember_user=$('field-remember-user').checked;raw.remember_password=$('field-remember-password').checked;
  try{await api('server',raw);$('server-dialog').close();toast('已保存');await poll();}catch(error){$('form-error').hidden=false;$('form-error').textContent=error.message;}finally{$('save-server').disabled=false;}
});
$('delete-server').addEventListener('click',async()=>{const server=state.servers.find(s=>s.id===$('field-id').value);if(!server||!confirm('删除“'+server.name+'”的连接配置？'))return;try{await api('remove',{id:server.id});$('server-dialog').close();toast('已删除');await poll();}catch(error){$('form-error').hidden=false;$('form-error').textContent=error.message;}});
$('field-remember-password').addEventListener('change',()=>{if($('field-remember-password').checked){$('field-remember-user').checked=true;$('field-clear-password').checked=false;}passwordHelp();});
$('field-remember-user').addEventListener('change',()=>{if(!$('field-remember-user').checked)$('field-remember-password').checked=false;passwordHelp();});
$('field-clear-password').addEventListener('change',()=>{if($('field-clear-password').checked)$('field-remember-password').checked=false;passwordHelp();});
for(const field of ['host','port','user'])$('field-'+field).addEventListener('input',passwordHelp);
$('field-auth').addEventListener('change',authFields);$('toggle-password').addEventListener('click',()=>{const input=$('field-password');input.type=input.type==='password'?'text':'password';$('toggle-password').textContent=input.type==='password'?'显示':'隐藏';});
for(const id of ['add-server','sidebar-add'])$(id).addEventListener('click',()=>editServer());
for(const id of ['close-dialog','cancel-dialog'])$(id).addEventListener('click',()=>$('server-dialog').close());
$('server-dialog').addEventListener('close',()=>{$('field-password').value='';});
$('close-detail').addEventListener('click',()=>$('detail-dialog').close());
for(const id of ['server-dialog','detail-dialog'])$(id).addEventListener('click',event=>{if(event.target===$(id)){const r=$(id).getBoundingClientRect();if(event.clientX<r.left||event.clientX>r.right||event.clientY<r.top||event.clientY>r.bottom)$(id).close();}});
$('pause-button').addEventListener('click',()=>mutate('settings',{paused:!state.settings.paused},state.settings.paused?'已继续':'已暂停'));
$('refresh-button').addEventListener('click',async()=>{try{if(state.settings.paused)await api('settings',{paused:false});await mutate('refresh',{},'正在刷新');}catch(error){toast(error.message,true);}});
$('interval').addEventListener('change',()=>mutate('settings',{interval:Number($('interval').value)}));
for(const button of document.querySelectorAll('[data-scope]'))button.addEventListener('click',()=>{if(button.dataset.scope!==state.settings.scope){page=1;mutate('settings',{scope:button.dataset.scope},'正在切换进程范围…');}});
$('server-filter').addEventListener('change',()=>selectServer($('server-filter').value));$('overview-button').addEventListener('click',()=>selectServer(''));document.querySelector('.brand').addEventListener('click',event=>{event.preventDefault();selectServer('');});
$('disk-server-filter').addEventListener('change',()=>selectServer($('disk-server-filter').value));
$('disk-search').addEventListener('input',()=>{diskQuery=$('disk-search').value;renderDisks();});
$('process-search').addEventListener('input',()=>{query=$('process-search').value;page=1;renderProcesses();});
$('process-state-filter').addEventListener('change',()=>{processState=$('process-state-filter').value;localStorage.setItem('process-state-filter',processState);page=1;renderProcesses();});
for(const button of document.querySelectorAll('[data-sort]'))button.addEventListener('click',()=>{if(sortKey===button.dataset.sort)descending=!descending;else{sortKey=button.dataset.sort;descending=!['server','user','pid'].includes(sortKey);}page=1;renderProcesses();});
$('previous-page').addEventListener('click',()=>{page--;renderProcesses();$('table-scroll').scrollTop=0;});$('next-page').addEventListener('click',()=>{page++;renderProcesses();$('table-scroll').scrollTop=0;});
$('quit-button').addEventListener('click',async()=>{try{await api('shutdown',{});stopped=true;$('connection-banner').hidden=false;$('connection-banner').textContent='程序已退出，可以关闭页面。';$('poll-label').textContent='已退出';$('poll-dot').className='dot';}catch(error){toast(error.message,true);}});
poll();setInterval(poll,1200);
