/* 配电箱报价工作台 · 前端逻辑
   所有数据来自后端接口，前端不再内置任何图纸数据。 */

const S = {
  route: 'projects',
  jobId: null,
  job: null,
  data: { boxes: [], circuits: [], components: [], requirements: [], uncertainties: [] },
  orig: [],                 // 服务端快照，用于算差异与"已修改"标记
  changes: [],              // 服务端修改记录
  chat: [],
  sub: 'circuits',
  pane: 'list',
  link: true,
  aiWidth: 400,
  zoom: 1,
  page: 1,
  pages: 1,
  reviewIdx: 0,
  voice: false,
  listening: false,
  cropMode: false,
  isCropping: false,
  cropStart: { x: 0, y: 0 },
  currentCropBBox: null,
  lastCropResult: null,
  rec: null,
  settings: {},
  queue: [],
};

const $ = id => document.getElementById(id);
const esc = v => String(v ?? '').replace(/[&<>"']/g, c =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const safeJsArg = v => encodeURIComponent(String(v ?? '')).replace(/'/g, '%27');

function toast(msg, ms) {
  const el = $('toast');
  el.textContent = msg;
  el.classList.add('show');
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.remove('show'), ms || 2600);
}

/* ===================== 用户鉴权与多租户体系 ===================== */

const Auth = {
  token: localStorage.getItem('dgzh_auth_token') || '',
  user: null,
};

async function checkAuth() {
  if (!Auth.token) {
    Auth.user = null;
    renderUserBadge();
    return;
  }
  try {
    const res = await api('/api/auth/me');
    if (res && res.authenticated && res.user) {
      Auth.user = res.user;
    } else {
      Auth.user = null;
      Auth.token = '';
      localStorage.removeItem('dgzh_auth_token');
    }
  } catch (e) {
    Auth.user = null;
    Auth.token = '';
    localStorage.removeItem('dgzh_auth_token');
  }
  renderUserBadge();
}

function renderUserBadge() {
  const btnLoginOpen = $('btnLoginOpen');
  const userBadge = $('userBadge');
  const userDropdown = $('userDropdown');
  if (!btnLoginOpen || !userBadge) return;

  if (Auth.user && Auth.token) {
    btnLoginOpen.hidden = true;
    btnLoginOpen.style.display = 'none';
    userBadge.hidden = false;
    userBadge.style.display = 'flex';
    const name = Auth.user.display_name || Auth.user.username || '工程师';
    const tenant = Auth.user.tenant_name || '工区/企业';
    if ($('userDisplayName')) $('userDisplayName').textContent = name;
    if ($('userTenantName')) $('userTenantName').textContent = tenant;
    if ($('userAvatar')) $('userAvatar').textContent = (name[0] || 'A').toUpperCase();
    
    if ($('udName')) $('udName').textContent = name;
    if ($('udUsername')) $('udUsername').textContent = Auth.user.username;
    if ($('udTenant')) $('udTenant').textContent = tenant;
  } else {
    btnLoginOpen.hidden = false;
    btnLoginOpen.style.display = 'inline-flex';
    userBadge.hidden = true;
    userBadge.style.display = 'none';
    if (userDropdown) {
      userDropdown.hidden = true;
      userDropdown.style.display = 'none';
    }
  }
}

function toggleUserDropdown() {
  const dd = $('userDropdown');
  if (!dd) return;
  const isHidden = dd.hidden || dd.style.display === 'none';
  if (isHidden) {
    dd.hidden = false;
    dd.style.display = 'block';
  } else {
    dd.hidden = true;
    dd.style.display = 'none';
  }
}

function closeUserDropdown() {
  const dd = $('userDropdown');
  if (dd) {
    dd.hidden = true;
    dd.style.display = 'none';
  }
}

window.addEventListener('click', e => {
  const wrap = $('userMenuWrap');
  if (wrap && !wrap.contains(e.target)) {
    closeUserDropdown();
  }
});

function openAuthModal(tab = 'login') {
  closeUserDropdown();
  const m = $('authModal');
  if (m) {
    m.hidden = false;
    m.style.display = 'flex';
    m.classList.add('show');
  }
  switchAuthTab(tab);
}

function closeAuthModal() {
  const m = $('authModal');
  if (m) {
    m.classList.remove('show');
    m.hidden = true;
    m.style.display = 'none';
  }
}

function switchAuthTab(tab) {
  const isLogin = tab === 'login';
  const tabLogin = $('tabBtnLogin');
  const tabReg = $('tabBtnRegister');
  if (tabLogin) tabLogin.classList.toggle('active', isLogin);
  if (tabReg) tabReg.classList.toggle('active', !isLogin);
  if ($('formLogin')) {
    $('formLogin').hidden = !isLogin;
    $('formLogin').style.display = isLogin ? 'block' : 'none';
  }
  if ($('formRegister')) {
    $('formRegister').hidden = isLogin;
    $('formRegister').style.display = isLogin ? 'none' : 'block';
  }
}

async function openAiLogs() {
  closeUserDropdown();
  try {
    $('aiLogProjTitle').textContent = `企业 AI 识别费用与 Token 账单总览`;
    $('aiLogSummaryCards').innerHTML = '<span class="mut">正在获取计费记录...</span>';
    $('aiLogTableBody').innerHTML = '<tr><td colspan="7" style="text-align:center;padding:20px" class="mut">加载中...</td></tr>';
    $('modalAiLog').classList.add('show');
    $('modalAiLog').hidden = false;
    $('modalAiLog').style.display = 'flex';

    const res = await api('/api/ai_logs');
    const totalCost = (res.ai_cost_total || 0).toFixed(4);
    const costIn = (res.ai_cost_in || 0).toFixed(4);
    const costOut = (res.ai_cost_out || 0).toFixed(4);
    const totalTokens = (res.ai_tokens_total || 0).toLocaleString();
    const promptTokens = (res.ai_prompt_tokens || 0).toLocaleString();
    const compTokens = (res.ai_completion_tokens || 0).toLocaleString();
    const logs = res.ai_logs || [];

    $('aiLogSummaryCards').innerHTML = `
      <div class="stat-card primary">
        <div class="label">累计 AI 识别总费用</div>
        <div class="val">￥${totalCost}</div>
        <div class="subval">共消耗 ${totalTokens} Tokens</div>
      </div>
      <div class="stat-card">
        <div class="label">输入 Token 及费用</div>
        <div class="val" style="font-size:14px">${promptTokens}</div>
        <div class="subval">费用: ￥${costIn}</div>
      </div>
      <div class="stat-card">
        <div class="label">输出 Token 及费用</div>
        <div class="val" style="font-size:14px">${compTokens}</div>
        <div class="subval">费用: ￥${costOut}</div>
      </div>
      <div class="stat-card">
        <div class="label">识别任务流水记录</div>
        <div class="val" style="font-size:14px">${logs.length} 次</div>
        <div class="subval">企业所有图纸切片并发与全量解析</div>
      </div>
    `;

    if (!logs.length) {
      $('aiLogTableBody').innerHTML = '<tr><td colspan="7" style="text-align:center;padding:26px" class="mut2">暂无 AI 调用记录（图纸解析时会自动记录流水）</td></tr>';
      return;
    }

    $('aiLogTableBody').innerHTML = logs.map(l => {
      const pTok = (l.prompt_tokens || 0).toLocaleString();
      const cTok = (l.completion_tokens || 0).toLocaleString();
      const cTot = (l.total_cost || 0).toFixed(5);
      const cIn = (l.cost_in || 0).toFixed(5);
      const cOut = (l.cost_out || 0).toFixed(5);
      const model = esc(l.model || 'deepseek-flash');
      const time = esc(l.timestamp || '').slice(5, 19).replace('T', ' ');
      const fname = esc(l.filename || l.job_id || '未知任务');
      const proj = esc(l.project_name || '未分组');
      return `<tr>
        <td class="mono" style="font-size:11px">${time}</td>
        <td><b>${fname}</b> <span class="sub" style="font-size:11px">(${proj})</span></td>
        <td><span class="pill" style="font-size:10.5px">${model}</span></td>
        <td style="text-align:center">${l.calls_count || 1}</td>
        <td class="mono" title="费用: ￥${cIn}">${pTok}</td>
        <td class="mono" title="费用: ￥${cOut}">${cTok}</td>
        <td class="mono" style="font-weight:700;color:var(--acc)">￥${cTot}</td>
      </tr>`;
    }).join('');
  } catch (err) {
    toast('获取 AI 账单失败: ' + err.message);
  }
}

async function handleLogin(e) {
  if (e) e.preventDefault();
  const username = ($('loginUsername').value || '').trim();
  const password = ($('loginPassword').value || '').trim();
  if (!username || !password) {
    toast('请输入账号和密码');
    return;
  }
  const btn = $('btnLoginSubmit');
  if (btn) btn.disabled = true;
  try {
    const res = await postJSON('/api/auth/login', { username, password });
    if (res && res.token) {
      Auth.token = res.token;
      Auth.user = res.user;
      localStorage.setItem('dgzh_auth_token', res.token);
      toast(`欢迎回来，${res.user.display_name || res.user.username}！`);
      closeAuthModal();
      renderUserBadge();
      await loadProjects();
    }
  } catch (err) {
    toast(`登录失败：${err.message || '账号或密码错误'}`);
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function handleRegister(e) {
  if (e) e.preventDefault();
  const tenant_name = ($('regTenant').value || '').trim();
  const username = ($('regUsername').value || '').trim();
  const display_name = ($('regDisplayName').value || '').trim();
  const password = ($('regPassword').value || '').trim();
  if (!tenant_name || !username || !password) {
    toast('请完整填写企业名、账号和密码');
    return;
  }
  const btn = $('btnRegSubmit');
  if (btn) btn.disabled = true;
  try {
    const res = await postJSON('/api/auth/register', {
      tenant_name,
      username,
      display_name: display_name || username,
      password,
    });
    if (res && res.token) {
      Auth.token = res.token;
      Auth.user = res.user;
      localStorage.setItem('dgzh_auth_token', res.token);
      toast(`企业工区【${tenant_name}】创建成功！`);
      closeAuthModal();
      renderUserBadge();
      await loadProjects();
    }
  } catch (err) {
    toast(`注册失败：${err.message || '请重试'}`);
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function doLogout() {
  try {
    await postJSON('/api/auth/logout', {});
  } catch (e) {
    // 忽略登出请求失败
  }
  Auth.token = '';
  Auth.user = null;
  localStorage.removeItem('dgzh_auth_token');
  closeUserDropdown();
  renderUserBadge();
  toast('已安全退出账号');
  await loadProjects();
}

async function doCleanTestData() {
  if (!confirm('⚠️ 警告：该操作将彻底清空当前系统中的全部图纸任务、项目历史、AI 账单记录及临时工作文件，不可恢复！\n\n确认要执行彻底清空吗？')) {
    return;
  }
  try {
    const res = await postJSON('/api/system/clean_test_data', {});
    toast(`系统测试数据已全部清空！清理了 ${res.cleared_jobs || 0} 份图纸任务。`);
    closeUserDropdown();
    if (S.route === 'workbench') {
      S.jobId = null;
      S.job = null;
      navGo('projects');
    }
    await loadProjects();
    const { projects } = await api('/api/projects');
    const hasJobs = (projects || []).some(p => (p.jobs || []).length);
    if (!hasJobs) {
      $('startscreen').hidden = false;
    }
  } catch (err) {
    toast(`清理失败：${err.message || '请重试'}`);
  }
}

async function api(path, options = {}) {
  const opts = { ...options };
  opts.headers = { ...(opts.headers || {}) };
  if (Auth.token) {
    opts.headers['Authorization'] = `Bearer ${Auth.token}`;
  }
  const res = await fetch(path, opts);
  if (!res.ok) {
    if (res.status === 401 && !path.includes('/api/auth/')) {
      Auth.token = '';
      Auth.user = null;
      localStorage.removeItem('dgzh_auth_token');
      renderUserBadge();
    }
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (e) { /* 非 JSON 错误体 */ }
    const err = new Error(detail);
    err.status = res.status;
    throw err;
  }
  return res.status === 204 ? null : res.json();
}

const postJSON = (path, body) => api(path, {
  method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
});
const putJSON = (path, body) => api(path, {
  method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
});

/* ===================== 路由 ===================== */

function navGo(page) {
  const sc = $('startscreen');
  if (sc) sc.hidden = true;
  S.route = page;
  document.querySelectorAll('.page').forEach(m => { m.hidden = m.id !== 'page-' + page; });
  document.querySelectorAll('.nav button').forEach(b => b.classList.toggle('active', b.dataset.p === page));
  if (page === 'projects') loadProjects();
  if (page === 'history') loadHistory();
  if (page === 'settings') loadSettings();
  if (page === 'upload') renderQueue();
  if (page === 'workbench') {
    if (S.jobId) renderWorkbench();
  }
}

/* ===================== 项目页 ===================== */

async function loadProjects() {
  const { projects } = await api('/api/projects');
  const rows = [];
  projects.forEach((p, idx) => {
    const jobs = p.jobs || [];
    const t = p.totals;
    const ready = jobs.find(j => j.status === 'done');
    const names = jobs.map(j => j.filename).join(' / ');
    const sub = jobs.length
      ? `${jobs.length} 份图纸${jobs.length <= 3 ? ' · ' + esc(names) : ''}`
      : '还没有图纸';
    const state = jobs.length === 0
      ? '<span class="pill">空项目</span>'
      : (t.done === jobs.length ? '<span class="pill ok">已完成</span>'
        : (jobs.some(j => j.status === 'failed') ? '<span class="pill bad">提取失败</span>'
          : `<span class="pill info">${t.done}/${jobs.length} 已提取</span>`));

    const costTotal = (p.ai_cost_total || 0).toFixed(4);
    const tokensTotal = p.ai_tokens_total ? `${(p.ai_tokens_total / 1000).toFixed(1)}k` : '0';
    const costCell = `<div style="display:flex;align-items:center;gap:6px">
      <span class="mono" style="font-weight:600;color:var(--txt)">￥${costTotal}</span>
      <span class="mut" style="font-size:11px">(${tokensTotal} tok)</span>
      <button class="linklike" style="padding:1px 5px;font-size:11px" onclick="event.stopPropagation();openProjectAiLogs(decodeURIComponent('${safeJsArg(p.name)}'))">账单</button>
    </div>`;

    const failedJob = jobs.find(j => j.status === 'failed');
    const retryBtn = failedJob
      ? `<span style="color:var(--line2);margin:0 3px">|</span><button class="linklike" style="color:var(--err)" onclick="event.stopPropagation();reparseJob('${failedJob.job_id}')" title="使用最新算法重试失败的图纸">重试</button>`
      : '';
    const action = ready
      ? `<button class="linklike" onclick="event.stopPropagation();toggleProjectDrawer(${idx})">图纸列表(${jobs.length})</button>
         <span style="color:var(--line2);margin:0 3px">|</span>
         <button class="linklike" onclick="event.stopPropagation();viewProjectBom(decodeURIComponent('${safeJsArg(p.name)}'))">总BOM</button>
         <span style="color:var(--line2);margin:0 3px">|</span>
         <button class="linklike" onclick="event.stopPropagation();viewProjectTopology(decodeURIComponent('${safeJsArg(p.name)}'))">供电拓扑</button>
         <span style="color:var(--line2);margin:0 3px">|</span>
         <a class="linklike" href="/api/projects/${encodeURIComponent(p.name)}/export_bom" onclick="event.stopPropagation()" download>导出采购表</a>${retryBtn}`
      : (failedJob
          ? `<button class="linklike" style="color:var(--err)" onclick="event.stopPropagation();reparseJob('${failedJob.job_id}')">重试提取</button>`
          : `<button class="linklike" onclick="event.stopPropagation();navGo('upload')">上传</button>`);
    
    const hasJobs = jobs.length > 0;
    const arrow = hasJobs ? `<span id="proj-arrow-${idx}" style="display:inline-block;width:14px;cursor:pointer;color:var(--mut);transition:transform 0.15s">▶</span> ` : '';
    rows.push(`<tr class="${hasJobs ? 'clickable' : ''}" onclick="toggleProjectDrawer(${idx})">
      <td>${arrow}<b>${esc(p.name)}</b><span class="sub">${sub}</span></td>
      <td>${jobs.length}</td>
      <td>${t.boxes}</td>
      <td>${t.circuits}</td>
      <td>${costCell}</td>
      <td>${t.unresolved ? `<span class="pill warn">${t.unresolved}</span>` : '<span class="pill">0</span>'}</td>
      <td>${state}</td>
      <td class="mono" style="font-size:11px">${esc((p.updated_at || '').replace('T', ' ').slice(0, 16) || '—')}</td>
      <td>${action}</td></tr>`);

    if (hasJobs) {
      const jobRows = jobs.map(j => {
        const jStatus = j.status === 'done' ? '<span class="pill ok" style="font-size:10.5px">已完成</span>'
          : (j.status === 'failed' ? '<span class="pill bad" style="font-size:10.5px">失败</span>' : '<span class="pill info" style="font-size:10.5px">解析中</span>');
        const jBoxes = j.summary?.boxes ?? 0;
        const jCircuits = j.summary?.circuits ?? 0;
        const jComponents = j.summary?.components ?? 0;
        const jPages = j.pages || 1;
        const ext = (j.filename || '').split('.').pop().toUpperCase();
        return `<tr>
          <td style="padding:6px 10px"><span class="pill" style="font-size:10px;margin-right:6px">${ext}</span><b>${esc(j.filename || j.job_id)}</b></td>
          <td style="padding:6px 10px;text-align:center">${jPages} 页</td>
          <td style="padding:6px 10px;text-align:right">${jBoxes} 台</td>
          <td style="padding:6px 10px;text-align:right">${jCircuits} 条</td>
          <td style="padding:6px 10px;text-align:right">${jComponents} 种</td>
          <td style="padding:6px 10px;text-align:center">${jStatus}</td>
          <td style="padding:6px 10px;text-align:right">
            <button class="linklike" onclick="event.stopPropagation();openJob('${j.job_id}')">进入工作台</button>
            <span style="color:var(--line2);margin:0 4px">|</span>
            <button class="linklike" onclick="event.stopPropagation();reparseJob('${j.job_id}')" title="无需重新上传，就地重新提取">重新解析</button>
            <span style="color:var(--line2);margin:0 4px">|</span>
            <button class="linklike" onclick="event.stopPropagation();quickMoveJobProject('${j.job_id}', decodeURIComponent('${safeJsArg(p.name)}'))">换项目</button>
          </td>
        </tr>`;
      }).join('');

      rows.push(`<tr id="proj-drawer-${idx}" style="display:none;background:#f8f9fc">
        <td colspan="9" style="padding:10px 16px 14px 28px;border-top:none">
          <div style="font-size:11.5px;font-weight:600;color:var(--mut);margin-bottom:6px">📂 该工程所辖图纸清单 (${jobs.length} 份)：</div>
          <table class="grid sm" style="width:100%;margin:0;background:#fff;border:1px solid var(--line2)">
            <thead>
              <tr style="background:var(--inset)">
                <th>图纸文件名</th>
                <th style="width:60px;text-align:center">切片页数</th>
                <th style="width:70px;text-align:right">配电箱</th>
                <th style="width:70px;text-align:right">回路数</th>
                <th style="width:70px;text-align:right">元器件</th>
                <th style="width:70px;text-align:center">状态</th>
                <th style="width:170px;text-align:right">操作</th>
              </tr>
            </thead>
            <tbody>${jobRows}</tbody>
          </table>
        </td>
      </tr>`);
    }
  });
  $('projrows').innerHTML = rows.join('');
  $('projempty').hidden = projects.length > 0;
}

async function openProjectAiLogs(projectName) {
  try {
    $('aiLogProjTitle').textContent = `项目 AI 识别费用与 Token 账单 · ${projectName}`;
    $('aiLogSummaryCards').innerHTML = '<span class="mut">正在获取计费记录...</span>';
    $('aiLogTableBody').innerHTML = '<tr><td colspan="7" style="text-align:center;padding:20px" class="mut">加载中...</td></tr>';
    $('modalAiLog').classList.add('show');

    const res = await api(`/api/projects/${encodeURIComponent(projectName)}/ai_logs`);
    const totalCost = (res.ai_cost_total || 0).toFixed(4);
    const costIn = (res.ai_cost_in || 0).toFixed(4);
    const costOut = (res.ai_cost_out || 0).toFixed(4);
    const totalTokens = (res.ai_tokens_total || 0).toLocaleString();
    const promptTokens = (res.ai_prompt_tokens || 0).toLocaleString();
    const compTokens = (res.ai_completion_tokens || 0).toLocaleString();
    const logs = res.ai_logs || [];

    $('aiLogSummaryCards').innerHTML = `
      <div class="stat-card primary">
        <div class="label">累计 AI 识别总费用</div>
        <div class="val">￥${totalCost}</div>
        <div class="subval">共消耗 ${totalTokens} Tokens</div>
      </div>
      <div class="stat-card">
        <div class="label">输入 Token 及费用</div>
        <div class="val" style="font-size:14px">${promptTokens}</div>
        <div class="subval">费用: ￥${costIn}</div>
      </div>
      <div class="stat-card">
        <div class="label">输出 Token 及费用</div>
        <div class="val" style="font-size:14px">${compTokens}</div>
        <div class="subval">费用: ￥${costOut}</div>
      </div>
      <div class="stat-card">
        <div class="label">识别任务流水记录</div>
        <div class="val" style="font-size:14px">${logs.length} 次</div>
        <div class="subval">包含切片并发与全量解析</div>
      </div>
    `;

    if (!logs.length) {
      $('aiLogTableBody').innerHTML = '<tr><td colspan="7" style="text-align:center;padding:24px" class="mut2">该项目暂无 AI 识别计费流水</td></tr>';
      return;
    }

    $('aiLogTableBody').innerHTML = logs.map(l => {
      const dt = (l.timestamp || '').replace('T', ' ').slice(0, 19);
      const cIn = (l.cost_in || 0).toFixed(4);
      const cOut = (l.cost_out || 0).toFixed(4);
      const cTot = (l.total_cost || 0).toFixed(4);
      const pTok = (l.prompt_tokens || 0).toLocaleString();
      const cTok = (l.completion_tokens || 0).toLocaleString();
      return `<tr>
        <td class="mono" style="font-size:11px">${esc(dt)}</td>
        <td><b>${esc(l.filename || l.job_id || '未命名图纸')}</b></td>
        <td class="mono" style="font-size:11px">${esc(l.model || '—')}</td>
        <td style="text-align:center">${l.calls_count || 1}</td>
        <td class="mono" title="费用: ￥${cIn}">${pTok}</td>
        <td class="mono" title="费用: ￥${cOut}">${cTok}</td>
        <td class="mono" style="font-weight:700;color:var(--acc)">￥${cTot}</td>
      </tr>`;
    }).join('');
  } catch (err) {
    toast('获取 AI 账单失败: ' + err.message);
  }
}

function closeAiLogs() {
  const modal = $('modalAiLog');
  if (modal) {
    modal.classList.remove('show');
    modal.hidden = true;
    modal.style.display = 'none';
  }
}

let currentBomProject = '';
let currentBomMode = 'raw';

function switchProjBomMode(mode) {
  viewProjectBom(currentBomProject, mode);
}

async function viewProjectBom(projectName, mode) {
  if (projectName) currentBomProject = projectName;
  if (mode !== undefined) currentBomMode = mode;
  projectName = currentBomProject;
  mode = currentBomMode || 'raw';

  const select = $('projBomModeSelect');
  if (select && select.value !== mode) select.value = mode;

  try {
    $('projBomTitle').textContent = `全项目采购总清单 (BOM) · ${projectName}`;
    $('projBomSub').textContent = mode === 'raw' ? '跨箱体汇总采购统计 (原设计规格)' : `跨箱体一键国产化平替方案 (${mode})`;
    $('projBomStats').innerHTML = '<span class="mut">正在聚合图纸数据...</span>';
    $('projBomTable').innerHTML = '<tr><td colspan="7" style="text-align:center;padding:20px" class="mut">加载中...</td></tr>';
    $('projBomModal').classList.add('show');

    if (mode === 'raw') {
      $('projBomExportBtn').href = `/api/projects/${encodeURIComponent(projectName)}/export_bom`;
      $('projBomExportBtn').textContent = '导出采购四联表 (Excel)';
      const head = $('projBomHead');
      if (head) {
        head.innerHTML = `<tr>
          <th style="width:40px">#</th>
          <th>元器件名称</th>
          <th>规格型号</th>
          <th style="width:60px">单位</th>
          <th style="width:80px">采购总量</th>
          <th>各配电箱分布明细</th>
        </tr>`;
      }
      const res = await api(`/api/projects/${encodeURIComponent(projectName)}/bom`);
      if (!res.ok) throw new Error(res.error || '获取失败');

      $('projBomSub').textContent = `共统计 ${res.job_count} 份图纸 · ${res.box_count} 台配电箱设备`;
      $('projBomStats').innerHTML = `
        <div><b>设备台数：</b><span class="mono">${res.box_count}</span> 台</div>
        <div><b>元器件品种：</b><span class="mono">${res.total_component_items}</span> 种</div>
        <div><b>采购总数量：</b><span class="mono" style="font-weight:700;color:var(--acc)">${res.total_component_quantity}</span></div>
        <div><b>成套辅材测算：</b>已自动核算钣金外壳、主母排/分相铜排、二次线及制造人工工时</div>
      `;

      const comps = res.components || [];
      if (!comps.length) {
        $('projBomTable').innerHTML = '<tr><td colspan="6" style="text-align:center;padding:24px" class="mut2">该项目暂多元器件数据</td></tr>';
        return;
      }

      $('projBomTable').innerHTML = comps.map((c, idx) => {
        const distStr = (c.distribution || []).map(d => `<span class="pill" style="margin-right:4px;font-size:10.5px">${esc(d.box)}: <b>${d.quantity}</b></span>`).join('');
        return `<tr>
          <td class="mono" style="text-align:center">${idx + 1}</td>
          <td><b>${esc(c.name)}</b></td>
          <td class="mono">${esc(c.spec || '—')}</td>
          <td>${esc(c.unit || '只')}</td>
          <td class="mono" style="font-weight:700;color:var(--acc)">${c.total_quantity}</td>
          <td>${distStr || '—'}</td>
        </tr>`;
      }).join('');
    } else {
      // 智能平替模式
      $('projBomExportBtn').href = `/api/projects/${encodeURIComponent(projectName)}/export_bom?target_brand=${encodeURIComponent(mode)}`;
      $('projBomExportBtn').textContent = `导出 ${mode} 平替总表 (Excel)`;
      const head = $('projBomHead');
      if (head) {
        head.innerHTML = `<tr>
          <th style="width:40px">#</th>
          <th>原设计物料 / 品牌</th>
          <th>平替推荐型号 (${mode})</th>
          <th style="width:60px">单位</th>
          <th style="width:80px">采购总量</th>
          <th style="width:75px">降本幅度</th>
          <th>对标核验说明</th>
        </tr>`;
      }
      const res = await api(`/api/projects/${encodeURIComponent(projectName)}/replacements?target_brand=${encodeURIComponent(mode)}`);
      if (!res.ok || !res.analysis) throw new Error(res.error || '获取平替方案失败');
      const an = res.analysis;
      const items = an.items || [];

      $('projBomStats').innerHTML = `
        <div><b>目标品牌：</b><span class="mono" style="font-weight:700;color:var(--c-brand,#0284c7)">${esc(an.target_brand)}</span></div>
        <div><b>物料总数：</b><span class="mono">${an.total_components}</span> 项 / <span class="mono">${an.total_quantity}</span> 件</div>
        <div><b>可平替件数：</b><span class="mono" style="font-weight:700;color:#137333">${an.replaceable_quantity}</span> 件</div>
        <div style="background:#e6f4ea;padding:4px 10px;border-radius:4px;color:#137333;font-weight:700">
          预计元器件采购成本直降约 ${an.estimated_overall_saving_pct}%
        </div>
      `;

      if (!items.length) {
        $('projBomTable').innerHTML = '<tr><td colspan="7" style="text-align:center;padding:24px" class="mut2">暂无可平替元器件</td></tr>';
        return;
      }

      $('projBomTable').innerHTML = items.map((it, idx) => {
        const savingPill = it.estimated_saving_pct > 0
          ? `<span class="pill" style="background:#e6f4ea;color:#137333;font-weight:700">↓${it.estimated_saving_pct}%</span>`
          : `<span class="pill" style="background:var(--inset);color:var(--mut)">已最优</span>`;
        return `<tr>
          <td class="mono" style="text-align:center">${idx + 1}</td>
          <td>
            <b>${esc(it.name)}</b>
            <div class="sub mono" style="font-size:10.5px">${esc(it.original_brand)} · ${esc(it.original_spec || '—')}</div>
          </td>
          <td>
            <div class="mono" style="font-weight:700;color:var(--c-brand,#0284c7)">${esc(it.recommended_model)}</div>
            <div class="sub" style="font-size:10.5px">分布：${esc(it.used_in || '—')}</div>
          </td>
          <td>${esc(it.unit || '只')}</td>
          <td class="mono" style="font-weight:700;color:var(--acc)">${it.quantity}</td>
          <td style="text-align:center">${savingPill}</td>
          <td class="sub" style="font-size:11px;max-width:180px;line-height:1.3">${esc(it.notes || '—')}</td>
        </tr>`;
      }).join('');
    }
  } catch (err) {
    toast('获取项目总 BOM 失败：' + err.message);
    closeProjBom();
  }
}

function closeProjBom() {
  const modal = $('projBomModal');
  if (modal) modal.classList.remove('show');
}

async function newProject() {
  const name = prompt('项目名称，例如：万达广场 · 强电');
  if (!name || !name.trim()) return;
  await postJSON('/api/projects', { name: name.trim() });
  toast('已新建项目：' + name.trim());
  loadProjects();
  fillProjectSelect();
}

function toggleProjectDrawer(idx) {
  const drawer = $(`proj-drawer-${idx}`);
  const arrow = $(`proj-arrow-${idx}`);
  if (!drawer) return;
  const isHidden = drawer.style.display === 'none';
  drawer.style.display = isHidden ? 'table-row' : 'none';
  if (arrow) {
    arrow.textContent = isHidden ? '▼' : '▶';
  }
}

let currentTopologyData = null;

async function viewProjectTopology(projectName) {
  try {
    $('projTopologyTitle').textContent = `全项目供电系统层级拓扑树 · ${projectName}`;
    $('projTopologySub').textContent = `跨所有系统图合并分析：一级总配电柜 → 二级配电分箱 → 一次末端回路 / 二次原理图控制关系`;
    $('projTopologyStats').innerHTML = '<span class="mut">正在聚合全项目配电系统拓扑网络...</span>';
    $('projTopologyContainer').innerHTML = '<div style="text-align:center;padding:30px" class="mut">正在计算拓扑关系...</div>';
    $('projTopologyFilter').value = '';
    $('projTopologyModal').classList.add('show');

    const res = await api(`/api/projects/${encodeURIComponent(projectName)}/topology`);
    if (!res.ok) throw new Error(res.error || '获取拓扑失败');
    currentTopologyData = res.topology || [];

    let totalCabinets = 0;
    let totalSecondaries = 0;
    let totalCircuits = 0;
    function countNodes(list) {
      for (const n of list) {
        if (n.node_type === 'secondary') totalSecondaries++;
        else totalCabinets++;
        totalCircuits += (n.circuits_count || 0);
        if (n.children && n.children.length) countNodes(n.children);
      }
    }
    countNodes(currentTopologyData);

    $('projTopologyStats').innerHTML = `
      <div><b>覆盖图纸：</b><span class="mono">${res.job_count}</span> 份</div>
      <div><b>配电箱/柜节点：</b><span class="mono">${totalCabinets}</span> 台</div>
      <div><b>二次控制原理图：</b><span class="mono">${totalSecondaries}</span> 幅</div>
      <div><b>聚合总回路：</b><span class="mono" style="font-weight:700;color:var(--acc)">${totalCircuits}</span> 条</div>
      <div style="color:var(--mut)">提示：供电关系已根据系统图一次进线、出线回路及箱体编号自动关联拓扑层级</div>
    `;

    renderTopologyView('');
  } catch (err) {
    toast('获取项目拓扑失败：' + err.message);
    closeProjTopology();
  }
}

function renderTopologyView(filter) {
  const container = $('projTopologyContainer');
  if (!container) return;
  const list = currentTopologyData || [];
  if (!list.length) {
    container.innerHTML = '<div style="text-align:center;padding:30px" class="mut2">该项目暂未发现配电箱或拓扑关联数据</div>';
    return;
  }

  const q = (filter || '').trim().toLowerCase();
  
  function renderNode(node) {
    const isSec = node.node_type === 'secondary';
    const match = !q || (node.code && node.code.toLowerCase().includes(q)) || (node.name && node.name.toLowerCase().includes(q));
    const childHtml = (node.children || []).map(renderNode).join('');
    if (q && !match && !childHtml) return '';

    const icon = isSec ? '⚡' : '🗄️';
    const typeBadge = isSec ? '<span class="pill warn" style="font-size:10px;margin-right:4px">二次原理图</span>'
      : '<span class="pill ok" style="font-size:10px;margin-right:4px">箱柜</span>';
    const power = node.power_kw ? `<span class="pill" style="font-size:10.5px;background:#eef2ff;color:#2e5ce6">功率: ${esc(node.power_kw)}</span>` : '';
    const circuits = node.circuits_count ? `<span class="pill" style="font-size:10.5px">出线回路: ${node.circuits_count}条</span>` : '';
    const note = node.note ? `<span class="mut" style="font-size:11px">(${esc(node.note)})</span>` : '';

    return `
      <div style="margin-left:20px;border-left:2px solid var(--line2);padding-left:14px;margin-top:8px;margin-bottom:8px">
        <div style="display:inline-flex;align-items:center;gap:6px;background:#fff;border:1px solid var(--line);border-radius:6px;padding:6px 12px;box-shadow:0 1px 2px rgba(0,0,0,0.03)">
          <span style="font-size:14px">${icon}</span>
          ${typeBadge}
          <b style="font-size:12.5px">${esc(node.code || '未编号')}</b>
          <span style="color:var(--txt);font-size:12px">${esc(node.name || '')}</span>
          ${power}
          ${circuits}
          ${note}
        </div>
        ${childHtml ? `<div style="margin-top:4px">${childHtml}</div>` : ''}
      </div>
    `;
  }

  const html = list.map(renderNode).join('');
  container.innerHTML = html || '<div style="text-align:center;padding:24px" class="mut2">未匹配到符合过滤条件的配电箱</div>';
}

function filterTopologyTree(val) {
  renderTopologyView(val);
}

function closeProjTopology() {
  const modal = $('projTopologyModal');
  if (modal) modal.classList.remove('show');
}

let batchJobsCache = [];

async function openBatchProjectModal(preSelectedJobId) {
  try {
    $('batchProjectModal').classList.add('show');
    $('batchProjectTable').innerHTML = '<tr><td colspan="6" style="text-align:center;padding:20px" class="mut">加载图纸列表中...</td></tr>';
    
    const [{ projects }, { jobs }] = await Promise.all([
      api('/api/projects'),
      api('/api/jobs')
    ]);

    batchJobsCache = jobs || [];

    const sel = $('batchTargetProject');
    sel.innerHTML = '<option value="">-- 选择已有项目 --</option>' +
      projects.filter(p => p.name !== '未分组').map(p => `<option value="${esc(p.name)}">${esc(p.name)} (${p.totals?.drawings || 0} 份图纸)</option>`).join('');
    $('batchNewProject').value = '';

    const rows = batchJobsCache.map(j => {
      const isDefaultChecked = preSelectedJobId ? (j.job_id === preSelectedJobId) : (!j.project || j.project === '未分组');
      const ext = (j.filename || '').split('.').pop().toUpperCase();
      const statusPill = j.status === 'done' ? '<span class="pill ok" style="font-size:10px">已完成</span>'
        : (j.status === 'failed' ? '<span class="pill bad" style="font-size:10px">失败</span>' : '<span class="pill info" style="font-size:10px">处理中</span>');
      return `<tr>
        <td style="text-align:center"><input type="checkbox" class="batch-chk" data-jobid="${j.job_id}" ${isDefaultChecked ? 'checked' : ''}></td>
        <td><span class="pill" style="font-size:10px;margin-right:4px">${ext}</span><b>${esc(j.filename || j.job_id)}</b></td>
        <td><span class="mut" style="font-size:11px">${esc(j.project || '未分组')}</span></td>
        <td style="text-align:right">${j.summary?.boxes ?? 0}</td>
        <td style="text-align:right">${j.summary?.circuits ?? 0}</td>
        <td style="text-align:center">${statusPill}</td>
      </tr>`;
    });

    $('batchProjectTable').innerHTML = rows.join('') || '<tr><td colspan="6" style="text-align:center;padding:20px" class="mut2">暂无已上传图纸</td></tr>';
    $('batchSelectAll').checked = false;
  } catch (err) {
    toast('打开批量归类失败：' + err.message);
  }
}

function closeBatchProjectModal() {
  const modal = $('batchProjectModal');
  if (modal) modal.classList.remove('show');
}

function toggleSelectAllBatch(checked) {
  document.querySelectorAll('.batch-chk').forEach(el => el.checked = checked);
}

async function submitBatchProject() {
  const selectedBoxes = Array.from(document.querySelectorAll('.batch-chk:checked'));
  const jobIds = selectedBoxes.map(el => el.getAttribute('data-jobid')).filter(Boolean);
  if (!jobIds.length) {
    toast('请先勾选需要归入项目的图纸');
    return;
  }

  let targetProj = $('batchNewProject').value.trim();
  if (!targetProj) {
    targetProj = $('batchTargetProject').value.trim();
  }
  if (!targetProj) {
    toast('请选择已有项目或输入新项目名称');
    return;
  }

  try {
    const res = await postJSON('/api/jobs/batch_set_project', {
      job_ids: jobIds,
      project: targetProj
    });
    if (!res.ok) throw new Error(res.error || '移动失败');
    toast(`已成功将 ${res.updated} 份图纸归入工程「${targetProj}」`);
    closeBatchProjectModal();
    loadProjects();
    fillProjectSelect();
  } catch (err) {
    toast('归入项目失败：' + err.message);
  }
}

async function quickMoveJobProject(jobId, currentProject) {
  const target = prompt(`将图纸移动到哪个项目？当前项目：${currentProject || '未分组'}`, currentProject || '');
  if (!target || !target.trim()) return;
  try {
    const res = await postJSON('/api/jobs/batch_set_project', {
      job_ids: [jobId],
      project: target.trim()
    });
    if (!res.ok) throw new Error(res.error || '移动失败');
    toast(`已将图纸移动到「${target.trim()}」`);
    loadProjects();
    fillProjectSelect();
  } catch (err) {
    toast('移动失败：' + err.message);
  }
}

/* ===================== 上传页 ===================== */

function renderQueue() {
  const tb = $('upqueue');
  if (!S.queue.length) {
    tb.innerHTML = '<tr><td colspan="4" class="mut2" style="text-align:center;padding:22px">队列为空</td></tr>';
    return;
  }
  tb.innerHTML = S.queue.map((f, i) => `<tr>
    <td class="mono">${esc(f.name)}</td><td>${fmtSize(f.size)}</td>
    <td>${f.state === 'done'
      ? '<span class="pill ok">提取完成</span>'
      : f.state === 'failed' ? `<span class="pill bad">失败</span><span class="sub" style="color:var(--bad,#e55353);display:block;margin-top:4px;white-space:pre-wrap;line-height:1.4">${esc(f.error || '')}</span>`
      : f.state === 'uploading' || f.state === 'extracting'
        ? `<span class="pill info">${f.state === 'uploading' ? '上传中' : '提取中'} ${f.progress}%</span>
           ${f.note ? `<span class="sub" style="display:block;margin-top:3px">${esc(f.note)}</span>` : ''}
           <div class="progress"><i style="width:${f.progress}%"></i></div>`
        : '<span class="pill">等待提取</span>'}</td>
    <td>${f.jobId && f.state === 'done' ? `<button class="linklike" onclick="openJob('${f.jobId}')">进入工作台</button>`
      : f.state === 'failed'
        ? `<button class="linklike" style="color:var(--acc);margin-right:10px;font-weight:600" onclick="retryQueued(${i})">重试</button><button class="linklike" onclick="removeQueued(${i})">移除</button>`
        : `<button class="linklike" onclick="removeQueued(${i})">移除</button>`}</td></tr>`).join('');
}

function retryQueued(i) {
  const item = S.queue[i];
  if (!item) return;
  item.state = 'queued';
  item.error = '';
  item.progress = 0;
  renderQueue();
  startExtract();
}

function fmtSize(n) {
  if (n > 1048576) return (n / 1048576).toFixed(1) + ' MB';
  if (n > 1024) return (n / 1024).toFixed(0) + ' KB';
  return n + ' B';
}

function addFiles(list) {
  [...list].forEach(f => {
    if (!/\.(pdf|dwg|dxf)$/i.test(f.name)) { toast('仅支持 PDF、DWG 或 DXF：' + f.name); return; }
    S.queue.push({ file: f, name: f.name, size: f.size, state: 'queued', progress: 0 });
  });
  if (S.route !== 'upload') navGo('upload');
  renderQueue();
}

function removeQueued(i) {
  S.queue.splice(i, 1);
  renderQueue();
}

async function startExtract() {
  let pending = S.queue.filter(f => f.state === 'queued');
  if (!pending.length) {
    const failed = S.queue.filter(f => f.state === 'failed');
    if (failed.length) {
      failed.forEach(f => { f.state = 'queued'; f.error = ''; f.progress = 0; });
      renderQueue();
      pending = failed;
    } else {
      toast('队列里没有等待提取的文件');
      return;
    }
  }
  const project = $('uploadProject').value;
  for (const item of pending) {
    try {
      item.state = 'uploading';
      item.progress = 20;
      renderQueue();
      const fd = new FormData();
      fd.append('file', item.file);
      fd.append('project', project);
      const { job_id } = await api('/api/jobs', { method: 'POST', body: fd });
      item.jobId = job_id;
      item.state = 'extracting';
      item.progress = 35;
      renderQueue();
      await pollJob(item);
    } catch (e) {
      item.state = 'failed';
      item.error = e.message;
      renderQueue();
    }
  }
  fillProjectSelect();
}

async function pollJob(item) {
  const stages = { converting: 20, rendering: 40, extracting: 60, building_excel: 90 };
  for (let i = 0; i < 600; i++) {
    await new Promise(r => setTimeout(r, 1500));
    let job;
    try {
      job = await api(`/api/jobs/${item.jobId}`);
    } catch (e) {
      item.state = 'failed';
      item.error = e.message;
      item.note = '';
      renderQueue();
      return;
    }
    const base = stages[job.status] || 35;
    if (!item.progress || item.progress < base) {
      item.progress = base;
    }
    if (job.status === 'extracting' && typeof job.progress === 'number' && job.progress > 0) {
      item.progress = Math.max(item.progress, job.progress);
      item.note = job.pages > 1 ? `第 ${job.current_page || 1}/${job.pages} 页` : '正在调用视觉模型识别回路与元器件...';
    } else if (job.status === 'converting') {
      item.note = '正在进行 CAD 矢量转码与图纸分析...';
    } else if (job.status === 'rendering') {
      item.note = '正在高保真光栅化页面与图纸切片...';
    } else if (job.status === 'building_excel') {
      item.note = '正在生成核对 Excel 与 BOM 汇总...';
    }
    if (job.status === 'done') {
      item.state = 'done';
      item.progress = 100;
      item.note = '';
      renderQueue();
      toast('提取完成，可进入工作台核对');
      return;
    }
    if (job.status === 'failed') {
      item.state = 'failed';
      item.error = job.error || '提取失败';
      item.note = '';
      renderQueue();
      return;
    }
    // 当前阶段无细粒度回调时平滑爬升，但不突破该阶段上限
    const nextStageCap = job.status === 'converting' ? 38 : (job.status === 'rendering' ? 58 : 88);
    if (item.progress < nextStageCap) {
      item.progress = item.progress + 1;
    }
    renderQueue();
  }
  item.state = 'failed';
  item.error = '等待超时，请在项目页查看任务状态';
  item.note = '';
  renderQueue();
}

async function fillProjectSelect() {
  const sel = $('uploadProject');
  if (!sel) return;
  const { projects } = await api('/api/projects');
  const keep = sel.value;
  sel.innerHTML = '<option value="">未分组</option>' +
    projects.map(p => `<option value="${esc(p.name)}">${esc(p.name)}</option>`).join('');
  if ([...sel.options].some(o => o.value === keep)) sel.value = keep;
}

/* ===================== 工作台：加载与渲染 ===================== */

async function openJob(jobId) {
  try {
    const job = await api(`/api/jobs/${jobId}`);
    S.jobId = jobId;
    S.job = job;
    S.data = job.data || { boxes: [], circuits: [], components: [], requirements: [], uncertainties: [] };
    S.changes = job.changes || [];
    snapshotOrigins();
    S.pages = Math.max(1, job.pages || 1);
    S.sheetNames = job.sheet_names || {};
    S._catalogOpenedOnce = false;
    S.page = 1;
    S.zoom = 1;
    S.chat = [];
    S.reviewIdx = 0;
    hideStart();
    navGo('workbench');
    renderWorkbench();
    addMsg('ai', welcome(job));
  } catch (e) {
    toast('打开失败：' + e.message);
  }
}

async function reparseJob(jobId) {
  if (!confirm('确定要基于服务器已保存的原图纸重新执行解析与提取吗？\n（将以最新规则刷新结果，完全无需重新上传文件）')) return;
  try {
    toast('正在启动重新解析…');
    const res = await api(`/api/jobs/${jobId}/reparse`, { method: 'POST' });
    toast(res.message || '已成功启动重新解析');
    if (S.jobId === jobId) {
      // 保持在当前工作台，轮询最新进度
      const pollTimer = setInterval(async () => {
        try {
          const j = await api(`/api/jobs/${jobId}`);
          S.job = j;
          renderWorkbench();
          if (j.status === 'done') {
            clearInterval(pollTimer);
            openJob(jobId);
            toast('图纸重新解析完成！');
          } else if (j.status === 'failed') {
            clearInterval(pollTimer);
            toast('重新解析失败: ' + (j.error || '未知错误'));
          }
        } catch (_) {}
      }, 2000);
    } else {
      if (S.route === 'projects') loadProjects();
      if (S.route === 'history') loadHistory();
    }
  } catch (err) {
    toast('重新解析请求失败: ' + err.message);
  }
}

function welcome(job) {
  const t = job.summary || {};
  const parts = [];
  if (t.circuits) parts.push(`${t.circuits} 条回路`);
  if (t.components) parts.push(`${t.components} 项器件`);
  const un = unresolved().length;
  if (un) parts.push(`${un} 处待核对`);
  const head = `已载入 <b>${esc(job.filename || '')}</b>${parts.length ? '：' + parts.join('、') : ''}。`;
  return head + '可以直接问清单里的内容，例如“WL1 用的是什么断路器”“统计断路器总数”。';
}

function modelMeta(job) {
  const m = (job.summary && job.summary.meta) || {};
  return [m.model || '未记录', 'v' + (m.prompt_version || '-'), 'v' + (m.contract_version || '-')];
}

function updateWorkbenchCrumb() {
  const job = S.job;
  if (!job) return;
  const box = (S.data.boxes || [])[0];
  const curSheetName = (S.sheetNames && (S.sheetNames[S.page] || S.sheetNames[String(S.page)])) || '';
  const sheetTag = curSheetName ? `<span style="background:var(--acc-t);color:var(--acc-d);padding:2px 7px;border-radius:2px;font-weight:600;margin:0 4px">${esc(curSheetName)}</span>` : '';
  const reparseBtn = `<button class="linklike" style="margin-left:12px;font-size:11px;padding:2px 7px;border:1px solid var(--line2);border-radius:3px;background:var(--card)" onclick="reparseJob('${S.jobId}')" title="无需重新上传，以最新引擎与提取规则重新解析本图纸">🔄 重新提取</button>`;
  $('crumb').innerHTML = `<b>${esc(job.filename || '')}</b>　/　${sheetTag}${box ? esc(box.name || '配电箱') + ' ' + esc(box.code || '') : '未识别箱体'}　<span class="mut2">· 第 ${S.page}/${S.pages} 块</span>${reparseBtn}`;
}

function renderWorkbench() {
  const job = S.job;
  if (!job) return;
  updateWorkbenchCrumb();

  const dot = $('aistatedot'), txt = $('aistatetext'), meta = $('aistatemeta');
  dot.className = 'dot';
  if (job.status === 'failed') {
    dot.classList.add('bad'); txt.textContent = '提取失败';
    meta.textContent = job.error ? ' · ' + job.error : '';
  } else if (job.status !== 'done') {
    dot.classList.add('busy'); txt.textContent = '处理中…'; meta.textContent = ' · ' + job.status;
  } else {
    const [model, prompt, contract] = modelMeta(job);
    txt.textContent = '提取完成';
    meta.textContent = ` · ${model} · 契约 ${contract} · ${(job.created_at || '').replace('T', ' ').slice(5, 16)}`;
  }

  const [model, prompt, contract] = modelMeta(job);
  $('verline').textContent = `模型 ${model} · 提示词 ${prompt} · 契约 ${contract}`;
  $('sb-left').textContent = job.created_at ? `提取于 ${job.created_at.replace('T', ' ')}` : '未打开图纸';
  $('sb-mid').textContent = '回路是唯一可编辑来源，保存后元器件汇总按回路重新生成';

  renderDrawing();
  renderSheetCatalog();
  applyAiWidth(S.aiWidth);
  renderBoxstrip();
  renderSeg();
  renderSub();
  renderReview();
  updateChg();
  updateBadge();
}

function renderDrawing() {
  const img = $('dwg');
  const zones = $('zones');
  $('canvasEmpty').hidden = true;
  $('stage').hidden = false;
  img.src = `/api/jobs/${S.jobId}/page/${S.page}`;
  $('ppct').textContent = `${S.page}/${S.pages}`;
  $('prevpage').disabled = S.page <= 1;
  $('nextpage').disabled = S.page >= S.pages;
  applyZoom();

  const located = (S.data.circuits || []).filter(c => c.bbox && c.bbox.w >= 0.01 && c.bbox.h >= 0.01);
  zones.innerHTML = located.map(c => {
    const r = c.bbox;
    const i = (S.data.circuits || []).indexOf(c);
    return `<div class="clickzone" data-cid="c${i}"
      style="left:${r.x * 100}%;top:${r.y * 100}%;width:${r.w * 100}%;height:${r.h * 100}%"
      onclick="selectCircuit('c${i}',false)" title="${esc(circuitLabel(c) + ' ' + (c.breaker || ''))}"></div>`;
  }).join('');

  const btn = $('linkbtn');
  btn.classList.toggle('on', S.link);
  if (!located.length) {
    btn.disabled = true;
    btn.title = '这份提取结果没有回路坐标，无法在图纸上定位';
    $('sb-mid').textContent = '该图纸未取得回路坐标，只做表格核对（数据不受影响）';
  } else {
    btn.disabled = false;
    btn.title = `点表格行在图纸上定位（${located.length} 条回路有坐标）`;
    $('sb-mid').textContent = '保存后元器件汇总按回路重新生成';
  }
}

function onDrawingError() {
  $('stage').hidden = true;
  $('canvasEmpty').hidden = false;
  $('canvasEmpty').innerHTML = '图纸页面图还没生成<br><span class="mut2">稍后再试，或回项目页查看任务状态</span>';
}

function applyZoom() {
  const stage = $('stage');
  if (!stage) return;
  stage.style.transform = `translate(${S.panX || 0}px, ${S.panY || 0}px) scale(${S.zoom})`;
  stage.style.transformOrigin = 'center';
  $('zpct').textContent = S.zoom === 1 ? '适应' : Math.round(S.zoom * 100) + '%';
}

function zoom(d) {
  if (!S.jobId) { toast('请先在项目或上传中打开一份图纸'); return; }
  S.zoom = Math.min(3, Math.max(0.5, S.zoom + d * 0.25));
  applyZoom();
}

function fitView() {
  if (!S.jobId) { toast('请先在项目或上传中打开一份图纸'); return; }
  S.zoom = 1;
  S.panX = 0;
  S.panY = 0;
  applyZoom();
}

function setPage(n) {
  if (!S.jobId) { toast('请先在项目或上传中打开一份图纸'); return; }
  S.page = Math.min(Math.max(1, n), S.pages);
  renderDrawing();
  updateWorkbenchCrumb();
  updateSheetCatalogActive();
}

function toggleSheetCatalog(force) {
  if (!S.jobId) { toast('请先在项目或上传中打开一份图纸'); return; }
  const cat = $('sheetCatalog');
  const btn = $('sheetCatalogTabBtn');
  if (!cat) return;
  const isCollapsed = cat.classList.contains('collapsed');
  const next = force !== undefined ? !force : isCollapsed;
  cat.classList.toggle('collapsed', !next);
  if (btn) btn.style.display = next ? 'none' : 'inline-flex';
}

function updateSheetCatalogActive() {
  document.querySelectorAll('.sheet-item').forEach(el => {
    const p = parseInt(el.dataset.page, 10);
    el.classList.toggle('active', p === S.page);
    if (p === S.page) {
      el.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }
  });
}

function renderSheetCatalog() {
  const list = $('sheetCatalogList');
  if (!list) return;
  const countSpan = $('sheetCount');
  const badgeSpan = $('scTotalBadge');
  if (countSpan) countSpan.textContent = S.pages;
  if (badgeSpan) badgeSpan.textContent = `共 ${S.pages} 块`;

  const cat = $('sheetCatalog');
  if (cat && S.pages > 1 && !S._catalogOpenedOnce) {
    cat.classList.remove('collapsed');
    const tabBtn = $('sheetCatalogTabBtn');
    if (tabBtn) tabBtn.style.display = 'none';
    S._catalogOpenedOnce = true;
  }

  const circuits = S.data.circuits || [];
  const boxes = S.data.boxes || [];

  let html = '';
  for (let p = 1; p <= S.pages; p++) {
    const sheetName = (S.sheetNames && (S.sheetNames[p] || S.sheetNames[String(p)])) || `切图图块 ${p}`;
    const pageCircuits = circuits.filter(c => ((c.bbox && c.bbox.page) || 1) === p);
    const pageBoxes = [...new Set(pageCircuits.map(c => c.box).filter(Boolean))];
    const boxLabel = pageBoxes.length ? pageBoxes.join(' / ') : (boxes[0] ? boxes[0].code : '箱体');
    const circuitSamples = pageCircuits.slice(0, 3).map(c => c.circuit_no || c.load_name).filter(Boolean).join('、');

    html += `
      <div class="sheet-item ${p === S.page ? 'active' : ''}" data-page="${p}" onclick="setPage(${p})">
        <div class="si-top">
          <div class="si-thumb">
            <img src="/api/jobs/${S.jobId}/page/${p}" alt="图块 ${p}" loading="lazy" onerror="this.style.display='none'">
          </div>
          <div class="si-info">
            <div class="si-title-row">
              <span class="si-title" id="sheet-title-${p}" title="${esc(sheetName)}">${esc(sheetName)}</span>
              <button class="si-rename-btn" onclick="editSheetName(event, ${p})" title="自定义重命名图块">✏️</button>
            </div>
            <div class="si-meta">
              <span class="si-tag mono">#${p}</span>
              <span class="si-tag">${esc(boxLabel)}</span>
              <span>${pageCircuits.length} 条回路</span>
            </div>
          </div>
        </div>
        <div class="si-actions">
          <span class="si-circuits-hint" title="${esc(circuitSamples || '回路列表')}">${circuitSamples ? '包含: ' + esc(circuitSamples) : '暂无回路标注'}</span>
          <button class="si-zoom-btn" onclick="zoomSheet(event, ${p})" title="切换至此图并放大至 175% 细节查看">🔍 放大查看</button>
        </div>
      </div>
    `;
  }
  list.innerHTML = html;
}

function filterSheetCatalog() {
  const query = ($('sheetSearchInput')?.value || '').trim().toLowerCase();
  document.querySelectorAll('.sheet-item').forEach(el => {
    const text = el.textContent.toLowerCase();
    el.style.display = (!query || text.includes(query)) ? '' : 'none';
  });
}

async function editSheetName(evt, page) {
  if (evt) evt.stopPropagation();
  const currentName = (S.sheetNames && (S.sheetNames[page] || S.sheetNames[String(page)])) || `切图图块 ${page}`;
  const newName = prompt(`请输入第 ${page} 块切图的自定义名称：`, currentName);
  if (!newName || !newName.trim() || newName.trim() === currentName) return;

  try {
    const res = await postJSON(`/api/jobs/${S.jobId}/rename_sheet`, {
      page: page,
      name: newName.trim(),
    });
    if (res.ok) {
      if (!S.sheetNames) S.sheetNames = {};
      S.sheetNames[String(page)] = newName.trim();
      S.sheetNames[page] = newName.trim();
      if (S.job) S.job.sheet_names = S.sheetNames;
      const titleEl = document.getElementById(`sheet-title-${page}`);
      if (titleEl) {
        titleEl.textContent = newName.trim();
        titleEl.title = newName.trim();
      }
      updateWorkbenchCrumb();
      toast(`切图 #${page} 已重命名为：${newName.trim()}`);
    }
  } catch (err) {
    toast('重命名失败: ' + err.message);
  }
}

function zoomSheet(evt, page) {
  if (evt) evt.stopPropagation();
  setPage(page);
  S.zoom = 1.75;
  applyZoom();
  toast(`已切换至图块 #${page} 并放大至 175%`);
}

function toggleLink() {
  if ($('linkbtn').disabled) return;
  S.link = !S.link;
  $('linkbtn').classList.toggle('on', S.link);
  if (!S.link) { $('hlbox').innerHTML = ''; document.querySelectorAll('#tb tr.sel').forEach(t => t.classList.remove('sel')); }
  toast(S.link ? '图-表联动已开启' : '图-表联动已关闭');
}

/* 多条备用回路没有编号，名称会重复，因此界面上一律用数组下标当身份，
   显示才用 circuitLabel。下标在保存前后稳定：组装是稳定排序，顺序不会变。 */
const circuitLabel = c => c.circuit_no || c.load_name || '(未编号)';

function renderBoxstrip() {
  const box = (S.data.boxes || [])[0];
  const el = $('boxstrip');
  if (!box) { el.hidden = true; return; }
  el.hidden = false;
  const note = /深化/.test(box.size || '') || /深化/.test(box.note || '');

  // 实时三相负荷与平衡度核验
  const circuits = S.data.circuits || [];
  let pL1 = 0, pL2 = 0, pL3 = 0, hasLoads = false;
  circuits.forEach(c => {
    const p = (c.phase || '').toUpperCase().trim();
    const kw = parseFloat(c.power_kw) || 0;
    if (kw > 0) {
      if (p === 'L1') { pL1 += kw; hasLoads = true; }
      else if (p === 'L2') { pL2 += kw; hasLoads = true; }
      else if (p === 'L3') { pL3 += kw; hasLoads = true; }
      else if (['L123', '3P', '3PH', 'L1,L2,L3'].includes(p)) {
        pL1 += kw / 3; pL2 += kw / 3; pL3 += kw / 3; hasLoads = true;
      }
    }
  });

  let balanceHtml = '';
  if (hasLoads) {
    const maxP = Math.max(pL1, pL2, pL3);
    const minP = Math.min(pL1, pL2, pL3);
    if (maxP > 1.0 && pL1 > 0 && pL2 > 0 && pL3 > 0) {
      const unbalance = ((maxP - minP) / maxP) * 100;
      const isBad = unbalance > 15.0;
      balanceHtml = `<span class="bs-pill ${isBad ? 'warn' : 'ok'}" title="三相负荷平衡度：国标要求相间不平衡度 ≤15%">三相平衡度: ${unbalance.toFixed(1)}% ${isBad ? '⚠ 偏载' : '✓ 均衡'} (L1:${pL1.toFixed(1)} / L2:${pL2.toFixed(1)} / L3:${pL3.toFixed(1)} kW)</span>`;
    } else {
      balanceHtml = `<span class="bs-pill ok">三相负荷: L1:${pL1.toFixed(1)} / L2:${pL2.toFixed(1)} / L3:${pL3.toFixed(1)} kW</span>`;
    }
  }

  el.innerHTML = `<div class="bs-h">
      <div style="display:flex;align-items:center;flex-wrap:wrap;gap:4px">
        <span>箱体 · ${esc(box.code || '')}</span>
        ${balanceHtml}
      </div>
      ${note ? '<span class="bs-warn">参考尺寸以厂家深化为准</span>' : ''}</div>
    <dl>
      <div><dt>设备名称</dt><dd>${esc(box.name || '—')}</dd></div>
      <div><dt>防护等级</dt><dd>${esc(box.ip_rating || '—')}</dd></div>
      <div><dt>参考尺寸</dt><dd class="mono">${esc(box.size || '—')}</dd></div>
      <div><dt>安装方式</dt><dd>${esc(box.install || '—')}</dd></div>
      <div><dt>安装位置</dt><dd>${esc(box.location || '—')}</dd></div>
      <div><dt>数量</dt><dd><b>${box.quantity ?? 1}</b> 台</dd></div>
    </dl>`;
}

const SUBS = [
  ['boxes', '箱体', () => (S.data.boxes || []).length],
  ['circuits', '回路', () => (S.data.circuits || []).length],
  ['devices', '器件', () => (S.data.components || []).length],
  ['replace', '降本平替', () => (S.data.components || []).length],
  ['reqs', '技术要求', () => (S.data.requirements || []).length],
];

function renderSeg() {
  $('seg').innerHTML = SUBS.map(([k, label, count]) =>
    `<button data-t="${k}" class="${S.sub === k ? 'active' : ''}" onclick="renderSub('${k}')">${label}<span class="n">${count()}</span></button>`).join('');
}

const EDITABLE_CIRCUIT = ['phase', 'breaker', 'cable', 'power_kw', 'load_name'];
const EDITABLE_BOX = ['name', 'ip_rating', 'install', 'location', 'size', 'quantity', 'note'];
const EDITABLE_DEVICE = ['name', 'spec', 'unit', 'quantity', 'used_in', 'note'];

/** 可编辑的三类事实。回路之外，箱体和非回路设备（SPD / 电表 / 铜排）也必须能人工补录，
 *  否则模型漏一项就只能在 Excel 里改，核对闭环是断的。 */
const EDIT_SETS = {
  circuit: { list: () => S.data.circuits || [], orig: () => S.origCircuits, fields: EDITABLE_CIRCUIT,
             label: i => circuitLabel((S.data.circuits || [])[i] || {}) },
  box: { list: () => S.data.boxes || [], orig: () => S.origBoxes, fields: EDITABLE_BOX,
         label: i => ((S.data.boxes || [])[i] || {}).code || `箱体 ${i + 1}` },
  device: { list: () => S.data.extra_devices || [], orig: () => S.origExtras, fields: EDITABLE_DEVICE,
            label: i => (((S.data.extra_devices || [])[i]) || {}).name || `设备 ${i + 1}` },
};

const keyOfScope = {
  // 回路不能拿编号当身份：多条备用回路没有编号，只能按位置比对
  circuit: null,
  box: b => b.code,
  device: d => `${d.name}|${d.spec || ''}`,
};

/** 取出某行对应的原值行。
 *  箱体与设备用 identity 对，不能只按位置：删掉一条后后面的索引会整体前移，
 *  基于位置的比对会把两条不同设备比在一起，生成一堆假修改记录。 */
function origRow(scope, i) {
  const set = EDIT_SETS[scope];
  if (!set) return undefined;
  const orig = set.orig();
  const keyOf = keyOfScope[scope];
  const row = set.list()[i];
  if (!keyOf || !row) return orig[i];
  const byIndex = orig[i];
  if (byIndex && keyOf(byIndex) === keyOf(row)) return byIndex;
  return orig.find(o => keyOf(o) === keyOf(row));
}

function scopeChanged(scope, i, field) {
  const set = EDIT_SETS[scope];
  if (!set) return false;
  const before = (origRow(scope, i) || {})[field];
  const now = (set.list()[i] || {})[field];
  return String(before ?? '') !== String(now ?? '');
}

function editableCell(scope, i, field, inner, mono) {
  const changed = scopeChanged(scope, i, field);
  const before = (origRow(scope, i) || {})[field];
  const title = changed ? '已修改，原值：' + esc(before) : '双击编辑';
  const cls = `ed${mono ? ' mono' : ''}${changed ? ' changed' : ''}`;
  return `<td class="${cls}" data-scope="${scope}" data-idx="${i}" data-f="${field}" title="${title}">${inner}</td>`;
}

function bindCells() {
  document.querySelectorAll('td.ed').forEach(td => {
    td.ondblclick = e => { e.stopPropagation(); startEdit(td); };
  });
}

function renderSub(sub) {
  if (sub) S.sub = sub;
  renderSeg();
  const th = $('th'), tb = $('tb');
  const d = S.data;

  if (S.sub !== 'boxes') {
    const ew = $('extraswrap');
    if (ew) ew.hidden = true;
  }

  if (S.sub === 'boxes') {
    th.innerHTML = '<tr><th>#</th><th>设备编号</th><th>设备名称</th><th>防护/安装</th><th>参考尺寸</th><th>数量</th></tr>';
    tb.innerHTML = (d.boxes || []).map((b, i) => `<tr><td>${i + 1}</td>
      <td class="mono"><b>${esc(b.code || '')}</b></td>
      ${editableCell('box', i, 'name', esc(b.name || '—'))}
      ${editableCell('box', i, 'ip_rating', `${esc(b.ip_rating || '—')}<span class="sub">${esc(b.install || '')}</span>`)}
      ${editableCell('box', i, 'size', `<span class="mono">${esc(b.size || '—')}</span>`, true)}
      ${editableCell('box', i, 'quantity', `<b>${b.quantity ?? 1}</b> 台`)}</tr>`).join('')
      || emptyRow(6, '没有识别到箱体信息');
    bindCells();
    renderExtrasTable();
    renderListFoot();
    return;
  }

  if (S.sub === 'circuits') {
    th.innerHTML = '<tr><th>#</th><th>回路</th><th>相序</th><th>断路器</th><th>导线</th><th>容量/用途</th></tr>';
    tb.innerHTML = (d.circuits || []).map((c, i) => {
      const cid = 'c' + i;
      return `<tr class="clickable" data-cid="${cid}" onclick="selectCircuit('${cid}',true)">
        <td>${i + 1}</td><td><b>${esc(circuitLabel(c))}</b></td>
        ${editableCell('circuit', i, 'phase', esc(c.phase || '—'))}
        ${editableCell('circuit', i, 'breaker', `<span class="mono">${esc(c.breaker || '—')}</span>`, true)}
        ${editableCell('circuit', i, 'cable', `<span class="mono">${esc(c.cable || '—')}</span>`, true)}
        ${editableCell('circuit', i, 'load_name', `${esc(c.power_kw || '—')}<span class="sub">${esc(c.load_name || '')}</span>`)}
      </tr>`;
    }).join('') || emptyRow(6, '没有识别到回路');
    bindCells();
    renderListFoot();
    return;
  }

  if (S.sub === 'devices') {
    // 右栏只有 400px，单位合并进数量列，否则“用于回路”会被挤出可视区
    th.innerHTML = '<tr><th>#</th><th>元器件</th><th>规格</th><th>数量</th><th>用于回路</th></tr>';
    tb.innerHTML = (d.components || []).map((c, i) => `<tr><td>${i + 1}</td>
      <td style="white-space:nowrap">${esc(c.name || '')}</td>
      <td class="mono" style="max-width:96px;word-break:break-all">${esc(c.spec || '')}</td>
      <td style="white-space:nowrap"><b>${fmtQty(c.quantity)}</b> ${esc(c.unit || '')}</td>
      <td class="mono" style="max-width:104px;word-break:break-all">${esc(c.used_in || '—')}${c.note ? `<span class="sub">${esc(c.note)}</span>` : ''}</td></tr>`).join('')
      || emptyRow(5, '没有汇总出元器件');
    renderListFoot();
    return;
  }

  if (S.sub === 'replace') {
    renderReplacementTable();
    return;
  }

  th.innerHTML = '<tr><th>#</th><th>类别</th><th>内容</th></tr>';
  tb.innerHTML = (d.requirements || []).map((r, i) => `<tr><td>${i + 1}</td>
    <td><b>${esc(r.item || '')}</b></td><td>${esc(r.content || '')}</td></tr>`).join('')
    || emptyRow(3, '图纸中没有与报价相关的技术要求');
  renderListFoot();
}

async function renderReplacementTable(targetBrand) {
  if (targetBrand) S.targetBrand = targetBrand;
  const brand = S.targetBrand || '正泰';
  const th = $('th'), tb = $('tb');

  th.innerHTML = `<tr>
    <th style="width:32px">#</th>
    <th>原图物料 / 品牌</th>
    <th>
      <div style="display:flex;align-items:center;justify-content:space-between">
        <span>平替推荐型号</span>
        <select id="brandSelect" onchange="renderReplacementTable(this.value)" style="padding:1px 4px;font-size:11px;border:1px solid var(--line);border-radius:3px;background:var(--bg)">
          <option value="正泰"${brand === '正泰' ? ' selected' : ''}>正泰 CHINT</option>
          <option value="德力西"${brand === '德力西' ? ' selected' : ''}>德力西 DELIXI</option>
          <option value="良信"${brand === '良信' ? ' selected' : ''}>良信 NADER</option>
        </select>
      </div>
    </th>
    <th style="width:48px">数量</th>
    <th style="width:62px">预计降本</th>
    <th>对标说明</th>
  </tr>`;

  tb.innerHTML = '<tr><td colspan="6" style="text-align:center;padding:24px" class="mut">正在智能对标国产化替代型号...</td></tr>';

  try {
    const res = await api(`/api/jobs/${S.jobId}/replacements?target_brand=${encodeURIComponent(brand)}`);
    if (!res.ok || !res.analysis) throw new Error(res.error || '测算失败');
    const an = res.analysis;
    const items = an.items || [];
    if (!items.length) {
      tb.innerHTML = emptyRow(6, '未发现可对标分析的元器件');
      renderListFoot();
      return;
    }

    tb.innerHTML = items.map((it, i) => {
      const savingPill = it.estimated_saving_pct > 0
        ? `<span class="pill" style="background:#e6f4ea;color:#137333;font-weight:700">↓${it.estimated_saving_pct}%</span>`
        : `<span class="pill" style="background:var(--inset);color:var(--mut)">已最优</span>`;
      return `<tr>
        <td class="mono" style="text-align:center">${i + 1}</td>
        <td>
          <div style="font-weight:600">${esc(it.name || '元器件')}</div>
          <div class="sub mono" style="font-size:10.5px">${esc(it.original_brand)} · ${esc(it.original_spec || '—')}</div>
        </td>
        <td>
          <div class="mono" style="font-weight:600;color:var(--c-brand,#0284c7)">${esc(it.recommended_model)}</div>
          <div class="sub" style="font-size:10.5px">用于: ${esc(it.used_in || '—')}</div>
        </td>
        <td class="mono" style="white-space:nowrap"><b>${fmtQty(it.quantity)}</b> ${esc(it.unit || '只')}</td>
        <td style="text-align:center">${savingPill}</td>
        <td class="sub" style="font-size:10.5px;max-width:140px;line-height:1.3">${esc(it.notes || '性能参数完全对标')}</td>
      </tr>`;
    }).join('');

    renderListFoot(`
      <div style="display:flex;align-items:center;justify-content:space-between;width:100%;font-size:11.5px;gap:8px;">
        <div><b>平替分析：</b>已对标 ${items.length} 项 · 可降本 ${an.replaceable_quantity} 件（预计降本 <b style="color:#137333">${an.estimated_overall_saving_pct}%</b>）</div>
        <div style="display:flex;align-items:center;gap:6px;">
          <a class="linklike" style="font-weight:600;color:var(--c-brand,#0284c7)" href="/api/jobs/${S.jobId}/excel?target_brand=${encodeURIComponent(brand)}" download>导出 ${esc(brand)} 平替表</a>
        </div>
      </div>
    `);
  } catch (err) {
    tb.innerHTML = `<tr><td colspan="6" style="text-align:center;padding:20px;color:var(--err)">平替分析异常：${esc(err.message)}</td></tr>`;
    renderListFoot();
  }
}

/** 非回路设备：浪涌保护器、电能表、铜排这些不在回路里，但必须能补录和删掉。 */
function renderExtrasTable() {
  $('extraswrap').hidden = false;
  const list = S.data.extra_devices || [];
  $('extrasbody').innerHTML = list.map((d, i) => `<tr><td>${i + 1}</td>
    ${editableCell('device', i, 'name', esc(d.name || '—'))}
    ${editableCell('device', i, 'spec', `<span class="mono">${esc(d.spec || '—')}</span>`, true)}
    ${editableCell('device', i, 'quantity', `<b>${fmtQty(d.quantity)}</b> <span class="mut2">${esc(d.unit || '')}</span>`)}
    ${editableCell('device', i, 'used_in', `<span class="mono">${esc(d.used_in || '—')}</span>`, true)}
    <td><button class="linklike" onclick="removeExtra(${i})">删除</button></td></tr>`).join('')
    || `<tr><td colspan="6" class="mut2" style="text-align:center;padding:18px">
        没有非回路设备。模型漏了的话，用上面的「补充设备」加上：浪涌保护器 / 电能表 / N排-PE排 / 指示灯…</td></tr>`;
  bindCells();
}

function removeExtra(i) {
  const list = S.data.extra_devices || [];
  const item = list[i];
  if (!item) return;
  if (!confirm(`从清单里删除「${item.name}」？`)) return;
  list.splice(i, 1);
  renderSub();
  updateChg();
  toast(`已删除 ${item.name}（点「保存」写入服务端，可在修改记录里撤销）`);
}

function addExtra() {
  if (!S.jobId) { toast('请先在项目或上传中打开一份图纸'); return; }
  const name = prompt('设备名称，例如：浪涌保护器 / 电能表 / N排-PE排');
  if (!name || !name.trim()) return;
  const spec = prompt('规格型号（看不清可以留空，之后补）：') || '';
  const unit = prompt('单位：', '套') || '套';
  const quantity = parseFloat(prompt('数量：', '1') || '1');
  if (!(quantity > 0)) { toast('数量必须是大于 0 的数字'); return; }
  if (!S.data.extra_devices) S.data.extra_devices = [];
  S.data.extra_devices.push({
    name: name.trim(), spec: spec.trim(), unit: unit.trim() || '套',
    quantity, used_in: '', note: '',
  });
  renderSub();
  updateChg();
  toast(`已补充 ${name.trim()}（点「保存」写入服务端）`);
}

function emptyRow(cols, text) {
  return `<tr><td colspan="${cols}" class="mut2" style="text-align:center;padding:26px">${text}</td></tr>`;
}

function fmtQty(q) {
  const n = Number(q);
  if (!isFinite(n)) return esc(q);
  return Number.isInteger(n) ? n : n.toFixed(2).replace(/0+$/, '').replace(/\.$/, '');
}

/* ===================== 单元格编辑 ===================== */

function findCircuit(cid) {
  const i = parseInt(String(cid).replace(/^c/, ''), 10);
  return (S.data.circuits || [])[i];
}

/** 模型返回的是回路编号或型号，可能对应多条备用回路，取第一条。 */
function findByLabel(label) {
  const key = String(label || '').trim();
  if (!key) return -1;
  const circuits = S.data.circuits || [];
  const byLabel = circuits.findIndex(c => circuitLabel(c) === key);
  if (byLabel >= 0) return byLabel;
  return circuits.findIndex(c => (c.breaker || '').includes(key));
}

function quickEdit(cid, field) {
  const td = document.querySelector(`td.ed[data-scope="circuit"][data-idx="${cid.replace(/^c/, '')}"][data-f="${field}"]`);
  if (td) startEdit(td);
}

function askRecheck(index) {
  const c = (S.data.circuits || [])[index];
  if (!c) return;
  switchPane('chat');
  ask(`复核 ${circuitLabel(c)}：清单上的值和图纸标注一致吗？`);
}

/** 表尾跟随选中回路：窄面板里不敢再用悬浮操作条，会盖住单元格本身。 */
function renderListFoot(customHtml) {
  const foot = $('listfoot');
  if (!foot) return;
  if (customHtml) {
    foot.innerHTML = customHtml;
    return;
  }
  if (S.sub === 'replace') {
    foot.textContent = '按实际设计规格1:1精准平替 · 支持正泰/德力西/良信多品牌一键切换对比';
    return;
  }
  if (S.sub === 'boxes') {
    foot.textContent = '箱体参数与非回路设备都可双击修改 · 保存后汇总按新数量重算';
    return;
  }
  if (S.sub !== 'circuits') {
    foot.textContent = '元器件汇总由回路与箱体推导，不能直接改 · 要改数量请改对应的回路或箱体';
    return;
  }
  const sel = document.querySelector('#tb tr.sel');
  const idx = sel ? parseInt(sel.dataset.cid.replace(/^c/, ''), 10) : -1;
  const c = (S.data.circuits || [])[idx];
  foot.innerHTML = c
    ? `<span>已选中 <b>${esc(circuitLabel(c))}</b></span>
       <button class="linklike" onclick="quickEdit('c${idx}','breaker')">改断路器</button>
       <button class="linklike" onclick="askRecheck(${idx})">问 AI 复核</button>`
    : '双击带虚线的单元格即可修改 · 点一行可在图纸上定位';
}

function startEdit(td) {
  if (td.querySelector('input,select')) return;
  const scope = td.dataset.scope, field = td.dataset.f;
  const idx = parseInt(td.dataset.idx, 10);
  const set = EDIT_SETS[scope];
  const row = set && set.list()[idx];
  if (!row) return;
  const old = row[field] ?? '';
  let editor;
  if (field === 'phase') {
    editor = document.createElement('select');
    ['L1', 'L2', 'L3', 'L123'].forEach(p => {
      const o = document.createElement('option');
      o.value = o.textContent = p;
      if (p === old) o.selected = true;
      editor.appendChild(o);
    });
  } else {
    editor = document.createElement('input');
    editor.value = old;
  }
  td.innerHTML = '';
  td.appendChild(editor);
  editor.focus();
  if (editor.select) editor.select();
  // Enter 和 blur 会连着触发，只让第一次生效；blur 里的重绘要挪到下一个任务，
  // 否则浏览器还在移除这个编辑框时就去换 <tbody>，会报 “node to be removed is no longer a child”。
  let live = true;
  const commit = (defer) => {
    if (!live) return;
    live = false;
    const value = String(editor.value ?? '').trim();
    if (defer) setTimeout(() => applyEdit(scope, idx, field, value), 0);
    else applyEdit(scope, idx, field, value);
  };
  editor.onkeydown = e => {
    e.stopPropagation();
    if (e.key === 'Enter') commit(false);
    if (e.key === 'Escape') { live = false; renderSub(); }
  };
  editor.onblur = () => commit(true);
  editor.onclick = e => e.stopPropagation();
}

function applyEdit(scope, idx, field, value) {
  const set = EDIT_SETS[scope];
  const row = set && set.list()[idx];
  if (!row) { renderSub(); return; }
  const before = row[field];
  if (field === 'quantity') {
    const n = parseFloat(value);
    if (!(n > 0)) { toast('数量必须是大于 0 的数字'); renderSub(); return; }
    if (n === before) { renderSub(); return; }
    row[field] = n;
  } else {
    if (value === '' || String(before ?? '') === value) { renderSub(); return; }
    row[field] = value;
  }
  renderSub();
  updateChg();
  if (scope === 'circuit') selectCircuit('c' + idx, true);
  toast(`已修改 ${set.label(idx)} · ${FIELD_LABEL[field] || field}（点「保存」写入服务端）`);
}

const FIELD_LABEL = {
  phase: '相序', breaker: '断路器', cable: '导线', power_kw: '容量', load_name: '用途',
  name: '名称', spec: '规格', unit: '单位', quantity: '数量', used_in: '用于',
  ip_rating: '防护等级', install: '安装方式', location: '安装位置', size: '参考尺寸', note: '备注',
};

const describeExtra = d => `${d.name}|${d.spec || ''}`;

function pendingChanges() {
  const out = [];
  Object.entries(EDIT_SETS).forEach(([scope, set]) => {
    set.list().forEach((row, i) => {
      const before = origRow(scope, i);
      if (!before) return;
      set.fields.forEach(f => {
        if (String(before[f] ?? '') !== String(row[f] ?? '')) {
          out.push({ scope, target: set.label(i), field: f,
                     old: before[f] ?? '', new: row[f] ?? '', source: '手动' });
        }
      });
    });
  });
  // 新增的回路
  const nowCircs = S.data.circuits || [];
  const beforeCircs = S.origCircuits || [];
  if (nowCircs.length > beforeCircs.length) {
    for (let i = beforeCircs.length; i < nowCircs.length; i++) {
      const c = nowCircs[i];
      out.push({
        scope: 'circuit', action: 'add', target: circuitLabel(c) || `回路 ${i + 1}`,
        after: c, new: `${c.circuit_no || ''} ${c.breaker || ''}`.trim() || '新回路',
        source: '手动',
      });
    }
  }

  // 新增 / 删除的非回路设备没法用字段差异表达，单独记一笔，撤销时整体还原
  const now = S.data.extra_devices || [];
  const before = S.origExtras || [];
  const seen = new Set(before.map(describeExtra));
  const nowSeen = new Set(now.map(describeExtra));
  now.forEach(d => {
    if (!seen.has(describeExtra(d))) {
      out.push({ scope: 'device', action: 'add', target: d.name,
                 after: d, new: describeExtra(d), source: '手动' });
    }
  });
  before.forEach(d => {
    if (!nowSeen.has(describeExtra(d))) {
      out.push({ scope: 'device', action: 'remove', target: d.name,
                 before: d, old: describeExtra(d), source: '手动' });
    }
  });

  // 技术要求的新增
  const nowReqs = S.data.requirements || [];
  const beforeReqs = S.origReqs || [];
  if (nowReqs.length > beforeReqs.length) {
    for (let i = beforeReqs.length; i < nowReqs.length; i++) {
      const r = nowReqs[i];
      out.push({
        scope: 'requirement', action: 'add', target: r.item || '技术要求',
        after: r, new: `${r.item || ''}：${r.content || ''}`, source: '手动',
      });
    }
  }

  return out;
}

function updateChg() {
  const pending = pendingChanges();
  $('chgcount').textContent = `修改 ${S.changes.length + pending.length}`;
  const btn = $('savebtn');
  btn.disabled = pending.length === 0;
  btn.textContent = pending.length ? `保存 ${pending.length}` : '已保存';
}

function snapshotOrigins() {
  S.origCircuits = JSON.parse(JSON.stringify(S.data.circuits || []));
  S.origBoxes = JSON.parse(JSON.stringify(S.data.boxes || []));
  S.origExtras = JSON.parse(JSON.stringify(S.data.extra_devices || []));
  S.origReqs = JSON.parse(JSON.stringify(S.data.requirements || []));
}

async function saveNow(silent) {
  const changes = pendingChanges();
  if (S.jobId && !changes.length && !silent) { toast('没有需要保存的修改'); return; }
  if (!S.jobId) { if (!silent) toast('请先在项目或上传中打开一份图纸'); return; }
  try {
    const res = await putJSON(`/api/jobs/${S.jobId}/data`, {
      ...S.data, changes, reason: '手动修改',
    });
    applyServerData(res);
    if (changes.length) toast(`已保存 ${changes.length} 处修改，元器件汇总已重算`);
  } catch (e) {
    toast('保存失败：' + e.message);
  }
}

function applyServerData(res) {
  if (!res || !res.data) return;
  S.data = res.data;
  S.changes = res.changes || [];
  snapshotOrigins();
  renderBoxstrip();
  renderSub();
  renderReview();
  updateChg();
  updateBadge();
}

function unresolved() {
  return (S.data.uncertainties || []).filter(u => !u.resolved);
}

function updateBadge() {
  const n = unresolved().length;
  const nav = $('navbdg');
  nav.textContent = n;
  nav.hidden = !n;
  const badge = $('rvbdg');
  badge.textContent = n;
  badge.hidden = !n;
  const top = $('ubadge');
  top.textContent = n ? `待核对 ${n}` : '核对完成';
  top.classList.toggle('warn', !!n);
  top.classList.toggle('done', !n);
}

/* ===================== 图-表联动 ===================== */

function selectCircuit(cid, fromTable) {
  // 如果是拖拽平移后的鼠标松开，不作为点选触发
  if (!fromTable && S.hasMoved) return;

  const c = findCircuit(cid);
  if (!c) return;

  // 无论从哪里点击，都确保进入清单主面板且处于回路页签
  switchPane('list');
  if (!fromTable && S.sub !== 'circuits') {
    renderSub('circuits');
  }

  // 选中当前行
  document.querySelectorAll('#tb tr').forEach(tr => tr.classList.toggle('sel', tr.dataset.cid === cid));
  renderListFoot();

  // 反向点选：从图面点回路时，自动将列表平滑滚动到视野居中，并触发双脉冲高亮
  if (!fromTable) {
    const row = document.querySelector(`#tb tr[data-cid="${cid}"]`);
    if (row) {
      row.scrollIntoView({ behavior: 'smooth', block: 'center' });
      row.classList.remove('flash-highlight');
      void row.offsetWidth; // 触发 reflow 重新执行动画
      row.classList.add('flash-highlight');
      setTimeout(() => row.classList.remove('flash-highlight'), 1500);
    }
  }

  // 图面标注高亮
  const r = c.bbox;
  if (!r) return;
  if ((r.page || 1) !== S.page) setPage(r.page || 1);
  $('hlbox').innerHTML = `<div class="hl show"
    style="left:${r.x * 100}%;top:${r.y * 100}%;width:${r.w * 100}%;height:${r.h * 100}%">
    <span class="tag">${esc(circuitLabel(c))} · ${esc(c.breaker || '')}</span></div>`;

  // 从表格正向点击时，按原逻辑自动聚焦放大该回路
  if (fromTable) {
    S.zoom = 1.6;
    S.panX = 0;
    S.panY = 0;
    const stage = $('stage');
    if (stage) {
      stage.style.transform = 'scale(1.6)';
      stage.style.transformOrigin = `${(r.x + r.w / 2) * 100}% ${(r.y + r.h / 2) * 100}%`;
    }
    $('zpct').textContent = '联动';
  }
}

/* ===================== 核对 ===================== */

function renderReview() {
  const items = S.data.uncertainties || [];
  const un = unresolved().length;
  const recon = S.data.reconciliation;

  let reconHtml = '';
  if (recon && recon.has_catalog) {
    const isBad = recon.missing_count > 0;
    reconHtml = `
    <div class="recon-card ${isBad ? 'bad' : 'ok'}">
      <div class="recon-card-head">
        <span>📋 图纸目录对账审计 ${isBad ? '⚠️ 存在范围缺失' : '✅ 100% 覆盖'}</span>
        <span style="font-weight:700;color:${isBad ? '#b91c1c' : '#15803d'}">
          覆盖率 ${Math.round(recon.coverage_rate * 100)}%
        </span>
      </div>
      <div class="recon-card-body">
        <div>目录声明箱柜：<b>${recon.total_declared_panels}</b> 个 ｜ 已提取：<b>${recon.covered_count}</b> 个 ｜ 缺失：<b style="color:${isBad ? '#b91c1c' : '#15803d'}">${recon.missing_count}</b> 个</div>
        ${isBad ? `<div style="margin-top:4px;color:#b91c1c">缺失箱号：${esc(recon.missing_box_codes.join('、'))}</div>` : ''}
      </div>
    </div>`;
  }

  let topBar = '';
  if (items.length) {
    const errorCount = items.filter(u => !u.resolved && u.severity === 'ERROR').length;
    topBar = `
    <div style="display:flex;justify-content:space-between;align-items:center;padding:9px 12px;background:#f8fafc;border:1px solid #cbd5e1;margin-bottom:12px;border-radius:6px;gap:8px">
      <div style="font-size:12px;color:#334155;white-space:nowrap">
        待核对：<b style="color:${un ? '#e11d48' : '#16a34a'}">${un}</b> / ${items.length} 处
        ${errorCount ? `<span style="margin-left:6px;padding:2px 6px;border-radius:3px;background:#fee2e2;color:#b91c1c;font-weight:600;font-size:11px">阻断错误 ${errorCount} 处</span>` : ''}
      </div>
      <div style="display:flex;gap:6px">
        <button class="btn sm" onclick="triggerAiReview()" title="让 AI 深度交叉复核全盘存疑项与电气设计规范" style="background:#4f46e5;color:#fff;border:none;font-size:11px;padding:3px 8px">
          🤖 AI 智能深度复核
        </button>
        <button class="btn sm" onclick="resolveAllIssues()" title="一键将全部待核对项标记为已确认并放行导出" style="background:#16a34a;color:#fff;border:none;font-size:11px;padding:3px 8px">
          ✅ 一键全部核对通过
        </button>
      </div>
    </div>`;
  }

  $('rvlist').innerHTML = reconHtml + (items.length ? (topBar + items.map((u, i) => {
    let tagClass = 'warn';
    let tagText = '待核对';
    if (u.resolved) {
      tagClass = 'ok';
      tagText = '已确认';
    } else if (u.severity === 'ERROR') {
      tagClass = 'error';
      tagText = '阻断错误';
    } else if (u.severity === 'INFO') {
      tagClass = 'info';
      tagText = '规范提示';
    }
    return `
    <div class="issue${u.resolved ? ' done' : ''}">
      <h4><span class="tag ${tagClass}">${tagText}</span>${esc(u.location || '待核对项 ' + (i + 1))}</h4>
      <p>${esc(u.detail || '')}</p>
      ${u.resolved ? '' : `<button class="btn ghost sm" onclick="openReviewAt(${i})">去核对</button>`}
    </div>`;
  }).join(''))
    : '<div class="empty">这份清单没有待核对项<br>识别结果与图纸一致</div>');
  updateBadge();
}

async function triggerAiReview() {
  if (!S.jobId) return toast('未打开有效图纸');
  toast('🤖 AI 正在全盘交叉复核图纸与规范…');
  try {
    const res = await postJSON(`/api/jobs/${S.jobId}/ai_review`, {});
    if (res.data) {
      applyServerData(res);
    }
    const autoResolved = res.auto_resolved_count || 0;
    const remaining = res.remaining_count || 0;
    const findings = res.findings || [];

    let msgContent = `<b>🤖 AI 智能全盘深度复核完成</b><br>${esc(res.summary)}<br><br>`;
    if (findings.length) {
      msgContent += '<b>📌 关键复核结论与电气研判：</b><br>';
      findings.forEach(f => {
        const icon = f.type === 'resolved' ? '✅' : (f.type === 'warning' ? '⚠️' : '💡');
        msgContent += `${icon} <b>${esc(f.title)}</b>：${esc(f.detail)}<br>`;
      });
    }

    addMsg('ai', msgContent);
    switchPane('chat');
    renderReview();
    updateBadge();
    toast(`AI 复核完成：已自动研判消除 ${autoResolved} 处存疑，剩余 ${remaining} 处`);
  } catch (e) {
    toast('AI 复核失败：' + e.message);
  }
}

async function resolveAllIssues() {
  if (!S.jobId) { toast('未打开有效图纸'); return false; }
  const un = unresolved();
  const errors = un.filter(u => u.severity === 'ERROR');
  if (errors.length > 0) {
    if (!confirm(`检测到 ${errors.length} 处系统级【阻断错误】（如图幅范围缺失或严重违反证据政策）。\n确定要强行人工全盘确认放行吗？`)) {
      return false;
    }
  }
  try {
    toast('正在一键确认所有存疑项…');
    const res = await postJSON(`/api/jobs/${S.jobId}/resolve_all`, {});
    if (res.data) {
      applyServerData(res);
    }
    renderReview();
    updateBadge();
    toast('✅ 已一键全部确认通过！可直接导出 Excel 报表');
    addMsg('sys', '✅ 用户已一键全部核对通过，当前清单已完全解锁导出');
    closeReview();
    return true;
  } catch (e) {
    toast('操作失败：' + e.message);
    return false;
  }
}

async function resolveAllAndExport() {
  const ok = await resolveAllIssues();
  if (!ok) return;
  await doExport();
}

async function triggerAiReviewAndExport() {
  await triggerAiReview();
  const left = unresolved().length;
  if (left > 0) {
    toast(`AI 复核后仍有 ${left} 处待人工核对，请处理后再导出`);
    return;
  }
  await doExport();
}

function openReview() {
  const first = (S.data.uncertainties || []).findIndex(u => !u.resolved);
  openReviewAt(first < 0 ? 0 : first);
}

function openReviewAt(i) {
  if (!(S.data.uncertainties || []).length) { toast('没有待核对项'); return; }
  S.reviewIdx = i;
  $('rvoverlay').classList.add('show');
  showIssue();
}

function closeReview() {
  $('rvoverlay').classList.remove('show');
  renderReview();
}

function showIssue() {
  const items = S.data.uncertainties || [];
  const it = items[S.reviewIdx];
  if (!it) { closeReview(); return; }
  const left = unresolved().length;
  $('rvprog-t').textContent = `存疑 ${S.reviewIdx + 1} / ${items.length}（剩余 ${left}）`;
  $('rvbar').style.width = (items.length ? ((items.length - left) / items.length) * 100 : 0) + '%';
  $('rvtitle').textContent = it.location || `待核对项 ${S.reviewIdx + 1}`;
  $('rvdesc').textContent = it.detail || '';
  $('rvsugwrap').hidden = true;
  $('rvsug').textContent = '';

  const img = $('rvdwg');
  const page = (it.bbox && it.bbox.page) || 1;
  img.src = `/api/jobs/${S.jobId}/page/${page}`;
  $('rvpageno').textContent = S.pages > 1 ? `第 ${page}/${S.pages} 页` : '';
  const r = it.bbox;
  if (r) {
    $('rvstage').style.transform = 'scale(2.4)';
    $('rvstage').style.transformOrigin = `${(r.x + r.w / 2) * 100}% ${(r.y + r.h / 2) * 100}%`;
    $('rvhl').innerHTML = `<div class="hl show" style="left:${r.x * 100}%;top:${r.y * 100}%;width:${r.w * 100}%;height:${r.h * 100}%"></div>`;
  } else {
    $('rvstage').style.transform = 'scale(1)';
    $('rvhl').innerHTML = '';
    $('rvsugwrap').hidden = false;
    $('rvsug').textContent = '这一项没能定位到图纸上的具体位置，请按上面的描述整页核对。';
  }
}

function resolveIssue(ok) {
  const items = S.data.uncertainties || [];
  const it = items[S.reviewIdx];
  if (!it) return;
  it.resolved = true;
  const left = unresolved().length;
  if (!left) {
    closeReview();
    addMsg('sys', '存疑项已全部确认，可以导出');
    toast('核对完成，可以导出了');
  } else {
    const next = items.findIndex(u => !u.resolved);
    S.reviewIdx = next;
    showIssue();
  }
  renderReview();
  saveNow(true).then(() => renderReview());
}

/* ===================== 修改记录 ===================== */

function openLog() {
  if (!S.jobId) { toast('请先在项目或上传中打开一份图纸'); return; }
  const list = $('loglist');
  const rows = [];
  pendingChanges().forEach(g => rows.push(logCard({ ...g, ts: '未保存', pending: true })));
  S.changes.slice().reverse().forEach((g, i) => rows.push(logCard({
    ...g, index: S.changes.length - 1 - i,
  })));
  list.innerHTML = rows.length ? rows.join('')
    : '<div class="logempty">暂无人工修改<br>双击表格单元格即可修改<br>AI 提取的原值会保留备查</div>';
  $('logdrawer').classList.add('open');
}

const SCOPE_LABEL = { circuit: '回路', box: '箱体', device: '非回路设备' };

function logCard(g) {
  const scope = g.scope || 'circuit';
  const isAdd = g.action === 'add';
  const isRemove = g.action === 'remove';
  const head = isAdd || isRemove
    ? `${isAdd ? '新增' : '删除'}${SCOPE_LABEL[scope] || ''} <b>${esc(g.target || '')}</b>`
    : `<b>${esc(g.target || '')}</b> · ${esc(SCOPE_LABEL[scope] || '')} ${esc(FIELD_LABEL[g.field] || g.field || '')}`;
  const diff = isAdd
    ? `<span class="new">${esc(g.new || '')}</span>`
    : isRemove
      ? `<span class="old">${esc(g.old || '')}</span>`
      : `<span class="old">${esc(g.old || '—')}</span> → <span class="new">${esc(g.new || '—')}</span>`;
  return `<div class="logitem">
    <div class="lt"><span>${esc(g.ts || '')} · ${esc(g.source || '')}${g.pending ? '（未保存）' : ''}</span>
      ${g.pending ? '' : `<button class="undo" onclick="undoChange(${g.index})">撤销</button>`}</div>
    <div>${head}</div>
    <div class="chg">${diff}</div>
  </div>`;
}

function closeLog() {
  $('logdrawer').classList.remove('open');
}

async function undoChange(index) {
  const entry = S.changes[index];
  if (!entry) return;
  try {
    const res = await postJSON(`/api/jobs/${S.jobId}/revert`, {
      target: entry.target, field: entry.field || '', ts: entry.ts || '',
      scope: entry.scope || 'circuit',
    });
    applyServerData(res);
    const what = entry.action ? (entry.action === 'add' ? '新增' : '删除') : (FIELD_LABEL[entry.field] || entry.field);
    toast(`已撤销：${entry.target} · ${what}`);
    openLog();
  } catch (e) {
    toast('撤销失败：' + e.message);
  }
}

async function undoLastChange() {
  if (!S.jobId) {
    toast('先打开一份图纸');
    return;
  }
  const pending = pendingChanges();
  if (pending.length > 0) {
    const lastP = pending[pending.length - 1];
    const set = EDIT_SETS[lastP.scope];
    if (set) {
      const list = set.list();
      for (let i = 0; i < list.length; i++) {
        if (set.label(i) === lastP.target) {
          const before = origRow(lastP.scope, i);
          if (before) {
            list[i][lastP.field] = before[lastP.field];
            renderSub(S.sub);
            renderListFoot();
            updateChg();
            toast(`已撤销未保存修改：${lastP.target} · ${FIELD_LABEL[lastP.field] || lastP.field}`);
            return;
          }
        }
      }
    }
  }
  if (S.changes && S.changes.length > 0) {
    await undoChange(S.changes.length - 1);
  } else {
    toast('暂无可撤销的历史记录');
  }
}

/* ===================== 导出 ===================== */

function openExport() {
  if (!S.jobId) { toast('先打开一份图纸'); return; }
  const un = unresolved().length;
  const circuits = (S.data.circuits || []).length;
  const comps = (S.data.components || []).length;
  const cross = (S.data.uncertainties || []).filter(u => /回路逐条计数/.test(u.detail || '')).length;
  const pending = pendingChanges().length;
  const checks = [
    { ok: un === 0, title: '存疑项状态', note: un === 0 ? `共 ${(S.data.uncertainties || []).length} 项，全部已确认` : `还有 ${un} 项待核对（可一键全部通过或AI复核）` },
    { ok: circuits > 0, title: '回路明细完整', note: `${circuits} 条回路` },
    { ok: comps > 0 && cross === 0, title: '元器件已按回路重新汇总', note: cross ? `有 ${cross} 条回路与汇总数量存在差异，已作工程标记` : `${comps} 项，与回路逐条计数一致` },
    { ok: true, title: pending ? `还有 ${pending} 处修改没保存` : '修改已留痕', note: pending ? '点「保存」后再导出，否则未保存修改不会进入变更记录' : `${S.changes.length} 处修改，随清单导出变更记录` },
  ];
  if (S.data.reconciliation && S.data.reconciliation.has_catalog) {
    const recon = S.data.reconciliation;
    const isBad = recon.missing_count > 0;
    checks.push({
      ok: !isBad,
      title: '图纸目录对账审计',
      note: isBad
        ? `目录声明 ${recon.total_declared_panels} 个箱体，仅覆盖 ${recon.covered_count} 个，遗漏：${recon.missing_box_codes.join('、')}`
        : `目录声明 ${recon.total_declared_panels} 个箱体 100% 覆盖提取`
    });
  }
  $('expsub').textContent = `${S.job ? S.job.filename : ''} · 导出前检查`;
  $('expchecks').innerHTML = checks.map(c => `<div class="chk${c.ok ? '' : ' bad'}">
    <span class="c">${c.ok ? '✓' : '!'}</span><div>${esc(c.title)}<small>${esc(c.note)}</small></div></div>`).join('');
  const meta = (S.job && S.job.summary && S.job.summary.meta) || {};
  $('expver').textContent = `模型 ${meta.model || '-'} · 提示词 v${meta.prompt_version || '-'} · 契约 v${meta.contract_version || '-'}`;

  const mactions = document.querySelector('#expmodal .mactions');
  if (mactions) {
    if (un > 0) {
      mactions.innerHTML = `
        <button class="btn ghost sm" onclick="closeExport()">取消</button>
        <button class="btn sm" onclick="triggerAiReviewAndExport()" title="让 AI 全盘复核存疑项，复核后自动尝试导出；若仍有存疑未确认，导出会被中止" style="background:#4f46e5;color:#fff;border:none">🤖 AI复核并导出</button>
        <button class="btn primary sm" onclick="resolveAllAndExport()" title="一键将剩余 ${un} 处待核对项全部标记为已确认并直接下载（由你担责确认）" style="background:#16a34a;border:none">✅ 一键确认并导出</button>
      `;
    } else {
      mactions.innerHTML = `
        <button class="btn ghost sm" onclick="closeExport()">取消</button>
        <button class="btn primary sm" id="expok" onclick="doExport()">确认导出</button>
      `;
    }
  }
  $('expmodal').classList.add('show');
}

function closeExport() {
  $('expmodal').classList.remove('show');
}

async function doExport() {
  try {
    const headers = {};
    if (Auth.token) headers['Authorization'] = `Bearer ${Auth.token}`;
    const res = await fetch(`/api/jobs/${S.jobId}/excel`, { headers });
    if (res.status === 409) {
      // 后端门禁：存疑未确认完不许导出
      let n = '?';
      try { const j = await res.json(); if (j && j.detail && j.detail.unresolved_count != null) n = j.detail.unresolved_count; } catch (e) {}
      closeExport();
      toast(`导出已中止：还有 ${n} 处存疑未确认`);
      addMsg('sys', `导出被拦截：还有 ${n} 处存疑未确认。请先逐项核对、用 AI 复核，或点「一键确认并导出」由你担责确认后再导出。`);
      return;
    }
    if (!res.ok) throw new Error('服务端没有生成 Excel');
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `配电箱元器件清单(报价用)-${S.jobId}.xlsx`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 4000);
    closeExport();
    addMsg('sys', `Excel 报价清单已下载（含 ${S.changes.length} 处修改记录）`);
    toast('Excel 报价清单已开始下载');
  } catch (e) {
    toast('导出失败：' + e.message);
  }
}

/* ===================== 框选解析 ===================== */

function toggleCropMode() {
  if (!S.jobId) { toast('请先在项目或上传中打开一份图纸'); return; }
  S.cropMode = !S.cropMode;
  const btn = $('cropbtn');
  if (btn) btn.classList.toggle('on', S.cropMode);
  const canvas = $('canvas');
  if (canvas) canvas.classList.toggle('crop-mode', S.cropMode);
  if (S.cropMode) {
    switchPane('crop');
    toast('已开启框选识图：在图纸上拖拽框选任意区域');
  } else {
    clearCrop();
    toast('已退出框选识图');
  }
}

function clearCrop() {
  const marquee = $('cropmarquee');
  if (marquee) marquee.hidden = true;
  S.currentCropBBox = null;
  S.lastCropResult = null;
  const resEl = $('crop-result');
  if (resEl) resEl.hidden = true;
  const loadEl = $('crop-loading');
  if (loadEl) loadEl.hidden = true;
  const emptyEl = $('crop-empty');
  if (emptyEl) {
    emptyEl.hidden = false;
    emptyEl.innerHTML = `<div style="font-size:26px;margin-bottom:8px">⛶</div>
      <div style="font-weight:600;margin-bottom:4px">在图纸上框选任意区域</div>
      <div class="mut2" style="font-size:11.5px;max-width:280px;margin:0 auto">点击左上方「框选识图」按钮开启模式，按住鼠标左键在图纸上拉框，支持精准提取元器件、回路、设计说明及箱体参数。</div>`;
  }
  const bdg = $('cropbdg');
  if (bdg) bdg.hidden = true;
  const meta = $('crop-meta');
  if (meta) meta.textContent = '在图纸上拖拽框选';
  if ($('crop-boxinfo-card')) $('crop-boxinfo-card').hidden = true;
  if ($('crop-req-card')) $('crop-req-card').hidden = true;
  if ($('crop-comp-card')) $('crop-comp-card').hidden = true;
  if ($('crop-none-hint')) $('crop-none-hint').hidden = true;
  if ($('crop-add-all-btn')) $('crop-add-all-btn').hidden = true;
}

function initCropSelection() {
  const stage = $('stage');
  const img = $('dwg');
  if (!stage || !img) return;

  stage.addEventListener('mousedown', e => {
    if (!S.cropMode || e.button !== 0) return;
    const rect = img.getBoundingClientRect();
    if (rect.width === 0 || rect.height === 0) return;
    if (e.clientX < rect.left || e.clientX > rect.right ||
        e.clientY < rect.top || e.clientY > rect.bottom) return;

    e.preventDefault();
    e.stopPropagation();

    S.isCropping = true;
    S.cropStart = {
      x: (e.clientX - rect.left) / rect.width,
      y: (e.clientY - rect.top) / rect.height,
    };

    const marquee = $('cropmarquee');
    if (marquee) {
      marquee.style.left = `${S.cropStart.x * 100}%`;
      marquee.style.top = `${S.cropStart.y * 100}%`;
      marquee.style.width = '0%';
      marquee.style.height = '0%';
      marquee.hidden = false;
    }
  });

  window.addEventListener('mousemove', e => {
    if (!S.isCropping) return;
    const rect = img.getBoundingClientRect();
    if (rect.width === 0 || rect.height === 0) return;

    const curX = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
    const curY = Math.max(0, Math.min(1, (e.clientY - rect.top) / rect.height));

    const left = Math.min(S.cropStart.x, curX);
    const top = Math.min(S.cropStart.y, curY);
    const width = Math.abs(curX - S.cropStart.x);
    const height = Math.abs(curY - S.cropStart.y);

    S.currentCropBBox = { x: left, y: top, w: width, h: height, page: S.page || 1 };

    const marquee = $('cropmarquee');
    if (marquee) {
      marquee.style.left = `${left * 100}%`;
      marquee.style.top = `${top * 100}%`;
      marquee.style.width = `${width * 100}%`;
      marquee.style.height = `${height * 100}%`;
      marquee.hidden = false;
      const tag = $('croptag');
      if (tag) {
        tag.textContent = `${Math.round(width * 100)}% × ${Math.round(height * 100)}%`;
      }
    }
  });

  window.addEventListener('mouseup', () => {
    if (!S.isCropping) return;
    S.isCropping = false;
    const bbox = S.currentCropBBox;
    if (!bbox || bbox.w < 0.015 || bbox.h < 0.015) {
      clearCrop();
      return;
    }
    parseSelectedRegion(bbox);
  });
}

async function parseSelectedRegion(bbox) {
  if (!S.jobId) {
    toast('先打开一份图纸');
    return;
  }
  switchPane('crop');
  $('crop-empty').hidden = true;
  $('crop-result').hidden = true;
  $('crop-loading').hidden = false;
  $('crop-meta').textContent = `第 ${bbox.page || 1} 页 · 选区 ${Math.round(bbox.w * 100)}%×${Math.round(bbox.h * 100)}%`;

  try {
    const res = await postJSON(`/api/jobs/${S.jobId}/parse_region`, {
      page: bbox.page || S.page || 1,
      x: bbox.x,
      y: bbox.y,
      w: bbox.w,
      h: bbox.h,
    });
    $('crop-loading').hidden = true;
    if (!res.ok && res.error) {
      $('crop-empty').hidden = false;
      $('crop-empty').innerHTML = `<div style="color:var(--err);margin-bottom:8px">解析失败：${esc(res.error)}</div>
        <button class="btn sm ghost" onclick="parseSelectedRegion(S.currentCropBBox)">重试</button>`;
      return;
    }
    S.lastCropResult = res;
    $('crop-result').hidden = false;
    $('crop-img').src = res.crop_url;
    $('crop-summary').textContent = res.summary || '已识别选区内容';

    const comps = (res.components || []).filter(c => (c.quantity ?? 1) > 0 || c.spec);
    const circs = res.circuits || [];
    const reqs = res.requirements || [];
    const boxInfo = res.box_info || {};
    const totalCount = comps.length + circs.length + reqs.length + (boxInfo.note || boxInfo.code ? 1 : 0);
    $('cropbdg').textContent = totalCount;
    $('cropbdg').hidden = totalCount === 0;

    // 1. 箱体说明卡片
    const hasBox = !!(boxInfo.code || boxInfo.note || boxInfo.name || boxInfo.install || boxInfo.size);
    if ($('crop-boxinfo-card')) $('crop-boxinfo-card').hidden = !hasBox;
    if (hasBox && $('crop-boxinfo-content')) {
      let bHtml = '';
      if (boxInfo.code || boxInfo.name) {
        bHtml += `<div><b>${esc([boxInfo.code, boxInfo.name].filter(Boolean).join(' · '))}</b></div>`;
      }
      const details = [];
      if (boxInfo.ip_rating) details.push(`防护: ${boxInfo.ip_rating}`);
      if (boxInfo.install) details.push(`安装: ${boxInfo.install}`);
      if (boxInfo.location) details.push(`位置: ${boxInfo.location}`);
      if (boxInfo.size) details.push(`尺寸: ${boxInfo.size}`);
      if (details.length) bHtml += `<div class="mut" style="font-size:11px;margin:3px 0">${esc(details.join(' ｜ '))}</div>`;
      if (boxInfo.note) bHtml += `<div style="margin-top:4px"><b>说明/备注：</b>${esc(boxInfo.note)}</div>`;
      $('crop-boxinfo-content').innerHTML = bHtml;
    }

    // 2. 设计说明与技术要求卡片
    if ($('crop-req-card')) $('crop-req-card').hidden = reqs.length === 0;
    if ($('crop-req-count')) $('crop-req-count').textContent = reqs.length;
    if (reqs.length && $('crop-req-rows')) {
      let reqHtml = '';
      reqs.forEach(r => {
        reqHtml += `<tr>
          <td style="white-space:nowrap;width:30%"><b>${esc(r.item || '设计说明')}</b></td>
          <td style="font-size:11.5px;line-height:1.4">${esc(r.content || '')}</td>
        </tr>`;
      });
      $('crop-req-rows').innerHTML = reqHtml;
      if ($('crop-add-req-btn')) $('crop-add-req-btn').textContent = `＋ 补入技术要求 (${reqs.length} 项)`;
    }

    // 3. 元器件与回路卡片
    const compTotal = comps.length + circs.length;
    if ($('crop-comp-card')) $('crop-comp-card').hidden = compTotal === 0;
    if ($('crop-comp-count')) $('crop-comp-count').textContent = compTotal;
    if (compTotal > 0 && $('crop-comp-rows')) {
      let rowsHtml = '';
      comps.forEach(c => {
        rowsHtml += `<tr>
          <td><b>${esc(c.name || '元器件')}</b></td>
          <td class="mono">${esc(c.spec || '—')}</td>
          <td><b>${c.quantity ?? 1}</b></td>
          <td>${esc(c.unit || '只')}</td>
          <td class="mut">${esc(c.note || '')}</td>
        </tr>`;
      });
      circs.forEach(c => {
        rowsHtml += `<tr style="background:rgba(46,92,230,.03)">
          <td><span class="pill" style="font-size:10px">回路</span> <b>${esc(c.circuit_no || '回路')}</b></td>
          <td class="mono">${esc(c.breaker || '—')}</td>
          <td><b>1</b></td>
          <td>只</td>
          <td class="mut">${esc([c.cable, c.load_name, c.power_kw ? c.power_kw + 'kW' : ''].filter(Boolean).join(' / '))}</td>
        </tr>`;
      });
      $('crop-comp-rows').innerHTML = rowsHtml;
      if ($('crop-add-btn')) $('crop-add-btn').textContent = `＋ 补入器件清单 (${compTotal} 项)`;
    }

    // 4. 无可提取内容提示
    if ($('crop-none-hint')) $('crop-none-hint').hidden = totalCount > 0;

    // 5. 组合补入按钮
    const activeSectionCount = [hasBox && (boxInfo.note || boxInfo.code), reqs.length > 0, compTotal > 0].filter(Boolean).length;
    if ($('crop-add-all-btn')) {
      $('crop-add-all-btn').hidden = activeSectionCount <= 1;
      if (activeSectionCount > 1) {
        $('crop-add-all-btn').textContent = `＋ 全部补入 (共 ${totalCount} 项)`;
      }
    }
  } catch (err) {
    $('crop-loading').hidden = true;
    $('crop-empty').hidden = false;
    $('crop-empty').innerHTML = `<div style="color:var(--err);margin-bottom:8px">请求失败：${esc(err.message)}</div>
      <button class="btn sm ghost" onclick="parseSelectedRegion(S.currentCropBBox)">重试</button>`;
  }
}

function applyCropRequirementsInternal(reqs) {
  S.data.requirements = S.data.requirements || [];
  const seen = new Set(S.data.requirements.map(r => `${r.item}|${r.content}`));
  let added = 0;
  reqs.forEach(r => {
    const key = `${r.item || ''}|${r.content || ''}`;
    if (!seen.has(key)) {
      seen.add(key);
      S.data.requirements.push({ item: r.item || '设计说明', content: r.content || '' });
      added++;
    }
  });
  return added;
}

async function applyCropRequirements() {
  if (!S.lastCropResult) { toast('请先在图纸上框选区域并等待解析完成'); return; }
  const reqs = S.lastCropResult.requirements || [];
  if (!reqs.length) { toast('没有可补入的技术要求'); return; }
  const added = applyCropRequirementsInternal(reqs);
  if (!added) {
    toast('这些技术要求已经存在于清单中');
    return;
  }
  updateChg();
  await saveNow(true);
  switchPane('list');
  renderSub('reqs');
  toast(`已成功补录 ${added} 项技术要求到清单！`);
}

function applyCropBoxInfoInternal(info) {
  S.data.boxes = S.data.boxes || [];
  if (!S.data.boxes.length) {
    S.data.boxes.push({
      code: info.code || 'AL1',
      name: info.name || '配电箱',
      ip_rating: info.ip_rating || 'IP30',
      install: info.install || '',
      location: info.location || '',
      size: info.size || '',
      quantity: info.quantity || 1,
      note: info.note || '',
    });
    return true;
  }
  let box = S.data.boxes.find(b => info.code && b.code && b.code.toUpperCase() === info.code.toUpperCase());
  if (!box) box = S.data.boxes[0];
  if (info.code && !box.code) box.code = info.code;
  if (info.name && !box.name) box.name = info.name;
  if (info.ip_rating) box.ip_rating = info.ip_rating;
  if (info.install) box.install = info.install;
  if (info.location) box.location = info.location;
  if (info.size) box.size = info.size;
  if (info.note) {
    if (box.note && !box.note.includes(info.note)) {
      box.note = `${box.note}；${info.note}`;
    } else if (!box.note) {
      box.note = info.note;
    }
  }
  return true;
}

async function applyCropBoxInfo() {
  if (!S.lastCropResult || !S.lastCropResult.box_info) { toast('请先在图纸上框选包含箱体信息的区域'); return; }
  applyCropBoxInfoInternal(S.lastCropResult.box_info);
  updateChg();
  await saveNow(true);
  switchPane('list');
  renderSub('boxes');
  toast('已将框选参数同步至配电箱说明！');
}

function applyCropComponentsInternal(comps, circs) {
  const boxCode = (S.data.boxes && S.data.boxes[0] && S.data.boxes[0].code) || '';
  let addedCount = 0;
  if (comps.length) {
    S.data.extra_devices = S.data.extra_devices || [];
    comps.forEach(c => {
      S.data.extra_devices.push({
        name: c.name || '元器件',
        spec: c.spec || '',
        unit: c.unit || '只',
        quantity: Number(c.quantity) || 1,
        used_in: c.note ? `${boxCode} ${c.note}`.trim() : boxCode,
        note: '来自框选识别补录',
      });
      addedCount++;
    });
  }
  if (circs.length) {
    S.data.circuits = S.data.circuits || [];
    circs.forEach(c => {
      S.data.circuits.push({
        box: boxCode,
        phase: c.phase || '',
        breaker: c.breaker || '',
        contactor: c.contactor || '',
        ct: c.ct || '',
        thermal: c.thermal || '',
        power_kw: c.power_kw || '',
        circuit_no: c.circuit_no || '',
        cable: c.cable || '',
        current_a: c.current_a || '',
        load_name: c.load_name || '框选补录回路',
        secondary_ref: '',
        start_method: '',
        note: '来自框选识别补录',
        bbox: S.currentCropBBox ? {
          x: S.currentCropBBox.x,
          y: S.currentCropBBox.y,
          w: S.currentCropBBox.w,
          h: S.currentCropBBox.h,
          page: S.currentCropBBox.page || S.page || 1,
        } : null,
      });
      addedCount++;
    });
  }
  return addedCount;
}

async function applyCropComponents() {
  if (!S.lastCropResult) { toast('请先在图纸上框选区域并等待解析完成'); return; }
  const comps = (S.lastCropResult.components || []).filter(c => (c.quantity ?? 1) > 0 || c.spec);
  const circs = S.lastCropResult.circuits || [];
  if (!comps.length && !circs.length) {
    toast('没有可补入的元器件');
    return;
  }
  const addedCount = applyCropComponentsInternal(comps, circs);
  updateChg();
  await saveNow(true);
  switchPane('list');
  renderSub(circs.length ? 'circuits' : 'devices');
  toast(`已成功补录 ${addedCount} 项到清单并重算汇总！`);
}

async function applyCropAll() {
  if (!S.lastCropResult) { toast('请先在图纸上框选区域并等待解析完成'); return; }
  const comps = (S.lastCropResult.components || []).filter(c => (c.quantity ?? 1) > 0 || c.spec);
  const circs = S.lastCropResult.circuits || [];
  const reqs = S.lastCropResult.requirements || [];
  const boxInfo = S.lastCropResult.box_info;

  let changesMade = 0;
  if (boxInfo && (boxInfo.note || boxInfo.code)) {
    applyCropBoxInfoInternal(boxInfo);
    changesMade++;
  }
  if (reqs.length) {
    changesMade += applyCropRequirementsInternal(reqs);
  }
  if (comps.length || circs.length) {
    changesMade += applyCropComponentsInternal(comps, circs);
  }
  if (!changesMade) {
    toast('没有可补入的内容');
    return;
  }
  updateChg();
  await saveNow(true);
  switchPane('list');
  if (reqs.length && !comps.length && !circs.length) renderSub('reqs');
  else if (circs.length) renderSub('circuits');
  else renderSub('devices');
  toast('已全部同步补入清单、技术要求与箱体说明！');
}

function sendCropToChat() {
  if (!S.lastCropResult) { toast('请先在图纸上框选区域并等待解析完成'); return; }
  const summary = S.lastCropResult.summary || '';
  const comps = (S.lastCropResult.components || []).filter(c => (c.quantity ?? 1) > 0 || c.spec)
    .map(c => `${c.name} ${c.spec} × ${c.quantity}${c.unit}`).join('，');
  const circs = (S.lastCropResult.circuits || []).map(c => `${c.circuit_no || '回路'} (${c.breaker || ''})`).join('，');
  const reqs = (S.lastCropResult.requirements || []).map(r => `${r.item}: ${r.content}`).join('；');
  const boxInfo = S.lastCropResult.box_info || {};

  const parts = [];
  if (summary) parts.push(`【识别摘要】${summary}`);
  if (boxInfo.code || boxInfo.note) parts.push(`【配电箱说明】${[boxInfo.code, boxInfo.note].filter(Boolean).join(' - ')}`);
  if (reqs) parts.push(`【设计技术要求】${reqs}`);
  if (comps) parts.push(`【元器件】${comps}`);
  if (circs) parts.push(`【回路】${circs}`);

  const text = `我对图纸框选区域进行了深度解析：\n${parts.join('\n')}\n\n请帮我复核这些说明与技术要求对该配电箱的元器件选型、分断能力报价及施工规范有何影响？`;
  switchPane('chat');
  $('cin').value = text;
  $('cin').focus();
}

/* ===================== 对话 ===================== */

function switchPane(name) {
  S.pane = name;
  document.querySelectorAll('.atabs>button[data-t]').forEach(b => b.classList.toggle('active', b.dataset.t === name));
  ['list', 'review', 'chat', 'crop'].forEach(k => { if ($('pane-' + k)) $('pane-' + k).hidden = k !== name; });
  if (name === 'review') renderReview();
}

function atab(name) { switchPane(name); }

function addMsg(who, html) {
  const d = document.createElement('div');
  d.className = 'msg ' + who;
  d.innerHTML = who === 'sys'
    ? `<div class="body">${html}</div>`
    : `<div class="who">${who === 'ai' ? 'AI' : who === 'err' ? '出错' : '我'}</div><div class="body">${html}</div>`;
  const m = $('msgs');
  m.appendChild(d);
  m.scrollTop = m.scrollHeight;
}

window._aiTables = window._aiTables || {};

function renderCustomTableCard(tableData, defaultTitle = '数据整理统计表') {
  if (!tableData || !Array.isArray(tableData.headers) || !tableData.headers.length) return '';
  const title = tableData.title || defaultTitle;
  const headers = tableData.headers;
  const rows = tableData.rows || [];
  const tableId = 'tbl_' + Math.random().toString(36).slice(2, 9);

  window._aiTables[tableId] = { title, headers, rows };

  const ths = headers.map(h =>
    `<th style="background:#f1f5f9;color:#1e293b;padding:7px 10px;border:1px solid #cbd5e1;font-weight:600;text-align:center;white-space:nowrap">${esc(h)}</th>`
  ).join('');

  const trs = rows.map((r, r_idx) => {
    const tds = r.map((cell, c_idx) => {
      const isNum = typeof cell === 'number' || (cell !== '' && !isNaN(cell) && isFinite(cell));
      const align = (c_idx === 0 && !isNum) ? 'center' : (isNum ? 'right' : 'left');
      const val = (isNum && typeof cell === 'number') ? cell.toLocaleString() : (cell ?? '');
      return `<td style="padding:6px 10px;border:1px solid #e2e8f0;text-align:${align};background:${r_idx % 2 === 1 ? '#f8fafc' : '#ffffff'};font-size:12px">${esc(String(val))}</td>`;
    }).join('');
    return `<tr>${tds}</tr>`;
  }).join('');

  return `
  <div class="ai-table-card" id="${tableId}" style="margin:10px 0;border:1px solid #cbd5e1;border-radius:6px;overflow:hidden;background:#ffffff;box-shadow:0 1px 4px rgba(0,0,0,0.06)">
    <div style="display:flex;justify-content:space-between;align-items:center;padding:7px 12px;background:#f8fafc;border-bottom:1px solid #cbd5e1">
      <div style="font-weight:600;font-size:12px;color:#1e293b;display:flex;align-items:center;gap:6px">
        <span>📊</span> <span>${esc(title)}</span>
        <span style="font-size:11px;color:#64748b;font-weight:normal">(${rows.length} 项)</span>
      </div>
      <div style="display:flex;gap:6px">
        <button class="btn-copy-tbl" onclick="copyAiTable('${tableId}')" title="复制表格数据为制表符TSV格式，可直接粘贴到Excel" style="background:#ffffff;border:1px solid #cbd5e1;color:#334155;padding:3px 8px;border-radius:4px;font-size:11px;cursor:pointer;display:inline-flex;align-items:center;gap:3px;font-weight:500">
          📋 复制表格
        </button>
        <button class="btn-export-tbl" onclick="exportAiTable('${tableId}')" title="直接导出为标准Excel工作簿并下载" style="background:#2563eb;border:none;color:#ffffff;padding:3px 10px;border-radius:4px;font-size:11px;font-weight:500;cursor:pointer;display:inline-flex;align-items:center;gap:3px">
          📥 导出 Excel
        </button>
      </div>
    </div>
    <div style="max-height:340px;overflow:auto">
      <table style="width:100%;border-collapse:collapse;font-size:12px;line-height:1.4">
        <thead><tr>${ths}</tr></thead>
        <tbody>${trs}</tbody>
      </table>
    </div>
  </div>`;
}

function copyAiTable(tableId) {
  const t = (window._aiTables && window._aiTables[tableId]) || null;
  if (!t) return toast('未找到表格数据');
  const lines = [];
  lines.push(t.headers.join('\t'));
  t.rows.forEach(r => lines.push(r.map(v => String(v ?? '')).join('\t')));
  const tsv = lines.join('\n');
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(tsv).then(() => {
      toast('✅ 已复制表格数据（TSV格式），可直接粘贴至 Excel');
    }).catch(() => _fallbackCopy(tsv));
  } else {
    _fallbackCopy(tsv);
  }
}

function _fallbackCopy(text) {
  const ta = document.createElement('textarea');
  ta.value = text;
  document.body.appendChild(ta);
  ta.select();
  document.execCommand('copy');
  ta.remove();
  toast('✅ 已复制表格数据，可直接粘贴至 Excel');
}

async function exportAiTable(tableId) {
  const t = (window._aiTables && window._aiTables[tableId]) || null;
  if (!t) return toast('未找到表格数据');
  try {
    toast('正在生成自定义 Excel 报表…');
    const headers = { 'Content-Type': 'application/json' };
    if (Auth.token) headers['Authorization'] = `Bearer ${Auth.token}`;
    const res = await fetch('/api/export_custom_table', {
      method: 'POST',
      headers,
      body: JSON.stringify({
        title: t.title,
        headers: t.headers,
        rows: t.rows,
        filename: `${t.title}.xlsx`
      })
    });
    if (!res.ok) throw new Error('导出失败 (HTTP ' + res.status + ')');
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `${t.title || '数据整理表'}.xlsx`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 4000);
    toast('✅ 自定义 Excel 报表已下载！');
  } catch (e) {
    toast('导出失败：' + e.message);
  }
}

function parseMarkdownTables(text) {
  const lines = text.split('\n');
  const resultParts = [];
  let inTable = false;
  let tableHeaders = [];
  let tableRows = [];
  let currentTextLines = [];

  function flushText() {
    if (currentTextLines.length) {
      resultParts.push(esc(currentTextLines.join('\n'))
        .replace(/\*\*(.+?)\*\*/g, '<b>$1</b>')
        .replace(/\n/g, '<br>'));
      currentTextLines = [];
    }
  }

  function flushTable() {
    if (tableHeaders.length && tableRows.length) {
      resultParts.push(renderCustomTableCard({
        title: '整理统计表',
        headers: tableHeaders,
        rows: tableRows
      }));
    }
    tableHeaders = [];
    tableRows = [];
    inTable = false;
  }

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i].trim();
    if (line.startsWith('|') && line.endsWith('|')) {
      const parts = line.slice(1, -1).split('|').map(s => s.trim());
      // 检查是否是分割线行 | --- | --- |
      if (parts.every(p => /^:?-+:?$/.test(p))) {
        continue;
      }
      if (!inTable) {
        flushText();
        inTable = true;
        tableHeaders = parts;
      } else {
        tableRows.push(parts);
      }
    } else {
      if (inTable) {
        flushTable();
      }
      currentTextLines.push(lines[i]);
    }
  }

  if (inTable) flushTable();
  flushText();

  return resultParts.join('');
}

function mdToHtml(text, customTable = null) {
  if (!text && !customTable) return '';
  let html = parseMarkdownTables(text || '');
  if (customTable && customTable.headers && customTable.headers.length) {
    html += renderCustomTableCard(customTable);
  }
  return html;
}

async function sendQ() {
  const input = $('cin');
  const text = input.value.trim();
  if (!text) return;
  input.value = '';
  if (!S.jobId) { toast('先打开一份图纸'); return; }
  addMsg('me', esc(text));
  switchPane('chat');
  await ask(text);
}

async function ask(text) {
  const R = $('vresult');
  R.classList.add('show');
  $('vtext').textContent = '“' + text + '”';
  $('vintent').textContent = '处理中…';
  $('vout').textContent = '';
  const thinking = document.createElement('div');
  thinking.className = 'msg sys';
  thinking.innerHTML = '<div class="body">正在分析清单…</div>';
  $('msgs').appendChild(thinking);
  $('msgs').scrollTop = $('msgs').scrollHeight;

  try {
    const res = await postJSON(`/api/jobs/${S.jobId}/chat`, { message: text, history: S.chat.slice(-6) });
    thinking.remove();
    S.chat.push({ role: 'user', content: text });
    handleAnswer(res, text);
  } catch (e) {
    thinking.remove();
    $('vintent').textContent = '调用失败';
    $('vout').textContent = e.message;
    addMsg('err', esc(e.message));
    // 失败时把问题放回输入框，直接回车就能重试
    if ($('cin') && !$('cin').value) $('cin').value = text;
    speak('助手这次没返回结果，可以再试一次');
  }
}

function handleAnswer(res, text) {
  if (res.action) { runLocalAction(res.action); return; }
  const reply = res.reply || '（模型没有返回内容）';
  S.chat.push({ role: 'assistant', content: reply });
  addMsg('ai', mdToHtml(reply, res.custom_table));
  $('vintent').textContent = res.source === 'model' ? 'AI 助手' : '指令';
  $('vout').innerHTML = mdToHtml(reply.length > 160 ? reply.slice(0, 160) + '…' : reply);

  if (res.changes && res.changes.length) {
    res.changes.forEach(c => {
      const what = c.action ? (c.action === 'add' ? '新增' : '删除') : (FIELD_LABEL[c.field] || c.field);
      const diff = c.action === 'add' ? `<b>${esc(c.new || '')}</b>`
        : c.action === 'remove' ? esc(c.old || '')
        : `${esc(c.old || '—')} → <b>${esc(c.new)}</b>`;
      addMsg('sys', `已修改 <b>${esc(c.target)}</b> · ${esc(what)}：${diff}`);
    });
  }
  if (res.data) applyServerData(res);
  const TAB_SUB = { circuits: 'circuits', components: 'devices', boxes: 'boxes', requirements: 'reqs', replace: 'replace' };
  if (res.tab && TAB_SUB[res.tab]) { switchPane('list'); renderSub(TAB_SUB[res.tab]); }
  if (res.focus) {
    const i = findByLabel(res.focus);
    if (i >= 0) selectCircuit('c' + i, true);
  }
  if (Array.isArray(res.highlight) && res.highlight.length) highlightSpecs(res.highlight);
}

function highlightSpecs(list) {
  const keys = list.map(String);
  const rows = [...document.querySelectorAll('#tb tr[data-cid]')];
  let hit = 0;
  rows.forEach(tr => {
    const c = findCircuit(tr.dataset.cid) || {};
    const blob = [circuitLabel(c), c.breaker, c.load_name, c.cable, c.phase].join(' ');
    if (keys.some(k => k && blob.includes(k))) { tr.classList.add('sel'); hit++; }
  });
  if (hit) toast(`已高亮 ${hit} 行`);
}

function runLocalAction(action) {
  if (action === 'review') { switchPane('review'); openReview(); $('vintent').textContent = '进入核对'; $('vout').textContent = '已打开存疑核对'; }
  if (action === 'resolve') {
    if (!$('rvoverlay').classList.contains('show')) { $('vout').textContent = '当前不在核对模式，请先说“开始核对”'; return; }
    resolveIssue(true); $('vout').textContent = '已确认当前项';
  }
  if (action === 'next') {
    if (!$('rvoverlay').classList.contains('show')) { $('vout').textContent = '当前不在核对模式'; return; }
    const next = (S.data.uncertainties || []).findIndex((u, i) => i > S.reviewIdx && !u.resolved);
    S.reviewIdx = next < 0 ? (S.data.uncertainties || []).findIndex(u => !u.resolved) : next;
    if (S.reviewIdx >= 0) showIssue();
    $('vout').textContent = '已切换到：' + ($('rvtitle').textContent || '');
  }
  if (action === 'export') { openExport(); $('vout').textContent = '已打开导出前检查'; }
}

/* ===================== 语音 ===================== */

const SR = window.SpeechRecognition || window.webkitSpeechRecognition;

function vstatus(t) { $('vstatus').textContent = t; }

function toggleListen() {
  if (!SR) {
    vstatus('当前浏览器不支持语音识别（Chrome / Edge 可用），可以直接用下方指令词条');
    return;
  }
  if (S.listening && S.rec) { S.rec.stop(); return; }
  try {
    const rec = new SR();
    S.rec = rec;
    rec.lang = 'zh-CN';
    rec.interimResults = true;
    rec.maxAlternatives = 1;
    rec.onstart = () => {
      S.listening = true;
      $('vmic').classList.add('live');
      vstatus('聆听中…请说话');
    };
    rec.onresult = e => {
      let interim = '', final = '';
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const t = e.results[i][0].transcript;
        if (e.results[i].isFinal) final += t; else interim += t;
      }
      if (final) { vstatus(''); addMsg('me', esc(final)); switchPane('chat'); ask(final); }
      else if (interim) vstatus('…' + interim);
    };
    rec.onend = () => {
      S.listening = false;
      $('vmic').classList.remove('live');
      vstatus('');
    };
    rec.onerror = e => {
      S.listening = false;
      $('vmic').classList.remove('live');
      vstatus('识别出错（' + e.error + '），可以改用文字输入');
    };
    rec.start();
  } catch (e) {
    vstatus('无法启动语音识别');
  }
}

// 语音阅读已完全禁用
if ('speechSynthesis' in window) {
  try { speechSynthesis.cancel(); } catch (_) {}
}

function toggleSpeak() {}
function speak() {}

/* ===================== 历史记录 ===================== */

async function loadHistory() {
  const { history } = await api('/api/history');
  $('histcount').textContent = history.length ? `共 ${history.length} 次导出` : '';
  $('histrows').innerHTML = history.map(h => `<tr>
    <td class="mono" style="font-size:11px">${esc((h.exported_at || '').replace('T', ' ').slice(0, 16))}</td>
    <td><b>${esc(h.project || '未分组')}</b></td>
    <td class="mono">${esc(h.filename || '')}</td>
    <td>${h.circuits || 0}</td>
    <td>${h.unresolved ? `<span class="pill warn">${h.unresolved} 未确认</span>`
      : `<span class="pill ok">${h.uncertainties || 0} 已确认</span>`}</td>
    <td>${h.changes || 0} 处</td>
    <td><button class="linklike" onclick="openJob('${h.job_id}')">查看</button>
        <span style="color:var(--line2);margin:0 3px">|</span>
        <button class="linklike" onclick="openBatchProjectModal('${h.job_id}')" title="将此图纸归入或移动到指定项目">归入项目</button>
        <span style="color:var(--line2);margin:0 3px">|</span>
        <button class="linklike" onclick="reparseJob('${h.job_id}')" title="无需重新上传，以最新引擎重新提取该图纸">重新解析</button>
        <span style="color:var(--line2);margin:0 3px">|</span>
        <a class="linklike" href="/api/jobs/${h.job_id}/excel">下载Excel</a></td></tr>`).join('');
  $('histempty').hidden = history.length > 0;
}

/* ===================== 设置 ===================== */

async function loadSettings() {
  const info = await api('/api/settings');
  const s = info.settings;
  S.settings = s;
  $('set-model').value = s.vision_model || '';
  $('set-baseurl').value = s.vision_base_url || '';
  $('set-apikey').value = '';
  $('set-keyhint').textContent = s.vision_api_key_set
    ? `当前已配置（${s.vision_api_key_hint}），留空表示不改动`
    : '当前没有配置 Key，提取和对话都不能用';
  $('set-temp').value = s.temperature ?? 0;
  $('set-seed').value = s.seed ?? '';
  $('set-assistant').value = s.assistant_model || '';
  $('set-template').value = s.excel_template || '';
  $('set-changes').checked = !!s.include_changes;
  $('set-tiling').checked = s.tile_large_pages !== false;
  $('set-prompt').textContent = `extract.txt · v${info.prompt_version}`;
  $('set-contract').textContent = `OUTPUT_CONTRACT · v${info.contract_version}`;
  $('set-status').textContent = info.vision_configured
    ? `当前生效模型：${info.vision_model}（${info.assistant_model || '助手复用同一模型'}）`
    : '视觉模型尚未配置，上传后无法提取';
}

async function saveSettings() {
  const seedRaw = $('set-seed').value.trim();
  const body = {
    vision_model: $('set-model').value.trim(),
    vision_base_url: $('set-baseurl').value.trim(),
    assistant_model: $('set-assistant').value.trim(),
    temperature: parseFloat($('set-temp').value || '0'),
    seed: seedRaw === '' ? null : parseInt(seedRaw, 10),
    excel_template: $('set-template').value.trim(),
    include_changes: $('set-changes').checked,
    tile_large_pages: $('set-tiling').checked,
  };
  const key = $('set-apikey').value.trim();
  if (key) body.vision_api_key = key;
  try {
    const res = await putJSON('/api/settings', body);
    $('set-apikey').value = '';
    $('set-status').textContent = '已保存 ' + new Date().toLocaleTimeString();
    toast('设置已保存');
    loadSettings();
  } catch (e) {
    toast('保存失败：' + e.message);
  }
}

/* ===================== 启动屏 ===================== */

function hideStart() {
  const sc = $('startscreen');
  if (sc) sc.hidden = true;
  if (!S.route) navGo('projects');
}

const SPLIT_MIN_AI = 300;    // 工作区再窄就看不下表格了
const SPLIT_MIN_VIEW = 320;  // 图纸区留这么宽才能真看
const SPLITTER_W = 6;        // 分隔线本身的宽度，算可用空间时要扣掉

/** 工作区宽度存成 CSS 变量，拖分隔线只改这一个值。 */
function applyAiWidth(px) {
  const work = document.querySelector('.workspace');
  if (!work) return;
  const avail = work.clientWidth;
  // 页面隐藏时 clientWidth 是 0，此时不夹紧，等进入工作台再夹
  const max = avail > 0 ? Math.max(SPLIT_MIN_AI, avail - SPLIT_MIN_VIEW - SPLITTER_W) : Infinity;
  S.aiWidth = Math.round(Math.min(Math.max(SPLIT_MIN_AI, px), max));
  work.style.setProperty('--aiw', S.aiWidth + 'px');
}

function setupSplitter() {
  const sp = $('splitter');
  const work = document.querySelector('.workspace');
  if (!sp || !work) return;
  let dragging = false;

  sp.addEventListener('mousedown', e => {
    dragging = true;
    sp.classList.add('dragging');
    document.body.classList.add('col-resizing');
    e.preventDefault();
  });
  window.addEventListener('mousemove', e => {
    if (!dragging) return;
    applyAiWidth(work.getBoundingClientRect().right - e.clientX);
  });
  window.addEventListener('mouseup', () => {
    if (!dragging) return;
    dragging = false;
    sp.classList.remove('dragging');
    document.body.classList.remove('col-resizing');
    try { localStorage.setItem('aiw', S.aiWidth); } catch (err) { /* 隐私模式禁用存储 */ }
  });
  sp.addEventListener('dblclick', () => {
    S.aiWidth = 400;
    try { localStorage.removeItem('aiw'); } catch (err) { /* 忽略 */ }
    applyAiWidth(S.aiWidth);
    toast('工作区宽度已恢复默认 400px');
  });
}

function initCanvasPan() {
  const canvas = $('canvas');
  if (!canvas) return;

  // 监听空格键按住状态
  window.addEventListener('keydown', e => {
    const typing = ['INPUT', 'SELECT', 'TEXTAREA'].includes(e.target.tagName);
    if (typing) return;
    if (e.code === 'Space' && !S.isSpaceDown) {
      S.isSpaceDown = true;
      if (!S.cropMode) canvas.classList.add('space-panning');
      e.preventDefault();
    }
  });

  window.addEventListener('keyup', e => {
    if (e.code === 'Space') {
      S.isSpaceDown = false;
      canvas.classList.remove('space-panning');
    }
  });

  let dragStartX = 0;
  let dragStartY = 0;
  let startPanX = 0;
  let startPanY = 0;

  canvas.addEventListener('mousedown', e => {
    // 框选模式左键优先走拉框
    if (S.cropMode && e.button === 0) return;

    // 中键 (button 1) 或 空格键按住时的左键，或空白背景按住左键拖动平移
    const isMiddle = e.button === 1;
    const isSpaceLeft = e.button === 0 && S.isSpaceDown;
    const isBgLeft = e.button === 0 && !e.target.closest('.clickzone') && !e.target.closest('#cropmarquee');

    if (isMiddle || isSpaceLeft || isBgLeft) {
      if (isMiddle || isSpaceLeft) e.preventDefault();
      S.isDraggingPan = true;
      S.hasMoved = false;
      dragStartX = e.clientX;
      dragStartY = e.clientY;
      startPanX = S.panX || 0;
      startPanY = S.panY || 0;
      canvas.classList.add('is-panning');
    }
  });

  window.addEventListener('mousemove', e => {
    if (!S.isDraggingPan) return;
    const dx = e.clientX - dragStartX;
    const dy = e.clientY - dragStartY;
    if (Math.abs(dx) > 3 || Math.abs(dy) > 3) {
      S.hasMoved = true;
    }
    S.panX = startPanX + dx;
    S.panY = startPanY + dy;
    applyZoom();
  });

  window.addEventListener('mouseup', () => {
    if (S.isDraggingPan) {
      S.isDraggingPan = false;
      canvas.classList.remove('is-panning');
    }
  });
}

async function boot() {
  await checkAuth();
  bindUpload();
  bindManual();
  bindKeys();
  initCropSelection();
  initCanvasPan();
  let saved = 0;
  try { saved = parseInt(localStorage.getItem('aiw') || '', 10) || 0; } catch (err) { saved = 0; }
  S.aiWidth = saved || 400;
  setupSplitter();
  try {
    const info = await api('/api/health');
    $('startcfg').textContent = info.vision_configured
      ? '视觉模型已配置，可以直接上传图纸'
      : '视觉模型还没配置，先去「设置」填 Key，否则上传后无法提取';
  } catch (e) {
    $('startcfg').textContent = '连不上后端服务';
  }
  await fillProjectSelect();
  const { projects } = await api('/api/projects');
  const hasJobs = projects.some(p => (p.jobs || []).length);
  navGo('projects');
  if (hasJobs) {
    hideStart();
  } else {
    $('startscreen').hidden = false;
  }
  $('ubadge').onclick = () => {
    if (!S.jobId) { toast('先去项目里打开一份图纸'); return; }
    navGo('workbench');
    openReview();
  };
}

function bindUpload() {
  const wire = (zoneId, inputId, startsExtract) => {
    const zone = $(zoneId), input = $(inputId);
    if (!zone || !input) return;
    zone.onclick = () => input.click();
    input.onchange = () => {
      if (input.files.length) {
        addFiles(input.files);
        input.value = '';
        if (startsExtract) { navGo('upload'); startExtract(); }
      }
    };
    ['dragenter', 'dragover'].forEach(ev => zone.addEventListener(ev, e => {
      e.preventDefault();
      zone.classList.add('over');
    }));
    ['dragleave', 'drop'].forEach(ev => zone.addEventListener(ev, e => {
      e.preventDefault();
      zone.classList.remove('over');
    }));
    zone.addEventListener('drop', e => {
      if (e.dataTransfer.files.length) {
        addFiles(e.dataTransfer.files);
        if (startsExtract) startExtract();
      }
    });
  };
  wire('dropzone', 'fileinput', false);
  wire('startdrop', 'startfile', true);
}

const VMANUAL = [
  ['查询', [['WL1 用的是什么断路器？', '查指定回路，表格与图纸联动定位'],
            ['统计断路器总数', '按元器件汇总回答分类数量'],
            ['进线容量是多少', '回答 Pjs / Ijs 等技术参数']]],
  ['修改', [['把 WL1 的断路器改成 MCB-63/C20A/1P', '改完自动留痕，可撤销'],
            ['把 WK1 的容量改成 5kW', '可改：相序 / 断路器 / 导线 / 容量 / 用途']]],
  ['核对', [['开始核对', '进入存疑核对（本地指令，不消耗模型调用）'],
            ['确认无误', '确认当前存疑项'],
            ['下一项', '跳到下一处存疑']]],
  ['操作', [['导出', '打开导出前检查'],
            ['切换页签', '直接点清单上方的子页签更快']]],
];

function bindManual() {
  $('vmanual-body').innerHTML = VMANUAL.map(([group, items]) =>
    `<div class="mgroup"><h5>${group}</h5>${items.map(([q, d]) =>
      `<button class="vcmd" data-q="${esc(q)}" onclick="quickAsk(this.dataset.q)">${esc(q)}<small>${esc(d)}</small></button>`).join('')}</div>`).join('');
}

function quickAsk(q) {
  if (!S.jobId) { toast('先打开一份图纸'); return; }
  $('cin').value = q;
  sendQ();
}

function bindKeys() {
  document.addEventListener('keydown', e => {
    const typing = ['INPUT', 'SELECT', 'TEXTAREA'].includes(e.target.tagName);
    if (e.key === 'Escape') { closeAuthModal(); closeAiLogs(); closeReview(); closeExport(); closeLog(); closeProjBom(); closeProjTopology(); closeBatchProjectModal(); return; }
    if (typing) return;
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 's') { e.preventDefault(); saveNow(); return; }
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'z') { e.preventDefault(); undoLastChange(); return; }
    if (S.route !== 'workbench' || S.sub !== 'circuits') return;
    const rows = [...document.querySelectorAll('#tb tr[data-cid]')];
    const cur = rows.findIndex(r => r.classList.contains('sel'));
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      const n = e.key === 'ArrowDown' ? Math.min(rows.length - 1, cur + 1) : Math.max(0, cur - 1);
      if (rows[n]) selectCircuit(rows[n].dataset.cid, true);
    }
    if (e.key === 'e' || e.key === 'E') openExport();
  });
}

boot();
