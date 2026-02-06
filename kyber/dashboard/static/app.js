/* ── Kyber Dashboard ── */
const API = '/api';
const TOKEN_KEY = 'kyber_dashboard_token';

// DOM refs
const $ = (s) => document.getElementById(s);
const loginModal = $('loginModal');
const tokenInput = $('tokenInput');
const tokenSubmit = $('tokenSubmit');
const statusPill = $('statusPill');
const statusText = $('statusText');
const savedAt = $('savedAt');
const pageTitle = $('pageTitle');
const pageDesc = $('pageDesc');
const contentBody = $('contentBody');
const saveBtn = $('saveBtn');
const refreshBtn = $('refreshBtn');
const toast = $('toast');

let config = null;
let configSnapshot = null;
let isDirty = false;
let activeSection = 'providers';
let toastTimer = null;

// ── Section metadata ──
const SECTIONS = {
  providers: {
    title: 'Providers',
    desc: 'Configure your LLM provider API keys and endpoints.',
  },
  agents: {
    title: 'Agent',
    desc: 'Default model, workspace, and tool loop settings.',
  },
  channels: {
    title: 'Channels',
    desc: 'Enable and configure chat platform integrations.',
  },
  tools: {
    title: 'Tools',
    desc: 'Web search and shell execution settings.',
  },
  gateway: {
    title: 'Gateway',
    desc: 'Host and port for the Kyber gateway server.',
  },
  dashboard: {
    title: 'Dashboard',
    desc: 'Dashboard access, auth token, and allowed hosts.',
  },
  json: {
    title: 'Raw JSON',
    desc: 'View and edit the full configuration as JSON.',
  },
};

// ── Helpers ──
function showToast(msg, type = 'info') {
  toast.textContent = msg;
  toast.className = 'toast ' + (type === 'error' ? 'error' : type === 'success' ? 'success' : '');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.add('hidden'), 2500);
}

function getToken() { return sessionStorage.getItem(TOKEN_KEY) || ''; }
function setToken(t) { sessionStorage.setItem(TOKEN_KEY, t); }

function humanize(key) {
  return key
    .replace(/([a-z0-9])([A-Z])/g, '$1 $2')
    .replace(/_/g, ' ')
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

function setPath(obj, path, val) {
  let t = obj;
  for (let i = 0; i < path.length - 1; i++) t = t[path[i]];
  t[path[path.length - 1]] = val;
}

function isObj(v) { return v && typeof v === 'object' && !Array.isArray(v); }

function isSensitive(key) {
  const k = key.toLowerCase();
  return k.includes('token') || k.includes('key') || k.includes('secret');
}

function markDirty() {
  isDirty = true;
  saveBtn.disabled = false;
  saveBtn.classList.remove('disabled');
}

function markClean() {
  isDirty = false;
  configSnapshot = JSON.stringify(config);
  saveBtn.disabled = true;
  saveBtn.classList.add('disabled');
}

// ── API ──
async function apiFetch(path, opts = {}) {
  const headers = { ...opts.headers };
  const token = getToken();
  if (token) headers['Authorization'] = `Bearer ${token}`;
  if (opts.body) headers['Content-Type'] = 'application/json';
  const res = await fetch(path, { ...opts, headers });
  if (res.status === 401) {
    statusText.textContent = 'Locked';
    statusPill.className = 'status-pill error';
    showLogin();
    throw new Error('Unauthorized');
  }
  return res;
}

function showLogin() { loginModal.classList.remove('hidden'); tokenInput.value = ''; tokenInput.focus(); }
function hideLogin() { loginModal.classList.add('hidden'); }

async function loadConfig() {
  try {
    statusText.textContent = 'Connecting…';
    statusPill.className = 'status-pill';
    const res = await apiFetch(`${API}/config`);
    config = await res.json();
    statusText.textContent = 'Connected';
    statusPill.className = 'status-pill connected';
    markClean();
    renderSection();
  } catch (e) {
    console.error(e);
  }
}

async function saveConfig() {
  if (!config) return;
  let payload = config;

  if (activeSection === 'json') {
    const ta = contentBody.querySelector('.json-editor');
    if (ta) {
      try { payload = JSON.parse(ta.value); }
      catch { showToast('Invalid JSON', 'error'); return; }
    }
  }

  try {
    const res = await apiFetch(`${API}/config`, { method: 'PUT', body: JSON.stringify(payload) });
    config = await res.json();
    savedAt.textContent = 'Saved ' + new Date().toLocaleTimeString();
    showToast('Configuration saved', 'success');
    markClean();
    renderSection();
  } catch {
    showToast('Save failed', 'error');
  }
}

// ── Navigation ──
function switchSection(section) {
  activeSection = section;
  document.querySelectorAll('.nav-item').forEach((btn) => {
    btn.classList.toggle('active', btn.dataset.section === section);
  });
  const meta = SECTIONS[section] || {};
  pageTitle.textContent = meta.title || humanize(section);
  pageDesc.textContent = meta.desc || '';
  renderSection();
}

// ── Rendering ──
function renderSection() {
  if (!config) { contentBody.innerHTML = '<div class="empty-state">Loading configuration…</div>'; return; }
  contentBody.innerHTML = '';

  if (activeSection === 'json') {
    renderJSON();
    return;
  }

  const data = config[activeSection];
  if (!data || !isObj(data)) {
    contentBody.innerHTML = '<div class="empty-state">No configuration for this section.</div>';
    return;
  }

  // Special renderers
  if (activeSection === 'providers') { renderProviders(data); return; }
  if (activeSection === 'channels') { renderChannels(data); return; }
  if (activeSection === 'agents') { renderAgents(data); return; }
  if (activeSection === 'tools') { renderTools(data); return; }
  if (activeSection === 'dashboard') { renderDashboard(data); return; }

  // Generic card
  const card = makeCard(humanize(activeSection));
  renderFields(card.body, data, [activeSection]);
  contentBody.appendChild(card.el);
}

// ── Card factory ──
function makeCard(title, badge) {
  const el = document.createElement('div');
  el.className = 'card';

  const header = document.createElement('div');
  header.className = 'card-header';
  const h = document.createElement('span');
  h.className = 'card-title';
  h.textContent = title;
  header.appendChild(h);

  if (badge !== undefined) {
    const b = document.createElement('span');
    b.className = 'card-badge' + (badge ? ' on' : '');
    b.textContent = badge ? 'Enabled' : 'Disabled';
    header.appendChild(b);
  }

  el.appendChild(header);
  const body = document.createElement('div');
  body.className = 'card-body';
  el.appendChild(body);
  contentBody.appendChild(el);
  return { el, body };
}

// ── Field rendering ──
function renderFields(container, obj, path) {
  for (const [key, value] of Object.entries(obj)) {
    const fullPath = [...path, key];

    if (isObj(value)) {
      // Nested object — sub-card
      const sub = document.createElement('div');
      sub.className = 'card';
      sub.style.marginTop = '12px';
      sub.style.border = '1px solid var(--border)';
      const sh = document.createElement('div');
      sh.className = 'card-header';
      sh.innerHTML = `<span class="card-title">${humanize(key)}</span>`;
      sub.appendChild(sh);
      const sb = document.createElement('div');
      sb.className = 'card-body';
      sub.appendChild(sb);
      renderFields(sb, value, fullPath);
      container.appendChild(sub);
      continue;
    }

    if (Array.isArray(value)) {
      renderArrayField(container, key, value, fullPath);
      continue;
    }

    renderField(container, key, value, fullPath);
  }
}

function renderField(container, key, value, path) {
  const row = document.createElement('div');
  row.className = 'field-row';

  const label = document.createElement('div');
  label.className = 'field-label';
  label.textContent = humanize(key);
  row.appendChild(label);

  const inputWrap = document.createElement('div');
  inputWrap.className = 'field-input';

  if (typeof value === 'boolean') {
    const wrap = document.createElement('div');
    wrap.className = 'checkbox-wrap';
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.checked = value;
    cb.id = 'cb-' + path.join('-');
    cb.addEventListener('change', () => {
      setPath(config, path, cb.checked);
      markDirty();
      if (key === 'enabled') renderSection();
    });
    wrap.appendChild(cb);
    const lbl = document.createElement('label');
    lbl.className = 'checkbox-label';
    lbl.htmlFor = cb.id;
    lbl.textContent = value ? 'Yes' : 'No';
    cb.addEventListener('change', () => { lbl.textContent = cb.checked ? 'Yes' : 'No'; });
    wrap.appendChild(lbl);
    inputWrap.appendChild(wrap);
  } else if (typeof value === 'number') {
    const inp = document.createElement('input');
    inp.type = 'number';
    inp.value = value;
    inp.addEventListener('input', () => {
      const n = Number(inp.value);
      setPath(config, path, Number.isNaN(n) ? 0 : n);
      markDirty();
    });
    inputWrap.appendChild(inp);
  } else {
    const inp = document.createElement('input');
    inp.type = isSensitive(key) ? 'password' : 'text';
    inp.value = value || '';
    inp.placeholder = isSensitive(key) ? '••••••••' : '';
    inp.addEventListener('input', () => { setPath(config, path, inp.value); markDirty(); });
    inputWrap.appendChild(inp);
  }

  row.appendChild(inputWrap);
  container.appendChild(row);
}

function renderArrayField(container, key, arr, path) {
  const row = document.createElement('div');
  row.className = 'field-row';
  row.style.alignItems = 'flex-start';

  const label = document.createElement('div');
  label.className = 'field-label';
  label.style.paddingTop = '8px';
  label.textContent = humanize(key);
  row.appendChild(label);

  const wrap = document.createElement('div');
  wrap.className = 'field-input array-field';

  const rebuild = () => {
    wrap.innerHTML = '';
    arr.forEach((item, i) => {
      const r = document.createElement('div');
      r.className = 'array-row';
      const inp = document.createElement('input');
      inp.type = 'text';
      inp.value = item;
      inp.addEventListener('input', () => { arr[i] = inp.value; markDirty(); });
      r.appendChild(inp);

      const del = document.createElement('button');
      del.className = 'btn-icon danger';
      del.innerHTML = '<svg width="12" height="12" viewBox="0 0 16 16" fill="none"><path d="M4 4l8 8M12 4l-8 8" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>';
      del.addEventListener('click', () => { arr.splice(i, 1); markDirty(); rebuild(); });
      r.appendChild(del);
      wrap.appendChild(r);
    });

    const add = document.createElement('button');
    add.className = 'btn-add';
    add.innerHTML = '<svg width="10" height="10" viewBox="0 0 16 16" fill="none"><path d="M8 2v12M2 8h12" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg> Add';
    add.addEventListener('click', () => { arr.push(''); markDirty(); rebuild(); });
    wrap.appendChild(add);
  };

  rebuild();
  row.appendChild(wrap);
  container.appendChild(row);
}

// ── Section-specific renderers ──

function renderProviders(data) {
  const providerNames = ['anthropic', 'openai', 'openrouter', 'deepseek', 'groq', 'gemini', 'zhipu', 'vllm'];
  for (const name of providerNames) {
    const prov = data[name];
    if (!prov) continue;
    const hasKey = !!(prov.apiKey || prov.api_key);
    const card = makeCard(humanize(name), hasKey);
    renderFields(card.body, prov, ['providers', name]);
  }
}

function renderChannels(data) {
  const channelNames = ['discord', 'telegram', 'whatsapp', 'feishu'];
  for (const name of channelNames) {
    const ch = data[name];
    if (!ch) continue;
    const card = makeCard(humanize(name), ch.enabled);
    renderFields(card.body, ch, ['channels', name]);
  }
}

function renderAgents(data) {
  if (data.defaults) {
    const card = makeCard('Agent Defaults');
    renderFields(card.body, data.defaults, ['agents', 'defaults']);
  } else {
    const card = makeCard('Agent');
    renderFields(card.body, data, ['agents']);
  }
}

function renderTools(data) {
  if (data.web) {
    if (data.web.search) {
      const card = makeCard('Web Search');
      renderFields(card.body, data.web.search, ['tools', 'web', 'search']);
    }
  }
  if (data.exec) {
    const card = makeCard('Shell Execution');
    renderFields(card.body, data.exec, ['tools', 'exec']);
  }
}

function renderDashboard(data) {
  // No enabled/disabled badge — if you're viewing this, the dashboard is running
  const card = makeCard('Dashboard Settings');
  // Render all fields except "enabled" since it's meaningless here
  const filtered = Object.fromEntries(
    Object.entries(data).filter(([k]) => k !== 'enabled')
  );
  renderFields(card.body, filtered, ['dashboard']);
}

function renderJSON() {
  const ta = document.createElement('textarea');
  ta.className = 'json-editor';
  ta.spellcheck = false;
  ta.value = JSON.stringify(config, null, 2);
  ta.addEventListener('input', () => {
    markDirty();
    try {
      JSON.parse(ta.value);
      ta.style.borderColor = '';
    } catch {
      ta.style.borderColor = 'var(--red)';
    }
  });
  contentBody.appendChild(ta);
}

// ── Event listeners ──
document.getElementById('sidebarNav').addEventListener('click', (e) => {
  const btn = e.target.closest('.nav-item');
  if (btn && btn.dataset.section) switchSection(btn.dataset.section);
});

saveBtn.addEventListener('click', saveConfig);
refreshBtn.addEventListener('click', loadConfig);

tokenSubmit.addEventListener('click', async () => {
  const t = tokenInput.value.trim();
  if (!t) return;
  setToken(t);
  hideLogin();
  await loadConfig();
});

tokenInput.addEventListener('keydown', (e) => {
  if (e.key === 'Enter') tokenSubmit.click();
});

// ── Init ──
window.addEventListener('load', async () => {
  if (!getToken()) { showLogin(); }
  else { await loadConfig(); }
});
