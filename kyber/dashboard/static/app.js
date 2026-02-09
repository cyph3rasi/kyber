/* ── Kyber Dashboard ── */
const API = '/api';
const TOKEN_KEY = 'kyber_dashboard_token';
const TASKS_AUTOREFRESH_KEY = 'kyber_tasks_autorefresh';
const DEBUG_AUTOREFRESH_KEY = 'kyber_debug_autorefresh';
const SKILLS_AUTOREFRESH_KEY = 'kyber_skills_autorefresh';
const CRON_AUTOREFRESH_KEY = 'kyber_cron_autorefresh';
const SECURITY_AUTOREFRESH_KEY = 'kyber_security_autorefresh';
const LAST_SECTION_KEY = 'kyber_dashboard_last_section';
const SCROLL_KEY_PREFIX = 'kyber_dashboard_scroll:';

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
const restartGwBtn = $('restartGwBtn');
const restartDashBtn = $('restartDashBtn');
const toast = $('toast');

let config = null;
let configSnapshot = null;
let isDirty = false;
let activeSection = 'providers';
let toastTimer = null;
let tasksPollTimer = null;
let restoreScrollAfterRender = false;
let pendingScrollY = 0;

// Cache fetched models per provider to avoid re-fetching
const modelCache = {};

// ── Section metadata ──
const SECTIONS = {
  providers: {
    title: 'Providers',
    desc: 'Configure your LLM providers and select models.',
  },
  agents: {
    title: 'Agent',
    desc: 'Select your active provider and configure agent settings.',
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
  skills: {
    title: 'Skills',
    desc: 'Install and manage SKILL.md packages (skills.sh compatible).',
  },
  cron: {
    title: 'Cron Jobs',
    desc: 'Schedule recurring or one-time tasks for your agent.',
  },
  security: {
    title: 'Security Center',
    desc: 'Environment security scans, findings, and recommendations.',
  },
  tasks: {
    title: 'Tasks',
    desc: 'View running background tasks, cancel them, and inspect recent results.',
  },
  debug: {
    title: 'Debug',
    desc: 'Error-only logs from the gateway (for quick copy/paste while remote).',
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

const BUILTIN_PROVIDERS = ['anthropic', 'openai', 'openrouter', 'deepseek', 'groq', 'gemini'];

// ── Helpers ──
function showToast(msg, type = 'info') {
  toast.textContent = msg;
  toast.className = 'toast ' + (type === 'error' ? 'error' : type === 'success' ? 'success' : '');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.add('hidden'), 2500);
}

function getToken() { return sessionStorage.getItem(TOKEN_KEY) || ''; }
function setToken(t) { sessionStorage.setItem(TOKEN_KEY, t); }
function getSavedSection() { return localStorage.getItem(LAST_SECTION_KEY) || ''; }
function setSavedSection(s) { localStorage.setItem(LAST_SECTION_KEY, s); }
function getSavedScroll(section) { return Number(sessionStorage.getItem(SCROLL_KEY_PREFIX + section) || '0') || 0; }
function setSavedScroll(section, y) { sessionStorage.setItem(SCROLL_KEY_PREFIX + section, String(Math.max(0, y || 0))); }

function humanize(key) {
  return key
    .replace(/([a-z0-9])([A-Z])/g, '$1 $2')
    .replace(/_/g, ' ')
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

function setPath(obj, path, val) {
  let t = obj;
  for (let i = 0; i < path.length - 1; i++) {
    if (t[path[i]] === undefined) t[path[i]] = {};
    t = t[path[i]];
  }
  t[path[path.length - 1]] = val;
}

function getPath(obj, path) {
  let t = obj;
  for (const k of path) {
    if (t == null) return undefined;
    t = t[k];
  }
  return t;
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
    const saved = getSavedSection();
    if (saved && SECTIONS[saved]) {
      switchSection(saved);
    } else {
      // Ensure UI matches whatever default is active.
      switchSection(activeSection);
    }
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
    const gwRestarted = config._gatewayRestarted;
    const gwMessage = config._gatewayMessage;
    delete config._gatewayRestarted;
    delete config._gatewayMessage;
    savedAt.textContent = 'Saved ' + new Date().toLocaleTimeString();
    if (gwRestarted) {
      showToast('Saved — gateway restarted', 'success');
    } else {
      showToast('Saved — gateway restart failed: ' + (gwMessage || 'unknown'), 'error');
    }
    markClean();
    renderSection();
  } catch {
    showToast('Save failed', 'error');
  }
}

// ── Model fetching ──
async function fetchModels(providerName, apiKey, apiBase) {
  const cacheKey = `${providerName}:${apiKey}:${apiBase || ''}`;
  if (modelCache[cacheKey]) return modelCache[cacheKey];

  const params = new URLSearchParams({ apiKey });
  if (apiBase) params.set('apiBase', apiBase);

  const res = await apiFetch(`${API}/providers/${providerName}/models?${params}`);
  const data = await res.json();
  if (data.models && data.models.length > 0) {
    modelCache[cacheKey] = data.models;
    return data.models;
  }
  throw new Error(data.error || 'No models returned');
}

// ── Navigation ──
function switchSection(section) {
  // Persist scroll for the old section before we switch.
  if (activeSection) setSavedScroll(activeSection, window.scrollY);
  activeSection = section;
  setSavedSection(section);
  restoreScrollAfterRender = true;
  pendingScrollY = getSavedScroll(section);
  if (tasksPollTimer) {
    clearInterval(tasksPollTimer);
    tasksPollTimer = null;
  }
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

  const finishRender = () => {
    if (restoreScrollAfterRender) {
      const y = pendingScrollY || 0;
      restoreScrollAfterRender = false;
      pendingScrollY = 0;
      requestAnimationFrame(() => window.scrollTo(0, y));
    }
  };

  if (activeSection === 'json') { renderJSON(); finishRender(); return; }
  if (activeSection === 'tasks') { renderTasks(); finishRender(); return; }
  if (activeSection === 'debug') { renderDebug(); finishRender(); return; }
  if (activeSection === 'skills') { renderSkills(); finishRender(); return; }
  if (activeSection === 'cron') { renderCron(); finishRender(); return; }
  if (activeSection === 'security') { renderSecurity(); finishRender(); return; }

  const data = config[activeSection];
  if (!data || !isObj(data)) {
    contentBody.innerHTML = '<div class="empty-state">No configuration for this section.</div>';
    return;
  }

  if (activeSection === 'providers') { renderProviders(data); finishRender(); return; }
  if (activeSection === 'channels') { renderChannels(data); finishRender(); return; }
  if (activeSection === 'agents') { renderAgents(data); finishRender(); return; }
  if (activeSection === 'tools') { renderTools(data); finishRender(); return; }
  if (activeSection === 'dashboard') { renderDashboard(data); finishRender(); return; }

  // Generic card
  const card = makeCard(humanize(activeSection));
  renderFields(card.body, data, [activeSection]);
  contentBody.appendChild(card.el);
  finishRender();
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

// ── Provider card with API key + model dropdown ──

function renderProviderCard(name, provObj, configPath, opts = {}) {
  const apiKeyField = provObj.apiKey ?? provObj.api_key ?? '';
  const chatModelField = provObj.chatModel ?? provObj.chat_model ?? '';
  const taskModelField = provObj.taskModel ?? provObj.task_model ?? '';
  const legacyModelField = provObj.model ?? '';
  const hasKey = !!apiKeyField;
  const card = makeCard(opts.displayName || humanize(name), hasKey);

  // Keep a direct reference to the base input (avoids querySelector issues)
  let baseInpRef = null;

  // API Base (only for custom providers)
  if (opts.showApiBase) {
    const baseRow = document.createElement('div');
    baseRow.className = 'field-row';
    const baseLabel = document.createElement('div');
    baseLabel.className = 'field-label';
    baseLabel.textContent = 'API Base URL';
    baseRow.appendChild(baseLabel);
    const baseWrap = document.createElement('div');
    baseWrap.className = 'field-input';
    const baseInp = document.createElement('input');
    baseInp.type = 'text';
    baseInp.className = 'api-base-input';
    baseInp.value = provObj.apiBase ?? provObj.api_base ?? '';
    baseInp.placeholder = 'https://your-server.com/v1';
    baseInp.addEventListener('input', () => {
      setPath(config, [...configPath, 'apiBase'], baseInp.value);
      markDirty();
    });
    baseWrap.appendChild(baseInp);
    baseRow.appendChild(baseWrap);
    card.body.appendChild(baseRow);
    baseInpRef = baseInp;
  }

  // API Key row
  const keyRow = document.createElement('div');
  keyRow.className = 'field-row';
  const keyLabel = document.createElement('div');
  keyLabel.className = 'field-label';
  keyLabel.textContent = 'API Key';
  keyRow.appendChild(keyLabel);
  const keyWrap = document.createElement('div');
  keyWrap.className = 'field-input';
  const apiKeyInp = document.createElement('input');
  apiKeyInp.type = 'password';
  apiKeyInp.value = apiKeyField;
  apiKeyInp.placeholder = '••••••••';
  keyWrap.appendChild(apiKeyInp);
  keyRow.appendChild(keyWrap);
  card.body.appendChild(keyRow);

  // Chat Model row
  const chatModelRow = document.createElement('div');
  chatModelRow.className = 'field-row';
  const chatModelLabel = document.createElement('div');
  chatModelLabel.className = 'field-label';
  chatModelLabel.textContent = 'Chat Model';
  chatModelRow.appendChild(chatModelLabel);
  const chatModelWrap = document.createElement('div');
  chatModelWrap.className = 'field-input';
  chatModelRow.appendChild(chatModelWrap);
  card.body.appendChild(chatModelRow);

  // Task Model row
  const taskModelRow = document.createElement('div');
  taskModelRow.className = 'field-row';
  const taskModelLabel = document.createElement('div');
  taskModelLabel.className = 'field-label';
  taskModelLabel.textContent = 'Task Model';
  taskModelRow.appendChild(taskModelLabel);
  const taskModelWrap = document.createElement('div');
  taskModelWrap.className = 'field-input';
  taskModelRow.appendChild(taskModelWrap);
  card.body.appendChild(taskModelRow);

  // Determine the provider name to use for API calls
  const fetchName = opts.fetchName || name;

  function renderModelDropdown(wrap, currentModel, configKey, placeholderText) {
    wrap.innerHTML = '';
    const currentKey = apiKeyInp.value.trim();
    const currentBase = baseInpRef ? baseInpRef.value.trim() : null;

    if (!currentKey) {
      const hint = document.createElement('div');
      hint.className = 'model-hint';
      hint.textContent = 'Enter API key to see available models';
      wrap.appendChild(hint);
      return;
    }

    if (opts.showApiBase && !currentBase) {
      const hint = document.createElement('div');
      hint.className = 'model-hint';
      hint.textContent = 'Enter API base URL and API key to see available models';
      wrap.appendChild(hint);
      return;
    }

    const loading = document.createElement('div');
    loading.className = 'model-hint';
    loading.textContent = 'Loading models…';
    wrap.appendChild(loading);

    fetchModels(fetchName, currentKey, currentBase)
      .then((models) => {
        wrap.innerHTML = '';
        const sel = document.createElement('select');
        const emptyOpt = document.createElement('option');
        emptyOpt.value = '';
        emptyOpt.textContent = placeholderText;
        sel.appendChild(emptyOpt);

        for (const m of models) {
          const opt = document.createElement('option');
          opt.value = m;
          opt.textContent = m;
          if (m === currentModel) opt.selected = true;
          sel.appendChild(opt);
        }

        if (currentModel && !models.includes(currentModel)) {
          const opt = document.createElement('option');
          opt.value = currentModel;
          opt.textContent = currentModel + ' (current)';
          opt.selected = true;
          sel.appendChild(opt);
        }

        sel.addEventListener('change', () => {
          setPath(config, [...configPath, configKey], sel.value);
          markDirty();
        });
        wrap.appendChild(sel);
      })
      .catch((err) => {
        wrap.innerHTML = '';
        const errEl = document.createElement('div');
        errEl.className = 'model-hint error-text';
        errEl.textContent = 'Failed to load models: ' + (err.message || err);
        wrap.appendChild(errEl);

        const retry = document.createElement('button');
        retry.className = 'btn-add';
        retry.textContent = 'Retry';
        retry.style.marginTop = '6px';
        retry.addEventListener('click', () => {
          const ck = `${fetchName}:${currentKey}:${currentBase || ''}`;
          delete modelCache[ck];
          renderModelDropdown(wrap, currentModel, configKey, placeholderText);
        });
        wrap.appendChild(retry);
      });
  }

  function renderModelAreas() {
    renderModelDropdown(chatModelWrap, chatModelField || legacyModelField, 'chatModel', '— Select chat model —');
    renderModelDropdown(taskModelWrap, taskModelField || legacyModelField, 'taskModel', '— Select task model —');
  }

  // Wire up API key changes
  let keyDebounce = null;
  apiKeyInp.addEventListener('input', () => {
    setPath(config, [...configPath, 'apiKey'], apiKeyInp.value);
    markDirty();
    clearTimeout(keyDebounce);
    keyDebounce = setTimeout(renderModelAreas, 600);
  });

  // Wire up API base changes via direct ref
  if (baseInpRef) {
    let baseDebounce = null;
    baseInpRef.addEventListener('input', () => {
      clearTimeout(baseDebounce);
      baseDebounce = setTimeout(renderModelAreas, 600);
    });
  }

  // Initial render of model areas
  renderModelAreas();

  return card;
}

// ── Section-specific renderers ──

function renderProviders(data) {
  for (const name of BUILTIN_PROVIDERS) {
    const prov = data[name];
    if (!prov) continue;
    const opts = {};
    renderProviderCard(name, prov, ['providers', name], opts);
  }

  // Custom providers
  const customs = data.custom || [];
  customs.forEach((cp, i) => {
    const card = renderProviderCard(cp.name || `custom-${i}`, cp, ['providers', 'custom', i], {
      showApiBase: true,
      displayName: cp.name || `Custom Provider ${i + 1}`,
      fetchName: 'custom',
    });

    // Add name field at the top of the card body (before other fields)
    const nameRow = document.createElement('div');
    nameRow.className = 'field-row';
    const nameLabel = document.createElement('div');
    nameLabel.className = 'field-label';
    nameLabel.textContent = 'Provider Name';
    nameRow.appendChild(nameLabel);
    const nameWrap = document.createElement('div');
    nameWrap.className = 'field-input';
    const nameInp = document.createElement('input');
    nameInp.type = 'text';
    nameInp.value = cp.name || '';
    nameInp.placeholder = 'e.g. ollama, together, etc.';
    nameInp.addEventListener('input', () => {
      setPath(config, ['providers', 'custom', i, 'name'], nameInp.value);
      markDirty();
    });
    nameWrap.appendChild(nameInp);
    nameRow.appendChild(nameWrap);
    card.body.insertBefore(nameRow, card.body.firstChild);

    // Add delete button in card header
    const delBtn = document.createElement('button');
    delBtn.className = 'btn-icon danger';
    delBtn.title = 'Remove this provider';
    delBtn.innerHTML = '<svg width="12" height="12" viewBox="0 0 16 16" fill="none"><path d="M4 4l8 8M12 4l-8 8" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>';
    delBtn.addEventListener('click', () => {
      config.providers.custom.splice(i, 1);
      markDirty();
      renderSection();
    });
    card.el.querySelector('.card-header').appendChild(delBtn);
  });

  // Add custom provider button
  const addBtn = document.createElement('button');
  addBtn.className = 'btn btn-ghost btn-full';
  addBtn.style.marginTop = '16px';
  addBtn.innerHTML = '<svg width="14" height="14" viewBox="0 0 16 16" fill="none"><path d="M8 2v12M2 8h12" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg> Add Custom Provider';
  addBtn.addEventListener('click', () => {
    if (!config.providers.custom) config.providers.custom = [];
    config.providers.custom.push({ name: '', apiBase: '', apiKey: '', model: '', chatModel: '', taskModel: '' });
    markDirty();
    renderSection();
  });
  contentBody.appendChild(addBtn);
}

function renderChannels(data) {
  const channelNames = ['discord', 'telegram', 'whatsapp'];
  for (const name of channelNames) {
    const ch = data[name];
    if (!ch) continue;
    const card = makeCard(humanize(name), ch.enabled);
    renderFields(card.body, ch, ['channels', name]);
  }
}

function renderAgents(data) {
  if (!data.defaults) {
    const card = makeCard('Agent');
    renderFields(card.body, data, ['agents']);
    return;
  }

  const defaults = data.defaults;
  const card = makeCard('Agent Defaults');

  const providers = config.providers || {};
  const currentChatProvider = (defaults.chatProvider || defaults.chat_provider || '').toLowerCase();
  const currentTaskProvider = (defaults.taskProvider || defaults.task_provider || '').toLowerCase();
  const currentLegacyProvider = (defaults.provider || '').toLowerCase();

  // Helper: build a provider <select> element
  function buildProviderSelect(currentValue, fallbackValue, configKey, label) {
    const row = document.createElement('div');
    row.className = 'field-row';
    const lbl = document.createElement('div');
    lbl.className = 'field-label';
    lbl.textContent = label;
    row.appendChild(lbl);
    const wrap = document.createElement('div');
    wrap.className = 'field-input';

    const sel = document.createElement('select');
    const emptyOpt = document.createElement('option');
    emptyOpt.value = '';
    emptyOpt.textContent = '— Use default provider —';
    sel.appendChild(emptyOpt);

    const selected = (currentValue || '').toLowerCase();

    for (const name of BUILTIN_PROVIDERS) {
      const prov = providers[name];
      if (!prov) continue;
      const hasKey = !!(prov.apiKey || prov.api_key);
      if (!hasKey) continue;
      const opt = document.createElement('option');
      opt.value = name;
      // Show which models are configured for this role
      const roleModel = configKey === 'chatProvider'
        ? (prov.chatModel || prov.chat_model || prov.model || '')
        : (prov.taskModel || prov.task_model || prov.model || '');
      const modelInfo = roleModel ? ` (${roleModel})` : '';
      opt.textContent = humanize(name) + modelInfo;
      if (selected === name) opt.selected = true;
      sel.appendChild(opt);
    }

    const customs = providers.custom || [];
    for (const cp of customs) {
      if (!cp.name || !cp.apiKey) continue;
      const opt = document.createElement('option');
      opt.value = cp.name.toLowerCase();
      const roleModel = configKey === 'chatProvider'
        ? (cp.chatModel || cp.chat_model || cp.model || '')
        : (cp.taskModel || cp.task_model || cp.model || '');
      const modelInfo = roleModel ? ` (${roleModel})` : '';
      opt.textContent = cp.name + modelInfo;
      if (selected === cp.name.toLowerCase()) opt.selected = true;
      sel.appendChild(opt);
    }

    if (selected && !sel.querySelector(`option[value="${selected}"]`)) {
      const opt = document.createElement('option');
      opt.value = selected;
      opt.textContent = humanize(selected) + ' (not configured)';
      opt.selected = true;
      sel.appendChild(opt);
    }

    sel.addEventListener('change', () => {
      setPath(config, ['agents', 'defaults', configKey], sel.value);
      markDirty();
    });

    wrap.appendChild(sel);
    row.appendChild(wrap);
    return row;
  }

  // Default Provider (legacy fallback)
  const defaultProvRow = document.createElement('div');
  defaultProvRow.className = 'field-row';
  const defaultProvLabel = document.createElement('div');
  defaultProvLabel.className = 'field-label';
  defaultProvLabel.textContent = 'Default Provider';
  defaultProvRow.appendChild(defaultProvLabel);
  const defaultProvWrap = document.createElement('div');
  defaultProvWrap.className = 'field-input';

  const defaultSel = document.createElement('select');
  const defaultEmpty = document.createElement('option');
  defaultEmpty.value = '';
  defaultEmpty.textContent = '— Select a provider —';
  defaultSel.appendChild(defaultEmpty);

  for (const name of BUILTIN_PROVIDERS) {
    const prov = providers[name];
    if (!prov) continue;
    const hasKey = !!(prov.apiKey || prov.api_key);
    if (!hasKey) continue;
    const opt = document.createElement('option');
    opt.value = name;
    opt.textContent = humanize(name);
    if (currentLegacyProvider === name) opt.selected = true;
    defaultSel.appendChild(opt);
  }
  const customs = providers.custom || [];
  for (const cp of customs) {
    if (!cp.name || !cp.apiKey) continue;
    const opt = document.createElement('option');
    opt.value = cp.name.toLowerCase();
    opt.textContent = cp.name;
    if (currentLegacyProvider === cp.name.toLowerCase()) opt.selected = true;
    defaultSel.appendChild(opt);
  }
  if (currentLegacyProvider && !defaultSel.querySelector(`option[value="${currentLegacyProvider}"]`)) {
    const opt = document.createElement('option');
    opt.value = currentLegacyProvider;
    opt.textContent = humanize(currentLegacyProvider) + ' (not configured)';
    opt.selected = true;
    defaultSel.appendChild(opt);
  }
  defaultSel.addEventListener('change', () => {
    setPath(config, ['agents', 'defaults', 'provider'], defaultSel.value);
    markDirty();
  });
  defaultProvWrap.appendChild(defaultSel);
  defaultProvRow.appendChild(defaultProvWrap);
  card.body.appendChild(defaultProvRow);

  // Chat Provider
  card.body.appendChild(buildProviderSelect(currentChatProvider, currentLegacyProvider, 'chatProvider', 'Chat Provider'));

  // Task Provider
  card.body.appendChild(buildProviderSelect(currentTaskProvider, currentLegacyProvider, 'taskProvider', 'Task Provider'));

  // Render remaining agent defaults (excluding provider fields, model, and timezone)
  const otherFields = Object.fromEntries(
    Object.entries(defaults).filter(([k]) =>
      !['provider', 'chatProvider', 'chat_provider', 'taskProvider', 'task_provider', 'model', 'timezone'].includes(k)
    )
  );
  renderFields(card.body, otherFields, ['agents', 'defaults']);

  // Timezone dropdown
  const tzRow = document.createElement('div');
  tzRow.className = 'field-row';
  const tzLabel = document.createElement('div');
  tzLabel.className = 'field-label';
  tzLabel.textContent = 'Timezone';
  tzRow.appendChild(tzLabel);
  const tzWrap = document.createElement('div');
  tzWrap.className = 'field-input';

  const tzSel = document.createElement('select');
  const tzEmpty = document.createElement('option');
  tzEmpty.value = '';
  tzEmpty.textContent = '— System default —';
  tzSel.appendChild(tzEmpty);

  const commonTimezones = [
    'US/Eastern', 'US/Central', 'US/Mountain', 'US/Pacific', 'US/Hawaii',
    'America/New_York', 'America/Chicago', 'America/Denver', 'America/Los_Angeles',
    'America/Toronto', 'America/Vancouver', 'America/Sao_Paulo', 'America/Mexico_City',
    'Europe/London', 'Europe/Paris', 'Europe/Berlin', 'Europe/Amsterdam', 'Europe/Madrid',
    'Europe/Rome', 'Europe/Moscow', 'Europe/Istanbul',
    'Asia/Tokyo', 'Asia/Shanghai', 'Asia/Hong_Kong', 'Asia/Singapore', 'Asia/Seoul',
    'Asia/Kolkata', 'Asia/Dubai', 'Asia/Bangkok',
    'Australia/Sydney', 'Australia/Melbourne', 'Pacific/Auckland',
    'Africa/Cairo', 'Africa/Lagos', 'Africa/Johannesburg',
    'UTC',
  ];

  const currentTz = (defaults.timezone || '').trim();
  for (const tz of commonTimezones) {
    const opt = document.createElement('option');
    opt.value = tz;
    opt.textContent = tz;
    if (currentTz === tz) opt.selected = true;
    tzSel.appendChild(opt);
  }

  // If user has a custom tz not in the list, add it
  if (currentTz && !commonTimezones.includes(currentTz)) {
    const opt = document.createElement('option');
    opt.value = currentTz;
    opt.textContent = currentTz;
    opt.selected = true;
    tzSel.appendChild(opt);
  }

  tzSel.addEventListener('change', () => {
    setPath(config, ['agents', 'defaults', 'timezone'], tzSel.value);
    markDirty();
  });

  tzWrap.appendChild(tzSel);
  tzRow.appendChild(tzWrap);
  card.body.appendChild(tzRow);
}

function renderTools(data) {
  if (data.web) {
    if (data.web.search) {
      const card = makeCard('Brave Web Search');
      renderFields(card.body, data.web.search, ['tools', 'web', 'search']);
      card.body.querySelectorAll('.field-label').forEach((lbl) => {
        if (lbl.textContent === 'Api Key') lbl.textContent = 'Brave API Key';
      });
    }
  }
  if (data.exec) {
    const card = makeCard('Shell Execution');
    renderFields(card.body, data.exec, ['tools', 'exec']);
  }
}

function renderDashboard(data) {
  const card = makeCard('Dashboard Settings');
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

// ── Tasks ──
function fmtWhen(iso) {
  if (!iso) return '';
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return String(iso);
  }
}

async function fetchTasks() {
  const res = await apiFetch(`${API}/tasks`);
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.error || `Tasks request failed (${res.status})`);
  }
  return await res.json();
}

function renderTasks() {
  const topRow = document.createElement('div');
  topRow.className = 'tasks-toprow';

  const leftControls = document.createElement('div');
  leftControls.className = 'tasks-controls';

  const refreshBtn = document.createElement('button');
  refreshBtn.className = 'btn btn-ghost';
  refreshBtn.textContent = 'Refresh';
  leftControls.appendChild(refreshBtn);

  const autoWrap = document.createElement('label');
  autoWrap.className = 'task-toggle';
  const autoCb = document.createElement('input');
  autoCb.type = 'checkbox';
  autoCb.checked = sessionStorage.getItem(TASKS_AUTOREFRESH_KEY) === '1';
  autoCb.addEventListener('change', () => {
    sessionStorage.setItem(TASKS_AUTOREFRESH_KEY, autoCb.checked ? '1' : '0');
    if (tasksPollTimer) {
      clearInterval(tasksPollTimer);
      tasksPollTimer = null;
    }
    if (autoCb.checked) {
      tasksPollTimer = setInterval(() => {
        if (activeSection !== 'tasks') return;
        doRender({ showLoading: false });
      }, 5000);
      doRender({ showLoading: false });
    }
  });
  const autoText = document.createElement('span');
  autoText.textContent = 'Auto-refresh';
  autoWrap.appendChild(autoCb);
  autoWrap.appendChild(autoText);
  leftControls.appendChild(autoWrap);

  topRow.appendChild(leftControls);

  const hint = document.createElement('div');
  hint.className = 'tasks-hint';
  hint.textContent = 'Tip: cancel stops the worker; you will still get a completion notice.';
  topRow.appendChild(hint);

  contentBody.appendChild(topRow);

  const activeCard = makeCard('Active Tasks');
  const historyCard = makeCard('Task History');

  const activeWrap = document.createElement('div');
  activeWrap.className = 'tasks-list';
  activeCard.body.appendChild(activeWrap);

  const historyWrap = document.createElement('div');
  historyWrap.className = 'tasks-history';
  historyCard.body.appendChild(historyWrap);

  async function doRender(opts = { showLoading: true }) {
    const showLoading = opts && opts.showLoading !== undefined ? !!opts.showLoading : true;

    // Preserve UI state across refreshes.
    const openKeys = new Set();
    historyWrap.querySelectorAll('details.task-disclosure[open]').forEach((d) => {
      const k = d.dataset.key;
      if (k) openKeys.add(k);
    });
    const y = window.scrollY;

    if (showLoading && !activeWrap.childElementCount) {
      activeWrap.innerHTML = '<div class="empty-state">Loading…</div>';
    }
    if (showLoading && !historyWrap.childElementCount) {
      historyWrap.innerHTML = '<div class="empty-state">Loading…</div>';
    }
    try {
      const data = await fetchTasks();
      const active = data.active || [];
      const history = data.history || [];

      // Active
      activeWrap.innerHTML = '';
      if (!active.length) {
        activeWrap.innerHTML = '<div class="empty-state">No active tasks.</div>';
      } else {
        active.forEach((t) => {
          const row = document.createElement('div');
          row.className = 'task-row';

          const left = document.createElement('div');
          left.className = 'task-left';

          const title = document.createElement('div');
          title.className = 'task-title';
          const label = t.label || 'Task';
          const ref = t.reference || '';
          title.textContent = `${label} (${ref})`;
          left.appendChild(title);

          const meta = document.createElement('div');
          meta.className = 'task-meta';
          const status = (t.status || '').toUpperCase();
          const iter = t.iteration || 0;
          const max = t.max_iterations;
          const step = max ? `${iter}/${max}` : `${iter}`;
          const action = (t.current_action || '').trim();
          meta.textContent = `${status} · step ${step}` + (action ? ` · ${action}` : '');
          left.appendChild(meta);

          row.appendChild(left);

          const right = document.createElement('div');
          right.className = 'task-right';

          // Per-task progress toggle
          const toggleWrap = document.createElement('label');
          toggleWrap.className = 'task-toggle';
          const cb = document.createElement('input');
          cb.type = 'checkbox';
          cb.checked = !!t.progress_updates_enabled;
          cb.addEventListener('change', async () => {
            try {
              await apiFetch(`${API}/tasks/${encodeURIComponent(ref)}/progress-updates`, {
                method: 'POST',
                body: JSON.stringify({ enabled: cb.checked }),
              });
              showToast(cb.checked ? 'Progress updates enabled' : 'Progress updates disabled', 'success');
            } catch {
              showToast('Failed to update task setting', 'error');
              cb.checked = !cb.checked;
            }
          });
          const cbText = document.createElement('span');
          cbText.textContent = '30s updates';
          toggleWrap.appendChild(cb);
          toggleWrap.appendChild(cbText);
          right.appendChild(toggleWrap);

          // Cancel button
          const cancelBtn = document.createElement('button');
          cancelBtn.className = 'btn-icon danger';
          cancelBtn.title = 'Cancel task';
          cancelBtn.innerHTML = '<svg width="12" height="12" viewBox="0 0 16 16" fill="none"><path d="M4 4l8 8M12 4l-8 8" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>';
          cancelBtn.addEventListener('click', async () => {
            cancelBtn.disabled = true;
            try {
              const res = await apiFetch(`${API}/tasks/${encodeURIComponent(ref)}/cancel`, { method: 'POST' });
              const out = await res.json();
              if (out.ok) showToast('Cancel requested', 'success');
              else showToast('Cancel failed (may have just finished)', 'error');
              await doRender();
            } catch {
              showToast('Cancel request failed', 'error');
            } finally {
              cancelBtn.disabled = false;
            }
          });
          right.appendChild(cancelBtn);

          row.appendChild(right);
          activeWrap.appendChild(row);
        });
      }

      // History
      historyWrap.innerHTML = '';
      if (!history.length) {
        historyWrap.innerHTML = '<div class="empty-state">No recent tasks yet.</div>';
      } else {
        history.forEach((t) => {
          const d = document.createElement('details');
          d.className = 'task-disclosure';
          const key = t.completion_reference || t.reference || t.id || '';
          d.dataset.key = key;

          const s = document.createElement('summary');
          s.className = 'task-summary';
          const label = t.label || 'Task';
          const status = (t.status || '').toUpperCase();
          const doneRef = t.completion_reference || '';
          const when = fmtWhen(t.completed_at || t.created_at);
          s.textContent = `${label} · ${status}` + (doneRef ? ` · ${doneRef}` : '') + (when ? ` · ${when}` : '');
          d.appendChild(s);

          const body = document.createElement('div');
          body.className = 'task-body';

          const actions = document.createElement('div');
          actions.className = 'task-actions';

          const redeliverBtn = document.createElement('button');
          redeliverBtn.className = 'btn btn-ghost';
          redeliverBtn.textContent = 'Redeliver to Chat';
          redeliverBtn.addEventListener('click', async (ev) => {
            ev.preventDefault();
            ev.stopPropagation();
            redeliverBtn.disabled = true;
            try {
              const ref = t.completion_reference || t.reference || t.id || '';
              await apiFetch(`${API}/tasks/${encodeURIComponent(ref)}/redeliver`, { method: 'POST' });
              showToast('Queued for delivery', 'success');
            } catch {
              showToast('Redeliver failed', 'error');
            } finally {
              redeliverBtn.disabled = false;
            }
          });
          actions.appendChild(redeliverBtn);
          body.appendChild(actions);

          const pre = document.createElement('pre');
          pre.className = 'task-output';
          const out = t.result || t.error || '';
          pre.textContent = out ? String(out).slice(0, 20000) : '(no output captured)';
          body.appendChild(pre);

          d.appendChild(body);
          if (key && openKeys.has(key)) d.open = true;
          historyWrap.appendChild(d);
        });
      }

      // Restore scroll after DOM rebuild.
      window.scrollTo(0, y);
    } catch (e) {
      activeWrap.innerHTML = '<div class="empty-state">Failed to load tasks.</div>';
      historyWrap.innerHTML = '<div class="empty-state">Failed to load tasks.</div>';
      console.error(e);
    }
  }

  refreshBtn.addEventListener('click', () => doRender({ showLoading: true }));

  contentBody.appendChild(activeCard.el);
  contentBody.appendChild(historyCard.el);

  doRender({ showLoading: true });
  // Default: manual refresh only. Auto-refresh is opt-in to avoid collapsing UI.
  if (autoCb.checked) {
    tasksPollTimer = setInterval(() => {
      if (activeSection !== 'tasks') return;
      doRender({ showLoading: false });
    }, 5000);
  }
}

// ── Skills ──
async function fetchSkills() {
  const res = await apiFetch(`${API}/skills`);
  return await res.json();
}

async function searchSkills(q, limit = 10) {
  const res = await apiFetch(`${API}/skills/search?q=${encodeURIComponent(q)}&limit=${encodeURIComponent(String(limit))}`);
  return await res.json();
}

async function installSkill(source, skill = null, replace = false) {
  const payload = { source, replace: !!replace };
  if (skill) payload.skill = skill;
  const res = await apiFetch(`${API}/skills/install`, { method: 'POST', body: JSON.stringify(payload) });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.detail || data.error || 'Install failed');
  }
  return await res.json();
}

async function removeSkill(name) {
  const res = await apiFetch(`${API}/skills/remove/${encodeURIComponent(name)}`, { method: 'POST' });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.detail || data.error || 'Remove failed');
  }
  return await res.json();
}

async function updateAllSkills() {
  const res = await apiFetch(`${API}/skills/update-all`, { method: 'POST', body: JSON.stringify({ replace: true }) });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.detail || data.error || 'Update failed');
  }
  return await res.json();
}

function renderSkills() {
  const topRow = document.createElement('div');
  topRow.className = 'tasks-toprow';

  const leftControls = document.createElement('div');
  leftControls.className = 'tasks-controls';

  const refreshBtn = document.createElement('button');
  refreshBtn.className = 'btn btn-ghost';
  refreshBtn.textContent = 'Refresh';
  leftControls.appendChild(refreshBtn);

  const updateBtn = document.createElement('button');
  updateBtn.className = 'btn btn-ghost';
  updateBtn.textContent = 'Update All';
  leftControls.appendChild(updateBtn);

  const autoWrap = document.createElement('label');
  autoWrap.className = 'task-toggle';
  const autoCb = document.createElement('input');
  autoCb.type = 'checkbox';
  autoCb.checked = sessionStorage.getItem(SKILLS_AUTOREFRESH_KEY) === '1';
  autoCb.addEventListener('change', () => {
    sessionStorage.setItem(SKILLS_AUTOREFRESH_KEY, autoCb.checked ? '1' : '0');
    if (tasksPollTimer) { clearInterval(tasksPollTimer); tasksPollTimer = null; }
    if (autoCb.checked) {
      tasksPollTimer = setInterval(() => {
        if (activeSection !== 'skills') return;
        doRender({ showLoading: false });
      }, 8000);
      doRender({ showLoading: false });
    }
  });
  const autoText = document.createElement('span');
  autoText.textContent = 'Auto-refresh';
  autoWrap.appendChild(autoCb);
  autoWrap.appendChild(autoText);
  leftControls.appendChild(autoWrap);

  topRow.appendChild(leftControls);

  const hint = document.createElement('div');
  hint.className = 'tasks-hint';
  hint.innerHTML = `Search uses <span class="mono">skills.sh</span> and installs into <span class="mono">~/.kyber/skills</span>.`;
  topRow.appendChild(hint);
  contentBody.appendChild(topRow);

  const searchCard = makeCard('Find Skills');
  const searchWrap = document.createElement('div');
  searchWrap.className = 'skills-search';

  // Manual install
  const manualRow = document.createElement('div');
  manualRow.className = 'skills-search-row';
  const srcInp = document.createElement('input');
  srcInp.type = 'text';
  srcInp.placeholder = 'Install from source (owner/repo or GitHub URL)…';
  srcInp.className = 'skills-search-input';
  manualRow.appendChild(srcInp);
  const addBtn = document.createElement('button');
  addBtn.className = 'btn btn-primary';
  addBtn.textContent = 'Install';
  manualRow.appendChild(addBtn);
  searchWrap.appendChild(manualRow);

  const searchRow = document.createElement('div');
  searchRow.className = 'skills-search-row';
  const qInp = document.createElement('input');
  qInp.type = 'text';
  qInp.placeholder = 'Search skills.sh (e.g. “github”, “tmux”, “typescript”)…';
  qInp.className = 'skills-search-input';
  searchRow.appendChild(qInp);
  const searchBtn = document.createElement('button');
  searchBtn.className = 'btn btn-primary';
  searchBtn.textContent = 'Search';
  searchRow.appendChild(searchBtn);
  searchWrap.appendChild(searchRow);

  const resultsWrap = document.createElement('div');
  resultsWrap.className = 'tasks-history';
  searchWrap.appendChild(resultsWrap);
  searchCard.body.appendChild(searchWrap);
  contentBody.appendChild(searchCard.el);

  const installedCard = makeCard('Installed Skills');
  const installedWrap = document.createElement('div');
  installedWrap.className = 'tasks-history';
  installedCard.body.appendChild(installedWrap);
  contentBody.appendChild(installedCard.el);

  async function doSearch() {
    const q = (qInp.value || '').trim();
    if (q.length < 2) { showToast('Type at least 2 characters', 'error'); return; }
    resultsWrap.innerHTML = '<div class="empty-state">Searching…</div>';
    try {
      const data = await searchSkills(q, 10);
      const results = data.results || [];
      resultsWrap.innerHTML = '';
      if (!results.length) {
        resultsWrap.innerHTML = '<div class="empty-state">No results.</div>';
        return;
      }
      results.forEach((r) => {
        const row = document.createElement('div');
        row.className = 'task-row';

        const left = document.createElement('div');
        left.className = 'task-left';
        const title = document.createElement('div');
        title.className = 'task-title';
        title.textContent = r.name || r.id || 'Skill';
        left.appendChild(title);
        const meta = document.createElement('div');
        meta.className = 'task-meta';
        meta.textContent = (r.source ? `${r.source} · ` : '') + `${r.installs || 0} installs`;
        left.appendChild(meta);
        row.appendChild(left);

        const right = document.createElement('div');
        right.className = 'task-right';

        const detailsBtn = document.createElement('button');
        detailsBtn.className = 'btn btn-ghost';
        detailsBtn.textContent = 'Details';
        right.appendChild(detailsBtn);

        const installBtn = document.createElement('button');
        installBtn.className = 'btn btn-primary';
        installBtn.textContent = 'Install';
        installBtn.addEventListener('click', async () => {
          installBtn.disabled = true;
          try {
            const src = (r.source || r.id || '').trim();
            if (!src) throw new Error('No source for this result');
            const skillId = (r.skill_id || r.skillId || '').trim();
            if (!skillId) throw new Error('No skillId for this result');
            const out = await installSkill(src, skillId, false);
            const installed = (out.installed || []).join(', ');
            showToast(installed ? `Installed: ${installed}` : 'Nothing installed (already present?)', 'success');
            await doRender({ showLoading: false });
          } catch (e) {
            showToast(e.message || String(e), 'error');
          } finally {
            installBtn.disabled = false;
          }
        });
        right.appendChild(installBtn);
        row.appendChild(right);

        resultsWrap.appendChild(row);

        // Expandable details panel (lazy-loaded preview).
        let panel = null;
        let loaded = false;
        detailsBtn.addEventListener('click', async () => {
          const src = (r.source || r.id || '').trim();
          if (!src) { showToast('No source for this result', 'error'); return; }

          if (panel) {
            panel.remove();
            panel = null;
            detailsBtn.textContent = 'Details';
            return;
          }

          panel = document.createElement('div');
          panel.className = 'skill-preview';
          panel.innerHTML = '<div class="empty-state">Loading details…</div>';
          row.insertAdjacentElement('afterend', panel);
          detailsBtn.textContent = 'Hide';

          if (loaded) return;
          try {
            const skillId = (r.skill_id || r.skillId || (r.id ? String(r.id).split('/').pop() : '') || r.name || '').trim();
            const res = await apiFetch(`${API}/skills/skillmd`, {
              method: 'POST',
              body: JSON.stringify({ source: src, skill: skillId }),
            });
            const data = await res.json();
            if (!res.ok) throw new Error(data.detail || data.error || 'Preview failed');

            const content = (data.skill && data.skill.content) ? String(data.skill.content) : '';
            const where = (data.skill && data.skill.path) ? String(data.skill.path) : '';

            const pre = document.createElement('pre');
            pre.className = 'task-output';
            pre.textContent =
              (data.source ? `source: ${data.source}\n` : '') +
              (data.revision ? `revision: ${data.revision}\n` : '') +
              (where ? `path: ${where}\n` : '') +
              '\n' +
              (content || '(no SKILL.md content)');

            panel.innerHTML = '';
            panel.appendChild(pre);
            loaded = true;
          } catch (e) {
            panel.innerHTML = '';
            const err = document.createElement('div');
            err.className = 'empty-state';
            err.textContent = 'Failed to load details.';
            panel.appendChild(err);
            console.error(e);
          }
        });
      });
    } catch (e) {
      console.error(e);
      resultsWrap.innerHTML = '<div class="empty-state">Search failed.</div>';
    }
  }

  async function doRender(opts = { showLoading: true }) {
    const showLoading = opts && opts.showLoading !== undefined ? !!opts.showLoading : true;
    if (showLoading) installedWrap.innerHTML = '<div class="empty-state">Loading…</div>';
    try {
      const data = await fetchSkills();
      const skills = data.skills || [];
      installedWrap.innerHTML = '';
      if (!skills.length) {
        installedWrap.innerHTML = '<div class="empty-state">No skills found.</div>';
        return;
      }
      skills.forEach((s) => {
        const d = document.createElement('details');
        d.className = 'task-disclosure';
        const sum = document.createElement('summary');
        sum.className = 'task-summary';
        sum.textContent = `${s.name} · ${s.source}`;
        d.appendChild(sum);

        const body = document.createElement('div');
        body.className = 'task-body';
        const pre = document.createElement('pre');
        pre.className = 'task-output';
        pre.textContent = s.path || '';
        body.appendChild(pre);

        if (s.source === 'managed') {
          const actions = document.createElement('div');
          actions.className = 'skills-actions';
          const rm = document.createElement('button');
          rm.className = 'btn btn-ghost';
          rm.textContent = 'Remove';
          rm.addEventListener('click', async (ev) => {
            ev.preventDefault();
            ev.stopPropagation();
            rm.disabled = true;
            try {
              await removeSkill(s.name);
              showToast(`Removed ${s.name}`, 'success');
              await doRender({ showLoading: false });
            } catch (e) {
              showToast(e.message || String(e), 'error');
            } finally {
              rm.disabled = false;
            }
          });
          actions.appendChild(rm);
          body.appendChild(actions);
        }

        d.appendChild(body);
        installedWrap.appendChild(d);
      });
    } catch (e) {
      console.error(e);
      installedWrap.innerHTML = '<div class="empty-state">Failed to load skills.</div>';
    }
  }

  searchBtn.addEventListener('click', doSearch);
  qInp.addEventListener('keydown', (e) => { if (e.key === 'Enter') doSearch(); });

  addBtn.addEventListener('click', async () => {
    const src = (srcInp.value || '').trim();
    if (!src) { showToast('Enter a source', 'error'); return; }
    addBtn.disabled = true;
    try {
      const out = await installSkill(src, null, false);
      const installed = (out.installed || []).join(', ');
      showToast(installed ? `Installed: ${installed}` : 'Nothing installed (already present?)', 'success');
      srcInp.value = '';
      await doRender({ showLoading: false });
    } catch (e) {
      showToast(e.message || String(e), 'error');
    } finally {
      addBtn.disabled = false;
    }
  });
  srcInp.addEventListener('keydown', (e) => { if (e.key === 'Enter') addBtn.click(); });

  refreshBtn.addEventListener('click', () => doRender({ showLoading: true }));
  updateBtn.addEventListener('click', async () => {
    updateBtn.disabled = true;
    try {
      const out = await updateAllSkills();
      showToast(`Updated: ${(out.updated || []).length}`, 'success');
      await doRender({ showLoading: false });
    } catch (e) {
      showToast(e.message || String(e), 'error');
    } finally {
      updateBtn.disabled = false;
    }
  });

  doRender({ showLoading: true });
  if (autoCb.checked) {
    tasksPollTimer = setInterval(() => {
      if (activeSection !== 'skills') return;
      doRender({ showLoading: false });
    }, 8000);
  }
}

// ── Cron Jobs ──
const CRON_API = '/api/cron';

async function fetchCronJobs() {
  const res = await apiFetch(`${CRON_API}/jobs`);
  return await res.json();
}

function cronHumanSchedule(sched) {
  if (!sched) return 'Unknown';
  if (sched.kind === 'every' && sched.everyMs) {
    const totalSec = Math.round(sched.everyMs / 1000);
    if (totalSec < 60) return `Every ${totalSec} second${totalSec !== 1 ? 's' : ''}`;
    const mins = Math.round(totalSec / 60);
    if (mins < 60) return `Every ${mins} minute${mins !== 1 ? 's' : ''}`;
    const hrs = Math.round(mins / 60);
    if (hrs < 24) return `Every ${hrs} hour${hrs !== 1 ? 's' : ''}`;
    const days = Math.round(hrs / 24);
    return `Every ${days} day${days !== 1 ? 's' : ''}`;
  }
  if (sched.kind === 'cron' && sched.expr) {
    return cronExprToHuman(sched.expr) + (sched.tz ? ` (${sched.tz})` : '');
  }
  if (sched.kind === 'at' && sched.atMs) {
    try { return 'Once at ' + new Date(sched.atMs).toLocaleString(); }
    catch { return 'One-time'; }
  }
  return 'Unknown';
}

function cronExprToHuman(expr) {
  // Simple human-readable translation for common cron patterns
  const parts = (expr || '').trim().split(/\s+/);
  if (parts.length < 5) return expr;
  const [min, hr, dom, mon, dow] = parts;

  // Every minute
  if (min === '*' && hr === '*' && dom === '*' && mon === '*' && dow === '*') return 'Every minute';
  // Every N minutes
  if (min.startsWith('*/') && hr === '*' && dom === '*') return `Every ${min.slice(2)} minutes`;
  // Every hour at :MM
  if (!min.includes('*') && !min.includes('/') && hr === '*' && dom === '*') return `Every hour at :${min.padStart(2, '0')}`;
  // Every N hours
  if (min === '0' && hr.startsWith('*/') && dom === '*') return `Every ${hr.slice(2)} hours`;
  // Daily at HH:MM
  if (!min.includes('*') && !hr.includes('*') && dom === '*' && mon === '*' && dow === '*') {
    return `Daily at ${hr.padStart(2, '0')}:${min.padStart(2, '0')}`;
  }
  // Weekdays at HH:MM
  if (!min.includes('*') && !hr.includes('*') && dom === '*' && mon === '*' && dow === '1-5') {
    return `Weekdays at ${hr.padStart(2, '0')}:${min.padStart(2, '0')}`;
  }
  // Weekly
  const dayNames = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
  if (!min.includes('*') && !hr.includes('*') && dom === '*' && mon === '*' && /^\d$/.test(dow)) {
    return `${dayNames[+dow] || dow} at ${hr.padStart(2, '0')}:${min.padStart(2, '0')}`;
  }
  return expr;
}

function cronFmtTime(ms) {
  if (!ms) return '—';
  try { return new Date(ms).toLocaleString(); }
  catch { return '—'; }
}

function renderCron() {
  // Top controls
  const topRow = document.createElement('div');
  topRow.className = 'tasks-toprow';
  const leftControls = document.createElement('div');
  leftControls.className = 'tasks-controls';

  const refreshBtn = document.createElement('button');
  refreshBtn.className = 'btn btn-ghost';
  refreshBtn.textContent = 'Refresh';
  leftControls.appendChild(refreshBtn);

  const newBtn = document.createElement('button');
  newBtn.className = 'btn btn-primary';
  newBtn.textContent = '+ New Job';
  leftControls.appendChild(newBtn);

  topRow.appendChild(leftControls);
  contentBody.appendChild(topRow);

  // Job list card
  const listCard = makeCard('Scheduled Jobs');
  const listWrap = document.createElement('div');
  listWrap.className = 'tasks-history';
  listCard.body.appendChild(listWrap);

  // Editor card (hidden by default)
  const editorCard = document.createElement('div');
  editorCard.className = 'card hidden';
  editorCard.id = 'cronEditor';
  const editorHeader = document.createElement('div');
  editorHeader.className = 'card-header';
  editorHeader.innerHTML = '<span class="card-title" id="cronEditorTitle">New Job</span>';
  const editorCloseBtn = document.createElement('button');
  editorCloseBtn.className = 'btn-icon';
  editorCloseBtn.innerHTML = '<svg width="12" height="12" viewBox="0 0 16 16" fill="none"><path d="M4 4l8 8M12 4l-8 8" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>';
  editorHeader.appendChild(editorCloseBtn);
  editorCard.appendChild(editorHeader);
  const editorBody = document.createElement('div');
  editorBody.className = 'card-body';
  editorCard.appendChild(editorBody);

  // State for editor
  let editingJobId = null;

  function buildEditor(job) {
    editingJobId = job ? job.id : null;
    editorCard.classList.remove('hidden');
    editorHeader.querySelector('.card-title').textContent = job ? `Edit: ${job.name}` : 'New Job';
    editorBody.innerHTML = '';

    const name = job ? job.name : '';
    const message = job ? (job.payload ? job.payload.message : '') : '';
    const schedKind = job ? job.schedule.kind : 'every';
    const everyMs = job && job.schedule.everyMs ? job.schedule.everyMs : 3600000;
    const cronExpr = job && job.schedule.expr ? job.schedule.expr : '';
    const cronTz = job && job.schedule.tz ? job.schedule.tz : '';
    const atMs = job && job.schedule.atMs ? job.schedule.atMs : null;
    const deliver = job && job.payload ? job.payload.deliver : false;
    const channel = job && job.payload ? (job.payload.channel || '') : '';
    const to = job && job.payload ? (job.payload.to || '') : '';
    const deleteAfterRun = job ? !!job.deleteAfterRun : false;

    // Name
    const nameRow = document.createElement('div');
    nameRow.className = 'field-row';
    nameRow.innerHTML = '<div class="field-label">Name</div>';
    const nameWrap = document.createElement('div');
    nameWrap.className = 'field-input';
    const nameInp = document.createElement('input');
    nameInp.type = 'text';
    nameInp.value = name;
    nameInp.placeholder = 'e.g. Daily summary, Hourly check-in';
    nameWrap.appendChild(nameInp);
    nameRow.appendChild(nameWrap);
    editorBody.appendChild(nameRow);

    // Message
    const msgRow = document.createElement('div');
    msgRow.className = 'field-row';
    msgRow.style.alignItems = 'flex-start';
    msgRow.innerHTML = '<div class="field-label" style="padding-top:8px">Message</div>';
    const msgWrap = document.createElement('div');
    msgWrap.className = 'field-input';
    const msgInp = document.createElement('textarea');
    msgInp.value = message;
    msgInp.placeholder = 'What should the agent do when this job runs?';
    msgWrap.appendChild(msgInp);
    msgRow.appendChild(msgWrap);
    editorBody.appendChild(msgRow);

    // Schedule type
    const typeRow = document.createElement('div');
    typeRow.className = 'field-row';
    typeRow.innerHTML = '<div class="field-label">Schedule Type</div>';
    const typeWrap = document.createElement('div');
    typeWrap.className = 'field-input';
    const typeSel = document.createElement('select');
    [['every', 'Repeating interval'], ['cron', 'Cron expression'], ['at', 'One-time']].forEach(([v, t]) => {
      const opt = document.createElement('option');
      opt.value = v;
      opt.textContent = t;
      if (v === schedKind) opt.selected = true;
      typeSel.appendChild(opt);
    });
    typeWrap.appendChild(typeSel);
    typeRow.appendChild(typeWrap);
    editorBody.appendChild(typeRow);

    // Schedule details container
    const schedDetails = document.createElement('div');
    editorBody.appendChild(schedDetails);

    function renderScheduleFields() {
      schedDetails.innerHTML = '';
      const kind = typeSel.value;

      if (kind === 'every') {
        // Interval with human-friendly unit selector
        const row = document.createElement('div');
        row.className = 'field-row';
        row.innerHTML = '<div class="field-label">Run every</div>';
        const wrap = document.createElement('div');
        wrap.className = 'field-input';
        wrap.style.display = 'flex';
        wrap.style.gap = '8px';

        const numInp = document.createElement('input');
        numInp.type = 'number';
        numInp.min = '1';
        numInp.style.width = '80px';
        numInp.style.flex = '0 0 80px';

        const unitSel = document.createElement('select');
        [['60000', 'minutes'], ['3600000', 'hours'], ['86400000', 'days']].forEach(([v, t]) => {
          const opt = document.createElement('option');
          opt.value = v;
          opt.textContent = t;
          unitSel.appendChild(opt);
        });

        // Decompose everyMs into value + unit
        if (everyMs >= 86400000 && everyMs % 86400000 === 0) {
          numInp.value = everyMs / 86400000;
          unitSel.value = '86400000';
        } else if (everyMs >= 3600000 && everyMs % 3600000 === 0) {
          numInp.value = everyMs / 3600000;
          unitSel.value = '3600000';
        } else {
          numInp.value = Math.max(1, Math.round(everyMs / 60000));
          unitSel.value = '60000';
        }

        wrap.appendChild(numInp);
        wrap.appendChild(unitSel);
        row.appendChild(wrap);
        schedDetails.appendChild(row);
      }

      if (kind === 'cron') {
        const row = document.createElement('div');
        row.className = 'field-row';
        row.innerHTML = '<div class="field-label">Cron Expression</div>';
        const wrap = document.createElement('div');
        wrap.className = 'field-input';
        const inp = document.createElement('input');
        inp.type = 'text';
        inp.value = cronExpr;
        inp.placeholder = '0 9 * * *  (min hr day month weekday)';
        inp.id = 'cronExprInput';

        const hint = document.createElement('div');
        hint.className = 'cron-hint';
        hint.style.fontSize = '12px';
        hint.style.color = 'var(--text-tertiary)';
        hint.style.marginTop = '6px';
        hint.textContent = cronExpr ? cronExprToHuman(cronExpr) : 'e.g. 0 9 * * * = Daily at 09:00';
        inp.addEventListener('input', () => {
          hint.textContent = inp.value.trim() ? cronExprToHuman(inp.value.trim()) : '';
        });

        wrap.appendChild(inp);
        wrap.appendChild(hint);
        row.appendChild(wrap);
        schedDetails.appendChild(row);

        // Timezone
        const tzRow = document.createElement('div');
        tzRow.className = 'field-row';
        tzRow.innerHTML = '<div class="field-label">Timezone</div>';
        const tzWrap = document.createElement('div');
        tzWrap.className = 'field-input';
        const tzInp = document.createElement('input');
        tzInp.type = 'text';
        tzInp.value = cronTz;
        tzInp.placeholder = 'e.g. US/Eastern (leave blank for system default)';
        tzInp.id = 'cronTzInput';
        tzWrap.appendChild(tzInp);
        tzRow.appendChild(tzWrap);
        schedDetails.appendChild(tzRow);
      }

      if (kind === 'at') {
        const row = document.createElement('div');
        row.className = 'field-row';
        row.innerHTML = '<div class="field-label">Run at</div>';
        const wrap = document.createElement('div');
        wrap.className = 'field-input';
        const inp = document.createElement('input');
        inp.type = 'datetime-local';
        inp.id = 'cronAtInput';
        if (atMs) {
          const d = new Date(atMs);
          // Format for datetime-local: YYYY-MM-DDTHH:MM
          const pad = (n) => String(n).padStart(2, '0');
          inp.value = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
        }
        wrap.appendChild(inp);
        row.appendChild(wrap);
        schedDetails.appendChild(row);

        // Delete after run
        const delRow = document.createElement('div');
        delRow.className = 'field-row';
        delRow.innerHTML = '<div class="field-label">Auto-delete</div>';
        const delWrap = document.createElement('div');
        delWrap.className = 'field-input';
        const delChk = document.createElement('div');
        delChk.className = 'checkbox-wrap';
        const delCb = document.createElement('input');
        delCb.type = 'checkbox';
        delCb.checked = deleteAfterRun;
        delCb.id = 'cronDeleteAfterRun';
        const delLbl = document.createElement('label');
        delLbl.className = 'checkbox-label';
        delLbl.htmlFor = 'cronDeleteAfterRun';
        delLbl.textContent = 'Remove job after it runs';
        delChk.appendChild(delCb);
        delChk.appendChild(delLbl);
        delWrap.appendChild(delChk);
        delRow.appendChild(delWrap);
        schedDetails.appendChild(delRow);
      }
    }

    typeSel.addEventListener('change', renderScheduleFields);
    renderScheduleFields();

    // Delivery section
    const deliverRow = document.createElement('div');
    deliverRow.className = 'field-row';
    deliverRow.innerHTML = '<div class="field-label">Deliver result</div>';
    const deliverWrap = document.createElement('div');
    deliverWrap.className = 'field-input';
    const deliverChk = document.createElement('div');
    deliverChk.className = 'checkbox-wrap';
    const deliverCb = document.createElement('input');
    deliverCb.type = 'checkbox';
    deliverCb.checked = deliver;
    deliverCb.id = 'cronDeliver';
    const deliverLbl = document.createElement('label');
    deliverLbl.className = 'checkbox-label';
    deliverLbl.htmlFor = 'cronDeliver';
    deliverLbl.textContent = 'Send response to a channel';
    deliverChk.appendChild(deliverCb);
    deliverChk.appendChild(deliverLbl);
    deliverWrap.appendChild(deliverChk);
    deliverRow.appendChild(deliverWrap);
    editorBody.appendChild(deliverRow);

    const deliveryDetails = document.createElement('div');
    editorBody.appendChild(deliveryDetails);

    function renderDeliveryFields() {
      deliveryDetails.innerHTML = '';
      if (!deliverCb.checked) return;

      const chRow = document.createElement('div');
      chRow.className = 'field-row';
      chRow.innerHTML = '<div class="field-label">Channel</div>';
      const chWrap = document.createElement('div');
      chWrap.className = 'field-input';
      const chSel = document.createElement('select');
      [['', '— Select —'], ['whatsapp', 'WhatsApp'], ['telegram', 'Telegram'], ['discord', 'Discord']].forEach(([v, t]) => {
        const opt = document.createElement('option');
        opt.value = v;
        opt.textContent = t;
        if (v === channel) opt.selected = true;
        chSel.appendChild(opt);
      });
      chSel.id = 'cronChannel';
      chWrap.appendChild(chSel);
      chRow.appendChild(chWrap);
      deliveryDetails.appendChild(chRow);

      const toRow = document.createElement('div');
      toRow.className = 'field-row';
      toRow.innerHTML = '<div class="field-label">Recipient</div>';
      const toWrap = document.createElement('div');
      toWrap.className = 'field-input';
      const toInp = document.createElement('input');
      toInp.type = 'text';
      toInp.value = to;
      toInp.placeholder = 'e.g. phone number, chat ID';
      toInp.id = 'cronTo';
      toWrap.appendChild(toInp);
      toRow.appendChild(toWrap);
      deliveryDetails.appendChild(toRow);
    }

    deliverCb.addEventListener('change', () => {
      deliverLbl.textContent = deliverCb.checked ? 'Send response to a channel' : 'Send response to a channel';
      renderDeliveryFields();
    });
    renderDeliveryFields();

    // Save / Cancel buttons
    const btnRow = document.createElement('div');
    btnRow.style.display = 'flex';
    btnRow.style.gap = '10px';
    btnRow.style.marginTop = '16px';

    const saveJobBtn = document.createElement('button');
    saveJobBtn.className = 'btn btn-primary';
    saveJobBtn.textContent = job ? 'Save Changes' : 'Create Job';
    btnRow.appendChild(saveJobBtn);

    const cancelBtn = document.createElement('button');
    cancelBtn.className = 'btn btn-ghost';
    cancelBtn.textContent = 'Cancel';
    cancelBtn.addEventListener('click', () => {
      editorCard.classList.add('hidden');
      editingJobId = null;
    });
    btnRow.appendChild(cancelBtn);

    editorBody.appendChild(btnRow);

    // Save handler
    saveJobBtn.addEventListener('click', async () => {
      const n = nameInp.value.trim();
      const m = msgInp.value.trim();
      if (!n) { showToast('Name is required', 'error'); return; }
      if (!m) { showToast('Message is required', 'error'); return; }

      const kind = typeSel.value;
      const schedule = { kind };

      if (kind === 'every') {
        const numEl = schedDetails.querySelector('input[type="number"]');
        const unitEl = schedDetails.querySelector('select');
        const val = parseInt(numEl.value) || 1;
        const unit = parseInt(unitEl.value) || 3600000;
        schedule.everyMs = val * unit;
      } else if (kind === 'cron') {
        const exprEl = document.getElementById('cronExprInput');
        const tzEl = document.getElementById('cronTzInput');
        if (!exprEl.value.trim()) { showToast('Cron expression is required', 'error'); return; }
        schedule.expr = exprEl.value.trim();
        if (tzEl.value.trim()) schedule.tz = tzEl.value.trim();
      } else if (kind === 'at') {
        const atEl = document.getElementById('cronAtInput');
        if (!atEl.value) { showToast('Date/time is required', 'error'); return; }
        schedule.atMs = new Date(atEl.value).getTime();
      }

      const payload = {
        name: n,
        message: m,
        schedule,
        deliver: deliverCb.checked,
      };

      if (deliverCb.checked) {
        const chEl = document.getElementById('cronChannel');
        const toEl = document.getElementById('cronTo');
        if (chEl) payload.channel = chEl.value;
        if (toEl) payload.to = toEl.value;
      }

      if (kind === 'at') {
        const darEl = document.getElementById('cronDeleteAfterRun');
        if (darEl) payload.deleteAfterRun = darEl.checked;
      }

      saveJobBtn.disabled = true;
      try {
        if (editingJobId) {
          await apiFetch(`${CRON_API}/jobs/${encodeURIComponent(editingJobId)}`, {
            method: 'PUT',
            body: JSON.stringify(payload),
          });
          showToast('Job updated', 'success');
        } else {
          await apiFetch(`${CRON_API}/jobs`, {
            method: 'POST',
            body: JSON.stringify(payload),
          });
          showToast('Job created', 'success');
        }
        editorCard.classList.add('hidden');
        editingJobId = null;
        await doRender({ showLoading: false });
      } catch (e) {
        showToast('Failed to save job', 'error');
      } finally {
        saveJobBtn.disabled = false;
      }
    });

    // Scroll editor into view
    requestAnimationFrame(() => editorCard.scrollIntoView({ behavior: 'smooth', block: 'start' }));
  }

  editorCloseBtn.addEventListener('click', () => {
    editorCard.classList.add('hidden');
    editingJobId = null;
  });

  newBtn.addEventListener('click', () => buildEditor(null));

  async function doRender(opts = { showLoading: true }) {
    const showLoading = opts && opts.showLoading !== undefined ? !!opts.showLoading : true;
    if (showLoading && !listWrap.childElementCount) {
      listWrap.innerHTML = '<div class="empty-state">Loading…</div>';
    }
    try {
      const data = await fetchCronJobs();
      const jobs = data.jobs || [];
      listWrap.innerHTML = '';

      if (!jobs.length) {
        listWrap.innerHTML = '<div class="empty-state">No cron jobs yet. Click "+ New Job" to create one.</div>';
        return;
      }

      jobs.forEach((job) => {
        const row = document.createElement('div');
        row.className = 'task-row';
        row.style.flexWrap = 'wrap';

        const left = document.createElement('div');
        left.className = 'task-left';
        left.style.flex = '1';
        left.style.minWidth = '200px';

        const title = document.createElement('div');
        title.className = 'task-title';
        title.textContent = job.name;
        left.appendChild(title);

        const meta = document.createElement('div');
        meta.className = 'task-meta';
        const schedText = cronHumanSchedule(job.schedule);
        const statusBit = job.enabled ? '🟢 Active' : '⏸ Paused';
        const nextRun = job.state && job.state.nextRunAtMs ? `Next: ${cronFmtTime(job.state.nextRunAtMs)}` : '';
        const lastStatus = job.state && job.state.lastStatus ? `Last: ${job.state.lastStatus}` : '';
        meta.textContent = [statusBit, schedText, nextRun, lastStatus].filter(Boolean).join(' · ');
        left.appendChild(meta);

        if (job.payload && job.payload.message) {
          const msgPreview = document.createElement('div');
          msgPreview.className = 'task-meta';
          msgPreview.style.fontStyle = 'italic';
          msgPreview.style.marginTop = '2px';
          const msg = job.payload.message;
          msgPreview.textContent = msg.length > 80 ? msg.slice(0, 80) + '…' : msg;
          left.appendChild(msgPreview);
        }

        row.appendChild(left);

        const right = document.createElement('div');
        right.className = 'task-right';

        // Toggle enabled
        const toggleWrap = document.createElement('label');
        toggleWrap.className = 'task-toggle';
        const toggleCb = document.createElement('input');
        toggleCb.type = 'checkbox';
        toggleCb.checked = job.enabled;
        toggleCb.addEventListener('change', async () => {
          try {
            await apiFetch(`${CRON_API}/jobs/${encodeURIComponent(job.id)}/toggle`, {
              method: 'POST',
              body: JSON.stringify({ enabled: toggleCb.checked }),
            });
            showToast(toggleCb.checked ? 'Job enabled' : 'Job paused', 'success');
            await doRender({ showLoading: false });
          } catch {
            showToast('Failed to toggle job', 'error');
            toggleCb.checked = !toggleCb.checked;
          }
        });
        const toggleText = document.createElement('span');
        toggleText.textContent = 'Enabled';
        toggleWrap.appendChild(toggleCb);
        toggleWrap.appendChild(toggleText);
        right.appendChild(toggleWrap);

        // Edit button
        const editBtn = document.createElement('button');
        editBtn.className = 'btn-icon';
        editBtn.title = 'Edit job';
        editBtn.innerHTML = '<svg width="12" height="12" viewBox="0 0 16 16" fill="none"><path d="M11.5 1.5l3 3L5 14H2v-3L11.5 1.5Z" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/></svg>';
        editBtn.addEventListener('click', () => buildEditor(job));
        right.appendChild(editBtn);

        // Delete button
        const delBtn = document.createElement('button');
        delBtn.className = 'btn-icon danger';
        delBtn.title = 'Delete job';
        delBtn.innerHTML = '<svg width="12" height="12" viewBox="0 0 16 16" fill="none"><path d="M4 4l8 8M12 4l-8 8" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>';
        delBtn.addEventListener('click', async () => {
          if (!confirm(`Delete job "${job.name}"?`)) return;
          delBtn.disabled = true;
          try {
            await apiFetch(`${CRON_API}/jobs/${encodeURIComponent(job.id)}`, { method: 'DELETE' });
            showToast('Job deleted', 'success');
            await doRender({ showLoading: false });
          } catch {
            showToast('Failed to delete job', 'error');
          } finally {
            delBtn.disabled = false;
          }
        });
        right.appendChild(delBtn);

        row.appendChild(right);
        listWrap.appendChild(row);
      });
    } catch (e) {
      console.error(e);
      listWrap.innerHTML = '<div class="empty-state">Failed to load cron jobs.</div>';
    }
  }

  refreshBtn.addEventListener('click', () => doRender({ showLoading: true }));

  contentBody.appendChild(editorCard);
  contentBody.appendChild(listCard.el);

  doRender({ showLoading: true });
}

// ── Security Center ──

async function fetchSecurityReports() {
  const res = await apiFetch(`${API}/security/reports`);
  return await res.json();
}

async function triggerSecurityScan() {
  const res = await apiFetch(`${API}/security/scan`, { method: 'POST' });
  return await res.json();
}

function securityScoreColor(score) {
  if (score >= 80) return 'var(--green)';
  if (score >= 60) return 'var(--amber)';
  return 'var(--red)';
}

function securityScoreLabel(score) {
  if (score >= 90) return 'Excellent';
  if (score >= 80) return 'Good';
  if (score >= 60) return 'Fair';
  if (score >= 40) return 'Poor';
  return 'Critical';
}

function severityColor(sev) {
  if (sev === 'critical') return 'var(--red)';
  if (sev === 'high') return '#f97316';
  if (sev === 'medium') return 'var(--amber)';
  return 'var(--text-tertiary)';
}

function categoryStatusIcon(status) {
  if (status === 'pass') return '✅';
  if (status === 'warn') return '⚠️';
  if (status === 'fail') return '🚨';
  return '⏭️';
}

function categoryLabel(cat) {
  const labels = {
    network: 'Network & Ports',
    ssh: 'SSH Configuration',
    permissions: 'File Permissions',
    secrets: 'Secrets & Env Vars',
    software: 'Software Updates',
    processes: 'Running Processes',
    firewall: 'Firewall',
    docker: 'Docker & Containers',
    git: 'Git Security',
    kyber: 'Kyber Config',
    malware: 'Malware Scan',
  };
  return labels[cat] || cat;
}

// ── Security scan state (hoisted outside renderSecurity so it persists across tab switches) ──
let _secPollTimer = null;
let _secScanRunning = false;
let _secScanTriggeredAt = 0;
let _secNeedsRefresh = false;  // set when scan finishes while on another tab

function _secStopPolling() {
  if (_secPollTimer) { clearInterval(_secPollTimer); _secPollTimer = null; }
}

function renderSecurity() {

  // Top controls
  const topRow = document.createElement('div');
  topRow.className = 'tasks-toprow';
  const leftControls = document.createElement('div');
  leftControls.className = 'tasks-controls';

  const refreshBtn = document.createElement('button');
  refreshBtn.className = 'btn btn-ghost';
  refreshBtn.textContent = 'Refresh';
  leftControls.appendChild(refreshBtn);

  const scanBtn = document.createElement('button');
  scanBtn.className = 'btn btn-primary';
  scanBtn.textContent = '🛡️ Run Scan Now';
  leftControls.appendChild(scanBtn);

  topRow.appendChild(leftControls);
  contentBody.appendChild(topRow);

  // In-progress banner (hidden by default, shown if scan is active)
  const progressBanner = document.createElement('div');
  progressBanner.className = _secScanRunning ? 'security-progress' : 'security-progress hidden';
  progressBanner.innerHTML = `
    <div class="security-progress-inner">
      <div class="security-progress-spinner"></div>
      <div class="security-progress-text">
        <span class="security-progress-title">Security scan in progress…</span>
        <span class="security-progress-detail" id="secScanDetail">Analyzing your environment</span>
      </div>
      <span class="security-progress-elapsed" id="secScanElapsed"></span>
    </div>
  `;
  contentBody.appendChild(progressBanner);

  // If a scan is already running, restore button state
  if (_secScanRunning) {
    scanBtn.disabled = true;
    scanBtn.textContent = '⏳ Scan Running…';
    scanBtn.classList.add('btn-disabled');
  }

  // Main container
  const container = document.createElement('div');
  container.id = 'securityContainer';
  contentBody.appendChild(container);

  // ── Scan status detection ──

  const SCAN_GRACE_MS = 90000; // 90s grace before hiding banner if no task found

  function _isScanTask(t) {
    const desc = ((t.description || '') + ' ' + (t.label || '')).toLowerCase();
    return desc.includes('security scan') || desc.includes('security-scan') || desc.includes('security audit');
  }

  let _checkingStatus = false; // guard against overlapping polls

  async function checkScanStatus() {
    if (_checkingStatus) return _secScanRunning;
    _checkingStatus = true;
    try {
      const res = await apiFetch(`${API}/tasks`);
      if (!res.ok) {
        return _secScanRunning;
      }
      const data = await res.json();
      const active = (data.active || []).filter(_isScanTask);
      if (active.length > 0) {
        const task = active[0];
        showScanRunning(task);
        return true;
      }
    } catch (_) {
      return _secScanRunning;
    } finally {
      _checkingStatus = false;
    }

    if (_secScanRunning) {
      const elapsed = Date.now() - _secScanTriggeredAt;
      if (_secScanTriggeredAt && elapsed < SCAN_GRACE_MS) {
        const detail = document.getElementById('secScanDetail');
        if (detail) detail.textContent = 'Waiting for agent to start scan…';
        const elapsedEl = document.getElementById('secScanElapsed');
        if (elapsedEl) {
          const s = Math.floor(elapsed / 1000);
          elapsedEl.textContent = `${s}s`;
        }
        return true;
      }
      hideScanRunning();
      _secStopPolling();
      // Auto-refresh reports — only if security tab is active (DOM is live).
      if (activeSection === 'security') {
        try {
          const data = await fetchSecurityReports();
          renderReport(data);
          if (data && data.latest) {
            showToast('Security scan complete — report updated', 'success');
          } else {
            showToast('Scan finished but no report was generated. The agent may have run out of steps. Try running again.', 'error');
          }
        } catch (_) {
          showToast('Scan finished but failed to load results', 'error');
        }
      } else {
        _secNeedsRefresh = true;
        showToast('Security scan complete — switch to Security Center to view results', 'success');
      }
    }
    return false;
  }

  function showScanRunning(task) {
    _secScanRunning = true;
    scanBtn.disabled = true;
    scanBtn.textContent = '⏳ Scan Running…';
    scanBtn.classList.add('btn-disabled');
    progressBanner.classList.remove('hidden');

    const detail = document.getElementById('secScanDetail');
    const elapsed = document.getElementById('secScanElapsed');
    if (detail) {
      const action = task.current_action || 'Analyzing your environment';
      const step = task.iteration ? `Step ${task.iteration}` : '';
      detail.textContent = step ? `${step} — ${action}` : action;
    }
    if (elapsed && task.created_at) {
      const secs = Math.floor((Date.now() - new Date(task.created_at).getTime()) / 1000);
      const m = Math.floor(secs / 60);
      const s = secs % 60;
      elapsed.textContent = m ? `${m}m ${s}s` : `${s}s`;
    }
  }

  function hideScanRunning() {
    _secScanRunning = false;
    _secScanTriggeredAt = 0;
    scanBtn.disabled = false;
    scanBtn.textContent = '🛡️ Run Scan Now';
    scanBtn.classList.remove('btn-disabled');
    progressBanner.classList.add('hidden');
  }

  function startPolling() {
    if (_secPollTimer) return;
    _secPollTimer = setInterval(() => checkScanStatus(), 3000);
  }

  function renderEmpty() {
    container.innerHTML = `
      <div class="security-empty">
        <div class="security-empty-icon">🛡️</div>
        <h3>No Security Reports Yet</h3>
        <p>Run your first security scan to get a comprehensive overview of your environment.</p>
        <p style="font-size:12px;color:var(--text-tertiary);margin-top:8px;">
          Tip: Set up a cron job to run scans automatically twice a day.
        </p>
      </div>
    `;
  }

  function renderReport(data) {
    if (!data || !data.latest) { renderEmpty(); return; }
    const report = data.latest;
    const summary = report.summary || {};
    const score = summary.score ?? 0;
    const findings = report.findings || [];
    const categories = report.categories || {};
    const notes = report.notes || '';
    const ts = report.timestamp ? new Date(report.timestamp).toLocaleString() : 'Unknown';
    const dur = report.duration_seconds ? `${report.duration_seconds}s` : '';

    container.innerHTML = '';

    // Score card
    const scoreCard = document.createElement('div');
    scoreCard.className = 'card';
    scoreCard.innerHTML = `
      <div class="card-body">
        <div class="security-score-row">
          <div class="security-score-ring" style="--score-color: ${securityScoreColor(score)}">
            <svg viewBox="0 0 120 120" class="security-score-svg">
              <circle cx="60" cy="60" r="52" fill="none" stroke="var(--border)" stroke-width="8"/>
              <circle cx="60" cy="60" r="52" fill="none" stroke="${securityScoreColor(score)}" stroke-width="8"
                stroke-dasharray="${(score / 100) * 327} 327"
                stroke-linecap="round" transform="rotate(-90 60 60)"
                style="transition: stroke-dasharray 0.6s ease"/>
            </svg>
            <div class="security-score-value">
              <span class="security-score-num" style="color:${securityScoreColor(score)}">${score}</span>
              <span class="security-score-label">${securityScoreLabel(score)}</span>
            </div>
          </div>
          <div class="security-score-details">
            <div class="security-score-title">Security Score</div>
            <div class="security-score-meta">Last scan: ${ts}${dur ? ' · ' + dur : ''}</div>
            <div class="security-severity-pills">
              ${summary.critical ? `<span class="severity-pill critical">${summary.critical} Critical</span>` : ''}
              ${summary.high ? `<span class="severity-pill high">${summary.high} High</span>` : ''}
              ${summary.medium ? `<span class="severity-pill medium">${summary.medium} Medium</span>` : ''}
              ${summary.low ? `<span class="severity-pill low">${summary.low} Low</span>` : ''}
              ${summary.total_findings === 0 ? '<span class="severity-pill pass">No Issues Found</span>' : ''}
            </div>
          </div>
        </div>
      </div>
    `;
    container.appendChild(scoreCard);

    // Categories grid
    const catCard = document.createElement('div');
    catCard.className = 'card';
    catCard.style.marginTop = '16px';
    const catHeader = document.createElement('div');
    catHeader.className = 'card-header';
    catHeader.innerHTML = '<span class="card-title">Scan Categories</span>';
    catCard.appendChild(catHeader);
    const catBody = document.createElement('div');
    catBody.className = 'card-body';
    catBody.style.padding = '0';

    const catGrid = document.createElement('div');
    catGrid.className = 'security-cat-grid';

    const catOrder = ['network','ssh','permissions','secrets','software','processes','firewall','docker','git','kyber'];
    for (const key of catOrder) {
      const cat = categories[key];
      if (!cat) continue;
      const item = document.createElement('div');
      item.className = 'security-cat-item';
      item.innerHTML = `
        <span class="security-cat-icon">${categoryStatusIcon(cat.status)}</span>
        <span class="security-cat-name">${categoryLabel(key)}</span>
        <span class="security-cat-count">${cat.checked ? (cat.finding_count || 0) + ' findings' : 'Skipped'}</span>
      `;
      catGrid.appendChild(item);
    }
    catBody.appendChild(catGrid);
    catCard.appendChild(catBody);
    container.appendChild(catCard);

    // Malware Scan card (dedicated, between categories and findings)
    const malCat = categories.malware;
    const malFindings = findings.filter(f => f.category === 'malware');
    const malCard = document.createElement('div');
    malCard.className = 'card';
    malCard.style.marginTop = '16px';

    const malHeader = document.createElement('div');
    malHeader.className = 'card-header';
    malHeader.innerHTML = '<span class="card-title">🦠 Malware Scan</span>';
    malCard.appendChild(malHeader);

    const malBody = document.createElement('div');
    malBody.className = 'card-body';

    if (!malCat) {
      // Category not present in report at all — scan predates malware feature or agent didn't run it
      malBody.innerHTML = `
        <div class="malware-status malware-status-skip">
          <div class="malware-status-icon">❓</div>
          <div class="malware-status-info">
            <div class="malware-status-title">Malware Scan Not Run</div>
            <div class="malware-status-desc">
              This scan did not include a malware check. Run a new scan to include ClamAV malware detection.
            </div>
            <div class="malware-status-action">
              If ClamAV is not installed, run <code>kyber setup-clamav</code> first.
            </div>
          </div>
        </div>
      `;
    } else if (!malCat.checked) {
      // Category exists but was skipped — ClamAV not installed
      const notInstalled = malFindings.find(f => f.id === 'MAL-000');
      malBody.innerHTML = `
        <div class="malware-status malware-status-skip">
          <div class="malware-status-icon">⚠️</div>
          <div class="malware-status-info">
            <div class="malware-status-title">ClamAV Not Installed</div>
            <div class="malware-status-desc">
              Malware scanning is disabled. ClamAV is a free, open-source antivirus engine maintained by Cisco Talos
              with over 3.6 million threat signatures.
            </div>
            <div class="malware-status-action">
              Run <code>kyber setup-clamav</code> to install and configure ClamAV automatically.
              Future scans will include full-system malware detection.
            </div>
          </div>
        </div>
      `;
    } else if (malCat.status === 'skip') {
      // Explicitly skipped (e.g. clamscan not found during scan)
      malBody.innerHTML = `
        <div class="malware-status malware-status-skip">
          <div class="malware-status-icon">⚠️</div>
          <div class="malware-status-info">
            <div class="malware-status-title">Malware Scan Skipped</div>
            <div class="malware-status-desc">
              ClamAV was not available during this scan. Install it to enable malware detection.
            </div>
            <div class="malware-status-action">
              Run <code>kyber setup-clamav</code> to install and configure ClamAV automatically.
            </div>
          </div>
        </div>
      `;
    } else if (malCat.finding_count === 0 && malCat.status === 'pass') {
      // Clean scan
      malBody.innerHTML = `
        <div class="malware-status malware-status-clean">
          <div class="malware-status-icon">✅</div>
          <div class="malware-status-info">
            <div class="malware-status-title">No Threats Detected</div>
            <div class="malware-status-desc">
              ClamAV performed a full system scan and found no malware, trojans, viruses, or other threats.
              Threat signatures were updated before scanning.
            </div>
          </div>
        </div>
      `;
    } else {
      // Threats found
      const threatCount = malCat.finding_count;
      malBody.innerHTML = `
        <div class="malware-status malware-status-threat">
          <div class="malware-status-icon">🚨</div>
          <div class="malware-status-info">
            <div class="malware-status-title">${threatCount} Threat${threatCount !== 1 ? 's' : ''} Detected</div>
            <div class="malware-status-desc">
              ClamAV detected potentially malicious files on your system. Review the findings below and take action immediately.
            </div>
          </div>
        </div>
      `;
      // Show malware findings inline in this card
      if (malFindings.length > 0) {
        const malList = document.createElement('div');
        malList.className = 'malware-findings';
        for (const f of malFindings) {
          const row = document.createElement('details');
          row.className = 'security-finding';
          const sum = document.createElement('summary');
          sum.className = 'security-finding-summary';
          sum.innerHTML = `
            <span class="security-finding-sev" style="background:${severityColor(f.severity)}">${f.severity.toUpperCase()}</span>
            <span class="security-finding-id">${f.id || ''}</span>
            <span class="security-finding-title">${f.title}</span>
            <svg class="security-finding-chevron" width="12" height="12" viewBox="0 0 16 16" fill="none"><path d="M6 4l4 4-4 4" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>
          `;
          row.appendChild(sum);
          const body = document.createElement('div');
          body.className = 'security-finding-body';
          body.innerHTML = `
            <p>${f.description || ''}</p>
            ${f.remediation ? `<div class="security-finding-fix"><strong>Fix:</strong> ${f.remediation}</div>` : ''}
            ${f.evidence ? `<pre class="security-finding-evidence">${f.evidence}</pre>` : ''}
          `;
          row.appendChild(body);
          malList.appendChild(row);
        }
        malBody.appendChild(malList);
      }
    }

    malCard.appendChild(malBody);
    container.appendChild(malCard);

    // Findings list (exclude malware findings since they're shown in the dedicated card)
    const nonMalwareFindings = findings.filter(f => f.category !== 'malware');
    if (nonMalwareFindings.length > 0) {
      const findCard = document.createElement('div');
      findCard.className = 'card';
      findCard.style.marginTop = '16px';
      const findHeader = document.createElement('div');
      findHeader.className = 'card-header';
      findHeader.innerHTML = `<span class="card-title">Findings (${nonMalwareFindings.length})</span>`;
      findCard.appendChild(findHeader);
      const findBody = document.createElement('div');
      findBody.className = 'card-body';
      findBody.style.padding = '0';

      // Sort: critical first, then high, medium, low
      const sevOrder = { critical: 0, high: 1, medium: 2, low: 3 };
      const sorted = [...nonMalwareFindings].sort((a, b) => (sevOrder[a.severity] ?? 9) - (sevOrder[b.severity] ?? 9));

      for (const f of sorted) {
        const row = document.createElement('details');
        row.className = 'security-finding';
        const sum = document.createElement('summary');
        sum.className = 'security-finding-summary';
        sum.innerHTML = `
          <span class="security-finding-sev" style="background:${severityColor(f.severity)}">${f.severity.toUpperCase()}</span>
          <span class="security-finding-id">${f.id || ''}</span>
          <span class="security-finding-title">${f.title}</span>
          <svg class="security-finding-chevron" width="12" height="12" viewBox="0 0 16 16" fill="none"><path d="M6 4l4 4-4 4" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>
        `;
        row.appendChild(sum);

        const body = document.createElement('div');
        body.className = 'security-finding-body';
        body.innerHTML = `
          <p>${f.description || ''}</p>
          ${f.remediation ? `<div class="security-finding-fix"><strong>Fix:</strong> ${f.remediation}</div>` : ''}
          ${f.evidence ? `<pre class="security-finding-evidence">${f.evidence}</pre>` : ''}
        `;
        row.appendChild(body);
        findBody.appendChild(row);
      }
      findCard.appendChild(findBody);
      container.appendChild(findCard);
    }

    // AI Notes
    if (notes) {
      const notesCard = document.createElement('div');
      notesCard.className = 'card';
      notesCard.style.marginTop = '16px';
      const notesHeader = document.createElement('div');
      notesHeader.className = 'card-header';
      notesHeader.innerHTML = '<span class="card-title">🤖 AI Notes & Recommendations</span>';
      notesCard.appendChild(notesHeader);
      const notesBody = document.createElement('div');
      notesBody.className = 'card-body';
      notesBody.innerHTML = `<div class="security-notes">${notes.replace(/\n/g, '<br>')}</div>`;
      notesCard.appendChild(notesBody);
      container.appendChild(notesCard);
    }

    // Report history
    if (data.reports && data.reports.length > 1) {
      const histCard = document.createElement('div');
      histCard.className = 'card';
      histCard.style.marginTop = '16px';
      const histHeader = document.createElement('div');
      histHeader.className = 'card-header';
      histHeader.innerHTML = `<span class="card-title">Scan History (${data.reports.length})</span>`;
      histCard.appendChild(histHeader);
      const histBody = document.createElement('div');
      histBody.className = 'card-body';
      histBody.style.padding = '0';

      for (const r of data.reports) {
        const s = r.summary || {};
        const rTs = r.timestamp ? new Date(r.timestamp).toLocaleString() : 'Unknown';
        const rScore = s.score ?? '?';
        const row = document.createElement('div');
        row.className = 'security-hist-row';
        row.innerHTML = `
          <span class="security-hist-score" style="color:${securityScoreColor(rScore)}">${rScore}</span>
          <span class="security-hist-date">${rTs}</span>
          <span class="security-hist-counts">
            ${s.critical ? `<span style="color:var(--red)">${s.critical}C</span>` : ''}
            ${s.high ? `<span style="color:#f97316">${s.high}H</span>` : ''}
            ${s.medium ? `<span style="color:var(--amber)">${s.medium}M</span>` : ''}
            ${s.low ? `<span style="color:var(--text-tertiary)">${s.low}L</span>` : ''}
          </span>
        `;
        histBody.appendChild(row);
      }
      histCard.appendChild(histBody);
      container.appendChild(histCard);
    }
  }

  async function doRender(opts = {}) {
    _secNeedsRefresh = false;  // clear the flag since we're rendering now
    if (opts.showLoading) container.innerHTML = '<div class="empty-state">Loading security reports…</div>';
    try {
      const data = await fetchSecurityReports();
      renderReport(data);
    } catch (e) {
      container.innerHTML = '<div class="empty-state">Failed to load security reports.</div>';
    }
    // Check if a scan is currently running
    try {
      const running = await checkScanStatus();
      if (running) startPolling();
      else _secStopPolling();
    } catch (_) { /* ignore */ }
  }

  scanBtn.addEventListener('click', async () => {
    if (_secScanRunning) return;
    scanBtn.disabled = true;
    scanBtn.textContent = '⏳ Triggering…';
    scanBtn.classList.add('btn-disabled');
    try {
      await triggerSecurityScan();
      showToast('Security scan triggered — the agent is working on it', 'success');
      _secScanRunning = true;
      _secScanTriggeredAt = Date.now();
      progressBanner.classList.remove('hidden');
      document.getElementById('secScanDetail').textContent = 'Waiting for agent to start scan…';
      document.getElementById('secScanElapsed').textContent = '0s';
      startPolling();
    } catch (e) {
      showToast('Failed to trigger scan', 'error');
      scanBtn.disabled = false;
      scanBtn.textContent = '🛡️ Run Scan Now';
      scanBtn.classList.remove('btn-disabled');
    }
  });

  refreshBtn.addEventListener('click', () => doRender({ showLoading: true }));

  doRender({ showLoading: true });
}

// ── Debug (Errors) ──
async function fetchErrors(limit = 200) {
  const res = await apiFetch(`${API}/errors?limit=${encodeURIComponent(String(limit))}`);
  const data = await res.json();
  return data.errors || [];
}

async function clearErrors() {
  const res = await apiFetch(`${API}/errors/clear`, { method: 'POST' });
  return await res.json();
}

function renderDebug() {
  const topRow = document.createElement('div');
  topRow.className = 'tasks-toprow';

  const leftControls = document.createElement('div');
  leftControls.className = 'tasks-controls';

  const refreshBtn = document.createElement('button');
  refreshBtn.className = 'btn btn-ghost';
  refreshBtn.textContent = 'Refresh';
  leftControls.appendChild(refreshBtn);

  const clearBtn = document.createElement('button');
  clearBtn.className = 'btn btn-ghost';
  clearBtn.textContent = 'Clear';
  leftControls.appendChild(clearBtn);

  const autoWrap = document.createElement('label');
  autoWrap.className = 'task-toggle';
  const autoCb = document.createElement('input');
  autoCb.type = 'checkbox';
  autoCb.checked = sessionStorage.getItem(DEBUG_AUTOREFRESH_KEY) === '1';
  autoCb.addEventListener('change', () => {
    sessionStorage.setItem(DEBUG_AUTOREFRESH_KEY, autoCb.checked ? '1' : '0');
    if (tasksPollTimer) {
      clearInterval(tasksPollTimer);
      tasksPollTimer = null;
    }
    if (autoCb.checked) {
      tasksPollTimer = setInterval(() => {
        if (activeSection !== 'debug') return;
        doRender({ showLoading: false });
      }, 5000);
      doRender({ showLoading: false });
    }
  });
  const autoText = document.createElement('span');
  autoText.textContent = 'Auto-refresh';
  autoWrap.appendChild(autoCb);
  autoWrap.appendChild(autoText);
  leftControls.appendChild(autoWrap);

  topRow.appendChild(leftControls);

  const hint = document.createElement('div');
  hint.className = 'tasks-hint';
  hint.textContent = 'Only ERROR-level logs are shown here.';
  topRow.appendChild(hint);

  contentBody.appendChild(topRow);

  const card = makeCard('Gateway Errors');
  const wrap = document.createElement('div');
  wrap.className = 'tasks-history';
  card.body.appendChild(wrap);
  contentBody.appendChild(card.el);

  async function doRender(opts = { showLoading: true }) {
    const showLoading = opts && opts.showLoading !== undefined ? !!opts.showLoading : true;

    const openKeys = new Set();
    wrap.querySelectorAll('details.task-disclosure[open]').forEach((d) => {
      const k = d.dataset.key;
      if (k) openKeys.add(k);
    });
    const y = window.scrollY;

    if (showLoading && !wrap.childElementCount) {
      wrap.innerHTML = '<div class="empty-state">Loading…</div>';
    }

    try {
      const errors = await fetchErrors(250);
      wrap.innerHTML = '';
      if (!errors.length) {
        wrap.innerHTML = '<div class="empty-state">No errors captured.</div>';
      } else {
        errors.forEach((e, idx) => {
          const d = document.createElement('details');
          d.className = 'task-disclosure';
          const key = (e.ts || '') + '|' + (e.where || '') + '|' + String(idx);
          d.dataset.key = key;

          const s = document.createElement('summary');
          s.className = 'task-summary';
          const ts = e.ts ? fmtWhen(e.ts) : '';
          const where = e.where || '';
          const msg = (e.message || '').split('\n')[0];
          s.textContent = `${ts} · ${where}` + (msg ? ` · ${msg}` : '');
          d.appendChild(s);

          const body = document.createElement('div');
          body.className = 'task-body';

          const pre = document.createElement('pre');
          pre.className = 'task-output';
          const detail = [];
          if (e.level) detail.push(`level: ${e.level}`);
          if (e.where) detail.push(`where: ${e.where}`);
          if (e.message) detail.push(`message:\n${e.message}`);
          if (e.exception) detail.push(`exception:\n${e.exception}`);
          pre.textContent = detail.join('\n\n').slice(0, 30000);
          body.appendChild(pre);

          d.appendChild(body);
          if (openKeys.has(key)) d.open = true;
          wrap.appendChild(d);
        });
      }

      window.scrollTo(0, y);
    } catch (err) {
      console.error(err);
      wrap.innerHTML = '<div class="empty-state">Failed to load errors.</div>';
    }
  }

  refreshBtn.addEventListener('click', () => doRender({ showLoading: true }));
  clearBtn.addEventListener('click', async () => {
    try {
      await clearErrors();
      showToast('Cleared error log', 'success');
      await doRender({ showLoading: true });
    } catch {
      showToast('Failed to clear error log', 'error');
    }
  });

  doRender({ showLoading: true });
  if (autoCb.checked) {
    tasksPollTimer = setInterval(() => {
      if (activeSection !== 'debug') return;
      doRender({ showLoading: false });
    }, 5000);
  }
}

// ── Event listeners ──
document.getElementById('sidebarNav').addEventListener('click', (e) => {
  const btn = e.target.closest('.nav-item');
  if (btn && btn.dataset.section) switchSection(btn.dataset.section);
});

saveBtn.addEventListener('click', saveConfig);

restartGwBtn.addEventListener('click', async () => {
  restartGwBtn.disabled = true;
  restartGwBtn.textContent = 'Restarting…';
  try {
    const res = await apiFetch(`${API}/restart-gateway`, { method: 'POST' });
    const data = await res.json();
    if (data.ok) {
      showToast('Gateway restarted', 'success');
    } else {
      showToast('Restart failed: ' + data.message, 'error');
    }
  } catch {
    showToast('Restart request failed', 'error');
  } finally {
    restartGwBtn.disabled = false;
    restartGwBtn.innerHTML = '<svg width="14" height="14" viewBox="0 0 16 16" fill="none"><path d="M2 2v5h5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/><path d="M3.05 10A6 6 0 1 0 4 4.5L2 7" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg> Restart Gateway';
  }
});

restartDashBtn.addEventListener('click', async () => {
  restartDashBtn.disabled = true;
  restartDashBtn.textContent = 'Restarting…';
  try {
    const res = await apiFetch(`${API}/restart-dashboard`, { method: 'POST' });
    const data = await res.json();
    if (data.ok) {
      showToast('Dashboard restarting — page will reload', 'success');
      // Give the service time to restart (1s delay + unload/load), then reload
      setTimeout(() => location.reload(), 4000);
    } else {
      showToast('Restart failed: ' + data.message, 'error');
    }
  } catch {
    showToast('Restart request failed', 'error');
  } finally {
    restartDashBtn.disabled = false;
    restartDashBtn.innerHTML = '<svg width="14" height="14" viewBox="0 0 16 16" fill="none"><path d="M2 2v5h5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/><path d="M3.05 10A6 6 0 1 0 4 4.5L2 7" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg> Restart Dashboard';
  }
});

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

// Persist scroll position for the current section on refresh/navigation.
window.addEventListener('pagehide', () => {
  if (activeSection) setSavedScroll(activeSection, window.scrollY);
});
