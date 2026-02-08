/* ── Kyber Dashboard ── */
const API = '/api';
const TOKEN_KEY = 'kyber_dashboard_token';
const TASKS_AUTOREFRESH_KEY = 'kyber_tasks_autorefresh';
const DEBUG_AUTOREFRESH_KEY = 'kyber_debug_autorefresh';
const SKILLS_AUTOREFRESH_KEY = 'kyber_skills_autorefresh';
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
  const data = await res.json();
  return data;
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
